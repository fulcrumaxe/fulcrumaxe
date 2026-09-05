"""Tests for hooks/repo_scope_warn.py's repo-target resolution (D#2348 PR-a).

The hook used to name a hard-coded slug in its warning. That slug was this
project's pre-rename name, so it stayed *reachable* through GitHub's rename
redirect and never produced an error — it just told everyone, including
adopters of a fork, to scope their gh calls at a repo that wasn't theirs.

What matters here is behaviour at runtime, not that a grep comes back empty:
each test below runs the hook as a real subprocess and reads the warning it
actually emits.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _REPO_ROOT / "hooks" / "repo_scope_warn.py"

sys.path.insert(0, str(_REPO_ROOT))


def _payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _run_hook(command: str, *, env: dict | None = None, hook: Path | None = None,
              cwd: Path | None = None) -> subprocess.CompletedProcess:
    run_env = dict(os.environ) if env is None else env
    return subprocess.run(
        [sys.executable, str(hook or _HOOK)],
        input=_payload(command),
        capture_output=True,
        text=True,
        env=run_env,
        cwd=str(cwd or _REPO_ROOT),
        timeout=30,
    )


class TestResolvedTarget:
    def test_warning_names_the_resolved_repo_for_this_checkout(self):
        """The live path: warning names whatever backend._repo resolves."""
        from backend._repo import REPO

        proc = _run_hook("gh api graphql -f query='{ viewer { login } }'")

        assert proc.returncode == 0
        assert f"--repo {REPO}" in proc.stderr
        # And that value is this repo's post-rename slug, not the old one.
        assert REPO == "autonomous-agent-7/fulcrumaxe"

    def test_warning_follows_an_env_override(self, tmp_path):
        """Proves the slug is resolved, not baked in.

        A literal would keep printing the same string no matter what the
        project is configured to target; this asserts the warning moves.
        """
        env = dict(os.environ)
        env["AUTONOMOUS_TEAM_REPO"] = "someone-else/their-repo"

        proc = _run_hook("gh issue list", env=env)

        assert proc.returncode == 0
        assert "--repo someone-else/their-repo" in proc.stderr
        # The configured slug is gone from the warning, not merely joined by
        # the override.
        from backend._repo import REPO

        assert REPO not in proc.stderr

    def test_no_slug_literal_in_the_hook_source(self):
        from backend._repo import REPO

        source = _HOOK.read_text(encoding="utf-8")
        assert "autonomous-forever" not in source
        assert REPO not in source


class TestFailOpen:
    def test_unresolvable_repo_still_warns_and_exits_zero(self, tmp_path):
        """A hook must never fail because the resolver could not answer.

        Copies the hook into a tree with no backend/ package next to it, so
        the import genuinely fails rather than being stubbed out. The hook
        should warn without naming any repo, and still exit 0.
        """
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        copied = hooks_dir / "repo_scope_warn.py"
        shutil.copy2(_HOOK, copied)

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "AUTONOMOUS_TEAM_REPO")
        }

        proc = _run_hook("gh pr list", env=env, hook=copied, cwd=tmp_path)

        assert proc.returncode == 0
        assert "WARN" in proc.stderr
        assert "AUTONOMOUS_TEAM_REPO" in proc.stderr
        # No repo named at all — not a guess, and not this project's slug.
        assert "<this project's repo>" in proc.stderr


class TestUnchangedBehaviour:
    """The parts of the hook this change was not supposed to move."""

    def test_no_warning_when_repo_flag_present(self):
        proc = _run_hook("gh pr list --repo autonomous-agent-7/fulcrumaxe")
        assert proc.returncode == 0
        assert "WARN" not in proc.stderr

    def test_non_bash_tool_is_ignored(self):
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}}),
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=30,
        )
        assert proc.returncode == 0
        assert proc.stderr == ""

    @pytest.mark.parametrize("command", ["gh api graphql", "gh pr view 1", "gh issue list"])
    def test_scoped_subcommands_all_warn(self, command):
        proc = _run_hook(command)
        assert proc.returncode == 0
        assert "WARN" in proc.stderr
