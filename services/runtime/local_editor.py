"""Opt-in, engine-owned experimental cleanup with recoverable raw output."""
from collections.abc import Mapping
import math
from itertools import islice
from threading import RLock
from time import monotonic, perf_counter

from services.config.settings import DEFAULT_SETTINGS
from services.llm.constrained_editor import ConstrainedEditor, EditRequest
from services.llm.local_worker import LocalEditorConfig, LocalEditorService
from services.pipeline.models import FormatResult


def editor_values(settings):
    values = settings.get('local_editor', {})
    if not isinstance(values, Mapping):
        raise ValueError('local_editor must be an object')
    if set(values) - set(DEFAULT_SETTINGS['local_editor']):
        raise ValueError('local_editor contains unsupported settings')
    values = {**DEFAULT_SETTINGS['local_editor'], **values}
    if type(values['enabled']) is not bool:
        raise ValueError('local_editor.enabled must be a boolean')
    return values


def editor_config(settings):
    values = editor_values(settings)
    return LocalEditorConfig(**{key: value for key, value in values.items() if key != 'enabled'})


def validate_editor_settings(settings):
    values = editor_values(settings)
    if not isinstance(values['manifest_path'], str):
        raise ValueError('local_editor.manifest_path must be text')
    for name, cap in (('startup_timeout', 600), ('request_timeout', 120),
                      ('idle_timeout', 3600), ('reap_timeout', 30)):
        value = values[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= cap:
            raise ValueError(f'local_editor.{name} must be positive and at most {cap}')
    for name, expected in (('threads', 2), ('context_tokens', 4096), ('output_tokens', 512)):
        if type(values[name]) is not int or values[name] != expected:
            raise ValueError(f'local_editor.{name} must be {expected}')


class LocalEditorOwner:
    """Retain ownership even when cleanup fails; never adopt an external server."""
    def __init__(self, service_factory=LocalEditorService):
        self._factory = service_factory
        self._service = service_factory()
        self._lock = RLock()
        self._closed = False
        self._reason = 'local_editor_disabled'

    def formatter(self, settings):
        reason = 'local_editor_disabled'
        config = None
        try:
            validate_editor_settings(settings)
            if editor_values(settings)['enabled']:
                config = editor_config(settings)
        except (ValueError, TypeError, OSError):
            reason = 'local_editor_configuration_invalid'
        with self._lock:
            if self._closed:
                return LocalCleanupFormatter(reason='local_editor_closed')
            service = self._service
        # Reaping may take seconds. Keep the owner lock free so shutdown can
        # publish closure and spend its own bounded budget on the same service.
        if config is None:
            if not service.close(timeout_seconds=5):
                reason = 'local_editor_cleanup_incomplete'
            with self._lock:
                self._reason = reason
            return LocalCleanupFormatter(reason=reason)
        replace = service.snapshot()['closed']
        if replace and not service.close(timeout_seconds=5):
            return LocalCleanupFormatter(reason='local_editor_cleanup_incomplete')
        with self._lock:
            if self._closed:
                return LocalCleanupFormatter(reason='local_editor_closed')
            if self._service is not service:
                return LocalCleanupFormatter(reason='local_editor_configuration_changed')
            if replace:
                self._service = self._factory()
            self._reason = 'experimental_unqualified'
            return LocalCleanupFormatter(editor=ConstrainedEditor(self._service.backend(config)))

    def snapshot(self):
        with self._lock:
            service, reason = self._service, self._reason
        return {**service.snapshot(), 'qualification': 'experimental_unqualified',
                'configuration_status': reason}

    def close(self, timeout_seconds=5):
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30:
            raise ValueError('local_deadline_invalid')
        deadline = monotonic() + timeout_seconds
        # Closing the service interrupts startup/editing without waiting for the
        # controller worker to finish; publication remains controller-owned.
        self._closed = True
        if not self._lock.acquire(timeout=max(0, deadline - monotonic())):
            return False
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            return self._service.close(timeout_seconds=remaining)
        finally:
            self._lock.release()


class LocalCleanupFormatter:
    def __init__(self, editor=None, reason='local_editor_disabled'):
        self.editor = editor
        self.reason = reason
        self.last_diagnostics = {'outcome': 'raw_fallback', 'reason': reason,
                                 'qualification': 'experimental_unqualified'}

    def format(self, raw_text, context, *, vocabulary_hints=None):
        started = perf_counter()
        text = raw_text
        outcome, reason = 'raw_fallback', self.reason
        if self.editor is not None:
            try:
                hints = vocabulary_hints if isinstance(vocabulary_hints, (list, tuple)) else ()
                vocabulary = tuple(islice((value for value in hints
                                   if isinstance(value, str) and value.strip()
                                   and len(value) <= 128
                                   and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)), 128))
                result = self.editor.edit(EditRequest(raw=raw_text, vocabulary=vocabulary))
                text, outcome, reason = result.text, result.outcome, result.reason
            except (ValueError, TypeError):
                reason = 'local_editor_request_invalid'
        self.last_diagnostics = {'outcome': outcome, 'reason': reason,
                                 'qualification': 'experimental_unqualified'}
        return FormatResult(text=text, model='local-editor-experimental' if outcome != 'raw_fallback' else 'raw-recovery',
                            duration_ms=round((perf_counter() - started) * 1000))
