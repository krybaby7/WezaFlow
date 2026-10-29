import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from services.config.settings import load_default_settings
from services.pipeline.models import AsrResult
from services.runtime.api import create_app, RuntimeApiState, _create_session_adapters
from services.runtime.controller import validate_session_settings


class FakeWorkerService:
    def __init__(self):
        self.configs = []
        self.calls = []
        self.closed = 0
        self.reaped = True

    def snapshot(self):
        return {'state': 'STARTING', 'busy': True, 'pid': None, 'generation': None, 'last_error': None}

    def transcriber(self, config):
        self.configs.append(config)
        owner = self
        class Adapter:
            def transcribe(self, audio, **kwargs):
                owner.calls.append(kwargs)
                return AsrResult('Bonjour print("hello.world") 3ayez 7aga.', None, 2000)
        return Adapter()

    def close(self, timeout_seconds=5):
        self.closed += 1
        return self.reaped


class LocalWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / 'model'
        self.snapshot.mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json', 'preprocessor_config.json', 'vocabulary.json'):
            (self.snapshot / name).write_text('{}', encoding='utf-8')

    def settings(self):
        data = load_default_settings()
        data['runtime']['profile'] = 'local-multilingual'
        data['runtime']['language'] = 'auto'
        data['local_asr'].update(snapshot_path=str(self.snapshot), device='cpu', compute_type='int8')
        return data

    def test_local_configuration_validated_before_settings_write(self):
        service = FakeWorkerService()
        app = create_app(root=self.root, engine_id='integration', engine_token='secret', asr_service=service)
        with TestClient(app, headers={'Authorization': 'Bearer secret'}) as client:
            data = self.settings()
            for key, value in [('snapshot_path', ''), ('device', 'cloud'), ('decode_timeout_seconds', 0), ('idle_timeout_seconds', -1), ('cpu_threads', True)]:
                invalid = {**data, 'local_asr': {**data['local_asr'], key: value}}
                response = client.put('/settings', json={'settings': invalid})
                self.assertEqual(response.status_code, 400, (key, response.text))
            response = client.put('/settings', json={'settings': data, 'apply_profile_defaults': True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['settings']['runtime']['language'], 'auto')
            self.assertEqual(service.configs, [])  # Saving never starts native work.
        self.assertGreaterEqual(service.closed, 1)

    def test_shared_service_reaches_pipeline_without_legacy_cleanup(self):
        service = FakeWorkerService()
        state = RuntimeApiState(self.root, self.root / 'log', lambda _: None, asr_service=service)
        data = self.settings()
        validate_session_settings(data)
        with patch('services.asr.audio_capture.AudioRecorder.microphone', return_value=object()):
            for _ in range(2):
                _, pipeline = _create_session_adapters(state, data)
                result = pipeline.process_audio([.2] * 32000, language=None)
                self.assertEqual(result.raw_transcript, result.polished_text)
                self.assertEqual(result.diagnostics['editing']['reason'], 'local_editor_not_ready')
        self.assertEqual(len(service.configs), 2)
        self.assertEqual(len(service.calls), 2)
        self.assertTrue(all(call['language'] is None for call in service.calls))
        self.assertTrue(all(config.snapshot == str(self.snapshot) for config in service.configs))

    def test_status_and_shutdown_report_actual_worker_cleanup(self):
        service = FakeWorkerService()
        app = create_app(root=self.root, engine_id='integration', engine_token='secret', asr_service=service)
        with TestClient(app, headers={'Authorization': 'Bearer secret'}) as client:
            self.assertEqual(client.get('/status').json()['asr_worker']['state'], 'STARTING')
            service.reaped = False
            failed = client.post('/runtime/shutdown')
            self.assertEqual(failed.status_code, 503)
            self.assertFalse(app.state.shutdown_requested.is_set())
            service.reaped = True
            self.assertTrue(client.post('/runtime/shutdown').json()['resources_reaped'])
            self.assertTrue(app.state.shutdown_requested.is_set())
        self.assertGreaterEqual(service.closed, 3)

    def test_existing_profile_does_not_construct_worker_transcriber(self):
        service = FakeWorkerService()
        state = RuntimeApiState(self.root, self.root / 'log', lambda _: None, asr_service=service)
        with patch('services.asr.audio_capture.AudioRecorder.microphone', return_value=object()):
            _create_session_adapters(state, load_default_settings())
        self.assertEqual(service.configs, [])

    def test_real_worker_bridge_accepts_actual_vad_list_output(self):
        from services.asr.worker import AsrWorkerService
        from tests.fixtures.asr_worker_fixture import fixture_worker
        (self.snapshot / 'fixture-mode').write_text('normal', encoding='utf-8')
        service = AsrWorkerService(_worker_target=fixture_worker)
        self.addCleanup(service.close)
        state = RuntimeApiState(self.root, self.root / 'log', lambda _: None, asr_service=service)
        with patch('services.asr.audio_capture.AudioRecorder.microphone', return_value=object()):
            _, pipeline = _create_session_adapters(state, self.settings())
            first = pipeline.process_audio([.2] * 32000, language=None)
            first_pid = service.snapshot()['pid']
            second = pipeline.process_audio([.2] * 32000, language=None)
        self.assertEqual(first.raw_transcript, second.raw_transcript)
        self.assertIsNotNone(first_pid)
        self.assertEqual(service.snapshot()['pid'], first_pid)
