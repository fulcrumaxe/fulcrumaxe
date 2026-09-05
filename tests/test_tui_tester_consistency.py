"""
test_tui_tester_consistency.py — unit tests for tui_tester_consistency module.

Each of the five invariants is tested with:
  (a) agreeing screen texts → check_all returns no violation for that invariant
  (b) disagreeing screen texts → check_all returns a violation for that invariant

One end-to-end test verifies that a mismatched pair produces a violation that
appears in check_all's output with the expected structure.

No Textual runtime or live app is needed — all tests use plain text strings.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from backend.tui_tester_consistency import (
    INVARIANTS,
    Invariant,
    Violation,
    _budget_roughly_agrees,
    _counts_agree,
    _read_agent_feed_stuck_count,
    _read_home_budget_percent,
    _read_home_last_run_ts,
    _read_home_open_pr_count,
    _read_loop_controller_budget,
    _read_loop_controller_stale,
    _read_loop_health_last_ts,
    _read_loop_health_status,
    _read_prs_all_count,
    _read_runs_stuck_count,
    _staleness_agrees,
    _timestamps_agree,
    check_all,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _violations_for(invariant_name: str, screens: dict) -> list[dict]:
    """Run check_all and return only violations for the named invariant."""
    return [v for v in check_all(screens) if v["widget_id"] == invariant_name]


# ---------------------------------------------------------------------------
# Invariant 1: loop_staleness_agreement
# ---------------------------------------------------------------------------


class TestLoopStalenessAgreement:
    LOOP_OK = "Status: ok\n10:00 | 3m30s | 5 | ok"
    LOOP_STALE = "Status: stale\n[stale] last run 45m ago"
    LC_OK = "Loop status: running\nBudget: $10 / $100"
    LC_STALE = "Loop status: [stale] — no run in 45 min"

    def test_both_ok_no_violation(self):
        violations = _violations_for(
            "loop_staleness_agreement",
            {"loop": self.LOOP_OK, "loop_controller": self.LC_OK},
        )
        assert violations == []

    def test_both_stale_no_violation(self):
        violations = _violations_for(
            "loop_staleness_agreement",
            {"loop": self.LOOP_STALE, "loop_controller": self.LC_STALE},
        )
        assert violations == []

    def test_loop_stale_lc_ok_violation(self):
        violations = _violations_for(
            "loop_staleness_agreement",
            {"loop": self.LOOP_STALE, "loop_controller": self.LC_OK},
        )
        assert len(violations) == 1
        assert violations[0]["status"] == "fail"
        assert "loop_staleness_agreement" in violations[0]["detail"]

    def test_loop_ok_lc_stale_violation(self):
        violations = _violations_for(
            "loop_staleness_agreement",
            {"loop": self.LOOP_OK, "loop_controller": self.LC_STALE},
        )
        assert len(violations) == 1

    def test_missing_loop_screen_no_violation(self):
        """Missing screen text → cannot assert → skip."""
        violations = _violations_for(
            "loop_staleness_agreement",
            {"loop_controller": self.LC_STALE},
        )
        assert violations == []

    def test_missing_lc_screen_no_violation(self):
        violations = _violations_for(
            "loop_staleness_agreement",
            {"loop": self.LOOP_STALE},
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Invariant 2: stuck_count_agreement
# ---------------------------------------------------------------------------


class TestStuckCountAgreement:
    FEED_0 = "running: 3 | stuck>15min: 0 | failed last hour: 1"
    FEED_2 = "running: 2 | stuck>15min: 2 | failed last hour: 0"
    RUNS_0 = "Stuck Runs | 0\nTotal Runs | 12"
    RUNS_2 = "Stuck Runs | 2\nTotal Runs | 12"

    def test_both_zero_no_violation(self):
        violations = _violations_for(
            "stuck_count_agreement", {"agent_feed": self.FEED_0, "runs": self.RUNS_0}
        )
        assert violations == []

    def test_both_two_no_violation(self):
        violations = _violations_for(
            "stuck_count_agreement", {"agent_feed": self.FEED_2, "runs": self.RUNS_2}
        )
        assert violations == []

    def test_feed_2_runs_0_violation(self):
        violations = _violations_for(
            "stuck_count_agreement", {"agent_feed": self.FEED_2, "runs": self.RUNS_0}
        )
        assert len(violations) == 1
        assert "stuck_count_agreement" in violations[0]["detail"]

    def test_feed_0_runs_2_violation(self):
        violations = _violations_for(
            "stuck_count_agreement", {"agent_feed": self.FEED_0, "runs": self.RUNS_2}
        )
        assert len(violations) == 1

    def test_missing_feed_no_violation(self):
        violations = _violations_for(
            "stuck_count_agreement", {"runs": self.RUNS_2}
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Invariant 3: budget_agreement
# ---------------------------------------------------------------------------


class TestBudgetAgreement:
    HOME_42 = "Weekly budget: 42%\nOpen PRs: 5"
    HOME_80 = "Weekly budget: 80%\nOpen PRs: 5"
    LC_42 = "Budget: $42 / $100\nLoop status: ok"
    LC_85 = "Budget: $85 / $100\nLoop status: ok"

    def test_both_42_no_violation(self):
        violations = _violations_for(
            "budget_agreement", {"home": self.HOME_42, "loop_controller": self.LC_42}
        )
        assert violations == []

    def test_within_5pct_no_violation(self):
        # 42% vs 45% — within ±5
        lc_45 = "Budget: $45 / $100\nLoop status: ok"
        violations = _violations_for(
            "budget_agreement", {"home": self.HOME_42, "loop_controller": lc_45}
        )
        assert violations == []

    def test_beyond_5pct_violation(self):
        # 42% vs 85% — disagreement
        violations = _violations_for(
            "budget_agreement", {"home": self.HOME_42, "loop_controller": self.LC_85}
        )
        assert len(violations) == 1
        assert "budget_agreement" in violations[0]["detail"]

    def test_80_vs_85_no_violation(self):
        # 80% vs 85% — within ±5
        violations = _violations_for(
            "budget_agreement", {"home": self.HOME_80, "loop_controller": self.LC_85}
        )
        assert violations == []

    def test_missing_lc_no_violation(self):
        violations = _violations_for(
            "budget_agreement", {"home": self.HOME_42}
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Invariant 4: open_pr_count_agreement
# ---------------------------------------------------------------------------


class TestOpenPrCountAgreement:
    HOME_5 = "Open PRs: 5\nWeekly budget: 42%"
    HOME_3 = "Open PRs: 3\nWeekly budget: 42%"
    PRS_5 = "5 all | 3 open | 2 merged"
    PRS_7 = "7 all | 5 open | 2 merged"

    def test_both_5_no_violation(self):
        violations = _violations_for(
            "open_pr_count_agreement", {"home": self.HOME_5, "prs": self.PRS_5}
        )
        assert violations == []

    def test_home_5_prs_7_violation(self):
        violations = _violations_for(
            "open_pr_count_agreement", {"home": self.HOME_5, "prs": self.PRS_7}
        )
        assert len(violations) == 1
        assert "open_pr_count_agreement" in violations[0]["detail"]

    def test_home_3_prs_5_violation(self):
        violations = _violations_for(
            "open_pr_count_agreement", {"home": self.HOME_3, "prs": self.PRS_5}
        )
        assert len(violations) == 1

    def test_missing_prs_no_violation(self):
        violations = _violations_for(
            "open_pr_count_agreement", {"home": self.HOME_5}
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Invariant 5: last_run_agreement
# ---------------------------------------------------------------------------


class TestLastRunAgreement:
    HOME_1042 = "Last loop run: 10:42\nOpen PRs: 5"
    HOME_1100 = "Last loop run: 11:00\nOpen PRs: 5"
    LOOP_1042 = "10:42 | 3m30s | 5 | ok\n10:32 | 4m00s | 3 | ok"
    LOOP_1100 = "11:00 | 2m15s | 2 | ok\n10:50 | 3m00s | 4 | ok"

    def test_both_1042_no_violation(self):
        violations = _violations_for(
            "last_run_agreement", {"home": self.HOME_1042, "loop": self.LOOP_1042}
        )
        assert violations == []

    def test_home_1042_loop_1100_violation(self):
        violations = _violations_for(
            "last_run_agreement", {"home": self.HOME_1042, "loop": self.LOOP_1100}
        )
        assert len(violations) == 1
        assert "last_run_agreement" in violations[0]["detail"]

    def test_home_1100_loop_1042_violation(self):
        violations = _violations_for(
            "last_run_agreement", {"home": self.HOME_1100, "loop": self.LOOP_1042}
        )
        assert len(violations) == 1

    def test_missing_loop_no_violation(self):
        violations = _violations_for(
            "last_run_agreement", {"home": self.HOME_1042}
        )
        assert violations == []


# ---------------------------------------------------------------------------
# End-to-end: one mismatched pair → violation in check_all output
# ---------------------------------------------------------------------------


class TestCheckAllEndToEnd:
    def test_no_violations_when_all_agree(self):
        screens = {
            "loop": "Status: ok\n10:00 | 3m30s | 5 | ok",
            "loop_controller": "Budget: $42 / $100\nLoop status: ok",
            "agent_feed": "running: 3 | stuck>15min: 0 | failed last hour: 1",
            "runs": "Stuck Runs | 0\nTotal Runs | 12",
            "home": "Weekly budget: 42%\nOpen PRs: 5\nLast loop run: 10:00",
            "prs": "5 all | 3 open | 2 merged",
        }
        violations = check_all(screens)
        assert violations == []

    def test_one_mismatch_produces_one_violation(self):
        """Stick count: feed says 2, runs says 0 — one violation in output."""
        screens = {
            "loop": "Status: ok\n10:00 | 3m30s | 5 | ok",
            "loop_controller": "Budget: $42 / $100\nLoop status: ok",
            "agent_feed": "running: 3 | stuck>15min: 2 | failed last hour: 1",
            "runs": "Stuck Runs | 0\nTotal Runs | 12",
            "home": "Weekly budget: 42%\nOpen PRs: 5\nLast loop run: 10:00",
            "prs": "5 all | 3 open | 2 merged",
        }
        violations = check_all(screens)
        names = [v["widget_id"] for v in violations]
        assert "stuck_count_agreement" in names
        # Only stuck_count should fire
        assert len(violations) == 1

    def test_violation_has_expected_finding_shape(self):
        """Violation dict must match the findings.json schema shape."""
        screens = {
            "agent_feed": "running: 3 | stuck>15min: 2 | failed last hour: 0",
            "runs": "Stuck Runs | 0\nTotal Runs | 10",
        }
        violations = check_all(screens)
        assert len(violations) >= 1
        v = violations[0]
        # Required fields in findings.json
        assert "tab" in v
        assert "widget_id" in v
        assert "check_name" in v
        assert v["check_name"] == "cross_screen_consistency"
        assert v["status"] == "fail"
        assert "detail" in v
        assert "cross_screen_disagreement" in v["detail"]

    def test_violation_detail_includes_both_values(self):
        """Detail string must include both screen values for debuggability."""
        screens = {
            "agent_feed": "running: 3 | stuck>15min: 2 | failed last hour: 0",
            "runs": "Stuck Runs | 0\nTotal Runs | 10",
        }
        violations = check_all(screens)
        assert len(violations) == 1
        detail = violations[0]["detail"]
        assert "2" in detail   # agent_feed value
        assert "0" in detail   # runs value

    def test_all_screens_missing_no_violations(self):
        """When no screen data is present, no assertion can be made."""
        violations = check_all({})
        assert violations == []

    def test_findings_json_writable(self):
        """findings.json format round-trips cleanly with violation dicts."""
        screens = {
            "agent_feed": "running: 3 | stuck>15min: 3 | failed last hour: 0",
            "runs": "Stuck Runs | 0\nTotal Runs | 10",
        }
        violations = check_all(screens)
        with tempfile.TemporaryDirectory() as tmpdir:
            findings_path = Path(tmpdir) / "findings.json"
            payload = {
                "verdict": "needs-fix",
                "findings": violations,
                "artifact_dir": tmpdir,
                "elapsed_s": 0.1,
            }
            findings_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            loaded = json.loads(findings_path.read_text(encoding="utf-8"))
        assert loaded["verdict"] == "needs-fix"
        assert len(loaded["findings"]) == 1
        assert loaded["findings"][0]["status"] == "fail"


# ---------------------------------------------------------------------------
# Reader unit tests (direct, not through check_all)
# ---------------------------------------------------------------------------


class TestReaders:
    def test_loop_health_ok(self):
        assert _read_loop_health_status("Status: ok\n10:00 | run") == "ok"

    def test_loop_health_stale(self):
        assert _read_loop_health_status("[stale] loop 45m ago") == "stale"

    def test_loop_health_empty(self):
        assert _read_loop_health_status("") is None

    def test_lc_stale_marker(self):
        assert _read_loop_controller_stale("Loop [stale] — 45min") == "stale"

    def test_lc_no_stale_marker(self):
        assert _read_loop_controller_stale("Budget: $10 / $100") == "ok"

    def test_agent_feed_stuck(self):
        assert _read_agent_feed_stuck_count("running: 2 | stuck>15min: 3 | failed: 0") == "3"

    def test_runs_stuck(self):
        assert _read_runs_stuck_count("Stuck Runs | 7") == "7"

    def test_home_budget(self):
        assert _read_home_budget_percent("Weekly budget: 55.5%") == "55.5"

    def test_lc_budget_dollar(self):
        assert _read_loop_controller_budget("Budget: $50 / $100") == "50"

    def test_lc_budget_percent(self):
        assert _read_loop_controller_budget("Budget: 42%") == "42"

    def test_home_pr_count(self):
        assert _read_home_open_pr_count("Open PRs: 8") == "8"

    def test_prs_all_count(self):
        assert _read_prs_all_count("8 all | 5 open") == "8"

    def test_home_last_run(self):
        assert _read_home_last_run_ts("Last loop run: 09:15") == "09:15"

    def test_loop_health_last_ts(self):
        assert _read_loop_health_last_ts("09:15 | 3m | 4 | ok") == "09:15"
