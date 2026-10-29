import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from services.runtime.api import create_app

class AuthenticatedApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = create_app(root=Path(self.temp.name), engine_token='test-secret', engine_id='test-engine')
        self.client = TestClient(self.app)
    def test_all_routes_require_auth_except_minimal_health(self):
        self.assertEqual(self.client.get('/health').json(), {'status':'ok'})
        for path in ('/status','/settings','/runtime/handshake','/runtime/check','/snippets'):
            self.assertEqual(self.client.get(path).status_code,401,path)
    def test_handshake_and_old_instance_rejection(self):
        headers={'Authorization':'Bearer test-secret'}
        result=self.client.get('/runtime/handshake',headers=headers).json()
        self.assertEqual((result['application_id'],result['protocol_version'],result['engine_id']),('wezza',1,'test-engine'))
        result=self.client.post('/runtime/start',headers=headers,json={'engine_id':'old','protocol_version':1}).json()
        self.assertFalse(result['accepted']); self.assertEqual(result['reason'],'engine_mismatch')
    def test_shutdown_acknowledges_and_closes_controller(self):
        headers={'Authorization':'Bearer test-secret'}
        self.assertEqual(self.client.post('/runtime/shutdown',headers=headers).status_code,200)
        self.assertTrue(self.app.state.shutdown_requested.is_set())

    def test_explicit_profile_application_replaces_materialized_model_defaults(self):
        headers={'Authorization':'Bearer test-secret'}
        settings=self.client.get('/settings',headers=headers).json()['settings']
        settings['runtime']['profile']='quality'
        result=self.client.put('/settings',headers=headers,json={'settings':settings,'apply_profile_defaults':True})
        self.assertEqual(result.status_code,200)
        effective=result.json()['settings']
        self.assertEqual(effective['models']['whisper'],'distil-large-v3')
        self.assertEqual(effective['models']['whisper_cpu_threads'],6)
        self.assertEqual(effective['models']['whisper_compute_type'],'int8')
        self.assertEqual(self.client.get('/settings',headers=headers).json()['settings'],effective)

    def test_saves_without_profile_intent_preserve_custom_model_overrides(self):
        headers={'Authorization':'Bearer test-secret'}
        settings=self.client.get('/settings',headers=headers).json()['settings']
        settings['runtime']['profile']='quality'
        settings['models'].update(whisper='local-custom-model',whisper_cpu_threads=3,whisper_compute_type='float32')
        for flag in (None,False):
            payload={'settings':settings}
            if flag is not None:payload['apply_profile_defaults']=flag
            effective=self.client.put('/settings',headers=headers,json=payload).json()['settings']
            self.assertEqual(effective['models'],settings['models'])
        settings['hotkeys']['dictation']=['Ctrl+Shift+Space']
        effective=self.client.put('/settings',headers=headers,json={'settings':settings}).json()['settings']
        self.assertEqual(effective['models'],settings['models'])
