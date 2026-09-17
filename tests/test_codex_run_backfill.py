"""
tests/test_codex_run_backfill.py — Muse session-log join writes measured tokens.

Covers backend/codex_run_backfill.py (D#2604): a minimal parent session.jsonl
(spawn_accepted + child_session_bound + result_ready) joined to a minimal
child session.jsonl (model_completed events) lands parsed totals on the
agent_run row with routed_via=muse.
"""
from __future__ import annotations

import json

import duckdb
import pytest

from backend import codex_run_backfill as cb


def _rec(payload_type, record, recorded_at=1789580877000000):
    return {
        "record_type": "event", "recorded_at": recorded_at,
        "payload_type": payload_type,
        "payload": {"kind": "subagent_control", "record": record},
    }


def _completed(i, o):
    return {
        "record_type": "event", "payload_type": "runtime.session",
        "payload": {"kind": "run", "event": {
            "kind": "model_completed", "model": "muse-spark-1.3",
            "usage": {"input_tokens": i, "output_tokens": o}}},
    }


@pytest.fixture
def session_pair(tmp_path):
    """Parent session dir: one completed Muse spawn plus its child log."""
    parent = tmp_path / "parent"
    child = parent / "subagent" / "child-aaa"
    child.mkdir(parents=True)
    (child / "session.jsonl").write_text(
        json.dumps(_completed(100, 10)) + "\n" + json.dumps(_completed(200, 20)) + "\n")
    (parent / "session.jsonl").write_text("\n".join([
        json.dumps(_rec("subagent.control.spawn_accepted",
                        {"subagent_id": "sub-1", "role": "executor"})),
        json.dumps(_rec("subagent.control.child_session_bound",
                        {"subagent_id": "sub-1", "child_session_id": "child-aaa"})),
        json.dumps(_rec("subagent.control.result_ready",
                        {"subagent_id": "sub-1", "error_kind": None,
                         "transcript_path": str(child / "session.jsonl")},
                        recorded_at=1789580915000000)),
    ]) + "\n")
    return parent


def _rows(db):
    conn = duckdb.connect(str(db), read_only=True)
    try:
        return conn.execute(
            "SELECT agent_id, role, discussion, verdict, model, input_tok,"
            " output_tok, routed_via, duration_s FROM agent_run").fetchall()
    finally:
        conn.close()


def test_join_writes_measured_tokens(tmp_path, session_pair):
    """Spawn-result join + child model_completed sums land on the row."""
    db = tmp_path / "stats.duckdb"
    report = cb.backfill_parent_log(
        session_pair / "session.jsonl", db_path=db, discussion=2604)
    assert report["spawns"] == 1 and report["tokens_null"] == 0
    (row,) = _rows(db)
    assert row[:8] == ("muse-child-aaa", "executor", 2604, "done",
                       "muse-spark-1.3", 300, 30, "muse")
    assert row[8] == pytest.approx(38.0)


def test_missing_child_log_writes_null_tokens(tmp_path, session_pair):
    """A genuinely missing child log still writes the row, tokens NULL."""
    (session_pair / "subagent" / "child-aaa" / "session.jsonl").unlink()
    db = tmp_path / "stats.duckdb"
    assert cb.backfill_parent_log(
        session_pair / "session.jsonl", db_path=db)["tokens_null"] == 1
    (row,) = _rows(db)
    assert row[5] is None and row[6] is None and row[7] == "muse"


def test_empty_child_log_parses_to_null(tmp_path):
    (tmp_path / "empty.jsonl").write_text('{"payload_type": "other"}\n')
    assert cb.parse_child_usage(tmp_path / "empty.jsonl") == (None, None, None)
    assert cb.parse_child_usage(tmp_path / "missing.jsonl") == (None, None, None)


def test_pending_spawn_writes_no_row(tmp_path):
    """A spawn with no result_ready is still running — no start-only row."""
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "session.jsonl").write_text(json.dumps(_rec(
        "subagent.control.spawn_accepted",
        {"subagent_id": "sub-9", "role": "executor"})) + "\n")
    db = tmp_path / "stats.duckdb"
    assert cb.backfill_parent_log(parent / "session.jsonl", db_path=db)["spawns"] == 0
    assert not db.exists()


def test_rerun_is_idempotent(tmp_path, session_pair):
    """A second pass neither duplicates the row nor clobbers tokens."""
    db = tmp_path / "stats.duckdb"
    cb.backfill_parent_log(session_pair / "session.jsonl", db_path=db)
    cb.backfill_parent_log(session_pair / "session.jsonl", db_path=db)
    (row,) = _rows(db)
    assert (row[5], row[6]) == (300, 30)


def test_failed_completion_verdict(tmp_path, session_pair):
    """error_kind records verdict=fail with tokens intact."""
    lines = (session_pair / "session.jsonl").read_text().splitlines()
    rec = json.loads(lines[2])
    rec["payload"]["record"]["error_kind"] = "cancelled"
    (session_pair / "session.jsonl").write_text(
        "\n".join(lines[:2] + [json.dumps(rec)]) + "\n")
    db = tmp_path / "stats.duckdb"
    cb.backfill_parent_log(session_pair / "session.jsonl", db_path=db)
    (row,) = _rows(db)
    assert row[3] == "fail" and (row[5], row[6]) == (300, 30)
