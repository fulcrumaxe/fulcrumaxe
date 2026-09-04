"""Tests for backend/red_main_check.py (D#1409).

Covers:
  - map_changed_to_test_files: various changed-file scenarios
  - run_bounded_tests: all-green -> passed=True; failure -> passed=False
  - check_pr: all-green -> overturns_recorded=0
  - check_pr: failing test + passing roles -> exactly one red_main per passing role
  - fail-open: subprocess error -> no overturn recorded
  - fail-open: no test files matched -> skipped=True, no overturns

All DB state is isolated via STATS_DB_PATH and agent_run DB path env vars.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    import duckdb as _duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stats_db(tmp_path: Path) -> Path:
    """Create a minimal stats.duckdb (metric_event + agent_run tables)."""
    db = tmp_path / "stats.duckdb"
    conn = _duckdb.connect(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metric_event (
            ts      TIMESTAMPTZ NOT NULL,
            metric  TEXT        NOT NULL,
            tags    JSON,
            value   DOUBLE      NOT NULL,
            unit    TEXT        NOT NULL,
            source  TEXT,
            PRIMARY KEY (ts, metric, tags)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_time ON metric_event(metric, ts)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_run (
            agent_id               VARCHAR PRIMARY KEY,
            role                   VARCHAR NOT NULL,
            discussion             INTEGER,
            pr                     INTEGER,
            start_ts               TIMESTAMPTZ NOT NULL,
            end_ts                 TIMESTAMPTZ,
            verdict                VARCHAR
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_run_pr ON agent_run(pr)"
    )
    conn.close()
    return db


def _insert_agent_run(db: Path, agent_id: str, role: str, pr: int, verdict: str) -> None:
    now = datetime.now(timezone.utc)
    conn = _duckdb.connect(str(db))
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_run
                (agent_id, role, pr, start_ts, end_ts, verdict)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [agent_id, role, pr, now, now, verdict],
        )
    finally:
        conn.close()


def _count_overturns(db: Path, pr: int, kind: str = "red_main") -> int:
    conn = _duckdb.connect(str(db))
    try:
        rows = conn.execute(
            """
            SELECT COUNT(*) FROM metric_event
            WHERE metric = 'verdict_overturn'
              AND json_extract_string(tags, '$.pr') = ?
              AND json_extract_string(tags, '$.kind') = ?
            """,
            [str(pr), kind],
        ).fetchone()
        return rows[0] if rows else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Isolated stats DB; patches STATS_DB_PATH so stats_writer uses it."""
    db = _make_stats_db(tmp_path)
    monkeypatch.setenv("STATS_DB_PATH", str(db))
    return db


@pytest.fixture()
def fake_repo(tmp_path):
    """Create a minimal repo layout with a few backend files and test stubs."""
    # backend/foo.py
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "foo.py").write_text("# foo\n")
    # backend/tests/test_foo.py — always passes
    (tmp_path / "backend" / "tests").mkdir()
    (tmp_path / "backend" / "tests" / "test_foo.py").write_text(
        "def test_ok():\n    assert True\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# map_changed_to_test_files
# ---------------------------------------------------------------------------


class TestMapChangedToTestFiles:
    def test_backend_module_maps_to_test(self, fake_repo):
        from backend.red_main_check import map_changed_to_test_files

        result = map_changed_to_test_files(["backend/foo.py"], fake_repo)
        assert len(result) == 1
        assert result[0] == fake_repo / "backend" / "tests" / "test_foo.py"

    def test_direct_test_file_included(self, fake_repo):
        from backend.red_main_check import map_changed_to_test_files

        result = map_changed_to_test_files(
            ["backend/tests/test_foo.py"], fake_repo
        )
        assert len(result) == 1
        assert result[0] == fake_repo / "backend" / "tests" / "test_foo.py"

    def test_missing_test_file_skipped(self, fake_repo):
        """backend/bar.py exists but backend/tests/test_bar.py does not."""
        from backend.red_main_check import map_changed_to_test_files

        (fake_repo / "backend" / "bar.py").write_text("# bar\n")
        result = map_changed_to_test_files(["backend/bar.py"], fake_repo)
        assert result == []

    def test_non_backend_file_ignored(self, fake_repo):
        from backend.red_main_check import map_changed_to_test_files

        result = map_changed_to_test_files(["tui/App.tsx"], fake_repo)
        assert result == []

    def test_dedup_same_test_file(self, fake_repo):
        """Both backend/foo.py and backend/tests/test_foo.py changed -> include once."""
        from backend.red_main_check import map_changed_to_test_files

        result = map_changed_to_test_files(
            ["backend/foo.py", "backend/tests/test_foo.py"], fake_repo
        )
        assert len(result) == 1

    def test_empty_input(self, fake_repo):
        from backend.red_main_check import map_changed_to_test_files

        assert map_changed_to_test_files([], fake_repo) == []

    def test_string_repo_root_no_type_error(self, fake_repo):
        """Passing repo_root as a plain string (e.g. '.') must not raise TypeError."""
        from backend.red_main_check import map_changed_to_test_files

        # str(fake_repo) gives an absolute string path — must work without TypeError
        result = map_changed_to_test_files(["backend/foo.py"], str(fake_repo))
        assert len(result) == 1
        assert result[0] == fake_repo / "backend" / "tests" / "test_foo.py"


# ---------------------------------------------------------------------------
# run_bounded_tests
# ---------------------------------------------------------------------------


class TestRunBoundedTests:
    def test_no_files_returns_skipped(self):
        from backend.red_main_check import run_bounded_tests

        result = run_bounded_tests([])
        assert result.skipped is True
        assert result.passed is True
        assert result.failures == []

    def test_passing_test_returns_passed(self, fake_repo):
        from backend.red_main_check import run_bounded_tests

        test_file = fake_repo / "backend" / "tests" / "test_foo.py"
        result = run_bounded_tests([test_file])
        assert result.passed is True
        assert result.skipped is False

    def test_failing_test_returns_failed(self, tmp_path):
        from backend.red_main_check import run_bounded_tests

        failing = tmp_path / "test_fail.py"
        failing.write_text("def test_fails():\n    assert False, 'intentional'\n")
        result = run_bounded_tests([failing])
        assert result.passed is False
        assert result.skipped is False
        assert any("intentional" in line for line in result.failures)

    def test_timeout_is_fail_open(self, tmp_path):
        """Simulate a timeout -> should return passed=True (fail-open)."""
        from backend.red_main_check import run_bounded_tests
        import subprocess

        slow_test = tmp_path / "test_slow.py"
        slow_test.write_text("import time\ndef test_hang():\n    time.sleep(999)\n")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pytest", 1)):
            result = run_bounded_tests([slow_test], timeout=1)

        assert result.passed is True
        assert result.skipped is False

    def test_subprocess_error_is_fail_open(self, tmp_path):
        """OSError during subprocess -> fail-open, no crash."""
        from backend.red_main_check import run_bounded_tests

        some_test = tmp_path / "test_x.py"
        some_test.write_text("def test_ok(): pass\n")

        with patch("subprocess.run", side_effect=OSError("no pytest")):
            result = run_bounded_tests([some_test])

        assert result.passed is True


# ---------------------------------------------------------------------------
# check_pr: all-green scenario
# ---------------------------------------------------------------------------


class TestCheckPrAllGreen:
    def test_no_overturns_when_tests_pass(self, fake_repo, tmp_db):
        """If tests pass, no overturn is recorded even with passing roles."""
        from backend.red_main_check import check_pr

        _insert_agent_run(tmp_db, "agent-1", "executor", pr=100, verdict="done")
        _insert_agent_run(tmp_db, "agent-2", "code-reviewer", pr=100, verdict="pass")

        result = check_pr(
            pr=100,
            changed_files=["backend/foo.py"],
            repo_root=fake_repo,
            timeout=30,
        )
        assert result["passed"] is True
        assert result["overturns_recorded"] == 0
        assert _count_overturns(tmp_db, pr=100) == 0

    def test_no_test_files_matched(self, fake_repo, tmp_db):
        """If no test files are mapped, check_pr is a no-op (skipped=True)."""
        from backend.red_main_check import check_pr

        _insert_agent_run(tmp_db, "agent-3", "executor", pr=101, verdict="done")

        result = check_pr(
            pr=101,
            changed_files=["tui/App.tsx"],
            repo_root=fake_repo,
        )
        assert result["skipped"] is True
        assert result["passed"] is True
        assert result["overturns_recorded"] == 0


# ---------------------------------------------------------------------------
# check_pr: red-main scenario (the #1379 scenario)
# ---------------------------------------------------------------------------


class TestCheckPrRedMain:
    def _make_failing_test(self, tmp_path: Path) -> Path:
        test_dir = tmp_path / "backend" / "tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        module = tmp_path / "backend" / "broken.py"
        module.write_text("# broken module\n")
        test_file = test_dir / "test_broken.py"
        test_file.write_text("def test_regression():\n    assert False, 'red main'\n")
        return test_file

    def test_failing_touched_test_records_one_overturn_per_passing_role(
        self, tmp_path, monkeypatch
    ):
        """Simulate #1379 scenario: executor+code-reviewer passed, then main goes RED.

        Expects exactly one red_main overturn per passing role (2 total).
        """
        db = _make_stats_db(tmp_path)
        monkeypatch.setenv("STATS_DB_PATH", str(db))

        self._make_failing_test(tmp_path)

        # Two passing roles for PR 1379
        _insert_agent_run(db, "agent-exec", "executor", pr=1379, verdict="done")
        _insert_agent_run(db, "agent-cr", "code-reviewer", pr=1379, verdict="pass")

        from backend.red_main_check import check_pr

        result = check_pr(
            pr=1379,
            changed_files=["backend/broken.py"],
            repo_root=tmp_path,
            timeout=30,
        )

        assert result["passed"] is False
        assert result["overturns_recorded"] == 2
        assert _count_overturns(db, pr=1379, kind="red_main") == 2

    def test_one_passing_role_gets_one_overturn(self, tmp_path, monkeypatch):
        db = _make_stats_db(tmp_path)
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        self._make_failing_test(tmp_path)

        _insert_agent_run(db, "agent-exec-2", "executor", pr=200, verdict="done")

        from backend.red_main_check import check_pr

        result = check_pr(
            pr=200,
            changed_files=["backend/broken.py"],
            repo_root=tmp_path,
            timeout=30,
        )
        assert result["passed"] is False
        assert result["overturns_recorded"] == 1
        assert _count_overturns(db, pr=200, kind="red_main") == 1

    def test_no_passing_roles_means_no_overturns(self, tmp_path, monkeypatch):
        """Red main but no passing roles in DB -> 0 overturns."""
        db = _make_stats_db(tmp_path)
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        self._make_failing_test(tmp_path)

        from backend.red_main_check import check_pr

        result = check_pr(
            pr=300,
            changed_files=["backend/broken.py"],
            repo_root=tmp_path,
            timeout=30,
        )
        assert result["passed"] is False
        assert result["overturns_recorded"] == 0

    def test_failed_roles_not_included(self, tmp_path, monkeypatch):
        """Only pass/done roles get overturns, not needs-fix/fail roles."""
        db = _make_stats_db(tmp_path)
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        self._make_failing_test(tmp_path)

        _insert_agent_run(db, "agent-exec-3", "executor", pr=400, verdict="done")
        _insert_agent_run(db, "agent-cr-3", "code-reviewer", pr=400, verdict="needs-fix")

        from backend.red_main_check import check_pr

        result = check_pr(
            pr=400,
            changed_files=["backend/broken.py"],
            repo_root=tmp_path,
            timeout=30,
        )
        assert result["overturns_recorded"] == 1  # only executor

    def test_dry_run_no_db_writes(self, tmp_path, monkeypatch):
        """dry_run=True -> result contains passing_roles but no DB writes."""
        db = _make_stats_db(tmp_path)
        monkeypatch.setenv("STATS_DB_PATH", str(db))
        self._make_failing_test(tmp_path)

        _insert_agent_run(db, "agent-exec-4", "executor", pr=500, verdict="done")

        from backend.red_main_check import check_pr

        result = check_pr(
            pr=500,
            changed_files=["backend/broken.py"],
            repo_root=tmp_path,
            timeout=30,
            dry_run=True,
        )
        assert result.get("dry_run") is True
        # No overturns in DB
        assert _count_overturns(db, pr=500, kind="red_main") == 0


# ---------------------------------------------------------------------------
# overturn_rate_by_role_24h readable via verdict_overturn module
# ---------------------------------------------------------------------------


class TestOverturnRateReadable:
    def test_red_main_overturns_visible_in_rate_query(self, tmp_path, monkeypatch):
        """Overturns written by check_pr are visible in overturn_rate_by_role_24h.

        This confirms the #1379 scenario end-to-end: merge leaves main red ->
        record overturns -> they are queryable via the standard API.
        """
        db = _make_stats_db(tmp_path)
        monkeypatch.setenv("STATS_DB_PATH", str(db))

        # Simulate failing test
        (tmp_path / "backend").mkdir(exist_ok=True)
        (tmp_path / "backend" / "tests").mkdir(exist_ok=True)
        (tmp_path / "backend" / "widget.py").write_text("# widget\n")
        test_file = tmp_path / "backend" / "tests" / "test_widget.py"
        test_file.write_text("def test_widget():\n    assert False, 'red'\n")

        _insert_agent_run(db, "agent-exec-5", "executor", pr=600, verdict="done")
        _insert_agent_run(db, "agent-cr-5", "acceptance-tester", pr=600, verdict="pass")

        from backend.red_main_check import check_pr

        result = check_pr(
            pr=600,
            changed_files=["backend/widget.py"],
            repo_root=tmp_path,
            timeout=30,
        )
        assert result["overturns_recorded"] == 2

        # Now verify readable via overturn_rate_by_role_24h
        # We need role_verdict rows too for the sample_size denominator.
        # Directly insert them so the rate function sees a sample.
        now = datetime.now(timezone.utc)
        conn = _duckdb.connect(str(db))
        try:
            for role in ("executor", "acceptance-tester"):
                for i in range(5):
                    conn.execute(
                        """INSERT OR IGNORE INTO metric_event
                           (ts, metric, tags, value, unit, source)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        [
                            now,
                            "role_verdict",
                            json.dumps({"role": role, "verdict": "pass", "seq": str(i)}),
                            1.0,
                            "count",
                            "test",
                        ],
                    )
        finally:
            conn.close()

        from backend.verdict_overturn import overturn_rate_by_role_24h

        rows = overturn_rate_by_role_24h()
        roles_with_overturns = {r["role"] for r in rows if r.get("overturns", 0) > 0}
        # Both executor and acceptance-tester should show up
        assert "executor" in roles_with_overturns
        assert "acceptance-tester" in roles_with_overturns
