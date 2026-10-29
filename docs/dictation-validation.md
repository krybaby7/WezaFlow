# Local dictation validation (M0)

This package evaluates supplied outputs without loading models, recording audio,
reading UI context, pasting text, or making network requests. Live adapters belong
to M3. Replay is not evidence of latency, power use, or real speech quality.

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
