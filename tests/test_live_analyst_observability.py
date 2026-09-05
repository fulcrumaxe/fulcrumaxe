"""tests/test_live_analyst_observability.py

Tests for live-analyst observability metric emissions (Discussion #574 PR-c).

Covers:
  - record_live_analyst_intervention writes correct metric rows
  - record_intervention_outcome writes correct metric rows
  - interventions_per_agent_avg stores per-agent count at call time
  - interventions_per_classifier emits with classifier tag
  - intervention_to_self_correction_rate stores 1.0/0.0 correctly
  - Multiple interventions accumulate correctly in DuckDB
  - Daemon imports guarded — PR-c ships without PR-b merged
"""

from __future__ import annotations

import json
import importlib
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

# Import stats_writer directly from file — not as a package — so we can patch DB path
_stats_writer_path = _REPO / "backend" / "stats_writer.py"
_spec = importlib.util.spec_from_file_location("stats_writer", _stats_writer_path)
stats_writer = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(stats_writer)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Return a temp DB path and set the env var stats_writer uses."""
    db = tmp_path / "test_stats.duckdb"
    os.environ["STATS_DB_PATH"] = str(db)
    return db


def _query_all(db: Path, metric: str) -> list[dict]:
    """Fetch all rows for a given metric from the test DB."""
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    conn = duckdb.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT ts, metric, tags, value, unit, source FROM metric_event WHERE metric = ? ORDER BY ts",
            [metric],
        ).fetchall()
        return [
            {
                "ts": r[0],
                "metric": r[1],
                "tags": json.loads(r[2]) if r[2] else {},
                "value": r[3],
                "unit": r[4],
                "source": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


def _query_metric_count(db: Path, metric: str) -> int:
    return len(_query_all(db, metric))


# ---------------------------------------------------------------------------
# 1. record_live_analyst_intervention — smoke test
# ---------------------------------------------------------------------------

class TestRecordLiveAnalystIntervention:

    def test_emits_three_metric_rows(self, tmp_path):
        db = _make_db(tmp_path)
        ts = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)

        stats_writer.record_live_analyst_intervention(
            agent_id="agent-abc123",
            classifier="git_rm_usage",
            intervention_number=1,
            ts=ts,
        )

        # Three distinct metric names must exist
        assert _query_metric_count(db, "intervention_count") == 1
        assert _query_metric_count(db, "interventions_per_classifier") == 1
        assert _query_metric_count(db, "interventions_per_agent_avg") == 1

    def test_intervention_count_tags_contain_agent_and_classifier(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_live_analyst_intervention(
            agent_id="agent-xyz",
            classifier="forbidden_subagent_type",
            intervention_number=2,
        )

        rows = _query_all(db, "intervention_count")
        assert rows, "intervention_count row should exist"
        tags = rows[0]["tags"]
        assert tags.get("agent_id") == "agent-xyz"
        assert tags.get("classifier") == "forbidden_subagent_type"

    def test_interventions_per_classifier_has_classifier_tag(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_live_analyst_intervention(
            agent_id="agent-1",
            classifier="wrong_premise_retries",
            intervention_number=1,
        )

        rows = _query_all(db, "interventions_per_classifier")
        assert rows
        tags = rows[0]["tags"]
        assert tags.get("classifier") == "wrong_premise_retries"
        # value should be 1.0 (count increment)
        assert rows[0]["value"] == 1.0

    def test_interventions_per_agent_avg_stores_intervention_number(self, tmp_path):
        db = _make_db(tmp_path)
        # Third intervention for this agent
        stats_writer.record_live_analyst_intervention(
            agent_id="agent-loop",
            classifier="git_rm_usage",
            intervention_number=3,
        )

        rows = _query_all(db, "interventions_per_agent_avg")
        assert rows
        assert rows[0]["value"] == 3.0

    def test_source_is_live_analyst(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_live_analyst_intervention(
            agent_id="a1",
            classifier="git_rm_usage",
            intervention_number=1,
        )

        for metric in ("intervention_count", "interventions_per_classifier", "interventions_per_agent_avg"):
            rows = _query_all(db, metric)
            assert rows, f"No rows for {metric}"
            assert rows[0]["source"] == "live-analyst", f"Wrong source for {metric}"

    def test_unit_is_count(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_live_analyst_intervention(
            agent_id="a1",
            classifier="git_rm_usage",
            intervention_number=1,
        )

        for metric in ("intervention_count", "interventions_per_classifier", "interventions_per_agent_avg"):
            rows = _query_all(db, metric)
            assert rows[0]["unit"] == "count"


# ---------------------------------------------------------------------------
# 2. record_intervention_outcome — self_corrected true/false
# ---------------------------------------------------------------------------

class TestRecordInterventionOutcome:

    def test_self_corrected_true_stores_1(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_intervention_outcome(
            agent_id="agent-a",
            classifier="git_rm_usage",
            self_corrected=True,
        )

        rows = _query_all(db, "intervention_to_self_correction_rate")
        assert rows
        assert rows[0]["value"] == 1.0

    def test_self_corrected_false_stores_0(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_intervention_outcome(
            agent_id="agent-b",
            classifier="wrong_premise_retries",
            self_corrected=False,
        )

        rows = _query_all(db, "intervention_to_self_correction_rate")
        assert rows
        assert rows[0]["value"] == 0.0

    def test_outcome_tags_contain_agent_and_classifier(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_intervention_outcome(
            agent_id="agent-test",
            classifier="forbidden_subagent_type",
            self_corrected=True,
        )

        rows = _query_all(db, "intervention_to_self_correction_rate")
        tags = rows[0]["tags"]
        assert tags.get("agent_id") == "agent-test"
        assert tags.get("classifier") == "forbidden_subagent_type"

    def test_unit_is_ratio(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_intervention_outcome(
            agent_id="a",
            classifier="git_rm_usage",
            self_corrected=True,
        )

        rows = _query_all(db, "intervention_to_self_correction_rate")
        assert rows[0]["unit"] == "ratio"

    def test_source_is_live_analyst(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_intervention_outcome(
            agent_id="a",
            classifier="git_rm_usage",
            self_corrected=False,
        )

        rows = _query_all(db, "intervention_to_self_correction_rate")
        assert rows[0]["source"] == "live-analyst"


# ---------------------------------------------------------------------------
# 3. Multiple interventions accumulate correctly
# ---------------------------------------------------------------------------

class TestMultipleInterventions:

    def test_two_interventions_on_same_agent_accumulate(self, tmp_path):
        db = _make_db(tmp_path)
        t1 = datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 11, 10, 1, 0, tzinfo=timezone.utc)

        stats_writer.record_live_analyst_intervention(
            agent_id="agent-multi",
            classifier="git_rm_usage",
            intervention_number=1,
            ts=t1,
        )
        stats_writer.record_live_analyst_intervention(
            agent_id="agent-multi",
            classifier="wrong_premise_retries",
            intervention_number=2,
            ts=t2,
        )

        # Two distinct intervention_count rows (different classifiers = different tags = different PKs)
        assert _query_metric_count(db, "intervention_count") == 2

    def test_two_classifiers_emit_separate_per_classifier_rows(self, tmp_path):
        db = _make_db(tmp_path)
        t1 = datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 11, 10, 0, 1, tzinfo=timezone.utc)

        stats_writer.record_live_analyst_intervention(
            agent_id="a1",
            classifier="git_rm_usage",
            intervention_number=1,
            ts=t1,
        )
        stats_writer.record_live_analyst_intervention(
            agent_id="a2",
            classifier="forbidden_subagent_type",
            intervention_number=1,
            ts=t2,
        )

        rows = _query_all(db, "interventions_per_classifier")
        classifiers = {r["tags"]["classifier"] for r in rows}
        assert "git_rm_usage" in classifiers
        assert "forbidden_subagent_type" in classifiers

    def test_mixed_outcomes_stored_separately(self, tmp_path):
        db = _make_db(tmp_path)
        t1 = datetime(2026, 5, 11, 9, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 11, 9, 0, 1, tzinfo=timezone.utc)

        stats_writer.record_intervention_outcome("a1", "git_rm_usage", True, ts=t1)
        stats_writer.record_intervention_outcome("a2", "git_rm_usage", False, ts=t2)

        rows = _query_all(db, "intervention_to_self_correction_rate")
        values = {r["tags"]["agent_id"]: r["value"] for r in rows}
        assert values["a1"] == 1.0
        assert values["a2"] == 0.0


# ---------------------------------------------------------------------------
# 4. PR-b daemon import guard — PR-c must ship without PR-b
# ---------------------------------------------------------------------------

class TestDaemonImportGuard:
    """PR-c must not require the daemon (PR-b) to be present.

    If live_analyst_daemon.py doesn't exist, stats_writer still works.
    The daemon, when it eventually lands, will import from stats_writer —
    not the other way around.
    """

    def test_stats_writer_has_no_daemon_import(self):
        """stats_writer.py must not import live_analyst_daemon."""
        source = _stats_writer_path.read_text()
        assert "live_analyst_daemon" not in source, (
            "stats_writer.py must not import live_analyst_daemon — "
            "PR-c must ship independently of PR-b"
        )

    def test_record_live_analyst_intervention_callable_without_daemon(self, tmp_path):
        """Calling record_live_analyst_intervention() must not raise even if daemon is absent."""
        db = _make_db(tmp_path)
        # Just calling it should succeed — no daemon import needed
        stats_writer.record_live_analyst_intervention(
            agent_id="standalone",
            classifier="git_rm_usage",
            intervention_number=1,
        )
        assert _query_metric_count(db, "intervention_count") == 1

    def test_record_intervention_outcome_callable_without_daemon(self, tmp_path):
        db = _make_db(tmp_path)
        stats_writer.record_intervention_outcome(
            agent_id="standalone",
            classifier="git_rm_usage",
            self_corrected=True,
        )
        assert _query_metric_count(db, "intervention_to_self_correction_rate") == 1
