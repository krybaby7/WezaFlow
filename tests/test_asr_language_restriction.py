"""Language restriction and worker decode options, using fakes only."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from services.asr.local_artifacts import REQUIRED_FILES
from services.asr.transcriber import FasterWhisperBackend
from services.asr.worker import AsrWorkerConfig, AsrWorkerService


class FakeModel:
    def __init__(self, probabilities=()):
        self.probabilities = list(probabilities)
        self.detect_calls = []
        self.transcribe_calls = []

    def detect_language(self, audio):
        self.detect_calls.append(audio)
        top = max(self.probabilities, key=lambda item: item[1]) if self.probabilities else (None, 0)
        return top[0], top[1], self.probabilities

    def transcribe(self, audio, **kwargs):
        self.transcribe_calls.append((audio, kwargs))
        return [], None


def backend(model, **kwargs):
    instance = FasterWhisperBackend('fixture', **kwargs)
    instance._model = model
    return instance


class BackendLanguageTests(unittest.TestCase):
    def setUp(self):
        import numpy
        self.audio = numpy.zeros(16, dtype=numpy.float32)

    def test_without_allowed_list_call_is_unchanged(self):
        model = FakeModel([('de', .9)])
        backend(model).transcribe(self.audio)
        self.assertEqual(model.detect_calls, [])
        (audio, kwargs), = model.transcribe_calls
        self.assertIs(audio, self.audio)
        self.assertEqual(kwargs, {'language': None, 'initial_prompt': None})

    def test_picks_most_probable_allowed_language(self):
        model = FakeModel([('de', .6), ('fr', .2), ('en', .1), ('ar', .05)])
        backend(model, allowed_languages=('en', 'fr', 'ar')).transcribe(self.audio, initial_prompt='hi')
        self.assertEqual(len(model.detect_calls), 1)
        self.assertEqual(model.transcribe_calls[0][1], {'language': 'fr', 'initial_prompt': 'hi'})

    def test_accepts_token_formatted_codes(self):
        model = FakeModel([('<|es|>', .7), ('<|ar|>', .2), ('<|en|>', .1)])
        backend(model, allowed_languages=['en', 'ar']).transcribe(self.audio)
        self.assertEqual(model.transcribe_calls[0][1]['language'], 'ar')

    def test_falls_back_to_unrestricted_when_no_allowed_code_detected(self):
        model = FakeModel([('de', .9), ('es', .1)])
        backend(model, allowed_languages=('en', 'fr')).transcribe(self.audio)
        self.assertIsNone(model.transcribe_calls[0][1]['language'])

    def test_falls_back_when_detection_is_unavailable(self):
        class NoDetect(FakeModel):
            detect_language = None
        model = NoDetect()
        backend(model, allowed_languages=('en',)).transcribe(self.audio)
        self.assertIsNone(model.transcribe_calls[0][1]['language'])

    def test_explicit_language_always_wins(self):
        model = FakeModel([('fr', .9)])
        backend(model, allowed_languages=('en', 'fr')).transcribe(self.audio, language='en')
        self.assertEqual(model.detect_calls, [])
        self.assertEqual(model.transcribe_calls[0][1]['language'], 'en')

    def test_condition_on_previous_text_is_explicit_option(self):
        model = FakeModel()
        backend(model, condition_on_previous_text=False).transcribe(self.audio)
        self.assertIs(model.transcribe_calls[0][1]['condition_on_previous_text'], False)

    def test_new_options_do_not_reach_model_constructor(self):
        instance = FasterWhisperBackend('fixture', allowed_languages=('en',), condition_on_previous_text=False,
                                        device='cpu', compute_type='int8')
        self.assertEqual(instance.model_kwargs, {'device': 'cpu', 'compute_type': 'int8'})
        self.assertEqual(instance.allowed_languages, ('en',))


class WorkerConfigLanguageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.snapshot = Path(self.directory.name)
        for name in REQUIRED_FILES:
            (self.snapshot / name).write_text('{}')

    def config(self, **kwargs):
        return AsrWorkerConfig(str(self.snapshot), 'cpu', 'int8', 2, **kwargs)

    def test_default_and_valid_languages(self):
        self.assertEqual(self.config().languages, ())
        self.assertEqual(self.config(languages=['en', 'fr', 'ar']).languages, ('en', 'fr', 'ar'))
        self.assertEqual(self.config(languages=('haw',)).languages, ('haw',))

    def test_invalid_languages_rejected(self):
        for value in ('en', ('EN',), ('e',), ('engl',), ('e1',), (1,), ('é',), tuple('ab' for _ in range(9)), None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'worker_languages_invalid'):
                self.config(languages=value)

    def test_languages_participate_in_identity(self):
        service = AsrWorkerService()
        self.addCleanup(service.close)
        base = self.config()
        identity, _ = service._prepare(base)
        self.assertEqual(identity, service._prepare(replace(base))[0])
        self.assertNotEqual(identity, service._prepare(replace(base, languages=('en', 'fr')))[0])


class SettingsLanguageTests(unittest.TestCase):
    def setUp(self):
        from services.config.settings import load_default_settings
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name) / 'model'
        root.mkdir()
        for name in REQUIRED_FILES:
            (root / name).write_text('{}')
        self.settings = load_default_settings()
        self.settings['runtime']['profile'] = 'local-multilingual'
        self.settings['local_asr'].update(snapshot_path=str(root), device='cpu', compute_type='int8')

    def test_default_is_unrestricted(self):
        from services.config.settings import DEFAULT_SETTINGS
        from services.runtime.local_asr import worker_config
        self.assertEqual(DEFAULT_SETTINGS['local_asr']['languages'], [])
        self.assertEqual(worker_config(self.settings).languages, ())
        legacy = {**self.settings, 'local_asr': {k: v for k, v in self.settings['local_asr'].items() if k != 'languages'}}
        self.assertEqual(worker_config(legacy).languages, ())

    def test_languages_carried_through_list_or_frozen_tuple(self):
        from services.runtime.controller import freeze_settings, validate_session_settings
        from services.runtime.local_asr import worker_config
        self.settings['local_asr']['languages'] = ['en', 'fr', 'ar']
        validate_session_settings(self.settings)
        self.assertEqual(worker_config(self.settings).languages, ('en', 'fr', 'ar'))
        self.assertEqual(worker_config(freeze_settings(self.settings)).languages, ('en', 'fr', 'ar'))

    def test_invalid_languages_rejected_by_session_validation(self):
        from services.runtime.controller import validate_session_settings
        for value in ('en', ['EN'], ['en'] * 9, {'en': 1}):
            self.settings['local_asr']['languages'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_session_settings(self.settings)

    def test_languages_round_trip_through_settings_file(self):
        from services.config.settings import load_settings, save_settings
        path = Path(self.directory.name) / 'config' / 'settings.yaml'
        self.settings['local_asr']['languages'] = ['en', 'fr', 'ar']
        save_settings(path, self.settings)
        self.assertEqual(load_settings(path)['local_asr']['languages'], ['en', 'fr', 'ar'])
        self.settings['local_asr']['languages'] = []
        save_settings(path, self.settings)
        self.assertEqual(load_settings(path)['local_asr']['languages'], [])


class FakeConnection:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def poll(self, timeout):
        return True

    def recv_bytes(self, maximum):
        if not self.messages:
            raise EOFError()
        return self.messages.pop(0)

    def send_bytes(self, data):
        import json
        self.sent.append(json.loads(data))

    def close(self):
        pass


class WorkerProcessOptionTests(unittest.TestCase):
    def test_worker_passes_languages_and_disables_conditioning(self):
        from services.asr.worker_process import encode, worker_main
        created = []
        class Backend:
            def __init__(self, model_name, **kwargs):
                created.append((model_name, kwargs))
                self.model = object()
            def transcribe(self, audio, language=None, initial_prompt=None):
                return {'text': '', 'language': language, 'duration_ms': 1}
        with tempfile.TemporaryDirectory() as directory:
            for name in REQUIRED_FILES:
                (Path(directory) / name).write_text('{}')
            config = AsrWorkerConfig(directory, 'cpu', 'int8', 2, languages=('en', 'fr', 'ar'))
            connection = FakeConnection([encode({'op': 'start', 'generation': 'g', 'identity': 'i'})])
            with patch('services.asr.transcriber.FasterWhisperBackend', Backend):
                worker_main(connection, config, 'i', 'g', None)
        self.assertEqual(connection.sent[0]['op'], 'ready')
        (model_name, kwargs), = created
        self.assertEqual(model_name, directory)
        self.assertEqual(kwargs['allowed_languages'], ('en', 'fr', 'ar'))
        self.assertIs(kwargs['condition_on_previous_text'], False)
        self.assertIs(kwargs['local_files_only'], True)


if __name__ == '__main__':
    unittest.main()
