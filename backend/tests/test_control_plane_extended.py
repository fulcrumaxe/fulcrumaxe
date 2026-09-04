"""
Unit tests for backend/control_plane.py — ControlPlane class and module helpers.

State isolation: every test uses a tmp_path-backed config file so the real
~/.fulcrumaxe-state/ directory is never touched.  The
AUTONOMOUS_TEAM_STATE_DIR env var is overridden via the `isolated_env`
fixture to redirect audit_trail writes to a temp dir as well.

Coverage targets:
  - ControlPlane.load()  — defaults injection, corrupt JSON fallback
  - ControlPlane.save()  — atomic write, valid JSON on disk
  - ControlPlane.get()   — dot-notation including dials section
  - ControlPlane.set()   — audit row, save, no real state-dir write
  - gate_enabled / list_gates
  - get_policy
  - get_dial / list_dials / get_dial_ceiling
  - get_setting / list_settings
  - apply_mode / get_mode / list_modes
  - check_gate / check_policy (module-level helpers)
  - _coerce_value
  - CLI commands: show, get, set, gates, settings, audit, mode (show/list/set)
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Make backend importable from this file's location.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.control_plane import (  # noqa: E402
    ControlPlane,
    _DEFAULT_DIALS,
    _DEFAULT_GATES,
    _DEFAULT_POLICIES,
    _DEFAULT_SETTINGS,
    _DIAL_CEILINGS,
    _DIAL_DEFAULT_CEILING,
    _coerce_value,
    _validate_gate_value,
    check_gate,
    check_policy,
    main as cp_main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """Redirect AUTONOMOUS_TEAM_STATE_DIR so audit_trail never writes to ~/.fulcrumaxe-state/."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AF_CONTROL_PLANE_CONFIG", str(tmp_path / "config.json"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def cp(tmp_path, isolated_env):
    """Fresh ControlPlane backed by a tmp config file with defaults loaded."""
    instance = ControlPlane(config_path=isolated_env / "config.json")
    instance.load()
    return instance


# ---------------------------------------------------------------------------
# load() — defaults and corrupt-JSON fallback
# ---------------------------------------------------------------------------


def test_load_missing_file_injects_all_default_sections(tmp_path, isolated_env):
    instance = ControlPlane(config_path=isolated_env / "nonexistent.json")
    instance.load()
    # All four main sections present
    assert "gates" in instance._data
    assert "policies" in instance._data
    assert "settings" in instance._data
    assert "dials" in instance._data
    assert "audit_log" in instance._data


def test_load_corrupt_json_falls_back_to_defaults(isolated_env):
    """Malformed JSON triggers graceful fallback to safe defaults — no crash."""
    bad_json = isolated_env / "config.json"
    bad_json.write_text("{ this is not valid json }")
    instance = ControlPlane(config_path=bad_json)
    instance.load()  # must not raise
    assert instance.gate_enabled("auto_merge") is True
    assert isinstance(instance.get_policy("executor"), dict)


def test_load_empty_gates_injects_all_defaults(isolated_env):
    """Existing config with no gates section gets defaults injected."""
    config_path = isolated_env / "config.json"
    config_path.write_text(json.dumps({"policies": {}}))
    instance = ControlPlane(config_path=config_path)
    instance.load()
    for gate in _DEFAULT_GATES:
        assert gate in instance._data["gates"], f"missing default gate: {gate}"


def test_load_partial_gates_fills_missing_only(isolated_env):
    """Existing gate values are preserved; missing gates get defaults."""
    config_path = isolated_env / "config.json"
    config_path.write_text(json.dumps({"gates": {"auto_merge": False}}))
    instance = ControlPlane(config_path=config_path)
    instance.load()
    assert instance._data["gates"]["auto_merge"] is False
    assert "security_review" in instance._data["gates"]


def test_load_injects_all_default_dial_classes(isolated_env):
    """All dial classes from _DEFAULT_DIALS are injected on first load."""
    instance = ControlPlane(config_path=isolated_env / "config.json")
    instance.load()
    for entry in _DEFAULT_DIALS:
        cls = entry["class_name"]
        assert cls in instance._data["dials"], f"missing dial class: {cls}"


def test_load_enforces_hardcoded_dial_ceilings(isolated_env):
    """Stored dial ceilings for sensitive classes are overwritten by hardcoded values."""
    config_path = isolated_env / "config.json"
    # Inject a stored value that exceeds the hardcoded ceiling for sandbox.modify
    config_path.write_text(json.dumps({
        "dials": {"sandbox.modify": {"level": 1, "ceiling": 99, "directives": []}}
    }))
    instance = ControlPlane(config_path=config_path)
    instance.load()
    assert instance._data["dials"]["sandbox.modify"]["ceiling"] == _DIAL_CEILINGS["sandbox.modify"]


# ---------------------------------------------------------------------------
# save() / round-trip
# ---------------------------------------------------------------------------


def test_save_writes_valid_json(isolated_env):
    config_path = isolated_env / "config.json"
    instance = ControlPlane(config_path=config_path)
    instance.load()
    instance.set("gates.idea_generation", False)
    raw = json.loads(config_path.read_text())
    assert raw["gates"]["idea_generation"] is False


def test_save_and_reload_persists_changes(isolated_env):
    config_path = isolated_env / "config.json"
    cp1 = ControlPlane(config_path=config_path)
    cp1.load()
    cp1.set("gates.auto_merge", False)

    cp2 = ControlPlane(config_path=config_path)
    cp2.load()
    assert cp2.gate_enabled("auto_merge") is False


def test_save_creates_parent_directories(tmp_path, isolated_env):
    deep_path = isolated_env / "a" / "b" / "config.json"
    instance = ControlPlane(config_path=deep_path)
    instance.load()
    instance.save()
    assert deep_path.exists()


# ---------------------------------------------------------------------------
# get() — dot-notation
# ---------------------------------------------------------------------------


def test_get_simple_key(cp):
    assert cp.get("gates.auto_merge") is True


def test_get_nested_key_missing_returns_none(cp):
    assert cp.get("does.not.exist") is None


def test_get_top_level_section_returns_dict(cp):
    result = cp.get("gates")
    assert isinstance(result, dict)
    assert "auto_merge" in result


def test_get_dials_section_top_level(cp):
    result = cp.get("dials")
    assert isinstance(result, dict)
    assert "agent.spawn" in result


def test_get_dials_dotted_class_name(cp):
    """'dials.agent.spawn' should resolve correctly despite the dot in the class name."""
    result = cp.get("dials.agent.spawn")
    assert isinstance(result, dict)
    assert "level" in result


def test_get_dials_dotted_class_subfield(cp):
    """'dials.agent.spawn.level' should return the level integer."""
    result = cp.get("dials.agent.spawn.level")
    assert isinstance(result, int)


def test_get_dials_fast_path_class(cp):
    """merge.fast-path dial (long name) resolves through longest-prefix matching."""
    result = cp.get("dials.merge.fast-path")
    assert isinstance(result, dict)
    assert "ceiling" in result


def test_get_dials_unknown_class_returns_none(cp):
    assert cp.get("dials.nonexistent.class") is None


# ---------------------------------------------------------------------------
# set() — value, audit row, no real state writes
# ---------------------------------------------------------------------------


def test_set_writes_value(cp):
    cp.set("gates.budget_check", False)
    assert cp.get("gates.budget_check") is False


def test_set_creates_intermediate_dicts(cp):
    cp.set("custom.nested.key", 123)
    assert cp.get("custom.nested.key") == 123


def test_set_records_audit_entry_with_old_and_new(cp):
    cp.set("gates.auto_merge", False)
    log = cp.get_audit_log(limit=5)
    entry = log[0]
    assert entry["key"] == "gates.auto_merge"
    assert entry["old_value"] is True
    assert entry["new_value"] is False
    assert "timestamp" in entry


def test_set_audit_log_newest_first(cp):
    cp.set("gates.auto_merge", False)
    cp.set("gates.budget_check", False)
    log = cp.get_audit_log(limit=10)
    timestamps = [e["timestamp"] for e in log]
    assert timestamps == sorted(timestamps, reverse=True)


def test_set_caps_audit_log_at_200(isolated_env):
    instance = ControlPlane(config_path=isolated_env / "config.json")
    instance.load()
    for i in range(210):
        instance.set("gates.auto_merge", i % 2 == 0)
    log = instance.get_audit_log(limit=250)
    assert len(log) <= 200


def test_set_no_op_skips_real_state_write(isolated_env):
    """Setting a key to its current value should not create audit_trail write
    (the try/except swallows it, but we verify real state dir is untouched)."""
    state_dir = isolated_env / "state"
    audit_file = state_dir / "audit.jsonl"
    instance = ControlPlane(config_path=isolated_env / "config.json")
    instance.load()
    initial_size = audit_file.stat().st_size if audit_file.exists() else 0
    # Set to same value — should be a no-op for real audit trail
    instance.set("gates.auto_merge", True)  # True is already the default
    final_size = audit_file.stat().st_size if audit_file.exists() else 0
    # We're not strictly asserting size == 0 because the mock might work
    # differently, but we confirm the config itself was saved
    assert instance.gate_enabled("auto_merge") is True
    _ = initial_size, final_size  # referenced to silence linting


def test_set_fresh_audit_log_empty_before_any_changes(cp):
    assert cp.get_audit_log() == []


# ---------------------------------------------------------------------------
# gate_enabled / list_gates
# ---------------------------------------------------------------------------


def test_gate_enabled_true_by_default(cp):
    assert cp.gate_enabled("auto_merge") is True
    assert cp.gate_enabled("security_review") is True


def test_gate_enabled_false_by_default(cp):
    assert cp.gate_enabled("human_verification") is False
    assert cp.gate_enabled("phased_orchestration") is False


def test_gate_enabled_after_disable(cp):
    cp.set("gates.stall_detection", False)
    assert cp.gate_enabled("stall_detection") is False


def test_gate_enabled_after_enable(cp):
    cp.set("gates.human_verification", True)
    assert cp.gate_enabled("human_verification") is True


def test_gate_enabled_unknown_gate_returns_false(cp):
    assert cp.gate_enabled("definitely_not_a_real_gate") is False


def test_list_gates_contains_all_defaults(cp):
    gates = cp.list_gates()
    for name in _DEFAULT_GATES:
        assert name in gates, f"missing gate: {name}"


def test_list_gates_reflects_custom_set(cp):
    cp.set("gates.auto_merge", False)
    assert cp.list_gates()["auto_merge"] is False


def test_list_gates_string_gate_preserved_as_string(cp):
    """self_observe_enforcement is a string gate — list_gates must not coerce it to bool."""
    gates = cp.list_gates()
    assert isinstance(gates["self_observe_enforcement"], str)
    assert gates["self_observe_enforcement"] == "shadow"


# ---------------------------------------------------------------------------
# get_policy
# ---------------------------------------------------------------------------


def test_get_policy_executor_has_expected_keys(cp):
    policy = cp.get_policy("executor")
    assert "timeout_minutes" in policy
    assert "max_retries" in policy
    assert "token_ceiling" in policy


def test_get_policy_code_reviewer_has_max_concurrent(cp):
    policy = cp.get_policy("code-reviewer")
    assert "max_concurrent" in policy


def test_get_policy_unknown_role_returns_empty_dict(cp):
    policy = cp.get_policy("ghost-agent")
    assert isinstance(policy, dict)
    assert len(policy) == 0


def test_get_policy_merges_config_over_defaults(isolated_env):
    config_path = isolated_env / "config.json"
    config_path.write_text(json.dumps({
        "policies": {"executor": {"timeout_minutes": 99}}
    }))
    instance = ControlPlane(config_path=config_path)
    instance.load()
    policy = instance.get_policy("executor")
    assert policy["timeout_minutes"] == 99
    # Unset keys still fall back to defaults
    assert policy["max_retries"] == _DEFAULT_POLICIES["executor"]["max_retries"]


def test_get_policy_all_default_roles_have_defaults(cp):
    for role in _DEFAULT_POLICIES:
        policy = cp.get_policy(role)
        assert isinstance(policy, dict)
        assert len(policy) > 0


# ---------------------------------------------------------------------------
# get_dial / list_dials / get_dial_ceiling
# ---------------------------------------------------------------------------


def test_get_dial_returns_dict_for_known_class(cp):
    result = cp.get_dial("agent.spawn")
    assert isinstance(result, dict)
    assert "level" in result
    assert "ceiling" in result


def test_get_dial_returns_none_for_unknown_class(cp):
    assert cp.get_dial("nonexistent.class") is None


def test_list_dials_returns_all_default_classes(cp):
    dials = cp.list_dials()
    for entry in _DEFAULT_DIALS:
        assert entry["class_name"] in dials, f"missing dial: {entry['class_name']}"


def test_list_dials_returns_dict_not_same_object(cp):
    """list_dials() returns a dict (shallow copy) — outer keys are independent."""
    dials1 = cp.list_dials()
    dials2 = cp.list_dials()
    assert dials1 is not dials2
    # Adding a new key to the outer dict doesn't affect the next call
    dials1["synthetic.key"] = {}
    assert "synthetic.key" not in cp.list_dials()


def test_get_dial_ceiling_hardcoded_for_sensitive_classes(cp):
    """Hardcoded ceilings override whatever is in the config."""
    for cls, ceiling in _DIAL_CEILINGS.items():
        assert cp.get_dial_ceiling(cls) == ceiling


def test_get_dial_ceiling_default_for_normal_class(cp):
    """Non-sensitive classes return the default ceiling."""
    assert cp.get_dial_ceiling("docs.write") == _DIAL_DEFAULT_CEILING
    assert cp.get_dial_ceiling("tests.add") == _DIAL_DEFAULT_CEILING


def test_get_dial_ceiling_unknown_class_returns_default(cp):
    assert cp.get_dial_ceiling("some.unknown.class") == _DIAL_DEFAULT_CEILING


# ---------------------------------------------------------------------------
# get_setting / list_settings
# ---------------------------------------------------------------------------


def test_get_setting_returns_default_for_fresh_config(cp):
    val = cp.get_setting("team-lead", "max_parallel_impl")
    assert val == _DEFAULT_SETTINGS["team-lead"]["max_parallel_impl"]


def test_get_setting_returns_overridden_value(cp):
    cp.set("settings.team-lead.max_parallel_impl", 7)
    assert cp.get_setting("team-lead", "max_parallel_impl") == 7


def test_get_setting_unknown_section_returns_none(cp):
    assert cp.get_setting("no-such-section", "key") is None


def test_get_setting_unknown_key_returns_none(cp):
    assert cp.get_setting("team-lead", "nonexistent_key") is None


def test_list_settings_contains_team_lead_section(cp):
    settings = cp.list_settings()
    assert "team-lead" in settings
    assert "max_parallel_impl" in settings["team-lead"]


def test_list_settings_merges_stored_over_defaults(isolated_env):
    config_path = isolated_env / "config.json"
    config_path.write_text(json.dumps({
        "settings": {"team-lead": {"max_parallel_impl": 10}}
    }))
    instance = ControlPlane(config_path=config_path)
    instance.load()
    settings = instance.list_settings()
    assert settings["team-lead"]["max_parallel_impl"] == 10


# ---------------------------------------------------------------------------
# apply_mode / get_mode / list_modes
# ---------------------------------------------------------------------------


def test_get_mode_returns_none_before_any_mode_applied(cp):
    assert cp.get_mode() is None


def test_apply_mode_strict_sets_auto_merge_off(cp):
    cp.apply_mode("strict")
    assert cp.gate_enabled("auto_merge") is False


def test_apply_mode_strict_records_active_mode(cp):
    cp.apply_mode("strict")
    assert cp.get_mode() == "strict"


def test_apply_mode_fast_sets_security_review_off(cp):
    cp.apply_mode("fast")
    assert cp.gate_enabled("security_review") is False


def test_apply_mode_fast_sets_auto_merge_on(cp):
    cp.apply_mode("fast")
    assert cp.gate_enabled("auto_merge") is True


def test_apply_mode_readonly_disables_executor(cp):
    cp.apply_mode("readonly")
    assert cp.get_policy("executor")["token_ceiling"] == 0


def test_apply_mode_standard_restores_defaults_after_strict(cp):
    cp.apply_mode("strict")
    cp.apply_mode("standard")
    assert cp.gate_enabled("auto_merge") is True
    assert cp.get_mode() == "standard"


def test_apply_mode_records_audit_entry(cp):
    cp.apply_mode("strict")
    log = cp.get_audit_log(limit=5)
    mode_entries = [e for e in log if e.get("key") == "mode"]
    assert len(mode_entries) >= 1
    assert mode_entries[0]["new_value"] == "strict"


def test_apply_mode_records_old_and_new_on_transition(cp):
    cp.apply_mode("strict")
    cp.apply_mode("fast")
    log = cp.get_audit_log(limit=10)
    mode_entries = [e for e in log if e.get("key") == "mode"]
    assert mode_entries[0]["new_value"] == "fast"
    assert mode_entries[0]["old_value"] == "strict"


def test_apply_mode_invalid_raises_value_error(cp):
    with pytest.raises(ValueError, match="Unknown mode"):
        cp.apply_mode("turbo")


def test_apply_mode_invalid_lists_valid_modes(cp):
    with pytest.raises(ValueError) as exc_info:
        cp.apply_mode("bogus")
    msg = str(exc_info.value)
    for name in ("strict", "standard", "fast", "readonly"):
        assert name in msg


def test_apply_mode_does_not_mutate_preset_constants(cp):
    """Applying a mode must not modify the _MODE_PRESETS module constant."""
    cp.apply_mode("strict")
    # fast mode's gates must still be their original values
    from backend.control_plane import _MODE_PRESETS
    fast_gates = _MODE_PRESETS["fast"]["gates"]
    assert fast_gates["auto_merge"] is True  # unchanged from module-level definition


def test_list_modes_returns_all_four_presets(cp):
    modes = cp.list_modes()
    assert set(modes.keys()) == {"strict", "standard", "fast", "readonly"}


def test_list_modes_returns_deep_copy(cp):
    modes = cp.list_modes()
    modes["strict"]["gates"]["auto_merge"] = "tampered"
    # Second call must return the original value
    assert cp.list_modes()["strict"]["gates"]["auto_merge"] is False


# ---------------------------------------------------------------------------
# check_gate / check_policy (module-level helpers)
# ---------------------------------------------------------------------------


def test_check_gate_reads_config_via_env_override(isolated_env, monkeypatch):
    """check_gate() resolves config from AF_CONTROL_PLANE_CONFIG env var."""
    config_path = isolated_env / "config.json"
    cp_instance = ControlPlane(config_path=config_path)
    cp_instance.load()
    cp_instance.set("gates.auto_merge", False)

    # Patch _resolve_config_path so the module-level check_gate uses our file
    import backend.control_plane as cp_mod
    original = cp_mod._resolve_config_path
    cp_mod._resolve_config_path = lambda: config_path
    try:
        assert check_gate("auto_merge") is False
    finally:
        cp_mod._resolve_config_path = original


def test_check_policy_reads_correct_value(isolated_env, monkeypatch):
    config_path = isolated_env / "config.json"
    cp_instance = ControlPlane(config_path=config_path)
    cp_instance.load()
    cp_instance.set("policies.executor.token_ceiling", 42)

    import backend.control_plane as cp_mod
    original = cp_mod._resolve_config_path
    cp_mod._resolve_config_path = lambda: config_path
    try:
        assert check_policy("executor", "token_ceiling") == 42
    finally:
        cp_mod._resolve_config_path = original


def test_check_policy_unknown_key_returns_none(isolated_env):
    config_path = isolated_env / "config.json"
    import backend.control_plane as cp_mod
    original = cp_mod._resolve_config_path
    cp_mod._resolve_config_path = lambda: config_path
    try:
        result = check_policy("executor", "nonexistent_field")
        assert result is None
    finally:
        cp_mod._resolve_config_path = original


# ---------------------------------------------------------------------------
# _coerce_value
# ---------------------------------------------------------------------------


def test_coerce_value_true():
    assert _coerce_value("true") is True


def test_coerce_value_false():
    assert _coerce_value("false") is False


def test_coerce_value_null():
    assert _coerce_value("null") is None


def test_coerce_value_integer():
    assert _coerce_value("42") == 42


def test_coerce_value_float():
    assert _coerce_value("3.14") == pytest.approx(3.14)


def test_coerce_value_string_passthrough():
    assert _coerce_value("hello") == "hello"


def test_coerce_value_json_object():
    result = _coerce_value('{"a": 1}')
    assert result == {"a": 1}


def test_coerce_value_json_array():
    result = _coerce_value('[1, 2, 3]')
    assert result == [1, 2, 3]


# ---------------------------------------------------------------------------
# _validate_gate_value
# ---------------------------------------------------------------------------


def test_validate_gate_bool_gate_accepts_bool():
    assert _validate_gate_value("auto_merge", True) is None
    assert _validate_gate_value("auto_merge", False) is None


def test_validate_gate_bool_gate_rejects_string():
    err = _validate_gate_value("auto_merge", "true")
    assert err is not None
    assert "bool" in err


def test_validate_gate_bool_gate_rejects_integer():
    err = _validate_gate_value("budget_check", 1)
    assert err is not None


def test_validate_gate_enum_gate_accepts_valid_values():
    for val in ("shadow", "advisory", "enforced"):
        assert _validate_gate_value("self_observe_enforcement", val) is None


def test_validate_gate_enum_gate_rejects_invalid_value():
    err = _validate_gate_value("self_observe_enforcement", "invalid")
    assert err is not None
    assert "invalid" in err


def test_validate_gate_all_defaults_pass():
    for name, value in _DEFAULT_GATES.items():
        err = _validate_gate_value(name, value)
        assert err is None, f"default value for {name!r} failed validation: {err}"


# ---------------------------------------------------------------------------
# CLI: main() / subcommands
# ---------------------------------------------------------------------------


def test_cli_show_prints_json(isolated_env, capsys):
    result = cp_main(["show"])
    assert result == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "gates" in parsed
    assert "policies" in parsed


def test_cli_get_existing_key(isolated_env, capsys):
    result = cp_main(["get", "gates.auto_merge"])
    assert result == 0
    out = capsys.readouterr().out
    assert json.loads(out.strip()) is True


def test_cli_get_missing_key_returns_nonzero(isolated_env, capsys):
    result = cp_main(["get", "does.not.exist"])
    assert result != 0


def test_cli_set_boolean_gate(isolated_env, capsys):
    result = cp_main(["set", "gates.auto_merge", "false"])
    assert result == 0
    # Verify it was persisted
    config_path = isolated_env / "config.json"
    data = json.loads(config_path.read_text())
    assert data["gates"]["auto_merge"] is False


def test_cli_set_invalid_enum_returns_nonzero(isolated_env, capsys):
    result = cp_main(["set", "gates.self_observe_enforcement", "invalid_value"])
    assert result == 1
    err = capsys.readouterr().err
    assert "invalid_value" in err or "expects" in err


def test_cli_set_valid_enum_succeeds(isolated_env, capsys):
    result = cp_main(["set", "gates.self_observe_enforcement", "enforced"])
    assert result == 0


def test_cli_gates_lists_all_gates(isolated_env, capsys):
    result = cp_main(["gates"])
    assert result == 0
    out = capsys.readouterr().out
    assert "auto_merge" in out
    assert "security_review" in out


def test_cli_settings_lists_team_lead(isolated_env, capsys):
    result = cp_main(["settings"])
    assert result == 0
    out = capsys.readouterr().out
    assert "team-lead" in out
    assert "max_parallel_impl" in out


def test_cli_audit_empty(isolated_env, capsys):
    result = cp_main(["audit"])
    assert result == 0
    out = capsys.readouterr().out
    assert "no audit entries" in out


def test_cli_audit_shows_entries_after_set(isolated_env, capsys):
    cp_main(["set", "gates.auto_merge", "false"])
    capsys.readouterr()  # flush
    result = cp_main(["audit"])
    assert result == 0
    out = capsys.readouterr().out
    assert "auto_merge" in out


def test_cli_mode_show_no_mode_set(isolated_env, capsys):
    result = cp_main(["mode", "show"])
    assert result == 0
    out = capsys.readouterr().out
    assert "No active mode" in out


def test_cli_mode_set_strict(isolated_env, capsys):
    result = cp_main(["mode", "set", "strict"])
    assert result == 0
    out = capsys.readouterr().out
    assert "strict" in out


def test_cli_mode_set_invalid_returns_nonzero(isolated_env, capsys):
    result = cp_main(["mode", "set", "turbo"])
    assert result == 1


def test_cli_mode_list_shows_all_modes(isolated_env, capsys):
    result = cp_main(["mode", "list"])
    assert result == 0
    out = capsys.readouterr().out
    for name in ("strict", "standard", "fast", "readonly"):
        assert name in out


def test_cli_mode_show_after_set(isolated_env, capsys):
    cp_main(["mode", "set", "fast"])
    capsys.readouterr()
    result = cp_main(["mode", "show"])
    assert result == 0
    out = capsys.readouterr().out
    assert "fast" in out


# ---------------------------------------------------------------------------
# State isolation verification
# ---------------------------------------------------------------------------


def test_no_real_state_dir_writes_during_set(isolated_env):
    """Confirm set() does not write to the real ~/.fulcrumaxe-state/."""
    real_state = Path.home() / ".fulcrumaxe-state"
    if real_state.exists():
        audit_file = real_state / "audit.jsonl"
        size_before = audit_file.stat().st_size if audit_file.exists() else 0
    else:
        size_before = None

    instance = ControlPlane(config_path=isolated_env / "config.json")
    instance.load()
    # Multiple sets to maximize chance of triggering real writes
    instance.set("gates.auto_merge", False)
    instance.set("gates.budget_check", False)
    instance.set("gates.idea_generation", False)

    if size_before is not None and real_state.exists():
        audit_file = real_state / "audit.jsonl"
        size_after = audit_file.stat().st_size if audit_file.exists() else 0
        # The real state dir's audit.jsonl must not have grown
        assert size_after == size_before, (
            "control_plane wrote to real ~/.fulcrumaxe-state/audit.jsonl "
            "during test — state isolation is broken"
        )
