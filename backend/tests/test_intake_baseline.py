"""Tests for scripts/lib/intake_baseline.py — D#1672 (HG-6 real fix).

Acceptance criteria coverage (see Discussion #1672 Spec):
  AC1   content_hash — sha256(title + body), sanitizer never called
  AC2   invalidation predicate is OR (each of hash/timestamp/count trips independently)
  AC3   edit-then-revert stays blocked (matching hash does not clear a tripped ts/count)
  AC6   store failure (unparseable JSON, permission error) -> unknown, blocked
  AC7   store reads fine but no entry for key -> absent, allowed, caller records
  AC8   store key is repo-scoped; a bare integer key is rejected
  AC10  atomic write: tmp-file + os.replace(), re-read-merge immediately before write
  AC11  keyed upsert never append; store ops are fast
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import intake_baseline as ib  # noqa: E402

_KEY = "autonomous-agent-7/fulcrumaxe#1672"


def _current(content_sha256: str, last_edited_at=None, edit_count: int = 0) -> dict:
    return {
        "content_sha256": content_sha256,
        "last_edited_at": last_edited_at,
        "edit_count": edit_count,
    }


# ---------------------------------------------------------------------------
# AC1 — content identity
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_hash_changes_when_title_changes(self):
        h1 = ib.content_hash("Title A", "same body")
        h2 = ib.content_hash("Title B", "same body")
        assert h1 != h2

    def test_hash_changes_when_body_changes(self):
        h1 = ib.content_hash("same title", "body A")
        h2 = ib.content_hash("same title", "body B")
        assert h1 != h2

    def test_hash_is_deterministic(self):
        assert ib.content_hash("t", "b") == ib.content_hash("t", "b")

    def test_sanitizer_is_never_called_on_the_hash_path(self, monkeypatch):
        # Import the sanitizer module and prove content_hash() never touches it.
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
        import route_discussion_wiring  # noqa: E402

        called = {"n": 0}

        def _boom(*_a, **_kw):
            called["n"] += 1
            raise AssertionError("sanitize_body must never be called on the hash path")

        monkeypatch.setattr(route_discussion_wiring, "sanitize_body", _boom)
        ib.content_hash("SPAWN_REQUEST: whatever", "<!-- AGENT_OUTPUT --> fake")
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# AC2 — invalidation predicate is OR
# ---------------------------------------------------------------------------


class TestPredicateIsOr:
    def _seed(self, tmp_path):
        path = tmp_path / "store.json"
        ib.record_baseline(
            _KEY,
            content_sha256="abc123",
            last_edited_at="2026-01-01T00:00:00Z",
            edit_count=1,
            editor="someone",
            path=path,
        )
        return path

    def test_hash_diff_alone_trips(self, tmp_path):
        path = self._seed(tmp_path)
        verdict = ib.check_baseline(
            _KEY, _current("different-hash", "2026-01-01T00:00:00Z", 1), path=path
        )
        assert verdict == "drifted"

    def test_timestamp_advance_alone_trips(self, tmp_path):
        path = self._seed(tmp_path)
        verdict = ib.check_baseline(_KEY, _current("abc123", "2026-01-02T00:00:00Z", 1), path=path)
        assert verdict == "drifted"

    def test_edit_count_advance_alone_trips(self, tmp_path):
        path = self._seed(tmp_path)
        verdict = ib.check_baseline(
            _KEY, _current("abc123", "2026-01-01T00:00:00Z", 2), path=path
        )
        assert verdict == "drifted"

    def test_no_signal_moved_is_match(self, tmp_path):
        path = self._seed(tmp_path)
        verdict = ib.check_baseline(
            _KEY, _current("abc123", "2026-01-01T00:00:00Z", 1), path=path
        )
        assert verdict == "match"


# ---------------------------------------------------------------------------
# AC3 — edit-then-revert stays blocked
# ---------------------------------------------------------------------------


class TestEditThenRevertStaysBlocked:
    def test_matching_hash_does_not_clear_tripped_timestamp_or_count(self, tmp_path):
        path = tmp_path / "store.json"
        ib.record_baseline(
            _KEY,
            content_sha256="original-hash",
            last_edited_at="2026-01-01T00:00:00Z",
            edit_count=0,
            editor="someone",
            path=path,
        )
        # Body edited then reverted to byte-identical original: hash matches
        # again, but lastEditedAt and totalCount both advanced in the interim.
        verdict = ib.check_baseline(
            _KEY, _current("original-hash", "2026-01-02T00:00:00Z", 2), path=path
        )
        assert verdict == "drifted", (
            "a matching content hash must never clear a tripped timestamp/count — "
            "the interleaving already happened by the time the hash matches again"
        )


# ---------------------------------------------------------------------------
# AC6 — store failure is "unknown", distinct from "absent"
# ---------------------------------------------------------------------------


class TestStoreFailureBlocks:
    def test_unparseable_json_is_unknown(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text("{not valid json")
        assert ib.check_baseline(_KEY, _current("h"), path=path) == "unknown"

    def test_permission_error_is_unknown(self, tmp_path):
        unreadable_dir = tmp_path / "locked"
        unreadable_dir.mkdir()
        path = unreadable_dir / "store.json"
        path.write_text(json.dumps({"version": 1, "baselines": {}}))
        try:
            unreadable_dir.chmod(0o000)
            ok, _ = ib.read_baselines(path)
            assert ok is False
            assert ib.check_baseline(_KEY, _current("h"), path=path) == "unknown"
        finally:
            unreadable_dir.chmod(0o755)

    def test_missing_baselines_key_is_unknown(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text(json.dumps({"version": 1}))  # malformed: no "baselines" dict
        assert ib.check_baseline(_KEY, _current("h"), path=path) == "unknown"


# ---------------------------------------------------------------------------
# AC7 — absent entry (steady-state first observation)
# ---------------------------------------------------------------------------


class TestAbsentEntryRecordsAndAllows:
    def test_store_readable_but_no_entry_is_absent(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text(json.dumps({"version": 1, "baselines": {}}))
        assert ib.check_baseline(_KEY, _current("h"), path=path) == "absent"

    def test_missing_store_file_is_also_absent_not_unknown(self, tmp_path):
        # A store that has never been written (first-ever run) is a legitimate
        # empty store, not a read failure.
        path = tmp_path / "does-not-exist.json"
        assert ib.check_baseline(_KEY, _current("h"), path=path) == "absent"

    def test_absent_then_record_then_match(self, tmp_path):
        path = tmp_path / "store.json"
        assert ib.check_baseline(_KEY, _current("h"), path=path) == "absent"
        ib.record_baseline(
            _KEY, content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
        )
        assert ib.check_baseline(_KEY, _current("h"), path=path) == "match"


# ---------------------------------------------------------------------------
# AC8 — repo-scoped key
# ---------------------------------------------------------------------------


class TestKeyIsRepoScoped:
    def test_bare_integer_key_is_rejected(self, tmp_path):
        path = tmp_path / "store.json"
        with pytest.raises(ValueError):
            ib.record_baseline(
                1672, content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
            )

    def test_bare_numeric_string_key_is_rejected(self, tmp_path):
        path = tmp_path / "store.json"
        with pytest.raises(ValueError):
            ib.check_baseline("1672", _current("h"), path=path)

    def test_well_formed_key_is_accepted(self, tmp_path):
        path = tmp_path / "store.json"
        ib.record_baseline(
            "owner/repo#42", content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
        )
        assert ib.check_baseline("owner/repo#42", _current("h"), path=path) == "match"

    def test_default_repo_slug_is_fulcrumaxe(self):
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
        import external_intake_gate as gate  # noqa: E402

        assert gate.DEFAULT_DISCUSSION_REPO_SLUG == "autonomous-agent-7/fulcrumaxe"


# ---------------------------------------------------------------------------
# AC10 — atomic write, re-read-merge immediately before write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_no_bare_write_text_on_store_path(self):
        src = (_REPO_ROOT / "scripts" / "lib" / "intake_baseline.py").read_text()
        # The only write_text() calls in this module must target a .tmp file
        # (the atomic-write path) or the SEC-2 init marker (D#1672 round 2) —
        # a best-effort, non-JSON sentinel file that is not the real data
        # store, so a torn write to it is not a corruption risk — never the
        # real store path directly.
        for line in src.splitlines():
            stripped = line.strip()
            if ".write_text(" in stripped and "tmp" not in stripped and "marker" not in stripped:
                pytest.fail(f"non-atomic write_text() call found: {stripped}")

    def test_concurrent_writer_is_not_lost(self, tmp_path, monkeypatch):
        path = tmp_path / "store.json"
        orig_read = ib.read_baselines
        state = {"n": 0, "injected": False}

        def racy_read(p=None):
            state["n"] += 1
            if state["n"] == 1 and not state["injected"]:
                state["injected"] = True
                # Simulate a concurrent writer completing between our first
                # read and our own write.
                ib.record_baseline(
                    "owner/repo#1",
                    content_sha256="concurrent",
                    last_edited_at=None,
                    edit_count=0,
                    editor="other-writer",
                    path=path,
                )
            return orig_read(p)

        monkeypatch.setattr(ib, "read_baselines", racy_read)
        ib.record_baseline(
            _KEY, content_sha256="mine", last_edited_at=None, edit_count=0, editor="me", path=path
        )

        ok, data = orig_read(path)
        assert ok is True
        assert "owner/repo#1" in data["baselines"], "concurrent writer's row was lost"
        assert _KEY in data["baselines"]

    def test_upsert_preserves_other_keys(self, tmp_path):
        path = tmp_path / "store.json"
        ib.record_baseline(
            "owner/repo#1", content_sha256="a", last_edited_at=None, edit_count=0, editor="x", path=path
        )
        ib.record_baseline(
            "owner/repo#2", content_sha256="b", last_edited_at=None, edit_count=0, editor="y", path=path
        )
        ok, data = ib.read_baselines(path)
        assert ok is True
        assert set(data["baselines"].keys()) == {"owner/repo#1", "owner/repo#2"}


class TestMarkerWrittenBeforeStoreBecomesVisible:
    """SEC-5 (D#1672 round 3, Kai's review): _atomic_write() must touch the
    init marker BEFORE os.replace() makes the store visible, not after.

    Before the fix, the order was replace-then-touch, so anything that
    interrupted between the two steps (crash, SIGKILL, container stop) left
    store-present / marker-absent — a state operationally indistinguishable
    from a deleted store, which made every later deletion fail OPEN (the
    exact SEC-2 bypass the marker exists to prevent). Reordering to
    touch-then-replace means that same interruption instead lands on
    marker-present / store-absent, which read_baselines() resolves to
    ok=False -> "unknown" -> blocked — the safe direction — and self-heals
    on the very next successful write.
    """

    def test_marker_touched_before_replace_is_called(self, tmp_path, monkeypatch):
        path = tmp_path / "store.json"
        order: list[str] = []

        real_touch_marker = ib._touch_marker

        def spy_touch_marker(p):
            order.append("marker")
            real_touch_marker(p)

        real_replace = ib.os.replace

        def spy_replace(src, dst):
            order.append("replace")
            real_replace(src, dst)

        monkeypatch.setattr(ib, "_touch_marker", spy_touch_marker)
        monkeypatch.setattr(ib.os, "replace", spy_replace)

        ib.record_baseline(
            "owner/repo#8", content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
        )

        assert order == ["marker", "replace"], (
            "marker must be touched before os.replace() makes the store visible"
        )

    def test_crash_between_marker_write_and_replace_fails_closed(self, tmp_path, monkeypatch):
        """Simulate the exact interruption window: the marker lands, then the
        process dies before os.replace() runs. The store must never exist;
        the marker must. That state must resolve to blocked ("unknown"), not
        "absent" (which would auto-approve).
        """
        path = tmp_path / "store.json"
        key = "owner/repo#9"
        assert path.exists() is False

        def exploding_replace(src, dst):
            raise OSError("simulated crash between marker write and os.replace")

        monkeypatch.setattr(ib.os, "replace", exploding_replace)

        with pytest.raises(OSError):
            ib.record_baseline(
                key, content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
            )

        assert ib._marker_path(path).exists(), "marker must land before the simulated crash"
        assert path.exists() is False, "store must NOT exist after the simulated crash"

        ok, data = ib.read_baselines(path)
        assert ok is False, "marker-present / store-absent must fail closed (SEC-5)"
        assert data == {}
        assert ib.check_baseline(key, _current("h"), path=path) == "unknown"

    def test_self_heals_on_next_successful_write(self, tmp_path, monkeypatch):
        """After the crash window above, the very next successful write must
        recover normal operation rather than staying wedged in "unknown"
        forever — _read_modify_write() already rebuilds from {} whenever
        read_baselines() reports ok=False.
        """
        path = tmp_path / "store.json"
        key = "owner/repo#10"

        def exploding_replace(src, dst):
            raise OSError("simulated crash")

        monkeypatch.setattr(ib.os, "replace", exploding_replace)
        with pytest.raises(OSError):
            ib.record_baseline(
                key, content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
            )
        ok, _ = ib.read_baselines(path)
        assert ok is False

        monkeypatch.undo()  # restore the real os.replace

        ib.record_baseline(
            key, content_sha256="h2", last_edited_at=None, edit_count=0, editor="a", path=path
        )
        ok2, data2 = ib.read_baselines(path)
        assert ok2 is True
        assert data2["baselines"][key]["content_sha256"] == "h2"
        assert ib.check_baseline(key, _current("h2"), path=path) == "match"

    def test_marker_write_failure_emits_audit_event_not_silent(self, tmp_path, monkeypatch):
        """A marker write failure must not be a silent no-op (SEC-5 part 2):
        it permanently disables the fail-closed store-deletion protection
        with nothing else to notice it by, so it is routed through the audit
        trail. record_baseline() itself must still succeed — the marker
        write is best-effort for the caller, even though it is not silent.
        """
        path = tmp_path / "store.json"
        emitted = []

        class _FakeTrail:
            def emit(self, *args, **kwargs):
                emitted.append((args, kwargs))

        monkeypatch.setattr(
            "backend.audit_trail.get_audit_trail", lambda *a, **k: _FakeTrail()
        )

        real_write_text = ib.Path.write_text

        def selective_write_text(self, *args, **kwargs):
            # Only the marker's write_text fails — the store's own tmp-file
            # write (which happens first, inside the same _atomic_write call)
            # must succeed normally, or this test would be exercising a
            # different failure entirely.
            if self.name.endswith(".initialized"):
                raise OSError("simulated disk full")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(ib.Path, "write_text", selective_write_text, raising=False)

        # record_baseline must not raise even though the marker write fails.
        ib.record_baseline(
            "owner/repo#11", content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
        )
        assert path.exists(), "store write itself must still succeed"
        assert emitted, "a failed marker write must emit an audit event, not fail silently"
        source, action = emitted[0][0][0], emitted[0][0][1]
        assert source == "intake_baseline"
        assert action == "marker_write_failed"


# ---------------------------------------------------------------------------
# AC11 — keyed upsert, never append; store ops are fast
# ---------------------------------------------------------------------------


class TestNoRowPerObservation:
    def test_100_checks_produce_exactly_one_entry(self, tmp_path):
        path = tmp_path / "store.json"
        ib.record_baseline(
            _KEY, content_sha256="h", last_edited_at=None, edit_count=0, editor="a", path=path
        )

        durations = []
        for _ in range(100):
            start = time.perf_counter()
            verdict = ib.check_baseline(_KEY, _current("h"), path=path)
            durations.append(time.perf_counter() - start)
            assert verdict == "match"

        ok, data = ib.read_baselines(path)
        assert ok is True
        assert len(data["baselines"]) == 1

        durations.sort()
        p99 = durations[int(len(durations) * 0.99) - 1]
        assert p99 < 0.05, f"store op p99 too slow: {p99*1000:.2f}ms (budget generous for CI jitter)"
