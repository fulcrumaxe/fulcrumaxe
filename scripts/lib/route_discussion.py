"""scripts/lib/route_discussion.py — cost-aware Discussion router.

Pure function. No file I/O, no network, no side effects.

Stdin/stdout JSON contract (CLI usage):
  echo '{"discussion":836,"body":"...","labels":["Feature"]}' | python3 route_discussion.py

Input:
  {"discussion": int, "body": str, "labels": [str]}

Output:
  {"route": str, "reason": str, "model_tier_hint": str,
   "labels_hash": str, "decided_at": str}

Routes (in priority order):
  direct-executor     — [Small] + body<500 + no denylist
  executor+reviewer   — [Bug] + body<2000 + no denylist
                        (denylist match on Bug → consensus-panel per Spec AC2)
  consensus-panel     — [Critical] | [Strategy] | denylist | body>3000 | ext-dep keywords
                        | [Feature] (default for Features without a more specific route)
  (no extra routes — Spec defines exactly 3 active routes)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Security-adjacent path/keyword denylist — when any pattern matches the
# Discussion body, the route is forced to consensus-panel (per D#836 Spec).
# Patterns are regex strings (case-insensitive) so we can use word boundaries
# for short tokens and avoid over-escalating on common English words.
_DENYLIST: tuple[str, ...] = (
    r"hooks/",
    r"scripts/lib/",
    r"\.claude/agents/",
    r"backend/sandbox",
    r"\.env(?:\b|/)",
    r"settings\.json",
    r"settings\.local\.json",
    r"\bauth\b",
    r"\bsecrets?\b",
    r"manifest\.json",
    r"host_permissions",
)

# External-dependency keywords that escalate to consensus-panel.
_EXT_DEP_KEYWORDS: tuple[str, ...] = (
    "npm",
    "pip",
    "cargo",
    "RFC",
    "W3C",
    "mcp",
    "sdk",
    "crate",
)

# Route constants.
ROUTE_DIRECT_EXECUTOR = "direct-executor"
ROUTE_EXECUTOR_REVIEWER = "executor+reviewer"
ROUTE_CONSENSUS_PANEL = "consensus-panel"

# Model tier hints per route.
_MODEL_TIER: dict[str, str] = {
    ROUTE_DIRECT_EXECUTOR: "haiku",
    ROUTE_EXECUTOR_REVIEWER: "sonnet",
    ROUTE_CONSENSUS_PANEL: "opus",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _labels_hash(labels: list[str]) -> str:
    """sha256 of sorted, joined labels — stable for any ordering."""
    joined = ",".join(sorted(labels))
    return hashlib.sha256(joined.encode()).hexdigest()


def _has_label(labels: list[str], target: str) -> bool:
    """Case-insensitive label check (brackets stripped for matching)."""
    target_clean = target.strip("[]").lower()
    for lbl in labels:
        if lbl.strip("[]").lower() == target_clean:
            return True
    return False


def _denylist_match(body: str) -> Optional[str]:
    """Return the first denylist pattern that matches *body*, or None.

    Patterns in _DENYLIST are already regex strings with appropriate boundary
    anchors (word boundaries for short tokens; literal paths for file paths).
    """
    for pattern in _DENYLIST:
        if re.search(pattern, body, re.IGNORECASE):
            return pattern
    return None


def _ext_dep_match(body: str) -> Optional[str]:
    """Return the first external-dep keyword found in *body*, or None."""
    for keyword in _EXT_DEP_KEYWORDS:
        # Word-boundary match so 'npm' doesn't match 'npmrc' or 'rnpm'.
        if re.search(r"\b" + re.escape(keyword) + r"\b", body, re.IGNORECASE):
            return keyword
    return None


def _has_multi_pr(labels: list[str], body: str) -> bool:
    """Return True when the Discussion indicates multi-PR scope."""
    # Explicit label
    if _has_label(labels, "multi-pr"):
        return True
    # Body contains a numbered list with >3 sub-PRs (e.g. "1. PR #1\n2. PR #2...")
    sub_pr_matches = re.findall(
        r"(?:^|\n)\s*\d+\.\s+(?:PR\s*#\d+|sub-PR|phase\s+\d)", body, re.IGNORECASE
    )
    return len(sub_pr_matches) > 3


# ---------------------------------------------------------------------------
# Core router — pure function, LRU-cached
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _route_cached(
    discussion: int,
    body: str,
    labels_tuple: tuple[str, ...],
    labels_hash: str,
) -> dict:
    """Cached routing logic.  Key = (discussion, labels_hash).

    Cache entry is invalidated automatically when labels_hash changes (different
    key → miss → fresh computation).
    """
    labels = list(labels_tuple)
    body_len = len(body)

    # ── Pre-compute shared signals ───────────────────────────────────────────
    deny_token = _denylist_match(body)
    ext_dep = _ext_dep_match(body)

    # ── Rule 1: direct-executor ──────────────────────────────────────────────
    # Must have no denylist match (security hard rule).
    if _has_label(labels, "Small") and body_len < 500 and not deny_token:
        return {"route": ROUTE_DIRECT_EXECUTOR, "reason": "[Small] label + body<500"}

    # ── Rule 2: executor+reviewer (Bug route) ────────────────────────────────
    # Denylist match on Bug → escalate to consensus-panel (per D#836 Spec AC2).
    # The Spec only defines 4 routes; security-adjacent Bug must reach the full
    # specialist panel, not a hybrid "executor+reviewer+security" we'd invent.
    if _has_label(labels, "Bug") and body_len < 2000:
        if deny_token:
            return {
                "route": ROUTE_CONSENSUS_PANEL,
                "reason": f"[Bug] label + security-adjacent match {deny_token!r} — full panel required",
            }
        return {
            "route": ROUTE_EXECUTOR_REVIEWER,
            "reason": "[Bug] label + body<2000",
        }

    # ── Rule 3: consensus-panel escalators ───────────────────────────────────
    if deny_token:
        return {
            "route": ROUTE_CONSENSUS_PANEL,
            "reason": f"security-adjacent denylist match: {deny_token!r}",
        }

    if _has_label(labels, "Critical"):
        return {"route": ROUTE_CONSENSUS_PANEL, "reason": "[Critical] label"}

    if _has_label(labels, "Strategy"):
        return {"route": ROUTE_CONSENSUS_PANEL, "reason": "[Strategy] label"}

    if body_len > 3000:
        return {
            "route": ROUTE_CONSENSUS_PANEL,
            "reason": f"body length {body_len} > 3000 chars",
        }

    if ext_dep:
        return {
            "route": ROUTE_CONSENSUS_PANEL,
            "reason": f"external-dep keyword: {ext_dep!r}",
        }

    # ── Default: consensus-panel for Feature and anything unmatched ────────
    # [Feature] discussions route to consensus-panel (full specialist review).
    if _has_label(labels, "Feature"):
        return {
            "route": ROUTE_CONSENSUS_PANEL,
            "reason": "[Feature] label — routes to consensus-panel",
        }

    return {"route": ROUTE_CONSENSUS_PANEL, "reason": "no matching route rule — defaulting to consensus-panel"}


def route(discussion: int, body: str, labels: list[str]) -> dict:
    """Route a Discussion to the appropriate agent workflow.

    Args:
        discussion: Discussion number (int).
        body:       Raw Discussion body text (str).
        labels:     List of label names attached to the Discussion.

    Returns:
        {
            "route": str,
            "reason": str,
            "model_tier_hint": str,
            "labels_hash": str,
            "decided_at": str,  # ISO8601 UTC
        }
    """
    lhash = _labels_hash(labels)
    decision = _route_cached(
        discussion=discussion,
        body=body,
        labels_tuple=tuple(labels),
        labels_hash=lhash,
    )
    route_name = decision["route"]
    return {
        "route": route_name,
        "reason": decision["reason"],
        "model_tier_hint": _MODEL_TIER.get(route_name, "sonnet"),
        "labels_hash": lhash,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    payload = json.load(sys.stdin)
    result = route(
        discussion=payload["discussion"],
        body=payload["body"],
        labels=payload.get("labels", []),
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
