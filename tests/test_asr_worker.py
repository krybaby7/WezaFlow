import tempfile
import threading
import time
import unittest
from pathlib import Path
import numpy as np
from services.asr.worker import AsrWorkerConfig, AsrWorkerService, AsrWorkerError
from services.asr.local_artifacts import REQUIRED_FILES
from tests.fixtures.asr_worker_fixture import fixture_worker

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        for name in REQUIRED_FILES:
            (Path(self.directory.name)/name).write_text('{}')
        self.service = AsrWorkerService(_worker_target=fixture_worker)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.service.close)
        self.audio = np.zeros(160,dtype=np.float32)

    def config(self, mode='normal', **kwargs):
        snapshot = Path(self.directory.name)/mode
        snapshot.mkdir(exist_ok=True)
        for name in REQUIRED_FILES:
            (snapshot/name).write_text('{}')
        (snapshot/'fixture-mode').write_text(mode)
        return AsrWorkerConfig(str(snapshot),'cpu','int8',2,startup_timeout_seconds=kwargs.pop('startup_timeout_seconds',5),decode_timeout_seconds=kwargs.pop('decode_timeout_seconds',2),**kwargs)

    def test_validation(self):
        with self.assertRaises(ValueError):
            AsrWorkerConfig('relative','cpu','int8',2)
        for field in ('startup_timeout_seconds','decode_timeout_seconds','idle_timeout_seconds','reap_timeout_seconds'):
            with self.assertRaises(ValueError):
                self.config(**{field:float('nan')})

    def test_close_without_start(self):
        self.assertTrue(self.service.close())
        self.assertTrue(self.service.close())
        self.assertEqual(self.service.snapshot()['status'],'CLOSED')

    def test_reuses_pid_and_replaces_configuration(self):
        transcriber = self.service.transcriber(self.config())
        first = transcriber.transcribe(self.audio)
        self.assertEqual(first.text,transcriber.transcribe(self.audio).text)
        replacement = self.service.transcriber(self.config('second')).transcribe(self.audio)
        self.assertNotEqual(first.text,replacement.text)
        self.assertTrue(self.service.close())
        self.assertFalse(self.service.snapshot()['owns_process'])

    def test_failure_modes_reap_real_child(self):
        for mode in ('hung_start','die_start','hung_decode','die_decode','malformed','oversized','mismatch'):
            with self.subTest(mode=mode):
                service = AsrWorkerService(_worker_target=fixture_worker)
                self.addCleanup(service.close)
                config = self.config(mode,startup_timeout_seconds=.6,decode_timeout_seconds=.3)
                before = time.monotonic()
                expected = 'worker_timeout' if mode in ('hung_start','hung_decode') else 'worker_protocol_error'
                with self.assertRaisesRegex(AsrWorkerError,expected):
                    service.transcriber(config).transcribe(self.audio)
                self.assertLess(time.monotonic()-before,8)
                self.assertFalse(service.snapshot()['owns_process'])
                self.assertFalse(service.snapshot()['owns_io'])
                if mode not in ('hung_start','die_start'):
                    self.assertTrue((Path(config.snapshot)/'fixture-decode').is_file(), 'decode fault must receive request')

    @unittest.skipUnless(__import__('os').name == 'nt','Windows spawn handle transfer')
    def test_startup_timeout_before_child_unpickles_releases_ownership(self):
        with self.assertRaisesRegex(AsrWorkerError,'worker_timeout'):
            self.service.transcriber(self.config('hung_start',startup_timeout_seconds=.001)).transcribe(self.audio)
        self.assertFalse(self.service.snapshot()['owns_process'])
        self.assertFalse(self.service.snapshot()['owns_io'])

    def test_busy_and_close_interrupt(self):
        transcriber = self.service.transcriber(self.config('hung_decode',decode_timeout_seconds=30))
        failures = []
        def call():
            try:
                transcriber.transcribe(self.audio)
            except AsrWorkerError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=call)
        thread.start()
        deadline = time.monotonic()+8
        while self.service.snapshot()['status'] != 'DECODING' and time.monotonic()<deadline:
            threading.Event().wait(.01)
        self.assertEqual(self.service.snapshot()['status'],'DECODING')
        pid = self.service.snapshot()['pid']
        with self.assertRaisesRegex(AsrWorkerError,'worker_busy'):
            transcriber.transcribe(self.audio)
        self.assertEqual(self.service.snapshot()['pid'],pid)
        self.assertTrue(self.service.close())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(failures)

    def test_invalid_audio_never_spawns(self):
        for audio in ('file.wav',np.zeros((1,2),dtype=np.float32),np.array([float('nan')],dtype=np.float32)):
            with self.assertRaises(ValueError):
                self.service.transcriber(self.config()).transcribe(audio)
        self.assertIsNone(self.service.snapshot()['pid'])

    def test_idle_zero_releases_immediately(self):
        self.service.transcriber(self.config(idle_timeout_seconds=0)).transcribe(self.audio)
        self.assertEqual(self.service.snapshot()['state'],'STOPPED')
        self.assertFalse(self.service.snapshot()['owns_process'])

    def test_runtime_manifest_change_replaces_worker(self):
        manifest = Path(self.directory.name)/'runtime.json'
        manifest.write_text('[]')
        transcriber = self.service.transcriber(self.config(runtime_manifest=str(manifest)))
        first = transcriber.transcribe(self.audio)
        manifest.write_text('[ ]')
        second = transcriber.transcribe(self.audio)
        self.assertNotEqual(first.text,second.text)

    def test_failed_reap_retains_ownership_and_retry(self):
        from unittest.mock import Mock
        process = Mock()
        process.pid = 123
        process.is_alive.return_value = True
        self.service._process = process
        self.service._status = 'READY'
        self.assertFalse(self.service.close(timeout_seconds=.01))
        self.assertTrue(self.service.snapshot()['owns_process'])
        self.assertEqual(self.service.snapshot()['state'],'REAPING')
        process.is_alive.return_value = False
        self.assertTrue(self.service.close())
        self.assertFalse(self.service.snapshot()['owns_process'])

    @unittest.skipUnless(__import__('os').name == 'nt','Windows Job Object contract')
    def test_parent_crash_kills_worker_job(self):
        import ctypes
        import queue
        import subprocess
        import sys
        from ctypes import wintypes
        launcher = subprocess.Popen([sys.executable,'-m','tests.fixtures.asr_worker_launcher',self.directory.name],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda: launcher.poll() is None and launcher.kill())
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(launcher.stdout.readline()),daemon=True).start()
        pid = int(lines.get(timeout=15).strip())
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000,False,pid)
        self.assertTrue(handle)
        try:
            launcher.kill()
            launcher.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
        finally:
            kernel.CloseHandle(handle)
            launcher.stdout.close()
            launcher.stderr.close()

    def test_idle_unload_and_resume(self):
        transcriber = self.service.transcriber(self.config(idle_timeout_seconds=.05))
        first = transcriber.transcribe(self.audio)
        deadline = time.monotonic()+5
        while self.service.snapshot()['owns_process'] and time.monotonic()<deadline:
            threading.Event().wait(.01)
        self.assertFalse(self.service.snapshot()['owns_process'])
        second = transcriber.transcribe(self.audio)
        self.assertNotEqual(first.text,second.text)

    def test_snapshot_during_cleanup_is_safe(self):
        self.service.transcriber(self.config()).transcribe(self.audio)
        thread = threading.Thread(target=self.service.close)
        thread.start()
        while thread.is_alive():
            self.assertIn(self.service.snapshot()['state'],('READY','REAPING','CLOSED'))
        thread.join()


    def test_real_partial_stream_frame_timeout_reaps(self):
        from tests.fixtures.asr_worker_fixture import stream_connections
        service = AsrWorkerService(_worker_target=fixture_worker,_connection_factory=stream_connections)
        self.addCleanup(service.close)
        started = time.monotonic()
        with self.assertRaisesRegex(AsrWorkerError,'worker_timeout'):
            service.transcriber(self.config('partial',decode_timeout_seconds=.2)).transcribe(self.audio)
        self.assertLess(time.monotonic()-started,7)
        self.assertFalse(service.snapshot()['owns_process'])
        self.assertFalse(service.snapshot()['owns_io'])


    def test_real_blocked_send_timeout_reaps(self):
        started = time.monotonic()
        with self.assertRaisesRegex(AsrWorkerError,'worker_timeout'):
            self.service.transcriber(self.config('hung_read',decode_timeout_seconds=.2)).transcribe(np.zeros(1000000,dtype=np.float32))
        self.assertLess(time.monotonic()-started,7)
        self.assertFalse(self.service.snapshot()['owns_process'])
        self.assertFalse(self.service.snapshot()['owns_io'])

    def test_config_rejects_unsupported_compute_and_excessive_deadlines(self):
        for field in ('startup_timeout_seconds','decode_timeout_seconds','idle_timeout_seconds','reap_timeout_seconds'):
            with self.assertRaises(ValueError):
                self.config(**{field:1e100})
        with self.assertRaises(ValueError):
            AsrWorkerConfig(self.directory.name,'cpu','garbage',2)

    def test_transcriber_accepts_bounded_pipeline_sample_lists(self):
        transcriber = self.service.transcriber(self.config())
        self.assertTrue(transcriber.transcribe([0.0,0.1,-0.1]).text)
        self.assertTrue(transcriber.transcribe((0.0,0.1),initial_prompt='').text)
        for value in ([float('nan')],[[0.0]],['not audio'],[]):
            with self.assertRaises(ValueError):
                transcriber.transcribe(value)


    def test_sanitized_child_errors_validate_stage(self):
        for mode, code in (('startup_error','worker_startup_failed'),('decode_error','worker_decode_failed'),('unsafe_error','worker_protocol_error')):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(AsrWorkerError,'^'+code+'$'):
                    self.service.transcriber(self.config(mode)).transcribe(self.audio)
                self.assertFalse(self.service.snapshot()['owns_process'])


    def test_retains_detached_model_provenance(self):
        import hashlib
        self.service.transcriber(self.config('provenance')).transcribe(self.audio)
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['provenance']['snapshot']['files']['model.bin']['sha256'],hashlib.sha256(b'{}').hexdigest())
        self.assertEqual(snapshot['provenance']['startup']['warmup_ms'],2)
        snapshot['provenance']['snapshot']['files'].clear()
        self.assertEqual(len(self.service.snapshot()['provenance']['snapshot']['files']),5)
        self.assertTrue(self.service.close())
        self.assertIsNotNone(self.service.snapshot()['provenance'])
