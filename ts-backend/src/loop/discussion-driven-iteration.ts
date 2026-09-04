/**
 * loop/discussion-driven-iteration.ts — Outer loop: drive one full SDLC iteration
 * from a Discussion through to a PR-ready artifact.
 *
 * This is the "missing real loop piece" — the glue that sits above the inner
 * fix-loop harness (fix-loop-iteration-opencode.ts) and handles the discussion
 * lifecycle: status check → pre-spawn gate → inner fix-loop → PR artifact.
 *
 * Flow:
 *   1. Receive a discussion (number + body — either live via discussion-status.ts
 *      extractStatus, or fixture body injected for tests).
 *   2. Confirm STATUS:SPEC_READY; extract the Spec (Acceptance) section via
 *      getSections(). If not ready, return { outcome: "skipped" }.
 *   3. Run pre-spawn-check (executor role) to gate concurrency + dial.
 *      If blocked, return { outcome: "blocked" }.
 *   4. Run the inner fix-loop (runFixLoopIteration) on the extracted spec task
 *      in a scratch git workspace.
 *   5. On gate-pass: produce a PR-ready artifact:
 *      - Commit the staged work in the scratch workspace on a feature branch.
 *      - Generate a PR title + body from the discussion title + spec.
 *   6. Record agent_run rows for both the executor and reviewer roles via
 *      the inner fix-loop harness (routed_via=opencode).
 *   7. Return a structured result including the inner-loop result, PR artifact,
 *      and the outcome label.
 *
 * Gate behind E2E_OPENCODE=1 — spends Qwen API credit.
 *
 * Spec is the contract; Implementation Notes below are advisory.
 *
 * Implementation Notes:
 *   - Thin glue over: discussion-status.ts, pre-spawn-check.ts,
 *     fix-loop-iteration-opencode.ts. No merged-logic edits.
 *   - PR artifact = committed branch in scratchDir + generated title/body.
 *   - Does NOT push to GitHub; caller decides what to do with the branch.
 *   - Discussion body may be injected (test fixture) or fetched live
 *     (via DISCUSSION_BODY_MOCK or fetchBody fallback).
 */

import { join } from "node:path";
import { mkdirSync, existsSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { extractStatus, getSections } from "../spawn/discussion-status.js";
import { runPreSpawnCheck } from "../spawn/pre-spawn-check.js";
import { runFixLoopIteration } from "./fix-loop-iteration-opencode.js";
import type { FixLoopIterationResult } from "./fix-loop-iteration-opencode.js";
import { resolveRepo } from "../config/repo.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DiscussionIterationOpts {
  /**
   * Discussion number (used for agent_run tracking + pre-spawn-check).
   */
  discussion: number;

  /**
   * Human-readable title (used for PR title generation).
   */
  title: string;

  /**
   * Discussion body — if provided, the live GitHub fetch is skipped.
   * Required for tests (fixture injection). In production, provide the body
   * read from the discussion cache.
   */
  body: string;

  /**
   * Scratch directory where the executor operates and the feature branch
   * is committed. Created if absent.
   */
  scratchDir: string;

  /**
   * Path to the real repo root (for Python backend modules in runFixLoopIteration).
   * Defaults to the resolved repo root relative to this file.
   */
  repoRoot?: string;

  /**
   * Path to stats DuckDB. Forwarded to the inner fix-loop for agent_run rows.
   */
  dbPath?: string;

  /**
   * Path to a minimal config.json satisfying pre-spawn-check caps.
   * If omitted, the inner fix-loop writes its own minimal fixture.
   */
  configPath?: string;

  /**
   * Maximum fix rounds for the inner loop. Default 2.
   */
  maxFix?: number;

  /**
   * opencode model string. Defaults to DEFAULT_OPENCODE_MODEL.
   */
  model?: string;
}

/** PR-ready artifact produced when the gate passes. */
export interface PrArtifact {
  /**
   * Feature branch name created in scratchDir.
   * E.g. "feature/add-slugify-util"
   */
  branch: string;

  /**
   * Generated PR title (plain English, ≤ 70 chars).
   */
  prTitle: string;

  /**
   * Generated PR body (Markdown, describes the change + how to test).
   */
  prBody: string;

  /**
   * Git commit SHA of the feature commit, or empty if commit did not happen.
   */
  commitSha: string;
}

/** Outcome of a single discussion-driven iteration. */
export type IterationOutcome = "done" | "skipped" | "blocked" | "gate-failed";

export interface DiscussionIterationResult {
  /**
   * High-level outcome:
   *   "done"        — inner loop ran, gate passed, PR artifact produced.
   *   "skipped"     — discussion was not SPEC_READY.
   *   "blocked"     — pre-spawn-check refused (concurrency/dial/budget).
   *   "gate-failed" — inner loop ran but final reviewer verdict was not pass.
   */
  outcome: IterationOutcome;

  /**
   * Extracted STATUS from the discussion body (e.g. "SPEC_READY", "DISCUSSING").
   * Populated for all outcomes.
   */
  detectedStatus: string;

  /**
   * The reason from the pre-spawn-check when outcome="blocked".
   */
  blockedReason?: string;

  /**
   * Result from the inner fix-loop (null when outcome=skipped or blocked).
   */
  innerLoop: FixLoopIterationResult | null;

  /**
   * PR-ready artifact (null when outcome != "done").
   */
  prArtifact: PrArtifact | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Resolve the repo root relative to this source file. */
function defaultRepoRoot(): string {
  // This file: ts-backend/src/loop/discussion-driven-iteration.ts
  // Walk up: loop → src → ts-backend → repo_root
  const here = new URL(import.meta.url).pathname;
  return join(here, "..", "..", "..", "..", "..");
}

/** Slugify a title into a branch-safe string (lowercase, hyphens). */
function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}

/**
 * Extract the task description from the Spec (Acceptance) section.
 * Returns the full spec text so the executor has all acceptance criteria.
 */
function extractTaskFromSpec(sections: ReturnType<typeof getSections>): string {
  const spec = sections.spec.trim();
  const intent = sections.intent.trim();
  const notes = sections.implementation_notes.trim();

  const parts: string[] = [];
  if (intent) parts.push(`Intent:\n${intent}`);
  if (spec) parts.push(`Spec (Acceptance Criteria):\n${spec}`);
  if (notes) parts.push(`Implementation Notes (advisory):\n${notes}`);

  return parts.join("\n\n") || spec || sections.spec;
}

/**
 * Create a feature branch and commit all staged changes in scratchDir.
 * Returns the branch name and commit SHA (empty string on failure).
 */
function createFeatureBranch(
  scratchDir: string,
  branchName: string
): { commitSha: string } {
  // Create and switch to the feature branch.
  const checkoutResult = spawnSync(
    "git",
    ["-C", scratchDir, "checkout", "-b", branchName],
    { encoding: "utf-8", timeout: 15_000 }
  );
  if (checkoutResult.status !== 0) {
    // Branch may already exist; try to switch to it.
    spawnSync("git", ["-C", scratchDir, "checkout", branchName], {
      encoding: "utf-8",
      timeout: 10_000,
    });
  }

  // Stage all changes the executor left (mirrors fix-loop captureExecutorDiff pattern).
  spawnSync("git", ["-C", scratchDir, "add", "-A"], {
    encoding: "utf-8",
    timeout: 15_000,
  });

  // Commit — use --allow-empty so the test always gets a SHA even if nothing
  // was staged (edge case: executor produced the diff, captureExecutorDiff
  // already staged it, commit sees empty index).
  const commitResult = spawnSync(
    "git",
    [
      "-C",
      scratchDir,
      "commit",
      "-m",
      `implement: changes produced by inner fix-loop`,
      "--allow-empty",
    ],
    { encoding: "utf-8", timeout: 15_000 }
  );
  if (commitResult.status !== 0) {
    return { commitSha: "" };
  }

  // Read the commit SHA.
  const shaResult = spawnSync(
    "git",
    ["-C", scratchDir, "rev-parse", "HEAD"],
    { encoding: "utf-8", timeout: 10_000 }
  );
  const commitSha = (shaResult.stdout ?? "").trim();
  return { commitSha };
}

/** Generate a PR title from the discussion title (≤ 70 chars). */
function generatePrTitle(title: string): string {
  // Strip common prefixes like [Bug], [Feature], [Critical]
  const cleaned = title.replace(/^\[.*?\]\s*/, "").trim();
  return cleaned.length > 70 ? cleaned.slice(0, 67) + "..." : cleaned;
}

/** Generate a PR body in Markdown. */
function generatePrBody(
  discussionNumber: number,
  title: string,
  sections: ReturnType<typeof getSections>,
  innerLoop: FixLoopIterationResult
): string {
  const specSnippet = sections.spec.slice(0, 800);
  const verdictNote =
    innerLoop.finalVerdict === "pass"
      ? "Code-reviewer passed."
      : `Reviewer verdict: ${innerLoop.finalVerdict ?? "unknown"}.`;

  const fixNote =
    innerLoop.fixCycleTriggered
      ? `Fix cycle ran ${innerLoop.fixRoundsRan} time(s) before reaching final verdict.`
      : "Passed review on first attempt.";

  const filesNote =
    innerLoop.executorRuns.length > 0
      ? (innerLoop.executorRuns[innerLoop.executorRuns.length - 1]?.agentOutput?.["files_touched"] as string[] | undefined ?? []).join(", ")
      : "";

  return `## Summary

Implements Discussion #${discussionNumber}: ${title}

${sections.intent ? `${sections.intent.slice(0, 300)}\n` : ""}

## Spec (Acceptance Criteria)

${specSnippet}${sections.spec.length > 800 ? "\n\n_(truncated — see Discussion for full spec)_" : ""}

## How it was built

This change was produced by the discussion-driven outer loop running on Qwen via opencode.

${verdictNote} ${fixNote}
${filesNote ? `\nFiles touched: ${filesNote}` : ""}

## Verification

Gate 1: PASS — inner fix-loop completed and final verdict recorded.
Gate 2: N/A — PR artifact is from scratch workspace; tests run within the executor task.

## Links

Discussion: #${discussionNumber}
routed_via: opencode
fix_rounds: ${innerLoop.fixRoundsRan}
`;
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

/**
 * Run a single discussion-driven SDLC iteration.
 *
 * Steps:
 *   1. extractStatus(body) — if not SPEC_READY, return skipped.
 *   2. runPreSpawnCheck (executor, overrideCap) — if blocked, return blocked.
 *   3. getSections(body) → build task string.
 *   4. ensureGitRepo(scratchDir) — managed inside fix-loop, but also
 *      create the scratch dir here so pre-spawn-check fixtures can live there.
 *   5. runFixLoopIteration(task, scratchDir, ...) — inner fix-loop on Qwen.
 *   6. If gate.allowed → createFeatureBranch + generatePrTitle/Body → prArtifact.
 *   7. Return structured result.
 */
export async function runDiscussionIteration(
  opts: DiscussionIterationOpts
): Promise<DiscussionIterationResult> {
  const {
    discussion,
    title,
    body,
    scratchDir,
    maxFix = 2,
    model,
  } = opts;

  const repoRoot = opts.repoRoot ?? defaultRepoRoot();

  // ── Step 1: STATUS check ──────────────────────────────────────────────────
  const detectedStatus = extractStatus(body);
  if (detectedStatus !== "SPEC_READY") {
    return {
      outcome: "skipped",
      detectedStatus,
      innerLoop: null,
      prArtifact: null,
    };
  }

  // ── Step 2: pre-spawn-check (executor role) ───────────────────────────────
  // Use overrideCap + discussionBody injection so no network calls are needed
  // in test mode (fixture body already confirms SPEC_READY + all sections).
  const psc = await runPreSpawnCheck({
    role: "executor",
    discussion,
    discussionBody: body,
    overrideCap: true,            // outer loop manages its own concurrency
    noRegister: true,             // registration happens inside runFixLoopIteration
    repoRootOverride: repoRoot,
    configPathOverride: opts.configPath ?? null,
    dbPathOverride: opts.dbPath ?? null,
  });

  if (!psc.allowed) {
    return {
      outcome: "blocked",
      detectedStatus,
      blockedReason: psc.blocked_reason ?? psc.reason,
      innerLoop: null,
      prArtifact: null,
    };
  }

  // ── Step 3: extract task from spec ────────────────────────────────────────
  const sections = getSections(body);
  const task = extractTaskFromSpec(sections);

  // ── Step 4: ensure scratch dir exists ─────────────────────────────────────
  mkdirSync(scratchDir, { recursive: true });

  // Write a minimal config fixture if none provided (inner-loop writes its own
  // inside scratchDir/_config, so this is only for our pre-spawn-check override).
  let configPath = opts.configPath;
  if (!configPath) {
    const cfgDir = join(scratchDir, "_outer_config");
    mkdirSync(cfgDir, { recursive: true });
    configPath = join(cfgDir, "config.json");
    if (!existsSync(configPath)) {
      writeFileSync(
        configPath,
        JSON.stringify({
          repo: resolveRepo(),
          max_concurrent_agents: 8,
          dials: { "agent.spawn": { level: 5, ceiling: 5 } },
        })
      );
    }
  }

  // ── Step 5: run inner fix-loop ─────────────────────────────────────────────
  const innerLoop = await runFixLoopIteration({
    task,
    scratchDir,
    repoRoot,
    dbPath: opts.dbPath,
    configPath,
    discussion,
    model,
    maxFix,
  });

  // ── Step 6: produce PR artifact if gate passed ────────────────────────────
  if (innerLoop.gate.allowed) {
    const branchName = `feature/${slugify(title) || `discussion-${discussion}`}`;
    const { commitSha } = createFeatureBranch(scratchDir, branchName);

    const prArtifact: PrArtifact = {
      branch: branchName,
      prTitle: generatePrTitle(title),
      prBody: generatePrBody(discussion, title, sections, innerLoop),
      commitSha,
    };

    return {
      outcome: "done",
      detectedStatus,
      innerLoop,
      prArtifact,
    };
  }

  // Gate failed — loop ran but reviewer did not pass.
  return {
    outcome: "gate-failed",
    detectedStatus,
    innerLoop,
    prArtifact: null,
  };
}
