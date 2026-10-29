# Wezza replacement-readiness audit

Date: 29 October 2026. Repository baseline: `484daed`, plus the user's existing uncommitted edits in `services/llm/cleanup.py` and `tests/test_llm_data.py`. Those edits were inspected and preserved. This investigation changes documentation only.

## Product requirement

The user wants smooth personal dictation that repairs stutters, accidental words, spoken take-backs, and grammar. English, French, and Arabic must work without switching settings, including switches within a sentence. Audio, context, recognition, and formatting must remain on this PC, with low power use. Follow-up clarification: Egyptian Arabic. Arabic-starting sentences preserve Arabic script for Arabic spans and Latin for English/French spans. English/French-starting sentences stay Latin, with Arabic spans rendered as Arabizi with numbers. These are target requirements; the original baseline does not implement them.

The existing application is a useful scaffold, but its defaults and tests do not establish these capabilities. The recommendation is a selective rebuild of orchestration and language processing, preserving the desktop shell, reusable adapters, and stores. See the [implementation and validation plan](superpowers/plans/2026-10-29-wezza-local-dictation.md).

## Evidence labels and boundary

- **Reproduced:** a bounded local probe demonstrated the behavior, with fake inputs/adapters and no microphone, clipboard, or keyboard interaction.
- **Static:** the actual source contains the described control/data flow. Frequency and user-visible impact have not been measured live.
- **Historical:** retained benchmark/report evidence from earlier work, not a fresh performance measurement.
- **Proposed:** a design or acceptance threshold, not an achieved capability.

Three GPT 6.1 Sol investigations covered runtime/insertion, recognition/formatting, and Wispr evidence. The orchestrator read central paths, independently reproduced the cleanup, VAD, scoring, lazy-ASR-lock, and clipboard findings, and ran the baseline checks below. No Terra investigation was used. No real dictation, model download, model inference, private Wispr history inspection, or live paste was performed.

## Current execution path

`src-tauri/src/lib.rs` owns desktop hotkeys and launches a Python HTTP service. Press and release independently call `/runtime/start` and `/runtime/stop`. `services/runtime/api.py` owns a mutable session; `services/runtime/session.py` captures all audio and invokes `services/pipeline/orchestrator.py`. The pipeline synchronously captures app context, applies amplitude-based filtering, calls faster-whisper, applies deterministic cleanup or optional Ollama, and immediately pastes through the clipboard. The optional speculative worker runs a snapshot through a second pipeline. React provides settings, vocabulary, snippets, and diagnostics.

The CLI has a separate hotkey path. Settings defaults, saved settings, profile metadata, and React's profile table are separate sources of configuration. Tests mostly exercise fakes or source-string assertions; the desktop-to-recognition-to-target-text path lacks an acceptance harness.

## Priority findings

P0 means prerequisite for trustworthy evaluation or safe integration. P1 means required for the requested replacement experience. P2 means subsequent improvement; it does not imply the issue is harmless.

| ID | Priority / evidence | Finding and consequence | Source |
|---|---|---|---|
| F01 | P0, static | Independent desktop start/stop tasks can reorder; the API has no serialized lifecycle or session identifier. A quick release during initialization can precede start. | `src-tauri/src/lib.rs:828-879`; `services/runtime/api.py:124-147,231-250` |
| F02 | P0, static + agent fake reproduction | Cancel is rejected during processing and the pipeline pastes before returning. Settings/status can discard a processing session while its old job continues. Desktop has no processing-cancel binding. | `services/runtime/session.py:125-160,187-213`; `services/runtime/api.py:74-90,170-175`; `services/pipeline/orchestrator.py:68,121` |
| F03 | P0, static | No intended-window/control/selection identity is captured and checked at insertion. Context is read after release; changing focus during inference can redirect the paste or command replacement. | `services/pipeline/orchestrator.py:40,98,148`; `services/runtime/session.py:63`; `services/injection/clipboard.py:35-43` |
| F04 | P0, reproduced | Clipboard restoration overwrites a newer user copy: fake writes were `dictation`, `new user copy`, `old`. Only text is preserved; non-text formats and delayed paste consumption are not protected. | `services/injection/clipboard.py:35-43`; `services/injection/selection_reader.py:26-39` |
| F05 | P1, static + model documentation | Desktop start/stop hard-code English. Low-impact/snappy/balanced use `small.en`. The ostensibly unrestricted quality profile uses `distil-large-v3`, from an English-only family. There is no valid demonstrated trilingual default. | `src-tauri/src/lib.rs:846,868`; `services/asr/profiles.py:43-74`; `src/App.tsx:91-107`; [upstream Distil-Whisper model card](https://huggingface.co/distil-whisper/distil-large-v3) |
| F06 | P1, reproduced | Cleanup changes `print("hello.world")` to `print("hello. world")`, `version 1.2.3` to `version 1. 2. 3`, and `we need one twenty dollar bill` to `we need 1:20 dollar bill`. Literal reset-marker phrases lose their prefix. Code category still runs destructive rules. | `services/llm/cleanup.py:88-105,114-173,214-222` |
| F07 | P1, static | Default formatting is English regex cleanup, not multilingual grammatical editing. Optional Ollama receives a broad polish prompt, accepts empty/arbitrary output, and has a ten-second request timeout without preserving raw output on formatter failure. | `services/llm/formatter.py:25-49,102-129`; `services/pipeline/orchestrator.py:59-68` |
| F08 | P0, reproduced | ASCII-only WER scores entirely different Arabic strings as WER 0 / accuracy 1. It also hides punctuation/case/identifier corruption. Historical model comparisons have four unique audio samples, not a representative corpus. | `services/asr/benchmark.py:22,143-175`; `artifacts/benchmarks/asr_voice_model_compare.jsonl`; `docs/asr-model-thread-test-report.md` |
| F09 | P1, reproduced/static | A silent second containing one above-threshold spike passes VAD. Minimum speech duration does not work as its name implies. Audio callback drops status/overflow information. Benchmark WAV loading accepts non-16-kHz samples without resampling them for the transcriber. | `services/asr/vad.py:35-39`; `services/asr/audio_capture.py:70-72`; `services/asr/benchmark.py:430-454` |
| F10 | P0, reproduced | The transcriber releases its lock before consuming lazy segments. A generator observed the lock unheld during inference. `AsrResult.duration_ms` inconsistently means audio duration or processing duration. Pipeline/benchmark outer timers do include consumption; do not discard those timings solely because the inner field is wrong. | `services/asr/transcriber.py:57-68,86-107` |
| F11 | P1, static | Factory forces CPU; current profiles do not select device. Warmup loads the model but does not run inference. Speculative full-pipeline workers can continue after cancellation/discard and compete for resources. | `services/pipeline/factory.py:61-67`; `services/asr/transcriber.py:33-35`; `services/runtime/speculative.py:70-103,137`; `services/runtime/session.py:136,157` |
| F12 | P1, static | Quality fallback runs only when the pipeline returns no-speech, not when recognition is inaccurate. It can retry rejected noise, while wrong nonempty text succeeds. Empty ASR output is not a distinct failure state. | `services/runtime/session.py:143-150`; `services/pipeline/orchestrator.py:46-74` |
| F13 | P1, static / latency hypothesis | UIA output is size-bounded, but traversal lacks a wall-time/node budget. Browser descendants are enumerated before slicing. Context lacks intended field/caret identity and useful locally extracted terminology. Actual UIA latency remains unmeasured. | `services/context/app_context.py:18-45,75-90,107-151`; `services/pipeline/models.py:11-17` |
| F14 | P2, static | Learning watcher treats continued typing in the same app/title as a possible correction without reading the changed span. Global correction substitutions and top-20 hints are not app/language scoped. Snippet trigger punctuation is fragile. | `services/runtime/correction_watcher.py:70-91,124-130`; `services/vocabulary/store.py:155-173`; `services/llm/cleanup.py:176-195`; `services/snippets/engine.py:25-34` |
| F15 | P1, static + agent fake reproduction | Capture startup can leave recording state active after failure; some factories escape recovery. Any binding release can stop another binding's session. Adopted runtime ownership, startup-timeout cleanup, and numeric-PID termination are unreliable. | `services/asr/audio_capture.py:40-46`; `services/runtime/session.py:97-115`; `src-tauri/src/lib.rs:421-425,972-1027,1105-1125` |
| F16 | P1/P2, static | Runtime depends on finding a source checkout and `.venv`; installer metadata alone does not package a working Python/model runtime. Python dependencies are unpinned. History settings/schema do not constitute an implemented recoverable-result/retention contract; JSONL logging appends without rotation. | `src-tauri/src/lib.rs:1031-1078,1219-1225`; `src-tauri/tauri.conf.json`; `requirements.txt`; `services/config/settings.py:43-46`; `db/schema.sql`; `services/runtime/logging.py:15-25` |
| F17 | P1, static | The pipeline factory loads defaults rather than the root's saved settings, and the default recorder factory has fixed constructor defaults. Several saved audio/VAD/model/injection controls therefore do not determine actual execution. A runtime-profile override does not fix all configuration paths. | `services/pipeline/factory.py:41-43,70-76`; `services/runtime/runner.py:148-176`; `services/runtime/api.py:435-442` |

## What to preserve, improve, replace

| Decision | Components | Reason |
|---|---|---|
| Preserve | Tauri/React shell, tray and existing desktop hotkey adapters | No evidence warrants an Electron or full Rust rewrite. Their coordination needs replacement, not their existence. |
| Preserve with contract changes | Recorder/ASR/formatter injection points, JSONL telemetry concept, SQLite vocabulary and snippets | Useful seams for deterministic tests and migration. Keep user's data and settings. |
| Preserve as comparison only | Existing CPU English profiles and recorded benchmark artifacts | Useful historical baseline; unsuitable as the trilingual product default. |
| Improve | Capture buffering, configuration validation, local context, clipboard adapter, deployment scripts, retention | Concrete gaps can be addressed behind explicit contracts. |
| Replace | Shared mutable lifecycle, direct pipeline paste, uncancellable speculative scheduling | These undermine reliable interaction regardless of ASR quality. |
| Replace | Destructive generic cleanup as the primary editor; ASCII-only evaluation | Neither can establish multilingual meaning preservation or grammatical quality. |
| Evaluate | Multilingual ASR candidates and quantized local formatter; CPU/GPU execution | Select by user's speech and measured quality/latency/energy, not labels. |
| Defer | Meetings/calendar, broad command editing, macOS parity, automatic learning, screenshots/OCR by default | They do not resolve the user's core dictation requirement. |

## Wispr Flow report evaluation

Primary report: `C:/Users/adami/projects/Reverse-engineer/investigations/wisprflow/REPORT.md`. Inspected version: Windows 1.6.1297. The extracted main JS was independently hashed as `0c6bf0a225ac450a324e8b7c49302c2b1d1e99452bf38825dfa883540dc79ea3`, matching the report. Companion excerpts and the extracted source corroborate the following. Offsets are approximate zero-based Unicode character positions in `.webpack/main/index.js`, unless stated otherwise.

| Claim | Evidence and confidence | Local design lesson |
|---|---|---|
| Session-scoped audio and stale fallback rejection | Static main ~4431131 and ~3866870; report section 1, C01-C06 | Bind every result, error, context update, and insertion to a session/generation. |
| Explicit stop/drain/finalize | Hub 790244-791844 and 873100-874150; report section 2 | Capture drain is a barrier; do not assume releasing a key means every sample has arrived. Local PCM does not need cloud Opus transport. |
| Init/audio/context/commit protocol | Static main 3858074-3859750; report sections 4-5 | Local workers need an explicit final-audio boundary. Streaming remains a measured design option. |
| Field/caret context and vocabulary extraction | Static main 3878450-3879450, 3636860-3637200; report section 3 | Collect bounded relevant context locally; nearby words can help recognition. Server use and active flags are unknown. |
| Correlated paste plus observed-text verification | Static main 4651320-4652230, 4483700-4485240, 4583600-4584850; report section 6 | Treat insertion as a transaction with observable outcomes. Native call sequence and clipboard restoration remain unknown. |
| Raw/degraded result path | Static main 3725516-3727716 and API pipeline excerpts | Preserve raw recognition when formatting fails. This does not prove every Wispr formatting failure recovers successfully. |
| Edit-derived vocabulary learning | Static main 4005400-4006800; report section 6 | Correlate verified insertion and actual edit differences; no evidence of local acoustic fine-tuning. |
| Separate privacy/storage/upload controls | Static no-share serializer 4112750-4113800; report section 7 | Local inference, content retention, and learning are separate controls. Wispr privacy mode does not imply offline inference. |

One caution from source review: the proper-noun extraction path computes `raceLost`, but the shown mutation checks current UUID truthiness rather than equality with the captured UUID. The report demonstrates several stale-result defenses, not universal race freedom.

REA coverage was explicitly partial; native helper internals, active remote flags, production server models/prompts, deployed accuracy, latency, and energy remain unknown. No live dictation was observed. The older repository `how-wispr-flow-works.md` makes unsupported model/provider/latency/privacy claims and must not guide architecture as fact. The new report is the stronger evidence source, with its limits intact.

## Machine and validation baseline

Observed on this machine: AMD Ryzen 9 PRO 8945HS, 8 cores / 16 logical processors, approximately 62.8 GiB physical RAM; NVIDIA GeForce RTX 5060 Ti with 16,311 MiB reported VRAM and driver 617.42. The AMD integrated GPU is also present. Project Python 3.14.3, faster-whisper 1.2.1, CTranslate2 4.7.1, NumPy 2.4.4, sounddevice 0.5.5, FastAPI 0.136.1; Node 24.14.1; Cargo 1.95.0.

CTranslate2 sees one CUDA device and advertises float16/int8-related compute types. This establishes device discovery, not successful model inference or CUDA/cuDNN deployment. [faster-whisper's GPU requirements](https://github.com/SYSTRAN/faster-whisper#gpu) and [CTranslate2 hardware support](https://opennmt.net/CTranslate2/hardware_support.html) are prerequisites to check against the pinned installed versions. No power, heat, fan, or inference-speed claims follow from GPU presence.

| Check executed | Result |
|---|---|
| `.venv/Scripts/python.exe -m unittest discover -s tests -v` | 193 tests; 191 passed, 2 failed. Both status tests expect `low-impact` but read the checked-in saved `snappy` settings; saved quality fallback also differs from defaults. |
| The two failing tests under an in-memory patch returning default settings | 2 passed. Confirms fixture/config coupling; does not fix the suite. User settings were not changed. |
| `npm test` | 25 tests in 3 files passed. |
| `npm run build` | TypeScript/Vite production build passed. |
| `cargo check --locked --offline --manifest-path src-tauri/Cargo.toml` | Passed. |
| `cargo test --locked --offline --manifest-path src-tauri/Cargo.toml --lib` | Failed before tests: old cached Tauri permission path under `C:/Users/adami/projects/WEZZA`. |
| Same Rust test command with `--target-dir artifacts/audit-2026-10-29/cargo-target` | 11 tests passed in a fresh build directory. Confirms stale build-output involvement; no production source change was needed. |

Baseline tests do not verify real microphones, first/last syllable retention, Unicode/RTL insertion, real-app clipboard timing, cold/warm model behavior, code switching, grammar repair, power use, or Wispr parity. These remain explicit gates in the plan.

## Investigation checklist

| Question | Status |
|---|---|
| Repository architecture and actual core path | Answered statically, key defects reproduced with fakes |
| Report credibility and transferable techniques | Answered within retained static evidence; server/native/runtime limits preserved |
| Current build and unit-test health | Measured above; Python fixture defects remain |
| Local machine and CUDA discovery | Measured; model compatibility/inference untested |
| Actual trilingual quality, grammar, smoothness, energy | Unresolved until corpus and live trials; cannot be inferred from tests |
| Prioritized implementation and validation | Defined in companion plan; no implementation claimed |

## Follow-up: native helper diagnostic evidence

A bounded read-only REA follow-up inspected the same Windows 1.6.1297 helper, SHA256 `0846b760e72630e313a005be8410295adca45ff0aa365d3ff1d47a592a34387f` (20,704,120 bytes). Native decompilation providers were unavailable: Ghidra and IDA were unconfigured and Hopper was unsupported on this Windows host. No runtime attachment, process memory, private history, keyboard or clipboard operation was used.

An ordinary reproducible UTF-16LE byte scan confirms that shipped diagnostics describe these mechanisms:

| Artifact evidence (decimal file offsets) | Design implication | What it does not prove |
|---|---|---|
| Clipboard change suppresses restoration (18459116); previous snapshot discarded after a change (18459244); earlier paste restore superseded (18688340) | Track clipboard ownership and paste generation before restoration | Exact native sequencing, preserved formats or atomic ownership |
| Delayed clipboard consumption (18459532); consumption timeout (18459396); delivered shortcut suppresses a duplicate fallback (18642740) | Model uncertain delivery and never automatically retry a possibly delivered paste | Timeout reliability or actual application compatibility |
| Dictation-start focused element saved (18675156); focus-change failure (18838588); conditional refocus (18418380) | Capture the intended target at dictation start and correlate focus changes | An atomic target-bound write or universal mismatch rejection |
| TextPattern readback falls back to ValuePattern (18717884); oversized contents skip verification (18471772); edit listener superseded by a newer paste (18498988) | Bound readback, keep verification outcomes distinct, correlate observations to the paste | ValuePattern-based insertion, exact-range proof or coverage in Codex/Claude/Comet |

These are confirmed diagnostic **contents**, not demonstrated execution or reconstructed control flow. They strengthen the rationale for Wezza's session/transaction/clipboard design without establishing a race-free native writer. The existing JavaScript paste-ID and before/after verification findings remain the stronger control-flow evidence. The exact scan, strings and limitations are retained locally under ignored `artifacts/m2-native-evidence-2026-10-29/`. Implementation progress and later test results are recorded separately in `docs/implementation-progress.md`; the original audit baseline above remains historical.
