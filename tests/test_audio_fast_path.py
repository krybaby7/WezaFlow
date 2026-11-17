"""The numpy audio path must produce exactly what the pure-Python path produced."""
import io
import math
import random
import unittest
import wave
from array import array
from unittest import mock

import numpy as np

from services.asr import audio_capture, vad as vad_module
from services.asr.audio_capture import AudioRecorder
from services.asr.vad import VADFilter
from services.runtime import session


def reference_wav_bytes(samples, sample_rate=16000):
    pcm = bytearray()
    for sample in samples:
        clipped = max(-1.0, min(1.0, float(sample)))
        pcm.extend(int(round(clipped * 32767)).to_bytes(2, 'little', signed=True))
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate)
        wav.writeframes(bytes(pcm))
    return buffer.getvalue()


def reference_wav_samples(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
        frames = wav.readframes(wav.getnframes())
    return [int.from_bytes(frames[i:i + 2], 'little', signed=True) / 32768.0 for i in range(0, len(frames), 2)]


def edge_samples(count=4000, seed=7):
    rng = random.Random(seed)
    values = [0.0, -0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5, 2.5 / 32767, -2.5 / 32767, 0.5 / 32767,
              1e-9, 0.02, -0.02, 0.019999999, 3.0, -7.0]
    values += [rng.uniform(-1.2, 1.2) for _ in range(count)]
    values += [float(np.float32(rng.uniform(-1, 1))) for _ in range(count)]
    values += [(k + 0.5) / 32767 for k in range(-40, 40)]
    return values


class WavConversionTests(unittest.TestCase):
    def test_wav_bytes_match_python_rounding(self):
        samples = edge_samples()
        expected = reference_wav_bytes(samples)
        for form in (samples, tuple(samples), np.asarray(samples), array('d', samples)):
            with self.subTest(form=type(form).__name__):
                self.assertEqual(AudioRecorder.float_samples_to_wav_bytes(form), expected)
        as_float32 = array('f', samples)
        self.assertEqual(AudioRecorder.float_samples_to_wav_bytes(as_float32), reference_wav_bytes(as_float32))

    def test_nonfinite_mixed_and_generator_inputs_match(self):
        for samples in ([float('nan'), float('inf'), float('-inf'), 0.25], [1, True, 0, -1, 0.5],
                        [np.float32(0.3), 0.7], []):
            with self.subTest(samples=samples):
                self.assertEqual(AudioRecorder.float_samples_to_wav_bytes(samples), reference_wav_bytes(samples))
        self.assertEqual(AudioRecorder.float_samples_to_wav_bytes(x / 10 for x in range(-12, 13)),
                         reference_wav_bytes([x / 10 for x in range(-12, 13)]))
        with self.assertRaises(TypeError):
            AudioRecorder.float_samples_to_wav_bytes([[0.5]])

    def test_wav_samples_match_python_decoding(self):
        wav_bytes = reference_wav_bytes(edge_samples())
        decoded = session.wav_bytes_to_float_samples(wav_bytes)
        self.assertIs(type(decoded), list)
        self.assertTrue(all(type(value) is float for value in decoded))
        self.assertEqual(decoded, reference_wav_samples(wav_bytes))
        self.assertEqual(session.wav_bytes_to_float_samples(reference_wav_bytes([])), [])

    def test_python_fallback_without_numpy(self):
        samples = edge_samples(200)
        expected_bytes = reference_wav_bytes(samples)
        with mock.patch.object(audio_capture, '_np_module', None), mock.patch.object(session, '_np_module', None):
            self.assertEqual(AudioRecorder.float_samples_to_wav_bytes(samples), expected_bytes)
            self.assertEqual(session.wav_bytes_to_float_samples(expected_bytes), reference_wav_samples(expected_bytes))


class AudioStatsTests(unittest.TestCase):
    def test_stats_match_python(self):
        for samples in (edge_samples(), [], [0.0], [float('nan'), 0.5], [0.5, float('nan')], [float('-inf')], [-3, 2]):
            with self.subTest(n=len(samples)):
                expected = {'sample_count': len(samples),
                            'max_abs': max((abs(float(s)) for s in samples), default=0.0)}
                actual = session.audio_stats(samples)
                if math.isnan(expected['max_abs']):
                    self.assertTrue(math.isnan(actual['max_abs']))
                else:
                    self.assertEqual(actual, expected)
                self.assertIs(type(actual['max_abs']), float)


class VadEquivalenceTests(unittest.TestCase):
    def reference(self, f, samples):
        with mock.patch.object(vad_module, '_np_module', None):
            out = f.filter(list(samples))
        return out, dict(f.last_stats)

    def test_vad_decisions_trims_and_stats_match(self):
        rng = random.Random(11)
        cases = [edge_samples(500), [0.0] * 8000, [0.01] * 8000, [0.0] * 3000 + [0.3] + [0.0] * 3000,
                 [0.0] * 100 + [0.05] * 4000 + [0.0] * 100, [rng.uniform(-.03, .03) for _ in range(16000)],
                 [0.02] * 4000, [0.0], [1, 0, -1], [float('nan')] * 10 + [0.5], [0.5, float('nan')]]
        configs = [dict(), dict(trim_padding_ms=50), dict(trim_padding_ms=100, trim_threshold=0.04),
                   dict(threshold=0.05, min_duration_ms=100, trim_padding_ms=10), dict(trim_padding_ms=5000)]
        for config in configs:
            for samples in cases:
                with self.subTest(config=config, n=len(samples)):
                    expected, expected_stats = self.reference(VADFilter(**config), samples)
                    f = VADFilter(**config)
                    actual = f.filter(samples)
                    self.assertEqual(repr(actual), repr(expected))
                    self.assertEqual(repr(f.last_stats), repr(expected_stats))
                    if actual is not None:
                        self.assertIs(type(actual), list)

    def test_untrimmed_result_is_the_input_list(self):
        samples = [0.5] * 8000
        self.assertIs(VADFilter().filter(samples), samples)


class RecorderFastPathTests(unittest.TestCase):
    def capture(self, blocks, max_samples=4_800_000):
        recorder = AudioRecorder(max_samples=max_samples)
        recorder.start()
        for block in blocks:
            recorder._callback(block)
        status = recorder.capture_status
        snapshot = recorder.snapshot_samples()
        return status.terminal_code, status.sample_count, snapshot, recorder.stop()

    def test_ndarray_blocks_match_list_blocks(self):
        rng = np.random.default_rng(3)
        blocks = [rng.uniform(-1.1, 1.1, (1024, 1)).astype(np.float32) for _ in range(5)]
        for max_samples in (4_800_000, 3000, 2048):
            with self.subTest(max_samples=max_samples):
                fast = self.capture(blocks, max_samples)
                slow = self.capture([block.tolist() for block in blocks], max_samples)
                self.assertEqual(fast[:3], slow[:3])
                self.assertEqual(fast[3], slow[3])
                self.assertIs(type(fast[2]), list)
        self.assertEqual(self.capture(blocks, 3000)[:2], ('duration_limit', 3000))

    def test_one_dimensional_and_other_dtypes_match(self):
        for block in (np.array([.1, -.2, .3], dtype=np.float64), np.array([[1], [0]], dtype=np.int16),
                      np.array([True, False]), np.zeros((0, 1), dtype=np.float32)):
            with self.subTest(dtype=str(block.dtype), shape=block.shape):
                self.assertEqual(self.capture([block]), self.capture([block.tolist()]))

    def test_invalid_ndarray_blocks_are_invalid_audio(self):
        biggest = np.finfo(np.float32).max
        for block in (np.array([[.1], [np.nan]], dtype=np.float32), np.array([[np.inf]], dtype=np.float32),
                      np.array([[-np.inf]], dtype=np.float32), np.array([[biggest]], dtype=np.float32),
                      np.array([[.1, .2]], dtype=np.float32), np.array([1e39])):
            with self.subTest(block=repr(block)):
                code, count, snapshot, _ = self.capture([np.zeros((4, 1), np.float32), block])
                self.assertEqual(code, 'invalid_audio')
                self.assertEqual(count, 4)
                self.assertEqual(snapshot, [0.0] * 4)
        # The pure-Python path rejects float32 max too, so both paths agree.
        self.assertEqual(self.capture([[[float(biggest)]]])[0], 'invalid_audio')
        self.assertEqual(self.capture([[float(biggest)]])[0], 'invalid_audio')

    def test_invalid_after_duration_limit_still_overrides(self):
        code, count, _, _ = self.capture([np.full((4, 1), .1, np.float32), np.array([[np.nan]], np.float32)], 3)
        self.assertEqual((code, count), ('invalid_audio', 3))

    def test_stale_callback_ndarray_is_ignored(self):
        callbacks = []
        class Stream:
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        recorder = AudioRecorder(stream_factory=lambda cb, *args: callbacks.append(cb) or Stream())
        recorder.start()
        callbacks[0](np.full((2, 1), .5, np.float32))
        recorder.stop()
        recorder.start()
        callbacks[0](np.full((2, 1), .8, np.float32))
        callbacks[1](np.full((1, 1), .25, np.float32))
        self.assertEqual(recorder.snapshot_samples(), [.25])
        recorder.stop()

    def test_inject_frame_lists_unchanged(self):
        recorder = AudioRecorder(max_samples=3)
        recorder.start()
        recorder.inject_frame([.125, .25])
        recorder.inject_frame([.5, .75])
        self.assertEqual(recorder.snapshot_samples(), [.125, .25, .5])
        self.assertEqual(recorder.capture_status.terminal_code, 'duration_limit')
        self.assertEqual(recorder.stop(), reference_wav_bytes([.125, .25, .5]))

    def test_python_fallback_accepts_ndarray_blocks(self):
        block = np.full((3, 1), .5, np.float32)
        with mock.patch.object(audio_capture, '_np_module', None):
            code, count, snapshot, wav_bytes = self.capture([block])
        self.assertEqual((code, count, snapshot), (None, 3, [.5] * 3))
        self.assertEqual(wav_bytes, reference_wav_bytes([.5] * 3))


class LazyNumpyImportTests(unittest.TestCase):
    def test_importing_audio_modules_does_not_import_numpy(self):
        # Spawned ASR and editor workers import services.asr and have a tight start-up budget.
        import subprocess
        import sys
        from pathlib import Path
        code = ('import sys\n'
                'import services.asr, services.asr.audio_capture, services.asr.vad, services.runtime.session\n'
                'print("numpy" in sys.modules)\n')
        completed = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), 'False')

    def test_capture_callback_never_triggers_first_numpy_import(self):
        with mock.patch.dict('sys.modules'), mock.patch.object(audio_capture, '_np_module', False):
            import sys
            del sys.modules['numpy']
            self.assertIsNone(audio_capture._loaded_numpy())
            self.assertNotIn('numpy', sys.modules)


if __name__ == '__main__':
    unittest.main()
