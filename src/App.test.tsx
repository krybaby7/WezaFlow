import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App, type RuntimeClient } from "./App";
import { bringLocalFlowWindowForward } from "./lib/tauri";

const tauriMocks = vi.hoisted(() => ({
  bringLocalFlowWindowForward: vi.fn(async () => undefined),
  configureDesktopSettings: vi.fn(async () => undefined),
  getDesktopWindowLabel: vi.fn(() => "main"),
  listenToRecordingBarState: vi.fn(async (_callback?: unknown) => () => undefined)
}));

vi.mock("./lib/tauri", () => ({
  bringLocalFlowWindowForward: tauriMocks.bringLocalFlowWindowForward,
  configureDesktopSettings: tauriMocks.configureDesktopSettings,
  getDesktopWindowLabel: tauriMocks.getDesktopWindowLabel,
  listenToRecordingBarState: tauriMocks.listenToRecordingBarState
}));

function fakeClient(overrides: Partial<RuntimeClient> = {}): RuntimeClient {
  return {
    getStatus: vi.fn(async () => ({
      status: "ok",
      state: "idle",
      mode: "idle",
      profile: "low-impact",
      quiet_mode: false,
      quality_fallback: false,
      last_error: null
    })),
    getSettings: vi.fn(async () => ({
      hotkeys: { dictation: "Ctrl+Alt+Space", command_mode: "Ctrl+Alt+E" },
      models: { whisper: "small.en", whisper_cpu_threads: 2 },
      runtime: {
        profile: "low-impact",
        language: "en",
        quiet_mode: false,
        quality_fallback: false,
        system_audio_ducking: true,
        system_audio_duck_volume: 8,
        use_ollama: false
      }
    })),
    saveSettings: vi.fn(async (settings) => settings),
    getDiagnostics: vi.fn(async () => ["Runtime: profile=low-impact"]),
    warmRuntime: vi.fn(async () => undefined),
    checkRuntime: vi.fn(async () => []),
    listVocabulary: vi.fn(async () => []),
    addVocabulary: vi.fn(async () => []),
    deleteVocabulary: vi.fn(async () => []),
    listCorrections: vi.fn(async () => []),
    addCorrection: vi.fn(async () => []),
    deleteCorrection: vi.fn(async () => []),
    listSnippets: vi.fn(async () => []),
    saveSnippet: vi.fn(async () => []),
    deleteSnippet: vi.fn(async () => []),
    getPendingCorrections: vi.fn(async () => []),
    confirmPendingCorrection: vi.fn(async () => []),
    dismissPendingCorrection: vi.fn(async () => undefined),
    getLearningSuggestions: vi.fn(async () => []),
    ...overrides
  };
}

describe("App", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    tauriMocks.getDesktopWindowLabel.mockReturnValue("main");
  });

  it("renders only the compact recording bar in the recording-bar window", () => {
    tauriMocks.getDesktopWindowLabel.mockReturnValue("recording-bar");

    render(<App client={fakeClient()} />);

    expect(screen.getByRole("status", { name: "Recording feedback" })).toBeInTheDocument();
    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.queryByText(/^\d+:\d{2}$/)).not.toBeInTheDocument();
    expect(screen.queryByRole("tablist", { name: "Settings sections" })).not.toBeInTheDocument();
  });

  it("renders the normal settings shell in the main window", async () => {
    render(<App client={fakeClient()} />);

    expect(await screen.findByText("WezaFlow")).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Settings sections" })).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Recording feedback" })).not.toBeInTheDocument();
  });

  it("renders status and diagnostics tabs from the runtime API", async () => {
    render(<App client={fakeClient()} />);

    expect(await screen.findByText("WezaFlow")).toBeInTheDocument();
    expect(screen.getByAltText("WezaFlow logo")).toBeInTheDocument();
    expect(screen.getByText("low-impact")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Diagnostics" }));

    expect(await screen.findByText("Runtime: profile=low-impact")).toBeInTheDocument();
  });

  it("uses the transparent logo asset in the header", async () => {
    render(<App client={fakeClient()} />);

    const logo = await screen.findByAltText("WezaFlow logo");

    expect(logo).toHaveAttribute("src", expect.stringContaining("Logo-no-bg.png"));
  });

  it("marks the document and body as transparent recording-bar chrome", () => {
    tauriMocks.getDesktopWindowLabel.mockReturnValue("recording-bar");

    render(<App client={fakeClient()} />);

    expect(document.documentElement).toHaveClass("recording-bar-window");
    expect(document.body).toHaveClass("recording-bar-window");
  });

  it("does not launch model warmup after loading settings", async () => {
    const warmRuntime = vi.fn(async () => undefined);
    render(<App client={fakeClient({ warmRuntime })} />);

    await screen.findByText("Runtime API connected");

    expect(warmRuntime).not.toHaveBeenCalled();
  });

  it("shows the capture limit warning on the originating result", async () => {
    const client = fakeClient({getStatus: vi.fn(async () => ({status:"ok",state:"ready",mode:"dictation",profile:"low-impact",quiet_mode:false,quality_fallback:false,last_error:null,session_id:"one",result_session_id:"one",result:{raw_transcript:"hello",polished_text:"Hello.",diagnostics:{capture:{terminal_code:"duration_limit"}}}}))});
    render(<App client={client} />);
    expect(await screen.findByText("This saved result contains only the first five minutes of its recording.")).toBeInTheDocument();
  });

  it.each(["starting", "recording", "error", "no_speech", "cancelled"])("qualifies the retained earlier result in %s", async (state) => {
    const client = fakeClient({getStatus: vi.fn(async () => ({status:"ok",state,mode:"dictation",profile:"low-impact",quiet_mode:false,quality_fallback:false,last_error:null,session_id:"new",result_session_id:"old",result:{raw_transcript:"hello",polished_text:"Hello.",diagnostics:{capture:{terminal_code:"duration_limit"}}}}))});
    render(<App client={client} />);
    await screen.findByLabelText("Recoverable dictation");
    expect(screen.getByText("This saved result contains only the first five minutes of its recording.")).toBeInTheDocument();
  });

  it("does not qualify a new unlimited result using the earlier capture status", async () => {
    const client = fakeClient({getStatus: vi.fn(async () => ({status:"ok",state:"ready",mode:"dictation",profile:"low-impact",quiet_mode:false,quality_fallback:false,last_error:null,session_id:"new",result_session_id:"new",capture:{terminal_code:"duration_limit"},result:{raw_transcript:"new",polished_text:"New result."}}))});
    render(<App client={client} />);
    expect(await screen.findByLabelText("Recoverable dictation")).toHaveValue("New result.");
    expect(screen.queryByText("This saved result contains only the first five minutes of its recording.")).not.toBeInTheDocument();
  });

  it("offers explicit recovery copy and identifies a retained earlier result", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {configurable: true, value: {writeText}});
    const client = fakeClient({getStatus: vi.fn(async () => ({status:"ok",state:"error",mode:"dictation",profile:"low-impact",quiet_mode:false,quality_fallback:false,last_error:"Audio failed",session_id:"new",result_session_id:"previous",result:{raw_transcript:"hello",polished_text:"Hello."}}))});
    render(<App client={client} />);
    expect(await screen.findByLabelText("Recoverable dictation")).toHaveValue("Hello.");
    expect(screen.getByText("Previous session result")).toBeInTheDocument();
    expect(writeText).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", {name:"Copy result"}));
    expect(writeText).toHaveBeenCalledWith("Hello.");
  });

  it("renders acknowledged processing, recovery and cancellation states after release", async () => {
    tauriMocks.getDesktopWindowLabel.mockReturnValue("recording-bar");
    render(<App client={fakeClient()} />);
    const handler = tauriMocks.listenToRecordingBarState.mock.calls[0]?.[0] as unknown as (value: {state:string;mode:string}) => void;
    for (const [state,label] of [["starting","Starting"],["recording","Listening"],["draining","Finishing recording"],["processing","Processing"],["ready","Ready to copy"],["cancelled","Cancelled"],["no_speech","No speech"],["error","Error"]]) {
      await act(async () => handler({state,mode:"dictation"}));
      expect(screen.getByText(label)).toBeInTheDocument();
      expect(screen.getByRole("status",{name:"Recording feedback"})).toBeInTheDocument();
    }
  });

  it("keeps persisted settings when shortcuts cannot reconfigure during capture", async () => {
    const busy = {status:"ok",state:"processing",mode:"dictation",profile:"low-impact",quiet_mode:false,quality_fallback:false,last_error:null,busy:true};
    const client=fakeClient({getStatus:vi.fn(async () => busy)});
    render(<App client={client} />);
    await screen.findByText("Runtime API connected");
    tauriMocks.configureDesktopSettings.mockRejectedValueOnce(new Error("active session"));
    await userEvent.click(screen.getByRole("tab",{name:"Dictation"}));
    await userEvent.click(screen.getByRole("button",{name:"Save settings"}));
    expect(await screen.findByText("Settings saved for the next session; desktop shortcuts will apply when recording finishes")).toBeInTheDocument();
    expect(client.saveSettings).toHaveBeenCalledOnce();
    expect(screen.getByLabelText(/Quality fallback/)).toBeDisabled();
  });

  it("saves edited hotkeys through the runtime API", async () => {
    const saveSettings = vi.fn(async (settings) => settings);
    render(<App client={fakeClient({ saveSettings })} />);

    await userEvent.click(await screen.findByRole("tab", { name: "Hotkeys" }));
    const dictation = await screen.findByLabelText("Dictation hotkeys 1");
    fireEvent.change(dictation, { target: { value: "Ctrl+Shift+Space" } });
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(saveSettings).toHaveBeenCalled());
    expect(saveSettings.mock.calls[0][0].hotkeys.dictation).toEqual(["Ctrl+Shift+Space"]);
  });

  it("captures a mouse side button as a hotkey", async () => {
    const saveSettings = vi.fn(async (settings) => settings);
    render(<App client={fakeClient({ saveSettings })} />);

    await userEvent.click(await screen.findByRole("tab", { name: "Hotkeys" }));
    await userEvent.click(screen.getAllByRole("button", { name: "Capture" })[0]);
    fireEvent.mouseDown(await screen.findByLabelText("Dictation hotkeys 1"), { button: 3 });
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(saveSettings).toHaveBeenCalled());
    expect(saveSettings.mock.calls[0][0].hotkeys.dictation).toEqual(["MouseX1"]);
  });

  it("saves audio ducking settings from the dictation tab", async () => {
    const saveSettings = vi.fn(async (settings) => settings);
    render(<App client={fakeClient({ saveSettings })} />);

    await userEvent.click(await screen.findByRole("tab", { name: "Dictation" }));
    await userEvent.click(await screen.findByLabelText("System audio ducking"));
    fireEvent.change(screen.getByLabelText("Ducked system volume"), { target: { value: "12" } });
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(saveSettings).toHaveBeenCalled());
    expect(saveSettings.mock.calls[0][0].runtime.system_audio_ducking).toBe(false);
    expect(saveSettings.mock.calls[0][0].runtime.system_audio_duck_volume).toBe(12);
  });

  it("switches to experimental local multilingual with auto language and local defaults", async () => {
    const client = fakeClient();
    render(<App client={client} />);
    await userEvent.click(await screen.findByRole("tab", {name: "Dictation"}));
    fireEvent.change(screen.getByLabelText("ASR profile"), {target: {value: "local-multilingual"}});
    expect(screen.getByLabelText("Language")).toHaveValue("auto");
    for (const name of ["English (en)", "Arabic (ar)", "French (fr)"]) {
      expect(screen.getByRole("option", {name})).not.toBeDisabled();
    }
    expect(screen.getByLabelText("Local model folder")).toHaveValue("");
    expect(screen.getByLabelText("Local device")).toHaveValue("cuda");
    expect(screen.getByLabelText("Local CPU threads")).toHaveValue(2);
    await userEvent.click(screen.getByRole("button", {name: "Save settings"}));
    await waitFor(() => expect(client.saveSettings).toHaveBeenCalledWith(expect.objectContaining({
      local_asr: expect.objectContaining({compute_type: "int8_float16", startup_timeout_seconds: 90})
    }), true));
  });

  it("preserves local hidden timeouts and unrelated settings while editing setup", async () => {
    const base = await fakeClient().getSettings();
    const local = {snapshot_path: "old", runtime_manifest_path: "old.json", device: "cuda" as const,
      compute_type: "custom", cpu_threads: 2, startup_timeout_seconds: 111, decode_timeout_seconds: 77,
      idle_timeout_seconds: 60, reap_timeout_seconds: 9};
    const client = fakeClient({getSettings: vi.fn(async () => ({...base, custom: "keep", local_asr: local,
      runtime: {...base.runtime, profile: "local-multilingual", language: "ar"}}))});
    render(<App client={client} />);
    await userEvent.click(await screen.findByRole("tab", {name: "Dictation"}));
    expect(screen.getByLabelText("Language")).toHaveValue("ar");
    fireEvent.change(screen.getByLabelText("Local model folder"), {target: {value: "C:/model"}});
    fireEvent.change(screen.getByLabelText("GPU runtime manifest (optional)"), {target: {value: "C:/runtime.json"}});
    fireEvent.change(screen.getByLabelText("Worker idle seconds"), {target: {value: "0"}});
    fireEvent.change(screen.getByLabelText("Local device"), {target: {value: "cpu"}});
    await userEvent.click(screen.getByRole("button", {name: "Save settings"}));
    await waitFor(() => expect(client.saveSettings).toHaveBeenCalledWith(expect.objectContaining({custom: "keep",
      models: base.models, local_asr: {...local, snapshot_path: "C:/model", runtime_manifest_path: "C:/runtime.json",
        device: "cpu", compute_type: "int8", idle_timeout_seconds: 0}}), false));
    fireEvent.change(screen.getByLabelText("Local device"), {target: {value: "cuda"}});
    await userEvent.click(screen.getByRole("button", {name: "Save settings"}));
    await waitFor(() => expect(client.saveSettings).toHaveBeenLastCalledWith(expect.objectContaining({
      local_asr: expect.objectContaining({compute_type: "int8_float16"})}), false));
  });

  it.each([
    ["STARTING", "Local speech worker: preparing. Dictation is not ready yet."],
    ["READY", "Local speech worker: ready."],
    ["STOPPED", "Local speech worker: unloaded. It will prepare on the next dictation."],
    ["REAPING", "Local speech worker: unloading. Dictation is not ready yet."],
    ["UNHEALTHY", "Local speech worker: unhealthy. Check diagnostics before retrying."]
  ])("shows %s worker status separately from dictation state", async (state, note) => {
    const base = await fakeClient().getStatus();
    render(<App client={fakeClient({getStatus: vi.fn(async () => ({...base,
      profile: "local-multilingual", asr_worker: {state, busy: false}}))})} />);
    expect(await screen.findByText(note)).toBeInTheDocument();
    expect(screen.getByText("idle")).toBeInTheDocument();
    expect(screen.queryByText("Ready to dictate")).not.toBeInTheDocument();
  });

  it("shows the distil small English ASR profile in dictation settings", async () => {
    render(<App client={fakeClient()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "Dictation" }));

    expect(screen.getByRole("option", { name: "distil-small-en" })).toBeInTheDocument();
  });

  it("shows the snappy ASR profile in dictation settings", async () => {
    render(<App client={fakeClient()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "Dictation" }));

    expect(screen.getByRole("option", { name: "snappy" })).toBeInTheDocument();
  });

  it("disables unsupported language options for English-only ASR profiles", async () => {
    render(<App client={fakeClient()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "Dictation" }));
    fireEvent.change(screen.getByRole("combobox", { name: "ASR profile" }), {
      target: { value: "snappy" }
    });

    expect(screen.getByRole("option", { name: "English (en)" })).not.toBeDisabled();
    expect(screen.getByRole("option", { name: "Auto detect" })).toBeDisabled();
    expect(screen.getByRole("option", { name: "Arabic (ar)" })).toBeDisabled();
    expect(screen.getByRole("option", { name: "French (fr)" })).toBeDisabled();
  });

  it("coerces unsupported languages to English before saving an English-only ASR profile", async () => {
    const saveSettings = vi.fn(async (settings) => settings);
    render(
      <App
        client={fakeClient({
          saveSettings,
          getSettings: vi.fn(async () => ({
            hotkeys: { dictation: "Ctrl+Alt+Space", command_mode: "Ctrl+Alt+E" },
            models: { whisper: "small.en", whisper_cpu_threads: 2 },
            runtime: {
              profile: "quality",
              language: "ar",
              quiet_mode: false,
              quality_fallback: false,
              system_audio_ducking: true,
              system_audio_duck_volume: 8,
              use_ollama: false
            }
          }))
        })}
      />
    );

    await userEvent.click(await screen.findByRole("tab", { name: "Dictation" }));
    fireEvent.change(screen.getByRole("combobox", { name: "ASR profile" }), {
      target: { value: "snappy" }
    });
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(saveSettings).toHaveBeenCalled());
    expect(saveSettings.mock.calls[0][0].runtime.profile).toBe("snappy");
    expect(saveSettings.mock.calls[0][0].runtime.language).toBe("en");
  });

  it("refreshes status and diagnostics after saving a dictation profile", async () => {
    const getStatus = vi
      .fn()
      .mockResolvedValueOnce({
        status: "ok",
        state: "idle",
        mode: "idle",
        profile: "low-impact",
        quiet_mode: false,
        quality_fallback: false,
        last_error: null
      })
      .mockResolvedValueOnce({
        status: "ok",
        state: "idle",
        mode: "idle",
        profile: "snappy",
        quiet_mode: false,
        quality_fallback: false,
        last_error: null
      });
    const getDiagnostics = vi
      .fn()
      .mockResolvedValueOnce(["Runtime: profile=low-impact model=small.en"])
      .mockResolvedValueOnce(["Runtime: profile=snappy model=small.en threads=4"]);
    const saveSettings = vi.fn(async (settings) => settings);

    const warmRuntime = vi.fn(async () => undefined);

    render(<App client={fakeClient({ getStatus, getDiagnostics, saveSettings, warmRuntime })} />);

    await screen.findByText("low-impact");
    await userEvent.click(screen.getByRole("tab", { name: "Dictation" }));
    fireEvent.change(screen.getByRole("combobox", { name: "ASR profile" }), {
      target: { value: "snappy" }
    });
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await userEvent.click(screen.getByRole("tab", { name: "Status" }));

    await waitFor(() => expect(getStatus).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getDiagnostics).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(warmRuntime).not.toHaveBeenCalled());
    expect(screen.getByText("snappy")).toBeInTheDocument();
    expect(screen.getByText(/Runtime: profile=snappy/)).toBeInTheDocument();
    expect(screen.queryByText(/Runtime: profile=low-impact model=small\.en/)).not.toBeInTheDocument();
  });

  it("sends profile-default intent only when the saved profile changes", async () => {
    const saveSettings = vi.fn(async (settings, _applyProfileDefaults?: boolean) => settings);
    render(<App client={fakeClient({saveSettings})} />);
    await screen.findByText("Runtime API connected");
    await userEvent.click(screen.getByRole("tab",{name:"Dictation"}));
    fireEvent.change(screen.getByRole("combobox",{name:"ASR profile"}),{target:{value:"quality"}});
    await userEvent.click(screen.getByRole("button",{name:"Save settings"}));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledOnce());
    expect(saveSettings.mock.calls[0][0].runtime.profile).toBe("quality");
    expect(saveSettings.mock.calls[0][1]).toBe(true);
    await screen.findByText("Settings saved");
    await userEvent.click(screen.getByRole("button",{name:"Save settings"}));
    await waitFor(() => expect(saveSettings).toHaveBeenCalledTimes(2));
    expect(saveSettings.mock.calls[1][1]).toBe(false);
  });

  it("retains profile intent after a failed settings save", async () => {
    const saveSettings = vi.fn(async (settings, _applyProfileDefaults?: boolean) => settings).mockRejectedValueOnce(new Error("unavailable"));
    render(<App client={fakeClient({saveSettings})} />);
    await screen.findByText("Runtime API connected");
    await userEvent.click(screen.getByRole("tab",{name:"Dictation"}));
    fireEvent.change(screen.getByRole("combobox",{name:"ASR profile"}),{target:{value:"quality"}});
    await userEvent.click(screen.getByRole("button",{name:"Save settings"}));
    await screen.findByText("Settings could not be saved. Your profile change is still pending; try saving again.");
    await userEvent.click(screen.getByRole("button",{name:"Save settings"}));
    await screen.findByText("Settings saved");
    expect(saveSettings.mock.calls.map(call => call[1])).toEqual([true,true]);
  });

  it("polls for a new pending correction, focuses the desktop window, and confirms it", async () => {
    vi.useFakeTimers();
    const pendingCorrection = {
      id: "42",
      original: "local flow",
      raw_transcript: "local flow",
      app_name: "Notes",
      window_title: "Draft",
      detected_at: "2026-05-10T09:30:00Z"
    };
    const getPendingCorrections = vi
      .fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([pendingCorrection])
      .mockResolvedValueOnce([]);
    const confirmPendingCorrection = vi.fn(async () => []);
    const getLearningSuggestions = vi
      .fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([{ kind: "vocabulary" as const, phrase: "LocalFlow", count: 3 }]);
    render(
      <App
        client={fakeClient({
          getPendingCorrections,
          confirmPendingCorrection,
          getLearningSuggestions
        })}
      />
    );

    expect(screen.getByText("WezaFlow")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(2000);

    expect(screen.getByText("Did this edit fix dictation?")).toBeInTheDocument();
    expect(screen.getByLabelText("Original dictated phrase")).toHaveValue("local flow");
    expect(bringLocalFlowWindowForward).toHaveBeenCalledTimes(1);
    vi.useRealTimers();

    fireEvent.change(screen.getByLabelText("Corrected phrase for pending edit"), {
      target: { value: "LocalFlow" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm correction" }));

    await waitFor(() =>
      expect(confirmPendingCorrection).toHaveBeenCalledWith("42", "local flow", "LocalFlow")
    );
    expect(await screen.findByText("Suggested vocabulary")).toBeInTheDocument();
    expect(screen.getByText("LocalFlow (3)")).toBeInTheDocument();
  });

  it("dismisses the current pending correction", async () => {
    const pendingCorrection = {
      id: "43",
      original: "open male",
      raw_transcript: "open male",
      app_name: "Mail",
      window_title: "Inbox",
      detected_at: "2026-05-10T09:31:00Z"
    };
    const dismissPendingCorrection = vi.fn(async () => undefined);
    render(
      <App
        client={fakeClient({
          getPendingCorrections: vi.fn(async () => [pendingCorrection]),
          dismissPendingCorrection
        })}
      />
    );

    expect(await screen.findByText("Did this edit fix dictation?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss correction" }));

    await waitFor(() => expect(dismissPendingCorrection).toHaveBeenCalledWith("43"));
    expect(screen.queryByText("Did this edit fix dictation?")).not.toBeInTheDocument();
  });
});
