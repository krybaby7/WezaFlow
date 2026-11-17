#[cfg(test)]
mod tests {
    use super::*;
    fn recording_near_limit() -> serde_json::Value {
        serde_json::json!({"session_id":"one","state":"recording","capture":{
            "terminal_code":null,"sample_count":4_640_000,"max_samples":4_800_000,
            "requested_sample_rate":16000,"actual_sample_rate":16000.0
        }})
    }
    #[test]
    fn recording_warning_begins_at_ten_seconds_remaining() {
        let mut reply = recording_near_limit();
        assert_eq!(runtime_message("one", &reply), Some("Recording ends soon."));
        reply["capture"]["sample_count"] = serde_json::json!(4_639_999);
        assert_eq!(runtime_message("one", &reply), None);
        reply["capture"]["sample_count"] = serde_json::json!(4_799_999);
        assert_eq!(runtime_message("one", &reply), Some("Recording ends soon."));
    }
    #[test]
    fn recording_warning_rejects_stale_cancelled_or_faulted_capture() {
        let mut reply = recording_near_limit();
        assert_eq!(runtime_message("old", &reply), None);
        reply["state"] = serde_json::json!("cancelled");
        assert_eq!(runtime_message("one", &reply), None);
        reply["state"] = serde_json::json!("recording");
        reply["capture"]["terminal_code"] = serde_json::json!("input_overflow");
        assert_eq!(runtime_message("one", &reply), None);
        reply["last_error"] = serde_json::json!("Microphone failed");
        assert_eq!(runtime_message("one", &reply), Some("Microphone failed"));
    }
    #[test]
    fn recording_warning_requires_valid_known_capture_bounds_and_rates() {
        for (field, invalid) in [
            ("sample_count", serde_json::json!(-1)),
            ("sample_count", serde_json::json!(4_800_000)),
            ("sample_count", serde_json::json!(4_800_001)),
            ("sample_count", serde_json::json!(4_640_000.5)),
            ("max_samples", serde_json::json!(0)),
            ("max_samples", serde_json::json!(4_800_001)),
            ("requested_sample_rate", serde_json::json!(8000)),
            ("actual_sample_rate", serde_json::json!(8000)),
            ("actual_sample_rate", serde_json::Value::Null),
            ("terminal_code", serde_json::json!(true)),
        ] {
            let mut reply = recording_near_limit();
            reply["capture"][field] = invalid;
            assert_eq!(runtime_message("one", &reply), None, "invalid {field}");
        }
        for field in ["sample_count", "max_samples", "requested_sample_rate", "actual_sample_rate", "terminal_code"] {
            let mut reply = recording_near_limit();
            reply["capture"].as_object_mut().unwrap().remove(field);
            assert_eq!(runtime_message("one", &reply), None, "missing {field}");
        }
    }
    #[test]
    fn duration_terminal_keeps_post_limit_message_instead_of_advance_warning() {
        let mut reply = recording_near_limit();
        reply["capture"]["terminal_code"] = serde_json::json!("duration_limit");
        assert_eq!(runtime_message("one", &reply), None);
        for state in ["draining", "processing", "ready", "no_speech"] {
            reply["state"] = serde_json::json!(state);
            assert_eq!(runtime_message("one", &reply), Some("Five-minute limit reached."));
        }
    }
    #[test]
    fn automatic_capture_finish_releases_without_extra_stop() {
        let mut c = SessionControl::default();
        c.press("key:1", "dictation", "first");
        let done = serde_json::json!({"session_id":"first","state":"ready","busy":false});
        assert!(c.observe("first", &done).unwrap().finished);
        assert!(c.release("key:1").is_none());
    }
    #[test]
    fn capture_warning_is_current_and_error_takes_priority() {
        let mut reply = serde_json::json!({"session_id":"one","state":"ready","capture":{"terminal_code":"duration_limit"}});
        assert_eq!(runtime_message("one", &reply), Some("Five-minute limit reached."));
        assert_eq!(runtime_message("old", &reply), None);
        reply["state"] = serde_json::json!("cancelled");
        assert_eq!(runtime_message("one", &reply), None);
        reply["last_error"] = serde_json::json!("Microphone failed");
        assert_eq!(runtime_message("one", &reply), Some("Microphone failed"));
    }
    #[test]
    fn release_is_retained_and_only_initiating_binding_owns_it() {
        let mut c = SessionControl::default();
        let start = c.press("keyboard:1", "dictation", "session").unwrap();
        assert!(c.press("mouse:X1", "dictation", "other").is_none());
        assert!(c.release("mouse:X1").is_none());
        let stop = c.release("keyboard:1").unwrap();
        assert_eq!((start.command_seq, stop.command_seq), (1, 2));
        assert_eq!(start.session_id, stop.session_id);
        assert!(c.release("keyboard:1").is_none());
        assert_eq!(c.cancel().unwrap().command_seq, 3);
        assert!(c.cancel().is_none());
    }
    fn ms(start: std::time::Instant, millis: u64) -> std::time::Instant {
        start + std::time::Duration::from_millis(millis)
    }
    #[test]
    fn hold_past_tap_threshold_stops_on_release() {
        let t = std::time::Instant::now();
        let mut g = PressGesture::default();
        assert_eq!(g.press("key:1", t), GestureAction::Start);
        assert_eq!(g.press("key:1", ms(t, 100)), GestureAction::Ignore); // auto-repeat
        assert_eq!(g.release("key:1", ms(t, 500)), GestureAction::Stop);
        assert!(g.binding().is_none() && g.deadline().is_none());
        assert_eq!(g.release("key:1", ms(t, 510)), GestureAction::Ignore);
    }
    #[test]
    fn short_tap_without_second_press_is_discarded_after_window() {
        let t = std::time::Instant::now();
        let mut g = PressGesture::default();
        g.press("mouse:X2", t);
        assert_eq!(g.release("mouse:X2", ms(t, 499)), GestureAction::Ignore);
        assert_eq!(g.deadline(), Some(ms(t, 999)));
        assert_eq!(g.expire(ms(t, 998)), GestureAction::Ignore);
        assert_eq!(g.expire(ms(t, 999)), GestureAction::DiscardTap);
        assert!(g.binding().is_none());
        assert_eq!(g.expire(ms(t, 2000)), GestureAction::Ignore);
        assert_eq!(g.press("mouse:X2", ms(t, 2000)), GestureAction::Start);
    }
    #[test]
    fn tap_then_press_locks_until_next_press_after_a_release() {
        let t = std::time::Instant::now();
        let mut g = PressGesture::default();
        g.press("key:1", t);
        g.release("key:1", ms(t, 100));
        assert_eq!(g.press("key:1", ms(t, 300)), GestureAction::Lock);
        assert!(g.deadline().is_none());
        assert_eq!(g.press("key:1", ms(t, 350)), GestureAction::Ignore); // auto-repeat
        assert_eq!(g.release("key:1", ms(t, 400)), GestureAction::Ignore);
        assert_eq!(g.expire(ms(t, 60_000)), GestureAction::Ignore);
        assert_eq!(g.release("key:1", ms(t, 60_000)), GestureAction::Ignore);
        assert_eq!(g.press("key:1", ms(t, 60_000)), GestureAction::Stop);
        assert_eq!(g.release("key:1", ms(t, 60_100)), GestureAction::Ignore);
        assert!(g.binding().is_none());
    }
    #[test]
    fn other_bindings_cannot_advance_a_gesture() {
        let t = std::time::Instant::now();
        let mut g = PressGesture::default();
        g.press("key:1", t);
        assert_eq!(g.press("mouse:X1", ms(t, 10)), GestureAction::Ignore);
        assert_eq!(g.release("mouse:X1", ms(t, 20)), GestureAction::Ignore);
        g.release("key:1", ms(t, 30));
        assert_eq!(g.press("mouse:X1", ms(t, 40)), GestureAction::Ignore);
        assert!(g.deadline().is_some());
        g.reset();
        assert_eq!(g.press("mouse:X1", ms(t, 50)), GestureAction::Start);
    }
    #[test]
    fn gestures_drive_session_control_through_owner_scoped_commands() {
        let t = std::time::Instant::now();
        let mut g = PressGesture::default();
        let mut c = SessionControl::default();
        assert_eq!(g.press("key:1", t), GestureAction::Start);
        c.press("key:1", "dictation", "locked");
        g.release("key:1", ms(t, 100));
        g.press("key:1", ms(t, 200));
        g.release("key:1", ms(t, 300));
        // Locked recording: the release was withheld, so the owner still stops it.
        assert!(c.release("mouse:X1").is_none());
        assert_eq!(g.press("key:1", ms(t, 5000)), GestureAction::Stop);
        assert_eq!(c.release("key:1").unwrap().action, "stop");
        assert!(c.release("key:1").is_none());
        c.finish("locked");
        g.press("key:1", ms(t, 6000));
        c.press("key:1", "dictation", "tapped");
        g.release("key:1", ms(t, 6100));
        assert_eq!(g.expire(ms(t, 6600)), GestureAction::DiscardTap);
        assert_eq!(c.cancel().unwrap().action, "cancel");
        assert!(!c.may_commit("tapped"));
    }
    #[test]
    fn stale_identity_and_status_cannot_commit() {
        let a = EngineIdentity {
            token: "secret".into(),
            engine_id: "old".into(),
        };
        let b = EngineIdentity {
            token: "new-secret".into(),
            engine_id: "new".into(),
        };
        let reply =
            serde_json::json!({"application_id":"wezza","protocol_version":1,"engine_id":"old"});
        assert!(validate_reply(&a, Some(&a), &reply).is_ok());
        assert!(validate_reply(&a, Some(&b), &reply).is_err());
        let mut c = SessionControl::default();
        c.press("one", "dictation", "first");
        assert!(!c.finish("old"));
        assert!(c.finish("first"));
        c.press("two", "dictation", "second");
        assert!(!c.finish("first"));
    }
    #[test]
    fn observations_are_generation_scoped_and_terminal_commit_is_once() {
        let mut c = SessionControl::default();
        c.press("key:1", "dictation", "first");
        let busy = serde_json::json!({"session_id":"first","state":"cancelled","busy":true});
        assert!(!c.observe("first", &busy).unwrap().finished);
        assert!(c.press("key:2", "dictation", "second").is_none());
        let done = serde_json::json!({"session_id":"first","state":"ready","busy":false});
        assert!(c.observe("first", &done).unwrap().finished);
        assert!(c.observe("first", &done).is_none());
        c.press("key:2", "dictation", "second");
        assert!(c.observe("first", &done).is_none());
        assert_eq!(c.active.unwrap().session_id, "second");
    }
    #[test]
    fn uncertain_transport_failure_retains_owner_and_retries_same_cancel() {
        let mut c = SessionControl::default();
        c.press("key:1", "dictation", "first");
        let cancel = c.failure_cancel("first").unwrap();
        assert_eq!(cancel.action, "cancel");
        assert_eq!(
            c.failure_cancel("first").unwrap().command_seq,
            cancel.command_seq
        );
        assert!(c.failure_cancel("old").is_none());
        assert!(c.press("key:2", "dictation", "second").is_none());
    }
    #[test]
    fn relative_api_queries_allow_encoded_values_without_url_redirection() {
        assert!(protected_path(
            "/vocabulary/corrections?original=hello%20world&corrected=%D8%A7%2Fbonjour"
        )
        .is_ok());
        assert!(protected_path("/runtime/diagnostics?limit=10").is_ok());
        for path in [
            "https://evil.invalid/",
            "//evil.invalid/",
            "/runtime/../health",
            "/runtime/%2e%2e/health",
            "/status#fragment",
            "/status\\evil",
            "/status\r\nInjected: secret",
        ] {
            assert!(protected_path(path).is_err(), "{path}");
        }
    }
    #[test]
    fn handshake_requires_serialized_recoverable_insertion_disabled_capabilities() {
        let valid = serde_json::json!({"capabilities":["serialized-sessions-v1","recoverable-results","insertion-disabled"]});
        assert!(validate_handshake(&valid).is_ok());
        assert!(
            validate_handshake(&serde_json::json!({"capabilities":["runtime-warmup"]})).is_err()
        );
        assert!(validate_handshake(&serde_json::json!({"capabilities":[]})).is_err());
    }
    #[test]
    fn rejected_cancel_after_uncertain_stop_observes_ready_and_releases_owner() {
        let mut control = SessionControl::default();
        control.press("key:1", "dictation", "session");
        control.release("key:1");
        control.failure_cancel("session");
        let reply = serde_json::json!({"accepted":false,"reason":"not_active","session_id":"session","state":"ready","busy":false,"result":{"text":"recoverable"}});
        assert_eq!(
            command_reply_decision("session", &reply),
            CommandReplyDecision::Observe { terminal: true }
        );
        assert!(control.observe("session", &reply).unwrap().finished);
        assert!(control.active.is_none());
        assert!(control.failure_cancel("session").is_none());
        assert_eq!(reply["result"]["text"], "recoverable");
    }
    #[test]
    fn rejected_already_cancelled_busy_reply_observes_until_worker_finishes() {
        let mut control = SessionControl::default();
        control.press("key:1", "dictation", "session");
        control.cancel();
        let mut reply = serde_json::json!({"accepted":false,"reason":"not_active","session_id":"session","state":"cancelled","busy":true});
        assert_eq!(
            command_reply_decision("session", &reply),
            CommandReplyDecision::Observe { terminal: true }
        );
        assert!(!control.observe("session", &reply).unwrap().finished);
        assert!(control.active.is_some());
        reply["busy"] = serde_json::json!(false);
        assert_eq!(
            command_reply_decision("session", &reply),
            CommandReplyDecision::Observe { terminal: true }
        );
        assert!(control.observe("session", &reply).unwrap().finished);
        assert!(control.active.is_none());
    }
    #[test]
    fn dispatcher_lost_start_keeps_owner_through_idle_status_until_cancel_rejection() {
        let mut c = SessionControl::default();
        c.press("key:1", "dictation", "session");
        let lost = c.settle_transport_failure("session");
        assert!(lost.pending);
        let cancel = lost.retry_cancel.unwrap();
        let idle = serde_json::json!({"session_id":null,"state":"idle","busy":false});
        let unknown = c.settle_status("session", &idle);
        assert!(unknown.pending);
        assert_eq!(
            unknown.retry_cancel.unwrap().command_seq,
            cancel.command_seq
        );
        assert!(c.press("key:2", "dictation", "second").is_none());
        let rejected = serde_json::json!({"accepted":false,"reason":"stale_session","session_id":null,"state":"idle","busy":false});
        let done = c.settle_command("session", &rejected);
        assert!(done.rejected && !done.pending && done.retry_cancel.is_none());
        assert!(c.active.is_none());
        assert!(c.press("key:2", "dictation", "second").is_some());
        // A late result from the cancelled generation cannot settle the new owner.
        assert!(!c.settle_command("session", &rejected).pending);
        assert_eq!(c.active.as_ref().unwrap().session_id, "second");
    }
    #[test]
    fn dispatcher_lost_stop_then_rejected_cancel_preserves_ready_result() {
        let mut c = SessionControl::default();
        c.press("key:1", "dictation", "session");
        c.release("key:1");
        assert!(c.settle_transport_failure("session").retry_cancel.is_some());
        let ready = serde_json::json!({"accepted":false,"reason":"not_active","session_id":"session","state":"ready","busy":false,"result":{"text":"recovered"}});
        let done = c.settle_command("session", &ready);
        assert!(!done.pending && done.retry_cancel.is_none());
        assert_eq!(done.observation.unwrap().state, "ready");
        assert!(c.active.is_none());
        assert_eq!(ready["result"]["text"], "recovered");
    }
    #[test]
    fn dispatcher_lost_cancel_and_transient_status_failure_eventually_settle() {
        let mut c = SessionControl::default();
        c.press("key:1", "dictation", "session");
        c.release("key:1");
        let cancel = c.cancel().unwrap();
        assert_eq!(
            c.settle_transport_failure("session")
                .retry_cancel
                .unwrap()
                .command_seq,
            cancel.command_seq
        );
        let busy = serde_json::json!({"session_id":"session","state":"cancelled","busy":true});
        let waiting = c.settle_status("session", &busy);
        assert!(waiting.pending && waiting.retry_cancel.is_none());
        assert!(c.settle_transport_failure("session").pending);
        let recovered = c.settle_status("session", &busy);
        assert!(recovered.pending && recovered.retry_cancel.is_none());
        let idle = serde_json::json!({"session_id":"session","state":"cancelled","busy":false});
        let done = c.settle_status("session", &idle);
        assert!(!done.pending && done.retry_cancel.is_none());
        assert!(c.active.is_none());
    }
    #[test]
    fn in_flight_reply_from_restarted_engine_is_rejected_and_stale_request_never_sends() {
        use std::cell::{Cell, RefCell};
        let old = EngineIdentity {
            engine_id: "old".into(),
            token: "old-secret".into(),
        };
        let new = EngineIdentity {
            engine_id: "new".into(),
            token: "new-secret".into(),
        };
        let current = RefCell::new(Some(old.clone()));
        let sends = Cell::new(0);
        let reply =
            serde_json::json!({"application_id":"wezza","protocol_version":1,"engine_id":"old"});
        let result = tauri::async_runtime::block_on(owned_request(
            &old,
            || Ok(current.borrow().clone()),
            async {
                sends.set(sends.get() + 1);
                *current.borrow_mut() = Some(new.clone());
                Ok(reply.clone())
            },
        ));
        assert!(result.is_err());
        assert_eq!(sends.get(), 1);
        let result = tauri::async_runtime::block_on(owned_request(
            &old,
            || Ok(current.borrow().clone()),
            async {
                sends.set(sends.get() + 1);
                Ok(reply)
            },
        ));
        assert!(result.is_err());
        assert_eq!(sends.get(), 1);
    }
}
#[derive(Clone, PartialEq, Eq)]
pub(super) struct EngineIdentity {
    pub token: String,
    pub engine_id: String,
}
pub(super) fn validate_reply(
    sent: &EngineIdentity,
    current: Option<&EngineIdentity>,
    reply: &serde_json::Value,
) -> Result<(), String> {
    if current != Some(sent) {
        return Err("Engine instance changed; discarded stale response.".into());
    }
    if reply["application_id"] != "wezza"
        || reply["protocol_version"] != 1
        || reply["engine_id"] != sent.engine_id
    {
        return Err("Engine identity or protocol mismatch.".into());
    }
    Ok(())
}
#[derive(Clone)]
pub(super) struct SessionCommand {
    pub command_seq: u64,
    pub session_id: String,
    pub binding_id: String,
    pub mode: String,
    pub action: &'static str,
}
#[derive(Default)]
pub(super) struct SessionControl {
    pub active: Option<SessionCommand>,
    seq: u64,
    released: bool,
    cancelled: bool,
    cancel_command: Option<SessionCommand>,
}
impl SessionControl {
    pub fn may_commit(&self, id: &str) -> bool {
        self.active.as_ref().is_some_and(|c| c.session_id == id) && !self.cancelled
    }
    pub fn press(&mut self, binding: &str, mode: &str, id: &str) -> Option<SessionCommand> {
        if self.active.is_some() {
            return None;
        }
        self.seq += 1;
        self.released = false;
        self.cancelled = false;
        self.cancel_command = None;
        let command = SessionCommand {
            command_seq: self.seq,
            session_id: id.into(),
            binding_id: binding.into(),
            mode: mode.into(),
            action: "start",
        };
        self.active = Some(command.clone());
        Some(command)
    }
    fn command(&mut self, action: &'static str) -> Option<SessionCommand> {
        let mut command = self.active.clone()?;
        self.seq += 1;
        command.command_seq = self.seq;
        command.action = action;
        Some(command)
    }
    pub fn release(&mut self, binding: &str) -> Option<SessionCommand> {
        if self.active.as_ref()?.binding_id != binding || self.released || self.cancelled {
            return None;
        }
        self.released = true;
        self.command("stop")
    }
    pub fn cancel(&mut self) -> Option<SessionCommand> {
        if self.cancelled {
            return None;
        }
        self.cancelled = true;
        let command = self.command("cancel");
        self.cancel_command = command.clone();
        command
    }
    pub fn finish(&mut self, id: &str) -> bool {
        if self.active.as_ref().map(|c| c.session_id.as_str()) != Some(id) {
            return false;
        }
        self.active = None;
        true
    }
}

/// A release sooner than this after the press is a tap, not a dictation.
pub(super) const TAP_THRESHOLD: std::time::Duration = std::time::Duration::from_millis(500);
/// After a tap, a second press within this window locks hands-free recording.
pub(super) const LOCK_WINDOW: std::time::Duration = std::time::Duration::from_millis(500);

#[derive(Debug, PartialEq, Eq)]
pub(super) enum GestureAction {
    Ignore,
    Start,
    Stop,
    DiscardTap,
    /// Recording continues hands-free; no session command is sent.
    Lock,
}

#[derive(Clone, Copy, Default)]
enum GesturePhase {
    #[default]
    Idle,
    Held(std::time::Instant),
    AwaitingLock(std::time::Instant),
    /// `released` turns true on the first release after locking, so key
    /// auto-repeat of the locking press cannot end the recording.
    Locked { released: bool },
}

/// Turns one binding's presses and releases into session commands: hold to
/// talk, a short tap is discarded, and tap then press locks hands-free. Only
/// the binding that started the gesture can advance it.
#[derive(Default)]
pub(super) struct PressGesture {
    binding: Option<String>,
    phase: GesturePhase,
}
impl PressGesture {
    pub fn binding(&self) -> Option<&str> {
        self.binding.as_deref()
    }
    pub fn deadline(&self) -> Option<std::time::Instant> {
        match self.phase {
            GesturePhase::AwaitingLock(deadline) => Some(deadline),
            _ => None,
        }
    }
    pub fn reset(&mut self) {
        *self = Self::default();
    }
    pub fn press(&mut self, binding: &str, now: std::time::Instant) -> GestureAction {
        if matches!(self.phase, GesturePhase::Idle) {
            self.binding = Some(binding.into());
            self.phase = GesturePhase::Held(now);
            return GestureAction::Start;
        }
        if self.binding.as_deref() != Some(binding) {
            return GestureAction::Ignore;
        }
        match self.phase {
            GesturePhase::AwaitingLock(_) => {
                self.phase = GesturePhase::Locked { released: false };
                GestureAction::Lock
            }
            GesturePhase::Locked { released: true } => {
                self.reset();
                GestureAction::Stop
            }
            _ => GestureAction::Ignore,
        }
    }
    pub fn release(&mut self, binding: &str, now: std::time::Instant) -> GestureAction {
        if self.binding.as_deref() != Some(binding) {
            return GestureAction::Ignore;
        }
        match self.phase {
            GesturePhase::Held(pressed) if now.saturating_duration_since(pressed) < TAP_THRESHOLD => {
                self.phase = GesturePhase::AwaitingLock(now + LOCK_WINDOW);
                GestureAction::Ignore
            }
            GesturePhase::Held(_) => {
                self.reset();
                GestureAction::Stop
            }
            GesturePhase::Locked { .. } => {
                self.phase = GesturePhase::Locked { released: true };
                GestureAction::Ignore
            }
            _ => GestureAction::Ignore,
        }
    }
    pub fn expire(&mut self, now: std::time::Instant) -> GestureAction {
        match self.phase {
            GesturePhase::AwaitingLock(deadline) if now >= deadline => {
                self.reset();
                GestureAction::DiscardTap
            }
            _ => GestureAction::Ignore,
        }
    }
}

pub(super) struct AcknowledgedState {
    pub state: String,
    pub mode: String,
    pub finished: bool,
}
impl SessionControl {
    pub fn observe(&mut self, id: &str, reply: &serde_json::Value) -> Option<AcknowledgedState> {
        let active = self.active.as_ref()?;
        if active.session_id != id || reply["session_id"].as_str() != Some(id) {
            return None;
        }
        let state = reply["state"].as_str()?.to_string();
        if !matches!(
            state.as_str(),
            "starting"
                | "recording"
                | "draining"
                | "processing"
                | "ready"
                | "no_speech"
                | "cancelled"
                | "error"
        ) {
            return None;
        }
        let finished = matches!(
            state.as_str(),
            "ready" | "no_speech" | "cancelled" | "error"
        ) && reply["busy"] == false;
        let result = AcknowledgedState {
            state,
            mode: active.mode.clone(),
            finished,
        };
        if finished {
            self.finish(id);
        }
        Some(result)
    }
}
pub(super) async fn owned_request<
    F: std::future::Future<Output = Result<serde_json::Value, String>>,
>(
    identity: &EngineIdentity,
    current: impl Fn() -> Result<Option<EngineIdentity>, String>,
    send: F,
) -> Result<serde_json::Value, String> {
    if current()?.as_ref() != Some(identity) {
        return Err("Engine instance changed before dispatch.".into());
    }
    let reply = send.await?;
    validate_reply(identity, current()?.as_ref(), &reply)?;
    Ok(reply)
}

impl SessionControl {
    pub fn failure_cancel(&mut self, id: &str) -> Option<SessionCommand> {
        if self.active.as_ref()?.session_id != id {
            return None;
        }
        if let Some(command) = self.cancel_command.as_ref() {
            return Some(command.clone());
        }
        self.cancel()
    }
}

pub(super) fn protected_path(path: &str) -> Result<(), String> {
    let route = path.split('?').next().unwrap_or("");
    let normalized = route.to_ascii_lowercase();
    if !route.starts_with('/')
        || route.starts_with("//")
        || path.contains(['\\', '#'])
        || path.chars().any(char::is_control)
        || route == "/health"
        || route.split('/').any(|s| matches!(s, "." | ".."))
        || ["%2e", "%2f", "%5c"].iter().any(|s| normalized.contains(s))
    {
        return Err("Invalid protected API path.".into());
    }
    Ok(())
}

pub(super) fn validate_handshake(reply: &serde_json::Value) -> Result<(), String> {
    let Some(capabilities) = reply["capabilities"].as_array() else {
        return Err("Engine handshake has no capabilities.".into());
    };
    if [
        "serialized-sessions-v1",
        "recoverable-results",
        "insertion-disabled",
    ]
    .iter()
    .any(|required| !capabilities.iter().any(|c| c.as_str() == Some(required)))
    {
        return Err("Engine does not support the required safe session protocol.".into());
    }
    Ok(())
}

#[derive(Debug, PartialEq)]
pub(super) enum CommandReplyDecision {
    Observe { terminal: bool },
    Reject,
    RetryCancel,
}
pub(super) fn command_reply_decision(id: &str, reply: &serde_json::Value) -> CommandReplyDecision {
    let terminal = matches!(
        reply["state"].as_str(),
        Some("ready" | "no_speech" | "cancelled" | "error")
    );
    if reply["session_id"].as_str() == Some(id) && terminal {
        return CommandReplyDecision::Observe { terminal: true };
    }
    if reply["accepted"] == false {
        if reply["session_id"].as_str() != Some(id) {
            CommandReplyDecision::Reject
        } else {
            CommandReplyDecision::RetryCancel
        }
    } else {
        CommandReplyDecision::Observe { terminal: false }
    }
}

#[derive(Default)]
pub(super) struct DispatchDecision {
    pub observation: Option<AcknowledgedState>,
    pub retry_cancel: Option<SessionCommand>,
    pub pending: bool,
    pub rejected: bool,
}
impl SessionControl {
    fn owns(&self, id: &str) -> bool {
        self.active.as_ref().map(|c| c.session_id.as_str()) == Some(id)
    }
    fn settle_observation(&mut self, id: &str, reply: &serde_json::Value) -> DispatchDecision {
        let Some(observation) = self.observe(id, reply) else {
            return self.settle_transport_failure(id);
        };
        let terminal = matches!(
            observation.state.as_str(),
            "ready" | "no_speech" | "cancelled" | "error"
        );
        DispatchDecision {
            pending: !observation.finished,
            retry_cancel: if terminal {
                None
            } else {
                self.cancel_command.clone()
            },
            observation: Some(observation),
            rejected: false,
        }
    }
    pub fn settle_command(&mut self, id: &str, reply: &serde_json::Value) -> DispatchDecision {
        if !self.owns(id) {
            return DispatchDecision::default();
        }
        match command_reply_decision(id, reply) {
            CommandReplyDecision::Observe { .. } => self.settle_observation(id, reply),
            CommandReplyDecision::RetryCancel => self.settle_transport_failure(id),
            CommandReplyDecision::Reject => {
                self.finish(id);
                DispatchDecision {
                    rejected: true,
                    ..Default::default()
                }
            }
        }
    }
    pub fn settle_status(&mut self, id: &str, reply: &serde_json::Value) -> DispatchDecision {
        if !self.owns(id) {
            return DispatchDecision::default();
        }
        // A null/other session status is observational. An earlier request can
        // still arrive, so retain ownership until cancellation is acknowledged.
        self.settle_observation(id, reply)
    }
    pub fn settle_transport_failure(&mut self, id: &str) -> DispatchDecision {
        if !self.owns(id) {
            return DispatchDecision::default();
        }
        DispatchDecision {
            pending: true,
            retry_cancel: self.failure_cancel(id),
            ..Default::default()
        }
    }
}

/// Selects only a warning belonging to the capture being observed.
pub(super) fn runtime_message<'a>(id: &str, reply: &'a serde_json::Value) -> Option<&'a str> {
    if let Some(error) = reply["last_error"].as_str() {
        return Some(error);
    }
    if reply["session_id"].as_str() != Some(id) {
        return None;
    }
    let capture = &reply["capture"];
    if reply["state"].as_str() == Some("recording")
        && capture.get("terminal_code").is_some_and(serde_json::Value::is_null)
        && capture["requested_sample_rate"].as_u64() == Some(16_000)
        && capture["actual_sample_rate"].as_f64() == Some(16_000.0)
    {
        if let (Some(count), Some(cap)) = (
            capture["sample_count"].as_u64(),
            capture["max_samples"].as_u64(),
        ) {
            if count < cap && cap <= 4_800_000 && cap - count <= 160_000 {
                return Some("Recording ends soon.");
            }
        }
    }
    if matches!(reply["state"].as_str(), Some("draining" | "processing" | "ready" | "no_speech"))
        && reply["capture"]["terminal_code"].as_str() == Some("duration_limit")
    {
        return Some("Five-minute limit reached.");
    }
    None
}
