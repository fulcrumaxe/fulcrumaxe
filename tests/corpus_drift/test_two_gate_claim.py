"""tests/corpus_drift/test_two_gate_claim.py

Unit tests for the two_gate claim's enforcement-boundary windowing.

Key behaviours under test:
- PRs below ENFORCEMENT_PR are excluded from the sample.
- Only post-enforcement PRs are counted (mix of old + new).
- Fewer than 3 post-enforcement PRs → status "n/a".
- 0 post-enforcement PRs → status "n/a".
- Sufficient post-enforcement PRs with both gates → "healthy".
- Sufficient post-enforcement PRs missing gates → non-healthy status.
"""

from __future__ import annotations

import pytest

import backend.corpus_drift.claims.two_gate as _m
from backend.corpus_drift.claims.two_gate import ENFORCEMENT_PR


_BODY_WITH_GATES = "## Gate 1: pytest green\n\n## Gate 2: audit run\n\nAll good."
_BODY_WITHOUT_GATES = "Fixes the bug. No gate markers here."
_BODY_GATE1_ONLY = "## Gate 1: passed.\n\nOnly one gate present; second gate absent."


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_runs(pr_numbers: list[int]) -> list[dict]:
    return [{"pr": str(n)} for n in pr_numbers]


def _stub_fetch(pairs: list[tuple[int, str]]):
    """Return a _fetch_pr_bodies replacement that returns fixed pairs."""
    def _fetch(pr_numbers, limit):
        return pairs
    return _fetch


# ── Enforcement boundary filtering ───────────────────────────────────────────

class TestEnforcementBoundary:
    def test_pre_enforcement_prs_excluded_from_runs(self, monkeypatch):
        """PRs below ENFORCEMENT_PR in runs are not passed to _fetch_pr_bodies."""
        fetched = []

        def _capture_fetch(pr_numbers, limit):
            fetched.extend(pr_numbers)
            return []

        monkeypatch.setattr(_m, "_fetch_pr_bodies", _capture_fetch)

        # Mix: some below, some at/above boundary
        below = [ENFORCEMENT_PR - 5, ENFORCEMENT_PR - 1]
        above = [ENFORCEMENT_PR, ENFORCEMENT_PR + 1, ENFORCEMENT_PR + 5]
        _m.evaluate(runs=_make_runs(below + above), transcripts_dir=None, window_days=30)

        assert all(n >= ENFORCEMENT_PR for n in fetched), (
            f"Expected only PRs >= {ENFORCEMENT_PR}, got {fetched}"
        )
        assert sorted(fetched) == sorted(above)

    def test_only_pre_enforcement_prs_gives_na(self, monkeypatch):
        """All runs reference PRs below ENFORCEMENT_PR → n/a (empty post-enforcement sample)."""
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch([]))
        result = _m.evaluate(
            runs=_make_runs([ENFORCEMENT_PR - 10, ENFORCEMENT_PR - 1]),
            transcripts_dir=None,
            window_days=30,
        )
        assert result.status == "n/a"
        assert result.sample_size == 0

    def test_fallback_path_filters_by_enforcement_boundary(self, monkeypatch):
        """Fallback (no pr_numbers) also discards PRs below ENFORCEMENT_PR."""
        # Simulate gh returning a mix of old and new PRs.
        # Use 5 post-enforcement PRs so classify_fraction has enough for a verdict.
        all_prs = [
            (ENFORCEMENT_PR - 3, _BODY_WITH_GATES),
            (ENFORCEMENT_PR - 1, _BODY_WITH_GATES),
            (ENFORCEMENT_PR, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 1, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 2, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 3, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 4, _BODY_WITH_GATES),
        ]
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch(all_prs))

        # runs has no pr field → fallback path triggered
        result = _m.evaluate(runs=[], transcripts_dir=None, window_days=30)

        # Only the 5 at/above boundary should be counted (2 below are filtered out)
        assert result.sample_size == 5
        assert result.score == pytest.approx(1.0)
        assert result.status == "healthy"


# ── n/a threshold (< 3 post-enforcement PRs) ─────────────────────────────────

class TestNaThreshold:
    def test_zero_post_enforcement_prs_is_na(self, monkeypatch):
        """No PRs >= ENFORCEMENT_PR → n/a."""
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch([]))
        result = _m.evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.status == "n/a"
        assert result.sample_size == 0

    def test_one_post_enforcement_pr_is_na(self, monkeypatch):
        """One PR at boundary → n/a (below 3-PR minimum)."""
        monkeypatch.setattr(
            _m, "_fetch_pr_bodies",
            _stub_fetch([(ENFORCEMENT_PR, _BODY_WITH_GATES)]),
        )
        result = _m.evaluate(
            runs=_make_runs([ENFORCEMENT_PR]),
            transcripts_dir=None,
            window_days=30,
        )
        assert result.status == "n/a"
        assert result.sample_size == 1

    def test_two_post_enforcement_prs_is_na(self, monkeypatch):
        """Two PRs at/above boundary → n/a (still below 3-PR minimum)."""
        pairs = [
            (ENFORCEMENT_PR, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 1, _BODY_WITH_GATES),
        ]
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch(pairs))
        result = _m.evaluate(
            runs=_make_runs([ENFORCEMENT_PR, ENFORCEMENT_PR + 1]),
            transcripts_dir=None,
            window_days=30,
        )
        assert result.status == "n/a"
        assert result.sample_size == 2

    def test_three_post_enforcement_prs_passes_windowing_guard(self, monkeypatch):
        """Three PRs passes the windowing guard (sample_size=3 is returned, not 0).

        Note: classify_fraction still returns "n/a" for sample_size < 5 via its
        own MIN_SAMPLE threshold — that is intentional and unchanged by this feature.
        The windowing guard only gates at < 3; higher samples go through normal scoring.
        """
        pairs = [
            (ENFORCEMENT_PR, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 1, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 2, _BODY_WITH_GATES),
        ]
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch(pairs))
        result = _m.evaluate(
            runs=_make_runs([ENFORCEMENT_PR, ENFORCEMENT_PR + 1, ENFORCEMENT_PR + 2]),
            transcripts_dir=None,
            window_days=30,
        )
        # sample_size is 3 — the windowing guard didn't cut it to 0
        assert result.sample_size == 3
        # Evidence should not be the windowing n/a message
        assert "fewer than 3" not in result.evidence
        assert "no executor PRs" not in result.evidence


# ── Scoring with post-enforcement sample ─────────────────────────────────────

class TestScoringPostEnforcement:
    def _make_mixed_runs(self) -> tuple[list[dict], list[tuple[int, str]]]:
        """5 pre-enforcement + 5 post-enforcement runs; only post-enforcement should count."""
        pre = list(range(ENFORCEMENT_PR - 5, ENFORCEMENT_PR))
        post = list(range(ENFORCEMENT_PR, ENFORCEMENT_PR + 5))
        runs = _make_runs(pre + post)
        # All post-enforcement PRs have both gates
        pairs = [(n, _BODY_WITH_GATES) for n in post]
        return runs, pairs

    def test_only_post_enforcement_count_in_sample(self, monkeypatch):
        """Sample size equals the number of post-enforcement PRs only."""
        runs, pairs = self._make_mixed_runs()

        def _fetch(pr_numbers, limit):
            # Return bodies only for the numbers it's given
            return [(n, _BODY_WITH_GATES) for n in pr_numbers]

        monkeypatch.setattr(_m, "_fetch_pr_bodies", _fetch)
        result = _m.evaluate(runs=runs, transcripts_dir=None, window_days=30)

        assert result.sample_size == 5  # only the 5 post-enforcement PRs

    def test_all_gates_present_is_healthy(self, monkeypatch):
        """All post-enforcement PRs with both gates → healthy."""
        pairs = [(ENFORCEMENT_PR + i, _BODY_WITH_GATES) for i in range(5)]
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch(pairs))
        result = _m.evaluate(
            runs=_make_runs([ENFORCEMENT_PR + i for i in range(5)]),
            transcripts_dir=None,
            window_days=30,
        )
        assert result.score == pytest.approx(1.0)
        assert result.status == "healthy"

    def test_no_gates_is_drift(self, monkeypatch):
        """All post-enforcement PRs missing gate markers → drift."""
        pairs = [(ENFORCEMENT_PR + i, _BODY_WITHOUT_GATES) for i in range(5)]
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch(pairs))
        result = _m.evaluate(
            runs=_make_runs([ENFORCEMENT_PR + i for i in range(5)]),
            transcripts_dir=None,
            window_days=30,
        )
        assert result.score == pytest.approx(0.0)
        assert result.status == "drift"

    def test_partial_gates_present(self, monkeypatch):
        """3 of 5 post-enforcement PRs have both gates → score 0.6 (watch)."""
        pairs = [
            (ENFORCEMENT_PR + 0, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 1, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 2, _BODY_WITH_GATES),
            (ENFORCEMENT_PR + 3, _BODY_WITHOUT_GATES),
            (ENFORCEMENT_PR + 4, _BODY_GATE1_ONLY),
        ]
        monkeypatch.setattr(_m, "_fetch_pr_bodies", _stub_fetch(pairs))
        result = _m.evaluate(
            runs=_make_runs([ENFORCEMENT_PR + i for i in range(5)]),
            transcripts_dir=None,
            window_days=30,
        )
        assert result.score == pytest.approx(3 / 5)
        assert result.status == "watch"
