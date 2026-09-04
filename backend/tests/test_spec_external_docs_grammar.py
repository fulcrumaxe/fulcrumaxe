"""Tests for D#2257: the extractors read prose as imports, and the TS side had
no built-in filter at all.

Two separate bugs, same subsystem:

1. `_extract_python_externals`'s old regex matched any line whose first word
   was `import`/`from`, with no requirement that the rest of the line
   actually be an import statement. Docstring/comment-continuation prose
   ("from the `~` case above...", "import stays lazy...") matched and was
   reported as a missing external dependency. Fixed by requiring the line's
   code portion to fully resolve to real import grammar (see
   `_PY_IMPORT_STMT_RE` in spec_external_docs.py).

2. `_extract_ts_externals` filtered only `./`, `../`, and `node:` — a Node
   built-in imported without the `node:` prefix (`fs`, `path`, `url`, ...)
   was reported as an undocumented external dependency. Fixed by mirroring
   the Python side's `_STDLIB_NAMES` check with `_NODE_BUILTIN_MODULES`.

This module holds a frozen, private reproduction of the pre-fix Python regex
(`_OLD_PY_IMPORT_RE` / `_old_extract_python_externals`) solely to measure the
real base-vs-head differential in-process against the live repo tree, per
D#2149 — a dry run or a hand-picked fixture is not evidence about the real
scan path. The reproduction is not used by any production code path.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spec_external_docs import (  # noqa: E402
    _extract_python_externals,
    _extract_ts_externals,
    _strip_python_line_comment,
    _TS_IMPORT_FROM_RE,
    _STDLIB_NAMES,
    _FIRST_PARTY_NAMES,
    _NODE_BUILTIN_MODULES,
    _load_allowlist,
)

# ---------------------------------------------------------------------------
# Frozen reproduction of the pre-fix Python regex (base behavior), used only
# to compute the differential in TestWholeTreeDifferential below.
# ---------------------------------------------------------------------------

_OLD_PY_IMPORT_RE = re.compile(
    r"^[+]?\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _old_extract_python_externals(text: str) -> list[str]:
    """Reproduction of `_extract_python_externals` as it existed before
    D#2257's grammar fix — same filters (stdlib/allowlist/first-party) and
    the same TS-shaped-line guard, but matching any line whose first word is
    import/from rather than requiring real import grammar."""
    stdlib = _STDLIB_NAMES
    allowlist = _load_allowlist()
    first_party = _FIRST_PARTY_NAMES
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _OLD_PY_IMPORT_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        code = _strip_python_line_comment(line)
        if _TS_IMPORT_FROM_RE.search(code):
            continue
        name = m.group(1)
        if name in stdlib or name in allowlist or name in first_party:
            continue
        if name not in seen_set:
            seen.append(name)
            seen_set.add(name)
    return seen


_EXCLUDED_DIRS = frozenset({"archive", "node_modules", ".git", ".claude"})


def _live_py_files() -> list[Path]:
    files = []
    for p in _REPO_ROOT.rglob("*.py"):
        parts = p.relative_to(_REPO_ROOT).parts
        if any(part in _EXCLUDED_DIRS for part in parts):
            continue
        files.append(p)
    return files


# ---------------------------------------------------------------------------
# Criteria 1 & 2 — red-before, on real source, file:line anchors from the Spec.
# ---------------------------------------------------------------------------


class TestRedBeforeRealSourceThe:
    """hooks/sandbox_rules.py:246 — prose beginning `from the ...`."""

    def test_old_regex_reports_the_from_real_file(self):
        text = (_REPO_ROOT / "hooks" / "sandbox_rules.py").read_text(encoding="utf-8")
        assert "the" in _old_extract_python_externals(text)

    def test_new_extractor_does_not_report_the(self):
        text = (_REPO_ROOT / "hooks" / "sandbox_rules.py").read_text(encoding="utf-8")
        assert "the" not in _extract_python_externals(text)


class TestRedBeforeRealSourceStaysLazy:
    """backend/server.py:1431 — `import stays lazy and inside this try.`"""

    def test_old_regex_reports_stays_from_real_file(self):
        text = (_REPO_ROOT / "backend" / "server.py").read_text(encoding="utf-8")
        assert "stays" in _old_extract_python_externals(text)

    def test_new_extractor_does_not_report_stays(self):
        text = (_REPO_ROOT / "backend" / "server.py").read_text(encoding="utf-8")
        assert "stays" not in _extract_python_externals(text)


# ---------------------------------------------------------------------------
# Criterion 3 — whole-tree differential, Python. Real files, not a fixture.
# ---------------------------------------------------------------------------

_DROPPED_14 = frozenset({
    "Discussion", "GitHub", "_is_kernel_device", "_state_dir", "a", "agent",
    "an", "clean", "config", "multiple", "other", "stays", "the", "wherever",
})


class TestWholeTreeDifferential:
    def test_old_vs_new_over_live_tree(self):
        files = _live_py_files()
        # State scope and host explicitly (CLAUDE.md: never a bare count).
        print(f"scanned {len(files)} .py files under repo root (excl. "
              f"archive/ node_modules/ .git/ .claude/), host={sys.platform}")

        old_names: set[str] = set()
        new_names: set[str] = set()
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            old_names |= set(_old_extract_python_externals(text))
            new_names |= set(_extract_python_externals(text))

        assert len(old_names) == 40, sorted(old_names)
        assert len(new_names) == 26, sorted(new_names)
        assert old_names - new_names == _DROPPED_14
        assert new_names - old_names == set()


# ---------------------------------------------------------------------------
# Criterion 4 — no true positive lost, each from its real importing file.
# ---------------------------------------------------------------------------

_TRUE_POSITIVES = (
    "fastapi", "textual", "yaml", "httpx", "claude_agent_sdk", "anthropic",
    "pydantic", "starlette", "uvicorn", "dateutil", "jwt", "torch", "trl",
    "unsloth", "transformers", "datasets", "huggingface_hub", "keyring",
    "pyseccomp", "cryptography", "aiohttp", "rich", "anyio", "PIL",
)


class TestNoTruePositiveLost:
    def test_each_true_positive_still_reported_from_live_tree(self):
        files = _live_py_files()
        found: set[str] = set()
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            found |= set(_extract_python_externals(text))
        missing = [n for n in _TRUE_POSITIVES if n not in found]
        assert not missing, f"lost true positives: {missing}"


# ---------------------------------------------------------------------------
# Criterion 5 — grammar coverage over each named shape.
# ---------------------------------------------------------------------------


class TestGrammarCoverage:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("import httpx", "httpx"),
            ("from fastapi import Depends", "fastapi"),
            ("from claude_agent_sdk import (", "claude_agent_sdk"),
            ("from dateutil.parser import parse", "dateutil"),
            ("import pyseccomp as _p", "pyseccomp"),
            ("import torch, trl", "torch"),
            ("import anthropic  # type: ignore[import]", "anthropic"),
            ("+from starlette.middleware.base import X", "starlette"),
            ("    import keyring", "keyring"),
        ],
    )
    def test_shape_resolves_to_module(self, line, expected):
        assert _extract_python_externals(line) == [expected]


# ---------------------------------------------------------------------------
# Criterion 6 — no regression test asserting `type` is CURRENTLY broken.
# It is already fixed (D#2237's TS-shaped-line guard). This guard instead
# asserts it stays absent, and holds on both old and new behavior.
# ---------------------------------------------------------------------------


class TestTypeStaysAbsent:
    def test_type_absent_old_and_new(self):
        text = 'import type { Project } from "./types";\n'
        assert "type" not in _old_extract_python_externals(text)
        assert "type" not in _extract_python_externals(text)


# ---------------------------------------------------------------------------
# Criterion 7 — red-before, TS built-ins, on real source.
# ---------------------------------------------------------------------------


class TestRedBeforeRealSourceTsBuiltins:
    _FIXTURE = _REPO_ROOT / "dashboard" / "scenarios" / "__tests__" / "route-coverage.test.ts"

    def test_fs_path_url_reported_without_builtin_filter(self):
        text = self._FIXTURE.read_text(encoding="utf-8")
        # Old TS extractor had no builtin filter at all — every specifier
        # not starting with ./, ../, or node: was reported. Reproduce that
        # by checking the raw specifiers the shared regex captures.
        specifiers = {
            m.group(1).split("/")[0]
            for m in _TS_IMPORT_FROM_RE.finditer(text)
            if not m.group(1).startswith(("./", "../", "node:"))
        }
        assert {"fs", "path", "url"} <= specifiers

    def test_fs_path_url_not_reported_by_new_extractor(self):
        text = self._FIXTURE.read_text(encoding="utf-8")
        result = _extract_ts_externals(text)
        assert not ({"fs", "path", "url"} & set(result))


# ---------------------------------------------------------------------------
# Criterion 8 — the built-in set is the full, live list, not a hand sample.
# ---------------------------------------------------------------------------


class TestNodeBuiltinSetComplete:
    def test_matches_live_node_module_builtin_modules(self):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not available in this environment")
        result = subprocess.run(
            [node, "-e", "console.log(JSON.stringify(require('module').builtinModules))"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        live = set(json.loads(result.stdout))
        assert set(_NODE_BUILTIN_MODULES) == live
        assert len(_NODE_BUILTIN_MODULES) == 68


# ---------------------------------------------------------------------------
# Criterion 9 — node: prefix still skipped; a real, non-allowlisted package
# is still reported. Uses a scratch allowlist so this doesn't depend on
# whether react/vitest happen to be allowlisted in the live file.
# ---------------------------------------------------------------------------


class TestNodePrefixAndRealPackageStillHandled:
    def test_node_prefixed_specifier_still_skipped(self):
        text = "import { readFile } from 'node:fs';\n"
        assert _extract_ts_externals(text) == []

    def test_unallowlisted_real_package_still_reported(self, tmp_path, monkeypatch):
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("")
        monkeypatch.setattr("backend.spec_external_docs._ALLOWLIST_PATH", allowlist)
        text = "import { render } from 'react-not-a-real-package-xyz';\n"
        assert _extract_ts_externals(text) == ["react-not-a-real-package-xyz"]


# ---------------------------------------------------------------------------
# Criterion 12 — a genuinely new, unallowlisted, undeclared dependency is
# still gated. Uses a scratch allowlist so this is independent of the live
# allowlist's contents.
# ---------------------------------------------------------------------------


class TestStatedAcceptanceEndToEnd:
    """Criterion 11 — a real, no-new-dependency dashboard/src diff clears the
    gate with an empty external_docs block, against the live allowlist (not
    a scratch one). Built from `git diff` over a real merged commit range,
    per the Spec: "Build the diff from real tracked files ..., not a
    fixture."."""

    def test_real_frontend_diff_with_no_new_deps_passes(self):
        try:
            result = subprocess.run(
                ["git", "diff", "bbf1ca21~1", "bbf1ca21", "--", "dashboard/src"],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip(f"git unavailable for real-diff fixture: {exc}")
        if result.returncode != 0 or not result.stdout.strip():
            pytest.skip("reference commit range not available in this checkout")

        from backend.spec_external_docs import check_imports_have_docs

        missing = check_imports_have_docs(result.stdout, "### external_docs\n")
        assert missing == []


class TestNewDependencyStillGated:
    def test_new_import_absent_from_allowlist_and_package_json_is_flagged(
        self, tmp_path, monkeypatch
    ):
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("")
        monkeypatch.setattr("backend.spec_external_docs._ALLOWLIST_PATH", allowlist)
        diff = (
            "diff --git a/dashboard/src/pages/Widget.tsx b/dashboard/src/pages/Widget.tsx\n"
            "--- a/dashboard/src/pages/Widget.tsx\n"
            "+++ b/dashboard/src/pages/Widget.tsx\n"
            "@@ -1,0 +2 @@\n"
            "+import totallyNewLib from 'totally-new-lib';\n"
        )
        from backend.spec_external_docs import check_imports_have_docs

        missing = check_imports_have_docs(diff, "### external_docs\n")
        assert "totally-new-lib" in missing
