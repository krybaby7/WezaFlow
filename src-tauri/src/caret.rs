//! Fits a dictation to the text around the caret before it is pasted. The
//! surrounding text is private: it is never logged, never sent to the engine,
//! and is dropped once the dictation has been shaped.
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    mpsc, Arc, Mutex,
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum CaretRead {
    Unknown,
    Password,
    Known(CaretContext),
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
pub(super) fn shape(text: &str, context: Option<&CaretContext>) -> String {
    if text.trim().is_empty() {
        return text.to_string();
    }
    let ends_in_space = text.ends_with(char::is_whitespace);
    let Some(context) = context else {
        return if ends_in_space { text.to_string() } else { format!("{text} ") };
    };
    let mut shaped = if continues_sentence(&context.before) {
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

/// Reads the caret context of the focused element. Implementations may block
/// inside another process; callers bound them with `CaretReads`.
pub(super) trait CaretReader: Send + Sync + 'static {
    fn read(&self, target_pid: u32) -> CaretRead;
}

struct PendingRead {
    session_id: String,
    deadline: Instant,
    result: mpsc::Receiver<CaretRead>,
}

/// One caret read per session, each on its own thread so a read stuck in
/// another process never delays a later dictation.
pub(super) struct CaretReads {
    reader: Arc<dyn CaretReader>,
    budget: Duration,
    pending: Mutex<Option<PendingRead>>,
    in_flight: Arc<AtomicUsize>,
}

impl Default for CaretReads {
    fn default() -> Self {
        Self::new(Arc::new(SystemCaretReader), CARET_READ_BUDGET)
    }
}

impl CaretReads {
    pub fn new(reader: Arc<dyn CaretReader>, budget: Duration) -> Self {
        Self { reader, budget, pending: Mutex::new(None), in_flight: Arc::new(AtomicUsize::new(0)) }
    }

    /// Starts reading for `session_id`, replacing any earlier read.
    pub fn start(&self, session_id: &str, target_pid: u32) {
        let (sender, result) = mpsc::channel();
        let read = PendingRead { session_id: session_id.into(), deadline: Instant::now() + self.budget, result };
        if let Ok(mut pending) = self.pending.lock() {
            *pending = Some(read);
        }
        if self.in_flight.fetch_add(1, Ordering::SeqCst) >= MAX_STUCK_READS {
            self.in_flight.fetch_sub(1, Ordering::SeqCst);
            return; // Dropping the sender makes the read unknown.
        }
        let reader = Arc::clone(&self.reader);
        let in_flight = Arc::clone(&self.in_flight);
        let spawned = std::thread::Builder::new().name("caret-read".into()).spawn(move || {
            let read = reader.read(target_pid);
            in_flight.fetch_sub(1, Ordering::SeqCst);
            let _ = sender.send(read);
        });
        if spawned.is_err() {
            self.in_flight.fetch_sub(1, Ordering::SeqCst);
        }
    }

    /// Waits only for what is left of the budget. A read that belongs to
    /// another session is never returned.
    pub fn collect(&self, session_id: &str) -> CaretRead {
        let read = {
            let Ok(mut pending) = self.pending.lock() else { return CaretRead::Unknown };
            if pending.as_ref().map(|p| p.session_id.as_str()) != Some(session_id) {
                return CaretRead::Unknown;
            }
            pending.take()
        };
        let Some(read) = read else { return CaretRead::Unknown };
        read.result
            .recv_timeout(read.deadline.saturating_duration_since(Instant::now()))
            .unwrap_or(CaretRead::Unknown)
    }
}

struct SystemCaretReader;

#[cfg(windows)]
impl CaretReader for SystemCaretReader {
    fn read(&self, target_pid: u32) -> CaretRead {
        unsafe { uia_read(target_pid) }.unwrap_or(CaretRead::Unknown)
    }
}

#[cfg(not(windows))]
impl CaretReader for SystemCaretReader {
    fn read(&self, _target_pid: u32) -> CaretRead {
        CaretRead::Unknown
    }
}

#[cfg(windows)]
unsafe fn uia_read(target_pid: u32) -> windows::core::Result<CaretRead> {
    use windows::core::Interface;
    use windows::Win32::{
        System::Com::{CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED},
        UI::Accessibility::*,
    };
    let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
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
    if element.CurrentProcessId()? as u32 != target_pid {
        return Ok(CaretRead::Unknown);
    }
    if element.CurrentIsPassword()?.as_bool() {
        return Ok(CaretRead::Password);
    }
    let Ok(pattern) = element.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId) else {
        return Ok(CaretRead::Unknown);
    };
    let ranges = pattern.GetSelection()?;
    if ranges.Length()? < 1 {
        return Ok(CaretRead::Unknown);
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
    Ok(CaretRead::Known(CaretContext::from_raw(&before, &after, selection)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn known(before: &str, after: &str) -> CaretContext {
        CaretContext::from_raw(before, after, false)
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
            assert_eq!(shape(text, context.as_ref()), *expected, "{name}");
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

    struct FakeReader {
        delay: Duration,
        answer: CaretRead,
        calls: AtomicUsize,
        stuck_first: bool,
    }
    impl FakeReader {
        fn new(delay_ms: u64, answer: CaretRead) -> Arc<Self> {
            Arc::new(Self { delay: Duration::from_millis(delay_ms), answer, calls: AtomicUsize::new(0), stuck_first: false })
        }
    }
    impl CaretReader for FakeReader {
        fn read(&self, target_pid: u32) -> CaretRead {
            let first = self.calls.fetch_add(1, Ordering::SeqCst) == 0;
            if self.stuck_first && first {
                std::thread::sleep(Duration::from_secs(5));
            } else {
                std::thread::sleep(self.delay);
            }
            if target_pid == 0 { CaretRead::Unknown } else { self.answer.clone() }
        }
    }
    fn answer() -> CaretRead {
        CaretRead::Known(known("we should", ""))
    }

    #[test]
    fn read_in_time_is_returned_for_its_session() {
        let reads = CaretReads::new(FakeReader::new(0, answer()), Duration::from_millis(500));
        reads.start("one", 42);
        assert_eq!(reads.collect("one"), answer());
        assert_eq!(reads.collect("one"), CaretRead::Unknown);
    }

    #[test]
    fn late_read_is_unknown_within_the_budget() {
        let reads = CaretReads::new(FakeReader::new(400, answer()), Duration::from_millis(40));
        reads.start("one", 42);
        let started = Instant::now();
        assert_eq!(reads.collect("one"), CaretRead::Unknown);
        assert!(started.elapsed() < Duration::from_millis(300));
    }

    #[test]
    fn failed_read_and_password_are_reported() {
        let reads = CaretReads::new(FakeReader::new(0, answer()), Duration::from_millis(500));
        reads.start("one", 0); // fake failure
        assert_eq!(reads.collect("one"), CaretRead::Unknown);
        let reads = CaretReads::new(FakeReader::new(0, CaretRead::Password), Duration::from_millis(500));
        reads.start("one", 42);
        assert_eq!(reads.collect("one"), CaretRead::Password);
    }

    #[test]
    fn read_for_another_session_is_never_applied() {
        let reads = CaretReads::new(FakeReader::new(0, answer()), Duration::from_millis(500));
        assert_eq!(reads.collect("one"), CaretRead::Unknown);
        reads.start("old", 42);
        assert_eq!(reads.collect("new"), CaretRead::Unknown);
        reads.start("newer", 42);
        assert_eq!(reads.collect("old"), CaretRead::Unknown);
        assert_eq!(reads.collect("newer"), answer());
    }

    #[test]
    fn stuck_read_does_not_delay_the_next_dictation() {
        let reader = Arc::new(FakeReader { delay: Duration::ZERO, answer: answer(), calls: AtomicUsize::new(0), stuck_first: true });
        let reads = CaretReads::new(reader, Duration::from_millis(50));
        reads.start("one", 42);
        assert_eq!(reads.collect("one"), CaretRead::Unknown);
        reads.start("two", 42);
        let started = Instant::now();
        assert_eq!(reads.collect("two"), answer());
        assert!(started.elapsed() < Duration::from_millis(200));
    }
}
