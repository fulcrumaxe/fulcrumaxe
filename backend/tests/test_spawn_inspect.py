"""Tests for backend/spawn_inspect.py."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import spawn_inspect  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "spawn_inspect_fixture.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text())


def _make_completed_process(stdout: str, returncode: int = 0):
    """Return a fake CompletedProcess object."""
    import subprocess

    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_required_args(self):
        args = spawn_inspect._parse_args(["--role", "executor", "--discussion", "365"])
        assert args.role == "executor"
        assert args.discussion == 365
        assert args.pr is None
        assert not args.json_only
        assert not args.prompt_only

    def test_optional_pr(self):
        args = spawn_inspect._parse_args(
            ["--role", "executor", "--discussion", "365", "--pr", "400"]
        )
        assert args.pr == 400

    def test_json_only_flag(self):
        args = spawn_inspect._parse_args(
            ["--role", "executor", "--discussion", "365", "--json-only"]
        )
        assert args.json_only

    def test_prompt_only_flag(self):
        args = spawn_inspect._parse_args(
            ["--role", "executor", "--discussion", "365", "--prompt-only"]
        )
        assert args.prompt_only


class TestRunDryRun:
    def test_returns_parsed_json(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(json.dumps(_FIXTURE))
            result = spawn_inspect._run_dry_run("executor", 365, None)
        assert result["allowed"] is True
        assert result["role"] == "executor"

    def test_exits_1_on_nonzero_returncode(self):
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=[], stderr="budget exceeded"
            )
            with pytest.raises(SystemExit) as exc_info:
                spawn_inspect._run_dry_run("executor", 365, None)
        assert exc_info.value.code == 1

    def test_exits_1_on_invalid_json(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process("not-json")
            with pytest.raises(SystemExit) as exc_info:
                spawn_inspect._run_dry_run("executor", 365, None)
        assert exc_info.value.code == 1


class TestBuildRenderVars:
    def test_includes_project_context(self):
        vars_ = spawn_inspect._build_render_vars(_FIXTURE, 365)
        assert vars_["project_context"] == "[project_context fixture]"

    def test_includes_agent_memory(self):
        vars_ = spawn_inspect._build_render_vars(_FIXTURE, 365)
        assert vars_["agent_memory"] == "[agent_memory fixture]"

    def test_discussion_number_is_string(self):
        vars_ = spawn_inspect._build_render_vars(_FIXTURE, 365)
        assert vars_["discussion_number"] == "365"

    def test_gate_context_serialized_to_string(self):
        vars_ = spawn_inspect._build_render_vars(_FIXTURE, 365)
        # gate_context in fixture is a dict — should be JSON string
        assert "gates" in vars_["gate_context"]


class TestMain:
    """Integration tests against the real render() function (no subprocess mocking)."""

    def _patched_main(self, argv: list[str], fixture: dict = _FIXTURE):
        """Run main() with subprocess stubbed to return fixture JSON."""
        with patch("backend.spawn_inspect._run_dry_run", return_value=fixture):
            spawn_inspect.main(argv)

    def test_full_output_contains_json_and_prompt(self, capsys):
        self._patched_main(["--role", "executor", "--discussion", "365"])
        captured = capsys.readouterr()
        # JSON envelope section
        assert '"allowed"' in captured.out
        # Divider
        assert "--- RENDERED PROMPT ---" in captured.out
        # Rendered prompt contains fixture project_context marker
        assert "[project_context fixture]" in captured.out

    def test_json_only_no_prompt_divider(self, capsys):
        self._patched_main(["--role", "executor", "--discussion", "365", "--json-only"])
        captured = capsys.readouterr()
        assert "--- RENDERED PROMPT ---" not in captured.out
        parsed = json.loads(captured.out)
        assert parsed["allowed"] is True

    def test_prompt_only_no_json(self, capsys):
        self._patched_main(
            ["--role", "executor", "--discussion", "365", "--prompt-only"]
        )
        captured = capsys.readouterr()
        assert '"allowed"' not in captured.out
        # The rendered prompt has actual text content
        assert len(captured.out.strip()) > 0

    def test_prompt_only_contains_agent_memory_marker(self, capsys):
        self._patched_main(
            ["--role", "executor", "--discussion", "365", "--prompt-only"]
        )
        captured = capsys.readouterr()
        assert "[agent_memory fixture]" in captured.out

    def test_unknown_role_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            spawn_inspect.main(["--role", "not-a-role", "--discussion", "365"])
        assert exc_info.value.code == 1

    def test_mutually_exclusive_flags_exit_1(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            spawn_inspect.main(
                ["--role", "executor", "--discussion", "365", "--json-only", "--prompt-only"]
            )
        assert exc_info.value.code == 1


class TestFetchPrBranch:
    """D#1788 round 3: _fetch_pr_branch must distinguish success from
    failure (not just return "" for both) so main() can tell a real gh api
    failure apart from a PR that genuinely has no branch info."""

    def test_success_returns_branch_and_no_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process("feature/x\n", returncode=0)
            branch, error = spawn_inspect._fetch_pr_branch("owner/repo", 1786)
        assert branch == "feature/x"
        assert error is None

    def test_gh_failure_returns_empty_branch_and_error(self):
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="API rate limit exceeded"
            )
            branch, error = spawn_inspect._fetch_pr_branch("owner/repo", 1786)
        assert branch == ""
        assert error is not None
        assert "rate limit" in error

    def test_timeout_returns_empty_branch_and_error(self):
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gh"], timeout=15)
            branch, error = spawn_inspect._fetch_pr_branch("owner/repo", 1786)
        assert branch == ""
        assert error is not None


class TestPrBranchApiFailureHardFail:
    """D#1788 round 3 (blocker 3): a gh api blip resolving pr_branch must
    hard-fail spawns for the roles that actually reference {{pr_branch}}
    (docs-writer, accessibility-reviewer, runbook-writer) instead of either
    silently rendering an empty branch name into a checkout command, or
    surfacing only as a generic "resolving to empty: pr_branch" contract
    error with no mention of the real cause. Roles that don't reference
    pr_branch must be unaffected by the same failure."""

    def _patched_main(self, argv: list[str], fixture: dict = _FIXTURE):
        with patch("backend.spawn_inspect._run_dry_run", return_value=fixture):
            with patch("subprocess.run") as mock_run:
                import subprocess as _subprocess

                mock_run.return_value = _subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="API rate limit exceeded"
                )
                spawn_inspect.main(argv)

    def test_role_needing_pr_branch_hard_fails_on_api_error(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._patched_main(
                ["--role", "docs-writer", "--discussion", "1761", "--pr", "1786", "--prompt-only"]
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "requires" in captured.err
        assert "pr_branch" in captured.err
        assert "rate limit" in captured.err
        assert captured.out == ""

    def test_role_not_needing_pr_branch_unaffected_by_api_error(self, capsys):
        # code-reviewer never references {{pr_branch}} -- an API failure
        # resolving it must not block this spawn at all.
        self._patched_main(
            ["--role", "code-reviewer", "--discussion", "1761", "--pr", "1786", "--prompt-only"]
        )
        captured = capsys.readouterr()
        assert "PR #1786" in captured.out
        assert "{{" not in captured.out
