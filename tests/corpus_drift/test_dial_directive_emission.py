"""tests/corpus_drift/test_dial_directive_emission.py

Unit tests for the global.dial_directive_emission claim.
Uses synthetic audit.jsonl fixtures — no live STATE_DIR access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from backend.corpus_drift.claims.dial_directive_emission import evaluate, CLAIM_ID, ROLE_SCOPE


def _ts(days_ago: float) -> str:
    """ISO-8601 UTC timestamp N days before now."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat(timespec="seconds")


def _write_audit(path: Path, rows: list[dict]) -> None:
    """Write JSONL audit file."""
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


class TestDialDirectiveEmission:
    def test_healthy_enough_changes(self, tmp_path):
        """≥4 dial_change rows in window → healthy."""
        audit = tmp_path / "audit.jsonl"
        rows = [
            {"kind": "dial_change", "class": "agent.spawn", "timestamp": _ts(i)}
            for i in range(1, 6)  # 5 rows
        ]
        _write_audit(audit, rows)

        result = evaluate(runs=[], transcripts_dir=None, window_days=30, audit_path=audit)
        assert result.claim_id == CLAIM_ID
        assert result.role_scope == ROLE_SCOPE
        assert result.score == 5
        assert result.status == "healthy"

    def test_watch_below_threshold(self, tmp_path):
        """1–3 dial_change rows → watch."""
        audit = tmp_path / "audit.jsonl"
        rows = [
            {"kind": "dial_change", "class": "agent.spawn", "timestamp": _ts(1)},
            {"kind": "dial_change", "class": "merge.standard", "timestamp": _ts(2)},
        ]
        _write_audit(audit, rows)

        result = evaluate(runs=[], transcripts_dir=None, window_days=30, audit_path=audit)
        assert result.score == 2
        assert result.status == "watch"

    def test_drift_no_changes(self, tmp_path):
        """0 dial_change rows → drift."""
        audit = tmp_path / "audit.jsonl"
        # Only rejection rows, no dial_change
        rows = [
            {"kind": "dial_directive_rejected", "class": "sandbox.modify", "timestamp": _ts(1)}
        ]
        _write_audit(audit, rows)

        result = evaluate(runs=[], transcripts_dir=None, window_days=30, audit_path=audit)
        assert result.score == 0
        assert result.status == "drift"

    def test_zero_score_when_audit_missing(self, tmp_path):
        """Missing audit file returns score=0."""
        audit = tmp_path / "nonexistent.jsonl"
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, audit_path=audit)
        assert result.score == 0

    def test_ignores_rows_outside_window(self, tmp_path):
        """Rows older than window are not counted."""
        audit = tmp_path / "audit.jsonl"
        rows = [
            # 5 rows outside window (40 days ago)
            *[{"kind": "dial_change", "class": "agent.spawn", "timestamp": _ts(40)} for _ in range(5)],
            # 4 rows inside window
            *[{"kind": "dial_change", "class": "agent.spawn", "timestamp": _ts(2)} for _ in range(4)],
        ]
        _write_audit(audit, rows)

        result = evaluate(runs=[], transcripts_dir=None, window_days=30, audit_path=audit)
        assert result.score == 4
        assert result.status == "healthy"

    def test_score_type_is_count(self, tmp_path):
        """Claim uses score_type='count'."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, audit_path=audit)
        assert result.score_type == "count"

    def test_mixed_kinds_ignored(self, tmp_path):
        """Only kind='dial_change' rows are counted."""
        audit = tmp_path / "audit.jsonl"
        rows = [
            {"kind": "agent_run", "timestamp": _ts(1)},
            {"kind": "dial_directive_rejected", "timestamp": _ts(1)},
            {"kind": "dial_change", "class": "agent.spawn", "timestamp": _ts(1)},
        ]
        _write_audit(audit, rows)

        result = evaluate(runs=[], transcripts_dir=None, window_days=30, audit_path=audit)
        assert result.score == 1
