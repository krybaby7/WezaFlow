# Wezza implementation progress

## 29 October 2026 — M0 technical baseline

Work is on `codex/local-trilingual-dictation` in the attached managed worktree. The original checkout and the existing cleanup/test edits remain unchanged. The approved plan remains active; this checkpoint does not establish Wispr replacement quality.

Implemented:

- Unicode-aware recognition WER/CER with explicit error counts and denominators; independent final-output alternatives, exact literals, and unscored human semantic/grammar review.
- Versioned paired manifests with unique IDs, explicit development/heldout selection, local audio checksums, and provenance in replay reports. Replay failure rows remain visible and yield a nonzero CLI exit. Inference and energy values are explicitly unmeasured.
- A local 30-prompt collection helper with user-initiated recording, manually checked references, non-overwriting exports, and Windows UTF-8 console handling. Draft prompts are not references or recorded samples.
- ASR locking through lazy segment materialization, distinct inference/queue/audio durations, and strict benchmark WAV validation.
- Recorder cleanup for failure/interruption, safe retries and stale callback rejection. Final callbacks during stream stop/close are retained before capture generation is invalidated.
- Temporary settings fixtures for the two previously failing API tests.

The user confirmed Egyptian Arabic and `sentence-anchor-v1`: Arabic-starting sentences retain Arabic script for Arabic spans and Latin for English/French; English/French-starting sentences use Latin throughout, rendering Arabic spans as Arabizi with numbers. This policy is recorded in the plan and corpus metadata; production formatting does not implement it yet.

Verification performed by the orchestrator:

- `./.venv/Scripts/python.exe -m unittest discover -s tests -v`: **228 passed, zero skips**.
- Independent Sol final review also ran all 228 tests successfully. Earlier targeted spec and quality reviews identified issues that were fixed and rechecked.
- Real CLI `collect status` displayed all thirty English/French/Arabic prompts after the encoding fix; no microphone was activated.
- `git diff --check` passed. SHA-256 checks confirmed both original and worktree copies of the user's cleanup/test edits were preserved.
- No frontend or Rust implementation changed in this slice; their audit baseline was 25 frontend tests, successful frontend build, and 11 Rust tests in a fresh target directory.

Limitations and next work:

1. No personal speech has been recorded or annotated during this work. The first 30 development clips are needed before model selection. Use [collection instructions](dictation-corpus-collection.md); [evaluation instructions](dictation-validation.md) explain replay versus live measurements.
2. M1 session/controller reliability can proceed independently while recordings are collected. Lifecycle/insertion defects from the audit remain outside the M0 fixes.
3. Live inference adapters, multilingual model selection, grammar editing, real target insertion, latency, energy, and the user trial remain outstanding. Do not call replay correctness an acoustic or product-quality result.

Development scripts currently use the existing project virtual environment and node_modules through worktree junctions. No new runtime dependencies were retained. Use the explicit project Python path in commands; the system Python can silently skip dependency-dependent tests.

## 29 October 2026 — M1 session/control implementation

The session/control slice is implemented and independently reviewed. The full replacement goal remains active, and live M1 qualification is still outstanding.

- One Python controller accepts ordered, identified commands. Recorder acquisition, draining, and processing run outside its control lock. An accepted cancellation immediately invalidates publication while a noninterruptible worker retains ownership until it finishes.
- The Rust shell sends ordered commands for the actual initiating keyboard/mouse binding, keeps Escape available through processing, checks per-run engine identity, and observes acknowledged states. Lost start/stop/cancel replies are covered by production settlement logic and sequence tests. Unknown status does not abandon a possibly delayed start.
- The desktop owns authenticated engine startup/shutdown. Startup failures clean up owned children; failed termination retains the handle and reports failure. Unrelated port occupants/PID files are not adopted or killed. The Windows launcher only launches the built desktop executable and no longer starts Ollama or an anonymous Python sidecar.
- Session settings are validated and frozen at acceptance. Saved audio/VAD/model/injection settings reach adapters. Selecting a profile explicitly requests its backend defaults; unrelated saves preserve custom model overrides. Diagnostics distinguish configured settings from the active snapshot.
- Processing does not insert text. The UI retains the last recoverable result with its originating session ID and provides explicit Copy result. Speculation, automatic warmup, command editing, automatic insertion, and legacy CLI/HTTP processing paths are explicitly unavailable. Default foreground context/selection capture and correction observation are disabled until target provenance is implemented.
- Profile metadata now accurately identifies the current English-only profiles. This does not implement multilingual ASR or the user's script/Arabizi formatting policy.

Parent verification on the integrated code:

| Check | Result | Scope |
|---|---|---|
| `./.venv/Scripts/python.exe -m unittest discover -s tests -q` | 251 passed, zero skips | Includes 1,000 seeded controller schedules, immutable settings, five independent HTTP-boundary cases, and two launcher fixture tests |
| `npm test` | 32 passed | Acknowledged feedback, retained recovery, profile intent, settings retry, transport and existing UI behavior |
| `npm run build` | Passed | TypeScript and production frontend bundle |
| `cargo test --locked --offline --manifest-path src-tauri/Cargo.toml --lib --target-dir artifacts/audit-2026-10-29/cargo-target` | 27 passed | Production settlement/watch mapping, stale instances, binding ownership, volume ownership and child cleanup with fakes |
| Temporary real HTTP process smoke | Eight checks passed | Actual Python main startup, auth/handshake, invalid commands/settings, disabled warmup, shutdown and released port; report in ignored `artifacts/m1-http-smoke.json` |
| Diff/user-change checks | Passed | `git diff --check`; both pre-existing user file hashes match in original checkout and worktree |

Sol spec/quality review identified profile-selection and transport-recovery defects; both were fixed and re-reviewed. The bounded Windows launcher task used Terra with explicit acceptance tests; the parent inspected the changes and independently ran both tests before accepting them. Obsolete source-string tests requiring immediate overlay hiding, anonymous capability probing, or launcher-owned PID handling were retired in favor of behavioral checks.

Still unverified: real microphone/hotkey/Escape timing, device faults, real Windows audio restoration, native shell startup/exit fault trials, and UI behavior in an actual desktop build. The HTTP smoke used a temporary configuration root and an ephemeral loopback port, and never issued a valid recording start. No real speech, model inference, clipboard write, or insertion was performed. The existing release executable was not rebuilt or launched.

Next: target-bound insertion and capture provenance (M2), then cached-model readiness/live evaluation adapters and constrained language editing (M3/M4). Personal speech is still required before selecting models or claiming accuracy. Cache directory inventory found multilingual faster-whisper large-v3-turbo and local GGUF candidates, but completeness, loadability and quality remain unverified. Model retention/warm latency and power measurements remain part of M3; the current per-session adapter construction is not a qualified latency policy.

## 29 October 2026 — M2 transaction core

Implemented the Python reservation/completion contract for a future native insertion broker. Automatic insertion remains disabled: no OS writer, target capture, clipboard operation, or desktop adapter is integrated in this slice.

- A server-issued session generation binds result, capture and transaction identity. Reusing an old session UUID cannot revive earlier write permission.
- Reservation and cancellation share the controller lock. Only the first successful reservation returns `write_authorized=true`; concurrent and historical replays never authorize another write.
- Reservation waits for the processing worker to relinquish ownership, including the interval after result publication but before cleanup finishes. Pending commits block new sessions and report cancellation as too late.
- Completion must mean the external writer has returned or been reaped. An uncertain delivery consumes the transaction without retrying it. Missing completion retains ownership; a mere timeout does not release a potentially live writer. The future broker must enforce this assertion.
- The transaction ledger is bounded to the current server generation. The latest result stays available for explicit recovery.

Independent Sol review passed 46 transaction/controller/API tests. The parent inspected the integrated diff and ran the complete Python suite: **284 passed, zero skips**. Coverage includes concurrent duplicate reservations, cancel/reserve races, all completion outcomes, old-generation replay, the worker-cleanup barrier and HTTP success/replay paths. The user's two pre-existing files retain their original hashes. No frontend or Rust changes were made in this slice.

The user identified **Codex, Claude and Comet browser** as primary targets, including browser LLM chats, Docs, Sheets and email. These now drive the real-app matrix. Read-only feasibility work recommends editor-bound browser transactions for qualified ordinary fields and a bounded TSF investigation for desktop composers. Neither path has been implemented or qualified; generic contenteditable tests cannot establish Docs/Sheets or desktop support. The actual insertion gate and manual app trials remain outstanding.

## 29 October 2026 — M3 live evaluation baseline

Added an explicit local ASR evaluation CLI. It validates the complete audio manifest and local snapshot before constructing one sequential backend; requires tokenizer and preprocessor artifacts to prevent remote fallback; hashes model files; and records installed backend versions, effective decoder options, language diagnostics, segments, failures, model-load time and lazy inference time. Optional progress emits phases/IDs/timings without transcript content. CPU/GPU energy fields remain explicitly unmeasured.

A native inference exception now invalidates that model instance. The failed row retains partial output; subsequent selected clips remain explicit unattempted failures with null inference timings and scores. This behavior was added after an actual CUDA experiment raised a missing-library error on its first clip and stalled inside the encoder on the next call. It prevents reuse after a reported failure; a first native call that never returns still requires external process supervision.

Parent verification: **286 Python tests passed, zero skips**. Independent Sol review passed all 18 live-runner tests and separately checked the native-error and pre-inference-error regressions. A real four-clip run with the missing GPU library now exits with status 1 in about eight seconds, records one attempted failure plus three unattempted failures, and does not retry the broken backend.

Actual experiments used four existing English clips from the repository's historical voice pack (3.97–9.98 seconds), copied into an ignored development-only manifest. These references are old expected prompts, not newly reviewed verbatim ground truth or heldout data. No new microphone recordings were made.

| Configuration | Observed result | Interpretation |
|---|---|---|
| Cached multilingual large-v3-turbo, CPU int8, two threads | All four clips completed; model load 7.46 s; inference 19.26 s cold and 16.57–17.28 s warm | Execution works, but this configuration is much slower than the target experience; no formatter/insertion was timed |
| Same snapshot, CUDA int8_float16, two CPU threads, original environment | Device discovery and model loading succeed; first inference raises `Library cublas64_12.dll is not found or cannot be loaded` | CUDA discovery/loading alone is not readiness; no successful GPU inference established at this checkpoint |
| Same snapshot, CUDA int8_float16, isolated verified NVIDIA runtime | Two fresh-process runs completed all four clips each. Load: 6.64 s and 7.15 s. First inference: 28.35 s and 10.93 s. Subsequent inference: 0.38–0.55 s across six clips | Warm GPU execution is promising; model construction alone is not sufficient warmup. Cold startup remains substantial. These are smoke timings, not end-to-end or percentile acceptance |

Reports and diagnostic logs are in ignored `artifacts/m3-historical-smoke-2026-10-29/`. Snapshot revision: `mobiuslabsgmbh/faster-whisper-large-v3-turbo@0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`. The first successful GPU run produced the same four raw strings as the CPU run; this is not an accuracy claim against the old prompt references.

Official NVIDIA cuBLAS 12.8.4.1 and cuDNN 9.10.2.21 CUDA12 archives were downloaded into ignored `artifacts/m3-cuda-runtime/`, matched against NVIDIA manifest SHA256 values, and extracted as DLLs/licenses only. The shared Python environment, system PATH, drivers and application defaults were not modified. The parent inspected the extraction/probe scripts, independently checked every extracted file hash before native inference, and retained runtime provenance in both successful GPU reports. CUDA bootstrap was explicit and limited to the experiment process; production startup does not yet provision or use these libraries.

This does not select a production model or prove French/Egyptian Arabic/mixed recognition, grammar/Arabizi editing, offline network isolation, latency percentiles, energy use, or replacement quality. The large first-inference cost requires a bounded worker/warm-retention policy and an explicit initialization state; do not run a new model constructor per dictation or assume loading is sufficient readiness.

## 29 October 2026 — M4 constrained editor contract

Implemented an injectable editing seam in `services/llm/constrained_editor.py`; it is not wired into production and no language model has been invoked. The versioned prompt carries English/French/Egyptian Arabic editing requirements and the user's per-sentence script/numeric-Arabizi rule. This states the requested behavior, not measured model compliance.

Requests are validated into an immutable local snapshot with bounded raw text, context, vocabulary and caller-supplied exact literals. Only strict JSON with one `edited_text` string is accepted. Backend exceptions, malformed/empty/oversized output, invalid Unicode/NUL output, or changed protected-literal occurrence counts retain the exact raw transcript with a machine-readable fallback reason. Normal Arabic, emoji, RTL marks, newlines and tabs are preserved. No existing regex cleanup or user-owned file was modified.

The parent independently ran **296 Python tests, zero skips**; Sol final review reran all ten editor-contract tests after the input-boundary and Unicode fixes. Fixtures are manually authored synthetic contract cases. They do not prove semantic fidelity, sentence anchoring, grammatical quality, or actual Arabizi transliteration. Literal discovery, meaning/negation checks beyond supplied literals, model/runtime provenance, token budgets, supervised timeouts, real model evaluation and application integration remain open M4 work.

## 29 October 2026 — M3 supervised worker and application integration

The optional `local-multilingual` profile now connects the application to one engine-owned ASR process. Existing settings/defaults remain unchanged. The profile uses explicit local snapshot/runtime paths, automatic language detection on selection, and raw-text recovery while the real grammar editor is still being qualified. It bypasses destructive legacy cleanup and snippet expansion in this experimental path. This raw stage is temporary and does not satisfy the editing/replacement goal.

The worker uses spawn isolation, a Windows kill-on-close Job Object assigned before model initialization, bounded transport/input/output, request/generation identities, initialization/decode deadlines and one retained process. Readiness requires a fully consumed synthetic decode. A failed backend is discarded; uncertain cleanup retains ownership and prevents replacement. Idle unload shares the operation boundary with decoding/reconfiguration. Shutdown is enforced through the authenticated endpoint, FastAPI lifespan and main-process cleanup. Per-start metadata retains five model artifact hashes, runtime manifest hash, and actual load/warmup times. The default 60-second retention interval is provisional, not a measured energy policy.

Parent verification on the integrated tree:

- **319 Python tests passed, zero skips**; **39 frontend tests passed**; production frontend build passed.
- Independent Sol review reran 23 worker/integration tests, including actual Windows parent-crash containment, blocked sends, partial-frame receives, timeout cleanup, failed-reap retention, idle races and real VAD output through a spawned fixture worker. The review found an audio-list/float32-array mismatch and misleading readiness text; both were fixed and rechecked.
- A real saved-audio smoke exercised authenticated start/stop, controller capture/drain, WAV normalization, VAD, spawned GPU inference, raw recovery, result provenance, idle unload and shutdown. It used two historical English clips and a replay recorder; no microphone, keyboard hook, clipboard or insertion was invoked.
- Release-to-result took **11.69 s** for the first request (fresh worker; OS/driver cache state uncontrolled) and **0.83 s** for the next request. Both used PID 25056. The test's three-second idle interval unloaded the worker; shutdown confirmed resources reaped, and a separate OS check confirmed the PID absent. First startup metadata: load 7.68 s, synthetic warmup 0.75 s, total child startup 10.84 s. Report: ignored `artifacts/m3-worker-integration-2026-10-29/runtime-smoke.json`.

These are two integration observations, not latency percentiles or a real desktop dictation acceptance. Real microphone/hotkey feedback, maximum-duration capture, app insertion, French/Egyptian Arabic/code-switch quality, grammar/Arabizi behavior, whole-system energy and outbound-denied operation remain open. The grammar-model/runtime provisioning experiment is separate and does not change the default profile or automatically run inference.
