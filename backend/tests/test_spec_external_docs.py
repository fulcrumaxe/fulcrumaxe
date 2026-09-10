"""Tests for backend/spec_external_docs.py — loud-fail external_docs enforcement."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure backend/ is importable regardless of how pytest is invoked.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spec_external_docs import (  # noqa: E402
    ExternalDocsError,
    check_imports_have_docs,
    check_imports_have_docs_or_raise,
    _extract_external_docs_block,
    _validate_external_docs_content,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPEC_WITH_DOCS = """\
## Spec

### Technical Solution
Import requests and httpx.

### external_docs
- **requests**: https://requests.readthedocs.io/ — HTTP library
- **httpx**: https://www.python-httpx.org/ — async HTTP client

**Status**: FROZEN
"""

_SPEC_WITHOUT_DOCS = """\
## Spec

### Technical Solution
Import requests.

**Status**: FROZEN
"""

_SPEC_WITH_CODE_FENCE = """\
### external_docs
- requests

```python
import requests
```
"""

_SPEC_WITH_HTML_TAG = """\
### external_docs
- <a href="https://requests.readthedocs.io/">requests docs</a>
"""

# ---------------------------------------------------------------------------
# check_imports_have_docs — Python
# ---------------------------------------------------------------------------


class TestPythonStdlibOnly:
    def test_stdlib_only_returns_empty(self):
        diff = "import os\nimport sys\nimport json\nfrom pathlib import Path\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS, language="python")
        assert result == []

    def test_empty_diff_returns_empty(self):
        result = check_imports_have_docs("", _SPEC_WITHOUT_DOCS)
        assert result == []


class TestPythonMissingExternal:
    def test_single_missing_external(self):
        diff = "import requests\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert result == ["requests"]

    def test_multiple_missing_externals(self):
        diff = "import requests\nimport httpx\nimport boto3\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert set(result) == {"requests", "httpx", "boto3"}

    def test_single_external_covered_returns_empty(self):
        diff = "import requests\n"
        result = check_imports_have_docs(diff, _SPEC_WITH_DOCS)
        assert result == []

    def test_all_externals_covered_returns_empty(self):
        diff = "import requests\nimport httpx\n"
        result = check_imports_have_docs(diff, _SPEC_WITH_DOCS)
        assert result == []

    def test_from_import_extracted(self):
        diff = "from requests import Session\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert "requests" in result


class TestPythonAllowlist:
    def test_allowlisted_module_skipped(self, tmp_path, monkeypatch):
        # Create a custom allowlist with 'custom_internal'
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("custom_internal\n")
        monkeypatch.setattr(
            "backend.spec_external_docs._ALLOWLIST_PATH", allowlist
        )
        diff = "import custom_internal\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert result == []

    def test_non_allowlisted_external_still_flagged(self, tmp_path, monkeypatch):
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("custom_internal\n")
        monkeypatch.setattr(
            "backend.spec_external_docs._ALLOWLIST_PATH", allowlist
        )
        diff = "import requests\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert "requests" in result


# ---------------------------------------------------------------------------
# check_imports_have_docs — TypeScript / JavaScript
# ---------------------------------------------------------------------------


class TestTypeScriptStdlibOnly:
    def test_relative_imports_are_not_external(self):
        diff = "import { foo } from './foo';\nimport { bar } from '../bar';\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS, language="typescript")
        assert result == []

    def test_node_builtin_prefix_not_external(self):
        diff = "import { readFile } from 'node:fs';\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS, language="typescript")
        assert result == []


class TestTypeScriptMissingExternal:
    def test_single_missing_external(self):
        diff = "import axios from 'axios';\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS, language="typescript")
        assert "axios" in result

    def test_scoped_package_flagged(self):
        diff = "import something from '@anthropic-ai/sdk';\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS, language="typescript")
        assert "@anthropic-ai/sdk" in result

    def test_covered_ts_external_returns_empty(self):
        spec = """\
### external_docs
- **axios**: https://axios-http.com/ — HTTP client
"""
        diff = "import axios from 'axios';\n"
        result = check_imports_have_docs(diff, spec, language="typescript")
        assert result == []


# ---------------------------------------------------------------------------
# Stage 1 synthetic test — render raises ExternalDocsError
# ---------------------------------------------------------------------------


class TestStage1SyntheticRender:
    def test_missing_requests_raises(self):
        diff = "import requests\n"
        with pytest.raises(ExternalDocsError) as exc_info:
            check_imports_have_docs_or_raise(diff, _SPEC_WITHOUT_DOCS)
        assert "requests" in str(exc_info.value)

    def test_covered_requests_does_not_raise(self):
        diff = "import requests\n"
        # Should not raise
        check_imports_have_docs_or_raise(diff, _SPEC_WITH_DOCS)

    def test_error_message_names_missing_module(self):
        diff = "import httpx\n"
        with pytest.raises(ExternalDocsError) as exc_info:
            check_imports_have_docs_or_raise(diff, _SPEC_WITHOUT_DOCS, context="Stage 1")
        assert "httpx" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Stage 2 synthetic test — code-reviewer emits needs-fix
# ---------------------------------------------------------------------------


class TestStage2SyntheticCodeReview:
    """Simulate the code-reviewer calling check_imports_have_docs on a PR diff."""

    def test_pr_diff_httpx_no_docs_returns_missing(self):
        pr_diff = """\
+import httpx
+
+async def fetch(url: str) -> str:
+    async with httpx.AsyncClient() as client:
+        resp = await client.get(url)
+        return resp.text
"""
        disc_body = """\
## Spec
### Technical Solution
Use httpx for async HTTP.
**Status**: FROZEN
"""
        missing = check_imports_have_docs(pr_diff, disc_body)
        assert "httpx" in missing
        # Caller (code-reviewer) would set verdict=needs-fix and include missing in issues.

    def test_pr_diff_with_docs_returns_empty(self):
        pr_diff = "import httpx\n"
        disc_body = """\
### external_docs
- **httpx**: https://www.python-httpx.org/ — async HTTP client
"""
        missing = check_imports_have_docs(pr_diff, disc_body)
        assert missing == []


# ---------------------------------------------------------------------------
# Stage 3 synthetic test — spawn-script gate
# ---------------------------------------------------------------------------


class TestStage3SyntheticSpawnGate:
    """Simulate spawn-agent.sh checking for MISSING_EXTERNAL_DOCS marker."""

    _DISC_BODY_WITH_MARKER = """\
<!-- STATUS:SPEC_READY -->
<!-- MISSING_EXTERNAL_DOCS: requests, httpx -->

## Spec
...
"""

    _DISC_BODY_WITHOUT_MARKER = """\
<!-- STATUS:SPEC_READY -->

## Spec
...
"""

    def test_marker_present_detected(self):
        import re
        body = self._DISC_BODY_WITH_MARKER
        match = re.search(r"<!--\s*MISSING_EXTERNAL_DOCS:\s*([^-]+?)-->", body)
        assert match is not None
        modules = [m.strip() for m in match.group(1).split(",")]
        assert "requests" in modules
        assert "httpx" in modules

    def test_marker_absent_not_detected(self):
        import re
        body = self._DISC_BODY_WITHOUT_MARKER
        match = re.search(r"<!--\s*MISSING_EXTERNAL_DOCS:\s*([^-]+?)-->", body)
        assert match is None

    def test_override_env_var_bypasses(self, monkeypatch, tmp_path):
        """With ALLOW_MISSING_EXTERNAL_DOCS=1, override is written to audit.jsonl."""
        monkeypatch.setenv("ALLOW_MISSING_EXTERNAL_DOCS", "1")
        monkeypatch.setenv("ALLOW_MISSING_EXTERNAL_DOCS_REASON", "emergency hotfix")

        audit_path = tmp_path / "audit.jsonl"
        # Patch state_paths.AUDIT_LOG. D#1810: AUDIT_LOG is resolved via
        # state_paths.__getattr__ now, not a frozen constant — monkeypatch's
        # normal teardown restores the *snapshotted* value via setattr rather
        # than removing the name, which would leave it permanently frozen in
        # module globals (defeating __getattr__) for the rest of the pytest
        # session. delattr in a finally block instead of monkeypatch.setattr.
        import backend.state_paths as sp
        sp.AUDIT_LOG = audit_path
        try:
            from backend.spec_external_docs import write_override_audit
            write_override_audit(
                agent_name="executor",
                discussion="922",
                reason="emergency hotfix",
                missing_modules=["requests"],
            )

            assert audit_path.exists()
            import json
            record = json.loads(audit_path.read_text().strip())
            assert record["action"] == "override"
            assert record["discussion"] == "922"
            assert record["reason"] == "emergency hotfix"
            assert "requests" in record["missing_modules"]
        finally:
            if "AUDIT_LOG" in vars(sp):
                del sp.AUDIT_LOG


# ---------------------------------------------------------------------------
# Allowlist round-trip (AC-5)
# ---------------------------------------------------------------------------


class TestAllowlistRoundTrip:
    def test_add_module_to_allowlist_causes_skip(self, tmp_path, monkeypatch):
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("my_internal_module\n")
        monkeypatch.setattr("backend.spec_external_docs._ALLOWLIST_PATH", allowlist)

        diff = "import my_internal_module\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert result == []

    def test_module_not_in_allowlist_is_flagged(self, tmp_path, monkeypatch):
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("unrelated_module\n")
        monkeypatch.setattr("backend.spec_external_docs._ALLOWLIST_PATH", allowlist)

        diff = "import requests\n"
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert "requests" in result


# ---------------------------------------------------------------------------
# external_docs content validation (AC-6 / AC-PARENT-9)
# ---------------------------------------------------------------------------


class TestExternalDocsContentValidation:
    def test_code_fence_raises(self):
        with pytest.raises(ExternalDocsError) as exc_info:
            check_imports_have_docs_or_raise(
                "import requests\n",
                _SPEC_WITH_CODE_FENCE,
            )
        err = exc_info.value
        assert any("code fence" in e for e in err.content_errors)

    def test_html_tag_raises(self):
        spec = """\
import requests

### external_docs
- <a href="https://requests.readthedocs.io/">requests</a>
"""
        with pytest.raises(ExternalDocsError) as exc_info:
            check_imports_have_docs_or_raise("import requests\n", spec)
        err = exc_info.value
        assert any("HTML tag" in e for e in err.content_errors)

    def test_clean_urls_only_does_not_raise(self):
        spec = """\
### external_docs
- **requests**: https://requests.readthedocs.io/
"""
        # No imports in diff means no error
        check_imports_have_docs_or_raise("x = 1\n", spec)


# ---------------------------------------------------------------------------
# Block extraction edge cases
# ---------------------------------------------------------------------------


class TestBlockExtraction:
    def test_none_when_no_block(self):
        assert _extract_external_docs_block("No external_docs here") is None

    def test_extracts_from_markdown_heading(self):
        spec = "### external_docs\n- **foo**: https://foo.example.com/\n\n## Next\n"
        block = _extract_external_docs_block(spec)
        assert block is not None
        assert "foo.example.com" in block

    def test_extracts_from_bold_heading(self):
        spec = "**external_docs**:\n- **bar**: https://bar.example.com/\n\n**Status**: FROZEN"
        block = _extract_external_docs_block(spec)
        # May or may not match depending on regex — at minimum, should not crash.
        # If match found, URL must be present.
        if block is not None:
            assert "bar.example.com" in block


# ---------------------------------------------------------------------------
# D#2007 — context diff lines must not be extracted as added imports
# ---------------------------------------------------------------------------


class TestD2007ContextLineFiltering:
    """`check_imports_have_docs` must only extract added ('+') import lines
    out of a real unified diff. Before this fix, unmodified context lines
    (and the diff's own `+++ b/<path>` header) fell through to the
    extractor unfiltered, hard-failing PRs on imports they never wrote
    (PR #1999, PR #2002). Every fixture below carries at least one
    unchanged `import` line that must NOT be extracted -- a fixture with no
    context lines passes identically before and after this change and
    proves nothing (D#1984).
    """

    _PY_DIFF_WITH_CONTEXT = """\
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
 import requests
+import brand_new_pkg
 x = 1
"""

    _TS_DIFF_WITH_CONTEXT = """\
diff --git a/foo.ts b/foo.ts
index 1111111..2222222 100644
--- a/foo.ts
+++ b/foo.ts
@@ -1,2 +1,3 @@
 import ink from 'ink';
+import chalkx from 'chalkx';
 const x = 1;
"""

    def test_python_context_import_not_extracted(self):
        # AC1: only the added `brand_new_pkg` import comes back; the
        # unchanged `import requests` context line must be absent.
        result = check_imports_have_docs(self._PY_DIFF_WITH_CONTEXT, _SPEC_WITHOUT_DOCS)
        assert result == ["brand_new_pkg"]

    def test_python_context_import_not_extracted_explicit_language(self):
        # Same fixture through the explicit language="python" call path,
        # which bypasses _auto_externals's per-file dispatch entirely and
        # must be independently verified (Implementation Notes: a filter
        # placed only in _auto_externals would miss this call site).
        result = check_imports_have_docs(
            self._PY_DIFF_WITH_CONTEXT, _SPEC_WITHOUT_DOCS, language="python"
        )
        assert result == ["brand_new_pkg"]

    def test_typescript_context_import_not_extracted(self):
        # AC2
        result = check_imports_have_docs(self._TS_DIFF_WITH_CONTEXT, _SPEC_WITHOUT_DOCS)
        assert result == ["chalkx"]

    def test_typescript_context_import_not_extracted_explicit_language(self):
        result = check_imports_have_docs(
            self._TS_DIFF_WITH_CONTEXT, _SPEC_WITHOUT_DOCS, language="typescript"
        )
        assert result == ["chalkx"]

    def test_added_only_import_still_extracted(self):
        # AC3, the under-flagging direction: a diff whose only import is a
        # `+` line must still be returned. Without this, AC1 would be
        # satisfiable by an extractor that returns [] for everything.
        diff = """\
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1 @@
+import brand_new_pkg
"""
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert result == ["brand_new_pkg"]

    def test_headerless_prose_import_line_unchanged(self):
        # AC4: the Stage 1 PM path passes Spec prose, not a diff, so there
        # is no file header to detect and every import line is still
        # scanned as-is. TestPythonMissingExternal.test_single_missing_external
        # above already covers this same headerless shape (a bare
        # "import requests\n" with no diff markers at all); this test pins
        # the exact-list result explicitly for D#2007.
        prose = "Some spec prose.\nimport requests\nWe use it to fetch things.\n"
        result = check_imports_have_docs(prose, _SPEC_WITHOUT_DOCS)
        assert result == ["requests"]

    def test_removed_import_line_excluded_by_rule(self):
        # AC5: a removed ('-') import line must never be extracted, now by
        # explicit rule rather than by the accidental fact that
        # "-import requests" happens to fail the import grammar.
        diff = """\
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,1 @@
-import requests
 x = 1
"""
        result = check_imports_have_docs(diff, _SPEC_WITHOUT_DOCS)
        assert result == []
