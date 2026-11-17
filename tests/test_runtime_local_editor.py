"""Integration regressions using fakes only; never load an editor model."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class LocalEditorOwnerTests(unittest.TestCase):
    def test_disabled_and_invalid_manifest_preserve_raw(self):
        service = FakeService()
        owner = LocalEditorOwner(lambda: service)
        for settings, reason in [({}, 'local_editor_disabled'),
                                 ({'local_editor': {'enabled': True, 'manifest_path': ''}},
                                  'local_editor_configuration_invalid')]:
            formatter = owner.formatter(settings)
            self.assertEqual(formatter.format('Raw text', None).text, 'Raw text')
            self.assertEqual(formatter.last_diagnostics['reason'], reason)
        self.assertEqual(service.close_calls, 2)

    def test_enabled_editor_is_owned_and_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text('{}')
            service = FakeService()
            owner = LocalEditorOwner(lambda: service)
            formatter = owner.formatter({'local_editor': {'enabled': True,
                                                          'manifest_path': str(manifest)}})
            self.assertEqual(formatter.format('Raw text', None).text, 'Clean text.')
            self.assertEqual(formatter.last_diagnostics['qualification'], 'experimental_unqualified')
            self.assertTrue(owner.close())
            self.assertTrue(service.closed)
            self.assertEqual(owner.formatter({'local_editor': {'enabled': True,
                                                               'manifest_path': str(manifest)}}).format('Raw', None).text, 'Raw')

    def test_backend_failure_retains_raw_and_diagnostic(self):
        class BrokenBackend:
            def edit(self, **kwargs):
                raise RuntimeError('unavailable')
        formatter = LocalCleanupFormatter(editor=ConstrainedEditor(BrokenBackend()))
        self.assertEqual(formatter.format('Recover me', None).text, 'Recover me')
        self.assertEqual(formatter.last_diagnostics['reason'], 'backend_exception')

    def test_editor_resource_bounds(self):
        for field, value in [('enabled', 1), ('threads', 4),
                             ('request_timeout', 121), ('idle_timeout', False)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_editor_settings({'local_editor': {field: value}})

    def test_unknown_editor_setting_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            validate_editor_settings({'local_editor': {'request_timout': 10}})

    def test_close_owner_lock_timeout_retains_service_and_publishes_closed(self):
        test = self
        class ContendedLock:
            def acquire(self, timeout):
                test.assertTrue(0 <= timeout <= .01)
                return False
        service = FakeService()
        owner = LocalEditorOwner(lambda: service)
        owner._lock = ContendedLock()
        self.assertIs(owner.close(.01), False)
        self.assertTrue(owner._closed)
        self.assertIs(owner._service, service)
        self.assertEqual(service.close_calls, 0)

    def test_close_passes_only_remaining_deadline_budget(self):
        import services.runtime.local_editor as integration
        ticks = iter([10, 10.5, 11])
        test = self
        class BudgetService(FakeService):
            def close(self, timeout_seconds):
                test.assertEqual(timeout_seconds, 1)
                return super().close(timeout_seconds)
        service = BudgetService()
        owner = LocalEditorOwner(lambda: service)
        with patch.object(integration, 'monotonic', lambda: next(ticks)):
            self.assertTrue(owner.close(2))

    def test_formatter_keeps_bounded_whole_vocabulary_terms(self):
        test = self
        class CapturingBackend:
            def edit(self, **kwargs):
                payload = json.loads(kwargs['user_payload'])
                test.assertEqual(payload['vocabulary'], ['  Keep spaces  ', 'Égyptien', *['term'] * 126])
                test.assertEqual(payload['protected_literals'], [])
                return json.dumps({'edited_text': 'Raw text'})
        formatter = LocalCleanupFormatter(editor=ConstrainedEditor(CapturingBackend()))
        hints = [None, '', ' ', 'x' * 129, '\ud800', '  Keep spaces  ', 'Égyptien', *['term'] * 200]
        self.assertEqual(formatter.format('Raw text', None, vocabulary_hints=hints).text, 'Raw text')
        self.assertEqual(formatter.last_diagnostics['outcome'], 'unchanged')


if __name__ == '__main__':
    unittest.main()
