"""
test_team_lead_protocol.py — Unit tests for the Team Lead spawn message schema.

Tests cover:
- Valid SpawnRequest construction and serialisation
- SpawnRequest validator rejects forbidden roles and forbidden phrases
- SpawnResult and SpawnBlocked round-trips
- parse_message dispatch
"""

import sys
import os
import uuid
import pytest

# Ensure backend/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import pydantic  # noqa: F401
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False

from team_lead_protocol import (
    SpawnRequest,
    SpawnResult,
    SpawnBlocked,
    parse_message,
    ROLE_PASS_LABELS,
    ROLE_FAIL_LABELS,
    VALID_ROLES,
)


# ---------------------------------------------------------------------------
# SpawnRequest — happy path
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not PYDANTIC_AVAILABLE, reason="Pydantic required for validation tests")
class TestSpawnRequestValid:
    def test_executor_request(self):
        req = SpawnRequest(
            discussion=445,
            role="executor",
            prompt="Implement the Spec from Discussion #445.",
        )
        assert req.kind == "spawn_request"
        assert req.role == "executor"
        assert req.isolation == "worktree"
        assert isinstance(req.request_id, str)
        assert len(req.request_id) > 0

    def test_code_reviewer_request(self):
        req = SpawnRequest(
            discussion=445,
            pr=99,
            role="code-reviewer",
            prompt="Review PR #99 for Discussion #445.",
            isolation="none",
        )
        assert req.pr == 99
        assert req.role == "code-reviewer"
        assert req.isolation == "none"

    def test_security_reviewer_request(self):
        req = SpawnRequest(
            discussion=445,
            pr=99,
            role="security-reviewer",
            prompt="Security review of PR #99.",
        )
        assert req.role == "security-reviewer"

    def test_custom_context(self):
        req = SpawnRequest(
            discussion=445,
            role="executor",
            prompt="Do the work.",
            context={"branch": "discussion-445-foo", "spec_summary": "short"},
        )
        assert req.context["branch"] == "discussion-445-foo"

    def test_request_id_unique(self):
        r1 = SpawnRequest(discussion=1, role="executor", prompt="a")
        r2 = SpawnRequest(discussion=1, role="executor", prompt="a")
        assert r1.request_id != r2.request_id

    def test_serialise_round_trip(self):
        req = SpawnRequest(discussion=445, role="executor", prompt="Do the work.")
        data = req.model_dump()
        assert data["kind"] == "spawn_request"
        assert data["discussion"] == 445

        reconstructed = SpawnRequest(**data)
        assert reconstructed.discussion == req.discussion
        assert reconstructed.request_id == req.request_id


# ---------------------------------------------------------------------------
# SpawnRequest — validation rejects bad input
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not PYDANTIC_AVAILABLE, reason="Pydantic required for validation tests")
class TestSpawnRequestInvalid:
    def test_rejects_general_purpose_role(self):
        from pydantic import ValidationError
        # Pydantic's Literal constraint fires before our validator; either error is correct
        with pytest.raises(ValidationError):
            SpawnRequest(discussion=1, role="general-purpose", prompt="Do stuff.")

    def test_rejects_agent_role(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SpawnRequest(discussion=1, role="agent", prompt="Do stuff.")

    def test_rejects_agent_call_in_prompt(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="forbidden phrase"):
            SpawnRequest(
                discussion=1,
                role="executor",
                prompt="Please Agent(executor_prompt) to spawn the agent.",
            )

    def test_rejects_spawn_call_in_prompt(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="forbidden phrase"):
            SpawnRequest(
                discussion=1,
                role="executor",
                prompt="Use spawn(executor) to start work.",
            )

    def test_missing_required_fields(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SpawnRequest(role="executor", prompt="missing discussion")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# SpawnResult
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not PYDANTIC_AVAILABLE, reason="Pydantic required for validation tests")
class TestSpawnResult:
    def test_pass_verdict(self):
        rid = str(uuid.uuid4())
        result = SpawnResult(
            request_id=rid,
            verdict="pass",
            envelope={"agent": "code-reviewer", "pr": 99, "verdict": "pass"},
        )
        assert result.kind == "spawn_result"
        assert result.verdict == "pass"
        assert result.error is None

    def test_fail_with_error(self):
        rid = str(uuid.uuid4())
        result = SpawnResult(
            request_id=rid,
            verdict="fail",
            envelope={},
            error="Preflight failed: typecheck error in src/App.tsx",
        )
        assert result.error is not None
        assert "typecheck" in result.error

    def test_needs_fix_verdict(self):
        rid = str(uuid.uuid4())
        result = SpawnResult(request_id=rid, verdict="needs-fix", envelope={})
        assert result.verdict == "needs-fix"


# ---------------------------------------------------------------------------
# SpawnBlocked
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not PYDANTIC_AVAILABLE, reason="Pydantic required for validation tests")
class TestSpawnBlocked:
    def test_budget_exceeded(self):
        rid = str(uuid.uuid4())
        blocked = SpawnBlocked(
            request_id=rid,
            reason="budget_exceeded",
            reason_detail="Session ceiling 5M tokens reached.",
            retry_after_seconds=600,
        )
        assert blocked.kind == "spawn_blocked"
        assert blocked.reason == "budget_exceeded"
        assert blocked.retry_after_seconds == 600

    def test_concurrency_cap(self):
        rid = str(uuid.uuid4())
        blocked = SpawnBlocked(
            request_id=rid,
            reason="concurrency_cap_reached",
            retry_after_seconds=None,
        )
        assert blocked.retry_after_seconds is None


# ---------------------------------------------------------------------------
# parse_message dispatch
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not PYDANTIC_AVAILABLE, reason="Pydantic required for validation tests")
class TestParseMessage:
    def test_parse_spawn_request(self):
        data = {
            "kind": "spawn_request",
            "discussion": 445,
            "role": "executor",
            "prompt": "Implement the spec.",
        }
        msg = parse_message(data)
        assert isinstance(msg, SpawnRequest)
        assert msg.discussion == 445

    def test_parse_spawn_result(self):
        rid = str(uuid.uuid4())
        data = {
            "kind": "spawn_result",
            "request_id": rid,
            "verdict": "done",
            "envelope": {},
        }
        msg = parse_message(data)
        assert isinstance(msg, SpawnResult)
        assert msg.verdict == "done"

    def test_parse_spawn_blocked(self):
        rid = str(uuid.uuid4())
        data = {
            "kind": "spawn_blocked",
            "request_id": rid,
            "reason": "circuit_breaker_tripped",
        }
        msg = parse_message(data)
        assert isinstance(msg, SpawnBlocked)
        assert msg.reason == "circuit_breaker_tripped"

    def test_parse_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown message kind"):
            parse_message({"kind": "unknown_thing"})


# ---------------------------------------------------------------------------
# Label constant correctness
# ---------------------------------------------------------------------------

class TestLabelConstants:
    def test_pass_labels_present(self):
        assert ROLE_PASS_LABELS["code-reviewer"] == "code-review-passed"
        assert ROLE_PASS_LABELS["security-reviewer"] == "security-review-passed"

    def test_fail_labels_present(self):
        assert ROLE_FAIL_LABELS["code-reviewer"] == "code-review-needs-fix"
        assert ROLE_FAIL_LABELS["security-reviewer"] == "security-issue"

    def test_valid_roles_include_executor(self):
        assert "executor" in VALID_ROLES
        assert "code-reviewer" in VALID_ROLES
        assert "general-purpose" not in VALID_ROLES
