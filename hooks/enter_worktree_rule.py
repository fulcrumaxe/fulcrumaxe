#!/usr/bin/env python3
"""hooks/enter_worktree_rule.py

PreToolUse rule for `EnterWorktree` (D#2050).

The trap this closes
---------------------
A worktree-pinned sub-agent that calls `EnterWorktree(path=<some other
agent's worktree>)` gets its Bash-tool cwd permanently resolved to that other
worktree, while `hooks/sandbox.py` keeps enforcing this session's own
identity. Every subsequent Bash call then fails with a cwd-mismatch block,
and `ExitWorktree` — the obvious way back — itself refuses a sub-agent
caller with a cwd override. The session is bricked: it cannot record a
verdict, label a PR, or leave a comment, because everything that reaches
GitHub goes through Bash.

`EnterWorktree` accepting the call and only failing on the *next* tool call
is what makes this a trap rather than an ordinary mistake — nothing warns at
the call site. This module is that warning: it refuses the `EnterWorktree`
call itself, before the cwd ever moves, and names a working alternative in
the refusal.

Policy
------
Deny only the one shape the Discussion describes: the caller's cwd is
pinned to a known worktree (`is_worktree` returns an id) AND the target
`path` resolves into a *different* worktree. Every other shape is allowed:

  - the caller is not worktree-pinned at all (Team Lead never reaches this
    rule anyway — see hooks/sandbox.py's team_lead branch, which returns
    before any tool-specific dispatch runs).
  - the target resolves to the caller's OWN worktree (re-entering yourself
    is a no-op, not the trap).
  - the target does not resolve into any known worktree prefix at all (a
    relative path, a path outside .claude/worktrees/ and /tmp/wt-*) — not
    the cross-worktree shape this rule exists for.
  - `tool_input` carries no recognisable path key, or the value under it
    isn't a usable string — the payload shape for this tool has never been
    observed (D#2050 verification note), so a rule written against a
    guessed key name must fail open rather than silently over-block on the
    wrong field. `path` is the one key EnterWorktree's own tool definition
    documents for entering an existing worktree (`name` is for creating a
    NEW one and never names an existing target, so it cannot participate in
    a cross-worktree deny).

This mirrors hooks/sandbox_rules.py's own scoring rule (CLAUDE.md: "hooks/
is a guardrail, not a boundary" — over-blocking is the worse failure), and
is intentionally narrow: additive registration, one call site, no change to
any existing matcher's decision.

Kept out of hooks/sandbox_rules.py on purpose (D#2050 concurrency note: a
sibling PR is editing that file's regex anchors right now) — pure functions,
no subprocess, same split hooks/sandbox.py already uses for its other rules.
"""

from __future__ import annotations

from hooks.sandbox_rules import Decision, is_worktree

# The one path-bearing key EnterWorktree's own tool definition documents for
# switching into an *existing* worktree. See this module's docstring for why
# `name` (create-a-new-worktree) is not a candidate.
_PATH_KEY = "path"

_UNRECOGNISED_PREFIX = "unrecognised_enter_worktree_shape"

_REMEDY = (
    "Reading another agent's worktree does not require entering it: "
    "`git show <ref>:<path>` or `git archive <ref>` reads a target ref "
    "directly without touching cwd, and scripts/lib/code-plane-pr.sh (on "
    "code-plane/main) is the sanctioned path for building a code-plane "
    "commit from a worktree."
)


def classify_enter_worktree(cwd: str, tool_input: object) -> Decision:
    """Decide whether an `EnterWorktree` call may proceed.

    Never raises. Every malformed shape (tool_input missing/not a dict, a
    non-string path, an unresolvable target) reaches a Decision instead of
    an exception — hooks/sandbox.py runs on every tool call, so an exception
    here would degrade the guardrail for everything, not just this rule.
    """
    caller_worktree = is_worktree(cwd)
    if not caller_worktree:
        # Not a worktree-pinned caller (Team Lead / untrusted). Out of scope
        # for this rule; hooks/sandbox.py's own tiering already governs
        # those contexts.
        return Decision(allow=True, reason="")

    if not isinstance(tool_input, dict):
        return Decision(
            allow=True,
            reason=f"{_UNRECOGNISED_PREFIX}: tool_input is not an object",
        )

    if _PATH_KEY not in tool_input:
        keys = sorted(str(k) for k in tool_input.keys())
        return Decision(
            allow=True,
            reason=f"{_UNRECOGNISED_PREFIX}: no '{_PATH_KEY}' key (keys={keys})",
        )

    target = tool_input.get(_PATH_KEY)
    if not isinstance(target, str) or not target:
        return Decision(
            allow=True,
            reason=f"{_UNRECOGNISED_PREFIX}: '{_PATH_KEY}' value is not a non-empty string",
        )

    target_worktree = is_worktree(target)
    if target_worktree is None:
        # Target isn't inside any known worktree prefix at all — not the
        # cross-worktree shape this rule exists for. Fail open.
        return Decision(allow=True, reason="")

    if target_worktree == caller_worktree:
        return Decision(allow=True, reason="")

    return Decision(
        allow=False,
        reason=(
            "cross_worktree_enter_forbidden: this session is pinned to "
            f"worktree '{caller_worktree}'; EnterWorktree(path=...) targets "
            f"a different worktree ('{target_worktree}'). Entering it would "
            "leave this session's Bash cwd resolved outside its own "
            "worktree with no way back — ExitWorktree refuses a "
            f"subagent-with-cwd-override caller. {_REMEDY}"
        ),
    )
