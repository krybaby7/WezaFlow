//! Explicit opt-in compatibility paste. Foreground checks reduce, but cannot
//! eliminate, the focus race between validation and global SendInput delivery.
use std::sync::Mutex;
use std::time::Duration;

use crate::caret::{self, EditorIdentity, FocusRead};
#[cfg(windows)]
use crate::caret::{FocusProbe, CARET_READ_BUDGET};

/// How long the target gets to read the dictated text after Ctrl+V before the
/// user's previous clipboard is put back.
pub(super) const RESTORE_DELAY: Duration = Duration::from_millis(700);
const SNAPSHOT_LIMIT_BYTES: usize = 32 * 1024 * 1024;
#[cfg(windows)]
const CF_UNICODETEXT: u32 = 13;

pub(super) struct Target {
    pub session_id: String,
    pub capture_id: String,
    #[cfg(windows)]
    identity: Identity,
    #[cfg(windows)]
    editor: FocusProbe,
}

#[cfg(windows)]
#[derive(Clone, PartialEq, Eq)]
struct Identity { window: usize, focus: usize, pid: u32, created: u64 }

impl Target {
    pub fn qualify(&self) -> Result<EditorIdentity, PasteReason> {
        #[cfg(windows)]
        {
            let pressed = self.editor.collect();
            let started = validate_focus(pressed)?;
            self.revalidate(&started.0, false)?;
            Ok(started.0)
        }
        #[cfg(not(windows))]
        {
            Err(PasteReason::UnknownTarget)
        }
    }
    pub fn revalidate(&self, editor: &EditorIdentity, include_context: bool) -> Result<Option<caret::CaretContext>, PasteReason> {
        #[cfg(windows)]
        {
            let expected = self.identity.clone();
            let current = FocusProbe::start(CARET_READ_BUDGET, move || {
                read_editor(&expected, include_context)
            }).collect();
            match_focus(editor, current)
        }
        #[cfg(not(windows))]
        {
            let _ = (editor, include_context);
            Err(PasteReason::UnknownTarget)
        }
    }
}

fn validate_focus(observation: FocusRead) -> Result<(EditorIdentity, Option<caret::CaretContext>), PasteReason> {
    match observation {
        FocusRead::Editor(identity, context) => Ok((identity, context)),
        FocusRead::Sensitive => Err(PasteReason::SensitiveTarget),
        FocusRead::Changed => Err(PasteReason::ChangedTarget),
        FocusRead::Unknown => Err(PasteReason::UnknownTarget),
    }
}

fn match_focus(pressed: &EditorIdentity, current: FocusRead) -> Result<Option<caret::CaretContext>, PasteReason> {
    let (current, context) = validate_focus(current)?;
    if pressed != &current {
        return Err(PasteReason::ChangedTarget);
    }
    Ok(context)
}

pub(super) fn capture(session_id: &str, capture_id: &str) -> Option<Target> {
    #[cfg(windows)]
    {
        let identity = inspect()?;
        let expected = identity.clone();
        let editor = FocusProbe::start(CARET_READ_BUDGET, move || read_editor(&expected, false));
        Some(Target { session_id: session_id.into(), capture_id: capture_id.into(), identity, editor })
    }
    #[cfg(not(windows))]
    { let _ = (session_id, capture_id); None }
}

#[cfg(windows)]
fn read_editor(identity: &Identity, include_context: bool) -> FocusRead {
    if inspect().as_ref() != Some(identity) {
        return FocusRead::Changed;
    }
    let result = match native_edit(identity.focus) {
        None => caret::read_focus(include_context),
        Some(metadata @ FocusRead::Editor(EditorIdentity::NativeEdit, _)) if include_context => {
            match caret::read_focus(true) {
                FocusRead::Editor(_, context) => FocusRead::Editor(EditorIdentity::NativeEdit, context),
                FocusRead::Unknown => metadata,
                other => other,
            }
        }
        Some(metadata) => metadata,
    };
    if inspect().as_ref() != Some(identity) {
        return FocusRead::Changed;
    }
    result
}

#[cfg(windows)]
fn native_edit(focus: usize) -> Option<FocusRead> {
    use windows_sys::Win32::{Foundation::{GetLastError, SetLastError}, UI::WindowsAndMessaging::*};
    unsafe {
        let mut class = [0u16; 32];
        let len = GetClassNameW(focus as _, class.as_mut_ptr(), class.len() as i32);
        if len <= 0 || !String::from_utf16_lossy(&class[..len as usize]).eq_ignore_ascii_case("Edit") {
            return None;
        }
        SetLastError(0);
        let style = GetWindowLongW(focus as _, GWL_STYLE) as u32;
        if style == 0 && GetLastError() != 0 {
            return Some(FocusRead::Unknown);
        }
        if style & ES_PASSWORD as u32 != 0 {
            return Some(FocusRead::Sensitive);
        }
        if style & (ES_READONLY as u32 | WS_DISABLED) != 0 {
            return Some(FocusRead::Unknown);
        }
        Some(FocusRead::Editor(EditorIdentity::NativeEdit, None))
    }
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


#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum PasteReason {
    ChangedTarget,
    UnknownTarget,
    SensitiveTarget,
    ModifierHeld,
    ClipboardBusy,
    ClipboardChanged,
    ClipboardUnavailable,
    InvalidText,
    Cancelled,
    InputRejected,
    InputPartial,
    InputSent,
}

impl PasteReason {
    pub const fn message(self) -> &'static str {
        match self {
            Self::ChangedTarget => "The focused field changed.",
            Self::UnknownTarget => "Wezza could not identify a writable, non-sensitive field.",
            Self::SensitiveTarget => "Automatic paste skips password fields.",
            Self::ModifierHeld => "A keyboard modifier was still held.",
            Self::ClipboardBusy => "Another app was using the clipboard.",
            Self::ClipboardChanged => "A newer clipboard copy was kept.",
            Self::ClipboardUnavailable => "Wezza could not safely preserve the clipboard.",
            Self::InvalidText => "The result is not usable for automatic paste.",
            Self::Cancelled => "Paste authorization ended before the shortcut was sent.",
            Self::InputRejected => "Windows did not accept the paste shortcut.",
            Self::InputPartial => "Windows accepted only part of the paste shortcut.",
            Self::InputSent => "Delivery is unverified.",
        }
    }

    pub const fn outcome(self) -> &'static str {
        match self {
            Self::InputSent => "inserted_unverified",
            Self::InputPartial => "delivery_uncertain",
            _ => "not_attempted_recoverable",
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum ClipboardMutation {
    NoRecoveryIssue,
    /// If restoration fails, Windows may still hold the dictated text or an empty clipboard.
    RestoreFailed,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) struct PasteResult {
    pub reason: PasteReason,
    pub clipboard: ClipboardMutation,
}
impl PasteResult {
    pub fn skipped(reason: PasteReason) -> Self {
        Self { reason, clipboard: ClipboardMutation::NoRecoveryIssue }
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

/// Identifies one staged write independently of Windows' clipboard sequence,
/// which can advance during CloseClipboard as it synthesizes text formats.
#[derive(Clone, Copy)]
struct ClipboardWrite {
    owner: usize,
    format: u32,
    nonce: [u8; 16],
}

impl ClipboardWrite {
    fn confirm(&self, owner: usize, nonce: Option<[u8; 16]>, sequence: u32) -> Result<u32, PasteReason> {
        if owner != self.owner || nonce != Some(self.nonce) {
            return Err(PasteReason::ClipboardChanged);
        }
        if sequence == 0 {
            return Err(PasteReason::ClipboardBusy);
        }
        Ok(sequence)
    }

    #[cfg(windows)]
    fn new(owner: usize) -> Option<Self> {
        use windows::Win32::System::Com::CoCreateGuid;
        let format = unsafe { clipboard_write_format()? };
        let nonce = unsafe { CoCreateGuid().ok()?.to_u128().to_le_bytes() };
        Some(Self { owner, format, nonce })
    }

    /// Caller has the clipboard open. Check the HWND before asking Windows
    /// for the eagerly staged marker and never inspect any foreign content.
    #[cfg(windows)]
    unsafe fn confirm_open(&self) -> Result<u32, PasteReason> {
        use windows_sys::Win32::System::{DataExchange::*, Memory::*};
        let owner = GetClipboardOwner() as usize;
        if owner != self.owner {
            return Err(PasteReason::ClipboardChanged);
        }
        let handle = GetClipboardData(self.format);
        let nonce = if !handle.is_null() && GlobalSize(handle) >= self.nonce.len() {
            let ptr = GlobalLock(handle).cast::<[u8; 16]>();
            if ptr.is_null() {
                None
            } else {
                let value = ptr.read_unaligned();
                GlobalUnlock(handle);
                Some(value)
            }
        } else {
            None
        };
        self.confirm(owner, nonce, GetClipboardSequenceNumber())
    }

    #[cfg(windows)]
    unsafe fn confirm_clipboard_write(&self) -> Result<u32, PasteReason> {
        use windows_sys::Win32::System::DataExchange::CloseClipboard;
        if !open_clipboard(self.owner) {
            return Err(PasteReason::ClipboardBusy);
        }
        let confirmed = self.confirm_open();
        CloseClipboard();
        confirmed
    }
}

#[cfg(windows)]
unsafe fn clipboard_write_format() -> Option<u32> {
    use windows_sys::Win32::System::DataExchange::RegisterClipboardFormatW;
    let name: Vec<u16> = "WezzaFlowPasteWriteV1".encode_utf16().chain(Some(0)).collect();
    let format = RegisterClipboardFormatW(name.as_ptr());
    (format != 0).then_some(format)
}

fn match_restore_generation(proof: Result<u32, PasteReason>, expected: Option<u32>) -> Result<bool, PasteReason> {
    match proof {
        Ok(current) => Ok(expected.map_or(true, |sequence| restore_allowed(sequence, current))),
        Err(PasteReason::ClipboardChanged) => Ok(false),
        Err(reason) => Err(reason),
    }
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
    write: ClipboardWrite,
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
    inherited: Option<ClipboardWrite>,
    owner: usize,
}
impl Drop for ClipboardPlan {
    // A paste that never touched the clipboard leaves our previous text there,
    // so the inherited original still has to come back.
    fn drop(&mut self) {
        if let (Some(write), Some(saved)) = (self.inherited, self.saved.take()) {
            schedule_restore(Restore { saved, write }, self.sequence);
        }
    }
}

pub(super) fn prepare_clipboard(owner: usize) -> ClipboardPlan {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::DataExchange::GetClipboardSequenceNumber;
        let mut slot = restores();
        if let Some((sequence, restore)) = slot.take_for_paste(GetClipboardSequenceNumber()) {
            return ClipboardPlan { saved: Some(restore.saved), sequence, inherited: Some(restore.write), owner };
        }
        drop(slot);
        let (saved, sequence) = bounded_snapshot(owner);
        ClipboardPlan { saved, sequence, inherited: None, owner }
    }
    #[cfg(not(windows))]
    { ClipboardPlan { saved: None, sequence: 0, inherited: None, owner } }
}

#[cfg(windows)]
fn bounded_snapshot(owner: usize) -> (Option<SavedClipboard>, u32) {
    use std::sync::{atomic::{AtomicBool, Ordering}, mpsc};
    static SNAPSHOTTING: AtomicBool = AtomicBool::new(false);
    if owner == 0 || SNAPSHOTTING.swap(true, Ordering::AcqRel) {
        return (None, 0);
    }
    let (send, receive) = mpsc::channel();
    let started = std::thread::Builder::new().name("clipboard-snapshot".into()).spawn(move || {
        struct Slot;
        impl Drop for Slot {
            fn drop(&mut self) { SNAPSHOTTING.store(false, Ordering::Release); }
        }
        let _slot = Slot;
        let _ = send.send(unsafe { take_snapshot(owner) });
    });
    if started.is_err() {
        SNAPSHOTTING.store(false, Ordering::Release);
        return (None, 0);
    }
    // GetClipboardData may wait for another application's delayed rendering.
    // The one worker can finish and release the clipboard on its own.
    receive.recv_timeout(Duration::from_millis(150)).unwrap_or((None, 0))
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
    let Some(write_format) = clipboard_write_format() else { return (None, 0); };
    if !open_clipboard(owner) { return (None, 0); }
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
        // The marker is a per-write proof, never a format to replay on restore.
        if format == write_format { continue; }
        if !snapshot_format_allowed(format) { complete = false; break; }
        let handle = GetClipboardData(format);
        let size = if handle.is_null() { 0 } else { GlobalSize(handle) };
        let next = snapshot_budget(total, size);
        let data = if size == 0 || next.is_none() { std::ptr::null() } else { GlobalLock(handle) as *const u8 };
        if data.is_null() { complete = false; break; }
        formats.push((format, std::slice::from_raw_parts(data, size).to_vec()));
        GlobalUnlock(handle);
        total = next.unwrap_or(total);
    }
    // Delayed formats can advance the sequence as GetClipboardData renders them.
    let sequence = GetClipboardSequenceNumber();
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
        while !self.0.is_empty() {
            let (format, memory) = self.0.remove(0);
            if SetClipboardData(format, memory).is_null() {
                windows_sys::Win32::Foundation::GlobalFree(memory);
                return false;
            }
        }
        true
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
/// clipboard monitors.
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
        let failed = slot.take_due(generation, current)
            .is_some_and(|due| !unsafe { restore_clipboard_retry(&due.saved, &due.write, Some(sequence)) });
        drop(slot);
        if failed {
            crate::record_paste_decision(format_args!("RestoreFailed"));
        }
    });
}
#[cfg(not(windows))]
fn schedule_restore(restore: Restore, sequence: u32) {
    let _ = (restore.saved.formats.len(), restore.write.owner, sequence);
}

#[cfg(windows)]
enum RestoreAttempt {
    Finished,
    Busy,
    Failed,
}

#[cfg(windows)]
unsafe fn restore_clipboard_retry(saved: &SavedClipboard, write: &ClipboardWrite, expected: Option<u32>) -> bool {
    use windows_sys::Win32::System::DataExchange::GetClipboardSequenceNumber;
    // Retry a busy clipboard once. An incomplete restore is not retried, and
    // a sequence from a competing write is never overwritten.
    for _ in 0..2 {
        if expected.is_some_and(|seq| !restore_allowed(seq, GetClipboardSequenceNumber())) {
            return true;
        }
        match restore_clipboard(saved, write, expected) {
            RestoreAttempt::Finished => return true,
            RestoreAttempt::Busy => (),
            RestoreAttempt::Failed => return false,
        }
    }
    false
}

#[cfg(windows)]
unsafe fn restore_clipboard(saved: &SavedClipboard, write: &ClipboardWrite, expected: Option<u32>) -> RestoreAttempt {
    use windows_sys::Win32::System::DataExchange::*;
    let Some(staged) = saved_formats(saved) else { return RestoreAttempt::Failed; };
    if !open_clipboard(write.owner) { return RestoreAttempt::Busy; }
    // Re-check while holding the clipboard: a newer copy must win.
    let result = match match_restore_generation(write.confirm_open(), expected) {
        Ok(false) => RestoreAttempt::Finished,
        Err(_) => RestoreAttempt::Busy,
        Ok(true) if EmptyClipboard() == 0 => RestoreAttempt::Busy,
        Ok(true) if staged.install() => RestoreAttempt::Finished,
        Ok(true) => RestoreAttempt::Failed,
    };
    CloseClipboard();
    result
}

#[cfg(windows)]
unsafe fn modifiers_held() -> bool {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::*;
    [VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN].iter()
        .any(|key| GetAsyncKeyState(*key as i32) < 0)
}


fn clipboard_preflight(expected: u32, current: u32, saved: bool) -> Result<(), PasteReason> {
    if expected == 0 {
        return Err(PasteReason::ClipboardBusy);
    }
    if !restore_allowed(expected, current) {
        return Err(PasteReason::ClipboardChanged);
    }
    if !saved {
        return Err(PasteReason::ClipboardUnavailable);
    }
    Ok(())
}

fn classify_send(sent: u32) -> PasteReason {
    match sent {
        0 => PasteReason::InputRejected,
        4 => PasteReason::InputSent,
        _ => PasteReason::InputPartial,
    }
}

#[cfg(windows)]
fn check_native_target(target: &Target) -> Result<(), PasteReason> {
    if inspect().as_ref() != Some(&target.identity) {
        return Err(PasteReason::ChangedTarget);
    }
    match native_edit(target.identity.focus) {
        Some(FocusRead::Sensitive) => return Err(PasteReason::SensitiveTarget),
        Some(FocusRead::Unknown) => return Err(PasteReason::UnknownTarget),
        _ => {}
    }
    if unsafe { modifiers_held() } {
        return Err(PasteReason::ModifierHeld);
    }
    Ok(())
}

/// One attempt. Native focus checks bracket the clipboard write, but Windows
/// does not bind SendInput to a recipient or acknowledge text insertion.
pub(super) fn paste(target: &Target, text: &str, mut plan: ClipboardPlan) -> PasteResult {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::{System::DataExchange::*, UI::Input::KeyboardAndMouse::*};
        if text.is_empty() || text.contains('\0') {
            return PasteResult::skipped(PasteReason::InvalidText);
        }
        let owner = plan.owner;
        if owner == 0 {
            return PasteResult::skipped(PasteReason::ClipboardUnavailable);
        }
        if let Err(reason) = check_native_target(target) {
            return PasteResult::skipped(reason);
        }
        let Some(write) = ClipboardWrite::new(owner) else {
            return PasteResult::skipped(PasteReason::ClipboardUnavailable);
        };
        let markers = privacy_markers();
        if markers.len() != 3 {
            return PasteResult::skipped(PasteReason::ClipboardUnavailable);
        }
        let zero = 0u32.to_le_bytes();
        let data: Vec<u8> = text.encode_utf16().chain(Some(0)).flat_map(u16::to_le_bytes).collect();
        // Install privacy markers first. If one fails, the text is never installed.
        let Some(payload) = Staged::new(markers.iter().map(|format| (*format, zero.as_slice()))
            .chain([(write.format, write.nonce.as_slice()), (CF_UNICODETEXT, data.as_slice())])) else {
            return PasteResult::skipped(PasteReason::ClipboardUnavailable);
        };
        if OpenClipboard(owner as _) == 0 {
            return PasteResult::skipped(PasteReason::ClipboardBusy);
        }
        let preflight = clipboard_preflight(plan.sequence, GetClipboardSequenceNumber(), plan.saved.is_some())
            .and_then(|()| check_native_target(target));
        if let Err(reason) = preflight {
            CloseClipboard();
            return PasteResult::skipped(reason);
        }
        if let Some(inherited) = &plan.inherited {
            if let Err(reason) = inherited.confirm_open() {
                CloseClipboard();
                return PasteResult::skipped(reason);
            }
        }
        let Some(saved) = plan.saved.as_ref() else {
            CloseClipboard();
            return PasteResult::skipped(PasteReason::ClipboardUnavailable);
        };
        let Some(rollback) = saved_formats(saved) else {
            CloseClipboard();
            return PasteResult::skipped(PasteReason::ClipboardUnavailable);
        };
        if let Err(reason) = check_native_target(target) {
            CloseClipboard();
            return PasteResult::skipped(reason);
        }
        if EmptyClipboard() == 0 {
            CloseClipboard();
            return PasteResult::skipped(PasteReason::ClipboardBusy);
        }
        // After EmptyClipboard, this attempt owns restoration, including failures.
        let saved = plan.saved.take().expect("an open clipboard write owns the checked snapshot");
        if !payload.install() {
            let restored = EmptyClipboard() != 0 && rollback.install();
            CloseClipboard();
            return PasteResult {
                reason: PasteReason::ClipboardUnavailable,
                clipboard: if restored { ClipboardMutation::NoRecoveryIssue } else { ClipboardMutation::RestoreFailed },
            };
        }
        CloseClipboard();
        let recover = |reason, expected| {
            let restored = restore_clipboard_retry(&saved, &write, expected);
            PasteResult {
                reason,
                clipboard: if restored { ClipboardMutation::NoRecoveryIssue } else { ClipboardMutation::RestoreFailed },
            }
        };
        let prepared = match write.confirm_clipboard_write() {
            Ok(sequence) => sequence,
            Err(PasteReason::ClipboardChanged) => return PasteResult::skipped(PasteReason::ClipboardChanged),
            Err(reason) => return recover(reason, None),
        };
        if !restore_allowed(prepared, GetClipboardSequenceNumber()) {
            return recover(PasteReason::ClipboardChanged, Some(prepared));
        }
        if let Err(reason) = check_native_target(target) {
            return recover(reason, Some(prepared));
        }
        let mut events: [INPUT; 4] = std::mem::zeroed();
        for (event, (key, flags)) in events.iter_mut().zip([(VK_CONTROL, 0), (0x56, 0), (0x56, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)]) {
            event.r#type = INPUT_KEYBOARD;
            event.Anonymous.ki = KEYBDINPUT { wVk: key, wScan: 0, dwFlags: flags, time: 0, dwExtraInfo: 0 };
        }
        if !restore_allowed(prepared, GetClipboardSequenceNumber()) {
            return recover(PasteReason::ClipboardChanged, Some(prepared));
        }
        let reason = classify_send(SendInput(4, events.as_ptr(), std::mem::size_of::<INPUT>() as i32));
        if reason == PasteReason::InputRejected {
            return recover(reason, Some(prepared));
        }
        if reason == PasteReason::InputPartial {
            // Release keys only. No retry, Enter or additional keydown.
            let releases = [events[2], events[3]];
            let _ = SendInput(2, releases.as_ptr(), std::mem::size_of::<INPUT>() as i32);
        }
        schedule_restore(Restore { saved, write }, prepared);
        PasteResult::skipped(reason)
    }
    #[cfg(not(windows))]
    { let _ = (target, text, plan.saved.take()); PasteResult::skipped(PasteReason::UnknownTarget) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn post_close_sequence_is_bound_only_to_the_owned_nonce_and_window() {
        let write = ClipboardWrite { owner: 7, format: 0xc001, nonce: [1; 16] };
        let before_close = 100;
        let after_close = 103;
        assert!(!restore_allowed(before_close, after_close));
        assert_eq!(write.confirm(7, Some([1; 16]), after_close), Ok(after_close));
        assert_eq!(write.confirm(8, Some([1; 16]), after_close), Err(PasteReason::ClipboardChanged));
        assert_eq!(write.confirm(7, Some([2; 16]), after_close), Err(PasteReason::ClipboardChanged));
        assert_eq!(write.confirm(7, None, after_close), Err(PasteReason::ClipboardChanged));
        assert_eq!(write.confirm(7, Some([1; 16]), 0), Err(PasteReason::ClipboardBusy));
    }

    #[test]
    fn partial_send_retains_only_the_confirmed_generation_for_restore() {
        let write = ClipboardWrite { owner: 7, format: 0xc001, nonce: [1; 16] };
        let sequence = write.confirm(7, Some([1; 16]), 103).unwrap();
        assert_eq!(classify_send(2).outcome(), "delivery_uncertain");
        let current = write.confirm(7, Some([1; 16]), 103);
        assert_eq!(match_restore_generation(current, Some(sequence)), Ok(true));
        let replayed = write.confirm(7, Some([1; 16]), 104);
        assert_eq!(match_restore_generation(replayed, Some(sequence)), Ok(false));
        let same_app_new_copy = write.confirm(7, None, 104);
        assert_eq!(match_restore_generation(same_app_new_copy, Some(sequence)), Ok(false));
        let newer_wezza_paste = write.confirm(7, Some([2; 16]), 104);
        assert_eq!(match_restore_generation(newer_wezza_paste, Some(sequence)), Ok(false));
    }

    #[test]
    fn failed_post_close_confirmation_can_restore_only_the_current_owned_write() {
        let write = ClipboardWrite { owner: 7, format: 0xc001, nonce: [1; 16] };
        assert_eq!(match_restore_generation(Err(PasteReason::ClipboardBusy), None), Err(PasteReason::ClipboardBusy));
        let untouched = write.confirm(7, Some([1; 16]), 103);
        assert_eq!(match_restore_generation(untouched, None), Ok(true));
        let replacement = write.confirm(8, Some([1; 16]), 104);
        assert_eq!(match_restore_generation(replacement, None), Ok(false));
    }

    #[test]
    fn stable_chromium_provider_can_differ_from_native_owner_but_distinct_fields_fail_closed() {
        let press = EditorIdentity::Automation { provider_pid: 17, runtime_id: vec![42, 1] };
        assert_eq!(match_focus(&press, FocusRead::Editor(press.clone(), None)), Ok(None));
        let another = EditorIdentity::Automation { provider_pid: 17, runtime_id: vec![42, 2] };
        assert_eq!(match_focus(&press, FocusRead::Editor(another, None)), Err(PasteReason::ChangedTarget));
        let reused_id = EditorIdentity::Automation { provider_pid: 51, runtime_id: vec![42, 1] };
        assert_eq!(match_focus(&press, FocusRead::Editor(reused_id, None)), Err(PasteReason::ChangedTarget));
        assert_eq!(match_focus(&press, FocusRead::Sensitive), Err(PasteReason::SensitiveTarget));
        assert_eq!(match_focus(&press, FocusRead::Unknown), Err(PasteReason::UnknownTarget));
        assert_eq!(match_focus(&press, FocusRead::Changed), Err(PasteReason::ChangedTarget));
    }

    #[cfg(windows)]
    #[test]
    fn native_edit_fixture_distinguishes_writable_password_and_read_only() {
        use windows_sys::Win32::UI::WindowsAndMessaging::*;
        let class: Vec<u16> = "EDIT".encode_utf16().chain(Some(0)).collect();
        for (style, expected) in [
            (0, FocusRead::Editor(EditorIdentity::NativeEdit, None)),
            (ES_PASSWORD as u32, FocusRead::Sensitive),
            (ES_READONLY as u32, FocusRead::Unknown),
            (WS_DISABLED, FocusRead::Unknown),
        ] {
            unsafe {
                // Invisible, owned, and never activated. No clipboard or keyboard input.
                let window = CreateWindowExW(WS_EX_NOACTIVATE, class.as_ptr(), std::ptr::null(),
                    WS_POPUP | style, 0, 0, 10, 10, std::ptr::null_mut(), std::ptr::null_mut(),
                    std::ptr::null_mut(), std::ptr::null());
                assert!(!window.is_null());
                let observed = native_edit(window as usize);
                assert_ne!(DestroyWindow(window), 0);
                assert_eq!(observed, Some(expected));
            }
        }
    }

    #[test]
    fn stale_unavailable_and_contended_clipboards_do_not_authorize_a_write() {
        assert_eq!(clipboard_preflight(101, 101, true), Ok(()));
        assert_eq!(clipboard_preflight(101, 102, true), Err(PasteReason::ClipboardChanged));
        assert_eq!(clipboard_preflight(101, 101, false), Err(PasteReason::ClipboardUnavailable));
        assert_eq!(clipboard_preflight(0, 101, false), Err(PasteReason::ClipboardBusy));
    }

    #[test]
    fn rejected_partial_and_full_input_batches_have_distinct_outcomes() {
        assert_eq!(classify_send(0), PasteReason::InputRejected);
        assert_eq!(classify_send(0).outcome(), "not_attempted_recoverable");
        for count in 1..=3 {
            assert_eq!(classify_send(count), PasteReason::InputPartial);
            assert_eq!(classify_send(count).outcome(), "delivery_uncertain");
        }
        assert_eq!(classify_send(4), PasteReason::InputSent);
        assert_eq!(classify_send(4).outcome(), "inserted_unverified");
    }

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
