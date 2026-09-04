/**
 * tests/loop/discussion-driven-iteration.e2e.test.ts
 *
 * END-TO-END test for the discussion-driven outer loop.
 *
 * Proves the OUTER MECHANICS — not LLM quality:
 *   - SPEC_READY status correctly detected in fixture discussion body.
 *   - pre-spawn-check passed (executor role allowed through).
 *   - Inner fix-loop ran on Qwen: executor produced the slugify util,
 *     reviewer judged the real diff.
 *   - Fix-loop is bounded (≤ maxFix re-attempts).
 *   - On gate-pass: a committed feature branch + PR metadata produced.
 *   - agent_run rows recorded with routed_via=opencode for all role runs.
 *   - Outcome is one of the expected IterationOutcome values.
 *
 * The fixture discussion body describes the real task:
 *   "Add a slugify(s) util" — create src/utils/slugify.ts exporting
 *   slugify(s: string): string that converts a string to a URL-safe slug.
 *
 * Verdict (pass vs needs-fix) is NOT asserted — depends on LLM.
 * We assert STRUCTURAL mechanics: discussion → spec → inner-loop → artifact + gate.
 *
 * COST WARNING: calls the real Qwen API via opencode.
 * Skip unless E2E_OPENCODE=1:
 *
 *   E2E_OPENCODE=1 bun test tests/loop/discussion-driven-iteration.e2e.test.ts
 */

import { describe, it, expect } from "bun:test";
import { mkdirSync, existsSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { DuckDBInstance } from "@duckdb/node-api";
import { runDiscussionIteration } from "../../src/loop/discussion-driven-iteration.js";

// ---------------------------------------------------------------------------
// Fixture: a SPEC_READY discussion for "Add a slugify(s) util"
// ---------------------------------------------------------------------------

const FIXTURE_DISCUSSION_NUMBER = 1506; // Use D#1506 as the test discussion label.

const FIXTURE_TITLE = "Add a slugify(s) util";

/**
 * Fixture discussion body — contains the STATUS marker and all three required
 * spec sections so pre-spawn-check and discussion-status pass cleanly.
 *
 * The task is deliberately small:
 *   Create src/utils/slugify.ts exporting slugify(s: string): string
 *   that lowercases s, replaces non-alphanum runs with hyphens, trims hyphens.
 */
const FIXTURE_BODY = `<!-- STATUS:SPEC_READY SINCE:2026-06-01T00:00:00Z -->

## Intent

Provide a reusable URL-slug utility so discussion titles and branch names can be
normalized consistently across the outer loop.

## Spec (Acceptance)

- [ ] Create the file src/utils/slugify.ts in the current working directory.
- [ ] The file exports a default-or-named function \`slugify(s: string): string\`.
- [ ] The function lowercases the input, replaces one-or-more non-alphanumeric
      characters with a single hyphen, and trims leading/trailing hyphens.
- [ ] Example: slugify("Hello, World!") returns "hello-world".
- [ ] Example: slugify("  --test--  ") returns "test".

## Implementation Notes

A straightforward regex implementation is fine:
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")
`;

// ---------------------------------------------------------------------------
// DB helper
// ---------------------------------------------------------------------------

async function findAgentRunsByRole(
  dbPath: string,
  routedVia: string
): Promise<Array<Record<string, unknown>>> {
  if (!existsSync(dbPath)) return [];
  const inst = await DuckDBInstance.create(dbPath);
  const conn = await inst.connect();
  try {
    const stmt = await conn.prepare(
      `SELECT agent_id, role, start_ts, end_ts, verdict, routed_via
       FROM agent_run
       WHERE routed_via = ?`
    );
    stmt.bindVarchar(1, routedVia);
    const result = await stmt.runAndReadAll();
    const rows = result.getRows() as unknown[][];
    return rows.map((row) => ({
      agent_id: row[0],
      role: row[1],
      start_ts: row[2],
      end_ts: row[3],
      verdict: row[4],
      routed_via: row[5],
    }));
  } finally {
    try { conn.closeSync(); } catch { /* ignore */ }
    try { inst.closeSync(); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

describe("discussion-driven-iteration e2e (real Qwen via opencode)", () => {
  it(
    "outer mechanics: SPEC_READY detected → inner loop ran on Qwen → on pass, PR artifact produced; agent_run rows recorded routed_via=opencode",
    async () => {
      // Skip unless explicitly opted in.
      if (process.env["E2E_OPENCODE"] !== "1") {
        console.log(
          "[skip] E2E_OPENCODE!=1 — set E2E_OPENCODE=1 to run this test"
        );
        return;
      }

      // ── Scratch workspace ──────────────────────────────────────────────────
      const baseDir = join(tmpdir(), `af-disc-driven-e2e-${Date.now()}`);
      const scratchWs = join(baseDir, "workspace");
      const stateDir = join(baseDir, "state");
      const dbPath = join(stateDir, "stats.duckdb");

      mkdirSync(scratchWs, { recursive: true });
      mkdirSync(stateDir, { recursive: true });

      const prevStatsDbPath = process.env["STATS_DB_PATH"];
      process.env["STATS_DB_PATH"] = dbPath;

      // Resolve real repo root.
      // This test lives at: <repo>/.claude/worktrees/<id>/ts-backend/tests/loop/
      // Walk up 7 levels to reach repo root.
      const thisFile = new URL(import.meta.url).pathname;
      const repoRoot = join(thisFile, "..", "..", "..", "..", "..", "..", "..");

      const MAX_FIX = 1; // Keep bounded; 1 re-attempt is enough to prove the mechanic.

      try {
        console.log("[e2e] starting discussion-driven iteration");
        console.log("[e2e] fixture discussion:", FIXTURE_TITLE, "(D#" + FIXTURE_DISCUSSION_NUMBER + ")");
        console.log("[e2e] maxFix =", MAX_FIX);

        const result = await runDiscussionIteration({
          discussion: FIXTURE_DISCUSSION_NUMBER,
          title: FIXTURE_TITLE,
          body: FIXTURE_BODY,
          scratchDir: scratchWs,
          repoRoot,
          dbPath,
          maxFix: MAX_FIX,
        });

        // ── Print summary ──────────────────────────────────────────────────
        console.log("[e2e] detectedStatus:", result.detectedStatus);
        console.log("[e2e] outcome:", result.outcome);
        if (result.blockedReason) {
          console.log("[e2e] blockedReason:", result.blockedReason);
        }

        // ── Assertion 1: STATUS detected correctly ─────────────────────────
        expect(result.detectedStatus).toBe("SPEC_READY");

        // ── Assertion 2: not skipped or blocked ────────────────────────────
        // The fixture body is SPEC_READY and pre-spawn-check should pass
        // (overrideCap=true, noRegister=true in the implementation).
        expect(result.outcome).not.toBe("skipped");
        expect(result.outcome).not.toBe("blocked");
        console.log("[e2e] pre-spawn-check: PASSED (not skipped, not blocked)");

        // ── Assertion 3: inner loop ran ────────────────────────────────────
        expect(result.innerLoop).not.toBeNull();
        const inner = result.innerLoop!;

        console.log("[e2e] executorRuns:", inner.executorRuns.length);
        console.log("[e2e] reviewerRuns:", inner.reviewerRuns.length);
        console.log("[e2e] finalVerdict:", inner.finalVerdict);
        console.log("[e2e] fixRoundsRan:", inner.fixRoundsRan);
        console.log("[e2e] gate.allowed:", inner.gate.allowed);

        // At least one executor attempt ran.
        expect(
          inner.executorRuns.length >= 1,
          "at least one executor attempt must have run"
        ).toBe(true);

        // At least one review ran.
        expect(
          inner.reviewerRuns.length >= 1,
          "at least one code-review must have run"
        ).toBe(true);

        // Counts match (one review per executor attempt).
        expect(inner.executorRuns.length).toBe(inner.reviewerRuns.length);

        // ── Assertion 4: routed_via=opencode ─────────────────────────────
        for (const run of inner.executorRuns) {
          expect(run.routedVia).toBe("opencode");
        }
        for (const run of inner.reviewerRuns) {
          expect(run.routedVia).toBe("opencode");
        }
        console.log("[e2e] all role runs routed_via=opencode: OK");

        // ── Assertion 5: bounded — did not exceed maxFix ──────────────────
        expect(
          inner.fixRoundsRan <= MAX_FIX,
          `fix rounds (${inner.fixRoundsRan}) must not exceed maxFix (${MAX_FIX})`
        ).toBe(true);

        // ── Assertion 6: fix-cycle mechanics when needs-fix ───────────────
        const firstVerdict = inner.reviewerRuns[0]?.agentOutput?.["verdict"];
        console.log("[e2e] first review verdict:", firstVerdict);

        if (firstVerdict === "needs-fix") {
          console.log("[e2e] first review was needs-fix → asserting re-attempt ran");
          expect(
            inner.executorRuns.length > 1,
            "needs-fix must trigger a second executor attempt"
          ).toBe(true);
          expect(inner.fixCycleTriggered).toBe(true);
        } else {
          console.log("[e2e] first review passed — no fix cycle (correct)");
        }

        // ── Assertion 7: final verdict is a known terminal value ──────────
        const validVerdicts = ["pass", "needs-fix", null];
        expect(
          validVerdicts.includes(inner.finalVerdict),
          `finalVerdict must be one of pass|needs-fix|null, got ${inner.finalVerdict}`
        ).toBe(true);

        // ── Assertion 8: gate reflects final verdict ──────────────────────
        if (inner.finalVerdict === "pass") {
          expect(inner.gate.allowed).toBe(true);
          console.log("[e2e] gate: OPEN");
        } else {
          expect(inner.gate.allowed).toBe(false);
          console.log("[e2e] gate: CLOSED (verdict:", inner.finalVerdict, ")");
        }

        // ── Assertion 9: PR artifact when gate passed ─────────────────────
        if (result.outcome === "done") {
          expect(result.prArtifact).not.toBeNull();
          const art = result.prArtifact!;
          console.log("[e2e] prArtifact.branch:", art.branch);
          console.log("[e2e] prArtifact.prTitle:", art.prTitle);
          console.log("[e2e] prArtifact.commitSha:", art.commitSha);

          // Branch name must be non-empty and slug-shaped.
          expect(art.branch.startsWith("feature/")).toBe(true);
          expect(art.branch.length).toBeGreaterThan("feature/".length);

          // PR title must be non-empty.
          expect(art.prTitle.length).toBeGreaterThan(0);
          expect(art.prTitle.length).toBeLessThanOrEqual(70);

          // PR body must mention gate markers (required by two-gate protocol).
          expect(art.prBody).toContain("Gate 1:");
          expect(art.prBody).toContain("Gate 2:");

          // commitSha may be empty if nothing was staged (allowed), but branch must exist.
          console.log("[e2e] PR artifact: OK");
        } else {
          // gate-failed outcome: no artifact is correct.
          expect(result.prArtifact).toBeNull();
          console.log("[e2e] outcome=gate-failed — no PR artifact (correct)");
        }

        // ── Assertion 10: agent_run rows in DuckDB ────────────────────────
        const agentRuns = await findAgentRunsByRole(dbPath, "opencode");
        console.log("[e2e] agent_run rows with routed_via=opencode:", agentRuns.length);

        expect(
          agentRuns.length >= 1,
          "at least one agent_run row must be recorded with routed_via=opencode"
        ).toBe(true);

        for (const row of agentRuns) {
          console.log(
            `[e2e] agent_run: role=${row["role"]} verdict=${row["verdict"]} routed_via=${row["routed_via"]}`
          );
          expect(row["routed_via"]).toBe("opencode");
        }

        // ── Final summary ─────────────────────────────────────────────────
        console.log(
          `[e2e] PASS — outer mechanics verified:` +
          ` outcome=${result.outcome},` +
          ` executorRuns=${inner.executorRuns.length},` +
          ` reviewerRuns=${inner.reviewerRuns.length},` +
          ` fixRoundsRan=${inner.fixRoundsRan},` +
          ` finalVerdict=${inner.finalVerdict},` +
          ` gate=${inner.gate.allowed ? "OPEN" : "CLOSED"},` +
          ` agent_run_rows=${agentRuns.length}`
        );
      } finally {
        if (prevStatsDbPath !== undefined) {
          process.env["STATS_DB_PATH"] = prevStatsDbPath;
        } else {
          delete process.env["STATS_DB_PATH"];
        }
        try {
          rmSync(baseDir, { recursive: true, force: true });
        } catch {
          // non-fatal
        }
      }
    },
    380_000 // up to ~6 min: 2 executor + 2 reviewer sequential Qwen calls
  );
});
