"""tests/corpus_drift/test_scrubber_bypass_absent.py

Unit tests for the executor.scrubber_bypass_absent claim.
Uses synthetic transcript fixtures to inject clean and dirty text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.corpus_drift.conftest import write_transcript, _make_transcript, _make_assistant_text_turn
from backend.corpus_drift.claims.scrubber_bypass_absent import evaluate, CLAIM_ID, ROLE_SCOPE


def _bash_tc(command: str) -> dict:
    return {"name": "Bash", "input": {"command": command}}


class TestScrubberBypassAbsent:
    @staticmethod
    def _patch_glob(monkeypatch, paths):
        import backend.corpus_drift.claims.scrubber_bypass_absent as _m
        monkeypatch.setattr(_m.glob, "glob", lambda pattern: paths)
        monkeypatch.setattr(_m.os.path, "getmtime", lambda p: 9999999999.0)

    def test_clean_transcripts_score_100(self, tmp_transcript_dir, monkeypatch):
        """Transcripts with no secret patterns score 100%."""
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-clean-{i}",
                [_make_transcript([_bash_tc("git status")])]
            )
            transcripts.append(str(t))

        self._patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.claim_id == CLAIM_ID
        assert result.role_scope == ROLE_SCOPE
        assert result.score == pytest.approx(1.0)
        assert result.status == "healthy"

    def test_gh_token_detected(self, tmp_transcript_dir, monkeypatch):
        """Transcript containing a raw ghp_ token is flagged."""
        dirty = write_transcript(
            tmp_transcript_dir, "run-dirty-ghp",
            [_make_transcript([_bash_tc("curl -H 'Authorization: Bearer ghp_abc1234567890xyz' https://api.github.com")])]
        )
        # Need enough transcripts to exceed min_sample
        transcripts = [str(dirty)]
        for i in range(4):
            clean = write_transcript(
                tmp_transcript_dir, f"run-clean-ghp-{i}",
                [_make_transcript([_bash_tc("echo hello")])]
            )
            transcripts.append(str(clean))

        self._patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        # 4 clean, 1 dirty out of 5 — score < 1.0, at least one transcript flagged
        assert result.score == pytest.approx(4 / 5)
        assert result.score < 1.0

    def test_sk_ant_key_detected(self, tmp_transcript_dir, monkeypatch):
        """Transcript containing a raw sk-ant- key is flagged."""
        dirty = write_transcript(
            tmp_transcript_dir, "run-dirty-sk",
            [_make_assistant_text_turn("My API key is sk-ant-api03-someLongKey1234567890")]
        )
        transcripts = [str(dirty)]
        for i in range(4):
            clean = write_transcript(
                tmp_transcript_dir, f"run-clean-sk-{i}",
                [_make_assistant_text_turn("Normal output here")]
            )
            transcripts.append(str(clean))

        self._patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(4 / 5)

    def test_url_credentials_detected(self, tmp_transcript_dir, monkeypatch):
        """Transcript containing URL credentials (https://user:pass@host) is flagged."""
        dirty = write_transcript(
            tmp_transcript_dir, "run-dirty-url",
            [_make_transcript([_bash_tc("git clone https://user:secrettoken@github.com/repo.git")])]
        )
        transcripts = [str(dirty)]
        for i in range(4):
            clean = write_transcript(
                tmp_transcript_dir, f"run-clean-url-{i}",
                [_make_transcript([_bash_tc("git clone https://github.com/repo.git")])]
            )
            transcripts.append(str(clean))

        self._patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(4 / 5)

    def test_auth_header_detected(self, tmp_transcript_dir, monkeypatch):
        """Authorization: Bearer <token> pattern is flagged."""
        dirty = write_transcript(
            tmp_transcript_dir, "run-dirty-auth",
            [_make_transcript([_bash_tc("curl -H 'Authorization: Bearer supersecrettoken123'")])]
        )
        transcripts = [str(dirty)]
        for i in range(4):
            clean = write_transcript(
                tmp_transcript_dir, f"run-clean-auth-{i}",
                [_make_transcript([_bash_tc("curl https://example.com")])]
            )
            transcripts.append(str(clean))

        self._patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(4 / 5)

    def test_no_transcripts_returns_na(self, monkeypatch):
        """No transcripts in window → n/a."""
        self._patch_glob(monkeypatch, [])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.status == "n/a"

    def test_score_type_is_fraction(self, tmp_transcript_dir, monkeypatch):
        """Claim uses score_type='fraction'."""
        transcripts = []
        for i in range(5):
            t = write_transcript(
                tmp_transcript_dir, f"run-st-{i}",
                [_make_transcript([_bash_tc("echo ok")])]
            )
            transcripts.append(str(t))
        self._patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score_type == "fraction"

    def test_all_dirty_is_drift(self, tmp_transcript_dir, monkeypatch):
        """All transcripts with secrets → drift."""
        transcripts = []
        for i in range(6):
            t = write_transcript(
                tmp_transcript_dir, f"run-all-dirty-{i}",
                [_make_transcript([_bash_tc(f"echo GH_TOKEN=ghp_abc1234567890xyz{i}")])]
            )
            transcripts.append(str(t))
        self._patch_glob(monkeypatch, transcripts)
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == pytest.approx(0.0)
        assert result.status == "drift"
