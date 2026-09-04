"""
Unit tests for backend/stats/agent_spend.py — the normalized agent-spend
reader that repoints cost_tracker at `agent_run` (authoritative) with
`budget/agents/` blackboard as a precedence fallback.

Covers: normalization, NULL -> 0, orphan-unmatched exclusion, precedence
(agent_run wins; blackboard only when agent_run is empty; never both),
missing-DB degradation, and `source` labelling.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.stats import agent_spend


# ---------------------------------------------------------------------------
# Fake DuckDB connection/cursor — mimics the subset of the API agent_spend uses
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self.description = [(c,) for c in columns]
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = rows
        self.closed = False

    def execute(self, query: str, params: list | None = None) -> _FakeCursor:
        return _FakeCursor(self._columns, self._rows)

    def close(self) -> None:
        self.closed = True


_AGENT_RUN_COLUMNS = [
    "agent_id", "role", "discussion", "pr", "end_ts", "model",
    "input_tok", "output_tok", "cache_read", "cache_write",
]


def _patch_connect(monkeypatch, rows: list[tuple], columns: list[str] = _AGENT_RUN_COLUMNS):
    """Patch backend.agent_run_reader._connect to return a fake connection."""
    fake_conn = _FakeConn(columns, rows)
    monkeypatch.setattr(
        "backend.agent_run_reader._connect", lambda: fake_conn
    )
    return fake_conn


def _patch_connect_raises(monkeypatch, exc: BaseException):
    def _raise():
        raise exc
    monkeypatch.setattr("backend.agent_run_reader._connect", _raise)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_agent_run_row_normalized(monkeypatch):
    """agent_run columns map to the record shape cost_tracker expects."""
    rows = [
        ("executor-14-1", "executor", 14, 55, "2026-09-01T12:00:00+00:00",
         "claude-sonnet-4-6", 1000, 200, 50, 10),
    ]
    _patch_connect(monkeypatch, rows)

    records = agent_spend.records_for_discussion(14)
    assert len(records) == 1
    r = records[0]
    assert r["agent"] == "executor"
    assert r["agent_id"] == "executor-14-1"
    assert r["input"] == 1000
    assert r["output"] == 200
    assert r["cache_read_tokens"] == 50
    assert r["cache_write_tokens"] == 10
    assert r["model"] == "claude-sonnet-4-6"
    assert r["discussion"] == 14
    assert r["pr"] == 55
    assert r["finished"] == "2026-09-01T12:00:00+00:00"
    assert r["source"] == "agent_run"


def test_null_numerics_become_zero(monkeypatch):
    """NULL input/output/cache columns normalize to 0, not None."""
    rows = [
        ("executor-14-1", "executor", 14, None, None, None, None, None, None, None),
    ]
    _patch_connect(monkeypatch, rows)

    records = agent_spend.records_for_discussion(14)
    assert len(records) == 1
    r = records[0]
    assert r["input"] == 0
    assert r["output"] == 0
    assert r["cache_read_tokens"] == 0
    assert r["cache_write_tokens"] == 0
    assert r["model"] == "default"


# ---------------------------------------------------------------------------
# orphan-unmatched exclusion
# ---------------------------------------------------------------------------


def test_orphan_unmatched_excluded(monkeypatch):
    """Rows with role='orphan-unmatched' have no discussion/PR and are excluded."""
    rows = [
        ("orphan-1", "orphan-unmatched", None, None, None, "default", 500, 100, 0, 0),
    ]
    fake_conn = _patch_connect(monkeypatch, rows)

    records = agent_spend.all_records(bb=MagicMock(list_keys=MagicMock(return_value=[])))
    assert records == []
    assert fake_conn.closed


# ---------------------------------------------------------------------------
# Precedence — agent_run wins; blackboard only when agent_run is empty
# ---------------------------------------------------------------------------


def test_agent_run_precedence_over_blackboard_same_discussion(monkeypatch):
    """When agent_run has rows for a discussion, blackboard rows for the same
    discussion must not appear (precedence, never union)."""
    rows = [
        ("executor-14-1", "executor", 14, None, "2026-09-01T12:00:00+00:00",
         "default", 1000, 200, 0, 0),
    ]
    _patch_connect(monkeypatch, rows)

    bb = MagicMock()
    bb.list_keys.return_value = ["budget/agents/legacy-14-1"]
    bb.read.return_value = {
        "agent": "executor", "agent_id": "legacy-14-1",
        "input": 999999, "output": 999999, "model": "default", "discussion": 14,
    }

    records = agent_spend.records_for_discussion(14, bb=bb)
    assert len(records) == 1
    assert records[0]["source"] == "agent_run"
    assert records[0]["input"] == 1000  # not the blackboard's 999999


def test_blackboard_fallback_when_agent_run_empty(monkeypatch):
    """When agent_run has nothing for a discussion, the blackboard answers."""
    _patch_connect(monkeypatch, [])  # agent_run has no rows at all

    bb = MagicMock()
    bb.list_keys.return_value = ["budget/agents/legacy-99-1"]
    bb.read.return_value = {
        "agent": "executor", "agent_id": "legacy-99-1",
        "input": 5000, "output": 1000, "model": "default", "discussion": 99,
    }

    records = agent_spend.records_for_discussion(99, bb=bb)
    assert len(records) == 1
    assert records[0]["source"] == "budget_blackboard"
    assert records[0]["input"] == 5000


def test_all_records_never_unions_same_discussion(monkeypatch):
    """all_records() must not double-count a discussion present in both stores."""
    rows = [
        ("executor-14-1", "executor", 14, None, "2026-09-01T12:00:00+00:00",
         "default", 1000, 200, 0, 0),
    ]
    _patch_connect(monkeypatch, rows)

    bb = MagicMock()
    bb.list_keys.return_value = [
        "budget/agents/legacy-14-1",  # same discussion — must be dropped
        "budget/agents/legacy-1793-1",  # historical record — must survive
    ]

    def _read(key: str):
        if key == "budget/agents/legacy-14-1":
            return {"agent": "executor", "agent_id": "legacy-14-1",
                     "input": 1, "output": 1, "model": "default", "discussion": 14}
        if key == "budget/agents/legacy-1793-1":
            return {"agent": "executor", "agent_id": "legacy-1793-1",
                     "input": 2000, "output": 500, "model": "default", "discussion": 1793}
        return None

    bb.read.side_effect = _read

    records = agent_spend.all_records(bb=bb)
    discussions = {r["discussion"] for r in records}
    assert 14 in discussions
    assert 1793 in discussions
    # Exactly one record for discussion 14 (agent_run's), not two.
    d14_records = [r for r in records if r["discussion"] == 14]
    assert len(d14_records) == 1
    assert d14_records[0]["source"] == "agent_run"
    d1793_records = [r for r in records if r["discussion"] == 1793]
    assert d1793_records[0]["source"] == "budget_blackboard"


# ---------------------------------------------------------------------------
# Missing-DB degradation
# ---------------------------------------------------------------------------


def test_missing_db_degrades_to_blackboard_only(monkeypatch, capsys):
    """A missing stats.duckdb must not raise — it degrades to [] and logs,
    falling through to the blackboard fallback."""
    _patch_connect_raises(monkeypatch, FileNotFoundError("stats.duckdb not found at /nowhere"))

    bb = MagicMock()
    bb.list_keys.return_value = ["budget/agents/a1"]
    bb.read.return_value = {
        "agent": "executor", "agent_id": "a1",
        "input": 100, "output": 50, "model": "default", "discussion": 5,
    }

    records = agent_spend.records_for_discussion(5, bb=bb)
    assert len(records) == 1
    assert records[0]["source"] == "budget_blackboard"

    captured = capsys.readouterr()
    assert "agent_run" in captured.err
    assert "stats.duckdb" in captured.err


def test_missing_db_and_empty_blackboard_returns_empty(monkeypatch):
    """Both sources empty -> [] , never a raise."""
    _patch_connect_raises(monkeypatch, FileNotFoundError("stats.duckdb not found"))

    bb = MagicMock()
    bb.list_keys.return_value = []

    records = agent_spend.records_for_discussion(2248, bb=bb)
    assert records == []


def test_unsandboxed_pytest_guard_degrades_gracefully(monkeypatch):
    """A RuntimeError from the state_paths pytest guard must also degrade to
    [] rather than propagate — the reader is non-fatal by construction."""
    _patch_connect_raises(monkeypatch, RuntimeError("AUTONOMOUS_TEAM_STATE_DIR is unset under pytest"))

    bb = MagicMock()
    bb.list_keys.return_value = []

    records = agent_spend.all_records(bb=bb)
    assert records == []


# ---------------------------------------------------------------------------
# records_for_pr
# ---------------------------------------------------------------------------


def test_records_for_pr_agent_run_exact_match(monkeypatch):
    """records_for_pr filters agent_run by the real pr column, no substring
    matching needed when agent_run has the row."""
    rows = [
        ("code-reviewer-14-1", "code-reviewer", 14, 2252, "2026-09-01T12:00:00+00:00",
         "default", 7000, 400, 0, 0),
    ]
    _patch_connect(monkeypatch, rows)

    records = agent_spend.records_for_pr(2252)
    assert len(records) == 1
    assert records[0]["pr"] == 2252
    assert records[0]["source"] == "agent_run"


def test_records_for_pr_blackboard_fallback_by_agent_id_substring(monkeypatch):
    """When agent_run has nothing, blackboard fallback matches by PR number
    appearing in agent_id (legacy records predating the `pr` field)."""
    _patch_connect(monkeypatch, [])

    bb = MagicMock()

    def _read(key: str):
        if key == "quality/385":
            return None
        if key == "budget/agents/executor-385-abc":
            return {"agent": "executor", "agent_id": "executor-385-abc",
                     "input": 38000, "output": 2800, "model": "default", "discussion": 368}
        return None

    bb.read.side_effect = _read
    bb.list_keys.return_value = ["budget/agents/executor-385-abc"]

    records = agent_spend.records_for_pr(385, bb=bb)
    assert len(records) == 1
    assert records[0]["source"] == "budget_blackboard"
    assert records[0]["agent_id"] == "executor-385-abc"
