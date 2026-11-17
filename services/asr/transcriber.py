from __future__ import annotations

from dataclasses import replace
from threading import RLock
from time import perf_counter
from typing import Any, Callable

from services.pipeline.models import AsrResult


class FasterWhisperBackend:
    def __init__(
        self,
        model_name: str = "large-v3-turbo",
        numpy_module: Any | None = None,
        *,
        allowed_languages: tuple[str, ...] | list[str] = (),
        condition_on_previous_text: bool | None = None,
        **model_kwargs: Any,
    ) -> None:
        self.model_name = model_name
        self.model_kwargs = model_kwargs
        self.allowed_languages = tuple(allowed_languages)
        self.condition_on_previous_text = condition_on_previous_text
        self._numpy = numpy_module
        self._model = None
        self._model_lock = RLock()

    @property
    def model(self):
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    self._model = WhisperModel(self.model_name, **self.model_kwargs)
        return self._model

    def warm_up(self, language: str | None = None) -> None:
        del language
        _ = self.model

    def transcribe(self, audio, language=None, initial_prompt=None):
        audio = self._coerce_audio(audio)
        if language is None and self.allowed_languages:
            language = self._restricted_language(audio)
        options = {}
        if self.condition_on_previous_text is not None:
            options["condition_on_previous_text"] = self.condition_on_previous_text
        return self.model.transcribe(
            audio,
            language=language,
            initial_prompt=initial_prompt,
            **options,
        )

    def _restricted_language(self, audio):
        """Most probable allowed language, or None to keep unrestricted detection."""
        try:
            _, _, probabilities = self.model.detect_language(audio)
            best = None
            for code, probability in probabilities:
                if isinstance(code, str) and code.startswith("<|") and code.endswith("|>"):
                    code = code[2:-2]
                if code in self.allowed_languages and (best is None or probability > best[1]):
                    best = (code, probability)
        except (AttributeError, TypeError, ValueError):
            return None
        return None if best is None else best[0]

    def _coerce_audio(self, audio):
        if isinstance(audio, (list, tuple)):
            numpy = self._numpy or _import_numpy()
            return numpy.asarray(audio, dtype=numpy.float32)
        return audio


class Transcriber:
    def __init__(
        self,
        backend=None,
        model_name: str = "large-v3-turbo",
        *,
        clock: Callable[[], float] = perf_counter,
        **model_kwargs: Any,
    ) -> None:
        self.backend = backend
        self._clock = clock
        self.model_name = model_name
        self.model_kwargs = model_kwargs
        self._lock = RLock()

    def transcribe(self, audio, language: str | None = None, initial_prompt: str | None = None) -> AsrResult:
        started = self._clock()
        with self._lock:
            acquired = self._clock()
            backend = self._backend()
            raw_result = backend.transcribe(
                audio,
                language=language,
                initial_prompt=initial_prompt,
            )
            # faster-whisper executes inference while its segments are iterated.
            result = self._to_asr_result(raw_result, fallback_language=language, elapsed_ms=0)
            finished = self._clock()
            elapsed_ms = int((finished - started) * 1000)
            has_duration = (
                isinstance(raw_result, AsrResult)
                or isinstance(raw_result, dict) and "duration_ms" in raw_result
                or isinstance(raw_result, tuple) and len(raw_result) == 2
                and getattr(raw_result[1], "duration", None) is not None
            )
            return replace(
                result,
                duration_ms=result.duration_ms if has_duration else elapsed_ms,
                inference_ms=int((finished - acquired) * 1000),
                queue_ms=int((acquired - started) * 1000),
            )

    def warm_up(self, language: str | None = None) -> None:
        """Prepare the backend; the default backend loads its model only."""
        with self._lock:
            backend = self._backend()
            warm_up = getattr(backend, "warm_up", None)
            if callable(warm_up):
                warm_up(language=language)
            else:
                getattr(backend, "model", None)

    def _backend(self):
        if self.backend is None:
            self.backend = FasterWhisperBackend(self.model_name, **self.model_kwargs)
        return self.backend

    @staticmethod
    def _to_asr_result(raw_result, fallback_language: str | None, elapsed_ms: int) -> AsrResult:
        if isinstance(raw_result, AsrResult):
            return raw_result

        if isinstance(raw_result, dict):
            return AsrResult(
                text=str(raw_result.get("text", "")).strip(),
                language=raw_result.get("language", fallback_language),
                duration_ms=int(raw_result.get("duration_ms", elapsed_ms)),
                audio_duration_ms=raw_result.get("audio_duration_ms"),
            )

        if isinstance(raw_result, tuple) and len(raw_result) == 2:
            segments, info = raw_result
            text = "".join(getattr(segment, "text", str(segment)) for segment in segments).strip()
            duration = getattr(info, "duration", None)
            duration_ms = int(duration * 1000) if duration is not None else elapsed_ms
            return AsrResult(
                text=text,
                language=getattr(info, "language", fallback_language),
                duration_ms=duration_ms,
                audio_duration_ms=int(duration * 1000) if duration is not None else None,
            )

        return AsrResult(text=str(raw_result).strip(), language=fallback_language, duration_ms=elapsed_ms)


def _import_numpy():
    try:
        import numpy
    except ImportError as exc:
        raise RuntimeError("numpy is required to transcribe in-memory audio samples") from exc
    return numpy
