"""
Tests for backend/control_plane.py — ControlPlane class.

Uses a temp config.json path so the real .autonomous-team/config.json is never touched.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.control_plane import (  # noqa: E402
    ControlPlane,
    _DEFAULT_GATES,
    _GATE_TYPES,
    _validate_gate_value,
)


@pytest.fixture()
def cp(tmp_path):
    """Fresh ControlPlane backed by a temp config file."""
    instance = ControlPlane(config_path=tmp_path / "config.json")
    instance.load()
    return instance


# --- load / save -----------------------------------------------------------

def test_load_creates_defaults_when_file_missing(tmp_path):
    cp = ControlPlane(config_path=tmp_path / "nonexistent.json")
    cp.load()
    # Defaults must be present
    assert cp.gate_enabled("auto_merge") is True
    assert "executor" in cp.list_gates() or cp.get_policy("executor") is not None


def test_save_and_reload(tmp_path):
    path = tmp_path / "config.json"
    cp = ControlPlane(config_path=path)
    cp.load()
    cp.set("gates.auto_merge", False)

    cp2 = ControlPlane(config_path=path)
    cp2.load()
    assert cp2.gate_enabled("auto_merge") is False


def test_save_writes_valid_json(tmp_path):
    path = tmp_path / "config.json"
    cp = ControlPlane(config_path=path)
    cp.load()
    cp.set("gates.idea_generation", False)
    raw = json.loads(path.read_text())
    assert raw["gates"]["idea_generation"] is False


# --- get / set -----------------------------------------------------------

def test_get_dot_notation(cp):
    value = cp.get("gates.auto_merge")
    assert value is True


def test_set_dot_notation(cp):
    cp.set("gates.budget_check", False)
    assert cp.get("gates.budget_check") is False


def test_get_missing_key_returns_none(cp):
    assert cp.get("does.not.exist") is None


def test_set_creates_intermediate_keys(cp):
    cp.set("custom.nested.value", 42)
    assert cp.get("custom.nested.value") == 42


# --- feature gates -----------------------------------------------------------

def test_gate_enabled_defaults_true(cp):
    assert cp.gate_enabled("auto_merge") is True
    assert cp.gate_enabled("security_review") is True


def test_gate_enabled_after_disable(cp):
    cp.set("gates.stall_detection", False)
    assert cp.gate_enabled("stall_detection") is False


def test_gate_enabled_missing_gate_returns_false(cp):
    assert cp.gate_enabled("nonexistent_gate") is False


def test_list_gates_contains_all_defaults(cp):
    gates = cp.list_gates()
    for expected in ("auto_merge", "security_review", "budget_check", "idea_generation", "stall_detection"):
        assert expected in gates


def test_list_gates_reflects_changes(cp):
    cp.set("gates.auto_merge", False)
    gates = cp.list_gates()
    assert gates["auto_merge"] is False


# --- agent policies -----------------------------------------------------------

def test_get_policy_executor_has_defaults(cp):
    policy = cp.get_policy("executor")
    assert "timeout_minutes" in policy
    assert "max_retries" in policy
    assert "token_ceiling" in policy


def test_get_policy_unknown_role_returns_empty_dict(cp):
    policy = cp.get_policy("ghost-agent")
    assert isinstance(policy, dict)
    assert len(policy) == 0


def test_get_policy_merges_config_over_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "policies": {"executor": {"timeout_minutes": 99}}
    }))
    cp = ControlPlane(config_path=path)
    cp.load()
    policy = cp.get_policy("executor")
    assert policy["timeout_minutes"] == 99
    # Default values still present for unset keys
    assert "max_retries" in policy


# --- audit log -----------------------------------------------------------

def test_audit_log_records_set_operations(cp):
    cp.set("gates.auto_merge", False)
    log = cp.get_audit_log(limit=5)
    assert len(log) >= 1
    entry = log[0]
    assert entry["key"] == "gates.auto_merge"
    assert entry["new_value"] is False
    assert entry["old_value"] is True


def test_audit_log_newest_first(cp):
    cp.set("gates.auto_merge", False)
    cp.set("gates.budget_check", False)
    log = cp.get_audit_log(limit=10)
    timestamps = [e["timestamp"] for e in log]
    assert timestamps == sorted(timestamps, reverse=True)


def test_audit_log_empty_on_fresh_config(cp):
    # Fresh load with no changes → audit log is empty
    log = cp.get_audit_log()
    assert log == []


def test_audit_log_capped_at_200_entries(tmp_path):
    cp = ControlPlane(config_path=tmp_path / "config.json")
    cp.load()
    for i in range(205):
        cp.set(f"gates.auto_merge", i % 2 == 0)
    log = cp.get_audit_log(limit=250)
    assert len(log) <= 200

# --- gate type metadata (_GATE_TYPES / _validate_gate_value) ----------------------------


def test_validate_gate_value_bool_gate_accepts_bool():
    assert _validate_gate_value("auto_merge", True) is None
    assert _validate_gate_value("auto_merge", False) is None


def test_validate_gate_value_bool_gate_rejects_string():
    err = _validate_gate_value("auto_merge", "true")
    assert err is not None
    assert "bool" in err


def test_validate_gate_value_self_observe_enforcement_accepts_valid():
    for val in ("shadow", "advisory", "enforced"):
        assert _validate_gate_value("self_observe_enforcement", val) is None


def test_validate_gate_value_self_observe_enforcement_rejects_invalid():
    err = _validate_gate_value("self_observe_enforcement", "bogus")
    assert err is not None
    assert "bogus" in err


def test_all_default_bool_gates_pass_validation():
    for gate_name, gate_value in _DEFAULT_GATES.items():
        err = _validate_gate_value(gate_name, gate_value)
        assert err is None, (
            f"gate {gate_name!r} default value {gate_value!r} failed validation: {err}"
        )


def test_self_observe_enforcement_is_in_gate_types():
    assert "self_observe_enforcement" in _GATE_TYPES
    assert "enum" in _GATE_TYPES["self_observe_enforcement"]


# --- schema validator integration -------------------------------------------------------


def test_set_self_observe_enforcement_emits_no_schema_warning(tmp_path, caplog):
    cp_instance = ControlPlane(config_path=tmp_path / "config.json")
    cp_instance.load()
    with caplog.at_level(logging.WARNING, logger="backend.control_plane"):
        cp_instance.set("gates.self_observe_enforcement", "advisory")
    schema_warnings = [
        r for r in caplog.records
        if "schema" in r.message.lower() or "expected bool" in r.message
    ]
    assert schema_warnings == [], (
        f"Unexpected schema warnings: {[r.message for r in schema_warnings]}"
    )


def test_set_bool_gate_emits_no_schema_warning(tmp_path, caplog):
    cp_instance = ControlPlane(config_path=tmp_path / "config.json")
    cp_instance.load()
    with caplog.at_level(logging.WARNING, logger="backend.control_plane"):
        cp_instance.set("gates.auto_merge", False)
        cp_instance.set("gates.security_review", True)
        cp_instance.set("gates.idea_generation", False)
    schema_warnings = [
        r for r in caplog.records
        if "schema" in r.message.lower() or "expected bool" in r.message
    ]
    assert schema_warnings == [], (
        f"Unexpected schema warnings: {[r.message for r in schema_warnings]}"
    )


def test_cli_set_invalid_enum_returns_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(tmp_path / "config.json"))
    cp_instance = ControlPlane(config_path=tmp_path / "config.json")
    cp_instance.load()

    from backend.control_plane import main as cp_main  # noqa: PLC0415

    result = cp_main(["set", "gates.self_observe_enforcement", "invalid_value"])
    assert result == 1
    captured = capsys.readouterr()
    assert "invalid_value" in captured.err or "expects" in captured.err


def test_cli_set_valid_enum_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(tmp_path / "config.json"))
    cp_instance = ControlPlane(config_path=tmp_path / "config.json")
    cp_instance.load()

    from backend.control_plane import main as cp_main  # noqa: PLC0415

    result = cp_main(["set", "gates.self_observe_enforcement", "enforced"])
    assert result == 0
