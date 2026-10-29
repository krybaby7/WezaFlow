# Wezza personal trial — this PC

Double-click **Launch Wezza.vbs**. Keep this folder on this PC: it uses the original WEZZA Python environment, cached speech model and local CUDA files. The large editor/runtime assets are hardlinks; do not edit these files. Nothing was registered to run at Windows startup.

Wait for **Runtime API connected** before recording. Hold **Ctrl+Shift+Alt+F11**, wait for the recording indicator, speak, then release **all four keys**. You can change this binding in Hotkeys and Save settings. No microphone recording starts just by launching. The first utterance loads and verifies both models and may take considerably longer than the second. **Ctrl+Shift+Alt+F12 command editing is unavailable** in this trial.

Start with the app's own results. Compare **raw transcript** with **final/edited text**, and use the copy controls to keep examples. History is off. Automatic paste is off by default. The existing Qwen3.5-9B editor is experimental and has known failed cases: grammar, self-corrections and Arabizi are not qualified. A fluent-looking edit can still change your meaning; raw text is your reference.

Try these six short recordings, each separately. Judge intended meaning and language/script; do not require one exact spelling for Arabizi.

1. English: “I, I need to send the report. He don't have it yet.” Aim for “I need to send the report. He doesn't have it yet.”
2. French: “Bonjour, je voudrais confirmer notre rendez-vous demain, pardon, vendredi à dix heures.” Keep French; the intended day is Friday, time ten.
3. Arabic: “من فضلك ابعتلي التقرير بكرة الساعة خمسة.” Keep Arabic meaning, tomorrow and five.
4. Arabic-first mixed sentence: “ابعتلي the final report بكرة before lunch.” Arabic spans should stay Arabic and English spans Latin; preserve the deadline.
5. Self-correction: “Let's meet on Monday, sorry, Tuesday at four.” Intended day is Tuesday, time four.
6. English-first mixed sentence: “I need حاجة صغيرة before lunch.” The entire result should use Latin script, including numeric Arabizi such as “7aga soghayara”. Judge meaning rather than insisting on that exact spelling. Repeat with a French opening of your choice.

After ordinary dictation works, optionally enable **experimental automatic paste** in the app and try an empty, unsent normal text field in Codex, Claude, Comet, a document, a sheet, or an email draft. Keep focus in that field while dictating. Do not click Send/Submit. Paste and focus-race behavior remains unverified; some password fields may not be recognized, and copied text can remain on your clipboard. Disable the option again when finished.

Also try **Escape** during a recording, switching to another application before completion, a silent recording, and waiting over one minute idle before another utterance. Confirm no unwanted insertion, no unexpected recording, and that the next recording still works. Moving between two fields inside the same browser window may evade the experimental paste checks: keep automatic paste off if that is a concern. Check idle worker status and your usual fan/power experience.

To stop, use the tray icon's **Quit WezaFlow**. The window's X only hides it. Before sharing logs, check them for dictated text or other private material.

For each problem, report **what you said, raw text, final text, what went wrong, language, target app, and rough timing** (first/cold or later/warm). Include a screenshot and relevant logs if useful: `logs` holds desktop output; `project/artifacts/logs` holds API/runtime output. If launch fails, read the popup and these logs. This package was assembled without launching, microphone checks, or model trials; all hands-on testing is yours.
