"""
team_lead_protocol.py — Schema scaffolding for Team Lead spawn message types.

INACTIVE STATE NOTE: The runtime SendMessage channel between worktree subagents and their
parent is not currently wired. Subagents return results via a single task-notification
(AGENT_OUTPUT envelope) when they complete — there is no bidirectional back-channel during
execution. Use task-notification + AGENT_OUTPUT envelope for return values today.

This module is kept as future-use scaffolding for when/if a real message bus exists.
The Pydantic schemas (SpawnRequest, SpawnResult, SpawnBlocked) and ROLE_PASS_LABELS /
ROLE_FAIL_LABELS constants remain valid and are used by tests.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Literal, Optional, Union

try:
    from pydantic import BaseModel, Field, field_validator
    _PYDANTIC_AVAILABLE = True
except ImportError:
    _PYDANTIC_AVAILABLE = False


# ---------------------------------------------------------------------------
# TypedDict fallback (when Pydantic is not available)
# ---------------------------------------------------------------------------

if _PYDANTIC_AVAILABLE:

    class SpawnRequest(BaseModel):
        """Sent to Team Lead to request an agent spawn."""

        kind: Literal["spawn_request"] = "spawn_request"
        request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
        discussion: int = Field(..., description="Discussion number driving this request")
        pr: Optional[int] = Field(None, description="PR number if spawning for a PR (e.g. reviewer)")
        role: Literal[
            "executor",
            "code-reviewer",
            "security-reviewer",
            "acceptance-tester",
        ] = Field(..., description="Named role — never 'general-purpose'")
        prompt: str = Field(..., description="Prompt to pass to the spawned agent")
        isolation: Literal["worktree", "none"] = Field(
            "worktree",
            description="Worktree isolation required when parallel agents touch same files",
        )
        context: Dict[str, Any] = Field(
            default_factory=dict,
            description="Extra key-value context (e.g. branch, spec_summary)",
        )

        @field_validator("role")
        @classmethod
        def role_must_be_named(cls, v: str) -> str:
            forbidden = {"general-purpose", "agent", "generic"}
            if v in forbidden:
                raise ValueError(
                    f"role '{v}' is forbidden — use a named role: executor, "
                    "code-reviewer, security-reviewer, acceptance-tester"
                )
            return v

        @field_validator("prompt")
        @classmethod
        def prompt_must_not_invoke_agent_directly(cls, v: str) -> str:
            # Guard: prompt text must not instruct spawned agent
            # to call Agent() itself — only Team Lead may spawn agents.
            forbidden_phrases = [
                "spawn(",
                "Agent(",
                "call Agent",
                "invoke Agent",
            ]
            for phrase in forbidden_phrases:
                if phrase in v:
                    raise ValueError(
                        f"Prompt contains forbidden phrase '{phrase}'. "
                        "Spawned agents must not call Agent() — only Team Lead may."
                    )
            return v

    class SpawnResult(BaseModel):
        """Sent by Team Lead after completing (or failing) a spawn."""

        kind: Literal["spawn_result"] = "spawn_result"
        request_id: str = Field(..., description="Echo of the originating SpawnRequest.request_id")
        verdict: Literal["done", "fail", "pass", "needs-fix", "skip"] = Field(
            ..., description="Agent outcome forwarded from agent AGENT_OUTPUT envelope"
        )
        envelope: Dict[str, Any] = Field(
            default_factory=dict,
            description="Full AGENT_OUTPUT envelope dict from the spawned agent",
        )
        error: Optional[str] = Field(
            None, description="Error message when Team Lead could not fulfil the request"
        )

    class SpawnBlocked(BaseModel):
        """Sent by Team Lead when a spawn is blocked by a gate."""

        kind: Literal["spawn_blocked"] = "spawn_blocked"
        request_id: str = Field(..., description="Echo of the originating SpawnRequest.request_id")
        reason: Literal[
            "budget_exceeded",
            "circuit_breaker_tripped",
            "concurrency_cap_reached",
            "worktree_cap_reached",
            "unknown",
        ] = Field(..., description="Machine-readable reason code")
        reason_detail: Optional[str] = Field(
            None, description="Human-readable explanation"
        )
        retry_after_seconds: Optional[int] = Field(
            None, description="Suggested retry delay (None means do not retry automatically)"
        )

    # Union type for type-checking Team Lead's incoming messages
    IncomingMessage = Union[SpawnRequest]
    TeamLeadReply = Union[SpawnResult, SpawnBlocked]

    def parse_message(data: Dict[str, Any]) -> Union[SpawnRequest, SpawnResult, SpawnBlocked]:
        """Parse a raw dict into the appropriate protocol message."""
        kind = data.get("kind")
        if kind == "spawn_request":
            return SpawnRequest(**data)
        elif kind == "spawn_result":
            return SpawnResult(**data)
        elif kind == "spawn_blocked":
            return SpawnBlocked(**data)
        else:
            raise ValueError(f"Unknown message kind: {kind!r}")

else:
    # Minimal TypedDict-style stubs when Pydantic is absent
    from typing import TypedDict

    class SpawnRequest(TypedDict, total=False):  # type: ignore[no-redef]
        kind: str
        request_id: str
        discussion: int
        pr: Optional[int]
        role: str
        prompt: str
        isolation: str
        context: Dict[str, Any]

    class SpawnResult(TypedDict, total=False):  # type: ignore[no-redef]
        kind: str
        request_id: str
        verdict: str
        envelope: Dict[str, Any]
        error: Optional[str]

    class SpawnBlocked(TypedDict, total=False):  # type: ignore[no-redef]
        kind: str
        request_id: str
        reason: str
        reason_detail: Optional[str]
        retry_after_seconds: Optional[int]

    def parse_message(data: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[misc]
        """Pass-through when Pydantic unavailable."""
        return data


# ---------------------------------------------------------------------------
# Constants — role-to-label mapping (used by Team Lead for label decisions)
# ---------------------------------------------------------------------------

ROLE_PASS_LABELS: Dict[str, str] = {
    "code-reviewer": "code-review-passed",
    "security-reviewer": "security-review-passed",
    "acceptance-tester": "acceptance-passed",
}

ROLE_FAIL_LABELS: Dict[str, str] = {
    "code-reviewer": "code-review-needs-fix",
    "security-reviewer": "security-issue",
    "acceptance-tester": "acceptance-failed",
}

VALID_ROLES = frozenset(ROLE_PASS_LABELS.keys()) | {"executor"}
