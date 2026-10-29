"""Strict paired manifest validation, including local audio integrity."""
import hashlib
import json
from pathlib import Path
import re
import math
from typing import Any


def read_json(path: str | Path) -> Any:
    """Reject duplicate object keys, including repeated prediction sample IDs."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON number: " + value)

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number: " + value)
        return parsed

    return json.loads(
        Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object,
        parse_constant=reject_constant, parse_float=finite_float,
    )


def load_manifest(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported manifest schema_version")
    if not isinstance(data.get("samples"), list) or not data["samples"]:
        raise ValueError("samples must be a nonempty list")
    ids = set()
    for s in data["samples"]:
        if not isinstance(s, dict):
            raise ValueError("sample must be an object")
        sid = s.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", sid):
            raise ValueError("invalid sample id")
        if sid in ids:
            raise ValueError("duplicate sample id: " + sid)
        ids.add(sid)
        if s.get("split") not in ("development", "heldout"):
            raise ValueError("invalid split")
        if not isinstance(s.get("verbatim"), str):
            raise ValueError("verbatim must be text")
        finals = s.get("acceptable_final_texts")
        if not isinstance(finals, list) or not finals or not all(isinstance(t, str) for t in finals):
            raise ValueError("acceptable_final_texts must contain text")
        tags = s.get("language_tags")
        if not isinstance(tags, list) or not tags or any(t not in ("EN", "FR", "AR", "mixed") for t in tags):
            raise ValueError("invalid language_tags")
        meta = s.get("metadata")
        if not isinstance(meta, dict) or not all(isinstance(meta.get(k), str) for k in ("dialect", "script")):
            raise ValueError("metadata requires dialect and script")
        literals = s.get("protected_literals", [])
        if not isinstance(literals, list) or any(not isinstance(x, dict) or not isinstance(x.get("text"), str) or not x["text"] or not isinstance(x.get("category"), str) for x in literals):
            raise ValueError("invalid protected_literals")
        spans = s.get("spans", [])
        if not isinstance(spans, list):
            raise ValueError("invalid spans")
        for span in spans:
            if not isinstance(span, dict) or type(span.get("start")) is not int or type(span.get("end")) is not int or not 0 <= span["start"] < span["end"] <= len(s["verbatim"]) or span.get("language") not in ("EN", "FR", "AR", "mixed"):
                raise ValueError("invalid span")
        audio = s.get("audio")
        if not isinstance(audio, dict) or not isinstance(audio.get("path"), str) or not audio["path"]:
            raise ValueError("invalid audio path")
        relative = Path(audio["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("audio path must be relative within manifest directory")
        resolved = (path.parent / relative).resolve()
        if not resolved.is_relative_to(path.parent.resolve()):
            raise ValueError("audio path escapes manifest directory")
        if not resolved.is_file():
            raise ValueError("missing audio: " + str(relative))
        digest = audio.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid audio sha256")
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != digest:
            raise ValueError("audio checksum mismatch: " + sid)
    return data
