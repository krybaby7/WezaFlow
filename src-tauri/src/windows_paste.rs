//! Explicit opt-in compatibility paste. Foreground checks reduce, but cannot
//! eliminate, the focus race between validation and global SendInput delivery.
#[derive(Clone)]
pub(super) struct Target {
    pub session_id: String,
    pub capture_id: String,
    #[cfg(windows)]
    identity: Identity,
}

#[cfg(windows)]
#[derive(Clone, PartialEq, Eq)]
struct Identity { window: usize, focus: usize, pid: u32, created: u64 }

pub(super) fn capture(session_id: &str, capture_id: &str) -> Option<Target> {
    #[cfg(windows)]
    { Some(Target { session_id: session_id.into(), capture_id: capture_id.into(), identity: inspect()? }) }
    #[cfg(not(windows))]
    { let _ = (session_id, capture_id); None }
}

#[cfg(windows)]
fn inspect() -> Option<Identity> {
    use windows_sys::Win32::{Foundation::{CloseHandle, FILETIME}, System::Threading::{OpenProcess, GetProcessTimes, PROCESS_QUERY_LIMITED_INFORMATION}, UI::WindowsAndMessaging::*};
    unsafe {
        let window = GetForegroundWindow();
        if window.is_null() { return None; }
        let mut pid = 0;
        let thread = GetWindowThreadProcessId(window, &mut pid);
        if thread == 0 || pid == std::process::id() { return None; }
        let mut gui: GUITHREADINFO = std::mem::zeroed();
        gui.cbSize = std::mem::size_of::<GUITHREADINFO>() as u32;
        if GetGUIThreadInfo(thread, &mut gui) == 0 || gui.hwndFocus.is_null() || gui.hwndActive != window { return None; }
        // Native EDIT password fields are observable without reading any text.
        // Chromium/Electron password semantics are unknown in this experiment.
        let mut class = [0u16; 128];
        let len = GetClassNameW(gui.hwndFocus, class.as_mut_ptr(), class.len() as i32);
        if len <= 0 { return None; }
        let class_name = String::from_utf16_lossy(&class[..len as usize]).to_ascii_lowercase();
        if (class_name == "edit" || class_name.starts_with("richedit"))
            && GetWindowLongW(gui.hwndFocus, GWL_STYLE) & 0x20 != 0 { return None; }
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() { return None; }
        let mut created: FILETIME = std::mem::zeroed();
        let mut exit = created; let mut kernel = created; let mut user = created;
        let ok = GetProcessTimes(process, &mut created, &mut exit, &mut kernel, &mut user);
        CloseHandle(process);
        if ok == 0 { return None; }
        Some(Identity { window: window as usize, focus: gui.hwndFocus as usize, pid,
            created: (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime) })
    }
}

pub(super) enum ClipboardMutation { Unchanged, Emptied, ResultInstalled, ReplacedSinceAttempt, CurrentStateUnknown }

pub(super) struct PasteResult {
    pub outcome: &'static str,
    pub clipboard: ClipboardMutation,
}
impl PasteResult {
    pub fn skipped() -> Self {
        Self { outcome: "not_attempted_recoverable", clipboard: ClipboardMutation::Unchanged }
    }
    pub fn clipboard_message(&self) -> &'static str {
        match self.clipboard {
            ClipboardMutation::Unchanged => "The clipboard was not changed by this attempt.",
            ClipboardMutation::Emptied => "The previous clipboard was emptied, but the result could not be installed. Copy the recoverable result manually.",
            ClipboardMutation::ResultInstalled => "The result remains on the clipboard.",
            ClipboardMutation::ReplacedSinceAttempt => "The clipboard changed after preparing paste; the newer copy was preserved.",
            ClipboardMutation::CurrentStateUnknown => "The result was installed on the clipboard, but its current state could not be checked. No restoration was attempted.",
        }
    }
}

#[cfg(windows)]
unsafe fn modifiers_held() -> bool {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::*;
    [VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN].iter()
        .any(|key| GetAsyncKeyState(*key as i32) < 0)
}

/// One attempt; never restores clipboard, retries injection, or sends Enter.
pub(super) fn paste(target: &Target, text: &str, clipboard_owner: usize) -> PasteResult {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::{Foundation::GlobalFree, System::{DataExchange::*, Memory::*}, UI::Input::KeyboardAndMouse::*};
        if clipboard_owner == 0 || text.is_empty() || text.contains('\0') || inspect().as_ref() != Some(&target.identity) { return PasteResult::skipped(); }
        // OpenClipboard retries only acquisition, never an OS write.
        let deadline = std::time::Instant::now() + std::time::Duration::from_millis(100);
        loop {
            if OpenClipboard(clipboard_owner as _) != 0 { break; }
            if std::time::Instant::now() >= deadline { return PasteResult::skipped(); }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        let data: Vec<u16> = text.encode_utf16().chain(Some(0)).collect();
        let memory = GlobalAlloc(GMEM_MOVEABLE, data.len() * 2);
        if memory.is_null() { CloseClipboard(); return PasteResult::skipped(); }
        let ptr = GlobalLock(memory) as *mut u16;
        if ptr.is_null() { GlobalFree(memory); CloseClipboard(); return PasteResult::skipped(); }
        std::ptr::copy_nonoverlapping(data.as_ptr(), ptr, data.len());
        GlobalUnlock(memory);
        // Revalidate after acquisition/allocation, immediately before the first
        // destructive clipboard call. Held PTT modifiers must preserve old data.
        if inspect().as_ref() != Some(&target.identity) || modifiers_held() {
            GlobalFree(memory); CloseClipboard(); return PasteResult::skipped();
        }
        if EmptyClipboard() == 0 {
            GlobalFree(memory); CloseClipboard(); return PasteResult::skipped();
        }
        if SetClipboardData(13, memory).is_null() {
            GlobalFree(memory); CloseClipboard();
            return PasteResult { outcome: "not_attempted_recoverable", clipboard: ClipboardMutation::Emptied };
        }
        // The OS owns memory now. Clipboard intentionally retains the result.
        let prepared_sequence = GetClipboardSequenceNumber();
        CloseClipboard();
        if prepared_sequence == 0 {
            return PasteResult { outcome: "not_attempted_recoverable", clipboard: ClipboardMutation::CurrentStateUnknown };
        }
        if GetClipboardSequenceNumber() != prepared_sequence {
            return PasteResult { outcome: "not_attempted_recoverable", clipboard: ClipboardMutation::ReplacedSinceAttempt };
        }
        if inspect().as_ref() != Some(&target.identity) || modifiers_held() {
            let clipboard = if GetClipboardSequenceNumber() == prepared_sequence {
                ClipboardMutation::ResultInstalled
            } else { ClipboardMutation::ReplacedSinceAttempt };
            return PasteResult { outcome: "not_attempted_recoverable", clipboard };
        }
        let mut events: [INPUT; 4] = std::mem::zeroed();
        for (event, (key, flags)) in events.iter_mut().zip([(VK_CONTROL, 0), (0x56, 0), (0x56, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)]) {
            event.r#type = INPUT_KEYBOARD;
            event.Anonymous.ki = KEYBDINPUT { wVk: key, wScan: 0, dwFlags: flags, time: 0, dwExtraInfo: 0 };
        }
        // This remains a non-atomic compatibility writer. Suppress a known
        // clipboard replacement without restoring over somebody else's copy.
        if GetClipboardSequenceNumber() != prepared_sequence {
            return PasteResult { outcome: "not_attempted_recoverable", clipboard: ClipboardMutation::ReplacedSinceAttempt };
        }
        let sent = SendInput(4, events.as_ptr(), std::mem::size_of::<INPUT>() as i32);
        if sent > 0 && sent < 4 {
            // Release only: no new keydown, paste attempt, Enter, or retry. A
            // partial batch may have left our V/Ctrl down. Delivery stays uncertain
            // even when this single best-effort cleanup batch succeeds.
            let releases = [events[2], events[3]];
            let _ = SendInput(2, releases.as_ptr(), std::mem::size_of::<INPUT>() as i32);
        }
        let outcome = if sent == 4 { "inserted_unverified" } else { "delivery_uncertain" };
        PasteResult { outcome, clipboard: ClipboardMutation::ResultInstalled }
    }
    #[cfg(not(windows))]
    { let _ = (target, text, clipboard_owner); PasteResult::skipped() }
}
