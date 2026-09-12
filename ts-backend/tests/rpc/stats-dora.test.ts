/**
 * tests/rpc/stats-dora.test.ts — project-scoping tests for stats.dora
 * (D#2540, porting the already-merged Python contract from D#2518).
 *
 * ts-backend/tests/rpc-stats-dora.test.ts already covers the no-project
 * (serving-checkout) response shape; this file covers only what changed
 * here: a `project` param now resolves that project's own repo slug and
 * checkout root, threads them through per request, and declines
 * (UnresolvableProjectError) rather than answering with the serving
 * checkout's numbers when a named project declares no repo.
 *
 * Coverage:
 *  1. Same project param, different backends (Python vs TS) → same answer,
 *     for a resolvable project (item 4's binding requirement) and for the
 *     decline case (item 5).
 *  2. Two different projects with different local release counts → the TS
 *     handler itself must report different deploy_frequency_per_day for
 *     each — not merely a non-empty response (item 4/6).
 *  3. Decline is distinguishable from an empty-but-resolvable project
 *     (item 5's companion assertion).
 *  4. No-project call is unaffected (no-regression companion).
 *
 * Fixture layout mirrors backend/tests/test_rpc_project_scope.py's
 * `_write_project_layout()` exactly: `<home>/.<name>-state/dashboard-runtime.json`
 * (repo field, or `{}` when the project declares none) plus
 * `<home>/<name>/.autonomous-team/{releases,registry.json}` — the
 * `state_dir.parent / project` convention stats_dora.py's handle() uses.
 *
 * `AF_PROJECT_HOME_OVERRIDE` (not `HOME`): Bun's `os.homedir()` resolves
 * once from the OS and does not observe a runtime-mutated
 * `process.env.HOME` (unlike Python's `Path.home()`, which the Python-side
 * test suite overrides via `monkeypatch.setattr(Path, "home", ...)`) — see
 * config/project-repo-slug.ts's `projectsHome()` for why the override env
 * var exists.
 *
 * Run: cd ts-backend && bun test tests/rpc/stats-dora.test.ts --timeout 30000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { handleDora } from "../../src/rpc/stats-dora.js";
import { UnresolvableProjectError } from "../../src/config/project-repo-slug.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const _thisFile = new URL(import.meta.url).pathname;
// This file: ts-backend/tests/rpc/stats-dora.test.ts
// → tests/rpc/ → tests/ → ts-backend/ → repo root
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");

function makeTempHome(label: string): string {
  const dir = join(
    tmpdir(),
    `dora-project-scope-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
  mkdirSync(dir, { recursive: true });
  return dir;
}

/**
 * Build one project's `~/.<name>-state/dashboard-runtime.json` (repo, or
 * `{}` when omitted) and its own checkout's
 * `.autonomous-team/{releases,registry.json}` — mirrors
 * backend/tests/test_rpc_project_scope.py's `_write_project_layout()`.
 */
function writeProjectLayout(
  home: string,
  name: string,
  repo: string | null,
  releaseCount: number
): void {
  const stateDir = join(home, `.${name}-state`);
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(
    join(stateDir, "dashboard-runtime.json"),
    JSON.stringify(repo !== null ? { repo } : {})
  );

  const teamDir = join(home, name, ".autonomous-team");
  const releasesDir = join(teamDir, "releases");
  mkdirSync(releasesDir, { recursive: true });
  const nowIso = new Date().toISOString();
  for (let i = 0; i < releaseCount; i++) {
    writeFileSync(
      join(releasesDir, `release-${i}.json`),
      JSON.stringify({ id: `2026-09-01-${i.toString().padStart(3, "0")}`, merged_at: nowIso })
    );
  }
  writeFileSync(join(teamDir, "registry.json"), JSON.stringify({ discussions: [] }));
}

function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}

// ---------------------------------------------------------------------------
// Cross-backend (Python) harness
// ---------------------------------------------------------------------------

/**
 * Inline Python probe for backend/rpc/stats_dora.py:handle() — no
 * checked-in backend/ script needed (this PR stays inside ts-backend/).
 * Sets HOME from argv (not env, mirroring the AF_PROJECT_HOME_OVERRIDE
 * rationale above: this must be settable per-call, after process start)
 * and a placeholder AUTONOMOUS_TEAM_REPO so importing
 * backend.release_manager's unconditional `from backend._repo import
 * CODE_REPO` (a module-level import, unrelated to project scoping) does
 * not fail for a project-scoped call that never uses that default.
 */
const PYTHON_STATS_DORA_PROBE = `
import json, os, sys
os.environ["HOME"] = sys.argv[1]
os.environ.setdefault("AUTONOMOUS_TEAM_REPO", "placeholder-owner/placeholder-repo")
from backend.rpc.stats_dora import handle
try:
    r = handle(json.loads(sys.argv[2]))
    print(json.dumps({"ok": True, "result": r}))
except Exception as e:
    print(json.dumps({
        "ok": False,
        "error": type(e).__name__,
        "rpc_code": getattr(e, "rpc_code", None),
        "message": str(e),
    }))
`;

interface PythonProbeResult {
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
  rpc_code?: number | null;
  message?: string;
}

async function runPythonStatsDora(
  home: string,
  params: Record<string, unknown>
): Promise<PythonProbeResult> {
  const proc = Bun.spawn(
    ["python3", "-c", PYTHON_STATS_DORA_PROBE, home, JSON.stringify(params)],
    { cwd: REPO_ROOT, stdout: "pipe", stderr: "pipe" }
  );
  const timeout = setTimeout(() => proc.kill(), 30_000);
  await proc.exited;
  clearTimeout(timeout);
  const stdout = await new Response(proc.stdout).text();
  const stderr = await new Response(proc.stderr).text();
  const lastLine = stdout.trim().split("\n").pop() ?? "";
  try {
    return JSON.parse(lastLine) as PythonProbeResult;
  } catch {
    throw new Error(
      `python probe produced no parseable JSON — exit=${proc.exitCode}\nstdout=${stdout}\nstderr=${stderr}`
    );
  }
}

// ---------------------------------------------------------------------------
// Env setup
// ---------------------------------------------------------------------------

let home: string;

beforeEach(() => {
  home = makeTempHome(Math.random().toString(36).slice(2));
  process.env["AF_PROJECT_HOME_OVERRIDE"] = home;
});

afterEach(() => {
  try {
    rmSync(home, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
  delete process.env["AF_PROJECT_HOME_OVERRIDE"];
});

// ---------------------------------------------------------------------------
// §1 — Two projects, different local data → different answers (item 4/6)
// ---------------------------------------------------------------------------

describe("handleDora — project param returns that project's own data", () => {
  it("projA (1 release) and projB (5 releases) report different deploy_frequency_per_day", async () => {
    writeProjectLayout(home, "projA", "acme/projA", 1);
    writeProjectLayout(home, "projB", "acme/projB", 5);

    const resultA = (await handleDora({ project: "projA" })) as Record<string, unknown>;
    const resultB = (await handleDora({ project: "projB" })) as Record<string, unknown>;

    expect(resultA["deploy_frequency_per_day"]).not.toBe(resultB["deploy_frequency_per_day"]);
    expect(resultA["deploy_frequency_per_day"]).toBe(round4(1 / 7));
    expect(resultB["deploy_frequency_per_day"]).toBe(round4(5 / 7));
  });
});

// ---------------------------------------------------------------------------
// §2 — Decline vs empty-but-resolvable (item 3/5)
// ---------------------------------------------------------------------------

describe("handleDora — decline contract", () => {
  it("declines with UnresolvableProjectError when the named project has no repo", async () => {
    writeProjectLayout(home, "norepoproj", null, 1);

    let caught: unknown;
    try {
      await handleDora({ project: "norepoproj" });
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeInstanceOf(UnresolvableProjectError);
    expect((caught as UnresolvableProjectError).rpc_code).toBe(-32001);
  });

  it("a resolvable project with genuinely no data does NOT decline — distinguishable from the no-repo case", async () => {
    writeProjectLayout(home, "emptyproj", "acme/emptyproj", 0);

    const result = (await handleDora({ project: "emptyproj" })) as Record<string, unknown>;
    expect(result["deploy_frequency_per_day"]).toBe(0.0);
    expect(result["applicable"]).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// §3 — No-project call is unaffected (no-regression companion)
// ---------------------------------------------------------------------------

describe("handleDora — no project param", () => {
  it("a call with no project param is not routed through project scoping", async () => {
    const result = (await handleDora({})) as Record<string, unknown>;
    expect(typeof result["applicable"]).toBe("boolean");
    expect(typeof result["deploy_frequency_per_day"]).toBe("number");
  });
});

// ---------------------------------------------------------------------------
// §4 — Cross-backend agreement: Python and TS for the SAME project (item 4)
// ---------------------------------------------------------------------------

describe("handleDora — cross-backend agreement with backend/rpc/stats_dora.py", () => {
  it("Python and TS return the same DORA/KPI numbers for the same resolvable project", async () => {
    writeProjectLayout(home, "projA", "acme/projA", 1);

    const tsResult = (await handleDora({ project: "projA" })) as Record<string, unknown>;
    const pyResult = await runPythonStatsDora(home, { project: "projA" });

    expect(pyResult.ok).toBe(true);
    const py = pyResult.result as Record<string, unknown>;
    expect(tsResult["deploy_frequency_per_day"]).toBe(py["deploy_frequency_per_day"] as number);
    expect(tsResult["velocity_all_time_per_day"]).toBe(py["velocity_all_time_per_day"] as number);
    expect(tsResult["cycle_time_median_hours"]).toBe(py["cycle_time_median_hours"] as number | null);
    expect(tsResult["change_failure_rate_pct"]).toBe(py["change_failure_rate_pct"] as string);
  });

  it("Python and TS both decline for the same unresolvable project", async () => {
    writeProjectLayout(home, "norepoproj", null, 1);

    let tsCaught: unknown;
    try {
      await handleDora({ project: "norepoproj" });
    } catch (e) {
      tsCaught = e;
    }
    const pyResult = await runPythonStatsDora(home, { project: "norepoproj" });

    expect(tsCaught).toBeInstanceOf(UnresolvableProjectError);
    expect((tsCaught as UnresolvableProjectError).rpc_code).toBe(-32001);
    expect(pyResult.ok).toBe(false);
    expect(pyResult.error).toBe("UnresolvableProjectError");
    expect(pyResult.rpc_code).toBe(-32001);
  });
});
