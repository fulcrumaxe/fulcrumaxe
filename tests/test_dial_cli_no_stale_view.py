"""tests/test_dial_cli_no_stale_view.py

Verify that the CLI never shows a stale view across set/revert cycles.

The test sets a directive, verifies the CLI shows the new level, reverts it,
verifies the CLI shows the default level again — repeated 3 times.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    """Patch dial_registry to use a temp state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    import backend.dial_registry as dr
    monkeypatch.setattr(dr, "_state_dir", lambda: state_dir)

    allowlist = state_dir / "dial-directive-allowlist.json"
    allowlist.write_text(
        json.dumps([{"kind": "github_user", "login": "ian"}], indent=2),
        encoding="utf-8",
    )
    return state_dir


@pytest.fixture()
def cp(tmp_path):
    from backend.control_plane import ControlPlane
    instance = ControlPlane(config_path=tmp_path / "config.json")
    instance.load()
    return instance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_cli_level_for_class(cp, capsys, class_name: str) -> tuple[int, int]:
    """Run cmd_dials and extract (level, directive_count) for class_name."""
    from backend.dial_cli import cmd_dials
    import re

    capsys.readouterr()  # clear buffer
    rc = cmd_dials(cp, None)
    assert rc == 0
    captured = capsys.readouterr().out

    line = next(
        (l for l in captured.splitlines() if class_name in l and "level=" in l),
        None,
    )
    assert line is not None, f"{class_name} not found in CLI output:\n{captured}"

    m_level = re.search(r"level=(\d+)", line)
    m_dirs = re.search(r"directives=(\d+)", line)
    assert m_level and m_dirs, f"Could not parse level/directives from: {line!r}"
    return int(m_level.group(1)), int(m_dirs.group(1))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dial_cli_no_stale_view(cp, isolated_registry, capsys):
    """
    Three set/revert cycles. Each time:
    - After set: CLI shows the elevated level and directives >= 1.
    - After revert: CLI shows the default level and directives == 0.
    """
    import backend.dial_registry as dr

    # Use cost.spend: default level 2, ceiling 5.
    class_name = "cost.spend"
    default_level = 2
    elevated_level = 4

    for cycle in range(3):
        # --- set ---
        dr.set_dial(
            class_name,
            elevated_level,
            source={"kind": "github_user", "login": "ian"},
        )
        lvl, ndirs = _get_cli_level_for_class(cp, capsys, class_name)
        assert lvl == elevated_level, (
            f"Cycle {cycle+1}: expected level={elevated_level} after set, got {lvl}"
        )
        assert ndirs >= 1, f"Cycle {cycle+1}: expected directives>=1 after set, got {ndirs}"

        # --- revert (simulate by setting back to default) ---
        # We replicate what revert_expired() does by setting the dial back
        # to its default level and clearing directives directly via _load/_save.
        registry = dr._load_registry()
        registry[class_name]["level"] = default_level
        registry[class_name]["directives"] = []
        dr._save_registry(registry)

        lvl, ndirs = _get_cli_level_for_class(cp, capsys, class_name)
        assert lvl == default_level, (
            f"Cycle {cycle+1}: expected level={default_level} after revert, got {lvl}"
        )
        assert ndirs == 0, f"Cycle {cycle+1}: expected directives=0 after revert, got {ndirs}"


def test_dial_cli_no_stale_view_after_revert_expired(cp, isolated_registry, capsys):
    """revert_expired() clears an expired directive and CLI reflects that."""
    import backend.dial_registry as dr
    from datetime import datetime, timedelta, timezone

    # Set a directive with a TTL 1 second in the past (already expired)
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")

    # Manually inject an expired directive
    dr._load_registry()  # ensure defaults are written
    registry = dr._load_registry()
    registry["memory.write"]["directives"] = [{
        "level": 5,
        "source": {"kind": "github_user", "login": "ian"},
        "set_at": past,
        "ttl_until": past,
    }]
    registry["memory.write"]["level"] = 5
    dr._save_registry(registry)

    # revert_expired should clear it and reset level to default (3)
    dr.revert_expired()

    lvl, ndirs = _get_cli_level_for_class(cp, capsys, "memory.write")
    assert lvl == 3, f"Expected default level=3 after expiry revert, got {lvl}"
    assert ndirs == 0, f"Expected directives=0 after expiry revert, got {ndirs}"
