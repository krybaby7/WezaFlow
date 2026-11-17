from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from services.pipeline.models import AppContext, PipelineResult
from services.vocabulary.aliases import ApprovedAlias, starts_with_canonical_name, substitute_aliases


ASR_PROMPT_LIMIT = 2000
FORMATTER_ALIAS_HINT_LIMIT = 20


class PipelineOrchestrator:
    def __init__(
        self,
        *,
        vad: Any,
        transcriber: Any,
        formatter: Any,
        context_provider: Callable[[], AppContext],
        injector: Any,
        snippets: Any | None = None,
        vocabulary_store: Any | None = None,
    ) -> None:
        self.vad = vad
        self.transcriber = transcriber
        self.formatter = formatter
        self.context_provider = context_provider
        self.injector = injector
        self.snippets = snippets
        self.vocabulary_store = vocabulary_store

    def process_audio(
        self,
        audio: Any,
        *,
        language: str | None = None,
        inject: bool = False,
        vocabulary_hints: list[str] | None = None,
    ) -> PipelineResult | None:
        if inject:
            raise RuntimeError("Automatic insertion is disabled until M2")
        started = time.perf_counter()
        phase_started = time.perf_counter()
        app_context = self.context_provider()
        context_ms = self._elapsed_ms(phase_started)

        phase_started = time.perf_counter()
        filtered_audio = self.vad.filter(audio)
        vad_ms = self._elapsed_ms(phase_started)
        if filtered_audio is None:
            return None

        phase_started = time.perf_counter()
        list_aliases = getattr(self.vocabulary_store, "list_aliases", None)
        aliases = list_aliases() if callable(list_aliases) else []
        canonical_names = list(dict.fromkeys(alias["canonical"] for alias in aliases))
        formatter_hints = self._formatter_hints(vocabulary_hints, canonical_names)
        asr_result = self.transcriber.transcribe(
            filtered_audio,
            language=language,
            initial_prompt=self._asr_initial_prompt(vocabulary_hints, canonical_names),
        )
        asr_ms = self._elapsed_ms(phase_started)
        if not asr_result.text.strip():
            return None

        phase_started = time.perf_counter()
        polished_text, preserve_initial_case = self._expand_or_format(
            asr_result.text,
            app_context,
            vocabulary_hints=formatter_hints,
            aliases=aliases,
            canonical_names=canonical_names,
        )
        format_ms = self._elapsed_ms(phase_started)

        phase_started = time.perf_counter()
        # M1: processing is pure; insertion requires the future M2 transaction.
        del inject
        inject_ms = self._elapsed_ms(phase_started)
        total_ms = self._elapsed_ms(started)

        diagnostics = self._diagnostics()
        if preserve_initial_case:
            diagnostics["preserve_initial_case"] = True
        return PipelineResult(
            raw_transcript=asr_result.text,
            polished_text=polished_text,
            app_context=app_context,
            duration_ms=total_ms,
            timings_ms={
                "context_ms": context_ms,
                "vad_ms": vad_ms,
                "asr_ms": asr_ms,
                "format_ms": format_ms,
                "inject_ms": inject_ms,
                "total_ms": total_ms,
            },
            diagnostics=diagnostics,
        )

    def process_command(
        self,
        selected_text: str,
        audio: Any,
        *,
        language: str | None = None,
        inject: bool = False,
    ) -> PipelineResult | None:
        if inject:
            raise RuntimeError("Automatic insertion is disabled until M2")
        started = time.perf_counter()
        phase_started = time.perf_counter()
        app_context = self.context_provider()
        context_ms = self._elapsed_ms(phase_started)

        phase_started = time.perf_counter()
        filtered_audio = self.vad.filter(audio)
        vad_ms = self._elapsed_ms(phase_started)
        if filtered_audio is None:
            return None

        phase_started = time.perf_counter()
        asr_result = self.transcriber.transcribe(
            filtered_audio,
            language=language,
            initial_prompt=self._asr_initial_prompt(None),
        )
        asr_ms = self._elapsed_ms(phase_started)
        if not asr_result.text.strip():
            return None

        phase_started = time.perf_counter()
        edit_result = self.formatter.command_edit(selected_text, asr_result.text)
        format_ms = self._elapsed_ms(phase_started)

        phase_started = time.perf_counter()
        del inject
        inject_ms = self._elapsed_ms(phase_started)
        total_ms = self._elapsed_ms(started)

        return PipelineResult(
            raw_transcript=asr_result.text,
            polished_text=edit_result.text,
            app_context=app_context,
            duration_ms=total_ms,
            timings_ms={
                "context_ms": context_ms,
                "vad_ms": vad_ms,
                "asr_ms": asr_ms,
                "format_ms": format_ms,
                "inject_ms": inject_ms,
                "total_ms": total_ms,
            },
            diagnostics=self._diagnostics(),
        )

    def _diagnostics(self) -> dict[str, Any]:
        diagnostics = {}
        vad_stats = getattr(self.vad, "last_stats", None)
        if isinstance(vad_stats, dict):
            diagnostics['vad'] = dict(vad_stats)
        editing = getattr(self.formatter, 'last_diagnostics', None)
        if isinstance(editing, dict):
            diagnostics['editing'] = dict(editing)
        return diagnostics

    def inject_result(self, result: PipelineResult) -> None:
        raise RuntimeError("Automatic insertion is disabled until M2")

    def _expand_or_format(
        self,
        raw_text: str,
        app_context: AppContext,
        *,
        vocabulary_hints: list[str] | None,
        aliases: list[ApprovedAlias],
        canonical_names: list[str],
    ) -> tuple[str, bool]:
        if self.snippets is not None:
            expansion = self.snippets.expand(raw_text)
            if expansion:
                return expansion, False
        format_result = self.formatter.format(
            raw_text,
            app_context,
            vocabulary_hints=vocabulary_hints,
        )
        if getattr(format_result, "model", None) == "raw-recovery":
            return raw_text, False
        polished_text = substitute_aliases(format_result.text, aliases)
        return polished_text, starts_with_canonical_name(polished_text, canonical_names)

    def _formatter_hints(
        self, explicit_hints: list[str] | None, canonical_names: list[str]
    ) -> list[str] | None:
        hints = _dedupe_non_empty(canonical_names)[:FORMATTER_ALIAS_HINT_LIMIT]
        formatter_hints = getattr(self.vocabulary_store, "formatter_hints", None)
        if callable(formatter_hints):
            hints.extend(str(hint) for hint in formatter_hints() if str(hint).strip())
        if explicit_hints:
            hints.extend(explicit_hints)
        return _dedupe_non_empty(hints) or None

    def _asr_initial_prompt(
        self, explicit_hints: list[str] | None, canonical_names: list[str] | None = None
    ) -> str | None:
        hints: list[str] = []
        asr_hints = getattr(self.vocabulary_store, "asr_hints", None)
        asr_terms = getattr(self.vocabulary_store, "asr_terms", None)
        if callable(asr_terms):
            hints.extend(asr_terms(canonical_names=canonical_names))
        elif callable(asr_hints):
            store_hints = str(asr_hints()).strip()
            if store_hints:
                hints.extend(part.strip() for part in store_hints.split(","))
        if explicit_hints:
            hints.extend(explicit_hints)
        bounded: list[str] = []
        used = 0
        for hint in _dedupe_non_empty(hints):
            cost = len(hint) + (2 if bounded else 0)
            if used + cost <= ASR_PROMPT_LIMIT:
                bounded.append(hint)
                used += cost
        return ", ".join(bounded) if bounded else None

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)


def _dedupe_non_empty(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result
