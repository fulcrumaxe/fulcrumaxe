"""Tests for backend/orchestrator/cost_verification.py.

All tests use fixture data — no real SDK calls, no DuckDB file on disk
(except where explicitly tested with a tmp path), no Anthropic API calls.

Coverage:
  - tokens_to_usd(): rate card math per token type
  - billing_regime(): correct regime assignment by date
  - RunRecord: actual_usd computed at construction, regime set
  - reconcile_runs(): variance, accuracy, by_role, by_discussion, anomaly
  - reconcile_runs(): empty runs (no data)
  - reconcile_runs(): zero estimated (cold start)
  - reconcile_runs(): subscription vs credit regime breakdown
  - reconcile_runs(): aggregate anomaly flag (>5% deviation)
  - reconcile_runs(): per-run outlier anomaly flag
  - verify(): end-to-end with tmp DuckDB + credit file (optional DuckDB)
  - CLI: --json output structure
  - CLI: human output (smoke test, no crash)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# Ensure repo root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.cost_verification import (
    RunRecord,
    billing_regime,
    reconcile_runs,
    tokens_to_usd,
    verify,
    _CREDIT_REGIME_START,
    _ANOMALY_THRESHOLD,
    _print_human,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_days: int = 0, regime: str = "subscription") -> datetime:
    """Return a UTC datetime that lands in the given regime.

    subscription: 2026-05-20 (before cutover)
    credit:       2026-06-20 (after cutover)
    """
    if regime == "credit":
        base = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    else:
        base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(days=offset_days)


def _make_run(
    agent_id: str = "executor-1-111",
    role: str = "executor",
    discussion: int | None = 1,
    pr: int | None = None,
    model: str = "claude-sonnet-4-6",
    start_ts: datetime | None = None,
    verdict: str = "done",
    input_tok: int = 10_000,
    output_tok: int = 1_000,
    cache_read: int = 0,
    cache_write: int = 0,
) -> RunRecord:
    if start_ts is None:
        start_ts = _ts(regime="subscription")
    return RunRecord(
        agent_id=agent_id,
        role=role,
        discussion=discussion,
        pr=pr,
        model=model,
        start_ts=start_ts,
        verdict=verdict,
        input_tok=input_tok,
        output_tok=output_tok,
        cache_read=cache_read,
        cache_write=cache_write,
    )


# ---------------------------------------------------------------------------
# tokens_to_usd
# ---------------------------------------------------------------------------

class TestTokensToUsd:
    def test_input_only(self):
        # 1M input tokens × $3/1M = $3.00
        cost = tokens_to_usd(input_tok=1_000_000, output_tok=0)
        assert abs(cost - 3.0) < 1e-6

    def test_output_only(self):
        # 1M output tokens × $15/1M = $15.00
        cost = tokens_to_usd(input_tok=0, output_tok=1_000_000)
        assert abs(cost - 15.0) < 1e-6

    def test_cache_read_cheap(self):
        # 1M cache-read tokens × $0.30/1M = $0.30
        cost = tokens_to_usd(input_tok=0, output_tok=0, cache_read=1_000_000)
        assert abs(cost - 0.30) < 1e-6

    def test_cache_write_more_than_input(self):
        # 1M cache-write tokens × $3.75/1M = $3.75
        cost = tokens_to_usd(input_tok=0, output_tok=0, cache_write=1_000_000)
        assert abs(cost - 3.75) < 1e-6

    def test_combined(self):
        cost = tokens_to_usd(
            input_tok=10_000, output_tok=500,
            cache_read=5_000, cache_write=2_000,
        )
        expected = (
            10_000 * 3.00 / 1_000_000
            + 500 * 15.00 / 1_000_000
            + 5_000 * 0.30 / 1_000_000
            + 2_000 * 3.75 / 1_000_000
        )
        assert abs(cost - expected) < 1e-9

    def test_unknown_model_falls_back_to_default(self):
        cost_unknown = tokens_to_usd(1000, 100, model="gpt-99-turbo")
        cost_default = tokens_to_usd(1000, 100, model="_default")
        assert cost_unknown == cost_default

    def test_zero_tokens_zero_cost(self):
        assert tokens_to_usd(0, 0, 0, 0) == 0.0


# ---------------------------------------------------------------------------
# billing_regime
# ---------------------------------------------------------------------------

class TestBillingRegime:
    def test_before_cutover_is_subscription(self):
        ts = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert billing_regime(ts) == "subscription"

    def test_on_cutover_day_is_credit(self):
        ts = _CREDIT_REGIME_START  # 2026-06-15 00:00:00 UTC
        assert billing_regime(ts) == "credit"

    def test_after_cutover_is_credit(self):
        ts = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert billing_regime(ts) == "credit"

    def test_naive_datetime_treated_as_utc(self):
        # Naive datetime should not raise; treated as UTC
        naive = datetime(2026, 5, 1, 12, 0, 0)  # no tzinfo
        regime = billing_regime(naive)
        assert regime == "subscription"


# ---------------------------------------------------------------------------
# RunRecord
# ---------------------------------------------------------------------------

class TestRunRecord:
    def test_actual_usd_computed_on_init(self):
        r = _make_run(input_tok=10_000, output_tok=1_000)
        expected = tokens_to_usd(10_000, 1_000)
        assert abs(r.actual_usd - expected) < 1e-9

    def test_regime_subscription(self):
        r = _make_run(start_ts=_ts(regime="subscription"))
        assert r.regime == "subscription"

    def test_regime_credit(self):
        r = _make_run(start_ts=_ts(regime="credit"))
        assert r.regime == "credit"

    def test_to_dict_keys(self):
        r = _make_run()
        d = r.to_dict()
        for key in ("agent_id", "role", "actual_usd", "regime", "start_ts"):
            assert key in d

    def test_none_model_falls_back_to_default(self):
        # model=None should not raise; _default rates used
        r = RunRecord(
            agent_id="x", role="executor", discussion=None, pr=None,
            model=None,  # type: ignore[arg-type]
            start_ts=_ts(), verdict="done",
            input_tok=1000, output_tok=100,
            cache_read=0, cache_write=0,
        )
        expected = tokens_to_usd(1000, 100, model="_default")
        assert abs(r.actual_usd - expected) < 1e-9


# ---------------------------------------------------------------------------
# reconcile_runs — math correctness
# ---------------------------------------------------------------------------

class TestReconcileRunsMath:
    def _two_runs(self) -> list[RunRecord]:
        return [
            _make_run("run-1", input_tok=10_000, output_tok=1_000),
            _make_run("run-2", input_tok=20_000, output_tok=2_000),
        ]

    def test_total_actual_usd_is_sum(self):
        runs = self._two_runs()
        expected_actual = sum(r.actual_usd for r in runs)
        report = reconcile_runs(runs, estimated_total_usd=expected_actual)
        assert abs(report["total_actual_usd"] - expected_actual) < 1e-6

    def test_perfect_accuracy_when_estimate_equals_actual(self):
        runs = self._two_runs()
        actual_sum = sum(r.actual_usd for r in runs)
        report = reconcile_runs(runs, estimated_total_usd=actual_sum)
        assert abs(report["aggregate_accuracy_pct"] - 100.0) < 0.01
        assert abs(report["aggregate_variance_usd"]) < 1e-8

    def test_variance_is_estimate_minus_actual(self):
        runs = self._two_runs()
        actual_sum = sum(r.actual_usd for r in runs)
        estimated = actual_sum + 1.00   # over-estimate by $1
        report = reconcile_runs(runs, estimated_total_usd=estimated)
        assert abs(report["aggregate_variance_usd"] - 1.00) < 1e-6

    def test_accuracy_below_100_when_off(self):
        runs = self._two_runs()
        actual_sum = sum(r.actual_usd for r in runs)
        estimated = actual_sum * 1.10   # 10 % over-estimate
        report = reconcile_runs(runs, estimated_total_usd=estimated)
        assert report["aggregate_accuracy_pct"] < 100.0

    def test_run_count(self):
        runs = self._two_runs()
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        assert report["run_count"] == 2


# ---------------------------------------------------------------------------
# reconcile_runs — empty / zero-data edge cases
# ---------------------------------------------------------------------------

class TestReconcileRunsEmpty:
    def test_no_runs_zero_estimated(self):
        report = reconcile_runs([], estimated_total_usd=0.0)
        assert report["run_count"] == 0
        assert report["total_actual_usd"] == 0.0
        assert report["aggregate_accuracy_pct"] == 100.0
        assert report["anomaly_count"] == 0

    def test_no_runs_nonzero_estimated(self):
        # Estimated > 0 but no recorded runs → accuracy 0 %
        report = reconcile_runs([], estimated_total_usd=10.0)
        assert report["aggregate_accuracy_pct"] == 0.0

    def test_zero_estimated_nonzero_actual(self):
        runs = [_make_run(input_tok=10_000, output_tok=1_000)]
        actual = runs[0].actual_usd
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        # variance is 0 - actual (negative)
        assert report["aggregate_variance_usd"] < 0
        assert report["aggregate_accuracy_pct"] < 100.0


# ---------------------------------------------------------------------------
# reconcile_runs — by_role and by_discussion attribution
# ---------------------------------------------------------------------------

class TestReconcileAttribution:
    def test_by_role_aggregation(self):
        runs = [
            _make_run("r1", role="executor", discussion=1, input_tok=10_000, output_tok=500),
            _make_run("r2", role="executor", discussion=2, input_tok=5_000, output_tok=200),
            _make_run("r3", role="code-reviewer", discussion=2, input_tok=3_000, output_tok=100),
        ]
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        assert "executor" in report["by_role"]
        assert "code-reviewer" in report["by_role"]
        assert report["by_role"]["executor"]["run_count"] == 2
        assert report["by_role"]["code-reviewer"]["run_count"] == 1

    def test_by_discussion_aggregation(self):
        runs = [
            _make_run("r1", discussion=42, input_tok=10_000, output_tok=500),
            _make_run("r2", discussion=42, input_tok=5_000, output_tok=200),
            _make_run("r3", discussion=99, input_tok=3_000, output_tok=100),
        ]
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        assert "42" in report["by_discussion"]
        assert "99" in report["by_discussion"]
        assert report["by_discussion"]["42"]["run_count"] == 2

    def test_no_discussion_run_excluded_from_by_discussion(self):
        runs = [_make_run("r1", discussion=None)]
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        assert report["by_discussion"] == {}


# ---------------------------------------------------------------------------
# reconcile_runs — anomaly flagging
# ---------------------------------------------------------------------------

class TestAnomalyFlagging:
    def test_aggregate_anomaly_when_deviation_exceeds_threshold(self):
        runs = [_make_run(input_tok=10_000, output_tok=1_000)]
        actual = runs[0].actual_usd
        # Estimate 20 % above actual → should trigger aggregate anomaly
        estimated = actual * (1 + _ANOMALY_THRESHOLD * 4)
        report = reconcile_runs(runs, estimated_total_usd=estimated)
        # Aggregate anomaly should be flagged
        assert report["anomaly_count"] >= 1
        reasons = [a["reason"] for a in report["anomalies"]]
        assert any("aggregate estimate vs actual" in r for r in reasons)

    def test_no_anomaly_within_threshold(self):
        runs = [_make_run(input_tok=10_000, output_tok=1_000)]
        actual = runs[0].actual_usd
        # Estimate exactly matches actual → no anomaly
        report = reconcile_runs(runs, estimated_total_usd=actual)
        assert report["anomaly_count"] == 0

    def test_per_run_outlier_flagged(self):
        # One cheap run, one very expensive run (>50% above mean)
        cheap = _make_run("cheap", input_tok=100, output_tok=10)
        expensive = _make_run("expensive", input_tok=500_000, output_tok=100_000)
        report = reconcile_runs([cheap, expensive], estimated_total_usd=0.0)
        # The expensive run should be flagged as an outlier
        outlier_anomalies = [
            a for a in report["anomalies"]
            if a.get("agent_id") == "expensive"
        ]
        assert len(outlier_anomalies) >= 1

    def test_single_run_no_outlier_anomaly(self):
        # With only one run, mean == that run's cost → no outlier flag
        runs = [_make_run(input_tok=50_000, output_tok=10_000)]
        actual = runs[0].actual_usd
        report = reconcile_runs(runs, estimated_total_usd=actual)
        assert report["anomaly_count"] == 0


# ---------------------------------------------------------------------------
# reconcile_runs — billing regime breakdown
# ---------------------------------------------------------------------------

class TestBillingRegimeBreakdown:
    def test_subscription_runs_counted(self):
        runs = [
            _make_run("s1", start_ts=_ts(regime="subscription")),
            _make_run("s2", start_ts=_ts(regime="subscription")),
        ]
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        regime = report["regime_summary"]
        assert regime["subscription"]["run_count"] == 2
        assert regime["credit"]["run_count"] == 0

    def test_credit_runs_counted(self):
        runs = [_make_run("c1", start_ts=_ts(regime="credit"))]
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        regime = report["regime_summary"]
        assert regime["credit"]["run_count"] == 1
        assert regime["subscription"]["run_count"] == 0

    def test_mixed_regime_both_counted(self):
        runs = [
            _make_run("s1", start_ts=_ts(regime="subscription")),
            _make_run("c1", start_ts=_ts(regime="credit")),
        ]
        report = reconcile_runs(runs, estimated_total_usd=0.0)
        regime = report["regime_summary"]
        assert regime["subscription"]["run_count"] == 1
        assert regime["credit"]["run_count"] == 1

    def test_subscription_note_in_report(self):
        report = reconcile_runs([], estimated_total_usd=0.0)
        note = report["regime_summary"]["subscription"]["note"]
        assert "subscription" in note.lower() or "no charge" in note.lower()

    def test_credit_note_mentions_200(self):
        report = reconcile_runs([], estimated_total_usd=0.0)
        note = report["regime_summary"]["credit"]["note"]
        assert "200" in note


# ---------------------------------------------------------------------------
# reconcile_runs — report structure invariants
# ---------------------------------------------------------------------------

class TestReportStructure:
    def test_required_keys_present(self):
        report = reconcile_runs([], estimated_total_usd=0.0)
        required = {
            "generated_at", "run_count", "total_actual_usd", "estimated_total_usd",
            "aggregate_variance_usd", "aggregate_accuracy_pct", "anomaly_count",
            "anomalies", "by_role", "by_discussion", "regime_summary", "data_source",
        }
        assert required.issubset(report.keys())

    def test_data_source_mentions_no_billing_api(self):
        report = reconcile_runs([], estimated_total_usd=0.0)
        billing_api = report["data_source"]["billing_api"]
        assert "NOT AVAILABLE" in billing_api or "future" in billing_api.lower()

    def test_generated_at_is_iso8601(self):
        report = reconcile_runs([], estimated_total_usd=0.0)
        ts = report["generated_at"]
        # Should parse without raising
        datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# verify() — end-to-end with injected state paths
# ---------------------------------------------------------------------------

class TestVerifyEndToEnd:
    def test_verify_no_db_returns_empty_report(self, tmp_path: Path):
        """verify() with a nonexistent DB path should return zero-data report."""
        db_path = tmp_path / "nonexistent.duckdb"
        credit_file = tmp_path / "sdk_credit.json"
        credit_file.write_text(json.dumps({"initial_usd": 200.0, "used_usd": 5.0}))
        report = verify(since_days=7, db_path=db_path, credit_file=credit_file)
        assert report["run_count"] == 0
        assert report["estimated_total_usd"] == 5.0

    def test_verify_no_credit_file_defaults_zero(self, tmp_path: Path):
        """When sdk_credit.json is absent, estimated is 0."""
        db_path = tmp_path / "nonexistent.duckdb"
        credit_file = tmp_path / "no_such_file.json"
        report = verify(since_days=7, db_path=db_path, credit_file=credit_file)
        assert report["estimated_total_usd"] == 0.0
        assert report["aggregate_accuracy_pct"] == 100.0

    def test_verify_with_duckdb(self, tmp_path: Path):
        """verify() reads actual token counts from DuckDB when available."""
        pytest.importorskip("duckdb")
        import duckdb
        db_path = tmp_path / "test_stats.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""
            CREATE TABLE agent_run (
                agent_id VARCHAR PRIMARY KEY,
                role VARCHAR NOT NULL,
                discussion INTEGER,
                pr INTEGER,
                start_ts TIMESTAMPTZ NOT NULL,
                end_ts TIMESTAMPTZ,
                duration_s DOUBLE,
                verdict VARCHAR,
                model VARCHAR,
                input_tok INTEGER,
                output_tok INTEGER,
                cache_read INTEGER,
                cache_write INTEGER,
                cache_creation_tokens INTEGER,
                blocked_reason VARCHAR,
                event_id VARCHAR,
                first_write_turn INTEGER,
                total_turns INTEGER
            )
        """)
        # Insert a completed run in subscription regime
        conn.execute("""
            INSERT INTO agent_run
                (agent_id, role, discussion, start_ts, end_ts, verdict,
                 model, input_tok, output_tok, cache_read, cache_write)
            VALUES
                ('exec-1', 'executor', 42,
                 '2026-05-20 10:00:00+00', '2026-05-20 10:05:00+00',
                 'done', 'claude-sonnet-4-6', 10000, 500, 0, 0)
        """)
        conn.close()

        credit_file = tmp_path / "sdk_credit.json"
        credit_file.write_text(json.dumps({"initial_usd": 200.0, "used_usd": 0.05}))

        report = verify(since_days=365, db_path=db_path, credit_file=credit_file)
        assert report["run_count"] == 1
        expected_actual = tokens_to_usd(10000, 500, model="claude-sonnet-4-6")
        assert abs(report["total_actual_usd"] - expected_actual) < 1e-6
        assert report["regime_summary"]["subscription"]["run_count"] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_json_output_is_parseable(self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
        """CLI with --json should produce valid JSON."""
        # D#1810 round 3: use monkeypatch.setenv/delenv, not a manual
        # os.environ mutation with an unconditional pop in `finally` — the
        # manual form has no saved original, so it unconditionally strips
        # AUTONOMOUS_TEAM_STATE_DIR for the rest of the pytest session
        # instead of restoring whatever was there before this test.
        # monkeypatch always restores correctly regardless of prior state.
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "nonexistent.duckdb"))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        rc = main(["--json", "--since-days", "7"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "run_count" in data
        assert "aggregate_accuracy_pct" in data
        assert rc == 0

    def test_human_output_no_crash(self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
        """CLI in human mode should not raise."""
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "nonexistent.duckdb"))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        rc = main(["--since-days", "7"])
        captured = capsys.readouterr()
        assert "Cost Verification" in captured.out
        assert rc == 0

    def test_print_human_shows_regime(self, capsys: pytest.CaptureFixture):
        """_print_human() should include billing regime section."""
        report = reconcile_runs([], estimated_total_usd=0.0)
        _print_human(report)
        captured = capsys.readouterr()
        assert "regime" in captured.out.lower() or "subscription" in captured.out.lower()
