"""test_classifier_counts.py — tests for backend/stats/classifier_counts.py.

Tests:
  - Returns empty list when retros file does not exist
  - Counts classifiers within 24h window
  - Excludes entries outside the window
  - Returns at most N entries (top-N)
  - Empty classifier field is ignored
  - Returns [] when all entries have empty classifier
  - pct sums to ~100
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.stats.classifier_counts import top_classifiers


def _write_retros(tmp_path: Path, entries: list[dict]) -> Path:
    """Write JSONL retros file and return its path."""
    p = tmp_path / "agent-retros.jsonl"
    with p.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return p


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _old() -> str:
    """Timestamp 48 hours ago — outside the 24h window."""
    ts = datetime.now(timezone.utc) - timedelta(hours=48)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestTopClassifiers:
    def test_missing_file_returns_empty(self, tmp_path):
        result = top_classifiers(retros_file=tmp_path / "no-such-file.jsonl")
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path):
        p = _write_retros(tmp_path, [])
        assert top_classifiers(retros_file=p) == []

    def test_counts_within_window(self, tmp_path):
        entries = [
            {"ts": _now(), "classifier": "tool_output_ignored"},
            {"ts": _now(), "classifier": "tool_output_ignored"},
            {"ts": _now(), "classifier": "bash_retry"},
        ]
        p = _write_retros(tmp_path, entries)
        result = top_classifiers(retros_file=p)
        assert len(result) == 2
        assert result[0]["classifier"] == "tool_output_ignored"
        assert result[0]["count_24h"] == 2
        assert result[1]["classifier"] == "bash_retry"
        assert result[1]["count_24h"] == 1

    def test_excludes_old_entries(self, tmp_path):
        entries = [
            {"ts": _old(), "classifier": "old_classifier"},
            {"ts": _now(), "classifier": "new_classifier"},
        ]
        p = _write_retros(tmp_path, entries)
        result = top_classifiers(retros_file=p)
        assert len(result) == 1
        assert result[0]["classifier"] == "new_classifier"

    def test_top_n_limit(self, tmp_path):
        entries = [
            {"ts": _now(), "classifier": f"clf_{i}"}
            for i in range(10)
        ]
        p = _write_retros(tmp_path, entries)
        result = top_classifiers(n=3, retros_file=p)
        assert len(result) == 3

    def test_empty_classifier_ignored(self, tmp_path):
        entries = [
            {"ts": _now(), "classifier": ""},
            {"ts": _now(), "classifier": "valid"},
        ]
        p = _write_retros(tmp_path, entries)
        result = top_classifiers(retros_file=p)
        assert len(result) == 1
        assert result[0]["classifier"] == "valid"

    def test_missing_classifier_key_ignored(self, tmp_path):
        entries = [
            {"ts": _now()},  # no classifier key
            {"ts": _now(), "classifier": "present"},
        ]
        p = _write_retros(tmp_path, entries)
        result = top_classifiers(retros_file=p)
        assert len(result) == 1

    def test_pct_sums_to_100(self, tmp_path):
        entries = [
            {"ts": _now(), "classifier": "a"},
            {"ts": _now(), "classifier": "a"},
            {"ts": _now(), "classifier": "b"},
        ]
        p = _write_retros(tmp_path, entries)
        result = top_classifiers(retros_file=p)
        total_pct = sum(r["pct"] for r in result)
        assert abs(total_pct - 100.0) < 1.0  # floating point tolerance

    def test_result_keys(self, tmp_path):
        entries = [{"ts": _now(), "classifier": "x"}]
        p = _write_retros(tmp_path, entries)
        result = top_classifiers(retros_file=p)
        assert len(result) == 1
        assert set(result[0].keys()) == {"classifier", "count_24h", "pct"}
