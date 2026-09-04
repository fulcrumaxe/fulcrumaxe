"""
Regression tests for registry.py hardening — bugs #1272 and #1273.

#1272: load() used dict(_EMPTY_REGISTRY) — a shallow copy — so the nested
       `discussions` list aliased the module-level sentinel. Appends on one
       caller's result leaked into subsequent calls and into _EMPTY_REGISTRY.

#1273: _fetch_all_discussions() had no error handling around
       subprocess.check_output / json.loads / dict field access, so a transient
       gh failure or malformed response would raise and crash the sync loop.

Run with:
    python -m pytest backend/tests/test_registry_hardening.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.registry import DiscussionRegistry, _EMPTY_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(tmp_path: Path) -> DiscussionRegistry:
    return DiscussionRegistry(state_dir=tmp_path)


# ---------------------------------------------------------------------------
# Bug #1272 — deepcopy: load() must not alias _EMPTY_REGISTRY
# ---------------------------------------------------------------------------


class TestLoadDeepCopy:
    """load() must return a fully independent copy each time it is called."""

    def test_discussions_list_not_same_object_missing_file(self, tmp_path):
        """Two load() calls on a missing registry return distinct list objects."""
        reg = _make_registry(tmp_path)
        a = reg.load()
        b = reg.load()
        assert a["discussions"] is not b["discussions"], (
            "discussions lists from two load() calls are the same object — shallow copy bug"
        )

    def test_discussions_list_not_same_object_empty_file(self, tmp_path):
        """Two load() calls after OSError fallback return distinct list objects."""
        # Write a corrupt file so load() hits the except branch.
        (tmp_path / "registry.json").write_text("{bad json")
        reg = _make_registry(tmp_path)
        a = reg.load()
        b = reg.load()
        assert a["discussions"] is not b["discussions"]

    def test_append_to_result_does_not_affect_empty_registry_sentinel(self, tmp_path):
        """Appending to a load() result must not mutate _EMPTY_REGISTRY."""
        reg = _make_registry(tmp_path)
        result = reg.load()
        # Mutate the returned list.
        result["discussions"].append({"number": 999, "title": "injected"})
        # _EMPTY_REGISTRY must be unchanged.
        assert _EMPTY_REGISTRY["discussions"] == [], (
            "Mutating load() result polluted the module-level _EMPTY_REGISTRY sentinel"
        )

    def test_append_to_first_does_not_affect_second(self, tmp_path):
        """Mutating the first load() result must not affect a subsequent load() call."""
        reg = _make_registry(tmp_path)
        first = reg.load()
        first["discussions"].append({"number": 1})
        second = reg.load()
        assert second["discussions"] == [], (
            "Mutation of first load() result leaked into second load() call"
        )

    def test_nested_list_independent_when_file_has_discussions(self, tmp_path):
        """load() from a real file still returns an independent discussions list."""
        content = {
            "version": 1,
            "synced_at": "2026-01-01T00:00:00+00:00",
            "discussions": [{"number": 1, "title": "Existing"}],
            "velocity": {},
        }
        (tmp_path / "registry.json").write_text(json.dumps(content))
        reg = _make_registry(tmp_path)
        a = reg.load()
        b = reg.load()
        # They come from json.load (fresh each time) so should already be distinct,
        # but the fix also prevents the fallback branches from aliasing.
        assert a["discussions"] is not b["discussions"]


# ---------------------------------------------------------------------------
# Bug #1273 — error handling: _fetch_all_discussions() must degrade gracefully
# ---------------------------------------------------------------------------


class TestFetchAllDiscussionsErrorHandling:
    """_fetch_all_discussions() must return a fallback, never raise, on errors."""

    def test_called_process_error_returns_empty_list(self, tmp_path, capsys):
        """CalledProcessError from gh → returns [] instead of raising."""
        reg = _make_registry(tmp_path)
        with patch(
            "backend.registry.subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "gh", stderr="auth error"),
        ):
            result = reg._fetch_all_discussions()

        assert result == [], f"Expected [] on CalledProcessError, got {result!r}"

    def test_called_process_error_logs_to_stderr(self, tmp_path, capsys):
        """CalledProcessError is logged to stderr — not swallowed silently."""
        reg = _make_registry(tmp_path)
        with patch(
            "backend.registry.subprocess.check_output",
            side_effect=subprocess.CalledProcessError(128, "gh"),
        ):
            reg._fetch_all_discussions()

        captured = capsys.readouterr()
        assert captured.err, "No stderr output — error was silently swallowed"
        assert "gh" in captured.err.lower() or "registry" in captured.err.lower(), (
            f"stderr didn't mention the source of failure: {captured.err!r}"
        )

    def test_malformed_json_returns_empty_list(self, tmp_path, capsys):
        """Non-JSON response from gh → returns [] instead of raising."""
        reg = _make_registry(tmp_path)
        with patch(
            "backend.registry.subprocess.check_output",
            return_value="not json at all !!!",
        ):
            result = reg._fetch_all_discussions()

        assert result == [], f"Expected [] on JSONDecodeError, got {result!r}"

    def test_malformed_json_logs_to_stderr(self, tmp_path, capsys):
        """Malformed JSON is logged to stderr — not swallowed silently."""
        reg = _make_registry(tmp_path)
        with patch(
            "backend.registry.subprocess.check_output",
            return_value="not json",
        ):
            reg._fetch_all_discussions()

        captured = capsys.readouterr()
        assert captured.err, "No stderr output — JSON error was silently swallowed"

    def test_missing_key_in_response_returns_empty_list(self, tmp_path, capsys):
        """Response missing expected keys → returns [] instead of raising KeyError."""
        reg = _make_registry(tmp_path)
        # Valid JSON but missing the nested structure entirely.
        with patch(
            "backend.registry.subprocess.check_output",
            return_value=json.dumps({"data": {}}),
        ):
            result = reg._fetch_all_discussions()

        assert result == [], f"Expected [] on KeyError, got {result!r}"

    def test_missing_key_logs_to_stderr(self, tmp_path, capsys):
        """Missing key error is logged to stderr."""
        reg = _make_registry(tmp_path)
        with patch(
            "backend.registry.subprocess.check_output",
            return_value=json.dumps({"data": {}}),
        ):
            reg._fetch_all_discussions()

        captured = capsys.readouterr()
        assert captured.err, "No stderr output — KeyError was silently swallowed"

    def test_error_on_second_page_returns_partial_results(self, tmp_path):
        """
        If the first page succeeds but the second page raises, we return the
        partial results from page 1 instead of crashing.
        """
        reg = _make_registry(tmp_path)

        page1 = json.dumps({
            "data": {
                "repository": {
                    "discussions": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-xyz"},
                        "nodes": [{"number": 1, "title": "Page 1 item"}],
                    }
                }
            }
        })

        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return page1
            raise subprocess.CalledProcessError(1, "gh")

        with patch("backend.registry.subprocess.check_output", side_effect=side_effect):
            result = reg._fetch_all_discussions()

        # Must return what we have, not raise.
        assert isinstance(result, list)
        # We got at least the nodes from page 1.
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_incomplete_fetch_sets_flag_to_false(self, tmp_path):
        """_fetch_all_discussions() must set _last_fetch_complete=False on any error path."""
        reg = _make_registry(tmp_path)
        with patch(
            "backend.registry.subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            reg._fetch_all_discussions()

        assert reg._last_fetch_complete is False, (
            "_last_fetch_complete should be False when the fetch fails"
        )

    def test_complete_fetch_sets_flag_to_true(self, tmp_path):
        """_fetch_all_discussions() must set _last_fetch_complete=True on a clean run."""
        reg = _make_registry(tmp_path)

        single_page = json.dumps({
            "data": {
                "repository": {
                    "discussions": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"number": 42, "title": "Done"}],
                    }
                }
            }
        })

        with patch("backend.registry.subprocess.check_output", return_value=single_page):
            reg._fetch_all_discussions()

        assert reg._last_fetch_complete is True, (
            "_last_fetch_complete should be True after a successful fetch"
        )


# ---------------------------------------------------------------------------
# Bug: sync() silently corrupts registry on partial fetch
# ---------------------------------------------------------------------------


class TestSyncNoWriteOnPartialFetch:
    """sync() must NOT write to disk when _fetch_all_discussions() returns an
    incomplete result due to a mid-pagination error."""

    def _page1_then_fail_side_effect(self):
        """Return a callable that succeeds on the first gh call then raises."""
        page1 = json.dumps({
            "data": {
                "repository": {
                    "discussions": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-abc"},
                        "nodes": [{"number": 1, "title": "First page item"}],
                    }
                }
            }
        })
        call_count = {"n": 0}

        def _side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return page1
            raise subprocess.CalledProcessError(1, "gh", stderr="network error")

        return _side_effect

    def test_registry_json_unchanged_on_mid_pagination_error(self, tmp_path, capsys):
        """sync() must not overwrite registry.json when the fetch is incomplete."""
        # Seed a known previous registry on disk.
        prior_content = {
            "version": 1,
            "synced_at": "2026-01-01T00:00:00+00:00",
            "discussions": [
                {"number": 10, "title": "Prior disc A"},
                {"number": 20, "title": "Prior disc B"},
            ],
            "velocity": {},
        }
        registry_path = tmp_path / "registry.json"
        registry_path.write_text(json.dumps(prior_content))

        reg = _make_registry(tmp_path)

        with patch(
            "backend.registry.subprocess.check_output",
            side_effect=self._page1_then_fail_side_effect(),
        ):
            result = reg.sync()

        # The file on disk must still be the prior content — not the truncated page-1 result.
        on_disk = json.loads(registry_path.read_text())
        assert on_disk["discussions"] == prior_content["discussions"], (
            "sync() overwrote registry.json with a partial result — "
            f"expected {prior_content['discussions']!r}, got {on_disk['discussions']!r}"
        )

    def test_sync_returns_previous_registry_on_mid_pagination_error(self, tmp_path):
        """sync() must return the previous registry (not the partial result) on error."""
        prior_content = {
            "version": 1,
            "synced_at": "2026-01-01T00:00:00+00:00",
            "discussions": [
                {"number": 10, "title": "Prior disc A"},
                {"number": 20, "title": "Prior disc B"},
            ],
            "velocity": {},
        }
        (tmp_path / "registry.json").write_text(json.dumps(prior_content))

        reg = _make_registry(tmp_path)

        with patch(
            "backend.registry.subprocess.check_output",
            side_effect=self._page1_then_fail_side_effect(),
        ):
            result = reg.sync()

        # The returned value must contain the full prior discussions, not just page 1.
        numbers = {d["number"] for d in result["discussions"]}
        assert numbers == {10, 20}, (
            f"sync() returned wrong discussions on partial fetch: {result['discussions']!r}"
        )

    def test_write_not_called_on_mid_pagination_error(self, tmp_path):
        """sync() must not call _write() when the fetch is incomplete."""
        reg = _make_registry(tmp_path)

        with patch(
            "backend.registry.subprocess.check_output",
            side_effect=self._page1_then_fail_side_effect(),
        ), patch.object(reg, "_write") as mock_write:
            reg.sync()

        mock_write.assert_not_called()

    def test_sync_logs_warning_on_incomplete_fetch(self, tmp_path, capsys):
        """sync() must emit a warning to stderr when it skips the write."""
        reg = _make_registry(tmp_path)

        with patch(
            "backend.registry.subprocess.check_output",
            side_effect=self._page1_then_fail_side_effect(),
        ):
            reg.sync()

        captured = capsys.readouterr()
        assert captured.err, "sync() emitted no stderr warning on incomplete fetch"
        assert "incomplete" in captured.err.lower() or "skip" in captured.err.lower(), (
            f"sync() stderr did not mention skipping: {captured.err!r}"
        )

    def test_sync_writes_normally_on_complete_single_page_fetch(self, tmp_path):
        """sync() must write to disk when the fetch completes normally."""
        reg = _make_registry(tmp_path)

        single_page = json.dumps({
            "data": {
                "repository": {
                    "discussions": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    }
                }
            }
        })

        registry_path = tmp_path / "registry.json"
        assert not registry_path.exists(), "Precondition: file should not exist yet"

        with patch("backend.registry.subprocess.check_output", return_value=single_page):
            reg.sync()

        assert registry_path.exists(), (
            "sync() did not write registry.json after a successful complete fetch"
        )
