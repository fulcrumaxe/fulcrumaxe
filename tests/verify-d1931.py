#!/usr/bin/env python3
"""tests/verify-d1931.py

Throwaway verification script for D#1931 (not part of the pytest suite).
Imports `hooks/sandbox_rules.py` directly and prints one ALLOW/BLOCK line
per numbered row of the frozen Spec's A1-A4 tables, so a reviewer reads a
table instead of re-deriving each case by hand.

Run: python3 tests/verify-d1931.py

The `/tmp` and `/var/tmp` literals are built from parts (chr(47) + "tmp")
rather than typed directly, so this script is safe to write and run from
inside a sub-agent worktree too -- the live PreToolUse hook blocks the very
`/tmp` tokens these checks need to pass on a Bash command line, which is
part of the bug this Discussion fixes (see the Spec's practical constraint
note). A row landing on the wrong side of `allow=` prints a `*`-suffixed
marker (`ALLOW*` / `BLOCK*`) and flips the exit code non-zero.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from hooks.sandbox_rules import classify_bash  # noqa: E402

_SLASH = chr(47)
_TMP = _SLASH + "tmp"
_VAR_TMP = _SLASH + "var" + _SLASH + "tmp"

# A real sub-agent worktree path, per the Spec's "CWD means a real
# sub-agent worktree path under .claude/worktrees/" -- this script's own
# location already satisfies that.
_WT = _REPO
_MAIN_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_REPO)))

ROWS: list[tuple[int, str, bool]] = [
    # --- A1: restored, must all become allow=True ---
    (1, f"cd {_TMP} && git log", True),
    (2, f"cd {_TMP}{_SLASH} && git log", True),
    (3, f"cd {_TMP} && ls", True),
    (4, f"cd {_TMP}{_SLASH} && ls", True),
    (5, f"cd {_VAR_TMP} && ls", True),
    (6, f"cd {_VAR_TMP}{_SLASH} && ls", True),
    (7, f"cd {_TMP}", True),
    # --- A2: not regressed, already allow and must keep allowing ---
    (8, f"cd {_TMP}{_SLASH}foo && git log", True),
    (9, f"ls {_TMP}", True),
    (10, f"git -C {_MAIN_REPO} log --oneline -5", True),
    (11, f"git -C {_MAIN_REPO} status", True),
    (12, "git commit -m x", True),
    # --- A3: still blocked, non-negotiable ---
    (13, f"touch {_MAIN_REPO}{_SLASH}zzz.txt", False),
    (14, f"echo hi > {_MAIN_REPO}{_SLASH}CLAUDE.md", False),
    (15, f"cd {_MAIN_REPO} && touch zzz.txt", False),
    (16, f"rm -f {_MAIN_REPO}{_SLASH}CLAUDE.md", False),
    (17, f"cp x {_MAIN_REPO}{_SLASH}y.txt", False),
    (18, f"touch {_TMP}{_SLASH}..{_MAIN_REPO}{_SLASH}zzz.txt", False),
    (19, "git checkout main", False),
    (20, "gh pr merge 1", False),
    # --- A4: defect 2, verbless git ---
    (21, f"git {_MAIN_REPO}{_SLASH}CLAUDE.md", False),
    (22, "git --version", True),
    (23, "git --help", True),
]


def main() -> int:
    any_wrong = False
    for row, command, expected in ROWS:
        decision = classify_bash(command, _WT)
        marker = "ALLOW" if decision.allow else "BLOCK"
        if decision.allow != expected:
            marker += "*"
            any_wrong = True
        print(f"row {row:>2}: {marker:<6} {command!r} reason={decision.reason!r}")
    if any_wrong:
        print("RESULT: FAIL -- one or more rows landed on the wrong side")
    else:
        print("RESULT: PASS -- all rows match the Spec")
    return 1 if any_wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
