"""
Meta-test: guard against duplicate test basenames across test directories.

pytest --import-mode=importlib (our setting) is sensitive to same-basename test files
in different directories: two files named test_foo.py in different dirs can shadow each
other at import time AND create the "missed twin" trap — editing a symbol breaks the
un-updated duplicate silently.

This test will PASS on the current codebase because the known pre-existing duplicates
are in the allowlist below. It will FAIL immediately if anyone introduces a NEW
duplicate basename, catching the collision before it causes confusion.

To add a new allowlist entry: paste the exact basename, add a comment explaining
WHY both copies are intentionally kept, and open a Discussion to track cleanup.
"""

import os
from collections import defaultdict
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Repo root: the directory containing this file's parent (tests/) lives inside
# the repo root or a worktree with the same layout.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories to scan. Paths are relative to repo root.
_SCAN_DIRS = [
    "backend/tests",
    "backend",           # legacy test_*.py files at the top of backend/
    "tests",
    "tests/orchestrator",
    "tests/backend",
    "tests/corpus_drift",
    "tests/integration",
    "dashboard_tui/tests",
]

# Directory name fragments to EXCLUDE from the scan.
# These are matched against each path component (directory name), not the full
# absolute path — so "worktrees" does not accidentally exclude the entire
# worktree when this test runs from inside a git worktree.
_EXCLUDE_DIR_NAMES = {
    "node_modules",
    ".venv",
    "archive",
    "loop-bootstrap",
    # 'scripts' is not a scan dir, but exclude it if somehow encountered
}

# ---------------------------------------------------------------------------
# Pre-existing duplicates allowlist
# ---------------------------------------------------------------------------
# These basenames currently appear in more than one scan directory.
# They are listed here so the test passes on main while still catching any
# NEW collision.  Each entry has a comment explaining the situation.
#
# IMPORTANT: do NOT add a new entry without opening a Discussion to track
# cleanup.  The goal is to shrink this list over time, not grow it.
#
_ALLOWLIST: set = {
    # backend/test_anomaly_detector.py  vs  backend/tests/test_anomaly_detector.py
    # Legacy root-level file predates the tests/ sub-dir; tracked for cleanup.
    "test_anomaly_detector.py",

    # backend/test_cost_tracker.py  vs  tests/test_cost_tracker.py
    "test_cost_tracker.py",

    # backend/test_discussion_cache.py  vs  tests/test_discussion_cache.py
    "test_discussion_cache.py",

    # backend/test_quality_scorer.py  vs  backend/tests/test_quality_scorer.py
    #   vs  tests/test_quality_scorer.py  (THREE copies — extra bad)
    "test_quality_scorer.py",

    # backend/test_replay.py  vs  backend/tests/test_replay.py
    "test_replay.py",

    # backend/test_server.py  vs  backend/tests/test_server.py
    "test_server.py",

    # backend/test_session_manager.py  vs  backend/tests/test_session_manager.py
    "test_session_manager.py",

    # backend/test_spawn_guard.py  vs  backend/tests/test_spawn_guard.py
    "test_spawn_guard.py",

    # backend/test_status_page.py  vs  backend/tests/test_status_page.py
    #   vs  tests/test_status_page.py
    "test_status_page.py",

    # backend/test_websocket.py  vs  backend/tests/test_websocket.py
    "test_websocket.py",

    # backend/tests/test_a2a_broker.py  vs  tests/test_a2a_broker.py
    "test_a2a_broker.py",

    # backend/tests/test_agent_retry.py  vs  tests/test_agent_retry.py
    "test_agent_retry.py",

    # backend/tests/test_agent_run_tracker.py  vs  tests/test_agent_run_tracker.py
    "test_agent_run_tracker.py",

    # backend/tests/test_blackboard.py  vs  tests/test_blackboard.py
    "test_blackboard.py",

    # backend/tests/test_changelog.py  vs  tests/test_changelog.py
    "test_changelog.py",

    # backend/tests/test_circuit_breaker.py  vs  tests/test_circuit_breaker.py
    "test_circuit_breaker.py",

    # backend/tests/test_config_watcher.py  vs  tests/test_config_watcher.py
    "test_config_watcher.py",

    # backend/tests/test_context_manager.py  vs  tests/test_context_manager.py
    "test_context_manager.py",

    # backend/tests/test_dispatch_offload.py  vs  tests/orchestrator/test_dispatch_offload.py
    "test_dispatch_offload.py",

    # backend/tests/test_event_bus.py  vs  tests/test_event_bus.py
    "test_event_bus.py",

    # backend/tests/test_fleet_concurrency.py  vs  tests/backend/test_fleet_concurrency.py
    "test_fleet_concurrency.py",

    # backend/tests/test_health_monitor.py  vs  tests/test_health_monitor.py
    "test_health_monitor.py",

    # backend/tests/test_import_epic_tasks.py  vs  tests/test_import_epic_tasks.py
    "test_import_epic_tasks.py",

    # backend/tests/test_kpi_engine.py  vs  tests/test_kpi_engine.py
    "test_kpi_engine.py",

    # backend/tests/test_loop_runs.py  vs  tests/backend/test_loop_runs.py
    "test_loop_runs.py",

    # backend/tests/test_pr_state.py  vs  tests/test_pr_state.py
    "test_pr_state.py",

    # backend/tests/test_redaction.py  vs  tests/test_redaction.py
    "test_redaction.py",

    # backend/tests/test_workflow_runner.py  vs  tests/test_workflow_runner.py
    "test_workflow_runner.py",
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _collect_test_files() -> dict[str, list[Path]]:
    """Return {basename: [full_path, ...]} for all test_*.py files in scan dirs."""
    by_basename: dict[str, list[Path]] = defaultdict(list)

    for rel_dir in _SCAN_DIRS:
        scan_dir = _REPO_ROOT / rel_dir
        if not scan_dir.is_dir():
            continue
        # Check that none of the path components relative to repo root are excluded.
        try:
            rel_parts = set(Path(rel_dir).parts)
        except Exception:
            rel_parts = set()
        if rel_parts & _EXCLUDE_DIR_NAMES:
            continue

        # Non-recursive: each entry in _SCAN_DIRS is a specific directory.
        # We do NOT recurse further to avoid picking up nested sub-dirs twice.
        for entry in scan_dir.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.startswith("test_") or not entry.name.endswith(".py"):
                continue
            by_basename[entry.name].append(entry.resolve())

    return dict(by_basename)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_no_new_duplicate_test_basenames() -> None:
    """Fail if any test_*.py basename appears in more than one scan directory,
    unless it is listed in _ALLOWLIST."""
    by_basename = _collect_test_files()

    new_dups: dict[str, list[Path]] = {}
    for name, paths in sorted(by_basename.items()):
        if len(paths) <= 1:
            continue
        # Duplicate — is it in the allowlist?
        if name in _ALLOWLIST:
            continue
        new_dups[name] = paths

    if not new_dups:
        return  # all good

    lines = [
        "",
        "NEW duplicate test basenames detected (not in _ALLOWLIST):",
        "",
    ]
    for name, paths in sorted(new_dups.items()):
        lines.append(f"  {name}:")
        for p in sorted(paths):
            lines.append(f"    {p}")
        lines.append("")
    lines += [
        "Each basename must be unique across test directories to prevent",
        "pytest --import-mode=importlib collisions and the 'missed twin' trap.",
        "",
        "If the duplication is intentional (rare), add the basename to",
        "_ALLOWLIST in tests/test_no_duplicate_test_basenames.py with a",
        "comment explaining why, then open a Discussion to track cleanup.",
    ]
    pytest.fail("\n".join(lines))
