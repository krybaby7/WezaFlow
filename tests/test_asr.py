import io
import math
import unittest
import wave


class AudioRecorderTests(unittest.TestCase):
    def test_float_samples_convert_to_16khz_mono_wav_bytes(self):
        from services.asr.audio_capture import AudioRecorder

        wav_bytes = AudioRecorder.float_samples_to_wav_bytes([0.0, 0.5, -1.0, 1.0])

        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getnframes(), 4)
            frames = wav.readframes(4)

        samples = [
            int.from_bytes(frames[index : index + 2], "little", signed=True)
            for index in range(0, len(frames), 2)
        ]
        self.assertEqual(samples, [0, 16384, -32767, 32767])

    def test_recorder_collects_injected_frames_between_start_and_stop(self):
        from services.asr.audio_capture import AudioRecorder

        recorder = AudioRecorder()

        recorder.inject_frame([0.25])
        recorder.start()
        recorder.inject_frame([0.5, -0.5])
        recorder.inject_frame([1.5])
        wav_bytes = recorder.stop()

        self.assertFalse(recorder.is_recording)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual(wav.getnframes(), 3)

    def test_recorder_returns_snapshot_without_stopping_recording(self):
        from services.asr.audio_capture import AudioRecorder

        recorder = AudioRecorder()

        recorder.start()
        recorder.inject_frame([0.25, -0.5])
        snapshot = recorder.snapshot_samples()
        recorder.inject_frame([0.75])
        wav_bytes = recorder.stop()

        self.assertEqual(snapshot, [0.25, -0.5])
        self.assertFalse(recorder.is_recording)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual(wav.getnframes(), 3)

    def test_recorder_can_capture_from_injected_stream_factory(self):
        from services.asr.audio_capture import AudioRecorder

        class Stream:
            def __init__(self, callback):
                self.callback = callback
                self.stopped = False
                self.closed = False

            def start(self):
                self.callback([[0.25], [-0.25]], 2, None, None)

            def stop(self):
                self.stopped = True

            def close(self):
                self.closed = True

        streams = []

        def stream_factory(callback, sample_rate, blocksize):
            stream = Stream(callback)
            streams.append((stream, sample_rate, blocksize))
            return stream

        recorder = AudioRecorder(stream_factory=stream_factory, blocksize=512)

        recorder.start()
        wav_bytes = recorder.stop()

        self.assertTrue(streams[0][0].stopped)
        self.assertTrue(streams[0][0].closed)
        self.assertEqual(streams[0][1], 16000)
        self.assertEqual(streams[0][2], 512)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual(wav.getnframes(), 2)


class VADFilterTests(unittest.TestCase):
    def test_energy_fallback_returns_none_for_short_quiet_audio(self):
        from services.asr.vad import VADFilter

        vad = VADFilter(threshold=0.05, min_duration_ms=100, sample_rate=16000)

        self.assertIsNone(vad.filter([0.01] * 1600))

    def test_energy_fallback_keeps_audio_above_threshold_for_min_duration(self):
        from services.asr.vad import VADFilter

        vad = VADFilter(threshold=0.05, min_duration_ms=100, sample_rate=16000)
        audio = [0.0] * 400 + [0.1] * 1600 + [0.0] * 400

        self.assertEqual(vad.filter(audio), audio)

    def test_energy_fallback_keeps_quiet_recording_with_real_peak(self):
        from services.asr.vad import VADFilter

        vad = VADFilter(threshold=0.02, min_duration_ms=250, sample_rate=16000)
        audio = [0.0] * 32000
        audio[1000] = 0.038
        audio[1500] = -0.041

        self.assertEqual(vad.filter(audio), audio)

    def test_energy_fallback_trims_leading_and_trailing_silence_with_padding(self):
        from services.asr.vad import VADFilter

        vad = VADFilter(
            threshold=0.05,
            min_duration_ms=100,
            sample_rate=1000,
            trim_padding_ms=100,
        )
        audio = [0.0] * 500 + [0.1] * 200 + [0.0] * 500

        self.assertEqual(vad.filter(audio), [0.0] * 100 + [0.1] * 200 + [0.0] * 100)

    def test_trim_uses_lower_boundary_threshold_to_keep_soft_edge_words(self):
        from services.asr.vad import VADFilter

        vad = VADFilter(
            threshold=0.02,
            min_duration_ms=100,
            sample_rate=1000,
            trim_padding_ms=100,
            trim_threshold=0.005,
        )
        audio = [0.0] * 500 + [0.008] * 300 + [0.03] * 100 + [0.008] * 300 + [0.0] * 500

        self.assertEqual(vad.filter(audio), [0.0] * 100 + [0.008] * 300 + [0.03] * 100 + [0.008] * 300 + [0.0] * 100)
        self.assertEqual(vad.last_stats["input_sample_count"], 1700)
        self.assertEqual(vad.last_stats["output_sample_count"], 900)
        self.assertEqual(vad.last_stats["trim_start_samples"], 400)
        self.assertEqual(vad.last_stats["trim_end_samples"], 400)


class TranscriberTests(unittest.TestCase):
    def test_transcriber_uses_injected_backend_and_returns_asr_result(self):
        from services.asr.transcriber import Transcriber
        from services.pipeline.models import AsrResult

        class Backend:
            def transcribe(self, audio, language=None, initial_prompt=None):
                self.calls = [(audio, language, initial_prompt)]
                return {"text": "hello world", "language": language or "en", "duration_ms": 42}

        backend = Backend()
        result = Transcriber(backend=backend).transcribe(
            b"wav", language="en", initial_prompt="Names: LocalFlow"
        )

        self.assertIsInstance(result, AsrResult)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.duration_ms, 42)
        self.assertEqual(backend.calls, [(b"wav", "en", "Names: LocalFlow")])

    def test_transcriber_reuses_lazy_default_backend(self):
        import services.asr.transcriber as transcriber_module

        original_backend = transcriber_module.FasterWhisperBackend

        class Backend:
            created = 0

            def __init__(self, *args, **kwargs):
                Backend.created += 1

            def transcribe(self, audio, language=None, initial_prompt=None):
                return {"text": "hello", "language": language, "duration_ms": 1}

        transcriber_module.FasterWhisperBackend = Backend
        try:
            transcriber = transcriber_module.Transcriber()
            transcriber.transcribe([0.1], language="en")
            transcriber.transcribe([0.2], language="en")
        finally:
            transcriber_module.FasterWhisperBackend = original_backend

        self.assertEqual(Backend.created, 1)

    def test_faster_whisper_backend_converts_python_samples_to_float32_array(self):
        from services.asr.transcriber import FasterWhisperBackend

        class FakeArray:
            def __init__(self, values, dtype):
                self.values = values
                self.dtype = dtype

        class FakeNumpy:
            float32 = "float32"

            @staticmethod
            def asarray(values, dtype=None):
                return FakeArray(values, dtype)

        class Model:
            def __init__(self):
                self.seen_audio = None

            def transcribe(self, audio, language=None, initial_prompt=None):
                self.seen_audio = audio
                return {"text": "hello", "language": language, "duration_ms": 1}

        model = Model()
        backend = FasterWhisperBackend(numpy_module=FakeNumpy())
        backend._model = model

        backend.transcribe([0.0, 0.1, -0.1], language="en")

        self.assertIsInstance(model.seen_audio, FakeArray)
        self.assertEqual(model.seen_audio.values, [0.0, 0.1, -0.1])
        self.assertEqual(model.seen_audio.dtype, "float32")

    def test_transcriber_normalizes_faster_whisper_segment_results(self):
        from services.asr.transcriber import Transcriber

        class Segment:
            text = "hello"

        class Info:
            language = "en"
            duration = 1.25

        class Backend:
            def transcribe(self, audio, language=None, initial_prompt=None):
                return [Segment()], Info()

        result = Transcriber(backend=Backend()).transcribe([math.sin(1.0)])

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.duration_ms, 1250)

    def test_transcriber_warm_up_reuses_lazy_backend(self):
        import services.asr.transcriber as transcriber_module

        original_backend = transcriber_module.FasterWhisperBackend

        class Backend:
            created = 0

            def __init__(self, *args, **kwargs):
                Backend.created += 1
                self.warmups = []

            def warm_up(self, language=None):
                self.warmups.append(language)

            def transcribe(self, audio, language=None, initial_prompt=None):
                return {"text": "hello", "language": language, "duration_ms": 1}

        transcriber_module.FasterWhisperBackend = Backend
        try:
            transcriber = transcriber_module.Transcriber()
            transcriber.warm_up(language="en")
            transcriber.transcribe([0.2], language="en")
        finally:
            transcriber_module.FasterWhisperBackend = original_backend

        self.assertEqual(Backend.created, 1)
        self.assertEqual(transcriber.backend.warmups, ["en"])

class TranscriberContractTests(unittest.TestCase):
    def test_lazy_segments_are_consumed_under_lock_for_transcribe_and_warmup(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, RLock
        from types import SimpleNamespace
        from services.asr.transcriber import Transcriber

        for operation in ('transcribe', 'warm_up'):
            with self.subTest(operation=operation):
                entered, release, attempted = Event(), Event(), Event()
                calls = []
                class ObservedLock:
                    def __init__(self):
                        self.lock = RLock()
                    def __enter__(self):
                        if entered.is_set():
                            attempted.set()
                        self.lock.acquire()
                    def __exit__(self, *args):
                        self.lock.release()
                class Backend:
                    def transcribe(self, *args, **kwargs):
                        calls.append('transcribe')
                        def segments():
                            entered.set()
                            if not release.wait(3):
                                raise RuntimeError('test release timed out')
                            calls.append('consumed')
                            yield SimpleNamespace(text='hello')
                        return segments(), SimpleNamespace(language='en', duration=1)
                    def warm_up(self, **kwargs):
                        calls.append('warm_up')
                transcriber = Transcriber(backend=Backend())
                transcriber._lock = ObservedLock()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(transcriber.transcribe, [])
                    self.assertTrue(entered.wait(3))
                    acquired = transcriber._lock.lock.acquire(blocking=False)
                    if acquired:
                        transcriber._lock.lock.release()
                        release.set()
                    self.assertFalse(acquired, 'lazy inference must retain the transcriber lock')
                    second = pool.submit(getattr(transcriber, operation), *([[]] if operation == 'transcribe' else []))
                    try:
                        self.assertTrue(attempted.wait(3))
                        self.assertEqual(calls, ['transcribe'])
                    finally:
                        release.set()
                    first.result(3)
                    second.result(3)
                self.assertLess(calls.index('consumed'), calls.index(operation, 1))

    def test_explicit_timings_include_lazy_work_and_separate_queue(self):
        from types import SimpleNamespace
        from services.asr.transcriber import Transcriber
        now = [0.0]
        class Lock:
            def __enter__(self):
                now[0] += 2
            def __exit__(self, *args):
                pass
        class Backend:
            def transcribe(self, *args, **kwargs):
                now[0] += 3
                def segments():
                    now[0] += 4
                    yield SimpleNamespace(text='hello')
                return segments(), SimpleNamespace(language='ar', duration=1.25)
        transcriber = Transcriber(backend=Backend(), clock=lambda: now[0])
        transcriber._lock = Lock()
        result = transcriber.transcribe([])
        self.assertEqual(result.inference_ms, 7000)
        self.assertEqual(result.queue_ms, 2000)
        self.assertEqual(result.audio_duration_ms, 1250)
        self.assertEqual(result.duration_ms, 1250)

    def test_generator_failure_releases_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from services.asr.transcriber import Transcriber
        class Backend:
            def transcribe(self, *args, **kwargs):
                def segments():
                    raise ValueError('broken segments')
                    yield
                return segments(), SimpleNamespace()
            def warm_up(self, **kwargs):
                return None
        transcriber = Transcriber(backend=Backend())
        with self.assertRaisesRegex(ValueError, 'broken segments'):
            transcriber.transcribe([])
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(transcriber.warm_up).result(3)

    def test_dict_and_existing_result_keep_metadata_and_legacy_duration(self):
        from services.asr.transcriber import Transcriber
        from services.pipeline.models import AsrResult
        for raw in ({'text': 'hi', 'language': 'ar', 'duration_ms': 42, 'audio_duration_ms': 900},
                    AsrResult('hi', 'ar', 42, audio_duration_ms=900)):
            with self.subTest(raw=raw):
                class Backend:
                    def transcribe(self, *args, **kwargs):
                        return raw
                ticks = iter([0, 2, 5])
                result = Transcriber(backend=Backend(), clock=lambda: next(ticks)).transcribe([])
                self.assertEqual((result.text, result.language, result.duration_ms), ('hi', 'ar', 42))
                self.assertEqual(result.audio_duration_ms, 900)
                self.assertEqual((result.inference_ms, result.queue_ms), (3000, 2000))


class AudioRecorderFailureTests(unittest.TestCase):
    def test_stop_and_close_drain_final_callbacks_before_invalidating_capture(self):
        from services.asr.audio_capture import AudioRecorder
        callbacks = []
        class Stream:
            def start(self):
                callbacks[-1]([[0.1]])
            def stop(self):
                callbacks[-1]([[0.2]])
            def close(self):
                callbacks[-1]([[0.3]])
        def factory(callback, *args):
            callbacks.append(callback)
            return Stream()
        recorder = AudioRecorder(stream_factory=factory)
        recorder.start()
        data = recorder.stop()
        with wave.open(io.BytesIO(data), 'rb') as wav:
            self.assertEqual(wav.getnframes(), 3)
            frames = wav.readframes(3)
        self.assertEqual([int.from_bytes(frames[i:i+2], 'little', signed=True)
                          for i in range(0, len(frames), 2)], [3277, 6553, 9830])
        callbacks[0]([[0.4]])
        self.assertEqual(recorder.snapshot_samples(), [])
        recorder.start()
        callbacks[0]([[0.8]])
        self.assertAlmostEqual(recorder.snapshot_samples()[0], 0.1, places=6)
        recorder.stop()


    def test_failure_cleanup_resets_state_discards_frames_and_allows_retry(self):
        from services.asr.audio_capture import AudioRecorder
        for stage in ('factory', 'start', 'stop', 'close'):
            for failure in (ValueError('failure'), KeyboardInterrupt('cancel')):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    callbacks, streams = [], []
                    failing = [True]
                    class Stream:
                        def __init__(self):
                            self.closed = 0
                            self.stopped = 0
                        def start(self):
                            callbacks[-1]([[0.25]])
                            if failing[0] and stage == 'start':
                                raise failure
                        def stop(self):
                            self.stopped += 1
                            callbacks[-1]([[0.9]])
                            if failing[0] and stage == 'stop':
                                raise failure
                        def close(self):
                            self.closed += 1
                            if failing[0] and stage in ('start', 'stop', 'close'):
                                raise failure if stage == 'close' else RuntimeError('cleanup failure')
                    def factory(callback, *args):
                        callbacks.append(callback)
                        callback([[0.1]])
                        if failing[0] and stage == 'factory':
                            raise failure
                        stream = Stream()
                        streams.append(stream)
                        return stream
                    recorder = AudioRecorder(stream_factory=factory)
                    with self.assertRaises(BaseException) as raised:
                        recorder.start()
                        recorder.stop()
                    self.assertIs(raised.exception, failure)
                    self.assertFalse(recorder.is_recording)
                    if streams:
                        self.assertIs(recorder._stream, streams[0])
                        self.assertTrue(recorder.capture_status.cleanup_pending)
                        with self.assertRaises(RuntimeError): recorder.start()
                    else:
                        self.assertIsNone(recorder._stream)
                    self.assertEqual(recorder.snapshot_samples(), [])
                    if streams:
                        self.assertEqual(streams[0].closed, 1)
                    old_callback = callbacks[0]
                    failing[0] = False
                    recorder.stop()
                    self.assertFalse(recorder.capture_status.cleanup_pending)
                    if streams:
                        self.assertEqual(streams[0].closed, 2)
                    recorder.start()
                    old_callback([[0.8]])
                    self.assertEqual(len(recorder.snapshot_samples()), 2)
                    self.assertAlmostEqual(recorder.snapshot_samples()[0], 0.1, places=6)
                    self.assertEqual(recorder.snapshot_samples()[1], 0.25)
                    data = recorder.stop()
                    with wave.open(io.BytesIO(data), 'rb') as wav:
                        self.assertEqual(wav.getnframes(), 3)
                    self.assertEqual(recorder.snapshot_samples(), [])
                    self.assertFalse(recorder.is_recording)


if __name__ == "__main__":
    unittest.main()
