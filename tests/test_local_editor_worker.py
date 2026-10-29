import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
import os
import sys
import threading
import time
import subprocess
from dataclasses import replace
from pathlib import Path


def fixture_child(connection, gate, config, generation):
    from services.llm.local_worker_process import child_main
    import subprocess
    native_popen = subprocess.Popen
    def launch(args, **kwargs):
        return native_popen([sys.executable, str(Path(__file__).parent / 'fixtures' / 'local_editor_native.py'), *args[1:]], **kwargs)
    with patch('services.llm.local_worker_process.subprocess.Popen', side_effect=launch):
        child_main(connection, gate, config, generation)


def protocol_child(connection, gate, config, generation):
    gate.wait()
    connection.send_bytes(json.dumps({'kind':'ready','generation':generation,'provenance':fixture_provenance(config)}).encode())
    while True:
        request = json.loads(connection.recv_bytes())
        mode = request['user']
        if mode == 'malformed':
            connection.send_bytes(b'not json')
        elif mode == 'partial':
            connection.send_bytes(b'{"generation":')
        else:
            connection.send_bytes(json.dumps({'kind':'result','generation':generation+1,'id':request['id'],'content':'{}'}).encode())


def unread_child(connection, gate, config, generation):
    gate.wait()
    connection.send_bytes(json.dumps({'kind':'ready','generation':generation,'provenance':fixture_provenance(config)}).encode())
    time.sleep(30)


def fixture_provenance(config):
    return dict(manifest_sha256=config.manifest_sha256,model_sha256='0'*64,template_sha256='0'*64,runtime_sha256=['0'*64],source='fixture',revision='v1',load_seconds=0,warmup_seconds=0)


def gated_wait_child(connection,gate,config,generation):
    from services.llm.local_worker_process import child_main
    connection.send_bytes(b'gated')
    child_main(connection,gate,config,generation)


def manifest(root):
    records = []
    for name in ('model.gguf', 'template.jinja', 'server.exe'):
        data = b'hard enable_thinking false' if 'template' in name else b'fixture'
        (root / name).write_bytes(data)
        records.append(dict(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    path = root / 'manifest.json'
    path.write_text(json.dumps(dict(schema_version=1, source='fixture', revision='v1', model=records[0], template=records[1], server=records[2], runtime_files=[])))
    return path


class ArtifactTests(unittest.TestCase):
    def test_verified_and_tamper(self):
        from services.llm.local_artifacts import verify_manifest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = manifest(root)
            self.assertEqual(verify_manifest(path)['model']['sha256'], hashlib.sha256(b'fixture').hexdigest())
            (root / 'model.gguf').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                verify_manifest(path)

    def test_invalid_paths(self):
        from services.llm.local_artifacts import verify_manifest
        for mutation in ('escape', 'duplicate', 'json_duplicate'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                path = manifest(Path(directory))
                value = json.loads(path.read_text())
                if mutation == 'escape':
                    value['model']['path'] = '../model.gguf'
                elif mutation == 'duplicate':
                    value['template'] = value['model']
                if mutation == 'json_duplicate':
                    path.write_text('{"schema_version":1,"schema_version":1}')
                else:
                    path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    verify_manifest(path)

    def test_symlink_or_junction_escape(self):
        from services.llm.local_artifacts import verify_manifest
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            path = manifest(root)
            (Path(outside)/'model.gguf').write_bytes(b'fixture')
            link = root/'escape'
            try:
                if os.name == 'nt':
                    import _winapi
                    _winapi.CreateJunction(outside,str(link))
                else:
                    link.symlink_to(outside,target_is_directory=True)
                value = json.loads(path.read_text())
                value['model']['path'] = 'escape/model.gguf'
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    verify_manifest(path)
            finally:
                if link.exists():
                    if os.name == 'nt':
                        os.rmdir(link)
                    else:
                        link.unlink()

    def test_config(self):
        from services.llm.local_worker import LocalEditorConfig
        with self.assertRaises(ValueError):
            LocalEditorConfig('relative.json')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                LocalEditorConfig(str(manifest(Path(directory))), request_timeout=float('nan'))

    def test_provenance_is_bounded_and_closed_timeout_validated(self):
        from services.llm.local_worker import LocalEditorService, LocalEditorConfig
        with tempfile.TemporaryDirectory() as directory:
            config = LocalEditorConfig(str(manifest(Path(directory))))
            value = fixture_provenance(config)
            self.assertTrue(LocalEditorService._valid_provenance(value,config))
            value['transcript'] = 'untrusted secret'
            self.assertFalse(LocalEditorService._valid_provenance(value,config))
            service = LocalEditorService()
            with self.assertRaises(ValueError):
                service.close(timeout_seconds=float('nan'))
            self.assertTrue(service.close(timeout_seconds=0))

    def test_deadline_caps(self):
        from services.llm.local_worker import LocalEditorConfig, LocalEditorService
        with tempfile.TemporaryDirectory() as directory:
            path = str(manifest(Path(directory)))
            for key,value in [('startup_timeout',601),('request_timeout',121),('idle_timeout',3601),('reap_timeout',31)]:
                with self.subTest(key=key),self.assertRaises(ValueError):
                    LocalEditorConfig(path,**{key:value})
            with self.assertRaises(ValueError):
                LocalEditorService().close(timeout_seconds=31)


class HttpTests(unittest.TestCase):
    def runtime(self, directory):
        from services.llm.local_worker import LocalEditorConfig
        from services.llm.local_worker_process import NativeRuntime
        return NativeRuntime(LocalEditorConfig(str(manifest(Path(directory)))))

    def replies(self, mutation=None):
        reply = {'model':'alias','choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"edited_text":"Hi."}'}}]}
        if mutation:
            mutation(reply)
        return [{'prompt':'request< think > <think>\n\n</think>\n\n'}, {'tokens':[1]*10}, reply]

    def test_exact_messages_tokenized_and_greedy(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(directory)
            runtime.alias = 'alias'
            with patch.object(runtime, 'http', side_effect=self.replies()) as http:
                self.assertEqual(runtime.edit('policy','payload'), '{"edited_text":"Hi."}')
            self.assertEqual(http.call_args_list[0].args[1]['messages'], http.call_args_list[2].args[1]['messages'])
            self.assertEqual(http.call_args_list[1].args[1]['content'], 'request< think > <think>\n\n</think>\n\n')
            self.assertEqual(http.call_args_list[2].args[1]['temperature'], 0)

    def test_rejects_alias_truncation_reasoning_tool_multiple_and_role(self):
        mutations = [lambda r:r.update(model='wrong'), lambda r:r['choices'][0].update(finish_reason='length'), lambda r:r['choices'][0]['message'].update(reasoning_content='secret'), lambda r:r['choices'][0]['message'].update(tool_calls=[{}]), lambda r:r['choices'].append(r['choices'][0]), lambda r:r['choices'][0]['message'].update(role='user')]
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                runtime = self.runtime(directory)
                runtime.alias = 'alias'
                with patch.object(runtime, 'http', side_effect=self.replies(mutation)), self.assertRaises(ValueError):
                    runtime.edit('policy','payload')

    def test_context_overflow_prevents_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(directory)
            with patch.object(runtime, 'http', side_effect=[{'prompt':'<think>\n\n</think>'}, {'tokens':[1]*4000}]) as http, self.assertRaises(ValueError):
                runtime.edit('policy','payload')
            self.assertEqual(http.call_count, 2)

    def test_nonempty_thinking_prefix_rejected_before_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(directory)
            with patch.object(runtime,'http',return_value={'prompt':'<think>secret thoughts'}) as http,self.assertRaises(ValueError):
                runtime.edit('policy','payload')
            self.assertEqual(http.call_count,1)

    def test_start_flags_environment_proxy_policy_and_pretty_json_warmup(self):
        import urllib.request
        from services.llm.local_worker_process import NativeRuntime
        from services.llm.local_worker import LocalEditorConfig
        with tempfile.TemporaryDirectory() as directory:
            config = LocalEditorConfig(str(manifest(Path(directory))))
            seen_proxies = []
            class Proxy(urllib.request.ProxyHandler):
                def __init__(self,proxies=None):
                    seen_proxies.append(proxies)
                    super().__init__(proxies)
            with patch('services.llm.local_worker_process.urllib.request.ProxyHandler',Proxy):
                runtime = NativeRuntime(config)
                self.assertEqual(seen_proxies,[{}])
            replies = [{'data':[{'id':runtime.alias}]},{'prompt':'<think>\n\n</think>\n\n'},{'tokens':[1]}, {'model':runtime.alias,'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{\n  "edited_text": "Ready."\n}'}}]}]
            with patch.dict(os.environ,{'LLAMA_INHERITED':'bad','LLAMA_API_KEY':'old'}),patch.object(runtime,'http',side_effect=replies),patch('services.llm.local_worker_process.subprocess.Popen') as popen:
                popen.return_value.poll.return_value = None
                runtime.start()
            args = popen.call_args.args[0]
            environment = popen.call_args.kwargs['env']
            self.assertNotIn('--api-key',args)
            self.assertNotIn(runtime.key,args)
            self.assertEqual(environment['LLAMA_API_KEY'],runtime.key)
            self.assertNotIn('LLAMA_INHERITED',environment)
            for flag,value in [('--host','127.0.0.1'),('--parallel','1'),('--threads','2'),('--threads-batch','2'),('--ctx-size','4096'),('--poll','0'),('--poll-batch','0'),('--reasoning','off'),('--n-predict','512')]:
                self.assertEqual(args[args.index(flag)+1],value)
            self.assertIn('--no-webui',args)
            self.assertIn('--no-context-shift',args)
            self.assertEqual(popen.call_args.kwargs['creationflags'],subprocess.CREATE_NO_WINDOW)

    def test_literal_think_text_is_returned_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(directory)
            runtime.alias = 'alias'
            replies = self.replies(lambda r:r['choices'][0]['message'].update(content='{"edited_text":"literal <think>"}'))
            with patch.object(runtime,'http',side_effect=replies):
                self.assertEqual(runtime.edit('policy','literal <think>'),'{"edited_text":"literal <think>"}')

    def test_bounded_http_response_and_redirect(self):
        from services.llm.local_worker_process import MAX_RESPONSE_BYTES, _NoRedirect
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.runtime(directory)
            runtime.url = 'http://127.0.0.1:1234'
            class Response:
                def __enter__(self): return self
                def __exit__(self,*args): pass
                def read(self,count):
                    self.count = count
                    return b'x'*count
            response = Response()
            with patch.object(runtime.opener,'open',return_value=response), self.assertRaises(ValueError):
                runtime.http('/v1/models')
            self.assertEqual(response.count,MAX_RESPONSE_BYTES+1)
            with self.assertRaises(ValueError):
                _NoRedirect().redirect_request(None,None,None,None,None,None)


@unittest.skipUnless(os.name == 'nt', 'Windows job containment required')
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        from services.llm.local_worker import LocalEditorConfig, LocalEditorService
        self.directory = tempfile.TemporaryDirectory()
        self.config = LocalEditorConfig(str(manifest(Path(self.directory.name))), startup_timeout=10, request_timeout=.4, reap_timeout=3, idle_timeout=.4)
        self.service = LocalEditorService()
        self.patcher = patch('services.llm.local_worker.child_main', fixture_child)
        self.patcher.start()

    def tearDown(self):
        self.assertTrue(self.service.close())
        self.patcher.stop()
        self.directory.cleanup()

    def test_real_warmup_reuse_and_idle_reap(self):
        backend = self.service.backend(self.config)
        self.assertEqual(backend.edit(system_prompt='policy',user_payload='hello'), '{"edited_text":"Ready."}')
        first = self.service.snapshot()
        self.assertGreaterEqual(first['provenance']['warmup_seconds'], 0)
        backend.edit(system_prompt='policy',user_payload='again')
        self.assertEqual(first['pid'],self.service.snapshot()['pid'])
        deadline = time.monotonic()+5
        while self.service.snapshot()['owns_process'] and time.monotonic()<deadline:
            time.sleep(.03)
        self.assertFalse(self.service.snapshot()['owns_process'])

    def test_startup_timeout_before_child_unpickles_releases_ownership(self):
        backend = self.service.backend(replace(self.config,startup_timeout=.001))
        with self.assertRaisesRegex(RuntimeError,'local_operation_timeout'):
            backend.edit(system_prompt='policy',user_payload='hello')
        self.assertFalse(self.service.snapshot()['owns_process'])
        self.assertFalse(self.service.snapshot()['owns_io'])

    def test_stalled_http_total_deadline_and_busy(self):
        backend = self.service.backend(self.config)
        backend.edit(system_prompt='policy',user_payload='hello')
        errors = []
        def stalled():
            try:
                backend.edit(system_prompt='policy',user_payload='stall')
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=stalled)
        start = time.monotonic()
        thread.start()
        time.sleep(.1)
        with self.assertRaisesRegex(RuntimeError,'busy'):
            backend.edit(system_prompt='policy',user_payload='hello')
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic()-start, 4)
        self.assertTrue(errors)
        self.assertFalse(self.service.snapshot()['owns_process'])

    def test_close_during_busy_reaps(self):
        backend = self.service.backend(self.config)
        backend.edit(system_prompt='policy',user_payload='hello')
        def stalled():
            try:
                backend.edit(system_prompt='policy',user_payload='stall')
            except RuntimeError:
                pass
        thread = threading.Thread(target=stalled)
        thread.start()
        time.sleep(.1)
        self.assertTrue(self.service.close())
        thread.join(4)
        self.assertFalse(thread.is_alive())

    def test_config_change_reaps_old_tree_before_replacement(self):
        self.service.backend(self.config).edit(system_prompt='policy',user_payload='hello')
        old = self.service.snapshot()
        self.service.backend(replace(self.config,idle_timeout=10)).edit(system_prompt='policy',user_payload='hello')
        new = self.service.snapshot()
        self.assertGreater(new['generation'],old['generation'])
        self.assertNotEqual(new['pid'],old['pid'])

    def test_changed_manifest_on_reuse_reaps_old_tree(self):
        backend = self.service.backend(self.config)
        backend.edit(system_prompt='policy',user_payload='hello')
        with open(self.config.manifest_path,'a') as stream:
            stream.write(' ')
        with self.assertRaises(RuntimeError):
            backend.edit(system_prompt='policy',user_payload='again')
        self.assertFalse(self.service.snapshot()['owns_process'])

    def test_unexpected_wrapper_exit_is_classified_in_snapshot(self):
        self.service.backend(self.config).edit(system_prompt='policy',user_payload='hello')
        self.service._process.terminate()
        self.service._process.join(2)
        self.assertEqual(self.service.snapshot()['state'],'UNHEALTHY')

    def test_startup_hash_failure_and_invalid_input_preserves_ready(self):
        backend = self.service.backend(self.config)
        backend.edit(system_prompt='policy',user_payload='hello')
        pid = self.service.snapshot()['pid']
        with self.assertRaises(ValueError):
            backend.edit(system_prompt='policy',user_payload='x'*120001)
        self.assertEqual(self.service.snapshot()['pid'],pid)
        self.assertTrue(self.service.close())
        from services.llm.local_worker import LocalEditorService
        self.service = LocalEditorService()
        (Path(self.directory.name)/'model.gguf').write_bytes(b'tampered')
        with self.assertRaises(RuntimeError):
            self.service.backend(self.config).edit(system_prompt='policy',user_payload='hello')
        self.assertFalse(self.service.snapshot()['owns_process'])

    def test_failed_reap_retains_ownership_and_blocks_replacement(self):
        self.service.backend(self.config).edit(system_prompt='policy',user_payload='hello')
        original = self.service.snapshot()
        with patch.object(self.service._job,'wait_empty',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'reap_incomplete'):
                self.service.backend(replace(self.config,idle_timeout=10)).edit(system_prompt='policy',user_payload='hello')
            retained = self.service.snapshot()
            self.assertTrue(retained['owns_process'])
            self.assertEqual(original['generation'],retained['generation'])
        self.assertTrue(self.service.close())

    def test_stale_malformed_partial_responses_poison_tree(self):
        for mode in ('stale','malformed','partial'):
            with self.subTest(mode=mode), patch('services.llm.local_worker.child_main',protocol_child):
                with self.assertRaises(RuntimeError):
                    self.service.backend(self.config).edit(system_prompt='policy',user_payload=mode)
                self.assertFalse(self.service.snapshot()['owns_process'])

    def test_deadline_includes_blocked_ipc_send(self):
        with patch('services.llm.local_worker.child_main',unread_child):
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                self.service.backend(self.config).edit(system_prompt='policy',user_payload='x'*110000)
            self.assertLess(time.monotonic()-started,4)
            self.assertFalse(self.service.snapshot()['owns_process'])

    def test_gate_is_closed_until_job_assignment(self):
        from services.asr.windows_job import WorkerJob
        original_assign = WorkerJob.assign
        marker = Path(self.directory.name)/'gated.pid'
        def assign(job,process):
            time.sleep(.2)
            self.assertFalse(marker.exists())
            return original_assign(job,process)
        with patch.dict(os.environ,{'WEZZA_TEST_PID_FILE':str(marker)}), patch.object(WorkerJob,'assign',assign):
            self.service.backend(self.config).edit(system_prompt='policy',user_payload='hello')
        self.assertTrue(marker.exists())

    def test_assignment_failure_reaps_gated_unassigned_wrapper(self):
        from services.asr.windows_job import WorkerJob
        try:
            with patch.object(WorkerJob,'assign',side_effect=RuntimeError('fixture')):
                with self.assertRaises(RuntimeError):
                    self.service.backend(self.config).edit(system_prompt='policy',user_payload='hello')
            self.assertFalse(self.service.snapshot()['owns_process'])
        finally:
            if self.service._process is not None:
                self.service._process.terminate()

    def test_cancelled_old_idle_callback_cannot_reap_new_activity(self):
        backend = self.service.backend(self.config)
        backend.edit(system_prompt='policy',user_payload='hello')
        generation = self.service._generation
        activity = self.service._activity
        backend.edit(system_prompt='policy',user_payload='again')
        self.service._idle(generation,activity)
        self.assertTrue(self.service.snapshot()['owns_process'])

    def test_close_budget_includes_concurrent_idle_reap(self):
        self.service.backend(self.config).edit(system_prompt='policy',user_payload='hello')
        entered = threading.Event()
        release = threading.Event()
        original_wait = self.service._job.wait_empty
        def delayed_wait(timeout):
            entered.set()
            release.wait(.6)
            return original_wait(timeout)
        with patch.object(self.service._job,'wait_empty',side_effect=delayed_wait):
            idle = threading.Thread(target=self.service._idle,args=(self.service._generation,self.service._activity))
            idle.start()
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            try:
                self.assertFalse(self.service.close(timeout_seconds=.01))
                self.assertLess(time.monotonic()-start,.15)
            finally:
                release.set()
                idle.join(3)
        self.assertTrue(self.service.close(timeout_seconds=1))

    def test_close_budget_includes_state_lock_contention(self):
        entered = threading.Event()
        release = threading.Event()
        def held_lock():
            with self.service._lock:
                entered.set()
                release.wait(.6)
        thread = threading.Thread(target=held_lock)
        thread.start()
        self.assertTrue(entered.wait(1))
        start = time.monotonic()
        try:
            self.assertFalse(self.service.close(timeout_seconds=.01))
            self.assertLess(time.monotonic()-start,.15)
        finally:
            release.set()
            thread.join(2)
        self.assertTrue(self.service.close(timeout_seconds=1))

    def test_normal_timeout_and_close_signal_wrapper_and_native_handles(self):
        import ctypes
        from ctypes import wintypes
        from services.llm.local_worker import LocalEditorService
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for mode in ('timeout','close'):
            with self.subTest(mode=mode):
                self.service = LocalEditorService()
                marker = Path(self.directory.name)/(mode+'.pid')
                with patch.dict(os.environ,{'WEZZA_TEST_PID_FILE':str(marker)}):
                    backend = self.service.backend(self.config)
                    backend.edit(system_prompt='policy',user_payload='hello')
                handles = []
                try:
                    for pid in (self.service.snapshot()['pid'],int(marker.read_text())):
                        handle = kernel.OpenProcess(0x100000,False,pid)
                        self.assertTrue(handle)
                        handles.append(handle)
                    if mode == 'timeout':
                        with self.assertRaises(RuntimeError):
                            backend.edit(system_prompt='policy',user_payload='stall')
                    else:
                        self.assertTrue(self.service.close(timeout_seconds=3))
                    for handle in handles:
                        self.assertEqual(kernel.WaitForSingleObject(handle,1000),0)
                finally:
                    self.service.close(timeout_seconds=3)
                    for handle in handles:
                        kernel.CloseHandle(handle)

    def test_constrained_editor_raw_fallback_on_native_timeout(self):
        from services.llm.constrained_editor import ConstrainedEditor, EditRequest
        backend = self.service.backend(self.config)
        backend.edit(system_prompt='policy',user_payload='hello')
        # Force the native HTTP stall while keeping the real constrained adapter.
        class StallBackend:
            def edit(self,**kwargs):
                return backend.edit(system_prompt=kwargs['system_prompt'],user_payload='stall')
        result = ConstrainedEditor(StallBackend()).edit(EditRequest(raw='preserve this raw'))
        self.assertEqual(result.outcome,'raw_fallback')
        self.assertEqual(result.text,'preserve this raw')

    def test_abrupt_owner_death_kills_wrapper_and_native_descendant(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        marker = Path(self.directory.name)/'native.pid'
        environment = dict(os.environ,WEZZA_TEST_PID_FILE=str(marker))
        owner = subprocess.Popen([sys.executable,str(Path(__file__).parent/'fixtures'/'local_editor_owner.py'),self.config.manifest_path],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment,creationflags=subprocess.CREATE_NO_WINDOW)
        handles = []
        try:
            # Reader is bounded: communicate below always kills the owner on failure.
            result = []
            reader = threading.Thread(target=lambda:result.append(owner.stdout.readline()),daemon=True)
            reader.start()
            reader.join(12)
            self.assertFalse(reader.is_alive())
            snapshot = json.loads(result[0])
            for pid in (snapshot['pid'],int(marker.read_text())):
                handle = kernel.OpenProcess(0x100000,False,pid)
                self.assertTrue(handle)
                handles.append(handle)
            owner.kill()
            owner.wait(5)
            for handle in handles:
                self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.communicate(timeout=5)
            for handle in handles:
                kernel.CloseHandle(handle)

    def test_preassignment_owner_death_exits_gated_wrapper_within_startup_budget(self):
        self._check_preassignment_owner()

    def test_preassignment_readiness_failure_still_reaps_known_child(self):
        cleanup = []
        with self.assertRaisesRegex(AssertionError, 'fixture_readiness_failed'):
            self._check_preassignment_owner(fail_readiness=True, cleanup=cleanup)
        self.assertEqual(cleanup, [0])

    def _check_preassignment_owner(self, *, fail_readiness=False, cleanup=None):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE,wintypes.UINT]
        args = [sys.executable,str(Path(__file__).parent/'fixtures'/'local_editor_owner.py'),self.config.manifest_path,'--unassigned']
        if fail_readiness:
            args.append('--fail-readiness')
        owner = subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        handle = None
        reader = None
        # PID and readiness share the original total five-second budget.
        deadline = time.monotonic()+5
        def line():
            nonlocal reader
            result = []
            reader = threading.Thread(target=lambda:result.append(owner.stdout.readline()),daemon=True)
            reader.start()
            reader.join(max(0,deadline-time.monotonic()))
            self.assertFalse(reader.is_alive(), 'fixture readiness deadline expired')
            self.assertTrue(result[0], 'fixture exited before readiness')
            return json.loads(result[0])
        try:
            pid = line()['pid']
            # Retain termination rights on this exact process even if readiness fails.
            handle = kernel.OpenProcess(0x100001,False,pid)
            self.assertTrue(handle)
            self.assertEqual(line(), {'kind':'gated'})
            owner.kill()
            owner.wait(3)
            self.assertEqual(kernel.WaitForSingleObject(handle,2000),0)
        finally:
            if owner.poll() is None:
                owner.kill()
            if handle:
                try:
                    if kernel.WaitForSingleObject(handle,0) != 0:
                        self.assertTrue(kernel.TerminateProcess(handle,1))
                    status = kernel.WaitForSingleObject(handle,2000)
                    if cleanup is not None:
                        cleanup.append(status)
                    self.assertEqual(status,0, 'unassigned fixture child survived cleanup')
                finally:
                    kernel.CloseHandle(handle)
            owner.communicate(timeout=3)
            if reader is not None:
                reader.join(1)
                self.assertFalse(reader.is_alive(), 'fixture stdout reader survived cleanup')
