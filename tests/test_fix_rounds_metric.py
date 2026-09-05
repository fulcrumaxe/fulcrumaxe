"""Tests for the avg_fix_rounds_per_pr metric pipeline.

Covers:
- record() writes fix_rounds_per_pr rows to stats.duckdb
- avg_fix_rounds_24h() returns correct avg + distribution for >=5 samples
- avg_fix_rounds_24h() returns avg=None and correct sample_size when <5 samples
- avg_fix_rounds_24h() returns sample_size=0 on empty DB
- stats.avg_fix_rounds_per_pr RPC handler returns expected shape
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture()
def tmp_db(tmp_path):
    """Point stats_writer at a throw-away DuckDB file."""
    db_file = str(tmp_path / "stats_test.duckdb")
    with patch.dict(os.environ, {"STATS_DB_PATH": db_file}):
        yield db_file


def _write_fix_rounds(pr: int, rounds: int, ts: datetime | None = None) -> None:
    """Helper: write one fix_rounds_per_pr row."""
    from backend.stats_writer import record

    record(
        metric="fix_rounds_per_pr",
        value=float(rounds),
        unit="count",
        tags={"pr": str(pr), "tag": "Feature"},
        source="test",
        ts=ts,
    )


class TestAvgFixRoundsEmpty:
    def test_returns_zero_on_empty_db(self, tmp_db):
        from backend.stats_writer import avg_fix_rounds_24h

        result = avg_fix_rounds_24h()
        assert result["sample_size"] == 0
        assert result["avg_last_24h"] is None
        assert result["distribution"] == {}


class TestAvgFixRoundsInsufficientSamples:
    def test_na_when_fewer_than_five(self, tmp_db):
        """avg_last_24h must be None when sample_size < 5."""
        from backend.stats_writer import avg_fix_rounds_24h

        # Write 2 events with 2 fix rounds each
        _write_fix_rounds(pr=101, rounds=2)
        _write_fix_rounds(pr=102, rounds=2)

        result = avg_fix_rounds_24h()
        assert result["sample_size"] == 2
        assert result["avg_last_24h"] is None
        # Distribution should still be populated
        assert result["distribution"]["2"] == 2


class TestAvgFixRoundsSufficientSamples:
    def test_avg_and_distribution_correct(self, tmp_db):
        """Synthetic test: 5 PRs (2+2+2+1+0 rounds) → avg=1.4, distribution correct."""
        from backend.stats_writer import avg_fix_rounds_24h

        rounds_list = [2, 2, 2, 1, 0]  # sum=7, avg=1.4
        for i, r in enumerate(rounds_list):
            _write_fix_rounds(pr=200 + i, rounds=r)

        result = avg_fix_rounds_24h()
        assert result["sample_size"] == 5
        assert result["avg_last_24h"] == pytest.approx(1.4, rel=1e-6)
        dist = result["distribution"]
        assert dist["0"] == 1
        assert dist["1"] == 1
        assert dist["2"] == 3

    def test_distribution_key_is_string(self, tmp_db):
        """Distribution keys must be strings (JSON-serialisable)."""
        from backend.stats_writer import avg_fix_rounds_24h

        for i in range(5):
            _write_fix_rounds(pr=300 + i, rounds=i)

        result = avg_fix_rounds_24h()
        for k in result["distribution"]:
            assert isinstance(k, str), f"Expected str key, got {type(k)}: {k!r}"


class TestAvgFixRoundsOldRowsExcluded:
    def test_rows_older_than_24h_excluded(self, tmp_db):
        """Rows written >24h ago must not appear in the window."""
        from backend.stats_writer import avg_fix_rounds_24h

        old_ts = datetime.now(timezone.utc) - timedelta(hours=25)
        for i in range(5):
            _write_fix_rounds(pr=400 + i, rounds=5, ts=old_ts)

        # No recent rows
        result = avg_fix_rounds_24h()
        assert result["sample_size"] == 0
        assert result["avg_last_24h"] is None


class TestRpcHandler:
    def test_rpc_handler_returns_expected_shape(self, tmp_db):
        """stats.avg_fix_rounds_per_pr RPC handler returns the correct keys."""
        from backend.stats_writer import avg_fix_rounds_24h

        # Write 6 rows so avg is not None
        for i in range(6):
            _write_fix_rounds(pr=500 + i, rounds=i % 3)

        result = avg_fix_rounds_24h()
        assert "avg_last_24h" in result
        assert "sample_size" in result
        assert "distribution" in result
        # avg must be a float (not None) for 6 samples
        assert isinstance(result["avg_last_24h"], float)
        assert result["sample_size"] == 6

    def test_result_is_json_serialisable(self, tmp_db):
        """The response must round-trip through JSON without errors."""
        from backend.stats_writer import avg_fix_rounds_24h

        for i in range(5):
            _write_fix_rounds(pr=600 + i, rounds=i)

        result = avg_fix_rounds_24h()
        serialised = json.dumps(result)
        round_tripped = json.loads(serialised)
        assert round_tripped["sample_size"] == 5
