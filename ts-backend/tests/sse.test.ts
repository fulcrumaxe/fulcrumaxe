/**
 * Tests for /feed and /events SSE routes (D#1437 P5).
 *
 * Run: bun test tests/sse.test.ts --timeout 15000
 *
 * Covers:
 *  - Auth parity: 401 no-token (auth enabled), 403 wrong-token
 *  - Auth via query param ?token= (EventSource-compatible)
 *  - Auth via Authorization: Bearer header
 *  - Auth disabled (no AF_API_AUTH_KEY) — all requests pass to stream
 *  - SSE response: content-type = text/event-stream
 *  - Initial connected frame is emitted
 *  - /feed: filters ?since= and ?filter[role]=
 *  - /events: emits _event_type field, filters ?loop_id=
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { feedHandler, eventsHandler } from "../src/routes/sse.js";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

// ---------------------------------------------------------------------------
// Per-test isolated environment helpers
// ---------------------------------------------------------------------------

interface TestEnv {
  tmpDir: string;
  feedFile: string;
  eventsBusFile: string;
  cleanup: () => void;
}

function createTestEnv(): TestEnv {
  const tmpDir = join(
    tmpdir(),
    `sse-test-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
  mkdirSync(tmpDir, { recursive: true });
  const feedFile = join(tmpDir, "agent-feed.jsonl");
  const eventsBusFile = join(tmpDir, "events-bus.jsonl");
  // Create empty files so handlers open successfully
  writeFileSync(feedFile, "");
  writeFileSync(eventsBusFile, "");

  return {
    tmpDir,
    feedFile,
    eventsBusFile,
    cleanup: () => {
      try { rmSync(tmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
    },
  };
}

// ---------------------------------------------------------------------------
// Test app factory (for auth tests via app.request())
// ---------------------------------------------------------------------------

function makeTestApp(authKey: string | undefined, teamDir: string): Hono {
  if (authKey !== undefined) {
    process.env.AF_API_AUTH_KEY = authKey;
  } else {
    delete process.env.AF_API_AUTH_KEY;
  }
  process.env.AUTONOMOUS_TEAM_DIR = teamDir;

  const app = new Hono();
  app.use("*", defaultDenyMiddleware);
  app.get("/feed", feedHandler);
  app.get("/events", eventsHandler);
  return app;
}

// ---------------------------------------------------------------------------
// Live server factory for streaming tests
// ---------------------------------------------------------------------------

interface LiveServer {
  port: number;
  baseUrl: string;
  stop: () => void;
}

function startLiveServer(authKey: string | undefined, teamDir: string): LiveServer {
  if (authKey !== undefined) {
    process.env.AF_API_AUTH_KEY = authKey;
  } else {
    delete process.env.AF_API_AUTH_KEY;
  }
  process.env.AUTONOMOUS_TEAM_DIR = teamDir;

  const app = new Hono();
  app.use("*", defaultDenyMiddleware);
  app.get("/feed", feedHandler);
  app.get("/events", eventsHandler);

  const port = 29100 + Math.floor(Math.random() * 900);
  const server = Bun.serve({
    port,
    hostname: "127.0.0.1",
    fetch: app.fetch,
  });

  return {
    port,
    baseUrl: `http://127.0.0.1:${port}`,
    stop: () => server.stop(true),
  };
}

/** Read up to maxFrames SSE frames from a live server. */
async function readLiveSSEFrames(
  url: string,
  headers: Record<string, string> = {},
  maxFrames: number,
  timeoutMs = 5000
): Promise<string[]> {
  const frames: string[] = [];
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, { headers, signal: controller.signal });
    if (!res.body) return frames;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (frames.length < maxFrames) {
      let result: { done: boolean; value: Uint8Array | undefined };
      try {
        result = await reader.read();
      } catch {
        break;
      }
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });

      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const trimmed = part.trim();
        if (trimmed) frames.push(trimmed);
        if (frames.length >= maxFrames) break;
      }
    }
    reader.cancel();
  } catch {
    // AbortError or connection closed — return what we have
  } finally {
    clearTimeout(timer);
  }

  return frames;
}

/** Parse "data: <json>" SSE frame → parsed object */
function parseSSEFrame(frame: string): unknown {
  const match = /^data: (.+)$/m.exec(frame);
  if (!match) throw new Error(`No data line in frame: ${JSON.stringify(frame)}`);
  return JSON.parse(match[1]);
}

// ---------------------------------------------------------------------------
// Saved env for restoration
// ---------------------------------------------------------------------------

let savedKey: string | undefined;
let savedTeamDir: string | undefined;

function saveEnv(): void {
  savedKey = process.env.AF_API_AUTH_KEY;
  savedTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
}

function restoreEnv(): void {
  if (savedKey !== undefined) {
    process.env.AF_API_AUTH_KEY = savedKey;
  } else {
    delete process.env.AF_API_AUTH_KEY;
  }
  if (savedTeamDir !== undefined) {
    process.env.AUTONOMOUS_TEAM_DIR = savedTeamDir;
  } else {
    delete process.env.AUTONOMOUS_TEAM_DIR;
  }
}

// ---------------------------------------------------------------------------
// Auth parity tests — /feed (via app.request())
// ---------------------------------------------------------------------------

describe("/feed — auth parity", () => {
  let env: TestEnv;

  beforeEach(() => {
    saveEnv();
    env = createTestEnv();
  });

  afterEach(() => {
    env.cleanup();
    restoreEnv();
  });

  it("401 when no token (auth enabled)", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/feed");
    expect(res.status).toBe(401);
    const body = (await res.json()) as Record<string, string>;
    expect(body.detail).toBe("unauthorized");
  });

  it("403 when wrong token in ?token=", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/feed?token=wrong-token");
    expect(res.status).toBe(403);
    const body = (await res.json()) as Record<string, string>;
    expect(body.detail).toBe("forbidden");
  });

  it("403 when wrong Bearer header", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/feed", {
      headers: { Authorization: "Bearer wrong-token" },
    });
    expect(res.status).toBe(403);
    const body = (await res.json()) as Record<string, string>;
    expect(body.detail).toBe("forbidden");
  });

  it("200 + text/event-stream when correct ?token=", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/feed?token=secret-key");
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    res.body?.cancel();
  });

  it("200 + text/event-stream when correct Bearer header", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/feed", {
      headers: { Authorization: "Bearer secret-key" },
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    res.body?.cancel();
  });

  it("200 when auth disabled (no AF_API_AUTH_KEY)", async () => {
    const app = makeTestApp(undefined, env.tmpDir);
    const res = await app.request("/feed");
    expect(res.status).toBe(200);
    res.body?.cancel();
  });
});

// ---------------------------------------------------------------------------
// Auth parity tests — /events (via app.request())
// ---------------------------------------------------------------------------

describe("/events — auth parity", () => {
  let env: TestEnv;

  beforeEach(() => {
    saveEnv();
    env = createTestEnv();
  });

  afterEach(() => {
    env.cleanup();
    restoreEnv();
  });

  it("401 when no token (auth enabled)", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/events");
    expect(res.status).toBe(401);
    const body = (await res.json()) as Record<string, string>;
    expect(body.detail).toBe("unauthorized");
  });

  it("403 when wrong ?token=", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/events?token=bad");
    expect(res.status).toBe(403);
  });

  it("200 + text/event-stream when correct ?token=", async () => {
    const app = makeTestApp("secret-key", env.tmpDir);
    const res = await app.request("/events?token=secret-key");
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    res.body?.cancel();
  });

  it("200 when auth disabled", async () => {
    const app = makeTestApp(undefined, env.tmpDir);
    const res = await app.request("/events");
    expect(res.status).toBe(200);
    res.body?.cancel();
  });
});

// ---------------------------------------------------------------------------
// /feed: initial connected frame + file event (live server)
// ---------------------------------------------------------------------------

describe("/feed — SSE frames (live server)", () => {
  let env: TestEnv;
  let server: LiveServer;

  beforeEach(() => {
    saveEnv();
    env = createTestEnv();
    server = startLiveServer(undefined, env.tmpDir);
  });

  afterEach(() => {
    server.stop();
    env.cleanup();
    restoreEnv();
  });

  it("emits data:{type:connected} as first frame", async () => {
    const frames = await readLiveSSEFrames(`${server.baseUrl}/feed`, {}, 1, 3000);
    expect(frames.length).toBeGreaterThanOrEqual(1);
    expect(parseSSEFrame(frames[0])).toEqual({ type: "connected" });
  });

  it("content-type is text/event-stream; charset=utf-8 (Python parity)", async () => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 2000);
    try {
      const res = await fetch(`${server.baseUrl}/feed`, { signal: controller.signal });
      // Python FastAPI sets "text/event-stream; charset=utf-8" — TS must match exactly.
      expect(res.headers.get("content-type")).toBe("text/event-stream; charset=utf-8");
      res.body?.cancel();
    } catch { /* AbortError */ }
  });

  it("X-Accel-Buffering: no (Python parity)", async () => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 2000);
    try {
      const res = await fetch(`${server.baseUrl}/feed`, { signal: controller.signal });
      // Python rpc_sse.py explicitly sets X-Accel-Buffering: no.
      expect(res.headers.get("x-accel-buffering")).toBe("no");
      res.body?.cancel();
    } catch { /* AbortError */ }
  });

  it("emits appended event after connected frame", async () => {
    const framesPromise = readLiveSSEFrames(`${server.baseUrl}/feed`, {}, 2, 6000);
    // Give handler time to open the file and send connected frame
    await Bun.sleep(300);
    const ev = { timestamp: "2026-05-23T10:00:00Z", role: "executor", message: "test-event" };
    writeFileSync(env.feedFile, JSON.stringify(ev) + "\n", { flag: "a" });

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    expect(parseSSEFrame(frames[0])).toEqual({ type: "connected" });

    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["role"]).toBe("executor");
    expect(eventFrame["message"]).toBe("test-event");
  });

  it("?since= filter skips older events, emits newer", async () => {
    const framesPromise = readLiveSSEFrames(
      `${server.baseUrl}/feed?since=2026-05-23T10:00:01Z`,
      {},
      2,
      6000
    );
    await Bun.sleep(300);
    const old = { timestamp: "2026-05-23T09:59:00Z", role: "executor", msg: "old" };
    const fresh = { timestamp: "2026-05-23T10:00:02Z", role: "executor", msg: "fresh" };
    writeFileSync(
      env.feedFile,
      JSON.stringify(old) + "\n" + JSON.stringify(fresh) + "\n",
      { flag: "a" }
    );

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["msg"]).toBe("fresh");
  });

  it("?filter[role]= skips events with different role", async () => {
    const framesPromise = readLiveSSEFrames(
      `${server.baseUrl}/feed?filter%5Brole%5D=executor`,
      {},
      2,
      6000
    );
    await Bun.sleep(300);
    const wrong = { timestamp: "2026-05-23T10:00:00Z", role: "reviewer", msg: "wrong" };
    const right = { timestamp: "2026-05-23T10:00:01Z", role: "executor", msg: "right" };
    writeFileSync(
      env.feedFile,
      JSON.stringify(wrong) + "\n" + JSON.stringify(right) + "\n",
      { flag: "a" }
    );

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["role"]).toBe("executor");
    expect(eventFrame["msg"]).toBe("right");
  });
});

// ---------------------------------------------------------------------------
// /events: connected frame + _event_type injection (live server)
// ---------------------------------------------------------------------------

describe("/events — SSE frames (live server)", () => {
  let env: TestEnv;
  let server: LiveServer;

  beforeEach(() => {
    saveEnv();
    env = createTestEnv();
    server = startLiveServer(undefined, env.tmpDir);
  });

  afterEach(() => {
    server.stop();
    env.cleanup();
    restoreEnv();
  });

  it("emits data:{type:connected} as first frame", async () => {
    const frames = await readLiveSSEFrames(`${server.baseUrl}/events`, {}, 1, 3000);
    expect(frames.length).toBeGreaterThanOrEqual(1);
    expect(parseSSEFrame(frames[0])).toEqual({ type: "connected" });
  });

  it("content-type is text/event-stream; charset=utf-8 (Python parity)", async () => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 2000);
    try {
      const res = await fetch(`${server.baseUrl}/events`, { signal: controller.signal });
      expect(res.headers.get("content-type")).toBe("text/event-stream; charset=utf-8");
      res.body?.cancel();
    } catch { /* AbortError */ }
  });

  it("X-Accel-Buffering: no (Python parity)", async () => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 2000);
    try {
      const res = await fetch(`${server.baseUrl}/events`, { signal: controller.signal });
      expect(res.headers.get("x-accel-buffering")).toBe("no");
      res.body?.cancel();
    } catch { /* AbortError */ }
  });

  it("emits GateChangeEvent with _event_type from events-bus.jsonl", async () => {
    const framesPromise = readLiveSSEFrames(`${server.baseUrl}/events`, {}, 2, 6000);
    await Bun.sleep(300);
    // Write event with _event_type already set (as BusEventFileAppender would)
    const ev = {
      timestamp: "2026-05-23T10:00:01Z",
      source: "config_watcher",
      trace_id: "",
      gate_name: "test_gate",
      new_value: true,
      old_value: false,
      _event_type: "GateChangeEvent",
    };
    writeFileSync(env.eventsBusFile, JSON.stringify(ev) + "\n", { flag: "a" });

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["_event_type"]).toBe("GateChangeEvent");
    expect(eventFrame["gate_name"]).toBe("test_gate");
    expect(eventFrame["new_value"]).toBe(true);
  });

  it("emits AgentOutputEvent with _event_type from events-bus.jsonl", async () => {
    const framesPromise = readLiveSSEFrames(`${server.baseUrl}/events`, {}, 2, 6000);
    await Bun.sleep(300);
    const ev = {
      timestamp: "2026-05-23T10:00:00Z",
      source: "server",
      trace_id: "",
      agent_id: "exec-1",
      agent_role: "executor",
      content: "hello",
      event_subtype: "content",
      _event_type: "AgentOutputEvent",
    };
    writeFileSync(env.eventsBusFile, JSON.stringify(ev) + "\n", { flag: "a" });

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["_event_type"]).toBe("AgentOutputEvent");
    expect(eventFrame["agent_id"]).toBe("exec-1");
    expect(eventFrame["content"]).toBe("hello");
  });

  it("emits BudgetSpendEvent with _event_type from events-bus.jsonl", async () => {
    const framesPromise = readLiveSSEFrames(`${server.baseUrl}/events`, {}, 2, 6000);
    await Bun.sleep(300);
    const ev = {
      timestamp: "2026-05-23T10:00:00Z",
      source: "budget_tracker",
      trace_id: "",
      agent_id: "exec-2",
      role: "executor",
      input_tokens: 100,
      output_tokens: 50,
      discussion: 42,
      model: "claude-sonnet-4-6",
      _event_type: "BudgetSpendEvent",
    };
    writeFileSync(env.eventsBusFile, JSON.stringify(ev) + "\n", { flag: "a" });

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["_event_type"]).toBe("BudgetSpendEvent");
    expect(eventFrame["input_tokens"]).toBe(100);
    expect(eventFrame["output_tokens"]).toBe(50);
  });

  it("emits LoopIterationEvent with _event_type from events-bus.jsonl", async () => {
    const framesPromise = readLiveSSEFrames(`${server.baseUrl}/events`, {}, 2, 6000);
    await Bun.sleep(300);
    const ev = {
      timestamp: "2026-05-23T10:00:00Z",
      source: "loop",
      trace_id: "",
      iteration_id: "iter-7",
      idle: false,
      duration_seconds: 12.5,
      agents_spawned: 3,
      _event_type: "LoopIterationEvent",
    };
    writeFileSync(env.eventsBusFile, JSON.stringify(ev) + "\n", { flag: "a" });

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["_event_type"]).toBe("LoopIterationEvent");
    expect(eventFrame["iteration_id"]).toBe("iter-7");
    expect(eventFrame["agents_spawned"]).toBe(3);
  });

  it("/events does not emit events from agent-feed.jsonl (separate files)", async () => {
    // Write to agent-feed.jsonl only — /events should NOT see it
    const agentFeedEv = { timestamp: "2026-05-23T10:00:00Z", msg: "feed-only" };
    writeFileSync(env.feedFile, JSON.stringify(agentFeedEv) + "\n", { flag: "a" });

    // Write to events-bus.jsonl — /events SHOULD see it
    const busEv = {
      timestamp: "2026-05-23T10:00:01Z",
      source: "bus",
      trace_id: "",
      agent_id: "exec-1",
      content: "bus-only",
      _event_type: "AgentOutputEvent",
    };
    const framesPromise = readLiveSSEFrames(`${server.baseUrl}/events`, {}, 2, 6000);
    await Bun.sleep(300);
    writeFileSync(env.eventsBusFile, JSON.stringify(busEv) + "\n", { flag: "a" });

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    // Must come from events-bus.jsonl, not agent-feed.jsonl
    expect(eventFrame["content"]).toBe("bus-only");
    expect((eventFrame["msg"] as string | undefined)).toBeUndefined();
  });

  it("?loop_id= filter emits only matching events from events-bus.jsonl", async () => {
    const framesPromise = readLiveSSEFrames(
      `${server.baseUrl}/events?loop_id=loop-123`,
      {},
      2,
      6000
    );
    await Bun.sleep(300);
    const wrong = {
      timestamp: "2026-05-23T10:00:00Z",
      loop_id: "loop-999",
      msg: "wrong",
      _event_type: "AgentOutputEvent",
    };
    const right = {
      timestamp: "2026-05-23T10:00:01Z",
      loop_id: "loop-123",
      msg: "right",
      _event_type: "LoopIterationEvent",
    };
    writeFileSync(
      env.eventsBusFile,
      JSON.stringify(wrong) + "\n" + JSON.stringify(right) + "\n",
      { flag: "a" }
    );

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["msg"]).toBe("right");
    expect(eventFrame["loop_id"]).toBe("loop-123");
  });

  it("infers _event_type for legacy events without the field (backward compat)", async () => {
    // Legacy events (before BusEventFileAppender) may lack _event_type — infer it
    const framesPromise = readLiveSSEFrames(`${server.baseUrl}/events`, {}, 2, 6000);
    await Bun.sleep(300);
    // GateChangeEvent: has gate_name but no _event_type
    const ev = {
      timestamp: "2026-05-23T10:00:01Z",
      gate_name: "some_gate",
      new_value: true,
      old_value: false,
    };
    writeFileSync(env.eventsBusFile, JSON.stringify(ev) + "\n", { flag: "a" });

    const frames = await framesPromise;
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const eventFrame = parseSSEFrame(frames[1]) as Record<string, unknown>;
    expect(eventFrame["_event_type"]).toBe("GateChangeEvent");
  });
});
