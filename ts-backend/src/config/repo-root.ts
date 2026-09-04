/**
 * config/repo-root.ts — canonical checkout-path resolver for ts-backend.
 *
 * Single source of truth for *where the checkout is on disk*. Companion to
 * config/repo.ts, which resolves the repo *slug* (owner/name) and already has
 * its own private, out-of-scope `repoRoot()` helper — that helper answers a
 * different question (the slug source) and is not folded into this module.
 * config/state-paths.ts answers a third question (the runtime state dir,
 * which deliberately lives outside the checkout). Neither existing module
 * resolves a checkout path, so this is confirmed to be new ground, not a
 * D#1997-style rediscovery of something already present.
 *
 * Two questions, kept deliberately separate because callers conflate them:
 *
 *   repoRoot()      the checkout this process is running in. Inside a linked
 *                   git working tree (a worktree) this is that working tree,
 *                   not the checkout it was branched from.
 *   mainRepoRoot()  the checkout a linked working tree was created from.
 *                   Outside one it equals repoRoot().
 *
 * Anything that locates a file the running agent reads or writes — a script,
 * a CLI, a config file inside the checkout — wants repoRoot(). Anything that
 * asks "did this write land outside the caller's sandbox" (a containment or
 * authorisation question) wants mainRepoRoot() instead.
 *
 * Neither environment override is load-bearing for any containment or
 * authorisation decision: both AUTONOMOUS_TEAM_REPO_ROOT and AF_REPO_ROOT are
 * settable by the very process whose checkout they name, so a decision that
 * keys off either one can be steered by the process it is supposed to be
 * checking.
 *
 * Mirrors backend/repo_root.py's two-answer contract; see that module's
 * docstring for the fuller history (D#1997, the pre-#1997 cwd-anchoring bug,
 * why hooks/repo_root.py stays a separate third implementation). This module
 * does not attempt to port that third implementation — hooks/sandbox_rules.py
 * is deliberately subprocess-free, and hooks/repo_root.py is deliberately
 * filesystem-only for that reason. Out of scope here.
 *
 * Resolution order for repoRoot():
 *   1. AUTONOMOUS_TEAM_REPO_ROOT — canonical override, matches the Python lane.
 *   2. AF_REPO_ROOT              — existing ts-backend convention. Eight of the
 *                                  fourteen-ish deferred hand-rolled sites check
 *                                  this today; honouring it here keeps their
 *                                  eventual conversion to this module behaviour-
 *                                  preserving rather than an escape-hatch removal.
 *   3. `git rev-parse --show-toplevel`, anchored at this module's own directory
 *      (never at process.cwd() — see backend/repo_root.py's docstring for why
 *      that distinction matters).
 *   4. This module's own location, walked up to the checkout root. Neither
 *      function throws; this floor is what makes that guarantee possible even
 *      with no git on PATH.
 *
 * mainRepoRoot() resolution:
 *   `git rev-parse --path-format=absolute --git-common-dir`, anchored at
 *   repoRoot(). Its parent is the main checkout when the common dir's
 *   basename is ".git"; falls back to repoRoot() for a bare repo or a
 *   `--separate-git-dir` layout where that assumption doesn't hold.
 *
 * Results are memoised at module level. Anything that mutates the
 * environment and re-resolves (tests, mostly) must call _clearCaches() first.
 */

import { statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

/** Canonical override — matches backend/repo_root.py's ENV_REPO_ROOT. */
export const ENV_REPO_ROOT = "AUTONOMOUS_TEAM_REPO_ROOT";

/** Secondary override — the pre-existing ts-backend convention. */
export const ENV_AF_REPO_ROOT = "AF_REPO_ROOT";

// This file lives at <repo-root>/ts-backend/src/config/repo-root.ts. Directory
// anchor (dirname of this module's own path), three parents up: config/ ->
// src/ -> ts-backend/ -> repo root. This is the one legitimate hand-rolled walk
// in ts-backend/src — see tests/config/repo-root-walk-baseline.txt, where it is
// listed with an inline comment explaining the exemption.
const _MODULE_DIR = dirname(fileURLToPath(import.meta.url));
const _MODULE_ANCHOR = resolve(_MODULE_DIR, "..", "..", "..");

let _repoRootCache: string | null = null;
let _mainRepoRootCache: string | null = null;

/**
 * Run `git *args` anchored at *cwd*; return trimmed stdout, or null.
 *
 * Every failure mode collapses to null on purpose — a missing git binary, a
 * directory that is not a work tree, and a git too old for a flag used below
 * are all "git cannot answer this", and every caller falls back the same way.
 */
function _git(args: string[], cwd: string): string | null {
  let result;
  try {
    result = spawnSync("git", args, { cwd, encoding: "utf-8", timeout: 10_000 });
  } catch {
    return null;
  }
  if (result.error || result.status !== 0) return null;
  const out = (result.stdout ?? "").trim();
  return out || null;
}

function _expandHome(p: string): string {
  if (p === "~") return homedir();
  if (p.startsWith("~/")) return resolve(homedir(), p.slice(2));
  return p;
}

/** Filesystem/git-derived checkout root. Never consults either env override. */
function _deriveRepoRoot(): string {
  const top = _git(["rev-parse", "--show-toplevel"], _MODULE_ANCHOR);
  if (top) return resolve(top);
  return _MODULE_ANCHOR;
}

/**
 * Shared resolution logic behind mainRepoRoot(), parameterised on *root* so
 * it can be measured from any starting checkout.
 *
 * `git rev-parse --git-common-dir` names the *shared* git directory: inside a
 * linked working tree that is the main checkout's `.git`; outside one it is
 * this checkout's own `.git`. Its parent is therefore the main checkout in
 * both cases, so there is no separate "am I in a linked tree" branch to get
 * wrong here.
 */
function _mainRepoRootFrom(root: string): string {
  let common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root);
  if (!common) {
    common = _git(["rev-parse", "--git-common-dir"], root);
  }
  if (!common) return root;

  // Joining an absolute right operand discards the left, so this handles the
  // absolute and the relative answer in one expression.
  const commonPath = resolve(root, common);
  if (basename(commonPath) !== ".git") return root;

  const parent = dirname(commonPath);
  try {
    if (!statSync(parent).isDirectory()) return root;
  } catch {
    return root;
  }
  return parent;
}

/**
 * Absolute path of the checkout this process is running in.
 *
 * Resolution order:
 *   1. AUTONOMOUS_TEAM_REPO_ROOT — explicit override always wins.
 *   2. AF_REPO_ROOT — explicit override, second precedence.
 *   3. `git rev-parse --show-toplevel`, anchored at this module. Inside a
 *      linked working tree this is that linked tree.
 *   4. This module's own location, walked up to the checkout root.
 *
 * Never throws.
 */
export function repoRoot(): string {
  if (_repoRootCache !== null) return _repoRootCache;

  const primary = process.env[ENV_REPO_ROOT];
  if (primary) {
    _repoRootCache = resolve(_expandHome(primary));
    return _repoRootCache;
  }

  const secondary = process.env[ENV_AF_REPO_ROOT];
  if (secondary) {
    _repoRootCache = resolve(_expandHome(secondary));
    return _repoRootCache;
  }

  _repoRootCache = _deriveRepoRoot();
  return _repoRootCache;
}

/**
 * Absolute path of the checkout a linked working tree was branched from.
 * Outside a linked working tree this equals repoRoot(). Never throws.
 */
export function mainRepoRoot(): string {
  if (_mainRepoRootCache !== null) return _mainRepoRootCache;
  _mainRepoRootCache = _mainRepoRootFrom(repoRoot());
  return _mainRepoRootCache;
}

/**
 * Drop memoised results so the next call re-resolves. Only needed by callers
 * that change the environment underneath this module (tests, mostly).
 */
export function _clearCaches(): void {
  _repoRootCache = null;
  _mainRepoRootCache = null;
}

// Re-exported for tests that want the env-immune floor without going through
// the memoised, override-aware public functions above.
export const _internal = { _deriveRepoRoot, _mainRepoRootFrom, _MODULE_ANCHOR };
