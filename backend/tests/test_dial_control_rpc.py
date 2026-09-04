"""Tests for backend/rpc/dial_control.py

Covers:
- dial.list returns all registered classes
- dial.set sets a level and writes audit row
- dial.set enforces ceiling (above-ceiling rejected with ValueError)
- dial.set requires auth (unauthenticated source rejected)
- dial.set with TTL records ttl_until in the audit row
- unknown class name rejected
- missing/invalid params rejected
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Fixture: isolated state dir with allowlist that includes dashboard_rpc source
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point AUTONOMOUS_TEAM_STATE_DIR at a temp dir and reload modules."""
    original = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

    import backend.state_paths as _sp
    import backend.dial_registry as _dr

    importlib.reload(_sp)
    importlib.reload(_dr)

    # Write the allowlist with the dashboard_rpc source that dial_control uses
    allowlist = [{"kind": "system", "reason": "dashboard_rpc"}]
    allowlist_path = tmp_path / "dial-directive-allowlist.json"
    allowlist_path.write_text(json.dumps(allowlist) + "\n", encoding="utf-8")

    yield tmp_path

    # Restore
    if original is not None:
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = original
    else:
        os.environ.pop("AUTONOMOUS_TEAM_STATE_DIR", None)
    importlib.reload(_sp)
    importlib.reload(_dr)


@pytest.fixture()
def state_dir_no_auth(tmp_path, monkeypatch):
    """Isolated state dir with an EMPTY allowlist — all set_dial calls refused."""
    original = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

    import backend.state_paths as _sp
    import backend.dial_registry as _dr

    importlib.reload(_sp)
    importlib.reload(_dr)

    # Empty allowlist
    allowlist_path = tmp_path / "dial-directive-allowlist.json"
    allowlist_path.write_text("[]", encoding="utf-8")

    yield tmp_path

    if original is not None:
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = original
    else:
        os.environ.pop("AUTONOMOUS_TEAM_STATE_DIR", None)
    importlib.reload(_sp)
    importlib.reload(_dr)


def _get_handler():
    import importlib
    import backend.rpc.dial_control as dc
    importlib.reload(dc)
    return dc


# ---------------------------------------------------------------------------
# dial.list tests
# ---------------------------------------------------------------------------

class TestDialList:
    def test_returns_all_registered_classes(self, state_dir):
        dc = _get_handler()
        result = dc.handle_list({})
        assert "dials" in result
        names = [d["name"] for d in result["dials"]]
        # All 13 default classes should be present
        assert "agent.spawn" in names
        assert "sandbox.modify" in names
        assert "methodology.change" in names
        assert len(names) == 13

    def test_each_dial_has_required_fields(self, state_dir):
        dc = _get_handler()
        result = dc.handle_list({})
        for dial in result["dials"]:
            assert "name" in dial
            assert "level" in dial
            assert "ceiling" in dial
            assert "active_directives" in dial
            assert "ttl_revert_at" in dial

    def test_ceiling_constraints_respected(self, state_dir):
        dc = _get_handler()
        result = dc.handle_list({})
        by_name = {d["name"]: d for d in result["dials"]}
        # Hardcoded ceilings
        assert by_name["sandbox.modify"]["ceiling"] == 1
        assert by_name["methodology.change"]["ceiling"] == 2
        assert by_name["external.system"]["ceiling"] == 2

    def test_active_directives_zero_initially(self, state_dir):
        dc = _get_handler()
        result = dc.handle_list({})
        for dial in result["dials"]:
            assert dial["active_directives"] == 0

    def test_ttl_revert_at_null_initially(self, state_dir):
        dc = _get_handler()
        result = dc.handle_list({})
        for dial in result["dials"]:
            assert dial["ttl_revert_at"] is None


# ---------------------------------------------------------------------------
# dial.set tests
# ---------------------------------------------------------------------------

class TestDialSet:
    def test_set_level_succeeds(self, state_dir):
        dc = _get_handler()
        result = dc.handle_set({"name": "agent.spawn", "level": 3})
        assert result["name"] == "agent.spawn"
        assert result["level"] == 3
        assert result["ceiling"] == 5

    def test_set_level_reflected_in_list(self, state_dir):
        dc = _get_handler()
        dc.handle_set({"name": "agent.spawn", "level": 2})
        list_result = dc.handle_list({})
        by_name = {d["name"]: d for d in list_result["dials"]}
        assert by_name["agent.spawn"]["level"] == 2

    def test_set_writes_audit_row(self, state_dir):
        dc = _get_handler()
        dc.handle_set({"name": "agent.spawn", "level": 3})
        audit_path = state_dir / "audit.jsonl"
        assert audit_path.exists()
        lines = [l for l in audit_path.read_text().splitlines() if l.strip()]
        rows = [json.loads(l) for l in lines]
        dial_rows = [r for r in rows if r.get("kind") == "dial_change"]
        assert len(dial_rows) >= 1
        last = dial_rows[-1]
        assert last["class"] == "agent.spawn"
        assert last["new_level"] == 3
        assert last["source"] == {"kind": "system", "reason": "dashboard_rpc"}

    def test_above_ceiling_rejected(self, state_dir):
        dc = _get_handler()
        with pytest.raises(ValueError, match="ceiling_exceeded"):
            dc.handle_set({"name": "sandbox.modify", "level": 2})

    def test_above_ceiling_writes_audit_rejection(self, state_dir):
        dc = _get_handler()
        try:
            dc.handle_set({"name": "sandbox.modify", "level": 2})
        except ValueError:
            pass
        audit_path = state_dir / "audit.jsonl"
        lines = [l for l in audit_path.read_text().splitlines() if l.strip()]
        rows = [json.loads(l) for l in lines]
        rejection_rows = [r for r in rows if r.get("kind") == "dial_directive_rejected"]
        assert any(r["class"] == "sandbox.modify" for r in rejection_rows)

    def test_unauthenticated_source_rejected(self, state_dir_no_auth):
        dc = _get_handler()
        with pytest.raises(ValueError):
            # With empty allowlist, dashboard_rpc source is rejected
            dc.handle_set({"name": "agent.spawn", "level": 3})

    def test_unknown_class_rejected(self, state_dir):
        dc = _get_handler()
        with pytest.raises(ValueError):
            dc.handle_set({"name": "nonexistent.class", "level": 1})

    def test_missing_name_rejected(self, state_dir):
        dc = _get_handler()
        with pytest.raises(ValueError, match="name"):
            dc.handle_set({"level": 3})

    def test_missing_level_rejected(self, state_dir):
        dc = _get_handler()
        with pytest.raises(ValueError, match="level"):
            dc.handle_set({"name": "agent.spawn"})

    def test_invalid_level_type_rejected(self, state_dir):
        dc = _get_handler()
        with pytest.raises(ValueError, match="level"):
            dc.handle_set({"name": "agent.spawn", "level": "three"})

    def test_set_with_ttl_for_today(self, state_dir):
        dc = _get_handler()
        result = dc.handle_set({"name": "agent.spawn", "level": 3, "ttl": "for-today"})
        assert result["level"] == 3
        # Audit row should have a ttl_until set
        audit_path = state_dir / "audit.jsonl"
        lines = [l for l in audit_path.read_text().splitlines() if l.strip()]
        rows = [json.loads(l) for l in lines]
        dial_rows = [r for r in rows if r.get("kind") == "dial_change"]
        last = dial_rows[-1]
        assert last["ttl_until"] is not None

    def test_set_level_1_on_sandbox_modify_succeeds(self, state_dir):
        """sandbox.modify ceiling=1, so level=1 is the only valid value."""
        dc = _get_handler()
        result = dc.handle_set({"name": "sandbox.modify", "level": 1})
        assert result["level"] == 1
        assert result["ceiling"] == 1

    def test_hash_chain_on_audit_rows(self, state_dir):
        """Each audit row's prev_hash should match SHA-256 of the previous row."""
        import hashlib
        dc = _get_handler()
        dc.handle_set({"name": "agent.spawn", "level": 3})
        dc.handle_set({"name": "agent.spawn", "level": 4})
        audit_path = state_dir / "audit.jsonl"
        lines = [l.encode() for l in audit_path.read_text().splitlines() if l.strip()]
        assert len(lines) >= 2
        # Second row's prev_hash should match SHA-256 of the first row
        row2 = json.loads(lines[1])
        expected_hash = hashlib.sha256(lines[0]).hexdigest()
        assert row2["prev_hash"] == expected_hash


# ---------------------------------------------------------------------------
# Fresh-process mutation-lands tests (D#1883 Spec items 7-9).
#
# In-process importlib.reload() is not sufficient evidence here — STATE_DIR
# is resolved once via os.environ.get() and it's easy for a reload trick to
# accidentally look like a fresh read even when it isn't testing what a
# second, independent `python3` invocation (a real CLI call, a real
# dashboard RPC handler restart) would see. These tests shell out to a
# genuinely separate interpreter for the read, the way the Spec's
# Real-world verification block does.
# ---------------------------------------------------------------------------

def _run_python(code: str, state_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestFreshProcessMutationLands:
    def test_docs_write_mutation_lands_in_fresh_process(self, state_dir):
        set_proc = _run_python(
            "from backend.rpc import dial_control\n"
            "dial_control.handle_set({'name': 'docs.write', 'level': 3, 'ttl': None})\n",
            state_dir,
        )
        assert set_proc.returncode == 0, set_proc.stderr

        read_proc = _run_python(
            "from backend.dial_registry import list_directives\n"
            "got = [d for d in list_directives() if d['class'] == 'docs.write']\n"
            "print(got[0]['level'])\n",
            state_dir,
        )
        assert read_proc.returncode == 0, read_proc.stderr
        assert read_proc.stdout.strip() == "3"

    def test_lowering_agent_spawn_lands_in_fresh_process(self, state_dir):
        """Lowering is the safety path — the one that must be proven (Spec item 8)."""
        set_proc = _run_python(
            "from backend.rpc import dial_control\n"
            "dial_control.handle_set({'name': 'agent.spawn', 'level': 1, 'ttl': None})\n",
            state_dir,
        )
        assert set_proc.returncode == 0, set_proc.stderr

        read_proc = _run_python(
            "from backend.dial_registry import list_directives\n"
            "got = [d for d in list_directives() if d['class'] == 'agent.spawn']\n"
            "print(got[0]['level'])\n",
            state_dir,
        )
        assert read_proc.returncode == 0, read_proc.stderr
        assert read_proc.stdout.strip() == "1"

    def test_ceiling_still_enforced_after_dashboard_seed(self, state_dir):
        """Provisioning the dashboard entry must not weaken sandbox.modify's ceiling=1."""
        proc = _run_python(
            "from backend.dial_registry import set_dial, DialCeilingExceeded\n"
            "try:\n"
            "    set_dial('sandbox.modify', 2, ttl=None, source={'kind': 'system', 'reason': 'dashboard_rpc'})\n"
            "    print('NOT_RAISED')\n"
            "except DialCeilingExceeded:\n"
            "    print('RAISED')\n",
            state_dir,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "RAISED"

        read_proc = _run_python(
            "from backend.dial_registry import list_directives\n"
            "got = [d for d in list_directives() if d['class'] == 'sandbox.modify']\n"
            "print(got[0]['level'])\n",
            state_dir,
        )
        assert read_proc.returncode == 0, read_proc.stderr
        assert read_proc.stdout.strip() == "1"
