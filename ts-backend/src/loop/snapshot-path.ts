/**
 * snapshot-path.ts — TS mirror of backend/snapshot_path.py.
 *
 * Resolution order, identical to the Python module:
 *   1. SNAPSHOT_PATH              — explicit full path (test override; still wins)
 *   2. AUTONOMOUS_TEAM_STATE_DIR  — the runtime state directory
 *   3. ~/.fulcrumaxe-state/loop-snapshot.json
 *
 * This is a pure mirror rather than a `python3 backend/snapshot_path.py` shell-out
 * so that reading the snapshot does not cost a subprocess. The default string
 * lives in exactly two places — here and the Python module — and the parity
 * test in tests/loop/snapshot-path.test.ts asserts they agree.
 */

import { homedir } from "node:os";
import { join } from "node:path";

/** Basename of the snapshot file inside the state directory. */
export const SNAPSHOT_FILENAME = "loop-snapshot.json";

/**
 * Age beyond which the snapshot must not be treated as current state.
 * Mirrors MAX_AGE_SECONDS in backend/snapshot_path.py.
 */
export const MAX_AGE_SECONDS = 600;

/** Resolve the state directory (mirrors backend/state_paths.py STATE_DIR). */
export function stateDir(): string {
  return process.env["AUTONOMOUS_TEAM_STATE_DIR"] ?? join(homedir(), ".fulcrumaxe-state");
}

/** Resolve the canonical loop-snapshot path. */
export function resolveSnapshotPath(): string {
  const override = process.env["SNAPSHOT_PATH"];
  if (override) return override;
  return join(stateDir(), SNAPSHOT_FILENAME);
}

/**
 * Seconds since `generatedAt`, or null when it is absent or unparseable.
 * A null result means "cannot date this snapshot" and must be treated as stale.
 */
export function snapshotAgeSeconds(snapshot: Record<string, unknown>): number | null {
  const raw = (snapshot["generated_at"] ?? snapshot["snapshot_at"]) as string | undefined;
  if (!raw) return null;
  const parsed = Date.parse(raw);
  if (Number.isNaN(parsed)) return null;
  return (Date.now() - parsed) / 1000;
}

/** True when the snapshot is missing a usable timestamp or is past MAX_AGE_SECONDS. */
export function isSnapshotStale(
  snapshot: Record<string, unknown>,
  maxAgeSeconds: number = MAX_AGE_SECONDS,
): boolean {
  const age = snapshotAgeSeconds(snapshot);
  if (age === null) return true;
  return age > maxAgeSeconds;
}
