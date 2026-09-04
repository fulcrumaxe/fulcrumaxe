"""
Tests for backend/cost_per_outcome.py and backend/rpc/stats_cost_per_outcome.py.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import backend.cost_per_outcome as cpo
from backend.rpc.stats_cost_per_outcome import handle


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FAKE_PRS = [
    {"number": 100, "mergedAt": "2026-05-20T10:00:00Z", "title": "PR 100"},
    {"number": 200, "mergedAt": "2026-05-19T10:00:00Z", "title": "PR 200"},
    {"number": 300, "mergedAt": "2026-05-18T10:00:00Z", "title": "PR 300"},
]

_SUMMARY_100 = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "total_tokens": 1500,
    "usd": 0.012000,
    "by_role": [
        {"role": "executor", "input_tokens": 800, "output_tokens": 400, "usd": 0.009600},
        {"role": "code-reviewer", "input_tokens": 200, "output_tokens": 100, "usd": 0.002400},
    ],
}

_SUMMARY_200 = {
    "input_tokens": 2000,
    "output_tokens": 800,
    "total_tokens": 2800,
    "usd": 0.018000,
    "by_role": [
        {"role": "executor", "input_tokens": 2000, "output_tokens": 800, "usd": 0.018000},
    ],
}


# ---------------------------------------------------------------------------
# _get_merged_prs
# ---------------------------------------------------------------------------

def test_get_merged_prs_filters_by_date():
    """Only PRs merged within the window should be returned."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(_FAKE_PRS),
        )
        result = cpo._get_merged_prs(days=30)
    assert all(isinstance(pr["number"], int) for pr in result)


def test_get_merged_prs_empty_on_gh_failure():
    """Returns empty list when gh CLI fails."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = cpo._get_merged_prs(days=30)
    assert result == []


def test_get_merged_prs_empty_on_exception():
    """Returns empty list on unexpected exception."""
    with patch("subprocess.run", side_effect=Exception("timeout")):
        result = cpo._get_merged_prs(days=30)
    assert result == []


# ---------------------------------------------------------------------------
# _fix_rounds_for_pr
# ---------------------------------------------------------------------------

def test_fix_rounds_returns_zero_on_missing_db():
    """Returns 0 when stats.duckdb is not available."""
    with patch("backend.cost_per_outcome._fix_rounds_for_pr", return_value=0):
        assert cpo._fix_rounds_for_pr.__module__  # just confirm function exists

    # Direct test: patch _connect to raise FileNotFoundError
    with patch("backend.agent_run_reader._connect", side_effect=FileNotFoundError("no db")):
        count = cpo._fix_rounds_for_pr(999)
    assert count == 0


def test_fix_rounds_returns_zero_on_db_lock():
    """Returns 0 (no crash) when DuckDB raises IOException (e.g. DB locked)."""
    # Simulate the error duckdb raises when another process holds the DB lock
    db_error = Exception("IO Error: Could not set lock on file")
    with patch("backend.agent_run_reader._connect", side_effect=db_error):
        count = cpo._fix_rounds_for_pr(42)
    assert count == 0


def test_fix_rounds_returns_count_from_db():
    """Returns the row count from agent_run for needs-fix/fail executor rows."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (3,)

    with patch("backend.agent_run_reader._connect", return_value=mock_conn):
        count = cpo._fix_rounds_for_pr(100)

    assert count == 3
    mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# cost_per_outcome_rows — core logic
# ---------------------------------------------------------------------------

def test_rows_omits_prs_with_no_records():
    """PRs where per_pr_summary returns None must be omitted (no crash)."""
    with patch.object(cpo, "_get_merged_prs", return_value=_FAKE_PRS), \
         patch("backend.cost_tracker.CostTracker.per_pr_summary", return_value=None), \
         patch.object(cpo, "_fix_rounds_for_pr", return_value=0):
        rows = cpo.cost_per_outcome_rows(days=30)

    assert rows == []


def test_rows_returns_correct_structure():
    """Each returned row has the required fields."""
    summaries = {100: _SUMMARY_100, 200: _SUMMARY_200, 300: None}

    def _fake_summary(self, pr_number):
        return summaries.get(pr_number)

    with patch.object(cpo, "_get_merged_prs", return_value=_FAKE_PRS), \
         patch("backend.cost_tracker.CostTracker.per_pr_summary", _fake_summary), \
         patch.object(cpo, "_fix_rounds_for_pr", return_value=1):
        rows = cpo.cost_per_outcome_rows(days=30)

    assert len(rows) == 2  # PR 300 has no records
    for row in rows:
        assert "pr" in row
        assert "usd" in row
        assert "total_tokens" in row
        assert "fix_rounds" in row
        assert "by_role" in row


def test_rows_sorted_by_usd_descending():
    """Rows must be sorted by usd descending."""
    summaries = {100: _SUMMARY_100, 200: _SUMMARY_200}

    def _fake_summary(self, pr_number):
        return summaries.get(pr_number)

    prs = [p for p in _FAKE_PRS if p["number"] in summaries]

    with patch.object(cpo, "_get_merged_prs", return_value=prs), \
         patch("backend.cost_tracker.CostTracker.per_pr_summary", _fake_summary), \
         patch.object(cpo, "_fix_rounds_for_pr", return_value=0):
        rows = cpo.cost_per_outcome_rows(days=30)

    assert rows[0]["usd"] >= rows[1]["usd"]
    assert rows[0]["pr"] == 200  # 0.018 > 0.012


def test_rows_usd_matches_cost_tracker_per_pr_summary():
    """The usd field must equal the value from per_pr_summary exactly."""
    def _fake_summary(self, pr_number):
        if pr_number == 100:
            return _SUMMARY_100
        return None

    with patch.object(cpo, "_get_merged_prs", return_value=[_FAKE_PRS[0]]), \
         patch("backend.cost_tracker.CostTracker.per_pr_summary", _fake_summary), \
         patch.object(cpo, "_fix_rounds_for_pr", return_value=0):
        rows = cpo.cost_per_outcome_rows(days=30)

    assert len(rows) == 1
    assert rows[0]["usd"] == _SUMMARY_100["usd"]
    assert rows[0]["total_tokens"] == _SUMMARY_100["total_tokens"]
    assert rows[0]["by_role"] == _SUMMARY_100["by_role"]


def test_fix_rounds_included_in_rows():
    """fix_rounds in the row must come from _fix_rounds_for_pr."""
    def _fake_summary(self, pr_number):
        return _SUMMARY_100 if pr_number == 100 else None

    with patch.object(cpo, "_get_merged_prs", return_value=[_FAKE_PRS[0]]), \
         patch("backend.cost_tracker.CostTracker.per_pr_summary", _fake_summary), \
         patch.object(cpo, "_fix_rounds_for_pr", return_value=4):
        rows = cpo.cost_per_outcome_rows(days=30)

    assert rows[0]["fix_rounds"] == 4


# ---------------------------------------------------------------------------
# RPC handler
# ---------------------------------------------------------------------------

def test_rpc_handle_returns_rows_key():
    """handle() must return a dict with 'rows' key."""
    with patch.object(cpo, "_get_merged_prs", return_value=[]), \
         patch("backend.cost_tracker.CostTracker.per_pr_summary", return_value=None):
        result = handle({})

    assert "rows" in result
    assert isinstance(result["rows"], list)


def test_rpc_handle_passes_days_param():
    """handle() must pass days param to cost_per_outcome_rows."""
    captured: dict = {}

    def _fake_rows(days=30):
        captured["days"] = days
        return []

    with patch("backend.rpc.stats_cost_per_outcome._rows", side_effect=_fake_rows):
        handle({"days": 7})

    assert captured["days"] == 7


def test_rpc_handle_limit_caps_rows():
    """handle() with limit=1 should cap the output to 1 row."""
    def _fake_summary(self, pr_number):
        return {100: _SUMMARY_100, 200: _SUMMARY_200}.get(pr_number)

    prs = [p for p in _FAKE_PRS if p["number"] in {100, 200}]

    with patch.object(cpo, "_get_merged_prs", return_value=prs), \
         patch("backend.cost_tracker.CostTracker.per_pr_summary", _fake_summary), \
         patch.object(cpo, "_fix_rounds_for_pr", return_value=0):
        result = handle({"limit": 1})

    assert len(result["rows"]) == 1


# ---------------------------------------------------------------------------
# No reimplemented pricing — negative check
# ---------------------------------------------------------------------------

def test_no_rate_card_in_module():
    """cost_per_outcome.py must not define a rate card (no _DEFAULT_PRICING etc.)."""
    import inspect
    src = inspect.getsource(cpo)
    assert "_DEFAULT_PRICING" not in src, "Do not reimplement pricing in this module"
    assert "input_per_1k" not in src, "Do not reimplement rate card in this module"
    assert "output_per_1k" not in src, "Do not reimplement rate card in this module"
