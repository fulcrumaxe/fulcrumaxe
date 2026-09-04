/**
 * tests/config/no-hand-rolled-repo-root-walks.test.ts
 *
 * Guard test (D#1825): ratchets the count of hand-rolled ".." checkout-path
 * walks in ts-backend/src down to zero over time, and never lets it rise.
 *
 * A "hand-rolled walk" here means a `join(...)`/`resolve(...)` call whose
 * arguments contain two or more literal ".." segments, anchored (inline, or
 * via a local variable assigned nearby) on `import.meta.url` — the same
 * `new URL(import.meta.url).pathname` / `fileURLToPath(import.meta.url)` /
 * `dirname(...)` of either shape that D#1825 found wrong at six sites.
 *
 * Baseline file: tests/config/repo-root-walk-baseline.txt — one
 * `<path-relative-to-src> <count>` line per file that currently has one or
 * more such walks, plus `#`-prefixed comments. A file's actual count MUST
 * equal its baseline entry:
 *   - actual > baseline  → a new hand-rolled walk was introduced (or an
 *     existing one grew). Fails, naming the file — this is the direction
 *     that must never silently pass.
 *   - actual < baseline  → a site was converted to config/repo-root.ts.
 *     Fails on purpose, instructing the committer to lower the baseline —
 *     this is what makes "converts opportunistically" enforceable rather
 *     than aspirational: the count can decrease, but only by an explicit,
 *     reviewed edit to this file, never silently.
 *   - a file with actual > 0 that isn't in the baseline at all is treated
 *     as baseline 0, so it fails the same way a fresh regression would.
 *
 * To regenerate after converting a site, from ts-backend/:
 *   DUMP_BASELINE=1 bun test tests/config/no-hand-rolled-repo-root-walks.test.ts
 * and paste the printed lines into repo-root-walk-baseline.txt.
 *
 * Run: bun test tests/config/no-hand-rolled-repo-root-walks.test.ts --timeout 60000
 */

import { describe, it, expect } from "bun:test";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const _TEST_FILE_DIR = dirname(fileURLToPath(import.meta.url));
const SRC_ROOT = resolve(_TEST_FILE_DIR, "..", "..", "src");
const BASELINE_PATH = resolve(_TEST_FILE_DIR, "repo-root-walk-baseline.txt");

// ---------------------------------------------------------------------------
// Scanner
// ---------------------------------------------------------------------------

function listTsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...listTsFiles(full));
    } else if (entry.isFile() && entry.name.endsWith(".ts")) {
      out.push(full);
    }
  }
  return out;
}

interface Call {
  args: string;
  startIdx: number;
}

/**
 * Extract every paren-balanced `join(...)`/`resolve(...)` call's argument
 * list from `src`. Paren-depth-aware so it handles nested calls correctly
 * (e.g. `join(new URL(import.meta.url).pathname, "..", "..")`), and skips
 * member-call spellings (`foo.join(`, `path.resolve(`) since every walk site
 * in this codebase uses the destructured, bare-identifier form.
 */
function extractCalls(src: string, fnName: "join" | "resolve"): Call[] {
  const calls: Call[] = [];
  const marker = fnName + "(";
  let searchFrom = 0;
  while (true) {
    const idx = src.indexOf(marker, searchFrom);
    if (idx === -1) break;
    const before = idx > 0 ? src[idx - 1] : "";
    if (before !== undefined && /[A-Za-z0-9_$.]/.test(before)) {
      searchFrom = idx + marker.length;
      continue;
    }
    const argsStart = idx + marker.length;
    let depth = 1;
    let i = argsStart;
    while (i < src.length && depth > 0) {
      if (src[i] === "(") depth++;
      else if (src[i] === ")") depth--;
      i++;
    }
    calls.push({ args: src.slice(argsStart, i - 1), startIdx: idx });
    searchFrom = i;
  }
  return calls;
}

const ANCHOR_RE = /import\.meta\.url/;
const DOTDOT_RE = /(['"])\.\.\1/g;

// How far back from a join()/resolve() call to look for its anchor. Covers
// the "const here = new URL(import.meta.url).pathname; ... join(here, ...)"
// two-statement idiom used throughout this codebase, without reaching far
// enough to pick up an unrelated import.meta.url reference elsewhere in a
// large function.
const ANCHOR_WINDOW_CHARS = 400;

function countHandRolledWalks(filePath: string): number {
  const src = readFileSync(filePath, "utf-8");
  let count = 0;
  for (const fn of ["join", "resolve"] as const) {
    for (const call of extractCalls(src, fn)) {
      const dotdotCount = (call.args.match(DOTDOT_RE) ?? []).length;
      if (dotdotCount < 2) continue;
      const windowStart = Math.max(0, call.startIdx - ANCHOR_WINDOW_CHARS);
      const window = src.slice(windowStart, call.startIdx) + call.args;
      if (ANCHOR_RE.test(window)) count++;
    }
  }
  return count;
}

function scanTree(): Map<string, number> {
  const results = new Map<string, number>();
  for (const file of listTsFiles(SRC_ROOT)) {
    const count = countHandRolledWalks(file);
    if (count > 0) {
      results.set(relative(SRC_ROOT, file), count);
    }
  }
  return results;
}

// ---------------------------------------------------------------------------
// Baseline
// ---------------------------------------------------------------------------

function loadBaseline(): Map<string, number> {
  const baseline = new Map<string, number>();
  const text = readFileSync(BASELINE_PATH, "utf-8");
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const match = line.match(/^(\S+)\s+(\d+)/);
    if (!match) continue;
    baseline.set(match[1] as string, Number(match[2]));
  }
  return baseline;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

const actual = scanTree();

if (process.env["DUMP_BASELINE"] === "1") {
  const lines = [...actual.entries()].sort(([a], [b]) => a.localeCompare(b));
  for (const [file, count] of lines) {
    console.log(`${file} ${count}`);
  }
}

describe("no-hand-rolled-repo-root-walks (baseline ratchet)", () => {
  const baseline = loadBaseline();
  const allFiles = new Set([...baseline.keys(), ...actual.keys()]);

  for (const file of [...allFiles].sort()) {
    it(`${file}: hand-rolled walk count matches baseline`, () => {
      const expected = baseline.get(file) ?? 0;
      const found = actual.get(file) ?? 0;
      if (found > expected) {
        throw new Error(
          `${file}: found ${found} hand-rolled repo-root walk(s), baseline allows ${expected}. ` +
            `A new hand-rolled ".." walk was introduced — use config/repo-root.ts instead ` +
            `(repoRoot() / mainRepoRoot()).`
        );
      }
      if (found < expected) {
        throw new Error(
          `${file}: found ${found} hand-rolled repo-root walk(s), baseline says ${expected}. ` +
            `A site was converted — lower this file's entry in ` +
            `tests/config/repo-root-walk-baseline.txt to ${found} to lock in the improvement.`
        );
      }
      expect(found).toBe(expected);
    });
  }

  it("config/repo-root.ts's own walk is the only entry with no conversion target", () => {
    // Sanity check that the baseline file documents the one exempt site
    // rather than silently omitting it.
    const baselineText = readFileSync(BASELINE_PATH, "utf-8");
    expect(baselineText).toContain("config/repo-root.ts");
  });
});
