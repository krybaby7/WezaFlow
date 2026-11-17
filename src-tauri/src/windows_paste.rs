//! Explicit opt-in compatibility paste. Foreground checks reduce, but cannot
//! eliminate, the focus race between validation and global SendInput delivery.
use std::sync::Mutex;
use std::time::Duration;

/// How long the target gets to read the dictated text after Ctrl+V before the
/// user's previous clipboard is put back.
pub(super) const RESTORE_DELAY: Duration = Duration::from_millis(700);
const SNAPSHOT_LIMIT_BYTES: usize = 32 * 1024 * 1024;
#[cfg(windows)]
const CF_UNICODETEXT: u32 = 13;

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

impl Target {
    pub fn process_id(&self) -> u32 {
        #[cfg(windows)]
        { self.identity.pid }
        #[cfg(not(windows))]
        { 0 }
    }
}

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

pub(super) enum ClipboardMutation {
    Unchanged,
    Emptied,
    /// The dictated text was left on the clipboard for a manual Ctrl+V.
    ResultInstalled,
    ReplacedSinceAttempt,
    CurrentStateUnknown,
    /// The previous clipboard comes back after `RESTORE_DELAY` unless
    /// something else is copied first.
    RestoreScheduled,
}

pub(super) struct PasteResult {
    pub outcome: &'static str,
    pub clipboard: ClipboardMutation,
}
impl PasteResult {
    pub fn skipped() -> Self {
        Self { outcome: "not_attempted_recoverable", clipboard: ClipboardMutation::Unchanged }
    }
    pub fn clipboard_message(&self) -> Option<&'static str> {
        match self.clipboard {
            ClipboardMutation::Unchanged => Some("The clipboard was not changed."),
            ClipboardMutation::Emptied => Some("The clipboard was emptied; copy the result from WezaFlow."),
            ClipboardMutation::ResultInstalled => Some("The text is on the clipboard; press Ctrl+V to paste it."),
            ClipboardMutation::ReplacedSinceAttempt => Some("A newer copy was kept on the clipboard."),
            ClipboardMutation::CurrentStateUnknown => Some("The text may be on the clipboard; check before pasting."),
            ClipboardMutation::RestoreScheduled => None,
        }
    }
}

/// Only HGLOBAL-backed formats can be copied byte for byte. GDI objects,
/// owner-display and private formats carry handles that cannot.
pub(super) fn snapshot_format_allowed(format: u32) -> bool {
    !matches!(
        format,
        0 | 2 // CF_BITMAP
            | 3 // CF_METAFILEPICT
            | 9 // CF_PALETTE
            | 14 // CF_ENHMETAFILE
            | 0x80..=0x8F // CF_OWNERDISPLAY and CF_DSP*
            | 0x200..=0x3FF // CF_PRIVATE* and CF_GDIOBJ*
    )
}

/// Running snapshot size after adding `next` bytes, or `None` past the bound.
pub(super) fn snapshot_budget(total: usize, next: usize) -> Option<usize> {
    total.checked_add(next).filter(|sum| *sum <= SNAPSHOT_LIMIT_BYTES)
}

/// A restore may only replace the clipboard we wrote ourselves.
pub(super) fn restore_allowed(recorded: u32, current: u32) -> bool {
    recorded != 0 && recorded == current
}

pub(super) struct SavedClipboard {
    formats: Vec<(u32, Vec<u8>)>,
}

/// At most one restore is pending. Each arm gets a new generation, so a
/// restore thread whose generation was replaced or taken does nothing.
pub(super) struct RestoreSlot<T> {
    generation: u64,
    pending: Option<(u64, u32, T)>,
}
impl<T> RestoreSlot<T> {
    pub const fn new() -> Self {
        Self { generation: 0, pending: None }
    }
    pub fn arm(&mut self, sequence: u32, saved: T) -> u64 {
        self.generation += 1;
        self.pending = Some((self.generation, sequence, saved));
        self.generation
    }
    /// A new paste cancels the pending restore. While the clipboard still
    /// holds our previous text, the original snapshot carries over to it.
    pub fn take_for_paste(&mut self, current: u32) -> Option<(u32, T)> {
        let (_, sequence, saved) = self.pending.take()?;
        restore_allowed(sequence, current).then_some((sequence, saved))
    }
    pub fn take_due(&mut self, generation: u64, current: u32) -> Option<T> {
        if !matches!(self.pending, Some((pending, ..)) if pending == generation) {
            return None;
        }
        let (_, sequence, saved) = self.pending.take()?;
        restore_allowed(sequence, current).then_some(saved)
    }
}

struct Restore {
    saved: SavedClipboard,
    owner: usize,
}

// Held during a restore, so a new paste never snapshots a half-restored clipboard.
// No shell lock is ever taken while holding it.
static RESTORES: Mutex<RestoreSlot<Restore>> = Mutex::new(RestoreSlot::new());

fn restores() -> std::sync::MutexGuard<'static, RestoreSlot<Restore>> {
    RESTORES.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The user's clipboard as it was before a paste. Prepared outside shell locks:
/// another program's delayed rendering can be slow.
pub(super) struct ClipboardPlan {
    saved: Option<SavedClipboard>,
    sequence: u32,
    inherited: bool,
    owner: usize,
}
impl Drop for ClipboardPlan {
    // A paste that never touched the clipboard leaves our previous text there,
    // so the inherited original still has to come back.
    fn drop(&mut self) {
        if let (true, Some(saved)) = (self.inherited, self.saved.take()) {
            schedule_restore(Restore { saved, owner: self.owner }, self.sequence);
        }
    }
}

pub(super) fn prepare_clipboard(owner: usize) -> ClipboardPlan {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::DataExchange::GetClipboardSequenceNumber;
        let mut slot = restores();
        if let Some((sequence, restore)) = slot.take_for_paste(GetClipboardSequenceNumber()) {
            return ClipboardPlan { saved: Some(restore.saved), sequence, inherited: true, owner };
        }
        drop(slot);
        let (saved, sequence) = take_snapshot(owner);
        ClipboardPlan { saved, sequence, inherited: false, owner }
    }
    #[cfg(not(windows))]
    { ClipboardPlan { saved: None, sequence: 0, inherited: false, owner } }
}

#[cfg(windows)]
unsafe fn open_clipboard(owner: usize) -> bool {
    use windows_sys::Win32::System::DataExchange::OpenClipboard;
    // Retries only acquisition, never an OS write.
    let deadline = std::time::Instant::now() + Duration::from_millis(100);
    loop {
        if OpenClipboard(owner as _) != 0 { return true; }
        if std::time::Instant::now() >= deadline { return false; }
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[cfg(windows)]
unsafe fn take_snapshot(owner: usize) -> (Option<SavedClipboard>, u32) {
    use windows_sys::Win32::{Foundation::{GetLastError, SetLastError}, System::{DataExchange::*, Memory::*}};
    if !open_clipboard(owner) { return (None, 0); }
    let sequence = GetClipboardSequenceNumber();
    let mut formats = Vec::new();
    let mut total = 0;
    let mut complete = true;
    let mut format = 0;
    loop {
        SetLastError(0);
        format = EnumClipboardFormats(format);
        if format == 0 {
            complete &= GetLastError() == 0;
            break;
        }
        if !snapshot_format_allowed(format) { continue; }
        let handle = GetClipboardData(format);
        let size = if handle.is_null() { 0 } else { GlobalSize(handle) };
        let next = snapshot_budget(total, size);
        let data = if size == 0 || next.is_none() { std::ptr::null() } else { GlobalLock(handle) as *const u8 };
        if data.is_null() { complete = false; break; }
        formats.push((format, std::slice::from_raw_parts(data, size).to_vec()));
        GlobalUnlock(handle);
        total = next.unwrap_or(total);
    }
    CloseClipboard();
    (complete.then_some(SavedClipboard { formats }), sequence)
}

#[cfg(windows)]
unsafe fn global_copy(bytes: &[u8]) -> Option<windows_sys::Win32::Foundation::HGLOBAL> {
    use windows_sys::Win32::{Foundation::GlobalFree, System::Memory::*};
    let memory = GlobalAlloc(GMEM_MOVEABLE, bytes.len().max(1));
    if memory.is_null() { return None; }
    let ptr = GlobalLock(memory) as *mut u8;
    if ptr.is_null() { GlobalFree(memory); return None; }
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), ptr, bytes.len());
    GlobalUnlock(memory);
    Some(memory)
}

/// Global memory not yet handed to the clipboard; freed on drop.
#[cfg(windows)]
struct Staged(Vec<(u32, windows_sys::Win32::Foundation::HGLOBAL)>);
#[cfg(windows)]
impl Staged {
    unsafe fn new<'a>(formats: impl IntoIterator<Item = (u32, &'a [u8])>) -> Option<Self> {
        let mut staged = Staged(Vec::new());
        for (format, bytes) in formats {
            staged.0.push((format, global_copy(bytes)?));
        }
        Some(staged)
    }
    /// Requires an open, emptied clipboard. True when every format was set.
    unsafe fn install(mut self) -> bool {
        use windows_sys::Win32::System::DataExchange::SetClipboardData;
        let mut complete = true;
        while !self.0.is_empty() {
            let (format, memory) = self.0.remove(0);
            if SetClipboardData(format, memory).is_null() {
                windows_sys::Win32::Foundation::GlobalFree(memory);
                complete = false;
            }
        }
        complete
    }
}
#[cfg(windows)]
impl Drop for Staged {
    fn drop(&mut self) {
        for (_, memory) in self.0.drain(..) {
            unsafe { windows_sys::Win32::Foundation::GlobalFree(memory) };
        }
    }
}

#[cfg(windows)]
unsafe fn saved_formats(saved: &SavedClipboard) -> Option<Staged> {
    Staged::new(saved.formats.iter().map(|(format, bytes)| (*format, bytes.as_slice())))
}

/// Keeps the dictated text out of clipboard history, cloud sync and other
/// clipboard monitors. Unregistrable markers are skipped, not fatal.
#[cfg(windows)]
unsafe fn privacy_markers() -> Vec<u32> {
    use windows_sys::Win32::System::DataExchange::RegisterClipboardFormatW;
    ["ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"]
        .iter()
        .map(|name| {
            let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
            RegisterClipboardFormatW(wide.as_ptr())
        })
        .filter(|format| *format != 0)
        .collect()
}

#[cfg(windows)]
fn schedule_restore(restore: Restore, sequence: u32) {
    let generation = restores().arm(sequence, restore);
    let _ = std::thread::Builder::new().name("clipboard-restore".into()).spawn(move || {
        std::thread::sleep(RESTORE_DELAY);
        let mut slot = restores();
        let current = unsafe { windows_sys::Win32::System::DataExchange::GetClipboardSequenceNumber() };
        if let Some(due) = slot.take_due(generation, current) {
            unsafe { restore_clipboard(&due.saved, due.owner, sequence) };
        }
    });
}
#[cfg(not(windows))]
fn schedule_restore(restore: Restore, sequence: u32) {
    let _ = (restore.saved.formats.len(), restore.owner, sequence);
}

#[cfg(windows)]
unsafe fn restore_clipboard(saved: &SavedClipboard, owner: usize, expected: u32) -> bool {
    use windows_sys::Win32::System::DataExchange::*;
    let Some(staged) = saved_formats(saved) else { return false; };
    if !open_clipboard(owner) { return false; }
    // Re-check while holding the clipboard: a copy made since the delayed
    // check must win over our restore.
    if !restore_allowed(expected, GetClipboardSequenceNumber()) || EmptyClipboard() == 0 {
        CloseClipboard();
        return false;
    }
    let restored = staged.install();
    CloseClipboard();
    restored
}

#[cfg(windows)]
unsafe fn modifiers_held() -> bool {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::*;
    [VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN].iter()
        .any(|key| GetAsyncKeyState(*key as i32) < 0)
}

/// One attempt; never retries injection or sends Enter. A sent paste uses
/// `shaped` (when given) and restores the previous clipboard later; an
/// undeliverable one leaves the unshaped `text` there.
pub(super) fn paste(target: &Target, text: &str, shaped: Option<&str>, mut plan: ClipboardPlan) -> PasteResult {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::{System::DataExchange::*, UI::Input::KeyboardAndMouse::*};
        let owner = plan.owner;
        let shaped = shaped.filter(|s| !s.is_empty() && !s.contains('\0') && *s != text);
        if owner == 0 || text.is_empty() || text.contains('\0') { return PasteResult::skipped(); }
        if !open_clipboard(owner) { return PasteResult::skipped(); }
        let zero = 0u32.to_le_bytes();
        let markers = privacy_markers();
        let stage = |text: &str| {
            let data: Vec<u8> = text.encode_utf16().chain(Some(0)).flat_map(u16::to_le_bytes).collect();
            Staged::new(std::iter::once((CF_UNICODETEXT, data.as_slice()))
                .chain(markers.iter().map(|format| (*format, zero.as_slice()))))
        };
        let Some(plain) = stage(text) else {
            CloseClipboard();
            return PasteResult::skipped();
        };
        let shaped = match shaped.map(stage) {
            Some(None) => { CloseClipboard(); return PasteResult::skipped(); }
            Some(Some(shaped)) => Some(shaped),
            None => None,
        };
        // Revalidate after acquisition/allocation, immediately before the first
        // destructive clipboard call. An undeliverable result still goes onto
        // the clipboard for a manual Ctrl+V and is never restored over.
        let deliverable = inspect().as_ref() == Some(&target.identity) && !modifiers_held();
        let staged = match shaped {
            Some(shaped) if deliverable => shaped,
            _ => plain,
        };
        let fresh = GetClipboardSequenceNumber() == plan.sequence;
        let saved = plan.saved.take();
        if EmptyClipboard() == 0 {
            plan.saved = saved;
            CloseClipboard();
            return PasteResult::skipped();
        }
        let saved = saved.filter(|_| deliverable && fresh);
        if !staged.install() {
            // The text could not be installed; put the user's clipboard back now.
            let restored = saved.as_ref().and_then(|s| saved_formats(s))
                .is_some_and(|previous| EmptyClipboard() != 0 && previous.install());
            let clipboard = if restored { ClipboardMutation::Unchanged } else { ClipboardMutation::Emptied };
            CloseClipboard();
            return PasteResult { outcome: "not_attempted_recoverable", clipboard };
        }
        let prepared_sequence = GetClipboardSequenceNumber();
        CloseClipboard();
        let left_on_clipboard = |prepared: u32| {
            let clipboard = if prepared == 0 {
                ClipboardMutation::CurrentStateUnknown
            } else if GetClipboardSequenceNumber() == prepared {
                ClipboardMutation::ResultInstalled
            } else { ClipboardMutation::ReplacedSinceAttempt };
            PasteResult { outcome: "not_attempted_recoverable", clipboard }
        };
        if !deliverable || prepared_sequence == 0 { return left_on_clipboard(prepared_sequence); }
        if GetClipboardSequenceNumber() != prepared_sequence {
            return PasteResult { outcome: "not_attempted_recoverable", clipboard: ClipboardMutation::ReplacedSinceAttempt };
        }
        if inspect().as_ref() != Some(&target.identity) || modifiers_held() {
            return left_on_clipboard(prepared_sequence);
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
        if sent != 4 {
            // Uncertain delivery keeps the text available for a manual paste.
            return PasteResult { outcome: "delivery_uncertain", clipboard: ClipboardMutation::ResultInstalled };
        }
        let clipboard = match saved {
            Some(saved) => {
                schedule_restore(Restore { saved, owner }, prepared_sequence);
                ClipboardMutation::RestoreScheduled
            }
            None => ClipboardMutation::ResultInstalled,
        };
        PasteResult { outcome: "inserted_unverified", clipboard }
    }
    #[cfg(not(windows))]
    { let _ = (target, text, shaped, plan.saved.take()); PasteResult::skipped() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_keeps_hglobal_formats_and_skips_gdi_handles() {
        for format in [1, 7, 8, 13, 15, 16, 17, 0xC000, 0xC1A2, 0xFFFF] {
            assert!(snapshot_format_allowed(format), "{format:#x}");
        }
        for format in [0, 2, 3, 9, 14, 0x80, 0x81, 0x82, 0x83, 0x8E, 0x200, 0x2FF, 0x300, 0x3FF] {
            assert!(!snapshot_format_allowed(format), "{format:#x}");
        }
    }

    #[test]
    fn snapshot_budget_stops_at_limit_without_overflow() {
        assert_eq!(snapshot_budget(0, SNAPSHOT_LIMIT_BYTES), Some(SNAPSHOT_LIMIT_BYTES));
        assert_eq!(snapshot_budget(SNAPSHOT_LIMIT_BYTES, 1), None);
        assert_eq!(snapshot_budget(10, 20), Some(30));
        assert_eq!(snapshot_budget(usize::MAX, 1), None);
    }

    #[test]
    fn restore_requires_our_write_to_still_be_current() {
        assert!(restore_allowed(41, 41));
        assert!(!restore_allowed(41, 42));
        assert!(!restore_allowed(0, 0));
    }

    #[test]
    fn due_restore_runs_once_and_never_over_a_newer_copy() {
        let mut slot = RestoreSlot::new();
        let generation = slot.arm(10, "original");
        assert_eq!(slot.take_due(generation, 11), None);
        assert!(slot.pending.is_none());
        let generation = slot.arm(10, "original");
        assert_eq!(slot.take_due(generation, 10), Some("original"));
        assert_eq!(slot.take_due(generation, 10), None);
    }

    #[test]
    fn back_to_back_paste_inherits_original_and_cancels_stale_restore() {
        let mut slot = RestoreSlot::new();
        let first = slot.arm(10, "original");
        // Our previous text is still on the clipboard: carry the original over.
        assert_eq!(slot.take_for_paste(10), Some((10, "original")));
        let second = slot.arm(12, "original");
        assert_eq!(slot.take_due(first, 12), None);
        assert_eq!(slot.take_due(second, 12), Some("original"));
    }

    #[test]
    fn new_copy_between_pastes_is_snapshotted_instead_of_inherited() {
        let mut slot = RestoreSlot::new();
        let first = slot.arm(10, "original");
        assert_eq!(slot.take_for_paste(11), None);
        assert_eq!(slot.take_due(first, 11), None);
        assert_eq!(slot.take_for_paste(11), None);
    }
}
