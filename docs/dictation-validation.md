# Local dictation validation (M0 replay and M3 live ASR)

The replay runner evaluates supplied outputs without loading models, recording
audio, reading UI context, pasting text, or making network requests. The separate
M3 live ASR runner is documented below. Replay is not evidence of latency, power
use, or real speech quality.

Run from the repository root with an explicitly selected split:

```powershell
./.venv/Scripts/python.exe -m services.evaluation.runner --manifest local/manifest.json --split development --stage full --mode replay --predictions local/predictions.json --output local/report.json
```

`--stage asr` scores only verbatim recognition. `full` additionally scores intended
final alternatives. Predictions are an object keyed by sample ID, for example:
`{"dev-001": {"raw": "um send AB-12", "final": "Send AB-12"}}`.
An explicit failure is `{"dev-001": {"raw": "partial output", "error": "timeout"}}`.
Missing selected IDs are configuration errors; malformed outputs or explicit errors
remain failed report rows. Extra predictions are ignored. The runner never selects
models, tunes prompts, or compares configurations automatically.

## Paired manifest version 1

`tests/fixtures/dictation/manifest.example.json` is a schema illustration, not a
recorded speech dataset. Its missing placeholder audio intentionally fails validation.
Each sample has a stable unique ASCII `id`, `split` (`development` or `heldout`),
`audio` with relative path and lowercase SHA256, `verbatim` text, nonempty
`acceptable_final_texts`, and nonempty `language_tags` drawn from EN/FR/AR/mixed.
`metadata.dialect` and `metadata.script` are strings (use `unknown` if unknown).
For this product, record `metadata.dialect` as `Egyptian Arabic` and
`metadata.script_policy` as `sentence-anchor-v1`. Each spoken sentence anchors
its output script independently: an Arabic-start sentence retains Arabic script
for Arabic spans and Latin for EN/FR spans; an EN/FR-start sentence uses Latin
throughout, transliterating Arabic spans without translating them. Arabizi-number
usage is enabled by user preference; record `metadata.arabizi_numbers` as `true`.
Intended final references must be authored under this policy;
the scorer does not infer anchor language, transliteration correctness, or meaning.
Optional `spans` use Python Unicode code-point offsets into verbatim, with start
inclusive, end exclusive, and a language tag. Optional `protected_literals` contain
nonempty text and a category such as name, number, code, path, or acronym.
Audio must exist, stay inside the manifest directory (including resolved symlinks),
and match its SHA256. Validation checks all manifest entries, while scoring selects
only the requested split. Keep heldout transcripts/predictions away from model
selection and freeze IDs/references before final evaluation.
Duplicate sample IDs and duplicate JSON object keys (including prediction IDs) are
rejected. Unknown fields are retained but ignored by validation/scoring; they do
not establish new metrics. SHA256 must use lowercase hexadecimal. Empty final
strings are allowed for intended silence, but the alternatives list cannot be empty.

## Metrics and interpretation

Normalized ASR uses NFC, Unicode casefold, curly-to-straight apostrophe conversion,
Unicode punctuation replaced by spaces except apostrophes, and whitespace collapse.
Accents, Arabic letters/diacritics, digits, symbols, and internal apostrophes survive.
No ASCII transliteration or Arabic letter conflation is performed. Words are split
on whitespace; CER includes normalized spaces and measures Unicode code points.
Both error counts and reference denominators are retained. WER may exceed one;
word accuracy is `1 - WER` and may be negative. Empty reference plus nonempty output
has positive errors, zero denominator, and JSON null rates; both empty gives zero.

Raw scores always compare verbatim speech, separately from final intended text.
Final scoring reports the alternative with least word errors, then character errors,
exact case/punctuation string equality against any alternative, normalized equality,
and literal case-sensitive whole-occurrence presence for every protected item/category.
Whole occurrences require no adjacent Unicode word character (letter, digit, or
underscore); `42` in `142` therefore fails, while `(42)` passes.
Literal presence is only presence: it cannot detect duplicates, relocated literals,
or a semantic relationship. Exact equality and normalized metrics must be reviewed
together for numbers, paths, and code where punctuation matters. Grammar and semantic
review are explicitly null: edit distance cannot establish grammaticality or meaning.

`--configuration config.json` requires a JSON object and records its canonical JSON
SHA256 fingerprint. `model_revision` is null when absent; never infer an exact
revision from a model label. Record real local artifact revisions/checksums in the
configuration when known. Replay timers cover validation (manifest reading, hashing,
selection, prediction checks), and scoring (row processing); they exclude CLI JSON
read/write and are not inference timings. Model-load and inference durations are null.
Separate model-load warmth and inference warmth are `not_applicable_replay`; a live
adapter must distinguish cold, model-load-only warm, and inference-warm runs rather
than interpreting file cache reuse as warmed inference.

Collect consented EN, FR, AR and same-sentence mixed samples across dialect/script,
stutters, false starts, take-backs, grammar repairs, protected numbers/names/codes,
and silence. Assign development/heldout splits before tuning. Report failure counts
and all per-sample rows; do not silently omit failed/hard cases or invent manual
review scores. M0 deliberately supplies no quality or performance acceptance claim.

Reports bind to the corpus using `metadata.manifest_sha256` (the manifest file bytes),
plus per-row `audio_sha256` and `language_tags`. Energy and power fields are null,
with `resource_measurement_status: not_measured_replay`. No hardware measurements
are inferred from replay timing. The CLI creates output parent directories and
writes reports even when prediction rows fail. Exit status is 0 for successful
rows, 1 for reported row failures, and 2 for input validation or report-write errors.
Inspect counts alongside the process status.
Unknown object fields are allowed, but manifest, prediction, and configuration roots
must follow their documented schemas. Arrays/scalars cannot substitute for a
configuration object, including empty arrays. Non-finite JSON numbers (NaN,
Infinity, and overflow such as 1e999) are rejected on read; configuration
fingerprinting also rejects non-finite values supplied through the Python API.

## Offline live ASR (M3)

`services.evaluation.live` is separate from the replay runner. It loads one owned
faster-whisper model from an explicitly selected absolute local snapshot and
transcribes utterances sequentially. It never records audio, invokes the formatter,
reads desktop context, inserts text, tunes parameters, or selects a winning model.
For example (use your own absolute snapshot and consented manifest):

```powershell
./.venv/Scripts/python.exe -m services.evaluation.live --manifest local/manifest.json --split development --snapshot C:\absolute\model-snapshot --source owner/model --revision exact-revision --device cpu --compute-type int8 --cpu-threads 4 --output local/live-report.json
```

Every option shown except `--source` and `--revision` is required. Those two must
be supplied together unless the path is an HF cache `models--owner--name/snapshots/`
40-character commit directory, whose source/revision can be derived. Arbitrary
local directories require caller-supplied provenance. File SHA256/size metadata
binds the claim to the actual local artifacts; a caller revision label alone does
not prove upstream authenticity. Required nonempty snapshot files are `model.bin`,
`config.json`, `tokenizer.json`, `preprocessor_config.json`, and `vocabulary.json`.
A comparator missing any required file is unavailable for this runner until an
explicit complete local snapshot is supplied. There are no implicit downloads
or remote tokenizer fallbacks; the backend receives `local_files_only=True`.

The complete manifest and all its audio are validated before model construction,
including entries outside the chosen split. WAV inputs use the existing benchmark
contract: uncompressed PCM16, mono, exactly 16 kHz, with complete frame data. Audio
checksums and manifest schema checks still apply. `heldout` must be explicitly
selected and must not be used to choose settings. Historical English development
clips can establish a mechanical smoke result, never fresh heldout or trilingual
quality acceptance.

Reports retain raw text, every yielded segment's start/end timestamps, detected
language and available language probabilities (including unknown labels), audio
duration, ASR Unicode scores, and failure rows including partial transcripts.
Load failures produce a failed row for every selected sample. No transcript is
translated or formatted. Exit codes are 0 for all successful rows, 1 for reported
failures, and 2 for invalid inputs or report write errors.

Model load is measured once, separately from each inference. Native lazy segment
generation is exhausted inside the inference timer. The first row is `cold` for
inference; a later row is `warm` only after an earlier inference completed. Failed
partial decoding does not warm the run. `model_load_warmth: unknown_os_cache`
explicitly declines to infer filesystem-cache warmth from successful loading.
`queue_seconds` is zero because the runner submits one utterance at a time with
no work queue; file decoding and float-array preparation precede inference timing.
Per-row `model_load_seconds` repeats the shared load duration as provenance and
must not be summed across rows. Snapshot hashing and full corpus validation are
inside validation timing, before model load. CLI report writing is outside timers.

CPU utilization, GPU memory, energy, and power are explicitly null with
`resource_measurement_status: unavailable_not_instrumented`. Inference latency
alone establishes no low-power or energy result. Installed faster-whisper, CTranslate2, and numpy versions are recorded without
importing model code (a missing distribution is null). They are included in the
configuration fingerprint, alongside whether the backend factory is native or injected.
Each row records the actual `info.transcription_options` dataclass when provided by
the backend, including beam size, temperature schedule, and previous-text conditioning.
Its canonical JSON plus backend versions has an `effective_decode_fingerprint`;
missing backend options are explicitly `unavailable_backend_did_not_report`, with
null options/fingerprint. Unsupported non-JSON options are marked unavailable rather
than fabricated. Native defaults are thereby auditable from actual effective options.
Model/configuration and corpus hashes accompany every run; compare successful and failed rows together.
Real consented French, Egyptian Arabic, and within-sentence mixed speech plus
human-reviewed references remain required for the product quality gate.

The configuration fingerprint includes the five model artifact SHA256 hashes and
sizes, so replacing files at the same path/source/revision changes the fingerprint.
Optional `--progress` writes flushed JSON events to stderr for validation, model
loading, clip start, inference start, clip completion, and run completion. Events
contain only phase, stable clip ID, elapsed/load/inference timings, and success
status; they contain no audio, transcripts, references, or backend error messages.
Default execution remains quiet. Python callers may pass `progress=callback` to
`run_live` to receive those same dictionaries. Progress callbacks should return
promptly; events are observations and provide no timeout or cancellation behavior.

A backend exception after `transcribe` begins (including lazy segment iteration)
invalidates that owned backend for the rest of the run. The current row retains
partial text, segment metadata, scoring, duration, and its error with status
`inference_failed`. Remaining selected rows stay in the failure denominator with
status `not_attempted_backend_failed`, an explicit prior-backend-failure error,
validated audio duration, and null inference time/raw score. The runner never
retries that native instance after a decode failure: a missing CUDA DLL can leave
native state unsafe for a second call. Fix the environment and launch a separate
explicit run. This rule also applies to injected test backends.

Failures before native inference starts use `pre_inference_failed` and do not
invalidate the backend. Load failures use `model_load_failed`; successful rows
use `succeeded`. Progress still emits clip completion for unattempted rows with
null inference time and `succeeded: false`; it emits no inference-start event for
those rows. All selected clips remain visible regardless of backend availability.
