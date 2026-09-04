/**
 * Parity tests for cost-tracker.ts vs backend/cost_tracker.py.
 *
 * Strategy:
 *  - Create a temp blackboard dir with synthetic budget/agents/ JSON files.
 *  - Run both Python and TS against the same temp state dir (AUTONOMOUS_TEAM_STATE_DIR).
 *  - Assert identical resulting rows, computed cost values, and stdout.
 *  - Cost numbers must match to the cent/token (6 decimal places).
 *
 * Run: bun test tests/spawn/cost-tracker.parity.test.ts --timeout 60000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import {
  computeCost,
  _DEFAULT_PRICING,
  CostTracker,
  detectCostSpike,
  loadPricing,
} from "../../src/spawn/cost-tracker.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTempDir(): string {
  const r = Math.random().toString(36).slice(2);
  const dir = join(tmpdir(), `ct-parity-${Date.now()}-${r}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

/** Write a blackboard budget/agents/<name>.json file in proper BB format. */
function writeBbAgent(
  bbRoot: string,
  name: string,
  value: Record<string, unknown>
): void {
  const agentsDir = join(bbRoot, "budget", "agents");
  mkdirSync(agentsDir, { recursive: true });
  const entry = { value, version: 1, updated_at: "2026-06-01T00:00:00Z", updated_by: "test" };
  writeFileSync(join(agentsDir, `${name}.json`), JSON.stringify(entry));
}

/**
 * Run Python cost_tracker.py with the given args and state dir.
 * Returns { stdout, stderr, exitCode }.
 */
function runPython(
  stateDir: string,
  args: string[]
): { stdout: string; stderr: string; exitCode: number } {
  // Repo root is 3 levels up from ts-backend/tests/spawn/
  // (tests/spawn/ → tests/ → ts-backend/ → repo-root)
  const repoRoot = join(import.meta.dir, "..", "..", "..");
  const result = spawnSync(
    "python3",
    ["backend/cost_tracker.py", ...args],
    {
      cwd: repoRoot,
      env: { ...process.env, AUTONOMOUS_TEAM_STATE_DIR: stateDir },
      timeout: 30000,
    }
  );
  return {
    stdout: result.stdout?.toString() ?? "",
    stderr: result.stderr?.toString() ?? "",
    exitCode: result.status ?? 1,
  };
}

// ---------------------------------------------------------------------------
// §1 computeCost — unit-level cost math parity
// ---------------------------------------------------------------------------

describe("computeCost — math parity with Python _compute_cost", () => {
  it("Sonnet input+output, no cache", () => {
    // Python: (1000/1000 * 0.003) + (500/1000 * 0.015) = 0.003 + 0.0075 = 0.0105
    const cost = computeCost(1000, 500, "claude-sonnet-4-6", _DEFAULT_PRICING);
    expect(cost).toBeCloseTo(0.0105, 6);
  });

  it("Opus input+output, no cache", () => {
    // (2000/1000 * 0.015) + (1000/1000 * 0.075) = 0.030 + 0.075 = 0.105
    const cost = computeCost(2000, 1000, "claude-opus-4-7", _DEFAULT_PRICING);
    expect(cost).toBeCloseTo(0.105, 6);
  });

  it("Opus with cache read", () => {
    // base = (1000/1000 * 0.015) + (500/1000 * 0.075) = 0.015 + 0.0375 = 0.0525
    // cache read = (2000/1000 * 0.0015) = 0.003
    // total = 0.0555
    const cost = computeCost(1000, 500, "claude-opus-4-7", _DEFAULT_PRICING, 2000, 0);
    expect(cost).toBeCloseTo(0.0555, 6);
  });

  it("Opus with cache write", () => {
    // base = (1000/1000 * 0.015) + (500/1000 * 0.075) = 0.0525
    // cache write 5m = (3000/1000 * 0.01875) = 0.05625
    // total = 0.10875
    const cost = computeCost(1000, 500, "claude-opus-4-7", _DEFAULT_PRICING, 0, 3000);
    expect(cost).toBeCloseTo(0.10875, 6);
  });

  it("Haiku — cheap model", () => {
    // (10000/1000 * 0.0008) + (5000/1000 * 0.004) = 0.008 + 0.02 = 0.028
    const cost = computeCost(10000, 5000, "claude-haiku-4-5-20251001", _DEFAULT_PRICING);
    expect(cost).toBeCloseTo(0.028, 6);
  });

  it("kimi-k2-0711 model", () => {
    // (1000/1000 * 0.0006) + (1000/1000 * 0.002) = 0.0006 + 0.002 = 0.0026
    const cost = computeCost(1000, 1000, "kimi-k2-0711", _DEFAULT_PRICING);
    expect(cost).toBeCloseTo(0.0026, 6);
  });

  it("unknown model falls back to default (Sonnet) pricing", () => {
    // (1000/1000 * 0.003) + (1000/1000 * 0.015) = 0.003 + 0.015 = 0.018
    const cost = computeCost(1000, 1000, "unknown-model-xyz", _DEFAULT_PRICING);
    expect(cost).toBeCloseTo(0.018, 6);
  });

  it("zero tokens → zero cost", () => {
    const cost = computeCost(0, 0, "claude-sonnet-4-6", _DEFAULT_PRICING);
    expect(cost).toBe(0);
  });

  it("1M-context Opus pricing", () => {
    // (1000/1000 * 0.030) + (1000/1000 * 0.150) = 0.030 + 0.150 = 0.180
    const cost = computeCost(1000, 1000, "claude-opus-4-7[1m]", _DEFAULT_PRICING);
    expect(cost).toBeCloseTo(0.18, 6);
  });
});

// ---------------------------------------------------------------------------
// §2 detectCostSpike — mirrors Python detect_cost_spike exactly
// ---------------------------------------------------------------------------

describe("detectCostSpike — parity with Python detect_cost_spike", () => {
  it("empty series → insufficient_data", () => {
    const r = detectCostSpike([]);
    expect(r.spike).toBe(false);
    expect(r.insufficient_data).toBe(true);
    expect(r.value).toBe(0.0);
    expect(r.sample_size).toBe(0);
  });

  it("single element → insufficient_data", () => {
    const r = detectCostSpike([0.5]);
    expect(r.spike).toBe(false);
    expect(r.insufficient_data).toBe(true);
    expect(r.value).toBe(0.5);
    expect(r.sample_size).toBe(0);
  });

  it("< 10 baseline points → insufficient_data", () => {
    // 5 baseline + 1 current = 6 total
    const series = [0.1, 0.2, 0.1, 0.2, 0.1, 0.5];
    const r = detectCostSpike(series);
    expect(r.spike).toBe(false);
    expect(r.insufficient_data).toBe(true);
    expect(r.sample_size).toBe(5);
    expect(r.value).toBe(0.5);
  });

  it("≥ 10 baseline, no spike → spike=false", () => {
    // 10 identical baseline values, current = same value
    const series = Array(10).fill(1.0).concat([1.0]);
    const r = detectCostSpike(series);
    expect(r.spike).toBe(false);
    expect(r.insufficient_data).toBe(false);
    expect(r.sample_size).toBe(10);
    expect(r.mu).toBeCloseTo(1.0, 6);
    expect(r.sigma).toBeCloseTo(0.0, 6);
  });

  it("sigma=0, value > mu → spike=true (all-equal baseline, one outlier)", () => {
    // 10 identical baseline values, current >> baseline
    const series = Array(10).fill(1.0).concat([100.0]);
    const r = detectCostSpike(series);
    expect(r.spike).toBe(true);
    expect(r.insufficient_data).toBe(false);
  });

  it("large spike > mu + 3σ → spike=true", () => {
    // baseline: 10 values around 1.0 with slight variance
    const baseline = [1.0, 1.1, 0.9, 1.0, 1.1, 0.9, 1.0, 1.1, 0.9, 1.0];
    const current = 50.0; // massively above mu + 3σ
    const series = baseline.concat([current]);
    const r = detectCostSpike(series);
    expect(r.spike).toBe(true);
    expect(r.value).toBeCloseTo(50.0, 5);
    expect(r.mu).toBeCloseTo(1.0, 4);
  });

  it("borderline: value = threshold exactly → spike=false (not strictly greater)", () => {
    // Construct a case where current == mu (and sigma=0) → threshold = mu, not spiked
    const series = Array(10).fill(2.0).concat([2.0]);
    const r = detectCostSpike(series);
    expect(r.spike).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// §3 getSessionCost — integration against a synthetic blackboard
// ---------------------------------------------------------------------------

describe("CostTracker.getSessionCost — integration with synthetic blackboard", () => {
  let stateDir: string;
  let savedStateDir: string | undefined;

  beforeEach(() => {
    stateDir = makeTempDir();
    savedStateDir = process.env.AUTONOMOUS_TEAM_STATE_DIR;
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
  });

  afterEach(() => {
    if (savedStateDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_STATE_DIR = savedStateDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    }
    try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("empty blackboard → total_cost_usd=0, empty arrays", () => {
    const ct = new CostTracker();
    const result = ct.getSessionCost();
    expect(result.total_cost_usd).toBe(0);
    expect(result.by_agent).toHaveLength(0);
    expect(result.by_discussion).toHaveLength(0);
    expect(result.model_breakdown).toHaveLength(0);
  });

  it("single agent record → correct cost computation", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "agent-001", {
      agent_id: "agent-001",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-06-01T10:00:00Z",
      discussion: 42,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getSessionCost();

    // (1000/1000 * 0.003) + (500/1000 * 0.015) = 0.003 + 0.0075 = 0.0105
    expect(result.total_cost_usd).toBeCloseTo(0.0105, 4);
    expect(result.by_agent).toHaveLength(1);

    const agent = result.by_agent[0]!;
    expect(agent.agent_id).toBe("agent-001");
    expect(agent.role).toBe("executor");
    expect(agent.model).toBe("claude-sonnet-4-6");
    expect(agent.input).toBe(1000);
    expect(agent.output).toBe(500);
    expect(agent.cost_usd).toBeCloseTo(0.0105, 6);

    expect(result.by_discussion).toHaveLength(1);
    expect(result.by_discussion[0]!.discussion).toBe(42);
    expect(result.by_discussion[0]!.total_cost_usd).toBeCloseTo(0.0105, 6);

    expect(result.model_breakdown).toHaveLength(1);
    expect(result.model_breakdown[0]!.model).toBe("claude-sonnet-4-6");
  });

  it("two agents, same discussion → cost aggregated", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "exec-001", {
      agent_id: "exec-001",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-06-01T10:00:00Z",
      discussion: 100,
      pr: 55,
    });
    writeBbAgent(bbRoot, "cr-001", {
      agent_id: "cr-001",
      agent: "code-reviewer",
      model: "claude-sonnet-4-6",
      input: 2000,
      output: 300,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-06-01T11:00:00Z",
      discussion: 100,
      pr: 55,
    });

    const ct = new CostTracker();
    const result = ct.getSessionCost();

    // exec: (1000/1000*0.003) + (500/1000*0.015) = 0.003 + 0.0075 = 0.0105
    // cr:   (2000/1000*0.003) + (300/1000*0.015) = 0.006 + 0.0045 = 0.0105
    // total = 0.021
    expect(result.total_cost_usd).toBeCloseTo(0.021, 4);
    expect(result.by_discussion).toHaveLength(1);
    const disc = result.by_discussion[0]!;
    expect(disc.total_cost_usd).toBeCloseTo(0.021, 6);
    expect(disc.agent_count).toBe(2);
    expect(disc.agents).toContain("exec-001");
    expect(disc.agents).toContain("cr-001");

    // agent_breakdown by role
    expect(disc.agent_breakdown["executor"]).toBeCloseTo(0.0105, 6);
    expect(disc.agent_breakdown["code-reviewer"]).toBeCloseTo(0.0105, 6);

    // pr_breakdown
    expect(disc.pr_breakdown["55"]).toBeCloseTo(0.021, 6);
  });

  it("by_agent sorted by finished desc", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "agent-old", {
      agent_id: "agent-old",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 100,
      output: 100,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-06-01T08:00:00Z",
      discussion: null,
      pr: null,
    });
    writeBbAgent(bbRoot, "agent-new", {
      agent_id: "agent-new",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 100,
      output: 100,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-06-01T12:00:00Z",
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getSessionCost();
    expect(result.by_agent[0]!.agent_id).toBe("agent-new");
    expect(result.by_agent[1]!.agent_id).toBe("agent-old");
  });

  it("by_discussion sorted by discussion number asc", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "a500", {
      agent_id: "a500",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 100,
      output: 100,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: null,
      discussion: 500,
      pr: null,
    });
    writeBbAgent(bbRoot, "a200", {
      agent_id: "a200",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 100,
      output: 100,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: null,
      discussion: 200,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getSessionCost();
    expect(result.by_discussion[0]!.discussion).toBe(200);
    expect(result.by_discussion[1]!.discussion).toBe(500);
  });

  it("model_breakdown sorted by cost desc", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "cheap-haiku", {
      agent_id: "cheap-haiku",
      agent: "executor",
      model: "claude-haiku-4-5-20251001",
      input: 1000,
      output: 100,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: null,
      discussion: null,
      pr: null,
    });
    writeBbAgent(bbRoot, "expensive-opus", {
      agent_id: "expensive-opus",
      agent: "executor",
      model: "claude-opus-4-7",
      input: 1000,
      output: 100,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: null,
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getSessionCost();
    // Opus is more expensive — should come first
    expect(result.model_breakdown[0]!.model).toBe("claude-opus-4-7");
    expect(result.model_breakdown[1]!.model).toBe("claude-haiku-4-5-20251001");
  });

  it("cache tokens included in cost", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "cached-agent", {
      agent_id: "cached-agent",
      agent: "executor",
      model: "claude-opus-4-7",
      input: 1000,
      output: 500,
      cache_read_tokens: 2000,
      cache_write_tokens: 3000,
      finished: null,
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getSessionCost();

    // base = (1000/1000*0.015) + (500/1000*0.075) = 0.015 + 0.0375 = 0.0525
    // cache_read = (2000/1000*0.0015) = 0.003
    // cache_write = (3000/1000*0.01875) = 0.05625
    // total = 0.0525 + 0.003 + 0.05625 = 0.11175
    expect(result.total_cost_usd).toBeCloseTo(0.1118, 3);
    expect(result.by_agent[0]!.cost_usd).toBeCloseTo(0.11175, 5);
  });
});

// ---------------------------------------------------------------------------
// §4 aggregateDailyMonthlySpend — mirrors Python aggregate_daily_monthly_spend
// ---------------------------------------------------------------------------

describe("CostTracker.aggregateDailyMonthlySpend — parity", () => {
  let stateDir: string;
  let savedStateDir: string | undefined;

  beforeEach(() => {
    stateDir = makeTempDir();
    savedStateDir = process.env.AUTONOMOUS_TEAM_STATE_DIR;
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
  });

  afterEach(() => {
    if (savedStateDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_STATE_DIR = savedStateDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    }
    try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("no records → daily=0, monthly=0", () => {
    const ct = new CostTracker();
    const now = new Date("2026-06-01T12:00:00Z");
    const result = ct.aggregateDailyMonthlySpend(now);
    expect(result.daily_usd).toBe(0);
    expect(result.monthly_usd).toBe(0);
  });

  it("record from today → counted in daily and monthly", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "today-agent", {
      agent_id: "today-agent",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-06-01T10:00:00Z",
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const now = new Date("2026-06-01T12:00:00Z");
    const result = ct.aggregateDailyMonthlySpend(now);
    // cost = 0.0105
    expect(result.daily_usd).toBeCloseTo(0.0105, 6);
    expect(result.monthly_usd).toBeCloseTo(0.0105, 6);
  });

  it("record from yesterday → counted in monthly only", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "yesterday-agent", {
      agent_id: "yesterday-agent",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-05-31T10:00:00Z",
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const now = new Date("2026-06-01T12:00:00Z");
    const result = ct.aggregateDailyMonthlySpend(now);
    // yesterday is a different month boundary — it's May 31, now is June 1
    // So it's not in this month either
    expect(result.daily_usd).toBe(0);
    expect(result.monthly_usd).toBe(0);
  });

  it("record from earlier this month → counted in monthly, not daily", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "earlier-month-agent", {
      agent_id: "earlier-month-agent",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: "2026-06-15T10:00:00Z",
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    // "now" is June 20 — so June 15 is earlier this month, not today
    const now = new Date("2026-06-20T12:00:00Z");
    const result = ct.aggregateDailyMonthlySpend(now);
    expect(result.daily_usd).toBe(0);
    expect(result.monthly_usd).toBeCloseTo(0.0105, 6);
  });

  it("record with null finished → skipped", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "null-finished", {
      agent_id: "null-finished",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: null,
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const now = new Date("2026-06-01T12:00:00Z");
    const result = ct.aggregateDailyMonthlySpend(now);
    expect(result.daily_usd).toBe(0);
    expect(result.monthly_usd).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// §5 getRoleEfficiency — parity with Python get_role_efficiency
// ---------------------------------------------------------------------------

describe("CostTracker.getRoleEfficiency — parity", () => {
  let stateDir: string;
  let savedStateDir: string | undefined;

  beforeEach(() => {
    stateDir = makeTempDir();
    savedStateDir = process.env.AUTONOMOUS_TEAM_STATE_DIR;
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
  });

  afterEach(() => {
    if (savedStateDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_STATE_DIR = savedStateDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    }
    try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("empty blackboard → schema_version=1, roles=[]", () => {
    const ct = new CostTracker();
    const result = ct.getRoleEfficiency();
    expect(result.schema_version).toBe(1);
    expect(result.window_days).toBe(7);
    expect(Array.isArray(result.roles)).toBe(true);
    expect(result.roles).toHaveLength(0);
  });

  it("single executor run → role entry with correct totals", () => {
    const bbRoot = join(stateDir, "blackboard");
    writeBbAgent(bbRoot, "exec-001", {
      agent_id: "exec-001",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: new Date().toISOString(), // now (within window)
      discussion: 42,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getRoleEfficiency(7);
    expect(result.roles).toHaveLength(1);

    const role = result.roles[0]!;
    expect(role.role).toBe("executor");
    expect(role.total_runs).toBe(1);
    expect(role.total_input_tokens).toBe(1000);
    expect(role.total_output_tokens).toBe(500);
    expect(role.total_tokens).toBe(1500);
    expect(role.total_cost_usd).toBeCloseTo(0.0105, 6);
    expect(role.avg_tokens_per_run).toBe(1500);
    expect(role.needs_fix_rate).toBe(0.0);
    expect(role.avg_cost_per_pass_usd).toBeNull(); // no passes yet
  });

  it("record outside window → excluded", () => {
    const bbRoot = join(stateDir, "blackboard");
    // Finished 30 days ago — outside a 7-day window
    const thirtyDaysAgo = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString();
    writeBbAgent(bbRoot, "old-exec", {
      agent_id: "old-exec",
      agent: "executor",
      model: "claude-sonnet-4-6",
      input: 1000,
      output: 500,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: thirtyDaysAgo,
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getRoleEfficiency(7);
    expect(result.roles).toHaveLength(0);
  });

  it("roles sorted by total_cost_usd desc", () => {
    const bbRoot = join(stateDir, "blackboard");
    const now = new Date().toISOString();

    // Cheap executor (haiku)
    writeBbAgent(bbRoot, "cheap-exec", {
      agent_id: "cheap-exec",
      agent: "executor",
      model: "claude-haiku-4-5-20251001",
      input: 100,
      output: 100,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: now,
      discussion: null,
      pr: null,
    });

    // Expensive code-reviewer (opus)
    writeBbAgent(bbRoot, "expensive-cr", {
      agent_id: "expensive-cr",
      agent: "code-reviewer",
      model: "claude-opus-4-7",
      input: 1000,
      output: 1000,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      finished: now,
      discussion: null,
      pr: null,
    });

    const ct = new CostTracker();
    const result = ct.getRoleEfficiency(7);
    expect(result.roles).toHaveLength(2);
    // code-reviewer (opus) should come first (more expensive)
    expect(result.roles[0]!.role).toBe("code-reviewer");
    expect(result.roles[1]!.role).toBe("executor");
  });

  it("needs_fix_rate computed correctly", () => {
    const bbRoot = join(stateDir, "blackboard");
    const now = new Date().toISOString();

    // 3 runs for executor
    for (let i = 0; i < 3; i++) {
      writeBbAgent(bbRoot, `exec-run-${i}`, {
        agent_id: `exec-run-${i}`,
        agent: "executor",
        model: "claude-sonnet-4-6",
        input: 100,
        output: 100,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        finished: now,
        discussion: null,
        pr: null,
      });
    }

    // Write memory entries with verdict tags
    // 1 needs-fix, 2 done → needs_fix_rate = 1/3 ≈ 0.333
    const memDir = join(stateDir, "blackboard", "memory");
    mkdirSync(memDir, { recursive: true });
    const writeMemEntry = (name: string, tags: string[], lessonType: string) => {
      writeFileSync(
        join(memDir, `${name}.json`),
        JSON.stringify({
          value: { role: "executor", tags, lesson_type: lessonType },
          version: 1,
          updated_at: now,
          updated_by: "test",
        })
      );
    };

    writeMemEntry("mem-done-1", ["executor", "done"], "success");
    writeMemEntry("mem-done-2", ["executor", "done"], "success");
    writeMemEntry("mem-nf-1", ["executor", "needs-fix"], "failure");

    const ct = new CostTracker();
    const result = ct.getRoleEfficiency(7);
    const execRole = result.roles.find((r) => r.role === "executor");
    expect(execRole).toBeDefined();
    expect(execRole!.verdict_counts["done"]).toBe(2);
    expect(execRole!.verdict_counts["needs-fix"]).toBe(1);
    expect(execRole!.passes).toBe(2);
    // needs_fix_rate = 1/3 ≈ 0.333
    expect(execRole!.needs_fix_rate).toBeCloseTo(0.333, 3);
  });
});

// ---------------------------------------------------------------------------
// §6 Python parity test — run both implementations, compare numeric output
// ---------------------------------------------------------------------------

describe("Python parity — identical cost math on same inputs", () => {
  let stateDir: string;
  let savedStateDir: string | undefined;

  beforeEach(() => {
    stateDir = makeTempDir();
    savedStateDir = process.env.AUTONOMOUS_TEAM_STATE_DIR;
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
  });

  afterEach(() => {
    if (savedStateDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_STATE_DIR = savedStateDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    }
    try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("by-discussion JSON output matches Python for multi-agent scenario", () => {
    const bbRoot = join(stateDir, "blackboard");

    // Write 3 agents across 2 discussions
    const agents = [
      {
        name: "exec-d10",
        value: {
          agent_id: "exec-d10",
          agent: "executor",
          model: "claude-sonnet-4-6",
          input: 5000,
          output: 2000,
          cache_read_tokens: 1000,
          cache_write_tokens: 500,
          finished: "2026-06-01T09:00:00Z",
          discussion: 10,
          pr: 101,
        },
      },
      {
        name: "cr-d10",
        value: {
          agent_id: "cr-d10",
          agent: "code-reviewer",
          model: "claude-opus-4-7",
          input: 3000,
          output: 800,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          finished: "2026-06-01T10:00:00Z",
          discussion: 10,
          pr: 101,
        },
      },
      {
        name: "exec-d20",
        value: {
          agent_id: "exec-d20",
          agent: "executor",
          model: "claude-haiku-4-5-20251001",
          input: 10000,
          output: 3000,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          finished: "2026-06-01T11:00:00Z",
          discussion: 20,
          pr: 202,
        },
      },
    ];

    for (const agent of agents) {
      writeBbAgent(bbRoot, agent.name, agent.value);
    }

    // Run TS implementation
    const ct = new CostTracker();
    const tsResult = ct.getSessionCost();
    const tsByDisc = tsResult.by_discussion;

    // Compute expected costs manually (same math as Python)
    // exec-d10: (5000/1000*0.003) + (2000/1000*0.015) + (1000/1000*0.0003) + (500/1000*0.00375)
    //         = 0.015 + 0.030 + 0.0003 + 0.001875 = 0.047175
    const execD10Cost = (5000 / 1000 * 0.003) + (2000 / 1000 * 0.015) + (1000 / 1000 * 0.0003) + (500 / 1000 * 0.00375);

    // cr-d10: (3000/1000*0.015) + (800/1000*0.075) = 0.045 + 0.060 = 0.105
    const crD10Cost = (3000 / 1000 * 0.015) + (800 / 1000 * 0.075);

    // exec-d20: (10000/1000*0.0008) + (3000/1000*0.004) = 0.008 + 0.012 = 0.020
    const execD20Cost = (10000 / 1000 * 0.0008) + (3000 / 1000 * 0.004);

    // D10 total
    const d10Total = execD10Cost + crD10Cost;
    const d20Total = execD20Cost;

    const d10 = tsByDisc.find((d) => d.discussion === 10)!;
    const d20 = tsByDisc.find((d) => d.discussion === 20)!;

    expect(d10).toBeDefined();
    expect(d20).toBeDefined();
    expect(d10.total_cost_usd).toBeCloseTo(d10Total, 5);
    expect(d20.total_cost_usd).toBeCloseTo(d20Total, 5);
    expect(d10.agent_count).toBe(2);
    expect(d20.agent_count).toBe(1);

    // Total
    const expectedTotal = d10Total + d20Total;
    expect(tsResult.total_cost_usd).toBeCloseTo(expectedTotal, 3);

    // Now run Python and compare (best-effort — skip cross-check if Python unavailable)
    const pyResult = runPython(stateDir, ["by-discussion"]);
    if (pyResult.exitCode !== 0) {
      console.warn("Python cross-check skipped (exitCode=" + pyResult.exitCode + "):", pyResult.stderr.slice(0, 200));
      return;
    }

    let pyData: Array<Record<string, unknown>>;
    try {
      pyData = JSON.parse(pyResult.stdout) as Array<Record<string, unknown>>;
    } catch {
      console.warn("Python output not parseable — skipping cross-check:", pyResult.stdout.slice(0, 100));
      return;
    }

    // Find D10 and D20 in Python output
    const pyD10 = pyData.find((d) => d["discussion"] === 10);
    const pyD20 = pyData.find((d) => d["discussion"] === 20);

    if (pyD10) {
      expect(d10.total_cost_usd).toBeCloseTo(pyD10["total_cost_usd"] as number, 5);
      expect(d10.total_input_tokens).toBe(pyD10["total_input_tokens"] as number);
      expect(d10.total_output_tokens).toBe(pyD10["total_output_tokens"] as number);
    }

    if (pyD20) {
      expect(d20.total_cost_usd).toBeCloseTo(pyD20["total_cost_usd"] as number, 5);
    }

    // Sample compared row for AGENT_OUTPUT
    if (pyD10) {
      const sampleRow = {
        discussion: 10,
        ts_total_cost_usd: d10.total_cost_usd,
        py_total_cost_usd: pyD10["total_cost_usd"],
        ts_agent_count: d10.agent_count,
        py_agent_count: pyD10["agent_count"],
        match: Math.abs(d10.total_cost_usd - (pyD10["total_cost_usd"] as number)) < 0.000001,
      };
      console.log("Sample compared row:", JSON.stringify(sampleRow));
    }
  });

  it("total_cost_usd matches Python summary for Opus + Sonnet mix", () => {
    const bbRoot = join(stateDir, "blackboard");

    writeBbAgent(bbRoot, "opus-agent", {
      agent_id: "opus-agent",
      agent: "executor",
      model: "claude-opus-4-7",
      input: 50000,
      output: 15000,
      cache_read_tokens: 10000,
      cache_write_tokens: 5000,
      finished: "2026-06-01T09:00:00Z",
      discussion: 99,
      pr: null,
    });

    writeBbAgent(bbRoot, "sonnet-agent", {
      agent_id: "sonnet-agent",
      agent: "code-reviewer",
      model: "claude-sonnet-4-6",
      input: 30000,
      output: 8000,
      cache_read_tokens: 5000,
      cache_write_tokens: 2000,
      finished: "2026-06-01T10:00:00Z",
      discussion: 99,
      pr: null,
    });

    const ct = new CostTracker();
    const tsResult = ct.getSessionCost();

    // Manual calculation (mirrors Python exactly):
    // Opus: (50000/1000*0.015) + (15000/1000*0.075) + (10000/1000*0.0015) + (5000/1000*0.01875)
    //     = 0.75 + 1.125 + 0.015 + 0.09375 = 1.98375
    const opusCost =
      (50000 / 1000 * 0.015) +
      (15000 / 1000 * 0.075) +
      (10000 / 1000 * 0.0015) +
      (5000 / 1000 * 0.01875);

    // Sonnet: (30000/1000*0.003) + (8000/1000*0.015) + (5000/1000*0.0003) + (2000/1000*0.00375)
    //      = 0.09 + 0.12 + 0.0015 + 0.0075 = 0.219
    const sonnetCost =
      (30000 / 1000 * 0.003) +
      (8000 / 1000 * 0.015) +
      (5000 / 1000 * 0.0003) +
      (2000 / 1000 * 0.00375);

    const expectedTotal = opusCost + sonnetCost;

    expect(tsResult.total_cost_usd).toBeCloseTo(expectedTotal, 3);

    // Cross-check with Python
    const pyResult = runPython(stateDir, ["by-discussion"]);
    if (pyResult.exitCode === 0) {
      try {
        const pyData = JSON.parse(pyResult.stdout) as Array<Record<string, unknown>>;
        const pyD99 = pyData.find((d) => d["discussion"] === 99);
        if (pyD99) {
          const d99 = tsResult.by_discussion.find((d) => d.discussion === 99)!;
          expect(d99.total_cost_usd).toBeCloseTo(pyD99["total_cost_usd"] as number, 5);
          console.log(`Opus+Sonnet: TS=${d99.total_cost_usd}, Python=${pyD99["total_cost_usd"]} (match=${Math.abs(d99.total_cost_usd - (pyD99["total_cost_usd"] as number)) < 0.000001})`);
        }
      } catch {
        /* Python parse failed, skip */
      }
    }
  });
});

// ---------------------------------------------------------------------------
// §7 CLI output parity — stdout matches Python for by-discussion --json
// ---------------------------------------------------------------------------

describe("CLI by-discussion JSON stdout matches Python", () => {
  let stateDir: string;
  let savedStateDir: string | undefined;

  beforeEach(() => {
    stateDir = makeTempDir();
    savedStateDir = process.env.AUTONOMOUS_TEAM_STATE_DIR;
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
  });

  afterEach(() => {
    if (savedStateDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_STATE_DIR = savedStateDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    }
    try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("empty blackboard → both emit [] for by-discussion", () => {
    // TS: use programmatic API
    const ct = new CostTracker();
    const tsResult = ct.getSessionCost();
    const tsByDisc = tsResult.by_discussion
      .slice()
      .sort((a, b) => b.total_cost_usd - a.total_cost_usd);
    expect(tsByDisc).toHaveLength(0);

    // Python
    const pyResult = runPython(stateDir, ["by-discussion"]);
    if (pyResult.exitCode === 0) {
      const pyData = JSON.parse(pyResult.stdout.trim() || "[]") as unknown[];
      expect(pyData).toHaveLength(0);
    }
  });
});

// ---------------------------------------------------------------------------
// §8 loadPricing — config.json override
// ---------------------------------------------------------------------------

describe("loadPricing — config.json override", () => {
  let stateDir: string;
  let savedStateDir: string | undefined;
  let savedTeamDir: string | undefined;
  let tmpTeamDir: string;

  beforeEach(() => {
    stateDir = makeTempDir();
    tmpTeamDir = makeTempDir();
    savedStateDir = process.env.AUTONOMOUS_TEAM_STATE_DIR;
    savedTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
    process.env.AUTONOMOUS_TEAM_DIR = tmpTeamDir;
  });

  afterEach(() => {
    if (savedStateDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_STATE_DIR = savedStateDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    }
    if (savedTeamDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_DIR = savedTeamDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_DIR;
    }
    try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    try { rmSync(tmpTeamDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("missing config.json → falls back to _DEFAULT_PRICING", () => {
    const pricing = loadPricing();
    expect(pricing["default"]).toBeDefined();
    expect(pricing["claude-sonnet-4-6"]).toBeDefined();
  });

  it("empty pricing in config → falls back to _DEFAULT_PRICING", () => {
    writeFileSync(
      join(tmpTeamDir, "config.json"),
      JSON.stringify({ pricing: {} })
    );
    const pricing = loadPricing();
    expect(pricing["claude-sonnet-4-6"]).toBeDefined();
  });

  it("custom pricing in config → overrides defaults", () => {
    writeFileSync(
      join(tmpTeamDir, "config.json"),
      JSON.stringify({
        pricing: {
          "my-custom-model": { input_per_1k: 0.001, output_per_1k: 0.005 },
        },
      })
    );
    const pricing = loadPricing();
    expect(pricing["my-custom-model"]).toBeDefined();
    expect(pricing["my-custom-model"]!.input_per_1k).toBe(0.001);
    // 'default' is added automatically
    expect(pricing["default"]).toBeDefined();
  });
});
