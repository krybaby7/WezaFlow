import importlib.util
import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch


def fixture(connection, gate, mode):
    gate.wait()
    if isinstance(mode, tuple) and mode[0] == 'stall':
        descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        Path(mode[1]).write_text(str(descendant.pid))
        time.sleep(60)
    elif mode == 'oversize':
        connection.send_bytes(b'x' * 20000)
    elif mode == 'exception':
        raise RuntimeError('PRIVATE FIELD CONTENT')
    else:
        connection.send_bytes(json.dumps({'password': mode == 'password', 'text_pattern': True,
            'inspected_characters': 0, 'unknown': None, 'name': 'PRIVATE FIELD CONTENT'}).encode())


def provider_fixture(connection, gate, mode):
    gate.wait()
    script = Path(__file__).parent / 'fixtures' / 'windows_probe_provider.ps1'
    module = Path(__file__).parents[1] / 'services' / 'injection' / 'windows_probe_uia.ps1'
    executable = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    child = subprocess.Popen([executable, '-NoProfile', '-NonInteractive', '-File', str(script), mode, str(module)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    data = child.stdout.read(16385)
    if len(data) <= 16384 and child.wait() == 0:
        connection.send_bytes(data)


class ProbeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell provider fixture')
    def test_native_observation_function_suppresses_password_unknown_and_inaccessible(self):
        from services.injection.windows_probe import WindowsProbe
        for mode in ('password', 'unknown', 'null', 'inaccessible', 'readable'):
            with self.subTest(mode=mode):
                result = WindowsProbe(_target=provider_fixture).run(timeout=10, _argument=mode)
                self.assertEqual(result['status'], 'ok', result)
                self.assertTrue(result['cleanup_complete'])
                self.assertEqual(result['metadata']['inspected_characters'], 9 if mode == 'readable' else None)
                self.assertEqual(result['metadata']['password'], True if mode == 'password' else False if mode == 'readable' else None)

    @unittest.skipUnless(os.name == 'nt', 'Windows UIA')
    def test_native_hidden_fixture_metadata_only(self):
        from services.injection.windows_probe import WindowsProbe
        result = WindowsProbe().run(timeout=15, _argument='native_fixture')
        self.assertEqual(result['status'], 'ok', result)
        self.assertTrue(result['cleanup_complete'])
        self.assertEqual(result['observation'], 'fixture_target')
        self.assertIsNone(result['metadata']['foreground_hwnd'])
        self.assertIsNotNone(result['metadata']['control_type'])
        self.assertIsNotNone(result['metadata']['runtime_id'])
        self.assertNotIn('Synthetic fixture', json.dumps(result))

    def test_close_rejects_unbounded_deadlines(self):
        from services.injection.windows_probe import WindowsProbe
        for value in (float('nan'), float('inf'), -1, 0, 31, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WindowsProbe().close(value)

    def test_observation_coherence_is_explicit_and_conservative(self):
        from services.injection.windows_probe import sanitize_metadata
        base = {'foreground_hwnd': 1, 'foreground_pid': 2, 'focus_hwnd': 3,
                'foreground_hwnd_after': 1, 'foreground_pid_after': 2, 'focus_hwnd_after': 3,
                'runtime_id': [1, 2], 'runtime_id_after': [1, 2], 'uia_pid': 2}
        self.assertEqual(sanitize_metadata(base)['coherence'], 'stable')
        self.assertEqual(sanitize_metadata(dict(base, foreground_hwnd_after=9))['coherence'], 'changed')
        cross_process = sanitize_metadata(dict(base, uia_pid=9))
        self.assertEqual(cross_process['coherence'], 'stable')
        self.assertFalse(cross_process['uia_same_process'])
        self.assertIsNone(sanitize_metadata({})['uia_same_process'])
        self.assertEqual(sanitize_metadata({})['coherence'], 'unknown')

    def test_inert_import(self):
        code = """
import multiprocessing.process
import subprocess

def fail(*args, **kwargs):
    raise AssertionError('implicit launch')

multiprocessing.process.BaseProcess.start = fail
subprocess.Popen = fail
from services.injection import windows_probe
assert callable(windows_probe.main)
"""
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')

    @unittest.skipUnless(os.name == 'nt', 'Windows containment')
    def test_success_sanitizes_unknown_fields(self):
        from services.injection.windows_probe import WindowsProbe
        probe = WindowsProbe(_target=fixture)
        result = probe.run(_argument='success')
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['cleanup_complete'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertFalse(probe.owns_process)

    @unittest.skipUnless(os.name == 'nt', 'Windows containment')
    def test_timeout_reaps_job_descendant(self):
        from services.injection.windows_probe import WindowsProbe
        probe = WindowsProbe(_target=fixture)
        start = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'pid'
            result = probe.run(timeout=1.5, _argument=('stall', str(pidfile)))
            self.assertTrue(pidfile.is_file(), 'descendant must actually start')
            descendant_pid = int(pidfile.read_text())
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, descendant_pid)
            if handle:
                code = wintypes.DWORD()
                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                kernel.CloseHandle(handle)
                self.assertNotEqual(code.value, 259)
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(result['cleanup_complete'])
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(probe.owns_process)

    @unittest.skipUnless(os.name == 'nt', 'Windows containment')
    def test_bounded_output_and_sanitized_errors(self):
        from services.injection.windows_probe import WindowsProbe
        for mode in ('oversize', 'exception'):
            result = WindowsProbe(_target=fixture).run(_argument=mode)
            self.assertEqual(result['status'], 'helper_failed')
            self.assertTrue(result['cleanup_complete'])
            self.assertNotIn('PRIVATE', json.dumps(result))

    @unittest.skipUnless(os.name == 'nt', 'Windows containment')
    def test_incomplete_cleanup_retains_owner_for_retry(self):
        from services.injection.windows_probe import WindowsProbe
        probe = WindowsProbe(_target=fixture)
        with patch('services.asr.windows_job.WorkerJob.wait_empty', return_value=False):
            result = probe.run(_argument='success')
        self.assertEqual(result['status'], 'cleanup_incomplete')
        self.assertFalse(result['cleanup_complete'])
        self.assertTrue(probe.owns_process)
        with self.assertRaisesRegex(RuntimeError, 'cleanup_pending'):
            probe.run()
        self.assertTrue(probe.close())
        self.assertFalse(probe.owns_process)

    def test_password_suppresses_observation_and_unknowns_stay_unknown(self):
        from services.injection.windows_probe import sanitize_metadata
        result = sanitize_metadata({'password': True, 'inspected_characters': 100, 'selection_count': 2})
        self.assertIsNone(result['inspected_characters'])
        self.assertIsNone(result['selection_count'])
        self.assertIsNone(result['read_only'])

if __name__ == '__main__':
    unittest.main()
