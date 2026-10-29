"""Integration regressions using fakes only; never load an editor model."""
import json

import pytest

from services.runtime.local_editor import (
    LocalCleanupFormatter, LocalEditorOwner, validate_editor_settings,
)
from services.llm.constrained_editor import ConstrainedEditor


class FakeService:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def close(self, timeout_seconds):
        self.close_calls += 1
        self.closed = True
        return True

    def snapshot(self):
        return {'closed': self.closed, 'state': 'STOPPED'}

    def backend(self, config):
        return self

    def edit(self, **kwargs):
        return json.dumps({'edited_text': 'Clean text.'})


def test_disabled_and_invalid_manifest_preserve_raw():
    service = FakeService()
    owner = LocalEditorOwner(lambda: service)
    for settings, reason in [({}, 'local_editor_disabled'),
                             ({'local_editor': {'enabled': True, 'manifest_path': ''}},
                              'local_editor_configuration_invalid')]:
        formatter = owner.formatter(settings)
        assert formatter.format('Raw text', None).text == 'Raw text'
        assert formatter.last_diagnostics['reason'] == reason
    assert service.close_calls == 2


def test_enabled_editor_is_owned_and_closed(tmp_path):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text('{}')
    service = FakeService()
    owner = LocalEditorOwner(lambda: service)
    formatter = owner.formatter({'local_editor': {'enabled': True,
                                                  'manifest_path': str(manifest)}})
    assert formatter.format('Raw text', None).text == 'Clean text.'
    assert formatter.last_diagnostics['qualification'] == 'experimental_unqualified'
    assert owner.close()
    assert service.closed
    assert owner.formatter({'local_editor': {'enabled': True,
                                             'manifest_path': str(manifest)}}).format('Raw', None).text == 'Raw'


def test_backend_failure_retains_raw_and_diagnostic():
    class BrokenBackend:
        def edit(self, **kwargs):
            raise RuntimeError('unavailable')
    formatter = LocalCleanupFormatter(editor=ConstrainedEditor(BrokenBackend()))
    assert formatter.format('Recover me', None).text == 'Recover me'
    assert formatter.last_diagnostics['reason'] == 'backend_exception'


@pytest.mark.parametrize('field,value', [('enabled', 1), ('threads', 4),
                                        ('request_timeout', 121), ('idle_timeout', False)])
def test_editor_resource_bounds(field, value):
    with pytest.raises(ValueError):
        validate_editor_settings({'local_editor': {field: value}})


def test_unknown_editor_setting_is_rejected():
    with pytest.raises(ValueError, match='unsupported'):
        validate_editor_settings({'local_editor': {'request_timout': 10}})


def test_close_owner_lock_timeout_retains_service_and_publishes_closed():
    class ContendedLock:
        def acquire(self, timeout):
            assert 0 <= timeout <= .01
            return False
    service = FakeService()
    owner = LocalEditorOwner(lambda: service)
    owner._lock = ContendedLock()
    assert owner.close(.01) is False
    assert owner._closed
    assert owner._service is service
    assert service.close_calls == 0


def test_close_passes_only_remaining_deadline_budget(monkeypatch):
    import services.runtime.local_editor as integration
    ticks = iter([10, 10.5, 11])
    monkeypatch.setattr(integration, 'monotonic', lambda: next(ticks))
    class BudgetService(FakeService):
        def close(self, timeout_seconds):
            assert timeout_seconds == 1
            return super().close(timeout_seconds)
    service = BudgetService()
    owner = LocalEditorOwner(lambda: service)
    assert owner.close(2)


def test_formatter_keeps_bounded_whole_vocabulary_terms():
    class CapturingBackend:
        def edit(self, **kwargs):
            payload = json.loads(kwargs['user_payload'])
            assert payload['vocabulary'] == ['  Keep spaces  ', 'Égyptien', *['term'] * 126]
            assert payload['protected_literals'] == []
            return json.dumps({'edited_text': 'Raw text'})
    formatter = LocalCleanupFormatter(editor=ConstrainedEditor(CapturingBackend()))
    hints = [None, '', ' ', 'x' * 129, '\ud800', '  Keep spaces  ', 'Égyptien', *['term'] * 200]
    assert formatter.format('Raw text', None, vocabulary_hints=hints).text == 'Raw text'
    assert formatter.last_diagnostics['outcome'] == 'unchanged'
