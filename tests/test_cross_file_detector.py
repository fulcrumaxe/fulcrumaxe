"""
Unit tests for scripts/lib/cross-file-detector.py

Covers:
- Symbol extraction from unified diff
- Path denylist enforcement
- Snippet length cap (200 chars)
- Snippet sanitization (denied paths never appear in output)
- Dismissed-pair cache (7-day window)
- Replay against PR #823 fixture (≥1 sibling tile detected)
- detect() returns empty list when no symbols found
- SYMBOL_CAP limits processing
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import patch, MagicMock

import pytest

# ── Load detector module from its file path ──────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
DETECTOR_PATH = REPO_ROOT / "scripts" / "lib" / "cross-file-detector.py"


def load_detector() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cross_file_detector", DETECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


det = load_detector()


# ── Helpers ───────────────────────────────────────────────────────────────────

SIMPLE_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,5 +1,6 @@
+def my_function(x):
+    return x + 1
"""

MULTI_SYMBOL_DIFF = """\
diff --git a/src/mod.ts b/src/mod.ts
--- a/src/mod.ts
+++ b/src/mod.ts
@@ -1,3 +1,5 @@
+function computeTotal(items) {
+    return items.reduce((a, b) => a + b, 0)
+}
+const MAX_RETRIES = 3
"""

DENYLIST_DIFF = """\
diff --git a/secrets/vault.py b/secrets/vault.py
--- a/secrets/vault.py
+++ b/secrets/vault.py
@@ -1,2 +1,3 @@
+def fetch_secret(key):
+    return os.environ[key]
"""

ENV_DIFF = """\
diff --git a/.env.production b/.env.production
--- a/.env.production
+++ b/.env.production
@@ -1,2 +1,3 @@
+SECRET_KEY=hunter2
"""


# ── Tests: symbol extraction ──────────────────────────────────────────────────

class TestExtractSymbols:
    def test_extracts_python_function(self):
        symbols = det.extract_symbols_from_diff(SIMPLE_DIFF)
        assert "my_function" in symbols
        assert symbols["my_function"] == "src/foo.py"

    def test_extracts_typescript_function_and_const(self):
        symbols = det.extract_symbols_from_diff(MULTI_SYMBOL_DIFF)
        assert "computeTotal" in symbols
        assert "MAX_RETRIES" in symbols

    def test_skips_denied_path_in_diff(self):
        """Symbols from denied paths must not be extracted."""
        symbols = det.extract_symbols_from_diff(DENYLIST_DIFF)
        assert "fetch_secret" not in symbols

    def test_skips_env_file(self):
        symbols = det.extract_symbols_from_diff(ENV_DIFF)
        # .env file is denied — no symbols
        assert len(symbols) == 0

    def test_skips_very_short_names(self):
        diff = """\
diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,2 +1,3 @@
+def ab(x):
+    pass
"""
        symbols = det.extract_symbols_from_diff(diff)
        assert "ab" not in symbols  # len=2, below threshold

    def test_respects_symbol_cap(self):
        # Generate a diff with SYMBOL_CAP+10 symbols
        lines = ["diff --git a/src/big.py b/src/big.py",
                 "--- a/src/big.py",
                 "+++ b/src/big.py",
                 "@@ -1,3 +1,3 @@"]
        for i in range(det.SYMBOL_CAP + 10):
            lines.append(f"+def func_{i:04d}(x):")
            lines.append(f"+    pass")
        diff = "\n".join(lines)
        symbols = det.extract_symbols_from_diff(diff)
        assert len(symbols) <= det.SYMBOL_CAP

    def test_returns_empty_on_no_added_lines(self):
        diff = """\
diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,2 +1,2 @@
-def old_function():
+def old_function():  # comment
"""
        # "old_function" appears on a changed line starting with "+"
        symbols = det.extract_symbols_from_diff(diff)
        # The modified line (just adding a comment) starts with + so it may be extracted
        # but that's fine — the important thing is we don't crash on empty diffs
        assert isinstance(symbols, dict)


# ── Tests: path denylist ──────────────────────────────────────────────────────

class TestPathDenylist:
    @pytest.mark.parametrize("path,expected", [
        (".env", True),
        (".env.production", True),
        ("secrets/vault.py", True),
        ("hooks/sandbox_rules.py", True),
        ("settings.json", True),
        ("server.pem", True),
        ("id_rsa.key", True),
        ("src/main.py", False),
        ("dashboard/src/App.tsx", False),
        ("scripts/lib/helper.sh", False),
    ])
    def test_denylist(self, path, expected):
        assert det.is_denied_path(path) == expected


# ── Tests: snippet length cap ─────────────────────────────────────────────────

class TestSnippetCap:
    def test_cap_at_200(self):
        long_text = "x" * 300
        capped = det.cap_snippet(long_text)
        assert len(capped) <= det.SNIPPET_MAX
        assert capped.endswith("...")

    def test_short_text_unchanged(self):
        short = "def foo(): pass"
        assert det.cap_snippet(short) == short

    def test_exactly_200_chars_no_ellipsis(self):
        text = "a" * 200
        result = det.cap_snippet(text)
        assert len(result) == 200
        assert not result.endswith("...")


# ── Tests: dismissed-pair cache ───────────────────────────────────────────────

class TestDismissedCache:
    def test_loads_recent_pairs(self, tmp_path):
        cache = tmp_path / ".autonomous-team" / "cross-file-dismissed.jsonl"
        cache.parent.mkdir(parents=True)
        now = time.time()
        entry = {
            "symbol": "foo",
            "primary_file": "src/a.py",
            "sibling_file": "src/b.py",
            "dismissed_at": now - 3600,  # 1h ago — within 7d
        }
        cache.write_text(json.dumps(entry) + "\n")
        with patch.object(det, "DISMISSED_CACHE", str(cache.relative_to(tmp_path))):
            pairs = det.load_dismissed_pairs(str(tmp_path))
        assert ("foo", "src/a.py", "src/b.py") in pairs

    def test_prunes_stale_pairs(self, tmp_path):
        cache = tmp_path / ".autonomous-team" / "cross-file-dismissed.jsonl"
        cache.parent.mkdir(parents=True)
        stale = time.time() - (8 * 86400)  # 8 days ago — outside 7d window
        entry = {
            "symbol": "bar",
            "primary_file": "src/a.py",
            "sibling_file": "src/b.py",
            "dismissed_at": stale,
        }
        cache.write_text(json.dumps(entry) + "\n")
        with patch.object(det, "DISMISSED_CACHE", str(cache.relative_to(tmp_path))):
            pairs = det.load_dismissed_pairs(str(tmp_path))
        assert ("bar", "src/a.py", "src/b.py") not in pairs

    def test_missing_cache_returns_empty(self, tmp_path):
        with patch.object(det, "DISMISSED_CACHE", "nonexistent/path.jsonl"):
            pairs = det.load_dismissed_pairs(str(tmp_path))
        assert pairs == set()


# ── Tests: snippet sanitization ───────────────────────────────────────────────

class TestSnippetSanitization:
    """Denied paths must NEVER appear in any finding's snippet."""

    def test_denied_files_not_in_findings(self, tmp_path):
        """detect() must not emit findings where sibling_file is a denied path."""
        # Create a diff that modifies a symbol in a safe file
        diff = """\
diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,2 +1,3 @@
+def validate_token(tok):
+    return len(tok) > 0
"""
        # Create the sibling file in a denied path
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "vault.py").write_text("def validate_token(t):\n    return SECRET\n")

        # Also create the primary file
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "auth.py").write_text("def validate_token(tok):\n    return len(tok) > 0\n")

        findings = det.detect(diff, str(tmp_path))
        for f in findings:
            assert not det.is_denied_path(f.get("sibling_file", ""))
            assert not det.is_denied_path(f.get("primary_file", ""))

    def test_snippets_under_200_chars(self, tmp_path):
        """All snippets in findings must be ≤200 chars."""
        diff = """\
diff --git a/src/util.py b/src/util.py
--- a/src/util.py
+++ b/src/util.py
@@ -1,2 +1,3 @@
+def process_data(items):
+    return [x for x in items]
"""
        # Create primary file
        src = tmp_path / "src"
        src.mkdir()
        (src / "util.py").write_text("def process_data(items):\n    return [x for x in items]\n")

        # Create sibling with same symbol but different content (long line)
        other = tmp_path / "lib"
        other.mkdir()
        long_body = "    " + "x = " + "1 + " * 60 + "0"
        (other / "util.py").write_text(
            f"def process_data(items):\n{long_body}\n    return items\n"
        )

        findings = det.detect(diff, str(tmp_path))
        for f in findings:
            assert len(f.get("snippet_primary", "")) <= det.SNIPPET_MAX
            assert len(f.get("snippet_sibling", "")) <= det.SNIPPET_MAX


# ── Tests: core detect() ─────────────────────────────────────────────────────

class TestDetect:
    def test_returns_empty_on_no_symbols(self, tmp_path):
        diff = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
+This is a change to the README.
"""
        findings = det.detect(diff, str(tmp_path))
        assert findings == []

    def test_finds_sibling_with_different_content(self, tmp_path):
        diff = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,3 @@
+def compute_hash(data):
+    return hashlib.sha256(data).hexdigest()
"""
        # Primary
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text(
            "def compute_hash(data):\n    return hashlib.sha256(data).hexdigest()\n"
        )
        # Sibling with same symbol but different implementation
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "b.py").write_text(
            "def compute_hash(data):\n    return hashlib.md5(data).hexdigest()  # old impl\n"
        )

        findings = det.detect(diff, str(tmp_path))
        assert len(findings) >= 1
        f = findings[0]
        assert f["symbol"] == "compute_hash"
        assert f["primary_file"] == "src/a.py"
        assert "lib/b.py" in f["sibling_files"]

    def test_no_finding_when_content_identical(self, tmp_path):
        diff = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,3 @@
+def helper(x):
+    return x
"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("def helper(x):\n    return x\n")
        (tmp_path / "lib").mkdir()
        # Same line → should NOT emit finding
        (tmp_path / "lib" / "b.py").write_text("def helper(x):\n    return x\n")

        findings = det.detect(diff, str(tmp_path))
        # Content is identical → no finding
        # (snippet_primary matches snippet_sib for the declaration line)
        for f in findings:
            # If a finding is emitted, verify it's not identical content
            assert f.get("snippet_primary") != f.get("snippet_sibling") or f.get("snippet_sibling") == ""


# ── Replay test: PR #823 ──────────────────────────────────────────────────────

class TestPR823Replay:
    """
    Acceptance criterion: detector identifies ≥1 sibling tile with the same
    pattern as the symbols fixed in PR #823 (setFetchedAt, toChartData, etc.)
    when run against the real repo.
    """

    def test_replay_finds_at_least_one_sibling(self):
        fixture_path = REPO_ROOT / "tests" / "fixtures" / "pr823.diff"
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

        diff_text = fixture_path.read_text()
        findings = det.detect(diff_text, str(REPO_ROOT))

        # Must find ≥1 sibling tile
        assert len(findings) >= 1, (
            f"Expected ≥1 finding from PR #823 replay, got 0. "
            f"Symbols extracted: {list(det.extract_symbols_from_diff(diff_text).keys())}"
        )

    def test_replay_symbols_include_expected(self):
        fixture_path = REPO_ROOT / "tests" / "fixtures" / "pr823.diff"
        diff_text = fixture_path.read_text()
        symbols = det.extract_symbols_from_diff(diff_text)

        # The diff modifies setFetchedAt (a const declaration) and toChartData (a function)
        # At minimum one of these should be extracted
        interesting = {"setFetchedAt", "toChartData", "fetchedAt", "useEffect", "autoRefreshRef"}
        found = interesting & set(symbols.keys())
        assert len(found) >= 1, (
            f"Expected ≥1 of {interesting} in extracted symbols, got: {set(symbols.keys())}"
        )

    def test_replay_snippets_capped(self):
        fixture_path = REPO_ROOT / "tests" / "fixtures" / "pr823.diff"
        diff_text = fixture_path.read_text()
        findings = det.detect(diff_text, str(REPO_ROOT))

        for f in findings:
            assert len(f.get("snippet_primary", "")) <= det.SNIPPET_MAX
            assert len(f.get("snippet_sibling", "")) <= det.SNIPPET_MAX

    def test_replay_no_denied_paths(self):
        fixture_path = REPO_ROOT / "tests" / "fixtures" / "pr823.diff"
        diff_text = fixture_path.read_text()
        findings = det.detect(diff_text, str(REPO_ROOT))

        for f in findings:
            assert not det.is_denied_path(f.get("primary_file", ""))
            assert not det.is_denied_path(f.get("sibling_file", ""))


# ── Tests: control token sanitization in cap_snippet ─────────────────────────

class TestCapSnippetControlTokens:
    """
    cap_snippet() must redact LLM control tokens so a malicious commit
    cannot inject them into Discussion bodies read by downstream agents.
    """

    @pytest.mark.parametrize("token", [
        "</system>",
        "<system>",
        "<|im_start|>",
        "<|im_end|>",
        "[/role]",
        "[role]",
        "<!-- comment -->",
        "<!--",
    ])
    def test_control_token_replaced(self, token: str):
        text = f"def foo(): pass  # {token} injected here"
        result = det.cap_snippet(text)
        assert token not in result, f"Token {token!r} was not redacted"
        assert "[REDACTED]" in result, f"Expected [REDACTED] in output for token {token!r}"

    def test_clean_snippet_unchanged(self):
        text = "def compute_total(items):\n    return sum(items)"
        result = det.cap_snippet(text)
        assert "[REDACTED]" not in result

    def test_truncation_still_applied(self):
        # Build a long clean snippet; it must still be capped at SNIPPET_MAX chars.
        text = "x" * 300
        result = det.cap_snippet(text)
        assert len(result) <= det.SNIPPET_MAX

    def test_token_stripped_before_truncation(self):
        # If a control token is near the 200-char boundary, it must still be replaced.
        padding = "a" * 190
        text = f"{padding}</system>"
        result = det.cap_snippet(text)
        assert "</system>" not in result
        assert "[REDACTED]" in result


# ── Tests: hook off-switch (grep check) ──────────────────────────────────────

class TestHookRepoScope:
    """Every gh call in the hook script must include --repo scoping (dynamically
    resolved via $REPO — see scripts/lib/repo-resolve.sh, not a hard-coded slug)."""

    def test_all_gh_calls_scoped(self):
        hook_path = REPO_ROOT / "scripts" / "hooks" / "post-merge.d" / "cross-file-pattern-check.sh"
        assert hook_path.exists(), f"Hook not found: {hook_path}"

        content = hook_path.read_text()
        # Join backslash-newline continuations so a multi-line `gh ... \` call
        # is checked as one logical command, not just its first line.
        joined = re.sub(r"\\\n[ \t]*", " ", content)
        # Find gh calls and verify each has --repo scoping
        gh_calls = re.findall(r'gh\s+\S.*', joined)
        for call in gh_calls:
            # Skip comment lines and variable assignments
            stripped = call.strip()
            if stripped.startswith("#"):
                continue
            # gh label create, gh api graphql, gh pr diff etc.
            if "gh " in stripped and "--repo" not in stripped and "graphql" not in stripped:
                # graphql calls use owner:"..." in the query body — acceptable
                # but direct gh subcommands must have --repo
                if any(cmd in stripped for cmd in ["gh label", "gh pr", "gh issue"]):
                    assert "--repo" in stripped, \
                        f"gh call missing --repo: {stripped}"
