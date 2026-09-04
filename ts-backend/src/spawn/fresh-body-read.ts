/**
 * spawn/fresh-body-read.ts — single fresh-read helper for gate-time Discussion-body checks.
 *
 * D#1794: the ts-backend spawn path carried the same stale-Discussion-cache bug that
 * PR #1783 fixed on the Python lane. The Python gate sites were fixed by threading
 * `--fresh` through to backend/discussion_cache.py and treating a stale fallback as a
 * hard block. This module is the equivalent for the TS gate-time call sites in
 * spawn-agent.ts and pre-spawn-check.ts — it is the ONE place that shells to
 * discussion_cache.py with `--fresh` and inspects the exit code; callers only see the
 * classified result below and never inline execFileSync + a bare catch again.
 *
 * backend/discussion_cache.py's `get-body --fresh` CLI contract (see its docstring,
 * backend/discussion_cache.py:14-35):
 *   exit 0 — body printed to stdout, live/current read
 *   exit 1 — nothing available at all, stdout empty
 *   exit 3 — --fresh requested but the live fetch failed; stdout holds a *stale*
 *            cached fallback (also flagged via a stderr warning)
 *
 * Node's execFileSync throws an Error on non-zero exit with `.status`, `.stdout`, and
 * `.stderr` populated from the child process (respecting the `encoding` option passed
 * to spawnSync under the hood). Exit 3 therefore arrives as a thrown Error carrying a
 * perfectly good — but stale — body on `err.stdout`. A bare `catch {}` cannot tell that
 * apart from "python3 is missing"; that is the defect this module closes.
 *
 * Scope note: only the Python cache (backend/discussion_cache.py) is touched here.
 * ts-backend/src/spawn/discussion-cache.ts (the bun-native cache used by
 * discussion-status.ts) has no `--fresh` support and none is added by this module —
 * discussion-status.ts's two call sites correctly mirror backend/discussion_status.py,
 * which also omits --fresh, and are out of scope for this change.
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

export type FreshBodyRead =
  | { status: "live"; body: string }
  | { status: "stale"; body: string }
  | { status: "unavailable" };

/**
 * Fresh-read a Discussion body via `backend/discussion_cache.py get-body <n> --fresh`.
 *
 * - "live": a current read succeeded; `body` is trustworthy for gate decisions.
 * - "stale": the fresh fetch failed and the CLI fell back to a cached body (exit 3);
 *   `body` is the stale fallback — the caller decides whether that is acceptable for
 *   its own disposition (gate-time sites must not use it; advisory sites may).
 * - "unavailable": the script is missing, produced no output, or failed in a way that
 *   is not the documented stale-fallback case (exit 1, timeout, missing python3, etc).
 */
export function readFreshBody(repoRoot: string, discussion: number): FreshBodyRead {
  const cacheScript = join(repoRoot, "backend", "discussion_cache.py");
  if (!existsSync(cacheScript)) {
    return { status: "unavailable" };
  }

  try {
    const stdout = execFileSync(
      "python3",
      [cacheScript, "get-body", String(discussion), "--fresh"],
      {
        timeout: 15_000,
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "pipe"],
        // Pass env explicitly (rather than relying on implicit inheritance) so a
        // PATH mutated at call time — e.g. a test's fake `python3` fixture — is
        // actually honored. Bun's execFileSync does not always re-read
        // process.env when the option is omitted.
        env: process.env,
      }
    );
    if (!stdout.trim()) {
      return { status: "unavailable" };
    }
    return { status: "live", body: stdout };
  } catch (err) {
    const e = err as NodeJS.ErrnoException & { status?: number; stdout?: string };
    if (e.status === 3 && typeof e.stdout === "string" && e.stdout.trim()) {
      return { status: "stale", body: e.stdout };
    }
    return { status: "unavailable" };
  }
}
