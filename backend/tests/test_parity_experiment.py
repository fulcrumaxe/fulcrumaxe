"""Tests for backend/orchestrator/parity_experiment.py.

All tests mock real calls (ClaudeAgentSDKRunner.run and the claude -p subprocess).
No real Anthropic API calls, no OAuth token, no network access.

Coverage:
  - run_role_parity: live guard raises without env var, produces ParityDiff when mocked
  - run_experiment: aggregates per-role + overall across multiple specs
  - Live guard: skips without RUN_SDK_PARITY=1, also checks credential requirement
  - Verdict agreement + token-delta computed correctly
  - run_experiment_dry: builds report without any real calls (smoke test)
  - CLI --dry mode: prints report, exits 0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Ensure repo root is on the path for absolute imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# ---------------------------------------------------------------------------
# Helpers: build canned RunResults
# ---------------------------------------------------------------------------

def _make_run_result(
    role: str = "executor",
    verdict: str = "done",
    final_text: str = "Task complete.",
    input_tokens: int = 100,
    output_tokens: int = 50,
    tool_calls_count: int = 2,
    error: str | None = None,
    routed_via: str = "sdk",
):
    from backend.orchestrator.sdk_runner import RunResult
    return RunResult(
        agent_id=f"{routed_via}-test-{role}",
        role=role,
        discussion=None,
        pr=None,
        verdict=verdict,
        final_text=final_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_calls_count=tool_calls_count,
        prompt_sha256="abc123",
        start_ts="2026-01-01T00:00:00Z",
        end_ts="2026-01-01T00:01:00Z",
        error=error,
        routed_via=routed_via,
    )


def _make_spec(role: str = "executor") -> "SpawnSpec":
    from backend.orchestrator.sdk_runner import SpawnSpec
    return SpawnSpec(
        role=role,
        task_prompt=f"Test task for {role}",
        tool_whitelist=["Read"],
        isolation="worktree",
        worktree_path="/tmp/fake-wt",
    )


# ---------------------------------------------------------------------------
# Tests: live guard
# ---------------------------------------------------------------------------

class TestLiveGuard:
    """Live guard must refuse to run without both opt-in conditions."""

    def test_raises_without_run_sdk_parity_env(self):
        """No RUN_SDK_PARITY=1 → ParityLiveGuardError."""
        from backend.orchestrator.parity_harness import ParityLiveGuardError
        from backend.orchestrator.parity_experiment import _check_experiment_opt_in

        env = {k: v for k, v in os.environ.items() if k not in ("RUN_SDK_PARITY",)}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ParityLiveGuardError, match="RUN_SDK_PARITY=1"):
                _check_experiment_opt_in()

    def test_raises_without_subscription_credential(self):
        """RUN_SDK_PARITY=1 but no subscription credential → ParityLiveGuardError."""
        from backend.orchestrator.parity_harness import ParityLiveGuardError
        from backend.orchestrator.parity_experiment import _check_experiment_opt_in

        # Patch detect_sdk_credential to return None
        with patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False):
            with patch(
                "backend.orchestrator.parity_experiment.detect_sdk_credential",
                return_value=None,
            ):
                with pytest.raises(ParityLiveGuardError, match="Subscription login"):
                    _check_experiment_opt_in()

    def test_raises_with_api_key_only(self):
        """RUN_SDK_PARITY=1 + api_key credential → ParityLiveGuardError (wrong billing mode)."""
        from backend.orchestrator.parity_harness import ParityLiveGuardError
        from backend.orchestrator.parity_experiment import _check_experiment_opt_in

        with patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False):
            with patch(
                "backend.orchestrator.parity_experiment.detect_sdk_credential",
                return_value="api_key",
            ):
                with pytest.raises(ParityLiveGuardError, match="api_key"):
                    _check_experiment_opt_in()

    def test_passes_with_oauth_token(self):
        """RUN_SDK_PARITY=1 + oauth_token → no exception."""
        from backend.orchestrator.parity_experiment import _check_experiment_opt_in

        with patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False):
            with patch(
                "backend.orchestrator.parity_experiment.detect_sdk_credential",
                return_value="oauth_token",
            ):
                _check_experiment_opt_in()  # must not raise

    def test_passes_with_login(self):
        """RUN_SDK_PARITY=1 + login → no exception."""
        from backend.orchestrator.parity_experiment import _check_experiment_opt_in

        with patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False):
            with patch(
                "backend.orchestrator.parity_experiment.detect_sdk_credential",
                return_value="login",
            ):
                _check_experiment_opt_in()  # must not raise


# ---------------------------------------------------------------------------
# Tests: run_role_parity
# ---------------------------------------------------------------------------

class TestRunRoleParity:
    """run_role_parity must produce a ParityDiff when both sides are mocked."""

    def _mock_env(self):
        """Return a dict-patch that satisfies the live guard."""
        return patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False)

    def _mock_credential(self):
        return patch(
            "backend.orchestrator.parity_experiment.detect_sdk_credential",
            return_value="oauth_token",
        )

    def test_returns_parity_diff(self):
        """run_role_parity returns a ParityDiff with matching fields."""
        from backend.orchestrator.parity_experiment import run_role_parity
        from backend.orchestrator.parity_harness import ParityDiff

        spec = _make_spec("code-reviewer")
        sdk_result = _make_run_result(role="code-reviewer", verdict="pass", input_tokens=200, output_tokens=80)
        cc_result = _make_run_result(role="code-reviewer", verdict="pass", input_tokens=150, output_tokens=60, routed_via="cc")

        with self._mock_env(), self._mock_credential():
            with patch(
                "backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner",
            ) as MockRunner:
                runner_instance = MagicMock()
                runner_instance.run = AsyncMock(return_value=sdk_result)
                MockRunner.return_value = runner_instance

                with patch(
                    "backend.orchestrator.parity_experiment._run_cc_side",
                    return_value=cc_result,
                ):
                    diff = run_role_parity(spec)

        assert isinstance(diff, ParityDiff)
        assert diff.spec_label == "code-reviewer"
        assert diff.sdk_verdict == "pass"
        assert diff.cc_verdict == "pass"
        assert diff.verdict_match is True

    def test_token_delta_computed_correctly(self):
        """token_input_delta == sdk_input_tokens - cc_input_tokens."""
        from backend.orchestrator.parity_experiment import run_role_parity

        spec = _make_spec("executor")
        sdk_result = _make_run_result(input_tokens=300, output_tokens=100, verdict="done")
        cc_result = _make_run_result(input_tokens=180, output_tokens=70, verdict="done", routed_via="cc")

        with self._mock_env(), self._mock_credential():
            with patch("backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner") as MockRunner:
                instance = MagicMock()
                instance.run = AsyncMock(return_value=sdk_result)
                MockRunner.return_value = instance

                with patch("backend.orchestrator.parity_experiment._run_cc_side", return_value=cc_result):
                    diff = run_role_parity(spec)

        assert diff.token_input_delta == 300 - 180  # 120
        assert diff.token_output_delta == 100 - 70   # 30

    def test_verdict_mismatch_captured(self):
        """verdict_match is False when SDK and CC produce different verdicts."""
        from backend.orchestrator.parity_experiment import run_role_parity

        spec = _make_spec("security-reviewer")
        sdk_result = _make_run_result(verdict="pass")
        cc_result = _make_run_result(verdict="needs-fix", routed_via="cc")

        with self._mock_env(), self._mock_credential():
            with patch("backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner") as MockRunner:
                instance = MagicMock()
                instance.run = AsyncMock(return_value=sdk_result)
                MockRunner.return_value = instance

                with patch("backend.orchestrator.parity_experiment._run_cc_side", return_value=cc_result):
                    diff = run_role_parity(spec)

        assert diff.verdict_match is False
        assert diff.sdk_verdict == "pass"
        assert diff.cc_verdict == "needs-fix"

    def test_output_similarity_identical_text(self):
        """Identical final_text → output_similarity == 1.0."""
        from backend.orchestrator.parity_experiment import run_role_parity

        text = "verdict done task complete"
        spec = _make_spec("docs-writer")
        sdk_result = _make_run_result(final_text=text, verdict="done")
        cc_result = _make_run_result(final_text=text, verdict="done", routed_via="cc")

        with self._mock_env(), self._mock_credential():
            with patch("backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner") as MockRunner:
                instance = MagicMock()
                instance.run = AsyncMock(return_value=sdk_result)
                MockRunner.return_value = instance

                with patch("backend.orchestrator.parity_experiment._run_cc_side", return_value=cc_result):
                    diff = run_role_parity(spec)

        assert diff.output_similarity == pytest.approx(1.0)

    def test_raises_live_guard_without_env(self):
        """run_role_parity raises ParityLiveGuardError when opt-in missing."""
        from backend.orchestrator.parity_harness import ParityLiveGuardError
        from backend.orchestrator.parity_experiment import run_role_parity

        spec = _make_spec()
        env = {k: v for k, v in os.environ.items() if k not in ("RUN_SDK_PARITY",)}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ParityLiveGuardError):
                run_role_parity(spec)


# ---------------------------------------------------------------------------
# Tests: run_experiment
# ---------------------------------------------------------------------------

class TestRunExperiment:
    """run_experiment must aggregate per-role + overall report correctly."""

    def _mock_env_and_cred(self):
        return (
            patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False),
            patch(
                "backend.orchestrator.parity_experiment.detect_sdk_credential",
                return_value="login",
            ),
        )

    def test_aggregates_multiple_specs(self):
        """run_experiment returns one per_role entry per spec + overall ParityReport."""
        from backend.orchestrator.parity_experiment import run_experiment, ExperimentReport
        from backend.orchestrator.parity_harness import ParityReport

        specs = [_make_spec("executor"), _make_spec("docs-writer"), _make_spec("quality-sweep")]

        sdk_results = [
            _make_run_result(role="executor", verdict="done", input_tokens=100, output_tokens=50),
            _make_run_result(role="docs-writer", verdict="done", input_tokens=80, output_tokens=40),
            _make_run_result(role="quality-sweep", verdict="pass", input_tokens=60, output_tokens=30),
        ]
        cc_results = [
            _make_run_result(role="executor", verdict="done", input_tokens=90, output_tokens=45, routed_via="cc"),
            _make_run_result(role="docs-writer", verdict="done", input_tokens=70, output_tokens=35, routed_via="cc"),
            _make_run_result(role="quality-sweep", verdict="pass", input_tokens=55, output_tokens=25, routed_via="cc"),
        ]

        call_idx = [0]

        def sdk_run_side_effect(spec_arg):
            idx = call_idx[0]
            call_idx[0] += 1
            return sdk_results[idx]

        cc_call_idx = [0]

        def cc_side_effect(spec_arg):
            idx = cc_call_idx[0]
            cc_call_idx[0] += 1
            return cc_results[idx]

        env_ctx, cred_ctx = self._mock_env_and_cred()
        with env_ctx, cred_ctx:
            with patch("backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner") as MockRunner:
                instance = MagicMock()
                instance.run = AsyncMock(side_effect=sdk_run_side_effect)
                MockRunner.return_value = instance

                with patch("backend.orchestrator.parity_experiment._run_cc_side", side_effect=cc_side_effect):
                    report = run_experiment(specs)

        assert isinstance(report, ExperimentReport)
        assert isinstance(report.parity, ParityReport)
        assert report.parity.total_specs == 3
        assert len(report.per_role) == 3

        # All verdicts agree → match_count == 3
        assert report.parity.verdict_match_count == 3
        assert report.parity.verdict_mismatch_count == 0

    def test_per_role_keys_present(self):
        """Each per_role entry has required keys."""
        from backend.orchestrator.parity_experiment import run_experiment

        spec = _make_spec("mission-analyst")
        sdk_res = _make_run_result(role="mission-analyst", verdict="done")
        cc_res = _make_run_result(role="mission-analyst", verdict="done", routed_via="cc")

        env_ctx, cred_ctx = self._mock_env_and_cred()
        with env_ctx, cred_ctx:
            with patch("backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner") as MockRunner:
                instance = MagicMock()
                instance.run = AsyncMock(return_value=sdk_res)
                MockRunner.return_value = instance

                with patch("backend.orchestrator.parity_experiment._run_cc_side", return_value=cc_res):
                    report = run_experiment([spec])

        required_keys = {
            "role", "spec_label", "sdk_verdict", "cc_verdict", "verdict_agree",
            "token_input_delta", "token_output_delta", "sdk_tool_calls",
            "cc_tool_calls", "output_similarity", "sdk_error", "cc_error",
        }
        assert required_keys.issubset(set(report.per_role[0].keys()))

    def test_raises_live_guard_without_env(self):
        """run_experiment raises ParityLiveGuardError when opt-in missing."""
        from backend.orchestrator.parity_harness import ParityLiveGuardError
        from backend.orchestrator.parity_experiment import run_experiment

        spec = _make_spec()
        env = {k: v for k, v in os.environ.items() if k not in ("RUN_SDK_PARITY",)}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ParityLiveGuardError):
                run_experiment([spec])


# ---------------------------------------------------------------------------
# Tests: run_experiment_dry
# ---------------------------------------------------------------------------

class TestRunExperimentDry:
    """run_experiment_dry builds a report without any real calls."""

    def test_dry_produces_report_with_correct_total(self):
        """dry run over 3 specs → total_specs == 3, no real call made."""
        from backend.orchestrator.parity_experiment import run_experiment_dry, ExperimentReport

        specs = [_make_spec("executor"), _make_spec("docs-writer"), _make_spec("mission-analyst")]
        report = run_experiment_dry(specs)

        assert isinstance(report, ExperimentReport)
        assert report.parity.total_specs == 3
        assert len(report.per_role) == 3

    def test_dry_verdicts_are_stubs(self):
        """Dry mode marks both verdicts as *_dry_stub."""
        from backend.orchestrator.parity_experiment import run_experiment_dry

        report = run_experiment_dry([_make_spec("code-reviewer")])
        entry = report.per_role[0]
        assert "dry_stub" in entry["sdk_verdict"]
        assert "dry_stub" in entry["cc_verdict"]

    def test_dry_to_dict_serializable(self):
        """ExperimentReport.to_dict() can be JSON-serialized."""
        from backend.orchestrator.parity_experiment import run_experiment_dry

        specs = [_make_spec(r) for r in ("executor", "docs-writer")]
        report = run_experiment_dry(specs)
        blob = json.dumps(report.to_dict())  # must not raise
        data = json.loads(blob)
        assert "overall" in data
        assert "per_role" in data

    def test_dry_empty_spec_list(self):
        """Dry run with empty spec list returns zero-count report."""
        from backend.orchestrator.parity_experiment import run_experiment_dry

        report = run_experiment_dry([])
        assert report.parity.total_specs == 0
        assert report.per_role == []


# ---------------------------------------------------------------------------
# Tests: default ROLE_EXPERIMENT_SPECS
# ---------------------------------------------------------------------------

class TestRoleExperimentSpecs:
    """ROLE_EXPERIMENT_SPECS should have the required roles with valid SpawnSpec shapes."""

    REQUIRED_ROLES = [
        "executor",
        "code-reviewer",
        "security-reviewer",
        "acceptance-tester",
        "docs-writer",
        "run-analyst",
        "quality-sweep",
        "feedback-scanner",
        "mission-analyst",
    ]

    def test_all_required_roles_present(self):
        from backend.orchestrator.parity_experiment import ROLE_EXPERIMENT_SPECS

        for role in self.REQUIRED_ROLES:
            assert role in ROLE_EXPERIMENT_SPECS, f"Missing role: {role}"

    def test_gated_roles_not_sdk_eligible(self):
        """executor, code-reviewer, security-reviewer, acceptance-tester must not be sdk_eligible."""
        from backend.orchestrator.parity_experiment import ROLE_EXPERIMENT_SPECS

        gated = ["executor", "code-reviewer", "security-reviewer", "acceptance-tester"]
        for role in gated:
            spec = ROLE_EXPERIMENT_SPECS[role]
            assert spec.sdk_eligible is False, f"{role} should have sdk_eligible=False"

    def test_each_spec_has_tool_whitelist(self):
        from backend.orchestrator.parity_experiment import ROLE_EXPERIMENT_SPECS

        for role, spec in ROLE_EXPERIMENT_SPECS.items():
            assert spec.tool_whitelist, f"{role}: tool_whitelist is empty"

    def test_each_spec_has_nonempty_task_prompt(self):
        from backend.orchestrator.parity_experiment import ROLE_EXPERIMENT_SPECS

        for role, spec in ROLE_EXPERIMENT_SPECS.items():
            assert spec.task_prompt.strip(), f"{role}: task_prompt is blank"


# ---------------------------------------------------------------------------
# Tests: CLI --dry mode
# ---------------------------------------------------------------------------

class TestCLIDryMode:
    """CLI --dry mode must exit 0 and print a report."""

    def test_dry_cli_exits_zero(self):
        """python3 -m parity_experiment --dry --roles executor exits 0."""
        result = subprocess.run(
            [
                sys.executable,
                "backend/orchestrator/parity_experiment.py",
                "--dry",
                "--roles",
                "executor,docs-writer",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        assert result.returncode == 0, (
            f"CLI exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_dry_cli_json_output_valid(self):
        """--dry --json produces valid JSON with 'overall' and 'per_role' keys."""
        result = subprocess.run(
            [
                sys.executable,
                "backend/orchestrator/parity_experiment.py",
                "--dry",
                "--roles",
                "executor,code-reviewer",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        data = json.loads(result.stdout)
        assert "overall" in data
        assert "per_role" in data
        assert data["overall"]["total_specs"] == 2

    def test_dry_cli_all_roles(self):
        """--dry --roles all processes all 9 default roles."""
        result = subprocess.run(
            [
                sys.executable,
                "backend/orchestrator/parity_experiment.py",
                "--dry",
                "--roles",
                "all",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["overall"]["total_specs"] == 9


# ---------------------------------------------------------------------------
# Tests: _parse_cc_stream_json — CC-side tool-call counting + text extraction
# ---------------------------------------------------------------------------

class TestParseCCStreamJson:
    """_parse_cc_stream_json must correctly extract tool_calls, tokens, and text
    from ``--output-format stream-json`` JSONL output.

    All fixtures are synthetic strings — no real claude -p calls.
    Root-cause fix: the old ``--output-format json`` parser never updated
    tool_calls_count (was always 0).  _parse_cc_stream_json counts
    ``type: tool_use`` blocks in ``type: assistant`` events.
    """

    def _stream(self, events: list[dict]) -> str:
        """Serialize a list of event dicts into JSONL (one per line)."""
        return "\n".join(json.dumps(e) for e in events)

    def test_counts_tool_use_blocks(self):
        """tool_use blocks in assistant events increment the tool count."""
        from backend.orchestrator.parity_experiment import _parse_cc_stream_json

        stream = self._stream([
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"path": "foo.py"}},
                        {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {"path": "bar.py"}},
                        {"type": "text", "text": "I will read those files."},
                    ]
                },
            },
            {"type": "result", "subtype": "success", "result": "Done.", "usage": {"input_tokens": 100, "output_tokens": 50}},
        ])

        final_text, input_tokens, output_tokens, tool_calls = _parse_cc_stream_json(stream)

        assert tool_calls == 2, f"Expected 2 tool calls, got {tool_calls}"
        assert input_tokens == 100
        assert output_tokens == 50
        assert final_text == "Done."

    def test_zero_tool_calls_no_tool_use(self):
        """Responses without tool_use blocks yield tool_calls_count == 0."""
        from backend.orchestrator.parity_experiment import _parse_cc_stream_json

        stream = self._stream([
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Here is my answer."},
                    ]
                },
            },
            {"type": "result", "subtype": "success", "result": "Here is my answer.", "usage": {"input_tokens": 80, "output_tokens": 30}},
        ])

        _, _, _, tool_calls = _parse_cc_stream_json(stream)
        assert tool_calls == 0

    def test_result_line_provides_authoritative_text(self):
        """final_text comes from the result event, not text blocks."""
        from backend.orchestrator.parity_experiment import _parse_cc_stream_json

        stream = self._stream([
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "intermediate text"}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "result": "Final authoritative answer.",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ])

        final_text, _, _, _ = _parse_cc_stream_json(stream)
        assert final_text == "Final authoritative answer."

    def test_fallback_to_text_blocks_when_no_result_line(self):
        """When result event is absent, text from assistant events is used."""
        from backend.orchestrator.parity_experiment import _parse_cc_stream_json

        stream = self._stream([
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "fallback text"}]},
            },
        ])

        final_text, input_tokens, output_tokens, tool_calls = _parse_cc_stream_json(stream)
        assert final_text == "fallback text"
        assert input_tokens == 0
        assert output_tokens == 0
        assert tool_calls == 0

    def test_multi_turn_tool_calls_accumulate(self):
        """Tool calls across multiple assistant events are summed."""
        from backend.orchestrator.parity_experiment import _parse_cc_stream_json

        stream = self._stream([
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {}},
                        {"type": "tool_use", "id": "tu_3", "name": "Bash", "input": {}},
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "result": "All done.",
                "usage": {"input_tokens": 200, "output_tokens": 80},
            },
        ])

        _, _, _, tool_calls = _parse_cc_stream_json(stream)
        assert tool_calls == 3

    def test_empty_stdout_returns_zeros(self):
        """Empty stdout yields all-zero counts and empty text."""
        from backend.orchestrator.parity_experiment import _parse_cc_stream_json

        final_text, input_tokens, output_tokens, tool_calls = _parse_cc_stream_json("")
        assert final_text == ""
        assert input_tokens == 0
        assert output_tokens == 0
        assert tool_calls == 0

    def test_malformed_lines_are_skipped(self):
        """Non-JSON lines are silently skipped; valid lines still parsed."""
        from backend.orchestrator.parity_experiment import _parse_cc_stream_json

        stream = (
            "not json\n"
            + json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}}]},
            })
            + "\n{broken json\n"
            + json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "ok",
                "usage": {"input_tokens": 50, "output_tokens": 20},
            })
        )

        final_text, input_tokens, output_tokens, tool_calls = _parse_cc_stream_json(stream)
        assert tool_calls == 1
        assert final_text == "ok"
        assert input_tokens == 50


# ---------------------------------------------------------------------------
# Tests: verdict extraction on AGENT_OUTPUT envelope
# ---------------------------------------------------------------------------

class TestExtractVerdictEnvelope:
    """_extract_verdict must parse the AGENT_OUTPUT envelope correctly.

    Root-cause fix: the old task prompts didn't instruct agents to emit the
    exact envelope format, so cc_verdict was always 'unknown'.  The new
    ROLE_EXPERIMENT_SPECS prompts include the exact format; these tests
    confirm the parser handles it correctly.
    """

    def _wrap(self, verdict: str, extra: str = "") -> str:
        """Build a valid AGENT_OUTPUT-wrapped response."""
        envelope = json.dumps({"agent": "executor", "verdict": verdict, "files_touched": []}, indent=2)
        return f"Some prose.\n\n<!-- AGENT_OUTPUT -->\n```json\n{envelope}\n```\n<!-- /AGENT_OUTPUT -->\n{extra}"

    def test_extracts_done_verdict(self):
        from backend.orchestrator.sdk_runner import _extract_verdict
        assert _extract_verdict(self._wrap("done")) == "done"

    def test_extracts_pass_verdict(self):
        from backend.orchestrator.sdk_runner import _extract_verdict
        assert _extract_verdict(self._wrap("pass")) == "pass"

    def test_extracts_needs_fix_verdict(self):
        from backend.orchestrator.sdk_runner import _extract_verdict
        assert _extract_verdict(self._wrap("needs-fix")) == "needs-fix"

    def test_extracts_fail_verdict(self):
        from backend.orchestrator.sdk_runner import _extract_verdict
        assert _extract_verdict(self._wrap("fail")) == "fail"

    def test_returns_unknown_when_no_envelope(self):
        from backend.orchestrator.sdk_runner import _extract_verdict
        assert _extract_verdict("No envelope here.") == "unknown"

    def test_returns_unknown_on_malformed_json(self):
        from backend.orchestrator.sdk_runner import _extract_verdict
        text = "<!-- AGENT_OUTPUT -->\n```json\n{broken\n```\n<!-- /AGENT_OUTPUT -->"
        assert _extract_verdict(text) == "unknown"

    def test_envelope_with_surrounding_prose(self):
        """Verdict extracted even when envelope is buried in long prose."""
        from backend.orchestrator.sdk_runner import _extract_verdict
        text = (
            "I read the file and found 5 functions.\n\n"
            "Here is a detailed breakdown:\n"
            "- compare_run: compares two results\n"
            "- parity_report: aggregates diffs\n\n"
            + self._wrap("done")
            + "\n\nEnd of response."
        )
        assert _extract_verdict(text) == "done"


# ---------------------------------------------------------------------------
# Tests: verdict_parse_rate in ExperimentReport
# ---------------------------------------------------------------------------

class TestVerdictParseRate:
    """verdict_parse_rate must correctly measure how many verdict slots
    produced a parseable (non-unknown, non-dry_stub) verdict."""

    def test_all_unknown_yields_zero(self):
        from backend.orchestrator.parity_experiment import _compute_verdict_parse_rate

        per_role = [
            {"sdk_verdict": "unknown", "cc_verdict": "unknown"},
            {"sdk_verdict": "unknown", "cc_verdict": "unknown"},
        ]
        assert _compute_verdict_parse_rate(per_role) == 0.0

    def test_all_parsed_yields_one(self):
        from backend.orchestrator.parity_experiment import _compute_verdict_parse_rate

        per_role = [
            {"sdk_verdict": "done", "cc_verdict": "done"},
            {"sdk_verdict": "pass", "cc_verdict": "pass"},
        ]
        assert _compute_verdict_parse_rate(per_role) == 1.0

    def test_dry_stubs_counted_as_unparsed(self):
        from backend.orchestrator.parity_experiment import _compute_verdict_parse_rate

        per_role = [
            {"sdk_verdict": "sdk_dry_stub", "cc_verdict": "cc_dry_stub"},
        ]
        assert _compute_verdict_parse_rate(per_role) == 0.0

    def test_mixed_partial_parse(self):
        from backend.orchestrator.parity_experiment import _compute_verdict_parse_rate

        # 4 slots: sdk=done, cc=unknown, sdk=pass, cc=done → 3 parsed out of 4
        per_role = [
            {"sdk_verdict": "done", "cc_verdict": "unknown"},
            {"sdk_verdict": "pass", "cc_verdict": "done"},
        ]
        rate = _compute_verdict_parse_rate(per_role)
        assert abs(rate - 0.75) < 0.001

    def test_empty_list_yields_zero(self):
        from backend.orchestrator.parity_experiment import _compute_verdict_parse_rate

        assert _compute_verdict_parse_rate([]) == 0.0

    def test_dry_report_has_verdict_parse_rate_key(self):
        """ExperimentReport.to_dict() includes verdict_parse_rate."""
        from backend.orchestrator.parity_experiment import run_experiment_dry

        specs = [_make_spec("executor"), _make_spec("docs-writer")]
        report = run_experiment_dry(specs)
        d = report.to_dict()
        assert "verdict_parse_rate" in d
        assert isinstance(d["verdict_parse_rate"], float)

    def test_run_experiment_computes_parse_rate(self):
        """run_experiment populates verdict_parse_rate from real verdicts."""
        from backend.orchestrator.parity_experiment import run_experiment

        spec = _make_spec("executor")
        # Both sides produce parseable verdicts
        sdk_res = _make_run_result(role="executor", verdict="done")
        cc_res = _make_run_result(role="executor", verdict="done", routed_via="cc")

        with patch.dict(os.environ, {"RUN_SDK_PARITY": "1"}, clear=False):
            with patch(
                "backend.orchestrator.parity_experiment.detect_sdk_credential",
                return_value="oauth_token",
            ):
                with patch("backend.orchestrator.parity_experiment.ClaudeAgentSDKRunner") as MockRunner:
                    instance = MagicMock()
                    instance.run = AsyncMock(return_value=sdk_res)
                    MockRunner.return_value = instance

                    with patch("backend.orchestrator.parity_experiment._run_cc_side", return_value=cc_res):
                        report = run_experiment([spec])

        # Both verdicts are "done" (parseable) → parse_rate == 1.0
        assert report.verdict_parse_rate == 1.0
