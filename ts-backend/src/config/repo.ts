/**
 * config/repo.ts — single source of truth for the GitHub repo slug that
 * ts-backend automation targets (gh CLI calls, GraphQL queries, PR/discussion
 * URLs).
 *
 * Consolidates ~20 call sites across 9 files that each hardcoded their own
 * fallback constant (see D#1632 Spec PR-1, item 1/3).
 *
 * Precedence (frozen — see D#1632 "R-rule"):
 *   1. .autonomous-team/config.json "repo" field
 *   2. GH_REPO environment variable
 *   3. _REPO environment variable
 *   4. DEFAULT_REPO constant (hardcoded fallback)
 *
 * DEFAULT_REPO used to be pinned by a rule reserving every edit of the
 * literal to open-source/export.sh's substitution pass. D#2348 retires that
 * pass — development moves to the public repo, so there is nothing left to
 * rewrite and nothing left to pin the literal for. The value it pinned was
 * also the pre-rename slug, which resolved only through GitHub's rename
 * redirect: a wrong target that could never surface as an error.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

export const DEFAULT_REPO = "autonomous-agent-7/fulcrumaxe";

function repoRoot(): string {
  return (
    process.env["AF_REPO_ROOT"] ??
    join(new URL(import.meta.url).pathname, "..", "..", "..", "..")
  );
}

function configJsonField(key: string): string | null {
  const configPath = join(repoRoot(), ".autonomous-team", "config.json");
  if (!existsSync(configPath)) return null;
  try {
    const data = JSON.parse(readFileSync(configPath, "utf-8")) as Record<string, unknown>;
    const value = data[key];
    return typeof value === "string" && value ? value : null;
  } catch {
    return null;
  }
}

function configJsonRepo(): string | null {
  return configJsonField("repo");
}

/**
 * Resolve the repo slug ("owner/name") using the frozen precedence order
 * documented above. Safe to call repeatedly — re-reads env/config each call
 * so tests can override AUTONOMOUS_TEAM_REPO-adjacent state between cases.
 */
export function resolveRepo(): string {
  return (
    configJsonRepo() ??
    process.env["GH_REPO"] ??
    process.env["_REPO"] ??
    DEFAULT_REPO
  );
}

/** Split helper: the "owner" half of a resolved (or supplied) repo slug. */
export function repoOwner(repo: string = resolveRepo()): string {
  return repo.split("/")[0] ?? "";
}

/** Split helper: the "name" half of a resolved (or supplied) repo slug. */
export function repoName(repo: string = resolveRepo()): string {
  return repo.split("/")[1] ?? "";
}

// --- Two names, one value ---------------------------------------------------
//
// Code, PRs and CI are moving to a public repo while Discussions and Issues
// stay in the private one. Two optional config.json keys name the two planes:
//
//   "code_repo"        the repo that holds commits, PRs and CI.
//   "discussion_repo"  the repo that holds Discussions and Issues.
//
// Neither is set in this tree, and neither is set by this change. With both
// absent these accessors return exactly what resolveRepo() returns, so adding
// them is inert. Setting "code_repo" *is* the cutover, and belongs to the
// change that performs it.
//
// The asymmetry between the two is deliberate. resolveCodeRepo() keeps the full
// precedence chain, DEFAULT_REPO included: every checkout has a code repo.
// resolveDiscussionRepo() stops before DEFAULT_REPO and returns "" instead — a
// forked adopter has no private twin, so "no Discussion plane" is a legitimate
// answer, and falling through to the hard-coded slug would point a fork's
// Discussion reads at our repo (the D#1870 hazard). Callers must branch on the
// empty string rather than treat it as a failure.

/** The repo that holds commits, PRs and CI. */
export function resolveCodeRepo(): string {
  return configJsonField("code_repo") ?? resolveRepo();
}

/**
 * The repo that holds Discussions and Issues, or "" when this checkout has
 * none. Empty is a valid answer, not an error — see the note above.
 */
export function resolveDiscussionRepo(): string {
  return (
    configJsonField("discussion_repo") ??
    configJsonRepo() ??
    process.env["GH_REPO"] ??
    process.env["_REPO"] ??
    ""
  );
}
