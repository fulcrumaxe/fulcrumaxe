"""tests/test_loop_snapshot_interpreter.py

Discriminating test for D#2087: scripts/loop-subsystem-snapshot.py must run
its subprocess targets under sys.executable, not a bare "python3" resolved
off PATH.

The test proves this by making PATH lie: it puts a fake `python3` shim first
on PATH that always fails, then calls the two functions that used to shell
out to bare "python3" (_workflows_available, _agent_cards) and asserts they
still succeed — because they now invoke sys.executable directly, which
bypasses the shim entirely.

`_run` (scripts/loop-subsystem-snapshot.py:39-52) passes no `env=`, so the
child inherits os.environ, including PATH — that's what makes the shim
reachable in the first place if the code ever regresses to a bare "python3".

Per the Spec, this must NOT stub or monkeypatch `_run` — that would test the
test, not the code path. It manipulates the actual PATH environment variable
and calls the real subprocess machinery.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "loop-subsystem-snapshot.py"

# The filename has hyphens, so it isn't a valid module identifier — load it
# by path, same pattern as tests/test_audit_dashboard_tiles.py.
_spec = importlib.util.spec_from_file_location("loop_subsystem_snapshot", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_mod.__name__ = "loop_subsystem_snapshot"
sys.modules["loop_subsystem_snapshot"] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


_SHIM = """#!/bin/sh
echo "shim: refusing to run, this is not a real interpreter" >&2
exit 17
"""


@pytest.fixture()
def broken_python3_first_on_path(tmp_path, monkeypatch):
    """Prepend a directory to PATH containing a `python3` that always fails.

    Any code that shells out to the literal string "python3" will hit this
    shim and fail. Code that invokes sys.executable (an absolute path) never
    consults PATH at all, so it is unaffected.
    """
    shim_dir = tmp_path / "fake-bin"
    shim_dir.mkdir()
    shim_path = shim_dir / "python3"
    shim_path.write_text(_SHIM)
    shim_path.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{old_path}")
    return shim_dir


def test_workflows_available_survives_broken_path_python3(broken_python3_first_on_path):
    """_workflows_available must return non-None even when "python3" on PATH is broken."""
    warnings: list[str] = []
    result = _mod._workflows_available(warnings)
    assert result is not None, (
        "workflows_available returned None with a broken python3 shim first on "
        f"PATH — the call site is still resolving the interpreter through PATH "
        f"instead of sys.executable. warnings={warnings!r}"
    )
    assert warnings == []


def test_agent_cards_survives_broken_path_python3(broken_python3_first_on_path):
    """_agent_cards must return non-None even when "python3" on PATH is broken."""
    warnings: list[str] = []
    result = _mod._agent_cards(warnings)
    assert result is not None, (
        "agent_cards returned None with a broken python3 shim first on PATH — "
        f"the call site is still resolving the interpreter through PATH instead "
        f"of sys.executable. warnings={warnings!r}"
    )
    assert warnings == []


def test_truncate_keep_tail_preserves_final_traceback_line():
    """A long traceback's last line (the actual exception) must survive truncation.

    Simulates the ModuleNotFoundError case: a >200-char blob whose only useful
    content is the last line. A bare out[:200] would drop it entirely.
    """
    filler = "\n".join(f"  File line {i}, in some_function" for i in range(40))
    out = filler + "\nModuleNotFoundError: No module named 'yaml'"
    assert len(out) > 200

    truncated = _mod._truncate_keep_tail(out)
    assert "ModuleNotFoundError: No module named 'yaml'" in truncated


def test_truncate_keep_tail_noop_under_budget():
    """Short output is returned unchanged."""
    out = "short warning"
    assert _mod._truncate_keep_tail(out) == out
