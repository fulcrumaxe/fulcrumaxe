/**
 * Differential fuzz harness: TS fnmatch port vs. Python fnmatch ground truth.
 *
 * Python ground truth is pre-computed by tests/rbac/generate-ground-truth.py
 * and stored in tests/rbac/ground-truth.json. This file is checked in so the
 * test is hermetic (no python3 runtime required at test time).
 *
 * Also tests minimatch and picomatch to document their divergence counts.
 */

import { describe, it, expect } from "bun:test";
import { fnmatch } from "../../src/rbac/fnmatch.js";
import { minimatch } from "minimatch";
import picomatch from "picomatch";
import groundTruthData from "./ground-truth.json";

interface TestCase {
  pattern: string;
  path: string;
  expected: boolean;
}

const corpus = groundTruthData as TestCase[];

// ---------------------------------------------------------------------------
// Primary test: our fnmatch.translate port must have ZERO divergences.
// ---------------------------------------------------------------------------

describe("fnmatch port — zero divergence from Python ground truth", () => {
  it(`passes all ${corpus.length} cases`, () => {
    const failures: string[] = [];
    for (const { pattern, path, expected } of corpus) {
      const result = fnmatch(path, pattern);
      if (result !== expected) {
        failures.push(
          `fnmatch(${JSON.stringify(path)}, ${JSON.stringify(pattern)}) = ${result}, expected ${expected}`,
        );
      }
    }
    if (failures.length > 0) {
      console.error(`\n=== DIVERGENCES (${failures.length}/${corpus.length}) ===`);
      for (const f of failures.slice(0, 30)) console.error(f);
      if (failures.length > 30) console.error(`... and ${failures.length - 30} more`);
    }
    expect(failures.length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Spec-required edge cases — explicit named tests for reviewability.
// ---------------------------------------------------------------------------

describe("fnmatch — spec-required edge cases", () => {
  it("* crosses / (Python fnmatch key semantic)", () => {
    expect(fnmatch("/agents/x/y/z", "/agents/*")).toBe(true);
    expect(fnmatch("/agents/x", "/agents/*")).toBe(true);
    expect(fnmatch("/agents/", "/agents/*")).toBe(true);
    expect(fnmatch("/agents", "/agents/*")).toBe(false);
    expect(fnmatch("/a/b/c/d/e", "/a/b/*")).toBe(true);
  });

  it("bare * matches everything including slashes and empty string", () => {
    expect(fnmatch("/agents/x/y/z", "*")).toBe(true);
    expect(fnmatch("", "*")).toBe(true);
    expect(fnmatch("/", "*")).toBe(true);
    expect(fnmatch("GET /anything", "*")).toBe(true);
  });

  it("METHOD-prefix rules from rbac.py", () => {
    // "GET *" — any GET path
    expect(fnmatch("GET /agents/x", "GET *")).toBe(true);
    expect(fnmatch("GET /", "GET *")).toBe(true);
    expect(fnmatch("POST /x", "GET *")).toBe(false);

    // "GET /agents/*"
    expect(fnmatch("/agents/x", "/agents/*")).toBe(true);
    expect(fnmatch("/agents/x/y", "/agents/*")).toBe(true);
    expect(fnmatch("/agents", "/agents/*")).toBe(false);
  });

  it("? matches exactly one character (any char)", () => {
    expect(fnmatch("/a/b/c", "/a/?/c")).toBe(true);
    expect(fnmatch("/a/bb/c", "/a/?/c")).toBe(false);
    // ? in Python fnmatch translates to regex `.` with DOTALL
    // /a/?/c is a 6-char pattern; /a//c is only 5 chars — no match.
    expect(fnmatch("/a//c", "/a/?/c")).toBe(false); // Python: False
    expect(fnmatch("a", "?")).toBe(true);
    expect(fnmatch("ab", "?")).toBe(false);
    expect(fnmatch("", "?")).toBe(false);
  });

  it("[seq] character classes", () => {
    expect(fnmatch("a", "[abc]")).toBe(true);
    expect(fnmatch("d", "[abc]")).toBe(false);
    expect(fnmatch("a/x", "[abc]/*")).toBe(true);
    expect(fnmatch("d/x", "[abc]/*")).toBe(false);
  });

  it("[!seq] negated character classes", () => {
    expect(fnmatch("d", "[!abc]")).toBe(true);
    expect(fnmatch("a", "[!abc]")).toBe(false);
    expect(fnmatch("d/x", "[!abc]/*")).toBe(true);
    expect(fnmatch("a/x", "[!abc]/*")).toBe(false);
  });

  it("[a-z] character ranges", () => {
    expect(fnmatch("b", "[a-z]")).toBe(true);
    expect(fnmatch("B", "[a-z]")).toBe(false);
    expect(fnmatch("B", "[A-Z]")).toBe(true);
    expect(fnmatch("X", "[a-zA-Z]")).toBe(true);
    expect(fnmatch("1", "[a-zA-Z]")).toBe(false);
  });

  it("[!a-z] negated ranges", () => {
    expect(fnmatch("B", "[!a-z]")).toBe(true);
    expect(fnmatch("b", "[!a-z]")).toBe(false);
    expect(fnmatch("5", "[!a-z]")).toBe(true);
  });

  it("dotfiles / leading dot — * matches them", () => {
    expect(fnmatch(".hidden", "*")).toBe(true);
    expect(fnmatch(".hidden.py", "*.py")).toBe(true);
    expect(fnmatch(".hidden", ".*")).toBe(true);
    expect(fnmatch("not-hidden", ".*")).toBe(false);
  });

  it("empty pattern and empty path", () => {
    expect(fnmatch("", "")).toBe(true);
    expect(fnmatch("x", "")).toBe(false);
  });

  it("trailing slash edge cases", () => {
    expect(fnmatch("/agents/", "/agents/*")).toBe(true);
    expect(fnmatch("/agents", "/agents/*")).toBe(false);
  });

  it("case sensitivity (Linux — same as Python fnmatch.fnmatch)", () => {
    expect(fnmatch("GET /health", "GET /health")).toBe(true);
    expect(fnmatch("get /health", "GET /health")).toBe(false);
    expect(fnmatch("GET /HEALTH", "GET /health")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Documentary tests: divergence counts for minimatch and picomatch.
// These are expected to fail — they document WHY we didn't use these libs.
// ---------------------------------------------------------------------------

describe("minimatch divergence count (documentary)", () => {
  it("reports actual divergence count vs Python ground truth", () => {
    let divergences = 0;
    const examples: string[] = [];
    for (const { pattern, path, expected } of corpus) {
      const result = minimatch(path, pattern);
      if (result !== expected) {
        divergences++;
        if (examples.length < 5) {
          examples.push(
            `minimatch(${JSON.stringify(path)}, ${JSON.stringify(pattern)}) = ${result}, Python says ${expected}`,
          );
        }
      }
    }
    console.log(`\nminimatch divergences: ${divergences}/${corpus.length}`);
    for (const ex of examples) console.log("  " + ex);
    // Divergences are expected — this test documents the count, not asserts zero.
    // The actual value is recorded in FINDINGS.md.
    expect(divergences).toBeGreaterThan(0);
  });
});

describe("picomatch divergence count (documentary)", () => {
  it("reports actual divergence count vs Python ground truth", () => {
    let divergences = 0;
    let errors = 0;
    const examples: string[] = [];
    for (const { pattern, path, expected } of corpus) {
      // picomatch throws on empty pattern string — treat as false.
      let result: boolean;
      try {
        const isMatch = picomatch(pattern);
        result = isMatch(path);
      } catch {
        result = false;
        errors++;
      }
      if (result !== expected) {
        divergences++;
        if (examples.length < 5) {
          examples.push(
            `picomatch(${JSON.stringify(path)}, ${JSON.stringify(pattern)}) = ${result}, Python says ${expected}`,
          );
        }
      }
    }
    console.log(`\npicomatch divergences: ${divergences}/${corpus.length} (${errors} threw)`);
    for (const ex of examples) console.log("  " + ex);
    expect(divergences).toBeGreaterThan(0);
  });
});
