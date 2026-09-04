"""
Tests for the test_coverage anchored-matching rule in backend/quality_scorer.py.

D#1866 measured that `_test_coverage_score`'s old same-directory-plus-
present-in-this-diff rule matched only 4.5% of tracked modules (17/381),
scoring 0/25 on almost every real PR that touched a module. The fixed rule
credits a module when a *tracked* test file exists anywhere in the repo
(via `git ls-files`, never a filesystem walk) whose basename is
boundary-anchored to the module's normalised stem.

Covers:
1. PR #1865's real 19-file list replays to test_coverage 25/25, 6/6 modules,
   with each of the six modules individually shown as matched.
2. Synthetic cases B, C, D, I (module + test in different naming/dir
   combinations) score 25/25; cases E, F (no matching test anywhere) stay 0/25.
3. A corpus test over all tracked non-test .py modules (via git ls-files,
   excluding archive/) lands the credited fraction strictly between 0.20
   and 0.90 — the measured value is 217/381 (56%).
4. The match is boundary-anchored: dashboard_tui/app.py is NOT credited by
   scripts/engine-sync/tests/test_apply.py, the one real over-credit an
   unanchored prefix match produces.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.quality_scorer import QualityScorer


@pytest.fixture()
def scorer() -> QualityScorer:
    """A QualityScorer with its real, repo-wide test index (from git ls-files)."""
    s = QualityScorer()
    assert s._test_index is not None, "git ls-files must succeed for these tests"
    return s


@pytest.fixture()
def synthetic_scorer() -> QualityScorer:
    """A QualityScorer whose test index is overridden per-case by the test."""
    return QualityScorer()


# ---------------------------------------------------------------------------
# 1. PR #1865 replay — real 19-file list, 6 modules, all individually matched
# ---------------------------------------------------------------------------

# The exact file list from `gh pr view 1865 --json files`.
_PR_1865_FILES = [
    ".gitignore",
    "CLAUDE.md",
    "backend/hooks/wrong_premise_guard.py",
    "backend/routers/rpc.py",
    "backend/server.py",
    "backend/tests/test_trigger.py",
    "backend/transcript_reader.py",
    "backend/trigger.py",
    "requirements.txt",
    "scripts/codebase-index.py",
    "scripts/post-agent-hook.sh",
    "scripts/pre-spawn-check.sh",
    "scripts/preflight-full.sh",
    "scripts/process-watchdog.sh",
    "tests/test_codebase_index.py",
    "tests/test_preflight_full.sh",
    "tests/test_process_watchdog_patterns.py",
    "tui/package.json",
    "tui/src/index.tsx",
]

_PR_1865_MODULES = [
    "backend/hooks/wrong_premise_guard.py",
    "backend/routers/rpc.py",
    "backend/server.py",
    "backend/transcript_reader.py",
    "backend/trigger.py",
    "scripts/codebase-index.py",
]


def test_pr_1865_replay_scores_full_marks(scorer: QualityScorer) -> None:
    """Baseline (old same-dir + in-diff rule) scored 0/25, '0/6 modules
    covered' — every one of these six tests lives in a different directory
    than its module. The fixed rule finds them anywhere in the tracked repo."""
    files = {f: [] for f in _PR_1865_FILES}
    result = scorer._test_coverage_score(files)
    assert result["score"] == 25
    assert result["detail"] == "6/6 modules covered"
    assert sorted(result["covered_modules"]) == sorted(_PR_1865_MODULES)


# ---------------------------------------------------------------------------
# 2. Synthetic case table (from the frozen Spec's evidence table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case,module,test_index",
    [
        # B: module + test, same dir, hyphen in module name normalised
        ("B", "scripts/codebase-index.py", ["scripts/test_codebase_index.py"]),
        # C: module + test, different dirs, hyphen normalised
        ("C", "scripts/codebase-index.py", ["tests/test_codebase_index.py"]),
        # D: module + test, different dirs, no hyphen at all
        ("D", "backend/server.py", ["tests/test_server.py"]),
        # I: module + test, different dirs (repo's dominant layout)
        ("I", "backend/server.py", ["backend/tests/test_server.py"]),
    ],
)
def test_synthetic_cases_score_full_marks(
    synthetic_scorer: QualityScorer, case: str, module: str, test_index: list[str]
) -> None:
    synthetic_scorer._test_index = test_index
    result = synthetic_scorer._test_coverage_score({module: []})
    assert result["score"] == 25, f"case {case} expected 25/25"
    assert result["detail"] == "1/1 modules covered"


@pytest.mark.parametrize(
    "case,module,test_index",
    [
        # E: module with no matching test anywhere in the repo
        ("E", "backend/genuinely_untested.py", ["backend/tests/test_unrelated.py"]),
        # F: hyphenated module, still no matching test anywhere
        ("F", "scripts/another-untested-module.py", ["tests/test_something_else.py"]),
    ],
)
def test_synthetic_cases_stay_zero(
    synthetic_scorer: QualityScorer, case: str, module: str, test_index: list[str]
) -> None:
    """Both directions matter: a rule that only moves zeros up (and never
    keeps a genuinely untested module at 0) is the same defect, sign-flipped."""
    synthetic_scorer._test_index = test_index
    result = synthetic_scorer._test_coverage_score({module: []})
    assert result["score"] == 0, f"case {case} expected 0/25"
    assert result["detail"] == "0/1 modules covered"
    assert result["covered_modules"] == []


# ---------------------------------------------------------------------------
# 3. Corpus discrimination band — credited fraction strictly in (0.20, 0.90)
# ---------------------------------------------------------------------------


def test_corpus_credited_fraction_in_measured_band(scorer: QualityScorer) -> None:
    """Walks git ls-files (never a filesystem walk — an untracked scratch
    file must not be able to manufacture coverage), excludes archive/, and
    treats every tracked non-test .py module as though it were the sole
    change in a diff. Measured value for the anchored rule is 217/381 (56%)."""
    modules = [
        f for f in scorer._test_index
        if f.endswith(".py")
        and not Path(f).name.startswith("test_")
        and not Path(f).name.endswith("_test.py")
    ]
    assert modules, "expected a non-empty corpus of tracked non-test .py modules"

    files = {m: [] for m in modules}
    result = scorer._test_coverage_score(files)

    match = re.match(r"(\d+)/(\d+) modules covered", result["detail"])
    assert match is not None
    covered, total = int(match.group(1)), int(match.group(2))
    assert total == len(modules)

    fraction = covered / total
    assert 0.20 < fraction < 0.90, (
        f"credited fraction {covered}/{total} ({fraction:.2%}) outside the "
        "measured band (0.20, 0.90) — either not discriminating or over-crediting"
    )


# ---------------------------------------------------------------------------
# 4. Anchor regression — test_apply.py must not falsely credit app.py
# ---------------------------------------------------------------------------


def test_anchor_rejects_unanchored_prefix_match(synthetic_scorer: QualityScorer) -> None:
    """dashboard_tui/app.py (stem 'app') must NOT be credited by
    scripts/engine-sync/tests/test_apply.py (stem 'test_apply') — an
    unanchored `startswith("test_app")` check would wrongly match here
    because 'test_apply' starts with 'test_app'."""
    synthetic_scorer._test_index = ["scripts/engine-sync/tests/test_apply.py"]
    result = synthetic_scorer._test_coverage_score({"dashboard_tui/app.py": []})
    assert result["score"] == 0
    assert result["detail"] == "0/1 modules covered"
    assert result["covered_modules"] == []


def test_anchor_still_matches_real_repo_files(scorer: QualityScorer) -> None:
    """Sanity check against the real tracked repo: both files this
    regression depends on actually exist, and the real index still does not
    cross-credit them."""
    assert "dashboard_tui/app.py" in scorer._test_index
    assert "scripts/engine-sync/tests/test_apply.py" in scorer._test_index
    result = scorer._test_coverage_score({"dashboard_tui/app.py": []})
    assert result["covered_modules"] == []
