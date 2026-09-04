"""backend/orchestrator/hook_runner.py — Lifecycle hook runner for SDK-routed agents.

Calls existing hook scripts at agent lifecycle points, since Claude Code's
harness no longer drives them for SDK-routed agents.

Hooks called:
  - Pre-spawn:  scripts/pre-spawn-check.sh  (optional, best-effort)
  - Post-agent: scripts/subagent-stop-hook.sh + scripts/post-agent-hook.sh

All hook calls are non-fatal: failures are logged to stderr and swallowed
so the agent lifecycle always completes.

Usage::

    from backend.orchestrator.hook_runner import HookRunner
    from backend.orchestrator.sdk_runner import SpawnSpec, RunResult

    hr = HookRunner(repo_root="<repo-root>")
    hr.post_agent(result)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class HookRunner:
    """Calls lifecycle hook scripts for SDK-routed agent runs.

    Parameters
    ----------
    repo_root:
        Path to the main repository root (not the worktree).
        Defaults to the parent of the backend/ directory.
    """

    def __init__(self, repo_root: Optional[str] = None) -> None:
        if repo_root:
            self._root = Path(repo_root)
        else:
            # backend/ is one level below repo root
            self._root = Path(__file__).resolve().parent.parent.parent

    def _run_script(self, script_path: Path, args: list[str], env: Optional[dict] = None) -> bool:
        """Run a shell script, returning True on success.

        Non-fatal: logs stderr and returns False on failure.
        """
        if not script_path.exists():
            logger.debug("Hook script not found, skipping: %s", script_path)
            return False
        try:
            result = subprocess.run(
                ["bash", str(script_path)] + args,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
                cwd=str(self._root),
            )
            if result.returncode != 0:
                logger.warning(
                    "Hook script %s exited %d: %s",
                    script_path.name,
                    result.returncode,
                    result.stderr[:500],
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning("Hook script %s timed out after 60s", script_path.name)
            return False
        except OSError as e:
            logger.warning("Hook script %s failed to launch: %s", script_path.name, e)
            return False

    def post_agent(
        self,
        result: "RunResult",  # type: ignore[name-defined]  # forward ref
    ) -> None:
        """Call post-agent hooks after an SDK-routed run completes.

        Mirrors what scripts/subagent-stop-hook.sh does for Claude Code runs.
        Non-fatal: hook failures do not affect the run result.

        Parameters
        ----------
        result:
            The completed RunResult from sdk_runner.SDKRunner.run().
        """
        from backend.orchestrator.sdk_runner import RunResult  # local to avoid circular  # noqa: PLC0415

        agent_id = result.agent_id
        verdict = result.verdict
        role = result.role
        discussion = str(result.discussion) if result.discussion is not None else ""
        pr = str(result.pr) if result.pr is not None else ""

        # Call post-agent-hook.sh which updates agent_run row and stats
        post_agent_script = self._root / "scripts" / "post-agent-hook.sh"
        args = [
            "--event-id", agent_id,
            "--verdict", verdict,
            "--role", role,
        ]
        if discussion:
            args += ["--discussion", discussion]
        if pr:
            args += ["--pr", pr]

        self._run_script(post_agent_script, args)

        logger.info(
            "post_agent hooks complete for %s (verdict=%s)",
            agent_id,
            verdict,
        )

    def pre_spawn(self, role: str, discussion: Optional[int] = None) -> bool:
        """Call pre-spawn-check.sh before an SDK-routed spawn.

        Returns True if spawn is allowed, False if blocked.
        Non-fatal on script errors (defaults to allow).
        """
        pre_spawn_script = self._root / "scripts" / "pre-spawn-check.sh"
        args = ["--role", role]
        if discussion is not None:
            args += ["--discussion", str(discussion)]

        success = self._run_script(pre_spawn_script, args)
        # pre-spawn-check returns 0 for "allowed", non-0 for "blocked"
        return success
