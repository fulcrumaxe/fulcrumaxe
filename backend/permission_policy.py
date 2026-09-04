"""
permission_policy.py — per-role tool permission policy engine.

Evaluates whether a tool call should be auto-approved, auto-denied,
or require human approval before the agent proceeds.

Decision priority:
  1. Role unknown → deny (defense in depth)
  2. Tool unknown for role → deny (defense in depth)
  3. Tool has pattern list → first matching pattern wins
  4. Tool has no matching pattern → tool-level default applies
"""

from __future__ import annotations
from typing import Literal

Decision = Literal["auto-approve", "deny", "human-approval"]

# ---------------------------------------------------------------------------
# Policy table
# ---------------------------------------------------------------------------
# Each entry is either a bare Decision string (applies to all inputs) or a
# dict with "default" + "patterns" keys for input-sensitive decisions.
# ---------------------------------------------------------------------------

POLICIES: dict[str, dict[str, object]] = {
    "executor": {
        "Read": "auto-approve",
        "Write": "auto-approve",
        "Edit": "auto-approve",
        "Glob": "auto-approve",
        "Grep": "auto-approve",
        "Bash": {
            "default": "auto-approve",
            "patterns": [
                {"match": "rm -rf", "decision": "human-approval"},
                {"match": "git push", "decision": "human-approval"},
                {"match": "curl ", "decision": "deny"},
                {"match": "wget ", "decision": "deny"},
            ],
        },
    },
    "code-reviewer": {
        "Read": "auto-approve",
        "Glob": "auto-approve",
        "Grep": "auto-approve",
        "Write": "deny",
        "Edit": "deny",
        "Bash": {
            "default": "deny",
            "patterns": [
                {"match": "git diff", "decision": "auto-approve"},
                {"match": "git log", "decision": "auto-approve"},
                {"match": "git show", "decision": "auto-approve"},
            ],
        },
    },
    "project-manager": {
        "Read": "auto-approve",
        "Glob": "auto-approve",
        "Grep": "auto-approve",
        "Write": "deny",
        "Edit": "deny",
        "Bash": {
            "default": "deny",
            "patterns": [
                {"match": "gh ", "decision": "auto-approve"},
                {"match": "git log", "decision": "auto-approve"},
            ],
        },
    },
    "security-reviewer": {
        "Read": "auto-approve",
        "Glob": "auto-approve",
        "Grep": "auto-approve",
        "Write": "deny",
        "Edit": "deny",
        "Bash": {
            "default": "deny",
            "patterns": [
                {"match": "git diff", "decision": "auto-approve"},
                {"match": "git log", "decision": "auto-approve"},
                {"match": "git show", "decision": "auto-approve"},
            ],
        },
    },
}


def evaluate(role: str, tool: str, input_text: str) -> Decision:
    """Return the policy decision for a given (role, tool, input_text) triple.

    Unknown roles and unknown tools for a known role both default to "deny"
    so that new roles or tools require explicit opt-in.
    """
    role_policy = POLICIES.get(role)
    if role_policy is None:
        return "deny"

    tool_rule = role_policy.get(tool)
    if tool_rule is None:
        return "deny"

    # Bare string rule — applies unconditionally
    if isinstance(tool_rule, str):
        return tool_rule  # type: ignore[return-value]

    # Dict rule with optional pattern list
    patterns = tool_rule.get("patterns", [])
    for pat in patterns:
        if pat["match"] in input_text:
            return pat["decision"]  # type: ignore[return-value]

    # No pattern matched — fall through to the default for this tool
    default = tool_rule.get("default", "deny")
    return default  # type: ignore[return-value]
