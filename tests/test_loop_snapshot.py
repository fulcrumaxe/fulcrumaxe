"""Tests for backend/loop_snapshot.py — staleness detection and loading."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure backend is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from loop_snapshot import SnapshotStale, age_seconds, load


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_snapshot(tmp_path: Path, generated_at: str, extra: dict | None = None) -> Path:
    data = {"generated_at": generated_at, "schema_version": "1.0.0"}
    if extra:
        data.update(extra)
    p = tmp_path / "loop-snapshot.json"
    p.write_text(json.dumps(data))
    return p


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# age_seconds
# ---------------------------------------------------------------------------

class TestAgeSeconds:
    def test_fresh_snapshot(self):
        now = datetime.now(timezone.utc)
        snap = {"generated_at": _iso(now)}
        age = age_seconds(snap)
        assert 0 <= age < 5  # should be nearly zero

    def test_old_snapshot(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=15)
        snap = {"generated_at": _iso(old)}
        age = age_seconds(snap)
        assert age > 800  # > 13 minutes in seconds

    def test_missing_generated_at_raises(self):
        with pytest.raises(SnapshotStale, match="missing 'generated_at'"):
            age_seconds({})

    def test_bad_generated_at_raises(self):
        with pytest.raises(SnapshotStale, match="Cannot parse"):
            age_seconds({"generated_at": "not-a-date"})


# ---------------------------------------------------------------------------
# load — file-not-found
# ---------------------------------------------------------------------------

class TestLoadMissingFile:
    def test_missing_file_raises_stale(self, tmp_path):
        missing = str(tmp_path / "nonexistent.json")
        with pytest.raises(SnapshotStale, match="not found"):
            load(path=missing)


# ---------------------------------------------------------------------------
# load — missing generated_at
# ---------------------------------------------------------------------------

class TestLoadMissingGeneratedAt:
    def test_raises_when_generated_at_absent(self, tmp_path):
        p = tmp_path / "snap.json"
        p.write_text(json.dumps({"schema_version": "1.0.0", "data": "something"}))
        with pytest.raises(SnapshotStale, match="missing 'generated_at'"):
            load(path=str(p))


# ---------------------------------------------------------------------------
# load — stale file (> max_age)
# ---------------------------------------------------------------------------

class TestLoadStaleFile:
    def test_11_min_old_raises_with_600s_max(self, tmp_path):
        eleven_min_ago = datetime.now(timezone.utc) - timedelta(minutes=11)
        p = _write_snapshot(tmp_path, _iso(eleven_min_ago))
        with pytest.raises(SnapshotStale, match=r"\d+s old"):
            load(path=str(p), max_age_seconds=600)

    def test_just_over_max_raises(self, tmp_path):
        over = datetime.now(timezone.utc) - timedelta(seconds=61)
        p = _write_snapshot(tmp_path, _iso(over))
        with pytest.raises(SnapshotStale):
            load(path=str(p), max_age_seconds=60)


# ---------------------------------------------------------------------------
# load — fresh file returns dict
# ---------------------------------------------------------------------------

class TestLoadFresh:
    def test_fresh_returns_dict(self, tmp_path):
        now = datetime.now(timezone.utc)
        p = _write_snapshot(tmp_path, _iso(now), extra={"discussions": [], "blackboard": {}})
        result = load(path=str(p), max_age_seconds=600)
        assert isinstance(result, dict)
        assert result["schema_version"] == "1.0.0"
        assert "discussions" in result

    def test_fresh_exactly_at_boundary_passes(self, tmp_path):
        # 1 second under max — should pass
        almost_stale = datetime.now(timezone.utc) - timedelta(seconds=59)
        p = _write_snapshot(tmp_path, _iso(almost_stale))
        result = load(path=str(p), max_age_seconds=60)
        assert isinstance(result, dict)
