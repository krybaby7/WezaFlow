# Collect the first 30 personal dictation samples

This workflow collects ten English, ten French, and ten Egyptian Arabic recordings before choosing models. The shipped prompts cover numbers, names, context, corrections, and selected bilingual/trilingual phrases. They are prompts only: change the wording naturally, and label what you actually said. There are no measured benchmark results yet.

Run commands from the repository root with the project Python environment. All audio and annotations remain local in the chosen collection directory. Do not commit personal recordings or labels. `init`, `status`, `annotate`, and `export` never access the microphone. Only `record` imports the existing audio smoke adapter, and capture starts only after you type `yes` for one selected sample. It stops after the requested duration (maximum 120 seconds). Hardware/backend failures are reported by the adapter.

```powershell
./.venv/Scripts/python.exe -m services.evaluation.collect init --output-dir artifacts/dictation-corpus
./.venv/Scripts/python.exe -m services.evaluation.collect status --collection artifacts/dictation-corpus/collection.json
./.venv/Scripts/python.exe -m services.evaluation.collect record --collection artifacts/dictation-corpus/collection.json --id en-01 --seconds 8
```

Read the displayed prompt before confirming. A quiet setting and a short pause before/after each sentence help. Record each ID separately. Existing recordings are never silently replaced; use a fresh collection directory for another take. `init` also refuses an existing collection. Record en-01 through en-10, fr-01 through fr-10, and ar-01 through ar-10. A failed take can be kept for inspection; this helper does not provide destructive replacement commands.

Listen to each local `audio/<id>.wav`. Create a UTF-8 annotation JSON file outside the collection metadata file, with your manually checked verbatim reference and one or more acceptable final texts. The following is an **illustrative annotation shape**, not a reference for an unrecorded utterance:

```json
{
  "verbatim": "Book twelve, no, thirteen seats.",
  "acceptable_final_texts": ["Book thirteen seats."],
  "language_tags": ["EN"],
  "metadata": {"dialect": "personal English", "script": "Latin", "script_policy": "sentence-anchor-v1", "arabizi_numbers": true},
  "protected_literals": [],
  "spans": []
}
```

```powershell
./.venv/Scripts/python.exe -m services.evaluation.collect annotate --collection artifacts/dictation-corpus/collection.json --id en-01 --annotation artifacts/en-01-annotation.json
```

The command shows the annotation and asks you to confirm you checked it against the recording. The prompt is never copied into a speech reference. Verbatim retains spoken corrections and repetitions; final text reflects the intended dictation. Correct the suggested language tags and metadata for the actual utterance. Optional spans use zero-based character offsets into verbatim (`start` inclusive, `end` exclusive); protected literals identify exact names, numbers, or identifiers that must survive finalization. Empty or invented references are not acceptable.

Prompt metadata records `script_policy: "sentence-anchor-v1"` and `arabizi_numbers: true`; relevant dialect labels use `Egyptian Arabic`. Apply the product script policy **per spoken sentence** when checking acceptable final texts:

- Arabic-start sentence: Arabic spans use Arabic script; English and French spans use Latin script.
- English/French-start sentence: all output uses Latin script, with Egyptian Arabic spans written in Arabizi with numbers (for example `3ayez` and `7aga`).

The user confirmed Arabizi with numbers for Egyptian Arabic spans in English/French-starting sentences. Manually record acceptable spellings for the samples you label; this collection tool does not generate those references. Mixed-language prompt spelling is a reading aid, not an output oracle. Verbatim should faithfully represent your speech; annotate language/script decisions and any ambiguities explicitly.

Export all thirty only after every entry is confirmed:

```powershell
./.venv/Scripts/python.exe -m services.evaluation.collect export --collection artifacts/dictation-corpus/collection.json --output artifacts/dictation-corpus/manifest.json
```

For a smaller explicitly selected ready subset, repeat `--id`:

```powershell
./.venv/Scripts/python.exe -m services.evaluation.collect export --collection artifacts/dictation-corpus/collection.json --output artifacts/dictation-corpus/first-two.json --id en-01 --id fr-01
```

Export requires a new manifest filename directly inside the collection directory; audio stays under that directory, so no copy is necessary. Existing manifests are not overwritten. Export fails for incomplete labels, missing recordings, changed checksums, or escaping audio paths, and runs the shared schema-1 manifest validator. Every initial sample is in `development`; reserve a separate future collection for held-out evaluation. A manifest establishes inputs and manually checked labels, not model quality.

Metadata writes use temporary files and atomic replacement. Do not run concurrent collection commands against the same collection. The helper verifies file integrity, not acoustic quality or reference correctness; listen and check every label. Tests inject capture/input and use temporary directories; they do not prove microphone availability and never capture real speech.

The CLI explicitly writes UTF-8 to stdout/stderr, including redirected output. Decode captured output as UTF-8 when invoking it from another tool.
