/**
 * loop/loop-preflight.ts — Loop pre-flight: runs coordination module CLIs before
 * each loop iteration.
 *
 * Mirrors scripts/loop-preflight.sh (144 LOC bash) 1:1.
 *
 * Outputs a JSON summary to stdout. Returns exit code 0 if the loop should
 * proceed, 1 if the loop should be skipped (gate disabled or budget exhausted).
 *
 * # Steps (in order, mirrors bash step sequence exactly)
 *   1. budget init (idempotent)
 *   2. registry sync + show (compact summary)
 *   2.5. context_manager warmup
 *   3. control_plane show → gates JSON
 *   4. budget status → budget summary
 *   assemble summary → stdout
 *   check loop_enabled gate
 *   check budget allowed
 *
 * # Side effects NOT covered by parity tests (require external systems)
 *   - python3 backend/budget.py init           (state.db / budget tracking)
 *   - python3 backend/registry.py sync/show    (Discussion registry sync from GitHub)
 *   - python3 backend/context_manager.py show  (context file warm-up)
 *   - python3 backend/control_plane.py show    (config.json read)
 *   - python3 backend/budget.py status         (budget spend lookup)
 *
 * CLI usage:
 *   bun run ts-backend/src/loop/loop-preflight.ts
 * Exit codes:
 *   0 — loop should proceed
 *   1 — loop should be skipped (gate disabled or budget exhausted)
 */

import { spawnSync } from "node:child_process";
import { join, dirname } from "node:path";

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

function _repoRoot(): string {
  if (process.env["AF_REPO_ROOT"]) return process.env["AF_REPO_ROOT"]!;
  // This file: ts-backend/src/loop/loop-preflight.ts
  // → ts-backend/src/loop → ts-backend/src → ts-backend → repo_root
  const here = dirname(new URL(import.meta.url).pathname);
  return join(here, "..", "..", "..", "..");
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PreflightResult {
  timestamp: string;
  gates: Record<string, unknown>;
  budget: Record<string, unknown>;
  registry: Record<string, unknown>;
  errors: string[];
}

export interface PreflightOutcome {
  result: PreflightResult;
  /** exit code: 0 = proceed, 1 = skip */
  exitCode: number;
  /** reason for skip (only set when exitCode=1) */
  skipReason?: string;
}

// ---------------------------------------------------------------------------
// Helper: run a python3 command and return stdout (or null on failure)
// ---------------------------------------------------------------------------

function _runPython3(
  root: string,
  args: string[],
  opts: { timeoutMs?: number } = {},
): { stdout: string; ok: boolean; stderr: string } {
  const result = spawnSync("python3", args, {
    cwd: root,
    encoding: "utf-8",
    timeout: opts.timeoutMs ?? 30_000,
    env: { ...process.env, PYTHONPATH: root },
  });
  return {
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    ok: result.status === 0 && !result.error,
  };
}

// ---------------------------------------------------------------------------
// Core function (fully testable without CLI plumbing)
// ---------------------------------------------------------------------------

/**
 * Run the full preflight sequence.
 * Mirrors the body of scripts/loop-preflight.sh.
 *
 * @param root - repo root override (defaults to AF_REPO_ROOT or resolved path)
 */
export function runPreflight(root?: string): PreflightOutcome {
  const repoDir = root ?? _repoRoot();
  const timestamp = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const errors: string[] = [];
  let gatesJson: Record<string, unknown> = {};
  let budgetJson: Record<string, unknown> = {};
  let registryJson: Record<string, unknown> = {};

  // ── Step 1: Initialize budget (idempotent) ────────────────────────────────
  // mirrors: if INIT_OUT=$(python3 backend/budget.py init 2>&1); then...
  {
    const r = _runPython3(repoDir, ["backend/budget.py", "init"]);
    if (!r.ok) {
      errors.push(`budget.py init failed: ${(r.stdout + r.stderr).trim()}`);
    }
  }

  // ── Step 2: Sync registry with latest Discussion state ────────────────────
  // mirrors: if SYNC_OUT=$(python3 backend/registry.py sync 2>&1); then...
  {
    const syncR = _runPython3(repoDir, ["backend/registry.py", "sync"]);
    if (!syncR.ok) {
      errors.push(`registry.py sync failed: ${(syncR.stdout + syncR.stderr).trim()}`);
    } else {
      const showR = _runPython3(repoDir, ["backend/registry.py", "show"]);
      if (showR.ok && showR.stdout.trim()) {
        try {
          const data = JSON.parse(showR.stdout) as Record<string, unknown>;
          const discussions = (data["discussions"] as unknown[] | undefined) ?? [];
          const statuses = discussions.map(
            (d) =>
              ((d as Record<string, unknown>)["status"] as string | undefined) ??
              "UNKNOWN"
          );
          const count = (s: string) => statuses.filter((x) => x === s).length;
          registryJson = {
            total: discussions.length,
            discussing: count("DISCUSSING"),
            spec_ready: count("SPEC_READY"),
            implementing: count("IMPLEMENTING"),
            reviewing: count("REVIEWING"),
            done: count("DONE"),
            synced_at: (data["synced_at"] as string | undefined) ?? "",
          };
        } catch {
          errors.push("registry.py show parse failed");
        }
      } else {
        errors.push("registry.py show failed");
      }
    }
  }

  // ── Step 2.5: Warm up context manager cache ───────────────────────────────
  // mirrors: python3 backend/context_manager.py show > /dev/null 2>&1 || ...
  {
    const r = _runPython3(repoDir, ["backend/context_manager.py", "show"]);
    if (!r.ok) {
      errors.push("context_manager warmup failed");
    }
  }

  // ── Step 3: Read feature gate states via control_plane show ───────────────
  // mirrors: if CP_OUT=$(python3 backend/control_plane.py show 2>/dev/null); then...
  {
    const r = _runPython3(repoDir, ["backend/control_plane.py", "show"]);
    if (r.ok && r.stdout.trim()) {
      try {
        const data = JSON.parse(r.stdout) as Record<string, unknown>;
        gatesJson =
          (data["gates"] as Record<string, unknown> | undefined) ?? {};
      } catch {
        errors.push("control_plane.py gates parse failed");
      }
    } else {
      errors.push("control_plane.py show failed");
    }
  }

  // ── Step 4: Get current budget status ────────────────────────────────────
  // mirrors: if BUDGET_OUT=$(python3 backend/budget.py status 2>/dev/null); then...
  {
    const r = _runPython3(repoDir, ["backend/budget.py", "status"]);
    if (r.ok && r.stdout.trim()) {
      try {
        const data = JSON.parse(r.stdout) as Record<string, unknown>;
        // Mirrors Python: ceiling / spent / remaining / allowed
        const ceiling =
          (data["ceiling"] as number | undefined) ??
          (data["session_ceiling"] as number | undefined) ??
          0;
        const spent =
          (data["spent"] as number | undefined) ??
          (data["session_spent"] as number | undefined) ??
          0;
        const remaining = ceiling > 0 ? ceiling - spent : 0;
        const allowed = ceiling > 0 ? spent < ceiling : true;
        budgetJson = { ceiling, spent, remaining, allowed };
      } catch {
        errors.push("budget.py status parse failed");
      }
    } else {
      errors.push("budget.py status failed");
    }
  }

  // ── Assemble summary ──────────────────────────────────────────────────────
  const result: PreflightResult = {
    timestamp,
    gates: gatesJson,
    budget: budgetJson,
    registry: registryJson,
    errors,
  };

  // ── Check loop_enabled gate ───────────────────────────────────────────────
  // mirrors: val = gates.get('loop_enabled', gates.get('loop', True))
  const gateRaw =
    gatesJson["loop_enabled"] !== undefined
      ? gatesJson["loop_enabled"]
      : gatesJson["loop"];
  const loopEnabled = gateRaw === undefined ? true : Boolean(gateRaw);

  if (!loopEnabled) {
    return {
      result,
      exitCode: 1,
      skipReason: "loop_enabled gate is false — skipping iteration",
    };
  }

  // ── Check budget allowed ──────────────────────────────────────────────────
  const budgetAllowed =
    budgetJson["allowed"] === undefined ? true : Boolean(budgetJson["allowed"]);

  if (!budgetAllowed) {
    return {
      result,
      exitCode: 1,
      skipReason: "budget exhausted — skipping iteration",
    };
  }

  return { result, exitCode: 0 };
}

// ---------------------------------------------------------------------------
// CLI entry point (mirrors bash main flow)
// ---------------------------------------------------------------------------

if (import.meta.main) {
  const outcome = runPreflight();

  // Print JSON summary to stdout (mirrors bash: echo "$SUMMARY")
  process.stdout.write(JSON.stringify(outcome.result, null, 2) + "\n");

  if (outcome.exitCode !== 0 && outcome.skipReason) {
    process.stderr.write(`[loop-preflight] ${outcome.skipReason}\n`);
  }

  process.exit(outcome.exitCode);
}
