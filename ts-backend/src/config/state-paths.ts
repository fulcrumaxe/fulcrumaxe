/**
 * config/state-paths.ts — single source of truth for runtime-state file
 * paths, mirroring backend/state_paths.py exactly (see D#1632 Spec PR-1,
 * item 2 — ts-backend had no equivalent SSOT before this file; ~17 call
 * sites each did their own `join(homedir(), ".<literal>", ...)` with the
 * AUTONOMOUS_TEAM_STATE_DIR override applied inconsistently).
 *
 * All mutable runtime state lives outside the repo tree so that git worktree
 * merges can never wipe it. Override the root directory via the
 * AUTONOMOUS_TEAM_STATE_DIR environment variable.
 *
 * FROZEN RULE: the DEFAULT_STATE_DIRNAME literal value stays the internal
 * directory name in tracked source. It is flipped to the public fork's name
 * ONLY by open-source/export.sh's substitution pass, over the exported copy
 * — never by hand-editing this file.
 */

import { homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_STATE_DIRNAME = ".autonomous-forever-state";

/** Root directory for all runtime state. Override via AUTONOMOUS_TEAM_STATE_DIR. */
export function stateDir(): string {
  return process.env["AUTONOMOUS_TEAM_STATE_DIR"] ?? join(homedir(), DEFAULT_STATE_DIRNAME);
}

/** DuckDB metrics store (stats_writer / stats_reader / stats-*.ts RPC modules). */
export function statsDb(): string {
  return join(stateDir(), "stats.duckdb");
}

/** SQLite key-value store (db.py / SqliteBlackboard equivalents). */
export function stateDb(): string {
  return join(stateDir(), "state.db");
}

/** Append-only audit log (audit_trail.py equivalent). */
export function auditJsonl(): string {
  return join(stateDir(), "audit.jsonl");
}

/** File-backed blackboard directory (blackboard.py Blackboard class equivalent). */
export function blackboardDir(): string {
  return join(stateDir(), "blackboard");
}
