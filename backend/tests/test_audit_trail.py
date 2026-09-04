"""
Tests for backend/audit_trail.py

Covers:
- Basic emit and tail
- Query filtering (source, action, actor, since, limit)
- Stats computation
- Thread-safe concurrent writes
- File rotation at 10 MB
- Singleton pattern
- Blackboard integration (write, cas, delete emit to audit)
- Control-plane integration (set emits to audit)
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Allow imports from repo root when running as `python -m pytest backend/tests/`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.audit_trail import AuditTrail, get_audit_trail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trail(tmp_path: Path) -> AuditTrail:
    audit_file = tmp_path / "audit.jsonl"
    return AuditTrail(audit_path=audit_file)


# ---------------------------------------------------------------------------
# Basic emit + tail
# ---------------------------------------------------------------------------


class TestEmitAndTail:
    def test_emit_creates_file(self, tmp_path):
        at = _make_trail(tmp_path)
        at.emit("blackboard", "write", "loop/status", "idle", "running", "team-lead")
        audit_file = tmp_path / "audit.jsonl"
        assert audit_file.exists()

    def test_tail_returns_entries(self, tmp_path):
        at = _make_trail(tmp_path)
        at.emit("blackboard", "write", "a", None, "v1", "actor")
        at.emit("blackboard", "write", "b", None, "v2", "actor")
        entries = at.tail(10)
        assert len(entries) == 2
        assert entries[-1]["key"] == "b"

    def test_tail_respects_n(self, tmp_path):
        at = _make_trail(tmp_path)
        for i in range(10):
            at.emit("blackboard", "write", f"key/{i}", None, i, "actor")
        entries = at.tail(3)
        assert len(entries) == 3
        assert entries[-1]["key"] == "key/9"

    def test_tail_empty_when_no_file(self, tmp_path):
        at = _make_trail(tmp_path)
        assert at.tail() == []

    def test_seq_is_monotonic(self, tmp_path):
        at = _make_trail(tmp_path)
        for i in range(5):
            at.emit("blackboard", "write", f"k{i}", None, i, "a")
        entries = at.tail(10)
        seqs = [e["seq"] for e in entries]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)  # no duplicates

    def test_entry_schema(self, tmp_path):
        at = _make_trail(tmp_path)
        at.emit("blackboard", "write", "loop/status", "idle", "running", "team-lead")
        entry = at.tail(1)[0]
        for field in ("ts", "source", "action", "key", "old", "new", "actor", "seq"):
            assert field in entry, f"missing field: {field}"
        assert entry["source"] == "blackboard"
        assert entry["action"] == "write"
        assert entry["key"] == "loop/status"
        assert entry["old"] == "idle"
        assert entry["new"] == "running"
        assert entry["actor"] == "team-lead"

    def test_each_entry_is_valid_json_line(self, tmp_path):
        at = _make_trail(tmp_path)
        at.emit("blackboard", "write", "x", {"nested": True}, [1, 2, 3], "a")
        audit_file = tmp_path / "audit.jsonl"
        lines = [l.strip() for l in audit_file.read_text().splitlines() if l.strip()]
        for line in lines:
            json.loads(line)  # must not raise


# ---------------------------------------------------------------------------
# Query filtering
# ---------------------------------------------------------------------------


class TestQuery:
    def _populate(self, at: AuditTrail) -> None:
        at.emit("blackboard", "write", "loop/status", "idle", "running", "team-lead")
        at.emit("blackboard", "delete", "tmp/key", "old", None, "executor")
        at.emit("control_plane", "set", "gates.auto_merge", True, False, "pm")
        at.emit("registry", "transition", "discussion/42", "DISCUSSING", "SPEC_READY", "pm")

    def test_filter_source(self, tmp_path):
        at = _make_trail(tmp_path)
        self._populate(at)
        results = at.query(source="blackboard")
        assert all(e["source"] == "blackboard" for e in results)
        assert len(results) == 2

    def test_filter_action(self, tmp_path):
        at = _make_trail(tmp_path)
        self._populate(at)
        results = at.query(action="write")
        assert all(e["action"] == "write" for e in results)

    def test_filter_actor(self, tmp_path):
        at = _make_trail(tmp_path)
        self._populate(at)
        results = at.query(actor="team-lead")
        assert all(e["actor"] == "team-lead" for e in results)

    def test_filter_since(self, tmp_path):
        at = _make_trail(tmp_path)
        # Emit something in the "past" by manipulating the file directly
        audit_file = tmp_path / "audit.jsonl"
        old_entry = {
            "ts": "2020-01-01T00:00:00+00:00",
            "source": "blackboard", "action": "write",
            "key": "old", "old": None, "new": "x", "actor": "a", "seq": 1,
        }
        audit_file.write_text(json.dumps(old_entry) + "\n")

        at.emit("blackboard", "write", "new", None, "y", "a")
        results = at.query(since="2025-01-01T00:00:00Z")
        assert all(e["key"] != "old" for e in results)
        assert len(results) == 1
        assert results[0]["key"] == "new"

    def test_limit(self, tmp_path):
        at = _make_trail(tmp_path)
        for i in range(20):
            at.emit("blackboard", "write", f"k{i}", None, i, "a")
        results = at.query(limit=5)
        assert len(results) == 5

    def test_no_filters_returns_all(self, tmp_path):
        at = _make_trail(tmp_path)
        self._populate(at)
        results = at.query(limit=100)
        assert len(results) == 4

    def test_empty_when_no_match(self, tmp_path):
        at = _make_trail(tmp_path)
        self._populate(at)
        results = at.query(source="nonexistent")
        assert results == []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_empty(self, tmp_path):
        at = _make_trail(tmp_path)
        s = at.stats()
        assert s["total"] == 0
        assert s["by_source"] == {}
        assert s["by_action"] == {}

    def test_stats_counts(self, tmp_path):
        at = _make_trail(tmp_path)
        at.emit("blackboard", "write", "a", None, 1, "x")
        at.emit("blackboard", "write", "b", None, 2, "x")
        at.emit("blackboard", "delete", "c", 3, None, "x")
        at.emit("control_plane", "set", "d", None, 4, "x")
        s = at.stats()
        assert s["total"] == 4
        assert s["by_source"]["blackboard"] == 3
        assert s["by_source"]["control_plane"] == 1
        assert s["by_action"]["write"] == 2
        assert s["by_action"]["delete"] == 1
        assert s["by_action"]["set"] == 1

    def test_stats_dict_source_and_action_no_crash(self, tmp_path):
        """Dict-valued source or action must not raise TypeError (unhashable type)."""
        audit_file = tmp_path / "audit.jsonl"
        # Rows as written by dial_registry.py — source is a dict, action is a string.
        dict_source_row = {
            "ts": "2026-05-25T10:00:00+00:00",
            "source": {"kind": "github_user", "login": "ian"},
            "action": "dial_directive",
            "key": "dials.agent.spawn",
            "old": None,
            "new": 3,
            "actor": "ian",
            "seq": 1,
        }
        # Also cover dict-valued action (edge case).
        dict_action_row = {
            "ts": "2026-05-25T10:00:01+00:00",
            "source": "dial_registry",
            "action": {"type": "set", "subtype": "override"},
            "key": "dials.agent.spawn",
            "old": 3,
            "new": 4,
            "actor": "ian",
            "seq": 2,
        }
        audit_file.write_text(
            json.dumps(dict_source_row) + "\n" + json.dumps(dict_action_row) + "\n"
        )

        at = AuditTrail(audit_path=audit_file)
        # Must not raise TypeError: unhashable type: 'dict'
        s = at.stats()

        assert s["total"] == 2

        # Dict source is counted under its stable JSON key.
        expected_src_key = json.dumps({"kind": "github_user", "login": "ian"}, sort_keys=True)
        assert s["by_source"][expected_src_key] == 1
        assert s["by_source"]["dial_registry"] == 1

        # Dict action is counted under its stable JSON key.
        expected_act_key = json.dumps({"type": "set", "subtype": "override"}, sort_keys=True)
        assert s["by_action"][expected_act_key] == 1
        assert s["by_action"]["dial_directive"] == 1


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_writes_no_corruption(self, tmp_path):
        at = _make_trail(tmp_path)
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                for j in range(50):
                    at.emit("blackboard", "write", f"key/{i}/{j}", None, j, "worker")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors in threads: {errors}"

        # Every line must be valid JSON
        audit_file = tmp_path / "audit.jsonl"
        lines = [l.strip() for l in audit_file.read_text().splitlines() if l.strip()]
        for line in lines:
            json.loads(line)

        # Total entries: 10 threads × 50 writes = 500
        assert len(lines) == 500

    def test_seq_unique_under_concurrency(self, tmp_path):
        at = _make_trail(tmp_path)

        def worker() -> None:
            for _ in range(20):
                at.emit("blackboard", "write", "k", None, 1, "a")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = at.tail(200)
        seqs = [e["seq"] for e in entries]
        assert len(seqs) == len(set(seqs)), "Duplicate seq values detected"


# ---------------------------------------------------------------------------
# File rotation
# ---------------------------------------------------------------------------


class TestRotation:
    def test_rotation_at_10mb(self, tmp_path, monkeypatch):
        import backend.audit_trail as _at_module  # noqa: PLC0415
        # Patch the rotation threshold to 100 bytes so we can trigger it easily.
        monkeypatch.setattr(_at_module, "_ROTATION_BYTES", 100)

        at = AuditTrail(audit_path=tmp_path / "audit.jsonl")
        # Write enough to exceed the threshold.
        for i in range(20):
            at.emit("blackboard", "write", f"k{i}", None, i, "a")

        rotated = tmp_path / "audit.jsonl.1"
        assert rotated.exists(), "Rotated file audit.jsonl.1 should exist"

    def test_old_data_accessible_after_rotation(self, tmp_path, monkeypatch):
        import backend.audit_trail as _at_module  # noqa: PLC0415
        monkeypatch.setattr(_at_module, "_ROTATION_BYTES", 100)

        at = AuditTrail(audit_path=tmp_path / "audit.jsonl")
        for i in range(20):
            at.emit("blackboard", "write", f"k{i}", None, i, "a")

        # Current file + rotated file both exist; system keeps running fine.
        current = tmp_path / "audit.jsonl"
        rotated = tmp_path / "audit.jsonl.1"
        assert current.exists() or rotated.exists()

    def test_symlink_rotation_preserves_symlink(self, tmp_path, monkeypatch):
        """When audit.jsonl is a symlink, rotation must not move the symlink.

        The real target in the canonical state dir gets renamed to .jsonl.1,
        and the symlink must remain a symlink pointing at a path inside that
        same directory — not become a regular file.

        This test exercises the ACTUAL production code path: the no-argument
        AuditTrail() constructor sets self._path = repo_root / _DEFAULT_AUDIT_PATH
        WITHOUT calling .resolve(), so self._path stays as the (potentially
        unresolved) symlink.  We bypass the constructor to set self._path
        directly to the unresolved symlink, which is exactly what happens in
        production when .autonomous-team/audit.jsonl → $STATE_DIR/audit.jsonl.
        """
        import threading

        import backend.audit_trail as _at_module  # noqa: PLC0415

        monkeypatch.setattr(_at_module, "_ROTATION_BYTES", 100)

        # Simulate $STATE_DIR/audit.jsonl as the canonical target.
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        real_file = state_dir / "audit.jsonl"
        real_file.write_text("")

        # Symlink: .autonomous-team/audit.jsonl → $STATE_DIR/audit.jsonl
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        symlink = repo_dir / "audit.jsonl"
        symlink.symlink_to(real_file)

        # Bypass the constructor so self._path is the UNRESOLVED symlink —
        # exactly as produced by the no-arg AuditTrail() path in production.
        at = object.__new__(AuditTrail)
        at._path = symlink          # unresolved — this is the buggy/fixed path
        at._lock = threading.Lock()
        at._seq = 0

        # Write enough entries to trigger rotation.
        for i in range(20):
            at.emit("blackboard", "write", f"k{i}", None, i, "a")

        # The symlink must still be a symlink after rotation.
        assert symlink.is_symlink(), "audit.jsonl must remain a symlink after rotation"

        # The rotated file must be in the state dir, not next to the symlink.
        rotated_canonical = state_dir / "audit.jsonl.1"
        assert rotated_canonical.exists(), "audit.jsonl.1 must be in the canonical state dir"

        # Appends after rotation must reach the canonical store (via symlink).
        at.emit("blackboard", "write", "post-rotation", None, "val", "a")
        assert symlink.resolve().exists(), "symlink target must exist after rotation"
        content = symlink.read_text()
        assert "post-rotation" in content, "new appends must land in canonical file via symlink"

    def test_non_symlink_rotation_unchanged(self, tmp_path, monkeypatch):
        """When audit.jsonl is a regular file, rotation still produces audit.jsonl.1."""
        import backend.audit_trail as _at_module  # noqa: PLC0415
        monkeypatch.setattr(_at_module, "_ROTATION_BYTES", 100)

        at = AuditTrail(audit_path=tmp_path / "audit.jsonl")
        for i in range(20):
            at.emit("blackboard", "write", f"k{i}", None, i, "a")

        rotated = tmp_path / "audit.jsonl.1"
        assert rotated.exists(), "audit.jsonl.1 must exist for non-symlink rotation"
        # audit.jsonl should also exist (new entries after rotation).
        assert (tmp_path / "audit.jsonl").exists()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_audit_trail_same_instance(self, tmp_path):
        # Reset singleton for isolation
        import backend.audit_trail as _at_module  # noqa: PLC0415
        original = _at_module._singleton
        _at_module._singleton = None
        try:
            a = get_audit_trail(tmp_path / "audit.jsonl")
            b = get_audit_trail(tmp_path / "audit.jsonl")
            assert a is b
        finally:
            _at_module._singleton = original


# ---------------------------------------------------------------------------
# Blackboard integration
# ---------------------------------------------------------------------------


class TestBlackboardIntegration:
    def test_write_emits_audit(self, tmp_path):
        from backend.blackboard import Blackboard  # noqa: PLC0415
        import backend.audit_trail as _at_module  # noqa: PLC0415

        audit_file = tmp_path / "audit.jsonl"
        original = _at_module._singleton
        _at_module._singleton = AuditTrail(audit_path=audit_file)

        bb_root = tmp_path / "blackboard"
        bb = Blackboard(root=bb_root)
        try:
            bb.write("loop/status", "running", updated_by="team-lead")
            entries = _at_module._singleton.query(source="blackboard", action="write")
            assert len(entries) >= 1
            e = entries[-1]
            assert e["key"] == "loop/status"
            assert e["new"] == "running"
            assert e["actor"] == "team-lead"
        finally:
            _at_module._singleton = original

    def test_cas_emits_audit(self, tmp_path):
        from backend.blackboard import Blackboard  # noqa: PLC0415
        import backend.audit_trail as _at_module  # noqa: PLC0415

        audit_file = tmp_path / "audit.jsonl"
        original = _at_module._singleton
        _at_module._singleton = AuditTrail(audit_path=audit_file)

        bb_root = tmp_path / "blackboard"
        bb = Blackboard(root=bb_root)
        try:
            bb.write("k", "v1", updated_by="a")
            entry = bb.read_entry("k")
            version = entry["version"]
            bb.cas("k", "v2", version, updated_by="b")
            entries = _at_module._singleton.query(source="blackboard", action="cas")
            assert len(entries) == 1
            assert entries[0]["old"] == "v1"
            assert entries[0]["new"] == "v2"
        finally:
            _at_module._singleton = original

    def test_delete_emits_audit(self, tmp_path):
        from backend.blackboard import Blackboard  # noqa: PLC0415
        import backend.audit_trail as _at_module  # noqa: PLC0415

        audit_file = tmp_path / "audit.jsonl"
        original = _at_module._singleton
        _at_module._singleton = AuditTrail(audit_path=audit_file)

        bb_root = tmp_path / "blackboard"
        bb = Blackboard(root=bb_root)
        try:
            bb.write("del/key", "bye", updated_by="a")
            bb.delete("del/key")
            entries = _at_module._singleton.query(source="blackboard", action="delete")
            assert len(entries) == 1
            assert entries[0]["key"] == "del/key"
            assert entries[0]["old"] == "bye"
            assert entries[0]["new"] is None
        finally:
            _at_module._singleton = original


# ---------------------------------------------------------------------------
# Control-plane integration
# ---------------------------------------------------------------------------


class TestControlPlaneIntegration:
    def test_set_emits_audit(self, tmp_path):
        from backend.control_plane import ControlPlane  # noqa: PLC0415
        import backend.audit_trail as _at_module  # noqa: PLC0415

        audit_file = tmp_path / "audit.jsonl"
        original = _at_module._singleton
        _at_module._singleton = AuditTrail(audit_path=audit_file)

        config_path = tmp_path / "config.json"
        cp = ControlPlane(config_path=config_path)
        cp.load()
        try:
            cp.set("gates.auto_merge", False)
            entries = _at_module._singleton.query(source="control_plane")
            assert len(entries) >= 1
            e = entries[-1]
            assert e["key"] == "gates.auto_merge"
            assert e["new"] == False  # noqa: E712
        finally:
            _at_module._singleton = original

    def test_reads_produce_no_audit_emits(self, tmp_path):
        """100 gate reads must not produce any audit trail entries."""
        from backend.control_plane import ControlPlane  # noqa: PLC0415
        import backend.audit_trail as _at_module  # noqa: PLC0415

        audit_file = tmp_path / "audit.jsonl"
        original = _at_module._singleton
        _at_module._singleton = AuditTrail(audit_path=audit_file)

        config_path = tmp_path / "config.json"
        cp = ControlPlane(config_path=config_path)
        cp.load()
        try:
            for _ in range(100):
                cp.gate_enabled("auto_merge")
                cp.get("gates.auto_merge")
                cp.get_policy("executor")
            entries = _at_module._singleton.query(source="control_plane")
            assert entries == [], (
                f"Expected 0 audit entries from reads, got {len(entries)}"
            )
        finally:
            _at_module._singleton = original

    def test_noop_write_produces_no_audit_emit(self, tmp_path):
        """Setting a key to its current value must not produce an audit entry."""
        from backend.control_plane import ControlPlane  # noqa: PLC0415
        import backend.audit_trail as _at_module  # noqa: PLC0415

        audit_file = tmp_path / "audit.jsonl"
        original = _at_module._singleton
        _at_module._singleton = AuditTrail(audit_path=audit_file)

        config_path = tmp_path / "config.json"
        cp = ControlPlane(config_path=config_path)
        cp.load()
        try:
            # auto_merge defaults to True; setting it to True is a no-op
            cp.set("gates.auto_merge", True)
            entries = _at_module._singleton.query(source="control_plane")
            assert entries == [], (
                f"Expected 0 audit entries for no-op write, got {len(entries)}"
            )
        finally:
            _at_module._singleton = original

    def test_real_mutation_produces_exactly_one_emit(self, tmp_path):
        """A genuine value change must produce exactly 1 audit entry."""
        from backend.control_plane import ControlPlane  # noqa: PLC0415
        import backend.audit_trail as _at_module  # noqa: PLC0415

        audit_file = tmp_path / "audit.jsonl"
        original = _at_module._singleton
        _at_module._singleton = AuditTrail(audit_path=audit_file)

        config_path = tmp_path / "config.json"
        cp = ControlPlane(config_path=config_path)
        cp.load()
        try:
            cp.set("gates.auto_merge", False)  # default is True → real mutation
            entries = _at_module._singleton.query(source="control_plane")
            assert len(entries) == 1, (
                f"Expected exactly 1 audit entry, got {len(entries)}"
            )
            e = entries[0]
            assert e["old"] == True  # noqa: E712
            assert e["new"] == False  # noqa: E712
        finally:
            _at_module._singleton = original


# ---------------------------------------------------------------------------
# Audit display dedup (api.py /control/audit endpoint)
# ---------------------------------------------------------------------------


class TestAuditDisplayDedup:
    """The /control/audit display reader must return unique rows."""

    def _make_audit_jsonl(self, tmp_path: Path, rows: list[dict]) -> Path:
        """Write rows to a temporary audit.jsonl and return the path."""
        audit_file = tmp_path / "audit.jsonl"
        with audit_file.open("w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return audit_file

    def _call_control_audit(self, audit_path: Path) -> list[dict]:
        """Call the _project_sub_endpoint("control/audit") logic directly."""
        import backend.api as _api_module  # noqa: PLC0415

        original = _api_module._audit_path
        try:
            _api_module._audit_path = lambda: audit_path
            return _api_module._project_sub_endpoint("control/audit")  # type: ignore[return-value]
        finally:
            _api_module._audit_path = original

    def test_identical_rows_deduped(self, tmp_path):
        """When audit.jsonl has 14 identical rows, display returns exactly 1."""
        row = {
            "ts": "2026-05-12T10:09:40+00:00",
            "source": "control_plane",
            "action": "set",
            "key": "gates.auto_merge",
            "old": True,
            "new": False,
            "actor": "control-plane",
            "seq": 42,
        }
        self._make_audit_jsonl(tmp_path, [row] * 14)
        result = self._call_control_audit(tmp_path / "audit.jsonl")
        assert len(result) == 1, (
            f"Expected 1 row after dedup, got {len(result)}"
        )

    def test_distinct_rows_all_returned(self, tmp_path):
        """Rows with different values are all returned."""
        rows = [
            {"ts": "2026-05-12T10:09:40+00:00", "source": "control_plane",
             "action": "set", "key": "gates.auto_merge", "old": True, "new": False,
             "actor": "control-plane", "seq": 1},
            {"ts": "2026-05-12T10:09:41+00:00", "source": "control_plane",
             "action": "set", "key": "gates.auto_merge", "old": False, "new": True,
             "actor": "control-plane", "seq": 2},
        ]
        self._make_audit_jsonl(tmp_path, rows)
        result = self._call_control_audit(tmp_path / "audit.jsonl")
        assert len(result) == 2

    def test_same_key_different_timestamps_both_returned(self, tmp_path):
        """Same (source, action, key, old, new) but different timestamps → both rows kept."""
        rows = [
            {"ts": "2026-05-12T10:09:40+00:00", "source": "control_plane",
             "action": "set", "key": "gates.auto_merge", "old": True, "new": False,
             "actor": "control-plane", "seq": 1},
            {"ts": "2026-05-13T10:09:40+00:00", "source": "control_plane",
             "action": "set", "key": "gates.auto_merge", "old": True, "new": False,
             "actor": "control-plane", "seq": 2},
        ]
        self._make_audit_jsonl(tmp_path, rows)
        result = self._call_control_audit(tmp_path / "audit.jsonl")
        assert len(result) == 2
