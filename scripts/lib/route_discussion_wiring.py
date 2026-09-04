"""scripts/lib/route_discussion_wiring.py — side-effects layer for the Discussion router.

Handles:
  - stdin/stdout JSON I/O
  - control-plane gate check (gates.cost_aware_router)
  - body sanitization before embedding into executor prompts
  - /route:<directive> override parsing (requires Team Lead signature)
  - audit log write to .autonomous-team/route-decisions.jsonl

The pure routing logic lives in route_discussion.py — this module is the
shell around it.

CLI usage:
  echo '{"discussion":836,"body":"...","labels":["Feature"]}' | python3 route_discussion_wiring.py
  # stdout: routing decision JSON (or null if gate is off)
  # stderr: audit log write confirmation
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_AUDIT_LOG = _REPO_ROOT / ".autonomous-team" / "route-decisions.jsonl"
_BODY_MAX_LEN = 4000

# Control-plane tokens stripped from body before passing to executor.
_SANITIZE_PATTERNS = [
    re.compile(r"SPAWN_REQUEST[^\n]*\n?", re.MULTILINE),
    re.compile(r"TERMINATE_REQUEST[^\n]*\n?", re.MULTILINE),
    re.compile(r"STATUS:[A-Z_]+[^\n]*\n?", re.MULTILINE),
    re.compile(r"<!--.*?-->", re.DOTALL),  # strip all HTML comments (incl. AGENT_OUTPUT blocks)
]


# ---------------------------------------------------------------------------
# Control-plane gate check
# ---------------------------------------------------------------------------


def _gate_enabled() -> bool:
    """Return True if gates.cost_aware_router is enabled (default: True)."""
    try:
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "control_plane.py"),
             "get", "gates.cost_aware_router"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            val = result.stdout.strip().strip('"').lower()
            return val not in ("false", "0", "no")
    except Exception:
        pass
    # Default: fail-closed — if we can't read the gate, don't route. Safer than
    # silently activating the router on a misconfigured control-plane subprocess.
    return False


# ---------------------------------------------------------------------------
# Body sanitization
# ---------------------------------------------------------------------------


def sanitize_body(body: str) -> str:
    """Strip control-plane tokens from *body* and cap at _BODY_MAX_LEN chars.

    Never modify the discussion body in-place — operates on a copy.
    Called by the wiring layer before embedding body into executor prompt.
    """
    sanitized = body
    for pattern in _SANITIZE_PATTERNS:
        sanitized = pattern.sub("", sanitized)
    return sanitized[:_BODY_MAX_LEN]


# ---------------------------------------------------------------------------
# Override parsing
# ---------------------------------------------------------------------------


def _parse_override(comments: list[dict], boss_username: str) -> Optional[dict]:
    """Scan Discussion comments for /route:<directive> override.

    Valid signer:
      - comment author == boss_github_username (configured in control plane).

    The '[team-lead-signed]' prefix bypass was removed — any commenter could
    write that string to forge an override on a security-sensitive Discussion.
    Only author identity is trusted.

    Returns dict with {route, override_signer} or None.
    """
    for comment in comments:
        author = comment.get("author", {}).get("login", "") if isinstance(comment.get("author"), dict) else comment.get("author", "")
        body = comment.get("body", "")

        if author != boss_username:
            continue

        match = re.search(r"/route:\s*(\S+)", body)
        if match:
            return {
                "route": match.group(1).strip(),
                "override_signer": author,
            }
    return None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _write_audit(record: dict) -> None:
    """Append a routing decision to the audit log.

    Fields written: discussion, route, reason, recommended_model (from
    model_tier_hint), actual_model (the model that will be used for the
    spawned agent), model_tier_hint (kept for back-compat), labels_hash,
    decided_at, shadow (True when gate is off), override_signer (optional).

    Body text is NEVER written to the audit log.
    """
    safe = {
        "discussion": record.get("discussion"),
        "route": record.get("route"),
        "reason": record.get("reason"),
        "recommended_model": record.get("model_tier_hint"),
        "actual_model": record.get("actual_model"),
        "model_tier_hint": record.get("model_tier_hint"),
        "labels_hash": record.get("labels_hash"),
        "decided_at": record.get("decided_at"),
    }
    if record.get("shadow"):
        safe["shadow"] = True
    if "override_signer" in record:
        safe["override_signer"] = record["override_signer"]

    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOG.open("a") as fh:
            fh.write(json.dumps(safe) + "\n")
    except OSError:
        pass  # Non-fatal — routing continues without audit write


# ---------------------------------------------------------------------------
# Main wiring function
# ---------------------------------------------------------------------------


def route_with_wiring(
    discussion: int,
    body: str,
    labels: list[str],
    comments: Optional[list[dict]] = None,
    boss_username: str = "",
    actual_model: Optional[str] = None,
) -> Optional[dict]:
    """Run the router with side effects (gate check, override, audit log).

    Returns routing decision dict when the gate is enabled, or None when
    the gate is disabled (shadow mode).

    Shadow logging: the audit row is ALWAYS written regardless of gate state,
    so route-decisions.jsonl captures the model decision for observability
    even when cost_aware_router is off.  The gate only controls whether the
    routing decision is returned to the caller (i.e. whether it affects
    spawning behavior).

    Parameters
    ----------
    discussion:   Discussion number.
    body:         Raw Discussion body (used for routing logic only — never
                  logged to audit).
    labels:       Discussion label list.
    comments:     Optional list of Discussion comments for override parsing.
    boss_username: GitHub login allowed to sign /route: overrides.
    actual_model: The model that will actually be used for the spawned agent
                  (e.g. from the role's .claude/agents/<role>.md frontmatter).
                  Logged in the audit row alongside recommended_model so the
                  two can be compared downstream.

    The returned dict is safe to embed in spawn prompts — it contains no
    body excerpts.  Call sanitize_body() separately when building the prompt.
    """
    gate_on = _gate_enabled()

    # Always compute the routing decision for shadow logging — even when the
    # gate is off, we want the audit row so we can observe what WOULD have
    # been routed.
    # Import here to keep the pure function isolated from the wiring module.
    from route_discussion import route  # type: ignore[import]

    decision = route(discussion=discussion, body=body, labels=labels)

    # Check for manual override in Discussion comments (only meaningful when
    # gate is on, but apply to audit record regardless for observability).
    if comments and boss_username:
        override = _parse_override(comments, boss_username)
        if override:
            decision = dict(decision)
            decision["route"] = override["route"]
            decision["reason"] = "manual_override"
            decision["override_signer"] = override["override_signer"]

    # Build audit record.  Shadow flag marks gate-off rows clearly.
    audit_record = dict(decision, discussion=discussion)
    audit_record["actual_model"] = actual_model
    if not gate_on:
        audit_record["shadow"] = True
    _write_audit(audit_record)

    # Fail-closed on the behavior path: return None when gate is off so
    # the caller never applies routing logic unless explicitly enabled.
    if not gate_on:
        return None

    return decision


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    payload = json.load(sys.stdin)
    result = route_with_wiring(
        discussion=payload["discussion"],
        body=payload["body"],
        labels=payload.get("labels", []),
        comments=payload.get("comments"),
        boss_username=payload.get("boss_username", ""),
    )
    if result is None:
        print("null")
    else:
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
