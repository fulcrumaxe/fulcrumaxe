"""
Tests for control plane mode presets — apply_mode(), get_mode(), list_modes(),
check_gate(), check_policy().
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.control_plane import ControlPlane, check_gate, check_policy, _MODE_PRESETS


@pytest.fixture()
def cp(tmp_path):
    """Fresh ControlPlane backed by a temp config file."""
    instance = ControlPlane(config_path=tmp_path / "config.json")
    instance.load()
    return instance


# ---------------------------------------------------------------------------
# list_modes
# ---------------------------------------------------------------------------

def test_list_modes_returns_all_four(cp):
    modes = cp.list_modes()
    assert set(modes.keys()) == {"strict", "standard", "fast", "readonly"}


def test_list_modes_returns_copy(cp):
    modes1 = cp.list_modes()
    modes1["strict"]["gates"]["auto_merge"] = "tampered"
    modes2 = cp.list_modes()
    # The original _MODE_PRESETS should not be mutated
    assert modes2["strict"]["gates"]["auto_merge"] is False


# ---------------------------------------------------------------------------
# apply_mode — strict
# ---------------------------------------------------------------------------

def test_apply_strict_sets_auto_merge_off(cp):
    cp.apply_mode("strict")
    assert cp.gate_enabled("auto_merge") is False


def test_apply_strict_sets_active_mode(cp):
    cp.apply_mode("strict")
    assert cp.get_mode() == "strict"


def test_apply_strict_executor_token_ceiling(cp):
    cp.apply_mode("strict")
    assert cp.get_policy("executor")["token_ceiling"] == 300_000


def test_apply_strict_max_parallel_impl_1(cp):
    cp.apply_mode("strict")
    assert cp.get_setting("team-lead", "max_parallel_impl") == 1


def test_apply_strict_security_review_on(cp):
    cp.apply_mode("strict")
    assert cp.gate_enabled("security_review") is True


def test_apply_strict_audit_log_entry(cp):
    cp.apply_mode("strict")
    log = cp.get_audit_log(limit=5)
    mode_entries = [e for e in log if e.get("key") == "mode"]
    assert len(mode_entries) >= 1
    entry = mode_entries[0]
    assert entry["new_value"] == "strict"
    assert entry["old_value"] is None


# ---------------------------------------------------------------------------
# apply_mode — fast
# ---------------------------------------------------------------------------

def test_apply_fast_sets_auto_merge_on(cp):
    cp.apply_mode("fast")
    assert cp.gate_enabled("auto_merge") is True


def test_apply_fast_sets_security_review_off(cp):
    cp.apply_mode("fast")
    assert cp.gate_enabled("security_review") is False


def test_apply_fast_executor_token_ceiling(cp):
    cp.apply_mode("fast")
    assert cp.get_policy("executor")["token_ceiling"] == 800_000


def test_apply_fast_max_parallel_impl_5(cp):
    cp.apply_mode("fast")
    assert cp.get_setting("team-lead", "max_parallel_impl") == 5


def test_apply_fast_budget_check_off(cp):
    cp.apply_mode("fast")
    assert cp.gate_enabled("budget_check") is False


# ---------------------------------------------------------------------------
# apply_mode — readonly
# ---------------------------------------------------------------------------

def test_apply_readonly_disables_idea_generation(cp):
    cp.apply_mode("readonly")
    assert cp.gate_enabled("idea_generation") is False


def test_apply_readonly_executor_token_ceiling_zero(cp):
    cp.apply_mode("readonly")
    assert cp.get_policy("executor")["token_ceiling"] == 0


def test_apply_readonly_auto_merge_off(cp):
    cp.apply_mode("readonly")
    assert cp.gate_enabled("auto_merge") is False


def test_apply_readonly_max_parallel_impl_zero(cp):
    cp.apply_mode("readonly")
    assert cp.get_setting("team-lead", "max_parallel_impl") == 0


# ---------------------------------------------------------------------------
# apply_mode — standard
# ---------------------------------------------------------------------------

def test_apply_standard_restores_auto_merge(cp):
    cp.apply_mode("strict")  # disable auto_merge
    cp.apply_mode("standard")  # should restore to default (True)
    assert cp.gate_enabled("auto_merge") is True


def test_apply_standard_records_mode(cp):
    cp.apply_mode("standard")
    assert cp.get_mode() == "standard"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_mode_change_records_old_and_new(cp):
    cp.apply_mode("strict")
    cp.apply_mode("fast")
    log = cp.get_audit_log(limit=10)
    mode_entries = [e for e in log if e.get("key") == "mode"]
    assert len(mode_entries) >= 2
    # Newest first: last applied is fast
    assert mode_entries[0]["new_value"] == "fast"
    assert mode_entries[0]["old_value"] == "strict"


# ---------------------------------------------------------------------------
# Preserve unmentioned keys
# ---------------------------------------------------------------------------

def test_apply_fast_preserves_custom_policy_key(cp):
    """Fast mode does not list project-manager policy — it should be left untouched."""
    cp.set("policies.project-manager.timeout_minutes", 99)
    cp.apply_mode("fast")
    assert cp.get_policy("project-manager")["timeout_minutes"] == 99


def test_apply_strict_preserves_unlisted_gate(cp):
    """Strict lists all 6 gates, but adding a custom gate should survive if not in preset."""
    cp.set("gates.custom_feature", True)
    cp.apply_mode("strict")
    assert cp.gate_enabled("custom_feature") is True


# ---------------------------------------------------------------------------
# Invalid mode name
# ---------------------------------------------------------------------------

def test_invalid_mode_raises_value_error(cp):
    with pytest.raises(ValueError, match="Unknown mode"):
        cp.apply_mode("turbo")


def test_invalid_mode_message_lists_valid(cp):
    with pytest.raises(ValueError) as exc_info:
        cp.apply_mode("bogus")
    msg = str(exc_info.value)
    for name in ("strict", "standard", "fast", "readonly"):
        assert name in msg


# ---------------------------------------------------------------------------
# check_gate convenience function
# ---------------------------------------------------------------------------

def test_check_gate_after_strict(tmp_path):
    cp = ControlPlane(config_path=tmp_path / "config.json")
    cp.load()
    cp.apply_mode("strict")

    # Patch the module-level _resolve_config_path to point to our temp file
    import backend.control_plane as cp_module
    original_fn = cp_module._resolve_config_path
    cp_module._resolve_config_path = lambda: tmp_path / "config.json"
    try:
        assert check_gate("auto_merge") is False
    finally:
        cp_module._resolve_config_path = original_fn


def test_check_gate_after_fast(tmp_path):
    cp = ControlPlane(config_path=tmp_path / "config.json")
    cp.load()
    cp.apply_mode("fast")

    import backend.control_plane as cp_module
    original_fn = cp_module._resolve_config_path
    cp_module._resolve_config_path = lambda: tmp_path / "config.json"
    try:
        assert check_gate("auto_merge") is True
        assert check_gate("security_review") is False
    finally:
        cp_module._resolve_config_path = original_fn


# ---------------------------------------------------------------------------
# check_policy convenience function
# ---------------------------------------------------------------------------

def test_check_policy_strict_executor_ceiling(tmp_path):
    cp = ControlPlane(config_path=tmp_path / "config.json")
    cp.load()
    cp.apply_mode("strict")

    import backend.control_plane as cp_module
    original_fn = cp_module._resolve_config_path
    cp_module._resolve_config_path = lambda: tmp_path / "config.json"
    try:
        assert check_policy("executor", "token_ceiling") == 300_000
    finally:
        cp_module._resolve_config_path = original_fn


def test_check_policy_fast_executor_ceiling(tmp_path):
    cp = ControlPlane(config_path=tmp_path / "config.json")
    cp.load()
    cp.apply_mode("fast")

    import backend.control_plane as cp_module
    original_fn = cp_module._resolve_config_path
    cp_module._resolve_config_path = lambda: tmp_path / "config.json"
    try:
        assert check_policy("executor", "token_ceiling") == 800_000
    finally:
        cp_module._resolve_config_path = original_fn
