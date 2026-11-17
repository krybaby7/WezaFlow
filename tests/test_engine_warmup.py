"""Model warm-up lifecycle with spawn fixtures and fakes only; never loads a model."""
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from services.asr.local_artifacts import REQUIRED_FILES
from services.asr.worker import AsrWorkerConfig, AsrWorkerError, AsrWorkerService
from tests.fixtures.asr_worker_fixture import fixture_worker


def slow_asr_worker(connection, config, identity, generation, runtime_hash):
    time.sleep(1.5)
    fixture_worker(connection, config, identity, generation, runtime_hash)


def slow_editor_child(connection, gate, config, generation):
    from tests.test_local_editor_worker import fixture_child
    time.sleep(1.5)
    fixture_child(connection, gate, config, generation)


def hung_editor_child(connection, gate, config, generation):
    gate.wait()
    time.sleep(60)


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    return predicate()


def snapshot_dir(root, mode='normal'):
    snapshot = Path(root) / mode
    snapshot.mkdir(exist_ok=True)
    for name in REQUIRED_FILES:
        (snapshot / name).write_text('{}')
    (snapshot / 'fixture-mode').write_text(mode)
    return snapshot


class AsrWarmTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio = np.zeros(160, dtype=np.float32)

    def service(self, target=fixture_worker):
        service = AsrWorkerService(_worker_target=target)
        self.addCleanup(service.close)
        return service

    def config(self, mode='normal', **kwargs):
        kwargs.setdefault('startup_timeout_seconds', 10)
        kwargs.setdefault('decode_timeout_seconds', 3)
        return AsrWorkerConfig(str(snapshot_dir(self.directory.name, mode)), 'cpu', 'int8', 2, **kwargs)

    def warm_in_thread(self, service, config):
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(service.warm(config)))
        thread.start()
        self.addCleanup(thread.join, 15)
        return thread, outcome

    def test_warm_starts_without_decoding_and_decode_reuses_it(self):
        service = self.service()
        config = self.config()
        self.assertTrue(service.warm(config))
        snapshot = service.snapshot()
        self.assertEqual(snapshot['status'], 'READY')
        self.assertTrue(snapshot['owns_process'])
        self.assertFalse((Path(config.snapshot) / 'fixture-decode').exists())
        pid = snapshot['pid']
        self.assertTrue(service.warm(config))
        self.assertEqual(service.snapshot()['pid'], pid)
        self.assertEqual(service.transcriber(config).transcribe(self.audio).text, str(pid))
        self.assertTrue(service.close())
        self.assertFalse(service.snapshot()['owns_process'])

    def test_warm_replaces_worker_for_changed_identity(self):
        service = self.service()
        config = self.config()
        self.assertTrue(service.warm(config))
        first = service.snapshot()
        self.assertTrue(service.warm(replace(config, languages=('en', 'fr'))))
        second = service.snapshot()
        self.assertNotEqual(first['pid'], second['pid'])
        self.assertNotEqual(first['config_identity'], second['config_identity'])

    def test_warm_is_noop_for_zero_idle_closed_or_invalid(self):
        service = self.service()
        self.assertFalse(service.warm(self.config(idle_timeout_seconds=0)))
        self.assertIsNone(service.snapshot()['pid'])
        self.assertFalse(service.warm('not a config'))
        self.assertTrue(service.close())
        self.assertFalse(service.warm(self.config()))
        self.assertFalse(service.snapshot()['owns_process'])

    def test_failed_warm_records_sanitized_error_and_reaps(self):
        service = self.service()
        self.assertFalse(service.warm(self.config('startup_error')))
        snapshot = service.snapshot()
        self.assertEqual(snapshot['last_error'], 'worker_startup_failed')
        self.assertFalse(snapshot['owns_process'])
        self.assertFalse(snapshot['owns_io'])
        self.assertTrue(service.transcriber(self.config()).transcribe(self.audio).text)

    def test_warmed_but_unused_worker_idles_out(self):
        service = self.service()
        self.assertTrue(service.warm(self.config(idle_timeout_seconds=.05)))
        self.assertTrue(wait_until(lambda: not service.snapshot()['owns_process'], 5))
        self.assertEqual(service.snapshot()['state'], 'STOPPED')

    def test_decode_after_warm_reschedules_idle(self):
        service = self.service()
        config = self.config(idle_timeout_seconds=.6)
        self.assertTrue(service.warm(config))
        pid = service.snapshot()['pid']
        time.sleep(.4)
        self.assertEqual(service.transcriber(config).transcribe(self.audio).text, str(pid))
        # The warm-up timer would have fired by now; the decode replaced it.
        time.sleep(.35)
        self.assertEqual(service.snapshot()['pid'], pid)
        self.assertTrue(wait_until(lambda: not service.snapshot()['owns_process'], 5))

    def test_decode_during_warm_waits_and_uses_that_worker(self):
        service = self.service(slow_asr_worker)
        config = self.config()
        thread, outcome = self.warm_in_thread(service, config)
        self.assertTrue(wait_until(lambda: service.snapshot()['status'] == 'STARTING'))
        pid = service.snapshot()['pid']
        result = service.transcriber(config).transcribe(self.audio)
        thread.join(10)
        self.assertEqual(outcome, [True])
        self.assertEqual(result.text, str(pid))
        self.assertEqual(service.snapshot()['pid'], pid)

    def test_second_decode_during_warm_fails_fast(self):
        service = self.service(slow_asr_worker)
        config = self.config()
        thread, outcome = self.warm_in_thread(service, config)
        self.assertTrue(wait_until(lambda: service.snapshot()['status'] == 'STARTING'))
        results, errors = [], []
        def first():
            try:
                results.append(service.transcriber(config).transcribe(self.audio))
            except AsrWorkerError as exc:
                errors.append(str(exc))
        waiter = threading.Thread(target=first)
        waiter.start()
        self.assertTrue(wait_until(lambda: service._warm_waiter))
        started = time.monotonic()
        with self.assertRaisesRegex(AsrWorkerError, 'worker_busy'):
            service.transcriber(config).transcribe(self.audio)
        self.assertLess(time.monotonic() - started, .5)
        waiter.join(10)
        thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(outcome, [True])

    def test_decode_waiting_on_warm_is_bounded(self):
        service = self.service()
        self.assertTrue(service._claim('warm'))
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(AsrWorkerError, 'worker_busy'):
                service.transcriber(self.config(startup_timeout_seconds=.2, reap_timeout_seconds=.1)).transcribe(self.audio)
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(service._warm_waiter)
        finally:
            service._release()

    def test_warm_during_decode_returns_false(self):
        service = self.service()
        config = self.config('hung_decode', decode_timeout_seconds=30)
        failures = []
        def decode():
            try:
                service.transcriber(config).transcribe(self.audio)
            except AsrWorkerError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=decode)
        thread.start()
        self.assertTrue(wait_until(lambda: service.snapshot()['status'] == 'DECODING'))
        pid = service.snapshot()['pid']
        self.assertFalse(service.warm(config))
        self.assertEqual(service.snapshot()['pid'], pid)
        self.assertTrue(service.close())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(failures)

    def test_close_during_warm_terminates_promptly(self):
        service = self.service()
        thread, outcome = self.warm_in_thread(service, self.config('hung_start', startup_timeout_seconds=60))
        self.assertTrue(wait_until(lambda: service.snapshot()['status'] == 'STARTING'))
        started = time.monotonic()
        self.assertTrue(service.close())
        self.assertLess(time.monotonic() - started, 6)
        thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [False])
        self.assertFalse(service.snapshot()['owns_process'])


@unittest.skipUnless(os.name == 'nt', 'Windows job containment required')
class EditorServiceWarmTests(unittest.TestCase):
    def setUp(self):
        from services.llm.local_worker import LocalEditorConfig, LocalEditorService
        from tests.test_local_editor_worker import fixture_child, manifest
        self.directory = tempfile.TemporaryDirectory()
        self.config = LocalEditorConfig(str(manifest(Path(self.directory.name))), startup_timeout=10,
                                        request_timeout=.4, reap_timeout=3, idle_timeout=.4)
        self.service = LocalEditorService()
        self.patcher = patch('services.llm.local_worker.child_main', fixture_child)
        self.patcher.start()

    def tearDown(self):
        self.assertTrue(self.service.close())
        self.patcher.stop()
        self.directory.cleanup()

    def warm_in_thread(self):
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.service.warm(self.config)))
        thread.start()
        self.addCleanup(thread.join, 15)
        return thread, outcome

    def test_warm_then_edit_reuses_worker_and_idles_out(self):
        self.assertTrue(self.service.warm(self.config))
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['state'], 'READY')
        pid = snapshot['pid']
        self.service.backend(self.config).edit(system_prompt='policy', user_payload='hello')
        self.assertEqual(self.service.snapshot()['pid'], pid)
        self.assertTrue(wait_until(lambda: not self.service.snapshot()['owns_process'], 5))

    def test_warmed_but_unused_editor_idles_out(self):
        self.assertTrue(self.service.warm(self.config))
        self.assertTrue(wait_until(lambda: not self.service.snapshot()['owns_process'], 5))

    def test_edit_during_warm_waits_and_second_edit_fails_fast(self):
        with patch('services.llm.local_worker.child_main', slow_editor_child):
            thread, outcome = self.warm_in_thread()
            self.assertTrue(wait_until(lambda: self.service.snapshot()['state'] == 'STARTING'))
            pid = self.service.snapshot()['pid']
            results, errors = [], []
            def edit():
                try:
                    results.append(self.service.backend(self.config).edit(system_prompt='policy', user_payload='hello'))
                except RuntimeError as exc:
                    errors.append(str(exc))
            waiter = threading.Thread(target=edit)
            waiter.start()
            self.assertTrue(wait_until(lambda: self.service._warm_waiter))
            with self.assertRaisesRegex(RuntimeError, 'local_worker_busy'):
                self.service.backend(self.config).edit(system_prompt='policy', user_payload='hello')
            waiter.join(15)
            thread.join(15)
        self.assertEqual(outcome, [True])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(self.service.snapshot()['pid'], pid)

    def test_close_during_warm_terminates_promptly(self):
        with patch('services.llm.local_worker.child_main', hung_editor_child):
            thread, outcome = self.warm_in_thread()
            self.assertTrue(wait_until(lambda: self.service.snapshot()['state'] == 'STARTING'))
            time.sleep(.2)
            started = time.monotonic()
            self.assertTrue(self.service.close(timeout_seconds=5))
            self.assertLess(time.monotonic() - started, 6)
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [False])
        self.assertFalse(self.service.snapshot()['owns_process'])

    def test_warm_rejects_closed_service_and_invalid_config(self):
        self.assertFalse(self.service.warm('not a config'))
        self.assertTrue(self.service.close())
        self.assertFalse(self.service.warm(self.config))
        self.assertIsNone(self.service.snapshot()['pid'])


class EditorBusyBudgetTests(unittest.TestCase):
    def test_edit_waiting_on_warm_is_bounded(self):
        from services.llm.local_worker import LocalEditorConfig, LocalEditorService
        from tests.test_local_editor_worker import manifest
        with tempfile.TemporaryDirectory() as directory:
            config = LocalEditorConfig(str(manifest(Path(directory))), startup_timeout=.2, reap_timeout=.1)
            service = LocalEditorService()
            self.assertTrue(service._claim('warm'))
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, 'local_worker_busy'):
                    service.backend(config).edit(system_prompt='policy', user_payload='hello')
                self.assertLess(time.monotonic() - started, 2)
                self.assertFalse(service._warm_waiter)
                self.assertIsNone(service.snapshot()['pid'])
            finally:
                service._release()
            self.assertTrue(service.close(timeout_seconds=1))


class FakeEditorService:
    def __init__(self, result=True, error=None):
        self.closed = False
        self.result, self.error = result, error
        self.warmed = []

    def close(self, timeout_seconds):
        self.closed = True
        return True

    def snapshot(self):
        return {'closed': self.closed, 'state': 'STOPPED'}

    def backend(self, config):
        return self

    def warm(self, config):
        self.warmed.append(config)
        if self.error:
            raise self.error
        return self.result


class EditorOwnerWarmTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest = Path(self.directory.name) / 'manifest.json'
        self.manifest.write_text('{}')

    def enabled(self):
        return {'local_editor': {'enabled': True, 'manifest_path': str(self.manifest)}}

    def test_enabled_editor_is_warmed_with_validated_config(self):
        from services.llm.local_worker import LocalEditorConfig
        from services.runtime.local_editor import LocalEditorOwner
        service = FakeEditorService()
        owner = LocalEditorOwner(lambda: service)
        self.assertTrue(owner.warm(self.enabled()))
        self.assertEqual(len(service.warmed), 1)
        self.assertIsInstance(service.warmed[0], LocalEditorConfig)

    def test_disabled_invalid_or_closed_editor_is_not_warmed(self):
        from services.runtime.local_editor import LocalEditorOwner
        service = FakeEditorService()
        owner = LocalEditorOwner(lambda: service)
        for settings in ({}, {'local_editor': {'enabled': False}},
                         {'local_editor': {'enabled': True, 'manifest_path': ''}},
                         {'local_editor': {'enabled': True, 'manifest_path': str(self.manifest), 'threads': 4}},
                         {'local_editor': {'enabled': True, 'manifest_path': str(self.manifest), 'unknown': 1}}):
            with self.subTest(settings=settings):
                self.assertFalse(owner.warm(settings))
        service.closed = True
        self.assertFalse(owner.warm(self.enabled()))
        service.closed = False
        self.assertTrue(owner.close())
        self.assertFalse(owner.warm(self.enabled()))
        self.assertEqual(service.warmed, [])

    def test_service_failures_and_missing_warm_are_tolerated(self):
        from services.runtime.local_editor import LocalEditorOwner
        self.assertFalse(LocalEditorOwner(lambda: FakeEditorService(error=RuntimeError('boom'))).warm(self.enabled()))
        self.assertFalse(LocalEditorOwner(lambda: FakeEditorService(result=False)).warm(self.enabled()))
        class NoWarm(FakeEditorService):
            warm = None
        self.assertFalse(LocalEditorOwner(NoWarm).warm(self.enabled()))

    def test_frozen_session_settings_are_accepted(self):
        from services.runtime.controller import freeze_settings
        from services.runtime.local_editor import LocalEditorOwner
        service = FakeEditorService()
        self.assertTrue(LocalEditorOwner(lambda: service).warm(freeze_settings(self.enabled())))


class FakeAsrService:
    def __init__(self, block=None, error=None):
        self.block, self.error = block, error
        self.warmed = []
        self.called = threading.Event()

    def warm(self, config):
        self.warmed.append(config)
        self.called.set()
        if self.block is not None:
            self.block.wait(5)
        if self.error:
            raise self.error
        return True

    def transcriber(self, config):
        return SimpleNamespace(transcribe=lambda audio, **kwargs: None)

    def snapshot(self):
        return {'state': 'STOPPED'}

    def close(self, timeout_seconds=5):
        return True


class FakeEditorOwner:
    def __init__(self, block=None):
        self.block = block
        self.warmed = []
        self.called = threading.Event()

    def warm(self, settings):
        self.warmed.append(settings)
        self.called.set()
        if self.block is not None:
            self.block.wait(5)
        return True

    def formatter(self, settings):
        from services.runtime.local_asr import RawRecoveryFormatter
        return RawRecoveryFormatter()

    def snapshot(self):
        return {'state': 'STOPPED'}

    def close(self, timeout_seconds=5):
        return True


class WarmLocalModelsTests(unittest.TestCase):
    def setUp(self):
        from services.config.settings import load_default_settings
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        settings = load_default_settings()
        settings['runtime']['profile'] = 'local-multilingual'
        settings['local_asr'].update(snapshot_path=str(snapshot_dir(self.root)), device='cpu',
                                     compute_type='int8', languages=['en', 'fr', 'ar'])
        self.settings = settings

    def test_returns_immediately_and_warms_both_services(self):
        from services.runtime.api import warm_local_models
        block = threading.Event()
        self.addCleanup(block.set)
        asr, editor = FakeAsrService(block), FakeEditorOwner(block)
        started = time.monotonic()
        threads = warm_local_models(asr, editor, self.settings)
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(asr.called.wait(5))
        self.assertTrue(editor.called.wait(5))
        self.assertTrue(all(thread.daemon for thread in threads))
        block.set()
        for thread in threads:
            thread.join(5)
        self.assertIsInstance(asr.warmed[0], AsrWorkerConfig)
        self.assertEqual(asr.warmed[0].languages, ('en', 'fr', 'ar'))
        self.assertIs(editor.warmed[0], self.settings)

    def test_other_profiles_do_nothing(self):
        from services.runtime.api import warm_local_models
        asr, editor = FakeAsrService(), FakeEditorOwner()
        self.settings['runtime']['profile'] = 'low-impact'
        self.assertEqual(warm_local_models(asr, editor, self.settings), [])
        self.assertEqual(warm_local_models(asr, editor, {}), [])
        self.assertEqual(warm_local_models(asr, editor, None), [])
        self.assertEqual(asr.warmed, [])
        self.assertEqual(editor.warmed, [])

    def test_tolerates_missing_warm_failures_and_invalid_config(self):
        from services.runtime.api import warm_local_models
        self.assertEqual(warm_local_models(object(), None, self.settings), [])
        failing = FakeAsrService(error=RuntimeError('boom'))
        for thread in warm_local_models(failing, None, self.settings):
            thread.join(5)
        self.assertEqual(len(failing.warmed), 1)
        invalid = FakeAsrService()
        self.settings['local_asr']['languages'] = ['EN']
        for thread in warm_local_models(invalid, None, self.settings):
            thread.join(5)
        self.assertEqual(invalid.warmed, [])

    def test_frozen_session_settings_are_accepted(self):
        from services.runtime.api import warm_local_models
        from services.runtime.controller import freeze_settings
        asr = FakeAsrService()
        for thread in warm_local_models(asr, None, freeze_settings(self.settings)):
            thread.join(5)
        self.assertEqual(asr.warmed[0].languages, ('en', 'fr', 'ar'))

    def test_session_adapters_start_warmup_without_waiting(self):
        from services.runtime.api import RuntimeApiState, _create_session_adapters
        from services.runtime.controller import freeze_settings
        block = threading.Event()
        self.addCleanup(block.set)
        asr, editor = FakeAsrService(block), FakeEditorOwner(block)
        state = RuntimeApiState(self.root, self.root / 'log', lambda _: None, asr_service=asr, editor_service=editor)
        with patch('services.asr.audio_capture.AudioRecorder.microphone', return_value=object()):
            started = time.monotonic()
            _create_session_adapters(state, freeze_settings(self.settings))
            self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(asr.called.wait(5))
        self.assertTrue(editor.called.wait(5))

    def test_create_app_does_not_warm(self):
        from services.config.settings import save_settings
        from services.runtime.api import create_app
        save_settings(self.root / 'config' / 'settings.yaml', self.settings)
        asr, editor = FakeAsrService(), FakeEditorOwner()
        create_app(root=self.root, engine_id='warm', engine_token='secret', asr_service=asr, editor_service=editor)
        time.sleep(.2)
        self.assertEqual(asr.warmed, [])
        self.assertEqual(editor.warmed, [])

    def test_main_warms_from_controller_snapshot(self):
        import services.runtime.api as api
        shutdown = threading.Event()
        self.addCleanup(shutdown.set)
        snapshot = {'runtime': {'profile': 'local-multilingual'}}
        app = SimpleNamespace(state=SimpleNamespace(
            asr_service='asr', editor_service='editor', shutdown_requested=shutdown,
            controller=SimpleNamespace(settings_snapshot=lambda: snapshot),
            close_resources=lambda: True))
        order = []
        class Server:
            def __init__(self, config):
                pass
            def run(self):
                order.append('run')
        with patch.object(api, 'create_app', return_value=app), \
                patch.object(api, 'warm_local_models', side_effect=lambda *args: order.append(args)), \
                patch('uvicorn.Server', Server), patch('uvicorn.Config'):
            api.main()
        self.assertEqual(order, [('asr', 'editor', snapshot), 'run'])


if __name__ == '__main__':
    unittest.main()
