"""
Static contract test for the TUI-backend JSON-RPC protocol.

Validates that tui/src/types.ts and the Python backend agree on:
- Event type names (union completeness)
- Field names for each event type
- No unexpected fields emitted by Python (besides optional 'id')

This is a pure file-parsing test — no subprocess, no API keys, runs in <2s.

The Python side of the protocol is split across two files as of the Claude
Agent SDK prompt-lane migration: backend/server.py still emits ready/done/
error directly (_emit(...) call sites), while backend/prompt_lane/sdk_lane.py
owns the rest (thinking/content/tool_use/tool_result/usage/agent_spawn/
agent_event/agent_exit) as `yield {"type": ..., ...}` dict literals — moved
there so business logic doesn't accumulate in the server.py hub. Both files
are scanned together; see py_source below.

Known issue (documented, not blocking):
  server.py emits {"session_id": None} on timeout, but DoneEvent.session_id
  is typed as `string` (non-nullable) in types.ts. The session_id nullable test is
  marked xfail to document this mismatch. Fix: make DoneEvent.session_id `string | null`.
"""

from __future__ import annotations

import re
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TYPES_TS = REPO_ROOT / "tui" / "src" / "types.ts"
SERVER_PY = REPO_ROOT / "backend" / "server.py"
SDK_LANE_PY = REPO_ROOT / "backend" / "prompt_lane" / "sdk_lane.py"

# ---------------------------------------------------------------------------
# Part 1: Canonical protocol schema (single source of truth)
# ---------------------------------------------------------------------------

PROTOCOL_SCHEMA: dict[str, dict[str, str]] = {
    "ready":        {"type": "str", "version": "str", "model": "str"},
    "thinking":     {"type": "str", "content": "str"},
    "content":      {"type": "str", "content": "str"},
    "tool_use":     {"type": "str", "tool": "str", "call_id": "str", "input": "dict"},
    "tool_result":  {"type": "str", "call_id": "str", "result": "str", "is_error": "bool"},
    "usage":        {"type": "str", "usage": "dict"},
    "done":         {"type": "str", "session_id": "str"},
    "error":        {"type": "str", "error": "str"},
    "agent_spawn":  {"type": "str", "agent_id": "str", "agent_name": "str", "parent_id": "str|null"},
    "agent_event":  {"type": "str", "agent_id": "str", "inner": "BackendEvent"},
    "agent_exit":   {"type": "str", "agent_id": "str", "exit_code": "number|null"},
    # TUI-only: created client-side from .autonomous-team/agent-feed.jsonl, never emitted by server.py.
    "agent_feed":   {"type": "str", "agent": "str", "role": "str", "event": "str", "detail": "str"},
}

# Event types that exist only in the TUI (client-created from the feed file, not emitted by server.py).
TUI_ONLY_TYPES: frozenset[str] = frozenset({"agent_feed"})

# Fields that are always allowed on any event without being in the canonical schema.
OPTIONAL_EXTRA_FIELDS = {"id"}

# ---------------------------------------------------------------------------
# Part 2: TypeScript parser
# ---------------------------------------------------------------------------

_TS_TYPE_MAP = {
    "string": "str",
    "number": "number",
    "boolean": "bool",
    "boolean;": "bool",
    "Record<string, unknown>": "dict",
    "BackendEvent": "BackendEvent",
    "string | null": "str|null",
    "number | null": "number|null",
}


def _map_ts_type(ts_type: str) -> str:
    """Map a TypeScript type string to its canonical schema representation."""
    ts_type = ts_type.strip().rstrip(";")
    return _TS_TYPE_MAP.get(ts_type, ts_type)


def parse_ts_event_interfaces(source: str) -> dict[str, dict[str, str]]:
    """
    Parse event interfaces from types.ts.

    Finds blocks like:
        export interface FooEvent extends BaseEvent {
          type: 'event_name';
          field: SomeType;
        }

    Returns a dict mapping event_type_string -> {field_name -> canonical_type}.
    """
    result: dict[str, dict[str, str]] = {}

    # Match each interface block
    interface_pattern = re.compile(
        r"export\s+interface\s+(\w+)\s+extends\s+BaseEvent\s*\{([^}]+)\}",
        re.DOTALL,
    )

    for match in interface_pattern.finditer(source):
        block = match.group(2)
        fields: dict[str, str] = {}
        event_type: str | None = None

        # Extract each field declaration: fieldName: TypeExpr;
        field_pattern = re.compile(r"(\w+)\s*\??:\s*([^;]+);")
        for fmatch in field_pattern.finditer(block):
            field_name = fmatch.group(1)
            ts_type = fmatch.group(2).strip()

            if field_name == "type":
                # Extract the literal string value
                lit = re.match(r"['\"](\w+)['\"]", ts_type)
                if lit:
                    event_type = lit.group(1)
                fields["type"] = "str"
            else:
                # Handle inline object types (e.g. { input_tokens: number; ... })
                if ts_type.startswith("{"):
                    fields[field_name] = "dict"
                else:
                    fields[field_name] = _map_ts_type(ts_type)

        if event_type is not None:
            result[event_type] = fields

    return result


def parse_ts_backend_event_union(source: str) -> set[str]:
    """
    Parse the BackendEvent union type to get the set of interface names it includes.

    Returns a set of event type strings (not interface names) by cross-referencing
    the union members against the parsed interfaces.
    """
    # Find the BackendEvent type alias
    union_match = re.search(
        r"export\s+type\s+BackendEvent\s*=\s*([\s\S]+?);",
        source,
    )
    if not union_match:
        return set()

    union_body = union_match.group(1)
    # Extract each member (interface name after |)
    members = re.findall(r"\|\s*(\w+)", union_body)
    # Also catch the first member without a leading |
    first = re.match(r"\s*\|?\s*(\w+)", union_body)
    if first:
        members.insert(0, first.group(1))
    return set(members)


# ---------------------------------------------------------------------------
# Part 3: Python emit parser
# ---------------------------------------------------------------------------

def parse_python_emit_sites(source: str) -> dict[str, dict[str, str]]:
    """
    Parse wire-protocol event dict literals from the Python source.

    Finds `{"type": "...", ...}` dict literals in real emit statements:
      - `_emit({...})`               — backend/server.py's ready/done/error sites
      - `yield {...}`                — backend/prompt_lane/sdk_lane.py's mapper
      - `return [{...}]`             — sdk_lane.py's _map_stream_event, which
                                        returns a one-item list rather than
                                        yielding (called via a plain `for` loop,
                                        not as its own generator)
    and extracts their keys, using the "type": "..." value to identify which
    event type they belong to. Deliberately does NOT match bare `{...}` text
    anywhere in the file — that would also catch docstring/comment examples
    (e.g. the module docstring's illustrative protocol sample) and unrelated
    dict literals (e.g. main()'s HTTP-mode print()) that are not real emit
    sites.

    Only matches ONE level of dict nesting (no nested {...} inside the
    literal itself) — sdk_lane.py's event dicts are deliberately written
    flat (nested values like "usage" or "inner" are built as a separate
    local variable first) so this stays a simple non-recursive regex
    instead of needing a real parser.

    Returns a dict mapping event_type_string -> set of field names emitted.
    Note: field types are not extracted from Python (untyped), so this returns
    the field name -> "?" mapping.
    """
    result: dict[str, set[str]] = {}

    # Match a `{ "type": "...", ... }` dict literal (no nested braces) right
    # after _emit(, yield, or return [ — i.e. an actual emit/yield/return
    # statement, not prose.
    emit_pattern = re.compile(
        r"(?:_emit\(\s*|yield\s*|return\s*\[\s*)\{([^{}]*\"type\"\s*:\s*\"\w+\"[^{}]*)\}",
        re.DOTALL,
    )

    for match in emit_pattern.finditer(source):
        dict_body = match.group(1)
        # Extract all key: value pairs
        key_pattern = re.compile(r"['\"](\w+)['\"]:\s*(?:['\"]([^'\"]*)['\"]|[^,\n]+)")
        keys: dict[str, str] = {}
        event_type: str | None = None
        for kmatch in key_pattern.finditer(dict_body):
            key = kmatch.group(1)
            val = kmatch.group(2)  # Only set for string literals
            keys[key] = val or ""
            if key == "type" and val:
                event_type = val

        if event_type:
            if event_type not in result:
                result[event_type] = set()
            result[event_type].update(keys.keys())

    # Convert sets to dicts with placeholder type "?"
    return {k: {f: "?" for f in fields} for k, fields in result.items()}


def parse_python_match_branches(source: str) -> set[str]:
    """
    Parse match event.type: case ... branches from server.py.

    Returns the set of AgentEventType enum member names handled.
    (We map these to protocol event type strings via the match arms' _emit calls.)
    """
    # Find all case AgentEventType.XXX: blocks
    case_pattern = re.compile(r"case\s+AgentEventType\.(\w+)\s*:")
    return {m.group(1) for m in case_pattern.finditer(source)}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ts_source() -> str:
    return TYPES_TS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def py_source() -> str:
    # Concatenated: server.py (ready/done/error) + sdk_lane.py (everything
    # else — moved out of the hub by the Claude Agent SDK migration).
    return (
        SERVER_PY.read_text(encoding="utf-8")
        + "\n"
        + SDK_LANE_PY.read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def ts_interfaces(ts_source) -> dict[str, dict[str, str]]:
    return parse_ts_event_interfaces(ts_source)


@pytest.fixture(scope="module")
def py_emit_sites(py_source) -> dict[str, dict[str, str]]:
    return parse_python_emit_sites(py_source)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCanonicalSchemaCompleteness:
    """Canonical schema must define all expected protocol event types."""

    def test_all_expected_types_in_schema(self):
        expected = {
            "ready", "thinking", "content", "tool_use", "tool_result",
            "usage", "done", "error", "agent_spawn", "agent_event", "agent_exit",
            # TUI-only events (created client-side from agent-feed.jsonl):
            "agent_feed",
        }
        assert set(PROTOCOL_SCHEMA.keys()) == expected, (
            f"Canonical schema mismatch. Extra: {set(PROTOCOL_SCHEMA) - expected}, "
            f"Missing: {expected - set(PROTOCOL_SCHEMA)}"
        )


class TestTypeScriptMatchesCanonical:
    """TypeScript event interfaces must match the canonical schema."""

    def test_ts_interfaces_parsed(self, ts_interfaces):
        assert len(ts_interfaces) >= 11, (
            f"Expected at least 11 event interfaces, got {len(ts_interfaces)}: "
            f"{list(ts_interfaces.keys())}"
        )

    def test_ts_event_types_match_canonical(self, ts_interfaces):
        canonical_types = set(PROTOCOL_SCHEMA.keys())
        ts_types = set(ts_interfaces.keys())
        missing_from_ts = canonical_types - ts_types
        extra_in_ts = ts_types - canonical_types
        assert not missing_from_ts, f"Types in canonical schema but not in TS: {missing_from_ts}"
        assert not extra_in_ts, f"Types in TS but not in canonical schema: {extra_in_ts}"

    def test_ts_field_names_match_canonical(self, ts_interfaces):
        mismatches = []
        for event_type, canonical_fields in PROTOCOL_SCHEMA.items():
            if event_type not in ts_interfaces:
                continue
            ts_fields = set(ts_interfaces[event_type].keys())
            canonical_field_names = set(canonical_fields.keys())
            missing = canonical_field_names - ts_fields
            if missing:
                mismatches.append(f"{event_type}: missing fields {missing} in TS")
        assert not mismatches, "\n".join(mismatches)

    def test_ts_backend_event_union_complete(self, ts_source, ts_interfaces):
        """BackendEvent union must include all event interface names."""
        union_members = parse_ts_backend_event_union(ts_source)
        # Build a map of interface_name -> event_type from parsed interfaces
        # by finding interface declarations
        interface_name_pattern = re.compile(
            r"export\s+interface\s+(\w+)\s+extends\s+BaseEvent"
        )
        iface_names = set(interface_name_pattern.findall(ts_source))
        # All interfaces extending BaseEvent should be in the union
        not_in_union = iface_names - union_members
        assert not not_in_union, (
            f"Interfaces extending BaseEvent not in BackendEvent union: {not_in_union}"
        )


class TestPythonMatchesCanonical:
    """Python _emit() call sites must match the canonical schema."""

    def test_py_emit_sites_parsed(self, py_emit_sites):
        assert len(py_emit_sites) >= 9, (
            f"Expected at least 9 event types emitted, got {len(py_emit_sites)}: "
            f"{list(py_emit_sites.keys())}"
        )

    def test_py_event_types_match_canonical(self, py_emit_sites):
        # TUI-only types are created client-side and are never emitted by server.py.
        canonical_types = set(PROTOCOL_SCHEMA.keys()) - TUI_ONLY_TYPES
        py_types = set(py_emit_sites.keys())
        missing_from_py = canonical_types - py_types
        assert not missing_from_py, (
            f"Canonical event types never emitted by Python: {missing_from_py}"
        )

    def test_py_required_fields_present(self, py_emit_sites):
        """Every required canonical field must appear in at least one Python emit site."""
        mismatches = []
        for event_type, canonical_fields in PROTOCOL_SCHEMA.items():
            if event_type not in py_emit_sites:
                continue
            py_fields = set(py_emit_sites[event_type].keys())
            required = set(canonical_fields.keys())
            missing = required - py_fields - OPTIONAL_EXTRA_FIELDS
            if missing:
                mismatches.append(
                    f"{event_type}: Python never emits required fields {missing}"
                )
        assert not mismatches, "\n".join(mismatches)

    def test_py_no_unexpected_fields(self, py_emit_sites):
        """Python must not emit fields that are not in the canonical schema (besides 'id')."""
        unexpected = []
        for event_type, py_fields_dict in py_emit_sites.items():
            if event_type not in PROTOCOL_SCHEMA:
                continue
            canonical_fields = set(PROTOCOL_SCHEMA[event_type].keys())
            py_fields = set(py_fields_dict.keys())
            extra = py_fields - canonical_fields - OPTIONAL_EXTRA_FIELDS
            if extra:
                unexpected.append(f"{event_type}: Python emits unexpected fields {extra}")
        assert not unexpected, "\n".join(unexpected)


class TestKnownMismatches:
    """Document known protocol mismatches as xfail until fixed."""

    @pytest.mark.xfail(
        reason=(
            "server.py line 397 emits session_id: None (JSON null) on timeout, "
            "but DoneEvent.session_id is typed as string (not nullable) in types.ts. "
            "Fix: change DoneEvent.session_id to `string | null` in tui/src/types.ts."
        ),
        strict=False,
    )
    def test_done_session_id_nullable_mismatch(self, ts_interfaces):
        """DoneEvent.session_id should be nullable to match Python timeout behavior."""
        done_fields = ts_interfaces.get("done", {})
        session_id_type = done_fields.get("session_id", "")
        assert "null" in session_id_type, (
            f"DoneEvent.session_id is '{session_id_type}' but should allow null "
            "(server.py emits session_id: None on timeout). "
            "Fix: change to `session_id: string | null` in types.ts."
        )
