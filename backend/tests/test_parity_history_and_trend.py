"""Tests for parity-experiment history persistence and trend RPC.

Coverage:
  - write_parity_history appends one JSON line per call
  - write_parity_history creates the file if absent
  - Multiple calls produce multiple lines (append-only, never rewrite)
  - run_experiment_dry does NOT call write_parity_history
  - run_experiment (live, mocked) DOES call write_parity_history
  - stats_parity_trend.handle returns graceful response when file absent
  - stats_parity_trend.handle returns graceful response for empty file
  - stats_parity_trend.handle returns recent runs with correct shape
  - stats_parity_trend.handle respects the limit param
  - stats_parity_trend.handle skips malformed lines without crashing
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure repo root is on sys.path for absolute imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(role: str = "executor") -> "SpawnSpec":
    from backend.orchestrator.sdk_runner import SpawnSpec
    return SpawnSpec(
        role=role,
        task_prompt=f"Test task for {role}",
        tool_whitelist=["Read"],
        isolation="worktree",
        worktree_path="/tmp/fake-wt",
    )


def _make_run_result(
    role: str = "executor",
    verdict: str = "done",
    input_tokens: int = 100,
    output_tokens: int = 50,
    routed_via: str = "sdk",
):
    from backend.orchestrator.sdk_runner import RunResult
    return RunResult(
        agent_id=f"{routed_via}-test-{role}",
        role=role,
        discussion=None,
        pr=None,
        verdict=verdict,
        final_text="Task complete.",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_calls_count=1,
        prompt_sha256="abc123",
        start_ts="2026-01-01T00:00:00Z",
        end_ts="2026-01-01T00:01:00Z",
        error=None,
        routed_via=routed_via,
    )


def _make_dry_report(roles: list[str] = None):
    """Build a dry ExperimentReport for testing write_parity_history."""
    from backend.orchestrator.parity_experiment import run_experiment_dry
    from backend.orchestrator.sdk_runner import SpawnSpec

    roles = roles or ["executor", "docs-writer"]
    specs = [_make_spec(r) for r in roles]
    return run_experiment_dry(specs)


# ---------------------------------------------------------------------------
# Tests: write_parity_history
# ---------------------------------------------------------------------------

class TestWriteParityHistory:
    """write_parity_history appends correctly and creates the file if absent."""

    def test_appends_one_line(self, tmp_path):
        """One call → one JSON line in the history file."""
        history_file = tmp_path / "parity-history.jsonl"
        report = _make_dry_report(["executor"])

        with patch("backend.orchestrator.parity_experiment.PARITY_HISTORY", history_file):
            from backend.orchestrator.parity_experiment import write_parity_history
            write_parity_history(report)

        lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert "ts" in record
        assert "overall" in record
        assert "per_role" in record

    def test_creates_file_if_absent(self, tmp_path):
        """write_parity_history creates the file when it does not exist."""
        history_file = tmp_path / "subdir" / "parity-history.jsonl"
        assert not history_file.exists()

        report = _make_dry_report(["executor"])
        with patch("backend.orchestrator.parity_experiment.PARITY_HISTORY", history_file):
            from backend.orchestrator.parity_experiment import write_parity_history
            write_parity_history(report)

        assert history_file.exists()

    def test_multiple_calls_append_not_overwrite(self, tmp_path):
        """Three calls → three lines in the file."""
        history_file = tmp_path / "parity-history.jsonl"
        from backend.orchestrator.parity_experiment import write_parity_history

        for _ in range(3):
            report = _make_dry_report(["executor"])
            with patch("backend.orchestrator.parity_experiment.PARITY_HISTORY", history_file):
                write_parity_history(report)

        lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    def test_per_role_summary_shape(self, tmp_path):
        """Each per_role entry has the required summary keys."""
        history_file = tmp_path / "parity-history.jsonl"
        report = _make_dry_report(["executor", "docs-writer"])

        with patch("backend.orchestrator.parity_experiment.PARITY_HISTORY", history_file):
            from backend.orchestrator.parity_experiment import write_parity_history
            write_parity_history(report)

        record = json.loads(history_file.read_text(encoding="utf-8").strip())
        assert len(record["per_role"]) == 2
        required_keys = {
            "role", "sdk_verdict", "cc_verdict", "verdict_agree",
            "token_input_delta", "token_output_delta", "output_similarity",
        }
        for entry in record["per_role"]:
            assert required_keys.issubset(set(entry.keys()))

    def test_dry_mode_does_not_write(self, tmp_path):
        """run_experiment_dry must NOT call write_parity_history."""
        history_file = tmp_path / "parity-history.jsonl"
        specs = [_make_spec("executor")]

        with patch("backend.orchestrator.parity_experiment.PARITY_HISTORY", history_file):
            with patch("backend.orchestrator.parity_experiment.write_parity_history") as mock_write:
                from backend.orchestrator.parity_experiment import run_experiment_dry
                run_experiment_dry(specs)
                mock_write.assert_not_called()

        assert not history_file.exists()

    def test_live_experiment_writes_history(self, tmp_path):
        """run_experiment (mocked) calls write_parity_history once."""
        history_file = tmp_path / "parity-history.jsonl"
        spec = _make_spec("executor")
        sdk_result = _make_run_result(role="executor", verdict="done")
        cc_result = _make_run_result(role="executor", verdict="done", routed_via="cc")

        with patch("backend.orchestrator.parity_experiment.PARITY_HISTORY", history_file):
            with patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False):
                with patch(
                    "backend.orchestrator.parity_experiment.detect_sdk_credential",
                    return_value="oauth_token",
                ):
                    with patch("backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner") as MockRunner:
                        instance = MagicMock()
                        instance.run = AsyncMock(return_value=sdk_result)
                        MockRunner.return_value = instance

                        with patch(
                            "backend.orchestrator.parity_experiment._run_cc_side",
                            return_value=cc_result,
                        ):
                            from backend.orchestrator.parity_experiment import run_experiment
                            run_experiment([spec])

        assert history_file.exists()
        lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Tests: stats_parity_trend RPC
# ---------------------------------------------------------------------------

class TestParityTrendRPC:
    """stats_parity_trend.handle returns trend data or graceful empty responses."""

    def _sample_record(self, role: str = "executor", ts: str = "2026-05-20T12:00:00+00:00") -> dict:
        return {
            "ts": ts,
            "overall": {
                "total_specs": 1,
                "verdict_match_count": 1,
                "verdict_mismatch_count": 0,
                "verdict_match_rate": 1.0,
                "avg_token_input_delta": 50.0,
                "avg_token_output_delta": 10.0,
                "avg_output_similarity": 0.95,
            },
            "per_role": [
                {
                    "role": role,
                    "sdk_verdict": "done",
                    "cc_verdict": "done",
                    "verdict_agree": True,
                    "token_input_delta": 50,
                    "token_output_delta": 10,
                    "output_similarity": 0.95,
                }
            ],
        }

    def test_graceful_when_file_absent(self, tmp_path):
        """Returns empty runs list when history file does not exist."""
        missing = tmp_path / "no-such-file.jsonl"
        with patch("backend.rpc.stats_parity_trend.PARITY_HISTORY", missing):
            from backend.rpc import stats_parity_trend
            result = stats_parity_trend.handle({})

        assert result["runs"] == []
        assert result["total_runs"] == 0

    def test_graceful_when_file_empty(self, tmp_path):
        """Returns empty runs list when history file exists but is empty."""
        empty_file = tmp_path / "parity-history.jsonl"
        empty_file.write_text("", encoding="utf-8")

        with patch("backend.rpc.stats_parity_trend.PARITY_HISTORY", empty_file):
            from backend.rpc import stats_parity_trend
            result = stats_parity_trend.handle({})

        assert result["runs"] == []
        assert result["total_runs"] == 0

    def test_returns_runs_with_correct_shape(self, tmp_path):
        """Returns run records with ts, overall, and per_role keys."""
        history_file = tmp_path / "parity-history.jsonl"
        record = self._sample_record()
        history_file.write_text(json.dumps(record) + "\n", encoding="utf-8")

        with patch("backend.rpc.stats_parity_trend.PARITY_HISTORY", history_file):
            from backend.rpc import stats_parity_trend
            result = stats_parity_trend.handle({})

        assert result["total_runs"] == 1
        assert len(result["runs"]) == 1
        run = result["runs"][0]
        assert "ts" in run
        assert "overall" in run
        assert "per_role" in run
        assert run["per_role"][0]["role"] == "executor"

    def test_returns_last_n_runs_by_limit(self, tmp_path):
        """Limit param trims result to the N most recent runs."""
        history_file = tmp_path / "parity-history.jsonl"
        lines = []
        for i in range(10):
            lines.append(json.dumps(self._sample_record(ts=f"2026-05-20T{i:02d}:00:00+00:00")))
        history_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with patch("backend.rpc.stats_parity_trend.PARITY_HISTORY", history_file):
            from backend.rpc import stats_parity_trend
            result = stats_parity_trend.handle({"limit": 3})

        assert result["total_runs"] == 10
        assert len(result["runs"]) == 3
        # Should be the last 3 (most recent)
        assert result["runs"][0]["ts"] == "2026-05-20T07:00:00+00:00"
        assert result["runs"][-1]["ts"] == "2026-05-20T09:00:00+00:00"

    def test_skips_malformed_lines(self, tmp_path):
        """Malformed JSON lines are skipped; valid lines are returned."""
        history_file = tmp_path / "parity-history.jsonl"
        good = json.dumps(self._sample_record())
        history_file.write_text(
            good + "\n" + "NOT_JSON\n" + good + "\n",
            encoding="utf-8",
        )

        with patch("backend.rpc.stats_parity_trend.PARITY_HISTORY", history_file):
            from backend.rpc import stats_parity_trend
            result = stats_parity_trend.handle({})

        assert result["total_runs"] == 2
        assert len(result["runs"]) == 2

    def test_includes_history_path_in_response(self, tmp_path):
        """Response always includes the history_path field."""
        missing = tmp_path / "parity-history.jsonl"
        with patch("backend.rpc.stats_parity_trend.PARITY_HISTORY", missing):
            from backend.rpc import stats_parity_trend
            result = stats_parity_trend.handle({})

        assert "history_path" in result
        assert str(missing) in result["history_path"]
