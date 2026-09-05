"""
Tests for backend/api.py _enrich_project() — ACs 1-5 of Discussion #611.

ACs covered:
  1. grep -rn 'from backend.registry import Registry\\b' returns no matches
  2. check_loop_health returning {"healthy": False, "reason": "no loop-runs logs found"}
     yields health: "healthy"
  3. DiscussionRegistry.stats → {total: 28, in_progress: 2} → momentum: "steady"
  4. check_loop_health → {"healthy": False, "reason": "no loop-runs logs found"} → health: "healthy"
  5. check_loop_health → {"healthy": False, "reason": "loop stalled 60min"} → health: "degraded"
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make backend importable from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.api as api_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_project() -> dict:
    return {
        "id": "test-proj",
        "name": "Test Project",
        "repo": "org/repo",
        "createdAt": "2026-05-01T00:00:00Z",
    }


def _call_enrich(
    *,
    reg_stats: dict | None = None,
    loop_health: dict | None = None,
    active_agents: int = 0,
) -> dict:
    """Call _enrich_project with stubbed DiscussionRegistry and check_loop_health."""
    if reg_stats is None:
        reg_stats = {"total": 0, "done": 0, "in_progress": 0}
    if loop_health is None:
        loop_health = {"healthy": True, "status": "healthy"}

    mock_reg_instance = MagicMock()
    mock_reg_instance.stats.return_value = reg_stats

    mock_agent_cards_instance = MagicMock()
    mock_agent_cards_instance.list_agents.return_value = ["x"] * active_agents

    with (
        patch("backend.registry.DiscussionRegistry", return_value=mock_reg_instance),
        patch("backend.api.check_loop_health", return_value=loop_health),
        patch("backend.api.AgentCards", return_value=mock_agent_cards_instance),
    ):
        return api_module._enrich_project(_minimal_project())


# ---------------------------------------------------------------------------
# AC 1 — no "from backend.registry import Registry" references
# ---------------------------------------------------------------------------

class TestNoLegacyRegistryImport:
    def test_no_plain_registry_import_in_backend(self) -> None:
        """AC-1: grep for 'from backend.registry import Registry' returns no matches."""
        backend_dir = Path(__file__).resolve().parent.parent / "backend"
        matches = []
        for py_file in backend_dir.rglob("*.py"):
            try:
                text = py_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                # Match the old wrong name but not DiscussionRegistry
                if "from backend.registry import Registry" in line and "DiscussionRegistry" not in line:
                    matches.append(f"{py_file.relative_to(backend_dir.parent)}:{lineno}: {line.strip()}")
        assert matches == [], (
            "Found legacy 'from backend.registry import Registry' (non-Discussion) imports:\n"
            + "\n".join(matches)
        )


# ---------------------------------------------------------------------------
# AC 2 / AC 4 — "no loop-runs logs found" treated as healthy
# ---------------------------------------------------------------------------

class TestEnrichProjectLoopHealthNoLogs:
    def test_no_loop_runs_logs_yields_healthy(self) -> None:
        """AC-2 / AC-4: reason='no loop-runs logs found' → health='healthy'."""
        result = _call_enrich(
            loop_health={
                "healthy": False,
                "status": "error",
                "reason": "no loop-runs logs found",
            }
        )
        assert result["health"] == "healthy", (
            f"Expected health='healthy' when reason='no loop-runs logs found', got {result['health']!r}"
        )

    def test_no_loop_runs_logs_does_not_degrade(self) -> None:
        """Regression: must not return 'degraded' for the interactive-session case."""
        result = _call_enrich(
            loop_health={
                "healthy": False,
                "reason": "no loop-runs logs found",
            }
        )
        assert result["health"] != "degraded"


# ---------------------------------------------------------------------------
# AC 3 — DiscussionRegistry.stats drives momentum
# ---------------------------------------------------------------------------

class TestEnrichProjectMomentum:
    def test_total_28_in_progress_2_gives_steady(self) -> None:
        """AC-3: total=28, in_progress=2 → momentum='steady'."""
        result = _call_enrich(reg_stats={"total": 28, "done": 20, "in_progress": 2})
        assert result["momentum"] == "steady", (
            f"Expected momentum='steady' for in_progress=2/total=28, got {result['momentum']!r}"
        )

    def test_in_progress_4_gives_accelerating(self) -> None:
        result = _call_enrich(reg_stats={"total": 10, "done": 3, "in_progress": 4})
        assert result["momentum"] == "accelerating"

    def test_total_0_gives_stalled(self) -> None:
        result = _call_enrich(reg_stats={"total": 0, "done": 0, "in_progress": 0})
        assert result["momentum"] == "stalled"

    def test_in_progress_0_nonzero_total_gives_steady(self) -> None:
        result = _call_enrich(reg_stats={"total": 5, "done": 5, "in_progress": 0})
        assert result["momentum"] == "steady"


# ---------------------------------------------------------------------------
# AC 5 — a real staleness reason degrades health
# ---------------------------------------------------------------------------

class TestEnrichProjectHealthDegradedOnStale:
    def test_loop_stalled_gives_degraded(self) -> None:
        """AC-5: reason='loop stalled 60min' (healthy=False) → health='degraded'."""
        result = _call_enrich(
            loop_health={
                "healthy": False,
                "status": "error",
                "reason": "loop stalled 60min",
            }
        )
        assert result["health"] == "degraded", (
            f"Expected health='degraded' for loop stalled, got {result['health']!r}"
        )

    def test_healthy_true_gives_healthy(self) -> None:
        result = _call_enrich(loop_health={"healthy": True, "status": "healthy"})
        assert result["health"] == "healthy"

    def test_exception_in_check_loop_health_defaults_to_healthy(self) -> None:
        """When check_loop_health raises, _enrich_project defaults to healthy=True."""
        mock_reg_instance = MagicMock()
        mock_reg_instance.stats.return_value = {"total": 5, "in_progress": 1}

        mock_agent_cards_instance = MagicMock()
        mock_agent_cards_instance.list_agents.return_value = []

        with (
            patch("backend.registry.DiscussionRegistry", return_value=mock_reg_instance),
            patch("backend.api.check_loop_health", side_effect=RuntimeError("boom")),
            patch("backend.api.AgentCards", return_value=mock_agent_cards_instance),
        ):
            result = api_module._enrich_project(_minimal_project())

        assert result["health"] == "healthy"
