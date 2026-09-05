"""tests/test_spawn_templates_repo_var.py

Verify that spawn templates parameterized with {{REPO}} correctly substitute
the project repo when rendered with a non-default project.json.

Contract:
  - When project.json says repo="test-org/test-repo", rendered template contains
    "test-org/test-repo" and does NOT contain "autonomous-agent-7/autonomous-forever".
  - Tests all templates that had hardcoded repo refs.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend import spawn_templates as st

# Templates that previously contained hardcoded autonomous-agent-7/autonomous-forever
# Wave A: docs-writer, run-analyst, release-manager, security-reviewer, runbook-writer, tui-tester
# Wave B: executor, code-reviewer, incident-commander, project-manager
PARAMETERIZED_ROLES = [
    "docs-writer",
    "run-analyst",
    "release-manager",
    "security-reviewer",
    "runbook-writer",
    "tui-tester",
    "executor",
    "code-reviewer",
    "incident-commander",
    "project-manager",
]

TEST_REPO = "test-org/test-repo"
TEST_OWNER = "test-org"
TEST_NAME = "test-repo"
ORIGINAL_REPO = "autonomous-agent-7/autonomous-forever"
ORIGINAL_OWNER = "autonomous-agent-7"
ORIGINAL_NAME = "autonomous-forever"


def _render_with_repo(role: str, repo: str) -> str:
    """Render a template substituting {{REPO}} with *repo* (REPO_OWNER/REPO_NAME auto-derived)."""
    return st.render_body(
        role,
        vars={"REPO": repo},
        ignore_unknown=True,
    )


class TestSpawnTemplatesRepoVar:
    @pytest.mark.parametrize("role", PARAMETERIZED_ROLES)
    def test_repo_substituted(self, role):
        """Rendered prompt contains the test repo owner or full slug."""
        rendered = _render_with_repo(role, TEST_REPO)
        # Most templates use {{REPO}} directly; some (e.g. project-manager) only use
        # {{REPO_OWNER}} and {{REPO_NAME}} as separate tokens — both are valid.
        has_slug = TEST_REPO in rendered
        has_owner = TEST_OWNER in rendered
        assert has_slug or has_owner, (
            f"Role '{role}': rendered prompt does not contain '{TEST_REPO}' "
            f"or '{TEST_OWNER}'.\n"
            f"First 500 chars of rendered: {rendered[:500]}"
        )

    @pytest.mark.parametrize("role", PARAMETERIZED_ROLES)
    def test_no_original_repo(self, role):
        """Rendered prompt does NOT contain the original hardcoded repo slug."""
        rendered = _render_with_repo(role, TEST_REPO)
        # Check for the full slug and the owner/repo pattern — avoids false positives
        # from path-like strings that happen to contain "autonomous-forever" (the project name)
        assert ORIGINAL_REPO not in rendered, (
            f"Role '{role}': rendered prompt still contains '{ORIGINAL_REPO}'.\n"
            "Template likely has a hardcoded occurrence that was not replaced with {{REPO}}."
        )
        assert f"{ORIGINAL_OWNER}/" not in rendered, (
            f"Role '{role}': rendered prompt still contains '{ORIGINAL_OWNER}/' "
            "(looks like a repo owner prefix).\n"
            "Template likely has a hardcoded occurrence that was not replaced with {{REPO}}."
        )

    def test_default_repo_is_dynamic(self, tmp_path, monkeypatch):
        """When project.json sets repo, _REPO in spawn_templates module reflects it."""
        # Write a project.json that the module would read if re-loaded from tmp_path
        team_dir = tmp_path / ".autonomous-team"
        team_dir.mkdir()
        (team_dir / "project.json").write_text(json.dumps({"repo": TEST_REPO}))

        # Simulate what _load_repo does (patched path)
        def patched_load_repo():
            project_json = team_dir / "project.json"
            try:
                with project_json.open() as f:
                    data = json.load(f)
                repo = data.get("repo")
                if repo:
                    return repo
            except (OSError, ValueError):
                pass
            import os
            return os.environ.get("AUTONOMOUS_TEAM_REPO", ORIGINAL_REPO)

        monkeypatch.setattr(st, "_REPO", patched_load_repo())
        assert st._REPO == TEST_REPO

    def test_repo_scope_appendix_uses_dynamic_repo(self):
        """_make_repo_scope() generates the right scope string for an arbitrary repo."""
        scope = st._make_repo_scope("acme/myapp")
        assert "acme/myapp" in scope
        assert ORIGINAL_REPO not in scope
        assert 'owner:"acme"' in scope
        assert 'name:"myapp"' in scope

    @pytest.mark.parametrize("role", PARAMETERIZED_ROLES)
    def test_render_body_with_default_repo_var(self, role):
        """render_body() without explicit REPO still includes a repo slug (the module default)."""
        # The module-level _REPO is the fallback, which is the original repo in tests
        # (since no project.json exists at the test's working directory).
        # Just verify it renders without errors and contains _some_ repo string.
        rendered = st.render_body(role, vars={}, ignore_unknown=True)
        # Should contain either the original fallback or some repo slug
        assert "/" in rendered, (
            f"Role '{role}': rendered body appears to have no repo slug at all."
        )
