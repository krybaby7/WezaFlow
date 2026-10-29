"""Serialized, side-effect-free dictation control.

Only this controller publishes lifecycle state. One owned worker performs all adapter
calls outside the state lock, including cleanup of cancelled noninterruptible work.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from threading import Condition, Thread
from typing import Any, Callable
from uuid import UUID
import math
from types import MappingProxyType

PROTOCOL_VERSION = 1
ACTIVE_STATES = frozenset({'starting', 'recording', 'draining', 'processing'})


class SerializedSessionController:
    def __init__(self, *, factory: Callable, settings_provider: Callable, engine_id: str):
        self.factory = factory
        self.settings_provider = settings_provider
        self._settings = deepcopy(settings_provider())
        validate_session_settings(self._settings)
        self.engine_id = engine_id
        self._condition = Condition()
        self._state = 'idle'
        self._session_id = None
        self._binding_id = None
        self._mode = 'idle'
        self._language = None
        self._busy = False
        self._cancelled = False
        self._stop = False
        self._closed = False
        self._last_seq = 0
        self._recent = OrderedDict()
        self._result = None
        self._result_session_id = None
        self._error = None
        self._thread = None
        self._active_configuration = None

    def snapshot(self):
        with self._condition:
            return self._snapshot()

    def _snapshot(self):
        return {'status': 'ok', 'protocol_version': PROTOCOL_VERSION,
                'engine_id': self.engine_id, 'state': self._state,
                'session_id': self._session_id, 'binding_id': self._binding_id,
                'mode': self._mode, 'busy': self._busy, 'last_error': self._error,
                'result': deepcopy(self._result), 'result_session_id': self._result_session_id,
                'automatic_insertion': False, 'active_configuration': deepcopy(self._active_configuration)}

    def command(self, action: str, payload: dict):
        # Validate the wire envelope before taking ownership of any resource.
        try:
            if payload.get('engine_id') != self.engine_id or (type(payload.get('protocol_version')) is not int or payload.get('protocol_version') != PROTOCOL_VERSION):
                raise ValueError('engine_mismatch')
            sid = str(UUID(str(payload['session_id'])))
            seq = payload['command_seq']
            if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
                raise ValueError('invalid_sequence')
            binding = payload['binding_id']
            if not isinstance(binding, str) or not binding or len(binding) > 128:
                raise ValueError('invalid_binding')
            mode = payload.get('mode', 'dictation')
            language = payload.get('language')
            if language is not None and not isinstance(language, str):
                raise ValueError('invalid_language')
            if action not in {'start', 'stop', 'cancel'}:
                raise ValueError('invalid_action')
        except (KeyError, TypeError, ValueError) as exc:
            return {**self.snapshot(), 'accepted': False, 'reason': str(exc)}
        fingerprint = (action, sid, binding, mode, language)
        with self._condition:
            def reply(accepted, reason=None):
                return {**self._snapshot(), 'accepted': accepted, 'reason': reason,
                        'command_seq': seq}
            if seq in self._recent:
                old_fingerprint, accepted, reason = self._recent[seq]
                if sid != self._session_id:
                    return reply(False, 'stale_session')
                return reply(accepted, reason) if fingerprint == old_fingerprint else reply(False, 'sequence_conflict')
            if seq <= self._last_seq:
                return reply(False, 'stale_command')
            self._last_seq = seq
            reason = None
            if self._closed:
                reason = 'shutdown'
            elif action == 'start':
                if mode != 'dictation':
                    reason = 'command_editing_unavailable_until_M2'
                elif self._busy:
                    reason = 'busy'
                elif sid == self._session_id:
                    reason = 'stale_session'
                else:
                    # Settings I/O/factory work belongs to worker, never this lock.
                    self._session_id, self._binding_id = sid, binding
                    self._mode = mode
                    self._language = None if language is None or language.strip().lower() in ('', 'auto', 'none') else language.strip()
                    self._state, self._error = 'starting', None
                    self._cancelled = self._stop = False
                    self._busy = True
                    snapshot = freeze_settings(deepcopy(self._settings))
                    self._active_configuration = {
                        'profile': snapshot.get('runtime', {}).get('profile', 'low-impact'),
                        'model': snapshot.get('models', {}).get('whisper'),
                        'cpu_threads': snapshot.get('models', {}).get('whisper_cpu_threads'),
                        'sample_rate': snapshot.get('audio', {}).get('sample_rate', 16000),
                        'blocksize': snapshot.get('audio', {}).get('blocksize', 1024),
                    }
                    self._thread = Thread(target=self._run, args=(snapshot,), name='wezza-session-worker', daemon=True)
                    self._thread.start()
            elif sid != self._session_id:
                reason = 'stale_session'
            elif action == 'stop' and binding != self._binding_id:
                reason = 'binding_mismatch'
            elif not self._busy:
                reason = 'not_active'
            elif action == 'cancel':
                self._cancelled = True
                self._state = 'cancelled'
                self._condition.notify_all()
            else:
                self._stop = True
                if self._state == 'recording':
                    self._state = 'draining'
                self._condition.notify_all()
            self._recent[seq] = (fingerprint, reason is None, reason)
            if len(self._recent) > 128:
                self._recent.popitem(last=False)
            return reply(reason is None, reason)

    def settings_snapshot(self):
        with self._condition:
            return deepcopy(self._settings)

    def update_settings(self, settings):
        snapshot = deepcopy(settings)
        validate_session_settings(snapshot)
        with self._condition:
            self._settings = snapshot

    def _run(self, settings):
        recorder = None
        stopped = False
        try:
            with self._condition:
                if self._cancelled: return
            recorder, pipeline = self.factory(settings)
            with self._condition:
                if self._cancelled: return
            recorder.start()
            with self._condition:
                if not self._cancelled:
                    self._state = 'draining' if self._stop else 'recording'
                while not self._stop and not self._cancelled:
                    self._condition.wait()
                if not self._cancelled: self._state = 'draining'
            stopped = True
            audio = recorder.stop()
            with self._condition:
                if self._cancelled: return
                self._state = 'processing'
            from services.runtime.session import normalize_audio_for_pipeline
            result = pipeline.process_audio(normalize_audio_for_pipeline(audio), language=self._language)
            with self._condition:
                if self._cancelled: return
                if result is None:
                    self._state = 'no_speech'
                else:
                    self._result = result.to_dict() if hasattr(result, 'to_dict') else deepcopy(result)
                    self._result_session_id = self._session_id
                    self._state = 'ready'
        except Exception:
            with self._condition:
                if not self._cancelled:
                    self._state = 'error'
                    # Adapter messages can contain transcript/context; do not expose them.
                    self._error = 'Session adapter failed; check local model and audio configuration.'
        finally:
            if recorder is not None and not stopped:
                try:
                    recorder.stop()
                except Exception:
                    pass
            with self._condition:
                self._busy = False
                self._condition.notify_all()

    def wait_for_worker(self, timeout=None):
        """Bounded join for shutdown and deterministic adapter tests."""
        with self._condition:
            return self._condition.wait_for(lambda: not self._busy, timeout)

    def close(self):
        with self._condition:
            self._closed = self._cancelled = True
            if self._busy: self._state = 'cancelled'
            self._condition.notify_all()


def freeze_settings(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze_settings(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_settings(item) for item in value)
    return value


def validate_session_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError('Settings must be an object')
    for section in ('audio', 'vad', 'models', 'runtime', 'injection'):
        if section in settings and not isinstance(settings[section], dict):
            raise ValueError(f'{section} must be an object')
    audio = settings.get('audio', {})
    if type(audio.get('sample_rate', 16000)) is not int or type(audio.get('channels', 1)) is not int or audio.get('sample_rate', 16000) != 16000 or audio.get('channels', 1) != 1:
        raise ValueError('Only mono 16 kHz capture is supported')
    for section, keys in {'audio': ('blocksize',), 'vad': ('threshold', 'min_speech_duration_ms', 'trim_threshold', 'quiet_threshold', 'quiet_trim_threshold'), 'models': ('whisper_cpu_threads', 'whisper_speculative_cpu_threads')}.items():
        for key in keys:
            value = settings.get(section, {}).get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
                raise ValueError(f'{section}.{key} must be finite and positive')
            if key in ('blocksize', 'whisper_cpu_threads', 'whisper_speculative_cpu_threads') and value is not None and not isinstance(value, int):
                raise ValueError(f'{section}.{key} must be an integer')
    for section, keys in {'vad': ('trim_padding_ms', 'quiet_trim_padding_ms'), 'injection': ('paste_delay_ms',)}.items():
        for key in keys:
            value = settings.get(section, {}).get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{section}.{key} must be finite and nonnegative')
    from services.asr.profiles import resolve_asr_profile
    runtime = settings.get('runtime', {})
    profile = runtime.get('profile', 'low-impact')
    if not isinstance(profile, str): raise ValueError('Invalid profile')
    resolve_asr_profile(profile)
    for key in ('whisper', 'whisper_compute_type', 'ollama'):
        value = settings.get('models', {}).get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f'models.{key} must be a nonempty string')
    language = runtime.get('language')
    if language is not None and not isinstance(language, str): raise ValueError('Invalid language')
