"""
Tests for backend/circuit_breaker.py — record_failure, record_success, is_blocked,
transition history emit, and history() query.

Uses an isolated Blackboard (tmp_path) patched into the module-level _bb.
History file is redirected to tmp_path to avoid touching the real repo state.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
import backend.circuit_breaker as cb


@pytest.fixture()
def isolated_bb(tmp_path):
    """Return a fresh Blackboard and patch it into the circuit_breaker module."""
    bb = Blackboard(root=tmp_path / "blackboard")
    with patch.object(cb, "_bb", bb):
        yield bb


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, isolated_bb):
    """Redirect the history file to tmp_path so tests don't write to the repo.

    autouse (D#2478 item 3): two functions in this file used to cross
    DEFAULT_THRESHOLD without requesting this fixture at all
    (test_is_blocked_at_threshold, test_record_failure_independent_discussions)
    and therefore wrote real transition rows into whatever _history_file()
    resolved to for every other test run — this is the exact "test suite
    contaminates the production history file" defect the Discussion measured.
    Making this autouse means every test gets an isolated file, whether or
    not it asks for the returned path.
    """
    history_path = tmp_path / "circuit-breaker-history.jsonl"
    with patch.object(cb, "_HISTORY_FILE", history_path):
        yield history_path


def test_record_failure_increments_count(isolated_bb):
    count = cb.record_failure(42, "executor", "timeout")
    assert count == 1
    count2 = cb.record_failure(42, "executor", "timeout")
    assert count2 == 2


def test_record_success_resets_counter(isolated_bb):
    cb.record_failure(10, "executor", "error")
    cb.record_failure(10, "executor", "error")
    cb.record_success(10)
    assert cb.is_blocked(10) is False
    # confirm key is deleted
    assert isolated_bb.read("failures/10") is None


def test_is_blocked_below_threshold(isolated_bb):
    cb.record_failure(7, "executor", "err")
    cb.record_failure(7, "executor", "err")
    # default threshold is 3 — 2 failures should NOT block
    assert cb.is_blocked(7) is False


def test_is_blocked_at_threshold(isolated_bb):
    for _ in range(3):
        cb.record_failure(99, "executor", "err")
    assert cb.is_blocked(99) is True


def test_is_blocked_with_custom_threshold(isolated_bb):
    cb.record_failure(5, "executor", "err")
    # threshold=1 means one failure is enough
    assert cb.is_blocked(5, threshold=1) is True
    # threshold=2 means one failure is not enough
    assert cb.is_blocked(5, threshold=2) is False


def test_record_failure_independent_discussions(isolated_bb):
    cb.record_failure(1, "executor", "err")
    cb.record_failure(1, "executor", "err")
    cb.record_failure(1, "executor", "err")
    # Discussion #2 must be unaffected
    assert cb.is_blocked(2) is False
    assert cb.is_blocked(1) is True


def test_record_success_on_never_failed_discussion(isolated_bb):
    # Should not raise even if no counter exists
    cb.record_success(999)
    assert cb.is_blocked(999) is False


def test_failure_count_persists_across_calls(isolated_bb):
    cb.record_failure(55, "code-reviewer", "bad output")
    val = isolated_bb.read("failures/55")
    assert val == 1


# ------------------------------------------------------------------
# History emit tests
# ------------------------------------------------------------------


def test_trip_emits_jsonl_line(isolated_history):
    """Crossing the threshold appends one line with correct schema fields."""
    threshold = cb.DEFAULT_THRESHOLD
    # Record enough failures to trip
    for i in range(threshold):
        cb.record_failure(200, "executor", "preflight failed", last_pr=412)

    lines = isolated_history.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["role"] == "executor"
    assert entry["from_state"] == "healthy"
    assert entry["to_state"] == "tripped"
    assert entry["last_pr"] == 412
    assert "timestamp" in entry
    assert "reason" in entry
    assert "context" in entry
    assert "recent_errors" in entry["context"]


def test_reset_emits_jsonl_line(isolated_history):
    """Resetting a tripped circuit appends a reset line."""
    threshold = cb.DEFAULT_THRESHOLD
    for _ in range(threshold):
        cb.record_failure(201, "executor", "lint failed")

    cb.record_success(201, agent="executor", last_pr=413)

    lines = isolated_history.read_text().strip().splitlines()
    assert len(lines) == 2  # trip + reset
    reset_entry = json.loads(lines[1])
    assert reset_entry["from_state"] == "tripped"
    assert reset_entry["to_state"] == "healthy"


def test_no_emit_below_threshold(isolated_history):
    """Failures below threshold must not write any history line."""
    for _ in range(cb.DEFAULT_THRESHOLD - 1):
        cb.record_failure(202, "executor", "err")
    assert not isolated_history.exists()


def test_no_reset_emit_when_not_tripped(isolated_history):
    """record_success on a healthy discussion must not write a history line."""
    cb.record_success(203, agent="executor")
    assert not isolated_history.exists()


def test_history_filters_by_role(isolated_history):
    """history() returns only lines for the requested role."""
    threshold = cb.DEFAULT_THRESHOLD
    for _ in range(threshold):
        cb.record_failure(300, "executor", "err A")
    for _ in range(threshold):
        cb.record_failure(301, "code-reviewer", "err B")

    exec_entries = cb.history("executor")
    assert all(e["role"] == "executor" for e in exec_entries)
    rev_entries = cb.history("code-reviewer")
    assert all(e["role"] == "code-reviewer" for e in rev_entries)


def test_history_limit_honored(isolated_history):
    """history(limit=N) returns at most N entries."""
    # Write 5 trips by manipulating the file directly
    threshold = cb.DEFAULT_THRESHOLD
    for disc in range(400, 405):
        for _ in range(threshold):
            cb.record_failure(disc, "executor", f"err {disc}")

    entries = cb.history("executor", limit=3)
    assert len(entries) == 3


def test_history_unknown_role_returns_empty(isolated_history):
    """history() with an unknown role returns an empty list and exits cleanly."""
    result = cb.history("nonexistent-role-xyz")
    assert result == []


def test_history_unknown_role_no_file():
    """history() returns empty list when the history file doesn't exist yet."""
    with patch.object(cb, "_HISTORY_FILE", Path("/tmp/does-not-exist-cb-history.jsonl")):
        result = cb.history("executor")
    assert result == []


def test_jsonl_atomic_append(isolated_history):
    """Multiple transitions write separate valid JSON lines (no corruption)."""
    import threading

    threshold = cb.DEFAULT_THRESHOLD
    errors: list[Exception] = []

    def trip(disc: int) -> None:
        try:
            for _ in range(threshold):
                cb.record_failure(disc, "executor", f"err {disc}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=trip, args=(500 + i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    lines = isolated_history.read_text().strip().splitlines()
    for line in lines:
        # Every line must be valid JSON
        parsed = json.loads(line)
        assert "role" in parsed
        assert "from_state" in parsed


# ------------------------------------------------------------------
# D#2478 — row identity, no dedup, one resolver, no test leak
# ------------------------------------------------------------------


def test_history_row_has_discussion_and_source_fields(isolated_history):
    """Row identity (item 1): a real threshold crossing writes both the
    breaker's own identifier and a field telling a test write apart from a
    production one."""
    cb.record_failure(777, "executor", "boom")
    cb.record_failure(777, "executor", "boom")
    cb.record_failure(777, "executor", "boom")

    lines = isolated_history.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["discussion"] == 777
    # This test itself runs under pytest, so the row it produced must be
    # stamped as such — PYTEST_CURRENT_TEST is what _append_history checks.
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert entry["source"] == "test"


def test_same_second_trips_produce_distinguishable_rows(isolated_history, monkeypatch):
    """Mutation check — the binding one (item 2).

    Two different breakers tripping in the same second, with the same role
    and reason, must not produce byte-identical rows. Fix the timestamp so
    "same second" isn't left to chance, then trip two different discussions.

    To reproduce the failure direction by hand: comment out the
    ``"discussion": discussion,`` line in `_append_history` and re-run this
    test — it goes red, because the two rows below then differ only in a
    field that no longer exists.
    """
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(cb, "datetime", _FixedDatetime)

    for _ in range(cb.DEFAULT_THRESHOLD):
        cb.record_failure(901, "executor", "same reason")
    for _ in range(cb.DEFAULT_THRESHOLD):
        cb.record_failure(902, "executor", "same reason")

    lines = isolated_history.read_text().strip().splitlines()
    assert len(lines) == 2
    row_a, row_b = json.loads(lines[0]), json.loads(lines[1])

    # Same second, same role, same reason, same trip_count_24h — everything
    # except the breaker identifier really is identical here by construction.
    assert row_a["timestamp"] == row_b["timestamp"]
    assert row_a["role"] == row_b["role"]
    assert row_a["reason"] == row_b["reason"]
    assert row_a["context"] == row_b["context"]

    assert row_a != row_b, "two distinct breakers produced byte-identical rows"
    assert row_a["discussion"] != row_b["discussion"]
    assert {row_a["discussion"], row_b["discussion"]} == {901, 902}


def test_no_dedup_every_crossing_appends_a_row(isolated_history):
    """No dedup (item 5): repeated threshold crossings for the SAME
    discussion, reason and role each append their own row — nothing here
    keys off (role, timestamp, to_state) or any other field subset to
    suppress a "duplicate" write."""
    cb.record_failure(950, "executor", "err")
    cb.record_failure(950, "executor", "err")
    cb.record_failure(950, "executor", "err")  # crosses threshold -> tripped
    cb.record_success(950, agent="executor")  # tripped -> healthy
    cb.record_failure(950, "executor", "err")
    cb.record_failure(950, "executor", "err")
    cb.record_failure(950, "executor", "err")  # crosses threshold again

    lines = isolated_history.read_text().strip().splitlines()
    # trip, reset, trip again — three rows, none suppressed as a "duplicate"
    # of an earlier one even though role/reason repeat.
    assert len(lines) == 3


def test_history_path_resolves_via_state_paths(monkeypatch, tmp_path):
    """One resolver (item 4): with no test override in place, the history
    path _append_history/history() actually use must be the exact same
    object backend.state_paths.CIRCUIT_BREAKER_HISTORY resolves to — not a
    module-relative Path(__file__) computation that can silently diverge
    from it (D#2478 Correction 3)."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    from backend import state_paths

    with patch.object(cb, "_HISTORY_FILE", None):
        assert cb._history_file() == state_paths.CIRCUIT_BREAKER_HISTORY


def test_full_suite_does_not_leak_into_production_history(tmp_path):
    """Test-leak check with a positive control (item 3).

    Runs this entire file as a real pytest subprocess (deselecting only
    this test, to avoid infinite recursion) and confirms it does not touch
    the production circuit-breaker history file that _history_file() falls
    back to when nothing overrides it.

    Positive control: a canary row is written to that same path first and
    read back byte-for-byte unchanged afterward. Without the canary, an
    absent-or-untouched file could just as easily mean "the subprocess
    never ran" as "nothing leaked" — the canary is what proves this check
    actually looked, not merely that the assertion happened to be true.

    This is exactly the case that failed before item 3's autouse fixture
    fix: two tests in this file (test_is_blocked_at_threshold,
    test_record_failure_independent_discussions) used to cross
    DEFAULT_THRESHOLD without requesting isolated_history at all, and wrote
    straight into whatever _history_file() resolved to.
    """
    this_file = Path(__file__).resolve()
    repo_root = this_file.parent.parent
    # Relative to repo_root (== the subprocess's cwd below): pytest's node
    # ids are reported relative to rootdir, and --deselect only matches a
    # node id it computed the same way — an absolute path here silently
    # fails to match, the deselect is a no-op, and this test recurses into
    # itself as a subprocess of a subprocess of a subprocess.
    rel_file = this_file.relative_to(repo_root)
    scratch_state = tmp_path / "state"
    live_path = scratch_state / "circuit-breaker-history.jsonl"
    live_path.parent.mkdir(parents=True, exist_ok=True)

    canary = json.dumps({"discussion": -1, "role": "canary", "note": "D#2478 positive control"}) + "\n"
    live_path.write_text(canary)
    before = live_path.read_text()

    env = dict(os.environ)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = str(scratch_state)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(rel_file),
            "-q",
            "--deselect",
            f"{rel_file}::test_full_suite_does_not_leak_into_production_history",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    after = live_path.read_text()
    assert result.returncode == 0, (
        f"suite failed under AUTONOMOUS_TEAM_STATE_DIR={scratch_state}:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    # Positive control: the canary line is still there, unchanged — proves
    # this check actually read the file rather than finding it absent.
    assert canary in after
    assert after == before, "the test suite wrote into the resolved history file"
