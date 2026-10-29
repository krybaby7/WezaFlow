"""HTTP-boundary acceptance checks using no microphone, model, or OS input."""
from __future__ import annotations

import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from services.config.settings import load_default_settings, save_settings
from services.runtime.api import create_app


class Recorder:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1
        return [0.2] * 160


class Pipeline:
    def __init__(self, text, *, blocked=False):
        self.text = text
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def process_audio(self, audio, *, language=None):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Test failed to release inference barrier")
        return {"raw_transcript": self.text, "polished_text": self.text}


class RuntimeProtocolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = load_default_settings()
        self.settings["audio"]["blocksize"] = 512
        save_settings(self.root / "config" / "settings.yaml", self.settings)
        self.engine_id = str(uuid4())
        self.token = "test-run-credential-" + str(uuid4())
        self.headers = {"Authorization": "Bearer " + self.token}
        self.seq = 0

    def make_client(self, factory):
        self.app = create_app(
            root=self.root,
            engine_token=self.token,
            engine_id=self.engine_id,
            controller_factory=factory,
        )
        client = TestClient(self.app)
        self.addCleanup(client.close)
        self.addCleanup(self.app.state.controller.close)
        return client

    def command(self, client, action, sid, *, binding="keyboard:dictation", **overrides):
        self.seq += 1
        payload = {
            "engine_id": self.engine_id,
            "protocol_version": 1,
            "session_id": sid,
            "command_seq": self.seq,
            "binding_id": binding,
            "mode": "dictation",
            "language": None,
            **overrides,
        }
        response = client.post("/runtime/" + action, json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["application_id"], "wezza")
        self.assertEqual(data["engine_id"], self.engine_id)
        self.assertEqual(data["protocol_version"], 1)
        return data

    def test_authentication_precedes_state_or_resource_access(self):
        calls = []
        client = self.make_client(lambda settings: calls.append(settings))
        for path in ("/status", "/settings", "/runtime/handshake", "/runtime/check"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(client.get(path, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        for path in ("/runtime/start", "/runtime/cancel", "/runtime/shutdown"):
            self.assertEqual(client.post(path, json={}).status_code, 401)
        health = client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertNotIn("engine_id", health.json())
        handshake = client.get("/runtime/handshake", headers=self.headers)
        self.assertEqual(handshake.status_code, 200)
        self.assertEqual(handshake.json()["engine_id"], self.engine_id)
        self.assertNotIn(self.token, handshake.text)
        self.assertFalse(calls)
        self.assertFalse(self.app.state.shutdown_requested.is_set())

    def test_release_then_cancel_during_acquisition_leaves_no_capture(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        recorder = Recorder()

        def factory(settings):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test failed to release acquisition barrier")
            return recorder, Pipeline("must never appear")

        client = self.make_client(factory)
        sid = str(uuid4())
        self.assertTrue(self.command(client, "start", sid)["accepted"])
        self.assertTrue(entered.wait(5))
        wrong = self.command(client, "stop", sid, binding="mouse:secondary")
        self.assertFalse(wrong["accepted"])
        self.assertEqual(wrong["reason"], "binding_mismatch")
        self.assertTrue(self.command(client, "stop", sid)["accepted"])
        cancelled = self.command(client, "cancel", sid, binding="escape")
        self.assertTrue(cancelled["accepted"])
        self.assertEqual(cancelled["state"], "cancelled")
        release.set()
        self.assertTrue(self.app.state.controller.wait_for_worker(5))
        status = client.get("/status", headers=self.headers).json()
        self.assertEqual(status["state"], "cancelled")
        self.assertFalse(status["busy"])
        self.assertIsNone(status["result"])
        self.assertEqual(recorder.starts, 0)
        self.assertEqual(recorder.stops, 1)

    def test_cancelled_worker_cannot_replace_previous_recovery_or_read_new_settings(self):
        first = Pipeline("First recoverable sentence.")
        second = Pipeline("Stale cancelled sentence.", blocked=True)
        self.addCleanup(second.release.set)
        observed_settings = []
        recorders = []

        def factory(settings):
            observed_settings.append(settings)
            recorder = Recorder()
            recorders.append(recorder)
            return recorder, (first if len(recorders) == 1 else second)

        client = self.make_client(factory)
        first_id = str(uuid4())
        self.assertTrue(self.command(client, "start", first_id)["accepted"])
        self.assertTrue(self.command(client, "stop", first_id)["accepted"])
        self.assertTrue(self.app.state.controller.wait_for_worker(5))
        second_id = str(uuid4())
        self.assertTrue(self.command(client, "start", second_id)["accepted"])
        self.assertTrue(self.command(client, "stop", second_id)["accepted"])
        self.assertTrue(second.entered.wait(5))

        changed = deepcopy(self.settings)
        changed["audio"]["blocksize"] = 2048
        saved = client.put("/settings", headers=self.headers, json={"settings": changed})
        self.assertEqual(saved.status_code, 200, saved.text)
        for _ in range(3):
            status = client.get("/status", headers=self.headers).json()
            self.assertEqual(status["state"], "processing")
            self.assertEqual(status["session_id"], second_id)
            self.assertEqual(status["result_session_id"], first_id)
            self.assertFalse(status["automatic_insertion"])
        self.assertEqual(observed_settings[1]["audio"]["blocksize"], 512)
        with self.assertRaises(TypeError):
            observed_settings[1]["audio"]["blocksize"] = 4096

        self.assertTrue(self.command(client, "cancel", second_id, binding="escape")["accepted"])
        rejected = self.command(client, "start", str(uuid4()))
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "busy")
        for _ in range(3):
            warmup = client.post("/runtime/warmup", headers=self.headers)
            self.assertEqual(warmup.status_code, 409)
        self.assertEqual(len(recorders), 2)
        second.release.set()
        self.assertTrue(self.app.state.controller.wait_for_worker(5))
        final = client.get("/status", headers=self.headers).json()
        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(final["result_session_id"], first_id)
        self.assertEqual(final["result"]["polished_text"], first.text)
        self.assertEqual([(r.starts, r.stops) for r in recorders], [(1, 1), (1, 1)])

    def test_old_engine_and_shutdown_reject_new_work_without_factory(self):
        calls = []
        client = self.make_client(lambda settings: calls.append(settings))
        stale = self.command(client, "start", str(uuid4()), engine_id=str(uuid4()))
        self.assertFalse(stale["accepted"])
        response = client.post("/runtime/shutdown", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.app.state.shutdown_requested.is_set())
        closed = self.command(client, "start", str(uuid4()))
        self.assertFalse(closed["accepted"])
        self.assertEqual(closed["reason"], "shutdown")
        self.assertFalse(calls)

    def test_cancel_ack_for_unknown_session_prevents_delayed_start(self):
        calls = []
        client = self.make_client(lambda settings: calls.append(settings))
        sid = str(uuid4())
        # A lost start response is uncertain: cancellation may reach the
        # controller before the original start request was even accepted.
        cancelled = self.command(client, "cancel", sid, command_seq=2)
        self.assertFalse(cancelled["accepted"])
        self.assertEqual(cancelled["reason"], "stale_session")
        delayed = self.command(client, "start", sid, command_seq=1)
        self.assertFalse(delayed["accepted"])
        self.assertEqual(delayed["reason"], "stale_command")
        status = client.get("/status", headers=self.headers).json()
        self.assertEqual(status["state"], "idle")
        self.assertFalse(status["busy"])
        self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()
