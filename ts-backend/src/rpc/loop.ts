/**
 * rpc/loop.ts — Native TS implementations of the loop.* RPC methods (batch 4).
 *
 * Mirrors the following Python RPC handlers exactly (1:1 parity):
 *   - loop.list              → handleLoopList()
 *   - loop.events            → handleLoopEvents()
 *   - loop.timeline          → handleLoopTimeline()
 *   - loop.iteration_detail  → handleLoopIterationDetail()
 *   - agents.tail            → handleAgentsTail()
 *   - dashboard.gates_snapshot → handleDashboardGatesSnapshot()
 *
 * All handlers are additive — Python runtime code is not modified.
 * All methods in this cluster are FILE readers:
 *   - loop.list: reads .autonomous-team/active-loops.json
 *   - loop.events / agents.tail: reads .autonomous-team/agent-feed.jsonl
 *   - loop.timeline / loop.iteration_detail: reads .autonomous-team/loop-metrics.jsonl
 *   - loop.iteration_detail: also reads .autonomous-team/loop-runs/fulcrumaxe/
 *   - dashboard.gates_snapshot: reads .autonomous-team/config.json
 *
 * Path resolution follows the same env-var order as Python:
 *   AF_REPO_ROOT, AF_ACTIVE_LOOPS_PATH, AF_AGENT_FEED_PATH, AF_LOOP_METRICS_PATH
 *   for test overrides; repo-relative defaults otherwise.
 *
 * Design notes (overrides vs Implementation Notes):
 *   - loop.iteration_detail: TS re-implements extract_references() inline using
 *     the same regex patterns as backend/loop_log_references.py — avoids a Python
 *     subprocess call while maintaining exact parity on output shape.
 *   - dashboard.gates_snapshot: reads config.json directly; merges with hardcoded
 *     _DEFAULT_GATES exactly as Python's ControlPlane.list_gates() does.
 *   - Per-project param: Python resolves a different state_dir for each project.
 *     TS batch 4 serves AF default only (same limitation as stats.ts batch 2).
 *     Documented as a P6b enhancement.
 */

import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

/**
 * Repo root — AF_REPO_ROOT env, or walk up 4 dirs from this file.
 * In worktree: AF_REPO_ROOT must be set to point to the real repo.
 */
function repoRoot(): string {
  return process.env.AF_REPO_ROOT
    ?? join(new URL(import.meta.url).pathname, "..", "..", "..", "..", "..");
}

/**
 * Path to .autonomous-team/active-loops.json.
 * Test override: AF_ACTIVE_LOOPS_PATH.
 */
function activeLoopsPath(): string {
  return process.env.AF_ACTIVE_LOOPS_PATH
    ?? join(repoRoot(), ".autonomous-team", "active-loops.json");
}

/**
 * Path to .autonomous-team/agent-feed.jsonl.
 * Test override: AF_AGENT_FEED_PATH.
 */
function agentFeedPath(): string {
  return process.env.AF_AGENT_FEED_PATH
    ?? join(repoRoot(), ".autonomous-team", "agent-feed.jsonl");
}

/**
 * Path to .autonomous-team/loop-metrics.jsonl.
 * Test override: AF_LOOP_METRICS_PATH (shared with stats.ts).
 */
function loopMetricsPath(): string {
  return process.env.AF_LOOP_METRICS_PATH
    ?? join(repoRoot(), ".autonomous-team", "loop-metrics.jsonl");
}

// ---------------------------------------------------------------------------
// Error helper — mirrors Python's _rpc_invalid_params
// ---------------------------------------------------------------------------

function invalidParams(msg: string): Error {
  const err = new Error(msg) as Error & { rpc_code: number };
  (err as Error & { rpc_code: number }).rpc_code = -32602;
  return err;
}

// ---------------------------------------------------------------------------
// loop.list
// ---------------------------------------------------------------------------

/**
 * Return all running loops from active-loops.json.
 *
 * Response: {"loops": [...running loop entries...]}
 * Mirrors: backend/active_loops.list_loops() → _rpc_loop_list()
 *
 * active-loops.json structure: {"loops": {"<id>": {"status": "running"|"stopped", ...}}}
 * list_loops() returns [entry for entry in loops.values() if entry["status"] == "running"]
 */
export function handleLoopList(_params: Record<string, unknown>): unknown {
  const path = activeLoopsPath();
  let data: Record<string, unknown> = { loops: {} };

  if (existsSync(path)) {
    try {
      const raw = readFileSync(path, "utf-8");
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && "loops" in parsed) {
        data = parsed as Record<string, unknown>;
      }
    } catch {
      // Failed to read/parse — return empty (mirrors active_loops._load_raw() fallback)
    }
  }

  const loopsObj = data["loops"];
  const loops: unknown[] = [];
  if (loopsObj && typeof loopsObj === "object" && !Array.isArray(loopsObj)) {
    for (const entry of Object.values(loopsObj as Record<string, unknown>)) {
      if (
        entry &&
        typeof entry === "object" &&
        (entry as Record<string, unknown>)["status"] === "running"
      ) {
        loops.push(entry);
      }
    }
  }

  return { loops };
}

// ---------------------------------------------------------------------------
// loop.events
// ---------------------------------------------------------------------------

/**
 * Return events for a specific loop from agent-feed.jsonl.
 *
 * Params:
 *   loop_id (str, required) — loop ID to filter events for
 *   since_event_id (str, optional) — skip events up to and including this ID
 *   limit (int, optional) — max events to return; default 50
 *
 * Response: {"events": [...], "next_since_id": str|null}
 * Mirrors: _rpc_loop_events() in server.py
 *
 * Raises -32000 when loop not found (mirrors Python: raise ValueError("loop not found: ..."))
 */
export function handleLoopEvents(params: Record<string, unknown>): unknown {
  const loopId = String(params["loop_id"] ?? "");
  const sinceEventId = (params["since_event_id"] as string | undefined) ?? null;
  const limit = parseInt(String(params["limit"] ?? 50), 10) || 50;

  // Verify the loop exists in active-loops.json (Python raises ValueError if not found)
  const loopsPath = activeLoopsPath();
  let loopFound = false;
  if (existsSync(loopsPath)) {
    try {
      const raw = readFileSync(loopsPath, "utf-8");
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && "loops" in parsed) {
        const loopsObj = (parsed as Record<string, unknown>)["loops"];
        if (loopsObj && typeof loopsObj === "object" && !Array.isArray(loopsObj)) {
          loopFound = loopId in (loopsObj as Record<string, unknown>);
        }
      }
    } catch {
      // Can't read → treat loop as not found (fail closed like Python)
    }
  }

  if (!loopFound) {
    const err = new Error(`loop not found: ${loopId}`) as Error & { rpc_code: number };
    err.rpc_code = -32000;
    throw err;
  }

  // Read events from agent-feed.jsonl, filtering by loop_id annotation
  const feedPath = agentFeedPath();
  const events: unknown[] = [];

  if (existsSync(feedPath)) {
    try {
      const content = readFileSync(feedPath, "utf-8");
      const lines = content.split("\n");
      let foundSince = sinceEventId === null;

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        let ev: Record<string, unknown>;
        try {
          ev = JSON.parse(trimmed) as Record<string, unknown>;
        } catch {
          continue;
        }

        const eid = (ev["id"] as string | undefined) ?? (ev["event_id"] as string | undefined) ?? null;

        if (!foundSince) {
          if (eid === sinceEventId) {
            foundSince = true;
          }
          continue;
        }

        if (ev["loop_id"] === loopId) {
          events.push(ev);
          if (events.length >= limit) break;
        }
      }
    } catch {
      // Ignore read errors — return empty
    }
  }

  const lastEvent = events.length > 0 ? events[events.length - 1] as Record<string, unknown> : null;
  const nextSinceId = lastEvent ? ((lastEvent["id"] as string | undefined) ?? null) : null;

  return { events, next_since_id: nextSinceId };
}

// ---------------------------------------------------------------------------
// agents.tail
// ---------------------------------------------------------------------------

/**
 * Return filtered tail of events from agent-feed.jsonl.
 *
 * Params:
 *   since (str, optional) — ISO timestamp lower bound; include events where ts >= since
 *   limit (int, optional) — max events; default 50
 *   filter (object, optional) — {role, discussion, event_type}
 *
 * Response: {"events": [...], "next_since": str|null}
 * Mirrors: _rpc_agents_tail() in server.py
 */
export function handleAgentsTail(params: Record<string, unknown>): unknown {
  const since = (params["since"] as string | undefined) ?? null;
  const limit = parseInt(String(params["limit"] ?? 50), 10) || 50;
  const flt = (params["filter"] as Record<string, unknown> | undefined) ?? {};
  const roleFilter = (flt["role"] as string | undefined) ?? null;
  const discussionFilter = flt["discussion"] !== undefined ? flt["discussion"] : null;
  const eventTypeFilter = (flt["event_type"] as string | undefined) ?? null;

  const feedPath = agentFeedPath();
  const events: unknown[] = [];

  if (existsSync(feedPath)) {
    try {
      const content = readFileSync(feedPath, "utf-8");
      const lines = content.split("\n");
      let foundSince = since === null;

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        let ev: Record<string, unknown>;
        try {
          ev = JSON.parse(trimmed) as Record<string, unknown>;
        } catch {
          continue;
        }

        const ts = String(
          (ev["timestamp"] as string | undefined) ??
          (ev["ts"] as string | undefined) ??
          ""
        );

        if (!foundSince) {
          // Python: if ts >= since: found_since = True; else: continue
          if (ts >= since!) {
            foundSince = true;
          } else {
            continue;
          }
        }

        if (roleFilter !== null && ev["role"] !== roleFilter) continue;
        if (discussionFilter !== null && ev["discussion"] !== discussionFilter) continue;
        if (eventTypeFilter !== null && ev["event_type"] !== eventTypeFilter) continue;

        events.push(ev);
        if (events.length >= limit) break;
      }
    } catch {
      // Ignore read errors
    }
  }

  const lastEvent = events.length > 0 ? events[events.length - 1] as Record<string, unknown> : null;
  const nextSince = lastEvent
    ? ((lastEvent["timestamp"] as string | undefined) ?? null)
    : since;

  return { events, next_since: nextSince };
}

// ---------------------------------------------------------------------------
// loop.timeline
// ---------------------------------------------------------------------------

const _MAX_ITER_DURATION_S = 86_400; // 24 hours — mirrors Python

/**
 * Return the last N loop iterations from loop-metrics.jsonl.
 *
 * Params:
 *   limit (int, optional) — default 100, max 500
 *   include_test (bool, optional) — default false; skip rows with origin=="test"
 *   project (str, optional) — per-project path; TS batch 4 serves AF default only
 *
 * Response: [{timestamp, duration_seconds, agents_spawned, prs_merged,
 *             discussions_scanned, prs_scanned, idle, error}, ...]
 *           ordered oldest → newest, malformed lines silently skipped.
 *
 * Mirrors: _rpc_loop_timeline() in server.py
 */
export function handleLoopTimeline(params: Record<string, unknown>): unknown {
  const limitRaw = params["limit"] ?? 100;
  let limit: number;
  try {
    limit = parseInt(String(limitRaw), 10);
    if (isNaN(limit)) throw new Error("bad limit");
  } catch {
    throw invalidParams(`limit must be an integer, got ${String(limitRaw)}`);
  }
  limit = Math.max(1, Math.min(limit, 500));

  const includeTest = Boolean(params["include_test"] ?? false);

  const metricsPath = loopMetricsPath();
  if (!existsSync(metricsPath)) {
    return [];
  }

  // Use a ring buffer of size `limit` (deque(maxlen=limit) equivalent)
  const buf: unknown[] = [];

  let content: string;
  try {
    content = readFileSync(metricsPath, "utf-8");
  } catch {
    return [];
  }

  for (const rawLine of content.split("\n")) {
    const raw = rawLine.trim();
    if (!raw) continue;

    let row: Record<string, unknown>;
    try {
      row = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      // Skip malformed lines — mirrors Python: print to stderr, continue
      continue;
    }

    // Rows missing 'origin' are treated as "cron" for back-compat.
    const rowOrigin = (row["origin"] as string | undefined) ?? "cron";
    if (!includeTest && rowOrigin === "test") continue;

    // Handle legacy 'ts' field name (pre-#487 rows)
    const timestamp = (row["timestamp"] as string | undefined)
      ?? (row["ts"] as string | undefined)
      ?? "";

    // Sanitise duration: historic rows stored the Unix epoch timestamp
    // instead of a delta, producing values in the billions.
    // A loop iteration longer than 24h is definitionally bad data.
    const rawDur = (
      (row["duration_seconds"] as number | undefined) ??
      (row["duration_s"] as number | undefined) ??
      0
    ) || 0;
    const durationSeconds = rawDur <= _MAX_ITER_DURATION_S ? rawDur : 0;

    buf.push({
      timestamp,
      duration_seconds: durationSeconds,
      agents_spawned: (row["agents_spawned"] as number | undefined) ?? 0,
      prs_merged: (row["prs_merged"] as number | undefined) ?? 0,
      discussions_scanned: (row["discussions_scanned"] as number | undefined) ?? 0,
      prs_scanned: (row["prs_scanned"] as number | undefined) ?? 0,
      idle: Boolean(row["idle"] ?? false),
      error: (row["error"] as string | undefined) ?? null,
    });

    // Ring buffer: keep only the last `limit` entries
    if (buf.length > limit) {
      buf.shift();
    }
  }

  return buf;
}

// ---------------------------------------------------------------------------
// loop.iteration_detail
// ---------------------------------------------------------------------------

/**
 * Extract D#N and PR#N references from log text.
 * Mirrors: backend/loop_log_references.extract_references()
 *
 * Patterns:
 *   D#(\d+)   — case-insensitive, word boundary
 *   PR\s*#(\d+) — case-insensitive, word boundary
 *
 * Cap at 50 references of each kind. Returns sorted, deduplicated arrays.
 */
function extractReferences(logText: string): { discussions: number[]; prs: number[] } {
  if (!logText) return { discussions: [], prs: [] };

  const CAP = 50;
  const dPattern = /\bD#(\d+)\b/gi;
  const prPattern = /\bPR\s*#(\d+)\b/gi;

  const discussions: Set<number> = new Set();
  const prs: Set<number> = new Set();

  let m: RegExpExecArray | null;

  while ((m = dPattern.exec(logText)) !== null) {
    discussions.add(parseInt(m[1], 10));
  }
  while ((m = prPattern.exec(logText)) !== null) {
    prs.add(parseInt(m[1], 10));
  }

  return {
    discussions: Array.from(discussions).sort((a, b) => a - b).slice(0, CAP),
    prs: Array.from(prs).sort((a, b) => a - b).slice(0, CAP),
  };
}

/**
 * Return full detail for one loop iteration.
 *
 * Params:
 *   timestamp (str, required) — ISO8601 (YYYY-MM-DDTHH:MM:SSZ)
 *   project (str, optional) — per-project; TS batch 4 serves AF default only
 *
 * Response: {timestamp, metrics: <row|{}>, log: str|null, log_path: str|null,
 *            references: {discussions: [], prs: []}}
 * Mirrors: _rpc_loop_iteration_detail() in server.py
 */
export function handleLoopIterationDetail(params: Record<string, unknown>): unknown {
  const _MAX_LOG_BYTES = 64 * 1024; // 64 KB — mirrors Python

  const ts = String(params["timestamp"] ?? "").trim();
  if (!ts) {
    throw invalidParams("timestamp is required");
  }

  // Validate ISO8601 format — mirrors Python re.match pattern
  const tsMatch = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z?$/.exec(ts);
  if (!tsMatch) {
    throw invalidParams(
      `timestamp must be ISO8601 (YYYY-MM-DDTHH:MM:SSZ), got ${JSON.stringify(ts)}`
    );
  }

  // Read loop-metrics.jsonl and find the matching row
  const metricsPath = loopMetricsPath();
  let metricsRow: Record<string, unknown> = {};

  if (existsSync(metricsPath)) {
    try {
      const content = readFileSync(metricsPath, "utf-8");
      for (const rawLine of content.split("\n")) {
        const raw = rawLine.trim();
        if (!raw) continue;
        let row: Record<string, unknown>;
        try {
          row = JSON.parse(raw) as Record<string, unknown>;
        } catch {
          continue;
        }
        if (row["timestamp"] === ts) {
          metricsRow = row; // Take the last match (mirrors Python: keep scanning for duplicates)
        }
      }
    } catch {
      // Ignore read errors — return detail with what we have
    }
  }

  // Normalise counter fields when the metrics row IS present
  // (mirrors Python: for _counter in ("agents_spawned", ...): if _counter not in metrics_row: metrics_row[_counter] = 0)
  if (Object.keys(metricsRow).length > 0) {
    for (const counter of ["agents_spawned", "prs_merged", "discussions_scanned", "prs_scanned"]) {
      if (!(counter in metricsRow)) {
        metricsRow[counter] = 0;
      }
    }
  }

  // Resolve log directory — AF default: .autonomous-team/loop-runs/fulcrumaxe/
  const logDir = join(repoRoot(), ".autonomous-team", "loop-runs", "fulcrumaxe");

  // Prefer run_id from metrics row (Bug 3 fix in Python)
  const runId = (metricsRow["run_id"] as string | undefined) ?? null;

  let logFile: string | null = null;
  if (runId) {
    // Glob for <run_id>*.log — pick alphabetically-last match for determinism
    // Mirrors Python: sorted(log_dir.glob(f"{run_id}*.log"))[-1]
    if (existsSync(logDir)) {
      try {
        const candidates = readdirSync(logDir)
          .filter(f => f.startsWith(runId) && f.endsWith(".log"))
          .sort();
        if (candidates.length > 0) {
          logFile = join(logDir, candidates[candidates.length - 1]);
        } else {
          // Fallback: try exact name
          const candidate = join(logDir, `${runId}.log`);
          logFile = existsSync(candidate) ? candidate : null;
        }
      } catch {
        logFile = null;
      }
    }
  } else {
    // Timestamp-derived filename: 2026-04-11T01:41:20Z → 20260411T014120Z.log
    const [, y, mo, d, h, mi, s] = tsMatch;
    const tsFname = `${y}${mo}${d}T${h}${mi}${s}Z.log`;
    const candidate = join(logDir, tsFname);
    logFile = existsSync(candidate) ? candidate : null;
  }

  let logContent: string | null = null;
  let logPathStr: string | null = null;

  if (logFile && existsSync(logFile)) {
    logPathStr = logFile;
    try {
      const stat = statSync(logFile);
      const size = stat.size;

      // Read up to _MAX_LOG_BYTES bytes — mirrors Python: lf.read(_MAX_LOG_BYTES)
      // We read the full file but truncate in JS (no partial-read fs API in Bun)
      const rawContent = readFileSync(logFile, "utf-8");
      const encoded = Buffer.from(rawContent, "utf-8");

      if (encoded.length > _MAX_LOG_BYTES) {
        logContent = encoded.slice(0, _MAX_LOG_BYTES).toString("utf-8");
        logContent += `\n[truncated: original size ${size} bytes]`;
      } else {
        logContent = rawContent;
      }
    } catch {
      // Can't read log file — return null
      logContent = null;
    }
  }

  const references = extractReferences(logContent ?? "");

  return {
    timestamp: ts,
    metrics: metricsRow,
    log: logContent,
    log_path: logPathStr,
    references,
  };
}

// ---------------------------------------------------------------------------
// dashboard.gates_snapshot
// ---------------------------------------------------------------------------

/**
 * Default gates — mirrors backend/control_plane._DEFAULT_GATES exactly.
 * This is the source of truth for which gates exist when config.json is absent
 * or a gate key is missing from the file.
 *
 * IMPORTANT: must stay in sync with _DEFAULT_GATES in backend/control_plane.py.
 * When Python adds a new default gate, add it here too.
 */
const _DEFAULT_GATES: Record<string, boolean | string> = {
  auto_merge: true,
  security_review: true,
  budget_check: true,
  idea_generation: true,
  stall_detection: true,
  wiki_sync: true,
  human_verification: false,
  // Self-observe gates
  self_observe_executor: false,
  self_observe_impl_coord: false,
  self_observe_enforcement: "shadow",
  // Docs-writer gate
  docs_writer: true,
  // Incident-commander gate
  incident_commander: false,
  // Release-manager gate
  release_manager: true,
  // Runbook-writer gate
  runbook_writer: true,
  // Analytics-engineer gate
  analytics_engineer: true,
  // Phased orchestration gates
  phased_orchestration: false,
  phased_code_review: true,
  // Cost-aware Discussion router
  cost_aware_router: false,
  // Debater pass
  debater_pass: false,
  // TUI tester pilot sweep
  tui_tester_pilot_sweep: false,
  // Execve fence
  execve_fence: true,
  // Loop-start gate
  loop_start: false,
  // Dial-state-summary
  dial_state_summary: false,
};

/**
 * Return all control-plane gates as a flat dict.
 *
 * FAITHFUL MIRROR: Python's _rpc_dashboard_gates_snapshot calls:
 *   cp = ControlPlane()           ← _data = {}  (NOT loaded from file)
 *   return {"gates": cp.list_gates()}
 *
 * And list_gates() does:
 *   gates = self._data.get("gates", {})   → {} because load() was NOT called
 *   result = dict(_DEFAULT_GATES)
 *   result.update(gates)                   → no-op
 *
 * So Python returns ONLY _DEFAULT_GATES with coercion, without reading config.json.
 * This is a Python quirk (cp.load() is never called in the RPC handler).
 * We faithfully mirror it: return _DEFAULT_GATES without reading the file.
 *
 * The AF_CONFIG_PATH env var is accepted for test overrides but the default
 * behavior (no env var) matches Python exactly.
 *
 * Response: {"gates": {name: bool|str, ...}}
 * Mirrors: _rpc_dashboard_gates_snapshot() in server.py
 */
export function handleDashboardGatesSnapshot(_params: Record<string, unknown>): unknown {
  // Read file only when AF_CONFIG_PATH override is set (for tests and future fix).
  // Default path: return _DEFAULT_GATES only — mirrors Python's unloaded ControlPlane quirk.
  let fileGates: Record<string, unknown> = {};
  const cfgPathOverride = process.env.AF_CONFIG_PATH;

  if (cfgPathOverride && existsSync(cfgPathOverride)) {
    try {
      const raw = readFileSync(cfgPathOverride, "utf-8");
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && "gates" in parsed) {
        const g = (parsed as Record<string, unknown>)["gates"];
        if (g && typeof g === "object" && !Array.isArray(g)) {
          fileGates = g as Record<string, unknown>;
        }
      }
    } catch {
      // Config file unreadable — use defaults only
    }
  }

  // Merge: start with defaults, overlay file values (no-op when fileGates is empty)
  const merged: Record<string, unknown> = { ..._DEFAULT_GATES, ...fileGates };

  // Coerce: string gates kept as-is; everything else → bool
  const gates: Record<string, boolean | string> = {};
  for (const [k, v] of Object.entries(merged)) {
    gates[k] = typeof v === "string" ? v : Boolean(v);
  }

  return { gates };
}
