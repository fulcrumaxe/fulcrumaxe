"""Tests for the Muse approval driver (backend/muse_approval.py) and the
generalized nested-spawn check (hooks/sandbox_rules.py).

Policy 1:1 with the Claude PreToolUse fence (hooks/sandbox.py): nested agent
spawns denied, classify_bash/classify_path_write verdicts reused verbatim,
unknown approval kinds fail closed per the MSP schema (ApprovalSubject:
unknown kinds are never auto-approved).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# backend/tests/ has __init__.py but backend/ does not, so pytest inserts
# backend/ at sys.path[0] and `import hooks` would resolve to backend/hooks/.
# Force the repo root first so hooks/ means the top-level hook package.
if str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))

from hooks import sandbox_rules  # noqa: E402


def _driver():
    import backend.muse_approval as m  # noqa: PLC0415

    return m


# ---------------------------------------------------------------------------
# Nested-spawn predicate: muse twin + claude regression
# ---------------------------------------------------------------------------


def test_muse_spawn_bare_denied() -> None:
    d = sandbox_rules.check_muse_spawn([], "muse exec --prompt 'hi'")
    assert d.allow is False
    assert d.reason.startswith("muse_spawn_forbidden")


def test_muse_spawn_absolute_path_denied() -> None:
    d = sandbox_rules.check_muse_spawn([], "/home/testuser/.local/bin/muse exec --prompt 'hi'")
    assert d.allow is False


def test_muse_spawn_pipeline_stage_denied() -> None:
    d = sandbox_rules.check_muse_spawn([], "echo hi; muse --version")
    assert d.allow is False


def test_muse_spawn_bash_c_recursion_denied() -> None:
    d = sandbox_rules.check_muse_spawn([], "bash -c 'muse exec x'")
    assert d.allow is False


def test_muse_spawn_dollar_paren_denied() -> None:
    d = sandbox_rules.check_muse_spawn([], "echo $(muse --version)")
    assert d.allow is False


def test_muse_operand_allowed() -> None:
    assert sandbox_rules.check_muse_spawn([], "grep muse notes.md").allow is True
    assert sandbox_rules.check_muse_spawn([], "echo muse-spark").allow is True


def test_claude_spawn_still_denied() -> None:
    d = sandbox_rules.check_claude_spawn([], "claude -p 'hi'")
    assert d.allow is False
    assert d.reason.startswith("claude_spawn_forbidden")


def test_claude_operand_still_allowed() -> None:
    assert sandbox_rules.check_claude_spawn([], "grep claude CLAUDE.md").allow is True


# ---------------------------------------------------------------------------
# Policy decide(): recorded approval shapes
# ---------------------------------------------------------------------------

_APPROVE = {"choiceId": "allow_once", "decision": "approved", "label": "Allow"}
_DENY = {"choiceId": "abort", "decision": "abort", "label": "Abort"}
_DENIED = {"choiceId": "deny", "decision": "denied", "label": "Deny"}


def _approval(tool_name, subject, choices=(_APPROVE, _DENY)):
    return {
        "approvalId": "ap-1",
        "currentRequirementId": "req-1",
        "toolName": tool_name,
        "subject": subject,
        "availableChoices": list(choices),
        "sessionId": "sess-1",
    }


def test_shell_benign_approved() -> None:
    out = _driver().decide_approval(
        _approval("bash", {"kind": "shell", "command": "echo hi", "workspaceRoot": str(_REPO_ROOT)}),
        repo_root=str(_REPO_ROOT),
    )
    assert out is not None and out[0] == "allow_once"


def test_shell_nested_muse_denied() -> None:
    out = _driver().decide_approval(
        _approval(
            "bash",
            {"kind": "shell", "command": "muse exec --prompt x", "workspaceRoot": str(_REPO_ROOT)},
        ),
        repo_root=str(_REPO_ROOT),
    )
    assert out is not None and out[0] == "abort"


def test_shell_pr_merge_denied() -> None:
    out = _driver().decide_approval(
        _approval(
            "bash", {"kind": "shell", "command": "gh pr merge 1", "workspaceRoot": str(_REPO_ROOT)}
        ),
        repo_root=str(_REPO_ROOT),
    )
    assert out is not None and out[0] in ("deny", "abort")


def test_file_write_inside_repo_approved() -> None:
    out = _driver().decide_approval(
        _approval(
            "edit",
            {"kind": "fileAccess", "path": "notes/hi.txt", "access": "write"},
            choices=(_APPROVE, _DENIED),
        ),
        repo_root=str(_REPO_ROOT),
        workspace_root=str(_REPO_ROOT),
    )
    assert out is not None and out[0] == "allow_once"


def test_file_write_outside_repo_denied() -> None:
    out = _driver().decide_approval(
        _approval(
            "write",
            {"kind": "fileAccess", "path": "/etc/cron.d/evil", "access": "write"},
            choices=(_APPROVE, _DENIED),
        ),
        repo_root=str(_REPO_ROOT),
        workspace_root=str(_REPO_ROOT),
    )
    assert out is not None and out[0] == "deny"


def test_unknown_kind_denied() -> None:
    out = _driver().decide_approval(
        _approval("frobnicate", {"kind": "quantum"}, choices=(_APPROVE, _DENIED)),
        repo_root=str(_REPO_ROOT),
    )
    assert out is not None and out[0] == "deny"


def test_no_suitable_choice_abstains() -> None:
    only_session = {"choiceId": "allow_session", "decision": "approvedForSession", "label": "Always"}
    out = _driver().decide_approval(
        _approval("bash", {"kind": "shell", "command": "echo hi", "workspaceRoot": str(_REPO_ROOT)}),
        repo_root=str(_REPO_ROOT),
    )
    # sanity: with a proper allow choice present we allow
    assert out is not None
    out2 = _driver().decide_approval(
        _approval(
            "bash",
            {"kind": "shell", "command": "echo hi", "workspaceRoot": str(_REPO_ROOT)},
            choices=(only_session,),
        ),
        repo_root=str(_REPO_ROOT),
    )
    assert out2 is None


# ---------------------------------------------------------------------------
# Host client with fake transport
# ---------------------------------------------------------------------------


class _FakeTransport:
    def __init__(self, script):
        self._script = list(script)
        self.decides = []

    async def call(self, method, params):
        if method == "approval/decide":
            self.decides.append(params)
            return {"result": {"status": "accepted"}}
        name = f"{method}"
        assert self._script, f"no scripted reply for {method}"
        kind, payload = self._script.pop(0)
        assert kind == name, f"expected {kind}, got {name}"
        return {"result": payload}


@pytest.mark.asyncio
async def test_watch_once_allows_benign() -> None:
    approval = _approval(
        "bash", {"kind": "shell", "command": "echo hi", "workspaceRoot": str(_REPO_ROOT)}
    )
    transport = _FakeTransport(
        [("approval/listPending", {"approvals": [approval]}), ("approval/listPending", {"approvals": []})]
    )
    client = _driver().MuseHost(transport)
    decided = await client.watch_once("sess-1", repo_root=str(_REPO_ROOT))
    assert decided == 1
    assert transport.decides[0]["choiceId"] == "allow_once"
    assert transport.decides[0]["approvalId"] == "ap-1"
    assert transport.decides[0]["requirementId"] == "req-1"


@pytest.mark.asyncio
async def test_watch_once_denies_nested_spawn() -> None:
    approval = _approval(
        "bash",
        {"kind": "shell", "command": "muse exec x", "workspaceRoot": str(_REPO_ROOT)},
        choices=(_APPROVE, _DENIED),
    )
    transport = _FakeTransport(
        [("approval/listPending", {"approvals": [approval]}), ("approval/listPending", {"approvals": []})]
    )
    client = _driver().MuseHost(transport)
    decided = await client.watch_once("sess-1", repo_root=str(_REPO_ROOT))
    assert decided == 1
    assert transport.decides[0]["choiceId"] == "deny"


@pytest.mark.asyncio
async def test_watch_once_abstains_without_choice() -> None:
    only_session = {"choiceId": "allow_session", "decision": "approvedForSession", "label": "Always"}
    approval = _approval(
        "bash",
        {"kind": "shell", "command": "echo hi", "workspaceRoot": str(_REPO_ROOT)},
        choices=(only_session,),
    )
    transport = _FakeTransport([("approval/listPending", {"approvals": [approval]})])
    client = _driver().MuseHost(transport)
    decided = await client.watch_once("sess-1", repo_root=str(_REPO_ROOT))
    assert decided == 0
    assert transport.decides == []
