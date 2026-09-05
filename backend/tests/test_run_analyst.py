"""Tests for backend/run_analyst.py -- uses fixture data only.

HARD RULE: These tests MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop. All subprocess calls to gh and audit_trail.py are mocked.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from testsupport.fixture_paths import FIXTURE_HOME, FIXTURE_MAIN_REPO  # noqa: E402

from run_analyst import (
    CHUNK_SIZE,
    classify_cost_outliers,
    classify_failure_clusters,
    classify_fix_cycle_loops,
    classify_spec_quality_flags,
    classify_stalled_patterns,
    classify_time_anomalies,
    classify_tool_use_anomalies,
    # Phase A new classifiers (Discussion #478)
    classify_worktree_contamination,
    classify_hard_rule_violations,
    classify_agent_output_missing,
    classify_test_coverage_gap,
    classify_missing_post_agent_hook,
    classify_token_burn_no_output,
    classify_discussion_respun_n_times,
    classify_hook_event_spam,
    classify_transcript_repetition,
    classify_spec_impl_semantic_gap,
    classify_branch_drift,
    classify_stale_snapshot_consumption,
    classify_budget_cap_proximity,
    classify_pre_spawn_check_missing,
    build_report,
    parse_since,
    load_loop_metrics,
    _median,
    _parse_ts,
    _entry_text,
)

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=7)

# NOW is safe for every fixture that is only ever compared against another
# NOW-derived value, or carried through a classifier that never reads a clock.
# It is NOT safe where the code under test ages the fixture against a live
# datetime.now(): there the interval actually under test is the offset written
# here PLUS however long the suite took to get from import to that assertion.
#
# classify_stale_snapshot_consumption is exactly that case — it compares
# against MAX_AGE = 600s — so its inputs are pinned to a literal instant and
# the clock it reads is frozen to the same instant. Before that, the "fresh"
# case (NOW - 100s) aged past the threshold once ~500s of suite time elapsed
# and failed with nothing wrong in the arithmetic it exists to check (D#2403).
SNAPSHOT_CLOCK = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@contextmanager
def frozen_clock(instant: datetime):
    """Pin the ``datetime.now()`` that ``run_analyst`` reads to ``instant``.

    Freezes the COMPARISON clock, not the input timestamp, so the interval
    under test is exact rather than "the offset, plus suite duration".

    Subclassing ``datetime`` rather than handing over a MagicMock keeps every
    other behaviour the classifier depends on — arithmetic, tz handling,
    ``strftime`` — served by the real implementation; only the reading of the
    clock is pinned. ``_parse_ts`` is imported into ``run_analyst`` from
    ``backend.loop_metrics_ts``, so patching this name leaves parsing alone.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    with patch("run_analyst.datetime", _Frozen):
        yield


def make_event(message: str, agent: str = "executor", ts_offset_hours: int = 0) -> dict:
    ts = (NOW - timedelta(hours=ts_offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"message": message, "agent": agent, "timestamp": ts}


FIXTURE_EVENTS = [
    make_event("executor run discussion:#438 -- needs-fix round 1", ts_offset_hours=2),
    make_event("executor run discussion:#438 -- needs-fix round 2", ts_offset_hours=1),
    make_event("executor run discussion:#438 -- needs-fix round 3"),
    make_event("preflight failed -- merge conflict detected", agent="executor", ts_offset_hours=5),
    make_event("preflight failed -- merge conflict detected", agent="executor", ts_offset_hours=4),
    make_event("preflight failed -- merge conflict detected", agent="executor", ts_offset_hours=3),
    make_event("STATUS:IMPLEMENTING started for discussion:#450", ts_offset_hours=30),
    make_event("scope creep noted in PR:#421 review", agent="code-reviewer"),
    make_event("claude -p invoked from Bash script", agent="executor"),
]

FIXTURE_ROLE_EFFICIENCY = {
    "roles": {
        "executor": {"avg_tokens_per_pass": 10000, "avg_duration_seconds": 300},
        "code-reviewer": {"avg_tokens_per_pass": 50000, "avg_duration_seconds": 400},
        "project-manager": {"avg_tokens_per_pass": 8000, "avg_duration_seconds": 600},
    }
}

FIXTURE_NEEDS_FIX_PRS = [
    {
        "number": 421,
        "title": "feat: some feature",
        "url": "https://github.com/...",
        "labels": [
            {"name": "code-review-needs-fix"},
            {"name": "code-review-needs-fix"},
        ],
        "createdAt": (NOW - timedelta(hours=48)).isoformat(),
    }
]


class TestParseSince(unittest.TestCase):
    def test_days(self):
        before = datetime.now(timezone.utc)
        result = parse_since("7d")
        expected = before - timedelta(days=7)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=5)

    def test_hours(self):
        before = datetime.now(timezone.utc)
        result = parse_since("24h")
        expected = before - timedelta(hours=24)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=5)

    def test_minutes(self):
        before = datetime.now(timezone.utc)
        result = parse_since("30m")
        expected = before - timedelta(minutes=30)
        self.assertAlmostEqual(result.timestamp(), expected.timestamp(), delta=5)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_since("invalid")


class TestMedian(unittest.TestCase):
    def test_odd(self):
        self.assertEqual(_median([1, 3, 5]), 3)

    def test_even(self):
        self.assertEqual(_median([1, 2, 3, 4]), 2.5)

    def test_empty(self):
        self.assertEqual(_median([]), 0.0)

    def test_single(self):
        self.assertEqual(_median([42]), 42)


class TestEntryText(unittest.TestCase):
    def test_message_field(self):
        self.assertIn("hello", _entry_text({"message": "hello"}))

    def test_body_field(self):
        self.assertIn("world", _entry_text({"body": "world"}))

    def test_empty(self):
        self.assertEqual(_entry_text({}), "")


class TestParseTs(unittest.TestCase):
    def test_z_format(self):
        ts = _parse_ts("2026-05-10T12:00:00Z")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)

    def test_invalid(self):
        self.assertIsNone(_parse_ts("not-a-date"))

    def test_empty(self):
        self.assertIsNone(_parse_ts(""))

    # -- D#1753 Part 1: _parse_ts must not raise on non-str input --

    def test_int_epoch_does_not_raise(self):
        # This is the exact value from the offending loop-metrics.jsonl row.
        result = _parse_ts(1784925063)
        self.assertTrue(result is None or isinstance(result, datetime))

    def test_non_str_types_do_not_raise(self):
        for bad in (None, 12.5, [], {}, b"2026-07-24T00:00:00Z", True):
            with self.subTest(bad=bad):
                try:
                    result = _parse_ts(bad)
                except Exception as exc:  # noqa: BLE001 - this IS the assertion
                    self.fail(f"_parse_ts({bad!r}) raised {type(exc).__name__}: {exc}")
                self.assertTrue(result is None or isinstance(result, datetime))

    def test_happy_path_unchanged(self):
        ts = _parse_ts("2026-07-24T09:57:28Z")
        self.assertIsNotNone(ts)
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(
            ts,
            datetime(2026, 7, 24, 9, 57, 28, tzinfo=timezone.utc),
        )


class TestLoadLoopMetrics(unittest.TestCase):
    """D#1753 Part 2: load_loop_metrics fails soft per row."""

    def _write_rows(self, tmpdir: str, rows: list[str]) -> Path:
        path = Path(tmpdir) / "loop-metrics.jsonl"
        path.write_text("\n".join(rows) + "\n")
        return path

    def test_bad_int_ts_row_is_skipped_not_fatal(self):
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
        good_row = json.dumps({"timestamp": "2026-07-24T09:57:28Z", "event_count": 1})
        rows = [good_row] * 9  # lines 1-9
        rows.append(
            json.dumps({"ts": 1784925063, "iso": "2026-07-24T20:31:00Z", "event_count": 3})
        )  # line 10, the offending row
        rows += [good_row] * 7  # lines 11-17 -- 16 good rows total, 1 bad

        with tempfile.TemporaryDirectory() as tmpdir:
            metrics_path = self._write_rows(tmpdir, rows)
            with patch("run_analyst.LOOP_METRICS", metrics_path):
                buf = io.StringIO()
                with redirect_stderr(buf):
                    result = load_loop_metrics(since)

        self.assertEqual(len(result), 16)

    def test_guard_catches_decode_type_and_value_errors(self):
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
        good_row = json.dumps({"timestamp": "2026-07-24T09:57:28Z", "event_count": 1})
        rows = [
            good_row,
            "{not json",  # raises json.JSONDecodeError
            good_row,
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            metrics_path = self._write_rows(tmpdir, rows)
            with patch("run_analyst.LOOP_METRICS", metrics_path):
                # Force _parse_ts to simulate TypeError/ValueError escapes on
                # specific calls, proving the widened except in
                # load_loop_metrics survives them without aborting the run.
                real_parse_ts = _parse_ts
                calls = {"n": 0}

                def flaky_parse_ts(ts_str):
                    calls["n"] += 1
                    if calls["n"] == 2:
                        raise TypeError("simulated")
                    if calls["n"] == 3:
                        raise ValueError("simulated")
                    return real_parse_ts(ts_str)

                buf = io.StringIO()
                with patch("run_analyst._parse_ts", side_effect=flaky_parse_ts):
                    with redirect_stderr(buf):
                        result = load_loop_metrics(since)

        # good_row (call 1) parses fine; the TypeError/ValueError rows are
        # skipped; nothing escapes load_loop_metrics.
        self.assertEqual(len(result), 1)

    def test_skip_reports_file_and_line_number_to_stderr(self):
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
        good_row = json.dumps({"timestamp": "2026-07-24T09:57:28Z", "event_count": 1})
        rows = [good_row] * 9
        rows.append(
            json.dumps({"ts": 1784925063, "iso": "2026-07-24T20:31:00Z", "event_count": 3})
        )  # line 10

        with tempfile.TemporaryDirectory() as tmpdir:
            metrics_path = self._write_rows(tmpdir, rows)
            with patch("run_analyst.LOOP_METRICS", metrics_path):
                buf = io.StringIO()
                with redirect_stderr(buf):
                    load_loop_metrics(since)

        self.assertIn("loop-metrics.jsonl:10", buf.getvalue())


class TestFailureClusters(unittest.TestCase):
    def test_detects_merge_conflict(self):
        events = [
            make_event("preflight failed -- merge conflict"),
            make_event("merge conflict in push"),
            make_event("another merge conflict error"),
        ]
        findings = classify_failure_clusters(events, [], [])
        categories = [f["category"] for f in findings]
        self.assertIn("failure_cluster", categories)

    def test_no_cluster_below_threshold(self):
        events = [
            make_event("merge conflict"),
            make_event("merge conflict"),
        ]
        findings = classify_failure_clusters(events, [], [])
        self.assertEqual(findings, [])

    def test_severity_high_at_5(self):
        events = [make_event("rate limit hit") for _ in range(5)]
        findings = classify_failure_clusters(events, [], [])
        high = [f for f in findings if f["severity"] == "high"]
        self.assertTrue(len(high) > 0)


class TestCostOutliers(unittest.TestCase):
    def test_detects_outlier(self):
        # median([4000, 5000, 5500]) = 5000; expensive-role=80000 >> 2*5000=10000
        efficiency = {
            "roles": {
                "executor": {"avg_tokens_per_pass": 4000},
                "code-reviewer": {"avg_tokens_per_pass": 5000},
                "project-manager": {"avg_tokens_per_pass": 5500},
                "expensive-role": {"avg_tokens_per_pass": 80000},
            }
        }
        findings = classify_cost_outliers(efficiency, {})
        self.assertTrue(any(f["category"] == "cost_outlier" for f in findings))
        titles = [f["title"] for f in findings]
        self.assertTrue(any("expensive-role" in t for t in titles))

    def test_no_outlier_below_2x(self):
        efficiency = {
            "roles": {
                "a": {"avg_tokens_per_pass": 5000},
                "b": {"avg_tokens_per_pass": 8000},
            }
        }
        findings = classify_cost_outliers(efficiency, {})
        self.assertEqual(findings, [])

    def test_empty_efficiency(self):
        findings = classify_cost_outliers({}, {})
        self.assertEqual(findings, [])


class TestFixCycleLoops(unittest.TestCase):
    def test_detects_3_rounds(self):
        findings = classify_fix_cycle_loops(FIXTURE_EVENTS, [], [])
        cats = [f["category"] for f in findings]
        self.assertIn("fix_cycle_loop", cats)

    def test_detects_pr_with_2_needs_fix_labels(self):
        findings = classify_fix_cycle_loops([], [], FIXTURE_NEEDS_FIX_PRS)
        self.assertTrue(any(f["category"] == "fix_cycle_loop" for f in findings))

    def test_no_loop_below_threshold(self):
        events = [
            make_event("discussion:#999 needs-fix round 1"),
            make_event("discussion:#999 needs-fix round 2"),
        ]
        findings = classify_fix_cycle_loops(events, [], [])
        self.assertEqual(findings, [])


class TestStalledPatterns(unittest.TestCase):
    def test_detects_stalled_without_pr(self):
        findings = classify_stalled_patterns(FIXTURE_EVENTS, SINCE)
        self.assertTrue(any(f["category"] == "stalled_pattern" for f in findings))

    def test_no_stall_if_pr_created(self):
        events = [
            make_event("STATUS:IMPLEMENTING started for discussion:#999", ts_offset_hours=30),
            make_event("PR #88 created for Discussion #999"),
        ]
        findings = classify_stalled_patterns(events, SINCE)
        stalled = [f for f in findings if "999" in str(f.get("evidence", []))]
        self.assertEqual(stalled, [])

    def test_no_stall_if_recent(self):
        events = [
            make_event("STATUS:IMPLEMENTING started for discussion:#777", ts_offset_hours=1),
        ]
        findings = classify_stalled_patterns(events, SINCE)
        stalled = [f for f in findings if "777" in str(f.get("evidence", []))]
        self.assertEqual(stalled, [])


class TestSpecQualityFlags(unittest.TestCase):
    def test_detects_scope_creep(self):
        findings = classify_spec_quality_flags(FIXTURE_EVENTS, [])
        self.assertTrue(any(f["category"] == "spec_quality_flag" for f in findings))

    def test_no_false_positive(self):
        events = [make_event("normal code review comment")]
        findings = classify_spec_quality_flags(events, [])
        self.assertEqual(findings, [])


class TestToolUseAnomalies(unittest.TestCase):
    def test_detects_claude_p(self):
        findings = classify_tool_use_anomalies(FIXTURE_EVENTS, [], [])
        self.assertTrue(any(f["category"] == "tool_use_anomaly" for f in findings))

    def test_detects_start_loop_run(self):
        events = [make_event("subprocess: _start_loop_run triggered")]
        findings = classify_tool_use_anomalies(events, [], [])
        self.assertTrue(any(f["category"] == "tool_use_anomaly" for f in findings))

    def test_no_false_positive(self):
        events = [make_event("normal executor run completed")]
        findings = classify_tool_use_anomalies(events, [], [])
        self.assertEqual(findings, [])


class TestTimeAnomalies(unittest.TestCase):
    def test_detects_slow_run(self):
        events = [
            {**make_event("run done", agent="executor"), "role": "executor", "duration_seconds": 900}
        ]
        findings = classify_time_anomalies(FIXTURE_ROLE_EFFICIENCY, events)
        self.assertTrue(any(f["category"] == "time_anomaly" for f in findings))

    def test_no_anomaly_within_2x(self):
        events = [
            {**make_event("run done", agent="executor"), "role": "executor", "duration_seconds": 400}
        ]
        findings = classify_time_anomalies(FIXTURE_ROLE_EFFICIENCY, events)
        self.assertEqual(findings, [])

    def test_empty_role_efficiency(self):
        events = [
            {**make_event("run done"), "role": "executor", "duration_seconds": 9000}
        ]
        findings = classify_time_anomalies({}, events)
        self.assertEqual(findings, [])


class TestBuildReport(unittest.TestCase):
    def test_shape(self):
        findings = [
            {
                "category": "cost_outlier",
                "severity": "medium",
                "title": "test",
                "evidence": [],
                "suggested_discussion_title": "test",
                "suggested_tag": "[Small]",
            }
        ]
        report = build_report(SINCE, findings, runs_analyzed=50)
        self.assertIn("report_at", report)
        self.assertIn("window", report)
        self.assertEqual(report["runs_analyzed"], 50)
        self.assertEqual(len(report["findings"]), 1)
        self.assertIn("since", report["window"])
        self.assertIn("until", report["window"])

    def test_empty_findings(self):
        report = build_report(SINCE, [], runs_analyzed=0)
        self.assertEqual(report["findings"], [])


class TestChunkSize(unittest.TestCase):
    def test_chunk_size_constant(self):
        self.assertLessEqual(CHUNK_SIZE, 30)
        self.assertGreater(CHUNK_SIZE, 0)


class TestLoadFunctions(unittest.TestCase):

    @patch("run_analyst.subprocess.run")
    def test_load_audit_trail_handles_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        from run_analyst import load_audit_trail
        result = load_audit_trail(SINCE)
        self.assertEqual(result, [])

    @patch("run_analyst.subprocess.run")
    def test_load_needs_fix_prs_handles_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        from run_analyst import load_needs_fix_prs
        result = load_needs_fix_prs()
        self.assertEqual(result, [])

    @patch("run_analyst.subprocess.run")
    def test_load_audit_trail_handles_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        from run_analyst import load_audit_trail
        result = load_audit_trail(SINCE)
        self.assertEqual(result, [])

    @patch("run_analyst.subprocess.run")
    def test_no_runaway_spawn_in_tests(self, mock_run):
        """Verify that running classifiers does not invoke claude or /loop."""
        classify_failure_clusters(FIXTURE_EVENTS, [], [])
        classify_cost_outliers(FIXTURE_ROLE_EFFICIENCY, {})
        classify_fix_cycle_loops(FIXTURE_EVENTS, [], [])
        classify_stalled_patterns(FIXTURE_EVENTS, SINCE)
        classify_spec_quality_flags(FIXTURE_EVENTS, [])
        classify_tool_use_anomalies(FIXTURE_EVENTS, [], [])
        classify_time_anomalies(FIXTURE_ROLE_EFFICIENCY, FIXTURE_EVENTS)

        for call_args in mock_run.call_args_list:
            args = call_args[0][0] if call_args[0] else []
            if isinstance(args, list):
                for arg in args:
                    self.assertNotIn("claude", str(arg).lower(),
                                     f"Classifier invoked claude: {args}")


# ---------------------------------------------------------------------------
# Phase A new classifier tests (Discussion #478)
# ---------------------------------------------------------------------------

class TestWorktreeContamination(unittest.TestCase):
    def test_detects_git_checkout_by_executor(self):
        events = [make_event("switched to branch discussion-99", agent="executor")]
        findings = classify_worktree_contamination(events, [], [])
        self.assertTrue(any(f["category"] == "worktree_contamination" for f in findings))

    def test_detects_gh_pr_checkout(self):
        events = [make_event("gh pr checkout 123 executed", agent="executor")]
        findings = classify_worktree_contamination(events, [], [])
        self.assertTrue(any(f["category"] == "worktree_contamination" for f in findings))

    def test_no_false_positive_on_normal_run(self):
        events = [make_event("executor created PR #42")]
        findings = classify_worktree_contamination(events, [], [])
        self.assertEqual(findings, [])

    def test_severity_is_high(self):
        events = [make_event("git checkout main in executor run", agent="executor")]
        findings = classify_worktree_contamination(events, [], [])
        high = [f for f in findings if f["severity"] == "high"]
        self.assertTrue(len(high) > 0)


class TestHardRuleViolations(unittest.TestCase):
    def test_detects_subprocess_popen_claude(self):
        events = [make_event("subprocess.Popen(['claude', '-p', '...'])")]
        findings = classify_hard_rule_violations(events, [], [])
        self.assertTrue(any(f["category"] == "hard_rule_violation" for f in findings))

    def test_detects_git_rm(self):
        events = [make_event("git rm backend/old_file.py from executor")]
        findings = classify_hard_rule_violations(events, [], [])
        self.assertTrue(any(f["category"] == "hard_rule_violation" for f in findings))

    def test_detects_general_purpose_subagent(self):
        events = [make_event("spawning general-purpose subagent_type for code review")]
        findings = classify_hard_rule_violations(events, [], [])
        self.assertTrue(any(f["category"] == "hard_rule_violation" for f in findings))

    def test_no_false_positive(self):
        events = [make_event("executor completed successfully")]
        findings = classify_hard_rule_violations(events, [], [])
        self.assertEqual(findings, [])

    def test_severity_is_high(self):
        events = [make_event("subprocess.Popen(['claude'])")]
        findings = classify_hard_rule_violations(events, [], [])
        self.assertTrue(all(f["severity"] == "high" for f in findings))


class TestAgentOutputMissing(unittest.TestCase):
    def test_detects_missing_envelope(self):
        events = [make_event("WARNING — AGENT_OUTPUT envelope missing or malformed from executor, falling back to prose parsing")]
        findings = classify_agent_output_missing(events, [], [])
        self.assertTrue(any(f["category"] == "agent_output_missing" for f in findings))

    def test_detects_malformed_envelope(self):
        events = [make_event("malformed envelope from code-reviewer — could not parse JSON")]
        findings = classify_agent_output_missing(events, [], [])
        self.assertTrue(any(f["category"] == "agent_output_missing" for f in findings))

    def test_no_false_positive(self):
        events = [make_event("AGENT_OUTPUT envelope received and parsed successfully")]
        findings = classify_agent_output_missing(events, [], [])
        self.assertEqual(findings, [])

    def test_severity_is_medium(self):
        events = [make_event("AGENT_OUTPUT missing from executor")]
        findings = classify_agent_output_missing(events, [], [])
        self.assertTrue(all(f["severity"] == "medium" for f in findings))


class TestTestCoverageGap(unittest.TestCase):
    def test_detects_empty_tests_run_on_pass(self):
        events = [make_event("tests_run: [] — code-review-passed verdict issued for pr:#55")]
        findings = classify_test_coverage_gap(events, [])
        self.assertTrue(any(f["category"] == "test_coverage_gap" for f in findings))

    def test_detects_from_envelope_data(self):
        event = {
            "message": "agent output",
            "agent": "code-reviewer",
            "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": {"agent": "code-reviewer", "verdict": "pass", "pr": 55, "tests_run": []},
        }
        findings = classify_test_coverage_gap([event], [])
        self.assertTrue(any(f["category"] == "test_coverage_gap" for f in findings))

    def test_no_flag_when_tests_run_present(self):
        event = {
            "message": "agent output",
            "agent": "code-reviewer",
            "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": {"agent": "code-reviewer", "verdict": "pass", "pr": 55,
                     "tests_run": [{"command": "pytest", "exit_code": 0}]},
        }
        findings = classify_test_coverage_gap([event], [])
        gap = [f for f in findings if f["category"] == "test_coverage_gap"]
        self.assertEqual(gap, [])


class TestMissingPostAgentHook(unittest.TestCase):
    def test_detects_spawn_without_hook(self):
        events = [
            make_event("SPAWN_REQUEST: Discussion #42 — Executor", agent="team-lead"),
        ]
        findings = classify_missing_post_agent_hook(events, [])
        self.assertTrue(any(f["category"] == "missing_post_agent_hook" for f in findings))

    def test_no_flag_when_hook_recorded(self):
        events = [
            {**make_event("SPAWN_REQUEST: Discussion #42 — Executor spawned", agent="team-lead"),
             "role": "executor"},
            {**make_event("post-agent-hook completed for executor", agent="team-lead"),
             "role": "executor"},
        ]
        findings = classify_missing_post_agent_hook(events, [])
        self.assertEqual(findings, [])

    def test_severity_is_medium(self):
        events = [make_event("SPAWN_REQUEST: Discussion #10 — executor spawned")]
        findings = classify_missing_post_agent_hook(events, [])
        self.assertTrue(all(f["severity"] == "medium" for f in findings))


class TestTokenBurnNoOutput(unittest.TestCase):
    def test_detects_high_token_run_no_output(self):
        event = {
            "message": "agent run completed silently",
            "agent": "executor",
            "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "input_tokens": 80000,
            "output_tokens": 25000,
        }
        findings = classify_token_burn_no_output([event], [])
        self.assertTrue(any(f["category"] == "token_burn_no_output" for f in findings))

    def test_no_flag_when_pr_mentioned(self):
        event = {
            "message": "PR #42 created for Discussion #99 — agent output",
            "agent": "executor",
            "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "input_tokens": 80000,
            "output_tokens": 25000,
        }
        findings = classify_token_burn_no_output([event], [])
        self.assertEqual(findings, [])

    def test_no_flag_below_threshold(self):
        event = {
            "message": "agent run completed silently",
            "agent": "executor",
            "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "input_tokens": 5000,
            "output_tokens": 2000,
        }
        findings = classify_token_burn_no_output([event], [])
        self.assertEqual(findings, [])

    def test_severity_is_medium(self):
        event = {
            "message": "silent run",
            "agent": "executor",
            "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "input_tokens": 90000,
            "output_tokens": 20000,
        }
        findings = classify_token_burn_no_output([event], [])
        self.assertTrue(all(f["severity"] == "medium" for f in findings))


class TestDiscussionRespunNTimes(unittest.TestCase):
    def test_detects_5_spawns(self):
        events = [
            make_event(f"executor spawned for discussion:#55 started attempt {i}", agent="team-lead")
            for i in range(6)
        ]
        findings = classify_discussion_respun_n_times(events, [])
        self.assertTrue(any(f["category"] == "discussion_respun_too_many" for f in findings))

    def test_severity_high_above_5(self):
        events = [
            make_event(f"executor spawned for discussion:#77 started", agent="team-lead")
            for _ in range(6)
        ]
        findings = classify_discussion_respun_n_times(events, [])
        high = [f for f in findings if f["severity"] == "high"]
        self.assertTrue(len(high) > 0)

    def test_no_flag_for_3_or_fewer(self):
        events = [
            make_event("executor spawned for discussion:#88 started", agent="team-lead")
            for _ in range(3)
        ]
        findings = classify_discussion_respun_n_times(events, [])
        self.assertEqual(findings, [])


class TestHookEventSpam(unittest.TestCase):
    def test_detects_10_duplicates(self):
        events = [{"hook_event_id": "abc123def456", "type": "pre_spawn"} for _ in range(12)]
        findings = classify_hook_event_spam(events)
        self.assertTrue(any(f["category"] == "hook_event_spam" for f in findings))

    def test_no_flag_below_10(self):
        events = [{"hook_event_id": "abc123def456", "type": "pre_spawn"} for _ in range(9)]
        findings = classify_hook_event_spam(events)
        self.assertEqual(findings, [])

    def test_no_flag_for_unique_ids(self):
        events = [{"hook_event_id": f"id_{i}", "type": "pre_spawn"} for i in range(20)]
        findings = classify_hook_event_spam(events)
        self.assertEqual(findings, [])

    def test_severity_is_medium(self):
        events = [{"hook_event_id": "spam_id_abc", "type": "hook"} for _ in range(10)]
        findings = classify_hook_event_spam(events)
        self.assertTrue(all(f["severity"] == "medium" for f in findings))


class TestTranscriptRepetition(unittest.TestCase):
    def test_detects_5_reads_same_file(self):
        # Same run_id, same Read call 5 times
        events = [
            {**make_event(f"Read {FIXTURE_MAIN_REPO}/CLAUDE.md"),
             "run_id": "run-99"}
            for _ in range(5)
        ]
        findings = classify_transcript_repetition(events, [])
        self.assertTrue(any(f["category"] == "transcript_repetition" for f in findings))

    def test_no_flag_below_5(self):
        events = [
            {**make_event(f"Read {FIXTURE_HOME}/file.txt"), "run_id": "run-100"}
            for _ in range(4)
        ]
        findings = classify_transcript_repetition(events, [])
        self.assertEqual(findings, [])

    def test_severity_high_above_20(self):
        events = [
            {**make_event(f"Read {FIXTURE_MAIN_REPO}/CLAUDE.md"),
             "run_id": "run-88"}
            for _ in range(22)
        ]
        findings = classify_transcript_repetition(events, [])
        high = [f for f in findings if f["severity"] == "high"]
        self.assertTrue(len(high) > 0)

    def test_low_severity_at_5(self):
        events = [
            {**make_event(f"Read {FIXTURE_HOME}/x.py"), "run_id": "run-77"}
            for _ in range(5)
        ]
        findings = classify_transcript_repetition(events, [])
        low = [f for f in findings if f["category"] == "transcript_repetition" and f["severity"] == "low"]
        self.assertTrue(len(low) > 0)


class TestSpecImplSemanticGap(unittest.TestCase):
    def test_detects_do_not_modify_violation(self):
        events = [
            make_event("reviewer noted: do not modify backend/api.py but diff modifies it — PR:#43")
        ]
        findings = classify_spec_impl_semantic_gap(events, [], [])
        self.assertTrue(any(f["category"] == "spec_impl_semantic_gap" for f in findings))

    def test_no_flag_on_clean_run(self):
        events = [make_event("code review passed — spec adherence confirmed")]
        findings = classify_spec_impl_semantic_gap(events, [], [])
        self.assertEqual(findings, [])

    @patch("run_analyst.get_pr_diff_size")
    def test_detects_size_ceiling_exceeded(self, mock_diff):
        mock_diff.return_value = {"additions": 400, "deletions": 200}
        # needs_fix_prs with pr#55; feed has spec ceiling ≤200 for that PR
        events = [make_event("spec says ≤200 lines for pr:#55")]
        needs_fix = [{"number": 55, "title": "big PR", "url": "", "labels": [], "createdAt": ""}]
        findings = classify_spec_impl_semantic_gap(events, [], needs_fix)
        self.assertTrue(any(f["category"] == "spec_impl_semantic_gap" for f in findings))


class TestBranchDrift(unittest.TestCase):
    @patch("run_analyst.get_current_branch")
    def test_detects_non_main_branch(self, mock_branch):
        mock_branch.return_value = "discussion-99-some-feature"
        findings = classify_branch_drift([])
        self.assertTrue(any(f["category"] == "branch_drift" for f in findings))

    @patch("run_analyst.get_current_branch")
    def test_no_flag_on_main(self, mock_branch):
        mock_branch.return_value = "main"
        findings = classify_branch_drift([])
        drift = [f for f in findings if f["category"] == "branch_drift"]
        self.assertEqual(drift, [])

    @patch("run_analyst.get_current_branch")
    def test_detects_historical_drift_from_feed(self, mock_branch):
        mock_branch.return_value = "main"
        events = [make_event("parent repo branch drifted off main — worktree contamination detected")]
        findings = classify_branch_drift(events)
        self.assertTrue(any(f["category"] == "branch_drift" for f in findings))

    @patch("run_analyst.get_current_branch")
    def test_severity_high_for_current_drift(self, mock_branch):
        mock_branch.return_value = "feat/broken-branch"
        findings = classify_branch_drift([])
        high = [f for f in findings if f["severity"] == "high"]
        self.assertTrue(len(high) > 0)


class TestStaleSnapshotConsumption(unittest.TestCase):
    def test_detects_stale_snapshot_in_feed(self):
        events = [make_event("SnapshotStale error — snapshot is too old, re-running")]
        findings = classify_stale_snapshot_consumption([], events)
        self.assertTrue(any(f["category"] == "stale_snapshot_consumption" for f in findings))

    def test_detects_stale_keyword_variant(self):
        events = [make_event("snapshot stale warning — regenerating loop-snapshot.json")]
        findings = classify_stale_snapshot_consumption([], events)
        self.assertTrue(any(f["category"] == "stale_snapshot_consumption" for f in findings))

    @patch("run_analyst.load_loop_snapshot")
    def test_no_flag_on_clean_snapshot_ref(self, mock_snap):
        mock_snap.return_value = {}
        events = [make_event("loop-snapshot loaded successfully — age=120s")]
        findings = classify_stale_snapshot_consumption([], events)
        self.assertEqual(findings, [])

    def _snapshot_findings(self, age_seconds: int) -> list[dict]:
        """Findings for a snapshot generated exactly ``age_seconds`` ago.

        Exactly, not approximately: both the input and the clock it is aged
        against come from SNAPSHOT_CLOCK, so the interval does not depend on
        how long the suite took to get here.
        """
        ts = (SNAPSHOT_CLOCK - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with patch("run_analyst.load_loop_snapshot", return_value={"generated_at": ts}):
            with frozen_clock(SNAPSHOT_CLOCK):
                findings = classify_stale_snapshot_consumption([], [])
        return [f for f in findings if f["category"] == "stale_snapshot_consumption"]

    def test_detects_old_snapshot_file(self):
        self.assertTrue(self._snapshot_findings(700))

    def test_no_flag_fresh_snapshot(self):
        self.assertEqual(self._snapshot_findings(100), [])

    def test_flags_exactly_at_the_max_age_boundary(self):
        """The threshold itself, which a live clock could never pin down.

        MAX_AGE is 600s and the comparison is strictly greater-than, so 601s
        flags and 599s does not. Asserting a one-second margin is only
        meaningful because the clock is frozen — against a live one this pair
        would have been decided by scheduler jitter.
        """
        self.assertTrue(self._snapshot_findings(601))
        self.assertEqual(self._snapshot_findings(599), [])


class TestBudgetCapProximity(unittest.TestCase):
    def test_detects_80_percent_usage(self):
        budget_data = {
            "agents/executor": {
                "role": "executor",
                "spent": 420000,
                "ceiling": 500000,
            }
        }
        findings = classify_budget_cap_proximity(budget_data)
        self.assertTrue(any(f["category"] == "budget_cap_proximity" for f in findings))

    def test_detects_100_percent(self):
        budget_data = {
            "agents/code-reviewer": {
                "role": "code-reviewer",
                "spent": 500000,
                "ceiling": 500000,
            }
        }
        findings = classify_budget_cap_proximity(budget_data)
        self.assertTrue(any(f["category"] == "budget_cap_proximity" for f in findings))

    def test_no_flag_below_80_percent(self):
        budget_data = {
            "agents/executor": {
                "role": "executor",
                "spent": 300000,
                "ceiling": 500000,
            }
        }
        findings = classify_budget_cap_proximity(budget_data)
        self.assertEqual(findings, [])

    def test_severity_is_medium(self):
        budget_data = {
            "agents/executor": {"role": "executor", "spent": 450000, "ceiling": 500000}
        }
        findings = classify_budget_cap_proximity(budget_data)
        self.assertTrue(all(f["severity"] == "medium" for f in findings))


class TestPreSpawnCheckMissing(unittest.TestCase):
    def test_detects_spawn_without_pre_check(self):
        events = [
            make_event("SPAWN_REQUEST: Discussion #42 — executor spawned", agent="team-lead"),
        ]
        findings = classify_pre_spawn_check_missing(events, [])
        self.assertTrue(any(f["category"] == "pre_spawn_check_missing" for f in findings))

    def test_no_flag_when_pre_check_present(self):
        events = [
            make_event("scripts/pre-spawn-check.sh --role executor --discussion 42 passed", agent="team-lead"),
            make_event("SPAWN_REQUEST: Discussion #42 — executor spawned", agent="team-lead"),
        ]
        findings = classify_pre_spawn_check_missing(events, [])
        self.assertEqual(findings, [])

    def test_no_flag_with_no_spawns(self):
        events = [make_event("normal team-lead heartbeat")]
        findings = classify_pre_spawn_check_missing(events, [])
        self.assertEqual(findings, [])

    def test_severity_is_medium(self):
        events = [make_event("SPAWN_REQUEST: Discussion #10 — executor", agent="team-lead")]
        findings = classify_pre_spawn_check_missing(events, [])
        self.assertTrue(all(f["severity"] == "medium" for f in findings))


class TestNewClassifiersNoRunaway(unittest.TestCase):
    """Verify none of the new classifiers invoke claude or /loop (Discussion #439)."""

    @patch("run_analyst.subprocess.run")
    @patch("run_analyst.get_current_branch")
    @patch("run_analyst.load_loop_snapshot")
    def test_no_claude_spawn_in_new_classifiers(self, mock_snap, mock_branch, mock_run):
        mock_branch.return_value = "main"
        mock_snap.return_value = {}
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")

        events = [make_event("sample event for hard rule test git rm backend/x.py")]
        classify_worktree_contamination(events, [], [])
        classify_hard_rule_violations(events, [], [])
        classify_agent_output_missing(events, [], [])
        classify_test_coverage_gap(events, [])
        classify_missing_post_agent_hook(events, [])
        classify_token_burn_no_output(events, [])
        classify_discussion_respun_n_times(events, [])
        classify_hook_event_spam([])
        classify_transcript_repetition(events, [])
        classify_spec_impl_semantic_gap(events, [], [])
        classify_branch_drift(events)
        classify_stale_snapshot_consumption([], events)
        classify_budget_cap_proximity({})
        classify_pre_spawn_check_missing(events, [])

        for call_args in mock_run.call_args_list:
            args = call_args[0][0] if call_args[0] else []
            if isinstance(args, list):
                for arg in args:
                    self.assertNotIn("claude", str(arg).lower(),
                                     f"New classifier invoked claude: {args}")


if __name__ == "__main__":
    unittest.main()
