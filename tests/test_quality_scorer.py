"""Tests for quality_scorer.py — applicable/non-applicable paths, cache behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.quality_scorer import QualityScorer


# ---------------------------------------------------------------------------
# Diff fixtures
# ---------------------------------------------------------------------------

MARKDOWN_ONLY_DIFF = """\
diff --git a/README.md b/README.md
index 0000000..1111111 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,4 @@
 # My project
+
+Updated docs to reflect new architecture.
+More detail here.
"""

SHELL_ONLY_DIFF = """\
diff --git a/scripts/deploy.sh b/scripts/deploy.sh
index 0000000..1111111 100755
--- a/scripts/deploy.sh
+++ b/scripts/deploy.sh
@@ -1,3 +1,6 @@
 #!/usr/bin/env bash
+set -euo pipefail
+
 echo "deploying"
+bash build.sh
+bash test.sh
"""

PYTHON_DIFF = """\
diff --git a/backend/foo.py b/backend/foo.py
index 0000000..1111111 100644
--- a/backend/foo.py
+++ b/backend/foo.py
@@ -1,3 +1,12 @@
 # foo module
+
+
+def add(a, b):
+    return a + b
+
+
+def subtract(a, b):
+    if b == 0:
+        raise ValueError("cannot subtract zero")
+    return a - b
"""

MIXED_DIFF = """\
diff --git a/README.md b/README.md
index 0000000..1111111 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 # project
+More docs.
diff --git a/backend/utils.py b/backend/utils.py
index 0000000..1111111 100644
--- a/backend/utils.py
+++ b/backend/utils.py
@@ -1,2 +1,5 @@
 # utils
+
+
+def helper():
+    return True
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_scorer() -> QualityScorer:
    """Return a scorer that uses the actual repo root (for file lookups)."""
    return QualityScorer()


# ---------------------------------------------------------------------------
# applicable=false cases
# ---------------------------------------------------------------------------


class TestNotApplicable:
    def test_markdown_only_returns_applicable_false(self):
        scorer = make_scorer()
        result = scorer.score_diff(MARKDOWN_ONLY_DIFF)

        assert result["applicable"] is False

    def test_markdown_only_total_score_is_none(self):
        scorer = make_scorer()
        result = scorer.score_diff(MARKDOWN_ONLY_DIFF)

        assert result["total_score"] is None

    def test_markdown_only_grade_is_na(self):
        scorer = make_scorer()
        result = scorer.score_diff(MARKDOWN_ONLY_DIFF)

        assert result["grade"] == "N/A"

    def test_markdown_only_reason_present(self):
        scorer = make_scorer()
        result = scorer.score_diff(MARKDOWN_ONLY_DIFF)

        assert "reason" in result
        assert result["reason"]  # non-empty

    def test_shell_only_returns_applicable_false(self):
        scorer = make_scorer()
        result = scorer.score_diff(SHELL_ONLY_DIFF)

        assert result["applicable"] is False

    def test_shell_only_total_score_is_none(self):
        scorer = make_scorer()
        result = scorer.score_diff(SHELL_ONLY_DIFF)

        assert result["total_score"] is None

    def test_empty_diff_returns_applicable_false(self):
        scorer = make_scorer()
        result = scorer.score_diff("")

        assert result["applicable"] is False
        assert result["total_score"] is None


# ---------------------------------------------------------------------------
# applicable=true cases
# ---------------------------------------------------------------------------


class TestApplicable:
    def test_python_diff_returns_applicable_true(self):
        scorer = make_scorer()
        result = scorer.score_diff(PYTHON_DIFF)

        assert result["applicable"] is True

    def test_python_diff_total_score_is_int(self):
        scorer = make_scorer()
        result = scorer.score_diff(PYTHON_DIFF)

        assert isinstance(result["total_score"], int)

    def test_python_diff_score_in_range(self):
        scorer = make_scorer()
        result = scorer.score_diff(PYTHON_DIFF)

        assert 0 <= result["total_score"] <= 100

    def test_python_diff_grade_not_na(self):
        scorer = make_scorer()
        result = scorer.score_diff(PYTHON_DIFF)

        assert result["grade"] != "N/A"
        assert result["grade"]  # non-empty

    def test_python_diff_no_reason_field(self):
        """applicable=true results should NOT carry a reason field."""
        scorer = make_scorer()
        result = scorer.score_diff(PYTHON_DIFF)

        assert "reason" not in result

    def test_mixed_diff_returns_applicable_true(self):
        """A diff with both markdown and Python files is still applicable."""
        scorer = make_scorer()
        result = scorer.score_diff(MIXED_DIFF)

        assert result["applicable"] is True
        assert isinstance(result["total_score"], int)


# ---------------------------------------------------------------------------
# stats() excludes non-applicable entries
# ---------------------------------------------------------------------------


class TestStatsExcludesNonApplicable:
    def test_stats_ignores_not_applicable_entries(self, tmp_path, monkeypatch):
        """Adding non-applicable PR scores must not change avg_total."""
        from backend.blackboard import Blackboard

        bb = Blackboard(root=tmp_path / "blackboard")

        # Write one real applicable score
        applicable_entry = {
            "applicable": True,
            "pr": 1,
            "total_score": 80,
            "grade": "B",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "breakdown": {
                "complexity": {"score": 25, "max": 30},
                "test_coverage": {"score": 20, "max": 25},
                "review_rounds": {"score": 20, "max": 25},
                "size": {"score": 15, "max": 20},
            },
            "files_changed": ["backend/foo.py"],
        }
        bb.write("quality/1", applicable_entry, updated_by="test")

        scorer = QualityScorer()
        monkeypatch.setattr(scorer, "_bb", bb)

        stats_before = scorer.stats()
        avg_before = stats_before["avg_total"]

        # Now add a non-applicable entry — avg should be unchanged
        not_applicable_entry = {
            "applicable": False,
            "pr": 2,
            "total_score": None,
            "grade": "N/A",
            "timestamp": "2026-01-02T00:00:00+00:00",
            "reason": "no scorable files in diff",
            "breakdown": {},
            "files_changed": ["README.md"],
        }
        bb.write("quality/2", not_applicable_entry, updated_by="test")

        stats_after = scorer.stats()
        avg_after = stats_after["avg_total"]

        assert avg_before == avg_after, (
            f"avg_total changed from {avg_before} to {avg_after} "
            "after adding non-applicable entry — stats() must filter them out"
        )
        # total_scored should reflect both entries
        assert stats_after["total_scored"] == 2


# ---------------------------------------------------------------------------
# Cache behaviour: hit on matching head_sha, miss on stale sha
# ---------------------------------------------------------------------------

_FRESH_SCORE = {
    "applicable": True,
    "pr": 99,
    "total_score": 75,
    "grade": "C+",
    "head_sha": "abc123def456abc123def456abc123def456abc1",
    "timestamp": None,  # filled in per test
    "breakdown": {
        "complexity": {"score": 20, "max": 30},
        "test_coverage": {"score": 20, "max": 25},
        "review_rounds": {"score": 20, "max": 25},
        "size": {"score": 15, "max": 20},
    },
    "files_changed": ["backend/foo.py"],
}

_HEAD_SHA = "abc123def456abc123def456abc123def456abc1"
_OTHER_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


class TestScorePrCache:
    """Integration-level tests for the head_sha cache in score_pr()."""

    def _make_scorer_with_bb(self, tmp_path):
        """Return a QualityScorer whose blackboard lives in tmp_path."""
        from backend.blackboard import Blackboard

        scorer = QualityScorer()
        bb = Blackboard(root=tmp_path / "blackboard")
        scorer._bb = bb
        return scorer, bb

    def _write_cached_score(self, bb, pr, head_sha, age_sec=10):
        """Write a fresh-looking score entry with the given head_sha."""
        import copy

        entry = copy.deepcopy(_FRESH_SCORE)
        entry["pr"] = pr
        entry["head_sha"] = head_sha
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        entry["timestamp"] = ts.isoformat(timespec="seconds")
        bb.write(f"quality/{pr}", entry, updated_by="test")
        return entry

    # ------------------------------------------------------------------
    # Cache hit: same SHA, within TTL
    # ------------------------------------------------------------------

    def test_cache_hit_skips_diff_fetch(self, tmp_path):
        """When a fresh same-SHA entry exists, _fetch_pr_diff must NOT be called."""
        scorer, bb = self._make_scorer_with_bb(tmp_path)
        self._write_cached_score(bb, pr=99, head_sha=_HEAD_SHA, age_sec=30)

        with (
            patch.object(scorer, "_fetch_pr_head_sha", return_value=_HEAD_SHA),
            patch.object(scorer, "_fetch_pr_diff") as mock_diff,
        ):
            result = scorer.score_pr(99, cache_ttl_sec=300)

        mock_diff.assert_not_called()
        assert result["total_score"] == 75

    def test_cache_hit_returns_cached_score(self, tmp_path):
        """Cached result is returned intact on a hit."""
        scorer, bb = self._make_scorer_with_bb(tmp_path)
        self._write_cached_score(bb, pr=99, head_sha=_HEAD_SHA, age_sec=10)

        with patch.object(scorer, "_fetch_pr_head_sha", return_value=_HEAD_SHA):
            result = scorer.score_pr(99, cache_ttl_sec=300)

        assert result["head_sha"] == _HEAD_SHA
        assert result["grade"] == "C+"

    def test_cache_hit_logs_to_stderr(self, tmp_path, capsys):
        """A cache hit emits the [scorer] cache-hit log line to stderr."""
        scorer, bb = self._make_scorer_with_bb(tmp_path)
        self._write_cached_score(bb, pr=99, head_sha=_HEAD_SHA, age_sec=5)

        with patch.object(scorer, "_fetch_pr_head_sha", return_value=_HEAD_SHA):
            scorer.score_pr(99, cache_ttl_sec=300)

        captured = capsys.readouterr()
        assert "[scorer] cache-hit" in captured.err
        assert "pr=#99" in captured.err
        assert _HEAD_SHA[:12] in captured.err

    # ------------------------------------------------------------------
    # Cache miss: different SHA
    # ------------------------------------------------------------------

    def test_cache_miss_on_stale_sha(self, tmp_path):
        """When the stored sha differs from the current head, recompute from diff."""
        scorer, bb = self._make_scorer_with_bb(tmp_path)
        # Existing entry has OLD_SHA; PR now has _HEAD_SHA
        self._write_cached_score(bb, pr=99, head_sha=_OTHER_SHA, age_sec=5)

        with (
            patch.object(scorer, "_fetch_pr_head_sha", return_value=_HEAD_SHA),
            patch.object(scorer, "_fetch_pr_diff", return_value=PYTHON_DIFF) as mock_diff,
        ):
            result = scorer.score_pr(99, cache_ttl_sec=300)

        mock_diff.assert_called_once()
        # Result should be freshly computed, not the cached 75
        assert result.get("head_sha") == _HEAD_SHA

    # ------------------------------------------------------------------
    # Cache miss: TTL expired
    # ------------------------------------------------------------------

    def test_cache_miss_on_expired_ttl(self, tmp_path):
        """When the cached entry is older than cache_ttl_sec, recompute."""
        scorer, bb = self._make_scorer_with_bb(tmp_path)
        # Entry is 400s old, TTL is 300s
        self._write_cached_score(bb, pr=99, head_sha=_HEAD_SHA, age_sec=400)

        with (
            patch.object(scorer, "_fetch_pr_head_sha", return_value=_HEAD_SHA),
            patch.object(scorer, "_fetch_pr_diff", return_value=PYTHON_DIFF) as mock_diff,
        ):
            scorer.score_pr(99, cache_ttl_sec=300)

        mock_diff.assert_called_once()

    # ------------------------------------------------------------------
    # cache_ttl_sec=0 always recomputes
    # ------------------------------------------------------------------

    def test_force_recompute_when_ttl_zero(self, tmp_path):
        """cache_ttl_sec=0 must bypass the cache entirely."""
        scorer, bb = self._make_scorer_with_bb(tmp_path)
        self._write_cached_score(bb, pr=99, head_sha=_HEAD_SHA, age_sec=1)

        with (
            patch.object(scorer, "_fetch_pr_head_sha") as mock_sha,
            patch.object(scorer, "_fetch_pr_diff", return_value=PYTHON_DIFF) as mock_diff,
        ):
            scorer.score_pr(99, cache_ttl_sec=0)

        mock_sha.assert_not_called()
        mock_diff.assert_called_once()
