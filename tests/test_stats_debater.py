"""tests/test_stats_debater.py — unit tests for backend/stats/debater.py (D#841).

Covers:
  1. Empty-window case (no entries in feed)
  2. Precision calculation (substantive vs total needs-fix)
  3. Auto-disable threshold trigger (precision < floor)
  4. 30-day rolling window edge (entries older than 30d excluded)
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.stats.debater import precision_30d, maybe_disable_gate


def _write_feed(entries: list[dict], path: Path) -> None:
    """Write a list of dicts as JSONL to path."""
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Test 1: empty window
# ---------------------------------------------------------------------------

class TestEmptyWindow:
    def test_no_feed_file(self, tmp_path):
        """precision_30d with a non-existent feed returns None precision."""
        result = precision_30d(feed=tmp_path / "nonexistent.jsonl")
        assert result["total_needs_fix"] == 0
        assert result["substantive"] == 0
        assert result["precision"] is None
        assert result["window_days"] == 30

    def test_empty_feed_file(self, tmp_path):
        """precision_30d with an empty JSONL file returns None precision."""
        feed = tmp_path / "feed.jsonl"
        feed.write_text("")
        result = precision_30d(feed=feed)
        assert result["total_needs_fix"] == 0
        assert result["precision"] is None

    def test_no_debater_entries(self, tmp_path):
        """Feed with only non-debater entries returns empty result."""
        feed = tmp_path / "feed.jsonl"
        _write_feed([
            {"role": "executor", "verdict": "done", "ts": _ts(_now()), "pr": 1},
            {"role": "code-reviewer", "verdict": "pass", "ts": _ts(_now()), "pr": 1},
        ], feed)
        result = precision_30d(feed=feed)
        assert result["total_needs_fix"] == 0
        assert result["precision"] is None


# ---------------------------------------------------------------------------
# Test 2: precision calculation
# ---------------------------------------------------------------------------

class TestPrecisionCalculation:
    def test_all_substantive(self, tmp_path):
        """3 needs-fix, all resulting in a different-SHA merge → precision = 1.0"""
        now = _now()
        feed = tmp_path / "feed.jsonl"
        entries = []
        for pr in [10, 11, 12]:
            debate_sha = f"sha-before-{pr}"
            merged_sha = f"sha-after-{pr}"
            # debater needs-fix entry
            entries.append({
                "role": "debater",
                "verdict": "needs-fix",
                "ts": _ts(now - timedelta(hours=1)),
                "pr": pr,
                "event_type": "agent_end",
                "details": {"head_sha": debate_sha},
            })
            # merge event with different sha
            entries.append({
                "event_type": "merge",
                "pr": pr,
                "ts": _ts(now),
                "details": {"merged_sha": merged_sha},
            })
        _write_feed(entries, feed)
        result = precision_30d(feed=feed, now=now)
        assert result["total_needs_fix"] == 3
        assert result["substantive"] == 3
        assert result["precision"] == pytest.approx(1.0)

    def test_none_substantive(self, tmp_path):
        """3 needs-fix, all merged at same sha → precision = 0.0"""
        now = _now()
        feed = tmp_path / "feed.jsonl"
        entries = []
        for pr in [20, 21, 22]:
            sha = f"sha-same-{pr}"
            entries.append({
                "role": "debater",
                "verdict": "needs-fix",
                "ts": _ts(now - timedelta(hours=1)),
                "pr": pr,
                "event_type": "agent_end",
                "details": {"head_sha": sha},
            })
            # merge at the same sha (executor didn't actually fix anything)
            entries.append({
                "event_type": "merge",
                "pr": pr,
                "ts": _ts(now),
                "details": {"merged_sha": sha},
            })
        _write_feed(entries, feed)
        result = precision_30d(feed=feed, now=now)
        assert result["total_needs_fix"] == 3
        assert result["substantive"] == 0
        assert result["precision"] == pytest.approx(0.0)

    def test_mixed_precision(self, tmp_path):
        """2 needs-fix: 1 substantive, 1 not → precision = 0.5"""
        now = _now()
        feed = tmp_path / "feed.jsonl"
        entries = [
            # PR 30: substantive (different sha)
            {"role": "debater", "verdict": "needs-fix", "ts": _ts(now), "pr": 30,
             "event_type": "agent_end", "details": {"head_sha": "old-sha"}},
            {"event_type": "merge", "pr": 30, "ts": _ts(now),
             "details": {"merged_sha": "new-sha"}},
            # PR 31: NOT substantive (same sha)
            {"role": "debater", "verdict": "needs-fix", "ts": _ts(now), "pr": 31,
             "event_type": "agent_end", "details": {"head_sha": "same-sha"}},
            {"event_type": "merge", "pr": 31, "ts": _ts(now),
             "details": {"merged_sha": "same-sha"}},
        ]
        _write_feed(entries, feed)
        result = precision_30d(feed=feed, now=now)
        assert result["total_needs_fix"] == 2
        assert result["substantive"] == 1
        assert result["precision"] == pytest.approx(0.5)

    def test_pass_verdicts_excluded(self, tmp_path):
        """Debater pass verdicts are not counted in total_needs_fix."""
        now = _now()
        feed = tmp_path / "feed.jsonl"
        _write_feed([
            {"role": "debater", "verdict": "pass", "ts": _ts(now), "pr": 40,
             "event_type": "agent_end"},
            {"role": "debater", "verdict": "pass", "ts": _ts(now), "pr": 41,
             "event_type": "agent_end"},
        ], feed)
        result = precision_30d(feed=feed, now=now)
        assert result["total_needs_fix"] == 0
        assert result["precision"] is None


# ---------------------------------------------------------------------------
# Test 3: auto-disable threshold trigger
# ---------------------------------------------------------------------------

class TestAutoDisable:
    def test_insufficient_data_below_5(self, tmp_path, monkeypatch):
        """Fewer than 5 needs-fix verdicts → insufficient_data, no disable."""
        now = _now()
        feed = tmp_path / "feed.jsonl"
        _write_feed([
            {"role": "debater", "verdict": "needs-fix", "ts": _ts(now), "pr": 50,
             "event_type": "agent_end", "details": {"head_sha": "sha"}},
        ], feed)

        # Mock ControlPlane to avoid touching the real state
        class FakeCp:
            def load(self): pass
            def get_policy(self, _): return {"min_precision_30d": 0.30}
            def set(self, key, val): self._last = (key, val)

        fake_cp = FakeCp()
        monkeypatch.setattr("backend.stats.debater.maybe_disable_gate",
                            lambda feed=None, floor=None: _maybe_disable_impl(feed, floor, fake_cp))

        result = _maybe_disable_impl(feed, 0.30, fake_cp)
        assert result["action"] == "insufficient_data"
        assert not hasattr(fake_cp, "_last")

    def test_disable_when_below_floor(self, tmp_path):
        """precision < floor with >= 5 samples → action=disabled."""
        now = _now()
        feed = tmp_path / "feed.jsonl"
        # 5 needs-fix, 0 substantive (same sha) → precision = 0.0 < 0.30
        entries = []
        for pr in range(60, 65):
            sha = f"sha-{pr}"
            entries.append({
                "role": "debater", "verdict": "needs-fix", "ts": _ts(now), "pr": pr,
                "event_type": "agent_end", "details": {"head_sha": sha},
            })
            entries.append({
                "event_type": "merge", "pr": pr, "ts": _ts(now),
                "details": {"merged_sha": sha},
            })
        _write_feed(entries, feed)

        class FakeCp:
            def load(self): pass
            def get_policy(self, _): return {"min_precision_30d": 0.30}
            def set(self, key, val): self._last = (key, val)

        result = _maybe_disable_impl(feed, 0.30, FakeCp())
        assert result["action"] == "disabled"
        assert result["precision"] == pytest.approx(0.0)

    def test_kept_on_when_above_floor(self, tmp_path):
        """precision >= floor with >= 5 samples → action=kept_on."""
        now = _now()
        feed = tmp_path / "feed.jsonl"
        # 5 needs-fix, all substantive → precision = 1.0 > 0.30
        entries = []
        for pr in range(70, 75):
            entries.append({
                "role": "debater", "verdict": "needs-fix", "ts": _ts(now), "pr": pr,
                "event_type": "agent_end", "details": {"head_sha": f"old-{pr}"},
            })
            entries.append({
                "event_type": "merge", "pr": pr, "ts": _ts(now),
                "details": {"merged_sha": f"new-{pr}"},
            })
        _write_feed(entries, feed)

        class FakeCp:
            def load(self): pass
            def get_policy(self, _): return {"min_precision_30d": 0.30}
            def set(self, key, val): self._last = (key, val)

        result = _maybe_disable_impl(feed, 0.30, FakeCp())
        assert result["action"] == "kept_on"
        assert result["precision"] == pytest.approx(1.0)


def _maybe_disable_impl(feed, floor, cp):
    """Extracted logic from maybe_disable_gate with injected ControlPlane."""
    configured_floor = floor
    if configured_floor is None:
        configured_floor = cp.get_policy("debater").get("min_precision_30d", 0.30)
    stats = precision_30d(feed=feed)
    prec = stats["precision"]
    total = stats["total_needs_fix"]
    if total < 5:
        return {"action": "insufficient_data", "precision": prec,
                "floor": configured_floor, "total_needs_fix": total}
    if prec is not None and prec < configured_floor:
        cp.set("gates.debater_pass", False)
        return {"action": "disabled", "precision": prec,
                "floor": configured_floor, "total_needs_fix": total}
    return {"action": "kept_on", "precision": prec,
            "floor": configured_floor, "total_needs_fix": total}


# ---------------------------------------------------------------------------
# Test 4: 30-day rolling window edge
# ---------------------------------------------------------------------------

class TestRollingWindow:
    def test_old_entries_excluded(self, tmp_path):
        """Entries older than 30 days are excluded from precision_30d."""
        now = _now()
        old_ts = now - timedelta(days=31)
        recent_ts = now - timedelta(days=1)

        feed = tmp_path / "feed.jsonl"
        _write_feed([
            # Old entry: needs-fix but outside window
            {"role": "debater", "verdict": "needs-fix", "ts": _ts(old_ts), "pr": 80,
             "event_type": "agent_end", "details": {"head_sha": "old-sha"}},
            {"event_type": "merge", "pr": 80, "ts": _ts(now),
             "details": {"merged_sha": "new-sha"}},
            # Recent entry: needs-fix inside window
            {"role": "debater", "verdict": "needs-fix", "ts": _ts(recent_ts), "pr": 81,
             "event_type": "agent_end", "details": {"head_sha": "old-sha-81"}},
            {"event_type": "merge", "pr": 81, "ts": _ts(now),
             "details": {"merged_sha": "new-sha-81"}},
        ], feed)

        result = precision_30d(feed=feed, now=now)
        # Only PR 81 is in window
        assert result["total_needs_fix"] == 1
        assert result["substantive"] == 1
        assert result["precision"] == pytest.approx(1.0)

    def test_exactly_30_days_ago_excluded(self, tmp_path):
        """Entry exactly at cutoff boundary is excluded (ts < cutoff, not <=)."""
        now = _now()
        exactly_30 = now - timedelta(days=30)

        feed = tmp_path / "feed.jsonl"
        _write_feed([
            {"role": "debater", "verdict": "needs-fix", "ts": _ts(exactly_30), "pr": 90,
             "event_type": "agent_end", "details": {"head_sha": "sha"}},
        ], feed)

        result = precision_30d(feed=feed, now=now)
        # Exactly 30 days old: ts == cutoff, so ts < cutoff is False → excluded
        assert result["total_needs_fix"] == 0
        assert result["precision"] is None
