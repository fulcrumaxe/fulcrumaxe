"""Behavioral tests for backend/stats/anomaly_detector.py.

Covers:
  - detect(): anomaly threshold logic (above / at / below threshold),
    output shape and field values, false-positive guards (zero, NaN, Inf,
    None, metric mismatch, non-numeric), bidirectional ratio (spike + drop),
    config override vs anomaly_config lookup, boundary values.
  - _post_team_log(): subprocess calls are mocked — no real gh calls made.
  - run_detection(): full flow with an in-memory DuckDB fixture; verifies
    anomaly rows written to stat_anomalies, team-log post suppressed via
    post_team_log=False, and no-history / missing-db early exits.

ISOLATION GUARANTEE:
  - DuckDB: tests that exercise run_detection() use tmp_path-backed .duckdb
    files; STATS_DB_PATH env var is NOT set, avoiding any real state dir.
  - subprocess: _post_team_log is tested by patching subprocess.run via
    monkeypatch; when post_team_log=False is passed to run_detection() the
    subprocess path is never reached.
  - No real team-log comment is posted by any test in this file.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make the repo root importable regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.stats.anomaly_config import DEFAULT_THRESHOLD, threshold_for
from backend.stats.anomaly_detector import (
    Anomaly,
    MetricRow,
    _post_team_log,
    _write_anomalies,
    detect,
    ensure_table,
    run_detection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(metric: str, value: float, ts: str = "2026-01-01T00:00:00Z", project_tag: str = "") -> dict:
    return {"metric": metric, "value": value, "ts": ts, "project_tag": project_tag}


def _make_duckdb_with_metric_event(tmp_path: Path, rows: list[dict]) -> Path:
    """Create a throwaway DuckDB file with a minimal metric_event table."""
    import duckdb  # type: ignore[import]

    db_file = tmp_path / "test_stats.duckdb"
    con = duckdb.connect(str(db_file))
    con.execute("""
        CREATE TABLE IF NOT EXISTS metric_event (
            ts          TIMESTAMPTZ NOT NULL,
            metric      TEXT NOT NULL,
            value       DOUBLE,
            tags        TEXT DEFAULT '{}'
        )
    """)
    for r in rows:
        con.execute(
            "INSERT INTO metric_event (ts, metric, value, tags) VALUES (?, ?, ?, ?)",
            [r["ts"], r["metric"], r["value"], r.get("tags", "{}")],
        )
    con.close()
    return db_file


# ===========================================================================
# detect() — pure function tests
# ===========================================================================


class TestDetectNormalReturnsEmpty:
    """Values that stay within threshold → empty list."""

    def test_no_change(self):
        prev = _row("fail_rate", 5.0, "2026-01-01T00:00:00Z")
        curr = _row("fail_rate", 5.0, "2026-01-01T01:00:00Z")
        assert detect(prev, curr) == []

    def test_small_increase_below_threshold(self):
        # fail_rate threshold = 10x; 9x increase should not fire
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", 9.0)
        result = detect(prev, curr)
        assert result == []

    def test_exactly_at_threshold_not_fired(self):
        # ratio == threshold → NOT an anomaly (> required, not >=)
        threshold = threshold_for("fail_rate")  # 10.0
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", threshold)  # ratio == 10.0, not > 10.0
        result = detect(prev, curr)
        assert result == []

    def test_small_decrease_below_threshold(self):
        # cost metric threshold = 5x; 4x drop should not fire
        prev = _row("total_cost_usd", 10.0)
        curr = _row("total_cost_usd", 2.5)  # 4x drop
        result = detect(prev, curr)
        assert result == []


class TestDetectAnomalyFired:
    """Values that exceed threshold → Anomaly returned."""

    def test_large_increase_fires_anomaly(self):
        # fail_rate threshold = 10x; 11x spike
        prev = _row("fail_rate", 1.0, "2026-01-01T00:00:00Z")
        curr = _row("fail_rate", 11.0, "2026-01-01T01:00:00Z")
        result = detect(prev, curr)
        assert len(result) == 1
        a = result[0]
        assert a.metric == "fail_rate"
        assert a.prev_value == pytest.approx(1.0)
        assert a.current_value == pytest.approx(11.0)
        assert a.ratio == pytest.approx(11.0)
        assert a.threshold == pytest.approx(10.0)
        assert a.ts == "2026-01-01T01:00:00Z"

    def test_large_decrease_fires_anomaly(self):
        # Ratio is always ≥ 1; a drop of 11x should also fire
        prev = _row("fail_rate", 11.0, "2026-01-01T00:00:00Z")
        curr = _row("fail_rate", 1.0, "2026-01-01T01:00:00Z")
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].ratio == pytest.approx(11.0)

    def test_cost_metric_fires_at_lower_threshold(self):
        # total_cost_usd threshold = 5x; 6x spike should fire
        prev = _row("total_cost_usd", 1.0)
        curr = _row("total_cost_usd", 6.0)
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].threshold == pytest.approx(5.0)

    def test_duration_metric_requires_large_swing(self):
        # duration_s threshold = 20x; 15x should not fire
        prev = _row("duration_s", 1.0)
        curr = _row("duration_s", 15.0)
        assert detect(prev, curr) == []

    def test_duration_metric_fires_past_20x(self):
        prev = _row("duration_s", 1.0)
        curr = _row("duration_s", 21.0)
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].threshold == pytest.approx(20.0)

    def test_unknown_metric_uses_default_threshold(self):
        # Unlisted metric → DEFAULT_THRESHOLD (10.0)
        prev = _row("totally_new_metric", 1.0)
        curr = _row("totally_new_metric", 11.0)
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].threshold == pytest.approx(DEFAULT_THRESHOLD)


class TestDetectOutputShape:
    """Verify Anomaly dataclass fields are populated correctly."""

    def test_project_tag_propagated(self):
        prev = _row("fail_rate", 1.0, project_tag="proj-alpha")
        curr = _row("fail_rate", 11.0, project_tag="proj-alpha")
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].project_tag == "proj-alpha"

    def test_ts_is_current_rows_ts(self):
        prev = _row("fail_rate", 1.0, ts="2026-01-01T00:00:00Z")
        curr = _row("fail_rate", 11.0, ts="2026-01-02T00:00:00Z")
        result = detect(prev, curr)
        assert result[0].ts == "2026-01-02T00:00:00Z"

    def test_format_log_line_contains_metric(self):
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", 11.0)
        a = detect(prev, curr)[0]
        line = a.format_log_line()
        assert "fail_rate" in line
        assert "→" in line
        assert "x," in line  # ratio formatted as Nx,


class TestDetectConfigOverride:
    """Custom config dict overrides anomaly_config thresholds."""

    def test_custom_lower_threshold_fires_earlier(self):
        # Without override: default 10x wouldn't fire at 3x
        prev = _row("my_metric", 1.0)
        curr = _row("my_metric", 3.0)
        assert detect(prev, curr) == []  # sanity — would not fire at default

        # With custom threshold of 2x → should fire
        result = detect(prev, curr, config={"my_metric": 2.0})
        assert len(result) == 1
        assert result[0].threshold == pytest.approx(2.0)

    def test_custom_higher_threshold_suppresses_alert(self):
        # fail_rate default = 10x; override to 100x → 11x should not fire
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", 11.0)
        result = detect(prev, curr, config={"fail_rate": 100.0})
        assert result == []

    def test_config_falls_through_to_anomaly_config_for_unlisted(self):
        # Passing a config that doesn't contain this metric → falls back to anomaly_config
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", 11.0)
        result = detect(prev, curr, config={"other_metric": 2.0})
        # fail_rate threshold is 10x from anomaly_config → 11x fires
        assert len(result) == 1


class TestDetectFalsePositiveGuards:
    """Guards that prevent noisy false positives."""

    def test_zero_prev_value_skipped(self):
        prev = _row("fail_rate", 0.0)
        curr = _row("fail_rate", 100.0)
        assert detect(prev, curr) == []

    def test_zero_current_value_skipped(self):
        prev = _row("fail_rate", 100.0)
        curr = _row("fail_rate", 0.0)
        assert detect(prev, curr) == []

    def test_both_zero_skipped(self):
        prev = _row("fail_rate", 0.0)
        curr = _row("fail_rate", 0.0)
        assert detect(prev, curr) == []

    def test_none_prev_value_skipped(self):
        prev = {"metric": "fail_rate", "value": None, "ts": "2026-01-01T00:00:00Z", "project_tag": ""}
        curr = _row("fail_rate", 11.0)
        assert detect(prev, curr) == []

    def test_none_current_value_skipped(self):
        prev = _row("fail_rate", 1.0)
        curr = {"metric": "fail_rate", "value": None, "ts": "2026-01-01T01:00:00Z", "project_tag": ""}
        assert detect(prev, curr) == []

    def test_nan_prev_skipped(self):
        prev = _row("fail_rate", float("nan"))
        curr = _row("fail_rate", 11.0)
        assert detect(prev, curr) == []

    def test_nan_current_skipped(self):
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", float("nan"))
        assert detect(prev, curr) == []

    def test_inf_prev_skipped(self):
        prev = _row("fail_rate", float("inf"))
        curr = _row("fail_rate", 11.0)
        assert detect(prev, curr) == []

    def test_inf_current_skipped(self):
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", float("inf"))
        assert detect(prev, curr) == []

    def test_metric_name_mismatch_skipped(self):
        prev = _row("fail_rate", 1.0)
        curr = _row("other_metric", 11.0)
        assert detect(prev, curr) == []

    def test_non_numeric_string_skipped(self):
        prev = {"metric": "fail_rate", "value": "abc", "ts": "2026-01-01T00:00:00Z", "project_tag": ""}
        curr = _row("fail_rate", 11.0)
        assert detect(prev, curr) == []


class TestDetectBoundaryValues:
    """Boundary and edge values."""

    def test_just_above_threshold_fires(self):
        # fail_rate = 10x; ratio = 10.0001 > 10.0
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", 10.0001)
        result = detect(prev, curr)
        assert len(result) == 1

    def test_just_below_threshold_no_fire(self):
        prev = _row("fail_rate", 1.0)
        curr = _row("fail_rate", 9.9999)
        assert detect(prev, curr) == []

    def test_very_large_values_work(self):
        prev = _row("fail_rate", 1e12)
        curr = _row("fail_rate", 1.2e12)  # 1.2x — well within 10x
        assert detect(prev, curr) == []

    def test_very_small_values_work(self):
        prev = _row("fail_rate", 1e-10)
        curr = _row("fail_rate", 1.2e-9)  # 12x — fires at 10x threshold
        result = detect(prev, curr)
        assert len(result) == 1

    def test_negative_values_ratio_computed_correctly(self):
        # Both negative: ratio = (-2)/(-1) = 2.0 — within 10x threshold
        prev = _row("fail_rate", -1.0)
        curr = _row("fail_rate", -2.0)
        assert detect(prev, curr) == []

    def test_sign_flip_with_large_magnitude(self):
        # prev = -1, curr = 11 → magnitude swing is 11x across zero.
        # abs(11) / abs(-1) = 11.0 > threshold (10.0) → anomaly fires.
        prev = _row("fail_rate", -1.0)
        curr = _row("fail_rate", 11.0)
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].ratio == pytest.approx(11.0)

    def test_sign_flip_small_magnitude_no_false_positive(self):
        # prev = -1, curr = 2 → magnitude swing is 2x across zero.
        # 2x is well within the 10x fail_rate threshold → no anomaly.
        prev = _row("fail_rate", -1.0)
        curr = _row("fail_rate", 2.0)
        assert detect(prev, curr) == []

    def test_sign_flip_reverse_direction_large_magnitude(self):
        # prev = 10, curr = -110 → magnitude swing is 11x → should fire.
        prev = _row("fail_rate", 10.0)
        curr = _row("fail_rate", -110.0)
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].ratio == pytest.approx(11.0)

    def test_both_negative_small_swing_no_false_positive(self):
        # Both negative, small change: prev=-10, curr=-12 → 1.2x → no fire.
        prev = _row("fail_rate", -10.0)
        curr = _row("fail_rate", -12.0)
        assert detect(prev, curr) == []

    def test_both_negative_large_swing_fires(self):
        # Both negative, large change: prev=-1, curr=-12 → 12x → fires.
        prev = _row("fail_rate", -1.0)
        curr = _row("fail_rate", -12.0)
        result = detect(prev, curr)
        assert len(result) == 1
        assert result[0].ratio == pytest.approx(12.0)


# ===========================================================================
# _post_team_log() — subprocess mock tests
# ===========================================================================


class TestPostTeamLog:
    """Verify _post_team_log posts to the right issue via subprocess, fully mocked.

    subprocess is lazy-imported inside _post_team_log, so we patch the stdlib
    subprocess.run directly rather than the module attribute on the detector.
    """

    def _make_anomaly(self) -> Anomaly:
        return Anomaly(
            metric="fail_rate",
            project_tag="",
            prev_value=1.0,
            current_value=15.0,
            ratio=15.0,
            threshold=10.0,
            ts="2026-01-01T00:00:00Z",
        )

    def test_posts_comment_when_issue_found(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            mock = MagicMock()
            if "list" in cmd:
                mock.stdout = "42\n"
            else:
                mock.stdout = ""
            return mock

        with patch("subprocess.run", fake_run):
            _post_team_log([self._make_anomaly()], "autonomous-agent-7/autonomous-forever")

        # First call: gh issue list to find team-log number
        assert calls[0][0] == "gh"
        assert "list" in calls[0]
        assert "--label" in calls[0]
        # Second call: gh issue comment
        assert calls[1][0] == "gh"
        assert "comment" in calls[1]
        # Body should mention the anomaly metric
        body_idx = calls[1].index("--body") + 1
        assert "fail_rate" in calls[1][body_idx]

    def test_skips_comment_when_no_team_log_found(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            mock = MagicMock()
            mock.stdout = ""  # no issue found
            return mock

        with patch("subprocess.run", fake_run):
            _post_team_log([self._make_anomaly()], "autonomous-agent-7/autonomous-forever")

        # Only one call (the list call) — comment not posted
        assert len(calls) == 1

    def test_multiple_anomalies_in_one_comment(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            mock = MagicMock()
            mock.stdout = "42\n" if "list" in cmd else ""
            return mock

        anomalies = [
            Anomaly("fail_rate", "", 1.0, 15.0, 15.0, 10.0, "2026-01-01T00:00:00Z"),
            Anomaly("total_cost_usd", "", 1.0, 8.0, 8.0, 5.0, "2026-01-01T00:00:00Z"),
        ]
        with patch("subprocess.run", fake_run):
            _post_team_log(anomalies, "autonomous-agent-7/autonomous-forever")

        body_idx = calls[1].index("--body") + 1
        body = calls[1][body_idx]
        assert "fail_rate" in body
        assert "total_cost_usd" in body

    def test_subprocess_exception_does_not_propagate(self):
        def fake_run(cmd, **kwargs):
            raise RuntimeError("network error")

        with patch("subprocess.run", fake_run):
            # Should swallow the exception — logged as warning
            _post_team_log([self._make_anomaly()], "autonomous-agent-7/autonomous-forever")


# ===========================================================================
# run_detection() — I/O flow tests with tmp DuckDB
# ===========================================================================


class TestRunDetectionNoDb:
    """run_detection() returns [] when no DB file exists."""

    def test_missing_db_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist.duckdb"
        result = run_detection(db_path=nonexistent, post_team_log=False)
        assert result == []


class TestRunDetectionNoHistory:
    """run_detection() returns [] when DB has < 2 distinct timestamps."""

    def test_only_one_timestamp_no_anomalies(self, tmp_path):
        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 5.0},
        ])
        result = run_detection(db_path=db, post_team_log=False)
        assert result == []

    def test_empty_metric_event_returns_empty(self, tmp_path):
        db = _make_duckdb_with_metric_event(tmp_path, [])
        result = run_detection(db_path=db, post_team_log=False)
        assert result == []


class TestRunDetectionAllNormal:
    """run_detection() returns [] when all metrics are within threshold."""

    def test_two_timestamps_no_spike(self, tmp_path):
        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 5.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 6.0},
        ])
        result = run_detection(db_path=db, post_team_log=False)
        assert result == []

    def test_multiple_metrics_all_normal(self, tmp_path):
        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 5.0},
            {"ts": "2026-01-01T00:00:00Z", "metric": "total_cost_usd", "value": 2.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 6.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "total_cost_usd", "value": 2.5},
        ])
        result = run_detection(db_path=db, post_team_log=False)
        assert result == []


class TestRunDetectionAnomalyDetected:
    """run_detection() detects anomalies and writes them to stat_anomalies."""

    def test_spike_detected_and_returned(self, tmp_path):
        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 1.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 15.0},
        ])
        result = run_detection(db_path=db, post_team_log=False)
        assert len(result) == 1
        assert result[0].metric == "fail_rate"
        assert result[0].ratio == pytest.approx(15.0)

    def test_anomaly_written_to_stat_anomalies_table(self, tmp_path):
        import duckdb

        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 1.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 15.0},
        ])
        run_detection(db_path=db, post_team_log=False)

        con = duckdb.connect(str(db))
        rows = con.execute("SELECT metric, prev_value, current_value, ratio FROM stat_anomalies").fetchall()
        con.close()
        assert len(rows) == 1
        assert rows[0][0] == "fail_rate"
        assert rows[0][1] == pytest.approx(1.0)
        assert rows[0][2] == pytest.approx(15.0)
        assert rows[0][3] == pytest.approx(15.0)

    def test_multiple_anomalies_all_written(self, tmp_path):
        import duckdb

        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 1.0},
            {"ts": "2026-01-01T00:00:00Z", "metric": "total_cost_usd", "value": 1.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 15.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "total_cost_usd", "value": 10.0},
        ])
        result = run_detection(db_path=db, post_team_log=False)
        assert len(result) == 2

        con = duckdb.connect(str(db))
        count = con.execute("SELECT COUNT(*) FROM stat_anomalies").fetchone()[0]
        con.close()
        assert count == 2

    def test_post_team_log_false_no_subprocess(self, tmp_path):
        """Confirm subprocess is never called when post_team_log=False."""
        subprocess_calls: list = []

        def fake_run(cmd, **kwargs):
            subprocess_calls.append(cmd)
            return MagicMock(stdout="")

        with patch("subprocess.run", fake_run):
            db = _make_duckdb_with_metric_event(tmp_path, [
                {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 1.0},
                {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 15.0},
            ])
            run_detection(db_path=db, post_team_log=False)

        assert subprocess_calls == [], (
            "subprocess.run was called despite post_team_log=False — real team-log post risk"
        )

    def test_post_team_log_true_calls_subprocess(self, tmp_path):
        """When post_team_log=True and anomaly exists, subprocess IS invoked (mocked)."""
        subprocess_calls: list = []

        def fake_run(cmd, **kwargs):
            subprocess_calls.append(cmd)
            mock = MagicMock()
            mock.stdout = "42\n" if "list" in cmd else ""
            return mock

        with patch("subprocess.run", fake_run):
            db = _make_duckdb_with_metric_event(tmp_path, [
                {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 1.0},
                {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 15.0},
            ])
            result = run_detection(db_path=db, post_team_log=True)

        assert len(result) == 1
        # subprocess was called for gh issue list + gh issue comment
        assert len(subprocess_calls) == 2

    def test_zero_value_in_data_no_anomaly(self, tmp_path):
        """Zero values in metric_event trigger the zero guard — no false positive."""
        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 0.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 15.0},
        ])
        result = run_detection(db_path=db, post_team_log=False)
        assert result == []

    def test_dedup_idempotent_on_second_run(self, tmp_path):
        """Running detection twice on the same data doesn't double-insert anomalies."""
        import duckdb

        db = _make_duckdb_with_metric_event(tmp_path, [
            {"ts": "2026-01-01T00:00:00Z", "metric": "fail_rate", "value": 1.0},
            {"ts": "2026-01-01T01:00:00Z", "metric": "fail_rate", "value": 15.0},
        ])
        run_detection(db_path=db, post_team_log=False)
        run_detection(db_path=db, post_team_log=False)

        con = duckdb.connect(str(db))
        count = con.execute("SELECT COUNT(*) FROM stat_anomalies").fetchone()[0]
        con.close()
        # INSERT OR IGNORE ensures exactly one row
        assert count == 1


# ===========================================================================
# _write_anomalies() — unit tests (DuckDB in tmp)
# ===========================================================================


class TestWriteAnomalies:
    """Direct tests for the _write_anomalies helper."""

    def _open_con(self, tmp_path: Path):
        import duckdb
        db = tmp_path / "write_test.duckdb"
        con = duckdb.connect(str(db))
        ensure_table(con)
        return con

    def test_inserts_single_anomaly(self, tmp_path):
        con = self._open_con(tmp_path)
        a = Anomaly("fail_rate", "", 1.0, 15.0, 15.0, 10.0, "2026-01-01T01:00:00Z")
        count = _write_anomalies(con, [a])
        assert count == 1
        rows = con.execute("SELECT metric FROM stat_anomalies").fetchall()
        assert rows == [("fail_rate",)]
        con.close()

    def test_inserts_multiple_anomalies(self, tmp_path):
        con = self._open_con(tmp_path)
        anomalies = [
            Anomaly("fail_rate", "", 1.0, 15.0, 15.0, 10.0, "2026-01-01T01:00:00Z"),
            Anomaly("total_cost_usd", "", 1.0, 8.0, 8.0, 5.0, "2026-01-01T01:00:00Z"),
        ]
        count = _write_anomalies(con, anomalies)
        assert count == 2
        con.close()

    def test_empty_list_inserts_nothing(self, tmp_path):
        con = self._open_con(tmp_path)
        count = _write_anomalies(con, [])
        assert count == 0
        con.close()

    def test_duplicate_ignored(self, tmp_path):
        con = self._open_con(tmp_path)
        a = Anomaly("fail_rate", "", 1.0, 15.0, 15.0, 10.0, "2026-01-01T01:00:00Z")
        _write_anomalies(con, [a])
        # Second insert of same (ts, metric, project_tag) → ignored
        count2 = _write_anomalies(con, [a])
        rows = con.execute("SELECT COUNT(*) FROM stat_anomalies").fetchone()[0]
        assert rows == 1
        # count2 may be 0 or 1 depending on whether OR IGNORE swallows quietly — just no crash
        con.close()


# ===========================================================================
# MetricRow dataclass smoke test
# ===========================================================================


class TestMetricRow:
    def test_fields(self):
        row = MetricRow(metric="fail_rate", project_tag="proj", value=5.0, ts="2026-01-01T00:00:00Z")
        assert row.metric == "fail_rate"
        assert row.value == 5.0
