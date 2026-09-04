"""
Schema validator — validates JSON configuration files against embedded Python schema definitions.

Uses stdlib only (no jsonschema dependency). The validation engine is a simple recursive
type checker that walks a schema tree and a data tree in parallel, collecting errors.

Schemas are defined as Python dicts with the following structure:
    {
        "type": "object" | "str" | "int" | "float" | "bool" | "list",
        "required": ["field1", "field2"],           # required fields (object only)
        "properties": {                              # per-field schemas (object only)
            "field1": {"type": "str"},
        },
        "values": {"type": "bool"},                 # schema for all values (object only)
        "items": {"type": "int"},                   # schema for all items (list only)
        "min": 0,                                   # minimum value (int/float only)
        "max": 100,                                 # maximum value (int/float only)
        "enum": ["a", "b"],                         # allowed values (any type)
    }

CLI:
    python backend/schema_validator.py validate                    # validate all known files
    python backend/schema_validator.py validate --file config.json # validate one file
    python backend/schema_validator.py schemas                     # list known schemas
    python backend/schema_validator.py check config.json           # exit 0 if valid, exit 1 if not

Usage (library):
    from backend.schema_validator import SchemaValidator
    sv = SchemaValidator()
    errors = sv.validate(data, "config")
    errors = sv.validate_file(Path(".autonomous-team/config.json"), "config")
    results = sv.validate_all()
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running as a script from repo root: `python backend/schema_validator.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

_POLICY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "timeout_minutes": {"type": "int", "min": 0},
        "max_retries": {"type": "int", "min": 0},
        "token_ceiling": {"type": "int", "min": 0},
    },
}

CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["version", "boss_github_username"],
    "properties": {
        "version": {"type": "str"},
        "boss_github_username": {"type": "str"},
        "bot_account": {"type": "str"},
        # D#1840 (CWE-290): the immutable trust key. The login fields above
        # stay required/typed as before and remain human-readable
        # annotations — they are NOT the trust key once these are present.
        # Optional so an adopter's existing config does not hard-fail on
        # upgrade; self-heals on first successful resolution (see
        # scripts/lib/trust_id_resolver.py).
        "boss_github_user_id": {"type": "str"},
        "bot_account_id": {"type": "str"},
        "budget": {
            "type": "object",
            "properties": {
                "session_ceiling": {"type": "int", "min": 0},
                "per_agent_ceiling": {"type": "int", "min": 0},
                "warn_threshold_pct": {"type": "int", "min": 0, "max": 100},
            },
        },
        "gates": {
            "type": "object",
            # Most gates are booleans; the `values` schema applies to all keys
            # NOT covered by an explicit `properties` entry below.
            "values": {"type": "bool"},
            # Per-gate overrides for non-boolean gates.
            "properties": {
                "self_observe_enforcement": {
                    "type": "str",
                    "enum": ["shadow", "advisory", "enforced"],
                },
            },
        },
        "policies": {
            "type": "object",
            "properties": {
                "executor": _POLICY_SCHEMA,
                "code-reviewer": _POLICY_SCHEMA,
                "security-reviewer": _POLICY_SCHEMA,
                "project-manager": _POLICY_SCHEMA,
            },
        },
        "settings": {"type": "object"},
    },
}

REGISTRY_SCHEMA: dict = {
    "type": "object",
    "required": ["version"],
    "properties": {
        "version": {"type": "int"},
        "synced_at": {"type": "str"},
        "discussions": {
            "type": "list",
            "items": {
                "type": "object",
                "required": ["number", "title", "status"],
                "properties": {
                    "number": {"type": "int"},
                    "title": {"type": "str"},
                    "status": {"type": "str"},
                },
            },
        },
        "velocity": {"type": "object"},
    },
}

AGENT_PROFILES_SCHEMA: dict = {
    "type": "object",
    "required": ["generated_at"],
    "properties": {
        "generated_at": {"type": "str"},
        "profiles": {
            "type": "object",
            "values": {
                "type": "object",
                "properties": {
                    "total_tasks": {"type": "int", "min": 0},
                    "success_rate": {"type": "float", "min": 0.0, "max": 1.0},
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Schema registry — maps schema_name -> (schema_dict, relative_file_path)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCHEMA_REGISTRY: dict[str, tuple[dict, Path]] = {
    "config": (CONFIG_SCHEMA, _REPO_ROOT / ".autonomous-team" / "config.json"),
    "registry": (REGISTRY_SCHEMA, _REPO_ROOT / ".autonomous-team" / "registry.json"),
    "agent-profiles": (
        AGENT_PROFILES_SCHEMA,
        _REPO_ROOT / ".autonomous-team" / "agent-profiles.json",
    ),
}

# Python type name -> set of accepted Python types
_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": (int, float),  # type: ignore[assignment]  # int is a valid float
    "bool": bool,
    "list": list,
    "object": dict,
}


# ---------------------------------------------------------------------------
# Validation engine
# ---------------------------------------------------------------------------


def _validate_node(data: object, schema: dict, path: str) -> list[str]:
    """Recursively validate *data* against *schema*, collecting error strings."""
    errors: list[str] = []
    warnings: list[str] = []

    schema_type = schema.get("type")
    if schema_type is None:
        # No type constraint — anything goes.
        return errors

    # Type check
    expected_types = _TYPE_MAP.get(schema_type)
    if expected_types is None:
        errors.append(f"{path}: unknown schema type '{schema_type}'")
        return errors

    # bool is a subclass of int in Python — check bool first to avoid false positives.
    if schema_type == "int" and isinstance(data, bool):
        errors.append(
            f"{path}: expected int, got bool"
        )
        return errors

    if not isinstance(data, expected_types):  # type: ignore[arg-type]
        actual = type(data).__name__
        errors.append(f"{path}: expected {schema_type}, got {actual}")
        return errors  # can't recurse into wrong-typed data

    # Enum check
    enum = schema.get("enum")
    if enum is not None and data not in enum:
        errors.append(f"{path}: value {data!r} not in allowed values {enum!r}")

    # Range check (int / float)
    if schema_type in ("int", "float"):
        min_val = schema.get("min")
        max_val = schema.get("max")
        if min_val is not None and data < min_val:  # type: ignore[operator]
            errors.append(f"{path}: value {data} is below minimum {min_val}")
        if max_val is not None and data > max_val:  # type: ignore[operator]
            errors.append(f"{path}: value {data} is above maximum {max_val}")

    # Object-specific checks
    if schema_type == "object" and isinstance(data, dict):
        # Required fields
        required = schema.get("required", [])
        for req_field in required:
            if req_field not in data:
                errors.append(f"{path}: missing required field '{req_field}'")

        # Known-key warnings (enum_keys)
        enum_keys = schema.get("enum_keys")
        if enum_keys is not None:
            for key in data:
                if key not in enum_keys:
                    warnings.append(
                        f"{path}: unknown key '{key}' (expected one of {enum_keys!r})"
                    )

        # Per-field property schemas
        properties = schema.get("properties", {})
        for field, field_schema in properties.items():
            if field in data:
                child_errors = _validate_node(data[field], field_schema, f"{path}.{field}")
                errors.extend(child_errors)
            # Missing non-required fields are not errors.

        # Uniform values schema (all values must match)
        values_schema = schema.get("values")
        if values_schema is not None:
            for key, val in data.items():
                # Skip fields already validated via properties
                if key not in properties:
                    child_errors = _validate_node(val, values_schema, f"{path}.{key}")
                    errors.extend(child_errors)

        # Unknown key warnings (for objects with explicit properties and no values schema)
        if properties and values_schema is None and enum_keys is None:
            for key in data:
                if key not in properties:
                    warnings.append(f"{path}: unknown key '{key}'")

    # List-specific checks
    if schema_type == "list" and isinstance(data, list):
        items_schema = schema.get("items")
        if items_schema is not None:
            for idx, item in enumerate(data):
                child_errors = _validate_node(item, items_schema, f"{path}[{idx}]")
                errors.extend(child_errors)

    # Log warnings (not errors — forward compatibility)
    for warning in warnings:
        _logger.debug("schema warning: %s", warning)

    return errors


# ---------------------------------------------------------------------------
# SchemaValidator public class
# ---------------------------------------------------------------------------


class SchemaValidator:
    """
    Validates JSON configuration files against embedded Python schema definitions.

    Usage:
        sv = SchemaValidator()
        errors = sv.validate(data, "config")           # returns list[str]
        errors = sv.validate_file(path, "config")      # loads file then validates
        results = sv.validate_all()                    # {filename: [errors]}
    """

    def validate(self, data: dict, schema_name: str) -> list[str]:
        """
        Validate *data* against the named schema.

        Returns a list of error strings. An empty list means valid.
        Raises KeyError if *schema_name* is unknown.
        """
        if schema_name not in _SCHEMA_REGISTRY:
            raise KeyError(f"Unknown schema: {schema_name!r}. Known: {list(_SCHEMA_REGISTRY)}")
        schema, _ = _SCHEMA_REGISTRY[schema_name]
        return _validate_node(data, schema, schema_name)

    def validate_file(self, path: Path, schema_name: str) -> list[str]:
        """
        Load a JSON file and validate it against the named schema.

        Returns a list of error strings. Reports file-not-found gracefully (does not crash).
        """
        if not path.exists():
            return [f"file not found: {path}"]
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            return [f"JSON parse error in {path}: {exc}"]
        except OSError as exc:
            return [f"could not read {path}: {exc}"]
        return self.validate(data, schema_name)

    def validate_all(self) -> dict[str, list[str]]:
        """
        Validate all known files against their schemas.

        Returns {filename: [errors]}. Files with no errors have an empty list.
        """
        results: dict[str, list[str]] = {}
        for schema_name, (_, file_path) in _SCHEMA_REGISTRY.items():
            errors = self.validate_file(file_path, schema_name)
            results[file_path.name] = errors
        return results

    @staticmethod
    def list_schemas() -> list[str]:
        """Return the list of known schema names."""
        return list(_SCHEMA_REGISTRY.keys())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    sv = SchemaValidator()
    if args.file:
        # Validate a single file — infer schema from file name
        file_path = Path(args.file)
        stem = file_path.stem  # e.g. "config" from "config.json"
        if stem not in _SCHEMA_REGISTRY:
            print(f"error: no schema for '{file_path.name}'. Known: {sv.list_schemas()}")
            return 2
        errors = sv.validate_file(file_path, stem)
        if errors:
            print(f"{file_path.name}: {len(errors)} error(s):")
            for e in errors:
                print(f"  - {e}")
            return 1
        print(f"{file_path.name}: valid")
        return 0
    else:
        # Validate all known files
        results = sv.validate_all()
        all_valid = True
        for filename, errors in results.items():
            if errors:
                all_valid = False
                print(f"{filename}: {len(errors)} error(s):")
                for e in errors:
                    print(f"  - {e}")
            else:
                print(f"{filename}: valid")
        return 0 if all_valid else 1


def _cmd_schemas(_args: argparse.Namespace) -> int:
    sv = SchemaValidator()
    print("Known schemas:")
    for name in sv.list_schemas():
        _, file_path = _SCHEMA_REGISTRY[name]
        print(f"  {name:<20} -> {file_path}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    file_path = Path(args.file)
    stem = file_path.stem
    sv = SchemaValidator()
    if stem not in _SCHEMA_REGISTRY:
        print(f"error: no schema for '{file_path.name}'", file=sys.stderr)
        return 2
    errors = sv.validate_file(file_path, stem)
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schema_validator",
        description="Validate JSON config files against embedded schemas.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    validate_p = sub.add_parser("validate", help="Validate config files")
    validate_p.add_argument(
        "--file",
        metavar="FILE",
        default=None,
        help="Validate a single file (e.g. config.json). Default: validate all known files.",
    )

    sub.add_parser("schemas", help="List known schemas and their associated files")

    check_p = sub.add_parser(
        "check", help="Validate a file; exit 0 if valid, exit 1 if invalid"
    )
    check_p.add_argument("file", metavar="FILE", help="Path to JSON file")

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "validate": _cmd_validate,
        "schemas": _cmd_schemas,
        "check": _cmd_check,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
