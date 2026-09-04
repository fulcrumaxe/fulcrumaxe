/**
 * spawn/spawn-agent.ts — Assemble a fully-wired spawn prompt for Agent() calls.
 *
 * Mirrors scripts/spawn-agent.sh (856 LOC bash).
 *
 * The script:
 *   1. Parses CLI args (role, discussion, task-prompt, isolation, touchpoints,
 *      override-cap, pr, operation-class, sdk-lane, etc.)
 *   2. Runs pre-flight checks (concurrency caps, spec readiness, touchpoint conflicts)
 *   3. Generates a stable event-id and registers an agent_run row
 *   4. Assembles the spawn prompt by delegating to Python backend.prompt_builder
 *   5. Prints the assembled prompt to stdout (exit 0) or error to stderr (exit non-zero)
 *
 * RUNTIME INVOCATION SEAM (Module 10 extension point):
 * =====================================================
 * At the end of runSpawnAgent(), after the prompt is assembled, there is a
 * clearly-marked seam where the actual agent invocation would happen.
 * Currently this function returns the assembled prompt string — the CALLER
 * (Team Lead's Agent() call) is responsible for the invocation.
 *
 * In Module 10 (orchestrator/* port), the opencode/Qwen adapter will slot in
 * here. Look for: // ── RUNTIME INVOCATION SEAM ──
 *
 * CLI usage:
 *   PROMPT=$(bun run ts-backend/src/spawn/spawn-agent.ts \
 *     --role executor \
 *     --discussion 543 \
 *     --task-prompt "Implement Discussion #543 ..." \
 *     [--isolation worktree] \
 *     [--worktree-path /absolute/path/to/worktree] \
 *     [--security-trigger] \
 *     [--touchpoints file1.ts,file2.ts] \
 *     [--override-cap] \
 *     [--dry-run-env-dump] \
 *     [--no-register] \
 *     [--pr N] \
 *     [--operation-class agent.spawn] \
 *     [--sdk-lane])
 *
 * Output contract:
 *   - Exit 0 + assembled prompt on stdout  → spawn allowed, prompt ready
 *   - Exit 1 + error on stderr             → spawn blocked (cap/spec/conflict)
 *
 * Environment overrides:
 *   SPAWN_AGENT_SKIP_EXIT_TRAP=1  — suppress EXIT trap (mirrors bash behaviour)
 *   SPAWN_AGENT_ALLOW_NO_SPEC=1   — skip spec-readiness gate for executor role
 *   ALLOW_MISSING_EXTERNAL_DOCS=1 — skip external_docs marker gate
 *   SDK_LANE=1                    — treat as --sdk-lane flag
 *   OVERRIDE_CAP=1                — treat as --override-cap flag
 *   ROUTE_VIA_DISPATCHER=1        — delegate to backend.orchestrator.dispatch
 *                                   (mirrors bash §6.7)
 *   AF_RUNTIME=opencode           — invoke assembled prompt via opencode/Qwen
 *                                   instead of returning it for Team Lead's Agent()
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { execFileSync, spawnSync } from "node:child_process";
import { runPreSpawnCheck } from "./pre-spawn-check.js";
import { readFreshBody } from "./fresh-body-read.js";
import { startRun, completeRun } from "./agent-run-tracker.js";
import { runOpencodeRole, isOpencodeRuntimeEnabled, DEFAULT_OPENCODE_MODEL } from "./runtime/opencode-runtime.js";
import { resolveRepo } from "../config/repo.js";
import { repoRoot as resolveRepoRoot } from "../config/repo-root.js";

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------
//
// resolveRepoRoot() used to be a hand-rolled ".." walk here (D#1825 — it was
// walking one directory too far, into the parent of the repo). It now
// delegates to config/repo-root.ts, which mirrors backend/repo_root.py's
// two-answer contract. This call site wants repoRoot() (the checkout this
// process is running in), not mainRepoRoot() — it locates files this process
// reads or writes inside its own checkout.

// ---------------------------------------------------------------------------
// CLI argument types
// ---------------------------------------------------------------------------

export interface SpawnAgentArgs {
  role: string;
  discussion: number | null;
  taskPrompt: string;
  isolation: string;
  worktreePath: string;
  securityTrigger: boolean;
  touchpoints: string;
  overrideCap: boolean;
  dryRunEnvDump: boolean;
  noRegister: boolean;
  pr: number | null;
  operationClass: string;
  sdkLane: boolean;
  /**
   * When "opencode", invoke the assembled prompt via the opencode/Qwen adapter
   * instead of returning it for the caller's Agent() invocation.
   * Defaults to the value of AF_RUNTIME env var when not provided.
   */
  runtime?: string;
}

// ---------------------------------------------------------------------------
// Env-scrub helpers (mirrors bash §106-155)
// ---------------------------------------------------------------------------

const _ENV_SCRUB_ALLOWLIST = new Set(["CLAUDE_CODE_SSE_PORT"]);

/** Secret-pattern env var names that must be stripped from subagent shell. */
function collectScrubVars(): string[] {
  const names: string[] = [];
  for (const key of Object.keys(process.env)) {
    if (
      key === "ANTHROPIC_API_KEY" ||
      key.startsWith("CLAUDE_") ||
      key.endsWith("_API_KEY") ||
      key.endsWith("_TOKEN")
    ) {
      if (!_ENV_SCRUB_ALLOWLIST.has(key)) {
        names.push(key);
      }
    }
  }
  return names;
}

function buildEnvScrubSnippet(vars: string[]): string {
  if (vars.length === 0) return "";
  return "unset " + vars.join(" ");
}

// ---------------------------------------------------------------------------
// Role model reader (mirrors bash §1a)
// ---------------------------------------------------------------------------

function resolveRoleModel(repoRoot: string, role: string): string {
  const cardPath = join(repoRoot, ".claude", "agents", `${role}.md`);
  if (!existsSync(cardPath)) return "";
  try {
    const text = readFileSync(cardPath, { encoding: "utf-8" }).slice(0, 512);
    const m = text.match(/^model:\s*(\S+)/m);
    return m ? m[1]!.trim() : "";
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Spec readiness gate (mirrors bash §250-289)
// Checks STATUS:SPEC_READY and warns about missing sections.
// ---------------------------------------------------------------------------

function checkSpecReadiness(
  repoRoot: string,
  discussion: number
): { blocked: boolean; reason: string } {
  // Gate-time read: this is the exact gate PR #1783 fixed on the Python lane
  // (D#1794). A fresh read is required; a stale fallback must not silently pass
  // through as if it were current — it hard-blocks instead.
  const read = readFreshBody(repoRoot, discussion);
  if (read.status === "unavailable") {
    return {
      blocked: true,
      reason: `cannot read Discussion #${discussion} body — refusing to spawn executor without spec verification`,
    };
  }
  if (read.status === "stale") {
    return {
      blocked: true,
      reason: `Discussion #${discussion} body could not be freshly read (stale cache fallback) — refusing to spawn executor without a live spec verification`,
    };
  }
  const body = read.body;

  // Check for DONE/CLOSED
  const statusMatch = body.match(/STATUS:\s*([A-Z_]+)/);
  const firstStatus = statusMatch ? statusMatch[1] : "";
  if (firstStatus === "DONE" || firstStatus === "CLOSED") {
    return {
      blocked: true,
      reason: `Discussion #${discussion} status is ${firstStatus} — work is already complete`,
    };
  }

  // Must be SPEC_READY
  if (!/STATUS:\s*SPEC_READY/.test(body)) {
    return {
      blocked: true,
      reason: `Discussion #${discussion} is not SPEC_READY — run project-manager first to write a Spec`,
    };
  }

  // Soft warning about missing sections (non-blocking in bash too)
  try {
    const statusScript = join(repoRoot, "backend", "discussion_status.py");
    if (existsSync(statusScript)) {
      const raw = execFileSync("python3", [statusScript, "missing-sections", String(discussion)], {
        timeout: 10_000,
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "pipe"],
      });
      const missing = JSON.parse(raw.trim()) as string[];
      if (missing.length > 0) {
        process.stderr.write(
          `WARN: discussion #${discussion} body missing section: ${missing.join(", ")}\n`
        );
      }
    }
  } catch {
    // non-fatal
  }

  return { blocked: false, reason: "" };
}

// ---------------------------------------------------------------------------
// External docs marker gate (mirrors bash §291-329)
// ---------------------------------------------------------------------------

function checkExternalDocs(
  repoRoot: string,
  discussion: number
): { blocked: boolean; reason: string } {
  // Advisory / fail-open site: attempt a fresh read, but keep the existing
  // open disposition on any non-live outcome (unavailable OR stale) — this PR
  // only changes the freshness of the read, not this site's block/pass behavior
  // (see D#1794 Implementation Notes: no behaviour change smuggled in at cutover).
  const read = readFreshBody(repoRoot, discussion);
  if (read.status === "unavailable") return { blocked: false, reason: "" };
  const body = read.body;

  const m = body.match(/<!--\s*MISSING_EXTERNAL_DOCS:\s*([^-]+?)-->/);
  if (!m) return { blocked: false, reason: "" };

  const missing = m[1]!.trim();
  return {
    blocked: true,
    reason:
      `Discussion #${discussion} has <!-- MISSING_EXTERNAL_DOCS: ${missing} --> — ` +
      `The PM spec is missing external_docs URLs for: ${missing}`,
  };
}

// ---------------------------------------------------------------------------
// Touchpoint conflict detection (mirrors bash §331-382)
// ---------------------------------------------------------------------------

function checkTouchpointConflicts(
  repoRoot: string,
  touchpoints: string,
  role: string,
  _discussion: number | null,
): { blocked: boolean; conflicts: string[] } {
  const tpArray = touchpoints
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  if (tpArray.length === 0) return { blocked: false, conflicts: [] };

  const conflicts: string[] = [];

  // Check open PRs for file conflicts
  try {
    const repoSlug = resolveRepo();

    const prFilesRaw = execFileSync(
      "gh",
      [
        "pr",
        "list",
        "--repo",
        repoSlug,
        "--state",
        "open",
        "--json",
        "number,files",
        "--jq",
        '.[] | {n: .number, f: [.files[].path]} | .f[] | . + " PR#" + (.n | tostring)',
      ],
      { timeout: 20_000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
    );

    const prLines = prFilesRaw.split("\n").filter(Boolean);
    for (const tp of tpArray) {
      for (const line of prLines) {
        if (line.includes(tp + " ")) {
          const prRef = line.match(/PR#\d+/)?.[0] ?? "unknown PR";
          conflicts.push(`${role} spawn blocked — ${tp} already claimed by ${prRef}`);
        }
      }
    }
  } catch {
    // gh may be unavailable in tests — skip silently
  }

  // Check worktree diffs for file conflicts
  try {
    const wtListRaw = execFileSync(
      "git",
      ["-C", repoRoot, "worktree", "list", "--porcelain"],
      { timeout: 10_000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
    );
    const wtLines = wtListRaw.split("\n");
    let currentWt: string | null = null;
    for (const line of wtLines) {
      if (line.startsWith("worktree ")) {
        currentWt = line.slice("worktree ".length).trim();
      }
      if (currentWt && currentWt !== repoRoot && line === "") {
        // End of worktree stanza — check its changed files
        try {
          const wtChanged = execFileSync(
            "git",
            ["-C", currentWt, "diff", "--name-only", "origin/main"],
            { timeout: 8_000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
          );
          const wtId = currentWt.split("/").pop() ?? "unknown-wt";
          for (const changedFile of wtChanged.split("\n").filter(Boolean)) {
            for (const tp of tpArray) {
              if (changedFile.includes(tp)) {
                conflicts.push(
                  `${role} spawn blocked — ${tp} already claimed by WT:${wtId}`
                );
              }
            }
          }
        } catch {
          /* ignore */
        }
        currentWt = null;
      }
    }
  } catch {
    // git may be unavailable in tests — skip silently
  }

  if (conflicts.length > 0) {
    for (const c of conflicts) {
      process.stderr.write(`CONFLICT: ${c}\n`);
    }
  }

  return { blocked: conflicts.length > 0, conflicts };
}

// ---------------------------------------------------------------------------
// Dial state snapshot (mirrors bash §468-537)
// ---------------------------------------------------------------------------

function getDialStateAtSpawn(
  repoRoot: string,
  role: string,
  discussion: number | null
): { notify: string; dialState: string } {
  try {
    const script = `
import sys, json
sys.path.insert(0, sys.argv[1])
role = sys.argv[2]
disc = sys.argv[3]
verb_labels = {1: 'ask', 2: 'propose-confirm', 3: 'propose-timeout', 4: 'announce', 5: 'act'}
try:
    from backend.dial_registry import list_directives, _ROLE_TO_DIAL_CLASS
    directives = list_directives()
    dial_class = _ROLE_TO_DIAL_CLASS.get(role, 'agent.spawn')
    entry = next((d for d in directives if d['class'] == dial_class), None)
    if entry:
        lvl = entry['level']
        ceil = entry['ceiling']
        verb = verb_labels.get(lvl, str(lvl))
        disc_part = f' for D#{disc}' if disc else ''
        notify_line = f'spawning {role}{disc_part} ({dial_class}: {verb} level {lvl}/{ceil})'
        state_parts = []
        for d in directives:
            cls = d['class']
            lv = d['level']
            vb = verb_labels.get(lv, str(lv))
            state_parts.append(f'{cls}={vb}')
        dial_state_str = ', '.join(state_parts)
        print(json.dumps({'notify': notify_line, 'dial_state': dial_state_str}))
    else:
        disc_part = f' for D#{disc}' if disc else ''
        print(json.dumps({'notify': f'spawning {role}{disc_part} (dial: unknown)', 'dial_state': ''}))
except Exception:
    disc_part = f' for D#{disc}' if disc else ''
    print(json.dumps({'notify': f'spawning {role}{disc_part} (dial registry unavailable)', 'dial_state': ''}))
`;
    const result = execFileSync(
      "python3",
      ["-c", script, repoRoot, role, discussion ? String(discussion) : ""],
      { timeout: 10_000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
    );
    const parsed = JSON.parse(result.trim()) as {
      notify: string;
      dial_state: string;
    };
    return { notify: parsed.notify ?? "", dialState: parsed.dial_state ?? "" };
  } catch {
    return {
      notify: `spawning ${role}${discussion ? ` for D#${discussion}` : ""} (dial registry unavailable)`,
      dialState: "",
    };
  }
}

// ---------------------------------------------------------------------------
// Prompt assembly via Python backend.prompt_builder (mirrors bash §657-715)
// ---------------------------------------------------------------------------

async function assemblePrompt(
  repoRoot: string,
  args: SpawnAgentArgs,
  eventId: string,
  pscResult: Record<string, unknown>,
  envScrubSnippet: string,
  priorTestRunsBlock: string,
  dialStateAtSpawn: string,
): Promise<string> {
  // Build the JSON payload that prompt_builder.render expects
  const gateContext = (
    pscResult["gate_context"] as Record<string, unknown> | undefined
  );
  const gates = (gateContext?.["gates"] as Record<string, unknown> | undefined) ?? {};
  const gatePairs = Object.entries(gates)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
  const gateLine = gatePairs ? `[Control plane gates: ${gatePairs}]` : "";

  const payload: Record<string, unknown> = {
    role: args.role,
    discussion: args.discussion ?? null,
    task_prompt: args.taskPrompt,
    persona_voice: (pscResult["persona_voice"] as string | undefined) ?? "",
    working_principles: (pscResult["working_principles"] as string | undefined) ?? "",
    self_observe_gate: (pscResult["self_observe_gate"] as string | undefined) ?? "",
    gate_line: gateLine,
    worktree_path:
      args.isolation === "worktree" ? (args.worktreePath || null) : null,
    // D#2222 (bash-only, tracked as a deliberate gap, not an oversight): the
    // bash implementation (scripts/spawn-agent.sh) provisions a PR-amend
    // tree via pr-tree.sh and sets worktree_unprovisioned /
    // worktree_unprovisioned_reason so backend/prompt_builder.py can tell a
    // real provisioning failure from the canonical fresh-spawn shape (see
    // that file's _build_unprovisioned_worktree_block). This TS lane has
    // neither concept ported yet — omitting both keys here means
    // worktree_unprovisioned defaults to false and no worktree block is
    // rendered at all for a worktree-isolated spawn with no worktreePath,
    // silently, rather than either the honest or the hard-fail message. This
    // lane is not live yet, so it's not fixed here; if/when it goes live,
    // pr-tree provisioning and the three-way reason distinction need to be
    // ported alongside it, not assumed to already match.
    security_block: args.securityTrigger,
    hook_event_id: eventId,
    env_scrub_snippet: envScrubSnippet,
    prior_test_runs_block: priorTestRunsBlock,
    dial_state_at_spawn: dialStateAtSpawn,
  };

  const payloadJson = JSON.stringify(payload);

  // Call Python prompt_builder via execFileSync (ARGV array — no shell-string interpolation)
  try {
    const result = execFileSync(
      "python3",
      ["-m", "backend.prompt_builder", "render"],
      {
        timeout: 30_000,
        encoding: "utf-8",
        cwd: repoRoot,
        env: {
          ...process.env,
          SPAWN_PROMPT_JSON: payloadJson,
          PYTHONPATH: repoRoot,
        },
        stdio: ["pipe", "pipe", "pipe"],
      }
    );
    return result;
  } catch (e: unknown) {
    const err = e as { stdout?: string; stderr?: string; status?: number };
    throw new Error(
      `prompt_builder failed (exit ${err.status ?? "?"}): ${err.stderr ?? String(e)}`
    );
  }
}

// ---------------------------------------------------------------------------
// Prior test runs artifact block (mirrors bash §640-655)
// ---------------------------------------------------------------------------

function collectPriorTestRunsBlock(repoRoot: string, pr: number | null): string {
  if (!pr) return "";
  const paLib = join(repoRoot, "scripts", "lib", "pr-artifacts.sh");
  if (!existsSync(paLib)) return "";
  try {
    const sha = execFileSync(
      "gh",
      [
        "api",
        `repos/${resolveRepo()}/pulls/${pr}`,
        "--jq",
        ".head.sha",
      ],
      { timeout: 15_000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
    )
      .trim()
      .slice(0, 8);
    if (!sha) return "";
    const result = spawnSync(
      "bash",
      ["-c", `source "${paLib}"; inject_for_pr ${pr} ${sha}`],
      {
        timeout: 20_000,
        encoding: "utf-8",
        env: {
          ...process.env,
          SCRIPT_DIR: join(repoRoot, "scripts"),
          REPO_ROOT: repoRoot,
        },
      }
    );
    return result.status === 0 ? (result.stdout ?? "") : "";
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Dispatcher gate (mirrors bash §6.7)
// ---------------------------------------------------------------------------

function runDispatcher(
  repoRoot: string,
  args: SpawnAgentArgs,
  _eventId: string,
): {
  route: "sdk" | "cc" | "both" | "blocked";
  runId: string;
  error: string;
} {
  const roleCardPath = join(repoRoot, ".claude", "agents", `${args.role}.md`);

  const spec: Record<string, unknown> = {
    role: args.role,
    task_prompt: args.taskPrompt,
    role_card_path: existsSync(roleCardPath) ? roleCardPath : "",
    isolation: args.isolation || "worktree",
    worktree_path: args.worktreePath || "",
    env_allowlist: [],
    discussion: args.discussion ?? null,
    pr: args.pr ?? null,
    sdk_eligible: args.sdkLane,
  };

  try {
    const result = execFileSync(
      "python3",
      ["-m", "backend.orchestrator.dispatch"],
      {
        timeout: 30_000,
        encoding: "utf-8",
        cwd: repoRoot,
        input: JSON.stringify(spec),
        env: { ...process.env, PYTHONPATH: repoRoot },
        stdio: ["pipe", "pipe", "pipe"],
      }
    );
    const parsed = JSON.parse(result.trim()) as {
      route?: string;
      run_id?: string;
      error?: string;
    };
    const route = parsed.route;
    if (
      route === "sdk" ||
      route === "cc" ||
      route === "both" ||
      route === "blocked"
    ) {
      return { route, runId: parsed.run_id ?? "", error: parsed.error ?? "" };
    }
    // Unknown route → fail-safe to cc
    process.stderr.write(
      `[spawn-agent] dispatcher: unknown route '${route}' — falling back to CC path\n`
    );
    return { route: "cc", runId: "", error: "" };
  } catch {
    process.stderr.write(
      `[spawn-agent] dispatcher: crashed — falling back to CC path\n`
    );
    return { route: "cc", runId: "", error: "" };
  }
}

// ---------------------------------------------------------------------------
// Core function (fully testable without CLI plumbing)
// ---------------------------------------------------------------------------

export interface SpawnAgentResult {
  /** Exit code: 0 = success, 1 = blocked, 2 = usage error */
  exitCode: number;
  /** Assembled prompt string (empty when blocked) */
  assembled: string;
  /** Event ID generated for this spawn */
  eventId: string;
  /** Reason for blocking (empty when not blocked) */
  blockReason: string;
  /** Dial state snapshot string (for PR footer injection) */
  dialState: string;
  /** Route decision when ROUTE_VIA_DISPATCHER=1: "cc" | "sdk" | "blocked" */
  routedVia?: string;
  /**
   * When AF_RUNTIME=opencode (or args.runtime="opencode"), the result of
   * running the assembled prompt through the opencode/Qwen adapter.
   * Undefined when runtime is the default CC path.
   */
  opencodeResult?: {
    output: string;
    agentOutput: Record<string, unknown> | null;
    exitCode: number;
  };
}

/**
 * Run the full spawn-agent pipeline:
 *   1. Pre-flight gates (cap, spec, external-docs, touchpoints)
 *   2. pre-spawn-check (dial, budget, fleet concurrency)
 *   3. Event-id + agent_run registration
 *   4. Prompt assembly via backend.prompt_builder
 *
 * Returns a SpawnAgentResult — callers use `assembled` as the Agent() prompt.
 *
 * ── RUNTIME INVOCATION SEAM ────────────────────────────────────────────────
 * After this function returns, the CALLER (Team Lead's Agent() call) invokes
 * the assembled prompt. In Module 10, the opencode/Qwen adapter will slot in
 * here: instead of returning `assembled`, it will pass it to the SDK runtime.
 *
 * Extension point:
 *   const result = await runSpawnAgent(args);
 *   if (result.exitCode === 0) {
 *     // ── MODULE 10 SLOT: replace this comment with SDK/opencode invocation ──
 *     // e.g.: await opencodeRunner.run({ prompt: result.assembled, role: args.role });
 *   }
 * ── END SEAM ────────────────────────────────────────────────────────────────
 */
export async function runSpawnAgent(
  args: SpawnAgentArgs,
  opts: {
    repoRootOverride?: string;
    configPathOverride?: string;
    dbPathOverride?: string;
    /** Inject discussion body for tests (avoids gh/cache calls). */
    discussionBody?: string;
    /** When true, skip writing agent_run row (mirrors --no-register). */
    noRegister?: boolean;
    /**
     * When AF_RUNTIME=opencode, use this directory as the working directory
     * for the opencode subprocess. Defaults to repoRootOverride ?? repoRoot.
     * Useful in tests where the Python backend lives in the real repo root
     * but the agent should operate in a scratch workspace.
     */
    opencodeWorkdir?: string;
  } = {}
): Promise<SpawnAgentResult> {
  const repoRoot = opts.repoRootOverride ?? resolveRepoRoot();
  const noRegister = args.noRegister || opts.noRegister;

  // ── Env-scrub ─────────────────────────────────────────────────────────────
  const scrubVars = collectScrubVars();
  const envScrubSnippet = buildEnvScrubSnippet(scrubVars);
  if (envScrubSnippet) {
    process.stderr.write(
      `[spawn-agent] env-scrub: injecting unset for ${scrubVars.length} secret-pattern var(s)\n`
    );
  }

  // ── 0. PM-gate (spec readiness) for executor role (mirrors bash §250-289) ─
  if (args.role === "executor" && args.discussion && !noRegister) {
    const allowNoSpec = process.env["SPAWN_AGENT_ALLOW_NO_SPEC"] === "1";
    if (!allowNoSpec) {
      const specCheck = opts.discussionBody
        ? (() => {
            const body = opts.discussionBody!;
            if (!body.trim()) {
              return {
                blocked: true,
                reason: `cannot read Discussion #${args.discussion} body`,
              };
            }
            const statusMatch = body.match(/STATUS:\s*([A-Z_]+)/);
            const firstStatus = statusMatch ? statusMatch[1] : "";
            if (firstStatus === "DONE" || firstStatus === "CLOSED") {
              return {
                blocked: true,
                reason: `Discussion #${args.discussion} status is ${firstStatus} — work is already complete`,
              };
            }
            if (!/STATUS:\s*SPEC_READY/.test(body)) {
              return {
                blocked: true,
                reason: `Discussion #${args.discussion} is not SPEC_READY — run project-manager first to write a Spec`,
              };
            }
            return { blocked: false, reason: "" };
          })()
        : checkSpecReadiness(repoRoot, args.discussion);

      if (specCheck.blocked) {
        process.stderr.write(`Spawn blocked: ${specCheck.reason}.\n`);
        process.stderr.write(
          `  (Override with SPAWN_AGENT_ALLOW_NO_SPEC=1 only when a sub-PR inherits an umbrella spec.)\n`
        );
        return {
          exitCode: 1,
          assembled: "",
          eventId: "",
          blockReason: specCheck.reason,
          dialState: "",
        };
      }
    }
  }

  // ── 0b. External docs marker gate for executor role (mirrors bash §291-329) ─
  if (args.role === "executor" && args.discussion && !opts.discussionBody) {
    const allowMissing = process.env["ALLOW_MISSING_EXTERNAL_DOCS"] === "1";
    const edCheck = checkExternalDocs(repoRoot, args.discussion);
    if (edCheck.blocked) {
      if (allowMissing) {
        const reason =
          process.env["ALLOW_MISSING_EXTERNAL_DOCS_REASON"] ?? "no reason given";
        process.stderr.write(
          `WARN: ALLOW_MISSING_EXTERNAL_DOCS override for Discussion #${args.discussion} — reason: ${reason}\n`
        );
      } else {
        process.stderr.write(`Spawn blocked: ${edCheck.reason}.\n`);
        return {
          exitCode: 1,
          assembled: "",
          eventId: "",
          blockReason: edCheck.reason,
          dialState: "",
        };
      }
    }
  }

  // ── 0c. File-scope touchpoint conflict (mirrors bash §331-382) ────────────
  if (!args.touchpoints) {
    process.stderr.write(
      `WARN: --touchpoints not set for role=${args.role} discussion=${args.discussion ?? ""} — file-scope conflict detection skipped\n`
    );
  } else {
    const tpCheck = checkTouchpointConflicts(
      repoRoot,
      args.touchpoints,
      args.role,
      args.discussion
    );
    if (tpCheck.blocked) {
      return {
        exitCode: 1,
        assembled: "",
        eventId: "",
        blockReason: tpCheck.conflicts.join("; "),
        dialState: "",
      };
    }
  }

  // ── 1. Generate stable event-id (mirrors bash §395) ──────────────────────
  const eventId = `${args.role}-${args.discussion ?? "nod"}-${Math.floor(Date.now() / 1000)}`;

  // ── 1a. Resolve model from role agent card (mirrors bash §398-418) ────────
  const roleModel = resolveRoleModel(repoRoot, args.role);

  // ── 2-3. Build pre-spawn-check args + run pre-spawn check ────────────────
  // Delegates to the already-ported pre-spawn-check.ts.
  const pscResult = await runPreSpawnCheck({
    role: args.role,
    discussion: args.discussion,
    dryRun: noRegister ? true : false,
    noRegister: noRegister ? true : false,
    operationClass: args.operationClass || null,
    overrideCap: args.overrideCap,
    discussionBody: opts.discussionBody,
    configPathOverride: opts.configPathOverride,
    dbPathOverride: opts.dbPathOverride,
    repoRootOverride: repoRoot,
  });

  if (!pscResult.allowed) {
    const reason =
      pscResult.blocked_reason ?? pscResult.reason ?? "pre-spawn-check denied";
    process.stderr.write(
      `Spawn blocked: pre-spawn-check returned allowed=false for role=${args.role} — ${reason}\n`
    );
    return {
      exitCode: 1,
      assembled: "",
      eventId,
      blockReason: reason,
      dialState: "",
    };
  }

  // ── 3a. Dial state snapshot + spawn notification (mirrors bash §468-537) ──
  let dialStateAtSpawn = "";
  let spawnNotify = "";
  if (!opts.discussionBody) {
    // Only do live dial-registry queries in production (not in test injection mode)
    const dialInfo = getDialStateAtSpawn(repoRoot, args.role, args.discussion);
    spawnNotify = dialInfo.notify;
    dialStateAtSpawn = dialInfo.dialState;
  } else {
    spawnNotify = `spawning ${args.role}${args.discussion ? ` for D#${args.discussion}` : ""}`;
  }
  if (spawnNotify) {
    process.stderr.write(`${spawnNotify}\n`);
  }

  // ── 3b. Start run event (mirrors bash §539-574) ───────────────────────────
  if (!noRegister) {
    await startRun({
      agentId: eventId,
      role: args.role,
      discussion: args.discussion,
      pr: args.pr,
      eventId: eventId,
      model: roleModel || null,
    });
  }

  // ── 4. Resolve worktree path for prompt injection (mirrors bash §620-638) ─
  const resolvedWorktreePath: string =
    args.isolation === "worktree" ? args.worktreePath : "";

  // ── 5. Collect prior test-run artifact block (mirrors bash §640-655) ──────
  const priorTestRunsBlock = collectPriorTestRunsBlock(repoRoot, args.pr);

  // ── 6. Assemble prompt (mirrors bash §657-715) ────────────────────────────
  let assembled: string;
  try {
    assembled = await assemblePrompt(
      repoRoot,
      { ...args, worktreePath: resolvedWorktreePath },
      eventId,
      pscResult as unknown as Record<string, unknown>,
      envScrubSnippet,
      priorTestRunsBlock,
      dialStateAtSpawn,
    );
  } catch (e) {
    const msg = String(e);
    process.stderr.write(`Spawn blocked: ${msg}\n`);
    return {
      exitCode: 1,
      assembled: "",
      eventId,
      blockReason: msg,
      dialState: dialStateAtSpawn,
    };
  }

  if (!assembled.trim()) {
    process.stderr.write(
      `Spawn blocked: prompt_builder returned empty prompt for role=${args.role}\n`
    );
    return {
      exitCode: 1,
      assembled: "",
      eventId,
      blockReason: "prompt_builder returned empty prompt",
      dialState: dialStateAtSpawn,
    };
  }

  // ── 6.5. Voice block audit (mirrors bash §717-726) ───────────────────────
  if (!assembled.includes("## Voice")) {
    process.stderr.write(
      `WARN: assembled prompt for role=${args.role} discussion=${args.discussion ?? ""} is missing ## Voice block — persona drift guard absent\n`
    );
  }

  // ── 6.6. Code-reviewer pytest discipline warning (mirrors bash §728-735) ──
  if (args.role === "code-reviewer" && !/pytest/i.test(assembled)) {
    process.stderr.write(
      `WARN: code-reviewer prompt for discussion=${args.discussion ?? ""} does not include pytest invocation — drift risk\n`
    );
  }

  // ── 6.7. Dispatcher gate (mirrors bash §737-853) ─────────────────────────
  const routeViaDispatcher = process.env["ROUTE_VIA_DISPATCHER"] === "1";
  if (routeViaDispatcher) {
    const dispatch = runDispatcher(repoRoot, args, eventId);

    if (dispatch.route === "sdk") {
      process.stderr.write(
        `routed_via=sdk run_id=${dispatch.runId} role=${args.role} discussion=${args.discussion ?? ""}\n`
      );
      const sdkResponse = JSON.stringify({
        routed_via: "sdk",
        run_id: dispatch.runId,
        role: args.role,
        discussion: args.discussion ?? "",
      });
      process.stdout.write(sdkResponse + "\n");
      return {
        exitCode: 0,
        assembled: sdkResponse,
        eventId,
        blockReason: "",
        dialState: dialStateAtSpawn,
        routedVia: "sdk",
      };
    }

    if (dispatch.route === "blocked") {
      const blockMsg = dispatch.error || "credit exhausted";
      process.stderr.write(
        `[spawn-agent] dispatcher: spawn blocked — ${blockMsg}\n`
      );
      return {
        exitCode: 1,
        assembled: "",
        eventId,
        blockReason: blockMsg,
        dialState: dialStateAtSpawn,
        routedVia: "blocked",
      };
    }

    // route === "cc" or "both" — continue with assembled prompt
    process.stderr.write(
      `routed_via=cc run_id=${dispatch.runId} role=${args.role} discussion=${args.discussion ?? ""}\n`
    );
  }

  // ── RUNTIME INVOCATION SEAM ──────────────────────────────────────────────
  // The assembled prompt is ready. Two paths diverge here:
  //
  //  1. Default (CC path): return the assembled string — the caller (Team Lead's
  //     Agent() call) is responsible for the actual invocation.
  //
  //  2. opencode path (AF_RUNTIME=opencode or args.runtime="opencode"):
  //     invoke the assembled prompt through the opencode/Qwen adapter right now,
  //     then complete the agent_run row with the result.
  // ── END SEAM ──────────────────────────────────────────────────────────────

  const useOpencode =
    args.runtime === "opencode" || isOpencodeRuntimeEnabled();

  if (!useOpencode) {
    return {
      exitCode: 0,
      assembled,
      eventId,
      blockReason: "",
      dialState: dialStateAtSpawn,
      routedVia: "cc",
    };
  }

  // ── opencode/Qwen path ───────────────────────────────────────────────────
  const model =
    process.env["AF_OPENCODE_MODEL"] ?? DEFAULT_OPENCODE_MODEL;

  const ocResult = await runOpencodeRole({
    prompt: assembled,
    role: args.role,
    model,
    cwd: opts.opencodeWorkdir ?? opts.repoRootOverride ?? repoRoot,
  });

  // Complete the agent_run row with whatever we know from the output.
  if (!noRegister) {
    const ao = ocResult.agentOutput;
    const verdict =
      typeof ao?.["verdict"] === "string" ? ao["verdict"] : null;
    const tokensUsed = (ao?.["tokens_used"] as Record<string, unknown> | undefined) ?? {};
    await completeRun({
      agentId: eventId,
      verdict: verdict ?? (ocResult.exitCode === 0 ? "done" : "fail"),
      model,
      inputTok: typeof tokensUsed["input"] === "number" ? tokensUsed["input"] as number : null,
      outputTok: typeof tokensUsed["output"] === "number" ? tokensUsed["output"] as number : null,
      routedVia: "opencode",
      autoRouted: false,
    });
  }

  return {
    exitCode: ocResult.exitCode,
    assembled,
    eventId,
    blockReason: ocResult.exitCode !== 0 ? "opencode exited non-zero" : "",
    dialState: dialStateAtSpawn,
    routedVia: "opencode",
    opencodeResult: ocResult,
  };
}

// ---------------------------------------------------------------------------
// CLI argument parser (mirrors bash §73-94)
// ---------------------------------------------------------------------------

function parseArgs(argv: string[]): SpawnAgentArgs {
  let role = "";
  let discussion: number | null = null;
  let taskPrompt = "";
  let isolation = "";
  let worktreePath = "";
  let securityTrigger = false;
  let touchpoints = "";
  let overrideCap = process.env["OVERRIDE_CAP"] === "1";
  let dryRunEnvDump = false;
  let noRegister = false;
  let pr: number | null = null;
  let operationClass = "";
  let sdkLane = process.env["SDK_LANE"] === "1";
  let runtime = process.env["AF_RUNTIME"] ?? "";

  let i = 0;
  while (i < argv.length) {
    const arg = argv[i]!;
    switch (arg) {
      case "--role":
        role = argv[++i] ?? "";
        break;
      case "--discussion": {
        const raw = argv[++i] ?? "";
        const n = parseInt(raw, 10);
        discussion = isNaN(n) ? null : n;
        break;
      }
      case "--task-prompt":
        taskPrompt = argv[++i] ?? "";
        break;
      case "--isolation":
        isolation = argv[++i] ?? "";
        break;
      case "--worktree-path":
        worktreePath = argv[++i] ?? "";
        break;
      case "--security-trigger":
        securityTrigger = true;
        break;
      case "--touchpoints":
        touchpoints = argv[++i] ?? "";
        break;
      case "--override-cap":
        overrideCap = true;
        break;
      case "--dry-run-env-dump":
        dryRunEnvDump = true;
        break;
      case "--no-register":
        noRegister = true;
        break;
      case "--pr": {
        const raw = argv[++i] ?? "";
        const n = parseInt(raw, 10);
        pr = isNaN(n) ? null : n;
        break;
      }
      case "--operation-class":
        operationClass = argv[++i] ?? "";
        break;
      case "--sdk-lane":
        sdkLane = true;
        break;
      case "--runtime":
        runtime = argv[++i] ?? "";
        break;
      default:
        process.stderr.write(`Unknown argument: ${arg}\n`);
        process.stderr.write(
          `Usage: spawn-agent.ts --role <role> --discussion <N> --task-prompt <text> ` +
            `[--isolation worktree] [--worktree-path <path>] [--security-trigger] ` +
            `[--touchpoints <comma-separated-paths>] [--override-cap] [--dry-run-env-dump] ` +
            `[--no-register] [--pr <N>] [--operation-class <class>] [--sdk-lane] ` +
            `[--runtime opencode]\n`
        );
        process.exit(1);
    }
    i++;
  }

  return {
    role,
    discussion,
    taskPrompt,
    isolation,
    worktreePath,
    securityTrigger,
    touchpoints,
    overrideCap,
    dryRunEnvDump,
    noRegister,
    pr,
    operationClass,
    sdkLane,
    runtime: runtime || undefined,
  };
}

// ---------------------------------------------------------------------------
// CLI entry point (mirrors bash top-level flow)
// ---------------------------------------------------------------------------

if (import.meta.main) {
  const cliArgs = parseArgs(process.argv.slice(2));

  // ── Validate required args ────────────────────────────────────────────────
  if (!cliArgs.role) {
    process.stderr.write("Error: --role is required\n");
    process.exit(1);
  }
  if (!cliArgs.taskPrompt && !cliArgs.dryRunEnvDump) {
    process.stderr.write("Error: --task-prompt is required\n");
    process.exit(1);
  }

  // ── --dry-run-env-dump: print scrubbed env and exit (mirrors bash §157-162) ─
  if (cliArgs.dryRunEnvDump) {
    const scrubVars = collectScrubVars();
    process.stderr.write(
      `[env-scrub] scrubbed ${scrubVars.length} var(s) matching secret patterns\n`
    );
    for (const v of scrubVars) {
      delete process.env[v];
    }
    for (const [k, v] of Object.entries(process.env)) {
      if (v !== undefined) {
        process.stdout.write(`${k}=${v}\n`);
      }
    }
    process.exit(0);
  }

  const result = await runSpawnAgent(cliArgs);

  if (result.exitCode === 0) {
    // ── 7. Print assembled prompt to stdout (mirrors bash §855-856) ──────────
    process.stdout.write(result.assembled);
  }

  process.exit(result.exitCode);
}
