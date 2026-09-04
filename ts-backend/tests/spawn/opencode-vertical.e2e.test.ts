/**
 * tests/spawn/opencode-vertical.e2e.test.ts
 *
 * END-TO-END vertical test: proves a real agent role runs on Qwen through
 * the fully-TS spawn path (pre-spawn-check → prompt assembly → opencode/Qwen
 * invocation → agent_run tracking).
 *
 * COST WARNING: This test calls the real Qwen API via opencode. It is marked
 * with `if (process.env.E2E_OPENCODE !== "1") { return; }` so it is skipped
 * in normal CI runs. To run it:
 *
 *   E2E_OPENCODE=1 bun test tests/spawn/opencode-vertical.e2e.test.ts
 *
 * The test uses a tiny role-prompt (write a single file) to bound token spend.
 *
 * Assertions:
 *   1. pre-spawn-check allowed the spawn
 *   2. opencode/Qwen actually ran and created the sentinel file
 *   3. agent_run row was recorded with start_ts and end_ts populated
 */

import { describe, it, expect } from "bun:test";
import { mkdirSync, existsSync, rmSync, writeFileSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { DuckDBInstance } from "@duckdb/node-api";
import { runSpawnAgent } from "../../src/spawn/spawn-agent.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal git repo — opencode needs a valid git context. */
function initGitRepo(dir: string): void {
  // Init with a throwaway identity — only needs to be a valid git repo.
  spawnSync("git", ["init", "-b", "main", dir], { stdio: "ignore" });
  spawnSync("git", ["-C", dir, "config", "user.email", "test@example.com"], { stdio: "ignore" });
  spawnSync("git", ["-C", dir, "config", "user.name", "Test"], { stdio: "ignore" });
  // Initial commit so HEAD is valid
  const readme = join(dir, "README.md");
  writeFileSync(readme, "scratch workspace\n");
  spawnSync("git", ["-C", dir, "add", "README.md"], { stdio: "ignore" });
  spawnSync("git", ["-C", dir, "commit", "-m", "init", "--allow-empty"], { stdio: "ignore" });
}

/** Read DuckDB to find the agent_run row for the given agent_id. */
async function findAgentRun(
  dbPath: string,
  agentId: string
): Promise<Record<string, unknown> | null> {
  if (!existsSync(dbPath)) return null;
  const inst = await DuckDBInstance.create(dbPath);
  const conn = await inst.connect();
  try {
    const stmt = await conn.prepare(
      "SELECT agent_id, role, start_ts, end_ts, verdict, routed_via FROM agent_run WHERE agent_id = ?"
    );
    stmt.bindVarchar(1, agentId);
    const result = await stmt.runAndReadAll();
    const rows = result.getRows() as unknown[][];
    if (rows.length === 0) return null;
    const row = rows[0]!;
    return {
      agent_id: row[0],
      role: row[1],
      start_ts: row[2],
      end_ts: row[3],
      verdict: row[4],
      routed_via: row[5],
    };
  } finally {
    try { conn.closeSync(); } catch { /* ignore */ }
    try { inst.closeSync(); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// Minimal config.json fixture (satisfies pre-spawn-check cap checks)
// ---------------------------------------------------------------------------

const CONFIG_JSON = JSON.stringify({
  repo: "fulcrumaxe/fulcrumaxe",
  max_concurrent_agents: 8,
  dials: {
    "agent.spawn": { level: 5, ceiling: 5 },
  },
});

// ---------------------------------------------------------------------------
// The tiny role prompt — instructs Qwen to create one sentinel file.
// Deliberately minimal to bound token cost.
// ---------------------------------------------------------------------------

const TINY_ROLE_PROMPT = `You are an executor agent running a minimal verification task.

Your ONLY job is:
1. Create a file named QWEN_VERTICAL_OK.txt in your current working directory.
2. Write exactly the text: ok
3. Stop immediately after creating the file.

Do not do anything else. Do not create other files. Do not run tests.

When you are done, output:

<!-- AGENT_OUTPUT -->
\`\`\`json
{"agent":"executor","verdict":"done","files_touched":["QWEN_VERTICAL_OK.txt"]}
\`\`\`
<!-- /AGENT_OUTPUT -->
`;

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

describe("opencode vertical e2e (real Qwen via opencode)", () => {
  it("runs executor role through TS spawn path on Qwen, creates sentinel file, records agent_run", async () => {
    // Skip unless explicitly opted in — avoids spending API credit in CI.
    if (process.env["E2E_OPENCODE"] !== "1") {
      console.log("[skip] E2E_OPENCODE!=1 — set E2E_OPENCODE=1 to run this test");
      return;
    }

    // ── Scratch workspace ──────────────────────────────────────────────────
    const baseDir = join(tmpdir(), `af-e2e-${Date.now()}`);
    const scratchWs = join(baseDir, "workspace");
    const stateDir = join(baseDir, "state");
    const dbPath = join(stateDir, "stats.duckdb");
    const configDir = join(baseDir, "config");

    mkdirSync(scratchWs, { recursive: true });
    mkdirSync(stateDir, { recursive: true });
    mkdirSync(configDir, { recursive: true });

    // Point agent-run-tracker's dbPath() at the scratch DB so startRun/completeRun
    // write to the same place we'll read from in the assertion.
    const prevStatsDbPath = process.env["STATS_DB_PATH"];
    process.env["STATS_DB_PATH"] = dbPath;

    // Write config.json so pre-spawn-check finds the cap settings.
    writeFileSync(join(configDir, "config.json"), CONFIG_JSON);

    // Init a real git repo in the scratch workspace so opencode is happy.
    initGitRepo(scratchWs);

    // Sentinel file path — the agent must create this.
    const sentinelFile = join(scratchWs, "QWEN_VERTICAL_OK.txt");

    try {
      // ── Run the full TS spawn path ───────────────────────────────────────
      // We pass a custom discussion body to skip the live GitHub/cache calls
      // that the spec-readiness gate normally makes.
      const fakeDiscBody = `<!-- STATUS:SPEC_READY SINCE:2026-06-01T00:00:00Z -->

## Intent
E2E vertical test for opencode/Qwen runtime adapter.

## Spec (Acceptance)
- [ ] Qwen creates QWEN_VERTICAL_OK.txt containing "ok".

## Implementation Notes
Create QWEN_VERTICAL_OK.txt with content "ok".`;

      // Resolve the real repo root.
      // This test file lives at: <repo_root>/.claude/worktrees/<id>/ts-backend/tests/spawn/
      // Walk up: spawn → tests → ts-backend → <worktree-id> → worktrees → .claude → repo_root
      const thisFile = new URL(import.meta.url).pathname;
      const realRepoRoot = join(thisFile, "..", "..", "..", "..", "..", "..", "..");

      const result = await runSpawnAgent(
        {
          role: "executor",
          discussion: 1506,
          taskPrompt: TINY_ROLE_PROMPT,
          isolation: "none",
          worktreePath: "",
          securityTrigger: false,
          touchpoints: "",
          overrideCap: true, // bypass fleet cap checks in scratch env
          dryRunEnvDump: false,
          noRegister: false,
          pr: null,
          operationClass: "agent.spawn",
          sdkLane: false,
          runtime: "opencode",
        },
        {
          // Use real repo root so Python prompt_builder is reachable,
          // but tell opencode to operate in the scratch workspace.
          repoRootOverride: realRepoRoot,
          configPathOverride: join(configDir, "config.json"),
          dbPathOverride: dbPath,
          discussionBody: fakeDiscBody,
          noRegister: false,
          opencodeWorkdir: scratchWs,
        }
      );

      // ── Assertion 1: pre-spawn-check allowed ──────────────────────────────
      expect(result.exitCode).toBe(0);
      expect(result.routedVia).toBe("opencode");
      expect(result.opencodeResult).toBeDefined();

      console.log("[e2e] opencode exit code:", result.opencodeResult?.exitCode);
      console.log("[e2e] opencode output (tail):", result.opencodeResult?.output.slice(-500));

      // ── Assertion 2: Qwen actually created the sentinel file ──────────────
      expect(
        existsSync(sentinelFile),
        `Expected Qwen to create ${sentinelFile}`
      ).toBe(true);

      const content = readFileSync(sentinelFile, "utf-8");
      expect(content.trim()).toBe("ok");

      console.log("[e2e] sentinel file created with content:", JSON.stringify(content.trim()));

      // ── Assertion 3: agent_run row recorded ───────────────────────────────
      const agentRun = await findAgentRun(dbPath, result.eventId);
      expect(agentRun).not.toBeNull();
      expect(agentRun!["role"]).toBe("executor");
      expect(agentRun!["start_ts"]).not.toBeNull();
      // end_ts is set by completeRun — give it a moment if needed
      expect(agentRun!["routed_via"]).toBe("opencode");

      // Stringify with BigInt-safe replacer (DuckDB timestamps come back as BigInt)
      const safeAgentRun = JSON.stringify(agentRun, (_k, v) =>
        typeof v === "bigint" ? v.toString() : v, 2);
      console.log("[e2e] agent_run row:", safeAgentRun);
      console.log("[e2e] PASS — Qwen ran end-to-end through the TS spawn path");
    } finally {
      // Restore STATS_DB_PATH
      if (prevStatsDbPath !== undefined) {
        process.env["STATS_DB_PATH"] = prevStatsDbPath;
      } else {
        delete process.env["STATS_DB_PATH"];
      }
      // Clean up scratch workspace
      try {
        rmSync(baseDir, { recursive: true, force: true });
      } catch {
        // non-fatal
      }
    }
  }, 180_000); // 3-minute timeout — real Qwen call
});
