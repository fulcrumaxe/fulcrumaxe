"""Tests for stats.team_lead_tokens RPC handler and the stats_writer helper.

Covers:
- team_lead_tokens_percentiles() returns N/A shape when sample_size < 5
- Returns correct avg/p50/p95 with sufficient data
- RPC handler wires through correctly
- Missing DB (no iterations yet) returns zeros gracefully
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure backend is importable
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_with_rows(tmp_path: Path, rows: list[int]) -> Path:
    """Create a stats.duckdb with loop_metrics rows containing the given token counts."""
    import duckdb

    db = tmp_path / "stats.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_metrics (
            ts                          TIMESTAMP PRIMARY KEY,
            duration_s                  DOUBLE    NOT NULL DEFAULT 0,
            team_lead_input_tokens      BIGINT    NOT NULL DEFAULT 0,
            team_lead_output_tokens     BIGINT    NOT NULL DEFAULT 0,
            team_lead_cache_read        BIGINT    NOT NULL DEFAULT 0,
            team_lead_cache_write       BIGINT    NOT NULL DEFAULT 0,
            team_lead_tokens_per_iter   BIGINT    NOT NULL DEFAULT 0
        )
    """)
    now = datetime.now(timezone.utc)
    for i, tokens in enumerate(rows):
        ts_str = now.strftime(f"%Y-%m-%d %H:%M:{i:02d}.000")
        conn.execute(
            "INSERT OR IGNORE INTO loop_metrics VALUES (CAST(? AS TIMESTAMP), ?, ?, ?, ?, ?, ?)",
            [ts_str, 1.0, tokens, 0, 0, 0, tokens],
        )
    conn.close()
    return db


# ---------------------------------------------------------------------------
# stats_writer.team_lead_tokens_percentiles tests
# ---------------------------------------------------------------------------

class TestTeamLeadTokensPercentiles:
    def test_no_db_returns_zeros(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "nonexistent.duckdb"))
        import importlib
        import stats_writer
        importlib.reload(stats_writer)
        result = stats_writer.team_lead_tokens_percentiles()
        assert result["avg"] is None
        assert result["p50"] is None
        assert result["p95"] is None
        assert result["sample_size"] == 0

    def test_less_than_five_rows_returns_na(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _make_db_with_rows(tmp_path, [100, 200, 300])  # only 3 rows
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        import importlib
        import stats_writer
        importlib.reload(stats_writer)
        result = stats_writer.team_lead_tokens_percentiles()
        assert result["avg"] is None
        assert result["p50"] is None
        assert result["p95"] is None
        assert result["sample_size"] == 3

    def test_five_or_more_rows_returns_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tokens = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        db = _make_db_with_rows(tmp_path, tokens)
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        import importlib
        import stats_writer
        importlib.reload(stats_writer)
        result = stats_writer.team_lead_tokens_percentiles()
        assert result["sample_size"] == 10
        assert result["avg"] is not None
        assert result["p50"] is not None
        assert result["p95"] is not None
        # avg of [100..1000] step 100 = 550
        assert abs(result["avg"] - 550.0) < 1.0
        # p50 of 10 uniform integers — median between 500 and 600
        assert 500 <= result["p50"] <= 600
        # p95 should be near the top
        assert result["p95"] >= 900

    def test_exactly_five_rows_not_na(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _make_db_with_rows(tmp_path, [10, 20, 30, 40, 50])
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        import importlib
        import stats_writer
        importlib.reload(stats_writer)
        result = stats_writer.team_lead_tokens_percentiles()
        assert result["sample_size"] == 5
        # Must return real values at the boundary
        assert result["avg"] is not None

    def test_four_rows_still_na(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _make_db_with_rows(tmp_path, [10, 20, 30, 40])
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        import importlib
        import stats_writer
        importlib.reload(stats_writer)
        result = stats_writer.team_lead_tokens_percentiles()
        assert result["sample_size"] == 4
        assert result["avg"] is None


# ---------------------------------------------------------------------------
# RPC handler wiring test
# ---------------------------------------------------------------------------

class TestTeamLeadTokensRpc:
    """Verify the stats.team_lead_tokens RPC method is registered and callable."""

    def test_rpc_method_registered(self) -> None:
        import server
        assert "stats.team_lead_tokens" in server._RPC_METHODS

    def test_rpc_returns_expected_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "nonexistent.duckdb"))
        import importlib
        import stats_writer
        importlib.reload(stats_writer)
        import server
        handler = server._RPC_METHODS["stats.team_lead_tokens"]
        result = handler({})
        assert "avg" in result
        assert "p50" in result
        assert "p95" in result
        assert "sample_size" in result

    def test_rpc_respects_since_hours_param(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "nonexistent.duckdb"))
        import importlib
        import stats_writer
        importlib.reload(stats_writer)
        import server
        handler = server._RPC_METHODS["stats.team_lead_tokens"]
        result = handler({"since_hours": 48})
        # Empty DB -> zeros regardless of window
        assert result["sample_size"] == 0
