"""Tests for scripts/lib/muse_subagent_payload.py.

Muse twin of the SubagentStop payload contract: resolve a wait-ready stop
into agent_run row fields. The join key is the spawn task name (the assembler's
--event-id, echoed back in the spawn-accept agent_path) — parent-side truth,
never child content. resolve() never raises: anything unrecognised degrades
to all-defaults, mirroring scripts/lib/subagent_payload.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PAYLOAD_PATH = _REPO_ROOT / "scripts" / "lib" / "muse_subagent_payload.py"


def _load():
    spec = importlib.util.spec_from_file_location("muse_subagent_payload_for_test", _PAYLOAD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["muse_subagent_payload_for_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def payload_mod():
    return _load()


@pytest.fixture()
def scratch_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))
    return state_dir


def _stop_input(*, event_name="executor-543-999", subagent_id="sub-1", err_kind=None):
    return {
        "spawn_accept": {
            "status": "accepted",
            "subagent_id": subagent_id,
            "agent_path": f"main/{event_name}/1",
            "task_ref": "task/abc#0",
        },
        "wait_result": {
            "status": "ready",
            "subagent_id": subagent_id,
            "task_ref": "task/abc#5",
            "summary": "PONG",
            "evidence_refs": [],
            **({"errorKind": err_kind} if err_kind else {}),
        },
        "wait_is_error": False,
    }


def test_happy_path_resolves_registry_identity(payload_mod, scratch_state):
    from backend.spawn_registry import record_spawn

    record_spawn("executor-543-999", "executor", 543, "2026-10-29T00:00:00Z")
    out = payload_mod.resolve(_stop_input())
    assert out["agent_id"] == "executor-543-999"
    assert out["subagent_id"] == "sub-1"
    assert out["role"] == "executor"
    assert out["discussion"] == 543
    assert out["verdict"] == "done"
    assert out["summary"] == "PONG"
    assert out["task_ref"] == "task/abc#5"
    assert out["evidence_refs"] == []
    assert out["input_tok"] == 0
    assert out["output_tok"] == 0


def test_subagent_id_mismatch_yields_unknown(payload_mod, scratch_state):
    from backend.spawn_registry import record_spawn

    record_spawn("executor-543-999", "executor", 543, "2026-10-29T00:00:00Z")
    blob = _stop_input()
    blob["wait_result"]["subagent_id"] = "sub-OTHER"
    out = payload_mod.resolve(blob)
    assert out["agent_id"] == ""
    assert out["verdict"] == "unknown"


def test_error_wait_yields_fail_verdict(payload_mod, scratch_state):
    from backend.spawn_registry import record_spawn

    record_spawn("executor-543-999", "executor", 543, "2026-10-29T00:00:00Z")
    blob = _stop_input(err_kind="timeout")
    blob["wait_is_error"] = True
    out = payload_mod.resolve(blob)
    assert out["agent_id"] == "executor-543-999"
    assert out["verdict"] == "fail"


def test_unregistered_event_id_yields_unknown(payload_mod, scratch_state):
    out = payload_mod.resolve(_stop_input(event_name="ghost-1-2"))
    assert out["agent_id"] == ""
    assert out["role"] is None
    assert out["verdict"] == "unknown"


def test_malformed_inputs_never_raise(payload_mod, scratch_state):
    for bad in (None, {}, {"spawn_accept": None}, {"wait_result": "x"}, {"spawn_accept": {}, "wait_result": {}}, []):
        out = payload_mod.resolve(bad)
        assert out["agent_id"] == ""
        assert out["verdict"] == "unknown"


def test_cli_reads_stdin_writes_flat_json(payload_mod, scratch_state):
    from backend.spawn_registry import record_spawn

    record_spawn("executor-543-999", "executor", 543, "2026-10-29T00:00:00Z")
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(_PAYLOAD_PATH)],
        input=json.dumps(_stop_input()),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["agent_id"] == "executor-543-999"
    assert out["verdict"] == "done"
