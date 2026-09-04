"""Unit tests for discussion_status.set_status()."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.discussion_status import extract_status, set_status, set_status_anchored

FIXED_NOW = "2026-05-22T00:00:00Z"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "d2021-precorruption-body.txt"


class TestSetStatusInsert:
    """Body WITHOUT an existing marker → marker prepended."""

    def test_marker_prepended(self):
        body = "Some discussion text.\n\n## Intent\nDo the thing."
        result = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        assert result.startswith(f"<!-- STATUS:SPEC_READY SINCE:{FIXED_NOW} -->")

    def test_original_body_preserved(self):
        body = "Some discussion text."
        result = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        assert "Some discussion text." in result

    def test_extract_status_round_trips(self):
        body = "Fresh discussion with no marker."
        result = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        assert extract_status(result) == "SPEC_READY"

    def test_empty_body_gets_marker(self):
        result = set_status("", "DISCUSSING", now_iso=FIXED_NOW)
        assert extract_status(result) == "DISCUSSING"

    def test_none_body_gets_marker(self):
        result = set_status(None, "DISCUSSING", now_iso=FIXED_NOW)  # type: ignore[arg-type]
        assert extract_status(result) == "DISCUSSING"

    def test_only_one_marker_after_insert(self):
        body = "No marker here."
        result = set_status(body, "IMPLEMENTING", now_iso=FIXED_NOW)
        assert result.count("<!-- STATUS:") == 1


class TestSetStatusReplace:
    """Body WITH an existing marker → marker replaced, not duplicated."""

    def test_existing_with_since_replaced(self):
        body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\nSome text."
        result = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        assert f"<!-- STATUS:SPEC_READY SINCE:{FIXED_NOW} -->" in result

    def test_old_status_absent_after_replace(self):
        body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\nSome text."
        result = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        assert "DISCUSSING" not in result

    def test_only_one_marker_after_replace(self):
        body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\nSome text."
        result = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        assert result.count("<!-- STATUS:") == 1

    def test_body_text_preserved_after_replace(self):
        body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\nSome text."
        result = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        assert "Some text." in result

    def test_bare_marker_no_since_replaced(self):
        body = "<!-- STATUS:DISCUSSING -->\n\nSome text."
        result = set_status(body, "IMPLEMENTING", now_iso=FIXED_NOW)
        assert extract_status(result) == "IMPLEMENTING"
        assert result.count("<!-- STATUS:") == 1

    def test_extract_status_round_trips_replace(self):
        body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\nSome text."
        result = set_status(body, "REVIEWING", now_iso=FIXED_NOW)
        assert extract_status(result) == "REVIEWING"


class TestRoundTrip:
    """Double set_status call → exactly one marker with the LAST status."""

    def test_double_set_leaves_one_marker(self):
        body = "Fresh body."
        body = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        body = set_status(body, "IMPLEMENTING", now_iso=FIXED_NOW)
        assert body.count("<!-- STATUS:") == 1
        assert extract_status(body) == "IMPLEMENTING"

    def test_triple_set_leaves_one_marker(self):
        body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\ntext"
        body = set_status(body, "SPEC_READY", now_iso=FIXED_NOW)
        body = set_status(body, "IMPLEMENTING", now_iso=FIXED_NOW)
        body = set_status(body, "REVIEWING", now_iso=FIXED_NOW)
        assert body.count("<!-- STATUS:") == 1
        assert extract_status(body) == "REVIEWING"


class TestSetStatusAnchored:
    """set_status_anchored() — the precondition backing `set-status --stdin`.

    Replaces scripts/post-merge-hook.sh:336's unanchored, global `sed`
    rewrite (D#2021): that sed rewrote every occurrence of the marker token
    anywhere in the body, prose and fenced code included. This function (and
    the CLI subcommand built on it) touches exactly the marker on the first
    non-empty line, and refuses outright when that line has no marker rather
    than guessing which occurrence elsewhere is authoritative.
    """

    def test_anchored_marker_is_rewritten(self):
        body = "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\n\nSome text."
        result = set_status_anchored(body, "DONE", now_iso=FIXED_NOW)
        assert result.startswith(f"<!-- STATUS:DONE SINCE:{FIXED_NOW} -->")

    def test_unanchored_marker_raises(self):
        # First non-empty line is ordinary prose; the only marker sits later.
        body = "Ordinary prose on line 1.\n\nmore prose\n\n<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\n"
        with pytest.raises(ValueError):
            set_status_anchored(body, "DONE", now_iso=FIXED_NOW)

    def test_empty_body_raises(self):
        with pytest.raises(ValueError):
            set_status_anchored("", "DONE", now_iso=FIXED_NOW)

    def test_blocked_by_survives_anchored_write(self):
        # Regression guard on D#1755 — same guarantee as set_status() itself,
        # just reached through the anchored wrapper.
        body = "<!-- STATUS:SPEC_READY BLOCKED-BY:#1750 SINCE:2026-01-01T00:00:00Z -->\n\ntext"
        result = set_status_anchored(body, "DONE", now_iso=FIXED_NOW)
        assert "BLOCKED-BY:#1750" in result

    def test_fixture_census_only_one_token_changes(self):
        # Reproduces D#2021's own pre-edit body: one marker (line 1), four
        # STATUS:DONE mentions in prose, two bare STATUS: prefixes in a
        # fenced code block — seven tokens total. The old sed at
        # post-merge-hook.sh:336 rewrote all seven; this must rewrite one.
        before = _FIXTURE.read_text()
        before_tokens = before.count("STATUS:")
        assert before_tokens == 7, f"fixture census drifted: expected 7 STATUS: tokens, found {before_tokens}"

        after = set_status_anchored(before, "DONE", now_iso=FIXED_NOW)
        after_tokens = after.count("STATUS:")
        assert after_tokens == 7, "token count must not change, only the marker value"

        before_lines = before.splitlines()
        after_lines = after.splitlines()
        assert len(before_lines) == len(after_lines)

        changed = [i for i in range(len(before_lines)) if before_lines[i] != after_lines[i]]
        assert changed == [0], f"expected only line 1 to change, but line(s) {changed} differed"
        assert "STATUS:DONE" in after_lines[0]
        for i in range(1, len(before_lines)):
            assert before_lines[i] == after_lines[i], f"line {i} changed unexpectedly (fence/prose must be untouched)"


class TestSetStatusCli:
    """Smoke-test the actual `set-status --stdin` subprocess entry point —
    not just the library function it wraps."""

    def _run_cli(self, stdin_body: str, value: str = "DONE"):
        return subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "discussion_status.py"), "set-status", "--stdin", value],
            input=stdin_body,
            capture_output=True,
            text=True,
        )

    def test_cli_anchored_write_succeeds(self):
        body = "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\n\ntext"
        result = self._run_cli(body)
        assert result.returncode == 0
        assert "STATUS:DONE" in result.stdout
        assert result.stdout.count("STATUS:") == 1

    def test_cli_unanchored_refuses(self):
        body = "prose first\n\n<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\n"
        result = self._run_cli(body)
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip() != ""

    def test_cli_fixture_rewrites_exactly_one_token(self):
        body = _FIXTURE.read_text()
        result = self._run_cli(body)
        assert result.returncode == 0
        assert result.stdout.count("STATUS:") == 7
        before_lines = body.splitlines()
        after_lines = result.stdout.splitlines()
        changed = [i for i in range(len(before_lines)) if before_lines[i] != after_lines[i]]
        assert changed == [0]
