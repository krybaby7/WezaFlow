import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.pipeline.models import AppContext, PipelineResult

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    TestClient = None


class PipelineServerTests(unittest.TestCase):
    def test_serialize_result_handles_no_speech(self):
        from services.pipeline_server import serialize_result

        self.assertEqual({"status": "no_speech"}, serialize_result(None))

    def test_serialize_result_returns_pipeline_payload(self):
        from services.pipeline_server import serialize_result

        result = PipelineResult(
            raw_transcript="hello",
            polished_text="Hello.",
            app_context=AppContext(
                app_name="Code.exe",
                window_title="LocalFlow",
                category="code",
                browser_url=None,
                visible_text=[],
            ),
            duration_ms=42,
        )

        payload = serialize_result(result)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["raw_transcript"], "hello")
        self.assertEqual(payload["polished_text"], "Hello.")
        self.assertEqual(payload["app_context"]["category"], "code")

    def test_create_app_exposes_runtime_status_routes(self):
        if TestClient is None:
            self.skipTest("fastapi is not installed in this Python environment")
        from services.config.settings import load_settings, save_settings
        from services.pipeline_server import create_app, create_runtime_api_app

        for profile, quality_fallback in (("low-impact", False), ("snappy", True)):
            with self.subTest(profile=profile, quality_fallback=quality_fallback):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    settings_path = root / "config" / "settings.yaml"
                    settings_path.parent.mkdir()
                    settings = load_settings(settings_path)
                    settings["runtime"].update(
                        profile=profile, quality_fallback=quality_fallback
                    )
                    save_settings(settings_path, settings)
                    with patch(
                        "services.pipeline_server.create_runtime_api_app",
                        side_effect=lambda: create_runtime_api_app(root=root),
                    ):
                        client = TestClient(create_app(pipeline=object()))
                    payload = client.get("/status").json()

                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["profile"], profile)
                self.assertEqual(payload["quality_fallback"], quality_fallback)



if __name__ == "__main__":
    unittest.main()
