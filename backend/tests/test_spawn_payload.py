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

    def test_all_seventeen_keys_present(self):
        payload = build_payload({"_ROLE": "executor"})
        expected_keys = {
            "role",
            "discussion",
            "task_prompt",
            "persona_voice",
            "working_principles",
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
        }
        assert set(payload.keys()) == expected_keys
