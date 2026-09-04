"""scripts/tests/test_pre_spawn_card_check.py

D#2100: scripts/pre-spawn-check.sh's _load_context() swallowed both stderr
and exit status on four subprocess calls (context_manager.py prompt,
agent_memory.py query, control_plane.py show, agent_cards.py show), so it
could not tell "the command ran and returned nothing" from "the command
could not run at all" -- both collapsed to the same empty string, and the
warning asserted the first cause regardless. Confirmed reproduction: a
broken interpreter reaching ModuleNotFoundError on the agent_cards.py call
produced "no agent card for executor" for a role with a valid, 1859-byte
card.

These tests never rely on a real interpreter actually crashing -- that
depends on the host's installed interpreter set (a nix store python3
without PyYAML, see D#2087), which is exactly the kind of thing that rots a
test the moment the host changes. Instead they install a PATH-shim `python3`
stub ahead of the real interpreter. The stub only intercepts invocations of
one target backend script (matched by a path substring) and forces that one
call to either:
  - exit 0 and print nothing (the genuine "ran fine, empty result" case), or
  - exit non-zero with a fixed stderr message (the "could not run" case)
every other python3 invocation -- including the other three context-load
sites, and everything pre-spawn-check.sh runs before/after _load_context --
is passed through to the real interpreter running this test process, so the
rest of the script keeps working normally and only the one call under test
is disturbed.

Mutation proof (required by the Spec's "test bar" section): reverting the
fix at scripts/pre-spawn-check.sh back to the bare `2>/dev/null || echo ""`
form (no exit-status capture) makes
test_execution_failure_never_asserts_the_zero_exit_cause and
test_card_check_execution_failure_is_never_reported_as_missing_card fail,
because the reverted code asserts the zero-exit-cause warning even though
the command's exit status was non-zero. Confirmed by hand against a scratch
copy of the pre-fix script; see the PR body for the transcript.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_SPAWN_CHECK = REPO_ROOT / "scripts" / "pre-spawn-check.sh"

# A static stub -- all per-test behavior is read from environment variables
# at run time, so the file content never needs to change between tests.
_STUB_PYTHON3 = """#!/usr/bin/env bash
# Test-only python3 stub -- see scripts/tests/test_pre_spawn_card_check.py.
# Intercepts only the one backend script this test run targets; every other
# invocation (including the other three context-load call sites, and the
# calls this script makes before/after _load_context) passes through to the
# real interpreter, so the rest of pre-spawn-check.sh behaves normally.
if [[ -n "${PRE_SPAWN_TEST_TARGET:-}" ]]; then
  for arg in "$@"; do
    case "$arg" in
      *"$PRE_SPAWN_TEST_TARGET"*)
        if [[ "${PRE_SPAWN_TEST_MODE:-}" == "fail" ]]; then
          echo "Traceback (most recent call last):" >&2
          echo "  File \\"$arg\\", line 9, in <module>" >&2
          echo "    import yaml" >&2
          echo "${PRE_SPAWN_TEST_FAIL_MSG:-simulated execution failure}" >&2
          exit 1
        else
          # Zero-exit "genuine" case: print whatever that real backend
          # script actually prints on a genuinely-empty result (context_manager,
          # agent_memory and agent_cards print nothing; control_plane.py show
          # always prints valid JSON, "{}" for an empty config -- never zero
          # bytes), so this must match the exact condition _load_context checks.
          if [[ -n "${PRE_SPAWN_TEST_EMPTY_OUTPUT:-}" ]]; then
            printf '%s' "${PRE_SPAWN_TEST_EMPTY_OUTPUT}"
          fi
          exit 0
        fi
        ;;
    esac
  done
fi
exec "$PRE_SPAWN_TEST_REAL_PYTHON3" "$@"
"""

# The four call sites inside _load_context, keyed by the path substring that
# identifies which backend script a given python3 invocation targets, paired
# with: the exact warning text the ORIGINAL (unfixed) code asserts on ANY
# empty result -- including a crash -- which must never appear when the
# command failed to run rather than genuinely returning nothing; and the
# exact stdout a genuinely-empty result produces for that real backend
# script (verified by reading each script: context_manager.py, agent_memory.py
# and agent_cards.py print nothing; control_plane.py show always emits valid
# JSON, "{}" for an empty config, and never zero bytes).
SITES = [
    pytest.param(
        "backend/context_manager.py",
        "context_manager.py prompt returned empty",
        "",
        id="context_manager",
    ),
    pytest.param(
        "backend/agent_memory.py",
        "agent_memory.py returned no lessons for role",
        "",
        id="agent_memory",
    ),
    pytest.param(
        "backend/control_plane.py",
        "control_plane.py show returned empty config",
        "{}",
        id="control_plane",
    ),
    pytest.param(
        "backend/agent_cards.py",
        "no agent card for",
        "",
        id="agent_cards",
    ),
]

FAIL_MSG = "ModuleNotFoundError: No module named 'yaml'"


def _write_stub(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "binstub"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(_STUB_PYTHON3)
    os.chmod(stub, 0o755)
    return bin_dir


def _run(
    tmp_path: Path,
    role: str,
    *,
    target: str | None = None,
    mode: str = "fail",
    fail_msg: str = FAIL_MSG,
    empty_output: str = "",
    discussion: int = 2100,
):
    bin_dir = _write_stub(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)

    env = dict(os.environ)
    # Prepend the shim so bare `python3` calls in pre-spawn-check.sh hit our
    # stub first -- a PATH-shim, never a hardcoded nix store path (D#2087's
    # test-rot concern, called out explicitly in this Spec's "test bar").
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["PRE_SPAWN_TEST_TARGET"] = target or ""
    env["PRE_SPAWN_TEST_MODE"] = mode
    env["PRE_SPAWN_TEST_FAIL_MSG"] = fail_msg
    env["PRE_SPAWN_TEST_EMPTY_OUTPUT"] = empty_output
    env["PRE_SPAWN_TEST_REAL_PYTHON3"] = sys.executable
    # D#2100 standing rule: backend modules invoked under pytest raise
    # UnsandboxedStatePathError unless AUTONOMOUS_TEAM_STATE_DIR is set --
    # PYTEST_CURRENT_TEST is inherited into this subprocess's env, so the
    # three non-targeted context-load calls (which do real work through the
    # real interpreter) need a scratch dir to avoid touching prod state.
    env["AUTONOMOUS_TEAM_STATE_DIR"] = str(state_dir)

    proc = subprocess.run(
        ["bash", str(PRE_SPAWN_CHECK), "--role", role, "--discussion", str(discussion), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc, payload


# ── Criterion 3: the genuine, zero-exit-but-empty case is untouched ────────
# (a regression guard -- this branch already worked before D#2100's fix, and
# must keep working exactly as before)


@pytest.mark.parametrize("target,wrong_cause,empty_output", SITES)
def test_zero_exit_empty_output_keeps_original_warning(tmp_path, target, wrong_cause, empty_output):
    proc, payload = _run(tmp_path, "executor", target=target, mode="empty", empty_output=empty_output)
    assert proc.returncode == 0, proc.stderr
    warnings = payload.get("warnings", [])
    assert any(wrong_cause in w for w in warnings), warnings


# ── Criteria 1, 2 & 4: an execution failure is classified, not misattributed,
#    and carries the last line of stderr, across all four sites ───────────


@pytest.mark.parametrize("target,wrong_cause,_empty_output", SITES)
def test_execution_failure_never_asserts_the_zero_exit_cause(tmp_path, target, wrong_cause, _empty_output):
    proc, payload = _run(tmp_path, "executor", target=target, mode="fail", fail_msg=FAIL_MSG)
    assert proc.returncode == 0, proc.stderr
    warnings = payload.get("warnings", [])
    assert not any(wrong_cause in w for w in warnings), warnings
    assert any("could not run" in w for w in warnings), warnings
    assert any(FAIL_MSG in w for w in warnings), warnings


# ── The exact reproduction from the filing: executor's card check crashing
#    must never be reported as "no agent card for executor" ──────────────


def test_card_check_execution_failure_is_never_reported_as_missing_card(tmp_path):
    proc, payload = _run(tmp_path, "executor", target="backend/agent_cards.py", mode="fail", fail_msg=FAIL_MSG)
    assert proc.returncode == 0, proc.stderr
    assert "no agent card for executor" not in proc.stdout
    warnings = payload.get("warnings", [])
    matches = [w for w in warnings if "executor" in w and "could not run" in w]
    assert matches, warnings
    assert FAIL_MSG in matches[0]


# ── Criterion 5: classification only -- the script's own stderr is not
#    turned into an unconditional passthrough of the subprocess's traceback ──


def test_execution_failure_does_not_amplify_stderr(tmp_path):
    proc, _ = _run(tmp_path, "executor", target="backend/agent_cards.py", mode="fail", fail_msg=FAIL_MSG)
    assert proc.returncode == 0
    assert "Traceback" not in proc.stderr
    assert FAIL_MSG not in proc.stderr


def test_successful_run_emits_no_stderr(tmp_path):
    proc, _ = _run(tmp_path, "executor", target=None, mode="empty")
    assert proc.returncode == 0
    assert proc.stderr == ""


# ── Criterion 6: an execution failure never turns into a hard spawn block ──


@pytest.mark.parametrize("target,_wrong_cause,_empty_output", SITES)
def test_execution_failure_does_not_change_exit_status(tmp_path, target, _wrong_cause, _empty_output):
    proc, _ = _run(tmp_path, "executor", target=target, mode="fail", fail_msg=FAIL_MSG)
    assert proc.returncode == 0, proc.stderr
