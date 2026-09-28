"""Tests for backend/spawn_payload.py.

D#1788 fix-round: the original bug was a missing `pr` key in the payload
dict — invisible to tests/test_spawn_pr_number_plumbing.py, which only
exercises spawn_templates.render_body() with hand-supplied vars (the
contract, not the plumbing). Reviewer proved this by reintroducing the bug
three ways and getting a clean pass on the whole spawn suite each time.
These tests close that gap by exercising build_payload() itself, the actual
function scripts/spawn-agent.sh calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_payload import build_payload  # noqa: E402


class TestBuildPayloadPrField:
    def test_pr_present_becomes_int(self):
        payload = build_payload({"_ROLE": "code-reviewer", "_DISC": "1761", "_PR": "1786"})
        assert payload["pr"] == 1786
        assert isinstance(payload["pr"], int)

    def test_pr_absent_is_none(self):
        payload = build_payload({"_ROLE": "code-reviewer", "_DISC": "1761"})
        assert payload["pr"] is None

    def test_pr_empty_string_is_none(self):
        # spawn-agent.sh always sets _PR (to "" when --pr wasn't given, via
        # ${PR_ARG:-}), never omits it — the empty-string case is the one
        # that actually happens on the wrapper's un-PR'd path.
        payload = build_payload({"_ROLE": "code-reviewer", "_DISC": "1761", "_PR": ""})
        assert payload["pr"] is None

    def test_pr_branch_present(self):
        payload = build_payload(
            {"_ROLE": "docs-writer", "_DISC": "1761", "_PR": "1786", "_PR_BRANCH": "feature/x"}
        )
        assert payload["pr_branch"] == "feature/x"

    def test_pr_branch_defaults_empty(self):
        payload = build_payload({"_ROLE": "docs-writer", "_DISC": "1761", "_PR": "1786"})
        assert payload["pr_branch"] == ""


class TestBuildPayloadOtherFields:
    """Sanity check the other 13 keys survive unchanged — the payload dict
    used to be built by an inline heredoc; this is the byte-for-byte parity
    check the reviewer ran across 8 environment matrices, kept as a fast
    regression here."""

    def test_role_and_discussion(self):
        payload = build_payload({"_ROLE": "executor", "_DISC": "42", "_TASK": "do it"})
        assert payload["role"] == "executor"
        assert payload["discussion"] == 42
        assert payload["task_prompt"] == "do it"

    def test_discussion_absent_is_none(self):
        payload = build_payload({"_ROLE": "executor"})
        assert payload["discussion"] is None

    def test_worktree_path_json_parsed(self):
        payload = build_payload({"_ROLE": "executor", "_WT_PATH": '"/tmp/wt-1"'})
        assert payload["worktree_path"] == "/tmp/wt-1"

    def test_worktree_unprovisioned_defaults_false(self):
        payload = build_payload({"_ROLE": "executor"})
        assert payload["worktree_unprovisioned"] is False

    def test_worktree_unprovisioned_set_from_env(self):
        payload = build_payload({"_ROLE": "executor", "_WT_UNPROVISIONED": "1"})
        assert payload["worktree_unprovisioned"] is True

    def test_gate_line_built_from_psc_gates(self):
        payload = build_payload(
            {
                "_ROLE": "executor",
                "PSC_JSON_INPUT": '{"gate_context": {"gates": {"lint_must_pass": true}}}',
            }
        )
        assert payload["gate_line"] == "[Control plane gates: lint_must_pass=True]"

    def test_all_twenty_keys_present(self):
        payload = build_payload({"_ROLE": "executor"})
        expected_keys = {
            "role",
            "discussion",
            "task_prompt",
            "persona_voice",
            "working_principles",
            "agent_scratchpad",
            "self_observe_gate",
            "gate_line",
            "worktree_path",
            "worktree_unprovisioned",
            "worktree_unprovisioned_reason",
            "security_block",
            "hook_event_id",
            "env_scrub_snippet",
            "prior_test_runs_block",
            "dial_state_at_spawn",
            "pr",
            "pr_branch",
            # D#2563: the repo plane #pr was resolved to.
            "pr_repo",
            # D#2644: "host" vs "static-only" for the three PR-scoped reviewers.
            "pr_host_execution",
        }
        assert set(payload.keys()) == expected_keys

    def test_agent_scratchpad_forwarded_from_psc(self):
        # D#2360 review round 1: this field reached pre-spawn-check.sh's
        # --dry-run JSON but was never forwarded here, so it never reached a
        # spawned agent. Mirrors the existing working_principles coverage.
        payload = build_payload(
            {
                "_ROLE": "executor",
                "PSC_JSON_INPUT": '{"agent_scratchpad": "## Scratchpad Convention\\nuse a subdirectory"}',
            }
        )
        assert payload["agent_scratchpad"] == "## Scratchpad Convention\nuse a subdirectory"

    def test_agent_scratchpad_defaults_empty(self):
        payload = build_payload({"_ROLE": "executor"})
        assert payload["agent_scratchpad"] == ""


class TestBuildPayloadPrHostExecution:
    """D#2644: build_payload calls backend.pr_execution_policy.resolve() for
    the three PR-scoped reviewer roles when a PR number is present, and never
    reads any environment variable for the mode itself — resolve()'s return
    value is the only input.
    """

    def test_calls_resolver_for_code_reviewer_with_pr(self, monkeypatch):
        import backend.pr_execution_policy as pep

        calls = []

        def fake_resolve(pr, pr_repo, discussion, **kwargs):
            calls.append((pr, pr_repo, discussion))
            return "host", "stubbed"

        monkeypatch.setattr(pep, "resolve", fake_resolve)
        payload = build_payload(
            {"_ROLE": "code-reviewer", "_DISC": "1761", "_PR": "1786", "_PR_REPO": "o/r"}
        )
        assert payload["pr_host_execution"] == "host"
        assert calls == [(1786, "o/r", 1761)]

    def test_security_reviewer_and_acceptance_tester_also_wired(self, monkeypatch):
        import backend.pr_execution_policy as pep

        monkeypatch.setattr(pep, "resolve", lambda pr, pr_repo, discussion, **kw: ("static-only", "x"))
        for role in ("security-reviewer", "acceptance-tester"):
            payload = build_payload({"_ROLE": role, "_PR": "99", "_PR_REPO": "o/r"})
            assert payload["pr_host_execution"] == "static-only"

    def test_other_roles_leave_pr_host_execution_empty(self, monkeypatch):
        import backend.pr_execution_policy as pep

        called = []
        monkeypatch.setattr(pep, "resolve", lambda *a, **kw: called.append(1) or ("host", "x"))
        payload = build_payload({"_ROLE": "executor", "_PR": "1786", "_PR_REPO": "o/r"})
        assert payload["pr_host_execution"] == ""
        assert called == []

    def test_reviewer_role_without_pr_leaves_pr_host_execution_empty(self, monkeypatch):
        import backend.pr_execution_policy as pep

        called = []
        monkeypatch.setattr(pep, "resolve", lambda *a, **kw: called.append(1) or ("host", "x"))
        payload = build_payload({"_ROLE": "code-reviewer"})
        assert payload["pr_host_execution"] == ""
        assert called == []

    def test_no_environment_variable_can_override_the_resolver(self, monkeypatch):
        # The whole point of D#2644: an operator (or a compromised PR) setting
        # any HOST_EXECUTION-shaped env var must not change the outcome —
        # only resolve()'s own return value may.
        import backend.pr_execution_policy as pep

        monkeypatch.setattr(pep, "resolve", lambda *a, **kw: ("static-only", "resolver says no"))
        payload = build_payload(
            {
                "_ROLE": "code-reviewer",
                "_DISC": "1761",
                "_PR": "1786",
                "_PR_REPO": "o/r",
                "_PR_HOST_EXECUTION": "host",
                "PR_HOST_EXECUTION": "host",
                "HOST_EXECUTION": "host",
            }
        )
        assert payload["pr_host_execution"] == "static-only"
