/**
 * cost-tracker.ts — Mirrors backend/cost_tracker.py 1:1.
 *
 * Cost tracking module — maps token usage to dollar amounts per model.
 *
 * Loads pricing from .autonomous-team/config.json under the `pricing` key.
 * Reads per-agent spend records from the file-based blackboard and computes
 * aggregate costs.
 *
 * CLI usage:
 *   bun run src/spawn/cost-tracker.ts summary
 *   bun run src/spawn/cost-tracker.ts by-discussion
 *   bun run src/spawn/cost-tracker.ts by-discussion --top 5
 *   bun run src/spawn/cost-tracker.ts by-discussion --discussion 367
 *   bun run src/spawn/cost-tracker.ts by-discussion --text
 *   bun run src/spawn/cost-tracker.ts by-role
 *   bun run src/spawn/cost-tracker.ts by-role --days 14
 *   bun run src/spawn/cost-tracker.ts by-role --json
 *   bun run src/spawn/cost-tracker.ts by-role --top 3
 *
 * Programmatic usage:
 *   import { CostTracker } from "./cost-tracker.js";
 *   const ct = new CostTracker();
 *   const breakdown = ct.getSessionCost();
 *   console.log(breakdown.total_cost_usd);
 */

import { existsSync, readFileSync, readdirSync, writeFileSync, mkdirSync, renameSync } from "node:fs";
import { join } from "node:path";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

function stateDir(): string {
  return sharedStateDir();
}

function repoRoot(): string {
  if (process.env.AF_REPO_ROOT) return process.env.AF_REPO_ROOT;
  if (process.env.AUTONOMOUS_TEAM_DIR)
    return join(process.env.AUTONOMOUS_TEAM_DIR, "..");
  // This file lives at ts-backend/src/spawn/cost-tracker.ts
  // → ts-backend/src/spawn/ → ts-backend/src/ → ts-backend/ → repo root
  const thisFile = new URL(import.meta.url).pathname;
  return join(thisFile, "..", "..", "..", "..");
}

function autonomousTeamDir(): string {
  return process.env.AUTONOMOUS_TEAM_DIR ?? join(repoRoot(), ".autonomous-team");
}

// ---------------------------------------------------------------------------
// Default pricing table (mirrors Python _DEFAULT_PRICING exactly)
// ---------------------------------------------------------------------------

export interface PricingRate {
  input_per_1k: number;
  output_per_1k: number;
  cache_read_per_1k?: number;
  cache_write_5m_per_1k?: number;
  cache_write_1h_per_1k?: number;
  input_per_1k_above_200k?: number;
}

export type PricingTable = Record<string, PricingRate>;

export const _DEFAULT_PRICING: PricingTable = {
  // Fallback for unknown models — Sonnet rates.
  default: { input_per_1k: 0.003, output_per_1k: 0.015 },
  // ── Current models (as of 2026-05-11) ────────────────────────────────────
  "claude-opus-4-7": {
    input_per_1k: 0.015,
    output_per_1k: 0.075,
    cache_read_per_1k: 0.0015,
    cache_write_5m_per_1k: 0.01875,
    cache_write_1h_per_1k: 0.03,
  },
  // 1M-context Opus variant — higher flat rate for all tokens
  "claude-opus-4-7[1m]": {
    input_per_1k: 0.030,
    output_per_1k: 0.150,
    input_per_1k_above_200k: 0.030,
    cache_read_per_1k: 0.003,
    cache_write_5m_per_1k: 0.0375,
  },
  "claude-sonnet-4-6": {
    input_per_1k: 0.003,
    output_per_1k: 0.015,
    cache_read_per_1k: 0.0003,
    cache_write_5m_per_1k: 0.00375,
  },
  "claude-sonnet-4-5-20250929": {
    input_per_1k: 0.003,
    output_per_1k: 0.015,
  },
  "claude-haiku-4-5-20251001": {
    input_per_1k: 0.0008,
    output_per_1k: 0.004,
  },
  // ── Legacy models (kept for historical backfill accuracy) ─────────────────
  "claude-sonnet-4-20250514": { input_per_1k: 0.003, output_per_1k: 0.015 },
  "claude-opus-4-20250514": { input_per_1k: 0.015, output_per_1k: 0.075 },
  "kimi-k2-0711": { input_per_1k: 0.0006, output_per_1k: 0.002 },
};

const _AGENTS_PREFIX = "budget/agents/";

// Track unknown models so we warn only once per process lifetime.
const _WARNED_UNKNOWN_MODELS = new Set<string>();

// ---------------------------------------------------------------------------
// loadPricing — mirrors Python _load_pricing()
// ---------------------------------------------------------------------------

export function loadPricing(): PricingTable {
  try {
    const configPath = join(autonomousTeamDir(), "config.json");
    if (!existsSync(configPath)) return { ..._DEFAULT_PRICING };
    const cfg = JSON.parse(readFileSync(configPath, "utf-8")) as Record<string, unknown>;
    const pricing = cfg["pricing"] as PricingTable | undefined;
    if (!pricing || typeof pricing !== "object" || Object.keys(pricing).length === 0) {
      return { ..._DEFAULT_PRICING };
    }
    if (!pricing["default"]) pricing["default"] = _DEFAULT_PRICING["default"]!;
    return pricing;
  } catch {
    return { ..._DEFAULT_PRICING };
  }
}

// ---------------------------------------------------------------------------
// computeCost — mirrors Python _compute_cost()
// ---------------------------------------------------------------------------

export function computeCost(
  inputTokens: number,
  outputTokens: number,
  model: string,
  pricing: PricingTable,
  cacheReadTokens = 0,
  cacheWriteTokens = 0,
): number {
  if (!(model in pricing)) {
    if (!_WARNED_UNKNOWN_MODELS.has(model)) {
      _WARNED_UNKNOWN_MODELS.add(model);
      process.stderr.write(
        `[cost_tracker] WARNING: unknown model '${model}' — using default pricing. ` +
          "Update _DEFAULT_PRICING or .autonomous-team/config.json.\n"
      );
    }
  }

  const rates = pricing[model] ?? pricing["default"] ?? _DEFAULT_PRICING["default"]!;
  const inputRate = rates.input_per_1k ?? 0.003;
  const outputRate = rates.output_per_1k ?? 0.015;

  let cost = (inputTokens / 1000.0) * inputRate + (outputTokens / 1000.0) * outputRate;

  if (cacheReadTokens > 0) {
    const cacheReadRate = rates.cache_read_per_1k ?? 0.0;
    cost += (cacheReadTokens / 1000.0) * cacheReadRate;
  }

  if (cacheWriteTokens > 0) {
    const cacheWriteRate = rates.cache_write_5m_per_1k ?? 0.0;
    cost += (cacheWriteTokens / 1000.0) * cacheWriteRate;
  }

  return cost;
}

// ---------------------------------------------------------------------------
// Internal data types
// ---------------------------------------------------------------------------

interface AgentRecord {
  agent_id: string;
  role: string;
  model: string;
  input: number;
  output: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  finished: string | null;
  discussion: number | null;
  pr: string | null;
}

interface DiscussionTotal {
  discussion: number;
  cost_usd: number;
  agents: string[];
  input_tokens: number;
  output_tokens: number;
  agent_count: number;
  _agent_breakdown: Record<string, number>;
  _pr_breakdown: Record<string, number>;
}

// ---------------------------------------------------------------------------
// Blackboard reading helpers
// ---------------------------------------------------------------------------

function bbRoot(): string {
  return join(stateDir(), "blackboard");
}

function listBbKeys(prefix: string): string[] {
  // prefix e.g. "budget/agents/"
  const parts = prefix.replace(/\/$/, "").split("/");
  const dirPath = join(bbRoot(), ...parts);
  if (!existsSync(dirPath)) return [];
  try {
    const files = readdirSync(dirPath).filter((f) => f.endsWith(".json"));
    return files.map((f) => prefix + f.replace(/\.json$/, ""));
  } catch {
    return [];
  }
}

function readBbRecord(key: string): Record<string, unknown> | null {
  // key e.g. "budget/agents/some-agent"
  const parts = key.split("/");
  const filePath = join(bbRoot(), ...parts) + ".json";
  if (!existsSync(filePath)) return null;
  try {
    const raw = readFileSync(filePath, "utf-8");
    const entry = JSON.parse(raw) as Record<string, unknown>;
    // File blackboard entry: {"value": {...}, "version": int, ...}
    const value = entry["value"];
    if (value !== null && value !== undefined && typeof value === "object") {
      return value as Record<string, unknown>;
    }
    return entry;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// CostTracker class — mirrors Python CostTracker exactly
// ---------------------------------------------------------------------------

export interface SessionCost {
  total_cost_usd: number;
  by_agent: AgentEntry[];
  by_discussion: DiscussionEntry[];
  model_breakdown: ModelEntry[];
}

export interface AgentEntry {
  agent_id: string;
  role: string;
  model: string;
  input: number;
  output: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  finished: string | null;
  discussion: number | null;
}

export interface DiscussionEntry {
  discussion: number;
  cost_usd: number;
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  agent_count: number;
  agents: string[];
  agent_breakdown: Record<string, number>;
  pr_breakdown: Record<string, number>;
}

export interface ModelEntry {
  model: string;
  input: number;
  output: number;
  cost_usd: number;
  agent_count: number;
}

export interface RoleEfficiencyData {
  schema_version: 1;
  generated_at: string;
  window_days: number;
  roles: RoleEntry[];
}

export interface RoleEntry {
  role: string;
  total_runs: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  avg_tokens_per_run: number;
  verdict_counts: Record<string, number>;
  passes: number;
  needs_fix_rate: number;
  avg_cost_per_pass_usd: number | null;
}

export class CostTracker {
  private _pricing: PricingTable;

  constructor() {
    this._pricing = loadPricing();
  }

  /** Return the dollar cost for a single spend record. */
  computeCost(
    inputTokens: number,
    outputTokens: number,
    model = "default",
    cacheReadTokens = 0,
    cacheWriteTokens = 0,
  ): number {
    return computeCost(
      inputTokens,
      outputTokens,
      model,
      this._pricing,
      cacheReadTokens,
      cacheWriteTokens,
    );
  }

  /**
   * Aggregate all per-agent spend records from the blackboard and compute costs.
   * Mirrors Python CostTracker.get_session_cost() exactly.
   */
  getSessionCost(): SessionCost {
    const agentKeys = listBbKeys(_AGENTS_PREFIX);

    const byAgent: AgentRecord[] = [];
    const modelTotals = new Map<string, ModelEntry>();
    const discussionTotals = new Map<number, DiscussionTotal>();

    for (const key of agentKeys) {
      const record = readBbRecord(key);
      if (!record) continue;

      const inputTokens = Math.trunc(parseInt(String(record["input"] ?? 0), 10)) || 0;
      const outputTokens = Math.trunc(parseInt(String(record["output"] ?? 0), 10)) || 0;
      const cacheReadTokens = Math.trunc(parseInt(String(record["cache_read_tokens"] ?? 0), 10)) || 0;
      const cacheWriteTokens = Math.trunc(parseInt(String(record["cache_write_tokens"] ?? 0), 10)) || 0;
      const model = (record["model"] as string) || "default";
      const agentId = (record["agent_id"] as string) || key.replace(_AGENTS_PREFIX, "");
      const role = (record["agent"] as string) || "unknown";

      const discussionRaw = record["discussion"];
      const discussionParsed =
        discussionRaw !== null && discussionRaw !== undefined
          ? Math.trunc(parseInt(String(discussionRaw), 10))
          : NaN;
      const discussion = isNaN(discussionParsed) ? null : discussionParsed;

      const prRaw = record["pr"];
      const pr: string | null =
        prRaw !== null && prRaw !== undefined ? String(prRaw) : null;

      const cost = computeCost(
        inputTokens,
        outputTokens,
        model,
        this._pricing,
        cacheReadTokens,
        cacheWriteTokens,
      );

      byAgent.push({
        agent_id: agentId,
        role,
        model,
        input: inputTokens,
        output: outputTokens,
        cache_read_tokens: cacheReadTokens,
        cache_write_tokens: cacheWriteTokens,
        cost_usd: parseFloat(cost.toFixed(6)),
        finished: (record["finished"] as string) || null,
        discussion,
        pr,
      });

      // Accumulate model totals
      if (!modelTotals.has(model)) {
        modelTotals.set(model, {
          model,
          input: 0,
          output: 0,
          cost_usd: 0,
          agent_count: 0,
        });
      }
      const mt = modelTotals.get(model)!;
      mt.input += inputTokens;
      mt.output += outputTokens;
      mt.cost_usd += cost;
      mt.agent_count++;

      // Accumulate discussion totals
      if (discussion !== null) {
        if (!discussionTotals.has(discussion)) {
          discussionTotals.set(discussion, {
            discussion,
            cost_usd: 0,
            agents: [],
            input_tokens: 0,
            output_tokens: 0,
            agent_count: 0,
            _agent_breakdown: {},
            _pr_breakdown: {},
          });
        }
        const dt = discussionTotals.get(discussion)!;
        dt.cost_usd += cost;
        dt.agents.push(agentId);
        dt.input_tokens += inputTokens;
        dt.output_tokens += outputTokens;
        dt.agent_count++;
        dt._agent_breakdown[role] = (dt._agent_breakdown[role] ?? 0) + cost;
        if (pr !== null) {
          dt._pr_breakdown[pr] = (dt._pr_breakdown[pr] ?? 0) + cost;
        }
      }
    }

    const totalCost = byAgent.reduce((s, a) => s + a.cost_usd, 0);

    // Round model totals
    const modelBreakdown: ModelEntry[] = [];
    for (const entry of modelTotals.values()) {
      modelBreakdown.push({
        ...entry,
        cost_usd: parseFloat(entry.cost_usd.toFixed(6)),
      });
    }

    // Build by_discussion output
    const byDiscussion: DiscussionEntry[] = [];
    for (const entry of discussionTotals.values()) {
      const costUsd = parseFloat(entry.cost_usd.toFixed(6));
      byDiscussion.push({
        discussion: entry.discussion,
        cost_usd: costUsd,
        total_cost_usd: costUsd,
        total_input_tokens: entry.input_tokens,
        total_output_tokens: entry.output_tokens,
        agent_count: entry.agent_count,
        agents: entry.agents,
        agent_breakdown: Object.fromEntries(
          Object.entries(entry._agent_breakdown).map(([k, v]) => [k, parseFloat(v.toFixed(6))])
        ),
        pr_breakdown: Object.fromEntries(
          Object.entries(entry._pr_breakdown).map(([k, v]) => [k, parseFloat(v.toFixed(6))])
        ),
      });
    }

    return {
      total_cost_usd: parseFloat(totalCost.toFixed(4)),
      // Sort by_agent: by finished desc (nulls last), mirrors Python sorted(..., reverse=True)
      by_agent: byAgent
        .sort((a, b) => {
          const fa = a.finished ?? "";
          const fb = b.finished ?? "";
          if (fa > fb) return -1;
          if (fa < fb) return 1;
          return 0;
        })
        .map((a) => ({
          agent_id: a.agent_id,
          role: a.role,
          model: a.model,
          input: a.input,
          output: a.output,
          cache_read_tokens: a.cache_read_tokens,
          cache_write_tokens: a.cache_write_tokens,
          cost_usd: a.cost_usd,
          finished: a.finished,
          discussion: a.discussion,
        })),
      by_discussion: byDiscussion.sort((a, b) => a.discussion - b.discussion),
      model_breakdown: modelBreakdown.sort((a, b) => b.cost_usd - a.cost_usd),
    };
  }

  /**
   * Aggregate spend for a specific PR.
   * Mirrors Python CostTracker.per_pr_summary() exactly.
   */
  perPrSummary(prNumber: number): Record<string, unknown> | null {
    // Try to find linked discussion from quality/<pr_number>
    const qualityRecord = readBbRecord(`quality/${prNumber}`);
    let linkedDiscussion: number | null = null;
    if (qualityRecord) {
      const disc = qualityRecord["discussion"] ?? qualityRecord["pr"];
      if (disc !== null && disc !== undefined) {
        const parsed = Math.trunc(parseInt(String(disc), 10));
        if (!isNaN(parsed)) linkedDiscussion = parsed;
      }
    }

    const agentKeys = listBbKeys(_AGENTS_PREFIX);
    const roleTotals = new Map<string, { role: string; input_tokens: number; output_tokens: number; usd: number }>();

    for (const key of agentKeys) {
      const record = readBbRecord(key);
      if (!record) continue;

      const discussion: number | null = (() => {
        const raw = record["discussion"];
        if (raw === null || raw === undefined) return null;
        const parsed = Math.trunc(parseInt(String(raw), 10));
        return isNaN(parsed) ? null : parsed;
      })();

      const agentId = (record["agent_id"] as string) || "";

      // Match by discussion link OR by pr_number in agent_id
      let matches = false;
      if (linkedDiscussion !== null && discussion === linkedDiscussion) {
        matches = true;
      } else if (String(agentId).includes(String(prNumber))) {
        matches = true;
      }

      if (!matches) continue;

      const inputTokens = Math.trunc(parseInt(String(record["input"] ?? 0), 10)) || 0;
      const outputTokens = Math.trunc(parseInt(String(record["output"] ?? 0), 10)) || 0;
      const cacheReadTokens = Math.trunc(parseInt(String(record["cache_read_tokens"] ?? 0), 10)) || 0;
      const cacheWriteTokens = Math.trunc(parseInt(String(record["cache_write_tokens"] ?? 0), 10)) || 0;
      const model = (record["model"] as string) || "default";
      const role = (record["agent"] as string) || "unknown";

      const cost = computeCost(
        inputTokens,
        outputTokens,
        model,
        this._pricing,
        cacheReadTokens,
        cacheWriteTokens,
      );

      if (!roleTotals.has(role)) {
        roleTotals.set(role, { role, input_tokens: 0, output_tokens: 0, usd: 0 });
      }
      const rt = roleTotals.get(role)!;
      rt.input_tokens += inputTokens;
      rt.output_tokens += outputTokens;
      rt.usd += cost;
    }

    if (roleTotals.size === 0) return null;

    let totalInput = 0, totalOutput = 0, totalUsd = 0;
    const byRole: Array<{ role: string; input_tokens: number; output_tokens: number; usd: number }> = [];

    for (const entry of roleTotals.values()) {
      totalInput += entry.input_tokens;
      totalOutput += entry.output_tokens;
      totalUsd += entry.usd;
      byRole.push({
        role: entry.role,
        input_tokens: entry.input_tokens,
        output_tokens: entry.output_tokens,
        usd: parseFloat(entry.usd.toFixed(6)),
      });
    }

    return {
      input_tokens: totalInput,
      output_tokens: totalOutput,
      total_tokens: totalInput + totalOutput,
      usd: parseFloat(totalUsd.toFixed(6)),
      by_role: byRole.sort((a, b) => b.usd - a.usd),
    };
  }

  /**
   * Return real USD spend for today (UTC) and the current calendar month.
   * Mirrors Python CostTracker.aggregate_daily_monthly_spend() exactly.
   */
  aggregateDailyMonthlySpend(now?: Date): { daily_usd: number; monthly_usd: number } {
    const utcNow = now ?? new Date();
    // today midnight UTC
    const todayMidnight = new Date(Date.UTC(
      utcNow.getUTCFullYear(),
      utcNow.getUTCMonth(),
      utcNow.getUTCDate(),
    ));
    // month start UTC
    const monthStart = new Date(Date.UTC(
      utcNow.getUTCFullYear(),
      utcNow.getUTCMonth(),
      1,
    ));

    const session = this.getSessionCost();
    let daily = 0.0;
    let monthly = 0.0;

    for (const record of session.by_agent) {
      const finishedRaw = record.finished;
      if (!finishedRaw) continue;

      let finishedDt: Date;
      try {
        // Handle "Z" suffix like Python does
        const ts = finishedRaw.trim().endsWith("Z")
          ? finishedRaw.trim().slice(0, -1) + "+00:00"
          : finishedRaw.trim();
        finishedDt = new Date(ts);
        if (isNaN(finishedDt.getTime())) continue;
      } catch {
        continue;
      }

      const cost = record.cost_usd;
      if (finishedDt >= monthStart) monthly += cost;
      if (finishedDt >= todayMidnight) daily += cost;
    }

    return {
      daily_usd: parseFloat(daily.toFixed(6)),
      monthly_usd: parseFloat(monthly.toFixed(6)),
    };
  }

  /** Return a lightweight summary: total cost and model breakdown only. */
  getSummary(): { total_cost_usd: number; model_breakdown: ModelEntry[] } {
    const full = this.getSessionCost();
    return {
      total_cost_usd: full.total_cost_usd,
      model_breakdown: full.model_breakdown,
    };
  }

  /**
   * Aggregate per-role cost and verdict stats from the blackboard.
   * Mirrors Python CostTracker.get_role_efficiency() exactly.
   */
  getRoleEfficiency(days = 7): RoleEfficiencyData {
    const now = new Date();
    const generatedAt = now.toISOString().replace(/\.\d{3}Z$/, "Z");
    const cutoffMs = now.getTime() - days * 24 * 60 * 60 * 1000;

    // 1. Pull budget/agents/* blackboard entries within the time window
    const agentKeys = listBbKeys(_AGENTS_PREFIX);
    const roleBuckets = new Map<string, {
      role: string;
      total_runs: number;
      total_input_tokens: number;
      total_output_tokens: number;
      total_cost_usd: number;
      verdict_counts: Record<string, number>;
    }>();

    for (const key of agentKeys) {
      const record = readBbRecord(key);
      if (!record) continue;

      const inputTokens = Math.trunc(parseInt(String(record["input"] ?? 0), 10)) || 0;
      const outputTokens = Math.trunc(parseInt(String(record["output"] ?? 0), 10)) || 0;
      if (inputTokens + outputTokens === 0) continue;

      const role = (record["agent"] as string) || (record["role"] as string) || "";
      if (!role) continue;

      // Time window filter using the 'finished' timestamp
      const finished = (record["finished"] as string) || "";
      if (finished) {
        try {
          const finishedDt = new Date(finished.replace("Z", "+00:00") as string);
          if (!isNaN(finishedDt.getTime()) && finishedDt.getTime() < cutoffMs) {
            continue;
          }
        } catch {
          // skip unparseable — same as Python's except ValueError: pass
        }
      }

      const model = (record["model"] as string) || "default";
      const cacheReadTokens = Math.trunc(parseInt(String(record["cache_read_tokens"] ?? 0), 10)) || 0;
      const cacheWriteTokens = Math.trunc(parseInt(String(record["cache_write_tokens"] ?? 0), 10)) || 0;
      const cost = computeCost(
        inputTokens,
        outputTokens,
        model,
        this._pricing,
        cacheReadTokens,
        cacheWriteTokens,
      );

      if (!roleBuckets.has(role)) {
        roleBuckets.set(role, {
          role,
          total_runs: 0,
          total_input_tokens: 0,
          total_output_tokens: 0,
          total_cost_usd: 0,
          verdict_counts: {},
        });
      }
      const bucket = roleBuckets.get(role)!;
      bucket.total_runs++;
      bucket.total_input_tokens += inputTokens;
      bucket.total_output_tokens += outputTokens;
      bucket.total_cost_usd += cost;
    }

    // 2. Pull memory/* entries to extract verdict tags
    try {
      const memoryKeys = listBbKeys("memory/");
      for (const mkey of memoryKeys) {
        const mem = readBbRecord(mkey);
        if (!mem) continue;
        const role = (mem["role"] as string) || "";
        if (!role || !roleBuckets.has(role)) continue;

        const tags = (mem["tags"] as string[]) || [];
        // Derive verdict from tags — last non-role tag is the verdict
        let verdict: string | null = null;
        for (let i = tags.length - 1; i >= 0; i--) {
          if (tags[i] !== role) {
            verdict = tags[i]!;
            break;
          }
        }
        if (verdict === null) {
          // Fall back to lesson_type
          const lt = (mem["lesson_type"] as string) || "";
          if (lt === "success") verdict = "pass";
          else if (lt === "failure") verdict = "fail";
        }
        if (verdict) {
          const vc = roleBuckets.get(role)!.verdict_counts;
          vc[verdict] = (vc[verdict] ?? 0) + 1;
        }
      }
    } catch {
      // same as Python's except Exception: pass
    }

    // 3. Compute derived fields
    const PASS_VERDICTS = new Set(["pass", "done"]);
    const rolesOut: RoleEntry[] = [];

    for (const bucket of roleBuckets.values()) {
      const totalRuns = bucket.total_runs;
      const totalTokens = bucket.total_input_tokens + bucket.total_output_tokens;
      const totalCost = parseFloat(bucket.total_cost_usd.toFixed(6));
      const verdictCounts = bucket.verdict_counts;
      const passes = Array.from(PASS_VERDICTS).reduce((s, v) => s + (verdictCounts[v] ?? 0), 0);
      const needsFix = verdictCounts["needs-fix"] ?? 0;
      const needsFixRate = totalRuns > 0 ? parseFloat((needsFix / totalRuns).toFixed(3)) : 0.0;
      const avgTokensPerRun = totalRuns > 0 ? Math.round(totalTokens / totalRuns) : 0;
      const avgCostPerPass = passes > 0 ? parseFloat((totalCost / passes).toFixed(6)) : null;

      rolesOut.push({
        role: bucket.role,
        total_runs: totalRuns,
        total_input_tokens: bucket.total_input_tokens,
        total_output_tokens: bucket.total_output_tokens,
        total_tokens: totalTokens,
        total_cost_usd: totalCost,
        avg_tokens_per_run: avgTokensPerRun,
        verdict_counts: verdictCounts,
        passes,
        needs_fix_rate: needsFixRate,
        avg_cost_per_pass_usd: avgCostPerPass,
      });
    }

    rolesOut.sort((a, b) => b.total_cost_usd - a.total_cost_usd);

    return {
      schema_version: 1,
      generated_at: generatedAt,
      window_days: days,
      roles: rolesOut,
    };
  }

  /**
   * Compute role efficiency data and atomically write to the JSON file.
   * Mirrors Python CostTracker.write_role_efficiency_json() exactly.
   */
  writeRoleEfficiencyJson(days = 7): RoleEfficiencyData {
    const data = this.getRoleEfficiency(days);
    const outPath = join(autonomousTeamDir(), "role-efficiency.json");
    const tmpPath = outPath + ".tmp";
    try {
      mkdirSync(join(autonomousTeamDir()), { recursive: true });
      writeFileSync(tmpPath, JSON.stringify(data, null, 2));
      renameSync(tmpPath, outPath);
    } catch {
      // best-effort, same as Python's except OSError: pass
    }
    return data;
  }

  /** Print a human-readable cost summary table to stdout. */
  printSummary(): void {
    const summary = this.getSessionCost();
    const total = summary.total_cost_usd;

    console.log("Cost Summary");
    console.log("=".repeat(60));
    console.log(
      `${"Model".padEnd(35)} ${"Input".padStart(10)} ${"Output".padStart(10)} ${"Cost (USD)".padStart(12)}`
    );
    console.log("-".repeat(60));

    for (const entry of summary.model_breakdown) {
      console.log(
        `${entry.model.padEnd(35)} ` +
          `${entry.input.toLocaleString().padStart(10)} ` +
          `${entry.output.toLocaleString().padStart(10)} ` +
          `$${entry.cost_usd.toFixed(4).padStart(11)}`
      );
    }
    console.log("-".repeat(60));
    console.log(`${"TOTAL".padEnd(35)} ${"".padStart(10)} ${"".padStart(10)} $${total.toFixed(4).padStart(11)}`);
    console.log();

    if (summary.by_discussion.length > 0) {
      console.log("By Discussion");
      console.log("-".repeat(40));
      for (const entry of summary.by_discussion) {
        console.log(
          `  Discussion #${entry.discussion}: ` +
            `$${entry.cost_usd.toFixed(4)} ` +
            `(${entry.agents.length} agent(s))`
        );
      }
    }

    // Team Lead section — always printed; shows error when unavailable
    console.log();
    console.log("  (Team Lead data unavailable: subscription_usage not ported to TS)");
  }
}

// ---------------------------------------------------------------------------
// Cost spike detection (mirrors Python detect_cost_spike())
// ---------------------------------------------------------------------------

export interface CostSpikeResult {
  spike: boolean;
  value: number;
  mu: number;
  sigma: number;
  threshold: number;
  sample_size: number;
  insufficient_data: boolean;
}

/**
 * Detect whether the latest per-iteration cost exceeds μ + 3σ of the 24h baseline.
 * Mirrors Python detect_cost_spike() exactly (same rules, same math).
 *
 * series: [oldest, ..., newest]; the last value is the current iteration cost.
 * When undefined, reads from DuckDB (not yet ported — returns insufficient_data).
 */
export function detectCostSpike(series?: number[]): CostSpikeResult {
  if (!series) {
    series = _loadIterationCostSeries();
  }

  if (series.length < 2) {
    return {
      spike: false,
      value: series.length > 0 ? series[series.length - 1]! : 0.0,
      mu: 0.0,
      sigma: 0.0,
      threshold: 0.0,
      sample_size: 0,
      insufficient_data: true,
    };
  }

  const current = series[series.length - 1]!;
  const baseline = series.slice(0, -1);

  if (baseline.length < 10) {
    return {
      spike: false,
      value: current,
      mu: 0.0,
      sigma: 0.0,
      threshold: 0.0,
      sample_size: baseline.length,
      insufficient_data: true,
    };
  }

  const n = baseline.length;
  const mu = baseline.reduce((s, x) => s + x, 0) / n;
  const variance = baseline.reduce((s, x) => s + (x - mu) ** 2, 0) / n;
  const sigma = Math.sqrt(variance);

  const threshold = mu + 3.0 * sigma;
  const spike = current > threshold;

  return {
    spike,
    value: parseFloat(current.toFixed(6)),
    mu: parseFloat(mu.toFixed(6)),
    sigma: parseFloat(sigma.toFixed(6)),
    threshold: parseFloat(threshold.toFixed(6)),
    sample_size: n,
    insufficient_data: false,
  };
}

/**
 * Load per-iteration cost values from DuckDB over the last 24h + current.
 * Mirrors Python _load_iteration_cost_series(). Returns empty array when DuckDB unavailable.
 * Note: DuckDB async not called here; CLI callers should pass series directly.
 */
function _loadIterationCostSeries(): number[] {
  // DuckDB querying is async in TS; for the CLI spike-detection use-case,
  // callers should pass the series directly. Returning [] mirrors the
  // Python fallback when duckdb is unavailable.
  return [];
}

// ---------------------------------------------------------------------------
// Formatting helpers (mirror Python _fmt_tokens, _print_by_role_table, etc.)
// ---------------------------------------------------------------------------

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
}

function printByRoleTable(
  roles: RoleEntry[],
  top: number | null,
  days: number,
  generatedAt: string,
): void {
  const display = top !== null ? roles.slice(0, top) : roles;

  const header =
    `${"Role".padEnd(20)} ${"Runs".padStart(6)} ${"Tokens".padStart(10)} ${"Cost($)".padStart(10)}` +
    ` ${"Avg$/pass".padStart(10)} ${"NeedsFix%".padStart(10)}`;
  console.log(header);
  console.log("-".repeat(header.length));

  for (const entry of display) {
    const role = entry.role;
    const runs = entry.total_runs;
    const tokens = entry.total_tokens;
    const cost = entry.total_cost_usd;
    const avgPass = entry.avg_cost_per_pass_usd;
    const nfr = entry.needs_fix_rate * 100;

    const tokensStr = fmtTokens(tokens);
    const avgPassStr = avgPass !== null ? avgPass.toFixed(4) : "  n/a  ";
    console.log(
      `  ${role.padEnd(18)} ${runs.toLocaleString().padStart(6)} ${tokensStr.padStart(10)}` +
        ` $${cost.toFixed(4).padStart(9)} ${avgPassStr.padStart(10)} ${(nfr.toFixed(1) + "%").padStart(10)}`
    );
  }

  console.log("-".repeat(header.length));
  console.log(`Window: ${days} days | Generated: ${generatedAt}`);
  console.log();
}

function printByDiscussionTable(entries: DiscussionEntry[]): void {
  console.log(
    `${"Discussion".padEnd(12)} ${"Cost (USD)".padStart(12)} ${"Agents".padStart(8)} ${"Input Tok".padStart(12)} ${"Output Tok".padStart(12)}`
  );
  console.log("-".repeat(60));
  for (const entry of entries) {
    const disc = entry.discussion;
    const cost = entry.total_cost_usd;
    const agentCount = entry.agent_count;
    const inp = entry.total_input_tokens;
    const out = entry.total_output_tokens;
    console.log(
      `  #${String(disc).padEnd(10)} $${cost.toFixed(4).padStart(11)} ${agentCount.toLocaleString().padStart(8)} ${inp.toLocaleString().padStart(12)} ${out.toLocaleString().padStart(12)}`
    );
    const ab = entry.agent_breakdown;
    const pb = entry.pr_breakdown;
    if (Object.keys(ab).length > 0) {
      const topRoles = Object.entries(ab)
        .sort(([, a], [, b]) => b - a)
        .slice(0, 3)
        .map(([r, v]) => `${r} $${v.toFixed(4)}`);
      console.log(`    top roles: ${topRoles.join(", ")}`);
    }
    if (Object.keys(pb).length > 0) {
      const topPrs = Object.entries(pb)
        .sort(([, a], [, b]) => b - a)
        .slice(0, 3)
        .map(([p, v]) => `PR#${p} $${v.toFixed(4)}`);
      console.log(`    top PRs:   ${topPrs.join(", ")}`);
    }
  }
  console.log();
}

// ---------------------------------------------------------------------------
// CLI entry point
// ---------------------------------------------------------------------------

export async function main(argv?: string[]): Promise<number> {
  const rawArgs = argv ?? process.argv.slice(2);

  if (rawArgs.length === 0 || rawArgs[0] === "summary") {
    const ct = new CostTracker();
    ct.printSummary();
    return 0;
  }

  if (rawArgs[0] === "by-discussion") {
    // Parse args
    let top: number | null = null;
    let discussion: number | null = null;
    let textOutput = false;
    let i = 1;
    while (i < rawArgs.length) {
      if (rawArgs[i] === "--top" && i + 1 < rawArgs.length) {
        top = parseInt(rawArgs[i + 1]!, 10);
        i += 2;
      } else if (rawArgs[i] === "--discussion" && i + 1 < rawArgs.length) {
        discussion = parseInt(rawArgs[i + 1]!, 10);
        i += 2;
      } else if (rawArgs[i] === "--text") {
        textOutput = true;
        i++;
      } else if (rawArgs[i] === "--json") {
        // accepted, default behavior
        i++;
      } else {
        i++;
      }
    }

    if (top !== null && top <= 0) {
      process.stderr.write("ERROR: --top must be a positive integer\n");
      return 1;
    }

    const ct = new CostTracker();
    const full = ct.getSessionCost();
    let entries = full.by_discussion
      .slice()
      .sort((a, b) => b.total_cost_usd - a.total_cost_usd);

    if (discussion !== null) {
      const matched = entries.find((e) => e.discussion === discussion) ?? null;
      if (textOutput) {
        if (matched === null) {
          console.log("(no record found)");
        } else {
          printByDiscussionTable([matched]);
        }
      } else {
        console.log(JSON.stringify(matched, null, 2));
      }
      return 0;
    }

    if (top !== null) {
      entries = entries.slice(0, top);
    }

    if (textOutput) {
      printByDiscussionTable(entries);
    } else {
      console.log(JSON.stringify(entries, null, 2));
    }
    return 0;
  }

  if (rawArgs[0] === "per-discussion") {
    // Alias for by-discussion --discussion N
    const translated: string[] = ["by-discussion"];
    let i = 1;
    while (i < rawArgs.length) {
      if (rawArgs[i] === "--discussion" && i + 1 < rawArgs.length) {
        translated.push("--discussion", rawArgs[i + 1]!);
        i += 2;
      } else if (rawArgs[i] === "--text") {
        translated.push("--text");
        i++;
      } else {
        i++;
      }
    }
    return main(translated);
  }

  if (rawArgs[0] === "top") {
    // Alias for by-discussion --top N
    const translated: string[] = ["by-discussion"];
    let limitVal = "10";
    let i = 1;
    while (i < rawArgs.length) {
      if (rawArgs[i] === "--limit" && i + 1 < rawArgs.length) {
        limitVal = rawArgs[i + 1]!;
        i += 2;
      } else if (rawArgs[i] === "--text") {
        translated.push("--text");
        i++;
      } else {
        i++;
      }
    }
    translated.push("--top", limitVal);
    return main(translated);
  }

  if (rawArgs[0] === "by-role") {
    let days = 7;
    let jsonOutput = false;
    let top: number | null = null;
    let i = 1;
    while (i < rawArgs.length) {
      if (rawArgs[i] === "--days" && i + 1 < rawArgs.length) {
        days = parseInt(rawArgs[i + 1]!, 10);
        i += 2;
      } else if (rawArgs[i] === "--json") {
        jsonOutput = true;
        i++;
      } else if (rawArgs[i] === "--top" && i + 1 < rawArgs.length) {
        top = parseInt(rawArgs[i + 1]!, 10);
        i += 2;
      } else {
        i++;
      }
    }

    if (days <= 0) {
      process.stderr.write("ERROR: --days must be a positive integer\n");
      return 1;
    }
    if (top !== null && top <= 0) {
      process.stderr.write("ERROR: --top must be a positive integer\n");
      return 1;
    }

    const ct = new CostTracker();
    const data = ct.writeRoleEfficiencyJson(days);
    const allRoles = data.roles;

    if (jsonOutput) {
      const outRoles = top !== null ? allRoles.slice(0, top) : allRoles;
      console.log(JSON.stringify({ ...data, roles: outRoles }, null, 2));
    } else {
      printByRoleTable(allRoles, top, days, data.generated_at);
    }
    return 0;
  }

  process.stderr.write(`Unknown subcommand: ${JSON.stringify(rawArgs[0])}\n`);
  process.stderr.write(
    "Usage: bun run src/spawn/cost-tracker.ts [summary|by-discussion|per-discussion|top|by-role]\n"
  );
  return 1;
}

// Run as CLI when invoked directly
if (import.meta.main) {
  main().then((code) => process.exit(code));
}
