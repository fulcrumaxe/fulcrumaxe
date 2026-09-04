"""backend/orchestrator/offload_policy.py — SDK selective-offload routing policy.

The SDK is an OFFLOAD LANE, not a replacement for Claude Code.

A spawn is eligible for SDK routing ONLY when ALL of the following are true:

  1. The spawn is EXPLICITLY flagged ``sdk_eligible=True`` for that spawn.
     There is NO automatic spill, NO alternation, and NO capacity-based routing.
     An executor or reviewer that happens to be spawned at a busy moment does NOT
     silently migrate to the SDK.

  2. The spawn's role is one of the low-stakes background roles defined in
     ``SDK_ELIGIBLE_ROLES`` below.  Roles that touch code quality, security,
     user-visible output, or the control plane (executor, code-reviewer,
     security-reviewer, acceptance-tester, project-manager, team-lead) are
     explicitly excluded.

Why separate low-stakes roles into a frozenset rather than an allowlist in
``dispatch.py``?  Two reasons:

  - Module-per-feature principle: the routing policy is a standalone concern,
    easy to audit and test in isolation.
  - The eligible set will grow slowly and deliberately.  Keeping it here makes
    accidental additions (e.g. a PR that touches dispatch.py for an unrelated
    reason) less likely to sneak in new roles.

Usage::

    from backend.orchestrator.offload_policy import is_offload_eligible

    if is_offload_eligible(spec.role, spec.sdk_eligible):
        # route to SDK
    else:
        # route to CC (main path)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Eligible roles
# ---------------------------------------------------------------------------

#: Roles that are permitted to run on the SDK offload lane.
#:
#: Criteria for inclusion:
#:   - Read-heavy, low-mutation (scan, summarise, report) work
#:   - No direct code changes committed to the repo
#:   - No verdict that gates a PR merge (code-review-passed, security-review-passed,
#:     acceptance-passed labels are NOT issued by these roles)
#:   - Safe to retry or discard if the SDK run produces a wrong answer
#:
#: Criteria for EXCLUSION (must stay on CC / main path):
#:   - executor — writes code, creates PRs
#:   - code-reviewer — issues code-review-passed label
#:   - security-reviewer — issues security-review-passed label
#:   - acceptance-tester — issues acceptance-passed label
#:   - project-manager — writes Specs, controls Discussion status
#:   - team-lead / orchestration roles — control-plane work
SDK_ELIGIBLE_ROLES: frozenset[str] = frozenset({
    "docs-writer",
    "run-analyst",
    "quality-sweep",
    "feedback-scanner",
    "mission-analyst",
})


# ---------------------------------------------------------------------------
# Policy function
# ---------------------------------------------------------------------------

def is_offload_eligible(role: str, sdk_eligible: bool) -> bool:
    """Return True when this spawn should be routed to the SDK offload lane.

    BOTH conditions must hold:

    1. ``sdk_eligible`` is True — the caller explicitly opted the spawn into
       the SDK lane.  Default is False; no spawn migrates to SDK without an
       explicit flag.

    2. ``role`` is in ``SDK_ELIGIBLE_ROLES`` — even if a caller mistakenly
       passes ``sdk_eligible=True`` for an executor or reviewer, the policy
       function hard-blocks the upgrade.  Executors and reviewers stay on the
       main path unconditionally.

    Parameters
    ----------
    role:
        The agent role string (e.g. ``"docs-writer"``, ``"executor"``).
    sdk_eligible:
        Explicit opt-in flag from the spawn spec.  Must be ``True`` for the
        SDK lane to be considered.  Defaults to ``False`` in ``SpawnSpec``.

    Returns
    -------
    bool
        ``True`` → route to SDK (when credit is also available).
        ``False`` → route to CC (main path).

    Examples
    --------
    >>> is_offload_eligible("docs-writer", sdk_eligible=True)
    True
    >>> is_offload_eligible("docs-writer", sdk_eligible=False)
    False
    >>> is_offload_eligible("executor", sdk_eligible=True)
    False
    >>> is_offload_eligible("code-reviewer", sdk_eligible=True)
    False
    >>> is_offload_eligible("unknown-role", sdk_eligible=True)
    False
    """
    return sdk_eligible and role in SDK_ELIGIBLE_ROLES
