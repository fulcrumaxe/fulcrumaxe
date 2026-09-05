"""tests/backend/test_project_status.py

Acceptance tests for the _enrich_project fix (Discussion #1014).

AC1: activeAgents comes from fleet concurrency table, not role catalog.
     availableRoles is a separate field for the catalog count.
AC2: idle system (no loop logs) → health:"healthy", healthReason:"no loop activity"
AC3: actual loop failure → health:"degraded", healthReason set.
"""
from __future__ import annotations

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_PROJECT = {"id": "af", "name": "autonomous-forever", "repo": "org/repo", "createdAt": "2026-01-01"}


def _call_enrich(project=None, *, loop_health=None, active_count=0, catalog_count=24, liveness="idle"):
    """Call _enrich_project with controlled mocks."""
    from backend.api import _enrich_project

    if project is None:
        project = _SAMPLE_PROJECT

    default_loop_health = {
        "healthy": False,
        "status": "error",
        "lastRunAt": None,
        "reason": "no loop-runs logs found",
    }

    with (
        patch("backend.api.check_loop_health", return_value=loop_health or default_loop_health),
        patch("backend.api.AgentCards") as mock_ac_cls,
        patch("backend.api._probe_liveness", return_value=liveness),
    ):
        # Mock: list_agents returns N objects (catalog)
        mock_ac_cls.return_value.list_agents.return_value = ["role"] * catalog_count

        # Patch fleet count_project inside the function's import
        with patch("backend.fleet.concurrency.count_project", return_value=active_count):
            return _enrich_project(project)


# ---------------------------------------------------------------------------
# AC1: activeAgents vs availableRoles
# ---------------------------------------------------------------------------


def test_active_agents_uses_fleet_count_not_catalog():
    """activeAgents must reflect fleet concurrency rows, not role catalog size."""
    result = _call_enrich(active_count=3, catalog_count=24)
    assert result["activeAgents"] == 3, (
        f"activeAgents should be 3 (fleet rows), got {result['activeAgents']}"
    )


def test_available_roles_reflects_catalog():
    """availableRoles must reflect the count from AgentCards.list_agents()."""
    result = _call_enrich(active_count=0, catalog_count=24)
    assert result["availableRoles"] == 24, (
        f"availableRoles should be 24 (catalog), got {result.get('availableRoles')}"
    )


def test_active_agents_drops_to_zero_when_fleet_empty():
    """When fleet is empty, activeAgents is 0 regardless of catalog size."""
    result = _call_enrich(active_count=0, catalog_count=24)
    assert result["activeAgents"] == 0


def test_active_and_available_are_independent():
    """activeAgents and availableRoles are independent fields."""
    result = _call_enrich(active_count=5, catalog_count=24)
    assert result["activeAgents"] == 5
    assert result["availableRoles"] == 24
    assert result["activeAgents"] != result["availableRoles"]


# ---------------------------------------------------------------------------
# AC2: idle system — no loop logs → healthy, not degraded
# ---------------------------------------------------------------------------


def test_idle_system_health_is_healthy():
    """No loop logs at all: health should be 'healthy', not 'degraded'."""
    idle_health = {
        "healthy": False,
        "status": "error",
        "lastRunAt": None,
        "reason": "no loop-runs logs found",
    }
    result = _call_enrich(loop_health=idle_health)
    assert result["health"] == "healthy", (
        f"Idle system should report healthy, got {result['health']!r}"
    )


def test_idle_system_has_health_reason():
    """No loop logs: healthReason should explain 'no loop activity'."""
    idle_health = {
        "healthy": False,
        "status": "error",
        "lastRunAt": None,
        "reason": "no loop-runs logs found",
    }
    result = _call_enrich(loop_health=idle_health)
    assert result.get("healthReason") == "no loop activity", (
        f"Expected 'no loop activity', got {result.get('healthReason')!r}"
    )


def test_idle_system_no_degraded_badge():
    """Idle system must NOT return health:'degraded'."""
    idle_health = {
        "healthy": False,
        "status": "error",
        "lastRunAt": None,
        "reason": "no loop-runs logs found",
    }
    result = _call_enrich(loop_health=idle_health)
    assert result["health"] != "degraded", "Idle system incorrectly reports degraded"


# ---------------------------------------------------------------------------
# AC3: real loop failure → degraded with a reason
# ---------------------------------------------------------------------------


def test_stale_loop_is_degraded():
    """Loop ran but is now stale (warning) → health:'degraded'."""
    stale_health = {
        "healthy": False,
        "status": "warning",
        "lastRunAt": "2026-05-18T00:00:00Z",
        "age_minutes": 45,
    }
    result = _call_enrich(loop_health=stale_health)
    assert result["health"] == "degraded", (
        f"Stale loop should be degraded, got {result['health']!r}"
    )
    assert result.get("healthReason") is not None, "healthReason should be set when degraded"


def test_healthy_loop_no_health_reason():
    """Healthy loop: no healthReason key in response."""
    good_health = {
        "healthy": True,
        "status": "healthy",
        "lastRunAt": "2026-05-18T12:00:00Z",
        "age_minutes": 5,
    }
    result = _call_enrich(loop_health=good_health)
    assert result["health"] == "healthy"
    assert result.get("healthReason") is None, (
        f"Healthy loop should have no healthReason, got {result.get('healthReason')!r}"
    )


def test_error_with_last_run_is_degraded():
    """Error status with a real lastRunAt (loop ran but is broken) → degraded."""
    error_health = {
        "healthy": False,
        "status": "error",
        "lastRunAt": "2026-05-17T22:00:00Z",
        "age_minutes": 800,
        "reason": "loop timeout",
    }
    result = _call_enrich(loop_health=error_health)
    assert result["health"] == "degraded"
    assert "loop" in (result.get("healthReason") or ""), (
        f"healthReason should mention loop, got {result.get('healthReason')!r}"
    )
