from __future__ import annotations

import importlib.util
import os
import secrets
from contextlib import asynccontextmanager
from copy import deepcopy
from threading import Event, Thread, RLock

from services.runtime.controller import SerializedSessionController, PROTOCOL_VERSION, validate_session_settings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.asr.profiles import resolve_asr_profile
from services.common.paths import ProjectPaths
from services.config.settings import load_settings, save_settings as persist_settings
from services.pipeline.models import PipelineResult
from services.runtime.runner import (
    DEFAULT_ASR_PROFILE,
    dependency_readiness,
    summarize_runtime_log,
)
from services.snippets.store import delete_snippet, list_snippets, upsert_snippet
from services.vocabulary.store import VocabularyStore


FindSpec = Callable[[str], Any]

RUNTIME_API_CAPABILITIES = (
    "serialized-sessions-v1",
    "configured-runtime-diagnostics",
    "recoverable-results",
    "insertion-disabled",
    "insertion-transactions-v1",
)


@dataclass
class RuntimeApiState:
    root: Path
    log_path: Path
    find_spec: FindSpec
    session: Any | None = None
    profile: str = DEFAULT_ASR_PROFILE
    quiet_mode: bool = False
    quality_fallback: bool = False
    asr_service: Any | None = None

    @property
    def settings_path(self) -> Path:
        return self.root / "config" / "settings.yaml"

    @property
    def snippets_path(self) -> Path:
        return self.root / "config" / "snippets.yaml"

    @property
    def db_path(self) -> Path:
        return self.root / "db" / "localflow.db"

    def load_runtime_settings(self) -> dict[str, Any]:
        settings = load_settings(self.settings_path)
        self._apply_runtime_settings(settings)
        return settings

    def save_runtime_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        saved = persist_settings(self.settings_path, settings)
        self._apply_runtime_settings(saved)
        # Active settings snapshots are owned by SerializedSessionController.
        return saved

    def diagnostic_lines(self, *, limit: int = 100) -> list[str]:
        self.load_runtime_settings()
        lines = summarize_runtime_log(self.log_path, limit=limit)
        if lines and lines[0].startswith("Runtime: "):
            history = [f"Last recording: {lines[0].removeprefix('Runtime: ')}", *lines[1:]]
        else:
            history = lines
        return [self._runtime_summary_line(), *history]

    def _apply_runtime_settings(self, settings: dict[str, Any]) -> None:
        runtime = settings.get("runtime", {})
        if isinstance(runtime, dict):
            self.profile = str(runtime.get("profile", self.profile) or DEFAULT_ASR_PROFILE)
            self.quiet_mode = bool(runtime.get("quiet_mode", self.quiet_mode))
            self.quality_fallback = bool(
                runtime.get("quality_fallback", self.quality_fallback)
            )

    def _runtime_summary_line(self) -> str:
        effective = load_settings(self.settings_path)
        model = effective["models"]["whisper"]
        threads = effective["models"]["whisper_cpu_threads"]
        if self.profile == 'local-multilingual':
            model = effective['local_asr']['snapshot_path']
            threads = effective['local_asr']['cpu_threads']
        return (
            f"Configured runtime: profile={self.profile} model={model} "
            f"threads={threads} quiet={self.quiet_mode}"
        )



def create_app(
    *,
    root: str | Path | None = None,
    session: Any | None = None,
    log_path: str | Path | None = None,
    find_spec: FindSpec = importlib.util.find_spec,
    engine_token: str | None = None,
    engine_id: str | None = None,
    controller_factory: Callable | None = None,
    asr_service: Any | None = None,
):
    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as exc:
        raise RuntimeError("fastapi is required to run the LocalFlow runtime API") from exc

    repo_root = _resolve_root(root)
    state = RuntimeApiState(
        root=repo_root,
        log_path=Path(log_path) if log_path is not None else repo_root / "artifacts" / "logs" / "runtime.jsonl",
        find_spec=find_spec,
        session=session,
    )
    token = engine_token or os.environ.get("WEZZA_ENGINE_TOKEN")
    instance_id = engine_id or os.environ.get("WEZZA_ENGINE_ID")
    if not token or not instance_id:
        raise RuntimeError("Explicit per-run engine credentials are required")
    if asr_service is None:
        from services.asr.worker import AsrWorkerService
        asr_service = AsrWorkerService()
    state.asr_service = asr_service
    controller = SerializedSessionController(
        factory=controller_factory or (lambda settings: _create_session_adapters(state, settings)),
        settings_provider=lambda: load_settings(state.settings_path), engine_id=instance_id,
    )
    identity = {"application_id": "wezza", "protocol_version": PROTOCOL_VERSION, "engine_id": instance_id}
    def close_resources():
        controller.close()
        reaped = state.asr_service.close(timeout_seconds=5)
        settled = controller.wait_for_worker(2)
        return reaped and settled

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            from starlette.concurrency import run_in_threadpool
            if not await run_in_threadpool(close_resources):
                raise RuntimeError('Local inference worker cleanup incomplete')

    app = FastAPI(title="LocalFlow Runtime API", version="0.1.0", lifespan=lifespan)
    app.state.controller = controller
    app.state.asr_service = asr_service
    app.state.close_resources = close_resources
    app.state.shutdown_requested = Event()

    @app.middleware("http")
    async def authenticate(request, call_next):
        from starlette.responses import JSONResponse
        if request.url.path != "/health":
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(supplied, f"Bearer {token}"):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        if request.url.path != "/health" and response.status_code < 400:
            import json
            body = b"".join([chunk async for chunk in response.body_iterator])
            payload = json.loads(body)
            if isinstance(payload, dict): payload.update(identity)
            return JSONResponse(payload, status_code=response.status_code)
        return response

    settings_lock = RLock()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/runtime/handshake")
    def handshake() -> dict[str, Any]:
        return {"status": "ok", **identity, "capabilities": ["serialized-sessions-v1", "recoverable-results", "insertion-disabled", "insertion-transactions-v1"]}

    @app.get("/status")
    def status() -> dict[str, Any]:
        settings = controller.settings_snapshot()
        runtime = settings.get("runtime", {})
        return {**controller.snapshot(), "profile": runtime.get("profile", DEFAULT_ASR_PROFILE),
                "quiet_mode": runtime.get("quiet_mode", False), "quality_fallback": False,
                "asr_worker": state.asr_service.snapshot()}

    @app.post("/runtime/start")
    def start_runtime(payload: dict[str, Any]) -> dict[str, Any]:
        return controller.command("start", payload)

    @app.post("/runtime/stop")
    def stop_runtime(payload: dict[str, Any]) -> dict[str, Any]:
        return controller.command("stop", payload)

    @app.post("/runtime/cancel")
    def cancel_runtime(payload: dict[str, Any]) -> dict[str, Any]:
        return controller.command("cancel", payload)

    @app.post("/runtime/commit/reserve")
    def reserve_commit(payload: dict[str, Any]) -> dict[str, Any]:
        return controller.reserve_commit(payload)

    @app.post("/runtime/commit/complete")
    def complete_commit(payload: dict[str, Any]) -> dict[str, Any]:
        return controller.complete_commit(payload)

    @app.post("/runtime/shutdown")
    def shutdown_runtime() -> dict[str, Any]:
        if not close_resources():
            raise HTTPException(status_code=503, detail='Local inference worker cleanup incomplete; retry shutdown')
        app.state.shutdown_requested.set()
        return {"status": "ok", "resources_reaped": True, **identity}

    @app.get("/runtime/check")
    def check_runtime() -> dict[str, Any]:
        return {
            "status": "ok",
            "dependencies": [
                {
                    "name": dependency.name,
                    "available": dependency.available,
                    "required": dependency.required,
                    "install_hint": dependency.install_hint,
                }
                for dependency in dependency_readiness(state.find_spec)
            ],
        }

    @app.get("/runtime/capabilities")
    def runtime_capabilities() -> dict[str, Any]:
        return {"status": "ok", "capabilities": list(RUNTIME_API_CAPABILITIES)}

    @app.post("/runtime/warmup")
    def runtime_warmup() -> dict[str, Any]:
        raise HTTPException(status_code=409, detail="Warmup is unavailable during the safety rebuild")

    @app.get("/runtime/diagnostics")
    def runtime_diagnostics(limit: int = 100) -> dict[str, Any]:
        with settings_lock:
            lines = state.diagnostic_lines(limit=limit)
        snapshot = controller.snapshot()
        active = snapshot.get("active_configuration")
        if active:
            lines.insert(1, f"Session snapshot: state={snapshot['state']} profile={active['profile']} model={active['model']} threads={active['cpu_threads']}; configured changes apply to the next session")
        return {"status": "ok", "lines": lines}

    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        return {"status": "ok", "settings": controller.settings_snapshot()}

    @app.put("/settings")
    def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            raise HTTPException(status_code=400, detail="settings must be an object")
        try:
            apply_profile = payload.get("apply_profile_defaults", False)
            if not isinstance(apply_profile, bool):
                raise ValueError("apply_profile_defaults must be a boolean")
            if apply_profile:
                settings = deepcopy(settings)
                runtime = settings.get("runtime", {})
                if not isinstance(runtime, dict):
                    raise ValueError("runtime must be an object")
                profile_name = runtime.get("profile")
                if not isinstance(profile_name, str):
                    raise ValueError("A selected profile is required to apply its defaults")
                profile = resolve_asr_profile(profile_name)
                models = settings.setdefault("models", {})
                if not isinstance(models, dict):
                    raise ValueError("models must be an object")
                models.update(whisper=profile.model_name,
                              whisper_compute_type=profile.compute_type,
                              whisper_cpu_threads=profile.cpu_threads)
            validate_session_settings(settings)
            with settings_lock:
                saved = persist_settings(state.settings_path, settings)
                controller.update_settings(saved)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "settings": saved}

    @app.get("/vocabulary/terms")
    def get_vocabulary_terms(limit: int | None = None) -> dict[str, Any]:
        return {"status": "ok", "terms": _vocabulary_store(state).list_vocabulary(limit=limit)}

    @app.post("/vocabulary/terms")
    def post_vocabulary_term(payload: dict[str, Any]) -> dict[str, Any]:
        word = str(payload.get("word", "")).strip()
        if not word:
            raise HTTPException(status_code=400, detail="word is required")
        store = _vocabulary_store(state)
        store.add_word(word)
        return {"status": "ok", "terms": store.list_vocabulary()}

    @app.delete("/vocabulary/terms/{word:path}")
    def delete_vocabulary_term(word: str) -> dict[str, Any]:
        store = _vocabulary_store(state)
        deleted = store.delete_word(word)
        return {"status": "ok", "deleted": deleted, "terms": store.list_vocabulary()}

    @app.get("/vocabulary/corrections")
    def get_corrections(limit: int | None = None) -> dict[str, Any]:
        return {
            "status": "ok",
            "corrections": _vocabulary_store(state).list_correction_pairs(limit=limit),
        }

    @app.post("/vocabulary/corrections")
    def post_correction(payload: dict[str, Any]) -> dict[str, Any]:
        original = str(payload.get("original", "")).strip()
        corrected = str(payload.get("corrected", "")).strip()
        if not original or not corrected:
            raise HTTPException(status_code=400, detail="original and corrected are required")
        store = _vocabulary_store(state)
        store.record_correction(original, corrected)
        return {"status": "ok", "corrections": store.list_correction_pairs()}

    @app.delete("/vocabulary/corrections")
    def delete_correction_pair(
        original: str = Query(...),
        corrected: str = Query(...),
    ) -> dict[str, Any]:
        store = _vocabulary_store(state)
        deleted = store.delete_correction(original, corrected)
        return {"status": "ok", "deleted": deleted, "corrections": store.list_correction_pairs()}

    @app.get("/corrections/pending")
    def get_pending_corrections() -> dict[str, Any]:
        watcher = None  # No insertion, therefore no automatic correction observation.
        if watcher is None or not hasattr(watcher, "list_pending"):
            return {"status": "ok", "pending": []}
        return {
            "status": "ok",
            "pending": [
                _serialize_pending_correction(item) for item in watcher.list_pending()
            ],
        }

    @app.post("/corrections/pending/{pending_id}/confirm")
    def confirm_pending_correction(pending_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        watcher = None  # No insertion, therefore no automatic correction observation.
        if watcher is None or not hasattr(watcher, "confirm_pending"):
            raise HTTPException(status_code=404, detail="pending correction not found")
        if not _pending_correction_exists(watcher, pending_id):
            raise HTTPException(status_code=404, detail="pending correction not found")

        original = str(payload.get("original", "")).strip()
        corrected = str(payload.get("corrected", "")).strip()
        if not original or not corrected:
            raise HTTPException(status_code=400, detail="original and corrected are required")

        result = watcher.confirm_pending(pending_id)
        if result is False:
            raise HTTPException(status_code=404, detail="pending correction not found")

        store = _vocabulary_store(state)
        store.record_correction(original, corrected)
        return {"status": "ok", "corrections": store.list_correction_pairs()}

    @app.post("/corrections/pending/{pending_id}/dismiss")
    def dismiss_pending_correction(pending_id: str) -> dict[str, Any]:
        watcher = None  # No insertion, therefore no automatic correction observation.
        if watcher is None or not hasattr(watcher, "dismiss_pending"):
            raise HTTPException(status_code=404, detail="pending correction not found")
        if not _pending_correction_exists(watcher, pending_id):
            raise HTTPException(status_code=404, detail="pending correction not found")

        result = watcher.dismiss_pending(pending_id)
        if result is False:
            raise HTTPException(status_code=404, detail="pending correction not found")
        return {"status": "ok"}

    @app.get("/learning/suggestions")
    def get_learning_suggestions() -> dict[str, Any]:
        return {
            "status": "ok",
            "suggestions": _vocabulary_store(state).learning_suggestions(),
        }

    @app.get("/snippets")
    def get_snippets() -> dict[str, Any]:
        return {"status": "ok", "snippets": list_snippets(state.snippets_path)}

    @app.post("/snippets")
    def post_snippet(payload: dict[str, Any]) -> dict[str, Any]:
        trigger_phrase = str(payload.get("trigger_phrase", "")).strip()
        expansion = str(payload.get("expansion", ""))
        if not trigger_phrase:
            raise HTTPException(status_code=400, detail="trigger_phrase is required")
        snippets = upsert_snippet(state.snippets_path, trigger_phrase, expansion)
        return {"status": "ok", "snippets": snippets}

    @app.delete("/snippets/{trigger_phrase:path}")
    def delete_snippet_endpoint(trigger_phrase: str) -> dict[str, Any]:
        deleted = delete_snippet(state.snippets_path, trigger_phrase)
        return {"status": "ok", "deleted": deleted, "snippets": list_snippets(state.snippets_path)}

    return app


def _resolve_root(root: str | Path | None) -> Path:
    if root is not None:
        return Path(root).resolve()
    return ProjectPaths.discover().repo_root


def _vocabulary_store(state: RuntimeApiState) -> VocabularyStore:
    return VocabularyStore(state.db_path)


def _correction_watcher(state: RuntimeApiState) -> Any | None:
    return None  # No inserted result exists in M1.


def _runtime_session_config(settings: dict[str, Any]) -> tuple[str, bool, bool, bool]:
    runtime = settings.get("runtime", {}) if isinstance(settings.get("runtime"), dict) else {}
    return (
        str(runtime.get("profile", DEFAULT_ASR_PROFILE) or DEFAULT_ASR_PROFILE),
        bool(runtime.get("use_ollama", False)),
        bool(runtime.get("quiet_mode", False)),
        bool(runtime.get("quality_fallback", False)),
    )


def _pending_correction_exists(watcher: Any, pending_id: str) -> bool:
    if not hasattr(watcher, "list_pending"):
        return True
    return any(
        str(_pending_correction_value(item, "id") or "") == pending_id
        for item in watcher.list_pending()
    )


def _serialize_pending_correction(item: Any) -> dict[str, str | None]:
    return {
        "id": str(_pending_correction_value(item, "id") or ""),
        "original": str(_pending_correction_value(item, "original") or ""),
        "raw_transcript": str(_pending_correction_value(item, "raw_transcript") or ""),
        "app_name": _optional_pending_string(item, "app_name"),
        "window_title": _optional_pending_string(item, "window_title"),
        "detected_at": str(_pending_correction_value(item, "detected_at") or ""),
    }


def _optional_pending_string(item: Any, key: str) -> str | None:
    value = _pending_correction_value(item, key)
    return str(value) if value is not None else None


def _pending_correction_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required to run the LocalFlow runtime API") from exc

    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765))
    def watch_shutdown():
        app.state.shutdown_requested.wait()
        server.should_exit = True
    Thread(target=watch_shutdown, name="wezza-shutdown", daemon=True).start()
    try:
        server.run()
    finally:
        if not app.state.close_resources():
            raise RuntimeError('Local inference worker cleanup incomplete')




def _create_session_adapters(state: RuntimeApiState, settings: dict):
    from services.asr.audio_capture import AudioRecorder
    from services.pipeline.factory import build_pipeline
    runtime = settings.get("runtime", {})
    profile = resolve_asr_profile(runtime.get("profile", DEFAULT_ASR_PROFILE))
    overrides = {}
    if profile.name == 'local-multilingual':
        from services.runtime.local_asr import worker_config, RawRecoveryFormatter
        from services.snippets.engine import SnippetEngine
        if state.asr_service is None:
            raise RuntimeError('Local inference service is unavailable')
        overrides = {'transcriber': state.asr_service.transcriber(worker_config(settings)),
                     'formatter': RawRecoveryFormatter(), 'snippets': SnippetEngine([])}
    pipeline = build_pipeline(root=state.root, settings=settings,
                              use_ollama=bool(runtime.get("use_ollama", False)),
                              quiet_mode=bool(runtime.get("quiet_mode", False)),
                              **profile.pipeline_kwargs(), **overrides)
    audio = settings["audio"]
    recorder = AudioRecorder.microphone(sample_rate=audio["sample_rate"], blocksize=audio["blocksize"])
    return recorder, pipeline


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    main()
