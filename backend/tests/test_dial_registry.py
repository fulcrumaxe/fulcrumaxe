"""
Comprehensive tests for backend.dial_registry.

All tests run in a fresh temporary state directory so they never touch
the real ~/.fulcrumaxe-state/ directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture: isolated state dir
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point STATE_DIR at a temp dir and reload the module for each test."""
    original = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

    # Force module reload so STATE_DIR picks up the env var
    import importlib
    import backend.state_paths as _sp
    import backend.dial_registry as _dr

    importlib.reload(_sp)
    importlib.reload(_dr)

    yield tmp_path

    # Restore env before reloading so backend.state_paths.STATE_DIR gets the
    # real production path (not the tmp dir).  monkeypatch.setenv teardown
    # runs AFTER fixture teardown, so we must undo it explicitly here.
    if original is not None:
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = original
    else:
        os.environ.pop("AUTONOMOUS_TEAM_STATE_DIR", None)
    importlib.reload(_sp)
    importlib.reload(_dr)


def _registry(state_dir):
    """Import the reloaded dial_registry module."""
    import backend.dial_registry as dr
    return dr


def _write_allowlist(state_dir, entries):
    path = state_dir / "dial-directive-allowlist.json"
    path.write_text(json.dumps(entries) + "\n", encoding="utf-8")


def _system_source(reason="test"):
    return {"kind": "system", "reason": reason}


def _allowlisted_source(state_dir, reason="test"):
    _write_allowlist(state_dir, [{"kind": "system", "reason": reason}])
    return {"kind": "system", "reason": reason}


# ---------------------------------------------------------------------------
# check() — allow / deny based on dial level
# ---------------------------------------------------------------------------

class TestCheck:
    def test_default_allow_at_threshold(self, state_dir):
        dr = _registry(state_dir)
        # agent.spawn defaults to level=4; requesting level 4 should pass
        allowed, reason = dr.check("agent.spawn", 4)
        assert allowed, reason

    def test_default_allow_below_threshold(self, state_dir):
        dr = _registry(state_dir)
        allowed, reason = dr.check("agent.spawn", 1)
        assert allowed, reason

    def test_default_deny_above_current_level(self, state_dir):
        dr = _registry(state_dir)
        # agent.spawn defaults to level=4; requesting level 5 should be denied
        allowed, reason = dr.check("agent.spawn", 5)
        assert not allowed
        assert "4" in reason or "5" in reason

    def test_sandbox_modify_level_1_allow(self, state_dir):
        dr = _registry(state_dir)
        allowed, reason = dr.check("sandbox.modify", 1)
        assert allowed, reason

    def test_sandbox_modify_level_2_deny(self, state_dir):
        dr = _registry(state_dir)
        # sandbox.modify ceiling=1, default level=1; requesting level 2 = deny
        allowed, reason = dr.check("sandbox.modify", 2)
        assert not allowed

    def test_after_set_dial_new_level_respected(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "test-allow")
        # Lower agent.spawn to 2
        dr.set_dial("agent.spawn", 2, source=src)
        allowed, _ = dr.check("agent.spawn", 3)
        assert not allowed

    def test_unknown_class_allows_level_1(self, state_dir):
        dr = _registry(state_dir)
        allowed, reason = dr.check("nonexistent.class", 1)
        assert allowed

    def test_unknown_class_denies_level_2(self, state_dir):
        dr = _registry(state_dir)
        allowed, _ = dr.check("nonexistent.class", 2)
        assert not allowed


# ---------------------------------------------------------------------------
# set_dial() — ceiling enforcement
# ---------------------------------------------------------------------------

class TestSetDialCeiling:
    def test_sandbox_modify_ceiling_1_refuse_level_2(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        with pytest.raises(ValueError, match="ceiling"):
            dr.set_dial("sandbox.modify", 2, source=src)

    def test_sandbox_modify_ceiling_1_allow_level_1(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        result = dr.set_dial("sandbox.modify", 1, source=src)
        assert result["level"] == 1

    def test_methodology_change_ceiling_2_refuse_level_3(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        with pytest.raises(ValueError, match="ceiling"):
            dr.set_dial("methodology.change", 3, source=src)

    def test_methodology_change_ceiling_2_allow_level_2(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        result = dr.set_dial("methodology.change", 2, source=src)
        assert result["level"] == 2

    def test_external_system_ceiling_2_refuse_level_3(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        with pytest.raises(ValueError, match="ceiling"):
            dr.set_dial("external.system", 3, source=src)

    def test_standard_class_can_reach_5(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        result = dr.set_dial("agent.spawn", 5, source=src)
        assert result["level"] == 5
        assert result["ceiling"] == 5

    def test_refuse_level_below_1(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        with pytest.raises(ValueError):
            dr.set_dial("agent.spawn", 0, source=src)

    def test_sandbox_modify_refused_even_by_allowlisted_source(self, state_dir):
        """sandbox.modify ceiling=1; level=2 must be refused even by an allowlisted source."""
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "super-ops")
        with pytest.raises(ValueError, match="ceiling"):
            dr.set_dial("sandbox.modify", 2, source=src)

    def test_ceiling_violation_raises_dial_ceiling_exceeded(self, state_dir):
        """Ceiling violations raise DialCeilingExceeded, not plain ValueError."""
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        with pytest.raises(dr.DialCeilingExceeded):
            dr.set_dial("sandbox.modify", 2, source=src)


# ---------------------------------------------------------------------------
# revert_expired() — TTL reversion
# ---------------------------------------------------------------------------

class TestRevertExpired:
    def test_not_expired_stays(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ttl-test")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        dr.set_dial("agent.spawn", 5, ttl=future, source=src)

        reverted = dr.revert_expired()
        assert reverted == 0
        allowed, _ = dr.check("agent.spawn", 5)
        assert allowed

    def test_expired_is_reverted(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ttl-test")
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        dr.set_dial("agent.spawn", 5, ttl=past, source=src)

        reverted = dr.revert_expired()
        assert reverted == 1
        # Should have reverted to default level=4
        allowed_5, _ = dr.check("agent.spawn", 5)
        assert not allowed_5
        allowed_4, _ = dr.check("agent.spawn", 4)
        assert allowed_4

    def test_for_today_ttl_is_parsed(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ttl-test")
        # "for-today" expiry is tomorrow's midnight — always in the future
        result = dr.set_dial("cost.spend", 3, ttl="for-today", source=src)
        directives = result.get("directives", [])
        assert len(directives) >= 1
        ttl_until = directives[-1].get("ttl_until")
        assert ttl_until is not None
        # Must be a parseable ISO-8601 string in the future
        dt = datetime.fromisoformat(ttl_until)
        assert dt.tzinfo is not None
        assert dt > datetime.now(timezone.utc), (
            f"for-today TTL {ttl_until!r} must be in the future (got past/present)"
        )

    def test_check_calls_revert_expired_lazily(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "lazy-test")
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        dr.set_dial("agent.spawn", 5, ttl=past, source=src)

        # check() internally calls revert_expired(); level should be back to 4
        allowed_5, _ = dr.check("agent.spawn", 5)
        assert not allowed_5


# ---------------------------------------------------------------------------
# Audit hash chain
# ---------------------------------------------------------------------------

class TestAuditChain:
    def test_first_row_has_genesis_prev_hash(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "chain-test")
        dr.set_dial("cost.spend", 3, source=src)

        audit_path = state_dir / "audit.jsonl"
        lines = [l for l in audit_path.read_text().splitlines() if l.strip()]
        # Find the first dial_change row
        dial_rows = [json.loads(l) for l in lines if "dial_change" in l]
        assert len(dial_rows) >= 1
        assert dial_rows[0]["prev_hash"] == "genesis"

    def test_hash_chain_validates_after_multiple_mutations(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "chain-test")

        # Perform several mutations
        dr.set_dial("cost.spend", 3, source=src)
        dr.set_dial("cost.spend", 2, source=src)
        dr.set_dial("archive.move", 5, source=src)

        audit_path = state_dir / "audit.jsonl"
        raw_lines = [l.strip() for l in audit_path.read_text().splitlines() if l.strip()]
        dial_rows = [
            json.loads(l) for l in raw_lines
            if json.loads(l).get("kind") == "dial_change"
        ]

        # Each dial_change row's prev_hash must equal sha256 of the preceding raw line.
        # Since only dial_change rows are written (in this test no other kinds interleave),
        # raw_lines[i-1] is the actual preceding row.
        for i, row in enumerate(dial_rows):
            if i == 0:
                assert row["prev_hash"] == "genesis"
            else:
                prev_line = raw_lines[i - 1]
                expected_hash = hashlib.sha256(prev_line.encode()).hexdigest()
                assert row["prev_hash"] == expected_hash, (
                    f"Row {i} prev_hash mismatch: "
                    f"got {row['prev_hash']!r}, expected {expected_hash!r}"
                )

    def test_audit_row_contains_expected_fields(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "audit-fields-test")
        dr.set_dial("memory.write", 5, source=src)

        audit_path = state_dir / "audit.jsonl"
        rows = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
        dial_rows = [r for r in rows if r.get("kind") == "dial_change"]
        assert len(dial_rows) >= 1

        row = dial_rows[-1]
        assert row["kind"] == "dial_change"
        assert "prev_hash" in row
        assert row["class"] == "memory.write"
        assert "prev_level" in row
        assert row["new_level"] == 5
        assert row["source"] == src
        assert "timestamp" in row


# ---------------------------------------------------------------------------
# Empty allowlist refuses all directives
# ---------------------------------------------------------------------------

class TestEmptyAllowlist:
    def test_no_allowlist_file_refuses(self, state_dir):
        dr = _registry(state_dir)
        # Allowlist file absent — set_dial should refuse
        src = {"kind": "system", "reason": "test"}
        with pytest.raises(ValueError, match="allowlist"):
            dr.set_dial("agent.spawn", 3, source=src)

    def test_empty_allowlist_file_refuses(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [])
        src = {"kind": "system", "reason": "test"}
        with pytest.raises(ValueError, match="allowlist"):
            dr.set_dial("agent.spawn", 3, source=src)

    def test_non_allowlisted_source_refused(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [{"kind": "github_user", "login": "alice"}])
        src = {"kind": "github_user", "login": "mallory"}
        with pytest.raises(ValueError, match="allowlist"):
            dr.set_dial("agent.spawn", 3, source=src)

    def test_allowlisted_github_user_permitted(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [{"kind": "github_user", "login": "ian"}])
        src = {"kind": "github_user", "login": "ian"}
        result = dr.set_dial("agent.spawn", 3, source=src)
        assert result["level"] == 3


# ---------------------------------------------------------------------------
# _authenticate_source() — an entry can never be more specific than the
# source it authorizes (D#1883 Decision 2). Seeding the dashboard entry
# {"kind":"system","reason":"dashboard_rpc"} must not implicitly authorize
# a bare {"kind":"system"} source just because the source's keys happen to
# be a subset of the entry's.
# ---------------------------------------------------------------------------

class TestAuthenticateSourceNoImplicitWidening:
    def test_bare_system_source_not_authorized_by_specific_entry(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [{"kind": "system", "reason": "dashboard_rpc"}])
        assert dr._authenticate_source({"kind": "system"}) is False

    def test_matching_system_source_still_authorized(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [{"kind": "system", "reason": "dashboard_rpc"}])
        assert dr._authenticate_source({"kind": "system", "reason": "dashboard_rpc"}) is True

    def test_widening_fix_does_not_over_narrow_matching_user(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [{"kind": "github_user", "login": "alice"}])
        assert dr._authenticate_source({"kind": "github_user", "login": "alice"}) is True

    def test_widening_fix_does_not_over_narrow_other_user_denied(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [{"kind": "github_user", "login": "alice"}])
        assert dr._authenticate_source({"kind": "github_user", "login": "bob"}) is False


# ---------------------------------------------------------------------------
# Refusal message names a real, runnable command (D#1883 Spec item 13)
# ---------------------------------------------------------------------------

class TestRefusalNamesRealCommand:
    def test_refusal_names_provisioning_script(self, state_dir):
        dr = _registry(state_dir)
        _write_allowlist(state_dir, [])
        src = {"kind": "system", "reason": "test"}
        with pytest.raises(ValueError) as excinfo:
            dr.set_dial("agent.spawn", 3, source=src)
        assert "scripts/provision-dial-allowlist.sh" in str(excinfo.value)

    def test_named_script_exists_on_disk(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        assert (repo_root / "scripts" / "provision-dial-allowlist.sh").exists()


# ---------------------------------------------------------------------------
# list_directives()
# ---------------------------------------------------------------------------

class TestListDirectives:
    def test_returns_all_default_classes(self, state_dir):
        dr = _registry(state_dir)
        directives = dr.list_directives()
        class_names = {d["class"] for d in directives}
        expected = {
            "docs.write", "tests.add", "deps.bump", "agent.spawn",
            "merge.standard", "merge.fast-path", "intent.generate",
            "methodology.change", "external.system", "sandbox.modify",
            "cost.spend", "memory.write", "archive.move",
        }
        assert expected.issubset(class_names)

    def test_each_entry_has_required_keys(self, state_dir):
        dr = _registry(state_dir)
        for entry in dr.list_directives():
            assert "class" in entry
            assert "level" in entry
            assert "ceiling" in entry
            assert "directives" in entry

    def test_sandbox_ceiling_is_1(self, state_dir):
        dr = _registry(state_dir)
        for entry in dr.list_directives():
            if entry["class"] == "sandbox.modify":
                assert entry["ceiling"] == 1
                return
        pytest.fail("sandbox.modify not found in list_directives()")

    def test_memory_write_default_level_is_3(self, state_dir):
        """memory.write default level is 3 (propose-timeout tier)."""
        dr = _registry(state_dir)
        for entry in dr.list_directives():
            if entry["class"] == "memory.write":
                assert entry["level"] == 3, (
                    f"memory.write default level should be 3, got {entry['level']}"
                )
                return
        pytest.fail("memory.write not found in list_directives()")

    def test_role_to_dial_class_maps_all_roles(self, state_dir):
        dr = _registry(state_dir)
        # Every value must be a registered dial class
        registered = {d["class"] for d in dr.list_directives()}
        for role, dial_class in dr._ROLE_TO_DIAL_CLASS.items():
            assert dial_class in registered, (
                f"role {role!r} maps to {dial_class!r} which is not a registered dial class"
            )


# ---------------------------------------------------------------------------
# control_plane integration: dials section
# ---------------------------------------------------------------------------

class TestControlPlaneDials:
    def test_load_populates_dials(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(tmp_path / "config.json"))
        from backend.control_plane import ControlPlane
        cp = ControlPlane()
        cp.load()
        dials = cp.list_dials()
        assert "agent.spawn" in dials
        assert "sandbox.modify" in dials
        assert dials["sandbox.modify"]["ceiling"] == 1

    def test_hardcoded_ceiling_enforced_on_load(self, tmp_path, monkeypatch):
        # Even if config.json has a wrong ceiling, load() corrects it
        config = {
            "dials": {
                "sandbox.modify": {"level": 1, "ceiling": 5, "directives": []}
            }
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(config_path))

        from backend.control_plane import ControlPlane
        cp = ControlPlane()
        cp.load()
        dial = cp.get_dial("sandbox.modify")
        assert dial is not None
        assert dial["ceiling"] == 1  # corrected to hardcoded ceiling

    def test_get_dial_ceiling_returns_hardcoded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(tmp_path / "config.json"))
        from backend.control_plane import ControlPlane
        cp = ControlPlane()
        cp.load()
        assert cp.get_dial_ceiling("sandbox.modify") == 1
        assert cp.get_dial_ceiling("methodology.change") == 2
        assert cp.get_dial_ceiling("external.system") == 2
        assert cp.get_dial_ceiling("agent.spawn") == 5


# ---------------------------------------------------------------------------
# AC1 — dotted dial-class keys resolve via control_plane.get()
# ---------------------------------------------------------------------------

# All 13 registered dial class names
_ALL_DIAL_CLASSES = [
    "docs.write",
    "tests.add",
    "deps.bump",
    "agent.spawn",
    "merge.standard",
    "merge.fast-path",
    "intent.generate",
    "methodology.change",
    "external.system",
    "sandbox.modify",
    "cost.spend",
    "memory.write",
    "archive.move",
]


class TestDottedDialKeyGet:
    """AC1: control_plane.get('dials.<class>.level') resolves for all 13 classes."""

    def _make_cp(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(tmp_path / "config.json"))
        import importlib
        import backend.control_plane as _cp_mod
        importlib.reload(_cp_mod)
        cp = _cp_mod.ControlPlane()
        cp.load()
        return cp

    @pytest.mark.parametrize("class_name", _ALL_DIAL_CLASSES)
    def test_all_classes_resolve_level(self, tmp_path, monkeypatch, class_name):
        cp = self._make_cp(tmp_path, monkeypatch)
        level = cp.get(f"dials.{class_name}.level")
        assert level is not None, (
            f"dials.{class_name}.level returned None — dotted-key get broken"
        )
        assert isinstance(level, int), f"Expected int, got {type(level).__name__}"
        assert 0 <= level <= 6, f"Level {level} out of expected 0–6 range"

    @pytest.mark.parametrize("class_name", _ALL_DIAL_CLASSES)
    def test_all_classes_resolve_ceiling(self, tmp_path, monkeypatch, class_name):
        cp = self._make_cp(tmp_path, monkeypatch)
        ceiling = cp.get(f"dials.{class_name}.ceiling")
        assert ceiling is not None, (
            f"dials.{class_name}.ceiling returned None — dotted-key get broken"
        )
        assert isinstance(ceiling, int)

    def test_dotted_class_get_returns_class_dict(self, tmp_path, monkeypatch):
        """get('dials.agent.spawn') returns the full state dict for agent.spawn."""
        cp = self._make_cp(tmp_path, monkeypatch)
        state = cp.get("dials.agent.spawn")
        assert isinstance(state, dict)
        assert "level" in state
        assert "ceiling" in state

    def test_hyphenated_class_resolves(self, tmp_path, monkeypatch):
        """merge.fast-path contains both a dot and a hyphen — must resolve."""
        cp = self._make_cp(tmp_path, monkeypatch)
        level = cp.get("dials.merge.fast-path.level")
        assert level is not None
        assert isinstance(level, int)

    def test_get_dials_returns_full_section(self, tmp_path, monkeypatch):
        """get('dials') returns the entire dials dict."""
        cp = self._make_cp(tmp_path, monkeypatch)
        dials = cp.get("dials")
        assert isinstance(dials, dict)
        assert "agent.spawn" in dials

    def test_nonexistent_subkey_returns_none(self, tmp_path, monkeypatch):
        cp = self._make_cp(tmp_path, monkeypatch)
        assert cp.get("dials.agent.spawn.nonexistent_field") is None

    def test_nonexistent_class_returns_none(self, tmp_path, monkeypatch):
        cp = self._make_cp(tmp_path, monkeypatch)
        assert cp.get("dials.nonexistent.class.level") is None


# ---------------------------------------------------------------------------
# AC2 — ceiling checked before auth in set_dial()
# ---------------------------------------------------------------------------

class TestCeilingBeforeAuth:
    """AC2: error precedence is ceiling > auth."""

    def test_unauthenticated_ceiling_violation_raises_ceiling_error(self, state_dir):
        """Unauthenticated source + ceiling violation → ceiling error, not auth error."""
        dr = _registry(state_dir)
        # No allowlist — source is unauthenticated
        src = {"kind": "github_user", "login": "fake"}
        with pytest.raises(dr.DialCeilingExceeded):
            dr.set_dial("sandbox.modify", 2, source=src)

    def test_unauthenticated_ceiling_violation_not_auth_error(self, state_dir):
        """Error message must mention ceiling, not allowlist."""
        dr = _registry(state_dir)
        src = {"kind": "github_user", "login": "fake"}
        with pytest.raises(Exception, match="ceiling"):
            dr.set_dial("sandbox.modify", 2, source=src)

    def test_unauthenticated_within_ceiling_raises_auth_error(self, state_dir):
        """Unauthenticated source within ceiling → auth error (ceiling passes)."""
        dr = _registry(state_dir)
        src = {"kind": "github_user", "login": "fake"}
        with pytest.raises(ValueError, match="allowlist"):
            dr.set_dial("agent.spawn", 3, source=src)

    def test_authenticated_ceiling_violation_raises_ceiling_error(self, state_dir):
        """Authenticated source + ceiling violation → ceiling error."""
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        with pytest.raises(dr.DialCeilingExceeded):
            dr.set_dial("sandbox.modify", 2, source=src)

    def test_authenticated_within_ceiling_succeeds(self, state_dir):
        """Authenticated source within ceiling → success."""
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        result = dr.set_dial("agent.spawn", 3, source=src)
        assert result["level"] == 3

    def test_invalid_level_beats_auth_error(self, state_dir):
        """level < 1 is caught before auth check."""
        dr = _registry(state_dir)
        src = {"kind": "github_user", "login": "fake"}  # unauthenticated
        with pytest.raises(ValueError, match="level"):
            dr.set_dial("agent.spawn", 0, source=src)

    def test_invalid_level_beats_ceiling_error(self, state_dir):
        """level < 1 is caught before ceiling check."""
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        # level 0 is invalid AND sandbox.modify has ceiling 1
        with pytest.raises(ValueError, match="level"):
            dr.set_dial("sandbox.modify", 0, source=src)


# ---------------------------------------------------------------------------
# AC3 — rejected directives produce an audit row
# ---------------------------------------------------------------------------

class TestRejectionAuditRows:
    """AC3: every set_dial() that raises appends a dial_directive_rejected row."""

    def _audit_rows(self, state_dir):
        audit_path = state_dir / "audit.jsonl"
        if not audit_path.exists():
            return []
        return [
            json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()
        ]

    def test_unauthenticated_source_writes_rejection_row(self, state_dir):
        dr = _registry(state_dir)
        src = {"kind": "github_user", "login": "mallory"}
        try:
            dr.set_dial("agent.spawn", 3, source=src)
        except ValueError:
            pass
        rows = self._audit_rows(state_dir)
        rejected = [r for r in rows if r.get("kind") == "dial_directive_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "unauthenticated_source"
        assert rejected[0]["class"] == "agent.spawn"
        assert rejected[0]["level"] == 3
        assert rejected[0]["source"] == src

    def test_ceiling_violation_writes_rejection_row(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        try:
            dr.set_dial("sandbox.modify", 2, source=src)
        except Exception:
            pass
        rows = self._audit_rows(state_dir)
        rejected = [r for r in rows if r.get("kind") == "dial_directive_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "ceiling_violation"
        assert rejected[0]["class"] == "sandbox.modify"
        assert rejected[0]["level"] == 2

    def test_invalid_level_writes_rejection_row(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        try:
            dr.set_dial("agent.spawn", 0, source=src)
        except ValueError:
            pass
        rows = self._audit_rows(state_dir)
        rejected = [r for r in rows if r.get("kind") == "dial_directive_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "invalid_level"

    def test_unknown_class_writes_rejection_row(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        try:
            dr.set_dial("nonexistent.class", 3, source=src)
        except ValueError:
            pass
        rows = self._audit_rows(state_dir)
        rejected = [r for r in rows if r.get("kind") == "dial_directive_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "unknown_class"
        assert rejected[0]["class"] == "nonexistent.class"

    def test_rejection_row_has_required_fields(self, state_dir):
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        try:
            dr.set_dial("sandbox.modify", 2, source=src)
        except Exception:
            pass
        rows = self._audit_rows(state_dir)
        rejected = [r for r in rows if r.get("kind") == "dial_directive_rejected"]
        assert len(rejected) == 1
        row = rejected[0]
        assert "kind" in row
        assert "prev_hash" in row
        assert "class" in row
        assert "level" in row
        assert "source" in row
        assert "reason" in row
        assert "timestamp" in row
        # timestamp must be parseable ISO-8601
        dt = datetime.fromisoformat(row["timestamp"])
        assert dt.tzinfo is not None

    def test_hash_chain_intact_across_mixed_accept_reject(self, state_dir):
        """Hash chain must validate end-to-end across accepted + rejected rows."""
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")

        # accept
        dr.set_dial("cost.spend", 3, source=src)
        # reject (ceiling)
        try:
            dr.set_dial("sandbox.modify", 2, source=src)
        except Exception:
            pass
        # accept
        dr.set_dial("cost.spend", 2, source=src)
        # reject (auth)
        try:
            dr.set_dial("agent.spawn", 3, source={"kind": "github_user", "login": "fake"})
        except Exception:
            pass
        # accept
        dr.set_dial("archive.move", 5, source=src)

        audit_path = state_dir / "audit.jsonl"
        raw_lines = [l.strip() for l in audit_path.read_text().splitlines() if l.strip()]
        assert len(raw_lines) == 5  # 3 accepted + 2 rejected

        # Validate hash chain: each row's prev_hash == sha256 of the previous raw line
        for i, raw_line in enumerate(raw_lines):
            row = json.loads(raw_line)
            if i == 0:
                assert row["prev_hash"] == "genesis", f"Row 0 prev_hash should be genesis"
            else:
                expected = hashlib.sha256(raw_lines[i - 1].encode()).hexdigest()
                assert row["prev_hash"] == expected, (
                    f"Row {i} hash chain broken: "
                    f"got {row['prev_hash']!r}, expected {expected!r}"
                )

    def test_successful_set_dial_writes_dial_change_row(self, state_dir):
        """Accepted calls write exactly one dial_change row (schema preserved)."""
        dr = _registry(state_dir)
        src = _allowlisted_source(state_dir, "ops")
        dr.set_dial("cost.spend", 3, source=src)
        rows = self._audit_rows(state_dir)
        accepted = [r for r in rows if r.get("kind") == "dial_change"]
        assert len(accepted) == 1
        assert accepted[0]["class"] == "cost.spend"
        assert accepted[0]["new_level"] == 3
        assert "timestamp" in accepted[0]

    def test_rejection_count_per_reason(self, state_dir):
        """Each rejection reason produces exactly one audit row per call."""
        dr = _registry(state_dir)
        src_authed = _allowlisted_source(state_dir, "ops")
        src_fake = {"kind": "github_user", "login": "fake"}

        # invalid_level
        try:
            dr.set_dial("agent.spawn", 0, source=src_authed)
        except Exception:
            pass
        # ceiling_violation
        try:
            dr.set_dial("sandbox.modify", 2, source=src_authed)
        except Exception:
            pass
        # unauthenticated_source
        try:
            dr.set_dial("agent.spawn", 3, source=src_fake)
        except Exception:
            pass
        # unknown_class
        try:
            dr.set_dial("no.such.class", 3, source=src_authed)
        except Exception:
            pass

        rows = self._audit_rows(state_dir)
        rejected = [r for r in rows if r.get("kind") == "dial_directive_rejected"]
        reasons = [r["reason"] for r in rejected]
        assert reasons.count("invalid_level") == 1
        assert reasons.count("ceiling_violation") == 1
        assert reasons.count("unauthenticated_source") == 1
        assert reasons.count("unknown_class") == 1
