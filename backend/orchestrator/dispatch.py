"""backend/orchestrator/dispatch.py — Spawn router for the hybrid orchestrator.

Receives a spawn spec JSON on stdin from spawn-agent.sh when ROUTE_VIA_DISPATCHER=1.
Reads credit_tracker.remaining_usd() and routes to:
  - sdk_runner  (when spawn is sdk_eligible + role in SDK_ELIGIBLE_ROLES + credit > $0)
  - Claude Code path (everything else, and always when credit is exhausted)

Routing policy (D#1322 — selective offload):
  The SDK is an OFFLOAD LANE, not a replacement.  A spawn routes to the SDK ONLY when:
  1. spec.sdk_eligible is True (explicit opt-in; set via --sdk-lane flag in spawn-agent.sh)
  2. spec.role is in SDK_ELIGIBLE_ROLES (docs-writer, run-analyst, quality-sweep,
     feedback-scanner, mission-analyst).  Executors, ALL reviewers, and the control
     plane always stay on CC.
  See backend/orchestrator/offload_policy.py for the canonical eligible-role set.

  SHADOW_MODE is preserved for narrow operator overrides:
  - SHADOW_MODE=sdk: force SDK path for ELIGIBLE-role spawns (bypasses the sdk_eligible
    flag requirement, but the role gate is still unconditional — ineligible roles → cc)
  - SHADOW_MODE=cc:  force Claude Code path for all (bypass; safe default for testing)
  - SHADOW_MODE=both: run both paths in parallel for eligible roles only (DEBUG ONLY —
    doubles credit spend; ineligible roles always → cc regardless)
  - SHADOW_MODE=alternate: DEPRECATED — previously alternated by discussion parity.
    Now treated the same as the default selective-opt-in path (both route to CC unless
    the spawn is explicitly sdk_eligible + eligible role).

Credit-exhausted UX:
  - At $150 remaining ($50 consumed): warn to stdout and loop log.
  - At $0 remaining: hard-stop unless --allow-subscription-fallback is in spec.

Returns a JSON result envelope to stdout:
    {"route": "sdk"|"cc"|"both", "run_id": "...", "verdict": "...", "error": null}

Usage from spawn-agent.sh::

    echo '{"role":"docs-writer","sdk_eligible":true,...}' | python3 -m backend.orchestrator.dispatch

Or as a library::

    from backend.orchestrator.dispatch import route
    result = route(spec_dict)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from backend.orchestrator.credit_tracker import CreditTracker
from backend.orchestrator.sdk_runner import SDKRunner, SpawnSpec
from backend.orchestrator.hook_runner import HookRunner
from backend.orchestrator.offload_policy import is_offload_eligible, SDK_ELIGIBLE_ROLES
from backend.orchestrator.auto_route import should_auto_route

logger = logging.getLogger(__name__)

# Shadow-mode env var
_SHADOW_MODE = os.environ.get("SHADOW_MODE", "alternate")

# Soft-cap warning threshold (fires at $150 remaining = $50 consumed of $200)
_WARN_THRESHOLD_USD = 150.0


# ---------------------------------------------------------------------------
# Route decision
# ---------------------------------------------------------------------------

def _should_use_sdk(
    discussion: Optional[int],
    remaining_usd: float,
    shadow_mode: str,
    allow_fallback: bool,
    role: str = "",
    sdk_eligible: bool = False,
) -> str:
    """Return the route to take: 'sdk', 'cc', or 'both'.

    Parameters
    ----------
    discussion:     Discussion number (None for non-Discussion spawns).
    remaining_usd:  Current credit balance from credit_tracker.
    shadow_mode:    One of 'sdk', 'cc', 'both', or anything else (treated as
                    selective-opt-in default).  'alternate' is deprecated and
                    now falls through to the selective-opt-in path.
    allow_fallback: If True, CC fallback is permitted even at $0.
    role:           Agent role string — checked against SDK_ELIGIBLE_ROLES.
                    Roles not in SDK_ELIGIBLE_ROLES always route to CC, in
                    every mode, including force modes (SHADOW_MODE=sdk/both).
    sdk_eligible:   Explicit opt-in flag from the spawn spec.  Must be True
                    AND role must be eligible for SDK routing to occur (in
                    the default/selective path).  Force modes (SHADOW_MODE=sdk)
                    waive this flag requirement but still require an eligible role.
    """
    # Credit exhausted — hard-stop SDK unless fallback is explicitly opted in
    if remaining_usd <= 0:
        if allow_fallback:
            logger.info("Credit exhausted; falling back to Claude Code (--allow-subscription-fallback)")
            return "cc"
        else:
            raise CreditExhaustedError(
                "SDK credit exhausted ($0 remaining). "
                "Pass allow_subscription_fallback=True or use --allow-subscription-fallback "
                "to enable the Claude Code fallback path."
            )

    # Role gate — UNCONDITIONAL: ineligible roles NEVER route to SDK, in any mode.
    # This must be evaluated BEFORE force-mode logic so that SHADOW_MODE=sdk cannot
    # send executors, reviewers, or control-plane roles to the SDK.
    if role not in SDK_ELIGIBLE_ROLES:
        return "cc"

    # Force modes — narrow operator overrides (SHADOW_MODE env var).
    # At this point the role IS eligible; force modes only affect whether the
    # sdk_eligible flag requirement is waived for eligible roles.
    if shadow_mode == "sdk":
        # Force eligible-role spawns to SDK even without the sdk_eligible flag.
        # Ineligible roles already returned "cc" above — the old "bypasses role check"
        # comment is now obsolete by design.
        return "sdk"
    if shadow_mode == "cc":
        # Force all to CC; safe bypass for testing, no SDK calls made
        return "cc"
    if shadow_mode == "both":
        # Both-path mode: runs SDK + CC in parallel for eligible roles only.
        # FORBIDDEN in continuous operation (burns credit on duplicate work).
        # Only for debugging/comparison.
        logger.warning(
            "SHADOW_MODE=both is running both SDK and CC paths. "
            "This doubles credit spend and is FORBIDDEN in continuous operation."
        )
        return "both"

    # Default (and deprecated "alternate"): selective opt-in policy.
    # A spawn reaches the SDK ONLY when:
    #   1. sdk_eligible=True (explicit flag from spawn-agent.sh --sdk-lane)
    #   2. role is in SDK_ELIGIBLE_ROLES (low-stakes background roles only)
    # Everything else — executors, ALL reviewers, control-plane — stays on CC.
    if shadow_mode == "alternate":
        logger.warning(
            "SHADOW_MODE=alternate is deprecated; the selective opt-in policy now applies. "
            "Remove SHADOW_MODE=alternate from your environment to silence this warning."
        )

    if is_offload_eligible(role, sdk_eligible):
        return "sdk"

    return "cc"


class CreditExhaustedError(RuntimeError):
    """Raised when credit is exhausted and fallback is not permitted."""


# ---------------------------------------------------------------------------
# Main route function
# ---------------------------------------------------------------------------

def route(spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Route a spawn spec to SDK or CC, returning a result envelope.

    Parameters
    ----------
    spec_dict:
        The parsed spawn spec JSON. Must contain at least 'role'.

    Returns
    -------
    dict with keys:
        route:    'sdk' | 'cc' | 'both'
        run_id:   agent_id string
        verdict:  verdict string from the run (or 'routed_to_cc' for CC path)
        error:    error string or None
    """
    tracker = CreditTracker()
    remaining = tracker.remaining_usd()
    allow_fallback = bool(spec_dict.get("allow_subscription_fallback", False))
    discussion = spec_dict.get("discussion")
    role = spec_dict.get("role", "")
    sdk_eligible = bool(spec_dict.get("sdk_eligible", False))

    # SDK_AUTO_ROUTE gate: when SDK_AUTO_ROUTE=1, eligible low-stakes roles are
    # automatically treated as sdk_eligible=True without the --sdk-lane flag.
    # DEFAULT OFF — zero effect when the env var is absent or not "1".
    # Non-eligible roles (executor, reviewers, control-plane) never auto-route;
    # should_auto_route() enforces the role gate via SDK_ELIGIBLE_ROLES.
    #
    # auto_routed tracks whether the SDK routing decision was made by the auto-route
    # gate (True) vs explicit --sdk-lane opt-in (False). CC runs and pre-D#1364 rows
    # record NULL. Used for rollback auditing to isolate auto-routed runs.
    auto_routed: bool | None = None
    if not sdk_eligible and should_auto_route(role):
        sdk_eligible = True
        auto_routed = True
        logger.info(
            "[orchestrator] SDK_AUTO_ROUTE: auto-routing role=%r to SDK lane "
            "(SDK_AUTO_ROUTE=1 and role is in SDK_ELIGIBLE_ROLES)",
            role,
        )
    elif sdk_eligible:
        # Explicit --sdk-lane opt-in (or already sdk_eligible=True in the spec)
        auto_routed = False

    # Soft-cap warning (AC4 — at $150 remaining)
    if remaining <= _WARN_THRESHOLD_USD and remaining > 0:
        _emit_credit_warning(remaining)

    try:
        chosen_route = _should_use_sdk(
            discussion=discussion,
            remaining_usd=remaining,
            shadow_mode=_SHADOW_MODE,
            allow_fallback=allow_fallback,
            role=role,
            sdk_eligible=sdk_eligible,
        )
    except CreditExhaustedError as e:
        return {
            "route": "blocked",
            "run_id": None,
            "verdict": "fail",
            "error": str(e),
        }

    if chosen_route == "cc":
        # Claude Code path: return a routing signal; the shell wrapper continues
        # with the existing claude -p / Agent() call.
        # Record routed_via="cc" in agent_run so the reader can count CC rows by real
        # column rather than falling back to the used_usd proxy for dispatcher-routed runs.
        agent_id = _make_agent_id(spec_dict)
        _record_cc_route(agent_id, spec_dict)
        return {
            "route": "cc",
            "run_id": agent_id,
            "verdict": "routed_to_cc",
            "error": None,
        }

    if chosen_route == "both":
        # Both paths in parallel (DEBUG mode — see warning above)
        return _run_both(spec_dict, tracker, auto_routed=auto_routed)

    # SDK path
    return _run_sdk(spec_dict, tracker, auto_routed=auto_routed)


def _select_sdk_backend() -> Any:
    """Return the appropriate SDK runner instance based on config and environment.

    Selection precedence (highest to lowest):

    1. SDK_BACKEND env var (explicit override):
       - "subscription" or "agent_sdk"  → ClaudeAgentSDKRunner
       - "apikey" or "anthropic"        → SDKRunner

    2. Auto-detect from credentials:
       - CLAUDE_CODE_OAUTH_TOKEN is set → ClaudeAgentSDKRunner (subscription)
       - ANTHROPIC_API_KEY is set       → SDKRunner (API-key)
       - When BOTH tokens are set, subscription is preferred.
         Rationale: if CLAUDE_CODE_OAUTH_TOKEN is explicitly set the caller
         intends subscription billing. Note that the claude CLI itself prefers
         ANTHROPIC_API_KEY when both exist — if you want the CLI to use the
         subscription, unset ANTHROPIC_API_KEY before invoking the runner.
         A warning is emitted so this isn't silent.

    3. Neither credential → returns None (caller should fall back to CC).

    Emits a logger.info line with the selected backend so telemetry can
    attribute runs to the right billing path.

    Returns
    -------
    SDKRunner | ClaudeAgentSDKRunner | None
        None means no SDK credential is available; caller should route to CC.

    Note: SDK_BACKEND is read from os.environ at call time (not module-load time)
    so that tests can override it via monkeypatch.setenv without reloading the
    module. This keeps CreditExhaustedError identity stable across test sessions.
    """
    override = os.environ.get("SDK_BACKEND", "").strip().lower()

    if override in ("subscription", "agent_sdk"):
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner  # noqa: PLC0415
        logger.info("[backend-selector] SDK_BACKEND=%s → using ClaudeAgentSDKRunner (subscription)", override)
        return ClaudeAgentSDKRunner()

    if override in ("apikey", "anthropic"):
        logger.info("[backend-selector] SDK_BACKEND=%s → using SDKRunner (API-key)", override)
        return SDKRunner()

    if override and override not in ("subscription", "agent_sdk", "apikey", "anthropic"):
        logger.warning(
            "[backend-selector] Unknown SDK_BACKEND=%r — falling back to auto-detect",
            override,
        )

    # Auto-detect via shared credential detector.
    # detect_sdk_credential() returns the KIND of credential present:
    #   "oauth_token" → explicit env-var subscription token → ClaudeAgentSDKRunner
    #   "api_key"     → explicit env-var API key           → SDKRunner
    #   "login"       → stored claude CLI login file       → ClaudeAgentSDKRunner
    #   None          → no credential                      → CC fallback
    #
    # Precedence (mirrors detect_sdk_credential's order):
    #   CLAUDE_CODE_OAUTH_TOKEN > ANTHROPIC_API_KEY > ~/.claude/.credentials.json
    #
    # When BOTH CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set, the oauth
    # token wins (subscription is preferred).  Note: the claude CLI itself prefers
    # ANTHROPIC_API_KEY — if you want the CLI subprocess to use subscription billing,
    # unset ANTHROPIC_API_KEY before invoking the runner.  A warning is emitted.
    from backend.orchestrator.agent_sdk_runner import detect_sdk_credential  # noqa: PLC0415
    cred_kind = detect_sdk_credential()

    if cred_kind == "oauth_token":
        has_apikey = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if has_apikey:
            logger.warning(
                "[backend-selector] Both CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set. "
                "Selecting ClaudeAgentSDKRunner (subscription). "
                "Note: the claude CLI itself prefers ANTHROPIC_API_KEY — "
                "unset it if you want the CLI subprocess to use subscription billing."
            )
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner  # noqa: PLC0415
        logger.info("[backend-selector] auto-detect → using ClaudeAgentSDKRunner (CLAUDE_CODE_OAUTH_TOKEN present)")
        return ClaudeAgentSDKRunner()

    if cred_kind == "api_key":
        logger.info("[backend-selector] auto-detect → using SDKRunner (ANTHROPIC_API_KEY present)")
        return SDKRunner()

    if cred_kind == "login":
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner  # noqa: PLC0415
        logger.info(
            "[backend-selector] auto-detect → using ClaudeAgentSDKRunner "
            "(stored claude CLI login at ~/.claude/.credentials.json)"
        )
        return ClaudeAgentSDKRunner()

    # No credential available
    logger.info(
        "[backend-selector] no SDK credential found "
        "(no CLAUDE_CODE_OAUTH_TOKEN, no ANTHROPIC_API_KEY, no stored claude login) "
        "— SDK path unavailable"
    )
    return None


def _run_sdk(
    spec_dict: dict[str, Any],
    tracker: Any,
    auto_routed: bool | None = None,
) -> dict[str, Any]:
    """Execute the SDK path and return the result envelope."""
    spec = _dict_to_spec(spec_dict)
    hook_runner = HookRunner()

    # Pre-spawn check (best-effort)
    hook_runner.pre_spawn(role=spec.role, discussion=spec.discussion)

    runner = _select_sdk_backend()

    if runner is None:
        # No credential available — fall back to CC without crashing.
        logger.info(
            "[orchestrator] _run_sdk: no SDK credential; routing to CC (sdk_runner=None)"
        )
        agent_id = spec.agent_id or _make_agent_id(spec_dict)
        return {
            "route": "cc",
            "run_id": agent_id,
            "verdict": "routed_to_cc",
            "error": "no SDK credential available; falling back to Claude Code",
        }

    try:
        result = asyncio.run(runner.run(spec, auto_routed=auto_routed))
    except Exception as e:  # noqa: BLE001
        return {
            "route": "sdk",
            "run_id": spec.agent_id or _make_agent_id(spec_dict),
            "verdict": "fail",
            "error": str(e),
        }

    # auto_routed was passed into runner.run() so it is already set on the
    # result and written to the DB row by _write_agent_run inside the runner.
    # No post-run stamp needed here.

    # Post-agent hooks
    hook_runner.post_agent(result)

    # Decrement credit (estimate from token counts if cost not available)
    _maybe_decrement_credit(tracker, result)

    return {
        "route": "sdk",
        "run_id": result.agent_id,
        "verdict": result.verdict,
        "error": result.error,
    }


def _run_both(
    spec_dict: dict[str, Any],
    tracker: Any,
    auto_routed: bool | None = None,
) -> dict[str, Any]:
    """Run both SDK and CC paths, log both outputs.

    Returns the SDK result (behaviorally authoritative for comparison logging).
    """
    sdk_result = _run_sdk(spec_dict, tracker, auto_routed=auto_routed)
    cc_result = {
        "route": "cc",
        "run_id": _make_agent_id(spec_dict),
        "verdict": "routed_to_cc",
        "error": None,
    }
    logger.info(
        "SHADOW_MODE=both comparison: sdk_verdict=%s cc_verdict=%s",
        sdk_result.get("verdict"),
        cc_result.get("verdict"),
    )
    return {
        "route": "both",
        "run_id": sdk_result.get("run_id"),
        "verdict": sdk_result.get("verdict"),
        "error": sdk_result.get("error"),
        "sdk_result": sdk_result,
        "cc_result": cc_result,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dict_to_spec(d: dict[str, Any]) -> SpawnSpec:
    return SpawnSpec(
        role=d["role"],
        task_prompt=d.get("task_prompt", ""),
        tool_whitelist=d.get("tool_whitelist", ["Read", "Bash"]),
        role_card_path=d.get("role_card_path", ""),
        isolation=d.get("isolation", "worktree"),
        worktree_path=d.get("worktree_path", ""),
        env_allowlist=d.get("env_allowlist", []),
        discussion=d.get("discussion"),
        pr=d.get("pr"),
        agent_id=d.get("agent_id"),
        sdk_eligible=bool(d.get("sdk_eligible", False)),
        untrusted_content=d.get("untrusted_content", {}),
    )


def _make_agent_id(spec_dict: dict[str, Any]) -> str:
    import time as _time
    role = spec_dict.get("role", "unknown")
    disc = spec_dict.get("discussion", "nod")
    return f"{role}-{disc}-{int(_time.time())}"


def _record_cc_route(agent_id: str, spec_dict: dict[str, Any]) -> None:
    """Record routed_via="cc" in agent_run for a dispatcher-routed CC spawn.

    Non-fatal: any failure is logged and swallowed. The CC runner
    (spawn-agent.sh + SubagentStop hook) owns the full lifecycle row; this
    call only writes the route column so _routing_counts can use the real
    column instead of the used_usd proxy.
    """
    try:
        from backend.agent_run_tracker import start_run, complete_run  # noqa: PLC0415
        role = spec_dict.get("role", "unknown")
        discussion = spec_dict.get("discussion")
        pr = spec_dict.get("pr")
        start_run(
            agent_id=agent_id,
            role=role,
            discussion=discussion,
            pr=pr,
            event_id=agent_id,
        )
        complete_run(
            agent_id=agent_id,
            routed_via="cc",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("_record_cc_route failed (non-fatal): %s", exc)


def _maybe_decrement_credit(tracker: Any, result: Any) -> None:
    """Estimate USD cost from token counts and decrement tracker.

    Phase 1 uses a conservative estimate based on claude-sonnet-4-6 pricing.
    Phase 2 will use the actual cost from the Anthropic billing API (AC1).

    TODO (Phase 2): replace estimate with balance-endpoint reconciliation to
    verify credit-pool attribution (AC1 blocker).
    """
    # claude-sonnet-4-6 approximate pricing (2026-05 estimate)
    # $3 / 1M input tokens, $15 / 1M output tokens
    input_cost = (result.input_tokens / 1_000_000) * 3.0
    output_cost = (result.output_tokens / 1_000_000) * 15.0
    estimated_cost = round(input_cost + output_cost, 6)
    if estimated_cost > 0:
        try:
            tracker.decrement(estimated_cost)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to decrement credit tracker: %s", e)


def _emit_credit_warning(remaining_usd: float) -> None:
    """Log a visible warning when credit drops to the soft-cap threshold."""
    msg = (
        f"[orchestrator] SDK credit soft-cap warning: "
        f"${remaining_usd:.2f} remaining of $200.00 monthly credit. "
        f"Approach the $0 limit will hard-stop SDK spawns."
    )
    logger.warning(msg)
    print(msg, file=sys.stderr)

    # Post to team-log only when the SDK dispatcher is genuinely live:
    # ROUTE_VIA_DISPATCHER=1 AND not running under pytest.
    # Under tests or with the gate off, the warning above is enough —
    # posting to the live team-log would be misleading (no real credit consumed).
    _is_live = (
        os.environ.get("ROUTE_VIA_DISPATCHER") == "1"
        and "PYTEST_CURRENT_TEST" not in os.environ
    )
    if not _is_live:
        return

    try:
        repo_root = Path(__file__).resolve().parent.parent.parent
        rotate_script = repo_root / "scripts" / "rotate-team-log.sh"
        if rotate_script.exists():
            subprocess.run(
                ["bash", str(rotate_script), "comment", msg],
                capture_output=True,
                timeout=15,
                cwd=str(repo_root),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not post credit warning to team-log: %s", e)


# ---------------------------------------------------------------------------
# CLI entry point (stdin JSON → stdout JSON)
# ---------------------------------------------------------------------------

def main() -> None:
    """Read spawn spec JSON from stdin, write result JSON to stdout."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        raw = sys.stdin.read()
        spec_dict = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        result = {"route": "error", "run_id": None, "verdict": "fail", "error": f"Invalid JSON: {e}"}
        print(json.dumps(result))
        sys.exit(1)

    result = route(spec_dict)
    print(json.dumps(result))
    # Exit non-zero only on hard error (so spawn-agent.sh can detect failure)
    if result.get("verdict") == "fail" and result.get("route") not in ("cc", "routed_to_cc"):
        sys.exit(1)


if __name__ == "__main__":
    main()
