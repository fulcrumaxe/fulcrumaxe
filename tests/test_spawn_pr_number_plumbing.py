"""Regression suite for D#1788: `--pr` must reach the rendered prompt for
every PR-scoped role, and a template placeholder with no supplier must fail
the spawn loudly instead of rendering an empty string.

Two independent bugs, verified live on the checkout this Spec was written
against:
  1. `backend/spawn_templates/code-reviewer.tmpl:11` used {{discussion_number}}
     in the PR slot — "Review PR #1761 for Discussion #1761" when the PR was
     actually #1786.
  2. `scripts/spawn-agent.sh` parsed `--pr` and never added a `pr` key to the
     payload handed to `backend.prompt_builder` — every {{pr_number}} in all
     8 PR-scoped role templates (46 references) rendered as an empty string.

Why a dedicated file instead of extending the existing render() tests: every
existing render/render_body call elsewhere in this suite passes the
unknown-token-tolerant flag (see D#1788 "Why the existing tests are green" —
that flag is exactly what let both bugs render clean while being live). No
test in *this* file may pass it — every call here must exercise the strict
path or it proves nothing about the regression.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.spawn_templates import _TEMPLATES_DIR, KNOWN_ROLES, render_body  # noqa: E402
from backend.spawn_var_contract import (  # noqa: E402
    RENDER_EMPTY_BY_DESIGN,
    SpawnVarContractError,
    referenced_vars,
)

# The 8 role templates D#1788 identified as referencing {{pr_number}}.
PR_SCOPED_ROLES = [
    "code-reviewer",
    "security-reviewer",
    "acceptance-tester",
    "browser-tester",
    "accessibility-reviewer",
    "docs-writer",
    "release-manager",
    "runbook-writer",
]

assert set(PR_SCOPED_ROLES) <= KNOWN_ROLES, "a PR-scoped role is missing its .tmpl file"

# Real, non-empty values for every variable D#1788 puts IN scope (the "LIVE"
# set — see the module docstring's Cause 3 census). A superset handed to
# every role; render_body only substitutes the tokens a given role's
# template actually contains.
_LIVE_VARS = {
    "task_brief": "Do the test thing",
    "discussion_number": "1761",
    "discussion_url": "https://github.com/autonomous-agent-7/fulcrumaxe/discussions/1761",
    "pr_number": "1786",
    "pr_url": "https://github.com/autonomous-agent-7/fulcrumaxe/pull/1786",
    "pr_branch": "feature/stub-branch",
}

# Names that resolve non-empty without being in _LIVE_VARS: render_body's own
# module-level defaults (REPO/REPO_OWNER/REPO_NAME from project.json,
# affected_pages_json defaulting to "[]").
_LIVE_DEFAULTS = {"REPO", "REPO_OWNER", "REPO_NAME", "affected_pages_json"}


def _render(role: str, vars_: dict | None = None) -> str:
    return render_body(role, vars_ if vars_ is not None else _LIVE_VARS, ignore_unknown=False)


class TestNoLiteralTokenSurvives:
    @pytest.mark.parametrize("role", PR_SCOPED_ROLES)
    def test_no_double_curly_survives(self, role):
        result = _render(role)
        assert "{{" not in result, f"role '{role}' rendered a literal {{...}} token"


class TestCodeReviewerPrSlot:
    """D#1788 Cause 1: the PR slot on line 11 used {{discussion_number}}."""

    def test_review_pr_line_names_the_pr_not_the_discussion(self):
        result = _render("code-reviewer")
        assert "#1786" in result
        assert "Review PR #1761" not in result

    def test_review_pr_line_still_names_the_discussion(self):
        result = _render("code-reviewer")
        assert "Discussion #1761" in result


class TestOmittingPrFailsLoudly:
    """D#1788 Cause 3/4: a role whose template references {{pr_number}} and
    gets no `pr` must fail the render, not produce a blank slot."""

    @pytest.mark.parametrize("role", PR_SCOPED_ROLES)
    def test_missing_pr_number_raises(self, role):
        vars_without_pr = {
            k: v for k, v in _LIVE_VARS.items() if k not in ("pr_number", "pr_url", "pr_branch")
        }
        with pytest.raises(SpawnVarContractError) as exc_info:
            _render(role, vars_without_pr)
        assert "pr_number" in str(exc_info.value)


class TestReferencedVsSupplied:
    def test_partition_covers_every_template(self):
        """Every {{var}} referenced by every role template must be either
        LIVE (this file's _LIVE_VARS / _LIVE_DEFAULTS) or explicitly excused
        by RENDER_EMPTY_BY_DESIGN. A new template placeholder with no
        supplier and no excuse fails this test immediately — that's the
        point (D#1788 criterion 6)."""
        all_referenced: set[str] = set()
        for tmpl_path in sorted(_TEMPLATES_DIR.glob("*.tmpl")):
            all_referenced |= referenced_vars(tmpl_path.read_text(encoding="utf-8"))

        supplied = set(_LIVE_VARS) | _LIVE_DEFAULTS
        unsupplied_unexcused = all_referenced - supplied - set(RENDER_EMPTY_BY_DESIGN)
        assert not unsupplied_unexcused, (
            f"Template variable(s) with no supplier and no RENDER_EMPTY_BY_DESIGN "
            f"excuse: {sorted(unsupplied_unexcused)}"
        )

    def test_render_empty_by_design_reasons_are_all_non_empty(self):
        assert len(RENDER_EMPTY_BY_DESIGN) > 0
        assert all(reason.strip() for reason in RENDER_EMPTY_BY_DESIGN.values())


class TestDiscussionOptionalAtRenderTime:
    """D#1788 fix-round (post-review). --discussion is optional at the
    scripts/spawn-agent.sh CLI level (only --role/--task-prompt are
    required), and existing callers rely on that — e.g.
    scripts/replay-debater.sh spawns debater with no --discussion at all.
    The first version of this contract broke that silently: reviewer found
    21 of 24 roles raised with discussion=None, vs 0 of 24 on main.
    discussion_number/discussion_url are excused in RENDER_EMPTY_BY_DESIGN
    specifically to keep the pr_number fix from becoming a second,
    uncoordinated global constraint. This is the regression test for that."""

    @pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
    def test_role_renders_without_discussion_or_pr(self, role):
        # Only task_brief -- every role needs that regardless of context.
        # Deliberately no discussion_number/discussion_url/pr_* supplied.
        vars_ = {"task_brief": "smoke test with no discussion or PR context"}
        try:
            _render(role, vars_)
        except SpawnVarContractError as exc:
            # A PR-scoped role legitimately still raises here (it needs
            # --pr) -- but discussion vars must never be the reason, for
            # ANY role, PR-scoped or not.
            assert role in PR_SCOPED_ROLES, (
                f"role '{role}' raised with no discussion context, but is not "
                f"PR-scoped -- discussion must not be a hard requirement: {exc}"
            )
            assert "discussion_number" not in str(exc), exc
            assert "discussion_url" not in str(exc), exc
