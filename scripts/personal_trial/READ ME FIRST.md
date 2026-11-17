# Wezza personal trial — this PC

Double-click **Launch Wezza.vbs**. Keep this folder on this PC: it uses the original WEZZA Python environment, the cached speech model and the local CUDA files. Nothing is registered to run at Windows startup, and nothing leaves this PC.

## Starting

The window shows **Runtime API connected** once the engine is up. The speech model and the editing model start loading at launch; give them about 20 seconds. You can dictate sooner, but that first result waits for the load. While loaded they hold roughly 6.5 GB of GPU memory, and they unload after an hour without dictation (the next key press starts loading them again).

## Dictating

- **Hold the mouse "forward" side button (MouseX2)**, speak, release. Keyboard alternative: **Ctrl+Shift+Alt+F11**. Change either in the Hotkeys tab. Ctrl+Alt+Space is not offered because another program on this PC already owns it.
- Hold for at least half a second. A shorter tap is discarded with a "hold to dictate" hint.
- **Hands-free:** tap, then press again within half a second. Recording continues without holding; press once more to finish.
- **Escape** cancels a recording.
- While Wezza runs, the side button no longer acts as "forward" in the browser. If Wispr Flow is bound to the same button or keys, both will react: quit one, or give them different bindings.

## What happens to the text

- **Automatic paste is on.** Wezza requests Ctrl+V when it can identify the same writable, non-sensitive field at the start and end of dictation. It never presses Enter.
- **Paste requested** means Windows accepted the shortcut. Check that the text arrived before trying again.
- After a paste request Wezza tries to restore your previous clipboard in about 0.7 seconds. A newer copy always takes precedence. Wezza marks its automatic clipboard writes to stay out of Win+V history.
- If the focused field changed, reports itself as a password field, or cannot be identified, Wezza skips the automatic paste. Read the reason in the bar and use **Copy result** in Wezza. Some editors do not expose enough information for automatic paste.
- **Fit to surrounding text** adjusts capitalisation, spacing and punctuation when a supported editor exposes the caret context. This setting does not change the safety checks for automatic paste.
- Both options are in the Dictation tab, next to **Local cleanup**.

The app window always keeps the last result: the **final text**, the **raw transcript** under it, and a copy button for each. History is off.

## What is still unproven

- The local cleanup model (Qwen3.5-9B) is experimental. In earlier trials it missed some spoken corrections and the Arabizi rule. A fluent-looking edit can still change your meaning; the raw transcript is your reference. Turn **Local cleanup** off to get recognition only.
- Recognition has not yet been measured on your French, Egyptian Arabic or mixed speech. Language detection is limited to English, French and Arabic.
- Command editing (Ctrl+Shift+Alt+F12) is unavailable.

## Six short recordings to start with

Judge intended meaning and language/script; do not require one exact spelling for Arabizi.

1. English: "I, I need to send the report. He don't have it yet." Aim: "I need to send the report. He doesn't have it yet."
2. French: "Bonjour, je voudrais confirmer notre rendez-vous demain, pardon, vendredi à dix heures." Keep French; the day is Friday, the time ten.
3. Arabic: "من فضلك ابعتلي التقرير بكرة الساعة خمسة." Keep the Arabic meaning, tomorrow and five.
4. Arabic-first mixed: "ابعتلي the final report بكرة before lunch." Arabic stays Arabic, English stays Latin; keep the deadline.
5. Self-correction: "Let's meet on Monday, sorry, Tuesday at four." The day is Tuesday, the time four.
6. English-first mixed: "I need حاجة صغيرة before lunch." The whole result should be in Latin script, with numeric Arabizi such as "7aga soghayara".

## Things worth checking

- **Speed:** time from release to text, right after launch and again later; and after more than an hour idle.
- **Clipboard:** copy something, dictate, wait a second, press Ctrl+V — you should get what you copied, not the dictation. Try an image or Excel range too. Wezza skips automatic paste when it cannot preserve all the clipboard formats. Check that Win+V omits text used for automatic paste.
- **Changed focus:** switch windows or move to another field in the same window before the text arrives. Expect **Ready to copy** with a reason. Recover the draft with **Copy result**.
- **Fitting:** dictate into the middle of a sentence; dictate twice in a row; try Notepad, a browser text box, your chat apps, a document and a terminal.
- **Last word:** release the key as you finish speaking and check the final word is still there.
- **Silence and noise:** hold the key without speaking, or while typing; nothing should be pasted.
- **Password field:** try a browser login field; nothing should be pasted or copied.
- **Hands-free and Escape** as described above.
- Fan noise and responsiveness of your other apps while Wezza is running.

## Stopping and reporting

Use the tray icon's **Quit WezaFlow**; the window's X only hides it. If the app is ever force-closed, its engine and models are shut down with it.

For each problem, note **what you said, the raw transcript, the final text, what went wrong, the language, the target app, and rough timing**. Logs: `logs` holds desktop output; `project/artifacts/logs` holds engine output. Check logs for dictated text before sharing them.
