"""Explicit-split, replay-only CLI. Never loads a model or accesses desktop state."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Sequence

from .manifest import load_manifest, read_json
from .scoring import score_final, score_transcription


def run_replay(
    manifest: str | Path,
    split: str,
    stage: str,
    predictions: dict[str, Any],
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start = time.perf_counter()
    if split not in ("development", "heldout"):
        raise ValueError("explicit split required")
    if stage not in ("asr", "full"):
        raise ValueError("invalid stage")
    data = load_manifest(manifest)
    manifest_sha256 = hashlib.sha256(Path(manifest).read_bytes()).hexdigest()
    selected = [s for s in data["samples"] if s["split"] == split]
    if not selected:
        raise ValueError("no samples in selected split")
    if not isinstance(predictions, dict):
        raise ValueError("predictions must map sample ids to outputs")
    for s in selected:
        if s["id"] not in predictions:
            raise ValueError("missing prediction: " + s["id"])
    validation_seconds = time.perf_counter() - start
    if configuration is not None and not isinstance(configuration, dict):
        raise ValueError("configuration must be a JSON object")
    config = dict(configuration) if configuration is not None else {}
    fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    rows = []
    scoring_start = time.perf_counter()
    for s in selected:
        row_start = time.perf_counter()
        p = predictions[s["id"]]
        row = {
            "id": s["id"], "audio_sha256": s["audio"]["sha256"],
            "language_tags": list(s["language_tags"]), "raw_score": None,
            "final_score": None, "error": None,
        }
        try:
            if not isinstance(p, dict):
                raise ValueError("prediction must be an object")
            raw = p.get("raw")
            if isinstance(raw, str):
                row["raw"] = raw
                row["raw_score"] = score_transcription(s["verbatim"], raw).to_dict()
            if p.get("error"):
                raise ValueError(str(p["error"]))
            if not isinstance(raw, str):
                raise ValueError("missing raw prediction")
            if stage == "full":
                final = p.get("final")
                if not isinstance(final, str):
                    raise ValueError("missing final prediction")
                row["final"] = final
                row["final_score"] = score_final(s["acceptable_final_texts"], final, s.get("protected_literals", []))
        except (ValueError, TypeError) as exc:
            row["error"] = str(exc)
        row["scoring_seconds"] = time.perf_counter() - row_start
        rows.append(row)
    failed = sum(r["error"] is not None for r in rows)
    return {
        "schema_version": 1, "split": split, "stage": stage, "mode": "replay",
        "counts": {"selected": len(rows), "succeeded": len(rows)-failed, "failed": failed},
        "rows": rows,
        "metadata": {
            "manifest_sha256": manifest_sha256,
            "configuration": config, "configuration_fingerprint": fingerprint,
            "model_revision": config.get("model_revision"), "model_load_seconds": None,
            "model_load_warmth": "not_applicable_replay",
            "inference_warmth": "not_applicable_replay",
            "validation_seconds": validation_seconds,
            "scoring_seconds": time.perf_counter()-scoring_start, "inference_seconds": None,
            "energy_joules": None, "power_watts": None,
            "resource_measurement_status": "not_measured_replay",
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--split", choices=["development", "heldout"], required=True)
    parser.add_argument("--stage", choices=["asr", "full"], required=True)
    parser.add_argument("--mode", choices=["replay"], default="replay")
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--configuration")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        predictions = read_json(args.predictions)
        configuration = read_json(args.configuration) if args.configuration else None
        report = run_replay(args.manifest, args.split, args.stage, predictions, configuration)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.error("cannot write report: " + str(exc))
    return 1 if report["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
