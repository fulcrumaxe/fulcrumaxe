"""Tests for the first-party classifier in backend/spec_external_docs.py.

Covers the fix for the external_docs gate false-positiving on first-party
modules that live in this repo but aren't hand-entered in the allowlist
(scripts/lib/*.py imported via sys.path.insert + a bare `import`, mainly).

Before this fix, `_extract_python_externals` knew exactly two things: the
stdlib and a manually maintained allowlist. Anything else — including a
module created by the very diff being checked — read as a third-party
dependency requiring an `external_docs:` URL. This file exercises the third
thing it now knows: whether a file for the module exists in the repo tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable regardless of how pytest is invoked,
# same pattern as backend/tests/test_spec_external_docs.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spec_external_docs import _extract_python_externals  # noqa: E402


# ---------------------------------------------------------------------------
# Known false positives (confirmed live on main before this fix)
# ---------------------------------------------------------------------------


class TestKnownFalsePositivesFixed:
    def test_route_discussion_no_longer_flagged(self):
        assert _extract_python_externals("import route_discussion") == []

    def test_transcript_event_id_no_longer_flagged(self):
        assert _extract_python_externals("import transcript_event_id") == []


# ---------------------------------------------------------------------------
# Control — a genuine external import must still be flagged. Run as two
# separate single-import inputs: `_extract_python_externals('import requests,
# numpy')` returns only `['requests']` today because the regex captures just
# the first name in a comma list. That's a pre-existing, separate limitation
# — explicitly out of scope here — and asserting the comma form would assert
# the wrong expected value.
# ---------------------------------------------------------------------------


class TestControlStillFlagsExternal:
    def test_requests_still_flagged(self):
        assert _extract_python_externals("import requests") == ["requests"]

    def test_numpy_still_flagged(self):
        assert _extract_python_externals("import numpy") == ["numpy"]


# ---------------------------------------------------------------------------
# Structural, not enumerated: every .py basename under the three per-feature
# directories the Module-per-Feature default fills up. Deliberately excludes
# scripts/hooks/post-agent.d/ — that directory holds 6 files and 0 are .py,
# so a criterion written over it would pass without checking anything.
# ---------------------------------------------------------------------------

_PER_FEATURE_DIRS = ("backend/rpc", "backend/stats", "scripts/lib")

# scripts/lib/cross-file-detector.py has a hyphen in its basename. A hyphen
# is not a valid character in a Python identifier, so `import
# cross-file-detector` is not — and never could be — a real import statement;
# nothing in a real diff can produce it. It's loaded elsewhere via
# importlib.util.spec_from_file_location, never a bare `import`. Testing it
# with `import <name>` the way every other file here is tested would assert
# against an input that can't occur, so it's named and excluded explicitly
# rather than silently dropped — if some other non-identifier basename shows
# up in these directories later, the test below fails loudly instead of
# quietly skipping it too.
_NON_IDENTIFIER_EXCEPTIONS = {"scripts/lib/cross-file-detector.py"}


def _per_feature_py_files() -> list[Path]:
    files: list[Path] = []
    for d in _PER_FEATURE_DIRS:
        files.extend(sorted((_REPO_ROOT / d).glob("*.py")))
    return files


class TestStructuralNotEnumerated:
    # Structural, not enumerated: assert the shape (each per-feature
    # directory actually contributes files, and the total clears a floor
    # low enough that this never blocks a PR that adds a module) rather than
    # a hardcoded count. A hardcoded count is exactly the decay pattern this
    # test's own name calls out — it goes red the moment anyone adds a file
    # under any Module-per-Feature directory, for no reason the test itself
    # cares about.
    _FLOOR = 40

    def test_each_per_feature_dir_contributes_files(self):
        for d in _PER_FEATURE_DIRS:
            count = len(sorted((_REPO_ROOT / d).glob("*.py")))
            assert count > 0, f"{d} contributed no .py files — is the glob or path stale?"

    def test_fixture_count_at_or_above_floor(self):
        # A floor, not a pin: the count is expected to grow as the
        # Module-per-Feature default adds files. It should never *shrink*
        # below a level that would make the coverage test below meaningless.
        assert len(_per_feature_py_files()) >= self._FLOOR

    def test_every_first_party_module_in_per_feature_dirs_resolves(self):
        files = _per_feature_py_files()
        checked = 0
        for path in files:
            rel = path.relative_to(_REPO_ROOT).as_posix()
            name = path.stem
            if not name.isidentifier():
                assert rel in _NON_IDENTIFIER_EXCEPTIONS, (
                    f"{rel} has a non-identifier basename and isn't in the "
                    "documented exception list — decide deliberately whether "
                    "to test it, don't let it fall through silently"
                )
                continue
            result = _extract_python_externals(f"import {name}")
            assert result == [], f"{rel} (import {name}) should resolve first-party, got {result}"
            checked += 1
        # Every file found minus the documented non-identifier exceptions —
        # derived from the live glob, not a count fixed at write time.
        assert checked == len(files) - len(_NON_IDENTIFIER_EXCEPTIONS)


# ---------------------------------------------------------------------------
# A brand-new file needs no allowlist entry. _first_party_names() is cached
# at module level and built at import time, so this needs a fresh
# interpreter to see a file created after the cache would already exist —
# a same-process test would just be re-reading a stale frozenset.
# ---------------------------------------------------------------------------


class TestNewFileNeedsNoAllowlistEntry:
    def test_freshly_created_module_resolves_without_allowlist_change(self):
        probe = _REPO_ROOT / "backend" / "rpc" / "_tmp_d1804_probe.py"
        assert not probe.exists(), "stale probe file left over from a previous run"
        probe.write_text("# throwaway probe for D#1804 structural first-party test\n")
        try:
            out = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from backend.spec_external_docs import _extract_python_externals as f; "
                    "print(f('import _tmp_d1804_probe'))",
                ],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == "[]"
        finally:
            probe.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Allowlist shrinks; the names it used to carry still resolve via the tree.
# ---------------------------------------------------------------------------


class TestAllowlistShrinkStillResolves:
    def test_removed_allowlist_names_still_resolve_via_tree(self):
        for name in ("backend", "scripts", "hooks", "testsupport"):
            assert _extract_python_externals(f"import {name}") == []

    def test_allowlist_no_longer_lists_first_party_package_names(self):
        allowlist_path = _REPO_ROOT / "backend" / "spec_external_docs_allowlist.txt"
        lines = {
            line.strip()
            for line in allowlist_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for name in ("backend", "scripts", "hooks", "testsupport"):
            assert name not in lines, f"{name} should have been removed from the allowlist"
