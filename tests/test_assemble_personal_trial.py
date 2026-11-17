import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import assemble_personal_trial as assembly
from services.config.settings import load_settings


class PersonalTrialAssemblyTests(unittest.TestCase):
    def test_explicit_output_creates_isolated_state_and_refuses_existing_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, source = root / "original", root / "source"
            live = original / "artifacts" / "Wezza-Personal-Trial"
            live.mkdir(parents=True)
            (live / ".package-owner").write_text(assembly.MARKER, encoding="utf-8")
            live_state = live / "live-state.txt"
            live_state.write_text("deployed", encoding="utf-8")

            services = source / "services"
            services.mkdir(parents=True)
            (services / "local.db").write_bytes(b"private state")
            (source / "pyproject.toml").touch()
            (source / "db").mkdir()
            (source / "db" / "schema.sql").touch()
            (source / "config").mkdir()
            for name in ("app_profiles.yaml", "snippets.yaml"):
                (source / "config" / name).touch()
            scripts = source / "scripts" / "personal_trial"
            scripts.mkdir(parents=True)
            for name in ("Launch Wezza.vbs", "launch.ps1", "READ ME FIRST.md"):
                (scripts / name).touch()

            model = original / "artifacts" / "m4-capacity-9b-2026-10-29"
            model.mkdir(parents=True)
            (model / "qwen3.5-9b-q4_k_m.gguf").write_bytes(b"model")
            (model / "chat_template.no-thinking.jinja").write_text("template", encoding="utf-8")
            runtime = original / "artifacts" / "m4-local-editor-runtime"
            (runtime / "bin").mkdir(parents=True)
            server = b"test server"
            (runtime / "bin" / "llama-server.exe").write_bytes(server)
            (runtime / "parent-verification.json").write_text(json.dumps({"files": [{
                "path": "llama-server.exe", "bytes": len(server), "sha256": hashlib.sha256(server).hexdigest()
            }]}), encoding="utf-8")
            exe = root / "fixture.exe"
            exe.write_bytes(b"desktop executable")
            candidate = live.parent / "Candidate #1"

            with patch.multiple(assembly, ORIGINAL=original, SOURCE=source), patch.object(
                assembly, "MODEL_BYTES", 5
            ):
                assembly.assemble(exe, output_dir=candidate)
                with self.assertRaises(FileExistsError):
                    assembly.assemble(exe, output_dir=candidate)
                for invalid in (live, live / "nested candidate"):
                    with self.assertRaises(ValueError):
                        assembly.assemble(exe, output_dir=invalid)

            self.assertEqual(live_state.read_text(encoding="utf-8"), "deployed")
            self.assertEqual(set(live.iterdir()), {live / ".package-owner", live_state})
            self.assertEqual((candidate / "Wezza.exe").read_bytes(), exe.read_bytes())
            self.assertTrue((candidate / "logs").is_dir())
            self.assertEqual(list((candidate / "project" / "db").iterdir()), [candidate / "project" / "db" / "schema.sql"])
            self.assertFalse((candidate / "project" / "services" / "local.db").exists())
            settings = load_settings(candidate / "project" / "config" / "settings.yaml")
            self.assertEqual(Path(settings["local_editor"]["manifest_path"]),
                             candidate / "assets" / "local-editor-manifest.json")
