"""tests/test_payload_shape.py

Unit tests for hooks/payload_shape.py (D#2324).

The end-to-end behaviour — the real hook process, real stdin, the real files
it writes — is measured by tests/test_sandbox_payload_shape.sh, and that is
the one that proves the probe is wired into the branch it claims to be in.
This file exists because the bash suites under tests/ are not run by CI and
the pytest ones are, so the invariants that would be expensive to rediscover
by hand get a regression net here: dedup is by key set, values other than
session_id never reach a row, and nothing raises.

Every test that touches the writer points AUTONOMOUS_TEAM_STATE_DIR at a
tmp_path first. Left unset it resolves to ~/.autonomous-forever-state, whose
audit.jsonl is append-only with no cleanup path — two agents leaked rows into
it on the day this was written.

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" python3 -m pytest tests/test_payload_shape.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.payload_shape import (  # noqa: E402
    _MAX_KEYS,
    _MAX_KEY_CHARS,
    _MAX_SESSION_ID_CHARS,
    build_payload_shape_row,
    payload_key_names,
    payload_shape_signature,
    record_payload_shape,
)


@pytest.fixture()
def telemetry(tmp_path, monkeypatch):
    """A scratch telemetry dir plus a scratch state dir, both isolated."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state))
    events = tmp_path / "hook-events"
    events.mkdir()
    return events, state


def _rows(directory: Path) -> list[dict]:
    out = []
    for path in sorted(directory.glob("blocks-*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return [r for r in out if r.get("kind") == "payload_shape"]


# ---------------------------------------------------------------------------
# payload_key_names / payload_shape_signature — pure
# ---------------------------------------------------------------------------


def test_key_names_are_sorted():
    assert payload_key_names({"tool_name": 1, "cwd": 2, "a": 3}) == [
        "a",
        "cwd",
        "tool_name",
    ]


@pytest.mark.parametrize("payload", [[], "s", 3, None, 3.5, True])
def test_non_dict_payloads_have_no_shape(payload):
    assert payload_key_names(payload) == []


def test_key_names_are_capped_in_count_and_length():
    payload = {f"k{i:04d}": i for i in range(500)}
    payload["x" * 5000] = 1
    names = payload_key_names(payload)
    assert len(names) == _MAX_KEYS
    assert all(len(n) <= _MAX_KEY_CHARS for n in names)


def test_signature_depends_only_on_the_key_set():
    a = payload_shape_signature(payload_key_names({"a": 1, "b": 2}))
    b = payload_shape_signature(payload_key_names({"b": "different", "a": None}))
    assert a == b
    assert a != payload_shape_signature(payload_key_names({"a": 1, "b": 2, "c": 3}))


@pytest.mark.parametrize(
    "left,right",
    [
        (["a", "b"], ["a\x00b"]),
        (["a", "b"], ["a,b"]),
        (["a", "b"], ["a|b"]),
        (["ab"], ["a", "b"]),
    ],
)
def test_signature_does_not_collide_on_a_separator_inside_a_key_name(left, right):
    # Any single join separator is itself a legal character in a key name, and
    # a collision here means the second shape is silently never recorded.
    assert payload_shape_signature(left) != payload_shape_signature(right)


def test_signature_survives_a_lone_surrogate():
    # json.loads produces these from a "\ud800" escape; plain utf-8 refuses them.
    assert payload_shape_signature(["lone\ud800"])


# ---------------------------------------------------------------------------
# build_payload_shape_row — pure
# ---------------------------------------------------------------------------


def test_row_records_the_absence_of_session_id_rather_than_inferring_one():
    row = build_payload_shape_row({"tool_name": "Bash", "cwd": "/x"})
    assert row["session_id"] is None
    assert row["kind"] == "payload_shape"
    assert row["decision"] == "observe"


def test_row_records_session_id_when_present():
    row = build_payload_shape_row({"session_id": "abc-123", "cwd": "/x"})
    assert row["session_id"] == "abc-123"


def test_row_bounds_a_hostile_session_id():
    row = build_payload_shape_row({"session_id": {"blob": "x" * 100000}})
    assert len(row["session_id"]) <= _MAX_SESSION_ID_CHARS


def test_row_reports_the_true_key_count_even_when_the_list_is_capped():
    payload = {f"k{i:04d}": i for i in range(300)}
    row = build_payload_shape_row(payload)
    assert row["key_count"] == 300
    assert len(row["payload_keys"]) == _MAX_KEYS


def test_row_carries_no_value_but_session_id():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "curl https://user:hunter2@example.com"},
        "cwd": "/home/someone/secret-project",
        "session_id": "sess-1",
    }
    encoded = json.dumps(build_payload_shape_row(payload))
    assert "hunter2" not in encoded
    assert "secret-project" not in encoded
    assert "curl" not in encoded
    assert "sess-1" in encoded


def test_row_is_json_serialisable_for_every_awkward_key():
    payload = {"a\x00b": 1, "lone\ud800": 2, "tool_name": "Bash"}
    json.dumps(build_payload_shape_row(payload))


# ---------------------------------------------------------------------------
# record_payload_shape — the writer
# ---------------------------------------------------------------------------


def test_first_shape_writes_one_row_and_a_repeat_writes_none(telemetry):
    events, _ = telemetry
    assert record_payload_shape({"tool_name": "Bash", "cwd": "/a"}, events) is True
    # Same key set, entirely different values.
    assert record_payload_shape({"tool_name": "Write", "cwd": "/b"}, events) is False
    assert len(_rows(events)) == 1


def test_a_new_key_set_writes_a_second_row(telemetry):
    events, _ = telemetry
    record_payload_shape({"tool_name": "Bash", "cwd": "/a"}, events)
    assert (
        record_payload_shape({"tool_name": "Bash", "cwd": "/a", "session_id": "s"}, events)
        is True
    )
    assert len(_rows(events)) == 2


def test_the_audit_copy_is_written_when_the_state_dir_exists(telemetry):
    events, state = telemetry
    record_payload_shape({"tool_name": "Bash", "cwd": "/a"}, events)
    audit = [
        json.loads(line)
        for line in (state / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [r["kind"] for r in audit] == ["payload_shape"]


def test_an_absent_state_dir_still_writes_the_hook_events_row(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "nope"))
    events = tmp_path / "hook-events"
    events.mkdir()
    assert record_payload_shape({"tool_name": "Bash", "cwd": "/a"}, events) is True
    assert len(_rows(events)) == 1


def test_an_empty_state_dir_variable_writes_no_audit_copy(tmp_path, monkeypatch):
    # Path("") is the process cwd; writing an audit.jsonl there would drop a
    # runtime file into whatever checkout the caller happened to run from.
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", "")
    events = tmp_path / "hook-events"
    events.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert record_payload_shape({"tool_name": "Bash", "cwd": "/a"}, events) is True
    assert not (cwd / "audit.jsonl").exists()


def test_a_non_dict_payload_writes_nothing(telemetry):
    events, _ = telemetry
    for payload in ([], "s", 3, None, {}):
        assert record_payload_shape(payload, events) is False
    assert _rows(events) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_telemetry_dir_returns_false_and_does_not_raise(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    events = tmp_path / "hook-events"
    events.mkdir()
    events.chmod(0o500)
    try:
        assert record_payload_shape({"tool_name": "Bash", "cwd": "/a"}, events) is False
    finally:
        events.chmod(0o700)


def test_the_marker_dir_lives_beside_the_rows(telemetry):
    events, _ = telemetry
    record_payload_shape({"tool_name": "Bash", "cwd": "/a"}, events)
    markers = list((events / "payload-shapes").glob("*.seen"))
    assert len(markers) == 1
