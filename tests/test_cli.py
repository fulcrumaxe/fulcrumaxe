"""
Tests for backend/cli.py — unified af CLI entry point.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.cli import main, _build_parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> tuple[int, str]:
    """Run main(argv), capturing stdout. Returns (exit_code, stdout_text)."""
    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    try:
        with redirect_stdout(out):
            code = main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue()


# ---------------------------------------------------------------------------
# 1. Help text lists all subcommands
# ---------------------------------------------------------------------------


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    for cmd in ("status", "budget", "control", "kpi", "health", "registry", "agents", "blackboard", "serve"):
        assert cmd in captured.out, f"Expected '{cmd}' in help output"


# ---------------------------------------------------------------------------
# 2. Unknown subcommand exits with error
# ---------------------------------------------------------------------------


def test_unknown_subcommand_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-subcommand"])
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# 3. status --json produces valid JSON combining budget/queue/health/kpi
# ---------------------------------------------------------------------------


def test_status_json_valid(tmp_path):
    """status --json should print valid JSON with top-level keys."""
    mock_budget = {
        "ceiling": 5_000_000,
        "spent": 1_200_000,
        "remaining": 3_800_000,
        "per_agent_ceiling": 200_000,
        "warn_threshold_pct": 80,
        "agents": [],
    }
    mock_stats = {
        "total": 42,
        "done": 36,
        "in_progress": 2,
        "tasks_per_day": 8.5,
        "avg_days_to_complete": 0.9,
        "completion_count": 36,
    }
    mock_registry_data = {
        "discussions": [
            {"status": "SPEC_READY"},
            {"status": "DONE"},
            {"status": "IMPLEMENTING"},
        ]
    }
    mock_health = {
        "healthy": True,
        "last_run": "2026-04-10T12:00:00Z",
        "age_minutes": 3.2,
        "threshold_minutes": 30,
    }
    mock_kpi = {
        "version": 1,
        "computed_at": "2026-04-10T12:00:00Z",
        "velocity": {"last_24h": 3, "all_time_per_day": 8.5, "total_done": 36},
        "estimation_accuracy": {},
        "idle_rate": {},
        "pr_cycle_time": {"mean_hours": 2.1, "median_hours": 1.8, "total_measured": 10},
    }

    mock_bt = MagicMock()
    mock_bt.get_status.return_value = mock_budget

    mock_reg = MagicMock()
    mock_reg.stats.return_value = mock_stats
    mock_reg.load.return_value = mock_registry_data
    # D#2310: cli.py reads the SPEC_READY count off queue_summary()'s
    # open-only buckets rather than re-deriving it from reg.load().
    mock_reg.queue_summary.return_value = {
        "total": 42,
        "open_total": 3,
        "excluded_closed": 39,
        "buckets": {"SPEC_READY": 1, "DONE": 0, "IMPLEMENTING": 1},
        "done": 36,
        "synced_at": "2026-04-10T12:00:00Z",
    }

    kpi_path = tmp_path / "kpi.json"
    kpi_path.write_text(json.dumps(mock_kpi))

    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with (
        patch("backend.cli.BudgetTracker", return_value=mock_bt),
        patch("backend.cli.DiscussionRegistry", return_value=mock_reg),
        patch("backend.cli.check_loop_health", return_value=mock_health),
        patch("backend.cli.KPI_OUT", kpi_path),
        redirect_stdout(out),
    ):
        code = main(["status", "--json"])

    assert code == 0
    data = json.loads(out.getvalue())
    assert "budget" in data
    assert "queue" in data
    assert "health" in data
    assert "kpi" in data
    assert data["budget"]["ceiling"] == 5_000_000
    assert data["queue"]["total"] == 42
    assert data["queue"]["spec_ready"] == 1
    assert data["health"]["healthy"] is True


# ---------------------------------------------------------------------------
# 4. status (human-readable) outputs expected fields
# ---------------------------------------------------------------------------


def test_status_human_readable(tmp_path):
    """status without --json should print Budget, Queue, Loop, Velocity lines."""
    mock_budget = {
        "ceiling": 5_000_000,
        "spent": 1_200_000,
        "remaining": 3_800_000,
        "per_agent_ceiling": 200_000,
        "warn_threshold_pct": 80,
        "agents": [],
    }
    mock_stats = {
        "total": 42,
        "done": 36,
        "in_progress": 2,
        "tasks_per_day": 8.5,
        "avg_days_to_complete": 0.9,
        "completion_count": 36,
    }
    mock_registry_data = {"discussions": [{"status": "SPEC_READY"}]}
    mock_health = {"healthy": True, "age_minutes": 3.2, "threshold_minutes": 30}
    mock_kpi = {
        "velocity": {"all_time_per_day": 8.5, "total_done": 36},
        "pr_cycle_time": {"mean_hours": 2.1},
    }

    mock_bt = MagicMock()
    mock_bt.get_status.return_value = mock_budget

    mock_reg = MagicMock()
    mock_reg.stats.return_value = mock_stats
    mock_reg.load.return_value = mock_registry_data
    # D#2310: cli.py reads the SPEC_READY count off queue_summary()'s
    # open-only buckets rather than re-deriving it from reg.load().
    mock_reg.queue_summary.return_value = {
        "total": 42,
        "open_total": 3,
        "excluded_closed": 39,
        "buckets": {"SPEC_READY": 1},
        "done": 36,
        "synced_at": "2026-04-10T12:00:00Z",
    }

    kpi_path = tmp_path / "kpi.json"
    kpi_path.write_text(json.dumps(mock_kpi))

    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with (
        patch("backend.cli.BudgetTracker", return_value=mock_bt),
        patch("backend.cli.DiscussionRegistry", return_value=mock_reg),
        patch("backend.cli.check_loop_health", return_value=mock_health),
        patch("backend.cli.KPI_OUT", kpi_path),
        redirect_stdout(out),
    ):
        code = main(["status"])

    assert code == 0
    text = out.getvalue()
    assert "Budget:" in text
    assert "Queue:" in text
    assert "Loop:" in text
    assert "Velocity:" in text
    assert "1,200,000" in text
    assert "5,000,000" in text


# ---------------------------------------------------------------------------
# 5. Subcommand delegation — budget delegates to budget.main
# ---------------------------------------------------------------------------


def test_budget_delegates_to_budget_main():
    """budget subcommand must call backend.budget.main with correct argv."""
    with patch("backend.cli._delegate_budget") as mock_delegate:
        mock_delegate.return_value = 0
        code = main(["budget", "status"])
    mock_delegate.assert_called_once_with(["status"])
    assert code == 0


# ---------------------------------------------------------------------------
# 6. Subcommand delegation — control delegates to control_plane.main
# ---------------------------------------------------------------------------


def test_control_delegates_to_control_main():
    """control subcommand must call backend.control_plane.main with correct argv."""
    with patch("backend.cli._delegate_control") as mock_delegate:
        mock_delegate.return_value = 0
        code = main(["control", "gates"])
    mock_delegate.assert_called_once_with(["gates"])
    assert code == 0


# ---------------------------------------------------------------------------
# 7. Exit codes propagated from delegates
# ---------------------------------------------------------------------------


def test_exit_code_propagated_from_delegate():
    """Non-zero exit from a delegate must be returned by main()."""
    with patch("backend.cli._delegate_budget") as mock_delegate:
        mock_delegate.return_value = 1
        code = main(["budget", "check"])
    assert code == 1


# ---------------------------------------------------------------------------
# 8. Parser includes all expected subcommands
# ---------------------------------------------------------------------------


def test_parser_has_all_subcommands():
    """_build_parser() must expose all 9 subcommands."""
    p = _build_parser()
    # argparse stores subcommand choices in the subparser action
    subcommands: set[str] = set()
    for action in p._actions:
        if hasattr(action, "_name_parser_map"):
            subcommands.update(action._name_parser_map.keys())
    expected = {"status", "budget", "control", "kpi", "health", "registry", "agents", "blackboard", "serve"}
    assert expected == subcommands
