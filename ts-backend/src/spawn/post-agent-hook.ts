/**
 * spawn/post-agent-hook.ts — Mirrors scripts/post-agent-hook.sh logic.
 *
 * Runs after every agent completion to enforce coordination discipline:
 * marks the agent_run row complete (tokens/cost), records cost, updates
 * team-log/metrics, and invokes external side-effect steps (gh, git, wiki).
 *
 * # Steps (mirrors bash step order)
 *   agent_feed → budget → circuit_breaker → kpi → audit → role_verdict_metric
 *   → complete_run → pr_artifacts → memory → training_mine → cost_summary
 *   → post_agent_cleanup → worktree_registry → self_observe_check
 *   → scope_drift_check → anomaly_check → reap_worktrees → team_log
 *
 * # CLI usage (matches bash CLI exactly)
 *   bun run src/spawn/post-agent-hook.ts \
 *     --role executor \
 *     --verdict done \
 *     --discussion 1506 \
 *     --pr 42 \
 *     --input-tokens 62000 \
 *     --output-tokens 8400 \
 *     [--cache-read-tokens 0] \
 *     [--cache-write-tokens 0] \
 *     [--cache-creation-tokens 0] \
 *     [--first-write-turn 3] \
 *     [--model claude-sonnet-4-6] \
 *     [--files "src/a.ts,src/b.ts"] \
 *     [--content "lesson text"] \
 *     [--event-id evt-id-123] \
 *     [--self-observed true] \
 *     [--blocked-reason "reason"] \
 *     [--resume]
 *
 * # Side effects NOT covered by parity tests (require external systems):
 *   - bash scripts/rotate-team-log.sh comment "..." (GitHub issue comment)
 *   - bash scripts/agent-feed-append.sh ...        (JSONL append)
 *   - bash scripts/record-agent-result.sh ...      (blackboard budget write)
 *   - python3 backend/circuit_breaker.py ...       (circuit breaker state)
 *   - python3 backend/quality_scorer.py ...        (quality score)
 *   - python3 backend/kpi_engine.py compute        (KPI refresh)
 *   - python3 backend/stats_writer.py emit-verdict (role verdict metric)
 *   - python3 scripts/training/incremental-miner.py (training miner)
 *   - python3 backend/control_plane.py get ...     (gate reads)
 *   - bash scripts/reap-worktrees.sh               (worktree reaper)
 *   - bash scripts/hooks/post-agent.d/*.sh         (feature modules)
 *   - git reset / symbolic-ref                     (branch contamination recovery)
 *   - python3 -m backend.fleet.concurrency ...     (fleet slot release)
 *
 * These side effects are reproduced faithfully in the implementation but are
 * mocked or skipped in the parity test — the parity test focuses on
 * completeRun() (agent_run DB row) and exit behaviour.
 */

import { join } from "node:path";
import { existsSync } from "node:fs";
import { completeRun } from "./agent-run-tracker.js";
import { resolveRepo } from "../config/repo.js";

// ---------------------------------------------------------------------------
// Path resolution (matches bash REPO_ROOT / SCRIPT_DIR logic)
// ---------------------------------------------------------------------------

function repoRoot(): string {
  if (process.env["AF_REPO_ROOT"]) return process.env["AF_REPO_ROOT"]!;
  // This file: ts-backend/src/spawn/post-agent-hook.ts
  // → ts-backend/src/spawn/ → ts-backend/src/ → ts-backend/ → repo root
  const thisFile = new URL(import.meta.url).pathname;
  return join(thisFile, "..", "..", "..", "..");
}

function scriptsDir(): string {
  return join(repoRoot(), "scripts");
}

// ---------------------------------------------------------------------------
// CLI args parser (mirrors bash getopts-style while loop)
// ---------------------------------------------------------------------------

export interface PostAgentHookArgs {
  role: string;
  verdict: string;
  discussion?: string | null;
  pr?: string | null;
  model: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  cacheCreationTokens: number;
  firstWriteTurn?: number | null;
  files?: string | null;
  content?: string | null;
  eventId?: string | null;
  selfObserved: boolean;
  blockedReason?: string | null;
  resume: boolean;
}

export function parseArgs(argv: string[]): PostAgentHookArgs {
  let role = "";
  let verdict = "";
  let discussion: string | null = null;
  let pr: string | null = null;
  let model = "claude-sonnet-4-20250514";
  let inputTokens = 0;
  let outputTokens = 0;
  let cacheReadTokens = 0;
  let cacheWriteTokens = 0;
  let cacheCreationTokens = 0;
  let firstWriteTurn: number | null = null;
  let files: string | null = null;
  let content: string | null = null;
  let eventId: string | null = null;
  let selfObserved = false;
  let blockedReason: string | null = null;
  let resume = false;

  let i = 0;
  while (i < argv.length) {
    const arg = argv[i]!;
    switch (arg) {
      case "--role":              role = argv[++i]!; break;
      case "--discussion":        discussion = argv[++i] ?? null; break;
      case "--verdict":           verdict = argv[++i]!; break;
      case "--input-tokens":      inputTokens = parseInt(argv[++i] ?? "0", 10) || 0; break;
      case "--output-tokens":     outputTokens = parseInt(argv[++i] ?? "0", 10) || 0; break;
      case "--cache-read-tokens": cacheReadTokens = parseInt(argv[++i] ?? "0", 10) || 0; break;
      case "--cache-write-tokens": cacheWriteTokens = parseInt(argv[++i] ?? "0", 10) || 0; break;
      case "--cache-creation-tokens": cacheCreationTokens = parseInt(argv[++i] ?? "0", 10) || 0; break;
      case "--first-write-turn":  firstWriteTurn = parseInt(argv[++i] ?? "0", 10) || null; break;
      case "--pr":                pr = argv[++i] ?? null; break;
      case "--model":             model = argv[++i]!; break;
      case "--files":             files = argv[++i] ?? null; break;
      case "--content":           content = argv[++i] ?? null; break;
      case "--event-id":          eventId = argv[++i] ?? null; break;
      case "--self-observed":     selfObserved = (argv[++i] ?? "false") === "true"; break;
      case "--blocked-reason":    blockedReason = argv[++i] ?? null; break;
      case "--resume":            resume = true; break;
      default:
        process.stderr.write(`Unknown argument: ${arg}\n`);
        process.stderr.write(
          "Usage: post-agent-hook.ts --role <role> --verdict <verdict> " +
          "--input-tokens <N> --output-tokens <N>\n"
        );
        process.exit(1);
    }
    i++;
  }

  if (!role || !verdict) {
    process.stderr.write("Error: --role and --verdict are required\n");
    process.exit(1);
  }

  return {
    role,
    verdict,
    discussion,
    pr,
    model,
    inputTokens,
    outputTokens,
    cacheReadTokens,
    cacheWriteTokens,
    cacheCreationTokens,
    firstWriteTurn,
    files,
    content,
    eventId,
    selfObserved,
    blockedReason,
    resume,
  };
}

// ---------------------------------------------------------------------------
// Shell command runner — non-fatal, stderr logged
// ---------------------------------------------------------------------------

async function runShell(
  cmd: string[],
  label: string,
  env?: Record<string, string>
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  try {
    const proc = Bun.spawn(cmd, {
      env: { ...process.env, ...(env ?? {}) },
      stdout: "pipe",
      stderr: "pipe",
      cwd: repoRoot(),
    });
    const timeout = setTimeout(() => proc.kill(), 30_000);
    await proc.exited;
    clearTimeout(timeout);
    const stdout = await new Response(proc.stdout).text();
    const stderr = await new Response(proc.stderr).text();
    return { exitCode: proc.exitCode ?? 0, stdout, stderr };
  } catch (e) {
    process.stderr.write(`[post-agent-hook] ${label} failed: ${String(e)}\n`);
    return { exitCode: 1, stdout: "", stderr: String(e) };
  }
}

// ---------------------------------------------------------------------------
// Idempotency step tracker — mirrors hook_event_{has,mark}_step in bash
// ---------------------------------------------------------------------------

export class StepTracker {
  private done = new Set<string>();

  has(step: string): boolean {
    return this.done.has(step);
  }

  mark(step: string): void {
    this.done.add(step);
  }

  completed(): string[] {
    return Array.from(this.done);
  }
}

// ---------------------------------------------------------------------------
// Individual step implementations
// ---------------------------------------------------------------------------

/**
 * Step 0: agent_feed — append JSONL event record.
 * Mirrors bash step "agent_feed" (calls agent-feed-append.sh).
 * Non-fatal; external side effect: disk write.
 */
async function stepAgentFeed(args: PostAgentHookArgs): Promise<void> {
  const legacyDisabled = process.env["AUTONOMOUS_DISABLE_LEGACY_BLACKBOARD"] === "1";
  if (legacyDisabled) return;

  let feedMsg = `${args.role}: ${args.verdict}`;
  if (args.discussion) feedMsg += ` D#${args.discussion}`;
  if (args.pr) feedMsg += ` PR#${args.pr}`;
  feedMsg = feedMsg.slice(0, 280);

  const feedArgs = [
    "bash", join(scriptsDir(), "agent-feed-append.sh"),
    "--role", args.role,
    "--event-type", "agent_end",
    "--message", feedMsg,
    "--verdict", args.verdict,
    "--input-tokens", String(args.inputTokens),
    "--output-tokens", String(args.outputTokens),
    "--model", args.model,
  ];
  if (args.discussion) feedArgs.push("--discussion", args.discussion);
  if (args.pr) feedArgs.push("--pr", args.pr);
  if (args.files) feedArgs.push("--files", args.files);
  if (args.cacheReadTokens > 0) feedArgs.push("--cache-read-tokens", String(args.cacheReadTokens));
  if (args.cacheWriteTokens > 0) feedArgs.push("--cache-write-tokens", String(args.cacheWriteTokens));

  const { exitCode } = await runShell(feedArgs, "agent-feed-append");
  if (exitCode !== 0) {
    process.stderr.write("[post-agent-hook] Warning: agent-feed-append.sh failed (non-fatal)\n");
  }
}

/**
 * Step 0b: team_substrate — write task record completion.
 * Mirrors bash step "team_substrate" (calls backend.agent_teams_substrate.write_task via python3).
 * Non-fatal.
 */
async function stepTeamSubstrate(args: PostAgentHookArgs): Promise<void> {
  const taskEventId = args.eventId ?? "";
  const root = repoRoot();
  const disc = args.discussion ?? "";
  const pr = args.pr ?? "";
  const verdict = args.verdict;

  // Values are passed as sys.argv to avoid any quote-breakout injection from
  // untrusted agent-authored AGENT_OUTPUT fields (CWE-94 fix).
  const pyCode =
    `import sys, os\n` +
    `sys.path.insert(0, sys.argv[1])\n` +
    `try:\n` +
    `    from backend.agent_teams_substrate import write_task\n` +
    `    disc = sys.argv[3] or None\n` +
    `    pr   = sys.argv[4] or None\n` +
    `    write_task(sys.argv[2], {'status': sys.argv[5], 'discussion': disc, 'pr': pr})\n` +
    `except Exception as e:\n` +
    `    print(f'[post-agent-hook] WARN: write_task failed: {e}', file=sys.stderr)\n`;

  await runShell(
    ["python3", "-c", pyCode, root, taskEventId, disc, pr, verdict],
    "team_substrate"
  );
}

/**
 * Step 1: budget — record agent result (blackboard spend).
 * Mirrors bash step "budget" (calls record-agent-result.sh).
 * Non-fatal.
 */
async function stepBudget(args: PostAgentHookArgs): Promise<void> {
  const recordArgs = [
    "bash", join(scriptsDir(), "record-agent-result.sh"),
    "--role", args.role,
    "--verdict", args.verdict,
    "--input-tokens", String(args.inputTokens),
    "--output-tokens", String(args.outputTokens),
    "--model", args.model,
  ];
  if (args.discussion) recordArgs.push("--discussion", args.discussion);
  if (args.files) recordArgs.push("--files", args.files);
  if (args.content) recordArgs.push("--content", args.content);
  if (args.pr) recordArgs.push("--pr", args.pr);
  if (args.cacheReadTokens > 0) recordArgs.push("--cache-read-tokens", String(args.cacheReadTokens));
  if (args.cacheWriteTokens > 0) recordArgs.push("--cache-write-tokens", String(args.cacheWriteTokens));

  process.stdout.write(`[post-agent-hook] Recording agent result: role=${args.role} verdict=${args.verdict}\n`);
  const { exitCode } = await runShell(recordArgs, "record-agent-result");
  if (exitCode !== 0) {
    process.stderr.write("[post-agent-hook] Warning: record-agent-result.sh failed (non-fatal)\n");
  }
}

/**
 * Step 2: circuit_breaker — record failure or reset on pass/done.
 * Mirrors bash step "circuit_breaker" (calls backend/circuit_breaker.py).
 * Non-fatal.
 */
async function stepCircuitBreaker(args: PostAgentHookArgs): Promise<void> {
  if (!args.discussion) return;

  const root = repoRoot();
  let cbArgs: string[];
  switch (args.verdict) {
    case "fail":
    case "needs-fix":
      process.stdout.write(
        `[post-agent-hook] Circuit breaker: recording failure for Discussion #${args.discussion}\n`
      );
      cbArgs = [
        "python3", join(root, "backend", "circuit_breaker.py"),
        "record", args.discussion, args.role, args.verdict,
      ];
      break;
    case "pass":
    case "done":
      process.stdout.write(
        `[post-agent-hook] Circuit breaker: resetting Discussion #${args.discussion}\n`
      );
      cbArgs = [
        "python3", join(root, "backend", "circuit_breaker.py"),
        "reset", args.discussion,
      ];
      break;
    default:
      process.stdout.write(
        `[post-agent-hook] Circuit breaker: no update for verdict=${args.verdict}\n`
      );
      return;
  }

  const { exitCode } = await runShell(cbArgs, "circuit_breaker");
  if (exitCode !== 0) {
    process.stderr.write("[post-agent-hook] Warning: circuit_breaker failed (non-fatal)\n");
  }
}

/**
 * Step 3: kpi — quality scorer + KPI snapshot refresh.
 * Mirrors bash step "kpi".
 * Non-fatal.
 */
async function stepKpi(args: PostAgentHookArgs): Promise<void> {
  const root = repoRoot();

  if (args.role === "code-reviewer" && args.verdict === "pass" && args.pr) {
    process.stdout.write(`[post-agent-hook] Running quality scorer for PR #${args.pr}\n`);
    const scorerArgs = [
      "python3", join(root, "backend", "quality_scorer.py"),
      "score", "--pr", args.pr,
    ];
    if (args.discussion) scorerArgs.push("--discussion", args.discussion);
    const { exitCode } = await runShell(scorerArgs, "quality_scorer");
    if (exitCode !== 0) {
      process.stderr.write("[post-agent-hook] Warning: quality_scorer failed (non-fatal)\n");
    }
  }

  process.stdout.write("[post-agent-hook] Refreshing KPI snapshot\n");
  const { exitCode } = await runShell(
    ["python3", join(root, "backend", "kpi_engine.py"), "compute"],
    "kpi_engine"
  );
  if (exitCode !== 0) {
    process.stderr.write("[post-agent-hook] Warning: kpi_engine compute failed (non-fatal)\n");
  }
}

/**
 * Step 4: audit — cost tracker note.
 * Mirrors bash step "audit" (no separate record call needed; spend already recorded via budget).
 */
function stepAudit(args: PostAgentHookArgs): void {
  const agentId = `${args.role}-${args.discussion ?? "nodisc"}-${Math.floor(Date.now() / 1000)}`;
  process.stdout.write(
    `[post-agent-hook] Cost tracker: spend already recorded via record-agent-result.sh (id=${agentId})\n`
  );
}

/**
 * Step 4b: role_verdict_metric — emit raw role_verdict event for success rate aggregation.
 * Mirrors bash step "role_verdict_metric" (calls backend/stats_writer.py emit-verdict).
 * Non-fatal. Skips on unknown/empty role or verdict.
 */
async function stepRoleVerdictMetric(args: PostAgentHookArgs): Promise<void> {
  if (!args.role || args.role === "unknown" || !args.verdict || args.verdict === "unknown") {
    process.stderr.write(
      "[post-agent-hook] role_verdict_metric: skipped — role or verdict is unknown/empty\n"
    );
    return;
  }

  await runShell(
    [
      "python3", join(repoRoot(), "backend", "stats_writer.py"),
      "emit-verdict", "--role", args.role, "--verdict", args.verdict,
    ],
    "stats_writer emit-verdict"
  );
  // Non-fatal; failures silently ignored (|| true in bash)
}

/**
 * Step 4c: complete_run — update agent_run row in DuckDB.
 * This is the PRIMARY parity-tested step. Uses completeRun() from agent-run-tracker.ts.
 * Mirrors bash step "complete_run" (calls backend/agent_run_tracker.py complete).
 * Non-fatal.
 */
export async function stepCompleteRun(args: PostAgentHookArgs): Promise<void> {
  // The raw spawn event_id (TASK_EVENT_ID in bash) must match what spawn-agent used in start_run.
  // Use eventId as agent_id key; fall back to constructing one if absent.
  const agentId = args.eventId ?? `${args.role}-${args.discussion ?? "nod"}-${Math.floor(Date.now() / 1000)}`;

  await completeRun({
    agentId,
    verdict: args.verdict || null,
    model: args.model || null,
    inputTok: args.inputTokens,
    outputTok: args.outputTokens,
    cacheRead: args.cacheReadTokens > 0 ? args.cacheReadTokens : null,
    cacheWrite: args.cacheWriteTokens > 0 ? args.cacheWriteTokens : null,
    cacheCreationTokens: args.cacheCreationTokens > 0 ? args.cacheCreationTokens : null,
    blockedReason: args.blockedReason || null,
    firstWriteTurn: args.firstWriteTurn || null,
  });
}

/**
 * Step 4c2: verdict_overturn — source verdict-overturn.sh feature module if present.
 * Mirrors bash step "verdict_overturn".
 * Non-fatal.
 */
async function stepVerdictOverturn(args: PostAgentHookArgs): Promise<void> {
  const hookPath = join(scriptsDir(), "hooks", "post-agent.d", "verdict-overturn.sh");
  if (!existsSync(hookPath)) return;

  await runShell(
    ["bash", "-c", `source ${hookPath} 2>/dev/null || true`],
    "verdict-overturn",
    {
      _REPO: resolveRepo(),
      REPO_ROOT: repoRoot(),
      PR: args.pr ?? "",
      ROLE: args.role,
      VERDICT: args.verdict,
    }
  );
}

/**
 * Step 4d: pr_artifacts — source pr-artifacts.sh feature module if present.
 * Mirrors bash step "pr_artifacts".
 * Non-fatal.
 */
async function stepPrArtifacts(args: PostAgentHookArgs): Promise<void> {
  const hookPath = join(scriptsDir(), "hooks", "post-agent.d", "pr-artifacts.sh");
  if (!existsSync(hookPath)) return;

  await runShell(
    ["bash", "-c", `source ${hookPath} 2>/dev/null || true`],
    "pr-artifacts",
    {
      _REPO: resolveRepo(),
      REPO_ROOT: repoRoot(),
      PR: args.pr ?? "",
      ROLE: args.role,
      CONTENT: args.content ?? "",
    }
  );
}

/**
 * Step 5: memory — no-op marker (recorded via record-agent-result.sh in budget step).
 * Mirrors bash step "memory".
 */
function stepMemory(): void {
  process.stdout.write("[post-agent-hook] Memory: recorded via record-agent-result.sh\n");
}

/**
 * Step 6: training_mine — incremental training miner + optional threshold trigger.
 * Mirrors bash step "training_mine".
 * Non-fatal.
 */
async function stepTrainingMine(_args: PostAgentHookArgs): Promise<void> {
  const root = repoRoot();

  process.stdout.write("[post-agent-hook] Mining new training examples\n");
  const { exitCode: mineExit } = await runShell(
    ["python3", join(root, "scripts", "training", "incremental-miner.py")],
    "incremental-miner"
  );
  if (mineExit !== 0) {
    process.stderr.write("[post-agent-hook] Warning: incremental-miner failed (non-fatal)\n");
  }

  // Training threshold trigger — gated behind gates.training_triggers
  const { stdout: gateOut } = await runShell(
    ["python3", join(root, "backend", "control_plane.py"), "get", "gates.training_triggers"],
    "control_plane gates.training_triggers"
  );
  const trainGate = gateOut.trim().replace(/"/g, "");

  if (trainGate === "true") {
    const { exitCode: trigExit } = await runShell(
      ["python3", join(root, "scripts", "training", "training-trigger.py"), "--check"],
      "training-trigger"
    );
    if (trigExit !== 0) {
      // Threshold reached — notify team log
      const hhmm = new Date().toISOString().slice(11, 16);
      await runShell(
        [
          "bash", join(scriptsDir(), "rotate-team-log.sh"), "comment",
          `[${hhmm}] training-trigger: threshold reached — ` +
          "run `bash scripts/training/vast-bringup.sh --confirm-cost` to start a training pass",
        ],
        "rotate-team-log training-trigger"
      );
    }
  }

  // Auto-fire training on existing box (opt-in env var)
  if (
    process.env["AUTO_TRAIN_EXISTING"] === "1" &&
    existsSync(join(root, ".autonomous-team", "vast-training.json"))
  ) {
    process.stdout.write("[post-agent-hook] Checking training threshold (existing-box mode)\n");
    await runShell(
      ["python3", join(root, "scripts", "training", "training-trigger.py"), "--mode", "existing-box", "--quiet"],
      "training-trigger existing-box"
    );
  }
}

/**
 * Step 6a-b: cost_summary — source cost-summary.sh feature module if present.
 * Mirrors bash step "cost_summary".
 * Non-fatal.
 */
async function stepCostSummary(): Promise<void> {
  const hookPath = join(scriptsDir(), "hooks", "post-agent.d", "cost-summary.sh");
  if (!existsSync(hookPath)) return;

  await runShell(
    ["bash", "-c", `source ${hookPath} 2>/dev/null || true`],
    "cost-summary"
  );
}

/**
 * Step 6b0: post_agent_cleanup — prune per-worktree build artifacts.
 * Mirrors bash step "post_agent_cleanup".
 * Non-fatal.
 */
async function stepPostAgentCleanup(): Promise<void> {
  const worktreeId = process.env["WORKTREE_ID"] ?? "";
  if (!worktreeId) return;

  const root = repoRoot();
  const worktreePath = join(root, ".claude", "worktrees", worktreeId);

  // Read post_agent_cleanup commands from project.json
  const projectJsonPath = join(root, ".autonomous-team", "project.json");
  if (!existsSync(projectJsonPath)) return;

  let cmds: string[] = [];
  try {
    const raw = await Bun.file(projectJsonPath).json() as Record<string, unknown>;
    const rawCmds = raw["post_agent_cleanup"];
    if (Array.isArray(rawCmds)) {
      cmds = rawCmds.filter((c): c is string => typeof c === "string");
    }
  } catch {
    return;
  }

  if (cmds.length === 0) return;

  process.stdout.write(
    `[post-agent-hook] Running post_agent_cleanup for worktree: ${worktreePath}\n`
  );

  for (const cmd of cmds) {
    const expanded = cmd.replace(/\$WORKTREE/g, worktreePath);
    process.stdout.write(
      `[post-agent-hook] post_agent_cleanup: ${worktreeId} — ${cmd}\n`
    );
    const { exitCode } = await runShell(["bash", "-c", expanded], "post_agent_cleanup_cmd");
    if (exitCode !== 0) {
      process.stderr.write(`[post_agent_cleanup] WARN: command failed: ${expanded}\n`);
    }
  }
}

/**
 * Step 6b: worktree_registry — update worktree status on graceful exit.
 * Mirrors bash step "worktree_registry" (calls scripts/lib/worktree-registry.sh).
 * Non-fatal.
 */
async function stepWorktreeRegistry(args: PostAgentHookArgs): Promise<void> {
  const worktreeId = process.env["WORKTREE_ID"] ?? "";
  if (!worktreeId) return;

  const registryLib = join(scriptsDir(), "lib", "worktree-registry.sh");
  if (!existsSync(registryLib)) return;

  let action: "discarded" | null = null;
  switch (args.verdict) {
    case "done":
      // Executor with no PR: discard. With PR: leave as-is (post-merge-hook will handle).
      if (!args.pr) action = "discarded";
      break;
    case "pass":
    case "fail":
    case "needs-fix":
    case "skip":
      action = "discarded";
      break;
  }

  if (action) {
    // worktreeId and action are passed via env vars to avoid shell-injection
    // from WORKTREE_ID (process.env) being interpolated into a bash -c string (CWE-78 fix).
    await runShell(
      [
        "bash", "-c",
        `source "$_REGISTRY_LIB" 2>/dev/null || true; ` +
        `worktree_registry mark-status "$_WORKTREE_ID" "$_REGISTRY_ACTION" 2>/dev/null || true`,
      ],
      "worktree_registry",
      {
        _REGISTRY_LIB: registryLib,
        _WORKTREE_ID: worktreeId,
        _REGISTRY_ACTION: action,
      }
    );
  }
}

/**
 * Fleet concurrency unregister — release the fleet slot.
 * Mirrors bash inline python3 -m backend.fleet.concurrency unregister.
 * Non-fatal.
 */
async function stepFleetUnregister(args: PostAgentHookArgs): Promise<void> {
  const root = repoRoot();

  // Determine project name from config.json
  let projectName = "fulcrumaxe";
  try {
    const configPath = join(root, ".autonomous-team", "config.json");
    if (existsSync(configPath)) {
      const cfg = await Bun.file(configPath).json() as Record<string, unknown>;
      projectName = (cfg["project_name"] as string) || projectName;
    }
  } catch { /* non-fatal */ }

  const agentId =
    args.eventId ||
    process.env["WORKTREE_ID"] ||
    `spawn-${process.pid}`;

  if (!args.eventId && !process.env["WORKTREE_ID"]) {
    process.stderr.write(
      "[post-agent-hook] WARN: TASK_EVENT_ID and WORKTREE_ID both unset; fleet slot may not be released\n"
    );
  }

  await runShell(
    ["python3", "-m", "backend.fleet.concurrency", "unregister", projectName, agentId],
    "fleet.concurrency unregister"
  );
}

/**
 * Step 6c: self_observe_check — advisory/enforced gate for self-observe field.
 * Mirrors bash step "self_observe_check".
 * Non-fatal.
 */
async function stepSelfObserveCheck(args: PostAgentHookArgs): Promise<void> {
  const root = repoRoot();

  const { stdout } = await runShell(
    ["python3", join(root, "backend", "control_plane.py"), "get", "gates.self_observe_enforcement"],
    "control_plane self_observe"
  );
  const enforcement = stdout.trim().replace(/"/g, "") || "shadow";

  if (enforcement !== "advisory" && enforcement !== "enforced") return;
  if (args.verdict !== "done" && args.verdict !== "pass") return;

  if (!args.selfObserved) {
    const agentId = `${args.role}-${args.discussion ?? "nodisc"}-${args.pr ?? "nopr"}`;
    const hhmm = new Date().toISOString().slice(11, 16);
    const msg =
      `[${hhmm}] team-lead: WARN — agent=${agentId} role=${args.role} ` +
      `skipped self-observe gate (${enforcement} mode)`;

    process.stderr.write(
      `[post-agent-hook] self-observe gate: WARN agent=${agentId} role=${args.role} ` +
      `verdict=${args.verdict} mode=${enforcement}\n`
    );
    await runShell(
      ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", msg],
      "rotate-team-log self-observe-warn"
    );
  }
}

/**
 * Step 6d: scope_drift_check — source scope-drift-check.sh if present.
 * Mirrors bash step "scope_drift_check".
 * Non-fatal.
 */
async function stepScopeDriftCheck(args: PostAgentHookArgs): Promise<void> {
  const hookPath = join(scriptsDir(), "hooks", "post-agent.d", "scope-drift-check.sh");
  if (!existsSync(hookPath)) return;

  await runShell(
    ["bash", "-c", `source ${hookPath} 2>/dev/null || true`],
    "scope-drift-check",
    { PR: args.pr ?? "", ROLE: args.role, VERDICT: args.verdict }
  );
}

/**
 * Step 6e: anomaly_check — source anomaly-check.sh if present.
 * Mirrors bash step "anomaly_check".
 * Non-fatal.
 */
async function stepAnomalyCheck(): Promise<void> {
  const hookPath = join(scriptsDir(), "hooks", "post-agent.d", "anomaly-check.sh");
  if (!existsSync(hookPath)) return;

  await runShell(
    ["bash", "-c", `source ${hookPath} 2>/dev/null || true`],
    "anomaly-check"
  );
}

/**
 * Step 6f: reap_worktrees — run reap-worktrees.sh.
 * Mirrors bash step "reap_worktrees".
 * Non-fatal.
 */
async function stepReapWorktrees(): Promise<void> {
  await runShell(
    ["bash", join(scriptsDir(), "reap-worktrees.sh"), "--quiet"],
    "reap-worktrees"
  );
}

/**
 * Step 7: team_log — terse one-liner comment.
 * Mirrors bash step "team_log" (calls rotate-team-log.sh comment).
 * Non-fatal.
 */
async function stepTeamLog(args: PostAgentHookArgs): Promise<void> {
  const hhmm = new Date().toISOString().slice(11, 16);
  let msg = `[${hhmm}] ${args.role}: ${args.verdict}`;
  if (args.discussion) msg += ` D#${args.discussion}`;
  if (args.pr) msg += ` PR#${args.pr}`;

  const { exitCode } = await runShell(
    ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", msg],
    "rotate-team-log"
  );
  if (exitCode !== 0) {
    process.stderr.write("[post-agent-hook] Warning: team-log comment failed (non-fatal)\n");
  }
}

/**
 * Step 7b: parent branch contamination recovery.
 * Mirrors bash inline check after team_log step.
 * Non-fatal; only runs when not inside a linked worktree.
 */
async function stepBranchContaminationRecovery(args: PostAgentHookArgs): Promise<void> {
  const worktreeId = process.env["WORKTREE_ID"] ?? "";
  if (worktreeId) return; // running from inside a linked worktree — intentional

  const root = repoRoot();

  // Check git-dir vs git-common-dir (canonical linked-worktree test)
  const gitDir = await runShell(
    ["git", "-C", root, "rev-parse", "--git-dir"],
    "git rev-parse --git-dir"
  );
  const gitCommonDir = await runShell(
    ["git", "-C", root, "rev-parse", "--git-common-dir"],
    "git rev-parse --git-common-dir"
  );

  if (
    gitDir.stdout.trim() &&
    gitCommonDir.stdout.trim() &&
    gitDir.stdout.trim() !== gitCommonDir.stdout.trim()
  ) {
    return; // inside a linked worktree
  }

  const branchResult = await runShell(
    ["git", "-C", root, "branch", "--show-current"],
    "git branch --show-current"
  );
  const branch = branchResult.stdout.trim();

  if (branch && branch !== "main") {
    process.stderr.write(
      `[post-agent-hook] Parent on '${branch}', auto-resetting to main (contamination recovery)\n`
    );
    const hhmm = new Date().toISOString().slice(11, 16);
    await runShell(
      [
        "bash", join(scriptsDir(), "rotate-team-log.sh"), "comment",
        `[${hhmm}] post-agent-hook: auto-recovered parent from contaminated branch '${branch}' → main ` +
        `(after ${args.role} ${args.verdict})`,
      ],
      "rotate-team-log contamination"
    );
    await runShell(
      ["git", "-C", root, "symbolic-ref", "HEAD", "refs/heads/main"],
      "git symbolic-ref"
    );
    await runShell(
      ["git", "-C", root, "fetch", "origin", "main", "--quiet"],
      "git fetch"
    );
    await runShell(
      ["git", "-C", root, "reset", "--hard", "origin/main"],
      "git reset --hard"
    );
  }
}

// ---------------------------------------------------------------------------
// Main orchestrator — runs all steps in order, respecting idempotency
// ---------------------------------------------------------------------------

/**
 * Run the full post-agent hook sequence.
 *
 * steps is an optional StepTracker for injection (used by parity tests to
 * skip external-system steps while still running stepCompleteRun).
 */
export async function runPostAgentHook(
  args: PostAgentHookArgs,
  steps?: StepTracker
): Promise<void> {
  const tracker = steps ?? new StepTracker();

  process.stdout.write(
    `[post-agent-hook] event_id=${args.eventId ?? "(none)"} role=${args.role} verdict=${args.verdict}\n`
  );

  if (!tracker.has("agent_feed")) {
    await stepAgentFeed(args);
    tracker.mark("agent_feed");
  }

  if (!tracker.has("team_substrate")) {
    await stepTeamSubstrate(args);
    tracker.mark("team_substrate");
  }

  if (!tracker.has("budget")) {
    await stepBudget(args);
    tracker.mark("budget");
  }

  if (!tracker.has("circuit_breaker")) {
    await stepCircuitBreaker(args);
    tracker.mark("circuit_breaker");
  }

  if (!tracker.has("kpi")) {
    await stepKpi(args);
    tracker.mark("kpi");
  }

  if (!tracker.has("audit")) {
    stepAudit(args);
    tracker.mark("audit");
  }

  if (!tracker.has("role_verdict_metric")) {
    await stepRoleVerdictMetric(args);
    tracker.mark("role_verdict_metric");
  }

  if (!tracker.has("complete_run")) {
    await stepCompleteRun(args);
    tracker.mark("complete_run");
  }

  if (!tracker.has("verdict_overturn")) {
    await stepVerdictOverturn(args);
    tracker.mark("verdict_overturn");
  }

  if (!tracker.has("pr_artifacts")) {
    await stepPrArtifacts(args);
    tracker.mark("pr_artifacts");
  }

  if (!tracker.has("memory")) {
    stepMemory();
    tracker.mark("memory");
  }

  if (!tracker.has("training_mine")) {
    await stepTrainingMine(args);
    tracker.mark("training_mine");
  }

  if (!tracker.has("cost_summary")) {
    await stepCostSummary();
    tracker.mark("cost_summary");
  }

  if (!tracker.has("post_agent_cleanup")) {
    await stepPostAgentCleanup();
    tracker.mark("post_agent_cleanup");
  }

  if (!tracker.has("worktree_registry")) {
    await stepWorktreeRegistry(args);
    tracker.mark("worktree_registry");
  }

  // Fleet concurrency unregister (no dedicated step name in bash, inline after worktree_registry)
  await stepFleetUnregister(args);

  if (!tracker.has("self_observe_check")) {
    await stepSelfObserveCheck(args);
    tracker.mark("self_observe_check");
  }

  if (!tracker.has("scope_drift_check")) {
    await stepScopeDriftCheck(args);
    tracker.mark("scope_drift_check");
  }

  if (!tracker.has("anomaly_check")) {
    await stepAnomalyCheck();
    tracker.mark("anomaly_check");
  }

  if (!tracker.has("reap_worktrees")) {
    await stepReapWorktrees();
    tracker.mark("reap_worktrees");
  }

  if (!tracker.has("team_log")) {
    await stepTeamLog(args);
    tracker.mark("team_log");
  }

  await stepBranchContaminationRecovery(args);

  process.stdout.write("[post-agent-hook] Done.\n");
}

// ---------------------------------------------------------------------------
// CLI entry point
// ---------------------------------------------------------------------------

if (import.meta.main) {
  const args = parseArgs(process.argv.slice(2));
  await runPostAgentHook(args);
}
