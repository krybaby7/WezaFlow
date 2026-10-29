import unittest
from services.asr.audio_capture import AudioRecorder

class BoundedCaptureTests(unittest.TestCase):
    def test_cap_and_fatal_drain(self):
        recorder = AudioRecorder(max_samples=3)
        recorder.start()
        recorder.inject_frame([.1,.2,.3,.4])
        self.assertEqual(len(recorder.snapshot_samples()), 3)
        self.assertEqual(recorder.capture_status.terminal_code, 'duration_limit')
        recorder._callback([[float('nan')]])
        self.assertEqual(recorder.capture_status.terminal_code, 'invalid_audio')
        recorder.stop()
        self.assertEqual(recorder.capture_status.sample_count, 3)
        self.assertEqual(recorder.snapshot_samples(), [])

    def test_failed_close_retains_ownership(self):
        class Stream:
            def start(self): pass
            def stop(self): pass
            def close(self):
                if not getattr(self, 'failed', False):
                    self.failed = True
                    raise RuntimeError('close')
        recorder = AudioRecorder(stream_factory=lambda *args: Stream())
        recorder.start()
        with self.assertRaises(RuntimeError): recorder.stop()
        self.assertTrue(recorder.capture_status.cleanup_pending)
        with self.assertRaises(RuntimeError): recorder.start()
        recorder.stop()
        self.assertFalse(recorder.capture_status.cleanup_pending)

class ControllerCaptureTests(unittest.TestCase):
    def test_limit_automatically_drains_and_binds_diagnostics(self):
        from services.runtime.controller import SerializedSessionController
        from uuid import uuid4
        class Recorder(AudioRecorder):
            def start(self):
                super().start()
                self.inject_frame([.1]*4)
        class Pipeline:
            def process_audio(self, audio, **kwargs):
                return {'raw_transcript':'hello','polished_text':'Hello'}
        c = SerializedSessionController(factory=lambda settings:(Recorder(max_samples=3),Pipeline()), settings_provider=lambda:{}, engine_id='e')
        c.command('start',dict(engine_id='e',protocol_version=1,session_id=str(uuid4()),command_seq=1,binding_id='k'))
        self.assertTrue(c.wait_for_worker(3))
        self.assertEqual(c.snapshot()['result']['diagnostics']['capture']['terminal_code'], 'duration_limit')
        c.close()

class NormalizationTests(unittest.TestCase):
    def test_rejects_rate_and_stereo(self):
        import io, wave
        from services.runtime.session import wav_bytes_to_float_samples
        for channels, rate in ((2,16000),(1,8000)):
            out=io.BytesIO()
            with wave.open(out,'wb') as wav:
                wav.setnchannels(channels); wav.setsampwidth(2); wav.setframerate(rate); wav.writeframes(b'\0\0'*channels)
            with self.assertRaises(RuntimeError): wav_bytes_to_float_samples(out.getvalue())

class CaptureTerminalCases(unittest.TestCase):
    def test_shapes_nonfinite_overflow_and_device_facts(self):
        for data, code in (([[1,2]],'invalid_audio'),([float('inf')],'invalid_audio')):
            r=AudioRecorder(); r.start(); r._callback(data)
            self.assertEqual(r.capture_status.terminal_code,code); r.stop()
        r=AudioRecorder(); r.start(); r._callback([.1],status=True)
        self.assertEqual(r.capture_status.terminal_code,'input_overflow'); r.stop()
        class Stream:
            active=True
            samplerate=8000
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        r=AudioRecorder(stream_factory=lambda *args:Stream());r.start()
        self.assertEqual(r.poll_capture_status().terminal_code,'sample_rate_mismatch');r.stop()

    def test_fatal_final_drain_prevents_asr(self):
        from services.runtime.controller import SerializedSessionController
        from uuid import uuid4
        calls=[]
        class Stream:
            def __init__(self,callback): self.callback=callback
            def start(self): self.callback([.1,.2,.3])
            def stop(self): self.callback([.4],status=True)
            def close(self): pass
        class Pipeline:
            def process_audio(self,*args,**kwargs): calls.append('ASR')
        r=AudioRecorder(max_samples=3,stream_factory=lambda cb,*args:Stream(cb))
        c=SerializedSessionController(factory=lambda settings:(r,Pipeline()),settings_provider=lambda:{},engine_id='e')
        c.command('start',dict(engine_id='e',protocol_version=1,session_id=str(uuid4()),command_seq=1,binding_id='k'))
        self.assertTrue(c.wait_for_worker(3));self.assertEqual(c.snapshot()['state'],'error');self.assertEqual(calls,[])
        self.assertEqual(c.snapshot()['capture']['terminal_code'],'input_overflow');c.close()

    def test_controller_retains_failed_cleanup_until_shutdown_retry(self):
        from services.runtime.controller import SerializedSessionController
        from uuid import uuid4
        import time
        class Stream:
            def start(self): pass
            def stop(self): pass
            def close(self):
                if not getattr(self,'failed',False):
                    self.failed=True;raise RuntimeError('secret')
        r=AudioRecorder(stream_factory=lambda *args:Stream())
        c=SerializedSessionController(factory=lambda settings:(r,object()),settings_provider=lambda:{},engine_id='e')
        sid=str(uuid4())
        def cmd(action,seq,sid=sid): return c.command(action,dict(engine_id='e',protocol_version=1,session_id=sid,command_seq=seq,binding_id='k'))
        cmd('start',1);cmd('stop',2)
        self.assertFalse(c.wait_for_worker(.1));self.assertTrue(c.snapshot()['busy'])
        self.assertEqual(cmd('start',3,str(uuid4()))['reason'],'busy')
        self.assertIn('Restart Wezza',c.snapshot()['last_error'])
        c.close();self.assertTrue(c.wait_for_worker(3));self.assertFalse(r.capture_status.cleanup_pending)

class CaptureConfigurationTests(unittest.TestCase):
    def test_invalid_constructor_primitives(self):
        for kwargs in ({'sample_rate': True},{'sample_rate':8000},{'blocksize':0},{'blocksize':1.5}):
            with self.assertRaises(ValueError): AudioRecorder(**kwargs)
    def test_nonfinite_device_rate_is_json_safe(self):
        import json
        class Stream:
            samplerate=float('nan')
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        r=AudioRecorder(stream_factory=lambda *args:Stream());r.start()
        status=r.poll_capture_status()
        self.assertEqual(status.terminal_code,'sample_rate_mismatch')
        self.assertIsNone(status.actual_sample_rate)
        r.stop()

class MalformedShapeTests(unittest.TestCase):
    def test_nested_channel_vector_is_not_mono(self):
        r=AudioRecorder();r.start();r._callback([[[.1]]])
        self.assertEqual(r.capture_status.terminal_code,'invalid_audio');r.stop()

class IncompleteWavTests(unittest.TestCase):
    def test_rejects_partial_last_frame(self):
        import io, wave
        from services.runtime.session import wav_bytes_to_float_samples
        out=io.BytesIO()
        with wave.open(out,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0\0\0')
        with self.assertRaises(RuntimeError): wav_bytes_to_float_samples(out.getvalue())

class AutomaticTerminalLifecycleTests(unittest.TestCase):
    def test_exact_cap_preserves_order_and_later_callbacks_cannot_grow_buffer(self):
        recorder = AudioRecorder(max_samples=3)
        self.addCleanup(recorder.stop)
        recorder.start()
        recorder._callback([.125, .25])
        recorder._callback([.5])
        self.assertEqual(recorder.capture_status.terminal_code, 'duration_limit')
        self.assertEqual(recorder.snapshot_samples(), [.125, .25, .5])
        recorder._callback([.75, 1.0])
        self.assertEqual(recorder.snapshot_samples(), [.125, .25, .5])
        self.assertEqual(recorder.capture_status.sample_count, 3)

    def test_unexpected_device_stop_automatically_drains_without_asr(self):
        import threading
        from uuid import uuid4
        from services.runtime.controller import SerializedSessionController

        started = threading.Event()
        calls = []

        class Stream:
            active = True
            # No samplerate capability: actual rate must remain unknown.
            def start(self):
                calls.append('start')
                started.set()
            def stop(self):
                calls.append('stop')
            def close(self):
                calls.append('close')

        class Pipeline:
            def process_audio(self, *args, **kwargs):
                calls.append('ASR')
                raise AssertionError('Unexpected device termination reached ASR')

        stream = Stream()
        recorder = AudioRecorder(stream_factory=lambda *args: stream)
        controller = SerializedSessionController(
            factory=lambda settings: (recorder, Pipeline()),
            settings_provider=lambda: {}, engine_id='e')
        self.addCleanup(controller.close)
        reply = controller.command('start', {
            'engine_id': 'e', 'protocol_version': 1, 'session_id': str(uuid4()),
            'command_seq': 1, 'binding_id': 'k'})
        self.assertTrue(reply['accepted'])
        self.assertTrue(started.wait(3))
        stream.active = False
        self.assertTrue(controller.wait_for_worker(3))
        snapshot = controller.snapshot()
        self.assertEqual(calls, ['start', 'stop', 'close'])
        self.assertEqual(snapshot['state'], 'error')
        self.assertEqual(snapshot['last_error'],
                         'Microphone capture failed. Check the input device and try again.')
        self.assertEqual(snapshot['capture']['terminal_code'], 'device_stopped')
        self.assertIsNone(snapshot['capture']['actual_sample_rate'])
        self.assertFalse(snapshot['capture']['cleanup_pending'])
        self.assertFalse(snapshot['busy'])
        self.assertIsNone(snapshot['result'])

    def test_cancellation_during_automatic_capped_drain_discards_publication(self):
        import threading
        from uuid import uuid4
        from services.runtime.controller import SerializedSessionController

        draining = threading.Event()
        release_drain = threading.Event()
        calls = []

        class Stream:
            def __init__(self, callback):
                self.callback = callback
            def start(self):
                calls.append('start')
                self.callback([.125, .25, .5])
            def stop(self):
                calls.append('stop')
                draining.set()
                if not release_drain.wait(3):
                    raise RuntimeError('Test drain barrier timed out')
                self.callback([.75])
            def close(self):
                calls.append('close')

        class Pipeline:
            def process_audio(self, *args, **kwargs):
                calls.append('ASR')
                return {'raw_transcript': 'must not publish', 'polished_text': 'Must not publish.'}

        recorder = AudioRecorder(max_samples=3, stream_factory=lambda cb, *args: Stream(cb))
        controller = SerializedSessionController(
            factory=lambda settings: (recorder, Pipeline()),
            settings_provider=lambda: {}, engine_id='e')
        self.addCleanup(controller.close)
        sid = str(uuid4())
        envelope = {'engine_id': 'e', 'protocol_version': 1,
                    'session_id': sid, 'binding_id': 'k'}
        self.assertTrue(controller.command('start', {**envelope, 'command_seq': 1})['accepted'])
        try:
            self.assertTrue(draining.wait(3))
            before_cancel = controller.snapshot()
            self.assertEqual(before_cancel['state'], 'draining')
            self.assertEqual(before_cancel['capture']['terminal_code'], 'duration_limit')
            self.assertEqual(recorder.snapshot_samples(), [.125, .25, .5])
            self.assertTrue(controller.command('cancel', {**envelope, 'command_seq': 2})['accepted'])
            self.assertIsNone(controller.snapshot()['capture'])
        finally:
            release_drain.set()
        self.assertTrue(controller.wait_for_worker(3))
        snapshot = controller.snapshot()
        self.assertEqual(calls, ['start', 'stop', 'close'])
        self.assertEqual(snapshot['state'], 'cancelled')
        self.assertIsNone(snapshot['capture'])
        self.assertIsNone(snapshot['result'])
        self.assertIsNone(snapshot['last_error'])
        self.assertFalse(snapshot['busy'])
        self.assertFalse(recorder.capture_status.cleanup_pending)
        self.assertEqual(recorder.capture_status.sample_count, 3)
        self.assertEqual(recorder.snapshot_samples(), [])
