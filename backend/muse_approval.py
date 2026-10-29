"""backend/muse_approval.py — external approval driver for Muse sessions.

The Muse twin of the PreToolUse fence (hooks/sandbox.py). Muse has no hook
scripts; instead the engine attaches to a `muse serve` MSP host and decides
pending approvals (approval/listPending + approval/decide) using the SAME
policy library (hooks/sandbox_rules) the fence uses:

- kind=shell  → check_muse_spawn + classify_bash (the tool_proxy two-stage)
- kind=fileAccess + access=write → cwd-tier gate + classify_path_write
  (exactly the fence's Edit/Write branch); access=read → allow (the fence
  has no Read gate)
- every other kind → deny. The MSP schema mandates that unknown kinds are
  never auto-approved; network/process/unixSocket/tool have no fence
  equivalent, so they fail closed too. (The hook's allow-unknown-tools
  branch is safe only because hook registration is a closed world of five
  tools; MSP kinds are an open union, so the polarity flips by necessity.)

Choice discipline (least privilege): allow only via a choice whose decision
is exactly "approved" — never approvedForSession/approvedPolicyAmendment
from automation. Deny via "denied", falling back to "abort". When no
suitable choice exists the driver abstains (no decide call) rather than
guessing: choice ids are an open set the schema forbids keying behavior on.

CLI:
  python -m backend.muse_approval watch --session-id <id> --repo-root <path>
      [--workspace-root <path>] [--provider meta] [--timeout-s 600]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
# backend/tests/ has __init__.py but backend/ does not, so pytest inserts
# backend/ ahead of the root and `import hooks` would resolve to
# backend/hooks/. Force the root first (same move as backend/conftest.py).
if str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))

from hooks import sandbox_rules  # noqa: E402

_ALLOW_DECISIONS = ("approved",)
_DENY_DECISIONS = ("denied", "abort")


def uuid7() -> str:
    """Mint a UUIDv7 (monotonic-ms id the MSP host requires for commandIds)."""
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rnd = random.getrandbits(74)
    val = (ms << 80) | (7 << 76) | ((rnd >> 62) << 64) | (2 << 62) | (rnd & 0x3FFFFFFFFFFFFFFF)
    return str(uuid.UUID(int=val))


def _pick_choice(available: list[dict[str, Any]], allow: bool) -> dict[str, Any] | None:
    """Pick the least-privilege choice matching the verdict, else None."""
    wanted = _ALLOW_DECISIONS if allow else _DENY_DECISIONS
    rank = {name: i for i, name in enumerate(wanted)}
    best: dict[str, Any] | None = None
    for choice in available:
        decision = choice.get("decision")
        if decision in rank and (best is None or rank[decision] < rank[best.get("decision")]):
            best = choice
    return best


def decide_shell(command: str, cwd: str):
    """Two-stage shell verdict; returns the denying Decision or an allow."""
    spawn_decision = sandbox_rules.check_muse_spawn([], command)
    if not spawn_decision.allow:
        return spawn_decision
    return sandbox_rules.classify_bash(command, cwd)


def decide_file_access(path: str, access: str, workspace_root: str):
    """Edit/Write-branch verdict for one fileAccess subject."""
    if access == "read":
        return sandbox_rules.Decision(allow=True, reason="")
    tier = sandbox_rules.classify_cwd(workspace_root)
    if tier == "untrusted":
        return sandbox_rules.Decision(
            allow=False,
            reason="untrusted cwd: all mutations are blocked outside repo and worktree paths",
        )
    candidate = path if os.path.isabs(path) else os.path.join(workspace_root, path)
    return sandbox_rules.classify_path_write(candidate, workspace_root)


def decide_approval(
    approval: dict[str, Any],
    repo_root: str,
    workspace_root: str | None = None,
) -> tuple[str, str | None] | None:
    """Decide one listPending approval.

    Returns (choiceId, feedback-or-None), or None to abstain when no
    suitable choice exists. Never raises: an unrecognised shape denies
    when a deny choice exists, else abstains.
    """
    try:
        subject = approval.get("subject") or {}
        if not isinstance(subject, dict):
            subject = {}
        kind = subject.get("kind")
        ws = workspace_root or subject.get("workspaceRoot") or repo_root
        choices = approval.get("availableChoices") or []
        if kind == "shell":
            command = subject.get("command") or ""
            stages = subject.get("stages") or []
            verdict = decide_shell(command, str(ws))
            if verdict.allow:
                for stage in stages:
                    if not isinstance(stage, dict):
                        continue
                    stage_cmd = stage.get("command") or stage.get("text") or ""
                    if stage_cmd:
                        stage_verdict = decide_shell(str(stage_cmd), str(ws))
                        if not stage_verdict.allow:
                            verdict = stage_verdict
                            break
        elif kind == "fileAccess":
            verdict = decide_file_access(str(subject.get("path") or ""), str(subject.get("access") or ""), str(ws))
        else:
            verdict = sandbox_rules.Decision(allow=False, reason=f"unsupported approval kind: {kind}")
        choice = _pick_choice(choices, verdict.allow)
        if choice is None:
            return None
        feedback = None
        if not verdict.allow and choice.get("acceptsFeedback") and verdict.reason:
            feedback = verdict.reason
        return str(choice["choiceId"]), feedback
    except Exception as exc:  # noqa: BLE001 — abstain, never crash the watch loop
        logger.warning("muse_approval.decide_approval failed (abstaining): %s", exc)
        return None


class MuseHost:
    """Minimal MSP client over an injected async transport.

    Transport protocol: `await transport.call(method, params) -> response`
    where response is the decoded JSON-RPC envelope. StdioTransport below
    speaks to a real `muse serve` child; tests inject fakes.
    """

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    async def list_pending(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._transport.call("approval/listPending", {"sessionId": session_id})
        result = (response or {}).get("result") or {}
        for key in ("approvals", "items", "pending"):
            items = result.get(key)
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
        return []

    async def decide(
        self,
        session_id: str,
        approval: dict[str, Any],
        choice_id: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "commandId": uuid7(),
            "sessionId": session_id,
            "approvalId": approval.get("approvalId"),
            "requirementId": approval.get("currentRequirementId"),
            "choiceId": choice_id,
        }
        if feedback is not None:
            params["feedback"] = feedback
        return await self._transport.call("approval/decide", params)

    async def watch_once(
        self,
        session_id: str,
        repo_root: str,
        workspace_root: str | None = None,
    ) -> int:
        """Decide every currently-pending approval once. Returns decided count."""
        decided = 0
        for approval in await self.list_pending(session_id):
            picked = decide_approval(approval, repo_root, workspace_root)
            if picked is None:
                logger.warning(
                    "muse_approval: abstaining on %s (no suitable choice)",
                    approval.get("approvalId"),
                )
                continue
            choice_id, feedback = picked
            await self.decide(session_id, approval, choice_id, feedback)
            decided += 1
        return decided

    async def watch(
        self,
        session_id: str,
        repo_root: str,
        workspace_root: str | None = None,
        poll_interval: float = 1.0,
        quiet_rounds: int = 3,
        timeout_s: float = 600.0,
    ) -> int:
        """Poll until quiet_rounds consecutive empty rounds or timeout.

        Returns the total decided count.
        """
        total = 0
        quiet = 0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            decided = await self.watch_once(session_id, repo_root, workspace_root)
            total += decided
            quiet = quiet + 1 if decided == 0 else 0
            if quiet >= quiet_rounds:
                return total
            await asyncio.sleep(poll_interval)
        return total


class StdioTransport:
    """JSON-RPC transport over a `muse serve` child process (newline framing)."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._pending: dict[str, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    @classmethod
    async def launch(
        cls,
        muse_bin: str | None = None,
        provider: str = "meta",
        extra_args: list[str] | None = None,
    ) -> "StdioTransport":
        binary = muse_bin or os.environ.get("MUSE_BIN") or shutil.which("muse")
        if binary is None:
            raise RuntimeError("cannot resolve muse binary — checked $MUSE_BIN and PATH")
        proc = await asyncio.create_subprocess_exec(
            binary,
            "serve",
            "--provider",
            provider,
            *(extra_args or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        transport = cls(proc)
        await transport._start_reader()
        init = await transport.call("initialize", {"clientInfo": {"name": "af_approval_driver", "version": "1"}})
        if not isinstance(init, dict) or "result" not in init:
            raise RuntimeError(f"muse serve initialize failed: {init!r:.300}")
        await transport._notify("initialized", {})
        return transport

    async def _start_reader(self) -> None:
        assert self._proc.stdout is not None

        async def _read() -> None:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    return
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                msg_id = message.get("id")
                if msg_id is not None and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        future.set_result(message)

        self._reader = asyncio.ensure_future(_read())

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
        await self._proc.stdin.drain()

    async def call(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        assert self._proc.stdin is not None
        msg_id = uuid7()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[msg_id] = future
        self._proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode()
        )
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass


async def _watch_command(args: argparse.Namespace) -> int:
    transport = await StdioTransport.launch(provider=args.provider)
    try:
        host = MuseHost(transport)
        total = await host.watch(
            args.session_id,
            repo_root=args.repo_root,
            workspace_root=args.workspace_root,
            poll_interval=args.poll_interval,
            quiet_rounds=args.quiet_rounds,
            timeout_s=args.timeout_s,
        )
        print(f"muse_approval: decided {total} approval(s) for {args.session_id}")
        return 0
    finally:
        await transport.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="muse_approval")
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch", help="Decide pending approvals until quiet or timeout")
    watch.add_argument("--session-id", required=True)
    watch.add_argument("--repo-root", required=True)
    watch.add_argument("--workspace-root", default=None)
    watch.add_argument("--provider", default="meta")
    watch.add_argument("--timeout-s", type=float, default=600.0)
    watch.add_argument("--poll-interval", type=float, default=1.0)
    watch.add_argument("--quiet-rounds", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "watch":
        return asyncio.run(_watch_command(args))
    return 2


if __name__ == "__main__":
    sys.exit(main())
