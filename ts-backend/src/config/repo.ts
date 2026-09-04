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
 * FROZEN RULE: the DEFAULT_REPO literal value stays the internal repo slug in
 * tracked source. It is flipped to the public fork's slug ONLY by
 * open-source/export.sh's substitution pass, over the exported copy — never
 * by hand-editing this file. Changing the literal here is a regression (it
 * would silently retarget internal automation).
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

export const DEFAULT_REPO = "fulcrumaxe/fulcrumaxe";

function repoRoot(): string {
  return (
    process.env["AF_REPO_ROOT"] ??
    join(new URL(import.meta.url).pathname, "..", "..", "..", "..")
  );
}

function configJsonRepo(): string | null {
  const configPath = join(repoRoot(), ".autonomous-team", "config.json");
  if (!existsSync(configPath)) return null;
  try {
    const data = JSON.parse(readFileSync(configPath, "utf-8")) as Record<string, unknown>;
    const repo = data["repo"];
    return typeof repo === "string" && repo ? repo : null;
  } catch {
    return null;
  }
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
