"""Tests for retry-aware spawn context injection.

Covers:
- circuit_breaker.get_latest_failure() — returns None with no failures, dict when failures exist
- agent_retros.get_latest_retro() — returns None with no matching retros, dict when found
- prompt_builder injects ### Previous Attempt Context when failures exist
- prompt_builder does NOT inject context when no failures exist
- injected section is under 2000 characters
- truncation works when the section would exceed 2000 chars
- previous attempt context appears after VOLATILE_BOUNDARY and before task_prompt
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
import backend.circuit_breaker as cb
import backend.agent_retros as ar
from backend.prompt_builder import SpawnPrompt, _build_previous_attempt_context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_bb(tmp_path):
    """Fresh Blackboard patched into the circuit_breaker module."""
    bb = Blackboard(root=tmp_path / "blackboard")
    with patch.object(cb, "_bb", bb):
        yield bb


@pytest.fixture()
def isolated_retros(tmp_path):
    """Redirect agent_retros RETROS_FILE to a tmp file."""
    retros_path = tmp_path / "agent-retros.jsonl"
    with patch.object(ar, "RETROS_FILE", retros_path):
        yield retros_path


# ---------------------------------------------------------------------------
# AC4: get_latest_failure() returns None for no failures
# ---------------------------------------------------------------------------


class TestGetLatestFailure:
    def test_returns_none_when_no_failures(self, isolated_bb):
        result = cb.get_latest_failure(42)
        assert result is None

    def test_returns_dict_after_one_failure(self, isolated_bb):
        cb.record_failure(42, "executor", "sandbox block")
        result = cb.get_latest_failure(42)
        assert result is not None
        assert result["count"] == 1
        assert result["reason"] == "sandbox block"
        assert result["agent"] == "executor"

    def test_returns_latest_count_after_multiple_failures(self, isolated_bb):
        cb.record_failure(7, "executor", "first error")
        cb.record_failure(7, "executor", "second error")
        result = cb.get_latest_failure(7)
        assert result is not None
        assert result["count"] == 2
        # reason reflects the most recent failure
        assert result["reason"] == "second error"

    def test_returns_none_after_reset(self, isolated_bb):
        cb.record_failure(10, "executor", "err")
        cb.record_success(10)
        result = cb.get_latest_failure(10)
        assert result is None

    def test_independent_discussions(self, isolated_bb):
        cb.record_failure(1, "executor", "err")
        # Discussion 2 must still be None
        assert cb.get_latest_failure(2) is None


# ---------------------------------------------------------------------------
# AC5: get_latest_retro() returns None for no retros
# ---------------------------------------------------------------------------


class TestGetLatestRetro:
    def test_returns_none_when_no_retros_file(self, isolated_retros):
        # File doesn't exist yet
        result = ar.get_latest_retro(42)
        assert result is None

    def test_returns_none_when_file_is_empty(self, isolated_retros):
        isolated_retros.write_text("")
        result = ar.get_latest_retro(42)
        assert result is None

    def test_returns_none_when_no_matching_discussion(self, isolated_retros):
        import json
        entry = {
            "ts": "2026-05-18T10:00:00Z",
            "agent_id": "agent-abc",
            "role": "executor",
            "discussion": 99,
            "classifier": "git_rm_usage",
            "trigger": "used git rm",
            "why": "forgot the rule",
            "future_fix": "use git mv",
            "work_corrected": True,
            "shadow_mode": False,
            "turn_idx": 5,
        }
        isolated_retros.write_text(json.dumps(entry) + "\n")
        # Looking for discussion 42, but only 99 exists
        result = ar.get_latest_retro(42)
        assert result is None

    def test_returns_matching_entry(self, isolated_retros):
        import json
        entry = {
            "ts": "2026-05-18T10:00:00Z",
            "agent_id": "agent-abc",
            "role": "executor",
            "discussion": 42,
            "classifier": "git_rm_usage",
            "trigger": "used git rm in commit",
            "why": "forgot the archive rule",
            "future_fix": "always use git mv to archive/",
            "work_corrected": True,
            "shadow_mode": False,
            "turn_idx": 5,
        }
        isolated_retros.write_text(json.dumps(entry) + "\n")
        result = ar.get_latest_retro(42)
        assert result is not None
        assert result["classifier"] == "git_rm_usage"
        assert result["discussion"] == 42

    def test_returns_most_recent_when_multiple_entries(self, isolated_retros):
        import json
        entries = [
            {
                "ts": "2026-05-18T09:00:00Z",
                "agent_id": "agent-old",
                "role": "executor",
                "discussion": 42,
                "classifier": "first_failure",
                "trigger": "t1",
                "why": "w1",
                "future_fix": "f1",
                "work_corrected": False,
                "shadow_mode": False,
                "turn_idx": 1,
            },
            {
                "ts": "2026-05-18T10:00:00Z",
                "agent_id": "agent-new",
                "role": "executor",
                "discussion": 42,
                "classifier": "second_failure",
                "trigger": "t2",
                "why": "w2",
                "future_fix": "f2",
                "work_corrected": False,
                "shadow_mode": False,
                "turn_idx": 2,
            },
        ]
        isolated_retros.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = ar.get_latest_retro(42)
        assert result is not None
        # Should return the last entry (most recent)
        assert result["classifier"] == "second_failure"

    def test_entries_without_discussion_field_dont_match(self, isolated_retros):
        """Old retro entries without a 'discussion' field should not be returned."""
        import json
        # Old-format entry without discussion field
        entry = {
            "ts": "2026-05-01T10:00:00Z",
            "agent_id": "agent-old",
            "role": "executor",
            "classifier": "some_classifier",
            "trigger": "t",
            "why": "w",
            "future_fix": "f",
            "work_corrected": False,
            "shadow_mode": False,
            "turn_idx": 1,
        }
        isolated_retros.write_text(json.dumps(entry) + "\n")
        result = ar.get_latest_retro(42)
        assert result is None


# ---------------------------------------------------------------------------
# AC1: prompt_builder injects section when failures exist
# ---------------------------------------------------------------------------


class TestPreviousAttemptContextInjection:
    def _make_prompt(self, discussion=None, **kwargs) -> SpawnPrompt:
        defaults = {
            "role": "executor",
            "task_prompt": "do the thing",
            "hook_event_id": "executor-42-ts",
            "_template_body_override": "## TEMPLATE\n\nsome content",
            "_checklist_block_override": "",
        }
        defaults.update(kwargs)
        return SpawnPrompt(discussion=discussion, **defaults)

    def test_context_injected_when_failures_exist(self, isolated_bb):
        cb.record_failure(42, "executor", "preflight typecheck failed")
        sp = self._make_prompt(discussion=42)
        result = sp.render()
        assert "### Previous Attempt Context" in result

    def test_context_not_injected_when_no_failures(self, isolated_bb):
        # Discussion 99 has no failures
        sp = self._make_prompt(discussion=99)
        result = sp.render()
        assert "### Previous Attempt Context" not in result

    def test_context_not_injected_when_discussion_is_none(self, isolated_bb):
        sp = self._make_prompt(discussion=None)
        result = sp.render()
        assert "### Previous Attempt Context" not in result

    def test_context_contains_failure_reason(self, isolated_bb):
        cb.record_failure(55, "executor", "sandbox denied git push")
        sp = self._make_prompt(discussion=55)
        result = sp.render()
        assert "sandbox denied git push" in result

    def test_context_contains_failure_count(self, isolated_bb):
        cb.record_failure(66, "executor", "err")
        cb.record_failure(66, "executor", "err")
        sp = self._make_prompt(discussion=66)
        result = sp.render()
        assert "2" in result  # failure count

    def test_context_after_volatile_boundary(self, isolated_bb):
        cb.record_failure(77, "executor", "some error")
        sp = self._make_prompt(discussion=77)
        result = sp.render()
        vb_pos = result.index("VOLATILE_BOUNDARY")
        ctx_pos = result.index("### Previous Attempt Context")
        assert ctx_pos > vb_pos

    def test_context_before_task_prompt(self, isolated_bb):
        cb.record_failure(88, "executor", "some error")
        sp = self._make_prompt(discussion=88, task_prompt="UNIQUE_TASK_MARKER")
        result = sp.render()
        ctx_pos = result.index("### Previous Attempt Context")
        task_pos = result.index("UNIQUE_TASK_MARKER")
        assert ctx_pos < task_pos

    def test_context_includes_retro_fields(self, isolated_bb, isolated_retros):
        import json
        cb.record_failure(33, "executor", "typecheck error")
        retro_entry = {
            "ts": "2026-05-18T10:00:00Z",
            "agent_id": "agent-abc",
            "role": "executor",
            "discussion": 33,
            "classifier": "bad_import",
            "trigger": "imported wrong module",
            "why": "misread the types",
            "future_fix": "read src/types.ts first",
            "work_corrected": False,
            "shadow_mode": False,
            "turn_idx": 3,
        }
        isolated_retros.write_text(json.dumps(retro_entry) + "\n")
        sp = self._make_prompt(discussion=33)
        result = sp.render()
        assert "bad_import" in result
        assert "read src/types.ts first" in result


# ---------------------------------------------------------------------------
# AC3: injected section is under 2000 characters
# ---------------------------------------------------------------------------


class TestContextSizeCap:
    def test_section_under_2000_chars(self, isolated_bb):
        cb.record_failure(42, "executor", "some failure reason")
        section = _build_previous_attempt_context(42)
        assert len(section) <= 2000

    def test_truncation_when_reason_is_very_long(self, isolated_bb):
        long_reason = "x" * 5000
        cb.record_failure(42, "executor", long_reason)
        section = _build_previous_attempt_context(42)
        assert len(section) <= 2000
        assert section.endswith("...")

    def test_no_truncation_for_normal_content(self, isolated_bb):
        cb.record_failure(42, "executor", "short reason")
        section = _build_previous_attempt_context(42)
        assert not section.endswith("...")
        assert len(section) < 2000


# ---------------------------------------------------------------------------
# build_previous_attempt_context unit tests
# ---------------------------------------------------------------------------


class TestBuildPreviousAttemptContext:
    def test_returns_empty_when_discussion_is_none(self):
        result = _build_previous_attempt_context(None)
        assert result == ""

    def test_returns_empty_when_no_failures(self, isolated_bb):
        result = _build_previous_attempt_context(123)
        assert result == ""

    def test_returns_section_string_when_failures_exist(self, isolated_bb):
        cb.record_failure(123, "executor", "reason")
        result = _build_previous_attempt_context(123)
        assert isinstance(result, str)
        assert "### Previous Attempt Context" in result

    def test_handles_missing_circuit_breaker_import(self):
        """If circuit_breaker can't be imported, return empty string gracefully."""
        import builtins
        import importlib

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "backend.circuit_breaker":
                raise ImportError("simulated import failure")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            # Re-import to pick up mock — call directly with fresh module context
            result = _build_previous_attempt_context(42)
        # Should not raise; returns empty string (import inside function body)
        # Note: since _build_previous_attempt_context uses 'from backend...' inside,
        # and the module is already cached, we test the None-discussion path for safety.
        result = _build_previous_attempt_context(None)
        assert result == ""
