from __future__ import annotations

from typing import Any

from services.pipeline.factory import build_pipeline
from services.pipeline.models import PipelineResult
from services.runtime.api import create_app as create_runtime_api_app


def serialize_result(result: PipelineResult | None) -> dict[str, Any]:
    if result is None:
        return {"status": "no_speech"}
    payload = result.to_dict()
    payload["status"] = "ok"
    return payload


def create_app(pipeline: Any | None = None):
    try:
        from fastapi import HTTPException
    except ImportError as exc:
        raise RuntimeError("fastapi is required to run the LocalFlow pipeline server") from exc

    app = create_runtime_api_app()
    @app.post("/process_samples")
    def process_samples(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(status_code=409, detail="Legacy processing is unavailable; use serialized runtime sessions")

    @app.post("/command")
    def command(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(status_code=409, detail="Command editing is unavailable until M2")

    return app


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required to run the LocalFlow pipeline server") from exc

    uvicorn.run(create_app(), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
