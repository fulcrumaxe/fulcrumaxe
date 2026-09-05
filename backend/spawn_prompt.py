#!/usr/bin/env python3
"""
spawn_prompt.py — thin CLI wrapper around spawn_templates.render().

Usage:
    python3 backend/spawn_prompt.py <role> --discussion <N> [--pr <N>] [--var KEY=VALUE ...]

This is a convenience shim for acceptance testing and ad-hoc rendering.
The canonical library API is backend.spawn_templates.render().

Examples:
    python3 backend/spawn_prompt.py docs-writer --discussion 551 --pr 487 \
        --var pr_branch=disc-551-docs-writer \
        --var pr_url=https://github.com/autonomous-agent-7/autonomous-forever/pull/487

    python3 backend/spawn_prompt.py executor --discussion 42 \
        --var discussion_title="add URL detection" \
        --var discussion_url=https://github.com/.../discussions/42 \
        --var task_brief="implement URL detection"
"""

import argparse
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.spawn_templates import render, KNOWN_ROLES, REQUIRED_VARS


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Render a spawn prompt for an agent role.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "role",
        choices=sorted(KNOWN_ROLES),
        help="Agent role name.",
    )
    parser.add_argument(
        "--discussion",
        metavar="N",
        default="",
        help="Discussion number (used as discussion_number variable).",
    )
    parser.add_argument(
        "--pr",
        metavar="N",
        default="",
        help="PR number (used as pr_number variable).",
    )
    parser.add_argument(
        "--var",
        action="append",
        dest="vars",
        metavar="KEY=VALUE",
        default=[],
        help="Additional variable substitutions (repeatable).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    vars_dict: dict[str, str] = {}

    # Convenience flags → standard variable names
    if args.discussion:
        vars_dict["discussion_number"] = args.discussion
    if args.pr:
        vars_dict["pr_number"] = args.pr

    # Parse --var KEY=VALUE pairs
    for item in args.vars:
        if "=" not in item:
            print(f"ERROR: --var must be KEY=VALUE format, got: {item!r}", file=sys.stderr)
            return 2
        k, _, v = item.partition("=")
        vars_dict[k.strip()] = v

    # Supply empty defaults for required vars that are still missing,
    # so a quick smoke-test call (--discussion 1 --pr 1) produces output
    # rather than a hard error on missing vars.
    for req_var in REQUIRED_VARS.get(args.role, []):
        if req_var not in vars_dict:
            vars_dict[req_var] = f"<{req_var}>"

    try:
        result = render(args.role, vars_dict)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
