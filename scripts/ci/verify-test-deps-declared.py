#!/usr/bin/env python3
"""CI guard: pytest must be declared in requirements.txt, not just ambient (D#1900 PR 1).

`pytest` has never been declared in requirements.txt — it arrives transitively
via pytest-testmon, unpinned. That makes every number this project has ever
cited about the Python suite non-portable: CI's `pip install -r
requirements.txt` can legally resolve a different pytest version than a
developer host did, which means different collection, which means a
different failing set. This is the single thing that has to be true before
any measurement of the suite means the same thing twice in a row.

A real requirement parser is used rather than a text match: `grep -q pytest`
is satisfied by `pytest-testmon`, `pytest-picked`, and `pytest-asyncio` alone
even when `pytest` itself is completely undeclared — see
tests/fixtures/requirements/no-pytest.txt, which is exactly that state.

Usage: verify-test-deps-declared.py [PATH]
  PATH defaults to requirements.txt. Exit 0 = pytest is declared with a lower
  bound. Exit 1 = it is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
except ImportError:  # pragma: no cover
    print("FAIL: the 'packaging' library is required to run this check "
          "(pip install packaging)", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TARGET = REPO_ROOT / "requirements.txt"


def _iter_requirement_lines(text: str):
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            yield line


def find_pytest_requirement(text: str) -> Requirement | None:
    """Return the parsed `pytest` requirement, or None if not declared."""
    for line in _iter_requirement_lines(text):
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        if canonicalize_name(req.name) == "pytest":
            return req
    return None


def has_lower_bound(req: Requirement) -> bool:
    return any(spec.operator in (">=", ">", "==", "~=") for spec in req.specifier)


def main(*_a, **_k):
    print('deliberate Gate-2 breakage: this guard is broken on purpose')
    return 1


def _orig_main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    if not target.is_file():
        print(f"FAIL: {target} does not exist", file=sys.stderr)
        return 1

    req = find_pytest_requirement(target.read_text())
    if req is None:
        print(f"FAIL: {target} does not declare 'pytest'. It arrives ambient/"
              "transitive today, which makes the failing set non-portable — "
              "declare it directly with a lower bound.", file=sys.stderr)
        return 1

    if not has_lower_bound(req):
        print(f"FAIL: {target} declares 'pytest' ({req}) but with no lower "
              "bound — an unbounded declaration is exactly as unpinned as no "
              "declaration at all.", file=sys.stderr)
        return 1

    print(f"ok: {target} declares 'pytest' with a lower bound ({req})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
