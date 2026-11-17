//! Fits a dictation to the text around the caret before it is pasted. The
//! surrounding text is private: it is never logged, never sent to the engine,
//! and is dropped once the dictation has been shaped.
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    mpsc,
};
use std::time::{Duration, Instant};

/// Overall time a caret read may take, measured from when it starts.
pub(super) const CARET_READ_BUDGET: Duration = Duration::from_millis(150);
const BEFORE_CHARS: usize = 200;
const AFTER_CHARS: usize = 50;
/// Reads stuck inside another process. Past this, new reads are skipped
/// instead of piling up more blocked threads.
const MAX_STUCK_READS: usize = 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct CaretContext {
    /// Text before the caret (or selection) on its line.
    before: String,
    /// Text after the caret (or selection) on its line.
    after: String,
    selection: bool,
}

impl CaretContext {
    /// Keeps only the current line, bounded to the characters the rules need.
    pub fn from_raw(before: &str, after: &str, selection: bool) -> Self {
        let line_break = |c: char| matches!(c, '\n' | '\r' | '\u{0B}' | '\u{2028}' | '\u{2029}');
        let before = before.rsplit(line_break).next().unwrap_or("");
        let skip = before.chars().count().saturating_sub(BEFORE_CHARS);
        let after = after.split(line_break).next().unwrap_or("");
        Self {
            before: before.chars().skip(skip).collect(),
            after: after.chars().take(AFTER_CHARS).collect(),
            selection,
        }
    }
}

const DAY_AND_MONTH_NAMES: [&str; 19] = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january",
    "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december",
];

fn is_opening(c: char) -> bool {
    matches!(c, '(' | '[' | '{' | '“' | '‘' | '«' | '‹' | '„')
}

fn is_closing(c: char) -> bool {
    matches!(c, ')' | ']' | '}' | '”' | '’' | '»' | '›')
}

/// A straight quote closes when it follows a non-space character.
fn straight_quote_closes(before: &str) -> bool {
    let mut chars = before.chars().rev();
    matches!(chars.next(), Some('"' | '\'')) && chars.next().is_some_and(|c| !c.is_whitespace())
}

fn keeps_capital(word: &str) -> bool {
    let word = word.trim_matches(|c: char| !c.is_alphanumeric() && c != '\'' && c != '’');
    let mut chars = word.chars();
    let Some(first) = chars.next() else { return true };
    if word.chars().filter(|c| c.is_uppercase()).count() >= 2
        || chars.any(|c| c.is_uppercase() || c.is_numeric())
        || first.is_numeric()
    {
        return true;
    }
    if word == "I" || word.starts_with("I'") || word.starts_with("I’") {
        return true;
    }
    let lower = word.to_lowercase();
    DAY_AND_MONTH_NAMES.contains(&lower.as_str())
}

fn continues_sentence(before: &str) -> bool {
    before
        .trim_end_matches(|c: char| {
            c.is_whitespace()
                || ",-/:;()[]{}\"'".contains(c)
                || is_closing(c)
        })
        .chars()
        .last()
        .is_some_and(char::is_alphanumeric)
}

fn wants_leading_space(before: &str) -> bool {
    let Some(last) = before.chars().last() else { return false };
    if last == '"' || last == '\'' {
        return straight_quote_closes(before);
    }
    if last.is_whitespace() || is_opening(last) {
        return false;
    }
    last.is_alphanumeric() || is_closing(last) || ".,;:!?%".contains(last)
}

fn lowercase_first_letter(text: &str) -> String {
    let Some((index, first)) = text.char_indices().find(|(_, c)| !c.is_whitespace()) else {
        return text.to_string();
    };
    let word = text[index..].split_whitespace().next().unwrap_or("");
    if !first.is_uppercase() || keeps_capital(word) {
        return text.to_string();
    }
    let mut shaped = String::with_capacity(text.len());
    shaped.push_str(&text[..index]);
    shaped.extend(first.to_lowercase());
    shaped.push_str(&text[index + first.len_utf8()..]);
    shaped
}

/// Returns the text to paste. `None` is an unknown context: only the trailing
/// space that separates back-to-back dictations is added.
pub(super) fn shape(text: &str, context: Option<&CaretContext>, preserve_initial_case: bool) -> String {
    if text.trim().is_empty() {
        return text.to_string();
    }
    let ends_in_space = text.ends_with(char::is_whitespace);
    let Some(context) = context else {
        return if ends_in_space { text.to_string() } else { format!("{text} ") };
    };
    let mut shaped = if !preserve_initial_case && continues_sentence(&context.before) {
        lowercase_first_letter(text)
    } else {
        text.to_string()
    };
    let next = context.after.chars().next();
    if next.is_some_and(|c| c.is_lowercase() || ",.;:!?)".contains(c)) {
        let body = shaped.trim_end();
        if body.ends_with(['.', '!', '?']) {
            let tail = shaped[body.len()..].to_string();
            shaped.truncate(body.len() - 1);
            shaped.push_str(&tail);
        }
    }
    if wants_leading_space(&context.before) && !shaped.starts_with(char::is_whitespace) {
        shaped.insert(0, ' ');
    }
    if !ends_in_space && next.map_or(true, char::is_alphanumeric) {
        shaped.push(' ');
    }
    shaped
}


#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum EditorIdentity {
    NativeEdit,
    Automation { provider_pid: u32, runtime_id: Vec<i32> },
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum FocusRead {
    Unknown,
    Sensitive,
    Changed,
    Editor(EditorIdentity, Option<CaretContext>),
}

static IN_FLIGHT: AtomicUsize = AtomicUsize::new(0);

pub(super) struct FocusProbe {
    deadline: Instant,
    result: mpsc::Receiver<(Instant, FocusRead)>,
}

impl FocusProbe {
    /// A slow provider cannot hold up the dispatcher or create unlimited workers.
    pub fn start(budget: Duration, read: impl FnOnce() -> FocusRead + Send + 'static) -> Self {
        let (sender, result) = mpsc::channel();
        let deadline = Instant::now() + budget;
        if IN_FLIGHT.fetch_add(1, Ordering::SeqCst) < MAX_STUCK_READS {
            if std::thread::Builder::new().name("focused-editor".into()).spawn(move || {
                struct Slot;
                impl Drop for Slot {
                    fn drop(&mut self) { IN_FLIGHT.fetch_sub(1, Ordering::SeqCst); }
                }
                let _slot = Slot;
                let observation = read();
                let _ = sender.send((Instant::now(), observation));
            }).is_err() {
                IN_FLIGHT.fetch_sub(1, Ordering::SeqCst);
            }
        } else {
            IN_FLIGHT.fetch_sub(1, Ordering::SeqCst);
        }
        Self { deadline, result }
    }

    pub fn collect(&self) -> FocusRead {
        match self.result.recv_timeout(self.deadline.saturating_duration_since(Instant::now())) {
            Ok((completed, observation)) if completed <= self.deadline => observation,
            _ => FocusRead::Unknown,
        }
    }
}

#[cfg(windows)]
pub(super) fn read_focus(include_context: bool) -> FocusRead {
    use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED};
    unsafe {
        if CoInitializeEx(None, COINIT_MULTITHREADED).is_err() {
            return FocusRead::Unknown;
        }
        let result = uia_read(include_context).unwrap_or(FocusRead::Unknown);
        CoUninitialize();
        result
    }
}

#[cfg(windows)]
unsafe fn explicit_bool(mut value: windows::Win32::System::Variant::VARIANT) -> Option<bool> {
    use windows::Win32::{
        Foundation::{VARIANT_FALSE, VARIANT_TRUE},
        System::Variant::{VariantClear, VT_BOOL},
    };
    let raw = &value.Anonymous.Anonymous;
    let result = if raw.vt != VT_BOOL {
        None
    } else {
        match raw.Anonymous.boolVal {
            VARIANT_FALSE => Some(false),
            VARIANT_TRUE => Some(true),
            _ => None,
        }
    };
    let _ = VariantClear(&mut value);
    result
}

#[cfg(windows)]
unsafe fn runtime_id(
    element: &windows::Win32::UI::Accessibility::IUIAutomationElement,
) -> Option<Vec<i32>> {
    use windows::Win32::System::Ole::{
        SafeArrayDestroy, SafeArrayGetDim, SafeArrayGetElement, SafeArrayGetElemsize,
        SafeArrayGetLBound, SafeArrayGetUBound,
    };
    let array = element.GetRuntimeId().ok()?;
    if array.is_null() {
        return None;
    }
    let id = (|| {
        if SafeArrayGetDim(array) != 1 || SafeArrayGetElemsize(array) != 4 {
            return None;
        }
        let lo = SafeArrayGetLBound(array, 1).ok()?;
        let hi = SafeArrayGetUBound(array, 1).ok()?;
        if !(0..64).contains(&hi.checked_sub(lo)?) {
            return None;
        }
        (lo..=hi).map(|index| {
            let mut entry = 0i32;
            SafeArrayGetElement(array, &index, (&mut entry as *mut i32).cast()).ok()?;
            Some(entry)
        }).collect()
    })();
    let _ = SafeArrayDestroy(array);
    id
}

fn writable_editor(has_edit_role: bool, has_text_edit_pattern: bool, writable: bool) -> bool {
    writable && (has_edit_role || has_text_edit_pattern)
}

#[cfg(windows)]
unsafe fn uia_read(include_context: bool) -> windows::core::Result<FocusRead> {
    use windows::core::Interface;
    use windows::Win32::{
        System::Com::{CoCreateInstance, CLSCTX_INPROC_SERVER},
        UI::Accessibility::*,
    };
    let timeout = CARET_READ_BUDGET.as_millis() as u32;
    let automation: IUIAutomation =
        match CoCreateInstance::<_, IUIAutomation2>(&CUIAutomation8, None, CLSCTX_INPROC_SERVER) {
            Ok(automation) => {
                let _ = automation.SetConnectionTimeout(timeout);
                let _ = automation.SetTransactionTimeout(timeout);
                automation.cast()?
            }
            Err(_) => CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER)?,
        };
    let element = automation.GetFocusedElement()?;
    match explicit_bool(element.GetCurrentPropertyValueEx(UIA_IsPasswordPropertyId, true)?) {
        Some(true) => return Ok(FocusRead::Sensitive),
        Some(false) => (),
        None => return Ok(FocusRead::Unknown),
    }
    if !element.CurrentHasKeyboardFocus()?.as_bool() || !element.CurrentIsEnabled()?.as_bool() {
        return Ok(FocusRead::Unknown);
    }
    let control = element.CurrentControlType()?;
    let edit_role = control == UIA_EditControlTypeId || control == UIA_DocumentControlTypeId;
    let text_edit = element.GetCurrentPatternAs::<IUIAutomationTextEditPattern>(UIA_TextEditPatternId).ok();
    let provider_pid = element.CurrentProcessId()? as u32;
    let Some(id) = runtime_id(&element).filter(|_| provider_pid != 0) else {
        return Ok(FocusRead::Unknown);
    };
    let text = element.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId).ok()
        .or_else(|| text_edit.as_ref().map(|pattern| (**pattern).clone()));
    let writable = match element.GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId) {
        Ok(value) => !value.CurrentIsReadOnly()?.as_bool(),
        Err(_) => text.as_ref().and_then(|pattern| pattern.DocumentRange().ok())
            .and_then(|range| range.GetAttributeValue(UIA_IsReadOnlyAttributeId).ok())
            .and_then(|value| explicit_bool(value)) == Some(false),
    };
    if !writable_editor(edit_role, text_edit.is_some(), writable) {
        return Ok(FocusRead::Unknown);
    }
    let context = if include_context {
        text.as_ref().and_then(|pattern| read_context(pattern).ok()).flatten()
    } else {
        None
    };
    let current = automation.GetFocusedElement()?;
    if current.CurrentProcessId()? as u32 != provider_pid || runtime_id(&current).as_ref() != Some(&id) {
        return Ok(FocusRead::Changed);
    }
    match explicit_bool(current.GetCurrentPropertyValueEx(UIA_IsPasswordPropertyId, true)?) {
        Some(false) => Ok(FocusRead::Editor(EditorIdentity::Automation { provider_pid, runtime_id: id }, context)),
        Some(true) => Ok(FocusRead::Sensitive),
        None => Ok(FocusRead::Unknown),
    }
}

#[cfg(windows)]
unsafe fn read_context(
    pattern: &windows::Win32::UI::Accessibility::IUIAutomationTextPattern,
) -> windows::core::Result<Option<CaretContext>> {
    use windows::Win32::UI::Accessibility::*;
    let ranges = pattern.GetSelection()?;
    if ranges.Length()? < 1 {
        return Ok(None);
    }
    let selected = ranges.GetElement(0)?;
    let selection = selected.CompareEndpoints(TextPatternRangeEndpoint_Start, &selected, TextPatternRangeEndpoint_End)? != 0;
    let before = selected.Clone()?;
    before.MoveEndpointByRange(TextPatternRangeEndpoint_End, &selected, TextPatternRangeEndpoint_Start)?;
    before.MoveEndpointByUnit(TextPatternRangeEndpoint_Start, TextUnit_Character, -(BEFORE_CHARS as i32))?;
    let after = selected.Clone()?;
    after.MoveEndpointByRange(TextPatternRangeEndpoint_Start, &selected, TextPatternRangeEndpoint_End)?;
    after.MoveEndpointByUnit(TextPatternRangeEndpoint_End, TextUnit_Character, AFTER_CHARS as i32)?;
    let before = before.GetText((BEFORE_CHARS * 2) as i32)?.to_string();
    let after = after.GetText((AFTER_CHARS * 2) as i32)?.to_string();
    Ok(Some(CaretContext::from_raw(&before, &after, selection)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn known(before: &str, after: &str) -> CaretContext {
        CaretContext::from_raw(before, after, false)
    }

    #[test]
    fn editable_role_or_text_edit_pattern_needs_explicit_writability() {
        assert!(writable_editor(true, false, true));
        assert!(writable_editor(false, true, true));
        assert!(!writable_editor(false, false, true));
        assert!(!writable_editor(true, true, false));
        assert!(!writable_editor(false, true, false));
    }

    #[test]
    fn preserves_approved_initial_capitals_without_changing_spacing_or_punctuation() {
        let context = known("we use", "today");
        assert_eq!(shape("Wezza.", Some(&context), true), " Wezza ");
        assert_eq!(shape("Wispr Flow.", Some(&context), true), " Wispr Flow ");
        assert_eq!(shape("Simple.", Some(&context), false), " simple ");
    }

    #[test]
    fn shapes_dictation_to_the_caret_context() {
        let cases: &[(&str, Option<(&str, &str)>, &str, &str)] = &[
            // Lowercase when continuing a sentence.
            ("Meet on Friday.", Some(("we should", "")), " meet on Friday. ", "continues after a word"),
            ("Then we go.", Some(("first, ", "")), "then we go. ", "ignores trailing comma and space"),
            ("Then we go.", Some(("see (", "")), "then we go. ", "ignores an opening bracket"),
            ("Then we go.", Some(("item 3: ", "")), "then we go. ", "after a digit and colon"),
            ("Then we go.", Some(("He said \"", "")), "then we go. ", "after an opening quote, no space"),
            ("Then we go.", Some(("Done.", "")), " Then we go. ", "new sentence keeps capital"),
            ("Then we go.", Some(("Really?", "")), " Then we go. ", "after a question mark"),
            // Proper nouns and fixed forms keep their capital.
            ("NASA called.", Some(("then", "")), " NASA called. ", "acronym"),
            ("McDonald called.", Some(("then", "")), " McDonald called. ", "internal capital"),
            ("B2B sales.", Some(("then", "")), " B2B sales. ", "digit in word"),
            ("I think so.", Some(("well", "")), " I think so. ", "pronoun I"),
            ("I'm here.", Some(("well", "")), " I'm here. ", "contraction of I"),
            ("Monday works.", Some(("so", "")), " Monday works. ", "day name"),
            ("September works.", Some(("so", "")), " September works. ", "month name"),
            ("Éric came.", Some(("and", "")), " éric came. ", "accented letter has case"),
            ("3 apples.", Some(("buy", "")), " 3 apples. ", "digit first is untouched"),
            // Leading space.
            ("Hello.", Some(("", "")), "Hello. ", "start of line"),
            ("Hello.", Some(("word ", "")), "hello. ", "after whitespace"),
            ("Hello.", Some(("(", "")), "Hello. ", "after opening bracket only"),
            ("Hello.", Some(("end)", "")), " hello. ", "after closing bracket"),
            ("Hello.", Some(("\"quoted\"", "")), " hello. ", "after closing straight quote"),
            ("Hello.", Some(("50%", "")), " Hello. ", "after percent: space, no lowercase"),
            ("Hello.", Some(("a-", "")), "hello. ", "after hyphen no space"),
            // Final punctuation dropped mid-sentence.
            ("Meet on Friday.", Some(("we should", "tomorrow")), " meet on Friday ", "lowercase follows"),
            ("Meet on Friday!", Some(("we should", ", ok")), " meet on Friday", "comma follows"),
            ("Meet on Friday.", Some(("we should", ") ok")), " meet on Friday", "closing bracket follows"),
            ("Meet on Friday.", Some(("we should", "Tomorrow")), " meet on Friday. ", "capital follows keeps stop"),
            ("Meet on Friday.", Some(("we should", " tomorrow")), " meet on Friday.", "space follows: no trailing space"),
            // Trailing space.
            ("Second sentence.", Some(("First sentence.", "")), " Second sentence. ", "back to back"),
            ("Hello ", Some(("", "")), "Hello ", "already ends in space"),
            ("Hello\n", Some(("", "")), "Hello\n", "already ends in newline"),
            ("Hello.", Some(("", "9 more")), "Hello. ", "digit follows"),
            // Unknown context.
            ("Hello there.", None, "Hello there. ", "unknown adds trailing space only"),
            ("Hello there. ", None, "Hello there. ", "unknown keeps existing space"),
            ("", None, "", "empty text untouched"),
            // Arabic: no case changes, spacing still applies.
            ("مرحبا بكم.", Some(("قلت", "")), " مرحبا بكم. ", "arabic after arabic word"),
            ("مرحبا بكم.", Some(("", "")), "مرحبا بكم. ", "arabic at line start"),
            ("مرحبا بكم.", Some(("قلت", "و")), " مرحبا بكم. ", "arabic follows keeps stop"),
            // Mixed Latin/Arabic.
            ("Hello مرحبا.", Some(("قلت", "")), " hello مرحبا. ", "latin after arabic continues"),
            ("مرحبا Hello.", Some(("we said", "")), " مرحبا Hello. ", "arabic first word untouched"),
        ];
        for (text, context, expected, name) in cases {
            let context = context.map(|(before, after)| known(before, after));
            assert_eq!(shape(text, context.as_ref(), false), *expected, "{name}");
        }
    }

    #[test]
    fn context_keeps_only_the_bounded_current_line() {
        let context = CaretContext::from_raw("old line.\nnew", " rest\nnext line", true);
        assert_eq!(context, CaretContext { before: "new".into(), after: " rest".into(), selection: true });
        let long = "x".repeat(500);
        let context = CaretContext::from_raw(&long, &long, false);
        assert_eq!((context.before.len(), context.after.len()), (BEFORE_CHARS, AFTER_CHARS));
        assert_eq!(CaretContext::from_raw("a\r\n", "", false).before, "");
    }

    #[test]
    fn timely_metadata_remains_available_for_its_capture() {
        let probe = FocusProbe::start(Duration::from_millis(500), || FocusRead::Sensitive);
        assert_eq!(probe.collect(), FocusRead::Sensitive);
        assert_eq!(probe.collect(), FocusRead::Unknown);
    }

    #[test]
    fn late_answer_is_discarded_even_if_it_arrives_before_collection() {
        let (finished_send, finished) = mpsc::channel();
        let probe = FocusProbe::start(Duration::ZERO, move || {
            let _ = finished_send.send(());
            FocusRead::Sensitive
        });
        finished.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(probe.collect(), FocusRead::Unknown);
    }

    #[test]
    fn blocked_provider_does_not_delay_a_later_probe() {
        let (unblock, blocked) = mpsc::channel();
        let probe = FocusProbe::start(Duration::from_millis(20), move || {
            let _ = blocked.recv();
            FocusRead::Sensitive
        });
        assert_eq!(probe.collect(), FocusRead::Unknown);
        let next = FocusProbe::start(Duration::from_millis(500), || FocusRead::Sensitive);
        assert_eq!(next.collect(), FocusRead::Sensitive);
        let _ = unblock.send(());
    }
}
