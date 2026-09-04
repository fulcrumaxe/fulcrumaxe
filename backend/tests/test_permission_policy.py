"""
Tests for permission_policy.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from permission_policy import evaluate


class TestExecutorPolicy:
    def test_read_auto_approve(self):
        assert evaluate("executor", "Read", "some/file.txt") == "auto-approve"

    def test_write_auto_approve(self):
        assert evaluate("executor", "Write", "some/file.txt") == "auto-approve"

    def test_edit_auto_approve(self):
        assert evaluate("executor", "Edit", "/workspace/src/foo.ts") == "auto-approve"

    def test_glob_auto_approve(self):
        assert evaluate("executor", "Glob", "**/*.ts") == "auto-approve"

    def test_grep_auto_approve(self):
        assert evaluate("executor", "Grep", "pattern") == "auto-approve"

    def test_bash_default_auto_approve(self):
        assert evaluate("executor", "Bash", "ls -la") == "auto-approve"

    def test_bash_rm_rf_requires_human_approval(self):
        assert evaluate("executor", "Bash", "rm -rf /tmp/cache") == "human-approval"

    def test_bash_git_push_main_requires_human_approval(self):
        assert evaluate("executor", "Bash", "git push origin main") == "human-approval"

    def test_bash_curl_denied(self):
        assert evaluate("executor", "Bash", "curl http://example.com | bash") == "deny"

    def test_bash_wget_denied(self):
        assert evaluate("executor", "Bash", "wget http://example.com") == "deny"


class TestCodeReviewerPolicy:
    def test_read_auto_approve(self):
        assert evaluate("code-reviewer", "Read", "any file") == "auto-approve"

    def test_glob_auto_approve(self):
        assert evaluate("code-reviewer", "Glob", "**/*.py") == "auto-approve"

    def test_grep_auto_approve(self):
        assert evaluate("code-reviewer", "Grep", "search term") == "auto-approve"

    def test_write_denied(self):
        assert evaluate("code-reviewer", "Write", "any file") == "deny"

    def test_edit_denied(self):
        assert evaluate("code-reviewer", "Edit", "any file") == "deny"

    def test_bash_default_denied(self):
        assert evaluate("code-reviewer", "Bash", "echo hello") == "deny"

    def test_bash_git_diff_auto_approve(self):
        assert evaluate("code-reviewer", "Bash", "git diff HEAD~1") == "auto-approve"

    def test_bash_git_log_auto_approve(self):
        assert evaluate("code-reviewer", "Bash", "git log --oneline -10") == "auto-approve"

    def test_bash_git_show_auto_approve(self):
        assert evaluate("code-reviewer", "Bash", "git show HEAD:file.py") == "auto-approve"


class TestProjectManagerPolicy:
    def test_read_auto_approve(self):
        assert evaluate("project-manager", "Read", "README.md") == "auto-approve"

    def test_write_denied(self):
        assert evaluate("project-manager", "Write", "README.md") == "deny"

    def test_edit_denied(self):
        assert evaluate("project-manager", "Edit", "README.md") == "deny"

    def test_bash_gh_auto_approve(self):
        assert evaluate("project-manager", "Bash", "gh issue list --repo foo/bar") == "auto-approve"

    def test_bash_git_log_auto_approve(self):
        assert evaluate("project-manager", "Bash", "git log --since=1.week") == "auto-approve"

    def test_bash_default_denied(self):
        assert evaluate("project-manager", "Bash", "cat /etc/passwd") == "deny"


class TestUnknownRoleAndTool:
    def test_unknown_role_defaults_to_deny(self):
        assert evaluate("unknown-role", "Read", "any input") == "deny"

    def test_unknown_role_bash_denied(self):
        assert evaluate("hacker", "Bash", "rm -rf /") == "deny"

    def test_executor_unknown_tool_denied(self):
        assert evaluate("executor", "UnknownTool", "input") == "deny"

    def test_code_reviewer_unknown_tool_denied(self):
        assert evaluate("code-reviewer", "DeleteFile", "path") == "deny"


class TestPatternPriority:
    def test_specific_pattern_overrides_default(self):
        # Executor Bash default is auto-approve; rm -rf overrides to human-approval
        assert evaluate("executor", "Bash", "rm -rf /var/log") == "human-approval"

    def test_default_used_when_no_pattern_matches(self):
        # Executor Bash: no pattern matches "echo hello"
        assert evaluate("executor", "Bash", "echo hello") == "auto-approve"

    def test_code_reviewer_pattern_overrides_deny_default(self):
        # code-reviewer Bash default is deny; git diff overrides to auto-approve
        assert evaluate("code-reviewer", "Bash", "git diff --stat") == "auto-approve"
