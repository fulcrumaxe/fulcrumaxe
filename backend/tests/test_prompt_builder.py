"""Tests for backend/prompt_builder.py.

Covers:
- Each role default produces non-empty output
- Empty gate_line / persona_voice produce no stray section header
- worktree_path=None → no worktree block
- security_block=True → security block present
- prompt_manifest dict round-trips correctly
- Section order is canonical (PARTS order)
- VOLATILE_BOUNDARY is always present
- hook_event_id is emitted as hook_event_id=<value>
"""

import json
import sys
from pathlib import Path

import pytest

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.prompt_builder import SpawnPrompt, build_from_psc, _SECURITY_BLOCK, _REPO_ROOT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prompt(**kwargs) -> SpawnPrompt:
    """Create a SpawnPrompt with minimal required fields plus any overrides."""
    defaults = {
        "role": "executor",
        "task_prompt": "do the thing",
        "hook_event_id": "executor-42-1234567890",
        # Inject a known template body so tests don't depend on spawn_templates
        "_template_body_override": "## TEMPLATE_BODY\n\nsome role content here",
        "_checklist_block_override": "",  # skip checklist for most tests
    }
    defaults.update(kwargs)
    return SpawnPrompt(**defaults)


# ---------------------------------------------------------------------------
# Basic rendering
# ---------------------------------------------------------------------------


class TestBasicRendering:
    def test_render_returns_nonempty_string(self):
        sp = _make_prompt()
        result = sp.render()
        assert isinstance(result, str)
        assert len(result) > 50

    def test_render_ends_with_newline(self):
        sp = _make_prompt()
        assert sp.render().endswith("\n")

    def test_volatile_boundary_always_present(self):
        sp = _make_prompt()
        assert "VOLATILE_BOUNDARY" in sp.render()

    def test_hook_event_id_line(self):
        sp = _make_prompt(hook_event_id="executor-42-9999")
        result = sp.render()
        assert "hook_event_id=executor-42-9999" in result

    def test_task_prompt_in_output(self):
        sp = _make_prompt(task_prompt="implement feature X")
        result = sp.render()
        assert "implement feature X" in result


# ---------------------------------------------------------------------------
# Empty-field gate: no stray headers
# ---------------------------------------------------------------------------


class TestEmptyFields:
    def test_empty_persona_voice_no_output(self):
        sp = _make_prompt(persona_voice="")
        result = sp.render()
        # persona_voice section should simply be absent — no "## Voice" header
        assert "## Voice\n\nYou are" not in result

    def test_empty_gate_line_no_output(self):
        sp = _make_prompt(gate_line="")
        result = sp.render()
        assert "Control plane gates" not in result

    def test_empty_working_principles_no_output(self):
        sp = _make_prompt(working_principles="")
        result = sp.render()
        # Should not have any stray double-newline from an empty section
        assert "\n\n\n" not in result

    def test_nonempty_gate_line_is_present(self):
        sp = _make_prompt(gate_line="[Control plane gates: foo=true]")
        result = sp.render()
        assert "[Control plane gates: foo=true]" in result

    def test_nonempty_persona_voice_is_present(self):
        sp = _make_prompt(persona_voice="## Voice\n\nYou are Sam.")
        result = sp.render()
        assert "## Voice" in result
        assert "You are Sam." in result


# ---------------------------------------------------------------------------
# Worktree block
# ---------------------------------------------------------------------------


class TestWorktreeBlock:
    def test_no_worktree_block_when_path_is_none(self):
        sp = _make_prompt(worktree_path=None)
        result = sp.render()
        assert "YOUR WORKTREE" not in result

    def test_worktree_block_present_when_path_set(self):
        sp = _make_prompt(worktree_path="/tmp/wt-test-abc")
        result = sp.render()
        assert "YOUR WORKTREE: /tmp/wt-test-abc" in result
        assert f"Never write to {_REPO_ROOT}/" in result

    def test_worktree_block_after_volatile_boundary(self):
        sp = _make_prompt(worktree_path="/tmp/wt-test-xyz")
        result = sp.render()
        vb_pos = result.index("VOLATILE_BOUNDARY")
        wt_pos = result.index("YOUR WORKTREE")
        assert wt_pos > vb_pos

    def test_provisioned_block_is_byte_identical_to_today(self):
        # D#2014 constraint: adding the unprovisioned branch must not touch
        # the provisioned-path block's rendering in any way.
        sp = _make_prompt(worktree_path="/tmp/wt-locked")
        result = sp.render()
        expected = (
            "YOUR WORKTREE: /tmp/wt-locked\n"
            "Use this path as the absolute prefix for EVERY Edit/Write call.\n"
            f"Never write to {_REPO_ROOT}/<file> — that is main, not your worktree.\n"
            "All file paths passed to Edit or Write MUST start with this worktree path.\n"
            "Before your first Edit/Write, run: pwd   to confirm your absolute worktree root.\n"
            "\n"
            "IMPORTANT — state symlinks: run this as your very first Bash step to ensure\n"
            ".autonomous-team/ state files point to the shared external state dir (not forked copies):\n"
            "  bash scripts/setup-state-dir.sh\n"
            "This is idempotent — safe to run every session."
        )
        assert expected in result


# ---------------------------------------------------------------------------
# Unprovisioned worktree block (D#2014)
# ---------------------------------------------------------------------------


class TestUnprovisionedWorktreeBlock:
    def test_absent_when_not_requested(self):
        # worktree_path=None and worktree_unprovisioned=False (default): no
        # isolation was requested at all — stay silent, exactly like before.
        sp = _make_prompt(worktree_path=None)
        result = sp.render()
        assert "YOUR WORKTREE" not in result
        assert "worktree_not_provisioned" not in result
        assert "NO WORKTREE WAS PROVISIONED" not in result

    def test_present_when_requested_but_unresolved(self):
        sp = _make_prompt(worktree_path=None, worktree_unprovisioned=True)
        result = sp.render()
        assert "NO WORKTREE WAS PROVISIONED" in result
        assert "worktree_not_provisioned" in result
        assert "verdict: fail" in result

    def test_never_contains_your_worktree_phrase(self):
        sp = _make_prompt(worktree_path=None, worktree_unprovisioned=True)
        result = sp.render()
        assert "YOUR WORKTREE" not in result

    def test_never_contains_never_write_to_line(self):
        # acceptance_files check (test_prompt_builder.py): the unprovisioned
        # block must not carry the provisioned block's "Never write to" line —
        # that line only makes sense once there IS a real worktree to write to.
        sp = _make_prompt(worktree_path=None, worktree_unprovisioned=True)
        result = sp.render()
        assert "Never write to" not in result

    def test_real_path_wins_over_unprovisioned_flag(self):
        # A concrete path always takes priority — worktree_unprovisioned is
        # only consulted when worktree_path is falsy.
        sp = _make_prompt(worktree_path="/tmp/wt-real", worktree_unprovisioned=True)
        result = sp.render()
        assert "YOUR WORKTREE: /tmp/wt-real" in result
        assert "NO WORKTREE WAS PROVISIONED" not in result

    def test_unprovisioned_block_after_volatile_boundary(self):
        sp = _make_prompt(worktree_path=None, worktree_unprovisioned=True)
        result = sp.render()
        vb_pos = result.index("VOLATILE_BOUNDARY")
        wt_pos = result.index("NO WORKTREE WAS PROVISIONED")
        assert wt_pos > vb_pos

    def test_readonly_role_gets_verify_tree_pointer(self):
        sp = _make_prompt(role="code-reviewer", worktree_path=None, worktree_unprovisioned=True)
        result = sp.render()
        assert "verify-tree.sh" in result
        assert "verify_tree_build" in result

    def test_mutating_role_gets_no_verify_tree_pointer(self):
        sp = _make_prompt(role="executor", worktree_path=None, worktree_unprovisioned=True)
        result = sp.render()
        assert "verify-tree.sh" not in result


# ---------------------------------------------------------------------------
# Unprovisioned worktree block, "agent tool provisions" reason (D#2222)
#
# The canonical fresh-spawn shape (--isolation worktree, no --pr, no
# --worktree-path — see scripts/lib/team-lead-prompts.sh's EXECUTOR snippet)
# used to render the same "NO WORKTREE WAS PROVISIONED ... hard-fail" block
# as a genuine pr_tree_provision failure, even though nothing had actually
# failed: the Agent tool's own isolation param provisions the real tree.
# That was D#2222 — the canonical spawn shape self-reported as broken.
# ---------------------------------------------------------------------------


class TestUnprovisionedAgentToolProvidesReason:
    def test_default_reason_keeps_old_hard_fail_wording(self):
        # No reason supplied (the pr_tree_failed / unknown case) must keep
        # rendering exactly the pre-existing hard-fail block — this is the
        # genuine-failure path and must still tell the agent to stop.
        sp = _make_prompt(worktree_path=None, worktree_unprovisioned=True)
        result = sp.render()
        assert "NO WORKTREE WAS PROVISIONED" in result
        assert "agent_tool_provisions" not in result

    def test_pr_tree_failed_reason_keeps_old_hard_fail_wording(self):
        sp = _make_prompt(
            worktree_path=None,
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="pr_tree_failed",
        )
        result = sp.render()
        assert "NO WORKTREE WAS PROVISIONED" in result
        assert "worktree_not_provisioned" in result

    def test_agent_tool_provisions_reason_does_not_claim_no_worktree(self):
        sp = _make_prompt(
            worktree_path=None,
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="agent_tool_provisions",
        )
        result = sp.render()
        assert "NO WORKTREE WAS PROVISIONED" not in result
        assert "Agent tool" in result

    def test_agent_tool_provisions_reason_still_guards_the_real_failure_case(self):
        # Even in the honest-message branch, if the agent finds itself at
        # the literal repo root (isolation truly wasn't applied), it must
        # still be told to hard-fail rather than proceed.
        sp = _make_prompt(
            worktree_path=None,
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="agent_tool_provisions",
        )
        result = sp.render()
        assert "worktree_not_provisioned" in result
        assert "verdict: fail" in result

    def test_agent_tool_provisions_reason_tells_agent_to_proceed_otherwise(self):
        sp = _make_prompt(
            worktree_path=None,
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="agent_tool_provisions",
        )
        result = sp.render()
        assert "proceed normally" in result

    def test_real_path_wins_over_reason_too(self):
        sp = _make_prompt(
            worktree_path="/tmp/wt-real",
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="agent_tool_provisions",
        )
        result = sp.render()
        assert "YOUR WORKTREE: /tmp/wt-real" in result
        assert "Agent tool" not in result


# ---------------------------------------------------------------------------
# Unprovisioned worktree block, "pr_resolution_failed" reason (D#2222 review
# follow-up on PR #2231).
#
# A --pr amend spawn where gh api fails to resolve the PR's head sha never
# reaches pr_tree_provision at all (spawn-agent.sh's _PA_SHA_FULL stays
# empty). The original fix collapsed that case into "agent_tool_provisions"
# because both share "PR_ARG branch condition is false" — but they are not
# the same: for a --pr amend, the Agent tool's own auto-provisioned tree is
# NOT the PR's branch, so telling the agent it's safe to proceed there would
# make it silently amend the wrong tree. This must hard-fail, distinctly
# from both the canonical fresh-spawn case and the generic pr_tree_failed
# case (so a reader of the rendered block, or of logs/audits keyed on the
# reason string, can tell the two apart).
# ---------------------------------------------------------------------------


class TestUnprovisionedPrResolutionFailedReason:
    def test_does_not_collapse_into_agent_tool_provisions(self):
        sp = _make_prompt(
            worktree_path=None,
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="pr_resolution_failed",
        )
        result = sp.render()
        assert "Agent tool provisions" not in result
        assert "proceed normally" not in result

    def test_hard_fails_like_a_genuine_provisioning_failure(self):
        sp = _make_prompt(
            worktree_path=None,
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="pr_resolution_failed",
        )
        result = sp.render()
        assert "NO WORKTREE WAS PROVISIONED" in result
        assert "worktree_not_provisioned" in result
        assert "verdict: fail" in result

    def test_names_the_pr_amend_risk_explicitly(self):
        # This is the substantive difference from the generic hard-fail
        # block: it must explain WHY proceeding is unsafe here specifically
        # (any tree the agent lands in is not the PR's branch), not just
        # that no tree was provisioned.
        sp = _make_prompt(
            worktree_path=None,
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="pr_resolution_failed",
        )
        result = sp.render()
        assert "amend" in result.lower()
        assert "not the pr" in result.lower() or "not the pr's branch" in result.lower()

    def test_real_path_wins_over_pr_resolution_failed_reason(self):
        sp = _make_prompt(
            worktree_path="/tmp/wt-real",
            worktree_unprovisioned=True,
            worktree_unprovisioned_reason="pr_resolution_failed",
        )
        result = sp.render()
        assert "YOUR WORKTREE: /tmp/wt-real" in result
        assert "NO WORKTREE WAS PROVISIONED" not in result


# ---------------------------------------------------------------------------
# Security block
# ---------------------------------------------------------------------------


class TestSecurityBlock:
    def test_security_block_absent_by_default(self):
        sp = _make_prompt(security_block=False)
        result = sp.render()
        assert "SECURITY CONTEXT" not in result

    def test_security_block_present_when_set(self):
        sp = _make_prompt(security_block=True)
        result = sp.render()
        assert "SECURITY CONTEXT" in result
        assert _SECURITY_BLOCK in result

    def test_security_block_after_volatile_boundary(self):
        sp = _make_prompt(security_block=True)
        result = sp.render()
        vb_pos = result.index("VOLATILE_BOUNDARY")
        sec_pos = result.index("SECURITY CONTEXT")
        assert sec_pos > vb_pos


# ---------------------------------------------------------------------------
# Prompt manifest
# ---------------------------------------------------------------------------


class TestPromptManifest:
    def test_no_manifest_line_when_empty(self):
        sp = _make_prompt(prompt_manifest={})
        result = sp.render()
        assert "prompt_manifest=" not in result

    def test_manifest_round_trips(self):
        manifest = {"manifest": "executor.tmpl@abc123", "fragments": {"bash-discipline": "def456"}}
        sp = _make_prompt(prompt_manifest=manifest)
        result = sp.render()
        assert "prompt_manifest=" in result
        # Extract the manifest line
        for line in result.splitlines():
            if line.startswith("prompt_manifest="):
                recovered = json.loads(line[len("prompt_manifest="):])
                assert recovered == manifest
                break
        else:
            pytest.fail("prompt_manifest= line not found in output")

    def test_manifest_is_last_nonempty_line(self):
        manifest = {"manifest": "executor.tmpl@abc123"}
        sp = _make_prompt(prompt_manifest=manifest, hook_event_id="executor-1-ts")
        result = sp.render()
        lines = [l for l in result.splitlines() if l.strip()]
        assert lines[-1].startswith("prompt_manifest=")


# ---------------------------------------------------------------------------
# Section order
# ---------------------------------------------------------------------------


class TestSectionOrder:
    def test_task_prompt_after_volatile_boundary(self):
        sp = _make_prompt(task_prompt="the task")
        result = sp.render()
        vb = result.index("VOLATILE_BOUNDARY")
        task = result.index("the task")
        assert task > vb

    def test_hook_event_id_after_task_prompt(self):
        sp = _make_prompt(task_prompt="the task", hook_event_id="role-42-ts")
        result = sp.render()
        task_pos = result.index("the task")
        hook_pos = result.index("hook_event_id=role-42-ts")
        assert hook_pos > task_pos

    def test_persona_voice_before_volatile_boundary(self):
        sp = _make_prompt(persona_voice="## Voice\n\nYou are Sam.")
        result = sp.render()
        voice_pos = result.index("## Voice")
        vb_pos = result.index("VOLATILE_BOUNDARY")
        assert voice_pos < vb_pos

    def test_template_body_before_persona_voice(self):
        sp = _make_prompt(
            _template_body_override="TEMPLATE_BODY_SENTINEL",
            persona_voice="## Voice\n\nYou are Sam.",
        )
        result = sp.render()
        tmpl_pos = result.index("TEMPLATE_BODY_SENTINEL")
        voice_pos = result.index("## Voice")
        assert tmpl_pos < voice_pos


# ---------------------------------------------------------------------------
# Role defaults (smoke test each known role)
# ---------------------------------------------------------------------------

_KNOWN_ROLES = [
    "executor",
    "code-reviewer",
    "security-reviewer",
    "acceptance-tester",
    "project-manager",
    "docs-writer",
]


class TestRoleDefaults:
    @pytest.mark.parametrize("role", _KNOWN_ROLES)
    def test_each_role_produces_output(self, role):
        sp = SpawnPrompt(
            role=role,
            task_prompt="smoke test",
            hook_event_id=f"{role}-1-ts",
            _template_body_override=f"## {role} BODY",
            _checklist_block_override="",
        )
        result = sp.render()
        assert len(result) > 30
        assert "VOLATILE_BOUNDARY" in result
        assert f"hook_event_id={role}-1-ts" in result


# ---------------------------------------------------------------------------
# D#1788 fix-round: PR plumbing through the REAL template-loading path
# ---------------------------------------------------------------------------
# Deliberately does NOT use _template_body_override — the reviewer proved
# tests/test_spawn_pr_number_plumbing.py only exercises
# spawn_templates.render_body() with hand-supplied vars (the contract, not
# the plumbing) by reintroducing the original bug three ways and re-running
# the whole spawn suite after each. Re-verified here, corrected from an
# earlier, overstated version of this comment: mutations 1 and 2
# (spawn_payload always returning pr=None; prompt_builder hardcoding
# template_vars["pr_number"] = "") ARE caught, each failing one test below.
# Mutation 3 (the spawn wrapper's own env-var line hardcoded to `_PR=""`
# instead of `_PR="${PR_ARG:-}"`) is NOT caught here or anywhere in this
# suite — that mutation is in the shell script itself, and nothing in the
# test suite executes the real script (sandbox-forbidden for this repo's
# subagents; also true for the reviewer's own re-run). backend/tests/
# test_spawn_payload.py's test_pr_empty_string_is_none constructs an empty
# `_PR` and checks build_payload()'s response to it, which is a different
# claim: it proves build_payload() is correct GIVEN that input, not that the
# wrapper is correct in PRODUCING it. What actually closes that gap is
# structural, not a test: the contract turns "_PR="" resolves to an empty
# pr_number" into a hard `exit 1` with stderr naming pr_number (see
# backend/prompt_builder.py's _main_render), so the failure mode this whole
# fix-round exists for — a silent blank slot in a PR-scoped role's rendered
# instructions — cannot recur regardless of which of the three layers
# regresses. That is stronger than a test that only catches one specific
# mutation, but it is not the same claim as "all three mutations are
# caught here," which was false.


class TestPrPlumbingThroughRealTemplate:
    def test_pr_number_reaches_code_reviewer_template(self):
        sp = SpawnPrompt(
            role="code-reviewer",
            discussion=1761,
            pr=1786,
            task_prompt="review it",
            hook_event_id="code-reviewer-1761-1",
        )
        result = sp.render()
        assert "Review PR #1786 for Discussion #1761" in result
        assert "{{" not in result

    def test_pr_branch_and_pr_url_reach_docs_writer_template(self):
        sp = SpawnPrompt(
            role="docs-writer",
            discussion=1761,
            pr=1786,
            pr_branch="feature/example",
            task_prompt="update docs",
            hook_event_id="docs-writer-1761-1",
        )
        result = sp.render()
        assert "PR #1786" in result
        assert "PR branch: feature/example" in result
        assert "/pull/1786" in result

    def test_missing_pr_raises_for_pr_scoped_role(self):
        sp = SpawnPrompt(
            role="code-reviewer",
            discussion=1761,
            task_prompt="review it",
            hook_event_id="code-reviewer-1761-1",
        )
        with pytest.raises(ValueError) as exc_info:
            sp.render()
        assert "pr_number" in str(exc_info.value)

    def test_role_without_pr_reference_does_not_need_pr(self):
        # executor.tmpl never references {{pr_number}} — omitting pr must
        # not raise for a role that doesn't need it.
        sp = SpawnPrompt(
            role="executor",
            discussion=1761,
            task_prompt="do the thing",
            hook_event_id="executor-1761-1",
        )
        result = sp.render()
        assert "{{" not in result


# ---------------------------------------------------------------------------
# build_from_psc factory
# ---------------------------------------------------------------------------


class TestBuildFromPsc:
    def _sample_psc(self) -> dict:
        return {
            "persona_voice": "## Voice\n\nYou are Sam.",
            "working_principles": "## Working Principles\n\n1. Think",
            "self_observe_gate": "## Self-Observe\n\nGate active.",
            "gate_context": {
                "gates": {
                    "auto_merge": True,
                    "security_review": True,
                }
            },
        }

    # D#1788: discussion=42 is now required on every build_from_psc() call
    # below that goes through the real executor.tmpl render (i.e. doesn't
    # override _template_body_override) — executor.tmpl references
    # {{discussion_number}}/{{discussion_url}}, both now enforced non-empty
    # by the new referenced-var contract, and the render() path derives
    # discussion_url from `discussion` itself. These tests exercise
    # persona_voice/gate_line/worktree/security_block, all orthogonal to
    # the discussion context, so a fixed stand-in value is enough.

    def test_persona_voice_injected(self):
        psc = self._sample_psc()
        sp = build_from_psc("executor", psc, discussion=42, task_prompt="do it", hook_event_id="ev-1")
        result = sp.render()
        assert "You are Sam." in result

    def test_gate_line_from_gates(self):
        psc = self._sample_psc()
        sp = build_from_psc("executor", psc, discussion=42, task_prompt="do it", hook_event_id="ev-1")
        result = sp.render()
        assert "Control plane gates:" in result
        assert "auto_merge=True" in result

    def test_empty_gates_no_gate_line_in_volatile_section(self):
        psc = {**self._sample_psc(), "gate_context": {"gates": {}}}
        sp = build_from_psc("executor", psc, discussion=42, task_prompt="do it", hook_event_id="ev-1")
        result = sp.render()
        # When gates are empty, no gate_line is appended after the VOLATILE_BOUNDARY.
        # (The template body may still contain a [Control plane gates: ] line from {{gate_context}} substitution)
        vb_pos = result.index("VOLATILE_BOUNDARY")
        volatile_section = result[vb_pos:]
        assert "[Control plane gates:" not in volatile_section

    def test_worktree_path_injected(self):
        psc = self._sample_psc()
        sp = build_from_psc(
            "executor", psc,
            discussion=42,
            task_prompt="do it",
            hook_event_id="ev-1",
            worktree_path="/tmp/wt-abc",
        )
        result = sp.render()
        assert "YOUR WORKTREE: /tmp/wt-abc" in result

    def test_security_block_injected(self):
        psc = self._sample_psc()
        sp = build_from_psc(
            "executor", psc,
            discussion=42,
            task_prompt="do it",
            hook_event_id="ev-1",
            security_block=True,
        )
        result = sp.render()
        assert "SECURITY CONTEXT" in result

    def test_worktree_unprovisioned_injected(self):
        psc = self._sample_psc()
        sp = build_from_psc(
            "executor", psc,
            discussion=42,
            task_prompt="do it",
            hook_event_id="ev-1",
            worktree_unprovisioned=True,
        )
        result = sp.render()
        assert "NO WORKTREE WAS PROVISIONED" in result
        assert "YOUR WORKTREE" not in result


# ---------------------------------------------------------------------------
# Env-scrub block
# ---------------------------------------------------------------------------


class TestEnvScrubBlock:
    """D#1956: the prompt-injection scrub lane was removed — it was denied by
    the permission classifier on every observed spawn, and even a permitted
    `unset` would only have protected the single fresh-shell Bash call it ran
    in. `env_scrub_snippet` is still accepted on the dataclass for payload
    compatibility but must never be rendered, regardless of content."""

    def test_no_scrub_block_when_empty(self):
        sp = _make_prompt(env_scrub_snippet="")
        result = sp.render()
        assert "SECURITY — run this as your FIRST Bash step" not in result

    def test_no_scrub_block_even_when_snippet_set(self):
        sp = _make_prompt(env_scrub_snippet="unset GH_TOKEN ANTHROPIC_API_KEY")
        result = sp.render()
        assert "SECURITY — run this as your FIRST Bash step" not in result
        assert "unset GH_TOKEN ANTHROPIC_API_KEY" not in result

    def test_no_credentials_prose_anywhere_in_render(self):
        # Split across concatenation so this test file itself doesn't trip a
        # grep for the exact removed sentence (D#1956 acceptance criterion 3
        # requires zero matches for that phrase in backend/ outside archive/).
        removed_prose = "removes inherited API " + "credentials"
        sp = _make_prompt(env_scrub_snippet="unset GH_TOKEN")
        result = sp.render()
        assert removed_prose not in result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


class TestCLI:
    def test_render_subcommand(self, capsys):
        import io
        import unittest.mock as mock

        input_data = json.dumps({
            "role": "executor",
            "discussion": 42,
            "task_prompt": "cli test prompt",
            "hook_event_id": "executor-42-cli",
            "persona_voice": "",
            "working_principles": "",
            "self_observe_gate": "",
        })

        with mock.patch.dict(__import__("os").environ, {"SPAWN_PROMPT_JSON": input_data}):
            from backend.prompt_builder import main
            rc = main(["render"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "cli test prompt" in captured.out
        assert "hook_event_id=executor-42-cli" in captured.out

    def test_empty_input_returns_error(self, capsys):
        import unittest.mock as mock

        with mock.patch.dict(__import__("os").environ, {}, clear=False):
            # Remove SPAWN_PROMPT_JSON if set
            env = dict(__import__("os").environ)
            env.pop("SPAWN_PROMPT_JSON", None)
            with mock.patch.dict(__import__("os").environ, env, clear=True):
                with mock.patch("sys.stdin") as mock_stdin:
                    mock_stdin.read.return_value = ""
                    from backend.prompt_builder import _main_render
                    rc = _main_render([])

        assert rc == 1

    def test_json_decode_error_returns_error(self, capsys):
        import unittest.mock as mock

        with mock.patch.dict(__import__("os").environ, {"SPAWN_PROMPT_JSON": "not json"}):
            from backend.prompt_builder import _main_render
            rc = _main_render([])

        captured = capsys.readouterr()
        assert rc == 1
        assert "JSONDecodeError" in captured.err

    def test_worktree_unprovisioned_round_trips_through_cli(self, capsys):
        import unittest.mock as mock

        input_data = json.dumps({
            "role": "executor",
            "discussion": 42,
            "task_prompt": "cli test prompt",
            "hook_event_id": "executor-42-cli",
            "worktree_unprovisioned": True,
        })

        with mock.patch.dict(__import__("os").environ, {"SPAWN_PROMPT_JSON": input_data}):
            from backend.prompt_builder import main
            rc = main(["render"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "worktree_not_provisioned" in captured.out
        assert "YOUR WORKTREE" not in captured.out

    def test_worktree_unprovisioned_reason_round_trips_through_cli(self, capsys):
        # D#2222: scripts/spawn-agent.sh sets worktree_unprovisioned_reason
        # via backend/spawn_payload.py's SPAWN_PROMPT_JSON — confirm the CLI
        # path (the one actually exercised in production) honors it and does
        # not fall back to the generic hard-fail wording.
        import unittest.mock as mock

        input_data = json.dumps({
            "role": "executor",
            "discussion": 42,
            "task_prompt": "cli test prompt",
            "hook_event_id": "executor-42-cli",
            "worktree_unprovisioned": True,
            "worktree_unprovisioned_reason": "agent_tool_provisions",
        })

        with mock.patch.dict(__import__("os").environ, {"SPAWN_PROMPT_JSON": input_data}):
            from backend.prompt_builder import main
            rc = main(["render"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "NO WORKTREE WAS PROVISIONED" not in captured.out
        assert "Agent tool" in captured.out
        assert "YOUR WORKTREE" not in captured.out
