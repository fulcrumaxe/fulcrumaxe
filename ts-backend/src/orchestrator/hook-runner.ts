/**
 * orchestrator/hook-runner.ts — Lifecycle hook runner for SDK-routed agents.
 *
 * Mirrors backend/orchestrator/hook_runner.py 1:1.
 *
 * Calls existing hook scripts at agent lifecycle points, since Claude Code's
 * harness no longer drives them for SDK-routed agents.
 *
 * Hooks called:
 *   - Pre-spawn:  scripts/pre-spawn-check.sh  (optional, best-effort)
 *   - Post-agent: scripts/post-agent-hook.sh
 *
 * All hook calls are non-fatal: failures are logged to stderr and swallowed
 * so the agent lifecycle always completes.
 *
 * Programmatic exports:
 *   import { HookRunner } from "./hook-runner.js";
 *
 *   const hr = new HookRunner("<repo-root>");
 *   hr.preSpawn({ role: "docs-writer", discussion: 42 });
 *   hr.postAgent(result);
 */

import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { repoRoot as resolveCheckoutRoot } from "../config/repo-root.js";

// ---------------------------------------------------------------------------
// RunResult interface (mirrors sdk_runner.RunResult fields used by HookRunner)
// ---------------------------------------------------------------------------

export interface RunResult {
  agentId: string;
  verdict: string;
  role: string;
  discussion: number | null;
  pr: number | null;
  inputTokens: number;
  outputTokens: number;
  error: string | null;
}

// ---------------------------------------------------------------------------
// HookRunner class
// ---------------------------------------------------------------------------

/**
 * Calls lifecycle hook scripts for SDK-routed agent runs.
 *
 * @param repoRoot - Path to the main repository root (not the worktree).
 *   Defaults to the parent of the orchestrator/src directory (inferred at runtime).
 */
export class HookRunner {
  private readonly _root: string;

  constructor(repoRoot?: string) {
    if (repoRoot) {
      this._root = resolve(repoRoot);
    } else {
      // Delegates to config/repo-root.ts (D#1825) — wants repoRoot(), since
      // this feeds hook scripts (scripts/pre-spawn-check.sh,
      // scripts/post-agent-hook.sh) that live in the checkout this process
      // is running in.
      this._root = resolveCheckoutRoot();
    }
  }

  /**
   * Run a shell script as an argv array, returning true on success.
   *
   * Non-fatal: logs to stderr and returns false on failure or timeout.
   * Uses spawnSync with argv array only — no shell interpolation.
   */
  private _runScript(
    scriptPath: string,
    args: string[],
    env?: Record<string, string>
  ): boolean {
    if (!existsSync(scriptPath)) {
      process.stderr.write(
        `[hook-runner] Hook script not found, skipping: ${scriptPath}\n`
      );
      return false;
    }

    const mergedEnv: Record<string, string> = {};
    // Copy process.env (filter undefined values)
    for (const [k, v] of Object.entries(process.env)) {
      if (v !== undefined) mergedEnv[k] = v;
    }
    if (env) {
      Object.assign(mergedEnv, env);
    }

    const result = spawnSync("bash", [scriptPath, ...args], {
      encoding: "utf-8",
      timeout: 60_000,
      env: mergedEnv,
      cwd: this._root,
      stdio: ["pipe", "pipe", "pipe"],
    });

    if (result.signal === "SIGTERM" || (result.status === null && result.error)) {
      process.stderr.write(
        `[hook-runner] Hook script ${scriptPath} timed out or errored: ${result.error?.message ?? "unknown"}\n`
      );
      return false;
    }

    if (result.status !== 0) {
      const stderr = (result.stderr ?? "").slice(0, 500);
      process.stderr.write(
        `[hook-runner] Hook script ${scriptPath} exited ${result.status ?? "null"}: ${stderr}\n`
      );
      return false;
    }

    return true;
  }

  /**
   * Call post-agent-hook.sh after an SDK-routed run completes.
   *
   * Mirrors what scripts/subagent-stop-hook.sh does for Claude Code runs.
   * Non-fatal: hook failures do not affect the run result.
   *
   * @param result - The completed RunResult from the SDK runner.
   */
  postAgent(result: RunResult): void {
    const { agentId, verdict, role, discussion, pr } = result;

    const postAgentScript = join(this._root, "scripts", "post-agent-hook.sh");
    const args: string[] = [
      "--event-id", agentId,
      "--verdict", verdict,
      "--role", role,
    ];
    if (discussion !== null && discussion !== undefined) {
      args.push("--discussion", String(discussion));
    }
    if (pr !== null && pr !== undefined) {
      args.push("--pr", String(pr));
    }

    this._runScript(postAgentScript, args);

    process.stderr.write(
      `[hook-runner] post_agent hooks complete for ${agentId} (verdict=${verdict})\n`
    );
  }

  /**
   * Call pre-spawn-check.sh before an SDK-routed spawn.
   *
   * Returns true if spawn is allowed, false if blocked.
   * Non-fatal on script errors (defaults to allow).
   *
   * @param opts.role - The agent role string.
   * @param opts.discussion - Optional discussion number.
   * @returns true → spawn allowed; false → spawn blocked.
   */
  preSpawn(opts: { role: string; discussion?: number | null }): boolean {
    const { role, discussion } = opts;
    const preSpawnScript = join(this._root, "scripts", "pre-spawn-check.sh");
    const args: string[] = ["--role", role];
    if (discussion !== null && discussion !== undefined) {
      args.push("--discussion", String(discussion));
    }
    return this._runScript(preSpawnScript, args);
  }
}
