"""
Tests for backend/circuit_breaker.py

Run with:
    python -m pytest backend/tests/test_circuit_breaker.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.circuit_breaker as cb_mod
from backend.circuit_breaker import (
    DEFAULT_THRESHOLD,
    STALE_BREAKER_DAYS,
    _age_str,
    _collect_tripped,
    expire_stale,
    is_blocked,
    main,
    record_failure,
    record_success,
)


# ---------------------------------------------------------------------------
# Fixtures — in-memory blackboard
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_bb(tmp_path):
    """Replace the module-level _bb with a fresh in-memory Blackboard."""
    from backend.blackboard import Blackboard
    bb = Blackboard(root=tmp_path / "bb")
    with patch.object(cb_mod, "_bb", bb):
        yield bb


# ---------------------------------------------------------------------------
# _discussion_state — GraphQL argv var/binding mismatch regression test
# ---------------------------------------------------------------------------


def test_discussion_state_argv_vars_match_bindings():
    """Regression: declared GraphQL vars must equal the -f/-F binding keys.

    This test exercises the REAL _discussion_state() function (not mocked)
    and patches only subprocess.run. It verifies that every variable declared
    in the query($...) header has a matching -f/-F binding key in the argv,
    and vice versa — the exact class of bug that shipped in the original code
    (name= instead of repo=).
    """
    import re
    from unittest.mock import MagicMock, patch

    payload = json.dumps(
        {"data": {"repository": {"discussion": {"closed": True}}}}
    )

    captured_argv: list[list] = []

    def fake_run(argv, **kwargs):
        captured_argv.append(argv)
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = payload
        return mock

    with patch.object(cb_mod.subprocess, "run", side_effect=fake_run):
        result = cb_mod._discussion_state(1207)

    assert len(captured_argv) == 1, "subprocess.run should be called exactly once"
    argv = captured_argv[0]

    # Extract the query= argument value
    query_arg = next(
        (argv[i + 1] for i, a in enumerate(argv) if a in ("-f", "-F") and
         argv[i + 1].startswith("query=")),
        None,
    )
    assert query_arg is not None, "argv must contain a query= binding"
    query_body = query_arg[len("query="):]

    # Parse declared variable names from query($owner:..., $repo:..., $num:...)
    # Find the query(...) declaration block
    decl_match = re.search(r"query\s*\(([^)]+)\)", query_body)
    assert decl_match, f"Could not find query(…) declaration in: {query_body!r}"
    declared_vars = set(re.findall(r"\$(\w+)", decl_match.group(1)))

    # Parse binding keys from -f KEY=... and -F KEY=... pairs (skip query= itself)
    binding_keys: set[str] = set()
    for i, tok in enumerate(argv):
        if tok in ("-f", "-F") and i + 1 < len(argv):
            val = argv[i + 1]
            if not val.startswith("query="):
                key = val.split("=", 1)[0]
                binding_keys.add(key)

    # Core assertion: no orphan declarations, no orphan bindings
    assert declared_vars == binding_keys, (
        f"GraphQL var/binding mismatch — declared: {declared_vars}, bound: {binding_keys}"
    )

    # Specific regression check: repo= present, name= absent. Asserted
    # against backend._repo's own resolved value (not a second hard-coded
    # copy) — D#1879: this literal used to be the stale pre-rename
    # "fulcrumaxe" and only passed because backend._repo.REPO_NAME
    # was itself stuck on that value; fixing the resolver's return value
    # correctly made the hard-coded copy here wrong instead.
    from backend._repo import REPO_NAME as _expected_repo_name

    flat_argv = " ".join(str(a) for a in argv)
    assert f"repo={_expected_repo_name}" in flat_argv, (
        f"argv must contain 'repo={_expected_repo_name}'"
    )
    assert "name=fulcrumaxe" not in flat_argv, (
        "argv must NOT contain 'name=fulcrumaxe' (old broken binding)"
    )

    # End-to-end: the real parsing logic must return "closed"
    assert result == "closed", f"Expected 'closed', got {result!r}"


# ---------------------------------------------------------------------------
# _discussion_state — returncode-before-parse regression cases
# ---------------------------------------------------------------------------


def _make_run_mock(returncode: int, stdout: str):
    """Helper: build a subprocess.run mock with given returncode and stdout."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    return mock


def _patch_run(returncode: int, stdout: str):
    """Context manager that patches subprocess.run to return a fixed result."""
    from unittest.mock import patch
    m = _make_run_mock(returncode, stdout)
    return patch.object(cb_mod.subprocess, "run", return_value=m)


NOT_FOUND_BODY = json.dumps({
    "data": {"repository": {"discussion": None}},
    "errors": [
        {
            "type": "NOT_FOUND",
            "path": ["repository", "discussion"],
            "message": "Could not resolve to a Discussion with the number of 99.",
        }
    ],
})

RATE_LIMIT_BODY = json.dumps({
    "data": {"repository": {"discussion": None}},
    "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
})


def test_discussion_state_absent_real_shape():
    """Regression: exit-1 + NOT_FOUND body → 'absent' (was broken: returned 'unknown')."""
    with _patch_run(1, NOT_FOUND_BODY):
        result = cb_mod._discussion_state(99)
    assert result == "absent", (
        f"Expected 'absent' for confirmed-not-found discussion, got {result!r}"
    )


def test_discussion_state_empty_stdout_is_unknown():
    """Exit-1 with empty stdout (network failure) → 'unknown' (fail-safe HOLD)."""
    with _patch_run(1, ""):
        result = cb_mod._discussion_state(99)
    assert result == "unknown"


def test_discussion_state_timeout_is_unknown():
    """subprocess.TimeoutExpired → 'unknown' (fail-safe HOLD)."""
    import subprocess
    with patch.object(cb_mod.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 10)):
        result = cb_mod._discussion_state(99)
    assert result == "unknown"


def test_discussion_state_non_not_found_error_is_unknown():
    """Rate-limit error alongside null discussion → 'unknown' (ambiguous, hold fail-safe)."""
    with _patch_run(1, RATE_LIMIT_BODY):
        result = cb_mod._discussion_state(99)
    assert result == "unknown"


def test_discussion_state_closed_returncode0():
    """Exit-0 + closed=true → 'closed'."""
    body = json.dumps({"data": {"repository": {"discussion": {"closed": True}}}})
    with _patch_run(0, body):
        result = cb_mod._discussion_state(1207)
    assert result == "closed"


def test_discussion_state_open_returncode0():
    """Exit-0 + closed=false → 'open'."""
    body = json.dumps({"data": {"repository": {"discussion": {"closed": False}}}})
    with _patch_run(0, body):
        result = cb_mod._discussion_state(1207)
    assert result == "open"


# ---------------------------------------------------------------------------
# record_failure / record_success
# ---------------------------------------------------------------------------


def test_record_failure_increments_count():
    count = record_failure(100, "executor", "could not implement")
    assert count == 1
    count2 = record_failure(100, "executor", "still failing")
    assert count2 == 2


def test_record_failure_writes_meta():
    record_failure(394, "executor", "could not implement")
    meta = cb_mod._bb.read("failures_meta/394")
    assert isinstance(meta, dict)
    assert meta["agent"] == "executor"
    assert meta["reason"] == "could not implement"
    assert meta["count"] == 1
    assert "updated_at" in meta
    # updated_at must be ISO-8601
    assert "T" in meta["updated_at"]


def test_record_failure_meta_updates_on_retry():
    record_failure(394, "executor", "first")
    record_failure(394, "executor", "second")
    meta = cb_mod._bb.read("failures_meta/394")
    assert meta["reason"] == "second"
    assert meta["count"] == 2


def test_record_success_clears_both_keys():
    record_failure(100, "executor", "oops")
    record_success(100)
    assert cb_mod._bb.read("failures/100") is None
    assert cb_mod._bb.read("failures_meta/100") is None


# ---------------------------------------------------------------------------
# is_blocked
# ---------------------------------------------------------------------------


def test_is_blocked_false_below_threshold():
    record_failure(1, "executor", "x")
    record_failure(1, "executor", "x")
    assert not is_blocked(1)


def test_is_blocked_true_at_threshold():
    for _ in range(DEFAULT_THRESHOLD):
        record_failure(1, "executor", "x")
    assert is_blocked(1)


# ---------------------------------------------------------------------------
# _collect_tripped
# ---------------------------------------------------------------------------


def test_collect_tripped_empty():
    assert _collect_tripped() == []


def test_collect_tripped_includes_meta():
    record_failure(200, "executor", "bad output")
    record_failure(200, "executor", "bad output")
    record_failure(200, "executor", "bad output")
    tripped = _collect_tripped()
    assert len(tripped) == 1
    entry = tripped[0]
    assert entry["discussion"] == 200
    assert entry["count"] == 3
    assert entry["agent"] == "executor"
    assert entry["reason"] == "bad output"
    assert entry["blocked"] is True


def test_collect_tripped_not_blocked_below_threshold():
    record_failure(201, "executor", "minor")
    entries = _collect_tripped()
    assert len(entries) == 1
    assert entries[0]["blocked"] is False


# ---------------------------------------------------------------------------
# CLI: summary --json
# ---------------------------------------------------------------------------


def test_summary_json_empty():
    rc = main(["summary", "--json"])
    assert rc == 0


def test_summary_json_shape(capsys):
    record_failure(99, "executor", "timed out")
    record_failure(99, "executor", "timed out")
    record_failure(99, "executor", "timed out")
    main(["summary", "--json"])
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert "tripped" in data
    assert "warnings" in data
    assert "threshold" in data
    assert data["threshold"] == DEFAULT_THRESHOLD
    assert isinstance(data["tripped"], list)
    assert len(data["tripped"]) == 1
    entry = data["tripped"][0]
    assert entry["discussion"] == 99
    assert entry["agent"] == "executor"
    assert entry["reason"] == "timed out"


def test_summary_json_empty_shape(capsys):
    main(["summary", "--json"])
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["tripped"] == []
    assert data["warnings"] == []
    assert data["threshold"] == DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# CLI: status (no-arg form)
# ---------------------------------------------------------------------------


def test_status_no_arg_empty(capsys):
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "no active failure counters"


def test_status_no_arg_with_entries(capsys):
    record_failure(300, "executor", "oops")
    record_failure(300, "executor", "oops")
    record_failure(300, "executor", "oops")
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "#300" in out
    assert "3 failures" in out
    assert "[BLOCKED]" in out


def test_status_single_discussion(capsys):
    record_failure(301, "code-reviewer", "lint")
    rc = main(["status", "301"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "1"


# ---------------------------------------------------------------------------
# _age_str helper
# ---------------------------------------------------------------------------


def test_age_str_none():
    assert _age_str(None) == ""


def test_age_str_recent():
    ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    result = _age_str(ts)
    assert "s ago" in result or "m ago" in result


# ---------------------------------------------------------------------------
# expire_stale — AC#1: age-based expiry
# ---------------------------------------------------------------------------


def _stale_ts(days_ago: int = STALE_BREAKER_DAYS + 1) -> str:
    """Return an ISO timestamp that is old enough to be age-stale."""
    return (
        (datetime.now(timezone.utc) - timedelta(days=days_ago))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _fresh_ts(days_ago: float = 0.5) -> str:
    """Return a recent ISO timestamp (within STALE_BREAKER_DAYS)."""
    return (
        (datetime.now(timezone.utc) - timedelta(days=days_ago))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _trip(discussion: int, updated_at: str | None = None, *, agent: str = "executor") -> None:
    """Create a tripped breaker for *discussion* with an arbitrary updated_at."""
    for _ in range(DEFAULT_THRESHOLD):
        record_failure(discussion, agent, "test-reason")
    if updated_at is not None:
        # Overwrite the metadata's updated_at to simulate age.
        meta = cb_mod._bb.read(f"failures_meta/{discussion}") or {}
        meta["updated_at"] = updated_at
        cb_mod._bb.write(f"failures_meta/{discussion}", meta, updated_by="test")


class TestExpireStaleAge:
    """AC#1 — stale breaker auto-expires by age."""

    def test_age_stale_breaker_expires(self):
        _trip(500, _stale_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="open"):
            results = expire_stale()
        assert len(results) == 1
        assert results[0]["discussion"] == 500
        assert results[0]["reason"] == "age"
        assert not is_blocked(500)

    def test_missing_ts_no_meta_open_discussion_held(self):
        """Missing updated_at + open Discussion must be HELD (safety fix)."""
        _trip(501)
        # Remove metadata so updated_at is None
        cb_mod._bb.delete("failures_meta/501")
        with patch.object(cb_mod, "_discussion_state", return_value="open"):
            results = expire_stale()
        assert results == [], f"Expected hold but got: {results}"
        assert is_blocked(501)


# ---------------------------------------------------------------------------
# AC#2 — dead Discussion auto-expires
# ---------------------------------------------------------------------------


class TestExpireStaleDeadDiscussion:
    def test_closed_discussion_expires(self):
        _trip(502, _fresh_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="closed"):
            results = expire_stale()
        assert len(results) == 1
        assert results[0]["reason"] == "closed"
        assert not is_blocked(502)

    def test_absent_discussion_expires(self):
        _trip(503, _fresh_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="absent"):
            results = expire_stale()
        assert results[0]["reason"] == "absent"
        assert not is_blocked(503)


# ---------------------------------------------------------------------------
# AC#3 — fresh + active breaker is HELD
# ---------------------------------------------------------------------------


class TestFreshActiveBreakerHeld:
    def test_fresh_open_breaker_not_expired(self):
        _trip(504, _fresh_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="open"):
            results = expire_stale()
        assert results == []
        assert is_blocked(504)

    def test_fresh_unknown_breaker_not_expired(self):
        """Fail-safe: unknown state counts as active — hold the breaker."""
        _trip(505, _fresh_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="unknown"):
            results = expire_stale()
        assert results == []
        assert is_blocked(505)


# ---------------------------------------------------------------------------
# AC#4 — offline safety
# ---------------------------------------------------------------------------


class TestOfflineSafety:
    def test_offline_lookup_holds_fresh_breaker(self):
        """When lookup raises, fresh breaker is held."""
        _trip(506, _fresh_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="unknown"):
            results = expire_stale()
        assert results == []
        assert is_blocked(506)

    def test_offline_age_stale_still_expires(self):
        """Age-based expiry works even when Discussion lookup returns unknown."""
        _trip(507, _stale_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="unknown"):
            results = expire_stale()
        assert len(results) == 1
        assert results[0]["reason"] == "age"
        assert not is_blocked(507)


# ---------------------------------------------------------------------------
# Missing-timestamp safety (safety fix: missing ts + open -> HOLD)
# ---------------------------------------------------------------------------


class TestMissingTimestampSafety:
    """Breaker with no updated_at must HOLD when Discussion is open/unknown.

    Only a definitively closed or absent Discussion may trigger expiry for a
    breaker that has no parseable timestamp.
    """

    def _trip_no_ts(self, disc: int) -> None:
        """Trip a breaker and then delete its metadata so updated_at is absent."""
        _trip(disc)
        cb_mod._bb.delete(f"failures_meta/{disc}")

    def test_missing_ts_open_discussion_held(self):
        """Missing timestamp + open Discussion -> HOLD."""
        self._trip_no_ts(520)
        with patch.object(cb_mod, "_discussion_state", return_value="open"):
            results = expire_stale()
        assert results == [], f"Expected hold but got: {results}"
        assert is_blocked(520)

    def test_missing_ts_unknown_discussion_held(self):
        """Missing timestamp + unknown Discussion -> HOLD (fail-safe)."""
        self._trip_no_ts(521)
        with patch.object(cb_mod, "_discussion_state", return_value="unknown"):
            results = expire_stale()
        assert results == [], f"Expected hold but got: {results}"
        assert is_blocked(521)

    def test_missing_ts_closed_discussion_expired(self):
        """Missing timestamp + closed Discussion -> EXPIRED."""
        self._trip_no_ts(522)
        with patch.object(cb_mod, "_discussion_state", return_value="closed"):
            results = expire_stale()
        assert len(results) == 1, f"Expected 1 expiry but got: {results}"
        assert results[0]["reason"] == "closed"
        assert not is_blocked(522)

    def test_missing_ts_absent_discussion_expired(self):
        """Missing timestamp + absent Discussion -> EXPIRED."""
        self._trip_no_ts(523)
        with patch.object(cb_mod, "_discussion_state", return_value="absent"):
            results = expire_stale()
        assert len(results) == 1, f"Expected 1 expiry but got: {results}"
        assert results[0]["reason"] == "absent"
        assert not is_blocked(523)


# ---------------------------------------------------------------------------
# AC#5 — overall health report flips
# ---------------------------------------------------------------------------


class TestHealthReportFlips:
    def test_stale_only_breakers_give_green(self, tmp_path):
        """After expiry, health check sees no active breakers → ok:true."""
        import backend.health_report as hr_mod
        from backend.health_report import check_circuit_breakers

        # Stale + closed discussion
        _trip(508, _stale_ts())

        def _fake_state(n):
            return "closed"

        with patch.object(cb_mod, "_discussion_state", side_effect=_fake_state):
            r = check_circuit_breakers()

        assert r["ok"] is True

    def test_fresh_active_breaker_gives_red(self):
        """A fresh + open breaker still fails the check."""
        import backend.health_report as hr_mod
        from backend.health_report import check_circuit_breakers

        _trip(509, _fresh_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="open"):
            r = check_circuit_breakers()
        assert r["ok"] is False
        assert "#509" in r["detail"]


# ---------------------------------------------------------------------------
# AC#6 — idempotent
# ---------------------------------------------------------------------------


class TestIdempotent:
    def test_second_run_empty(self):
        _trip(510, _stale_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="closed"):
            first = expire_stale()
            second = expire_stale()
        assert len(first) == 1
        assert second == []


# ---------------------------------------------------------------------------
# AC#7 — dry_run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_clear(self):
        _trip(511, _stale_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="absent"):
            results = expire_stale(dry_run=True)
        assert len(results) == 1
        # Breaker should still be tripped
        assert is_blocked(511)

    def test_dry_run_followed_by_real_run(self):
        _trip(512, _stale_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="closed"):
            expire_stale(dry_run=True)
            real = expire_stale()
        assert len(real) == 1
        assert not is_blocked(512)


# ---------------------------------------------------------------------------
# CLI: expire subcommand
# ---------------------------------------------------------------------------


class TestExpireCli:
    def test_expire_cli_no_stale(self, capsys):
        rc = main(["expire"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no stale" in out

    def test_expire_cli_with_stale(self, capsys):
        _trip(513, _stale_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="absent"):
            rc = main(["expire"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "#513" in out
        assert "1 breaker" in out

    def test_expire_cli_dry_run(self, capsys):
        _trip(514, _stale_ts())
        with patch.object(cb_mod, "_discussion_state", return_value="closed"):
            rc = main(["expire", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        # Breaker must still be active
        assert is_blocked(514)
