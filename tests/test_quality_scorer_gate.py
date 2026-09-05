"""
Unit tests for the three skip paths in the quality gate logic embedded in
scripts/team-lead-iteration.sh.

Rather than shelling out to the 800-line script (slow, needs GitHub auth), we
replicate the gate logic as a pure Python function that mirrors the bash
decision tree exactly. That keeps the tests fast and self-contained.

The three skip conditions under test:
  1. Scorer returned .error → skip entirely (no label flip, no comment)
  2. total_score == 0 AND complexity.detail == "no Python files changed" → skip
  3. too_many_review_rounds is the only failing dimension → skip
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Pure-Python replica of the bash quality-gate decision logic
# ---------------------------------------------------------------------------

def _quality_gate_decision(score_json: dict) -> dict:
    """
    Return a dict describing what the quality gate would do for a given
    score_json payload.

    Keys:
      action: "skip_no_content" | "skip_rounds_only" | "flag" | "pass"
      log_reason: short string used in log output
      failing_dims: list[str]  (executor-visible, rounds excluded)
      comment_detail: str (the per-dimension block)
    """
    has_error = "error" in score_json
    score = score_json.get("total_score", 0)

    # Guard 1: error or zero-score with no Python files
    no_python_scored = False
    if score == 0:
        complexity_detail = (
            score_json.get("breakdown", {})
            .get("complexity", {})
            .get("detail", "")
        )
        if "no Python files" in complexity_detail:
            no_python_scored = True

    if has_error or no_python_scored:
        return {
            "action": "skip_no_content",
            "log_reason": "skipping quality gate (no scorable content)",
            "failing_dims": [],
            "comment_detail": "",
        }

    # Guard 2: score below threshold
    if score >= 60:
        return {
            "action": "pass",
            "log_reason": "score above threshold",
            "failing_dims": [],
            "comment_detail": "",
        }

    bd = score_json.get("breakdown", {})

    # Build failing dimensions — executor-visible only (exclude review_rounds)
    failing_dims: list[str] = []
    if (bd.get("complexity", {}).get("score", 30)) < 20:
        failing_dims.append("complexity")
    if (bd.get("test_coverage", {}).get("score", 25)) < 15:
        failing_dims.append("test_coverage")
    if (bd.get("size", {}).get("score", 20)) < 10:
        failing_dims.append("size")

    # Guard 3: if no executor-actionable dims but review_rounds is low → skip
    if not failing_dims:
        rounds_score = bd.get("review_rounds", {}).get("score", 25)
        if rounds_score < 15:
            return {
                "action": "skip_rounds_only",
                "log_reason": "skipping quality gate (only too_many_review_rounds — not executor-actionable)",
                "failing_dims": [],
                "comment_detail": "",
            }

    # Build per-dimension detail block
    detail_lines: list[str] = []
    if "complexity" in failing_dims:
        s = bd["complexity"]["score"]
        d = bd["complexity"].get("detail", "see Python files")
        detail_lines.append(f"- **complexity** {s}/30: {d}")
    if "test_coverage" in failing_dims:
        s = bd["test_coverage"]["score"]
        d = bd["test_coverage"].get("detail", "check module coverage")
        detail_lines.append(f"- **test_coverage** {s}/25: {d}")
    if "size" in failing_dims:
        s = bd["size"]["score"]
        d = bd["size"].get("detail", "diff too large")
        detail_lines.append(f"- **size** {s}/20: {d}")

    return {
        "action": "flag",
        "log_reason": "flagging PR for fix round",
        "failing_dims": failing_dims,
        "comment_detail": "\n".join(detail_lines),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSkipOnError:
    """Gate skips when quality_scorer returns an error field."""

    def test_skip_when_error_present(self):
        payload = {
            "error": "quality_scorer unavailable",
            "total_score": 0,
            "breakdown": {},
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "skip_no_content"
        assert "no scorable content" in result["log_reason"]

    def test_skip_when_error_and_nonzero_score(self):
        # Shouldn't happen in practice but the error field takes precedence
        payload = {
            "error": "partial failure",
            "total_score": 43,
            "breakdown": {
                "complexity": {"score": 5, "max": 30, "detail": "avg_func_complexity=12.0"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "skip_no_content"


class TestSkipNoContentZeroScore:
    """Gate skips when total_score is 0 and complexity.detail says 'no Python files'."""

    def test_skip_markdown_only_pr(self):
        payload = {
            "total_score": 0,
            "grade": "F",
            "breakdown": {
                "complexity": {"score": 30, "max": 30, "detail": "no Python files changed"},
                "test_coverage": {"score": 25, "max": 25, "detail": "no modules changed"},
                "review_rounds": {"score": 25, "max": 25, "detail": "0 needs-fix round(s)"},
                "size": {"score": 0, "max": 20, "detail": "0 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "skip_no_content"
        assert "no scorable content" in result["log_reason"]

    def test_flag_when_zero_score_but_python_present(self):
        # Score is 0 but complexity detail does NOT say "no Python files"
        # (degenerate case — scorer ran on Python files and still gave 0)
        payload = {
            "total_score": 0,
            "grade": "F",
            "breakdown": {
                "complexity": {"score": 0, "max": 30, "detail": "avg_func_complexity=18.0 (4 functions)"},
                "test_coverage": {"score": 0, "max": 25, "detail": "0/2 modules covered"},
                "review_rounds": {"score": 5, "max": 25, "detail": "3 needs-fix round(s)"},
                "size": {"score": 0, "max": 20, "detail": "620 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        # Should NOT skip — real low score
        assert result["action"] == "flag"


class TestSkipOnlyReviewRounds:
    """Gate skips when too_many_review_rounds is the only failing dimension."""

    def test_skip_when_only_rounds_below_threshold(self):
        # Score < 60 but the only dim dragging it down is review_rounds
        payload = {
            "total_score": 55,
            "grade": "D",
            "breakdown": {
                "complexity": {"score": 30, "max": 30, "detail": "avg_func_complexity=3.2 (5 functions)"},
                "test_coverage": {"score": 25, "max": 25, "detail": "3/3 modules covered"},
                "review_rounds": {"score": 5, "max": 25, "detail": "3 needs-fix round(s)"},
                "size": {"score": 15, "max": 20, "detail": "180 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "skip_rounds_only"
        assert "too_many_review_rounds" in result["log_reason"]

    def test_no_skip_when_complexity_also_failing(self):
        # review_rounds is low AND complexity is failing — must flag
        payload = {
            "total_score": 30,
            "grade": "F",
            "breakdown": {
                "complexity": {"score": 5, "max": 30, "detail": "avg_func_complexity=14.2 (6 functions)"},
                "test_coverage": {"score": 25, "max": 25, "detail": "2/2 modules covered"},
                "review_rounds": {"score": 5, "max": 25, "detail": "3 needs-fix round(s)"},
                "size": {"score": 15, "max": 20, "detail": "200 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "flag"
        assert "complexity" in result["failing_dims"]
        assert "too_many_review_rounds" not in result["failing_dims"]


class TestActionableCommentDetail:
    """When the gate fires, the comment body includes metric values from breakdown.detail."""

    def test_complexity_detail_in_comment(self):
        payload = {
            "total_score": 25,
            "grade": "F",
            "breakdown": {
                "complexity": {"score": 5, "max": 30, "detail": "avg_func_complexity=16.0 (3 functions)"},
                "test_coverage": {"score": 20, "max": 25, "detail": "2/3 modules covered"},
                "review_rounds": {"score": 25, "max": 25, "detail": "0 needs-fix round(s)"},
                "size": {"score": 15, "max": 20, "detail": "195 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "flag"
        # Complexity score + detail must appear in the comment block
        assert "5/30" in result["comment_detail"]
        assert "avg_func_complexity=16.0" in result["comment_detail"]

    def test_test_coverage_detail_in_comment(self):
        payload = {
            "total_score": 28,
            "grade": "F",
            "breakdown": {
                "complexity": {"score": 28, "max": 30, "detail": "avg_func_complexity=4.1 (2 functions)"},
                "test_coverage": {"score": 0, "max": 25, "detail": "0/2 modules covered"},
                "review_rounds": {"score": 25, "max": 25, "detail": "0 needs-fix round(s)"},
                "size": {"score": 10, "max": 20, "detail": "310 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "flag"
        assert "0/2 modules covered" in result["comment_detail"]

    def test_too_many_review_rounds_not_in_failing_dims(self):
        payload = {
            "total_score": 35,
            "grade": "F",
            "breakdown": {
                "complexity": {"score": 5, "max": 30, "detail": "avg_func_complexity=13.0 (4 functions)"},
                "test_coverage": {"score": 25, "max": 25, "detail": "2/2 modules covered"},
                "review_rounds": {"score": 5, "max": 25, "detail": "3 needs-fix round(s)"},
                "size": {"score": 15, "max": 20, "detail": "180 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "flag"
        assert "too_many_review_rounds" not in result["failing_dims"]
        assert "complexity" in result["failing_dims"]

    def test_pass_above_threshold(self):
        payload = {
            "total_score": 75,
            "grade": "B",
            "breakdown": {
                "complexity": {"score": 25, "max": 30, "detail": "avg_func_complexity=4.5 (8 functions)"},
                "test_coverage": {"score": 25, "max": 25, "detail": "5/5 modules covered"},
                "review_rounds": {"score": 25, "max": 25, "detail": "0 needs-fix round(s)"},
                "size": {"score": 20, "max": 20, "detail": "80 lines changed"},
            },
        }
        result = _quality_gate_decision(payload)
        assert result["action"] == "pass"


class TestScriptSyntax:
    """Ensure the modified shell script is still valid bash."""

    def test_script_syntax_valid(self):
        script = REPO_ROOT / "scripts" / "team-lead-iteration.sh"
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"
