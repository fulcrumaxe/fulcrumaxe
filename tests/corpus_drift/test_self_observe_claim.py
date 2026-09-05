"""tests/corpus_drift/test_self_observe_claim.py

Unit tests for the self_observe corpus-drift claim.

Key regression guarded here: before the fix, `evaluate()` checked
agent_retros.jsonl rows for a "self_observed" field that the writer never
sets.  With 77+ rows and none having the field the claim always returned
0/N (drift).  After the fix the claim reads transcript AGENT_OUTPUT
envelopes only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.corpus_drift.conftest import write_transcript, _make_assistant_text_turn
from backend.corpus_drift.claims.self_observe import evaluate


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _patch_glob(monkeypatch, paths: list[str]) -> None:
    import backend.corpus_drift.claims.self_observe as _m
    monkeypatch.setattr(_m.glob, "glob", lambda pattern: paths)
    monkeypatch.setattr(_m.os.path, "getmtime", lambda p: 9_999_999_999.0)


# ── Regression: retros.jsonl with no self_observed field must NOT short-circuit ─

class TestRetrosPathIgnored:
    """Verify the claim skips retros.jsonl and scores from transcripts."""

    def test_retros_with_no_self_observed_field_does_not_produce_zero(
        self, tmp_transcript_dir, tmp_path, monkeypatch
    ):
        """
        REGRESSION: before the fix, passing a retros.jsonl with 10 rows (none
        having self_observed) caused evaluate() to return 0/10 without scanning
        transcripts.  After the fix, the retros path is ignored and the score
        comes from envelopes in transcripts.
        """
        import json

        # Build a fake retros.jsonl — 10 rows, zero have self_observed
        retros = tmp_path / "agent-retros.jsonl"
        for i in range(10):
            retros.write_text(
                retros.read_text() if retros.exists() else "",
                encoding="utf-8",
            )
            with retros.open("a") as fh:
                fh.write(json.dumps({
                    "ts": "2026-05-19T10:00:00Z",
                    "agent_id": f"agent-{i:03d}",
                    "role": "executor",
                    "classifier": "git_rm_usage",
                    "trigger": "...",
                    "why": "...",
                    "future_fix": "...",
                    "work_corrected": True,
                    "shadow_mode": False,
                    "turn_idx": i,
                }) + "\n")

        # All 5 transcripts have self_observed=true in their envelopes
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-reg-{i}",
                [_make_assistant_text_turn(_ENVELOPE_WITH)],
            )
            transcripts.append(str(t))

        _patch_glob(monkeypatch, transcripts)

        result = evaluate(
            runs=[],
            transcripts_dir=None,
            window_days=30,
            retros_path=retros,  # supply non-existent-field retros — must be ignored
        )

        # Should reflect transcript reality (1.0), not retros.jsonl (0.0)
        assert result.score == pytest.approx(1.0), (
            f"Expected 1.0 (all transcripts have self_observed), got {result.score}. "
            "The retros.jsonl path was probably not skipped."
        )
        assert result.sample_size == 5
        assert result.status in ("healthy", "watch")


# ── Positive path ─────────────────────────────────────────────────────────────

class TestSelfObservePositive:
    def test_all_envelopes_have_self_observed(self, tmp_transcript_dir, monkeypatch):
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-pos-{i}",
                [_make_assistant_text_turn(_ENVELOPE_WITH)],
            )
            transcripts.append(str(t))

        _patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, retros_path=Path("/nonexistent"))
        assert result.score == pytest.approx(1.0)
        assert result.status in ("healthy", "watch")
        assert result.sample_size == 5

    def test_partial_self_observed(self, tmp_transcript_dir, monkeypatch):
        transcripts = []
        for i in range(4):
            t = write_transcript(
                tmp_transcript_dir, f"run-part-with-{i}",
                [_make_assistant_text_turn(_ENVELOPE_WITH)],
            )
            transcripts.append(str(t))
        for i in range(2):
            t = write_transcript(
                tmp_transcript_dir, f"run-part-without-{i}",
                [_make_assistant_text_turn(_ENVELOPE_WITHOUT)],
            )
            transcripts.append(str(t))

        _patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, retros_path=Path("/nonexistent"))
        assert result.score == pytest.approx(4 / 6, abs=0.01)
        assert result.sample_size == 6


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestSelfObserveEdgeCases:
    def test_no_transcripts_returns_na(self, monkeypatch):
        _patch_glob(monkeypatch, [])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, retros_path=Path("/nonexistent"))
        assert result.status == "n/a"
        assert result.sample_size == 0

    def test_transcripts_without_any_envelope_returns_na(self, tmp_transcript_dir, monkeypatch):
        """Transcripts that emit no AGENT_OUTPUT envelope at all → n/a."""
        transcripts = []
        for i in range(3):
            t = write_transcript(
                tmp_transcript_dir, f"run-noenv-{i}",
                [_make_assistant_text_turn("Just some plain text, no envelope here.")],
            )
            transcripts.append(str(t))

        _patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, retros_path=Path("/nonexistent"))
        assert result.status == "n/a"
        assert result.evidence == "no AGENT_OUTPUT envelopes found in transcripts"

    def test_retros_path_none_falls_through_to_transcripts(self, tmp_transcript_dir, monkeypatch):
        """retros_path=None with no actual retros file on disk → transcripts scanned."""
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-fallthrough-{i}",
                [_make_assistant_text_turn(_ENVELOPE_WITH)],
            )
            transcripts.append(str(t))

        _patch_glob(monkeypatch, transcripts)
        # Pass retros_path=None; the evaluate fn resolves it from env/defaults which won't exist in tmp
        import backend.corpus_drift.claims.self_observe as _m
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", "/nonexistent-state-dir")
        result = evaluate(runs=[], transcripts_dir=None, window_days=30, retros_path=None)
        assert result.score == pytest.approx(1.0)
