"""Tests for Team Lead token tracking — cost_tracker.summarize_team_lead()
and subscription_usage.team_lead_usage().

Acceptance criteria verified:
  AC6: Worktree JSONL dirs (sub-agents) are excluded.
  AC7: JSONL read failure degrades gracefully (returns zeros).
  AC2: summarize_team_lead() returns cost_usd_equivalent + token fields.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure backend is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.subscription_usage import team_lead_usage, _TEAM_LEAD_PROJECT_DIR_NAME
from backend.cost_tracker import CostTracker
from backend.blackboard import Blackboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(dt: datetime) -> str:
    """Format a datetime as ISO 8601 with Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry(dt: datetime, inp: int = 0, out: int = 0, cache_read: int = 0, cache_write: int = 0) -> str:
    """Build a Claude Code JSONL entry line (message.usage shape)."""
    return json.dumps({
        "timestamp": _ts(dt),
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    })


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def projects_dir(tmp_path: Path) -> Path:
    """Create a mock ~/.claude/projects/ tree."""
    pd = tmp_path / "projects"
    pd.mkdir()
    return pd


@pytest.fixture()
def tl_dir(projects_dir: Path) -> Path:
    """Create the Team Lead project directory."""
    d = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Tests: team_lead_usage()
# ---------------------------------------------------------------------------

class TestTeamLeadUsage:

    def test_returns_zeros_when_no_files(self, tmp_path):
        """Missing projects dir → all zeros, graceful."""
        missing = tmp_path / "no-projects"
        result = team_lead_usage(_projects_dir_override=missing)
        assert result["input"] == 0
        assert result["output"] == 0
        assert result["cache_read"] == 0
        assert result["cache_write"] == 0
        assert result["sessions_count"] == 0
        assert result["p50_tokens_per_turn"] == 0
        assert result["p95_tokens_per_turn"] == 0

    def test_sums_tokens_in_window(self, projects_dir, tl_dir, now):
        """Normal entries within window are summed correctly."""
        within = now - timedelta(minutes=30)
        outside = now - timedelta(hours=6)

        lines = [
            _entry(within, inp=1000, out=200),
            _entry(within, inp=500, out=100),
            _entry(outside, inp=9999, out=9999),  # should be excluded
        ]
        _write(tl_dir / "session-abc.jsonl", lines)

        result = team_lead_usage(
            since_ts=(now - timedelta(hours=5)).timestamp(),
            until_ts=now.timestamp(),
            _projects_dir_override=projects_dir,
        )

        assert result["input"] == 1500
        assert result["output"] == 300
        assert result["sessions_count"] == 1

    def test_cache_tokens_tracked_separately(self, projects_dir, tl_dir, now):
        """cache_read and cache_write are tracked and not conflated with input/output."""
        within = now - timedelta(minutes=10)
        lines = [_entry(within, inp=100, out=50, cache_read=2000, cache_write=1000)]
        _write(tl_dir / "session-cache.jsonl", lines)

        result = team_lead_usage(
            since_ts=(now - timedelta(hours=1)).timestamp(),
            until_ts=now.timestamp(),
            _projects_dir_override=projects_dir,
        )

        assert result["input"] == 100
        assert result["output"] == 50
        assert result["cache_read"] == 2000
        assert result["cache_write"] == 1000

    def test_worktree_dirs_excluded(self, projects_dir, tl_dir, now):
        """Sub-agent worktree dirs must NOT be counted as Team Lead."""
        within = now - timedelta(minutes=10)

        # Team Lead dir — should be included
        tl_lines = [_entry(within, inp=100, out=50)]
        _write(tl_dir / "session-tl.jsonl", tl_lines)

        # Sub-agent worktree dir — must be excluded (longer name with suffix)
        worktree_dir = projects_dir / f"{_TEAM_LEAD_PROJECT_DIR_NAME}--claude-worktrees-agent-abc123"
        worktree_lines = [_entry(within, inp=99999, out=99999)]
        _write(worktree_dir / "session-sub.jsonl", worktree_lines)

        result = team_lead_usage(
            since_ts=(now - timedelta(hours=1)).timestamp(),
            until_ts=now.timestamp(),
            _projects_dir_override=projects_dir,
        )

        # Only Team Lead tokens should appear
        assert result["input"] == 100
        assert result["output"] == 50

    def test_malformed_lines_tolerated(self, projects_dir, tl_dir, now):
        """Malformed JSON lines are skipped without raising."""
        within = now - timedelta(minutes=5)
        lines = [
            "NOT JSON {{{",
            _entry(within, inp=200, out=80),
            "",
            '{"timestamp": "bad-ts", "usage": {"input_tokens": 999}}',
        ]
        _write(tl_dir / "session-bad.jsonl", lines)

        result = team_lead_usage(
            since_ts=(now - timedelta(hours=1)).timestamp(),
            until_ts=now.timestamp(),
            _projects_dir_override=projects_dir,
        )

        # Only the valid entry should be counted
        assert result["input"] == 200
        assert result["output"] == 80

    def test_p50_p95_computed(self, projects_dir, tl_dir, now):
        """p50 and p95 per-turn totals are computed correctly."""
        base = now - timedelta(minutes=30)

        # 4 entries with per-turn totals: 100, 200, 300, 400
        lines = [
            _entry(base, inp=50, out=50),    # total=100
            _entry(base, inp=100, out=100),  # total=200
            _entry(base, inp=150, out=150),  # total=300
            _entry(base, inp=200, out=200),  # total=400
        ]
        _write(tl_dir / "session-p.jsonl", lines)

        result = team_lead_usage(
            since_ts=(now - timedelta(hours=1)).timestamp(),
            until_ts=now.timestamp(),
            _projects_dir_override=projects_dir,
        )

        assert result["p50_tokens_per_turn"] > 0
        assert result["p95_tokens_per_turn"] > 0
        # p95 >= p50
        assert result["p95_tokens_per_turn"] >= result["p50_tokens_per_turn"]

    def test_default_window_applied(self, projects_dir, tl_dir):
        """When since_ts is None, default 5h window is applied (entries beyond 5h excluded)."""
        now_dt = datetime.now(timezone.utc)
        within = now_dt - timedelta(hours=4)
        outside = now_dt - timedelta(hours=6)

        lines = [
            _entry(within, inp=300, out=100),
            _entry(outside, inp=9999, out=9999),
        ]
        _write(tl_dir / "session-win.jsonl", lines)

        result = team_lead_usage(_projects_dir_override=projects_dir)

        assert result["input"] == 300
        assert result["output"] == 100

    def test_multiple_session_files(self, projects_dir, tl_dir, now):
        """Multiple JSONL files in TL dir are all read."""
        within = now - timedelta(minutes=10)
        _write(tl_dir / "session-1.jsonl", [_entry(within, inp=100, out=10)])
        _write(tl_dir / "session-2.jsonl", [_entry(within, inp=200, out=20)])

        result = team_lead_usage(
            since_ts=(now - timedelta(hours=1)).timestamp(),
            until_ts=now.timestamp(),
            _projects_dir_override=projects_dir,
        )

        assert result["input"] == 300
        assert result["output"] == 30
        assert result["sessions_count"] == 2


# ---------------------------------------------------------------------------
# Tests: CostTracker.summarize_team_lead()
# ---------------------------------------------------------------------------

class TestCostTrackerSummarizeTeamLead:

    def _make_tracker(self, tmp_path: Path) -> CostTracker:
        bb = Blackboard(root=tmp_path / "bb")
        return CostTracker(bb=bb)

    def test_returns_zero_when_no_jsonl(self, tmp_path):
        """Returns dict with all zeros when no JSONL files exist."""
        ct = self._make_tracker(tmp_path)
        missing = tmp_path / "no-projects"

        with patch("backend.subscription_usage._projects_dir", return_value=missing):
            result = ct.summarize_team_lead()

        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["cost_usd_equivalent"] == 0.0
        assert "p50_tokens_per_turn" in result
        assert "p95_tokens_per_turn" in result

    def test_cost_computed_with_opus_pricing(self, tmp_path):
        """cost_usd_equivalent uses Opus 4.7 pricing (not default Sonnet)."""
        now_dt = datetime.now(timezone.utc)
        within = now_dt - timedelta(minutes=10)

        projects_dir = tmp_path / "projects"
        tl_dir = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
        _write(tl_dir / "session.jsonl", [_entry(within, inp=10000, out=2000)])

        ct = self._make_tracker(tmp_path)

        with patch("backend.subscription_usage._projects_dir", return_value=projects_dir):
            result = ct.summarize_team_lead()

        # Opus 4.7: input=$0.015/1k, output=$0.075/1k
        # 10000 * 0.015/1000 + 2000 * 0.075/1000 = 0.150 + 0.150 = 0.300
        assert result["input_tokens"] == 10000
        assert result["output_tokens"] == 2000
        expected_cost = (10000 / 1000 * 0.015) + (2000 / 1000 * 0.075)
        assert abs(result["cost_usd_equivalent"] - expected_cost) < 1e-5

    def test_worktree_dirs_excluded_from_cost(self, tmp_path):
        """Sub-agent worktree tokens must not appear in Team Lead cost summary."""
        now_dt = datetime.now(timezone.utc)
        within = now_dt - timedelta(minutes=5)

        projects_dir = tmp_path / "projects"
        tl_dir = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
        worktree_dir = projects_dir / f"{_TEAM_LEAD_PROJECT_DIR_NAME}--claude-worktrees-agent-xyz"

        _write(tl_dir / "session.jsonl", [_entry(within, inp=1000, out=100)])
        _write(worktree_dir / "subagent.jsonl", [_entry(within, inp=99999, out=99999)])

        ct = self._make_tracker(tmp_path)

        with patch("backend.subscription_usage._projects_dir", return_value=projects_dir):
            result = ct.summarize_team_lead()

        assert result["input_tokens"] == 1000
        assert result["output_tokens"] == 100
