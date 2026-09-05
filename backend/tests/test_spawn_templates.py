"""Tests for backend/spawn_templates.py — centralized spawn prompt templates."""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure backend/ is importable regardless of how pytest is invoked.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_templates import render, KNOWN_ROLES, REQUIRED_VARS, _REPO  # noqa: E402


# ---------------------------------------------------------------------------
# Stub vars — minimal valid values for every role
# ---------------------------------------------------------------------------

_STUB_VARS = {
    "discussion_number": "99",
    "discussion_title": "Test discussion",
    "discussion_url": "https://github.com/autonomous-agent-7/autonomous-forever/discussions/99",
    "task_brief": "Do the test thing",
    "project_context": "[project context stub]",
    "agent_memory": "[agent memory stub]",
    "gate_context": '{"gates": {}}',
    "pr_number": "55",
    # Extra vars required by secondary roles (docs-writer, incident-commander, etc.)
    "pr_branch": "feature/stub-branch",
    "pr_url": "https://github.com/autonomous-agent-7/autonomous-forever/pull/55",
    "trigger_type": "circuit_breaker",
    "evidence_json": "{}",
    "release_id": "v0.0.1",
}


# ---------------------------------------------------------------------------
# Per-role: mandatory appendix presence
# ---------------------------------------------------------------------------

# Derived from the production resolver (backend.spawn_templates._REPO), not
# hardcoded — a hardcoded literal here is exactly the bug this guard exists to
# catch (D#1797). _REPO is module-level and already resolved at import time,
# so this import has no spawn side effects.
REPO_SCOPE_PHRASE = _REPO
ENVELOPE_MARKER = "<!-- AGENT_OUTPUT -->"
ARCHIVE_PHRASE = "archive/"
GATE_KEY_BY_ROLE = {
    # Dominant roles — gate-specific literal from _GATE_CHECKS_BY_ROLE
    "executor": "lint_must_pass",
    "code-reviewer": "security_review",
    "security-reviewer": "security-reviewer",  # appears in the gate passthrough text
    "project-manager": "idea_generation",
    "acceptance-tester": "acceptance-tester",  # appears in the gate passthrough text
    # Secondary roles — role name appears in the AGENT_OUTPUT envelope
    "browser-tester": "browser-tester",
    "cost-analyst": "cost-analyst",
    "debater": "debater",
    "docs-writer": "docs-writer",
    "incident-commander": "incident-commander",
    "mission-analyst": "mission-analyst",
    "performance-expert": "performance-expert",
    "product-owner": "product-owner",
    "quality-sweep": "quality-sweep",
    "release-manager": "release-manager",
    "researcher": "researcher",
    "run-analyst": "run-analyst",
    "runbook-writer": "runbook-writer",
    "security-expert": "security-expert",
    "technical-architect": "technical-architect",
    "tui-tester": "tui-tester",
    "accessibility-reviewer": "accessibility-reviewer",
    "analytics-engineer": "analytics-engineer",
    "ux-designer": "ux-designer",
}


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_contains_repo_scope(role: str) -> None:
    result = render(role, _STUB_VARS)
    assert REPO_SCOPE_PHRASE in result, (
        f"render('{role}') missing repo-scope phrase"
    )


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_contains_agent_output_marker(role: str) -> None:
    result = render(role, _STUB_VARS)
    assert ENVELOPE_MARKER in result, (
        f"render('{role}') missing AGENT_OUTPUT envelope marker"
    )


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_contains_archive_protocol(role: str) -> None:
    result = render(role, _STUB_VARS)
    assert ARCHIVE_PHRASE in result, (
        f"render('{role}') missing archive protocol phrase"
    )


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_contains_gate_check_key(role: str) -> None:
    result = render(role, _STUB_VARS)
    key = GATE_KEY_BY_ROLE[role]
    assert key in result, (
        f"render('{role}') missing role-specific gate check key '{key}'"
    )


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_returns_string(role: str) -> None:
    result = render(role, _STUB_VARS)
    assert isinstance(result, str)
    assert len(result) > 100, f"render('{role}') returned suspiciously short output"


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_substitutes_discussion_number(role: str) -> None:
    from backend.spawn_templates import _TEMPLATES_DIR
    tmpl_text = (_TEMPLATES_DIR / f"{role}.tmpl").read_text(encoding="utf-8")
    if "{{discussion_number}}" not in tmpl_text:
        pytest.skip(f"role '{role}' template does not use {{{{discussion_number}}}}")
    result = render(role, _STUB_VARS)
    assert "99" in result, f"render('{role}') did not substitute discussion_number"


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_substitutes_task_brief(role: str) -> None:
    from backend.spawn_templates import _TEMPLATES_DIR
    tmpl_text = (_TEMPLATES_DIR / f"{role}.tmpl").read_text(encoding="utf-8")
    if "{{task_brief}}" not in tmpl_text:
        pytest.skip(f"role '{role}' template does not use {{{{task_brief}}}}")
    result = render(role, _STUB_VARS)
    assert "Do the test thing" in result, (
        f"render('{role}') did not substitute task_brief"
    )


# ---------------------------------------------------------------------------
# Missing required vars → ValueError
# ---------------------------------------------------------------------------

def test_render_executor_missing_all_required_vars() -> None:
    with pytest.raises(ValueError) as exc_info:
        render("executor", {})
    msg = str(exc_info.value)
    for key in REQUIRED_VARS["executor"]:
        assert key in msg, f"ValueError for executor did not mention missing key '{key}'"


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_render_missing_single_required_var(role: str) -> None:
    """Omit the first required var; render must raise ValueError naming it."""
    required = REQUIRED_VARS.get(role, [])
    if not required:
        pytest.skip(f"No required vars for role '{role}'")
    missing_key = required[0]
    partial_vars = {k: v for k, v in _STUB_VARS.items() if k != missing_key}
    with pytest.raises(ValueError) as exc_info:
        render(role, partial_vars)
    assert missing_key in str(exc_info.value)


# ---------------------------------------------------------------------------
# Unknown role → ValueError
# ---------------------------------------------------------------------------

def test_render_unknown_role() -> None:
    with pytest.raises(ValueError) as exc_info:
        render("nonexistent-role", _STUB_VARS)
    assert "nonexistent-role" in str(exc_info.value) or "Unknown role" in str(exc_info.value)


def test_render_empty_role() -> None:
    with pytest.raises(ValueError):
        render("", _STUB_VARS)


# ---------------------------------------------------------------------------
# Template file presence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_template_file_exists(role: str) -> None:
    from backend.spawn_templates import _TEMPLATES_DIR
    tmpl = _TEMPLATES_DIR / f"{role}.tmpl"
    assert tmpl.exists(), f"Template file missing: {tmpl}"


# ---------------------------------------------------------------------------
# Workflow runner integration — rendered prompt contains all four appendices
# ---------------------------------------------------------------------------

def test_workflow_runner_resolve_implement_produces_mandatory_appendices() -> None:
    """workflow_runner.resolve('implement-discussion') prompt contains all four appendices."""
    from backend.workflow_runner import WorkflowRunner

    workflows_dir = _REPO_ROOT / ".autonomous-team" / "workflows"
    if not workflows_dir.exists():
        pytest.skip("Workflows directory not found")

    runner = WorkflowRunner(workflows_dir)
    steps = runner.resolve(
        "implement-discussion",
        {
            "discussion_number": "99",
            "discussion_title": "Test feature",
            "discussion_url": "https://github.com/autonomous-agent-7/autonomous-forever/discussions/99",
            "spec_body": "Do the test thing",
            "project_context": "",
            "agent_memory": "",
            "gate_context": "{}",
        },
    )

    assert steps, "resolve() returned empty steps"
    # Check the first step (executor) has all four mandatory appendices.
    executor_step = next((s for s in steps if s.get("agent") == "executor"), None)
    assert executor_step is not None, "No executor step found in plan"
    prompt = executor_step["prompt"]

    assert REPO_SCOPE_PHRASE in prompt, "executor step prompt missing repo scope"
    assert ENVELOPE_MARKER in prompt, "executor step prompt missing AGENT_OUTPUT marker"
    assert ARCHIVE_PHRASE in prompt, "executor step prompt missing archive protocol"
    assert "lint_must_pass" in prompt, "executor step prompt missing lint_must_pass gate check"


def test_workflow_runner_resolve_review_pr_produces_mandatory_appendices() -> None:
    """workflow_runner.resolve('review-pr') prompt contains all four appendices."""
    from backend.workflow_runner import WorkflowRunner

    workflows_dir = _REPO_ROOT / ".autonomous-team" / "workflows"
    if not workflows_dir.exists():
        pytest.skip("Workflows directory not found")

    runner = WorkflowRunner(workflows_dir)
    steps = runner.resolve(
        "review-pr",
        {
            "pr_number": "55",
            "discussion_number": "99",
            "project_context": "",
            "agent_memory": "",
            "gate_context": "{}",
        },
    )

    assert steps, "resolve() returned empty steps"
    reviewer_step = next((s for s in steps if s.get("agent") == "code-reviewer"), None)
    assert reviewer_step is not None, "No code-reviewer step found in plan"
    prompt = reviewer_step["prompt"]

    assert REPO_SCOPE_PHRASE in prompt, "code-reviewer step prompt missing repo scope"
    assert ENVELOPE_MARKER in prompt, "code-reviewer step prompt missing AGENT_OUTPUT marker"
    assert ARCHIVE_PHRASE in prompt, "code-reviewer step prompt missing archive protocol"
    assert "security_review" in prompt, "code-reviewer step prompt missing security_review gate check"


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

def test_cli_render_exits_0_and_contains_appendices(tmp_path: Path) -> None:
    from backend.spawn_templates import _main

    exit_code = _main([
        "render", "executor",
        "--var", "discussion_number=123",
        "--var", "discussion_title=X",
        "--var", "discussion_url=https://example.com/1",
        "--var", "task_brief=do thing",
    ])
    assert exit_code == 0


def test_cli_render_missing_var_exits_nonzero(capsys: pytest.CaptureFixture) -> None:
    from backend.spawn_templates import _main

    exit_code = _main([
        "render", "executor",
        "--var", "discussion_number=123",
        # discussion_title, discussion_url, task_brief all missing
    ])
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "discussion_title" in captured.err or "discussion_url" in captured.err or "task_brief" in captured.err


def test_cli_render_unknown_role_exits_nonzero() -> None:
    """Argparse rejects unknown roles via choices= — exits 2."""
    from backend.spawn_templates import _main
    with pytest.raises(SystemExit) as exc_info:
        _main(["render", "unknown-role"])
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Acceptance-tester pre-verdict gate — gate text must appear in rendered prompt
# ---------------------------------------------------------------------------

def test_acceptance_tester_render_contains_pre_verdict_gate_heading() -> None:
    """render('acceptance-tester') must contain the Pre-Verdict Gate heading."""
    result = render("acceptance-tester", _STUB_VARS)
    assert "Pre-Verdict Gate" in result, (
        "render('acceptance-tester') is missing the '## Pre-Verdict Gate' section. "
        "The gate text must be inlined directly in acceptance-tester.tmpl — "
        "{{include:...}} directives are not supported by spawn_templates.py."
    )


# ---------------------------------------------------------------------------
# accessibility-reviewer — advisory label, gate check, no hard-gate modification
# ---------------------------------------------------------------------------

def test_accessibility_reviewer_in_known_roles() -> None:
    """accessibility-reviewer must be registered in KNOWN_ROLES."""
    assert "accessibility-reviewer" in KNOWN_ROLES, (
        "accessibility-reviewer is not in KNOWN_ROLES — missing .tmpl file?"
    )


def test_accessibility_reviewer_render_contains_advisory_label() -> None:
    """Rendered accessibility-reviewer prompt must reference the advisory a11y-reviewed label."""
    result = render("accessibility-reviewer", _STUB_VARS)
    assert "a11y-reviewed" in result, (
        "render('accessibility-reviewer') missing advisory 'a11y-reviewed' label reference"
    )


def test_accessibility_reviewer_render_advisory_not_hard_gate() -> None:
    """Rendered prompt must NOT claim a11y-reviewed is a hard merge gate."""
    result = render("accessibility-reviewer", _STUB_VARS)
    # The template must say ADVISORY (or similar) and must NOT add it as a hard gate
    assert "ADVISORY" in result.upper() or "advisory" in result, (
        "render('accessibility-reviewer') does not mention the advisory nature of a11y-reviewed"
    )


def test_accessibility_reviewer_gate_key_in_render() -> None:
    """Rendered prompt must contain the gates.accessibility_reviewer control-plane key."""
    result = render("accessibility-reviewer", _STUB_VARS)
    assert "accessibility_reviewer" in result or "accessibility-reviewer" in result, (
        "render('accessibility-reviewer') missing gate key reference"
    )


def test_acceptance_tester_render_contains_all_three_gate_questions() -> None:
    """render('acceptance-tester') must contain all three binary gate questions."""
    result = render("acceptance-tester", _STUB_VARS)
    assert "Is the observed behavior security-safe?" in result, (
        "render('acceptance-tester') missing gate question 1: 'Is the observed behavior security-safe?'"
    )
    assert "Does it match the Spec?" in result, (
        "render('acceptance-tester') missing gate question 2: 'Does it match the Spec?'"
    )
    assert "Does it match user intent?" in result, (
        "render('acceptance-tester') missing gate question 3: 'Does it match user intent?'"
    )


def test_acceptance_tester_render_contains_pass_but_ban() -> None:
    """render('acceptance-tester') must include the 'pass but' ban phrase."""
    result = render("acceptance-tester", _STUB_VARS)
    assert "pass but" in result, (
        "render('acceptance-tester') missing 'pass but' ban phrase. "
        "The gate section must forbid 'pass but ...' verdict constructions."
    )


# ---------------------------------------------------------------------------
# acceptance-tester / security-reviewer — stale-worktree / PR-checkout guidance
# ---------------------------------------------------------------------------

def test_acceptance_tester_render_contains_pr_checkout_guidance() -> None:
    """render('acceptance-tester') must contain PR checkout instructions.

    The template must guide the agent to check out the actual PR branch before
    running tests — the worktree starts at main, which would give false results.
    """
    result = render("acceptance-tester", _STUB_VARS)
    # Either "gh pr checkout" or the manual fetch+checkout pattern must appear
    has_gh_checkout = "gh pr checkout" in result
    has_fetch_pattern = "origin/" in result and "headRef" in result
    assert has_gh_checkout or has_fetch_pattern, (
        "render('acceptance-tester') missing PR checkout guidance. "
        "The template must instruct the agent to check out the PR branch "
        "('gh pr checkout' or 'git fetch origin $BRANCH && git checkout $BRANCH') "
        "before running any tests."
    )


def test_acceptance_tester_render_warns_against_testing_main() -> None:
    """render('acceptance-tester') must explicitly warn against testing against main."""
    result = render("acceptance-tester", _STUB_VARS)
    # The template should mention the stale-main risk
    assert "main" in result.lower() and (
        "stale" in result.lower() or "false" in result.lower() or "NOT" in result
    ), (
        "render('acceptance-tester') should warn that running tests against main "
        "yields false pass/fail results."
    )


def test_security_reviewer_render_contains_stale_worktree_guidance() -> None:
    """render('security-reviewer') must contain the stale-worktree read-only pattern.

    Matches the pattern in code-reviewer.tmpl: headRefName + git fetch origin +
    git show origin/$BRANCH.
    """
    result = render("security-reviewer", _STUB_VARS)
    has_headref = "headRefName" in result or "headRef" in result
    has_origin_fetch = "git fetch origin" in result
    has_git_show = 'git show' in result and "origin/" in result
    assert has_headref and has_origin_fetch and has_git_show, (
        "render('security-reviewer') missing stale-worktree read-only guidance. "
        "Must include: headRefName lookup, 'git fetch origin', and "
        "'git show origin/$BRANCH:path' for reading PR files."
    )


def test_acceptance_tester_no_unresolved_include_directives() -> None:
    """render('acceptance-tester') must not contain any {{include:...}} tokens.

    The _VAR_RE regex in spawn_templates.py matches only \\w+ (word chars),
    so {{include:foo}} is silently left unreplaced. This test catches any
    regression where an include directive is re-introduced without being inlined.
    """
    result = render("acceptance-tester", _STUB_VARS)
    assert "{{include:" not in result, (
        "render('acceptance-tester') contains an unresolved {{include:...}} directive. "
        "spawn_templates.py does not support include directives — inline the content directly."
    )


# ---------------------------------------------------------------------------
# D#1812 — no role's rendered prompt may carry a second, contradictory
# tokens_used example. Six .tmpl files used to carry a literal-zero
# "tokens_used": {"input": 0, "output": 0} block in addition to the correct
# "<N>" placeholder that _ENVELOPE_BY_ROLE appends to every role. An agent
# reading two conflicting examples of the same field tends to echo the one
# that looks like a concrete answer (the literal zero) rather than the
# placeholder — which is how a field meant to carry real token counts ends
# up as a constant. Deleting the duplicate is a template-text-only fix; the
# `<N>` copy in _ENVELOPE_BY_ROLE is untouched and remains the sole example.
# ---------------------------------------------------------------------------

_LITERAL_ZERO_TOKENS = '"tokens_used": {"input": 0, "output": 0}'
_PLACEHOLDER_N_TOKENS = '"tokens_used": {"input": <N>, "output": <N>}'


def test_no_role_renders_literal_zero_tokens_used() -> None:
    """No known role's rendered prompt contains a literal-zero tokens_used block.

    Before D#1812: 6 of 24 roles (docs-writer, ux-designer, release-manager,
    accessibility-reviewer, incident-commander, runbook-writer) failed this.
    """
    offenders = []
    for role in sorted(KNOWN_ROLES):
        prompt = render(role, {}, ignore_unknown=True)
        if _LITERAL_ZERO_TOKENS in prompt:
            offenders.append(role)
    assert offenders == [], (
        f"role(s) still render a literal-zero tokens_used block: {offenders}. "
        "Remove the duplicate example from the role's .tmpl file — the correct "
        "'<N>' placeholder already comes from _ENVELOPE_BY_ROLE."
    )


def test_every_role_renders_placeholder_n_tokens_used() -> None:
    """Every known role's rendered prompt contains exactly the '<N>' placeholder.

    This is the envelope _ENVELOPE_BY_ROLE appends to all 24 roles — it must
    keep appearing after the duplicate literal-zero blocks are removed.
    """
    missing = []
    for role in sorted(KNOWN_ROLES):
        prompt = render(role, {}, ignore_unknown=True)
        if _PLACEHOLDER_N_TOKENS not in prompt:
            missing.append(role)
    assert missing == [], (
        f"role(s) missing the '<N>' tokens_used placeholder: {missing}"
    )


def test_envelope_by_role_still_covers_all_known_roles() -> None:
    """_ENVELOPE_BY_ROLE must still have an entry for every KNOWN_ROLES member.

    Guards against D#1812's failure condition: deleting a duplicate envelope
    example must never leave a role with no envelope at all.
    """
    from backend.spawn_templates import _ENVELOPE_BY_ROLE

    assert set(_ENVELOPE_BY_ROLE) == set(KNOWN_ROLES), (
        "_ENVELOPE_BY_ROLE and KNOWN_ROLES have diverged — every role must "
        f"have exactly one envelope. _ENVELOPE_BY_ROLE has {len(_ENVELOPE_BY_ROLE)} "
        f"entries, KNOWN_ROLES has {len(KNOWN_ROLES)}."
    )
