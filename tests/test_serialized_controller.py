import threading
import unittest
from uuid import uuid4

from services.runtime import controller as module


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
    def __call__(self):
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('test barrier timed out')


class ControllerTests(unittest.TestCase):
    def make(self, *, factory_gate=None, start_gate=None, stop_gate=None, process_gate=None, failure=None):
        calls = []
        class Recorder:
            def start(self):
                calls.append('start')
                if start_gate: start_gate()
                if failure == 'start': raise RuntimeError('failed')
            def stop(self):
                calls.append('stop')
                if stop_gate: stop_gate()
                if failure == 'stop': raise RuntimeError('failed')
                return [0.2]
        class Pipeline:
            def process_audio(self, audio, **kwargs):
                calls.append('process')
                if process_gate: process_gate()
                if failure == 'process': raise RuntimeError('failed')
                return {'raw_transcript': 'hello', 'polished_text': 'Hello.'}
        def factory(settings):
            calls.append('factory')
            if factory_gate: factory_gate()
            if failure == 'factory': raise RuntimeError('failed')
            return Recorder(), Pipeline()
        controller = module.SerializedSessionController(factory=factory, settings_provider=lambda: {'audio': {'sample_rate': 16000, 'channels': 1}} , engine_id='engine')
        self.addCleanup(controller.close)
        return controller, calls
    def command(self, c, action, seq, sid=None, binding='key'):
        return c.command(action, {'engine_id': 'engine', 'protocol_version': 1, 'session_id': sid or self.sid, 'command_seq': seq, 'binding_id': binding, 'mode': 'dictation', 'language': None})
    def setUp(self): self.sid = str(uuid4())
    def test_release_before_start_and_duplicate(self):
        gate = Gate(); c, calls = self.make(start_gate=gate)
        self.assertTrue(self.command(c, 'start', 1)['accepted'])
        self.assertTrue(gate.entered.wait(3))
        self.assertTrue(self.command(c, 'stop', 2)['accepted'])
        self.assertTrue(self.command(c, 'stop', 2)['accepted'])
        gate.release.set(); self.assertTrue(c.wait_for_worker(3))
        self.assertEqual(c.snapshot()['state'], 'ready')
        self.assertEqual(calls, ['factory', 'start', 'stop', 'process'])
    def test_cancel_processing_keeps_worker_busy_and_discards_result(self):
        gate = Gate(); c, calls = self.make(process_gate=gate)
        self.command(c, 'start', 1); self.command(c, 'stop', 2)
        self.assertTrue(gate.entered.wait(3))
        self.assertTrue(self.command(c, 'cancel', 3, binding='escape')['accepted'])
        self.assertEqual(c.snapshot()['state'], 'cancelled')
        self.assertEqual(self.command(c, 'start', 4, str(uuid4()))['reason'], 'busy')
        gate.release.set(); self.assertTrue(c.wait_for_worker(3))
        self.assertIsNone(c.snapshot()['result'])
    def test_cross_binding_release_is_rejected(self):
        gate = Gate(); c, _ = self.make(start_gate=gate)
        self.command(c, 'start', 1); self.assertTrue(gate.entered.wait(3))
        self.assertEqual(self.command(c, 'stop', 2, binding='other')['reason'], 'binding_mismatch')
        self.command(c, 'cancel', 3); gate.release.set(); self.assertTrue(c.wait_for_worker(3))
    def test_failures_release_worker(self):
        for phase in ('factory', 'start', 'stop', 'process'):
            with self.subTest(phase=phase):
                c, calls = self.make(failure=phase)
                self.command(c, 'start', 1); self.command(c, 'stop', 2)
                self.assertTrue(c.wait_for_worker(3)); self.assertEqual(c.snapshot()['state'], 'error')
                self.assertFalse(c.snapshot()['busy'])
                if phase == 'start': self.assertIn('stop', calls)
    def test_cancel_at_factory_and_drain_never_processes(self):
        for phase in ('factory', 'start', 'stop'):
            with self.subTest(phase=phase):
                gate = Gate(); c, calls = self.make(**{phase + '_gate':gate})
                self.command(c, 'start', 1)
                if phase == 'stop': self.command(c, 'stop', 2)
                self.assertTrue(gate.entered.wait(3))
                self.command(c, 'cancel', 3)
                gate.release.set(); self.assertTrue(c.wait_for_worker(3))
                self.assertEqual(c.snapshot()['state'], 'cancelled')
                self.assertNotIn('process',calls)
    def test_1000_seeded_lifecycle_interleavings(self):
        import random
        rng = random.Random(20261029)
        for trial in range(1000):
            phase = rng.choice(('factory','start','stop','process'))
            gate = Gate(); c, calls = self.make(**{phase+'_gate':gate})
            self.command(c,'start',1)
            if phase in ('stop','process'): self.command(c,'stop',2)
            self.assertTrue(gate.entered.wait(3))
            seq = 3
            cancelled = False
            for _ in range(rng.randrange(1,8)):
                action = rng.choice(('stop','cancel','duplicate','foreign','old_engine','new_start'))
                if action == 'duplicate':
                    response = self.command(c,'start',1)
                    self.assertTrue(response['accepted'])
                elif action == 'old_engine':
                    response = c.command('cancel',{'engine_id':'old'})
                    self.assertFalse(response['accepted'])
                elif action == 'foreign':
                    self.assertFalse(self.command(c,'stop',seq,binding='other')['accepted']); seq += 1
                elif action == 'new_start':
                    self.assertEqual(self.command(c,'start',seq,str(uuid4()))['reason'],'busy'); seq += 1
                else:
                    response=self.command(c,action,seq);seq+=1
                    self.assertTrue(response['accepted'])
                    cancelled |= action == 'cancel'
            if phase in ('factory','start') and not cancelled:
                self.command(c,'stop',seq);seq+=1
            gate.release.set(); self.assertTrue(c.wait_for_worker(3))
            self.assertEqual(c.snapshot()['state'],'cancelled' if cancelled else 'ready')
            self.assertLessEqual(calls.count('factory'),1)
            self.assertLessEqual(calls.count('start'),1)
            self.assertLessEqual(calls.count('stop'),1)
            self.assertEqual(c.snapshot()['result'] is None,cancelled)
            c.close()
    def test_concurrent_first_commands_have_one_owner(self):
        gate = Gate(); c,calls=self.make(factory_gate=gate)
        barrier=threading.Barrier(3); responses=[]
        def submit(seq):
            barrier.wait();responses.append(self.command(c,'start',seq,str(uuid4())))
        threads=[threading.Thread(target=submit,args=(n,)) for n in (1,2)]
        for t in threads:t.start()
        barrier.wait()
        for t in threads:t.join(3)
        self.assertEqual(sum(r['accepted'] for r in responses),1)
        accepted=next(r for r in responses if r['accepted'])
        self.command(c,'cancel',3,accepted['session_id'])
        gate.release.set();self.assertTrue(c.wait_for_worker(3))

    def test_duplicate_changed_payload_and_evicted_commands_are_rejected(self):
        gate=Gate();c,calls=self.make(start_gate=gate)
        self.command(c,'start',1);self.assertTrue(gate.entered.wait(3))
        self.assertEqual(self.command(c,'start',1,binding='other')['reason'],'sequence_conflict')
        for seq in range(2,200):
            self.command(c,'stop',seq,binding='other')
        self.assertLessEqual(len(c._recent),128)
        self.assertEqual(self.command(c,'start',1)['reason'],'stale_command')
        self.command(c,'cancel',200);gate.release.set();self.assertTrue(c.wait_for_worker(3))
    def test_cancelled_inference_error_is_not_published(self):
        gate=Gate();c,calls=self.make(process_gate=gate,failure='process')
        self.command(c,'start',1);self.command(c,'stop',2);self.assertTrue(gate.entered.wait(3))
        self.command(c,'cancel',3);gate.release.set();self.assertTrue(c.wait_for_worker(3))
        self.assertEqual(c.snapshot()['state'],'cancelled');self.assertIsNone(c.snapshot()['last_error'])

    def test_settings_snapshot_is_immutable_and_next_session_only(self):
        gate=Gate(); settings={'audio':{'sample_rate':16000,'channels':1,'blocksize':256}}
        observed=[]
        class Recorder:
            def start(self): gate()
            def stop(self): return []
        class Pipeline:
            def process_audio(self,*args,**kwargs):return None
        def factory(snapshot):
            observed.append(snapshot)
            with self.assertRaises(TypeError): snapshot['audio']['blocksize']=4096
            return Recorder(),Pipeline()
        c=module.SerializedSessionController(factory=factory,settings_provider=lambda:settings,engine_id='engine');self.addCleanup(c.close)
        self.command(c,'start',1);self.assertTrue(gate.entered.wait(3))
        settings['audio']['blocksize']=512;c.update_settings(settings)
        self.assertEqual(observed[0]['audio']['blocksize'],256)
        self.command(c,'cancel',2);gate.release.set();self.assertTrue(c.wait_for_worker(3))
        self.command(c,'start',3,str(uuid4()));self.command(c,'stop',4,c.snapshot()['session_id']);self.assertTrue(c.wait_for_worker(3))
        self.assertEqual(observed[1]['audio']['blocksize'],512)
    def test_invalid_settings_are_rejected_before_adapter_calls(self):
        for settings in ({'audio':{'channels':2}}, {'audio':{'sample_rate':48000}}, {'audio':{'blocksize':1.5}}, {'vad':{'threshold':float('nan')}}, {'models':{'whisper_cpu_threads':2.5}}, {'runtime':{'profile':[]}}, {'audio':[]}):
            with self.subTest(settings=settings),self.assertRaises(ValueError):
                module.validate_session_settings(settings)
    def test_explicit_legacy_injection_request_is_rejected_before_processing(self):
        from services.pipeline.orchestrator import PipelineOrchestrator
        from types import SimpleNamespace
        p=PipelineOrchestrator(vad=SimpleNamespace(filter=lambda _:None),transcriber=None,formatter=None,context_provider=lambda:None,injector=None)
        with self.assertRaisesRegex(RuntimeError,'disabled'):
            p.process_audio([],inject=True)
        with self.assertRaisesRegex(RuntimeError,'disabled'):
            p.process_command('text',[],inject=True)
        with self.assertRaisesRegex(RuntimeError,'disabled'):
            p.inject_result(None)

    def test_no_automatic_injection_contract(self):
        from services.pipeline.orchestrator import PipelineOrchestrator
        from services.pipeline.models import AppContext, AsrResult
        from types import SimpleNamespace
        injected = []
        p = PipelineOrchestrator(vad=SimpleNamespace(filter=lambda a:a), transcriber=SimpleNamespace(transcribe=lambda *a, **k:AsrResult(text='hello', language='en', duration_ms=1)), formatter=SimpleNamespace(format=lambda *a, **k:SimpleNamespace(text='Hello.')), context_provider=lambda:AppContext(app_name='test',window_title='',category='general'), injector=SimpleNamespace(inject=injected.append))
        self.assertEqual(p.process_audio([.2]).polished_text, 'Hello.')
        self.assertEqual(injected, [])

if __name__ == '__main__': unittest.main()
