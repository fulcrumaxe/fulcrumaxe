#!/usr/bin/env python3
"""Size gate for the shared working-principles block (D#2253).

Measures the rendered `working_principles_block` from
scripts/lib/working-principles.sh — the shared block that ships on every
one of 24 role spawn prompts — and fails if it grows past MAX_CHARS.

This replaces the old D#1881 per-role whole-prompt growth bound
(formerly `test_rendered_prompt_growth_bounded` in
backend/tests/test_working_principles_background.py), which measured the
*entire rendered role prompt* against a frozen 2026-08 baseline and was
red on three of three roles. Most of that growth was one unrelated shared
fragment (backend/spawn_templates/fragments/hard-stop-no-claude.md, 1454
chars) charged in full to every role that includes it — not bloat in the
block this check actually guards. This check measures only the block its
name claims to guard, and nothing else.

MAX_CHARS = 6100, derived 2026-09-03 as:
    4765 (measured block, in characters, on this host)
  + 1329 (largest existing section in the block, §8)
  = 6094, rounded up to 6100 for headroom equal to one more section as
    large as the biggest one already there.

Counts CHARACTERS (Python `len()` on the decoded str), not bytes. `wc -c`
on the same block reads ~15 higher — the block contains multi-byte UTF-8
em dashes, so a byte count reads tighter than the actual character count
(D#2253 Finding 6). Keep using `len()` here; do not switch to `wc -c`.

Run from the repo root:

    python3 scripts/ci/working-principles-size.py
"""
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# See module docstring for the derivation. Defined once here — the test
# suite (backend/tests/test_working_principles_background.py) imports this
# constant rather than hardcoding its own copy, so the bound can't drift
# between the CI check and the test.
MAX_CHARS = 6100


def working_principles_block() -> str:
    """Shell out to the real helper — the same call pre-spawn-check.sh makes."""
    result = subprocess.run(
        ["bash", "scripts/lib/working-principles.sh", "working_principles_block"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    return result.stdout


def main() -> int:
    block = working_principles_block()
    size = len(block)
    print(f"working-principles block: {size} characters (limit {MAX_CHARS} characters)")
    if size > MAX_CHARS:
        print(f"FAIL: {size - MAX_CHARS} characters over the limit")
        return 1
    print(f"OK: {MAX_CHARS - size} characters of headroom")
    return 0


if __name__ == "__main__":
    sys.exit(main())
