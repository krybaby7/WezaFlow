# WezaFlow

WezaFlow is a local-first desktop dictation pipeline inspired by Wispr Flow.
The implementation is organized as a Tauri desktop shell plus Python services for
audio capture, speech recognition, context detection, LLM formatting, and text
injection.

The first implementation slice focuses on stable module boundaries:

- `services/asr`: microphone capture, VAD, and Whisper transcription
- `services/context`: active app and browser context detection
- `services/llm`: local Ollama-based text formatting
- `services/injection`: clipboard and keyboard text insertion
- `services/pipeline`: shared contracts and orchestration
- `db`: SQLite schema for vocabulary, snippets, profiles, corrections, and history
- `scripts`: platform setup, startup, and test helpers

## Windows

Run the tests with the project environment:

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Build the Windows Tauri release with:

```powershell
npm run tauri -- build
```

For development, run `npm run tauri -- dev`. The desktop shell starts and owns
its authenticated local Python engine. `scripts/start_services.ps1` launches an
existing release executable; rebuild it after source changes. Do not start a
separate Python API before opening the desktop app.

The local trilingual rebuild currently returns recoverable text with an explicit
**Copy result** action. Automatic insertion, command editing, speculative
processing, automatic warmup, and the legacy CLI capture path are unavailable
while the new lifecycle and insertion adapters are qualified. Existing ASR
profiles are English-only; multilingual recognition and grammar editing are
still pending evaluation. See [implementation progress](docs/implementation-progress.md)
and [speech collection instructions](docs/dictation-corpus-collection.md).

## macOS test build

The macOS path has not been qualified for the current session rebuild. It is
intended for developer testing from a GitHub checkout. It uses
the same Tauri shell and Python runtime, with macOS-specific Command+C/Command+V
text injection through `pyautogui`.

From a Mac:

```bash
bash scripts/setup_macos.sh
npm run tauri -- dev
```

For a local macOS release build:

```bash
npm run tauri -- build
```

On first use, macOS may prompt for Microphone and Accessibility permissions.
Grant Accessibility to the terminal during dev runs, or to WezaFlow when running
the built app. The GitHub workflow in `.github/workflows/build-macos.yml` can
produce a downloadable unsigned macOS test artifact from GitHub Actions. See
`docs/macos-test-build.md` for current limits.
