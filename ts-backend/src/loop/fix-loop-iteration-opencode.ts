/**
 * loop/fix-loop-iteration-opencode.ts — Iterative fix-cycle via opencode/Qwen.
 *
 * Extends the proven 2-role pattern (executor → code-reviewer) with bounded
 * re-attempt logic when the reviewer returns needs-fix. The feedback from the
 * reviewer is injected into the next executor prompt alongside the original
 * task, so the re-attempt is informed by what went wrong.
 *
 * Fix cycle:
 *   attempt 1:  executor runs task  →  reviewer reviews diff
 *   if needs-fix and fixRound < maxFix:
 *     attempt 2:  executor re-runs with original task + reviewer feedback
 *               →  reviewer reviews new diff
 *   ... repeat until pass or maxFix exhausted.
 *
 * Every role-run (each executor attempt + each review) is recorded as an
 * agent_run row in DuckDB with routed_via=opencode.
 *
 * The gate (mergeGateAllowed) is applied to the FINAL reviewer verdict only.
 *
 * Reuses without modification:
 *   - runSpawnAgent()        from spawn/spawn-agent.ts
 *   - mergeGateAllowed()     from loop/loop-phased-step5.ts
 *   - DEFAULT_OPENCODE_MODEL from spawn/runtime/opencode-runtime.ts
 *
 * Usage:
 *   import { runFixLoopIteration } from "./fix-loop-iteration-opencode.js";
 *   const result = await runFixLoopIteration({ ... });
 *
 * Gate behind E2E_OPENCODE=1 — spends provider credit.
 */

import { join } from "node:path";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { runSpawnAgent } from "../spawn/spawn-agent.js";
import { mergeGateAllowed } from "./loop-phased-step5.js";
import type { MergeGateResult } from "./loop-phased-step5.js";
import { DEFAULT_OPENCODE_MODEL } from "../spawn/runtime/opencode-runtime.js";
import { resolveRepo } from "../config/repo.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface FixLoopIterationOpts {
  /**
   * Plain-English task for the executor role.
   * E.g. "create greet.ts exporting greet(name) returning `Hello, ${name}!`"
   */
  task: string;

  /**
   * Scratch directory where the executor operates.
   * Must be (or will be initialised as) a git repo.
   */
  scratchDir: string;

  /**
   * Path to the real repo root so Python backend modules are reachable.
   * Defaults to the resolved repo root relative to this file.
   */
  repoRoot?: string;

  /**
   * Path to the stats DuckDB file agent_run rows are written to.
   * Defaults to the ambient STATS_DB_PATH / state-dir default.
   */
  dbPath?: string;

  /**
   * Path to a minimal config.json satisfying pre-spawn-check cap checks.
   * If omitted, a minimal fixture is written into scratchDir.
   */
  configPath?: string;

  /**
   * Discussion number to label the spawn (used in event IDs + agent_run rows).
   * May be null for standalone / exploratory runs.
   */
  discussion?: number | null;

  /**
   * opencode model string. Defaults to DEFAULT_OPENCODE_MODEL.
   */
  model?: string;

  /**
   * Maximum number of fix rounds after the first review.
   * Default is 2. Set to 0 to disable the fix cycle (single pass only).
   *
   * Total executor invocations = 1 + min(needs-fix rounds, maxFix).
   * Total reviewer invocations = 1 + min(needs-fix rounds, maxFix).
   */
  maxFix?: number;
}

export interface RoleRunRecord {
  /** Logical role name: "executor" or "code-reviewer". */
  role: string;
  /** Agent event ID (unique per spawn). */
  eventId: string;
  /** Full ANSI-stripped stdout from opencode. */
  output: string;
  /** Parsed AGENT_OUTPUT envelope, or null if absent. */
  agentOutput: Record<string, unknown> | null;
  /** opencode process exit code. */
  exitCode: number;
  /** The routed_via value recorded in agent_run. Should be "opencode". */
  routedVia: string;
  /** Fix round index. 0 = initial attempt, 1+ = re-attempts after needs-fix. */
  fixRound: number;
}

export interface FixLoopIterationResult {
  /**
   * All executor role-run records in order (initial attempt first, then
   * fix-round re-attempts). Length is 1 to maxFix+1.
   */
  executorRuns: RoleRunRecord[];
  /**
   * All code-reviewer role-run records in order (one per executor attempt).
   * Length matches executorRuns.
   */
  reviewerRuns: RoleRunRecord[];
  /**
   * The merge-gate decision computed from the FINAL reviewer verdict.
   * allowed=true only if the last reviewer emitted verdict:pass.
   */
  gate: MergeGateResult;
  /**
   * Final reviewer verdict extracted from the last reviewerRun's agentOutput.
   * "pass", "needs-fix", or null if the reviewer did not emit a parseable envelope.
   */
  finalVerdict: string | null;
  /**
   * Whether the fix cycle was triggered (i.e. at least one re-attempt ran
   * after a needs-fix verdict).
   */
  fixCycleTriggered: boolean;
  /**
   * Number of fix rounds that ran. 0 if the first review passed.
   * Bounded by maxFix.
   */
  fixRoundsRan: number;
  /**
   * The diff text handed to the final reviewer.
   * Useful for debugging what the last executor produced.
   */
  finalDiff: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Resolve the repo root relative to this source file. */
function defaultRepoRoot(): string {
  // This file: ts-backend/src/loop/fix-loop-iteration-opencode.ts
  // Walk up: loop → src → ts-backend → repo_root
  const here = new URL(import.meta.url).pathname;
  return join(here, "..", "..", "..", "..", "..");
}

/** Ensure dir is a valid git repo with an initial commit. */
function ensureGitRepo(dir: string): void {
  mkdirSync(dir, { recursive: true });
  const gitDir = join(dir, ".git");
  if (!existsSync(gitDir)) {
    spawnSync("git", ["init", "-b", "main", dir], { stdio: "ignore" });
    spawnSync("git", ["-C", dir, "config", "user.email", "test@example.com"], { stdio: "ignore" });
    spawnSync("git", ["-C", dir, "config", "user.name", "Test Agent"], { stdio: "ignore" });
    const readme = join(dir, "README.md");
    writeFileSync(readme, "scratch workspace\n");
    spawnSync("git", ["-C", dir, "add", "README.md"], { stdio: "ignore" });
    spawnSync("git", ["-C", dir, "commit", "-m", "init", "--allow-empty"], { stdio: "ignore" });
  }
}

/** Minimal config.json — satisfies the pre-spawn-check concurrency cap check. */
const MINIMAL_CONFIG = JSON.stringify({
  repo: resolveRepo(),
  max_concurrent_agents: 8,
  dials: {
    "agent.spawn": { level: 5, ceiling: 5 },
  },
});

/**
 * Capture the executor's change as a real git diff.
 *
 * Uses `git add -A` to stage all new/modified/deleted files so that newly
 * created files appear in `git diff --cached`. Without staging, new files
 * are untracked and invisible to the diff, so the reviewer sees nothing.
 *
 * Caps at 6000 chars to keep reviewer prompts manageable.
 */
function captureExecutorDiff(scratchDir: string): string {
  spawnSync("git", ["-C", scratchDir, "add", "-A"], { timeout: 15_000 });

  const staged = spawnSync("git", ["-C", scratchDir, "diff", "--cached"], {
    encoding: "utf-8",
    timeout: 15_000,
  });
  const nameStatus = spawnSync(
    "git",
    ["-C", scratchDir, "diff", "--cached", "--name-status"],
    { encoding: "utf-8", timeout: 10_000 }
  );

  const diff = (staged.stdout ?? "").trim();
  const fileList = (nameStatus.stdout ?? "").trim();

  const parts: string[] = [];
  if (fileList) parts.push(`=== files changed ===\n${fileList}`);
  if (diff) parts.push(`=== diff (staged) ===\n${diff}`);

  const full = parts.join("\n\n");
  return full.length > 6000 ? full.slice(0, 6000) + "\n...[truncated]" : full;
}

/** Build a fake discussion body that passes the spec-readiness gate. */
function fakeDiscBody(task: string): string {
  return `<!-- STATUS:SPEC_READY SINCE:2026-06-01T00:00:00Z -->

## Intent
Fix-loop gated iteration e2e test.

## Spec (Acceptance)
- [ ] Executor completes: ${task}
- [ ] Code-reviewer emits a pass or needs-fix verdict.
- [ ] If needs-fix, executor re-attempts with reviewer feedback (bounded).
- [ ] Gate decision reflects final reviewer verdict.

## Implementation Notes
${task}`;
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

function executorPrompt(task: string, fixFeedback?: string): string {
  const fixSection = fixFeedback
    ? `\nPREVIOUS REVIEW FEEDBACK (fix this before completing the task):\n${fixFeedback}\n`
    : "";

  return `You are an executor agent running a minimal verification task.
${fixSection}
Your ONLY job:
${task}

Rules:
- Do the task in your current working directory.
- Keep changes minimal — new files only, no deletions.
- After completing the task, output your AGENT_OUTPUT envelope and stop.

End your response with exactly:

<!-- AGENT_OUTPUT -->
\`\`\`json
{"agent":"executor","verdict":"done","files_touched":[]}
\`\`\`
<!-- /AGENT_OUTPUT -->

Replace the files_touched array with the actual files you created or modified.
`;
}

function reviewerPrompt(task: string, diff: string, fixRound: number): string {
  const roundNote =
    fixRound > 0
      ? `\n(This is fix attempt #${fixRound} — check that the prior feedback was addressed.)\n`
      : "";

  return `You are a code-reviewer agent.
${roundNote}
The executor was asked to: ${task}

Below is the diff of EXACTLY what the executor produced (staged via git add -A then git diff --cached):

${diff || "(no diff captured — executor produced no file changes)"}

Review the diff above. Check that the executor correctly completed the task.
- If the diff shows the correct implementation, emit verdict:pass.
- If the diff is wrong, missing, or incomplete, emit verdict:needs-fix with a description of what's wrong.

Reference the specific function names, file names, or code from the diff in your verdict issues.
Do not write files. Do not do any work other than reviewing. Stop after emitting your envelope.

End your response with exactly:

<!-- AGENT_OUTPUT -->
\`\`\`json
{"agent":"code-reviewer","verdict":"pass","issues":[]}
\`\`\`
<!-- /AGENT_OUTPUT -->

Replace verdict and issues based on your actual review of the diff.
`;
}

/** Extract issues text from a reviewer's AGENT_OUTPUT for injection into the next executor prompt. */
function extractFeedback(reviewerRecord: RoleRunRecord): string {
  const ao = reviewerRecord.agentOutput;
  if (!ao) return reviewerRecord.output.slice(-800);

  const issues = ao["issues"];
  if (Array.isArray(issues) && issues.length > 0) {
    return issues
      .map((issue, i) => {
        if (typeof issue === "string") return `${i + 1}. ${issue}`;
        if (typeof issue === "object" && issue !== null) {
          const obj = issue as Record<string, unknown>;
          return `${i + 1}. ${obj["description"] ?? obj["message"] ?? JSON.stringify(obj)}`;
        }
        return `${i + 1}. ${String(issue)}`;
      })
      .join("\n");
  }

  // Fall back to the tail of the reviewer's raw output as feedback.
  const raw = reviewerRecord.output;
  return raw.length > 800 ? raw.slice(-800) : raw;
}

// ---------------------------------------------------------------------------
// Core function
// ---------------------------------------------------------------------------

/**
 * Run a fix-loop gated iteration on opencode/Qwen.
 *
 * Steps:
 *   1. Ensure scratch workspace is a valid git repo.
 *   2. Run executor role (with optional fix feedback in the prompt).
 *   3. Capture git diff: `git add -A` then `git diff --cached`.
 *   4. Run code-reviewer with the diff injected into the prompt.
 *   5. If reviewer returns needs-fix and fixRound < maxFix:
 *      a. Extract feedback from reviewer's AGENT_OUTPUT issues.
 *      b. Go to step 2 with fixRound+1 and feedback injected into executor prompt.
 *   6. Once the reviewer returns pass OR maxFix is exhausted:
 *      Apply mergeGateAllowed() with the final verdict.
 *   7. Return structured result with all role-run records.
 *
 * Every executor attempt and every review is recorded as an agent_run row
 * (routed_via=opencode) via runSpawnAgent's internal startRun/completeRun calls.
 */
export async function runFixLoopIteration(
  opts: FixLoopIterationOpts
): Promise<FixLoopIterationResult> {
  const {
    task,
    scratchDir,
    discussion = null,
    model = DEFAULT_OPENCODE_MODEL,
    maxFix = 2,
  } = opts;

  const repoRoot = opts.repoRoot ?? defaultRepoRoot();

  // ── Config fixture ─────────────────────────────────────────────────────────
  let configPath = opts.configPath;
  if (!configPath) {
    const cfgFile = join(scratchDir, "_config", "config.json");
    mkdirSync(join(scratchDir, "_config"), { recursive: true });
    writeFileSync(cfgFile, MINIMAL_CONFIG);
    configPath = cfgFile;
  }

  // ── Env overrides ──────────────────────────────────────────────────────────
  const prevDbPath = process.env["STATS_DB_PATH"];
  if (opts.dbPath) process.env["STATS_DB_PATH"] = opts.dbPath;

  const prevOcModel = process.env["AF_OPENCODE_MODEL"];
  process.env["AF_OPENCODE_MODEL"] = model;

  // ── Shared spawn options ───────────────────────────────────────────────────
  const sharedSpawnOpts = {
    repoRootOverride: repoRoot,
    configPathOverride: configPath,
    discussionBody: fakeDiscBody(task),
    noRegister: false,
    opencodeWorkdir: scratchDir,
  };

  const sharedSpawnArgs = {
    discussion,
    isolation: "none" as const,
    worktreePath: "",
    securityTrigger: false,
    touchpoints: "",
    overrideCap: true,
    dryRunEnvDump: false,
    noRegister: false,
    pr: null,
    operationClass: "agent.spawn",
    sdkLane: false,
    runtime: "opencode",
  };

  // ── Accumulators ───────────────────────────────────────────────────────────
  const executorRuns: RoleRunRecord[] = [];
  const reviewerRuns: RoleRunRecord[] = [];

  let fixRound = 0;
  let fixFeedback: string | undefined;
  let finalVerdict: string | null = null;
  let finalDiff = "";

  try {
    // ── Step 1: Ensure scratch workspace is a valid git repo ─────────────────
    ensureGitRepo(scratchDir);

    // ── Fix loop ──────────────────────────────────────────────────────────────
    while (true) {
      // ── Executor run ──────────────────────────────────────────────────────
      const execResult = await runSpawnAgent(
        {
          ...sharedSpawnArgs,
          role: "executor",
          taskPrompt: executorPrompt(task, fixFeedback),
        },
        sharedSpawnOpts
      );

      const executorRecord: RoleRunRecord = {
        role: "executor",
        eventId: execResult.eventId,
        output: execResult.opencodeResult?.output ?? "",
        agentOutput: execResult.opencodeResult?.agentOutput ?? null,
        exitCode: execResult.opencodeResult?.exitCode ?? execResult.exitCode,
        routedVia: execResult.routedVia ?? "opencode",
        fixRound,
      };
      executorRuns.push(executorRecord);

      // ── Capture diff ──────────────────────────────────────────────────────
      const diff = captureExecutorDiff(scratchDir);
      finalDiff = diff;

      // ── Reviewer run ──────────────────────────────────────────────────────
      const reviewerResult = await runSpawnAgent(
        {
          ...sharedSpawnArgs,
          role: "code-reviewer",
          taskPrompt: reviewerPrompt(task, diff, fixRound),
        },
        sharedSpawnOpts
      );

      const reviewerRecord: RoleRunRecord = {
        role: "code-reviewer",
        eventId: reviewerResult.eventId,
        output: reviewerResult.opencodeResult?.output ?? "",
        agentOutput: reviewerResult.opencodeResult?.agentOutput ?? null,
        exitCode: reviewerResult.opencodeResult?.exitCode ?? reviewerResult.exitCode,
        routedVia: reviewerResult.routedVia ?? "opencode",
        fixRound,
      };
      reviewerRuns.push(reviewerRecord);

      // ── Parse verdict ─────────────────────────────────────────────────────
      const verdict =
        typeof reviewerRecord.agentOutput?.["verdict"] === "string"
          ? (reviewerRecord.agentOutput["verdict"] as string)
          : null;
      finalVerdict = verdict;

      // ── Termination check ─────────────────────────────────────────────────
      // Pass → done. Null verdict (parse failure) → treat as pass (can't loop blindly).
      // needs-fix AND fixRound < maxFix → continue with feedback.
      if (verdict !== "needs-fix" || fixRound >= maxFix) {
        break;
      }

      // ── Prepare feedback for next executor ────────────────────────────────
      fixFeedback = extractFeedback(reviewerRecord);
      fixRound++;
    }

    // ── Gate decision (final verdict only) ────────────────────────────────────
    const labels: string[] =
      finalVerdict === "pass" ? ["code-review-passed"] : [];

    const gate = mergeGateAllowed({
      labels,
      needsSecurityReview: false,
      securityTriggerDetected: false,
      dashboardTouched: false,
      debaterGateOn: false,
    });

    return {
      executorRuns,
      reviewerRuns,
      gate,
      finalVerdict,
      fixCycleTriggered: fixRound > 0,
      fixRoundsRan: fixRound,
      finalDiff,
    };
  } finally {
    // Restore env overrides
    if (opts.dbPath) {
      if (prevDbPath !== undefined) {
        process.env["STATS_DB_PATH"] = prevDbPath;
      } else {
        delete process.env["STATS_DB_PATH"];
      }
    }
    if (prevOcModel !== undefined) {
      process.env["AF_OPENCODE_MODEL"] = prevOcModel;
    } else {
      delete process.env["AF_OPENCODE_MODEL"];
    }
  }
}
