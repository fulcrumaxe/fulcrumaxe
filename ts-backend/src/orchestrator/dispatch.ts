/**
 * orchestrator/dispatch.ts — Spawn router for the hybrid orchestrator.
 *
 * Mirrors backend/orchestrator/dispatch.py 1:1.
 *
 * Receives a spawn spec JSON on stdin from spawn-agent.sh when ROUTE_VIA_DISPATCHER=1.
 * Reads credit balance and routes to:
 *   - sdk_runner  (when spawn is sdk_eligible + role in SDK_ELIGIBLE_ROLES + credit > $0)
 *   - Claude Code path (everything else, and always when credit is exhausted)
 *
 * Routing policy (D#1322 — selective offload):
 *   The SDK is an OFFLOAD LANE, not a replacement. A spawn routes to the SDK ONLY when:
 *   1. spec.sdkEligible is true (explicit opt-in; set via --sdk-lane flag in spawn-agent.sh)
 *   2. spec.role is in SDK_ELIGIBLE_ROLES (docs-writer, run-analyst, quality-sweep,
 *      feedback-scanner, mission-analyst). Executors, ALL reviewers, and the control
 *      plane always stay on CC.
 *   See offload-policy.ts for the canonical eligible-role set.
 *
 *   SHADOW_MODE is preserved for narrow operator overrides:
 *   - SHADOW_MODE=sdk:  force SDK path for ELIGIBLE-role spawns (bypasses sdkEligible flag,
 *     but the role gate is still unconditional — ineligible roles → cc)
 *   - SHADOW_MODE=cc:   force Claude Code path for all (bypass; safe default for testing)
 *   - SHADOW_MODE=both: run both paths in parallel for eligible roles only (DEBUG ONLY —
 *     doubles credit spend; ineligible roles always → cc regardless)
 *   - SHADOW_MODE=alternate: DEPRECATED — now treated the same as the default
 *     selective-opt-in path (both route to CC unless the spawn is explicitly sdk_eligible
 *     + eligible role).
 *
 * Credit-exhausted UX:
 *   - At $150 remaining ($50 consumed): warn to stderr.
 *   - At $0 remaining: hard-stop unless allowSubscriptionFallback is in spec.
 *
 * Returns a JSON result envelope to stdout:
 *   {"route": "sdk"|"cc"|"both", "run_id": "...", "verdict": "...", "error": null}
 *
 * CLI usage:
 *   echo '{"role":"docs-writer","sdk_eligible":true,...}' | bun run dispatch.ts
 *
 * Programmatic exports:
 *   import { route, CreditExhaustedError } from "./dispatch.js";
 *   const result = route(specDict);
 */

import { existsSync } from "node:fs";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

import { isOffloadEligible, SDK_ELIGIBLE_ROLES } from "./offload-policy.js";
import { shouldAutoRoute } from "./auto-route.js";
import { HookRunner, type RunResult } from "./hook-runner.js";
import { repoRoot as resolveCheckoutRoot } from "../config/repo-root.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Shadow-mode env var — read at call time so tests can override via env mutation. */
function _shadowMode(): string {
  return process.env["SHADOW_MODE"] ?? "alternate";
}

/** Soft-cap warning threshold: fires at $150 remaining = $50 consumed of $200. */
const WARN_THRESHOLD_USD = 150.0;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Parsed spawn spec passed to route(). Fields mirror SpawnSpec in sdk_runner.py. */
export interface SpawnSpec {
  role: string;
  taskPrompt?: string;
  toolWhitelist?: string[];
  roleCardPath?: string;
  isolation?: string;
  worktreePath?: string;
  envAllowlist?: string[];
  discussion?: number | null;
  pr?: number | null;
  agentId?: string | null;
  sdkEligible?: boolean;
  allowSubscriptionFallback?: boolean;
  untrustedContent?: Record<string, unknown>;
  /** Raw dict form (snake_case keys) — accepted for CLI/JSON parity. */
  [key: string]: unknown;
}

/** Result envelope returned by route() and emitted as JSON to stdout by main(). */
export interface RouteResult {
  route: "sdk" | "cc" | "both" | "blocked" | "error";
  run_id: string | null;
  verdict: string;
  error: string | null;
  /** Only present for SHADOW_MODE=both */
  sdk_result?: RouteResult;
  /** Only present for SHADOW_MODE=both */
  cc_result?: RouteResult;
}

/** Raised when credit is exhausted and fallback is not permitted. */
export class CreditExhaustedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CreditExhaustedError";
  }
}

// ---------------------------------------------------------------------------
// Credit tracker (thin wrapper — shells to credit_tracker.py for parity)
// ---------------------------------------------------------------------------

interface CreditBalance {
  remainingUsd: number;
}

function _readCreditBalance(repoRoot: string): CreditBalance {
  const script = join(repoRoot, "backend", "orchestrator", "credit_tracker.py");
  if (!existsSync(script)) {
    // No credit tracker available — treat as unlimited (CC path).
    return { remainingUsd: 200.0 };
  }
  try {
    const result = spawnSync(
      "python3",
      [script, "remaining"],
      {
        encoding: "utf-8",
        timeout: 10_000,
        stdio: ["pipe", "pipe", "pipe"],
      }
    );
    const raw = (result.stdout ?? "").trim();
    const parsed = parseFloat(raw);
    if (!isNaN(parsed)) return { remainingUsd: parsed };
  } catch {
    // Fall through
  }
  return { remainingUsd: 200.0 };
}

// ---------------------------------------------------------------------------
// Route decision (pure — mirrors _should_use_sdk())
// ---------------------------------------------------------------------------

/**
 * Return the route to take: 'sdk', 'cc', or 'both'.
 *
 * @param discussion - Discussion number (null for non-Discussion spawns).
 * @param remainingUsd - Current credit balance.
 * @param shadowMode - One of 'sdk', 'cc', 'both', or anything else (selective opt-in).
 * @param allowFallback - If true, CC fallback is permitted even at $0.
 * @param role - Agent role string — checked against SDK_ELIGIBLE_ROLES.
 * @param sdkEligible - Explicit opt-in flag from the spawn spec.
 */
export function _shouldUseSdk(opts: {
  discussion: number | null | undefined;
  remainingUsd: number;
  shadowMode: string;
  allowFallback: boolean;
  role: string;
  sdkEligible: boolean;
}): "sdk" | "cc" | "both" {
  const { remainingUsd, shadowMode, allowFallback, role, sdkEligible } = opts;

  // Credit exhausted — hard-stop SDK unless fallback is explicitly opted in
  if (remainingUsd <= 0) {
    if (allowFallback) {
      return "cc";
    }
    throw new CreditExhaustedError(
      "SDK credit exhausted ($0 remaining). " +
      "Pass allowSubscriptionFallback=true or use --allow-subscription-fallback " +
      "to enable the Claude Code fallback path."
    );
  }

  // Role gate — UNCONDITIONAL: ineligible roles NEVER route to SDK, in any mode.
  // This must be evaluated BEFORE force-mode logic so that SHADOW_MODE=sdk cannot
  // send executors, reviewers, or control-plane roles to the SDK.
  if (!SDK_ELIGIBLE_ROLES.has(role)) {
    return "cc";
  }

  // Force modes — narrow operator overrides (SHADOW_MODE env var).
  // At this point the role IS eligible; force modes only affect whether the
  // sdkEligible flag requirement is waived for eligible roles.
  if (shadowMode === "sdk") {
    // Force eligible-role spawns to SDK even without the sdkEligible flag.
    return "sdk";
  }
  if (shadowMode === "cc") {
    // Force all to CC; safe bypass for testing, no SDK calls made.
    return "cc";
  }
  if (shadowMode === "both") {
    // Both-path mode: runs SDK + CC in parallel for eligible roles only.
    // FORBIDDEN in continuous operation (burns credit on duplicate work).
    process.stderr.write(
      "[dispatch] SHADOW_MODE=both is running both SDK and CC paths. " +
      "This doubles credit spend and is FORBIDDEN in continuous operation.\n"
    );
    return "both";
  }

  // Default (and deprecated "alternate"): selective opt-in policy.
  // A spawn reaches the SDK ONLY when:
  //   1. sdkEligible=true (explicit flag from spawn-agent.sh --sdk-lane)
  //   2. role is in SDK_ELIGIBLE_ROLES (low-stakes background roles only)
  if (shadowMode === "alternate") {
    process.stderr.write(
      "[dispatch] SHADOW_MODE=alternate is deprecated; the selective opt-in policy now applies. " +
      "Remove SHADOW_MODE=alternate from your environment to silence this warning.\n"
    );
  }

  if (isOffloadEligible(role, sdkEligible)) {
    return "sdk";
  }

  return "cc";
}

// ---------------------------------------------------------------------------
// Agent ID helper
// ---------------------------------------------------------------------------

function _makeAgentId(spec: SpawnSpec): string {
  const role = (spec.role as string | undefined) ?? "unknown";
  const disc = spec.discussion ?? spec["discussion"] ?? "nod";
  return `${role}-${disc}-${Math.floor(Date.now() / 1000)}`;
}

// ---------------------------------------------------------------------------
// Record CC route (non-fatal — mirrors _record_cc_route())
// ---------------------------------------------------------------------------

function _recordCcRoute(agentId: string, spec: SpawnSpec, repoRoot: string): void {
  // Best-effort: shell to agent_run_tracker.py start_run + complete_run
  // to record routed_via="cc" so routing counts work without the used_usd proxy.
  const script = join(repoRoot, "backend", "agent_run_tracker.py");
  if (!existsSync(script)) return;
  try {
    const role = spec.role ?? "unknown";
    const discussion = spec.discussion ?? null;
    const pr = spec.pr ?? null;

    // start_run
    const startArgs = [
      script, "start_run",
      "--agent-id", agentId,
      "--role", String(role),
    ];
    if (discussion !== null) startArgs.push("--discussion", String(discussion));
    if (pr !== null) startArgs.push("--pr", String(pr));

    spawnSync("python3", startArgs, {
      timeout: 10_000,
      stdio: ["pipe", "pipe", "pipe"],
    });

    // complete_run with routed_via=cc
    spawnSync("python3", [
      script, "complete_run",
      "--agent-id", agentId,
      "--routed-via", "cc",
    ], {
      timeout: 10_000,
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch {
    // non-fatal
  }
}

// ---------------------------------------------------------------------------
// Credit warning
// ---------------------------------------------------------------------------

function _emitCreditWarning(remainingUsd: number): void {
  const msg =
    `[orchestrator] SDK credit soft-cap warning: ` +
    `$${remainingUsd.toFixed(2)} remaining of $200.00 monthly credit. ` +
    `Approaching the $0 limit will hard-stop SDK spawns.`;
  process.stderr.write(msg + "\n");

  // Post to team-log only when the SDK dispatcher is genuinely live.
  const isLive =
    process.env["ROUTE_VIA_DISPATCHER"] === "1" &&
    !process.env["PYTEST_CURRENT_TEST"];
  if (!isLive) return;

  // Resolve repo root at call time. Delegates to config/repo-root.ts
  // (D#1825) — wants repoRoot() since this locates a script inside the
  // checkout the running process is in.
  const repoRoot = resolveCheckoutRoot();
  const rotateScript = join(repoRoot, "scripts", "rotate-team-log.sh");
  if (!existsSync(rotateScript)) return;
  try {
    spawnSync("bash", [rotateScript, "comment", msg], {
      timeout: 15_000,
      stdio: ["pipe", "pipe", "pipe"],
      cwd: repoRoot,
    });
  } catch {
    // best-effort
  }
}

// ---------------------------------------------------------------------------
// SDK path runner (mirrors _run_sdk())
// ---------------------------------------------------------------------------

/**
 * Execute the SDK path, returning a result envelope.
 *
 * This implementation SKIPS the actual Claude SDK invocation (agent_sdk_runner.py)
 * per the task spec — the opencode runtime adapter is being built in a sibling task.
 * Instead it records the routing decision and signals to the caller that the SDK
 * backend is not yet wired. The routing logic (shouldUseSdk, credit check, role gate)
 * IS faithfully ported; only the final runner.run() call is stubbed.
 */
function _runSdk(
  spec: SpawnSpec,
  _repoRoot: string,
  hookRunner: HookRunner,
  autoRouted: boolean | null
): RouteResult {
  // Pre-spawn check (best-effort)
  hookRunner.preSpawn({ role: spec.role, discussion: spec.discussion ?? null });

  // SDK backend selection and invocation is deferred (agent_sdk_runner.py port
  // is in a sibling task). Emit a clear stub result so callers know the route was
  // selected but the runner is not yet connected.
  const agentId = (spec.agentId as string | null | undefined) ?? _makeAgentId(spec);
  process.stderr.write(
    `[dispatch] SDK path selected for role=${spec.role} agentId=${agentId} ` +
    `autoRouted=${autoRouted} — SDK runner not yet wired (see agent_sdk_runner port task)\n`
  );

  // Build a minimal RunResult for post-agent hooks so they still fire
  const stubResult: RunResult = {
    agentId,
    verdict: "skip",
    role: spec.role,
    discussion: spec.discussion ?? null,
    pr: spec.pr ?? null,
    inputTokens: 0,
    outputTokens: 0,
    error: "SDK runner not yet wired",
  };
  hookRunner.postAgent(stubResult);

  return {
    route: "sdk",
    run_id: agentId,
    verdict: "skip",
    error: "SDK runner not yet wired (agent_sdk_runner port is a sibling task)",
  };
}

// ---------------------------------------------------------------------------
// CC path (mirrors the cc branch in route())
// ---------------------------------------------------------------------------

function _routeToCc(spec: SpawnSpec, repoRoot: string): RouteResult {
  const agentId = (spec.agentId as string | null | undefined) ?? _makeAgentId(spec);
  _recordCcRoute(agentId, spec, repoRoot);
  return {
    route: "cc",
    run_id: agentId,
    verdict: "routed_to_cc",
    error: null,
  };
}

// ---------------------------------------------------------------------------
// Both-path mode (mirrors _run_both())
// ---------------------------------------------------------------------------

function _runBoth(
  spec: SpawnSpec,
  repoRoot: string,
  hookRunner: HookRunner,
  autoRouted: boolean | null
): RouteResult {
  const sdkResult = _runSdk(spec, repoRoot, hookRunner, autoRouted);
  const ccResult: RouteResult = {
    route: "cc",
    run_id: _makeAgentId(spec),
    verdict: "routed_to_cc",
    error: null,
  };
  process.stderr.write(
    `[dispatch] SHADOW_MODE=both comparison: sdk_verdict=${sdkResult.verdict} cc_verdict=${ccResult.verdict}\n`
  );
  return {
    route: "both",
    run_id: sdkResult.run_id,
    verdict: sdkResult.verdict,
    error: sdkResult.error,
    sdk_result: sdkResult,
    cc_result: ccResult,
  };
}

// ---------------------------------------------------------------------------
// Resolve repo root at module level
// ---------------------------------------------------------------------------

function _resolveRepoRoot(): string {
  // Delegates to config/repo-root.ts (D#1825) — see its docstring for the
  // full resolution order. This site wants repoRoot(), not mainRepoRoot():
  // it feeds HookRunner, which shells out to scripts inside the checkout
  // this process is running in.
  return resolveCheckoutRoot();
}

// ---------------------------------------------------------------------------
// Main route function (mirrors route())
// ---------------------------------------------------------------------------

/**
 * Route a spawn spec to SDK or CC, returning a result envelope.
 *
 * @param specDict - The parsed spawn spec. Must contain at least 'role'.
 *   Accepts both camelCase (TypeScript-native) and snake_case (JSON/CLI) keys.
 * @returns RouteResult with keys: route, run_id, verdict, error.
 */
export function route(specDict: SpawnSpec | Record<string, unknown>): RouteResult {
  const repoRoot = _resolveRepoRoot();
  const hookRunner = new HookRunner(repoRoot);

  // Normalise snake_case → camelCase for JSON input compatibility
  const role = String(
    (specDict as Record<string, unknown>)["role"] ?? ""
  );
  const discussion =
    ((specDict as Record<string, unknown>)["discussion"] as number | null | undefined) ??
    null;
  const pr =
    ((specDict as Record<string, unknown>)["pr"] as number | null | undefined) ?? null;
  const agentId =
    ((specDict as Record<string, unknown>)["agent_id"] as string | null | undefined) ??
    ((specDict as Record<string, unknown>)["agentId"] as string | null | undefined) ??
    null;
  const allowFallback = Boolean(
    (specDict as Record<string, unknown>)["allow_subscription_fallback"] ??
    (specDict as Record<string, unknown>)["allowSubscriptionFallback"] ??
    false
  );
  let sdkEligible = Boolean(
    (specDict as Record<string, unknown>)["sdk_eligible"] ??
    (specDict as Record<string, unknown>)["sdkEligible"] ??
    false
  );

  const spec: SpawnSpec = {
    role,
    discussion,
    pr,
    agentId,
    sdkEligible,
    allowSubscriptionFallback: allowFallback,
  };

  // Read credit balance
  const { remainingUsd } = _readCreditBalance(repoRoot);

  // SDK_AUTO_ROUTE gate: when SDK_AUTO_ROUTE=1, eligible low-stakes roles are
  // automatically treated as sdkEligible=true without the --sdk-lane flag.
  // DEFAULT OFF — zero effect when the env var is absent or not "1".
  let autoRouted: boolean | null = null;
  if (!sdkEligible && shouldAutoRoute(role)) {
    sdkEligible = true;
    spec.sdkEligible = true;
    autoRouted = true;
    process.stderr.write(
      `[orchestrator] SDK_AUTO_ROUTE: auto-routing role=${JSON.stringify(role)} to SDK lane ` +
      `(SDK_AUTO_ROUTE=1 and role is in SDK_ELIGIBLE_ROLES)\n`
    );
  } else if (sdkEligible) {
    // Explicit --sdk-lane opt-in
    autoRouted = false;
  }

  // Soft-cap warning (at $150 remaining)
  if (remainingUsd <= WARN_THRESHOLD_USD && remainingUsd > 0) {
    _emitCreditWarning(remainingUsd);
  }

  // Route decision
  let chosenRoute: "sdk" | "cc" | "both";
  try {
    chosenRoute = _shouldUseSdk({
      discussion,
      remainingUsd,
      shadowMode: _shadowMode(),
      allowFallback,
      role,
      sdkEligible,
    });
  } catch (err) {
    if (err instanceof CreditExhaustedError) {
      return {
        route: "blocked",
        run_id: null,
        verdict: "fail",
        error: err.message,
      };
    }
    throw err;
  }

  if (chosenRoute === "cc") {
    return _routeToCc(spec, repoRoot);
  }

  if (chosenRoute === "both") {
    return _runBoth(spec, repoRoot, hookRunner, autoRouted);
  }

  // SDK path
  return _runSdk(spec, repoRoot, hookRunner, autoRouted);
}

// ---------------------------------------------------------------------------
// CLI entry point (stdin JSON → stdout JSON)
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  let raw = "";
  for await (const chunk of process.stdin) {
    raw += chunk;
  }

  let specDict: Record<string, unknown>;
  try {
    specDict = JSON.parse(raw) as Record<string, unknown>;
  } catch (err) {
    const result: RouteResult = {
      route: "error",
      run_id: null,
      verdict: "fail",
      error: `Invalid JSON: ${String(err)}`,
    };
    process.stdout.write(JSON.stringify(result) + "\n");
    process.exit(1);
  }

  const result = route(specDict);
  process.stdout.write(JSON.stringify(result) + "\n");

  // Exit non-zero only on hard error (so spawn-agent.sh can detect failure)
  // "blocked" and "error" routes with verdict="fail" are hard errors; "cc" is always OK
  if (result.verdict === "fail" && result.route !== "cc") {
    process.exit(1);
  }
}

if (import.meta.main) {
  await main();
}
