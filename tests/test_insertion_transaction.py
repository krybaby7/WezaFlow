import unittest
from uuid import uuid4
from tests import test_serialized_controller as helpers

class CommitTests(unittest.TestCase):
    make = helpers.ControllerTests.make
    command = helpers.ControllerTests.command
    setUp = helpers.ControllerTests.setUp
    def prepared(self):
        c, calls = self.make()
        capture = str(uuid4())
        p = dict(engine_id='engine', protocol_version=1, session_id=self.sid, command_seq=1, binding_id='key', capture_id=capture, target_token='opaque')
        c.command('start', p)
        self.command(c, 'stop', 2)
        self.assertTrue(c.wait_for_worker(3))
        request = dict(session_generation=c.snapshot()['session_generation'], engine_id='engine', protocol_version=1, session_id=self.sid, result_session_id=self.sid, capture_id=capture, transaction_id=str(uuid4()))
        return c, request

    def test_reservation_and_completion_are_idempotent(self):
        c, p = self.prepared()
        self.assertTrue(c.reserve_commit(p)['accepted'])
        self.assertTrue(c.reserve_commit(p)['accepted'])
        self.assertEqual(c.snapshot()['state'], 'committing')
        done = dict(p, outcome='inserted_verified')
        self.assertTrue(c.complete_commit(done)['accepted'])
        self.assertTrue(c.complete_commit(done)['accepted'])
        self.assertFalse(c.snapshot()['busy'])
        self.assertFalse(c.complete_commit(dict(p, outcome='error'))['accepted'])
        self.assertFalse(c.reserve_commit(dict(p, transaction_id=str(uuid4())))['accepted'])

    def test_completed_uncertain_releases_writer_but_never_retries(self):
        c, p = self.prepared(); c.reserve_commit(p)
        self.assertEqual(self.command(c,'cancel',3)['reason'], 'too_late')
        self.assertEqual(self.command(c,'start',4,str(uuid4()))['reason'], 'busy')
        self.assertTrue(c.complete_commit(dict(p, outcome='delivery_uncertain'))['accepted'])
        self.assertFalse(c.snapshot()['busy'])
        self.assertTrue(c.complete_commit(dict(p, outcome='delivery_uncertain'))['accepted'])
        self.assertFalse(c.complete_commit(dict(p, outcome='inserted_verified'))['accepted'])
        self.assertFalse(c.reserve_commit(dict(p,transaction_id=str(uuid4())))['accepted'])
        self.assertTrue(self.command(c,'start',5,str(uuid4()))['accepted'])
        self.assertIsNotNone(c.snapshot()['result'])
        self.command(c,'cancel',6,sid=c.snapshot()['session_id'])
        self.assertTrue(c.wait_for_worker(3))

    def test_stale_identity_and_recoverable_preflight(self):
        c, p = self.prepared()
        for k,v in [('engine_id','wrong'),('capture_id',str(uuid4())),('session_id',str(uuid4())),('result_session_id',str(uuid4()))]:
            self.assertFalse(c.reserve_commit(dict(p, **{k:v}))['accepted'])
            self.assertEqual(c.snapshot()['state'],'ready')
        self.assertTrue(c.reserve_commit(p)['accepted'])
        self.assertFalse(c.complete_commit(dict(p, transaction_id=str(uuid4()), outcome='error'))['accepted'])
        self.assertTrue(c.complete_commit(dict(p, outcome='not_attempted_recoverable'))['accepted'])
        self.assertEqual(c.snapshot()['state'],'ready')
        self.assertIsNotNone(c.snapshot()['result'])
        self.assertFalse(c.reserve_commit(dict(p, transaction_id=str(uuid4())))['accepted'])

    def test_cancel_ready_prevents_reservation(self):
        c,p=self.prepared()
        self.assertTrue(self.command(c,'cancel',3)['accepted'])
        self.assertFalse(c.reserve_commit(p)['accepted'])

    def test_previous_result_not_committable_to_new_capture(self):
        c,p=self.prepared()
        self.command(c,'start',3,str(uuid4()))
        self.assertFalse(c.reserve_commit(p)['accepted'])
        self.assertIsNotNone(c.snapshot()['result'])
        self.command(c,'cancel',4,sid=c.snapshot()['session_id'])
        self.assertTrue(c.wait_for_worker(3))

    def test_bad_wire_never_raises(self):
        c,p=self.prepared()
        for bad in [None, [], {}, dict(p, protocol_version=True),dict(p, transaction_id='x')]:
            self.assertFalse(c.reserve_commit(bad)['accepted'])
    def test_cancel_vs_reserve_has_one_owner(self):
        import threading
        for _ in range(40):
            self.sid = str(uuid4())
            c,p=self.prepared()
            gate=threading.Barrier(3); results={}
            def reserve():
                gate.wait(); results['reserve']=c.reserve_commit(p)
            def cancel():
                gate.wait(); results['cancel']=self.command(c,'cancel',3)
            threads=[threading.Thread(target=reserve),threading.Thread(target=cancel)]
            for t in threads: t.start()
            gate.wait()
            for t in threads: t.join(3); self.assertFalse(t.is_alive())
            self.assertNotEqual(results['reserve']['accepted'],results['cancel']['accepted'])
            if results['reserve']['accepted']:
                self.assertEqual(results['cancel']['reason'],'too_late')
            else:
                self.assertEqual(c.snapshot()['state'],'cancelled')

    def test_ready_worker_still_owned_cannot_reserve(self):
        import threading
        c,_=self.make()
        ready=threading.Event(); release=threading.Event()
        class ReadyCondition(threading.Condition):
            def __exit__(condition, *args):
                hold=c._state == 'ready' and c._busy and threading.current_thread() is c._thread
                result=super().__exit__(*args)
                if hold:
                    ready.set()
                    if not release.wait(3): raise RuntimeError('test barrier timed out')
                return result
        c._condition=ReadyCondition()
        capture=str(uuid4())
        c.command('start',dict(engine_id='engine',protocol_version=1,session_id=self.sid,command_seq=1,binding_id='key',capture_id=capture))
        self.command(c,'stop',2)
        self.assertTrue(ready.wait(3))
        p=dict(session_generation=c.snapshot()['session_generation'],engine_id='engine',protocol_version=1,session_id=self.sid,result_session_id=self.sid,capture_id=capture,transaction_id=str(uuid4()))
        try:
            self.assertEqual(c.reserve_commit(p)['reason'],'not_ready')
        finally:
            release.set()
        self.assertTrue(c.wait_for_worker(3))
        self.assertTrue(c.reserve_commit(p)['accepted'])
        self.assertTrue(c.snapshot()['busy'])

    def test_settings_during_commit_do_not_change_snapshot(self):
        c,p=self.prepared(); c.reserve_commit(p)
        active=c.snapshot()['active_configuration']
        c.update_settings({'audio':{'sample_rate':16000,'channels':1},'models':{'whisper':'new'}})
        self.assertEqual(c.snapshot()['active_configuration'],active)
        self.assertEqual(c.settings_snapshot()['models']['whisper'],'new')

    def test_conflicting_reservation_does_not_mutate(self):
        c,p=self.prepared(); c.reserve_commit(p)
        before=c.snapshot()
        self.assertEqual(c.reserve_commit(dict(p,capture_id=str(uuid4())))['reason'],'transaction_conflict')
        self.assertEqual(c.snapshot(),before)

    def test_http_authentication_and_transaction_endpoints(self):
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        from services.runtime.api import create_app
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(root=Path(folder),engine_token='secret',engine_id='engine')
            c,p=self.prepared()
            # Route closures own their controller, so exercise rejects on this app.
            with TestClient(app) as client:
                for route in ('reserve','complete'):
                    path='/runtime/commit/'+route
                    self.assertEqual(client.post(path,json=p).status_code,401)
                    response=client.post(path,json=p,headers={'Authorization':'Bearer secret'})
                    self.assertEqual(response.status_code,200)
                    self.assertFalse(response.json()['accepted'])
                    self.assertEqual(response.json()['engine_id'],'engine')
                caps=client.get('/runtime/handshake',headers={'Authorization':'Bearer secret'}).json()['capabilities']
                self.assertIn('insertion-transactions-v1',caps)
                self.assertIn('insertion-disabled',caps)
    def test_new_generation_requires_fresh_capture(self):
        c,p=self.prepared(); c.reserve_commit(p)
        c.complete_commit(dict(p,outcome='not_attempted_recoverable'))
        new=dict(engine_id='engine',protocol_version=1,session_id=str(uuid4()),command_seq=3,binding_id='key',capture_id=p['capture_id'])
        self.assertEqual(c.command('start',new)['reason'],'capture_reused')
        self.assertEqual(c.snapshot()['session_id'],self.sid)

    def test_reused_session_uuid_does_not_resurrect_old_transaction(self):
        c,p=self.prepared(); c.reserve_commit(p)
        c.complete_commit(dict(p,outcome='inserted_verified'))
        sid_b=str(uuid4())
        self.assertTrue(self.command(c,'start',3,sid_b)['accepted'])
        self.command(c,'cancel',4,sid_b); self.assertTrue(c.wait_for_worker(3))
        new=dict(engine_id='engine',protocol_version=1,session_id=self.sid,command_seq=5,binding_id='key',capture_id=str(uuid4()))
        self.assertTrue(c.command('start',new)['accepted'])
        before=c.snapshot()
        self.assertFalse(c.reserve_commit(p)['accepted'])
        self.assertEqual(c.snapshot(),before)
        self.assertFalse(c.complete_commit(dict(p,outcome='inserted_verified'))['accepted'])
        self.command(c,'cancel',6); self.assertTrue(c.wait_for_worker(3))

    def test_only_first_concurrent_reservation_authorizes_write(self):
        import threading
        c,p=self.prepared(); gate=threading.Barrier(3); results=[]
        def reserve():
            gate.wait(); results.append(c.reserve_commit(p))
        workers=[threading.Thread(target=reserve) for _ in range(2)]
        for t in workers: t.start()
        gate.wait()
        for t in workers: t.join(3); self.assertFalse(t.is_alive())
        self.assertEqual(sum(r.get('write_authorized',False) for r in results),1)
        self.assertEqual({r.get('reservation_decision') for r in results},{'reserved','replayed_inflight'})

    def test_all_completed_outcomes_replay_without_write_authorization(self):
        for outcome in ('inserted_verified','inserted_unverified','not_attempted_recoverable','delivery_uncertain','cancelled','error'):
            with self.subTest(outcome=outcome):
                c,p=self.prepared()
                first=c.reserve_commit(p)
                self.assertTrue(first.get('write_authorized'))
                self.assertTrue(c.complete_commit(dict(p,outcome=outcome))['accepted'])
                replay=c.reserve_commit(p)
                self.assertTrue(replay['accepted'])
                self.assertFalse(replay['write_authorized'])
                self.assertEqual(replay['reservation_decision'],'replayed_completed')
                self.assertEqual(replay['insertion_transaction']['outcome'],outcome)
    def test_http_success_serializes_generation_and_replay_decision(self):
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        from services.runtime.api import create_app
        fake,_=self.make()
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(root=Path(folder),engine_token='secret',engine_id='engine',controller_factory=fake.factory)
            self.addCleanup(app.state.controller.close)
            with TestClient(app,headers={'Authorization':'Bearer secret'}) as client:
                capture=str(uuid4())
                base=dict(engine_id='engine',protocol_version=1,session_id=self.sid,binding_id='key')
                started=client.post('/runtime/start',json=dict(base,command_seq=1,capture_id=capture)).json()
                self.assertTrue(started['accepted'])
                client.post('/runtime/stop',json=dict(base,command_seq=2))
                self.assertTrue(app.state.controller.wait_for_worker(3))
                p=dict(engine_id='engine',protocol_version=1,session_generation=started['session_generation'],session_id=self.sid,result_session_id=self.sid,capture_id=capture,transaction_id=str(uuid4()))
                reserved=client.post('/runtime/commit/reserve',json=p).json()
                self.assertEqual(reserved['reservation_decision'],'reserved')
                self.assertTrue(reserved['write_authorized'])
                self.assertEqual(reserved['insertion_transaction']['session_generation'],started['session_generation'])
                done=client.post('/runtime/commit/complete',json=dict(p,outcome='inserted_unverified')).json()
                self.assertTrue(done['accepted'])
                self.assertEqual(done['insertion_transaction']['outcome'],'inserted_unverified')
                self.assertFalse(done['busy'])
                replay=client.post('/runtime/commit/reserve',json=p).json()
                self.assertEqual(replay['reservation_decision'],'replayed_completed')
                self.assertFalse(replay['write_authorized'])
                self.assertFalse(replay['automatic_insertion'])

    def test_history_is_bounded_to_current_generation(self):
        c,p=self.prepared()
        for index in range(30):
            self.assertTrue(c.reserve_commit(p)['accepted'])
            self.assertTrue(c.complete_commit(dict(p,outcome='inserted_verified'))['accepted'])
            self.assertEqual(len(c._transactions),1)
            sid=str(uuid4()); capture=str(uuid4())
            started=c.command('start',dict(engine_id='engine',protocol_version=1,session_id=sid,command_seq=3+index*2,binding_id='key',capture_id=capture))
            self.assertTrue(started['accepted'])
            self.assertEqual(len(c._transactions),0)
            self.assertFalse(c.reserve_commit(p)['accepted'])
            self.command(c,'stop',4+index*2,sid)
            self.assertTrue(c.wait_for_worker(3))
            p=dict(p,session_id=sid,result_session_id=sid,capture_id=capture,session_generation=started['session_generation'],transaction_id=str(uuid4()))
