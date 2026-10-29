# Wezza Local Trilingual Dictation Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to execute individual implementation slices. The parent orchestrator owns interfaces, integration, acceptance, and final verification. This is an evidence-based program plan; model choices are deliberately gated on measurements.

**Goal:** Replace Wispr Flow for this user's English/French/Arabic dictation, including within-sentence language switches, stutters, spoken revisions, and grammatical correction, entirely on this PC with low power use.

**Architecture:** Retain the Tauri/React shell and Python engine. Introduce one serialized session controller, bounded inference workers, multilingual ASR, a constrained local language editor, and a separate guarded insertion transaction. Select models and warm-retention policies using quality, latency, and energy on the user's speech.

**Tech stack:** Existing Rust/Tauri, TypeScript/React, Python, faster-whisper/CTranslate2, sounddevice, Windows accessibility/clipboard APIs, SQLite; local Ollama is an existing formatter adapter to evaluate, not a mandated winning backend.

**Evidence:** [Repository and Wispr audit](../../wezza-audit-2026-10-29.md), baseline `484daed` plus preserved user edits. Date: 29 October 2026.

---

## 1. Scope and architectural decision

The user clarified that fluent transcription and language editing are the main problem. Trilingual support and within-sentence code switching are core acceptance criteria, not a later localization task. “Local” includes all inference and low resource/power impact. Do not quietly substitute cloud calls when local processing is slow or unavailable.

Three approaches were considered:

| Approach | Benefit | Limitation | Decision |
|---|---|---|---|
| Patch current CPU/English pipeline and add more regex | Small initial diff | Cannot satisfy multilingual grammar; preserves lifecycle/insertion defects | Reject as target architecture; use only narrow safe fixes |
| Retain shell/adapters, rebuild coordination, evaluate local multilingual ASR + editor | Reuses working UI/stores while addressing measured gaps; permits staged acceptance | Requires contract changes and real speech evaluation | Recommended |
| Full native rewrite plus a new streaming recognition stack | Could simplify packaging and later optimize resource use | Large unmeasured rewrite; language quality still depends on models | Defer unless profiling demonstrates an unavoidable boundary cost |

Do not equate `language=None` with successful code switching. Do not assume English-only Distil-Whisper becomes multilingual because the profile omits a language restriction. Preserve the CPU English path for comparison/recovery, but it cannot pass the user's target gate.

The user confirmed their regular destinations: Codex, Claude, and Comet browser, especially browser LLM chats, Google Docs, Google Sheets, and email. Other browsers are occasional. Prioritize those actual composers and editors; Notepad and synthetic textarea/contenteditable fixtures are diagnostic controls, not substitutes for real-app acceptance. Installed Claude desktop was observed, but exact editor capabilities remain unverified. Browser extension support for ordinary web fields does not establish support for desktop apps, Comet's internal assistant, or Docs/Sheets canvas editors. Do not trigger send/submit when inserting. General command editing, meetings, calendar, mobile/macOS parity, and screenshot capture are outside this program's first release.

## 2. Target contracts

### Session ownership and control

The shell sends ordered commands containing `session_id`, monotonic `command_seq`, initiating `binding_id`, and mode. The controller accepts or rejects them explicitly. Stop/cancel for a session that is still starting is retained, so release can never leave delayed capture active. Duplicate commands are idempotent. Only the accepted initiating binding owns release; Escape cancels the active session independently.

Use one controller with transitions `idle -> starting -> recording -> draining -> processing -> ready -> committing -> terminal`. Terminal outcomes include inserted, unverified, recoverable, no-speech, cancelled, and error; they are not collapsed into success. Heavy inference runs outside the control loop, so cancellation and status remain responsive. Worker results include session/generation and are discarded when stale. Settings reset waits for a safe terminal point; a session holds an immutable settings snapshot.

An accepted cancel before `committing` guarantees no insertion. The commit transition is atomic with cancellation checking. Cancellation arriving after OS injection has begun reports “too late”/actual outcome; do not claim it can retract text already delivered. Never retry an uncertain insertion automatically.

### Capture and recognition

Capture uses typed mono float32 buffers with an explicit sample rate, sample count, monotonic timestamps, overflow/device-error diagnostics, and an explicit stopped/drained signal. Avoid the current Python-list -> PCM WAV -> Python-list round trip on the live hot path unless measurement justifies retaining it. Keep WAV import/export at adapters. Cap a session initially at five minutes; show a warning before stopping and preserve the completed utterance. A disconnect or overflow yields a visible degraded/recoverable outcome, not silent success.

The ASR adapter returns raw text, segments/timestamps where available, language diagnostics, audio duration, inference duration, backend/model revision, and a typed outcome. Do not manufacture calibrated confidence from an arbitrary score. It must transcribe, not translate. Auto behavior must preserve English, French, Arabic, and mixed spans. Full-utterance recognition is the first baseline; chunking or streaming is introduced only when long-utterance latency requires it and boundary tests pass.

Use one bounded ASR worker and one bounded formatting worker initially, with at most one active session. Cancellation invalidates output immediately; if a library cannot interrupt native inference, track that worker until it completes and apply backpressure rather than spawning another. Keep speculative duplicate pipelines disabled until a separate measured benefit passes the same energy and correctness gates.

### Local language editing

The formatter receives the complete raw utterance, language-preserving instructions, relevant bounded context, and vocabulary. Its job is to remove accidental repetitions/stutters, resolve explicit spoken take-backs (including multiword replacements), and correct grammar while retaining intent, register, dialect, facts, names, numbers, and negation. Preserve deliberate emphasis, quotations, code, URLs, and the speaker's language choices. The user confirmed Egyptian Arabic. Anchor script to the first spoken language of each sentence before grammar rewriting: Arabic-starting sentences use Arabic script for Arabic spans and Latin for English/French spans; English/French-starting sentences use Latin throughout, transliterating Egyptian Arabic with Arabizi numbers (for example 3ayez and 7aga). This is transliteration, never translation. Preserve that anchor through grammar/revision edits and do not rewrite Egyptian dialect into formal Arabic. User-reviewed alternatives handle legitimate spelling variation.

Keep raw text and edited text separate. Validate structured output, emptiness, unexpected commentary, protected entities/literals, abnormal expansion/deletion, and unwanted translation. A validation failure or timeout returns a conservative raw result with a clear degraded state. This is a reliability fallback, not a success toward the grammar target. Heuristic validators cannot prove semantic equivalence: human-reviewed multilingual fixtures remain required. Treat visible text as context data, never as instructions to the formatter.

Start with one small quantized multilingual formatter candidate supported by the chosen local runtime and the existing configured model only if its local weights/capabilities are verified. Pin model revision, quantization, prompt, and generation settings in every result. Evaluate ASR/formatter combinations; a formatter must not conceal unreliable ASR by inventing missing content. Prefer deterministic cleanup only for tested safe normalization and literal protection.

### Context and insertion

Capture a lightweight intended target identity at press, before UI overlays can change focus. Enrich it asynchronously with field selection and bounded before/after text. Initial enrichment budget: 100 ms, 128 visited nodes, 2,000 characters total; timeout means proceed without enrichment. Blocking native UIA calls need a supervised worker/process, because a Python deadline alone cannot interrupt COM. Preserve target identity even when enrichment fails. Skip password fields and allow context capture to be disabled.

Selection provenance must refer to the recording-start target. Capture an initial comparable selection/caret snapshot or target generation before asynchronous enrichment; if unavailable, mark it unknown. A later selection read is not proof of the original range. Same-control edits and caret moves invalidate replacement eligibility.

The insertion adapter alone produces OS side effects. Immediately before commit, revalidate foreground process/window/control and selected range where available. Changed or ambiguous targets retain output for explicit user recovery. Prefer target-addressed insertion where supported. Foreground validation and synthetic Ctrl+V are not atomic: test focus loss between them. An adapter that cannot bind the write to the validated target must expose that limitation and use explicit recovery/manual paste rather than claim the zero-wrong-target guarantee. Verify observed before/inserted/after text where the control exposes it. Unsupported observation returns `unverified`, not `verified`. Protect clipboard formats and sequence ownership; never restore over a newer user copy. Delayed paste consumption and uncertain verification need bounded completion rules; never create duplicate pastes while guessing whether the first worked.

Maintain an in-memory recoverable result for the latest session from the first lifecycle slice. Offer an explicit recovery action. Optional persisted history comes later with retention controls; audio retention stays off by default. No transcript, nearby text, or full URLs in ordinary diagnostic logs.

### Power and offline operation

Compare CPU int8 and supported GPU quantization/precision combinations; fastest is not automatically lowest energy. Do not keep heavy inference looping at idle. Evaluate short warm-retention periods against immediate unload using realistic bursts and gaps; use the lowest-energy policy that passes latency and quality. Measure shared ASR/formatter memory pressure, cold starts, and foreground responsiveness. No automatic broad system power-setting changes.

After explicitly provisioning model files, the complete pipeline must work with outbound networking blocked. Missing models produce a local readiness error, not an implicit download. Loopback transport must have an explicit application identity/version and per-run authorization; lifecycle control belongs to the launched/adopted engine instance. Downloads/updates are separate user-visible maintenance operations.

## 3. Acceptance criteria established before major changes

These are proposed engineering gates, not current measured capability. The initial dataset and power measurements can expose infeasible targets; report such conflicts and seek a product decision rather than silently weakening gates.

| Dimension | Replacement gate | Method |
|---|---|---|
| Meaning | Zero introduced fact/name/number/negation changes in critical fixtures; no lost final clause or minority-language span | Human-authored critical references plus automated entity/literal assertions |
| Editing | At least 95% correct explicit take-back resolution; at least 90% appropriate grammar repairs in curated cases, without unwanted tone/dialect changes | Separate stutter/revision/grammar labels, reviewed by the user |
| Overall usability | At least 90% accepted unchanged overall AND within EN, FR, AR, and mixed groups; no more than 2 extra correction characters per 100 versus matched Wispr samples if collected | Local blind output review and daily-use correction log; do not average away weak Arabic/mixed results |
| Language switching | No settings change between or within utterances; zero unwanted whole-span translation in critical switch fixtures; at least 95% exact critical names/numbers across groups | Paired/trilingual examples and switch-region scoring; report WER/CER separately |
| Short utterance latency | Warm release-to-visible-text p50 <=1 s, p95 <=2 s for 2-15 s utterances; formatting included | At least 30 real pipeline runs per major condition; failures remain in denominator |
| Long utterance latency | Warm release-to-visible-text p95 <=3 s for 15-60 s speech and <=5 s for 60-120 s speech | Separate duration buckets; no missing tails or artificial end-silence |
| Extended utterances | Up to five minutes: complete tail/meaning retention and release-to-visible-text p95 <=10 s; no unbounded buffering or formatter truncation | Dedicated 2-5-minute speech cases, including corrections and language switches near the end |
| Feedback/cancellation | Press-to-visible-recording acknowledgment p95 <=150 ms; cancel acknowledgment p95 <=150 ms; accepted pre-commit cancellation never pastes | Deterministic control tests plus live timing |
| Cold start | Explicit initializing state; capture does not lose words while a model warms; report time to ready and first-result latency separately | Cold process/model runs, including cache-ready offline startup |
| Insertion | Zero wrong-target/duplicate insertions; >=99.5% successful supported-field insertions across 200 trials; all failures recoverable | Controlled target fixture plus manual real-app matrix; report unverified results separately |
| Lifecycle | 1,000 seeded fake event interleavings with no stuck recorder, orphan commit, overlapping owned sessions, or worker growth | Barrier-driven tests, no timing sleeps as correctness assertions |
| Silence | Zero inserted text in 30 silence/click/keyboard/noise fixtures; quiet first/last syllables preserved | Audio corpus plus live microphone trials |
| Idle power | Whole-app mean CPU <=1% of total machine capacity over 10 minutes; no sustained GPU inference; incremental system idle power target <=2 W over comparable baseline | CPU normalized explicitly; matched idle runs, power telemetry or external meter |
| Active efficiency | Provisional whole-system incremental mean <=10 W on a ten-minute burst/gap workload; choose lowest joules per dictated minute within that budget and quality/latency gates; foreground typing p95 delay regression <=20 ms, no >100 ms visible stalls | Ten 15-second utterances with 45-second gaps, repeated three times against matched no-Wezza workload; include model loading, formatting, retention and unload. GPU board power alone does not establish this gate |
| Offline | Zero outbound network attempts from engine/model workers during a representative session set; all results still work disconnected | Connection tracing plus outbound-denied controlled trial after provisioning |
| Daily replacement | Five working days / at least 100 real dictations, all three languages and mixed speech included, >=90% accepted unchanged, zero unrecoverable loss/wrong-target paste | User trial; final user judgment on smoothness, fan noise, and trust |

Power unavailable from trustworthy system sensors is an explicit measurement gap. CPU percentage and GPU utilization are not energy measurements. The idle and burst/gap targets require baseline subtraction and repeated matched conditions; if measurement uncertainty is too high, do not claim they passed. The active budget is provisional and must be confirmed against the user's heat/fan/battery tolerance in the first on-machine trial; choosing the best of several power-hungry candidates is insufficient.

### Corpus and scoring

Create 180 unique user utterances: 30 English, 30 French, 30 Arabic, 20 EN/FR, 20 EN/AR, 20 FR/AR, and 30 trilingual. Tag overlapping categories for short chat, long prose, jargon/names, numerals/currency/dates, grammar, stutters, take-backs, deliberate repetition, literal commands, noise, and quiet speech. Include at least 12 extended 2-5-minute examples across those groups, checking tail retention, correction scope, formatter length and memory limits. Add 30 no-speech/noise negatives. Collect across at least two recording sessions and the actual microphone. Record Egyptian Arabic and the confirmed sentence-anchor-v1 script policy with arabizi_numbers=true in metadata. Keep verbatim native-script ASR references separate from intended final Arabizi outputs so transliteration is scored in the appropriate stage.

Split 60 speech examples for development and 120 held out, stratified by language and feature; retain at least 20 EN, 20 FR, 20 AR, and 60 mixed held-out samples. All model selection, prompt tuning, thresholds, and context ablations use development data only. Freeze model revisions, prompts, settings and policies before the final held-out evaluation. If failures inform a change, disclose that iteration and collect a fresh holdout for an independent final claim. More examples are required if a small subgroup cannot support a credible claim. Store audio locally outside Git, with stable clip IDs/checksums and a versioned manifest.

Each sample has (1) verbatim spoken reference, (2) one or more acceptable intended insertion references, (3) protected entities/literals, (4) language spans, and (5) whether grammar/revision edits are expected. Raw WER must not penalize an intentional formatter correction; final-output scoring must not reward deleting difficult words. Use Unicode-aware WER and CER, with documented Arabic normalization and French apostrophe/accent handling. Preserve case/symbol-sensitive exact metrics alongside normalized recognition metrics.

Use a matched, non-sensitive Wispr comparison only when the user elects to collect one; it sends audio to Wispr and is not part of Wezza's local inference. Preserve its build/date/settings and the same source speech when practical. If no matched Wispr set exists, report absolute performance and user trial results, not measured parity.

Representative tests must include:

- English `Tuesday, sorry, Thursday`; French `mardi, pardon, jeudi`; user-authored Arabic and mixed-language revisions. Preserve correct multiword replacement scope.
- Number/negation revisions, quoted “scratch that”/“wait no”, deliberate repetitions, and literal punctuation words. Do not silently drop earlier valid clauses.
- French accents/apostrophes, Arabic dialect and punctuation, English identifiers inside Arabic, and a language switch every few words without a pause.
- `print("hello.world")`, `1.2.3`, decimal money, URLs, paths, project names, and Arabic/Latin bidirectional text inside a sentence.
- Focus switch during ASR, moved selection during formatting, duplicate release, release-before-start, cancellation at every barrier, clipboard change during paste, hotkey overlap, microphone disconnect, suspend/resume, model timeout/OOM, offline startup, and settings change during processing.

## 4. Prioritized work packages

Every package ends with parent-reviewed code, exact commands/output, and its acceptance evidence. Write regression tests before the change; verify they expose the original defect. Run targeted checks, then the shared integration suite. Keep each package working and reviewable; do not commit all components in one rewrite.

### M0 — Trustworthy baseline and evaluation infrastructure (P0)

**Owner:** Sol; narrow helpers may use the Terra contracts in section 5. **Dependencies:** none. **Findings:** F08, F10, baseline fixture failures.

**Modify:** `tests/test_runtime_api.py`, `tests/test_pipeline_server.py`, `services/asr/benchmark.py`, `services/asr/transcriber.py`, `services/pipeline/models.py`, `services/runtime/benchmark_pack.py`.

**Create:** `services/evaluation/scoring.py`, `services/evaluation/manifest.py`, `services/evaluation/runner.py`, `tests/test_evaluation_scoring.py`, `tests/test_evaluation_manifest.py`, `tests/fixtures/dictation/manifest.example.json`, `docs/dictation-validation.md`. Keep recorded corpus under ignored `artifacts/dictation-corpus/`.

- [x] Make status/API tests use temporary roots with explicit fixtures. Do not alter saved user settings to make tests pass.
- [x] Add failing Unicode, French accent, Arabic mismatch, exact-entity, empty-reference, and mixed-language score cases; introduce separate ASR and final-output scores.
- [x] Enforce 16 kHz mono input at the inference boundary. For the first benchmark repair reject unsupported rates explicitly; add measured resampling only in the later audio adapter.
- [x] Prove lazy segments execute under the model lock; separate audio/inference/queue timing fields. Preserve compatibility where existing callers need a migration.
- [x] Add paired reference manifests, configuration fingerprints, caller-supplied model revisions, failure counts, and explicit replay timing boundaries. Replay marks inference/power/warmth measurements unavailable; live inference metadata remains M3. Preserve historical benchmark files.
- [ ] Define the corpus and obtain the first 30 representative speech clips (10 EN/FR/AR with some code switching) before selecting models; finish the 180-sample set before final acceptance.

**Checks:** `.venv/Scripts/python.exe -m unittest discover -s tests -v`; `npm test`; `npm run build`; `cargo test --locked --offline --manifest-path src-tauri/Cargo.toml --lib` with a fresh `--target-dir` if moved-workspace cache paths recur. New scorer tests must demonstrate that incorrect Arabic cannot score perfect. Tests must leave config/data unchanged.

**Technical status (29 October 2026):** evaluation replay, safe corpus collection, audio/ASR contract repairs, and isolated fixtures are implemented; 228 Python tests pass. User recordings and reviewed references are still outstanding. **Exit:** reproducible baseline, honest scoring, and a usable recorded local speech manifest. No model winner or user-experience claim yet.

### M1 — Serialized sessions and side-effect-free processing (P0)

**Owner:** one Sol across Rust/Python; do not split coupled lifecycle work. **Dependencies:** M0 technical baseline; user corpus collection may continue in parallel because lifecycle correctness is independently testable. **Findings:** F01-F03, F10-F11, F15, F17.

**Implementation coordination update:** after defining the transport contract, the orchestrator assigned Python/React and Rust to two Sol implementers with disjoint file ownership. They deliver one integrated M1 slice, with parent-owned protocol decisions and independent HTTP-boundary, Rust, and frontend verification. No Terra tasks or independent completion claims at this boundary. The agreed interface is the authenticated `runtime_request(method, path, body)` Tauri bridge; responses carry `application_id=wezza`, `protocol_version=1`, and per-run `engine_id`. Status distinguishes the active `session_id` from `result_session_id` of retained recovery text.

**Modify:** `src-tauri/src/lib.rs`, `services/runtime/api.py`, `services/runtime/session.py`, `services/runtime/hotkeys.py`, `services/runtime/runner.py`, `services/pipeline/orchestrator.py`, `services/runtime/speculative.py`, `src/lib/api.ts`, `src/App.tsx`.

**Create:** `src-tauri/src/dictation_controller.rs`, `services/runtime/controller.py`, `services/runtime/contracts.py`, `services/runtime/workers.py`, `tests/test_runtime_controller.py`, `tests/test_runtime_concurrency.py`.

- [x] Introduce typed session commands/outcomes and an immutable validated settings snapshot. Wire saved audio/VAD/model/injection settings to actual adapters; define profile precedence explicitly and test non-default values. Use automatic multilingual intent in the new contract; never hard-code English in shell requests.
- [x] Establish engine-instance identity, authenticated/versioned handshake, owned/adopted process semantics, startup-timeout child cleanup, and graceful cancellation/shutdown. Reject old-engine responses after restart and validate PID identity before termination.
- [x] Move lifecycle state to the single controller; keep inference outside its command queue. Serialize accepted start/stop/reset operations and make duplicate commands idempotent.
- [x] Make processing return a result without inserting. Add commit eligibility/cancellation generations and in-memory recovery UI from this slice. Keep automatic OS insertion disabled until M2's target-bound adapter passes; M1 ends at ready/recoverable output.
- [x] Make recorder startup transactional; every injected factory/start/stop failure closes acquired resources exactly once. Bind release to accepted initiating binding.
- [x] Disable current speculation. Track all workers across cancellation/shutdown; reject additional work explicitly while a noninterruptible cancelled job still owns the worker.
- [x] Render states from acknowledged controller events. Show processing/recoverable/error states after release, instead of hiding the bar while work continues. Restore ducked audio on every terminal/error/shutdown path without overwriting a later user volume change.

**Checks:** barrier-controlled release-before-start, duplicate start/stop, concurrent first requests, settings+status during processing, cross-binding release, cancellation before commit, stale result/error/context arrival, injected capture failures, startup timeout with no surviving child, old-engine replies, and stale-PID identity mismatch. Run `.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_runtime*.py' -v`, Rust controller tests, and `npm test`.

**Technical status:** implemented and independently reviewed; 251 Python, 32 frontend and 27 Rust tests pass. The real Python HTTP startup/shutdown smoke passes without capture/inference. See [progress and evidence](../../implementation-progress.md). Physical microphone/hotkey timing, native shell lifecycle faults and Windows volume restoration remain manual qualification gaps. Automatic insertion remains disabled.

**Exit:** 1,000 seeded lifecycle schedules pass; one microphone owner; no direct pipeline insertion; no stale commit; cancellation and recovery demonstrable in the UI. Automated coverage passes; live qualification is not yet complete.

### M2 — Guarded Windows insertion and minimal target context (P0)

**Owner:** Sol. **Dependencies:** M1. **Findings:** F03-F04, F13.

**Modify:** `services/injection/clipboard.py`, `services/injection/selection_reader.py`, `services/context/app_context.py`, `services/pipeline/models.py`, `services/runtime/controller.py`, `src/App.tsx`.

**Create:** `services/injection/transaction.py`, `services/injection/windows_target.py`, `services/injection/windows_clipboard.py`, `tests/test_insertion_transaction.py`, `tests/test_clipboard_ownership.py`, `tests/fixtures/insertion/target.html`.

- [ ] Capture intended process/window/control and selection identity at recording start; revalidate before commit. Test caret movement before enrichment completes and edits inside the same control. Fail into recovery when provenance cannot safely be matched.
- [ ] Add paste transaction IDs, clipboard sequence ownership, supported-format preservation, and verified/unverified/failed outcome reporting.
- [ ] Build a local controlled target fixture that logs received text, focus and selection changes. Include a delayed paste consumer and Unicode/RTL fields.
- [ ] Add explicit copy/retry recovery; require current target selection for retry. Never auto-retry uncertain commits or submit the destination form/chat.

**Checks:** fake focus/selection/clipboard interleavings, including focus loss after preflight validation but before OS write; exact delivered text in the controlled fixture; manual Notepad/editor/browser/chat-compose matrix with 200 insertion trials. Run the full baseline suite after integration. Preflight checks alone do not prove the target-bound-write gate.

**Exit:** zero wrong-target/duplicate paste; concurrent user clipboard copies preserved; unavailable verification remains labeled; all failures leave recoverable output. Full OS clipboard/UIA behavior requires live testing, not only mocks.

**Adapter evidence to preserve during implementation:** Microsoft's [TextPattern documentation](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-textpattern-overview) describes a read-oriented API, not a general text insertion transaction. [EM_REPLACESEL](https://learn.microsoft.com/en-us/windows/win32/controls/em-replacesel) can address a native edit control, but [window handles can be recycled](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-iswindow). Neither a successful preflight check nor a timed-out [SendMessageTimeout](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw) establishes verified delivery. Therefore qualify support by actual control/adapter, retain uncertain results, and do not describe generic Ctrl+V as target-bound. Clipboard restoration must acquire the clipboard and recheck ownership before writing; [sequence numbers](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclipboardsequencenumber) detect changes but delayed rendering complicates observation. These sources constrain the adapter design; they do not establish that Wezza has passed the insertion gate.

### M3 — Multilingual ASR, capture quality, and power experiment (P1)

**Owner:** Sol. **Dependencies:** M0 for the offline experiment; integrate after M1/M2. **Findings:** F05, F09-F12.

**Modify:** `services/asr/transcriber.py`, `services/asr/profiles.py`, `services/asr/audio_capture.py`, `services/asr/vad.py`, `services/pipeline/factory.py`, `services/config/settings.py`, `src/App.tsx`, `services/evaluation/runner.py`.

**Create:** `services/asr/audio_buffer.py`, `services/asr/model_registry.py`, `services/evaluation/resources.py`, `tests/test_audio_contract.py`, `tests/test_model_registry.py`.

- [ ] Benchmark a small multilingual CPU candidate, a multilingual `large-v3-turbo` candidate on the available GPU, and a higher-quality multilingual comparator only if the first pair misses quality. Start with cached weights where available; pin exact source/revision/conversion. These are experiments, not defaults.
- [ ] Verify GPU model loading and inference, not only CUDA device discovery. Compare quantization choices under actual ASR+formatter memory pressure; gracefully report unsupported execution.
- [ ] Remove false multilingual profile metadata; serve model/profile capabilities from the backend so UI and runtime agree. Automatic EN/FR/AR behavior is the product default when the chosen model passes; optional overrides remain diagnostic tools.
- [ ] Preserve raw language spans and diagnostics where supported. Test dominant-language bias and switches inside one sentence; do not independently decode three languages and vote without evidence that the extra cost helps.
- [ ] Introduce typed buffers, sample-rate handling, overflow/device events, and conservative speech filtering. Compare a local speech detector against raw audio/energy baseline using quiet syllable and noise tests; do not blindly change the current boolean gate.
- [ ] Measure CPU/GPU energy, model residency, cold and genuine inference-warm latency. Compare unload and short retention policies. Select only configurations on the measured quality/latency/energy frontier.

**Checks:** audio contract/scorer suites; development-corpus raw WER/CER per language and switch region; silence/quiet/last-word tests; cold/warm/load resource runs. `.venv/Scripts/python.exe -m services.evaluation.runner --manifest artifacts/dictation-corpus/manifest.json --split development --stage asr --output artifacts/evaluation/asr.jsonl` is the CLI contract to implement in M0/M3, not a command available today.

**Exit:** a proven multilingual baseline and documented resource tradeoff. If within-sentence switching fails, conduct a bounded alternative-backend experiment using the same harness; do not declare auto detection solved or expand to an unlimited model search.

### M4 — Meaning-preserving local grammar and revision editor (P1)

**Owner:** Sol. **Dependencies:** M0 and the ASR contract in M3; integration follows M1. **Findings:** F06-F07, F12.

**Modify:** `services/llm/formatter.py`, `services/llm/cleanup.py`, `services/pipeline/orchestrator.py`, `services/pipeline/models.py`, `services/config/settings.py`, `tests/test_llm_data.py`.

**Create:** `services/llm/policy.py`, `services/llm/validation.py`, `services/llm/prompts/dictation.txt`, `tests/test_formatter_policy.py`, `tests/test_formatter_failures.py`, `tests/fixtures/dictation/formatting.json`.

- [ ] Preserve the user's existing terminal-comma changes and regressions while adding adversarial literal/code/number/negation tests.
- [ ] Replace unrestricted destructive regex with conservative normalization. Add the multilingual editor contract from section 2, with versioned prompt and fixed generation settings.
- [ ] Implement local-only backend checks, bounded output/time, validation, raw fallback, cancellation, and explicit degraded outcomes. Budget formatting inside the two-second short-utterance target rather than giving it an independent ten seconds.
- [ ] Evaluate formatter-only cases from correct raw text, then the same cases from actual ASR. Separate ASR-caused errors from formatter-caused changes.
- [ ] Compare existing configured local model, a smaller multilingual quantized option, and conservative raw output; choose based on actual French/Arabic/mixed grammar and energy. A model name in config is not proof of installed weights or suitable capability.
- [ ] Keep full-utterance context for multiword take-backs; preserve dialect and language choices. Do not let validators overreject legitimate grammar changes: score fallback rate alongside correctness.

**Checks:** protected literals and quoted commands; stutter versus emphasis; multilingual grammar and revisions; empty/malformed/timed-out/OOM backend outputs; language preservation; parent-reviewed development-set paired outputs. Implemented evaluator invocation: `.venv/Scripts/python.exe -m services.evaluation.runner --manifest artifacts/dictation-corpus/manifest.json --split development --stage full --output artifacts/evaluation/full.jsonl`.

**Exit:** editing and meaning gates pass in every language group with the selected ASR+formatter pair; latency/energy reported for the combined path. Frequent raw fallback fails the product-quality gate even if operationally reliable.

### M5 — Useful local context, vocabulary, and measured overlap (P1/P2)

**Owner:** Sol. **Dependencies:** M2-M4. **Findings:** F11, F13-F14.

**Modify:** `services/context/app_context.py`, `services/context/profiles.py`, `services/vocabulary/store.py`, `services/snippets/engine.py`, `services/runtime/correction_watcher.py`, `services/runtime/api.py`.

**Create:** `services/context/snapshot.py`, `services/context/terms.py`, `tests/test_context_budget.py`, `tests/test_context_vocabulary.py`.

- [ ] Enforce time/node/character budgets across all fields; collect field-adjacent text and locally selected terminology, not arbitrary large window trees.
- [ ] Scope vocabulary/replacements by language/app where useful and cap ASR prompt tokens. Make replacement strings literal-safe and snippet punctuation policy explicit.
- [ ] Keep learning manual until a verified insertion range and actual changed span can be observed. Continued typing alone must not generate correction claims.
- [ ] Run context-on/off ablations against the development corpus; retain only improvements without meaning or power regressions.
- [ ] Only if latency misses persist, evaluate bounded incremental ASR or overlap of context/capture work. Retain final tail, prohibit partial auto-pastes, and require measured improvement over full-utterance baseline without duplicate model work growth.

**Checks:** blocked/slow UIA, stale contexts, prompt budgets, adversarial nearby instructions, no context in password controls, name recall and false replacement rate, energy/latency ablations.

**Exit:** context demonstrably improves relevant words; no unbounded traversal or stale updates. Streaming remains optional if baseline already meets targets.

### M6 — Personal daily-use runtime, privacy, and final qualification (P1)

**Owner:** Sol; parent owns final review. **Dependencies:** M1-M5 gates, with context overlap optional. **Findings:** F15-F16.

**Modify:** `src-tauri/src/lib.rs`, `services/runtime/api.py`, `services/common/paths.py`, `services/runtime/logging.py`, `services/config/settings.py`, `scripts/setup.ps1`, `scripts/start_services.ps1`, `scripts/install_startup_task.ps1`, `README.md`.

**Create:** `requirements.lock`, `services/runtime/history.py`, `tests/test_runtime_ownership.py`, `tests/test_retention.py`, `docs/dictation-acceptance-results.md`.

- [ ] Revalidate M1's owned/adopted backend identity, shutdown, timeout cleanup, and stale-PID protection in the installed personal launcher. Add sleep/resume/device-change handling.
- [ ] Pin a tested Python/dependency/model runtime, validate local cache completeness, use atomic settings writes, and separate per-user data from the source checkout. A reproducible personal launcher is sufficient; broad installer distribution is deferred.
- [ ] Add opt-in bounded local history with deletion/retention, preserve manual vocabulary, rotate diagnostic logs, and keep content out of default logs. Prove history-disabled behavior independently from offline inference.
- [ ] Run outbound-denied operation after provisioning and trace network attempts; verify local formatter endpoint/model availability and clear missing-model recovery.
- [ ] Execute the live app matrix, cold/warm/load/power trials, five-day user trial, and optional matched Wispr comparison. Record every failed/degraded attempt, not only successful sessions.

**Exit:** parent reruns checks on integrated tree, inspects sampled raw/edited/inserted triples, and publishes measured results against every gate. Anything requiring the user or unavailable hardware instrumentation remains explicitly open. Passing builds alone never marks this complete.

## 5. Delegation and integration rules

Default implementation agent: **GPT 6.1 Sol**. Lifecycle, cross-component settings, multilingual ASR policy, formatting, insertion, background workers, architecture, and final correctness review always go to Sol or the parent. The parent freezes shared contracts before parallel implementation, reviews diffs, runs checks, and decides integration. Independent offline language experiments may run alongside lifecycle implementation after M0; do not run competing performance benchmarks simultaneously.

Terra is optional, never required. Only these examples are eligible once the parent has fixed the precise contract:

| Bounded Terra task | Scope and expected behavior | Constraints | Required checks before parent acceptance |
|---|---|---|---|
| Isolate two configuration-coupled tests | Only `tests/test_runtime_api.py` and `tests/test_pipeline_server.py`; pass explicit temporary settings roots/fixtures | No runtime behavior or user settings changes; escalate if an API seam needs design | Run affected files and full Python suite with differing explicit fixture profiles; inspect diff and config hashes |
| Repair benchmark sample-rate validation | Only WAV import helper plus `tests/test_asr_benchmark.py`; accept current 16-kHz PCM contract and reject unsupported rates with clear error | No resampling design, inference changes, or model work | Deterministic in-memory WAV fixtures for 8/16/44.1/48 kHz, stereo policy, malformed input; parent runs checks |
| Implement a frozen scoring primitive | One pure helper in `services/evaluation/scoring.py` after Sol specifies Unicode normalization/tokenization | No language-policy decisions or corpus relabeling | Exact parent-supplied Arabic/French/mixed/empty edit-distance examples and invariants; parent independently runs them |

Each actual Terra dispatch must restate scope, expected behavior, constraints, and required verification. A completion statement without checks is incomplete. Expanded scope, unresolved checks, or deeper architecture findings escalate to Sol. Do not fragment tightly coupled changes merely to use Terra.

No worker edits another worker's owned files concurrently. The parent integrates one coherent slice at a time and preserves existing user changes. No giant rewrite branch, no model changes without paired results, and no automatic acceptance of subagent claims.

## 6. Execution order and review gates

1. M0 scoring/test baseline and first multilingual speech sample set.
2. M1 reliable sessions; M3 offline model experiments can proceed independently after M0.
3. M2 insertion safety; M4 formatter experiments use M3's ASR contract and corpus.
4. Integrate M3+M4 with M1+M2 and run the development-set full-pipeline gate.
5. M5 context and only measured latency improvements, selected using development data.
6. Freeze the complete configuration and run the held-out gate; M6 daily operation and user qualification follow. Changes informed by held-out failures require a disclosed iteration and fresh final holdout.

The earliest valuable integrated milestone is reliable capture/cancel/recovery plus multilingual ASR and a constrained editor in a controlled target field. It must show language preservation, take-back/grammar improvements, and end-to-end timings before expanding features.

Stop a model experiment when quality, memory, or power makes it dominated by another configuration. If no local combination meets all gates, report the actual frontier and the specific shortfall; do not promise Wispr equivalence or quietly enable a cloud fallback.

## 7. Manual and unavailable validation

The orchestrator can automate unit/concurrency checks, saved-audio inference, controlled field insertion, structured scoring, telemetry, and offline failure tests. The user must supply representative speech and intended wording, supply accepted Arabizi spelling variants, judge grammatical changes and unwanted translations, and assess real daily smoothness/fan/battery impact. Physical microphone behavior, accessibility differences in real apps, and true whole-system energy need on-machine trials and possibly an external meter.

The audit established none of those live acceptance results. Implementation is now approved and underway. M0 technical work and user corpus collection are tracked separately; M1 can proceed after technical review while recordings are collected. Model selection remains gated on representative recordings. See docs/dictation-validation.md and docs/dictation-corpus-collection.md for the implemented evaluation workflow; replay is not live inference or an acceptance result.

## 8. Candidate provisioning notes (29 October; not model selection)

The first formatter experiment should evaluate **Qwen3.5-4B Q4_K_M**, with thinking explicitly disabled, against unchanged raw text. Its official model card describes broader language coverage and a hybrid architecture relative to Qwen3-4B. Those claims justify a trial; they do not establish Egyptian Arabic, Arabizi, grammar fidelity, speed or energy. No formatter weights have been provisioned or evaluated at this checkpoint. [Qwen3.5-4B card](https://huggingface.co/Qwen/Qwen3.5-4B).

- Text-only GGUF candidate: the official Ollama distributor's `qwen3.5:4b` model blob, **2,648,593,216 bytes**, SHA256 `a9d0a3e1d8c91732a3812becd31f8d4a8ff57414903f226306069f0437e2ac50`. This is an Ollama-distributed quantization, not a Qwen-authored GGUF. Pin the blob digest rather than the mutable tag. Omit the separate vision projector. [Registry manifest](https://registry.ollama.ai/v2/library/qwen3.5/manifests/4b).
- Runtime experiment: official llama.cpp **b11981**, commit `da6c30ade8c30930dbf49fe57f9f02d727e99b15`, Windows x64 CUDA 12.4 archives. Executable archive SHA256 `dcb0f6be103aec6c55a365be71a678c0cf620f9c85bf9a16de78090edd44ddbb` (279,763,823 bytes); matching CUDA runtime archive SHA256 `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6` (391,443,627 bytes). These are published asset digests, not locally verified downloads. Verify actual RTX 5060 Ti execution/offload; an archive name does not establish compatibility. Keep this experiment isolated from the ASR runtime and system PATH. [Official release](https://github.com/ggml-org/llama.cpp/releases/tag/b11981).
- Template baseline: Qwen revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`, `chat_template.jinja`, with hard `enable_thinking=false`. Inspect the rendered template and actual returned content before using it for editing. Dictated `/think` text must not enable reasoning output. [Pinned template](https://huggingface.co/Qwen/Qwen3.5-4B/blob/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a/chat_template.jinja).
- If this candidate fails runtime or measured editing/resource gates, Qwen3-4B Q4_K_M is one bounded alternate; do not expand downloads without an observed failure category. Model provisioning and inference must remain separate operations; never download implicitly during dictation.
