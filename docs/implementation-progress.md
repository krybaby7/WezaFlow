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
