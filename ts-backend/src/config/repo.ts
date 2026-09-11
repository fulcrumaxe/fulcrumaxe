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

/**
 * Treat unset, empty, and whitespace-only values as absent — the same
 * "no answer" outcome as `null`/`undefined`. A precedence step's `??` only
 * short-circuits on nullish values, so a defined-but-empty string (e.g.
 * `GH_REPO=""`) would otherwise win the step and compose into an empty
 * `--repo` argument downstream. `gh --repo ""` is not an error — it exits 0
 * and silently resolves from the checkout's git remote, so the empty pin
 * becomes exactly the bare unpinned call it exists to replace, and it still
 * greps as pinned (D#2520; mirrors tui/src/config/repo.ts, D#2380).
 */
function nonEmpty(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  return value.trim() ? value : null;
}

function configJsonField(key: string): string | null {
  const configPath = join(repoRoot(), ".autonomous-team", "config.json");
  if (!existsSync(configPath)) return null;
  try {
    const data = JSON.parse(readFileSync(configPath, "utf-8")) as Record<string, unknown>;
    const value = data[key];
    return typeof value === "string" ? nonEmpty(value) : null;
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
 *
 * Every step is normalized through `nonEmpty()` so a defined-but-empty or
 * whitespace-only value falls through to the next source instead of winning
 * the step. DEFAULT_REPO is a non-empty hardcoded literal, so in practice
 * this chain always resolves — the explicit throw below is a guard on that
 * invariant, not reachable code today: it exists so a future edit that
 * weakens DEFAULT_REPO fails loudly at the resolution site instead of
 * silently handing an empty slug to a `gh --repo` call (D#2520).
 */
export function resolveRepo(): string {
  const repo =
    configJsonRepo() ??
    nonEmpty(process.env["GH_REPO"]) ??
    nonEmpty(process.env["_REPO"]) ??
    nonEmpty(DEFAULT_REPO);
  if (!repo) {
    throw new Error(
      "resolveRepo(): no non-empty repo slug from .autonomous-team/config.json, GH_REPO, " +
        "_REPO, or DEFAULT_REPO — refusing to return an empty slug",
    );
  }
  return repo;
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
