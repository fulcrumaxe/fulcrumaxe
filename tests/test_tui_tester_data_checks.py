"""
test_tui_tester_data_checks.py — unit tests for the data_checks validators added in D#855.

Each test exercises a validator against both good data (validator returns True)
and bad data (validator returns False), using a minimal fake widget tree so no
Textual runtime is needed.

Strategy: call the validator functions directly from tui_tester_kpi_registry.
The REGISTRY entries are also inspected to confirm the expected (widget_id,
validator) pairs are present for each screen.
"""

from __future__ import annotations

import pytest

from backend.tui_tester_kpi_registry import (
    REGISTRY,
    _budget_percent_in_range,
    _feed_status_parseable,
    _loop_table_duration_varies,
    _loop_table_spawned_varies,
    _no_idem_test_agent,
    _no_sentinel_minus_one,
    _stuck_count_reasonable,
    _table_rows_unique,
)


# ---------------------------------------------------------------------------
# Helper: extract validator from REGISTRY for a given (tab_id, widget_id)
# ---------------------------------------------------------------------------


def _get_validators(tab_id: str, widget_id: str):
    """Return all validators registered for (tab_id, widget_id)."""
    shape = REGISTRY.get(tab_id)
    assert shape is not None, f"tab '{tab_id}' missing from REGISTRY"
    return [v for wid, v in shape.data_checks if wid == widget_id]


# ---------------------------------------------------------------------------
# home: _budget_percent_in_range
# ---------------------------------------------------------------------------


class TestBudgetPercentInRange:
    def test_good_normal(self):
        assert _budget_percent_in_range("42.3%") is True

    def test_good_zero(self):
        assert _budget_percent_in_range("0.0%") is True

    def test_good_exactly_100(self):
        assert _budget_percent_in_range("100.0%") is True

    def test_good_upper_boundary(self):
        assert _budget_percent_in_range("200.0%") is True

    def test_bad_over_200(self):
        assert _budget_percent_in_range("201.0%") is False

    def test_bad_negative(self):
        assert _budget_percent_in_range("-1.0%") is False

    def test_bad_no_percent(self):
        assert _budget_percent_in_range("42.3") is False

    def test_bad_non_numeric(self):
        assert _budget_percent_in_range("N/A%") is False

    def test_registered_for_kpi_budget(self):
        validators = _get_validators("home", "kpi-budget")
        assert _budget_percent_in_range in validators


# ---------------------------------------------------------------------------
# home: _stuck_count_reasonable
# ---------------------------------------------------------------------------


class TestStuckCountReasonable:
    def test_good_zero(self):
        assert _stuck_count_reasonable("0") is True

    def test_good_small(self):
        assert _stuck_count_reasonable("3") is True

    def test_good_boundary_50(self):
        assert _stuck_count_reasonable("50") is True

    def test_bad_51(self):
        assert _stuck_count_reasonable("51") is False

    def test_bad_large(self):
        assert _stuck_count_reasonable("9999") is False

    def test_bad_negative(self):
        assert _stuck_count_reasonable("-1") is False

    def test_bad_non_numeric(self):
        assert _stuck_count_reasonable("N/A") is False

    def test_registered_for_kpi_stuck(self):
        validators = _get_validators("home", "kpi-stuck")
        assert _stuck_count_reasonable in validators


# ---------------------------------------------------------------------------
# stats: _no_sentinel_minus_one
# ---------------------------------------------------------------------------


class TestNoSentinelMinusOne:
    def test_good_normal_table(self):
        text = "weekly_budget_pct | 42.0 | % | 2026-05-14\nopen_prs | 5 | count | 2026-05-14"
        assert _no_sentinel_minus_one(text) is True

    def test_good_empty(self):
        assert _no_sentinel_minus_one("") is True

    def test_bad_contains_minus_one(self):
        text = "some_metric | -1.0 | % | 2026-05-14"
        assert _no_sentinel_minus_one(text) is False

    def test_bad_minus_one_in_context(self):
        # Sentinel leaked into a row
        text = "kpi_value\n-1.0\nother_value"
        assert _no_sentinel_minus_one(text) is False

    def test_registered_for_kpi_table(self):
        validators = _get_validators("stats", "kpi-table")
        assert _no_sentinel_minus_one in validators


# ---------------------------------------------------------------------------
# runs: _no_idem_test_agent
# ---------------------------------------------------------------------------


class TestNoIdemTestAgent:
    def test_good_normal_agents(self):
        text = "executor-123 | executor | 45m | 55\nreviewer-456 | code-reviewer | 12m | 60"
        assert _no_idem_test_agent(text) is True

    def test_good_empty(self):
        assert _no_idem_test_agent("") is True

    def test_bad_idem_test_prefix(self):
        text = "idem-test-42 | executor | 5m | 0"
        assert _no_idem_test_agent(text) is False

    def test_bad_idem_test_among_real(self):
        text = "executor-1 | executor | 5m | 55\nidem-test-7 | tester | 3m | 0"
        assert _no_idem_test_agent(text) is False

    def test_registered_for_stuck_table(self):
        validators = _get_validators("runs", "stuck-table")
        assert _no_idem_test_agent in validators


# ---------------------------------------------------------------------------
# loop: _loop_table_spawned_varies and _loop_table_duration_varies
# ---------------------------------------------------------------------------


class TestLoopTableSpawnedVaries:
    def test_good_mixed_values(self):
        # Table with different spawn counts — not all zero
        assert _loop_table_spawned_varies("10:00 | 5m30s | 3 | ok\n10:10 | 4m55s | 0 | ok") is True

    def test_good_empty(self):
        assert _loop_table_spawned_varies("") is True

    def test_bad_all_zero(self):
        text = "10:00 | 5m00s | 0 | ok\n10:10 | 5m00s | 0 | ok\n10:20 | 5m00s | 0 | ok"
        assert _loop_table_spawned_varies(text) is False

    def test_good_single_nonzero(self):
        assert _loop_table_spawned_varies("10:00 | 5m00s | 1 | ok") is True

    def test_registered_for_loop_table(self):
        validators = _get_validators("loop", "loop-table")
        assert _loop_table_spawned_varies in validators


class TestLoopTableDurationVaries:
    def test_good_mixed_durations(self):
        text = "10:00 | 5m30s | 3 | ok\n10:10 | 4m55s | 0 | ok"
        assert _loop_table_duration_varies(text) is True

    def test_good_empty(self):
        assert _loop_table_duration_varies("") is True

    def test_bad_all_5m00s(self):
        text = "10:00 | 5m00s | 0 | ok\n10:10 | 5m00s | 0 | ok\n10:20 | 5m00s | 0 | ok"
        assert _loop_table_duration_varies(text) is False

    def test_good_one_different(self):
        text = "10:00 | 5m00s | 0 | ok\n10:10 | 4m59s | 0 | ok"
        assert _loop_table_duration_varies(text) is True

    def test_registered_for_loop_table(self):
        validators = _get_validators("loop", "loop-table")
        assert _loop_table_duration_varies in validators


# ---------------------------------------------------------------------------
# agent_feed: _feed_status_parseable
# ---------------------------------------------------------------------------


class TestFeedStatusParseable:
    def test_good_normal_status(self):
        text = "running: 3 | stuck>15min: 0 | failed last hour: 1"
        assert _feed_status_parseable(text) is True

    def test_good_nonzero_stuck(self):
        text = "running: 2 | stuck>15min: 1 | failed last hour: 0"
        assert _feed_status_parseable(text) is True

    def test_good_empty_widget(self):
        assert _feed_status_parseable("") is True

    def test_bad_missing_stuck_field(self):
        text = "running: 3 | failed last hour: 1"
        assert _feed_status_parseable(text) is False

    def test_bad_sentinel_negative(self):
        text = "running: 3 | stuck>15min: -1 | failed last hour: 0"
        assert _feed_status_parseable(text) is False

    def test_registered_for_agent_feed_status(self):
        validators = _get_validators("agent_feed", "agent-feed-status")
        assert _feed_status_parseable in validators


# ---------------------------------------------------------------------------
# loop_controller: _table_rows_unique (lc-errors)
# ---------------------------------------------------------------------------


class TestTableRowsUniqueLoopController:
    def test_good_unique_rows(self):
        text = "2026-05-14T10:00Z | executor | spawn | discussion\n2026-05-14T10:01Z | executor | spawn | discussion2"
        assert _table_rows_unique(text) is True

    def test_good_empty(self):
        assert _table_rows_unique("") is True

    def test_bad_duplicate_rows(self):
        text = "2026-05-14T10:00Z | executor | spawn | discussion\n2026-05-14T10:00Z | executor | spawn | discussion"
        assert _table_rows_unique(text) is False

    def test_registered_for_lc_errors(self):
        validators = _get_validators("loop_controller", "lc-errors")
        assert _table_rows_unique in validators


# ---------------------------------------------------------------------------
# settings: _table_rows_unique (settings-audit)
# ---------------------------------------------------------------------------


class TestTableRowsUniqueSettings:
    def test_good_unique_audit_rows(self):
        text = "2026-05-14T10:00Z | team-lead | gate_update | main | lint_must_pass\n2026-05-14T10:01Z | team-lead | gate_update | main | auto_merge"
        assert _table_rows_unique(text) is True

    def test_bad_duplicate_audit_rows(self):
        row = "2026-05-14T10:00Z | team-lead | gate_update | main | lint_must_pass"
        text = f"{row}\n{row}"
        assert _table_rows_unique(text) is False

    def test_registered_for_settings_audit(self):
        validators = _get_validators("settings", "settings-audit")
        assert _table_rows_unique in validators


# ---------------------------------------------------------------------------
# Coverage: every screen with data_checks has at least one entry
# ---------------------------------------------------------------------------


SCREENS_REQUIRING_DATA_CHECKS = [
    "home",
    "loop",
    "runs",
    "agent_feed",
    "stats",
    "loop_controller",
    "settings",
]


@pytest.mark.parametrize("tab_id", SCREENS_REQUIRING_DATA_CHECKS)
def test_screen_has_data_checks(tab_id: str) -> None:
    """Every screen listed in the spec must have at least one data_check entry."""
    shape = REGISTRY.get(tab_id)
    assert shape is not None, f"tab '{tab_id}' missing from REGISTRY"
    assert len(shape.data_checks) > 0, (
        f"tab '{tab_id}' has no data_checks — at least one validator required per D#855"
    )


# ---------------------------------------------------------------------------
# Coverage: screens NOT in the required list may still have data_checks, but
# prs/discussions/pr_detail/ideas are out of scope for D#855
# ---------------------------------------------------------------------------


OUT_OF_SCOPE_SCREENS = ["prs", "discussions", "pr_detail", "ideas"]


@pytest.mark.parametrize("tab_id", OUT_OF_SCOPE_SCREENS)
def test_out_of_scope_screens_present_in_registry(tab_id: str) -> None:
    """Out-of-scope screens must still have registry entries (no regressions)."""
    shape = REGISTRY.get(tab_id)
    assert shape is not None, f"tab '{tab_id}' missing from REGISTRY"
