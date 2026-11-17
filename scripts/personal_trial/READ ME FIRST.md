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

- **Automatic paste is on.** The text is pasted into the field that had focus when you pressed the key. Wezza never presses Enter.
- Your clipboard is put back about 0.7 seconds after the paste, unless you copied something else in the meantime. Dictated text is marked to stay out of Win+V history.
- If the window changed before the text was ready, nothing is pasted: the bar says **Copied to clipboard** and Ctrl+V pastes it where you want.
- **Fit to surrounding text** is on: mid-sentence the first letter is lowercased, spaces are added where needed, and a final period is dropped when text follows. Where an app does not expose its text (terminals, some editors), only a trailing space is added.
- Fields that report themselves as password fields are refused.
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
- **Clipboard:** copy something, dictate, wait a second, press Ctrl+V — you should get what you copied, not the dictation. Try it with an image and with an Excel range too. Check Win+V does not list dictated text.
- **Wrong window:** switch windows before the text arrives; you should see "Copied to clipboard" and nothing pasted.
- **Fitting:** dictate into the middle of a sentence; dictate twice in a row; try Notepad, a browser text box, your chat apps, a document and a terminal.
- **Last word:** release the key as you finish speaking and check the final word is still there.
- **Silence and noise:** hold the key without speaking, or while typing; nothing should be pasted.
- **Password field:** try a browser login field; nothing should be pasted or copied.
- **Hands-free and Escape** as described above.
- Fan noise and responsiveness of your other apps while Wezza is running.

## Stopping and reporting

Use the tray icon's **Quit WezaFlow**; the window's X only hides it. If the app is ever force-closed, its engine and models are shut down with it.

For each problem, note **what you said, the raw transcript, the final text, what went wrong, the language, the target app, and rough timing**. Logs: `logs` holds desktop output; `project/artifacts/logs` holds engine output. Check logs for dictated text before sharing them.
