"""
Regression tests for the _EMPTY_SKELETON shallow-copy bug in context_manager.py.

Before the fix, dict(_EMPTY_SKELETON) produced a shallow copy — the list values
(goals, decisions, milestones, stack, banned) aliased the module-level skeleton.
Appending to one load's list would silently mutate _EMPTY_SKELETON and affect
every subsequent load() call that returned the missing-file or corrupt-file path.

These tests verify:
1. Two calls to load() against a missing context file return dicts whose list
   values are NOT the same object (identity check with `is not`).
2. Appending to one load's `goals` list does NOT affect a second fresh load.
3. Appending to one load's `goals` list does NOT affect _EMPTY_SKELETON itself.
"""

import sys
from pathlib import Path

import pytest

# Ensure the backend package is importable regardless of how pytest is invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.context_manager import ProjectContext, _EMPTY_SKELETON  # noqa: E402


class TestSkeletonIsolation:
    """Each load() must return an independent copy — no list aliasing."""

    def test_two_loads_return_distinct_list_objects(self, tmp_path):
        """List values from two separate load() calls must not be the same object."""
        ctx = ProjectContext(state_dir=tmp_path)
        # Context file does not exist — exercises the missing-file path.
        first = ctx.load()
        second = ctx.load()

        for key in ("goals", "decisions", "milestones", "stack", "banned"):
            assert first[key] is not second[key], (
                f"field '{key}': two load() calls returned the same list object "
                f"(shallow copy bug still present)"
            )

    def test_append_to_first_load_does_not_affect_second_load(self, tmp_path):
        """Mutating a list from one load() must not bleed into the next load()."""
        ctx = ProjectContext(state_dir=tmp_path)
        first = ctx.load()

        # Append a sentinel to goals on the first result.
        first["goals"].append({"id": "g-sentinel", "text": "canary"})

        # Second fresh load must see an empty goals list.
        second = ctx.load()
        assert second["goals"] == [], (
            "Appending to first load's 'goals' mutated _EMPTY_SKELETON "
            "and leaked into the second load() call"
        )

    def test_append_does_not_mutate_module_level_skeleton(self, tmp_path):
        """Mutating a list from load() must not alter _EMPTY_SKELETON itself."""
        ctx = ProjectContext(state_dir=tmp_path)
        result = ctx.load()

        original_goals = list(_EMPTY_SKELETON["goals"])  # snapshot before mutation
        result["goals"].append({"id": "g-canary", "text": "should not leak"})

        assert _EMPTY_SKELETON["goals"] == original_goals, (
            "Appending to load()'s 'goals' mutated the module-level _EMPTY_SKELETON "
            "(shallow copy bug still present)"
        )

    def test_corrupt_file_path_also_returns_independent_copies(self, tmp_path):
        """The corrupt-file fallback path must also return independent copies."""
        ctx = ProjectContext(state_dir=tmp_path)

        # Write a corrupt JSON file to trigger the except branch.
        data_path = tmp_path / "project-context.json"
        data_path.write_text("{ NOT VALID JSON <<<", encoding="utf-8")

        first = ctx.load()
        second = ctx.load()

        for key in ("goals", "decisions", "milestones", "stack", "banned"):
            assert first[key] is not second[key], (
                f"corrupt-file path: field '{key}' still aliases _EMPTY_SKELETON lists"
            )

    def test_all_list_fields_isolated(self, tmp_path):
        """Verify isolation for every list field, not just goals."""
        ctx = ProjectContext(state_dir=tmp_path)
        first = ctx.load()

        sentinel = {"_test": "canary"}
        for key in ("goals", "decisions", "milestones", "stack", "banned"):
            first[key].append(sentinel)

        second = ctx.load()
        for key in ("goals", "decisions", "milestones", "stack", "banned"):
            assert sentinel not in second[key], (
                f"Mutation of first load's '{key}' leaked into second load"
            )
        for key in ("goals", "decisions", "milestones", "stack", "banned"):
            assert sentinel not in _EMPTY_SKELETON[key], (
                f"Mutation of first load's '{key}' leaked into _EMPTY_SKELETON"
            )
