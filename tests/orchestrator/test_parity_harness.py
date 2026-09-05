"""tests/orchestrator/test_parity_harness.py — Parity harness unit tests.

All tests use MOCK / FIXTURE RunResult objects — NO real Anthropic API calls
are made, not even conditionally. The live guard test verifies the guard fires
correctly without actually reaching the SDK.

Design contract (verified by these tests):
  - compare_run() produces a correctly structured ParityDiff from two RunResults
  - parity_report() aggregates diffs correctly (verdict match rate, deltas, similarity)
  - Mismatch detection: mismatched verdicts are flagged in the diff
  - Empty set: parity_report([]) returns a zero-count report with 0.0 averages
  - NO real SDK call can happen in the default test path (guard is enforced)
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from backend.orchestrator.parity_harness import (
    ParityDiff,
    ParityLiveGuardError,
    ParityReport,
    _check_live_opt_in,
    _jaccard_similarity,
    compare_run,
    parity_report,
)
from backend.orchestrator.sdk_runner import RunResult, SpawnSpec


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_result(
    verdict: str = "pass",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    tool_calls_count: int = 3,
    final_text: str = "Good code. AGENT_OUTPUT done.",
    agent_id: str = "code-reviewer-101-999",
    role: str = "code-reviewer",
    discussion: int = 101,
    error: str | None = None,
) -> RunResult:
    return RunResult(
        agent_id=agent_id,
        role=role,
        discussion=discussion,
        pr=None,
        verdict=verdict,
        final_text=final_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_calls_count=tool_calls_count,
        prompt_sha256="abc123",
        start_ts="2026-05-20T00:00:00Z",
        end_ts="2026-05-20T00:01:00Z",
        error=error,
    )


# ---------------------------------------------------------------------------
# Jaccard similarity
# ---------------------------------------------------------------------------


class TestJaccardSimilarity:
    def test_identical_texts_return_1(self):
        assert _jaccard_similarity("hello world", "hello world") == 1.0

    def test_completely_different_texts_return_0(self):
        assert _jaccard_similarity("hello world", "foo bar baz") == 0.0

    def test_partial_overlap_between_0_and_1(self):
        sim = _jaccard_similarity("the quick brown fox", "the lazy brown dog")
        assert 0.0 < sim < 1.0

    def test_empty_strings_return_1(self):
        assert _jaccard_similarity("", "") == 1.0

    def test_one_empty_one_not_return_0(self):
        assert _jaccard_similarity("hello", "") == 0.0

    def test_case_insensitive(self):
        assert _jaccard_similarity("Hello World", "hello world") == 1.0


# ---------------------------------------------------------------------------
# compare_run — verdict match
# ---------------------------------------------------------------------------


class TestCompareRunVerdictMatch:
    def test_matching_verdicts_flagged_true(self):
        sdk = _make_result(verdict="pass")
        cc = _make_result(verdict="pass", agent_id="cc-101-888")
        diff = compare_run(sdk, cc, spec_label="discussion-101")

        assert diff.verdict_match is True
        assert diff.sdk_verdict == "pass"
        assert diff.cc_verdict == "pass"

    def test_mismatched_verdicts_flagged_false(self):
        sdk = _make_result(verdict="pass")
        cc = _make_result(verdict="fail", agent_id="cc-101-888")
        diff = compare_run(sdk, cc, spec_label="discussion-101-mismatch")

        assert diff.verdict_match is False
        assert diff.sdk_verdict == "pass"
        assert diff.cc_verdict == "fail"

    def test_spec_label_propagated(self):
        sdk = _make_result()
        cc = _make_result(agent_id="cc-999")
        diff = compare_run(sdk, cc, spec_label="my-custom-label")
        assert diff.spec_label == "my-custom-label"

    def test_spec_label_defaults_to_agent_id(self):
        sdk = _make_result(agent_id="code-reviewer-101-1234")
        cc = _make_result(agent_id="cc-101-5678")
        diff = compare_run(sdk, cc)  # no spec_label
        assert diff.spec_label == "code-reviewer-101-1234"


# ---------------------------------------------------------------------------
# compare_run — token deltas
# ---------------------------------------------------------------------------


class TestCompareRunTokenDeltas:
    def test_positive_delta_when_sdk_uses_more_tokens(self):
        sdk = _make_result(input_tokens=2000, output_tokens=800)
        cc = _make_result(input_tokens=1000, output_tokens=400, agent_id="cc-x")
        diff = compare_run(sdk, cc)

        assert diff.token_input_delta == 1000   # 2000 - 1000
        assert diff.token_output_delta == 400   # 800 - 400

    def test_negative_delta_when_cc_uses_more_tokens(self):
        sdk = _make_result(input_tokens=500, output_tokens=200)
        cc = _make_result(input_tokens=1500, output_tokens=600, agent_id="cc-x")
        diff = compare_run(sdk, cc)

        assert diff.token_input_delta == -1000  # 500 - 1500
        assert diff.token_output_delta == -400  # 200 - 600

    def test_zero_delta_when_tokens_equal(self):
        sdk = _make_result(input_tokens=1000, output_tokens=500)
        cc = _make_result(input_tokens=1000, output_tokens=500, agent_id="cc-x")
        diff = compare_run(sdk, cc)

        assert diff.token_input_delta == 0
        assert diff.token_output_delta == 0

    def test_raw_token_counts_preserved(self):
        sdk = _make_result(input_tokens=1234, output_tokens=567)
        cc = _make_result(input_tokens=100, output_tokens=50, agent_id="cc-y")
        diff = compare_run(sdk, cc)

        assert diff.sdk_input_tokens == 1234
        assert diff.cc_input_tokens == 100
        assert diff.sdk_output_tokens == 567
        assert diff.cc_output_tokens == 50


# ---------------------------------------------------------------------------
# compare_run — tool call delta
# ---------------------------------------------------------------------------


class TestCompareRunToolCallDelta:
    def test_tool_call_delta_computed_correctly(self):
        sdk = _make_result(tool_calls_count=5)
        cc = _make_result(tool_calls_count=3, agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert diff.tool_call_delta == 2  # 5 - 3

    def test_tool_call_delta_negative(self):
        sdk = _make_result(tool_calls_count=1)
        cc = _make_result(tool_calls_count=7, agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert diff.tool_call_delta == -6

    def test_raw_tool_counts_preserved(self):
        sdk = _make_result(tool_calls_count=4)
        cc = _make_result(tool_calls_count=9, agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert diff.sdk_tool_calls == 4
        assert diff.cc_tool_calls == 9


# ---------------------------------------------------------------------------
# compare_run — output similarity
# ---------------------------------------------------------------------------


class TestCompareRunOutputSimilarity:
    def test_identical_outputs_have_similarity_1(self):
        text = "LGTM. Great code. No issues found."
        sdk = _make_result(final_text=text)
        cc = _make_result(final_text=text, agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert diff.output_similarity == 1.0

    def test_completely_different_outputs_have_similarity_0(self):
        sdk = _make_result(final_text="alpha beta gamma delta")
        cc = _make_result(final_text="zulu yankee xray whiskey", agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert diff.output_similarity == 0.0

    def test_partial_overlap_similarity_between_0_and_1(self):
        sdk = _make_result(final_text="the code looks good overall")
        cc = _make_result(final_text="the code has some issues overall", agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert 0.0 < diff.output_similarity < 1.0

    def test_similarity_is_between_0_and_1(self):
        sdk = _make_result(final_text="verdict pass token count normal")
        cc = _make_result(final_text="verdict fail token count high", agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert 0.0 <= diff.output_similarity <= 1.0


# ---------------------------------------------------------------------------
# compare_run — error propagation
# ---------------------------------------------------------------------------


class TestCompareRunErrors:
    def test_sdk_error_propagated(self):
        sdk = _make_result(error="connection timeout")
        cc = _make_result(error=None, agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert diff.sdk_error == "connection timeout"
        assert diff.cc_error is None

    def test_cc_error_propagated(self):
        sdk = _make_result(error=None)
        cc = _make_result(error="rate limit exceeded", agent_id="cc-x")
        diff = compare_run(sdk, cc)
        assert diff.cc_error == "rate limit exceeded"
        assert diff.sdk_error is None


# ---------------------------------------------------------------------------
# parity_report — aggregation
# ---------------------------------------------------------------------------


class TestParityReport:
    def _make_matching_diff(self, label: str = "d-1") -> ParityDiff:
        sdk = _make_result(verdict="pass", input_tokens=1000, output_tokens=500, tool_calls_count=3)
        cc = _make_result(verdict="pass", input_tokens=800, output_tokens=400, tool_calls_count=2, agent_id="cc-x")
        return compare_run(sdk, cc, spec_label=label)

    def _make_mismatching_diff(self, label: str = "d-2") -> ParityDiff:
        sdk = _make_result(verdict="pass", input_tokens=2000, output_tokens=600, tool_calls_count=5)
        cc = _make_result(verdict="fail", input_tokens=500, output_tokens=200, tool_calls_count=1, agent_id="cc-y")
        return compare_run(sdk, cc, spec_label=label)

    def test_empty_list_returns_zero_report(self):
        report = parity_report([])
        assert report.total_specs == 0
        assert report.verdict_match_count == 0
        assert report.verdict_mismatch_count == 0
        assert report.avg_token_input_delta == 0.0
        assert report.avg_token_output_delta == 0.0
        assert report.avg_tool_call_delta == 0.0
        assert report.avg_output_similarity == 0.0
        assert report.diffs == []

    def test_all_matching_report(self):
        diffs = [self._make_matching_diff(f"d-{i}") for i in range(3)]
        report = parity_report(diffs)
        assert report.total_specs == 3
        assert report.verdict_match_count == 3
        assert report.verdict_mismatch_count == 0
        assert report.verdict_match_rate == 1.0

    def test_all_mismatching_report(self):
        diffs = [self._make_mismatching_diff(f"d-{i}") for i in range(2)]
        report = parity_report(diffs)
        assert report.total_specs == 2
        assert report.verdict_match_count == 0
        assert report.verdict_mismatch_count == 2
        assert report.verdict_match_rate == 0.0

    def test_mixed_match_mismatch(self):
        diffs = [
            self._make_matching_diff("d-1"),
            self._make_mismatching_diff("d-2"),
            self._make_matching_diff("d-3"),
        ]
        report = parity_report(diffs)
        assert report.total_specs == 3
        assert report.verdict_match_count == 2
        assert report.verdict_mismatch_count == 1
        assert abs(report.verdict_match_rate - 2 / 3) < 0.001

    def test_avg_token_input_delta_computed(self):
        # sdk=1000, cc=800 → delta=200 for each of 3 diffs → avg=200
        diffs = [self._make_matching_diff(f"d-{i}") for i in range(3)]
        report = parity_report(diffs)
        assert report.avg_token_input_delta == 200.0  # 1000-800

    def test_avg_tool_call_delta_computed(self):
        # sdk=3, cc=2 → delta=1 for each of 3 → avg=1
        diffs = [self._make_matching_diff(f"d-{i}") for i in range(3)]
        report = parity_report(diffs)
        assert report.avg_tool_call_delta == 1.0

    def test_diffs_list_included_in_report(self):
        diffs = [self._make_matching_diff("d-1"), self._make_mismatching_diff("d-2")]
        report = parity_report(diffs)
        assert len(report.diffs) == 2

    def test_to_dict_serializable(self):
        """parity_report().to_dict() must be JSON-serializable."""
        import json
        diffs = [self._make_matching_diff("d-1")]
        report = parity_report(diffs)
        # Should not raise
        serialized = json.dumps(report.to_dict())
        parsed = json.loads(serialized)
        assert parsed["total_specs"] == 1
        assert "diffs" in parsed


# ---------------------------------------------------------------------------
# Live guard — ensure no real SDK call can happen without opt-in
# ---------------------------------------------------------------------------


class TestLiveGuard:
    def test_guard_raises_without_run_sdk_parity_env(self):
        """The live guard must raise if RUN_SDK_PARITY is not set."""
        env_without_opt_in = {k: v for k, v in os.environ.items() if k != "RUN_SDK_PARITY"}
        with patch.dict(os.environ, env_without_opt_in, clear=True):
            # Ensure RUN_SDK_PARITY is definitely absent
            os.environ.pop("RUN_SDK_PARITY", None)
            with pytest.raises(ParityLiveGuardError, match="RUN_SDK_PARITY=1"):
                _check_live_opt_in()

    def test_guard_raises_with_wrong_value(self):
        """RUN_SDK_PARITY=0 must still block live calls."""
        with patch.dict(os.environ, {"RUN_SDK_PARITY": "0"}, clear=False):
            with pytest.raises(ParityLiveGuardError):
                _check_live_opt_in()

    def test_guard_raises_without_api_key_even_with_env(self):
        """Setting RUN_SDK_PARITY=1 alone is not enough — API key must also be present."""
        from pathlib import Path as _Path
        from unittest.mock import MagicMock

        with patch.dict(
            os.environ,
            {"RUN_SDK_PARITY": "1"},
            clear=False,
        ):
            # Remove ANTHROPIC_API_KEY from env
            os.environ.pop("ANTHROPIC_API_KEY", None)
            # Patch Path.home() to return a path with no .anthropic/credentials
            fake_creds = MagicMock()
            fake_creds.exists.return_value = False
            with patch("backend.orchestrator.parity_harness.Path") as mock_path_cls:
                mock_path_cls.home.return_value.__truediv__.return_value.__truediv__.return_value = fake_creds
                with pytest.raises(ParityLiveGuardError, match="ANTHROPIC_API_KEY"):
                    _check_live_opt_in()

    def test_guard_passes_with_both_conditions(self, tmp_path):
        """Guard should not raise when RUN_SDK_PARITY=1 AND ANTHROPIC_API_KEY is set."""
        with patch.dict(
            os.environ,
            {"RUN_SDK_PARITY": "1", "ANTHROPIC_API_KEY": "sk-ant-test-fake"},
            clear=False,
        ):
            # Should not raise
            _check_live_opt_in()

    def test_no_real_sdk_call_in_compare_run(self):
        """compare_run() is a pure comparison — calling it never touches the SDK."""
        sdk_result = _make_result(verdict="pass")
        cc_result = _make_result(verdict="pass", agent_id="cc-x")

        # No mock needed — compare_run is purely computational
        # If this calls the SDK, it would fail with ImportError or RuntimeError
        diff = compare_run(sdk_result, cc_result)

        # Verify result is correct without any network interaction
        assert diff.verdict_match is True
        assert isinstance(diff.output_similarity, float)

    def test_no_real_sdk_call_in_parity_report(self):
        """parity_report() is a pure aggregation — never calls the SDK."""
        diffs = [
            compare_run(
                _make_result(verdict="pass"),
                _make_result(verdict="pass", agent_id="cc-x"),
                spec_label="d-1",
            )
        ]
        report = parity_report(diffs)
        assert report.total_specs == 1
