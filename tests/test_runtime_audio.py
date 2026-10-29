from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


class _SounddeviceBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sounddevice":
            raise AssertionError("sounddevice imported at module import time")
        return None


class RuntimeAudioTests(unittest.TestCase):
    def test_import_does_not_import_sounddevice(self) -> None:
        sys.modules.pop("services.runtime.audio_smoke", None)
        blocker = _SounddeviceBlocker()
        sys.meta_path.insert(0, blocker)
        try:
            importlib.import_module("services.runtime.audio_smoke")
        finally:
            sys.meta_path.remove(blocker)

    def test_record_wav_records_for_duration_and_writes_wav_bytes(self) -> None:
        from services.runtime.audio_smoke import record_wav

        events: list[object] = []
        wav_bytes = b"RIFFfake-wav"

        class FakeRecorder:
            def start(self) -> None:
                events.append("start")

            def stop(self) -> bytes:
                events.append("stop")
                return wav_bytes

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "test.wav"

            result = record_wav(
                output,
                seconds=1.25,
                recorder_factory=FakeRecorder,
                sleep=lambda seconds: events.append(seconds),
            )

            self.assertEqual(output, result)
            self.assertEqual(wav_bytes, output.read_bytes())
            self.assertEqual(["start", 1.25, "stop"], events)

    def test_record_wav_wraps_startup_errors_with_actionable_message(self) -> None:
        from services.runtime.audio_smoke import record_wav

        class BrokenRecorder:
            def start(self) -> None:
                raise ImportError("No module named sounddevice")

            def stop(self) -> bytes:
                return b""  # Failed starts still receive best-effort cleanup.

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RuntimeError) as raised:
                record_wav(
                    Path(temp_dir) / "test.wav",
                    recorder_factory=BrokenRecorder,
                    sleep=lambda seconds: None,
                )

        message = str(raised.exception).lower()
        self.assertIn("sounddevice", message)
        self.assertIn("microphone", message)

    def test_record_test_script_uses_project_virtualenv(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "record_test.ps1"

        content = script.read_text(encoding="utf-8")

        self.assertIn(".venv", content)
        self.assertIn("Scripts\\python.exe", content)
        self.assertIn("services.runtime.audio_smoke", content)

    def test_record_benchmark_pack_records_prompts_and_writes_manifest(self) -> None:
        from services.runtime.benchmark_pack import BenchmarkPrompt, record_benchmark_pack

        prompts = [
            BenchmarkPrompt(label="short", text="Hello world.", seconds=1.0),
            BenchmarkPrompt(label="long", text="This is a longer sample.", seconds=2.0),
        ]
        events: list[object] = []

        def fake_record(path, *, seconds):
            events.append((Path(path).name, seconds))
            Path(path).write_bytes(b"RIFFfake")
            return Path(path)

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = record_benchmark_pack(
                Path(temp_dir),
                prompts=prompts,
                record=fake_record,
                input_fn=lambda prompt="": events.append("input"),
                output=events.append,
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [event for event in events if isinstance(event, tuple)],
            [("short.wav", 1.0), ("long.wav", 2.0)],
        )
        self.assertEqual(manifest["samples"][0]["label"], "short")
        self.assertEqual(manifest["samples"][0]["audio"], "short.wav")
        self.assertEqual(manifest["samples"][0]["expected"], "Hello world.")

    def test_record_benchmark_pack_script_uses_project_virtualenv(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "record_benchmark_pack.ps1"

        content = script.read_text(encoding="utf-8")

        self.assertIn(".venv", content)
        self.assertIn("Scripts\\python.exe", content)
        self.assertIn("services.runtime.benchmark_pack", content)

    def test_record_benchmark_pack_cmd_launches_powershell_script(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "record_benchmark_pack.cmd"

        content = script.read_text(encoding="utf-8")

        self.assertIn("powershell.exe", content)
        self.assertIn("-ExecutionPolicy Bypass", content)
        self.assertIn("-File", content)
        self.assertIn("record_benchmark_pack.ps1", content)


class RecordingCleanupTests(unittest.TestCase):
    def test_interruptions_and_failures_cleanup_once_without_output(self):
        from services.runtime.audio_smoke import record_wav
        for stage in ('start', 'sleep', 'stop'):
            for failure in (KeyboardInterrupt('cancel'), ValueError('failure')):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    calls = []
                    class Recorder:
                        def start(self):
                            calls.append('start')
                            if stage == 'start':
                                raise failure
                        def stop(self):
                            calls.append('stop')
                            if stage == 'stop':
                                raise failure
                            if stage in ('start', 'sleep'):
                                raise RuntimeError('cleanup failed too')
                    def sleep(seconds):
                        if stage == 'sleep':
                            raise failure
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / 'test.wav'
                        with self.assertRaises(BaseException) as raised:
                            record_wav(path, recorder_factory=Recorder, sleep=sleep)
                        actual = raised.exception
                        if stage == 'start' and isinstance(failure, Exception):
                            self.assertIs(actual.__cause__, failure)
                            self.assertIn('sounddevice', str(actual))
                        else:
                            self.assertIs(actual, failure)
                        self.assertEqual(calls, ['start', 'stop'])
                        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
