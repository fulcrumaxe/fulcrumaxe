"""Tests for the `language="auto"` per-file dispatch in backend/spec_external_docs.py.

Covers D#2237: `check_imports_have_docs()` defaulted to the Python extractor
unconditionally, so a TS/TSX-only diff returned a false positive (`type`) and
missed its real externals entirely — a passing gate that meant nothing on any
frontend PR. This module exercises AC-1 through AC-6 from the Spec: the auto
default now splits a diff on its file headers, dispatches per section by the
post-image path suffix, and unions results; headerless text runs both
extractors; explicit `language=` values are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spec_external_docs import (  # noqa: E402
    check_imports_have_docs,
    _extract_python_externals,
    _split_diff_by_path,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_FRONTEND_DIFF = (_FIXTURES / "ext_docs_frontend.diff").read_text(encoding="utf-8")
_MIXED_DIFF = (_FIXTURES / "ext_docs_mixed.diff").read_text(encoding="utf-8")

_SPEC_WITHOUT_DOCS = "Spec with no external_docs block"

_SPEC_WITH_ALL_THREE_DOCUMENTED = """\
### external_docs
- **axios**: https://axios-http.com/ — HTTP client
- **dayjs**: https://day.js.org/ — date library
- **requests**: https://requests.readthedocs.io/ — HTTP library
"""


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — false negative fixed, false positive gone
# ---------------------------------------------------------------------------


class TestAC1FalseNegativeFixed:
    def test_tsx_only_diff_returns_real_externals(self):
        missing = check_imports_have_docs(_FRONTEND_DIFF, _SPEC_WITHOUT_DOCS)
        assert set(missing) >= {"axios", "dayjs"}

    def test_tsx_only_diff_drops_type_false_positive(self):
        missing = check_imports_have_docs(_FRONTEND_DIFF, _SPEC_WITHOUT_DOCS)
        assert "type" not in missing


# ---------------------------------------------------------------------------
# AC-3 — mixed diff yields both languages from one call
# ---------------------------------------------------------------------------


class TestAC3MixedDiffUnion:
    def test_mixed_diff_returns_all_three_names(self):
        missing = check_imports_have_docs(_MIXED_DIFF, _SPEC_WITHOUT_DOCS)
        assert set(missing) == {"axios", "dayjs", "requests"}


# ---------------------------------------------------------------------------
# AC-4 — documented externals still clear the gate
# ---------------------------------------------------------------------------


class TestAC4DocumentedExternalsClearGate:
    def test_mixed_diff_all_documented_returns_empty(self):
        missing = check_imports_have_docs(_MIXED_DIFF, _SPEC_WITH_ALL_THREE_DOCUMENTED)
        assert missing == []


# ---------------------------------------------------------------------------
# AC-5 — explicit language= is unchanged (back-compat)
# ---------------------------------------------------------------------------


class TestAC5ExplicitLanguageUnchanged:
    def test_explicit_typescript_on_mixed_drops_python_names(self):
        missing = check_imports_have_docs(_MIXED_DIFF, _SPEC_WITHOUT_DOCS, language="typescript")
        assert missing == ["axios", "dayjs"]

    def test_explicit_python_on_mixed_drops_ts_names(self):
        missing = check_imports_have_docs(_MIXED_DIFF, _SPEC_WITHOUT_DOCS, language="python")
        assert missing == ["requests"]

    def test_unsupported_language_still_raises(self):
        with pytest.raises(ValueError):
            check_imports_have_docs(_MIXED_DIFF, _SPEC_WITHOUT_DOCS, language="rust")


# ---------------------------------------------------------------------------
# AC-6 — headerless source text runs both extractors
# ---------------------------------------------------------------------------


class TestAC6HeaderlessRunsBoth:
    def test_headerless_axios_import_returns_only_package_name(self):
        text = "import axios from \"axios\";\n"
        missing = check_imports_have_docs(text, _SPEC_WITHOUT_DOCS)
        assert missing == ["axios"]

    def test_headerless_axios_import_does_not_return_binding_or_type(self):
        text = "import axios from \"axios\";\nimport type { X } from \"./x\";\n"
        missing = check_imports_have_docs(text, _SPEC_WITHOUT_DOCS)
        assert "type" not in missing
        assert "axios" in missing


# ---------------------------------------------------------------------------
# TS-shaped-line guard must not eat a real Python import over a trailing
# comment. A real import line's *comment* can legally carry a quoted
# from/require-shaped fragment (e.g. "# ported from 'legacy.py'") — the guard
# must look only at the code portion of the line, or it silently drops a real
# dependency, which is the exact false-negative class D#2237 exists to fix.
# ---------------------------------------------------------------------------


class TestGuardIgnoresTrailingComments:
    def test_import_with_from_quoted_comment_is_kept(self):
        text = 'import requests  # ported from "legacy.py"\n'
        assert _extract_python_externals(text) == ["requests"]

    def test_import_with_require_quoted_comment_is_kept(self):
        text = 'import requests  # see require("legacy")\n'
        assert _extract_python_externals(text) == ["requests"]

    def test_from_import_with_from_quoted_comment_is_kept(self):
        text = 'from numpy import array  # see notes from "old.py"\n'
        assert _extract_python_externals(text) == ["numpy"]

    def test_from_import_with_require_quoted_comment_is_kept(self):
        text = 'from boto3 import client  # see require("legacy")\n'
        assert _extract_python_externals(text) == ["boto3"]

    def test_plain_trailing_comment_unaffected(self):
        # Control: a comment with no quoted from/require shape never
        # triggered the guard and must keep working the same way.
        text = "import simplejson  # normal comment\n"
        assert _extract_python_externals(text) == ["simplejson"]

    def test_real_ts_shaped_line_still_guarded(self):
        # Control: the guard's actual job — a genuine TS import line with no
        # comment involved — must still be caught.
        text = 'import type { Project } from "./types";\n'
        assert _extract_python_externals(text) == []


# ---------------------------------------------------------------------------
# _split_diff_by_path — structural coverage of the new helper
# ---------------------------------------------------------------------------


class TestSplitDiffByPath:
    def test_frontend_fixture_yields_one_section(self):
        sections = _split_diff_by_path(_FRONTEND_DIFF)
        assert len(sections) == 1
        path, _ = sections[0]
        assert path == "dashboard/src/pages/__tests__/Sample.test.tsx"

    def test_mixed_fixture_yields_two_sections_in_order(self):
        sections = _split_diff_by_path(_MIXED_DIFF)
        paths = [p for p, _ in sections]
        assert paths == [
            "dashboard/src/pages/__tests__/Sample.test.tsx",
            "backend/sample_mod.py",
        ]

    def test_headerless_text_yields_no_sections(self):
        assert _split_diff_by_path("import requests\n") == []

    def test_plus_header_fallback_without_diff_git_line(self):
        text = (
            "--- /dev/null\n"
            "+++ b/backend/sample_mod.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+import requests\n"
        )
        sections = _split_diff_by_path(text)
        assert len(sections) == 1
        assert sections[0][0] == "backend/sample_mod.py"
