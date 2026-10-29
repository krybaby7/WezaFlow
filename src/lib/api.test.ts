import { describe, expect, it, vi } from "vitest";

import { RuntimeApiClient } from "./api";

const jsonResponse = (payload: unknown) =>
  new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => ({status:"ok",state:"processing"})) }));

describe("RuntimeApiClient", () => {
  it("uses the authenticated Rust bridge by default without browser fetch", async () => {
    const { invoke } = await import("@tauri-apps/api/core");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({status:"ok"})));
    await new RuntimeApiClient().getStatus();
    expect(invoke).toHaveBeenCalledWith("runtime_request", {method:"GET",path:"/status",body:null});
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
  it("posts start and stop requests to the Python runtime API", async () => {
    const fetcher = vi.fn(async () => jsonResponse({ status: "ok" }));
    const client = new RuntimeApiClient("http://127.0.0.1:8765", fetcher);

    await client.startRuntime("command", "en");
    await client.stopRuntime("en");

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8765/runtime/start",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ mode: "command", language: "en" })
      })
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8765/runtime/stop",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ language: "en" })
      })
    );
  });

  it("posts runtime warmup requests to the Python runtime API", async () => {
    const fetcher = vi.fn(async () => jsonResponse({ status: "ok" }));
    const client = new RuntimeApiClient("http://127.0.0.1:8765", fetcher);

    await client.warmRuntime();

    expect(fetcher).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/runtime/warmup",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("sends explicit profile application metadata only on request", async () => {
    const fetcher=vi.fn(async () => jsonResponse({settings:{runtime:{profile:"quality"}}}));
    const client=new RuntimeApiClient("http://fixture",fetcher);
    const settings={runtime:{profile:"quality"}} as import("./api").Settings;
    await client.saveSettings(settings,true);
    expect(fetcher).toHaveBeenLastCalledWith("http://fixture/settings",expect.objectContaining({body:JSON.stringify({settings,apply_profile_defaults:true})}));
    await client.saveSettings(settings);
    expect(fetcher).toHaveBeenLastCalledWith("http://fixture/settings",expect.objectContaining({body:JSON.stringify({settings})}));
  });

  it("roundtrips settings and validates HTTP failures", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: "ok", settings: { hotkeys: {} } }))
      .mockResolvedValueOnce(new Response("nope", { status: 500 }));
    const client = new RuntimeApiClient("http://127.0.0.1:8765", fetcher);

    await expect(client.getSettings()).resolves.toEqual({ hotkeys: {} });
    await expect(client.getStatus()).rejects.toThrow("Runtime API request failed");
  });

  it("handles pending corrections and learning suggestions", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          pending: [
            {
              id: "7",
              original: "local flow",
              raw_transcript: "local flow",
              app_name: "Notes",
              window_title: "Draft",
              detected_at: "2026-05-10T09:30:00Z"
            }
          ]
        })
      )
      .mockResolvedValueOnce(jsonResponse({ status: "confirmed" }))
      .mockResolvedValueOnce(jsonResponse({ status: "dismissed" }))
      .mockResolvedValueOnce(
        jsonResponse({
          suggestions: [
            { kind: "vocabulary", phrase: "LocalFlow", count: 3 },
            { kind: "snippet", expansion: "Thanks for the update.", count: 2 }
          ]
        })
      );
    const client = new RuntimeApiClient("http://127.0.0.1:8765", fetcher);

    await expect(client.getPendingCorrections()).resolves.toEqual([
      {
        id: "7",
        original: "local flow",
        raw_transcript: "local flow",
        app_name: "Notes",
        window_title: "Draft",
        detected_at: "2026-05-10T09:30:00Z"
      }
    ]);
    await client.confirmPendingCorrection("7", "local flow", "LocalFlow");
    await client.dismissPendingCorrection("7");
    await expect(client.getLearningSuggestions()).resolves.toEqual([
      { kind: "vocabulary", phrase: "LocalFlow", count: 3 },
      { kind: "snippet", expansion: "Thanks for the update.", count: 2 }
    ]);

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8765/corrections/pending",
      expect.any(Object)
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8765/corrections/pending/7/confirm",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ original: "local flow", corrected: "LocalFlow" })
      })
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8765/corrections/pending/7/dismiss",
      expect.objectContaining({ method: "POST" })
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8765/learning/suggestions",
      expect.any(Object)
    );
  });
});
