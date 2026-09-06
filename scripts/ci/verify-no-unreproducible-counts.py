#!/usr/bin/env python3
"""CI guard: no unreproducible Python test-failure counts in ci.yml (D#1900 PR 1).

`.github/workflows/ci.yml` used to cite "~151 failures" as the reason no
pytest gate exists. That number was unreproducible on three counts: it
disagreed with this Discussion's own title (90) and its own measurement (93),
and the precondition it named (D#1477 restoring backend test health) had
already been resolved — D#1477 and D#1411 are both closed, so the comment
described a wait that had ended and nobody noticed, while remaining the
workflow's own stated justification for the gap.

Every number this project cites about the Python suite must be reproducible
from a committed artifact under tests/baselines/pytest/, or not cited at all.
This scans a workflow file for a figure asserting a count of failing Python
tests and refuses it outright — the reproducibility of a cited number is not
something this check can verify, so it does not try; it simply forbids citing
one in prose.

Usage: verify-no-unreproducible-counts.py [PATH]
  PATH defaults to .github/workflows/ci.yml. Exit 0 = clean. Exit 1 = a count
  was found; the offending line number(s) are printed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TARGET = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Two shapes seen in the wild: "~151 failures" / "151 failures", and
# "90 of ~9,494 tests" (this Discussion's own title).
COUNT_PATTERNS = [
    re.compile(r"~?\d[\d,]*\s+failures?\b", re.IGNORECASE),
    re.compile(r"~?\d[\d,]*\s+of\s+~?\d[\d,]*\s+tests?\b", re.IGNORECASE),
]


def find_violations(text: str) -> list[tuple[int, str]]:
    violations = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in COUNT_PATTERNS:
            if pattern.search(line):
                violations.append((lineno, line.strip()))
                break
    return violations


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    if not target.is_file():
        print(f"FAIL: {target} does not exist", file=sys.stderr)
        return 1

    violations = find_violations(target.read_text())
    if violations:
        print(f"FAIL: {target} cites an unreproducible Python test-failure "
              f"count:", file=sys.stderr)
        for lineno, line in violations:
            print(f"  line {lineno}: {line}", file=sys.stderr)
        print("Cite a committed artifact under tests/baselines/pytest/ "
              "instead, or state the fact without a number.", file=sys.stderr)
        return 1

    print(f"ok: {target} cites no unreproducible Python test-failure counts")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
