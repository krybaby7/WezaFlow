import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from services.pipeline.models import AppContext, AsrResult
from services.pipeline.orchestrator import PipelineOrchestrator
from services.runtime.controller import SerializedSessionController


class EmptyAsrOutcomeTests(unittest.TestCase):
    def pipeline(self, transcribe):
        formatter = Mock()
        formatter.format.return_value = SimpleNamespace(text='formatted')
        formatter.command_edit.return_value = SimpleNamespace(text='edited')
        snippets = Mock()
        snippets.expand.return_value = None
        injector = Mock()
        pipeline = PipelineOrchestrator(
            vad=SimpleNamespace(filter=lambda audio: audio),
            transcriber=SimpleNamespace(transcribe=transcribe),
            formatter=formatter,
            context_provider=lambda: AppContext('test', '', 'other'),
            injector=injector,
            snippets=snippets,
        )
        return pipeline, formatter, snippets, injector

    def invoke(self, pipeline, mode):
        if mode == 'dictation':
            return pipeline.process_audio([0.2])
        return pipeline.process_command('selected', [0.2])

    def test_empty_asr_skips_all_text_processing_and_injection(self):
        for mode in ('dictation', 'command'):
            for text in ('', ' \t\r\n', '\u00a0\u2003\u202f\u3000'):
                with self.subTest(mode=mode, text=repr(text)):
                    pipeline, formatter, snippets, injector = self.pipeline(
                        lambda *a, **k: AsrResult(text, 'en', 1))
                    self.assertIsNone(self.invoke(pipeline, mode))
                    formatter.format.assert_not_called()
                    formatter.command_edit.assert_not_called()
                    snippets.expand.assert_not_called()
                    injector.inject.assert_not_called()

    def test_nonempty_text_reaches_processing_unchanged(self):
        for mode in ('dictation', 'command'):
            for text in ('  hello world  ', ' \u0645\u0631\u062d\u0628\u0627 \u4e16\u754c\n', ' \t...!?\u061f '):
                with self.subTest(mode=mode, text=text):
                    pipeline, formatter, snippets, injector = self.pipeline(
                        lambda *a, **k: AsrResult(text, None, 1))
                    result = self.invoke(pipeline, mode)
                    self.assertEqual(result.raw_transcript, text)
                    if mode == 'dictation':
                        snippets.expand.assert_called_once_with(text)
                        self.assertEqual(formatter.format.call_args.args[0], text)
                        self.assertEqual(result.polished_text, 'formatted')
                    else:
                        formatter.command_edit.assert_called_once_with('selected', text)
                        self.assertEqual(result.polished_text, 'edited')
                    injector.inject.assert_not_called()

    def test_backend_failure_propagates_without_text_processing(self):
        failure = RuntimeError('decode failed')
        for mode in ('dictation', 'command'):
            with self.subTest(mode=mode):
                pipeline, formatter, snippets, injector = self.pipeline(Mock(side_effect=failure))
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke(pipeline, mode)
                self.assertIs(caught.exception, failure)
                formatter.format.assert_not_called()
                formatter.command_edit.assert_not_called()
                snippets.expand.assert_not_called()
                injector.inject.assert_not_called()

    def controller(self, pipeline):
        recorder = SimpleNamespace(start=lambda: None, stop=lambda: [0.2])
        controller = SerializedSessionController(
            factory=lambda settings: (recorder, pipeline),
            settings_provider=lambda: {}, engine_id='engine')
        self.addCleanup(controller.close)
        return controller

    def command(self, controller, action, seq, sid):
        response = controller.command(action, {
            'engine_id': 'engine', 'protocol_version': 1,
            'session_id': sid, 'command_seq': seq, 'binding_id': 'key',
            'mode': 'dictation', 'language': None,
        })
        self.assertTrue(response['accepted'], response)

    def test_controller_no_speech_preserves_previous_result_and_origin(self):
        texts = iter(('previous', '\u2003\t'))
        pipeline, formatter, snippets, injector = self.pipeline(
            lambda *a, **k: AsrResult(next(texts), None, 1))
        controller = self.controller(pipeline)
        previous_sid, empty_sid = str(uuid4()), str(uuid4())
        self.command(controller, 'start', 1, previous_sid)
        self.command(controller, 'stop', 2, previous_sid)
        self.assertTrue(controller.wait_for_worker(3))
        previous = controller.snapshot()
        self.assertEqual(previous['state'], 'ready')
        self.command(controller, 'start', 3, empty_sid)
        self.command(controller, 'stop', 4, empty_sid)
        self.assertTrue(controller.wait_for_worker(3))
        current = controller.snapshot()
        self.assertEqual(current['state'], 'no_speech')
        self.assertFalse(current['busy'])
        self.assertIsNone(current['last_error'])
        self.assertEqual(current['session_id'], empty_sid)
        for key in ('result', 'result_session_id', 'result_generation'):
            self.assertEqual(current[key], previous[key])
        self.assertEqual(current['result_session_id'], previous_sid)
        self.assertEqual(formatter.format.call_count, 1)
        self.assertEqual(snippets.expand.call_count, 1)
        injector.inject.assert_not_called()

    def test_controller_cancel_remains_authoritative_after_empty_decode(self):
        entered, release = threading.Event(), threading.Event()
        def transcribe(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('decode barrier timed out')
            return AsrResult('', None, 1)
        pipeline, formatter, snippets, injector = self.pipeline(transcribe)
        controller = self.controller(pipeline)
        self.addCleanup(release.set)
        sid = str(uuid4())
        self.command(controller, 'start', 1, sid)
        self.command(controller, 'stop', 2, sid)
        self.assertTrue(entered.wait(3))
        self.command(controller, 'cancel', 3, sid)
        self.assertEqual(controller.snapshot()['state'], 'cancelled')
        release.set()
        self.assertTrue(controller.wait_for_worker(3))
        current = controller.snapshot()
        self.assertEqual(current['state'], 'cancelled')
        self.assertFalse(current['busy'])
        self.assertIsNone(current['result'])
        self.assertIsNone(current['last_error'])
        formatter.format.assert_not_called()
        snippets.expand.assert_not_called()
        injector.inject.assert_not_called()


if __name__ == '__main__':
    unittest.main()
