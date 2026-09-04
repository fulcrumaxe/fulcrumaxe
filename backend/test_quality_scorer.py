"""
Tests for backend/quality_scorer.py

Covers:
1. Complexity score — high complexity penalised
2. Complexity score — no Python files → full marks
3. Test coverage score — modules with tests
4. Test coverage score — no test files → 0 pts
5. Test coverage score — no modules changed → full marks
6. Review rounds score — 0 rounds → 25 pts
7. Review rounds score — multiple rounds → reduced score
8. Size score — small PR
9. Size score — large PR
10. Grade mapping
11. score_diff with empty diff
12. score_diff with non-Python files only
13. history() and stats()
14. measured flag + renormalised total when review_rounds is unmeasured
15. Deletion-only diff — no scorable files, applicable=False

See backend/tests/test_quality_scorer_matching.py for the anchored
test-index matching rule: the PR #1865 replay, the synthetic case table,
the corpus discrimination band, and the app.py / test_apply.py anchor
regression.
"""

from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Use a temp blackboard so tests don't pollute .autonomous-team
from backend.blackboard import Blackboard
from backend.quality_scorer import QualityScorer, _to_grade, _parse_diff, _count_diff_lines


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_bb(tmp_path: Path) -> Blackboard:
    """Return an isolated Blackboard rooted under tmp_path."""
    return Blackboard(root=tmp_path / "blackboard")


@pytest.fixture()
def scorer(tmp_bb: Blackboard) -> QualityScorer:
    """Return a QualityScorer backed by a temporary blackboard."""
    s = QualityScorer()
    s._bb = tmp_bb
    return s


# ---------------------------------------------------------------------------
# 1. Complexity score — high complexity code
# ---------------------------------------------------------------------------


def test_complexity_score_high_complexity(scorer: QualityScorer) -> None:
    """High-complexity Python file should score below max."""
    # Build a diff with a deeply nested function
    source_lines = textwrap.dedent("""\
        def foo(x):
            if x > 0:
                for i in range(x):
                    while i > 0:
                        try:
                            if i % 2 == 0:
                                pass
                        except Exception:
                            pass
                        i -= 1
    """).splitlines()
    added = "\n".join(f"+{line}" for line in source_lines)
    diff = f"diff --git a/backend/foo.py b/backend/foo.py\n+++ b/backend/foo.py\n{added}\n"
    files = _parse_diff(diff)
    # Patch file reading so it uses added lines (no real file)
    with patch.object(Path, "exists", return_value=False):
        result = scorer._complexity_score(files)
    assert result["score"] < 30
    assert result["max"] == 30


# ---------------------------------------------------------------------------
# 2. Complexity score — no Python files → full marks
# ---------------------------------------------------------------------------


def test_complexity_score_no_python_files(scorer: QualityScorer) -> None:
    diff = "diff --git a/README.md b/README.md\n+++ b/README.md\n+Some text\n"
    files = _parse_diff(diff)
    result = scorer._complexity_score(files)
    assert result["score"] == 30
    assert "no Python files" in result["detail"]


# ---------------------------------------------------------------------------
# 3. Test coverage score — module with matching test
# ---------------------------------------------------------------------------


def test_coverage_score_with_tests(scorer: QualityScorer) -> None:
    # Test index is repo-wide (per git ls-files), not diff-only — inject a
    # synthetic index so this stays isolated from the real repo's state.
    scorer._test_index = ["backend/foo.py", "backend/test_foo.py"]
    files = {
        "backend/foo.py": ["+def foo(): pass"],
    }
    result = scorer._test_coverage_score(files)
    assert result["score"] == 25
    assert "1/1" in result["detail"]
    assert result["covered_modules"] == ["backend/foo.py"]


# ---------------------------------------------------------------------------
# 4. Test coverage score — no test files → 0 pts
# ---------------------------------------------------------------------------


def test_coverage_score_no_tests(scorer: QualityScorer) -> None:
    scorer._test_index = ["backend/foo.py", "backend/bar.py"]
    files = {
        "backend/foo.py": ["+def foo(): pass"],
        "backend/bar.py": ["+def bar(): pass"],
    }
    result = scorer._test_coverage_score(files)
    assert result["score"] == 0
    assert "0/2" in result["detail"]
    assert result["covered_modules"] == []


def test_coverage_score_unmeasured_when_index_build_fails(scorer: QualityScorer) -> None:
    """git ls-files failing must report measured=False, not score everything 0."""
    scorer._test_index = None
    files = {"backend/foo.py": ["+def foo(): pass"]}
    result = scorer._test_coverage_score(files)
    assert result["measured"] is False
    assert result["score"] == 25  # excluded from total, not penalised as 0


# ---------------------------------------------------------------------------
# 5. Test coverage score — no modules changed → full marks
# ---------------------------------------------------------------------------


def test_coverage_score_no_modules(scorer: QualityScorer) -> None:
    files = {
        "README.md": ["+some docs"],
    }
    result = scorer._test_coverage_score(files)
    assert result["score"] == 25
    assert "no modules" in result["detail"]


# ---------------------------------------------------------------------------
# 6. Review rounds score — 0 rounds → 25 pts
# ---------------------------------------------------------------------------


def test_review_rounds_zero(scorer: QualityScorer) -> None:
    # No blackboard entries anywhere in agent_output/ — this dimension has no
    # real signal to read, so it must report measured=False rather than
    # silently claiming "0 needs-fix rounds" it never actually checked.
    result = scorer._review_rounds_score(pr_number=999)
    assert result["score"] == 25
    assert result["measured"] is False
    assert "unmeasured" in result["detail"]


def test_review_rounds_no_pr_number(scorer: QualityScorer) -> None:
    result = scorer._review_rounds_score(pr_number=None)
    assert result["measured"] is False
    assert "unmeasured" in result["detail"]


# ---------------------------------------------------------------------------
# 7. Review rounds score — multiple rounds → reduced score
# ---------------------------------------------------------------------------


def test_review_rounds_multiple(scorer: QualityScorer, tmp_bb: Blackboard) -> None:
    # Write two needs-fix entries for PR 42 — the namespace is non-empty, so
    # this dimension is measurable (even though these entries happen to be
    # the unit test's own fixtures; they're the only writer of this
    # namespace anywhere in the tree, per D#1866, and are kept deliberately).
    tmp_bb.write("agent_output/review-1", {"pr": 42, "verdict": "needs-fix"}, updated_by="test")
    tmp_bb.write("agent_output/review-2", {"pr": 42, "verdict": "needs-fix"}, updated_by="test")
    scorer._bb = tmp_bb
    result = scorer._review_rounds_score(pr_number=42)
    assert result["score"] == 15  # 2 rounds → 15 pts
    assert result["measured"] is True
    assert "2 needs-fix" in result["detail"]


# ---------------------------------------------------------------------------
# 8. Size score — small PR (<100 lines)
# ---------------------------------------------------------------------------


def test_size_score_small(scorer: QualityScorer) -> None:
    result = scorer._size_score(50)
    assert result["score"] == 20


# ---------------------------------------------------------------------------
# 9. Size score — large PR (500+ lines)
# ---------------------------------------------------------------------------


def test_size_score_large(scorer: QualityScorer) -> None:
    result = scorer._size_score(600)
    assert result["score"] == 5


# ---------------------------------------------------------------------------
# 10. Grade mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("score,expected_grade", [
    (100, "A+"),
    (95,  "A+"),
    (90,  "A"),
    (85,  "B+"),
    (80,  "B"),
    (75,  "C+"),
    (70,  "C"),
    (60,  "D"),
    (59,  "F"),
    (0,   "F"),
])
def test_grade_mapping(score: int, expected_grade: str) -> None:
    assert _to_grade(score) == expected_grade


# ---------------------------------------------------------------------------
# 11. score_diff with empty diff
# ---------------------------------------------------------------------------


def test_score_diff_empty(scorer: QualityScorer) -> None:
    """An empty diff touches no files at all, so — same principle as a
    deletion-only diff — there is nothing scorable and the gate is skipped
    entirely rather than awarding a score."""
    result = scorer.score_diff("", pr_number=None)
    assert "total_score" in result
    assert "grade" in result
    assert result["applicable"] is False
    assert result["total_score"] is None
    assert result["grade"] == "N/A"


# ---------------------------------------------------------------------------
# 12. score_diff with non-Python files only
# ---------------------------------------------------------------------------


def test_score_diff_non_python(scorer: QualityScorer) -> None:
    diff = textwrap.dedent("""\
        diff --git a/README.md b/README.md
        +++ b/README.md
        +Some new docs
        diff --git a/tui/src/App.tsx b/tui/src/App.tsx
        +++ b/tui/src/App.tsx
        +const x = 1;
    """)
    result = scorer.score_diff(diff, pr_number=None)
    assert result["breakdown"]["complexity"]["score"] == 30
    assert result["breakdown"]["test_coverage"]["score"] == 25
    # 2 lines changed → size score 20
    assert result["breakdown"]["size"]["score"] == 20


# ---------------------------------------------------------------------------
# 14. measured flag + renormalised total when review_rounds is unmeasured
# ---------------------------------------------------------------------------


def test_measured_flag_present_on_normal_python_diff(scorer: QualityScorer) -> None:
    """complexity, test_coverage and size report measured=True on a normal diff."""
    diff = textwrap.dedent("""\
        diff --git a/backend/foo.py b/backend/foo.py
        +++ b/backend/foo.py
        +def foo(): pass
    """)
    files = _parse_diff(diff)
    scorer._test_index = ["backend/foo.py"]
    assert scorer._complexity_score(files)["measured"] is True
    assert scorer._test_coverage_score(files)["measured"] is True
    assert scorer._size_score(1)["measured"] is True


def test_score_diff_renormalizes_when_review_rounds_unmeasured(scorer: QualityScorer) -> None:
    """With review_rounds unmeasured (empty agent_output/ namespace), the
    total renormalises over the measured maxima only: complexity 30 +
    test_coverage 25 + size 20 = 75 becomes 100, not 75."""
    # A trivial module (0 functions → complexity full marks) with its test
    # already covered in the injected index, so complexity and test_coverage
    # are both full marks and this diff is unambiguously applicable.
    scorer._test_index = ["backend/const_only.py", "backend/test_const_only.py"]
    diff = "diff --git a/backend/const_only.py b/backend/const_only.py\n+++ b/backend/const_only.py\n+X = 1\n"
    result = scorer.score_diff(diff, pr_number=55)
    assert result["applicable"] is True
    assert result["breakdown"]["review_rounds"]["measured"] is False
    assert result["breakdown"]["complexity"]["measured"] is True
    assert result["breakdown"]["complexity"]["score"] == 30
    assert result["breakdown"]["test_coverage"]["measured"] is True
    assert result["breakdown"]["test_coverage"]["score"] == 25
    assert result["breakdown"]["size"]["measured"] is True
    assert result["total_score"] == 100


# ---------------------------------------------------------------------------
# 15. Deletion-only diff — no scorable files, applicable=False
# ---------------------------------------------------------------------------


def test_deletion_only_diff_not_applicable(scorer: QualityScorer) -> None:
    """A pure deletion emits `+++ /dev/null`, which _parse_diff never
    records, so the diff has no scorable files at all."""
    diff = textwrap.dedent("""\
        diff --git a/backend/old_module.py b/backend/old_module.py
        deleted file mode 100644
        index abc123..0000000
        --- a/backend/old_module.py
        +++ /dev/null
        @@ -1,3 +0,0 @@
        -def old(): pass
    """)
    result = scorer.score_diff(diff, pr_number=None)
    assert result["applicable"] is False
    assert result["total_score"] is None


# ---------------------------------------------------------------------------
# 13. history() and stats()
# ---------------------------------------------------------------------------


def test_history_and_stats(scorer: QualityScorer) -> None:
    # Write two scored PRs directly to blackboard
    scorer._bb.write("quality/10", {
        "pr": 10, "discussion": None, "timestamp": "2026-04-10T10:00:00+00:00",
        "total_score": 80, "grade": "B",
        "breakdown": {
            "complexity": {"score": 25, "max": 30, "detail": ""},
            "test_coverage": {"score": 20, "max": 25, "detail": ""},
            "review_rounds": {"score": 20, "max": 25, "detail": ""},
            "size": {"score": 15, "max": 20, "detail": ""},
        },
        "files_changed": [],
    }, updated_by="test")
    scorer._bb.write("quality/11", {
        "pr": 11, "discussion": None, "timestamp": "2026-04-10T11:00:00+00:00",
        "total_score": 60, "grade": "D",
        "breakdown": {
            "complexity": {"score": 15, "max": 30, "detail": ""},
            "test_coverage": {"score": 10, "max": 25, "detail": ""},
            "review_rounds": {"score": 20, "max": 25, "detail": ""},
            "size": {"score": 15, "max": 20, "detail": ""},
        },
        "files_changed": [],
    }, updated_by="test")

    history = scorer.history()
    assert len(history) == 2
    assert history[0]["pr"] == 11  # most recent first

    stats = scorer.stats()
    assert stats["total_scored"] == 2
    assert stats["avg_total"] == 70.0
    assert stats["grade_distribution"]["B"] == 1
    assert stats["grade_distribution"]["D"] == 1
