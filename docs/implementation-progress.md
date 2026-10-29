# Wezza implementation progress

## 29 October 2026 — Recovery checkpoint and Windows startup cleanup

Current work is on `codex/local-trilingual-recovery` in `C:/Users/adami/.codex/worktrees/wezza-recovery/WEZZA`. The user moved the earlier worktree during validation. Its committed history remained intact; a fresh managed worktree starts from `596f4b0`. The ten uncommitted capture files were reconstructed from the exact recorded edit commands, preserved separately with hashes, and revalidated. Both pre-existing user edits (`services/llm/cleanup.py` and `tests/test_llm_data.py`) retain their original hashes and remain uncommitted. No original-checkout source was changed.

At this checkpoint the original worktree's model runtimes and raw experiment reports are unavailable at their previous paths, pending recovery of the moved folder. References to retained ignored artifacts in the historical entries below describe their locations before the move. The committed findings and recorded tool outputs survive; they must not be presented as a fresh inspection of currently unavailable files. New verification below applies to the recovery checkout.

The full suite exposed a real Windows spawn cancellation defect. Killing an ASR child before it unpickled its pipe endpoint left CPython's duplicated endpoint in the parent. The child was confirmed dead, but the I/O thread stayed blocked because that parent duplicate prevented EOF. An instrumented reproduction closed only that tracked duplicate and immediately released the blocked operation. The seven reported failure-mode assertions initially shared one unreaped service, so six were consequential failures rather than independent fault trials.

Commit `8da37df` adds a narrowly scoped Windows connection-transfer wrapper used by both ASR and local editor supervisors. It duplicates the endpoint directly into the spawning child's process, making early child exit release the handle. Job assignment, startup gates and deadlines are unchanged. Other platforms and alternate connection types retain their existing transfer behavior. New near-immediate startup-timeout regressions check both process and I/O ownership; fault modes use independent services, exact error classifications and request-receipt markers. The 1 ms deadline exercises early startup but does not itself prove the exact unpickle boundary on every run.

Independent Sol review inspected the local CPython implementation and ran all 52 ASR/editor tests successfully. After adding the final capture acceptance cases, the parent ran the complete **367-test Python suite (32.371 s, zero skips)**, **46 frontend tests**, the production frontend build, and **29 Rust tests**. No live microphone, native model inference, insertion or power measurement occurred in these checks. The existing release executable was not rebuilt or launched.

### Bounded capture

The recorder now retains at most 4,800,000 float32 samples (five minutes at mono 16 kHz), automatically drains at the limit, and attaches a visible qualification to the saved result. Later sessions cannot remove that result's warning or attach it to a new unlimited result. Invalid/nonfinite audio, detected overflow, unexpected device stop and sample-rate mismatch suppress ordinary ASR processing; a fatal final-drain fault takes precedence over the duration limit. Failed stream closure retains microphone ownership, blocks a replacement capture and permits a later cleanup retry. WAV ingestion rejects incompatible rates/channels/formats and partial frames. Intermediate conversion copies remain; this is a retained-storage bound, not a zero-copy claim.

Independent capture spec/quality reviews passed; the final UI review independently ran 35 App tests. Parent inspection then identified missing explicit device-stop and automatic-cap cancellation cases. Tests now prove that an inactive device automatically drains/closes without ASR, cancellation during a gated capped drain suppresses publication and clears current capture status, and further callbacks cannot grow or reorder the saved prefix. Production code needed no further change. Automated streams cover these behaviors; real microphone unplugging/overflow, a five-minute recording, hotkey release and the user-visible overlay still require a desktop trial. The main targets remain Codex, Claude, and Comet browser chats, Google Docs, Sheets and email. Automatic insertion and daily-use qualification remain open.

### Bounded inference diagnosis before the capacity comparison

Before the worktree move, a controlled discriminator ran four frozen synthetic inputs: three English/French/Arabic revisions and unchanged English negation. All requests disabled prompt caching and reported zero cached tokens. The GPU and CPU-configured original-prompt outputs matched; all three clear revisions remained unresolved. A compact revision-only prompt on GPU did not fix them and removed additional Arabic material. The negation control stayed unchanged. GPU requests took approximately 0.267–0.486 s, compact-prompt requests 0.342–0.450 s, and CPU-configured requests 2.418–4.732 s. The CPU flags requested no GPU layers/KV offload, but the low-verbosity log did not establish every buffer's actual placement. All owned servers were reaped. These historical observations do not identify a template/cache/GPU bug or qualify either model.

One Qwen3.5-9B Q4_K_M capacity comparison is pending, using the already frozen 24 development cases and fixed prompt/decoding settings. The initial download stopped at its 1200-second limit; one same-blob Range continuation validated its exact 206 byte range before appending. No inference has run. Runtime launch is held until the original frozen input hash and pinned runtime are recovered and verified. Personal speech, language fidelity, offline-denied operation, power and real-app insertion remain required before model selection or a replacement claim.

## 29 October 2026 — M4 local model development experiments

The Qwen3.5-4B Q4_K_M candidate is provisioned and has executed locally through llama.cpp b11981 on the RTX 5060 Ti. The parent independently rechecked both published runtime archive hashes, compared all 56 extracted runtime files against their archived bytes, and checked model/template hashes. The opt-in worker manifest is retained under ignored `artifacts/m4-local-editor-runtime/`; no global runtime, user settings or defaults changed.

The first run reached server readiness after 47.75 seconds but timed out on its first generation after 40 seconds. It was terminated and reaped. A separately instrumented run reached readiness in 2.16 seconds, took 21.04 seconds on its first edit, and 0.24–0.48 seconds on the next eleven edits. The server log records all 33 layers offloaded to GPU. The subsequent requests reused 315 cached prompt tokens. These observations establish local execution; they are not latency percentiles, a low-power result, or a cold-start guarantee. The cause of the initial delay is not established.

The twelve manually authored synthetic cases exposed serious quality failures despite valid JSON: the model inserted a French negation, retained abandoned take-back alternatives, translated an Arabic span into French, violated numeric-Arabizi anchoring, and formalized some Egyptian wording. The exact-literal validator accepted these changes; it is not a semantic fidelity check. The candidate is **not qualified for automatic dictation editing**.

A bounded follow-up froze eighteen development cases before running two conditions: greedy decoding with the original prompt, and the same decoding with clearer instructions plus five illustrative examples. Both ran on one owned server with separate neutral warmups. The second prompt improved some transliteration and dialect preservation, but still failed take-backs and sentence anchors. Median warm times were approximately 315 and 320 ms. Neither condition establishes product quality. Complete inputs, outputs, provenance and review are retained in ignored `artifacts/m4-editor-smoke-2026-10-29/` and `artifacts/m4-editor-ablation-2026-10-29/`.

The one alternate comparison used official `Qwen/Qwen3-4B-GGUF@bc640142c66e1fdd12af0bd68f40445458f3869b`, Q4_K_M (2,497,280,256 bytes; SHA256 `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`). It ran the same frozen eighteen cases and both prompts with a model-appropriate hard-nonthinking template. Warm medians were 255/237 ms, but revisions and script rules still failed. The original prompt also produced invented French content and followed a dictated translation instruction. The examples prompt avoided those two observed failures while leaving mixed Arabic in the wrong script. The Qwen3 wall timer additionally includes a template probe per request, so timing is not a controlled general speed ranking. The parent inspected all thirty-six output rows, independently hashed the model, and compared the official/forced templates; neither candidate is qualified. Native processes for these experiments were reaped. Artifacts and the recorded timeout/resumed-download provenance are in ignored `artifacts/m4-editor-qwen3-2026-10-29/`.

A final bounded hypothesis experiment used existing Qwen3.5 weights to separate minimal grammar/revision editing from script conversion. It froze twenty-four development inputs (the same eighteen plus six new challenges), both prompts and the qualification rule before its forty-eight measured requests. Some revisions improved, but the first stage still translated mixed speech and left clear corrections unresolved; the second stage often reconstructed the original text, reintroduced removed material, and changed Arabic-first sentences to Latin. Two exact-literal guard fallbacks retained their stage input. The parent inspected all paired stage outputs. Median combined warm time was about 670 ms; the predeclared quality rule failed. **Do not integrate this tested two-stage approach or promote either model.** Its complete artifacts are in ignored `artifacts/m4-editor-stages-2026-10-29/`, and its native server was reaped. These experiments identify observed failure categories, not a general impossibility result for local dictation. Further work should distinguish runtime/configuration effects from model capacity before another bounded candidate comparison; unlimited prompt tuning against these same cases is not a validation strategy.

The owned local editor runtime is implemented as an opt-in Python backend, without enabling this unqualified candidate in the normal profile. It verifies a pinned local manifest, launches a private loopback server inside a Windows job, completes a real neutral generation before readiness, checks rendered token budgets and response identity, bounds complete operations, preserves raw recovery on failure, and releases the process tree on idle/close. Defaults use two CPU threads, one server slot, no busy polling and fixed greedy generation. These settings are experimental, not a selected quality/power configuration.

The parent independently ran **351 Python tests, zero skips**. The 32 new tests include real fixture wrapper/native processes, Windows handles proving exit after timeout/close/owner crash, malformed and stalled transports, idle/config replacement, manifest tampering/path escape, disabled proxies/redirects, and preserved raw fallback. Review reproduced a caller shutdown deadline being exceeded while another cleanup held a lock; the fix and regression tests include lock contention in the deadline. A separate preassignment crash window now bounds the gated wrapper's wait by startup timeout; postassignment descendants are contained by the kill-on-close job. This is a bounded preassignment exit, not atomic process creation inside a job.

Before the shutdown contention fix, the parent also exercised the actual Qwen3.5 model through the service: first result 6.41 s including startup (5.49 s verification/loading and 0.24 s neutral warmup), then 0.37/0.29 s. A three-second test idle policy released the worker; close confirmed reaping, and an independent OS query found no wrapper or llama-server. Outputs still retained an abandoned revision and translated a mixed phrase; native execution does not qualify editing. Reports remain in ignored `artifacts/m4-worker-smoke-2026-10-29/`. Later fixture tests specifically verify the shutdown changes. No live microphone, insertion, whole-PC energy or network-denial test was performed.

Representative personal speech, paired ASR/editor measurements, offline-denied operation, energy and user judgment remain open gates. The ordinary local profile still exposes raw recovery while candidate evaluation continues.

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
