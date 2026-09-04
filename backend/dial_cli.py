"""
dial_cli.py — CLI subcommand handler for the dials section of control_plane.

Kept separate from control_plane.py so that dial-specific display logic does
not inflate the control_plane module.  Imported and registered by control_plane.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cmd_dials(cp, _args) -> int:  # noqa: ARG001
    """List all autonomy dial classes with level/ceiling/directive count.

    Delegates to dial_registry.list_directives() so the output always
    reflects the live registry state (including active TTL directives)
    rather than the stale config.json snapshot.
    """
    from backend.dial_registry import list_directives  # noqa: PLC0415

    directives = list_directives()
    if not directives:
        print("(no dials configured)")
        return 0
    max_name = max((len(d["class"]) for d in directives), default=10)
    for d in directives:
        name = d["class"]
        lvl = d["level"]
        ceil = d["ceiling"]
        ndirs = len(d.get("directives", []))
        print(f"  {name:<{max_name}}  level={lvl}  ceiling={ceil}  directives={ndirs}")
    return 0
