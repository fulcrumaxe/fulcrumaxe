"""tests/corpus_drift/test_three_section_spec_used.py

Unit tests for the project-manager.three_section_spec_used claim.
Uses injected Discussion fixtures — no GitHub API calls.
"""

from __future__ import annotations

import pytest

from backend.corpus_drift.claims.three_section_spec_used import (
    evaluate,
    CLAIM_ID,
    ROLE_SCOPE,
    ENFORCEMENT_PR,
    ENFORCEMENT_MERGED_AT,
)


# Body with all three required sections
_FULL_BODY = """\
## Intent

This is the intent section explaining why.

## Spec (Acceptance)

1. It does X.
2. It does Y.

## Implementation Notes

Use module Z.

<!-- STATUS:SPEC_READY SINCE:2026-05-19T10:00:00Z -->
"""

# Body missing Implementation Notes
_MISSING_IMPL = """\
## Intent

Why we need this.

## Spec (Acceptance)

The acceptance criteria.

<!-- STATUS:SPEC_READY SINCE:2026-05-19T10:00:00Z -->
"""

# Body missing all sections (legacy format)
_LEGACY_BODY = """\
We need to add a feature. Here's what it does.

<!-- STATUS:SPEC_READY SINCE:2026-05-19T10:00:00Z -->
"""


class TestThreeSectionSpecUsed:
    def test_all_three_sections_present(self):
        """Discussion body with all 3 headers → passes."""
        discussions = [{"number": 100, "body": _FULL_BODY}] * 5
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, discussions=discussions)
        assert result.claim_id == CLAIM_ID
        assert result.role_scope == ROLE_SCOPE
        assert result.score == pytest.approx(1.0)
        assert result.status == "healthy"

    def test_missing_section_reduces_score(self):
        """Discussion missing Implementation Notes → score < 1."""
        discussions = [
            {"number": 101, "body": _FULL_BODY},
            {"number": 102, "body": _FULL_BODY},
            {"number": 103, "body": _FULL_BODY},
            {"number": 104, "body": _MISSING_IMPL},
            {"number": 105, "body": _MISSING_IMPL},
        ]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, discussions=discussions)
        # 3/5 pass
        assert result.score == pytest.approx(0.6)
        assert result.status == "watch"

    def test_legacy_body_fails(self):
        """Discussion with no section headers → fails."""
        discussions = [{"number": 200, "body": _LEGACY_BODY}] * 5
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, discussions=discussions)
        assert result.score == pytest.approx(0.0)
        assert result.status == "drift"

    def test_no_discussions_returns_na(self):
        """No SPEC_READY Discussions in window → n/a."""
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, discussions=[])
        assert result.status == "n/a"
        assert result.sample_size == 0

    def test_score_type_is_fraction(self):
        """Claim uses score_type='fraction'."""
        discussions = [{"number": 300, "body": _FULL_BODY}] * 5
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, discussions=discussions)
        assert result.score_type == "fraction"

    def test_evidence_mentions_last_missing(self):
        """Evidence string references the last Discussion that failed."""
        # Use post-enforcement timestamps and enough Discussions (≥3) to clear the
        # windowing guard so we reach the scoring path where evidence names D#405.
        discussions = [
            {"number": 400, "body": _FULL_BODY, "created_at": "2026-05-19T18:00:00Z"},
            {"number": 401, "body": _FULL_BODY, "created_at": "2026-05-19T18:01:00Z"},
            {"number": 402, "body": _FULL_BODY, "created_at": "2026-05-19T18:02:00Z"},
            {"number": 403, "body": _FULL_BODY, "created_at": "2026-05-19T18:03:00Z"},
            {"number": 404, "body": _FULL_BODY, "created_at": "2026-05-19T18:04:00Z"},
            {"number": 405, "body": _LEGACY_BODY, "created_at": "2026-05-19T18:05:00Z"},
        ]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, discussions=discussions)
        assert "405" in result.evidence

    def test_full_body_all_passing_evidence(self):
        """When all pass, evidence says 'all N Discussions'."""
        discussions = [{"number": 500 + i, "body": _FULL_BODY} for i in range(6)]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, discussions=discussions)
        assert result.score == pytest.approx(1.0)
        assert "all" in result.evidence.lower() or "6/6" in result.evidence


# Timestamps bracketing the enforcement boundary
_PRE_ENFORCEMENT_TS = "2026-05-19T14:00:00Z"   # before PR #1132 merge
_POST_ENFORCEMENT_TS = "2026-05-19T18:00:00Z"   # after PR #1132 merge


def _make_disc(number: int, body: str, created_at: str) -> dict:
    return {"number": number, "body": body, "created_at": created_at}


class TestEnforcementBoundary:
    """Windowing: only Discussions filed after PR #1132 merge should be counted."""

    def test_constants_exported(self):
        """ENFORCEMENT_PR and ENFORCEMENT_MERGED_AT are importable constants."""
        assert ENFORCEMENT_PR == 1132
        assert "2026-05-19" in ENFORCEMENT_MERGED_AT

    def test_pre_enforcement_discussions_excluded(self):
        """Discussions with created_at <= enforcement timestamp are filtered out."""
        # 5 pre-enforcement + 5 post-enforcement; only post should count
        pre = [_make_disc(100 + i, _FULL_BODY, _PRE_ENFORCEMENT_TS) for i in range(5)]
        post = [_make_disc(200 + i, _FULL_BODY, _POST_ENFORCEMENT_TS) for i in range(5)]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=pre + post)
        assert result.sample_size == 5  # only post-enforcement
        assert result.score == pytest.approx(1.0)

    def test_only_pre_enforcement_returns_na(self):
        """All Discussions pre-date enforcement boundary → n/a."""
        pre = [_make_disc(300 + i, _FULL_BODY, _PRE_ENFORCEMENT_TS) for i in range(5)]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=pre)
        assert result.status == "n/a"
        assert result.sample_size == 0

    def test_fewer_than_three_post_enforcement_returns_na(self):
        """Only 2 post-enforcement Discussions → n/a (below 3-item minimum)."""
        post = [_make_disc(400 + i, _FULL_BODY, _POST_ENFORCEMENT_TS) for i in range(2)]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=post)
        assert result.status == "n/a"
        assert result.sample_size == 2

    def test_exactly_three_post_enforcement_passes_windowing_guard(self):
        """Three post-enforcement Discussions clears the windowing guard (sample_size=3)."""
        post = [_make_disc(500 + i, _FULL_BODY, _POST_ENFORCEMENT_TS) for i in range(3)]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=post)
        assert result.sample_size == 3
        assert "fewer than 3" not in result.evidence

    def test_no_created_at_included_conservatively(self):
        """Discussions without created_at are included (conservative — don't silently drop)."""
        # Mix: 2 without timestamp (included) + 3 post-enforcement
        no_ts = [{"number": 600 + i, "body": _FULL_BODY} for i in range(2)]
        post = [_make_disc(700 + i, _FULL_BODY, _POST_ENFORCEMENT_TS) for i in range(3)]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=no_ts + post)
        assert result.sample_size == 5  # all 5 included


class TestDoneDiscussionsIncluded:
    """Regression: DONE discussions must be counted, not filtered out.

    Before the fix, only SPEC_READY discussions were fetched from GitHub.
    Post-enforcement discussions that have been implemented and moved to DONE
    were silently excluded, causing N=0 even when 20 post-enforcement
    discussions existed.
    """

    # DONE-status bodies that should be scored regardless of their current status
    _DONE_FULL_BODY = """\
## Intent

This improvement adds X to the system.

## Spec (Acceptance)

1. X is implemented.
2. Y works correctly.

## Implementation Notes

Use module Z. Don't touch W.

<!-- STATUS:DONE SINCE:2026-05-19T22:00:00Z -->
"""

    _DONE_MISSING_IMPL = """\
## Intent

Why we need this.

## Spec (Acceptance)

The acceptance criteria.

<!-- STATUS:DONE SINCE:2026-05-19T22:00:00Z -->
"""

    def test_five_post_enforcement_done_discussions_all_counted(self):
        """Synthesise 5 post-enforcement DONE Discussions — all 5 must be counted."""
        discussions = [
            _make_disc(1140 + i, self._DONE_FULL_BODY, _POST_ENFORCEMENT_TS)
            for i in range(5)
        ]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=discussions)
        assert result.sample_size == 5
        assert result.score == pytest.approx(1.0)
        assert result.status == "healthy"

    def test_mixed_done_and_spec_ready_both_counted(self):
        """DONE and SPEC_READY discussions in same batch — all included in sample."""
        done_discs = [_make_disc(1150 + i, self._DONE_FULL_BODY, _POST_ENFORCEMENT_TS)
                      for i in range(3)]
        spec_ready_discs = [_make_disc(1160 + i, _FULL_BODY, _POST_ENFORCEMENT_TS)
                            for i in range(3)]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=done_discs + spec_ready_discs)
        assert result.sample_size == 6
        assert result.score == pytest.approx(1.0)

    def test_done_discussions_with_missing_sections_counted_as_failures(self):
        """DONE discussions missing sections still count as failures, not excluded."""
        discussions = [
            _make_disc(1170 + i, self._DONE_MISSING_IMPL, _POST_ENFORCEMENT_TS)
            for i in range(5)
        ]
        result = evaluate(runs=[], transcripts_dir=None, window_days=30,
                          discussions=discussions)
        assert result.sample_size == 5
        assert result.score == pytest.approx(0.0)
        assert result.status == "drift"
