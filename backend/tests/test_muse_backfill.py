"""Tests for backend/muse_backfill.py — token backfill from child session logs.

A Muse wait-ready carries the child's summary but not its usage. The child's
session log (wait_result.evidence_refs) records assistant_message_committed
(final text) and goal_usage_attribution (token quantities) per model call;
backfill sums them into the agent_run row via complete_run (upsert,
idempotent). Fixture lines use the observed on-disk schema (payload.event).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import backend.muse_backfill as muse_backfill  # noqa: E402


def _log(*events: dict) -> str:
    return "\n".join(json.dumps({"payload": {"event": e}}) for e in events) + "\n"


_FIXTURE = _log(
    {"kind": "assistant_message_committed", "text": "FIRST"},
    {"kind": "assistant_message_committed", "text": "SECOND"},
    {"kind": "goal_usage_attribution", "record": {"quantity": {"input_tokens": 100, "output_tokens": 10}}},
    {
        "kind": "goal_usage_attribution",
        "record": {"quantity": {"input_tokens": 50, "output_tokens": 5, "cached_tokens": 7}},
    },
    {"kind": "resource_usage_sampled", "usage": {"rss_self_bytes": 1}},
)


def test_read_child_result_sums_usage_and_last_text(tmp_path) -> None:
    log = tmp_path / "session.jsonl"
    log.write_text(_FIXTURE)
    out = muse_backfill.read_child_result(str(log))
    assert out["text"] == "SECOND"
    assert out["input_tok"] == 150
    assert out["output_tok"] == 15


def test_read_child_result_skips_malformed(tmp_path) -> None:
    log = tmp_path / "session.jsonl"
    log.write_text("not json\n" + _log({"kind": "assistant_message_committed", "text": "ONLY"}) + "{}\n")
    out = muse_backfill.read_child_result(str(log))
    assert out["text"] == "ONLY"
    assert out["input_tok"] == 0
    assert out["output_tok"] == 0


def test_read_child_result_missing_file() -> None:
    assert muse_backfill.read_child_result("/nonexistent/session.jsonl") is None


def test_backfill_tokens_updates_row(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "stats.duckdb"))
    from backend.agent_run_tracker import complete_run, start_run

    start_run(agent_id="ev-1", role="executor", event_id="ev-1")
    log = tmp_path / "session.jsonl"
    log.write_text(_FIXTURE)
    out = muse_backfill.backfill_tokens("ev-1", [str(log)])
    assert out is not None and out["input_tok"] == 150
    import duckdb

    row = duckdb.connect(str(tmp_path / "stats.duckdb"), read_only=True).execute(
        "select input_tok, output_tok from agent_run where agent_id='ev-1'"
    ).fetchone()
    assert row == (150, 15)


def test_backfill_tokens_skips_non_logs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "stats.duckdb"))
    assert muse_backfill.backfill_tokens("ev-1", ["not-a-path", "01a18517-aa5a-note-ref"]) is None


def test_backfill_tokens_never_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "stats.duckdb"))
    assert muse_backfill.backfill_tokens("", []) is None
