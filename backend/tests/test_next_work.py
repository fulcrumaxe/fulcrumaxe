"""Tests for backend/next_work.py.

Verifies:
- Ranking is deterministic (same input → same output, twice).
- --json emits valid JSON array.
- Coverage-gap detection works on a fixture directory.
- No network / LLM is invoked (all data sources are injectable).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.next_work import (
    coverage_gaps,
    health_reds,
    history_signal,
    rank_next_work,
    stale_registry_candidates,
    _human_output,
    _get_git_tracked_files,
    HISTORY_SIGNAL_WEIGHT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OPEN_DISCUSSING = {
    "number": 10,
    "title": "Old discussion",
    "status": "DISCUSSING",
    "created_at": "2024-01-01T00:00:00Z",
    "closed_at": None,
}

OPEN_SPEC_READY = {
    "number": 20,
    "title": "Newer spec-ready",
    "status": "SPEC_READY",
    "created_at": "2025-06-01T00:00:00Z",
    "closed_at": None,
}

CLOSED_DONE = {
    "number": 30,
    "title": "Already done",
    "status": "DONE",
    "created_at": "2024-01-01T00:00:00Z",
    "closed_at": "2024-06-01T00:00:00Z",
}

IMPLEMENTING = {
    "number": 40,
    "title": "In flight",
    "status": "IMPLEMENTING",
    "created_at": "2025-01-01T00:00:00Z",
    "closed_at": None,
}


# ---------------------------------------------------------------------------
# stale_registry_candidates
# ---------------------------------------------------------------------------


def test_only_actionable_statuses_returned():
    """Closed and non-actionable discussions are filtered out."""
    discussions = [OPEN_DISCUSSING, OPEN_SPEC_READY, CLOSED_DONE, IMPLEMENTING]
    result = stale_registry_candidates(discussions=discussions)
    numbers = [r["number"] for r in result]
    assert 10 in numbers
    assert 20 in numbers
    assert 30 not in numbers   # closed
    assert 40 not in numbers   # IMPLEMENTING, not actionable


def test_oldest_first_ordering():
    """Oldest created_at appears first."""
    discussions = [OPEN_SPEC_READY, OPEN_DISCUSSING]
    result = stale_registry_candidates(discussions=discussions)
    assert result[0]["number"] == 10  # 2024 < 2025


def test_category_label():
    result = stale_registry_candidates(discussions=[OPEN_DISCUSSING])
    assert result[0]["category"] == "stale_discussion"


def test_age_days_is_positive():
    result = stale_registry_candidates(discussions=[OPEN_DISCUSSING])
    assert result[0]["age_days"] is not None
    assert result[0]["age_days"] > 0


# ---------------------------------------------------------------------------
# health_reds
# ---------------------------------------------------------------------------


def _fake_check_pass():
    return {"name": "fake_pass", "ok": True, "detail": "all good"}


def _fake_check_fail():
    return {"name": "fake_fail", "ok": False, "detail": "something broken"}


def test_health_reds_filters_only_failed():
    result = health_reds(checks=[_fake_check_pass, _fake_check_fail])
    assert len(result) == 1
    assert result[0]["name"] == "fake_fail"
    assert result[0]["category"] == "health_red"


def test_health_reds_empty_when_all_pass():
    result = health_reds(checks=[_fake_check_pass])
    assert result == []


def test_health_reds_no_network_invoked():
    """Injecting check functions means no real I/O happens."""
    # This would fail if real checks ran and needed DB/files
    result = health_reds(checks=[_fake_check_fail])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# coverage_gaps
# ---------------------------------------------------------------------------


def test_coverage_gap_detected():
    """A module with no test file is detected as a gap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        # Create a module with no test
        (backend_dir / "my_module.py").write_text("# module\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "my_module" in modules


def test_covered_module_not_in_gaps():
    """A module with a matching test_*.py is NOT a gap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "my_module.py").write_text("# module\n")
        (tests_dir / "test_my_module.py").write_text("# tests\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "my_module" not in modules


def test_test_files_excluded_from_gaps():
    """test_*.py files in backend dir are not flagged as modules needing tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "test_something.py").write_text("# test file\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "test_something" not in modules
        assert "something" not in modules


def test_dunder_and_private_excluded():
    """__init__.py and _private.py are not flagged as coverage gaps."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "__init__.py").write_text("")
        (backend_dir / "__main__.py").write_text("")
        (backend_dir / "_private.py").write_text("")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "__init__" not in modules
        assert "__main__" not in modules
        assert "_private" not in modules


def test_coverage_gap_category_label():
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()
        (backend_dir / "orphan.py").write_text("# orphan\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        assert gaps[0]["category"] == "coverage_gap"


def test_covered_by_tests_subdir_not_flagged():
    """A module with a backend/tests/test_<mod>.py is NOT flagged as a gap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        # Module in backend/
        (backend_dir / "budget.py").write_text("# budget module\n")
        # Test file in backend/tests/ with the exact name
        (tests_dir / "test_budget.py").write_text("# tests\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "budget" not in modules


def test_covered_by_import_in_test_not_flagged():
    """A module imported by any test file is NOT flagged as a gap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        # Module with no dedicated test file
        (backend_dir / "budget.py").write_text("# budget module\n")
        # An indirectly-named test file that imports the module
        (tests_dir / "test_loop_controller_budget.py").write_text(
            "from backend.budget import BudgetTracker\n"
        )

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "budget" not in modules


def test_covered_by_import_backend_dot_mod():
    """'import backend.<mod>' in a test file removes the module from gaps."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "mymod.py").write_text("# module\n")
        (tests_dir / "test_other.py").write_text("import backend.mymod as m\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "mymod" not in modules


def test_genuinely_untested_module_still_flagged():
    """A module with no test file and no import references IS still flagged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "orphan_mod.py").write_text("# truly untested\n")
        # Test file that imports something else, not orphan_mod
        (tests_dir / "test_other.py").write_text("from backend.other import Foo\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "orphan_mod" in modules


# ---------------------------------------------------------------------------
# rank_next_work — determinism
# ---------------------------------------------------------------------------


def test_ranking_is_deterministic():
    """Same input produces identical output on repeated calls."""
    discussions = [OPEN_DISCUSSING, OPEN_SPEC_READY]

    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()
        (backend_dir / "alpha.py").write_text("")
        (backend_dir / "beta.py").write_text("")

        kwargs = dict(
            discussions=discussions,
            health_checks=[_fake_check_fail],
            backend_dir=backend_dir,
            tests_dir=tests_dir,
        )
        result1 = rank_next_work(**kwargs)
        result2 = rank_next_work(**kwargs)

    assert result1 == result2


def test_category_ordering():
    """Categories appear in the order: stale_discussion > health_red > coverage_gap."""
    discussions = [OPEN_DISCUSSING]

    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()
        (backend_dir / "orphan.py").write_text("")

        result = rank_next_work(
            discussions=discussions,
            health_checks=[_fake_check_fail],
            backend_dir=backend_dir,
            tests_dir=tests_dir,
        )

    categories = [r["category"] for r in result]
    assert "stale_discussion" in categories
    assert "health_red" in categories
    assert "coverage_gap" in categories

    stale_idx = categories.index("stale_discussion")
    health_idx = categories.index("health_red")
    gap_idx = categories.index("coverage_gap")
    assert stale_idx < health_idx < gap_idx


# ---------------------------------------------------------------------------
# --json flag
# ---------------------------------------------------------------------------


def test_json_flag_emits_valid_json_array():
    """--json flag produces valid JSON array output."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, "backend/next_work.py", "--json"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=30,
    )
    # Script should exit 0
    assert result.returncode == 0, f"stderr: {result.stderr[:300]}"
    # Output must be valid JSON
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, list)


def test_human_output_no_crash():
    """_human_output renders without errors for various inputs."""
    items = [
        {"category": "stale_discussion", "number": 1, "title": "Test", "status": "DISCUSSING",
         "age_days": 5.0, "reason": "open DISCUSSING, created 5d ago"},
        {"category": "health_red", "name": "check_foo", "detail": "broken", "reason": "health check failed: broken"},
        {"category": "coverage_gap", "module": "my_module", "reason": "no test file found"},
    ]
    out = _human_output(items)
    assert "stale" in out.lower() or "Stale" in out
    assert "check_foo" in out
    assert "my_module" in out


def test_human_output_empty_items():
    """_human_output handles empty list without crash."""
    out = _human_output([])
    assert "no candidates" in out


# ---------------------------------------------------------------------------
# coverage_gaps — subpackage enumeration
# ---------------------------------------------------------------------------


def test_subpackage_module_detected_as_gap():
    """A module inside a subpackage with no test file is reported as a gap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        orch = backend_dir / "orchestrator"
        orch.mkdir()
        (orch / "__init__.py").write_text("")
        (orch / "dispatch.py").write_text("# dispatch module\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "orchestrator.dispatch" in modules


def test_subpackage_module_covered_by_test_file():
    """test_dispatch.py in tests/ clears orchestrator.dispatch from gaps."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        orch = backend_dir / "orchestrator"
        orch.mkdir()
        (orch / "__init__.py").write_text("")
        (orch / "dispatch.py").write_text("# dispatch module\n")
        (tests_dir / "test_dispatch.py").write_text("# tests for dispatch\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "orchestrator.dispatch" not in modules


def test_subpackage_module_covered_by_import():
    """'from backend.orchestrator.dispatch import X' clears it from gaps."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        orch = backend_dir / "orchestrator"
        orch.mkdir()
        (orch / "__init__.py").write_text("")
        (orch / "dispatch.py").write_text("# dispatch module\n")
        (tests_dir / "test_hook_runner.py").write_text(
            "from backend.orchestrator.dispatch import Dispatcher\n"
        )

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "orchestrator.dispatch" not in modules


def test_subpackage_init_not_in_gaps():
    """__init__.py in subpackages is excluded from enumeration."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        orch = backend_dir / "orchestrator"
        orch.mkdir()
        (orch / "__init__.py").write_text("")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "orchestrator.__init__" not in modules
        assert "__init__" not in modules


def test_top_level_modules_still_detected():
    """Top-level backend/*.py modules are still enumerated after the recursive change."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "top_level.py").write_text("# top-level\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert "top_level" in modules


def test_tests_dir_not_enumerated_as_modules():
    """Files inside backend/tests/ are never listed as modules needing coverage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (tests_dir / "conftest.py").write_text("")
        (tests_dir / "helpers.py").write_text("")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        modules = [g["module"] for g in gaps]
        assert not any("tests" in m for m in modules)


def test_coverage_gap_module_field_uses_dotted_path():
    """The module field uses dotted path for subpackage modules; reason shows the file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend_dir = Path(tmpdir) / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        sub = backend_dir / "sub"
        sub.mkdir()
        (sub / "__init__.py").write_text("")
        (sub / "worker.py").write_text("# worker\n")

        gaps = coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir)
        gap = next((g for g in gaps if "worker" in g["module"]), None)
        assert gap is not None
        assert gap["module"] == "sub.worker"
        assert "backend/sub/worker.py" in gap["reason"]


# ---------------------------------------------------------------------------
# coverage_gaps — git-tracked filtering
# ---------------------------------------------------------------------------


def test_untracked_module_not_in_gaps():
    """An untracked file in the same dir as a tracked file is NOT reported as a gap.

    Uses the git_tracked injectable to simulate git ls-files output without
    touching the real repo's index.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        backend_dir = tmpdir / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        # Two files exist on disk
        tracked_file = backend_dir / "real_module.py"
        tracked_file.write_text("# real module\n")

        untracked_file = backend_dir / "registry_test_copy.py"
        untracked_file.write_text("# junk copy — not in git\n")

        # Simulate: only real_module.py is tracked (paths relative to repo root = tmpdir)
        # Since tmpdir is the "repo root" here, paths are relative to it.
        tracked_set = frozenset(["backend/real_module.py"])

        gaps = coverage_gaps(
            backend_dir=backend_dir,
            tests_dir=tests_dir,
            git_tracked=tracked_set,
        )
        modules = [g["module"] for g in gaps]

        # Untracked junk does NOT appear
        assert "registry_test_copy" not in modules, (
            "Untracked file registry_test_copy.py should be excluded from coverage gaps"
        )
        # Tracked module appears as a gap (no test file exists)
        assert "real_module" in modules, (
            "Tracked file real_module.py should appear as a coverage gap (no test exists)"
        )


def test_tracked_module_covered_by_test_with_git_filter():
    """A tracked module with a corresponding tracked test file is NOT a gap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        backend_dir = tmpdir / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "real_module.py").write_text("# real module\n")
        (tests_dir / "test_real_module.py").write_text("# tests\n")

        tracked_set = frozenset([
            "backend/real_module.py",
            "backend/tests/test_real_module.py",
        ])

        gaps = coverage_gaps(
            backend_dir=backend_dir,
            tests_dir=tests_dir,
            git_tracked=tracked_set,
        )
        modules = [g["module"] for g in gaps]
        assert "real_module" not in modules


def test_git_filter_none_falls_back_to_walk_all():
    """git_tracked=None disables filtering — all walked files are considered."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        backend_dir = tmpdir / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "would_be_untracked.py").write_text("# not in git\n")

        # With git_tracked=None, no filtering — file appears in gaps
        gaps = coverage_gaps(
            backend_dir=backend_dir,
            tests_dir=tests_dir,
            git_tracked=None,
        )
        modules = [g["module"] for g in gaps]
        assert "would_be_untracked" in modules


def test_untracked_test_file_not_used_for_coverage():
    """An untracked test file should not grant coverage to any module."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        backend_dir = tmpdir / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "real_module.py").write_text("# real module\n")
        # test file exists on disk but is NOT tracked
        (tests_dir / "test_real_module.py").write_text("# untracked test\n")

        # Only the module is tracked, not the test file
        tracked_set = frozenset(["backend/real_module.py"])

        gaps = coverage_gaps(
            backend_dir=backend_dir,
            tests_dir=tests_dir,
            git_tracked=tracked_set,
        )
        modules = [g["module"] for g in gaps]
        # real_module is still a gap — the untracked test doesn't count
        assert "real_module" in modules


def test_git_tracked_false_same_as_none():
    """git_tracked=False also disables filtering."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        backend_dir = tmpdir / "backend"
        backend_dir.mkdir()
        tests_dir = backend_dir / "tests"
        tests_dir.mkdir()

        (backend_dir / "any_module.py").write_text("# any module\n")

        gaps = coverage_gaps(
            backend_dir=backend_dir,
            tests_dir=tests_dir,
            git_tracked=False,
        )
        modules = [g["module"] for g in gaps]
        assert "any_module" in modules


def test_get_git_tracked_files_returns_frozenset_or_none():
    """_get_git_tracked_files returns a frozenset on a real git repo or None."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    result = _get_git_tracked_files(repo_root)
    # In a real git repo this should work
    if result is not None:
        assert isinstance(result, frozenset)
        # next_work.py itself should be tracked
        assert "backend/next_work.py" in result
    # None is also acceptable (git unavailable in some CI envs)


def test_get_git_tracked_files_non_repo_returns_none():
    """_get_git_tracked_files returns None when run outside a git repo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _get_git_tracked_files(Path(tmpdir))
        assert result is None


# ---------------------------------------------------------------------------
# history_signal — unit tests
# ---------------------------------------------------------------------------


def _make_run(verdict: str, input_tok: int = 10000) -> dict:
    """Build a minimal agent_run dict for history_signal tests."""
    return {"verdict": verdict, "input_tok": input_tok}


def test_history_signal_empty_runs_is_neutral():
    """Empty run list → 0.0 (neutral, no penalty)."""
    assert history_signal([]) == 0.0


def test_history_signal_all_pass_is_neutral():
    """All passing runs produce no demotion."""
    runs = [_make_run("done"), _make_run("pass"), _make_run("done")]
    assert history_signal(runs) == 0.0


def test_history_signal_high_failure_rate_penalizes():
    """≥50% failure rate produces a positive penalty."""
    runs = [_make_run("fail"), _make_run("fail"), _make_run("done")]
    score = history_signal(runs)
    assert score > 0.0, "high failure rate should produce a non-zero penalty"


def test_history_signal_below_threshold_no_penalty():
    """Failure rate just below threshold (1 fail out of 3) is treated as neutral."""
    runs = [_make_run("fail"), _make_run("done"), _make_run("done")]
    score = history_signal(runs)
    # 1/3 ≈ 0.33 < 0.5 threshold → no failure penalty
    assert score == 0.0


def test_history_signal_cost_spike_penalizes():
    """A run with tokens ≥3× median triggers the cost-spike penalty."""
    # median is 10000, spike is 35000 (≥3× 10000)
    runs = [
        _make_run("done", input_tok=10000),
        _make_run("done", input_tok=10000),
        _make_run("done", input_tok=35000),
    ]
    score = history_signal(runs)
    assert score > 0.0, "cost spike should produce a non-zero penalty"


def test_history_signal_capped_at_one():
    """Combined failure + spike penalties are capped at 1.0."""
    runs = [_make_run("fail", input_tok=100000) for _ in range(5)]
    score = history_signal(runs)
    assert score <= 1.0


def test_history_signal_none_fields_do_not_crash():
    """Runs with None/missing verdict and input_tok fields are handled gracefully."""
    runs = [
        {"verdict": None, "input_tok": None},
        {"verdict": "done"},
        {},
    ]
    score = history_signal(runs)
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# rank_next_work — history signal integration
# ---------------------------------------------------------------------------


def test_high_failure_role_demoted_within_stale_group():
    """A discussion with a high executor failure rate is sorted after clean ones."""
    older = {
        "number": 100,
        "title": "Older clean discussion",
        "status": "SPEC_READY",
        "created_at": "2024-01-01T00:00:00Z",
        "closed_at": None,
    }
    newer_but_failing = {
        "number": 200,
        "title": "Newer but failing discussion",
        "status": "SPEC_READY",
        "created_at": "2024-06-01T00:00:00Z",
        "closed_at": None,
    }

    # older has no failures; newer_but_failing has 100% fail rate
    run_history = {
        100: [_make_run("done"), _make_run("done")],
        200: [_make_run("fail"), _make_run("fail"), _make_run("fail")],
    }

    result = rank_next_work(
        discussions=[older, newer_but_failing],
        health_checks=[],
        backend_dir=Path("/tmp"),
        tests_dir=Path("/tmp"),
        run_history=run_history,
    )

    stale = [r for r in result if r["category"] == "stale_discussion"]
    nums = [r["number"] for r in stale]
    assert len(nums) == 2
    # older (clean) should appear before newer_but_failing (chronically failing)
    assert nums.index(100) < nums.index(200), (
        f"clean discussion should rank before failing one; got order {nums}"
    )


def test_clean_role_unaffected_by_history():
    """A discussion with all-pass runs has history_penalty == 0.0."""
    disc = {
        "number": 300,
        "title": "Clean discussion",
        "status": "DISCUSSING",
        "created_at": "2025-01-01T00:00:00Z",
        "closed_at": None,
    }
    run_history = {300: [_make_run("done"), _make_run("pass")]}

    result = rank_next_work(
        discussions=[disc],
        health_checks=[],
        backend_dir=Path("/tmp"),
        tests_dir=Path("/tmp"),
        run_history=run_history,
    )

    stale = [r for r in result if r["category"] == "stale_discussion"]
    assert len(stale) == 1
    assert stale[0]["history_penalty"] == 0.0


def test_empty_run_history_dict_neutral_no_crash():
    """run_history={} (empty agent_run simulation) → neutral, no crash."""
    disc = {
        "number": 400,
        "title": "Discussion with no run history",
        "status": "SPEC_READY",
        "created_at": "2025-03-01T00:00:00Z",
        "closed_at": None,
    }

    result = rank_next_work(
        discussions=[disc],
        health_checks=[],
        backend_dir=Path("/tmp"),
        tests_dir=Path("/tmp"),
        run_history={},
    )

    stale = [r for r in result if r["category"] == "stale_discussion"]
    assert len(stale) == 1
    assert stale[0]["history_penalty"] == 0.0


def test_rank_next_work_history_penalty_field_present():
    """All stale_discussion items carry a history_penalty field."""
    discs = [OPEN_DISCUSSING, OPEN_SPEC_READY]
    result = rank_next_work(
        discussions=discs,
        health_checks=[],
        backend_dir=Path("/tmp"),
        tests_dir=Path("/tmp"),
        run_history={},
    )

    for item in result:
        if item["category"] == "stale_discussion":
            assert "history_penalty" in item, (
                f"stale_discussion item missing history_penalty: {item}"
            )


def test_history_signal_weight_constant_is_positive():
    """HISTORY_SIGNAL_WEIGHT is a positive float — guards against accidental zeroing."""
    assert isinstance(HISTORY_SIGNAL_WEIGHT, float)
    assert HISTORY_SIGNAL_WEIGHT > 0.0
