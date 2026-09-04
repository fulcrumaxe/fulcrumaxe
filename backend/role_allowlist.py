#!/usr/bin/env python3
"""backend/role_allowlist.py — role-roster trimming gate (D#1622 Batch C1).

Consulted by scripts/pre-spawn-check.sh before every spawn. Answers one
question: is `role` allowed to spawn for this project, per the project's
own config.json `active_roles` allowlist?

Backward compatible by design: a config.json with no `active_roles` key (or
an empty one) allows every role -- this is the case for this repo's own
`.autonomous-team/config.json` today, and for any project generated before
this feature existed. Only a coldstarted project whose generate.py run
populated a non-empty `active_roles` array actually gets trimmed.

This module does not delete or modify anything -- it is a pure read-only
check. `.claude/agents/*.md` files are never touched by any Slice C code.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Union


def is_role_active(role: str, config_path: Union[str, Path]) -> bool:
    """True when `role` may be spawned for the project at `config_path`.

    - Missing/unreadable/malformed config.json -> True (fail open; a broken
      or absent config must never brick spawning).
    - `active_roles` key absent or empty -> True (no trimming configured).
    - Otherwise -> True iff `role` is a member of `active_roles`.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return True

    if not isinstance(config, dict):
        return True

    active_roles = config.get("active_roles")
    if not active_roles:
        return True

    return role in active_roles


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="print 'true' or 'false' for role/config_path")
    check.add_argument("role")
    check.add_argument("config_path")

    args = parser.parse_args(argv)

    if args.command == "check":
        print("true" if is_role_active(args.role, args.config_path) else "false")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
