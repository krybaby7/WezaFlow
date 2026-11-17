# WezzaFlow

WezzaFlow is a local-first desktop dictation app. The Tauri desktop shell manages recording and delivery, and an owned Python engine runs speech recognition and optional cleanup on this PC.

## Windows

Open **Launch Wezza.vbs** in `artifacts/WezzaFlow`. This is the stable entry point for the installed build. Opening it again while the same installation is running leaves the existing session alone. Use the tray icon to quit.

Hold your configured dictation shortcut, speak, and release. Change the binding in **Hotkeys**. In **Status**, **Copy result** copies the final text and **Copy raw transcript** copies the original recognition. Disable **Local cleanup (experimental)** in **Dictation** for recognition-only output. The local multilingual profile supports English, French, and Arabic. Cleanup can still miss corrections or change wording, so check the raw transcript when the exact meaning matters.

Use **Vocabulary → Approved aliases** to approve spellings such as **WESA → Wezza** and **whisper flow → Wispr Flow**. Saved terms can guide recognition, and approved aliases also apply to cleaned dictation. Wezza checks the focused field for automatic paste and can use supported caret context for spacing, punctuation, and capitalization. The spelling list comes from the vocabulary you save.

In **Dictation**, turn on **Enable experimental automatic paste** and save to request Ctrl+V when the same writable, non-sensitive field is still focused. **Paste requested** means Windows accepted the shortcut. Check the field before retrying. If paste is skipped, use **Copy result**. **Fit dictation to surrounding text** can be toggled separately.

### Windows development

Run the tests and build from the checkout:

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -v
npm test
npm run tauri -- build
```

Use `npm run tauri -- dev` for development, or `./scripts/start_services.ps1 -DevelopmentRelease` to launch the built checkout executable. Without the switch, the script opens the installed launcher. The desktop owns its local engine.

On this PC, `scripts/assemble_personal_trial.py --exe <path>` assembles the canonical package if no installation exists. Use `--output-dir <fresh-path>` to stage a separate build. The installed package keeps its own settings, database, logs, and WebView profile.

See the [implementation progress](docs/implementation-progress.md) and [speech collection instructions](docs/dictation-corpus-collection.md).

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
Grant Accessibility to the terminal during dev runs, or to WezzaFlow when running
the built app. The GitHub workflow in `.github/workflows/build-macos.yml` can
produce a downloadable unsigned macOS test artifact from GitHub Actions. See
`docs/macos-test-build.md` for current limits.
