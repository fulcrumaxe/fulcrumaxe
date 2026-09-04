"""Tests for backend.schema_validator."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.schema_validator import SchemaValidator, _validate_node, CONFIG_SCHEMA


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _valid_config() -> dict:
    return {
        "version": "2.0.2",
        "boss_github_username": "example-owner",
        "budget": {
            "session_ceiling": 5_000_000,
            "per_agent_ceiling": 500_000,
            "warn_threshold_pct": 80,
        },
        "gates": {
            "auto_merge": True,
            "security_review": True,
        },
        "policies": {
            "executor": {
                "timeout_minutes": 45,
                "max_retries": 2,
                "token_ceiling": 500_000,
            }
        },
    }


# ---------------------------------------------------------------------------
# AC 1: validate() returns no errors for a valid config
# ---------------------------------------------------------------------------


def test_valid_config_no_errors():
    sv = SchemaValidator()
    errors = sv.validate(_valid_config(), "config")
    assert errors == [], f"Expected no errors, got: {errors}"


# ---------------------------------------------------------------------------
# AC 2: gates with string value instead of bool raises a type error
# ---------------------------------------------------------------------------


def test_gates_string_value_is_error():
    sv = SchemaValidator()
    data = _valid_config()
    data["gates"]["auto_merge"] = "yes"  # string instead of bool
    errors = sv.validate(data, "config")
    assert errors, "Expected at least one error for string gate value"
    error_text = " ".join(errors)
    assert "bool" in error_text or "str" in error_text, (
        f"Expected type mismatch mention, got: {errors}"
    )


# ---------------------------------------------------------------------------
# AC 3: missing 'version' field is an error
# ---------------------------------------------------------------------------


def test_missing_required_version():
    sv = SchemaValidator()
    data = _valid_config()
    del data["version"]
    errors = sv.validate(data, "config")
    assert errors, "Expected error for missing required 'version'"
    error_text = " ".join(errors)
    assert "version" in error_text, f"Expected 'version' in error message, got: {errors}"


# ---------------------------------------------------------------------------
# AC 4: unknown key produces a warning (not an error)
# ---------------------------------------------------------------------------


def test_unknown_key_is_not_error():
    sv = SchemaValidator()
    data = _valid_config()
    data["totally_unknown_field"] = "surprise"
    errors = sv.validate(data, "config")
    assert errors == [], f"Expected no errors for unknown key, got: {errors}"


# ---------------------------------------------------------------------------
# AC 5: GET /validate — tested via unit call (integration endpoint checked separately)
# ---------------------------------------------------------------------------


def test_validate_all_returns_dict():
    sv = SchemaValidator()
    results = sv.validate_all()
    assert isinstance(results, dict), "validate_all() should return a dict"
    # config.json is always in the registry
    assert "config.json" in results, "config.json should appear in validate_all() results"


# ---------------------------------------------------------------------------
# AC 6: validate_all does not crash when a file is valid
# ---------------------------------------------------------------------------


def test_validate_all_does_not_crash():
    sv = SchemaValidator()
    try:
        results = sv.validate_all()
    except Exception as exc:
        pytest.fail(f"validate_all() raised an exception: {exc}")
    assert isinstance(results, dict)


# ---------------------------------------------------------------------------
# AC 9: missing files handled gracefully
# ---------------------------------------------------------------------------


def test_validate_file_missing_file():
    sv = SchemaValidator()
    errors = sv.validate_file(Path("/nonexistent/path/config.json"), "config")
    assert errors, "Expected error for missing file"
    assert "not found" in errors[0] or "not" in errors[0].lower()


# ---------------------------------------------------------------------------
# AC 10: nested validation — invalid policies.executor.timeout_minutes type
# ---------------------------------------------------------------------------


def test_nested_validation_policy_type():
    sv = SchemaValidator()
    data = _valid_config()
    data["policies"]["executor"]["timeout_minutes"] = "forty-five"  # string, not int
    errors = sv.validate(data, "config")
    assert errors, "Expected error for wrong type in nested policy"
    error_text = " ".join(errors)
    assert "timeout_minutes" in error_text or "int" in error_text, (
        f"Expected timeout_minutes or int in error, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Additional unit tests for the validation engine
# ---------------------------------------------------------------------------


def test_bool_not_accepted_as_int():
    """Python bools are subclasses of int; we must reject them for int fields."""
    errors = _validate_node(True, {"type": "int"}, "field")
    assert errors, "bool should not be accepted where int is expected"


def test_float_accepts_int():
    """An int value is a valid float (numeric widening)."""
    errors = _validate_node(5, {"type": "float"}, "field")
    assert errors == [], f"int should be accepted for float field, got: {errors}"


def test_range_min():
    errors = _validate_node(-1, {"type": "int", "min": 0}, "count")
    assert errors, "Expected error for value below minimum"


def test_range_max():
    errors = _validate_node(101, {"type": "int", "max": 100}, "pct")
    assert errors, "Expected error for value above maximum"


def test_enum_valid():
    errors = _validate_node("a", {"type": "str", "enum": ["a", "b"]}, "field")
    assert errors == []


def test_enum_invalid():
    errors = _validate_node("c", {"type": "str", "enum": ["a", "b"]}, "field")
    assert errors, "Expected error for value not in enum"


def test_list_items_validated():
    schema = {"type": "list", "items": {"type": "int"}}
    errors = _validate_node([1, "oops", 3], schema, "arr")
    assert errors, "Expected error for string item in int list"


def test_required_field_missing():
    schema = {"type": "object", "required": ["name"], "properties": {}}
    errors = _validate_node({}, schema, "obj")
    assert any("name" in e for e in errors)


def test_type_mismatch_returns_early():
    """When type is wrong, do not recurse — just report the mismatch."""
    schema = {"type": "object", "required": ["x"]}
    errors = _validate_node("not-a-dict", schema, "field")
    # Should report type error, not required field error
    assert any("object" in e or "str" in e for e in errors)


def test_unknown_schema_raises_keyerror():
    sv = SchemaValidator()
    with pytest.raises(KeyError):
        sv.validate({}, "nonexistent_schema")


def test_list_schemas():
    sv = SchemaValidator()
    schemas = sv.list_schemas()
    assert "config" in schemas
    assert "registry" in schemas
    assert "agent-profiles" in schemas


def test_validate_file_json_error(tmp_path):
    bad_file = tmp_path / "config.json"
    bad_file.write_text("{broken json", encoding="utf-8")
    sv = SchemaValidator()
    errors = sv.validate_file(bad_file, "config")
    assert errors
    assert "parse error" in errors[0] or "JSON" in errors[0]


def test_gates_all_values_bool():
    """All gate values must be bool — mixed types should error."""
    sv = SchemaValidator()
    data = _valid_config()
    data["gates"] = {"auto_merge": True, "security_review": 1}  # int, not bool
    errors = sv.validate(data, "config")
    assert errors, "Expected error for int gate value"


def test_budget_warn_threshold_out_of_range():
    sv = SchemaValidator()
    data = _valid_config()
    data["budget"]["warn_threshold_pct"] = 150  # > 100
    errors = sv.validate(data, "config")
    assert errors, "Expected error for warn_threshold_pct > 100"


def test_valid_registry():
    sv = SchemaValidator()
    data = {
        "version": 1,
        "synced_at": "2026-01-01T00:00:00Z",
        "discussions": [
            {"number": 1, "title": "Test", "status": "DONE"}
        ],
        "velocity": {},
    }
    errors = sv.validate(data, "registry")
    assert errors == [], f"Expected no errors for valid registry, got: {errors}"


def test_registry_missing_version():
    sv = SchemaValidator()
    data = {"synced_at": "2026-01-01T00:00:00Z", "discussions": [], "velocity": {}}
    errors = sv.validate(data, "registry")
    assert any("version" in e for e in errors)


def test_valid_agent_profiles():
    sv = SchemaValidator()
    data = {
        "generated_at": "2026-01-01T00:00:00Z",
        "profiles": {
            "executor": {"total_tasks": 10, "success_rate": 0.9}
        },
    }
    errors = sv.validate(data, "agent-profiles")
    assert errors == [], f"Expected no errors for valid agent profiles, got: {errors}"


def test_agent_profiles_success_rate_out_of_range():
    sv = SchemaValidator()
    data = {
        "generated_at": "2026-01-01T00:00:00Z",
        "profiles": {
            "executor": {"total_tasks": 10, "success_rate": 1.5}  # > 1.0
        },
    }
    errors = sv.validate(data, "agent-profiles")
    assert errors, "Expected error for success_rate > 1.0"


# ---------------------------------------------------------------------------
# CLI tests (new — coverage batch 2)
# ---------------------------------------------------------------------------


def test_main_validate_all(tmp_path, monkeypatch, capsys):
    """main(['validate']) validates all known files and returns 0 or 1."""
    from backend.schema_validator import main as sv_main
    # Redirect known file paths to nonexistent tmp files so they are "missing"
    # validate_file returns ["file not found: ..."] which means errors → exit 1
    # But the function should not crash.
    rc = sv_main(["validate"])
    out, err = capsys.readouterr()
    # Either exit 0 (all valid) or 1 (some errors) — should not crash
    assert rc in (0, 1)
    # Output should mention the known schema files
    combined = out + err
    assert "config.json" in combined or "valid" in combined or "error" in combined


def test_main_validate_single_file_valid(tmp_path, capsys):
    from backend.schema_validator import main as sv_main
    valid_data = {
        "version": "2.0",
        "boss_github_username": "testuser",
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(valid_data))
    rc = sv_main(["validate", "--file", str(config_file)])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "valid" in out


def test_main_validate_single_file_invalid(tmp_path, capsys):
    from backend.schema_validator import main as sv_main
    # Missing required 'boss_github_username'
    invalid_data = {"version": "2.0"}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(invalid_data))
    rc = sv_main(["validate", "--file", str(config_file)])
    out, err = capsys.readouterr()
    assert rc == 1
    combined = out + err
    assert "boss_github_username" in combined or "error" in combined.lower()


def test_main_validate_unknown_schema(tmp_path, capsys):
    from backend.schema_validator import main as sv_main
    f = tmp_path / "unknown.json"
    f.write_text("{}")
    rc = sv_main(["validate", "--file", str(f)])
    out, err = capsys.readouterr()
    assert rc == 2
    assert "unknown" in (out + err).lower() or "no schema" in (out + err).lower()


def test_main_schemas_lists_known(capsys):
    from backend.schema_validator import main as sv_main
    rc = sv_main(["schemas"])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "config" in out
    assert "registry" in out
    assert "agent-profiles" in out


def test_main_check_valid_file(tmp_path, capsys):
    from backend.schema_validator import main as sv_main
    data = {"version": "1.0", "boss_github_username": "alice"}
    f = tmp_path / "config.json"
    f.write_text(json.dumps(data))
    rc = sv_main(["check", str(f)])
    assert rc == 0


def test_main_check_invalid_file(tmp_path, capsys):
    from backend.schema_validator import main as sv_main
    # Missing required boss_github_username
    data = {"version": "1.0"}
    f = tmp_path / "config.json"
    f.write_text(json.dumps(data))
    rc = sv_main(["check", str(f)])
    assert rc == 1


def test_main_check_unknown_schema(tmp_path, capsys):
    from backend.schema_validator import main as sv_main
    f = tmp_path / "foobar.json"
    f.write_text("{}")
    rc = sv_main(["check", str(f)])
    assert rc == 2


def test_validate_file_valid_json(tmp_path):
    """validate_file with a correctly-structured JSON file returns no errors."""
    sv = SchemaValidator()
    data = {"version": "1.0", "boss_github_username": "bob"}
    f = tmp_path / "config.json"
    f.write_text(json.dumps(data))
    errors = sv.validate_file(f, "config")
    assert errors == []


def test_validate_all_with_tmp_dir(tmp_path, monkeypatch):
    """validate_all does not crash when schema files are absent."""
    sv = SchemaValidator()
    # All real schema files may or may not exist — just verify it returns a dict
    results = sv.validate_all()
    assert isinstance(results, dict)
    # All values are lists
    for v in results.values():
        assert isinstance(v, list)


# ---------------------------------------------------------------------------
# D#1840 (CWE-290) AC-9 — boss_github_user_id / bot_account_id are optional.
# ---------------------------------------------------------------------------


def test_config_with_no_id_fields_validates():
    sv = SchemaValidator()
    errors = sv.validate(_valid_config(), "config")
    assert errors == []


def test_config_with_id_fields_validates():
    sv = SchemaValidator()
    cfg = _valid_config()
    cfg["boss_github_user_id"] = "U_kgDOB3DbAw"
    cfg["bot_account_id"] = "U_kgDOEGPiOg"
    errors = sv.validate(cfg, "config")
    assert errors == []


def test_boss_github_username_stays_required_even_with_id_fields_present():
    sv = SchemaValidator()
    cfg = _valid_config()
    del cfg["boss_github_username"]
    cfg["boss_github_user_id"] = "U_kgDOB3DbAw"
    errors = sv.validate(cfg, "config")
    assert any("boss_github_username" in e for e in errors)
