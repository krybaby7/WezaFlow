"""Explicit read-only diagnostic. Importing this module launches nothing."""
import argparse
import json
import math
import multiprocessing
import os
from pathlib import Path
import subprocess
import time
import threading
from services.asr.windows_job import WorkerJob

MAX_OUTPUT = 16384
BOOL_FIELDS = ('password', 'read_only', 'text_pattern', 'text_pattern2', 'value_pattern', 'selection_observable')
INT_FIELDS = {'foreground_hwnd': 2**64-1, 'foreground_pid': 2**32-1, 'process_creation_identity': 2**64-1,
              'focus_hwnd': 2**64-1, 'foreground_hwnd_after': 2**64-1, 'foreground_pid_after': 2**32-1,
              'focus_hwnd_after': 2**64-1, 'uia_pid': 2**32-1, 'uia_native_hwnd': 2**64-1, 'control_type': 100000, 'ancestors_inspected': 8,
              'inspected_characters': 2000, 'selection_count': 1000}


def sanitize_metadata(raw):
    if not isinstance(raw, dict):
        raise ValueError('probe_protocol_invalid')
    result = {key: raw.get(key) if type(raw.get(key)) is bool else None for key in BOOL_FIELDS}
    for key, cap in INT_FIELDS.items():
        value = raw.get(key)
        minimum = 0 if key in ('ancestors_inspected', 'inspected_characters', 'selection_count') else 1
        result[key] = value if type(value) is int and minimum <= value <= cap else None
    for key in ('runtime_id', 'runtime_id_after'):
        runtime = raw.get(key)
        result[key] = runtime if isinstance(runtime, list) and 0 < len(runtime) <= 32 and all(type(x) is int and -(2**31) <= x < 2**31 for x in runtime) else None
    pairs = [('foreground_hwnd', 'foreground_hwnd_after'), ('foreground_pid', 'foreground_pid_after'),
             ('focus_hwnd', 'focus_hwnd_after'), ('runtime_id', 'runtime_id_after')]
    comparisons = [None if result[a] is None or result[b] is None else result[a] == result[b] for a, b in pairs]
    result['coherence'] = 'changed' if False in comparisons else 'stable' if all(x is True for x in comparisons) else 'unknown'
    result['coherence_scope'] = 'sampled_values_only_not_atomic'
    result['uia_same_process'] = None if result['foreground_pid'] is None or result['uia_pid'] is None else result['foreground_pid'] == result['uia_pid']
    if result['password'] is not False:
        for key in ('inspected_characters', 'selection_count', 'selection_observable'):
            result[key] = None
    return result


def _entry(connection, gate, target, argument, gate_timeout):
    try:
        if gate.wait(gate_timeout):
            target(connection, gate, argument)
    except BaseException:
        pass
    finally:
        connection.close()


def _helper(connection, gate, argument):
    try:
        script = Path(__file__).with_name('windows_probe.ps1')
        executable = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        child = subprocess.Popen([executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)] + (['-FixtureTarget'] if argument == 'native_fixture' else []),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        data = child.stdout.read(MAX_OUTPUT + 1)
        if len(data) > MAX_OUTPUT or child.wait() != 0:
            return
        connection.send_bytes(data)
    except BaseException:
        pass
    finally:
        connection.close()


class WindowsProbe:
    """Owns the helper job until empty. run permits timeout plus at most 2s cleanup.

    Coherence compares sequential samples only, never an atomic target guarantee.
    """
    def __init__(self, *, _target=_helper):
        self._target = _target
        self._process = self._job = self._connection = None
        self._io = None

    @property
    def owns_process(self):
        return self._process is not None or self._job is not None

    def close(self, timeout=2):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('probe_reap_deadline_invalid')
        deadline = time.monotonic() + timeout
        try:
            if self._job is not None:
                self._job.terminate()
            if self._process is not None:
                if self._process.is_alive():
                    self._process.terminate()
                if self._process.pid is not None:
                    self._process.join(max(0, deadline-time.monotonic()))
                if self._process.is_alive():
                    return False
            if self._job is not None and not self._job.wait_empty(max(0, deadline-time.monotonic())):
                return False
            if self._io is not None:
                self._io.join(max(0, deadline-time.monotonic()))
                if self._io.is_alive():
                    return False
            if self._connection is not None:
                self._connection.close()
            if self._job is not None:
                self._job.close()
            if self._process is not None:
                self._process.close()
            self._process = self._job = self._connection = self._io = None
            return True
        except BaseException:
            return False

    def run(self, *, timeout=5, _argument=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('probe_deadline_invalid')
        if self.owns_process:
            raise RuntimeError('probe_cleanup_pending')
        start = time.monotonic()
        result = {'status': 'helper_failed', 'metadata': None, 'observation': 'fixture_target' if _argument == 'native_fixture' else 'focused_editor'}
        if os.name != 'nt':
            return dict(result, status='unsupported_platform', cleanup_complete=True, elapsed_seconds=0)
        child = None
        try:
            context = multiprocessing.get_context('spawn')
            parent, child = context.Pipe(duplex=False)
            self._connection = parent
            gate = context.Event()
            self._job = WorkerJob()
            from services.spawn_connection import child_connection
            self._process = context.Process(target=_entry, args=(child_connection(child), gate, self._target, _argument, timeout))
            self._process.start()
            child.close()
            self._job.assign(self._process)
            gate.set()
            done = threading.Event()
            output = []
            def receive():
                try:
                    output.append(parent.recv_bytes(MAX_OUTPUT))
                except BaseException:
                    pass
                finally:
                    done.set()
            self._io = threading.Thread(target=receive, daemon=True, name='editor-probe-read')
            self._io.start()
            if not done.wait(max(0, timeout-(time.monotonic()-start))):
                result['status'] = 'timeout'
            elif output:
                result['metadata'] = sanitize_metadata(json.loads(output[0]))
                result['status'] = 'ok'
        except BaseException:
            pass
        finally:
            if child is not None:
                child.close()
            result['cleanup_complete'] = self.close()
            if not result['cleanup_complete']:
                result['status'] = 'cleanup_incomplete'
            result['elapsed_seconds'] = round(time.monotonic()-start, 3)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only focused-editor metadata diagnostic; no insertion qualification.')
    parser.add_argument('--probe-focused-editor', action='store_true', required=True)
    parser.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args(argv)
    probe = WindowsProbe()
    result = probe.run(timeout=args.timeout)
    print(json.dumps(result, separators=(',', ':')))
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
