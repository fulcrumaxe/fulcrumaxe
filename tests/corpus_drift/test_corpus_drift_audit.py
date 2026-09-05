"""tests/corpus_drift/test_corpus_drift_audit.py

Fixture-based unit tests for the corpus drift audit subsystem.
Each claim has at least one passing path and one failing path test.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tests.corpus_drift.conftest import (
    write_transcript,
    _make_transcript,
    _make_assistant_text_turn,
)
from backend.corpus_drift.types import ClaimResult


# ── Helpers ─────────────────────────────────────────────────────────────────

def _bash_tc(command: str) -> dict:
    return {"name": "Bash", "input": {"command": command}}


def _agent_tc() -> dict:
    return {"name": "Agent", "input": {"prompt": "do stuff"}}


# ── ClaimResult helpers ──────────────────────────────────────────────────────

class TestClaimResult:
    def test_classify_fraction_healthy(self):
        assert ClaimResult.classify_fraction(0.80, 10) == "healthy"

    def test_classify_fraction_watch(self):
        assert ClaimResult.classify_fraction(0.60, 10) == "watch"

    def test_classify_fraction_drift(self):
        assert ClaimResult.classify_fraction(0.30, 10) == "drift"

    def test_classify_fraction_na_small_sample(self):
        assert ClaimResult.classify_fraction(0.90, 3) == "n/a"

    def test_classify_count_healthy(self):
        assert ClaimResult.classify_count(0, 10) == "healthy"

    def test_classify_count_drift(self):
        assert ClaimResult.classify_count(3, 10) == "drift"

    def test_classify_count_na_small_sample(self):
        assert ClaimResult.classify_count(0, 2) == "n/a"

    def test_score_display_fraction(self):
        r = ClaimResult(
            claim_id="x.y", role_scope="x", sample_size=10,
            score=0.75, score_type="fraction", status="healthy", evidence=""
        )
        assert r.score_display() == "75%"

    def test_score_display_count(self):
        r = ClaimResult(
            claim_id="x.y", role_scope="x", sample_size=10,
            score=3, score_type="count", status="drift", evidence=""
        )
        assert r.score_display() == "3"


# ── pytest_invoked claim ────────────────────────────────────────────────────

class TestPytestInvoked:
    def test_passing_transcript_detected(self, tmp_transcript_dir, monkeypatch):
        """Transcript with 'pytest tests/' Bash call counts as passing."""
        # Create 5 identical transcripts so sample_size >= MIN_SAMPLE
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-00{i}",
                [_make_transcript([_bash_tc("pytest tests/ -q")])]
            )
            transcripts.append(t)
        self._patch_find(monkeypatch, transcripts)

        from backend.corpus_drift.claims.pytest_invoked import evaluate
        result = evaluate(
            runs=[{"agent_id": f"run-00{i}"} for i in range(5)],
            transcripts_dir=None, window_days=30,
        )
        assert result.score == pytest.approx(1.0)
        assert result.status == "healthy"

    def test_missing_pytest_call(self, tmp_transcript_dir, monkeypatch):
        """Transcript with no pytest call does not pass."""
        t = write_transcript(
            tmp_transcript_dir, "run-002",
            [_make_transcript([_bash_tc("npm test")])]
        )
        self._patch_find(monkeypatch, [t])

        from backend.corpus_drift.claims.pytest_invoked import evaluate
        result = evaluate(runs=[{"agent_id": "run-002"}], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(0.0)

    def test_python_m_pytest_counts(self, tmp_transcript_dir, monkeypatch):
        """'python -m pytest ...' also matches."""
        t = write_transcript(
            tmp_transcript_dir, "run-003",
            [_make_transcript([_bash_tc("python3 -m pytest tests/")])]
        )
        self._patch_find(monkeypatch, [t])

        from backend.corpus_drift.claims.pytest_invoked import evaluate
        result = evaluate(runs=[{"agent_id": "run-003"}], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(1.0)

    def test_no_transcripts_returns_na(self, monkeypatch):
        self._patch_find(monkeypatch, [])
        from backend.corpus_drift.claims.pytest_invoked import evaluate
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.status == "n/a"

    def test_npm_test_only_not_counted(self, tmp_transcript_dir, monkeypatch):
        """Failure mode: code-reviewer uses 'npm test' but no pytest — should score 0."""
        t = write_transcript(
            tmp_transcript_dir, "run-npm-only",
            [_make_transcript([_bash_tc("npm test -- --watchAll=false")])]
        )
        self._patch_find(monkeypatch, [t])

        from backend.corpus_drift.claims.pytest_invoked import evaluate
        result = evaluate(runs=[{"agent_id": "run-npm-only"}], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(0.0), "npm test without pytest should score 0"

    def test_backend_tests_suite_counts(self, tmp_transcript_dir, monkeypatch):
        """Corrected pattern: 'pytest backend/tests/ -q' is the canonical invocation."""
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-backend-{i}",
                [_make_transcript([_bash_tc("pytest backend/tests/ -q")])]
            )
            transcripts.append(t)
        self._patch_find(monkeypatch, transcripts)

        from backend.corpus_drift.claims.pytest_invoked import evaluate
        result = evaluate(
            runs=[{"agent_id": f"run-backend-{i}"} for i in range(5)],
            transcripts_dir=None, window_days=30,
        )
        assert result.score == pytest.approx(1.0), "pytest backend/tests/ -q should count as pytest invocation"
        assert result.status == "healthy"

    @staticmethod
    def _patch_find(monkeypatch, paths: list[Path]):
        """Patch find_transcripts in the claim module to return the given paths."""
        import backend.corpus_drift.claims.pytest_invoked as _m
        monkeypatch.setattr(_m, "find_transcripts", lambda since_seconds=None: paths)


# ── archive_protocol claim ───────────────────────────────────────────────────

class TestArchiveProtocol:
    def test_no_git_rm_is_healthy(self, tmp_transcript_dir, monkeypatch):
        """No git rm calls in transcripts → healthy (count=0)."""
        transcripts = []
        for i in range(6):
            t = write_transcript(
                tmp_transcript_dir, f"run-ap-{i:03d}",
                [_make_transcript([_bash_tc("git add . && git commit -m 'x'")])]
            )
            transcripts.append(str(t))

        self._patch_glob(monkeypatch, transcripts)
        from backend.corpus_drift.claims.archive_protocol import evaluate
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0
        assert result.status == "healthy"

    def test_git_rm_detected(self, tmp_transcript_dir, monkeypatch):
        """Transcript with git rm → drift (count > 0)."""
        transcripts = []
        # 5 clean transcripts + 1 violating to exceed min_sample
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-ap-clean-{i}",
                [_make_transcript([_bash_tc("git add file.py")])]
            )
            transcripts.append(str(t))
        bad = write_transcript(
            tmp_transcript_dir, "run-ap-bad",
            [_make_transcript([_bash_tc("git rm old-file.py")])]
        )
        transcripts.append(str(bad))

        self._patch_glob(monkeypatch, transcripts)
        from backend.corpus_drift.claims.archive_protocol import evaluate
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score >= 1
        assert result.status == "drift"
        assert result.score_type == "count"

    @staticmethod
    def _patch_glob(monkeypatch, paths):
        import backend.corpus_drift.claims.archive_protocol as _m
        monkeypatch.setattr(_m.glob, "glob", lambda pattern: paths)
        monkeypatch.setattr(_m.os.path, "getmtime", lambda p: 9999999999.0)


# ── self_observe claim ───────────────────────────────────────────────────────

class TestSelfObserve:
    _ENVELOPE_WITH = (
        '<!-- AGENT_OUTPUT -->\n```json\n'
        '{"agent": "executor", "verdict": "done", "self_observed": true}\n'
        '```\n<!-- /AGENT_OUTPUT -->'
    )
    _ENVELOPE_WITHOUT = (
        '<!-- AGENT_OUTPUT -->\n```json\n'
        '{"agent": "executor", "verdict": "done"}\n'
        '```\n<!-- /AGENT_OUTPUT -->'
    )

    def test_self_observed_true_counts(self, tmp_transcript_dir, monkeypatch):
        """Envelope with self_observed=true counted as present."""
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-so-{i}",
                [_make_assistant_text_turn(self._ENVELOPE_WITH)]
            )
            transcripts.append(str(t))

        self._patch(monkeypatch, transcripts)
        from backend.corpus_drift.claims.self_observe import evaluate
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, retros_path=Path("/nonexistent"))
        assert result.score == pytest.approx(1.0)

    def test_missing_self_observed_not_counted(self, tmp_transcript_dir, monkeypatch):
        """Envelope without self_observed key → score < 1."""
        transcripts = []
        # 4 with, 2 without
        for i in range(4):
            t = write_transcript(
                tmp_transcript_dir, f"run-so-with-{i}",
                [_make_assistant_text_turn(self._ENVELOPE_WITH)]
            )
            transcripts.append(str(t))
        for i in range(2):
            t = write_transcript(
                tmp_transcript_dir, f"run-so-without-{i}",
                [_make_assistant_text_turn(self._ENVELOPE_WITHOUT)]
            )
            transcripts.append(str(t))

        self._patch(monkeypatch, transcripts)
        from backend.corpus_drift.claims.self_observe import evaluate
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, retros_path=Path("/nonexistent"))
        # 4/6 envelopes have self_observed
        assert result.score == pytest.approx(4 / 6, abs=0.01)

    @staticmethod
    def _patch(monkeypatch, paths):
        import backend.corpus_drift.claims.self_observe as _m
        monkeypatch.setattr(_m.glob, "glob", lambda pattern: paths)
        monkeypatch.setattr(_m.os.path, "getmtime", lambda p: 9999999999.0)


# ── spawn_wrapper claim ──────────────────────────────────────────────────────

class TestSpawnWrapper:
    def test_agent_preceded_by_wrapper(self, tmp_transcript_dir, monkeypatch):
        """Agent() call preceded by spawn-agent.sh Bash call → counts as covered."""
        lines = [
            _make_transcript([
                _bash_tc("bash scripts/spawn-agent.sh --role executor"),
                _agent_tc(),
            ])
        ]
        transcripts = []
        for i in range(5):
            t = write_transcript(tmp_transcript_dir, f"run-sw-{i}", lines)
            transcripts.append(t)

        self._patch_find(monkeypatch, transcripts)
        from backend.corpus_drift.claims.spawn_wrapper import evaluate
        result = evaluate(runs=[{"agent_id": f"run-sw-{i}"} for i in range(5)],
                          transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(1.0)

    def test_agent_without_wrapper(self, tmp_transcript_dir, monkeypatch):
        """Agent() call without preceding spawn-agent.sh → uncovered."""
        lines = [
            _make_transcript([
                _bash_tc("echo hello"),
                _agent_tc(),
            ])
        ]
        transcripts = []
        for i in range(5):
            t = write_transcript(tmp_transcript_dir, f"run-sw-no-{i}", lines)
            transcripts.append(t)

        self._patch_find(monkeypatch, transcripts)
        from backend.corpus_drift.claims.spawn_wrapper import evaluate
        result = evaluate(runs=[{"agent_id": f"run-sw-no-{i}"} for i in range(5)],
                          transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(0.0)

    def test_inline_agent_prompt_not_counted(self, tmp_transcript_dir, monkeypatch):
        """Failure mode: Team Lead uses inline Agent() without the wrapper — not covered."""
        lines = [
            _make_transcript([
                # Team Lead writes inline prompt directly — no wrapper call
                _bash_tc("gh pr list --repo autonomous-agent-7/autonomous-forever --state open"),
                _agent_tc(),
            ])
        ]
        transcripts = []
        for i in range(5):
            t = write_transcript(tmp_transcript_dir, f"run-inline-{i}", lines)
            transcripts.append(t)

        self._patch_find(monkeypatch, transcripts)
        from backend.corpus_drift.claims.spawn_wrapper import evaluate
        result = evaluate(runs=[{"agent_id": f"run-inline-{i}"} for i in range(5)],
                          transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(0.0), "Inline Agent() without wrapper should score 0"

    def test_spawn_wrapper_with_role_flag_counts(self, tmp_transcript_dir, monkeypatch):
        """Corrected pattern: wrapper call with --role and --discussion flags → covered."""
        lines = [
            _make_transcript([
                _bash_tc("bash scripts/spawn-agent.sh --role code-reviewer --discussion 42 --pr 88"),
                _agent_tc(),
            ])
        ]
        transcripts = []
        for i in range(5):
            t = write_transcript(tmp_transcript_dir, f"run-sw-full-{i}", lines)
            transcripts.append(t)

        self._patch_find(monkeypatch, transcripts)
        from backend.corpus_drift.claims.spawn_wrapper import evaluate
        result = evaluate(runs=[{"agent_id": f"run-sw-full-{i}"} for i in range(5)],
                          transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(1.0), "Full wrapper call should score 1.0"
        assert result.status == "healthy"

    @staticmethod
    def _patch_find(monkeypatch, paths: list[Path]):
        """Patch find_transcripts in the claim module to return the given paths."""
        import backend.corpus_drift.claims.spawn_wrapper as _m
        monkeypatch.setattr(_m, "find_transcripts", lambda since_seconds=None: paths)


# ── two_gate claim ───────────────────────────────────────────────────────────

class TestTwoGate:
    def test_pr_with_both_gates_passes(self, monkeypatch):
        """PR body containing 'Gate 1' and 'Gate 2' counts as passing."""
        import backend.corpus_drift.claims.two_gate as _m
        body = "## Gate 1: tests pass\n\n## Gate 2: live audit run\n\nLooks good."
        # Use PR numbers at/above ENFORCEMENT_PR so they aren't filtered out
        base = _m.ENFORCEMENT_PR
        monkeypatch.setattr(_m, "_fetch_pr_bodies", lambda pr_numbers, limit: [(base + i, body) for i in range(6)])
        result = _m.evaluate(runs=[{"pr": str(base)}], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(1.0)
        assert result.status == "healthy"

    def test_pr_missing_gate_2_fails(self, monkeypatch):
        """PR body with only 'Gate 1' does not count as passing (Gate 2 absent)."""
        import backend.corpus_drift.claims.two_gate as _m
        body = "## Gate 1: tests pass\n\nOnly one gate here."
        base = _m.ENFORCEMENT_PR
        monkeypatch.setattr(_m, "_fetch_pr_bodies", lambda pr_numbers, limit: [(base + i, body) for i in range(6)])
        result = _m.evaluate(runs=[{"pr": str(base)}], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(0.0)

    def test_no_prs_returns_na(self, monkeypatch):
        """No PRs in window → n/a."""
        import backend.corpus_drift.claims.two_gate as _m
        monkeypatch.setattr(_m, "_fetch_pr_bodies", lambda pr_numbers, limit: [])
        result = _m.evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.status == "n/a"


# ── Report renderer ─────────────────────────────────────────────────────────

class TestReportRenderer:
    def _make_results(self) -> list[ClaimResult]:
        return [
            ClaimResult(
                claim_id="code-reviewer.pytest_invoked",
                role_scope="code-reviewer",
                sample_size=10,
                score=0.8,
                score_type="fraction",
                status="healthy",
                evidence="all passing",
            ),
            ClaimResult(
                claim_id="global.archive_protocol_honored",
                role_scope="global",
                sample_size=0,
                score=0,
                score_type="count",
                status="n/a",
                evidence="no transcripts",
            ),
        ]

    def test_report_written(self, tmp_path):
        from backend.corpus_drift.report import render_markdown
        results = self._make_results()
        report_path = tmp_path / "Corpus-Drift-Report.md"
        content = render_markdown(
            results=results,
            window_days=30,
            generated_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc),
            sample_cap=100,
            report_path=report_path,
        )
        assert report_path.exists()
        assert "Corpus Drift Report" in content
        assert "code-reviewer.pytest_invoked" in content
        assert "80%" in content

    def test_json_snapshot_written(self, tmp_path):
        from backend.corpus_drift.report import write_json_snapshot
        results = self._make_results()
        snapshot_path = tmp_path / "2026-05-19.json"
        write_json_snapshot(
            results=results,
            window_days=30,
            generated_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc),
            snapshot_path=snapshot_path,
        )
        assert snapshot_path.exists()
        payload = json.loads(snapshot_path.read_text())
        assert payload["window_days"] == 30
        assert len(payload["claims"]) == 2
        assert payload["summary"]["healthy"] == 1
        assert payload["summary"]["na"] == 1

    def test_report_no_drift_message(self, tmp_path):
        from backend.corpus_drift.report import render_markdown
        results = [
            ClaimResult(
                claim_id="code-reviewer.pytest_invoked",
                role_scope="code-reviewer",
                sample_size=10,
                score=0.9,
                score_type="fraction",
                status="healthy",
                evidence="9/10 passing",
            )
        ]
        report_path = tmp_path / "report.md"
        content = render_markdown(
            results=results,
            window_days=30,
            generated_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
            sample_cap=100,
            report_path=report_path,
        )
        assert "No drift detected" in content

    def test_report_drift_finding_present(self, tmp_path):
        from backend.corpus_drift.report import render_markdown
        results = [
            ClaimResult(
                claim_id="executor.two_gate_evidence",
                role_scope="executor",
                sample_size=10,
                score=0.3,
                score_type="fraction",
                status="drift",
                evidence="last failing: PR #99",
            )
        ]
        report_path = tmp_path / "report.md"
        content = render_markdown(
            results=results,
            window_days=30,
            generated_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
            sample_cap=100,
            report_path=report_path,
        )
        assert "DRIFT" in content
        assert "executor.two_gate_evidence" in content
