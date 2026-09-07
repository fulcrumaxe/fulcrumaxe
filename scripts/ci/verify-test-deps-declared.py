#!/usr/bin/env python3
"""CI guard: test dependencies must be declared, not ambient (D#1900 PR 1, D#2441).

Two checks, both about the same failure mode — a test dependency that works
on a developer host because something else dragged it in, and is declared
nowhere.

Check 1 — `pytest` itself is declared with a lower bound.
------------------------------------------------------------------
`pytest` used to arrive transitively via pytest-testmon, unpinned. That made
every number this project has ever cited about the Python suite
non-portable: CI's `pip install -r requirements.txt` can legally resolve a
different pytest version than a developer host did, which means different
collection, which means a different failing set.

A real requirement parser is used rather than a text match: `grep -q pytest`
is satisfied by `pytest-testmon`, `pytest-picked`, and `pytest-asyncio` alone
even when `pytest` itself is completely undeclared — see
tests/fixtures/requirements/no-pytest.txt, which is exactly that state.

Check 2 — every marker used in the tree resolves to something declared.
------------------------------------------------------------------
Check 1 has a subject set of exactly one package, and that is how D#2441
happened: `pytest-timeout` was undeclared, so eight `@pytest.mark.timeout`
call sites were inert. pytest accepts an unknown marker (`strict-markers` is
off) and does nothing with it, so a bound that is never enforced looks
exactly like one that is — until a test hangs.

So this check is a COMPARISON, not a list. The used-marker set is derived by
walking the AST of every `*.py` file in the tree; the declared set comes from
`pytest.ini` and `requirements.txt`. Nothing here asserts which markers exist
— a hardcoded `{"timeout", "integration", ...}` would pass forever after the
ninth marker landed, which is exactly how the eighth survived.

A marker resolves if any of:

  a. it is a pytest builtin (`parametrize`, `skipif`, ...). This set comes
     from pytest itself, not from this tree, which is why it can be literal.
  b. a package declared in requirements.txt provides it. Marker `m` is
     provided by the distribution `pytest-<m>` — the convention both live
     cases follow (`timeout` -> pytest-timeout, `asyncio` -> pytest-asyncio).
     Names are compared canonicalized and WHOLE: a declared
     `pytest-timeout-extras` does not resolve `timeout`. Substring matching
     is the precise bug check 1's docstring warns about, and re-introducing
     it here would rebuild it.
  c. it is registered in `pytest.ini`'s `markers` block AND it is never used
     with arguments anywhere in the tree.

The argument clause in (c) is the load-bearing part. Registering a marker in
`pytest.ini` silences pytest's unknown-marker warning but gives the marker no
behaviour. That is all a bare selection marker like `@pytest.mark.integration`
needs. A marker invoked with arguments — `@pytest.mark.timeout(10)` — is
asking some plugin to read those arguments and act on them, and registration
reads nothing. Without this clause, deleting `pytest-timeout` from
requirements.txt while leaving `timeout` in pytest.ini would go green with
all eight bounds inert again: the guard would document the hole it has.

Usage: verify-test-deps-declared.py [PATH]
  PATH defaults to requirements.txt. Exit 0 = both checks pass.
  Exit 1 = either fails.
"""

from __future__ import annotations

import ast
import configparser
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
PYTEST_INI = REPO_ROOT / "pytest.ini"

# pytest's own built-in marks. This is a property of pytest, not of this
# tree, which is why it is written out rather than derived: it changes when
# pytest changes, not when someone adds a test. Contrast the used-marker set
# below, which is always scanned.
PYTEST_BUILTIN_MARKERS = frozenset({
    "filterwarnings",
    "parametrize",
    "skip",
    "skipif",
    "usefixtures",
    "xfail",
})

# Directories that are not this project's source tree.
SKIP_DIR_NAMES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})


def _iter_requirement_lines(text: str):
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            yield line


def parse_requirements(text: str) -> dict[str, Requirement]:
    """Canonicalized distribution name -> parsed requirement."""
    found: dict[str, Requirement] = {}
    for line in _iter_requirement_lines(text):
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        found.setdefault(canonicalize_name(req.name), req)
    return found


def has_lower_bound(req: Requirement) -> bool:
    return any(spec.operator in (">=", ">", "==", "~=") for spec in req.specifier)


def providing_distribution(marker: str) -> str:
    """The distribution expected to provide `marker`.

    Consulted only AFTER the used-marker set has been derived from the tree;
    it is a name lookup, never a claim about which markers exist. Both live
    plugin markers follow the `pytest-<marker>` convention. If a plugin ever
    breaks it, add an override table here — do not relax the whole-name
    comparison in `resolve_marker`.
    """
    return f"pytest-{marker}"


def _marker_name(node: ast.AST) -> str | None:
    """Return `<name>` for an AST node spelling `pytest.mark.<name>`."""
    if not isinstance(node, ast.Attribute):
        return None
    parent = node.value
    if (
        isinstance(parent, ast.Attribute)
        and parent.attr == "mark"
        and isinstance(parent.value, ast.Name)
        and parent.value.id == "pytest"
    ):
        return node.attr
    return None


def scan_markers(root: Path) -> tuple[dict[str, set[str]], set[str], list[Path]]:
    """Walk the tree's ASTs for `pytest.mark.<name>` usage.

    Returns (marker -> set of relative paths using it, markers used with
    arguments, files that could not be parsed).

    The AST is walked rather than the text grepped so that a marker name
    appearing inside a string literal — a test fixture that builds a sample
    test file, say — is not mistaken for a marker this tree actually uses.
    Walking every Attribute node rather than only decorator lists picks up
    the module-level `pytestmark = pytest.mark.skipif(...)` form too.
    """
    used: dict[str, set[str]] = {}
    called: set[str] = set()
    unparseable: list[Path] = []

    for path in sorted(root.rglob("*.py")):
        if SKIP_DIR_NAMES.intersection(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            unparseable.append(path)
            continue

        rel = str(path.relative_to(root))
        for node in ast.walk(tree):
            name = _marker_name(node)
            if name is not None:
                used.setdefault(name, set()).add(rel)
            if isinstance(node, ast.Call):
                called_name = _marker_name(node.func)
                if called_name is not None and (node.args or node.keywords):
                    called.add(called_name)

    return used, called, unparseable


def registered_ini_markers(ini_path: Path) -> set[str]:
    """Marker names registered in pytest.ini's `markers =` block."""
    if not ini_path.is_file():
        return set()
    parser = configparser.ConfigParser()
    try:
        parser.read(ini_path, encoding="utf-8")
    except (OSError, UnicodeDecodeError, configparser.Error):
        return set()
    raw = parser.get("pytest", "markers", fallback="")
    names = set()
    for line in raw.splitlines():
        line = line.strip()
        if line:
            names.add(line.split(":", 1)[0].split("(", 1)[0].strip())
    return names


def resolve_marker(
    marker: str,
    declared: dict[str, Requirement],
    ini_markers: set[str],
    called_with_args: set[str],
) -> str | None:
    """Return a one-word reason the marker is satisfied, or None if it is not."""
    if marker in PYTEST_BUILTIN_MARKERS:
        return "pytest builtin"

    dist = providing_distribution(marker)
    if canonicalize_name(dist) in declared:
        return f"provided by declared {declared[canonicalize_name(dist)]}"

    if marker in ini_markers:
        if marker in called_with_args:
            # Registration silences the warning and does nothing else. See
            # the module docstring: this is the D#2441 hole.
            return None
        return "registered in pytest.ini"

    return None


def check_pytest_declared(target: Path, declared: dict[str, Requirement]) -> bool:
    req = declared.get("pytest")
    if req is None:
        print(f"FAIL: {target} does not declare 'pytest'. It arrives ambient/"
              "transitive today, which makes the failing set non-portable — "
              "declare it directly with a lower bound.", file=sys.stderr)
        return False

    if not has_lower_bound(req):
        print(f"FAIL: {target} declares 'pytest' ({req}) but with no lower "
              "bound — an unbounded declaration is exactly as unpinned as no "
              "declaration at all.", file=sys.stderr)
        return False

    print(f"ok: {target} declares 'pytest' with a lower bound ({req})")
    return True


def check_markers_declared(target: Path, declared: dict[str, Requirement]) -> bool:
    used, called_with_args, unparseable = scan_markers(REPO_ROOT)

    for path in unparseable:
        print(f"note: could not parse {path.relative_to(REPO_ROOT)}; it was "
              "not scanned for markers", file=sys.stderr)

    # A scan that finds nothing is a broken scan, not agreement. A guard
    # that goes green when its subject disappears gates nothing.
    if not used:
        print(f"FAIL: scanned {REPO_ROOT} and found zero `pytest.mark.*` uses. "
              "This tree has markers, so an empty scan means the scan broke — "
              "reporting agreement over zero comparisons would hide every "
              "undeclared marker at once.", file=sys.stderr)
        return False

    ini_markers = registered_ini_markers(PYTEST_INI)
    unresolved: list[tuple[str, set[str]]] = []
    for marker in sorted(used):
        if resolve_marker(marker, declared, ini_markers, called_with_args) is None:
            unresolved.append((marker, used[marker]))

    if unresolved:
        for marker, paths in unresolved:
            where = ", ".join(sorted(paths)[:4])
            if len(paths) > 4:
                where += f", +{len(paths) - 4} more"
            dist = providing_distribution(marker)
            if marker in ini_markers:
                why = (f"'{marker}' is registered in pytest.ini but is used with "
                       f"arguments, which registration cannot act on; declare "
                       f"{dist} in {target.name}")
            else:
                why = (f"'{marker}' is not a pytest builtin, is not registered in "
                       f"pytest.ini's markers block, and {dist} is not declared "
                       f"in {target.name}")
            print(f"FAIL: {why}. Used in: {where}", file=sys.stderr)
        print(f"FAIL: {len(unresolved)} marker(s) used in the tree resolve to "
              "nothing declared. An unresolved marker is accepted by pytest and "
              "silently does nothing (D#2441).", file=sys.stderr)
        return False

    print(f"ok: all {len(used)} marker(s) used in the tree resolve "
          f"({', '.join(sorted(used))})")
    return True


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    if not target.is_file():
        print(f"FAIL: {target} does not exist", file=sys.stderr)
        return 1

    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Unreadable input is a failure, never a pass over zero comparisons.
        print(f"FAIL: could not read {target}: {exc}", file=sys.stderr)
        return 1

    declared = parse_requirements(text)

    ok = check_pytest_declared(target, declared)
    ok = check_markers_declared(target, declared) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
