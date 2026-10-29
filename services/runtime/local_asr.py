"""Explicit configuration bridge for the supervised local ASR profile."""
from collections.abc import Mapping

from services.asr.worker import AsrWorkerConfig
from services.config.settings import DEFAULT_SETTINGS
from services.pipeline.models import FormatResult


def worker_config(settings):
    values = settings.get('local_asr', {})
    if not isinstance(values, Mapping):
        raise ValueError('local_asr must be an object')
    values = {**DEFAULT_SETTINGS['local_asr'], **values}
    manifest = values['runtime_manifest_path']
    if not isinstance(manifest, str):
        raise ValueError('local_asr.runtime_manifest_path must be text')
    return AsrWorkerConfig(
        snapshot=values['snapshot_path'], device=values['device'],
        compute_type=values['compute_type'], cpu_threads=values['cpu_threads'],
        runtime_manifest=manifest or None,
        **{name: values[name] for name in ('startup_timeout_seconds', 'decode_timeout_seconds',
                                          'idle_timeout_seconds', 'reap_timeout_seconds')},
    )


class RawRecoveryFormatter:
    """Temporary recovery until the constrained local editor is qualified/wired."""
    last_diagnostics = {'outcome': 'raw_fallback', 'reason': 'local_editor_not_ready'}

    def format(self, raw_text, context, *, vocabulary_hints=None):
        return FormatResult(text=raw_text, model='raw-recovery', duration_ms=0)
