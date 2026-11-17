import threading
import time
import unittest
from uuid import uuid4

from services.asr.audio_capture import AudioRecorder
from services.runtime import controller as module


class TailController(module.SerializedSessionController):
    """Exposes the tail wait so tests can act inside it without sleeps."""

    def __init__(self, *args, hold_tail=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.tail_calls = []
        self.tail_entered = threading.Event()
        self.tail_release = threading.Event()
        self.hold_tail = hold_tail

    def _wait_release_tail(self, seconds):
        self.tail_calls.append(seconds)
        self.tail_entered.set()
        if self.hold_tail and not self.tail_release.wait(3):
            raise RuntimeError('test barrier timed out')
        super()._wait_release_tail(seconds)


class ReleaseTailTests(unittest.TestCase):
    def setUp(self):
        self.sid = str(uuid4())

    def make(self, audio=None, *, hold_tail=False, recorder=None):
        calls = []
        received = []
        recorder = recorder or AudioRecorder()
        class Pipeline:
            def process_audio(self, audio, **kwargs):
                calls.append('process')
                received.append(audio)
                return {'raw_transcript': 'hello', 'polished_text': 'Hello.'}
        settings = {'audio': {'sample_rate': 16000, 'channels': 1, **(audio or {})}}
        c = TailController(factory=lambda snapshot: (recorder, Pipeline()),
                           settings_provider=lambda: settings, engine_id='engine', hold_tail=hold_tail)
        self.addCleanup(c.close)
        return c, recorder, calls, received

    def command(self, c, action, seq, binding='key'):
        return c.command(action, {'engine_id': 'engine', 'protocol_version': 1, 'session_id': self.sid,
                                  'command_seq': seq, 'binding_id': binding, 'mode': 'dictation',
                                  'language': None})

    def test_frames_after_release_are_kept_and_state_stays_draining(self):
        c, recorder, calls, received = self.make({'release_tail_ms': 50}, hold_tail=True)
        self.assertTrue(self.command(c, 'start', 1)['accepted'])
        self.assertTrue(self.command(c, 'stop', 2)['accepted'])
        try:
            self.assertTrue(c.tail_entered.wait(3))
            self.assertTrue(recorder.is_recording)
            self.assertEqual(c.snapshot()['state'], 'draining')
            recorder.inject_frame([.25])
        finally:
            c.tail_release.set()
        self.assertTrue(c.wait_for_worker(3))
        self.assertEqual(c.tail_calls, [.05])
        self.assertEqual(c.snapshot()['state'], 'ready')
        self.assertEqual(received, [[.25]])

    def test_absent_or_zero_tail_stops_immediately(self):
        for audio in ({}, {'release_tail_ms': 0}):
            with self.subTest(audio=audio):
                c, _, calls, _ = self.make(audio)
                self.command(c, 'start', 1); self.command(c, 'stop', 2)
                self.assertTrue(c.wait_for_worker(3))
                self.assertEqual(c.tail_calls, [])
                self.assertEqual(calls, ['process'])

    def test_cancel_during_tail_interrupts_wait_and_suppresses_processing(self):
        c, recorder, calls, _ = self.make({'release_tail_ms': 500})
        self.command(c, 'start', 1); self.command(c, 'stop', 2)
        self.assertTrue(c.tail_entered.wait(3))
        started = time.monotonic()
        self.assertTrue(self.command(c, 'cancel', 3, binding='escape')['accepted'])
        self.assertTrue(c.wait_for_worker(3))
        self.assertLess(time.monotonic() - started, .4)
        snapshot = c.snapshot()
        self.assertEqual(snapshot['state'], 'cancelled')
        self.assertIsNone(snapshot['result'])
        self.assertIsNone(snapshot['capture'])
        self.assertEqual(calls, [])
        self.assertFalse(recorder.is_recording)

    def test_shutdown_during_tail_interrupts_wait(self):
        c, recorder, calls, _ = self.make({'release_tail_ms': 500})
        self.command(c, 'start', 1); self.command(c, 'stop', 2)
        self.assertTrue(c.tail_entered.wait(3))
        started = time.monotonic()
        c.close()
        self.assertTrue(c.wait_for_worker(3))
        self.assertLess(time.monotonic() - started, .4)
        self.assertEqual(calls, [])
        self.assertFalse(recorder.is_recording)

    def test_terminal_capture_code_skips_tail(self):
        class Capped(AudioRecorder):
            def start(self):
                super().start()
                self.inject_frame([.1] * 4)
        c, _, calls, _ = self.make({'release_tail_ms': 500}, recorder=Capped(max_samples=3))
        self.command(c, 'start', 1)
        self.assertTrue(c.wait_for_worker(3))
        self.assertEqual(c.tail_calls, [])
        self.assertEqual(c.snapshot()['result']['diagnostics']['capture']['terminal_code'], 'duration_limit')

    def test_cancel_while_recording_skips_tail(self):
        gate = threading.Event()
        class Recorder(AudioRecorder):
            def start(self):
                super().start()
                gate.set()
        c, _, calls, _ = self.make({'release_tail_ms': 500}, recorder=Recorder())
        self.command(c, 'start', 1)
        self.assertTrue(gate.wait(3))
        self.command(c, 'cancel', 2)
        self.assertTrue(c.wait_for_worker(3))
        self.assertEqual(c.tail_calls, [])
        self.assertEqual(calls, [])

    def test_release_tail_validation(self):
        for value in (0, 1, 120, 12.5, 500):
            with self.subTest(value=value):
                module.validate_session_settings({'audio': {'release_tail_ms': value}})
        for value in (-1, 500.5, 501, True, float('nan'), float('inf'), '100', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate_session_settings({'audio': {'release_tail_ms': value}})


if __name__ == '__main__':
    unittest.main()
