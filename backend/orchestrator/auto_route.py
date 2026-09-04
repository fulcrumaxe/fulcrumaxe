"""backend/orchestrator/auto_route.py — SDK_AUTO_ROUTE gate for eligible roles.

When the environment variable SDK_AUTO_ROUTE=1 is set, spawns of offload-eligible
low-stakes roles are automatically treated as sdk_eligible=True — so they use the
proven SDK lane without each spawn needing the manual --sdk-lane flag.

DEFAULT: OFF.  Setting SDK_AUTO_ROUTE=1 is the only way to enable this behaviour.

Safety invariants:
  - When SDK_AUTO_ROUTE is unset or not "1", this module has zero effect on routing.
  - Non-eligible roles (executor, code-reviewer, security-reviewer, acceptance-tester,
    project-manager, team-lead, etc.) NEVER auto-route — should_auto_route() reuses
    SDK_ELIGIBLE_ROLES from offload_policy, so the role gate is enforced here before
    the caller even reads the return value.
  - The decision is pure (no side-effects); callers are responsible for audit logging.

Usage::

    from backend.orchestrator.auto_route import should_auto_route

    if should_auto_route(spec.role):
        # treat this spawn as sdk_eligible=True
        ...
"""

from __future__ import annotations

import os

from backend.orchestrator.offload_policy import SDK_ELIGIBLE_ROLES


def should_auto_route(role: str) -> bool:
    """Return True when SDK_AUTO_ROUTE=1 AND role is in SDK_ELIGIBLE_ROLES.

    Parameters
    ----------
    role:
        The agent role string (e.g. ``"docs-writer"``, ``"executor"``).

    Returns
    -------
    bool
        ``True``  → auto-route this spawn to SDK (caller should treat sdk_eligible=True).
        ``False`` → no auto-routing; preserve the original sdk_eligible flag as-is.

    Examples
    --------
    >>> import os
    >>> os.environ["SDK_AUTO_ROUTE"] = "0"
    >>> should_auto_route("docs-writer")
    False
    >>> os.environ["SDK_AUTO_ROUTE"] = "1"
    >>> should_auto_route("docs-writer")
    True
    >>> should_auto_route("executor")
    False
    >>> should_auto_route("code-reviewer")
    False
    >>> del os.environ["SDK_AUTO_ROUTE"]
    >>> should_auto_route("docs-writer")
    False
    """
    return os.environ.get("SDK_AUTO_ROUTE") == "1" and role in SDK_ELIGIBLE_ROLES
