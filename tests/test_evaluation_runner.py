import hashlib
import json
import sys
import contextlib
import io

import unittest
import tempfile
from pathlib import Path

from services.evaluation.manifest import load_manifest, read_json
from services.evaluation.runner import run_replay, main


def fixture(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"fixture only")
    sample = {"id": "dev-001", "split": "development", "audio": {"path": "sample.wav", "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}, "verbatim": "Bonjour مرحبا", "acceptable_final_texts": ["Bonjour مرحبا"], "language_tags": ["mixed"], "metadata": {"dialect": "unknown", "script": "mixed"}}
    manifest = {"schema_version": 1, "samples": [sample, dict(sample, id="held-001", split="heldout")]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


class RunnerTests(unittest.TestCase):
    def test_manifest_validation(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, data = fixture(tmp_path)
        assert len(load_manifest(path)["samples"]) == 2
        for change, match in [(lambda d: d.update(schema_version=2), "schema"), (lambda d: d["samples"][1].update(id="dev-001"), "duplicate"), (lambda d: d["samples"][0].update(split="test"), "split"), (lambda d: d["samples"][0]["audio"].update(path="missing.wav"), "missing"), (lambda d: d["samples"][0]["audio"].update(sha256="0"*64), "checksum")]:
            fresh = json.loads(json.dumps(data)); change(fresh)
            path.write_text(json.dumps(fresh), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, match): load_manifest(path)


    def test_replay_isolates_heldout_and_retains_failures(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, _ = fixture(tmp_path)
        before = set(sys.modules)
        report = run_replay(path, "development", "full", {"dev-001": {"raw": "Bonjour مرحبا", "error": "formatter failed"}})
        assert report["counts"] == {"selected": 1, "succeeded": 0, "failed": 1}
        assert report["rows"][0]["raw_score"]["word_error_rate"] == 0
        assert report["rows"][0]["error"] == "formatter failed"
        assert report["metadata"]["model_revision"] is None
        assert report["metadata"]["inference_warmth"] == "not_applicable_replay"
        assert not any(m.startswith(("services.asr", "services.injection", "services.context", "torch")) for m in set(sys.modules)-before)
        with self.assertRaisesRegex(ValueError, "prediction"): run_replay(path, "heldout", "asr", {})


    def test_cli_requires_predictions(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, _ = fixture(tmp_path)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main(["--manifest", str(path), "--split", "development", "--stage", "asr", "--output", str(tmp_path / "out.json")])

    def test_cli_replays_final_and_fingerprints_configuration(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, _ = fixture(tmp_path)
        predictions = tmp_path / "predictions.json"
        predictions.write_text(json.dumps({"dev-001": {"raw": "Bonjour مرحبا", "final": "Bonjour مرحبا"}}), encoding="utf-8")
        output = tmp_path / "out.json"
        assert main(["--manifest", str(path), "--split", "development", "--stage", "full", "--predictions", str(predictions), "--output", str(output)]) == 0
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["rows"][0]["final_score"]["exact_match"]
        assert len(report["metadata"]["configuration_fingerprint"]) == 64
        assert report["metadata"]["inference_seconds"] is None
        assert report["metadata"]["scoring_seconds"] >= 0

    def test_optional_spans_and_malformed_samples(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, data = fixture(tmp_path)
        for key, value in [("spans", [{"start": 0, "end": 999, "language": "FR"}]), ("language_tags", ["DE"]), ("acceptable_final_texts", []), ("protected_literals", [{"text": "x"}]), ("metadata", {})]:
            fresh = json.loads(json.dumps(data)); fresh["samples"][0][key] = value
            path.write_text(json.dumps(fresh), encoding="utf-8")
            with self.assertRaises(ValueError): load_manifest(path)

    def test_duplicate_json_keys_are_rejected(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, _ = fixture(tmp_path)
        path.write_text('{"schema_version":1,"schema_version":1,"samples":[]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            load_manifest(path)

    def test_provenance_and_unmeasured_resources(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, data = fixture(tmp_path)
        report = run_replay(path, "development", "asr", {"dev-001": {"raw": "Bonjour مرحبا"}})
        assert report["metadata"]["manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert report["rows"][0]["audio_sha256"] == data["samples"][0]["audio"]["sha256"]
        assert report["rows"][0]["language_tags"] == ["mixed"]
        assert report["metadata"]["energy_joules"] is None
        assert report["metadata"]["power_watts"] is None
        assert report["metadata"]["resource_measurement_status"] == "not_measured_replay"

    def test_failed_cli_writes_nested_report_and_returns_failure(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, _ = fixture(tmp_path)
        predictions = tmp_path / "predictions.json"
        predictions.write_text(json.dumps({"dev-001": {"error": "recognizer failed"}}), encoding="utf-8")
        output = tmp_path / "nested" / "reports" / "out.json"
        assert main(["--manifest", str(path), "--split", "development", "--stage", "asr", "--predictions", str(predictions), "--output", str(output)]) == 1
        assert json.loads(output.read_text(encoding="utf-8"))["counts"]["failed"] == 1

    def test_configuration_requires_object_and_finite_json(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, _ = fixture(tmp_path)
        predictions = {"dev-001": {"raw": "Bonjour مرحبا"}}
        for configuration in ([], [["model", "x"]], 5, "model", False):
            with self.subTest(configuration=configuration), self.assertRaises(ValueError):
                run_replay(path, "development", "asr", predictions, configuration)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_replay(path, "development", "asr", predictions, {"value": value})
        config = tmp_path / "configuration.json"
        for value in ("NaN", "Infinity", "-Infinity", "1e999"):
            config.write_text('{"value":' + value + '}', encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_json(config)

    def test_cli_rejects_invalid_configuration_root(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path, _ = fixture(tmp_path)
        predictions = tmp_path / "predictions.json"
        predictions.write_text(json.dumps({"dev-001": {"raw": "text"}}), encoding="utf-8")
        config = tmp_path / "configuration.json"
        for value in ([], 5, "model"):
            config.write_text(json.dumps(value), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["--manifest", str(path), "--split", "development", "--stage", "asr", "--predictions", str(predictions), "--configuration", str(config), "--output", str(tmp_path / "out.json")])
            assert caught.exception.code == 2
