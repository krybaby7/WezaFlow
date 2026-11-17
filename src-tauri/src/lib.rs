use std::{
    fs::{self, File, OpenOptions},
    net::TcpStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    str::FromStr,
    sync::{mpsc, Mutex},
    thread,
    time::{Duration, Instant},
};

use serde::{de::Error as DeError, Deserialize, Deserializer, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    App, AppHandle, Emitter, Manager, PhysicalPosition, WebviewWindow, Window, WindowEvent,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const RUNTIME_BASE_URL: &str = "http://127.0.0.1:8765";
const RUNTIME_SOCKET_ADDR: &str = "127.0.0.1:8765";
const RECORDING_BAR_LABEL: &str = "recording-bar";
const RECORDING_BAR_EVENT: &str = "recording-bar-state";
const RECORDING_BAR_WIDTH: i32 = 300;
const RECORDING_BAR_HEIGHT: i32 = 72;
const RECORDING_BAR_BOTTOM_MARGIN: i32 = 72;
use session::{
    owned_request, protected_path, validate_handshake, validate_reply, DispatchDecision,
    EngineIdentity, GestureAction, PressGesture, SessionCommand, SessionControl,
};
use std::sync::atomic::{AtomicBool, Ordering};
#[cfg(not(windows))]
use std::io::Read;

#[cfg(windows)]
use std::{
    os::windows::process::CommandExt,
    sync::{Once, OnceLock},
};
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{LPARAM, LRESULT, WPARAM},
    UI::WindowsAndMessaging::{
        CallNextHookEx, DispatchMessageW, GetMessageW, SetWindowsHookExW, TranslateMessage, MSG,
        MSLLHOOKSTRUCT, WH_MOUSE_LL, WM_XBUTTONDOWN, WM_XBUTTONUP, XBUTTON1, XBUTTON2,
    },
};

#[cfg(windows)]
use windows::Win32::{
    Media::Audio::{
        eConsole, eRender, Endpoints::IAudioEndpointVolume, IMMDeviceEnumerator, MMDeviceEnumerator,
    },
    System::Com::{CoCreateInstance, CoInitializeEx, CLSCTX_ALL, COINIT_MULTITHREADED},
};

#[cfg(windows)]
static START_MOUSE_HOOK: Once = Once::new();
#[cfg(windows)]
static MOUSE_HOOK_APP: OnceLock<AppHandle> = OnceLock::new();

/// Physical input observed by the OS hooks. The hooks only post these; one
/// worker thread turns them into session commands.
enum InputEvent {
    Shortcut { shortcut: Shortcut, pressed: bool },
    Mouse { button: MouseButtonBinding, pressed: bool },
}

static INPUT_EVENTS: std::sync::OnceLock<mpsc::Sender<InputEvent>> = std::sync::OnceLock::new();

// Read by the low-level mouse hook, which must not take locks. Published
// whenever the registered hotkeys change, including when they are cleared.
static MOUSE_X1_BOUND: AtomicBool = AtomicBool::new(false);
static MOUSE_X2_BOUND: AtomicBool = AtomicBool::new(false);

fn bound_mouse_buttons(hotkeys: &RegisteredHotkeys) -> (bool, bool) {
    let bound = |button| {
        hotkeys.dictation.mouse.contains(&button) || hotkeys.command_mode.mouse.contains(&button)
    };
    (bound(MouseButtonBinding::X1), bound(MouseButtonBinding::X2))
}

fn publish_mouse_bindings(hotkeys: &RegisteredHotkeys) {
    let (x1, x2) = bound_mouse_buttons(hotkeys);
    MOUSE_X1_BOUND.store(x1, Ordering::Relaxed);
    MOUSE_X2_BOUND.store(x2, Ordering::Relaxed);
}

fn post_input_event(event: InputEvent) {
    if let Some(sender) = INPUT_EVENTS.get() {
        let _ = sender.send(event);
    }
}

/// Keeps the Python engine tree inside a kill-on-close job, so an engine can
/// never outlive this shell (crash, hang or forced close) and block port 8765.
#[cfg(windows)]
struct RuntimeJob(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
unsafe impl Send for RuntimeJob {}
#[cfg(windows)]
unsafe impl Sync for RuntimeJob {}

#[cfg(windows)]
static RUNTIME_JOB: OnceLock<Option<RuntimeJob>> = OnceLock::new();

#[cfg(windows)]
fn contain_runtime_child(child: &Child) -> bool {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{Foundation::CloseHandle, System::JobObjects::*};
    let job = RUNTIME_JOB.get_or_init(|| unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return None;
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
        {
            CloseHandle(job);
            return None;
        }
        // Never closed explicitly: the OS closes it when this process ends.
        Some(RuntimeJob(job))
    });
    match job {
        Some(job) => unsafe { AssignProcessToJobObject(job.0, child.as_raw_handle() as _) != 0 },
        None => false,
    }
}

#[cfg(not(windows))]
fn contain_runtime_child(_child: &Child) -> bool {
    false
}

/// Runs a task on the UI thread and waits for it. Callers must not hold a lock
/// the UI thread can wait on; the UI thread itself never takes shell locks.
fn on_main_thread<T: Send + 'static>(
    app: &AppHandle,
    task: impl FnOnce(&AppHandle) -> T + Send + 'static,
) -> Result<T, String> {
    let (sender, receiver) = mpsc::channel();
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let _ = sender.send(task(&handle));
    })
    .map_err(|err| err.to_string())?;
    receiver.recv().map_err(|err| err.to_string())
}

#[derive(Default)]
struct DesktopState {
    runtime_process: Mutex<Option<Child>>,
    engine_identity: Mutex<Option<EngineIdentity>>,
    hotkeys: Mutex<RegisteredHotkeys>,
    audio_ducking: Mutex<AudioDuckingState>,
    audio_ducking_settings: Mutex<AudioDuckingSettings>,
    commands: Mutex<ShellCommands>,
    recording_bar: Mutex<RecordingBarLifetime>,
}

#[derive(Default)]
struct ShellCommands {
    control: SessionControl,
    sender: Option<mpsc::Sender<(SessionCommand, EngineIdentity)>>,
    experimental_paste: bool,
    paste_target: Option<windows_paste::Target>,
    /// Session cancelled because its binding was only tapped, with the hint
    /// shown when the cancellation is acknowledged.
    discarded_tap: Option<(String, &'static str)>,
    /// Session locked into hands-free recording.
    hands_free: Option<String>,
    caret_shaping: bool,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct RegisteredHotkeys {
    dictation: HotkeyBindings,
    command_mode: HotkeyBindings,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct HotkeyBindings {
    keyboard: Vec<Shortcut>,
    mouse: Vec<MouseButtonBinding>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum MouseButtonBinding {
    X1,
    X2,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct HotkeySettings {
    #[serde(deserialize_with = "deserialize_hotkey_list")]
    pub dictation: Vec<String>,
    #[serde(deserialize_with = "deserialize_hotkey_list")]
    pub command_mode: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
struct RecordingBarStatePayload {
    state: String,
    mode: String,
    message: Option<String>,
    /// A shortcut request is distinct from verified delivery.
    delivery: Option<String>,
}

#[derive(Default)]
struct RecordingBarLifetime {
    revision: u64,
    terminal: bool,
    state: String,
    dismissed: bool,
}

impl RecordingBarLifetime {
    fn update(&mut self, state: &str) -> Option<u64> {
        if self.state == state && self.terminal {
            return None;
        }
        self.revision += 1;
        self.state = state.into();
        self.dismissed = false;
        self.terminal = matches!(state, "ready" | "no_speech" | "cancelled" | "error");
        self.terminal.then_some(self.revision)
    }

    fn may_hide(&self, revision: u64) -> bool {
        self.terminal && !self.dismissed && self.revision == revision
    }

    fn dismiss(&mut self, revision: u64) -> bool {
        if !self.may_hide(revision) {
            return false;
        }
        self.dismissed = true;
        true
    }

    fn invalidate(&mut self) {
        self.revision += 1;
        self.terminal = false;
        self.state.clear();
        self.dismissed = true;
    }
}

/// What automatic paste tells the recording bar about a ready result.
#[derive(Debug, Default, PartialEq)]
struct PasteFeedback {
    delivery: Option<&'static str>,
    message: Option<String>,
}

impl PasteFeedback {
    fn recovery(message: impl Into<String>) -> Self {
        Self { delivery: Some("recovery"), message: Some(message.into()) }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
struct AudioDuckingSettings {
    enabled: bool,
    target_volume: f32,
}

impl Default for AudioDuckingSettings {
    fn default() -> Self {
        Self {
            enabled: true,
            target_volume: 0.08,
        }
    }
}

impl AudioDuckingSettings {
    fn from_runtime_settings(runtime: DesktopRuntimeSettings) -> Self {
        Self {
            enabled: runtime.system_audio_ducking.unwrap_or(true),
            target_volume: duck_volume_percent_to_scalar(runtime.system_audio_duck_volume),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct VolumeSnapshot {
    volume: f32,
    muted: bool,
}

#[derive(Clone, Debug, Default)]
struct AudioDuckingState {
    snapshot: Option<VolumeSnapshot>,
    applied: Option<VolumeSnapshot>,
}

#[derive(Clone, Debug, Deserialize)]
struct DesktopSettings {
    hotkeys: HotkeySettings,
    runtime: Option<DesktopRuntimeSettings>,
}

#[derive(Clone, Debug, Deserialize)]
struct DesktopRuntimeSettings {
    system_audio_ducking: Option<bool>,
    system_audio_duck_volume: Option<u8>,
    experimental_automatic_paste: Option<bool>,
    caret_shaping: Option<bool>,
}

trait VolumeEndpoint {
    fn get_volume(&mut self) -> Result<f32, String>;
    fn set_volume(&mut self, volume: f32) -> Result<(), String>;
    fn get_muted(&mut self) -> Result<bool, String>;
    fn set_muted(&mut self, muted: bool) -> Result<(), String>;
}

#[tauri::command]
async fn start_runtime_process(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || spawn_runtime_api(&app))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn stop_runtime_process(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || shutdown_runtime(&app))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn runtime_request(
    app: AppHandle,
    method: String,
    path: String,
    body: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    let identity = current_identity(&app)?;
    authenticated_request(&app, &identity, &method, &path, body).await
}

#[tauri::command]
async fn runtime_status(app: AppHandle) -> Result<serde_json::Value, String> {
    runtime_request(app, "GET".into(), "/status".into(), None).await
}
#[tauri::command]
async fn runtime_check(app: AppHandle) -> Result<serde_json::Value, String> {
    runtime_request(app, "GET".into(), "/runtime/check".into(), None).await
}
#[tauri::command]
async fn runtime_diagnostics(app: AppHandle) -> Result<serde_json::Value, String> {
    runtime_request(app, "GET".into(), "/runtime/diagnostics".into(), None).await
}

#[tauri::command]
fn show_settings_window(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.show().map_err(|err| err.to_string())?;
        window.set_focus().map_err(|err| err.to_string())?;
    }
    Ok(())
}

// Both commands take shell locks, so they run off the UI thread: session
// workers hold those locks while waiting for UI-thread window work.
#[tauri::command(async)]
fn register_hotkeys(app: AppHandle, hotkeys: HotkeySettings) -> Result<(), String> {
    register_hotkeys_inner(&app, hotkeys)
}

#[tauri::command(async)]
fn configure_desktop_settings(app: AppHandle, settings: DesktopSettings) -> Result<(), String> {
    configure_desktop_settings_inner(&app, settings)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(DesktopState::default())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(handle_shortcut)
                .build(),
        )
        .setup(|app| {
            create_tray(app)?;
            start_command_dispatcher(app.handle().clone());
            start_input_worker(app.handle().clone());
            let handle = app.handle().clone();
            thread::spawn(move || {
                if let Err(err) = spawn_runtime_api(&handle) {
                    eprintln!("Engine startup failed: {err}");
                }
            });
            start_mouse_button_listener(app.handle().clone());
            // Start without bindings. The settings window applies the user's loaded
            // configuration and reports conflicts without aborting desktop startup.
            Ok(())
        })
        .on_window_event(handle_window_event)
        .invoke_handler(tauri::generate_handler![
            configure_desktop_settings,
            register_hotkeys,
            runtime_check,
            runtime_diagnostics,
            runtime_status,
            runtime_request,
            show_settings_window,
            start_runtime_process,
            stop_runtime_process
        ])
        .build(tauri::generate_context!())
        .expect("error while building WezaFlow")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                // A session worker may be waiting for this thread while holding a
                // lock shutdown needs. Never let that keep a dead window alive: the
                // engine job is reaped by the OS when the process ends.
                thread::spawn(|| {
                    thread::sleep(Duration::from_secs(8));
                    std::process::exit(0);
                });
                let _ = shutdown_runtime(app);
            }
        });
}

fn create_tray(app: &mut App) -> tauri::Result<()> {
    let open_settings =
        MenuItem::with_id(app, "open_settings", "Open Settings", true, None::<&str>)?;
    let start_runtime =
        MenuItem::with_id(app, "start_runtime", "Start Engine", true, None::<&str>)?;
    let stop_runtime = MenuItem::with_id(app, "stop_runtime", "Stop Engine", true, None::<&str>)?;
    let diagnostics = MenuItem::with_id(app, "diagnostics", "Diagnostics", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit WezaFlow", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open_settings,
            &start_runtime,
            &stop_runtime,
            &diagnostics,
            &quit,
        ],
    )?;

    TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .tooltip("WezaFlow")
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open_settings" | "diagnostics" => {
                let _ = show_settings_window(app.clone());
            }
            "start_runtime" => {
                let handle = app.clone();
                thread::spawn(move || {
                    let _ = spawn_runtime_api(&handle);
                });
            }
            "stop_runtime" => {
                let handle = app.clone();
                thread::spawn(move || {
                    let _ = shutdown_runtime(&handle);
                });
            }
            "quit" => {
                let handle = app.clone();
                thread::spawn(move || {
                    let _ = shutdown_runtime(&handle);
                    handle.exit(0);
                });
            }
            _ => {}
        })
        .build(app)?;

    Ok(())
}

fn handle_window_event(window: &Window, event: &WindowEvent) {
    if let WindowEvent::CloseRequested { api, .. } = event {
        api.prevent_close();
        let _ = window.hide();
    }
}

fn register_hotkeys_inner(app: &AppHandle, hotkeys: HotkeySettings) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let commands = state.commands.lock().map_err(|e| e.to_string())?;
    apply_hotkeys(app, hotkeys, commands.control.active.is_some())
}

fn apply_hotkeys(app: &AppHandle, hotkeys: HotkeySettings, active: bool) -> Result<(), String> {
    let registered = parse_registered_hotkeys(hotkeys)?;
    let state = app.state::<DesktopState>();
    let mut current = state.hotkeys.lock().map_err(|e| e.to_string())?;
    let result = replace_hotkeys(
        &mut *current,
        registered,
        active,
        &mut DesktopHotkeyRegistrar(app),
    );
    publish_mouse_bindings(&current);
    result
}

trait HotkeyRegistrar {
    fn unregister_all(&mut self) -> Result<(), String>;
    fn register(&mut self, shortcut: Shortcut) -> Result<(), String>;
}

struct DesktopHotkeyRegistrar<'a>(&'a AppHandle);

impl HotkeyRegistrar for DesktopHotkeyRegistrar<'_> {
    // The plugin's bulk unregister holds its shortcut table while it waits for
    // the UI thread, and the UI thread needs that table to deliver a key press.
    // Running the whole call on the UI thread removes that wait.
    fn unregister_all(&mut self) -> Result<(), String> {
        on_main_thread(self.0, |app| {
            app.global_shortcut()
                .unregister_all()
                .map_err(|e| e.to_string())
        })?
    }
    fn register(&mut self, shortcut: Shortcut) -> Result<(), String> {
        on_main_thread(self.0, move |app| {
            app.global_shortcut()
                .register(shortcut)
                .map_err(|e| e.to_string())
        })?
    }
}

fn replace_hotkeys(
    current: &mut RegisteredHotkeys,
    registered: RegisteredHotkeys,
    active: bool,
    registrar: &mut impl HotkeyRegistrar,
) -> Result<(), String> {
    if *current == registered {
        return Ok(());
    }
    if active {
        return Err("Finish or cancel the active session before changing shortcuts.".into());
    }
    registrar.unregister_all()?;
    // Never publish a partial configuration. Handlers consult this state, so any
    // OS registrations left after a cleanup failure remain inert, including mice.
    *current = RegisteredHotkeys::default();
    for shortcut in registered
        .dictation
        .keyboard
        .iter()
        .chain(registered.command_mode.keyboard.iter())
    {
        if let Err(error) = registrar.register(shortcut.clone()) {
            return match registrar.unregister_all() {
                Ok(()) => Err(error),
                Err(cleanup) => Err(format!("{error}. Shortcut cleanup failed: {cleanup}. Shortcuts are inactive; retry saving settings.")),
            };
        }
    }
    *current = registered;
    Ok(())
}

fn configure_desktop_settings_inner(
    app: &AppHandle,
    settings: DesktopSettings,
) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let mut commands = state.commands.lock().map_err(|e| e.to_string())?;
    let experimental_paste = settings.runtime.as_ref()
        .and_then(|r| r.experimental_automatic_paste).unwrap_or(false);
    let caret_shaping = settings.runtime.as_ref()
        .and_then(|r| r.caret_shaping).unwrap_or(true);
    let requested = settings
        .runtime
        .map(AudioDuckingSettings::from_runtime_settings)
        .unwrap_or_default();
    let mut audio_settings = state
        .audio_ducking_settings
        .lock()
        .map_err(|e| e.to_string())?;
    let active = commands.control.active.is_some();
    if active && commands.experimental_paste != experimental_paste {
        return Err("Finish or cancel the active session before changing paste mode.".into());
    }
    if active && *audio_settings != requested {
        return Err("Finish or cancel the active session before changing audio settings.".into());
    }
    apply_hotkeys(app, settings.hotkeys, active)?;
    *audio_settings = requested;
    commands.experimental_paste = experimental_paste;
    commands.caret_shaping = caret_shaping;
    Ok(())
}

fn handle_shortcut(
    _app: &AppHandle,
    shortcut: &Shortcut,
    event: tauri_plugin_global_shortcut::ShortcutEvent,
) {
    // The plugin calls this while holding its shortcut table, and for a press
    // on the UI thread. Registering Escape from here re-locks that table and
    // freezes the app, so only hand the event to the input worker.
    post_input_event(InputEvent::Shortcut {
        shortcut: shortcut.clone(),
        pressed: event.state() == ShortcutState::Pressed,
    });
}

fn start_input_worker(app: AppHandle) {
    let (sender, receiver) = mpsc::channel::<InputEvent>();
    if INPUT_EVENTS.set(sender).is_err() {
        return;
    }
    thread::spawn(move || {
        let mut input = InputGesture::default();
        loop {
            // The only timed wait: a tap's window for the hands-free second press.
            let event = match input.gesture.deadline() {
                Some(deadline) => {
                    match receiver.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
                        Ok(event) => Some(event),
                        Err(mpsc::RecvTimeoutError::Timeout) => None,
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    }
                }
                None => match receiver.recv() {
                    Ok(event) => Some(event),
                    Err(_) => break,
                },
            };
            let binding = input.gesture.binding().map(str::to_string);
            let expired = input.gesture.expire(Instant::now());
            input.act(&app, expired, binding.as_deref().unwrap_or(""), "");
            match event {
                Some(InputEvent::Shortcut { shortcut, pressed }) => {
                    process_shortcut(&app, &mut input, &shortcut, pressed)
                }
                Some(InputEvent::Mouse { button, pressed }) => {
                    handle_mouse_button(&app, &mut input, button, pressed)
                }
                None => {}
            }
        }
    });
}

/// Hold-to-talk, tap discard and hands-free lock for the input worker.
#[derive(Default)]
struct InputGesture {
    gesture: PressGesture,
    session: Option<String>,
}

impl InputGesture {
    fn binding_input(&mut self, app: &AppHandle, binding: &str, mode: &str, pressed: bool) {
        // A session the engine ended on its own (limit, error) leaves no gesture.
        if self.gesture.binding().is_some()
            && !self.session.as_deref().is_some_and(|id| session_is_active(app, id))
        {
            self.gesture.reset();
        }
        let now = Instant::now();
        let action = if pressed {
            self.gesture.press(binding, now)
        } else {
            self.gesture.release(binding, now)
        };
        self.act(app, action, binding, mode);
    }

    fn act(&mut self, app: &AppHandle, action: GestureAction, binding: &str, mode: &str) {
        match action {
            GestureAction::Ignore => {}
            GestureAction::Start => {
                self.session =
                    enqueue_session_command(app, SessionRequest::Start { binding, mode });
                if self.session.is_none() {
                    self.gesture.reset();
                }
            }
            GestureAction::Stop => {
                enqueue_session_command(app, SessionRequest::Stop { binding });
                self.session = None;
            }
            GestureAction::Lock => {
                // Shown by the dispatcher's next recording status.
                if let (Some(id), Ok(mut commands)) =
                    (self.session.as_ref(), app.state::<DesktopState>().commands.lock())
                {
                    commands.hands_free = Some(id.clone());
                }
            }
            GestureAction::DiscardTap => {
                if let Some(session_id) = self.session.take() {
                    let hint = if binding.starts_with("mouse:") {
                        "Hold the button to dictate."
                    } else {
                        "Hold the key to dictate."
                    };
                    enqueue_session_command(
                        app,
                        SessionRequest::DiscardTap { session_id: &session_id, hint },
                    );
                }
            }
        }
    }

    fn cancel(&mut self, app: &AppHandle) {
        self.gesture.reset();
        self.session = None;
        enqueue_session_command(app, SessionRequest::Cancel);
    }
}

fn session_is_active(app: &AppHandle, id: &str) -> bool {
    app.state::<DesktopState>()
        .commands
        .lock()
        .is_ok_and(|commands| commands.control.active.as_ref().is_some_and(|c| c.session_id == id))
}

fn process_shortcut(app: &AppHandle, input: &mut InputGesture, shortcut: &Shortcut, pressed: bool) {
    if *shortcut == Shortcut::new(None, Code::Escape) {
        if pressed {
            input.cancel(app);
        }
        return;
    }
    let state = app.state::<DesktopState>();
    let hotkeys = match state.hotkeys.lock() {
        Ok(hotkeys) => hotkeys.clone(),
        Err(_) => return,
    };
    let mode = if hotkeys
        .dictation
        .keyboard
        .iter()
        .any(|registered| registered == shortcut)
    {
        Some("dictation")
    } else if hotkeys
        .command_mode
        .keyboard
        .iter()
        .any(|registered| registered == shortcut)
    {
        Some("command")
    } else {
        None
    };

    let Some(mode) = mode else {
        return;
    };
    input.binding_input(app, &format!("keyboard:{}", shortcut.id()), mode, pressed);
}

fn parse_registered_hotkeys(hotkeys: HotkeySettings) -> Result<RegisteredHotkeys, String> {
    let registered = RegisteredHotkeys {
        dictation: parse_hotkey_bindings(&hotkeys.dictation)?,
        command_mode: parse_hotkey_bindings(&hotkeys.command_mode)?,
    };
    ensure_unique_bindings(&registered)?;
    Ok(registered)
}

fn parse_hotkey_bindings(values: &[String]) -> Result<HotkeyBindings, String> {
    if values.is_empty() {
        return Err("At least one hotkey is required.".to_string());
    }
    let mut bindings = HotkeyBindings::default();
    for value in values {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            return Err("Hotkey cannot be empty.".to_string());
        }
        if let Some(button) = parse_mouse_button(trimmed) {
            bindings.mouse.push(button);
        } else {
            bindings.keyboard.push(parse_shortcut(trimmed)?);
        }
    }
    Ok(bindings)
}

fn ensure_unique_bindings(hotkeys: &RegisteredHotkeys) -> Result<(), String> {
    let keyboard_bindings: Vec<(&str, &Shortcut)> = hotkeys
        .dictation
        .keyboard
        .iter()
        .map(|shortcut| ("dictation", shortcut))
        .chain(
            hotkeys
                .command_mode
                .keyboard
                .iter()
                .map(|shortcut| ("command mode", shortcut)),
        )
        .collect();
    for left in 0..keyboard_bindings.len() {
        for right in (left + 1)..keyboard_bindings.len() {
            if keyboard_bindings[left].1 == keyboard_bindings[right].1 {
                return duplicate_hotkey_error(
                    keyboard_bindings[left].0,
                    keyboard_bindings[right].0,
                );
            }
        }
    }

    let mouse_bindings: Vec<(&str, MouseButtonBinding)> = hotkeys
        .dictation
        .mouse
        .iter()
        .copied()
        .map(|button| ("dictation", button))
        .chain(
            hotkeys
                .command_mode
                .mouse
                .iter()
                .copied()
                .map(|button| ("command mode", button)),
        )
        .collect();
    for left in 0..mouse_bindings.len() {
        for right in (left + 1)..mouse_bindings.len() {
            if mouse_bindings[left].1 == mouse_bindings[right].1 {
                return duplicate_hotkey_error(mouse_bindings[left].0, mouse_bindings[right].0);
            }
        }
    }
    Ok(())
}

fn duplicate_hotkey_error(left: &str, right: &str) -> Result<(), String> {
    if left == right {
        Err("Hotkeys must be unique.".to_string())
    } else {
        Err("Dictation and command mode hotkeys must be different.".to_string())
    }
}

fn duck_volume_percent_to_scalar(value: Option<u8>) -> f32 {
    f32::from(value.unwrap_or(8).min(30)) / 100.0
}

fn apply_audio_ducking<E: VolumeEndpoint>(
    endpoint: &mut E,
    settings: AudioDuckingSettings,
    state: &mut AudioDuckingState,
) -> Result<(), String> {
    if !settings.enabled || state.snapshot.is_some() {
        return Ok(());
    }

    let current_muted = endpoint.get_muted()?;
    let current_volume = endpoint.get_volume()?.clamp(0.0, 1.0);
    let target = settings.target_volume.clamp(0.0, 0.30);
    if current_muted || current_volume <= target {
        return Ok(());
    }

    let snapshot = VolumeSnapshot {
        volume: current_volume,
        muted: current_muted,
    };
    endpoint.set_volume(target)?;
    state.snapshot = Some(snapshot);
    state.applied = Some(VolumeSnapshot {
        volume: target,
        muted: current_muted,
    });
    Ok(())
}

fn restore_audio_ducking<E: VolumeEndpoint>(
    endpoint: &mut E,
    state: &mut AudioDuckingState,
) -> Result<(), String> {
    let Some(snapshot) = state.snapshot.take() else {
        return Ok(());
    };
    let applied = state.applied.take();
    let current = VolumeSnapshot {
        volume: endpoint.get_volume()?,
        muted: endpoint.get_muted()?,
    };
    if !applied
        .map(|a| (a.volume - current.volume).abs() <= 0.0001 && a.muted == current.muted)
        .unwrap_or(false)
    {
        return Ok(());
    }
    endpoint.set_volume(snapshot.volume.clamp(0.0, 1.0))?;
    endpoint.set_muted(snapshot.muted)?;
    Ok(())
}

fn duck_system_audio(app: &AppHandle) {
    let settings = app
        .state::<DesktopState>()
        .audio_ducking_settings
        .lock()
        .map(|settings| *settings)
        .unwrap_or_default();
    if !settings.enabled {
        return;
    }
    let Err(err) = with_system_volume_endpoint(|endpoint| {
        let desktop_state = app.state::<DesktopState>();
        let mut state = desktop_state
            .audio_ducking
            .lock()
            .map_err(|err| err.to_string())?;
        apply_audio_ducking(endpoint, settings, &mut state)
    }) else {
        return;
    };
    eprintln!("WezaFlow could not duck system audio: {err}");
}

fn restore_system_audio(app: &AppHandle) {
    let Err(err) = with_system_volume_endpoint(|endpoint| {
        let desktop_state = app.state::<DesktopState>();
        let mut state = desktop_state
            .audio_ducking
            .lock()
            .map_err(|err| err.to_string())?;
        restore_audio_ducking(endpoint, &mut state)
    }) else {
        return;
    };
    eprintln!("WezaFlow could not restore system audio: {err}");
}

#[cfg(windows)]
struct WindowsVolumeEndpoint {
    endpoint: IAudioEndpointVolume,
}

#[cfg(windows)]
impl VolumeEndpoint for WindowsVolumeEndpoint {
    fn get_volume(&mut self) -> Result<f32, String> {
        unsafe {
            self.endpoint
                .GetMasterVolumeLevelScalar()
                .map_err(|err| err.to_string())
        }
    }

    fn set_volume(&mut self, volume: f32) -> Result<(), String> {
        unsafe {
            self.endpoint
                .SetMasterVolumeLevelScalar(volume.clamp(0.0, 1.0), std::ptr::null())
                .map_err(|err| err.to_string())
        }
    }

    fn get_muted(&mut self) -> Result<bool, String> {
        unsafe {
            self.endpoint
                .GetMute()
                .map(|muted| muted.as_bool())
                .map_err(|err| err.to_string())
        }
    }

    fn set_muted(&mut self, muted: bool) -> Result<(), String> {
        unsafe {
            self.endpoint
                .SetMute(muted.into(), std::ptr::null())
                .map_err(|err| err.to_string())
        }
    }
}

#[cfg(windows)]
fn with_system_volume_endpoint<F>(operation: F) -> Result<(), String>
where
    F: FnOnce(&mut WindowsVolumeEndpoint) -> Result<(), String>,
{
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
        let enumerator: IMMDeviceEnumerator =
            CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                .map_err(|err| err.to_string())?;
        let device = enumerator
            .GetDefaultAudioEndpoint(eRender, eConsole)
            .map_err(|err| err.to_string())?;
        let endpoint = device
            .Activate(CLSCTX_ALL, None)
            .map_err(|err| err.to_string())?;
        operation(&mut WindowsVolumeEndpoint { endpoint })
    }
}

#[cfg(not(windows))]
fn with_system_volume_endpoint<F>(_operation: F) -> Result<(), String>
where
    F: FnOnce(&mut NoopVolumeEndpoint) -> Result<(), String>,
{
    Ok(())
}

#[cfg(not(windows))]
struct NoopVolumeEndpoint;

#[cfg(not(windows))]
impl VolumeEndpoint for NoopVolumeEndpoint {
    fn get_volume(&mut self) -> Result<f32, String> {
        Ok(1.0)
    }

    fn set_volume(&mut self, _volume: f32) -> Result<(), String> {
        Ok(())
    }

    fn get_muted(&mut self) -> Result<bool, String> {
        Ok(false)
    }

    fn set_muted(&mut self, _muted: bool) -> Result<(), String> {
        Ok(())
    }
}

pub fn parse_shortcut(value: &str) -> Result<Shortcut, String> {
    let mut modifiers = Modifiers::empty();
    let mut key: Option<Code> = None;

    for raw_part in value.split('+') {
        let part = raw_part.trim();
        if part.is_empty() {
            continue;
        }
        match part.to_ascii_lowercase().as_str() {
            "ctrl" | "control" => modifiers.insert(Modifiers::CONTROL),
            "alt" | "option" => modifiers.insert(Modifiers::ALT),
            "shift" => modifiers.insert(Modifiers::SHIFT),
            "meta" | "super" | "win" | "windows" | "cmd" | "command" => {
                modifiers.insert(Modifiers::SUPER)
            }
            _ => {
                if key.is_some() {
                    return Err("Shortcut must contain exactly one non-modifier key.".to_string());
                }
                key = Some(parse_code(part)?);
            }
        }
    }

    let Some(key) = key else {
        return Err("Shortcut must contain exactly one non-modifier key.".to_string());
    };
    let modifiers = if modifiers.is_empty() {
        None
    } else {
        Some(modifiers)
    };
    Ok(Shortcut::new(modifiers, key))
}

fn parse_code(part: &str) -> Result<Code, String> {
    let normalized = part.trim();
    match normalized.to_ascii_lowercase().as_str() {
        "space" | "spacebar" => return Ok(Code::Space),
        "esc" | "escape" => return Ok(Code::Escape),
        "enter" | "return" => return Ok(Code::Enter),
        "tab" => return Ok(Code::Tab),
        "backspace" => return Ok(Code::Backspace),
        "delete" | "del" => return Ok(Code::Delete),
        _ => {}
    }

    if normalized.len() == 1 {
        let character = normalized.chars().next().unwrap();
        if character.is_ascii_alphabetic() {
            return Code::from_str(&format!("Key{}", character.to_ascii_uppercase()))
                .map_err(|_| format!("Unsupported shortcut key: {part}"));
        }
        if character.is_ascii_digit() {
            return Code::from_str(&format!("Digit{character}"))
                .map_err(|_| format!("Unsupported shortcut key: {part}"));
        }
    }

    Code::from_str(normalized).map_err(|_| format!("Unsupported shortcut key: {part}"))
}

fn parse_mouse_button(part: &str) -> Option<MouseButtonBinding> {
    match part
        .trim()
        .to_ascii_lowercase()
        .replace([' ', '_', '-'], "")
        .as_str()
    {
        "mousex1" | "mouse4" | "button4" | "xbutton1" | "back" => Some(MouseButtonBinding::X1),
        "mousex2" | "mouse5" | "button5" | "xbutton2" | "forward" => Some(MouseButtonBinding::X2),
        _ => None,
    }
}

fn deserialize_hotkey_list<'de, D>(deserializer: D) -> Result<Vec<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = serde_json::Value::deserialize(deserializer)?;
    match value {
        serde_json::Value::String(shortcut) => Ok(vec![shortcut]),
        serde_json::Value::Array(items) => items
            .into_iter()
            .map(|item| match item {
                serde_json::Value::String(shortcut) => Ok(shortcut),
                _ => Err(D::Error::custom("hotkey entries must be strings")),
            })
            .collect(),
        _ => Err(D::Error::custom(
            "hotkey must be a string or list of strings",
        )),
    }
}

fn recording_bar_payload(
    state: &str,
    mode: &str,
    message: Option<&str>,
    delivery: Option<&str>,
) -> RecordingBarStatePayload {
    RecordingBarStatePayload {
        state: state.to_string(),
        mode: mode.to_string(),
        message: message.map(|value| value.to_string()),
        delivery: delivery.map(|value| value.to_string()),
    }
}

fn recording_bar_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(RECORDING_BAR_LABEL)
}

fn position_recording_bar(window: &WebviewWindow) {
    let Ok(Some(monitor)) = window.primary_monitor() else {
        return;
    };
    let work_area = monitor.work_area();
    let x = work_area.position.x + ((work_area.size.width as i32 - RECORDING_BAR_WIDTH) / 2);
    let y = work_area.position.y + work_area.size.height as i32
        - RECORDING_BAR_HEIGHT
        - RECORDING_BAR_BOTTOM_MARGIN;
    if let Err(err) = window.set_position(PhysicalPosition::new(
        x.max(work_area.position.x),
        y.max(work_area.position.y),
    )) {
        eprintln!("WezaFlow could not position recording bar: {err}");
    }
}

fn show_recording_bar(app: &AppHandle, state: &str, mode: &str, message: Option<&str>) {
    show_recording_bar_with_delivery(app, state, mode, message, None);
}

#[cfg(windows)]
unsafe fn set_feedback_window_visible(window: windows_sys::Win32::Foundation::HWND, visible: bool) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{ShowWindow, SW_SHOWNA, SW_HIDE};
    ShowWindow(window, if visible { SW_SHOWNA } else { SW_HIDE });
}

fn set_recording_bar_visible(window: &WebviewWindow, visible: bool) -> tauri::Result<()> {
    #[cfg(windows)]
    {
        let target = window.clone();
        window.run_on_main_thread(move || {
            if visible && target.app_handle().state::<DesktopState>().recording_bar.lock()
                .map(|lifetime| lifetime.dismissed).unwrap_or(true) {
                return;
            }
            if let Ok(handle) = target.hwnd() {
                unsafe {
                    set_feedback_window_visible(handle.0 as _, visible);
                }
            }
        })
    }
    #[cfg(not(windows))]
    {
        if visible { window.show() } else { window.hide() }
    }
}

fn show_recording_bar_with_delivery(
    app: &AppHandle,
    state: &str,
    mode: &str,
    message: Option<&str>,
    delivery: Option<&str>,
) {
    let Some(window) = recording_bar_window(app) else {
        return;
    };
    let revision = match app.state::<DesktopState>().recording_bar.lock() {
        Ok(mut lifetime) => {
            let revision = lifetime.update(state);
            if lifetime.dismissed {
                return;
            }
            revision
        }
        Err(_) => None,
    };
    position_recording_bar(&window);
    if let Err(err) = window.emit(
        RECORDING_BAR_EVENT,
        recording_bar_payload(state, mode, message, delivery),
    ) {
        eprintln!("WezaFlow could not emit recording bar state: {err}");
    }
    if let Err(err) = set_recording_bar_visible(&window, true) {
        eprintln!("WezaFlow could not show recording bar: {err}");
    }
    if let Some(revision) = revision {
        let delay = if (message.is_some() && delivery != Some("requested")) || state == "error" {
            Duration::from_secs(5)
        } else {
            Duration::from_millis(1500)
        };
        let handle = app.clone();
        thread::spawn(move || {
            thread::sleep(delay);
            let ui_handle = handle.clone();
            let _ = handle.run_on_main_thread(move || {
                let state = ui_handle.state::<DesktopState>();
                if let Ok(mut lifetime) = state.recording_bar.lock() {
                    if lifetime.dismiss(revision) {
                        if let Some(window) = recording_bar_window(&ui_handle) {
                            #[cfg(windows)]
                            if let Ok(handle) = window.hwnd() {
                                unsafe { set_feedback_window_visible(handle.0 as _, false); }
                            }
                            #[cfg(not(windows))]
                            let _ = window.hide();
                        }
                    }
                };
            });
        });
    }
}

fn hide_recording_bar(app: &AppHandle) {
    if let Ok(mut lifetime) = app.state::<DesktopState>().recording_bar.lock() {
        lifetime.invalidate();
    }
    let Some(window) = recording_bar_window(app) else {
        return;
    };
    if let Err(err) = set_recording_bar_visible(&window, false) {
        eprintln!("WezaFlow could not hide recording bar: {err}");
    }
}

fn current_identity(app: &AppHandle) -> Result<EngineIdentity, String> {
    app.state::<DesktopState>()
        .engine_identity
        .lock()
        .map_err(|e| e.to_string())?
        .clone()
        .ok_or_else(|| "Engine is not ready.".into())
}

enum SessionRequest<'a> {
    Start { binding: &'a str, mode: &'a str },
    Stop { binding: &'a str },
    Cancel,
    /// Cancels only `session_id`, never a later or unrelated session.
    DiscardTap { session_id: &'a str, hint: &'static str },
}

/// Returns the session id of a command handed to the dispatcher.
fn enqueue_session_command(app: &AppHandle, request: SessionRequest) -> Option<String> {
    let state = app.state::<DesktopState>();
    let current = state.engine_identity.lock().ok()?;
    let identity = current.as_ref().cloned()?;
    let mut commands = state.commands.lock().ok()?;
    let command = match request {
        SessionRequest::Start { binding, mode } => {
            let id = new_uuid().ok()?;
            let command = commands.control.press(binding, mode, &id);
            if command.is_some() {
                if let Ok(mut lifetime) = state.recording_bar.lock() {
                    lifetime.invalidate();
                }
                commands.discarded_tap = None;
                commands.hands_free = None;
                commands.paste_target = if commands.experimental_paste && mode == "dictation" {
                    new_uuid().ok().and_then(|capture| windows_paste::capture(&id, &capture))
                } else { None };
            }
            command
        }
        SessionRequest::Stop { binding } => {
            commands.control.release(binding)
        }
        SessionRequest::Cancel => commands.control.cancel(),
        SessionRequest::DiscardTap { session_id, hint } => {
            if !commands.control.active.as_ref().is_some_and(|c| c.session_id == session_id) {
                return None;
            }
            let command = commands.control.cancel();
            if command.is_some() {
                commands.discarded_tap = Some((session_id.to_string(), hint));
            }
            command
        }
    };
    let command = command?;
    if command.action == "start" {
        let escape = Shortcut::new(None, Code::Escape);
        if let Err(err) = app.global_shortcut().register(escape) {
            commands.control.finish(&command.session_id);
            show_recording_bar(
                app,
                "error",
                &command.mode,
                Some(&format!("Cannot register cancellation key: {err}")),
            );
            return None;
        }
    }
    if commands
        .sender
        .as_ref()
        .map(|s| s.send((command.clone(), identity)).is_ok())
        != Some(true)
    {
        commands.control.finish(&command.session_id);
        let _ = app
            .global_shortcut()
            .unregister(Shortcut::new(None, Code::Escape));
        show_recording_bar(
            app,
            "error",
            &command.mode,
            Some("Engine command dispatcher unavailable."),
        );
        return None;
    }
    Some(command.session_id)
}

fn start_command_dispatcher(app: AppHandle) {
    let (sender, receiver) = mpsc::channel::<(SessionCommand, EngineIdentity)>();
    if let Ok(mut commands) = app.state::<DesktopState>().commands.lock() {
        commands.sender = Some(sender);
    }
    thread::spawn(move || {
        let mut observing: Option<(String, EngineIdentity)> = None;
        let mut retry_cancel: Option<(SessionCommand, EngineIdentity)> = None;
        loop {
            match receiver.recv_timeout(Duration::from_millis(100)) {
                Ok((command, identity)) => {
                    if command.action == "start" {
                        // Wait behind startup without launching a replacement for an old queued press.
                        let state = app.state::<DesktopState>();
                        let _startup = state.runtime_process.lock();
                    }
                    let capture_id = app.state::<DesktopState>().commands.lock().ok()
                        .and_then(|c| c.paste_target.as_ref().filter(|t| t.session_id == command.session_id).map(|t| t.capture_id.clone()));
                    let payload = serde_json::json!({"engine_id":identity.engine_id,"protocol_version":1,
                        "session_id":command.session_id,"command_seq":command.command_seq,
                        "binding_id":command.binding_id,"mode":command.mode,"language":null,
                        "capture_id": if command.action == "start" { capture_id } else { None }});
                    let result = tauri::async_runtime::block_on(authenticated_request(
                        &app,
                        &identity,
                        "POST",
                        &format!("/runtime/{}", command.action),
                        Some(payload),
                    ));
                    let decision = match result {
                        Ok(reply) => {
                            settle_runtime_reply(&app, &command.session_id, &identity, &reply, true)
                        }
                        Err(err) => session_failure(&app, &command.session_id, &identity, &err),
                    };
                    let (watch, retry) = dispatch_watch(decision, &command.session_id, &identity);
                    observing = watch;
                    retry_cancel = retry;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if let Some((id, identity)) = observing.clone() {
                        // Observe on every retry turn. Cancellation retries never
                        // starve status, and mismatched status never discards them.
                        let decision = match tauri::async_runtime::block_on(authenticated_request(
                            &app, &identity, "GET", "/status", None,
                        )) {
                            Ok(reply) => settle_runtime_reply(&app, &id, &identity, &reply, false),
                            Err(err) => session_failure(&app, &id, &identity, &err),
                        };
                        let (watch, retry) = dispatch_watch(decision, &id, &identity);
                        observing = watch;
                        retry_cancel = retry;
                    }
                    if let Some((command, identity)) = retry_cancel.take() {
                        if let Ok(commands) = app.state::<DesktopState>().commands.lock() {
                            if let Some(sender) = commands.sender.as_ref() {
                                let _ = sender.send((command, identity));
                            }
                        }
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
    });
}

fn dispatch_watch(
    decision: Option<DispatchDecision>,
    id: &str,
    identity: &EngineIdentity,
) -> (
    Option<(String, EngineIdentity)>,
    Option<(SessionCommand, EngineIdentity)>,
) {
    let Some(decision) = decision else {
        return (None, None);
    };
    (
        decision.pending.then(|| (id.to_string(), identity.clone())),
        decision
            .retry_cancel
            .map(|command| (command, identity.clone())),
    )
}

fn session_failure(
    app: &AppHandle,
    id: &str,
    identity: &EngineIdentity,
    message: &str,
) -> Option<DispatchDecision> {
    let state = app.state::<DesktopState>();
    let current = state.engine_identity.lock().ok()?;
    if current.as_ref() != Some(identity) {
        return None;
    }
    let mut commands = state.commands.lock().ok()?;
    let mode = commands
        .control
        .active
        .as_ref()
        .filter(|c| c.session_id == id)
        .map(|c| c.mode.clone());
    let decision = commands.control.settle_transport_failure(id);
    if let Some(mode) = mode {
        restore_system_audio(app);
        show_recording_bar(app, "error", &mode, Some(message));
    }
    Some(decision)
}

fn command_rejection_message(reason: Option<&str>) -> String {
    match reason {
        Some("command_editing_unavailable_until_M2") => {
            "Command editing is unavailable during the safety rebuild.".into()
        }
        Some("busy") => {
            "The engine is finishing the previous session. Try again when it is ready.".into()
        }
        Some("shutting_down") => "The engine is shutting down.".into(),
        Some("binding_mismatch") => "This shortcut does not own the active recording.".into(),
        Some("unknown_session") => "This recording session is no longer active.".into(),
        _ => "The engine could not accept this recording command.".into(),
    }
}

fn settle_runtime_reply(
    app: &AppHandle,
    id: &str,
    identity: &EngineIdentity,
    reply: &serde_json::Value,
    command: bool,
) -> Option<DispatchDecision> {
    // This is the existing serialized dispatcher, not an additional writer worker.
    // Consume the captured target once before observing READY releases ownership.
    let paste = maybe_compatibility_paste(app, id, identity, reply).unwrap_or_default();
    let state = app.state::<DesktopState>();
    let current = state.engine_identity.lock().ok()?;
    if validate_reply(identity, current.as_ref(), reply).is_err() {
        return None;
    }
    let mut commands = state.commands.lock().ok()?;
    let mode = commands
        .control
        .active
        .as_ref()
        .filter(|c| c.session_id == id)
        .map(|c| c.mode.clone());
    let decision = if command {
        commands.control.settle_command(id, reply)
    } else {
        commands.control.settle_status(id, reply)
    };
    if let Some(ack) = decision.observation.as_ref() {
        if ack.state == "recording" && ack.mode == "dictation" {
            duck_system_audio(app);
        } else if ack.state != "starting" {
            restore_system_audio(app);
        }
        let tap_hint = match &commands.discarded_tap {
            Some((session, hint)) if session == id && ack.state == "cancelled" => Some(*hint),
            _ => None,
        };
        let hands_free = (ack.state == "recording"
            && commands.hands_free.as_deref() == Some(id))
        .then_some("Hands-free. Press again to finish.");
        show_recording_bar_with_delivery(
            app,
            &ack.state,
            &ack.mode,
            paste
                .message
                .as_deref()
                .or(tap_hint)
                .or_else(|| session::runtime_message(id, reply))
                .or(hands_free),
            paste.delivery,
        );
        if matches!(
            ack.state.as_str(),
            "ready" | "no_speech" | "cancelled" | "error"
        ) {
            let _ = app
                .global_shortcut()
                .unregister(Shortcut::new(None, Code::Escape));
        }
    } else if decision.rejected {
        restore_system_audio(app);
        let _ = app
            .global_shortcut()
            .unregister(Shortcut::new(None, Code::Escape));
        if let Some(mode) = mode {
            show_recording_bar(
                app,
                "error",
                &mode,
                Some(&command_rejection_message(reply["reason"].as_str())),
            );
        }
    }
    Some(decision)
}

fn maybe_compatibility_paste(
    app: &AppHandle, id: &str, identity: &EngineIdentity, reply: &serde_json::Value,
) -> Option<PasteFeedback> {
    if reply["state"] != "ready" || reply["busy"] != false
        || reply["session_id"].as_str() != Some(id) { return None; }
    let state = app.state::<DesktopState>();
    let target = {
        let current = state.engine_identity.lock().ok()?;
        validate_reply(identity, current.as_ref(), reply).ok()?;
        let mut commands = state.commands.lock().ok()?;
        if !commands.experimental_paste || !commands.control.may_commit(id) { return None; }
        let caret_shaping = commands.caret_shaping;
        // Taking this token makes every later observation inert, even if HTTP
        // reservation/completion fails or returns an idempotent replay.
        match commands.paste_target.take() {
            None => Err(("NoCapturedTarget", "No focused field was captured at key-down. Copy the result from Wezza.")),
            Some(target) if target.session_id != id || reply["capture_id"] != target.capture_id
                || reply["result_session_id"].as_str() != Some(id)
                || reply["result_generation"] != reply["session_generation"] => {
                Err(("CaptureMismatch", "The result did not match the captured field. Copy the result from Wezza."))
            }
            Some(target) => Ok((target, caret_shaping)),
        }
    };
    let (target, caret_shaping) = match target {
        Ok(target) => target,
        Err((code, message)) => {
            record_paste_decision(format_args!("{code}"));
            return Some(PasteFeedback::recovery(message));
        }
    };
    let Some(text) = reply["result"]["polished_text"].as_str().filter(|s| !s.trim().is_empty()) else {
        record_paste_decision(format_args!("InvalidText"));
        return Some(PasteFeedback::recovery("There was no usable text for automatic paste. Check the result in Wezza."));
    };
    let transaction = new_uuid().ok()?;
    let mut payload = serde_json::json!({"engine_id":identity.engine_id,"protocol_version":1,
        "session_generation":reply["session_generation"],"session_id":id,
        "transaction_id":transaction,"capture_id":target.capture_id,"result_session_id":id});
    let reserved = tauri::async_runtime::block_on(authenticated_request(
        app, identity, "POST", "/runtime/commit/reserve", Some(payload.clone())));
    let Ok(reserved) = reserved else {
        record_paste_decision(format_args!("ReservationUnavailable"));
        return Some(PasteFeedback::recovery("Paste authorization was unavailable. Copy the result from Wezza. Restart Wezza if it stays busy."));
    };
    if reserved["reservation_decision"] != "reserved" || reserved["write_authorized"] != true {
        record_paste_decision(format_args!("ReservationDenied"));
        return Some(PasteFeedback::recovery("Paste was not authorized. Copy the result from Wezza."));
    }
    use windows_paste::{PasteReason, PasteResult};
    let paste_result = (|| {
        // A sensitive, unknown or already-changed field never triggers clipboard rendering.
        let editor = target.qualify()?;
        // Snapshot outside shell locks. Delayed clipboard formats can wait on another app.
        #[cfg(windows)]
        let owner = app.get_webview_window("main")
            .and_then(|window| window.hwnd().ok()).map(|handle| handle.0 as usize).unwrap_or(0);
        #[cfg(not(windows))]
        let owner = 0;
        let plan = windows_paste::prepare_clipboard(owner);
        let context = target.revalidate(&editor, caret_shaping)?;
        let preserve_initial_case = reply["result"]["diagnostics"]["preserve_initial_case"]
            .as_bool().unwrap_or(false);
        let shaped = caret_shaping.then(|| caret::shape(text, context.as_ref(), preserve_initial_case));
        let current = state.engine_identity.lock().ok();
        let commands = state.commands.lock().ok();
        if current.as_ref().and_then(|v| v.as_ref()) != Some(identity)
            || !commands.as_ref().is_some_and(|c| c.control.may_commit(id)) {
            drop((commands, current));
            drop(plan);
            return Err(PasteReason::Cancelled);
        }
        Ok(windows_paste::paste(&target, shaped.as_deref().unwrap_or(text), plan))
    })().unwrap_or_else(PasteResult::skipped);
    record_paste_decision(format_args!("{:?} {:?}", paste_result.reason, paste_result.clipboard));
    payload["outcome"] = serde_json::json!(paste_result.reason.outcome());
    let completed = tauri::async_runtime::block_on(authenticated_request(
        app, identity, "POST", "/runtime/commit/complete", Some(payload)));
    let recorded = completed.as_ref().is_ok_and(|r| r["accepted"] == true);
    if !recorded {
        record_paste_decision(format_args!("CompletionUnverified"));
    }
    Some(paste_feedback(&paste_result, recorded))
}

// Callers supply only static outcome codes and enums, never dictation or window data.
fn record_paste_decision(outcome: std::fmt::Arguments<'_>) {
    use std::io::Write;
    use std::time::{SystemTime, UNIX_EPOCH};

    let path = project_root().join("artifacts").join("logs").join("desktop-paste.log");
    let Ok(mut log) = OpenOptions::new().create(true).append(true).open(path) else { return; };
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
    let _ = writeln!(log, "{stamp} {outcome}");
}

fn paste_feedback(result: &windows_paste::PasteResult, recorded: bool) -> PasteFeedback {
    use windows_paste::{ClipboardMutation, PasteReason};
    let message = if !recorded {
        "Paste completion could not be recorded. Check the target before pasting again; restart Wezza if busy.".into()
    } else {
        match result.reason {
            PasteReason::InputSent => result.reason.message().into(),
            PasteReason::InputPartial => format!("{} Check the field before copying the result from Wezza.", result.reason.message()),
            reason => format!("{} Copy the result from Wezza.", reason.message()),
        }
    };
    let message = if result.clipboard == ClipboardMutation::RestoreFailed {
        format!("{message} Wezza could not restore the previous clipboard.")
    } else {
        message
    };
    let delivery = if recorded && result.reason == PasteReason::InputSent {
        Some("requested")
    } else {
        Some("recovery")
    };
    PasteFeedback { delivery, message: Some(message) }
}

fn handle_mouse_button(
    app: &AppHandle,
    input: &mut InputGesture,
    button: MouseButtonBinding,
    pressed: bool,
) {
    let state = app.state::<DesktopState>();
    let hotkeys = match state.hotkeys.lock() {
        Ok(hotkeys) => hotkeys.clone(),
        Err(_) => return,
    };
    let mode = if hotkeys.dictation.mouse.contains(&button) {
        Some("dictation")
    } else if hotkeys.command_mode.mouse.contains(&button) {
        Some("command")
    } else {
        None
    };

    let Some(mode) = mode else {
        return;
    };
    input.binding_input(app, &format!("mouse:{button:?}"), mode, pressed);
}

#[cfg(windows)]
fn start_mouse_button_listener(app: AppHandle) {
    START_MOUSE_HOOK.call_once(move || {
        let _ = MOUSE_HOOK_APP.set(app);
        thread::spawn(|| unsafe {
            let hook =
                SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_hook_proc), std::ptr::null_mut(), 0);
            if hook.is_null() {
                return;
            }
            let mut message: MSG = std::mem::zeroed();
            while GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) > 0 {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        });
    });
}

#[cfg(not(windows))]
fn start_mouse_button_listener(_app: AppHandle) {}

#[cfg(windows)]
unsafe extern "system" fn mouse_hook_proc(code: i32, w_param: WPARAM, l_param: LPARAM) -> LRESULT {
    if code >= 0 && (w_param as u32 == WM_XBUTTONDOWN || w_param as u32 == WM_XBUTTONUP) {
        let info = *(l_param as *const MSLLHOOKSTRUCT);
        let x_button = ((info.mouseData >> 16) & 0xffff) as u16;
        let button = match x_button {
            XBUTTON1 => Some(MouseButtonBinding::X1),
            XBUTTON2 => Some(MouseButtonBinding::X2),
            _ => None,
        };
        // A low-level hook must return at once or Windows drops it; the input
        // worker does the session work.
        if let (Some(_), Some(button)) = (MOUSE_HOOK_APP.get(), button) {
            post_input_event(InputEvent::Mouse {
                button,
                pressed: w_param as u32 == WM_XBUTTONDOWN,
            });
        }
        let bound = match button {
            Some(MouseButtonBinding::X1) => MOUSE_X1_BOUND.load(Ordering::Relaxed),
            Some(MouseButtonBinding::X2) => MOUSE_X2_BOUND.load(Ordering::Relaxed),
            None => false,
        };
        if bound {
            // Swallow a bound side button so the focused app (for example a
            // browser navigating back or forward) never sees it.
            return 1;
        }
    }
    CallNextHookEx(std::ptr::null_mut(), code, w_param, l_param)
}

async fn authenticated_request(
    app: &AppHandle,
    identity: &EngineIdentity,
    method: &str,
    path: &str,
    body: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    protected_path(path)?;
    let method = match method {
        "GET" => reqwest::Method::GET,
        "POST" => reqwest::Method::POST,
        "PUT" => reqwest::Method::PUT,
        "DELETE" => reqwest::Method::DELETE,
        "PATCH" => reqwest::Method::PATCH,
        _ => return Err("Unsupported API method.".into()),
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
        .map_err(|e| e.to_string())?;
    owned_request(
        identity,
        || {
            app.state::<DesktopState>()
                .engine_identity
                .lock()
                .map(|id| id.clone())
                .map_err(|e| e.to_string())
        },
        async {
            let complete = method == reqwest::Method::POST && path == "/runtime/commit/complete";
            let mut retry = complete;
            loop {
                let mut request = client
                    .request(method.clone(), format!("{RUNTIME_BASE_URL}{path}"))
                    .bearer_auth(&identity.token);
                if let Some(body) = &body {
                    request = request.json(body);
                }
                if complete {
                    request = request.timeout(Duration::from_secs(1));
                }
                let response = async {
                    request.send().await?.error_for_status()?.json::<serde_json::Value>().await
                }.await;
                match response {
                    Ok(value) => return Ok(value),
                    Err(error) if retry && retry_completion_transport(&error) => {
                        // Only repeat the idempotent completion payload. Native input
                        // already settled and can never run on this path.
                        retry = false;
                        if current_identity(app)? != *identity {
                            return Err("The runtime engine changed.".into());
                        }
                    }
                    Err(error) => return Err(error.to_string()),
                }
            }
        },
    )
    .await
}

fn retry_completion_transport(error: &reqwest::Error) -> bool {
    !error.is_status() && !error.is_decode()
        && (error.is_timeout() || error.is_connect() || error.is_request() || error.is_body())
}

#[cfg(windows)]
fn new_uuid() -> Result<String, String> {
    unsafe {
        windows::Win32::System::Com::CoCreateGuid()
            .map(|id| format!("{id:?}").to_lowercase())
            .map_err(|e| e.to_string())
    }
}
#[cfg(not(windows))]
fn new_uuid() -> Result<String, String> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|e| e.to_string())?;
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    let h = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &h[..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..]
    ))
}

fn spawn_runtime_api(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let mut runtime = state.runtime_process.lock().map_err(|e| e.to_string())?;
    if let Some(child) = runtime.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            let identity = current_identity(app).map_err(|_| {
                "An owned engine remains after failed cleanup. Stop it before restarting."
                    .to_string()
            })?;
            let reply = tauri::async_runtime::block_on(authenticated_request(
                app,
                &identity,
                "GET",
                "/runtime/handshake",
                None,
            ))?;
            return validate_handshake(&reply);
        }
        *runtime = None;
        retire_engine(app, Some("Owned engine exited."))?;
    }
    if runtime_api_is_listening() {
        return Err(
            "An unowned service is using 127.0.0.1:8765; refusing to adopt or terminate it.".into(),
        );
    }
    let root = project_root();
    if !is_project_root(&root) {
        return Err("Could not find Python runtime project root.".into());
    }
    let identity = EngineIdentity {
        engine_id: new_uuid()?,
        token: format!("{}{}", new_uuid()?, new_uuid()?),
    };
    let mut command = Command::new(python_executable(&root));
    command
        .args(["-m", "services.runtime.api"])
        .current_dir(&root)
        .env("WEZZA_ENGINE_ID", &identity.engine_id)
        .env("WEZZA_ENGINE_TOKEN", &identity.token);
    if let Ok(log) = runtime_log_file(&root) {
        if let Ok(stderr) = log.try_clone() {
            command.stdout(Stdio::from(log)).stderr(Stdio::from(stderr));
        }
    }
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);
    // Store ownership before any readiness work that can fail.
    let child = command.spawn().map_err(|e| e.to_string())?;
    if !contain_runtime_child(&child) {
        eprintln!("WezaFlow could not place the engine in its cleanup job.");
    }
    *runtime = Some(child);
    *state.engine_identity.lock().map_err(|e| e.to_string())? = Some(identity.clone());
    let result = wait_for_owned_runtime(
        app,
        runtime.as_mut().unwrap(),
        &identity,
        Duration::from_secs(8),
    );
    if result.is_err() {
        retire_engine(app, Some("Owned engine startup failed."))?;
        if let Err(cleanup) =
            cleanup_owned_slot(&mut runtime, 0, || thread::sleep(Duration::from_millis(50)))
        {
            return Err(format!("{} Cleanup failed: {cleanup}", result.unwrap_err()));
        }
    }
    result
}

fn wait_for_owned_runtime(
    app: &AppHandle,
    child: &mut Child,
    identity: &EngineIdentity,
    timeout: Duration,
) -> Result<(), String> {
    let started = Instant::now();
    while started.elapsed() < timeout {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            return Err(format!("Owned engine exited during startup: {status}"));
        }
        if let Ok(reply) = tauri::async_runtime::block_on(authenticated_request(
            app,
            identity,
            "GET",
            "/runtime/handshake",
            None,
        )) {
            validate_handshake(&reply)?;
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err("Owned engine startup timed out; child was terminated and reaped.".into())
}

fn shutdown_runtime(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let mut runtime = state.runtime_process.lock().map_err(|e| e.to_string())?;
    if let Ok(identity) = current_identity(app) {
        let _ = tauri::async_runtime::block_on(authenticated_request(
            app,
            &identity,
            "POST",
            "/runtime/shutdown",
            Some(serde_json::json!({"engine_id":identity.engine_id,"protocol_version":1})),
        ));
    }
    retire_engine(app, None)?;
    cleanup_owned_slot(&mut runtime, 20, || {
        thread::sleep(Duration::from_millis(50))
    })
}

fn retire_engine(app: &AppHandle, message: Option<&str>) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let mut identity = state.engine_identity.lock().map_err(|e| e.to_string())?;
    *identity = None;
    let mut commands = state.commands.lock().map_err(|e| e.to_string())?;
    let mode = commands
        .control
        .active
        .take()
        .map(|c| c.mode)
        .unwrap_or_else(|| "dictation".into());
    let _ = app
        .global_shortcut()
        .unregister(Shortcut::new(None, Code::Escape));
    restore_system_audio(app);
    if let Some(message) = message {
        show_recording_bar(app, "error", &mode, Some(message));
    } else {
        hide_recording_bar(app);
    }
    Ok(())
}

fn project_root() -> PathBuf {
    find_project_root_from_candidates(project_root_candidates())
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")))
}

fn project_root_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(root) = std::env::var("LOCALFLOW_PROJECT_ROOT") {
        candidates.push(PathBuf::from(root));
    }
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd);
    }
    if let Ok(exe) = std::env::current_exe() {
        candidates.push(exe);
    }
    candidates
}

fn find_project_root_from_candidates<I>(candidates: I) -> Option<PathBuf>
where
    I: IntoIterator<Item = PathBuf>,
{
    for candidate in candidates {
        let start = if candidate.is_file() || candidate.extension().is_some() {
            candidate.parent().map(PathBuf::from)
        } else {
            Some(candidate)
        };
        let Some(start) = start else {
            continue;
        };
        for ancestor in start.ancestors() {
            if is_project_root(ancestor) {
                return Some(ancestor.to_path_buf());
            }
        }
    }
    None
}

fn is_project_root(path: &std::path::Path) -> bool {
    path.join("pyproject.toml").exists()
        && path
            .join("services")
            .join("runtime")
            .join("api.py")
            .exists()
}

fn runtime_log_file(root: &std::path::Path) -> Result<File, String> {
    let log_dir = root.join("artifacts").join("logs");
    fs::create_dir_all(&log_dir).map_err(|err| err.to_string())?;
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("desktop-python-api.log"))
        .map_err(|err| err.to_string())
}

fn runtime_api_is_listening() -> bool {
    let Ok(addr) = RUNTIME_SOCKET_ADDR.parse() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

fn python_executable(root: &PathBuf) -> PathBuf {
    let venv_python = if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    };
    if venv_python.exists() {
        venv_python
    } else {
        PathBuf::from("python")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct FakeHotkeyRegistrar {
        installed: Vec<Shortcut>,
        calls: Vec<&'static str>,
        fail_register_at: Option<usize>,
        register_count: usize,
        fail_unregister_at: Option<usize>,
        unregister_count: usize,
    }
    impl HotkeyRegistrar for FakeHotkeyRegistrar {
        fn unregister_all(&mut self) -> Result<(), String> {
            self.calls.push("unregister");
            self.unregister_count += 1;
            if self.fail_unregister_at == Some(self.unregister_count) {
                return Err("cleanup denied".into());
            }
            self.installed.clear();
            Ok(())
        }
        fn register(&mut self, shortcut: Shortcut) -> Result<(), String> {
            self.calls.push("register");
            self.register_count += 1;
            if self.fail_register_at == Some(self.register_count) {
                return Err("HotKey already registered".into());
            }
            self.installed.push(shortcut);
            Ok(())
        }
    }
    fn test_hotkeys(key: &str) -> RegisteredHotkeys {
        parse_registered_hotkeys(HotkeySettings {
            dictation: vec![key.into(), "MouseX2".into()],
            command_mode: vec!["Ctrl+Alt+E".into()],
        })
        .unwrap()
    }

    #[test]
    fn desktop_starts_unbound_and_applies_only_loaded_shortcuts() {
        let desktop = DesktopState::default();
        let mut current = desktop.hotkeys.lock().unwrap();
        assert_eq!(*current, RegisteredHotkeys::default());
        let loaded = parse_registered_hotkeys(HotkeySettings {
            dictation: vec!["Ctrl+Shift+Alt+F11".into()],
            command_mode: vec!["Ctrl+Shift+Alt+F12".into()],
        }).unwrap();
        let mut os = FakeHotkeyRegistrar::default();
        replace_hotkeys(&mut *current, loaded.clone(), false, &mut os).unwrap();
        assert_eq!(*current, loaded);
        assert_eq!(os.installed, vec![parse_shortcut("Ctrl+Shift+Alt+F11").unwrap(), parse_shortcut("Ctrl+Shift+Alt+F12").unwrap()]);
    }

    #[test]
    fn hotkey_conflict_discards_partial_bindings_and_allows_retry() {
        let mut current = test_hotkeys("Ctrl+Alt+B");
        let requested = test_hotkeys("Ctrl+Alt+Space");
        let mut os = FakeHotkeyRegistrar {
            fail_register_at: Some(2),
            ..Default::default()
        };
        let error = replace_hotkeys(&mut current, requested.clone(), false, &mut os).unwrap_err();
        assert!(error.contains("HotKey already registered"));
        assert_eq!(current, RegisteredHotkeys::default());
        assert!(os.installed.is_empty());
        assert_eq!(
            os.calls,
            vec!["unregister", "register", "register", "unregister"]
        );
        os.fail_register_at = None;
        replace_hotkeys(&mut current, requested.clone(), false, &mut os).unwrap();
        assert_eq!(current, requested);
        assert_eq!(os.installed.len(), 2);
    }

    #[test]
    fn hotkey_conflict_reports_failed_cleanup_without_activating_bindings() {
        let mut current = test_hotkeys("Ctrl+Alt+B");
        let mut os = FakeHotkeyRegistrar {
            fail_register_at: Some(2),
            fail_unregister_at: Some(2),
            ..Default::default()
        };
        let error = replace_hotkeys(&mut current, test_hotkeys("Ctrl+Alt+Space"), false, &mut os)
            .unwrap_err();
        assert!(error.contains("HotKey already registered"));
        assert!(error.contains("cleanup denied"));
        assert_eq!(current, RegisteredHotkeys::default());
        assert_eq!(os.installed.len(), 1); // Present at OS level, inert in handlers.
        os.fail_register_at = None;
        os.fail_unregister_at = None;
        replace_hotkeys(&mut current, test_hotkeys("Ctrl+Alt+Space"), false, &mut os).unwrap();
        assert_eq!(os.installed.len(), 2);
    }

    #[test]
    fn only_bound_side_buttons_are_swallowed_and_failed_registration_clears_them() {
        let mut current = test_hotkeys("Ctrl+Alt+B");
        assert_eq!(bound_mouse_buttons(&current), (false, true));
        let command_x1 = parse_registered_hotkeys(HotkeySettings {
            dictation: vec!["Ctrl+Alt+B".into()],
            command_mode: vec!["MouseX1".into()],
        })
        .unwrap();
        assert_eq!(bound_mouse_buttons(&command_x1), (true, false));
        let mut os = FakeHotkeyRegistrar {
            fail_register_at: Some(1),
            ..Default::default()
        };
        assert!(replace_hotkeys(&mut current, command_x1, false, &mut os).is_err());
        assert_eq!(bound_mouse_buttons(&current), (false, false));
    }

    #[test]
    fn sent_paste_does_not_claim_verified_delivery() {
        use windows_paste::{ClipboardMutation, PasteReason, PasteResult};
        let sent = paste_feedback(&PasteResult::skipped(PasteReason::InputSent), true);
        assert_eq!(sent.delivery, Some("requested"));
        assert!(sent.message.unwrap().contains("unverified"));
        let uncertain = paste_feedback(&PasteResult::skipped(PasteReason::InputPartial), true);
        assert!(uncertain.message.unwrap().contains("Check the field"));
        let changed = paste_feedback(&PasteResult::skipped(PasteReason::ChangedTarget), true);
        assert_eq!(changed.delivery, Some("recovery"));
        assert!(changed.message.unwrap().contains("focused field changed"));
        let unrecorded = paste_feedback(&PasteResult {
            reason: PasteReason::InputSent,
            clipboard: ClipboardMutation::RestoreFailed,
        }, false);
        assert!(unrecorded.message.as_ref().unwrap().contains("could not be recorded"));
        assert!(unrecorded.message.unwrap().contains("could not restore"));
    }

    #[test]
    fn recording_bar_hides_completed_feedback_without_hiding_a_new_recording() {
        let mut lifetime = RecordingBarLifetime::default();
        assert_eq!(lifetime.update("recording"), None);
        let finished = lifetime.update("ready").unwrap();
        assert!(lifetime.may_hide(finished));
        assert_eq!(lifetime.update("ready"), None);
        assert!(lifetime.may_hide(finished));
        assert!(lifetime.dismiss(finished));
        assert_eq!(lifetime.update("ready"), None);
        assert!(lifetime.dismissed, "a repeated terminal status must stay hidden");
        lifetime.update("recording");
        assert!(!lifetime.dismissed);
        assert!(!lifetime.may_hide(finished));
        let failed = lifetime.update("error").unwrap();
        assert!(lifetime.may_hide(failed));
        lifetime.invalidate();
        assert!(!lifetime.may_hide(failed));
    }

    #[cfg(windows)]
    #[test]
    fn showing_feedback_never_activates_the_overlay_window() {
        use windows_sys::Win32::UI::WindowsAndMessaging::*;
        use windows_sys::Win32::UI::Input::KeyboardAndMouse::GetActiveWindow;
        unsafe {
            let class: Vec<u16> = "STATIC\0".encode_utf16().collect();
            let window = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, class.as_ptr(), class.as_ptr(),
                WS_POPUP, -32000, -32000, 1, 1, std::ptr::null_mut(),
                std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null());
            assert!(!window.is_null());
            let active = GetActiveWindow();
            set_feedback_window_visible(window, true);
            let after = GetActiveWindow();
            let visible = IsWindowVisible(window);
            set_feedback_window_visible(window, false);
            let hidden = IsWindowVisible(window);
            set_feedback_window_visible(window, true);
            let reopened_active = GetActiveWindow();
            DestroyWindow(window);
            assert_ne!(visible, 0);
            assert_eq!(hidden, 0);
            assert_eq!(after, active, "feedback took keyboard focus from the target");
            assert_eq!(reopened_active, active, "reopening feedback took keyboard focus");
        }
    }

    #[test]
    fn hotkey_initial_unregister_failure_preserves_existing_bindings() {
        let original = test_hotkeys("Ctrl+Alt+B");
        let mut current = original.clone();
        let mut os = FakeHotkeyRegistrar {
            fail_unregister_at: Some(1),
            ..Default::default()
        };
        assert!(
            replace_hotkeys(&mut current, test_hotkeys("Ctrl+Alt+Space"), false, &mut os).is_err()
        );
        assert_eq!(current, original);
        assert_eq!(os.calls, vec!["unregister"]);
    }

    #[test]
    fn hotkey_identical_configuration_is_noop_even_during_session() {
        let mut current = test_hotkeys("Ctrl+Alt+B");
        let same = current.clone();
        let mut os = FakeHotkeyRegistrar::default();
        replace_hotkeys(&mut current, same, true, &mut os).unwrap();
        assert!(os.calls.is_empty());
    }

    #[test]
    fn hotkey_changes_during_session_are_rejected_without_os_calls() {
        let original = test_hotkeys("Ctrl+Alt+B");
        let mut current = original.clone();
        let mut os = FakeHotkeyRegistrar::default();
        let error = replace_hotkeys(&mut current, test_hotkeys("Ctrl+Alt+Space"), true, &mut os)
            .unwrap_err();
        assert!(error.contains("Finish or cancel"));
        assert_eq!(current, original);
        assert!(os.calls.is_empty());
    }

    #[test]
    fn parses_ctrl_alt_space() {
        let shortcut = parse_shortcut("Ctrl+Alt+Space").unwrap();

        assert!(shortcut.mods.contains(Modifiers::CONTROL));
        assert!(shortcut.mods.contains(Modifiers::ALT));
        assert_eq!(shortcut.key, Code::Space);
    }

    #[test]
    fn parses_single_letter_command_shortcut() {
        let shortcut = parse_shortcut("control + alt + e").unwrap();

        assert!(shortcut.mods.contains(Modifiers::CONTROL));
        assert!(shortcut.mods.contains(Modifiers::ALT));
        assert_eq!(shortcut.key, Code::KeyE);
    }

    #[test]
    fn rejects_shortcuts_without_non_modifier_key() {
        let error = parse_shortcut("Ctrl+Alt").unwrap_err();

        assert!(error.contains("one non-modifier key"));
    }

    #[test]
    fn parses_keyboard_and_mouse_bindings() {
        let bindings =
            parse_hotkey_bindings(&["Ctrl+Alt+Space".to_string(), "MouseX1".to_string()]).unwrap();

        assert_eq!(bindings.keyboard.len(), 1);
        assert_eq!(bindings.mouse, vec![MouseButtonBinding::X1]);
    }

    #[test]
    fn rejects_duplicate_bindings_across_modes() {
        let error = parse_registered_hotkeys(HotkeySettings {
            dictation: vec!["Ctrl+Alt+Space".to_string(), "MouseX1".to_string()],
            command_mode: vec!["Ctrl+Alt+E".to_string(), "Mouse 4".to_string()],
        })
        .unwrap_err();

        assert!(error.contains("must be different"));
    }

    #[test]
    fn finds_project_root_from_release_executable_path() {
        let root = std::env::temp_dir().join(format!(
            "localflow-root-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let release_dir = root.join("src-tauri").join("target").join("release");
        let runtime_dir = root.join("services").join("runtime");
        std::fs::create_dir_all(&release_dir).unwrap();
        std::fs::create_dir_all(&runtime_dir).unwrap();
        std::fs::write(root.join("pyproject.toml"), "").unwrap();
        std::fs::write(runtime_dir.join("api.py"), "").unwrap();

        let found = find_project_root_from_candidates([release_dir.join("localflow.exe")]).unwrap();

        assert_eq!(found, root);
        std::fs::remove_dir_all(found).unwrap();
    }

    #[derive(Debug)]
    struct FakeVolumeEndpoint {
        volume: f32,
        muted: bool,
        changes: Vec<String>,
    }

    impl VolumeEndpoint for FakeVolumeEndpoint {
        fn get_volume(&mut self) -> Result<f32, String> {
            Ok(self.volume)
        }

        fn set_volume(&mut self, volume: f32) -> Result<(), String> {
            self.volume = volume;
            self.changes.push(format!("volume:{volume:.2}"));
            Ok(())
        }

        fn get_muted(&mut self) -> Result<bool, String> {
            Ok(self.muted)
        }

        fn set_muted(&mut self, muted: bool) -> Result<(), String> {
            self.muted = muted;
            self.changes.push(format!("muted:{muted}"));
            Ok(())
        }
    }

    #[test]
    fn ducks_and_restores_volume_once_per_dictation_hold() {
        let mut endpoint = FakeVolumeEndpoint {
            volume: 0.75,
            muted: false,
            changes: Vec::new(),
        };
        let mut state = AudioDuckingState::default();
        let settings = AudioDuckingSettings {
            enabled: true,
            target_volume: 0.08,
        };

        apply_audio_ducking(&mut endpoint, settings, &mut state).unwrap();
        apply_audio_ducking(&mut endpoint, settings, &mut state).unwrap();
        restore_audio_ducking(&mut endpoint, &mut state).unwrap();

        assert_eq!(endpoint.volume, 0.75);
        assert_eq!(
            endpoint.changes,
            vec![
                "volume:0.08".to_string(),
                "volume:0.75".to_string(),
                "muted:false".to_string()
            ]
        );
    }

    #[test]
    fn skips_ducking_when_disabled_or_already_quieter_than_target() {
        let mut endpoint = FakeVolumeEndpoint {
            volume: 0.05,
            muted: false,
            changes: Vec::new(),
        };
        let mut state = AudioDuckingState::default();

        apply_audio_ducking(
            &mut endpoint,
            AudioDuckingSettings {
                enabled: true,
                target_volume: 0.08,
            },
            &mut state,
        )
        .unwrap();
        apply_audio_ducking(
            &mut endpoint,
            AudioDuckingSettings {
                enabled: false,
                target_volume: 0.08,
            },
            &mut state,
        )
        .unwrap();

        assert!(endpoint.changes.is_empty());
    }
}

mod caret;
mod session;
mod windows_paste;
#[cfg(test)]
mod ownership_tests {
    use super::*;
    struct Endpoint(f32, bool);
    impl VolumeEndpoint for Endpoint {
        fn get_volume(&mut self) -> Result<f32, String> {
            Ok(self.0)
        }
        fn set_volume(&mut self, v: f32) -> Result<(), String> {
            self.0 = v;
            Ok(())
        }
        fn get_muted(&mut self) -> Result<bool, String> {
            Ok(self.1)
        }
        fn set_muted(&mut self, v: bool) -> Result<(), String> {
            self.1 = v;
            Ok(())
        }
    }
    #[test]
    fn preserves_user_volume_and_mute_changes() {
        for user in [(0.4, false), (0.08, true)] {
            let mut e = Endpoint(0.75, false);
            let mut s = AudioDuckingState::default();
            apply_audio_ducking(&mut e, AudioDuckingSettings::default(), &mut s).unwrap();
            e.0 = user.0;
            e.1 = user.1;
            restore_audio_ducking(&mut e, &mut s).unwrap();
            assert_eq!((e.0, e.1), user);
        }
    }
}
#[cfg(test)]
mod child_tests {
    use super::*;
    #[derive(Default)]
    struct FakeChild {
        killed: usize,
        reaped: usize,
        exited: bool,
        kill_fails: bool,
        survives_kill: bool,
    }
    impl OwnedProcess for FakeChild {
        fn exited(&mut self) -> Result<bool, String> {
            Ok(self.exited)
        }
        fn kill_owned(&mut self) -> Result<(), String> {
            self.killed += 1;
            if self.kill_fails {
                return Err("kill denied".into());
            }
            if !self.survives_kill {
                self.exited = true;
            }
            Ok(())
        }
        fn reap(&mut self) -> Result<(), String> {
            self.reaped += 1;
            Ok(())
        }
    }
    #[test]
    fn startup_timeout_kills_and_reaps_only_owned_child() {
        let mut owned = FakeChild::default();
        let unrelated = FakeChild::default();
        finish_owned_child(&mut owned, 0, || {}).unwrap();
        assert_eq!((owned.killed, owned.reaped), (1, 1));
        assert_eq!((unrelated.killed, unrelated.reaped), (0, 0));
    }
    #[test]
    fn failed_kill_never_blocks_on_reap_and_retains_owned_handle() {
        let mut slot = Some(FakeChild {
            kill_fails: true,
            ..Default::default()
        });
        assert!(cleanup_owned_slot(&mut slot, 0, || {}).is_err());
        let child = slot
            .as_ref()
            .expect("ownership must survive failed cleanup");
        assert_eq!((child.killed, child.reaped), (1, 0));
    }
    #[test]
    fn successful_kill_with_surviving_child_has_bounded_cleanup_and_keeps_handle() {
        let mut slot = Some(FakeChild {
            survives_kill: true,
            ..Default::default()
        });
        let mut pauses = 0;
        assert!(cleanup_owned_slot(&mut slot, 0, || {
            pauses += 1;
        })
        .is_err());
        assert!(pauses <= 20);
        assert_eq!(slot.as_ref().unwrap().reaped, 0);
    }
    #[test]
    fn graceful_exit_is_reaped_without_kill() {
        let mut owned = FakeChild {
            exited: true,
            ..Default::default()
        };
        finish_owned_child(&mut owned, 2, || {}).unwrap();
        assert_eq!((owned.killed, owned.reaped), (0, 1));
    }
}
trait OwnedProcess {
    fn exited(&mut self) -> Result<bool, String>;
    fn kill_owned(&mut self) -> Result<(), String>;
    fn reap(&mut self) -> Result<(), String>;
}
fn finish_owned_child<P: OwnedProcess>(
    child: &mut P,
    attempts: usize,
    mut pause: impl FnMut(),
) -> Result<(), String> {
    for _ in 0..attempts {
        if child.exited()? {
            return child.reap();
        }
        pause();
    }
    if child.exited()? {
        return child.reap();
    }
    if let Err(err) = child.kill_owned() {
        // Natural exit can race kill. Never block on wait while it may still be alive.
        if child.exited()? {
            return child.reap();
        }
        return Err(format!(
            "Could not terminate owned engine; retaining process handle: {err}"
        ));
    }
    for _ in 0..20 {
        if child.exited()? {
            return child.reap();
        }
        pause();
    }
    Err("Owned engine did not exit after termination; retaining process handle.".into())
}
impl OwnedProcess for Child {
    fn exited(&mut self) -> Result<bool, String> {
        self.try_wait()
            .map(|s| s.is_some())
            .map_err(|e| e.to_string())
    }
    fn kill_owned(&mut self) -> Result<(), String> {
        self.kill().map_err(|e| e.to_string())
    }
    fn reap(&mut self) -> Result<(), String> {
        self.wait().map(|_| ()).map_err(|e| e.to_string())
    }
}

fn cleanup_owned_slot<P: OwnedProcess>(
    slot: &mut Option<P>,
    attempts: usize,
    pause: impl FnMut(),
) -> Result<(), String> {
    let result = if let Some(child) = slot.as_mut() {
        finish_owned_child(child, attempts, pause)
    } else {
        Ok(())
    };
    if result.is_ok() {
        slot.take();
    }
    result
}
#[cfg(test)]
mod dispatcher_watch_tests {
    use super::*;
    #[test]
    fn lost_start_unknown_status_keeps_watch_and_cancel_until_definitive_rejection() {
        let identity = EngineIdentity {
            engine_id: "engine".into(),
            token: "fake-secret".into(),
        };
        let mut control = SessionControl::default();
        control.press("key:1", "dictation", "session");
        let (watch, retry) = dispatch_watch(
            Some(control.settle_transport_failure("session")),
            "session",
            &identity,
        );
        assert_eq!(watch.unwrap().0, "session");
        let seq = retry.unwrap().0.command_seq;
        let idle = serde_json::json!({"state":"idle","session_id":null,"busy":false});
        let (watch, retry) = dispatch_watch(
            Some(control.settle_status("session", &idle)),
            "session",
            &identity,
        );
        assert_eq!(watch.unwrap().0, "session");
        assert_eq!(retry.unwrap().0.command_seq, seq);
        let ack = serde_json::json!({"accepted":false,"reason":"stale_session","state":"idle","session_id":null,"busy":false});
        let (watch, retry) = dispatch_watch(
            Some(control.settle_command("session", &ack)),
            "session",
            &identity,
        );
        assert!(watch.is_none() && retry.is_none() && control.active.is_none());
    }
    #[test]
    fn cancelled_busy_keeps_watch_without_retry_then_idle_finishes_watch() {
        let identity = EngineIdentity {
            engine_id: "engine".into(),
            token: "fake-secret".into(),
        };
        let mut control = SessionControl::default();
        control.press("key:1", "dictation", "session");
        control.cancel();
        let busy = serde_json::json!({"state":"cancelled","session_id":"session","busy":true});
        for _ in 0..3 {
            let (watch, retry) = dispatch_watch(
                Some(control.settle_transport_failure("session")),
                "session",
                &identity,
            );
            assert!(watch.is_some() && retry.is_some());
            let (watch, retry) = dispatch_watch(
                Some(control.settle_status("session", &busy)),
                "session",
                &identity,
            );
            assert!(watch.is_some() && retry.is_none());
        }
        let done = serde_json::json!({"state":"cancelled","session_id":"session","busy":false});
        let (watch, retry) = dispatch_watch(
            Some(control.settle_status("session", &done)),
            "session",
            &identity,
        );
        assert!(watch.is_none() && retry.is_none() && control.active.is_none());
        // The transport rejects an old reply after engine retirement before settlement.
        let (watch, retry) = dispatch_watch(None, "session", &identity);
        assert!(watch.is_none() && retry.is_none());
    }
}
