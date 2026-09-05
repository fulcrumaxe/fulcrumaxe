"""tests/test_dial_cli_reflects_registry.py

Verify that `control_plane.py dials` delegates to the live dial_registry
rather than the stale config.json snapshot.

After set_dial() updates the registry, the CLI output must reflect the
new level and non-zero directive count without a config reload.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    """
    Patch dial_registry to use a temp state dir so we don't touch
    $AUTONOMOUS_TEAM_STATE_DIR during tests.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    import backend.dial_registry as dr
    monkeypatch.setattr(dr, "_state_dir", lambda: state_dir)

    # Write an allowlist that permits the test source
    allowlist = state_dir / "dial-directive-allowlist.json"
    allowlist.write_text(
        json.dumps([{"kind": "github_user", "login": "ian"}], indent=2),
        encoding="utf-8",
    )
    return state_dir


@pytest.fixture()
def cp(tmp_path):
    """Fresh ControlPlane backed by a temp config file."""
    from backend.control_plane import ControlPlane
    instance = ControlPlane(config_path=tmp_path / "config.json")
    instance.load()
    return instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dial_cli_reflects_registry(cp, isolated_registry, capsys):
    """
    CLI output must show the live registry level, not the config.json snapshot.

    After set_dial('methodology.change', 2, ...), the CLI must print level=2
    and directives>=1 for methodology.change.
    """
    import backend.dial_registry as dr
    dr.set_dial(
        "methodology.change",
        2,
        ttl="for-today",
        source={"kind": "github_user", "login": "ian"},
    )

    # Run the dials subcommand
    from backend.dial_cli import cmd_dials
    rc = cmd_dials(cp, None)
    assert rc == 0

    captured = capsys.readouterr().out
    # Find the methodology.change line
    mc_line = next(
        (line for line in captured.splitlines() if "methodology.change" in line),
        None,
    )
    assert mc_line is not None, f"methodology.change not found in output:\n{captured}"
    assert "level=2" in mc_line, f"Expected level=2 in: {mc_line!r}"
    # directives count >= 1
    import re
    m = re.search(r"directives=(\d+)", mc_line)
    assert m is not None, f"No directives count in: {mc_line!r}"
    assert int(m.group(1)) >= 1, f"Expected >=1 directive in: {mc_line!r}"


def test_dial_cli_agent_spawn_default(cp, isolated_registry, capsys):
    """agent.spawn should show at default level=4 with directives=0 initially."""
    from backend.dial_cli import cmd_dials
    rc = cmd_dials(cp, None)
    assert rc == 0

    captured = capsys.readouterr().out
    spawn_line = next(
        (line for line in captured.splitlines() if "agent.spawn" in line),
        None,
    )
    assert spawn_line is not None, f"agent.spawn not found in output:\n{captured}"
    assert "level=4" in spawn_line, f"Expected default level=4 in: {spawn_line!r}"


def test_dial_cli_shows_all_13_classes(cp, isolated_registry, capsys):
    """The CLI must list exactly 13 dial classes."""
    from backend.dial_cli import cmd_dials
    rc = cmd_dials(cp, None)
    assert rc == 0

    captured = capsys.readouterr().out
    data_lines = [line for line in captured.splitlines() if line.strip() and "level=" in line]
    assert len(data_lines) == 13, (
        f"Expected 13 dial classes, got {len(data_lines)}:\n{captured}"
    )
