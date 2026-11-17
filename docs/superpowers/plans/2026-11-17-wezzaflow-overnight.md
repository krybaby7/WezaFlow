# WezzaFlow overnight improvement plan · November 17–18

Deliver a side-by-side Windows candidate that handles explicit spoken corrections and approved name spellings locally, and tells the user what happened when text failed to appear. The first acceptance case is the user's full sentence about Friday and Adam. The second is a focused chat where the user had to paste manually.

## Evidence and starting point

The November 17 REA report on Windows 1.6.1483 verified client paths for canonical vocabulary, nearby-context hints, explicit replacements, and formatting. Its correction-cue detector recognizes an edit after formatting. Wispr's deployed prompt and server algorithm are unknown. Wezza already sends vocabulary to ASR, and its editor already requests take-backs. The trial dictionary was empty at investigation time. The prior 24-case Qwen3.5-9B experiment missed clear revisions and mixed-script requirements. These findings justify testing the local result at each stage. [Focused REA report](<C:/Users/adami/projects/Reverse-engineer/investigations/wisprflow/2026-11-17-corrections-names/REPORT.md>) · [Prior editor comparison](<C:/Users/adami/.codex/worktrees/wezza-recovery/WEZZA/docs/implementation-progress.md>)

Use the November 17 trial as baseline. Its package records recovery commit `ec6ec7e` plus working changes. This run verified all 250 recorded source hashes and the packaged executable SHA-256 `e47b75d92444ddf695d7a24c3601f8dce81c871f15de4d5dcf9ab038c6c49b54`. The isolated candidate branch is `codex/overnight-20261117`, with baseline checkpoint `69c414e`. Run the commands below from [the candidate checkout](<C:/Users/adami/.codex/worktrees/wezza-overnight-20261117/WEZZA>). [Trial manifest](<C:/Users/adami/projects/Hobby/Tier 1/WEZZA/artifacts/Wezza-Personal-Trial/BUILD-INFO.json>)

## Run order

Estimated budget is up to six hours from kickoff. Advance as soon as each gate passes. The orchestrator owns the baseline, acceptance examples, integration, release build and morning decision. Two GPT-6.2 Sol agents own implementation at a time. GPT-6.1 Terra runs independent checks and prepares adversarial fixtures. Keep model benchmarks sequential on this PC.

| Elapsed budget | Owner | Work and exit condition |
|---|---|---|
| 0–30 min | Orchestrator + Terra | Verify the package and checkpoint, freeze examples, capture baseline behavior, start existing suites. |
| 30 min–2 h | Sol A and Sol B | Implement approved names and spoken revisions independently. Each reports a reproducing failure, fix, and passing focused tests. |
| 2–4 h | Sol A or B, Terra | Trace desktop delivery and correct reproducible faults. Terra exercises the integrated text path and checks destructive edits against frozen controls. |
| 4–5 h | Orchestrator + Terra | Compare the current local editor and candidate on the same frozen text, run regression suites, record warm and cold measurements separately. |
| 5–6 h | Orchestrator | Build the side-by-side trial, verify contents, record passing/failed cases and give a short personal microphone/chat check. |

## Work packages

### 1. Names the user chooses

- [ ] Give explicitly accepted product-name aliases their own representation. Preserve canonical casing in the vocabulary UI and in ASR hints. Older correction history remains advisory. Provide a reviewable way to enable `WESA → Wezza` and `whisper flow → Wispr Flow` in the candidate.
- [ ] Apply approved substitutions once, longest complete phrase first, with Unicode boundaries, deterministic duplicate/conflict handling, and literal-safe matching. Preserve the exact ASR raw field. Keep identity/raw-recovery behavior explicit when cleanup is disabled or fails.
- [ ] Exercise the normal desktop API, SQLite store, pipeline and raw/final presentation. Check overlaps, mixed casing, punctuation, mixed scripts, ordinary “whisper”, identifiers/URLs, removal of an alias, and reopening an older database. Bound ASR hints to the existing worker input limit.
- Primary files are `services/vocabulary/store.py`, `db/schema.sql`, `services/runtime/api.py`, `services/pipeline/orchestrator.py`, `src/lib/api.ts` and the Vocabulary panel in `src/App.tsx`.

### 2. Spoken revisions with preserved meaning

- [ ] Freeze the paired Friday case and the control table below before tuning. Retain the current local editor as comparator. Try a bounded edit for unmistakable corrections to a deadline or recipient with an explicit cue and compatible old/new spans. Preserve both surrounding clauses. Reject ambiguous or unsupported spans; keep the experimental model and recovery path available. Do not extend the legacy reset rules.
- [ ] Exercise the active multilingual runtime in `services/runtime/local_editor.py` and `services/llm/constrained_editor.py`. Check editor-disabled, timeout and malformed-response behavior, raw preservation, mixed script, quoted speech and negation. Track the reason for a deterministic revision separately from a model edit.
- [ ] Run baseline and candidate on identical reviewed text. A fallback, missed correction, changed negation or translated phrase is visible in the report. Any fallback to the model is subject to the same checks. If quality misses a gate, keep that behavior experimental and report the exact case. Defer further model procurement until the errors warrant it.

| Fixed input or control | Required outcome |
|---|---|
| “I need to send the report tomorrow. Actually, make that Friday at 10. Can you confirm whether Adam will attend?” | Friday at 10 is the sole deadline. The report and the complete question about Adam survive. |
| “Send it to Adam, sorry, to Amir.” | Amir is the recipient. Preserve the request. |
| Clear French and Egyptian Arabic revisions with a trailing clause | Keep the final date, clause, language and dialect. Record each language separately. |
| Two genuine deadlines, quoted “actually”, and “do not send it on Friday” | Retain both dates, quoted content or negation respectively. |
| English-first and Arabic-first mixed utterances | Preserve each original sentence's script policy, numeric Arabizi for Arabic in English/French-first sentences, and Latin foreign spans in Arabic-first sentences. |
| Approved names alongside “whisper”, a URL and an identifier | Apply only the approved product-name occurrences. Raw ASR remains recoverable. |

### 3. Delivery into a focused chat

- [ ] Start at press-time capture in `src-tauri/src/lib.rs`, then follow the reservation, focus/clipboard decisions and `SendInput` in `src-tauri/src/windows_paste.rs`. Four accepted key events establish that Windows accepted the request. They cannot establish that a chat field received the text.
- [ ] Expose the smallest actionable, text-free reason for a skipped or uncertain paste. Retain at-most-once delivery, cancellation, focus protection, no Enter, and a recoverable final result. Check for sensitive targets independently of caret shaping. Refuse an automatic clipboard mutation when the target cannot be qualified or a newer user copy intervenes.
- [ ] Test normal delivery and controlled failures with an owned target or injected Windows adapter. Cover a focus change, held modifier, clipboard contention/new copy, password field and partial send. Treat same-window field changes explicitly. Only label delivery observed when the fixture verifies the target contents. Record the limits of every exercised target.

## Acceptance, packaging and morning handoff

- [ ] Terra freezes extra cases before candidate evaluation. The orchestrator inspects raw, final, and expected outcomes per case. Require the full Friday and Amir cases, approved name examples and zero new destructive control failures. Report EN, FR, Egyptian AR and mixed results separately with numerator and denominator.
- [ ] Measure the frozen text through the real local editor on this PC where capacity permits. Record model/prompt/config, input set, failures, warm sample count, median and p95, and first request separately. Keep formatter time, ASR time and release-to-visible-text distinct. Report unavailable measurements explicitly.
- [ ] Run these in the candidate and inspect each exit status. Keep timing-sensitive Python/model checks away from heavy parallel builds.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
npm test
npm run build
cargo test --locked --offline --manifest-path src-tauri/Cargo.toml --lib
cargo build --release --locked --offline --manifest-path src-tauri/Cargo.toml --features tauri/custom-protocol
```

- [ ] Assemble in a fresh sibling folder with its own settings, database, logs and WebView profile. The existing assembler defaults to the live trial; use an explicit candidate destination. Preserve the deployed trial and its running process. Record the candidate commit, executable hash, launcher, check results and rollback route.
- [ ] In the morning, use the candidate alone for one short check. Say the Friday/Adam example and a product-name sentence in a disposable chat draft. Record raw, final, automatic versus manual delivery, and time after release. Repeat a French or Egyptian Arabic correction. Compare with Wispr if desired. These are the remaining personal-speech and real-chat acceptance gates.

Nearby-text collection and learning from subsequent edits are follow-up work. The former needs a bounded, session-bound source of local text. The latter needs evidence that the intended editor received the original dictation. Neither is required to fix the paired example.
