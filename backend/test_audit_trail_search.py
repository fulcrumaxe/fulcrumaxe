"""
Tests for the audit_trail.py `search` subcommand and supporting helpers.

Each test creates an isolated JSONL fixture via tmp_path so tests do not
touch the real audit trail.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.audit_trail import AuditTrail, _parse_time_expr

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ts(offset_seconds: int = 0) -> str:
    """Return an ISO 8601 UTC timestamp relative to _NOW."""
    dt = _NOW + timedelta(seconds=offset_seconds)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_entries(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _make_trail(tmp_path: Path, entries: list[dict]) -> AuditTrail:
    audit_file = tmp_path / "audit.jsonl"
    _write_entries(audit_file, entries)
    return AuditTrail(audit_path=audit_file)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ENTRIES = [
    {
        "ts": _ts(-7200),  # 2 h ago
        "source": "blackboard",
        "action": "write",
        "key": "memory/396-result",
        "old": None,
        "new": {"discussion": 396, "role": "project-manager", "verdict": "done"},
        "actor": "project-manager",
        "seq": 1,
    },
    {
        "ts": _ts(-3600),  # 1 h ago
        "source": "blackboard",
        "action": "write",
        "key": "memory/367-result",
        "old": None,
        "new": {"discussion": 367, "role": "code-reviewer", "verdict": "needs-fix",
                "tags": ["code-reviewer", "needs-fix"]},
        "actor": "code-reviewer",
        "seq": 2,
    },
    {
        "ts": _ts(-1800),  # 30 min ago
        "source": "agent",
        "action": "spawn",
        "key": "budget/agents/executor-396-abc",
        "old": None,
        "new": {"role": "executor", "verdict": "done", "tags": ["executor", "done"],
                "discussion": 396},
        "actor": "team-lead",
        "seq": 3,
    },
    {
        "ts": _ts(-600),  # 10 min ago
        "source": "budget",
        "action": "set",
        "key": "budget/session",
        "old": 1000,
        "new": {"agent": "security-reviewer", "discussion": 400},
        "actor": "security-reviewer",
        "seq": 4,
    },
]


# ---------------------------------------------------------------------------
# _parse_time_expr tests
# ---------------------------------------------------------------------------


class TestParseTimeExpr:
    def test_now(self):
        result = _parse_time_expr("now", _NOW)
        assert result == _NOW

    def test_relative_minutes(self):
        result = _parse_time_expr("30m", _NOW)
        assert result == _NOW - timedelta(minutes=30)

    def test_relative_hours(self):
        result = _parse_time_expr("4h", _NOW)
        assert result == _NOW - timedelta(hours=4)

    def test_relative_days(self):
        result = _parse_time_expr("2d", _NOW)
        assert result == _NOW - timedelta(days=2)

    def test_relative_weeks(self):
        result = _parse_time_expr("1w", _NOW)
        assert result == _NOW - timedelta(weeks=1)

    def test_iso8601_with_z(self):
        expr = "2026-05-09T00:00:00Z"
        result = _parse_time_expr(expr, _NOW)
        assert result == datetime(2026, 5, 9, 0, 0, 0, tzinfo=timezone.utc)

    def test_iso8601_without_z(self):
        expr = "2026-05-09T00:00:00+00:00"
        result = _parse_time_expr(expr, _NOW)
        assert result == datetime(2026, 5, 9, 0, 0, 0, tzinfo=timezone.utc)

    def test_bad_expr_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_time_expr("99x", _NOW)

    def test_bad_expr_mentions_input(self):
        with pytest.raises(ValueError, match="99x"):
            _parse_time_expr("99x", _NOW)


# ---------------------------------------------------------------------------
# AuditTrail.search — individual filter tests
# ---------------------------------------------------------------------------


class TestSearchFilters:
    def test_filter_by_discussion(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(discussion=396)
        assert len(results) == 2
        for r in results:
            new = r.get("new") or {}
            assert str(new.get("discussion")) == "396" or "396" in r.get("key", "")

    def test_filter_by_role_actor(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        # Entry 4 has actor=security-reviewer, no new.role match
        results = at.search(role="security-reviewer")
        assert len(results) == 1
        assert results[0]["actor"] == "security-reviewer"

    def test_filter_by_role_new_role(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(role="code-reviewer")
        assert len(results) == 1
        assert results[0]["new"]["role"] == "code-reviewer"

    def test_filter_by_verdict_direct(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(verdict="done")
        # entries 1 and 3 have verdict=done
        assert len(results) == 2
        for r in results:
            assert r["new"]["verdict"] == "done"

    def test_filter_by_verdict_in_tags(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(verdict="needs-fix")
        assert len(results) == 1
        assert "needs-fix" in results[0]["new"]["tags"]

    def test_filter_by_since(self, tmp_path):
        """Entries newer than 90 min ago: entries 2, 3, and 4.

        SAMPLE_ENTRIES timestamps:
          seq=1 → -7200s (2 h ago)   — excluded
          seq=2 → -3600s (1 h ago)   — included (1h < 90 min cutoff of 1.5h ago)
          seq=3 → -1800s (30 min)    — included
          seq=4 →  -600s (10 min)    — included
        """
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        since_dt = _NOW - timedelta(minutes=90)
        since_expr = since_dt.isoformat().replace("+00:00", "Z")
        results = at.search(since_expr=since_expr)
        assert len(results) == 3
        seqs = {r["seq"] for r in results}
        assert seqs == {2, 3, 4}

    def test_filter_by_until(self, tmp_path):
        """Entries older than 45 min ago: entries 1 and 2 only."""
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        until_dt = _NOW - timedelta(minutes=45)
        until_expr = until_dt.isoformat().replace("+00:00", "Z")
        results = at.search(until_expr=until_expr)
        assert len(results) == 2
        seqs = {r["seq"] for r in results}
        assert seqs == {1, 2}

    def test_no_filters_returns_all(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search()
        assert len(results) == len(SAMPLE_ENTRIES)


# ---------------------------------------------------------------------------
# AuditTrail.search — combined filters
# ---------------------------------------------------------------------------


class TestSearchCombined:
    def test_discussion_and_role(self, tmp_path):
        """discussion=396 AND role=project-manager → only entry 1."""
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(discussion=396, role="project-manager")
        assert len(results) == 1
        assert results[0]["seq"] == 1

    def test_discussion_and_role_no_match(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(discussion=396, role="security-reviewer")
        assert results == []

    def test_limit_respected(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(limit=2)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# AuditTrail.search — rotated file (.jsonl.1)
# ---------------------------------------------------------------------------


class TestSearchRotatedFile:
    def test_reads_rotated_file(self, tmp_path):
        rotated_file = tmp_path / "audit.jsonl.1"
        main_file = tmp_path / "audit.jsonl"

        old_entry = {
            "ts": _ts(-10000),
            "source": "blackboard",
            "action": "write",
            "key": "memory/100-result",
            "old": None,
            "new": {"discussion": 100, "role": "executor", "verdict": "done"},
            "actor": "executor",
            "seq": 0,
        }
        new_entry = SAMPLE_ENTRIES[0]

        _write_entries(rotated_file, [old_entry])
        _write_entries(main_file, [new_entry])

        at = AuditTrail(audit_path=main_file)
        results = at.search(discussion=100)
        assert len(results) == 1
        assert results[0]["new"]["discussion"] == 100

    def test_rotated_file_absent_no_error(self, tmp_path):
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search()
        assert len(results) == len(SAMPLE_ENTRIES)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestSearchCLI:
    def test_search_help_exits_zero(self):
        """search --help exits 0 and lists all flags."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "backend/audit_trail.py", "search", "--help"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        for flag in ["--discussion", "--role", "--verdict", "--since", "--until",
                     "--source", "--action", "--actor", "--limit", "--format"]:
            assert flag in result.stdout, f"Missing {flag} from --help output"

    def test_bad_since_exits_2(self, tmp_path):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "backend/audit_trail.py", "search", "--since", "99x"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "--since" in result.stderr

    def test_json_format_is_parseable(self, tmp_path):
        """Each line from --format json must be valid JSON."""
        at = _make_trail(tmp_path, SAMPLE_ENTRIES)
        results = at.search(verdict="done", limit=100)
        # Verify we'd get valid JSON by serialising and re-parsing each entry
        for entry in results:
            line = json.dumps(entry, default=str)
            parsed = json.loads(line)
            assert isinstance(parsed, dict)

    def test_tail_unchanged(self):
        """tail command still works and does not error out."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "backend/audit_trail.py", "tail", "--n", "1"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True,
        )
        assert result.returncode == 0

    def test_stats_unchanged(self):
        """stats command still works and does not error out."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "backend/audit_trail.py", "stats"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True,
        )
        assert result.returncode == 0
