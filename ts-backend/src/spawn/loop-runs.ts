/**
 * spawn/loop-runs.ts — Mirrors backend/loop_runs.py 1:1.
 *
 * Per-iteration loop run recorder + tail CLI.
 *
 * Write side (called from run-loop-iteration.sh):
 *   bun run src/spawn/loop-runs.ts start              → writes started_at stub, prints filepath
 *   bun run src/spawn/loop-runs.ts finish --file F \
 *     --exit N [--stderr PATH]                        → finalises the file
 *
 * Read side:
 *   bun run src/spawn/loop-runs.ts tail [--n 10] [--failures-only]
 *
 * Programmatic exports:
 *   import { cmdStart, cmdFinish, cmdTail, latestFailingRunPath } from "./loop-runs.js"
 */

import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
  renameSync,
  unlinkSync,
} from "node:fs";
import { join } from "node:path";
import { randomBytes } from "node:crypto";

// ---------------------------------------------------------------------------
// Paths (mirrors Python _runs_dir)
// ---------------------------------------------------------------------------

function repoRoot(): string {
  if (process.env["AF_REPO_ROOT"]) return process.env["AF_REPO_ROOT"]!;
  // This file lives at ts-backend/src/spawn/loop-runs.ts
  // → ts-backend/src/spawn/ → ts-backend/src/ → ts-backend/ → repo root
  const thisFile = new URL(import.meta.url).pathname;
  return join(thisFile, "..", "..", "..", "..");
}

/**
 * Return the .autonomous-team/loop-runs/ dir, creating it if needed.
 * Mirrors Python _runs_dir() exactly.
 */
export function runsDir(repoRootOverride?: string): string {
  const root = repoRootOverride ?? repoRoot();
  const d = join(root, ".autonomous-team", "loop-runs");
  mkdirSync(d, { recursive: true });
  return d;
}

// ---------------------------------------------------------------------------
// Timestamp helpers (mirrors Python _now_iso and _ts_to_filename)
// ---------------------------------------------------------------------------

/**
 * Return current UTC timestamp in the format Python uses: "2026-05-15T02:46:00Z".
 * Matches Python datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ").
 */
function nowIso(): string {
  const d = new Date();
  const pad = (n: number): string => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`
  );
}

/**
 * Convert ISO8601 timestamp to a filename-safe string.
 * 2026-05-15T02:46:00Z  →  2026-05-15T02-46-00Z.json
 * Colons are replaced with hyphens so the name is safe on all platforms.
 * Mirrors Python _ts_to_filename() exactly.
 */
export function tsToFilename(ts: string): string {
  let safe = ts.replace(/:/g, "-");
  if (!safe.endsWith(".json")) safe += ".json";
  return safe;
}

// ---------------------------------------------------------------------------
// Loop run stub shape
// ---------------------------------------------------------------------------

export interface LoopRunStub {
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  duration_s: number | null;
  last_stderr_lines: string[];
}

// ---------------------------------------------------------------------------
// Prune (mirrors Python _prune)
// ---------------------------------------------------------------------------

function prune(dir: string, keep = 1000): void {
  try {
    const files = readdirSync(dir)
      .filter((f) => f.endsWith(".json"))
      .sort();
    const excess = files.length - keep;
    if (excess > 0) {
      for (const old of files.slice(0, excess)) {
        try {
          unlinkSync(join(dir, old));
        } catch {
          // best-effort
        }
      }
    }
  } catch {
    // best-effort
  }
}

// ---------------------------------------------------------------------------
// Write side: start (mirrors Python cmd_start)
// ---------------------------------------------------------------------------

/**
 * Write a stub JSON file and print its path to stdout.
 * Mirrors Python loop_runs.cmd_start() exactly.
 * Returns the path that was written (same as what's printed).
 */
export function cmdStart(repoRootOverride?: string): string {
  const ts = nowIso();
  // ts has colons (e.g. "2026-05-15T02:46:00Z"); filename replaces them with hyphens
  const startedAt = ts;
  const dir = runsDir(repoRootOverride);
  const filename = tsToFilename(ts);
  const path = join(dir, filename);

  const stub: LoopRunStub = {
    started_at: startedAt,
    finished_at: null,
    exit_code: null,
    duration_s: null,
    last_stderr_lines: [],
  };
  writeFileSync(path, JSON.stringify(stub, null, 2) + "\n");
  process.stdout.write(path + "\n");
  return path;
}

// ---------------------------------------------------------------------------
// Write side: finish (mirrors Python cmd_finish)
// ---------------------------------------------------------------------------

/**
 * Finalise a loop-run file with exit code, duration, and stderr tail.
 * Mirrors Python loop_runs.cmd_finish() exactly.
 * Returns 0 on success, 1 on error.
 */
export function cmdFinish(opts: {
  file: string;
  exit: number;
  stderr?: string;
}): number {
  const runFile = opts.file;
  if (!existsSync(runFile)) {
    process.stderr.write(`loop_runs finish: file not found: ${runFile}\n`);
    return 1;
  }

  let data: LoopRunStub;
  try {
    data = JSON.parse(readFileSync(runFile, "utf-8")) as LoopRunStub;
  } catch (e) {
    process.stderr.write(`loop_runs finish: cannot read ${runFile}: ${String(e)}\n`);
    return 1;
  }

  // nowIso() returns colon-format (e.g. "2026-05-15T02:46:00Z") — stored as-is
  const finishedAt = nowIso();
  const startedAt = data.started_at ?? finishedAt;

  // Compute duration from ISO strings (seconds, integer) — mirrors Python exactly
  let durationS = 0;
  try {
    const fmt = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$/;
    const parseZ = (s: string): number => {
      const m = s.match(fmt);
      if (!m) throw new Error("bad ts");
      return Date.UTC(
        parseInt(m[1]!), parseInt(m[2]!) - 1, parseInt(m[3]!),
        parseInt(m[4]!), parseInt(m[5]!), parseInt(m[6]!)
      );
    };
    durationS = Math.trunc((parseZ(finishedAt) - parseZ(startedAt)) / 1000);
  } catch {
    durationS = 0;
  }

  // Read last_stderr_lines (last 20 lines, truncated to 4KB total)
  let lastStderrLines: string[] = [];
  if (opts.stderr) {
    const stderrPath = opts.stderr;
    if (existsSync(stderrPath)) {
      try {
        let raw = readFileSync(stderrPath, "utf-8");
        // Truncate to 4KB before splitting
        if (raw.length > 4096) raw = raw.slice(-4096);
        lastStderrLines = raw
          .split("\n")
          .filter((l) => l.length > 0)
          .slice(-20);
      } catch {
        // ignore
      }
    }
  }

  data.finished_at = finishedAt;
  data.exit_code = opts.exit;
  data.duration_s = durationS;
  data.last_stderr_lines = lastStderrLines;

  // Atomic write via temp file + rename
  const rnd = randomBytes(4).toString("hex");
  const parentDir = runFile.replace(/\/[^/]+$/, "");
  const tmpPath = join(parentDir, `.tmp-${rnd}`);
  try {
    writeFileSync(tmpPath, JSON.stringify(data, null, 2) + "\n");
    renameSync(tmpPath, runFile);
  } catch (e) {
    try { unlinkSync(tmpPath); } catch { /* ignore */ }
    process.stderr.write(`loop_runs finish: write failed: ${String(e)}\n`);
    return 1;
  }

  // Prune oldest files, keeping last 1000
  prune(parentDir, 1000);
  return 0;
}

// ---------------------------------------------------------------------------
// Read side: tail (mirrors Python cmd_tail)
// ---------------------------------------------------------------------------

/**
 * Print recent loop runs as a one-line-per-run table.
 * Mirrors Python loop_runs.cmd_tail() exactly.
 * Returns 0 always.
 */
export function cmdTail(opts: {
  n?: number;
  failuresOnly?: boolean;
  repoRoot?: string;
}): number {
  const n = opts.n ?? 10;
  const failuresOnly = opts.failuresOnly ?? false;
  const dir = runsDir(opts.repoRoot);

  let files: string[];
  try {
    files = readdirSync(dir)
      .filter((f) => f.endsWith(".json"))
      .sort();
  } catch {
    files = [];
  }

  if (files.length === 0) {
    console.log("no loop runs recorded yet");
    return 0;
  }

  // Parse files — newest last in sorted order, so take tail
  const rows: LoopRunStub[] = [];
  for (const filename of files) {
    try {
      const data = JSON.parse(readFileSync(join(dir, filename), "utf-8")) as LoopRunStub;
      // Skip stubs (not yet finished)
      if (data.exit_code === null || data.exit_code === undefined) continue;
      rows.push(data);
    } catch {
      continue;
    }
  }

  let filtered = failuresOnly ? rows.filter((r) => r.exit_code !== 0) : rows;

  // Take last n rows
  filtered = filtered.slice(-n);

  if (filtered.length === 0) {
    if (failuresOnly) {
      console.log("no failed loop runs recorded yet");
    } else {
      console.log("no loop runs recorded yet");
    }
    return 0;
  }

  // Print table: timestamp  exit  duration_s  brief_reason
  const header = `${"timestamp".padEnd(25)} ${"exit".padStart(4)} ${"duration_s".padStart(10)}  last_stderr`;
  console.log(header);
  console.log("-".repeat(70));
  for (const r of filtered) {
    // Trim trailing Z, keep 19 chars: mirrors Python ts[:19]
    let ts = (r.started_at ?? "?").slice(0, 19);
    if (ts.endsWith("T")) ts = ts.slice(0, -1);
    const exitCode = r.exit_code ?? "?";
    const dur = r.duration_s ?? "?";
    const stderrLines = r.last_stderr_lines ?? [];
    let reason = "";
    for (const line of stderrLines) {
      const stripped = line.trim();
      if (stripped) {
        reason = stripped.slice(0, 60);
        break;
      }
    }
    console.log(
      `${ts.padEnd(25)} ${String(exitCode).padStart(4)} ${String(dur).padStart(10)}  ${reason}`
    );
  }

  return 0;
}

// ---------------------------------------------------------------------------
// Latest-failure path helper (mirrors Python latest_failing_run_path)
// ---------------------------------------------------------------------------

/**
 * Return path of the most recent loop-run JSON with non-zero exit_code, or null.
 * Mirrors backend/loop_runs.latest_failing_run_path() exactly.
 */
export function latestFailingRunPath(repoRootOverride?: string): string | null {
  const dir = runsDir(repoRootOverride);
  let files: string[];
  try {
    files = readdirSync(dir)
      .filter((f) => f.endsWith(".json"))
      .sort()
      .reverse();
  } catch {
    return null;
  }
  for (const filename of files) {
    const fullPath = join(dir, filename);
    try {
      const data = JSON.parse(readFileSync(fullPath, "utf-8")) as LoopRunStub;
      const ec = data.exit_code;
      if (ec !== null && ec !== undefined && ec !== 0) return fullPath;
    } catch {
      continue;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

interface ParsedArgs {
  command: string | null;
  flags: Record<string, string | boolean>;
}

function parseCliArgs(argv: string[]): ParsedArgs {
  const flags: Record<string, string | boolean> = {};
  let command: string | null = null;
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i]!;
    if (command === null && !arg.startsWith("--")) {
      command = arg;
      i++;
    } else if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        flags[key] = next;
        i += 2;
      } else {
        flags[key] = true;
        i++;
      }
    } else {
      i++;
    }
  }
  return { command, flags };
}

async function main(argv?: string[]): Promise<number> {
  const rawArgs = argv ?? process.argv.slice(2);
  const { command, flags } = parseCliArgs(rawArgs);

  if (command === "start") {
    cmdStart();
    return 0;
  }

  if (command === "finish") {
    const file = flags["file"];
    const exitCodeRaw = flags["exit"];
    const stderrPath = flags["stderr"];

    if (!file || typeof file !== "string") {
      process.stderr.write("finish: --file is required\n");
      return 1;
    }
    if (exitCodeRaw === undefined || exitCodeRaw === true) {
      process.stderr.write("finish: --exit is required\n");
      return 1;
    }
    const exitCode = parseInt(String(exitCodeRaw), 10);
    if (isNaN(exitCode)) {
      process.stderr.write("finish: --exit must be an integer\n");
      return 1;
    }

    return cmdFinish({
      file,
      exit: exitCode,
      stderr: typeof stderrPath === "string" ? stderrPath : undefined,
    });
  }

  if (command === "tail") {
    const nRaw = flags["n"];
    const n = nRaw !== undefined && typeof nRaw === "string" ? parseInt(nRaw, 10) : 10;
    const failuresOnly = flags["failures-only"] === true;
    return cmdTail({ n, failuresOnly });
  }

  process.stderr.write(
    "loop_runs.py — Loop iteration exit-code recorder and tail CLI\n\n" +
    "  start                         Write a new loop-run stub, print its path\n" +
    "  finish --file F --exit N [--stderr PATH]\n" +
    "                                Finalise a loop-run file\n" +
    "  tail [--n N] [--failures-only]\n" +
    "                                Print recent loop runs\n"
  );
  return 1;
}

// Run as CLI when invoked directly
if (import.meta.main) {
  main().then((code) => process.exit(code));
}
