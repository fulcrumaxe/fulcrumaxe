"""tests/corpus_drift/test_archive_protocol.py

Unit tests for the archive_protocol_honored corpus-drift claim.

Key behaviours under test:

False-positive buckets (must NOT be counted):
- Heredoc body containing the phrase
- python3 -c inline script
- Shell comment line containing "git rm equivalent"
- echo '{"command":"git rm ..."}' JSON payload
- gh pr comment --body "do not git rm files"
- git rm --cached .pr-body.txt / pr-body-NNN.txt (scratch/gitignored)
- git -C <worktree> rm pr_body.txt (scratch file in worktree)
- git commit -m "$(cat <<'EOF' ... git rm ... EOF)" (git rm only in heredoc message)
- cd /tmp && git init && git rm f (git rm in scratch /tmp repo)

Real violation buckets (MUST still be counted):
- Bare: git rm tracked_file.py
- git -C /repo rm a.py
- xargs git rm
- Brace group: { git rm a.py; }
- git rm -f tracked_file.py (force flag)
- git rm --cached backend/api.py (tracked project file via --cached)

Other correctness:
- Text turn containing "git rm" -> not a violation (only Bash tool calls)
- Non-Bash tool call -> not a violation
- Empty transcript -> no violations
- No transcripts in window -> n/a
"""

from __future__ import annotations

from pathlib import Path

import pytest

import backend.corpus_drift.claims.archive_protocol as _m
from backend.corpus_drift.claims.archive_protocol import (
    CLAIM_ID,
    ROLE_SCOPE,
    _executed_command_only,
    _is_git_rm_violation,
    _is_scratch_only_rm,
    evaluate,
)
from tests.corpus_drift.conftest import (
    _make_assistant_text_turn,
    _make_transcript,
    write_transcript,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bash_tc(command: str) -> dict:
    return {"name": "Bash", "input": {"command": command}}


def _non_bash_tc(command: str) -> dict:
    return {"name": "Edit", "input": {"command": command}}


def _patch_glob(monkeypatch, paths: list[str]) -> None:
    monkeypatch.setattr(_m.glob, "glob", lambda pattern: paths)
    monkeypatch.setattr(_m.os.path, "getmtime", lambda p: 9_999_999_999.0)


# ── _strip_shell_comments unit tests ─────────────────────────────────────────

class TestStripShellComments:
    def test_no_comment_unchanged(self):
        assert _m._strip_shell_comments("git status") == "git status"

    def test_comment_only_line_stripped(self):
        result = _m._strip_shell_comments("# git rm file.txt")
        assert "git rm" not in result

    def test_trailing_comment_stripped(self):
        result = _m._strip_shell_comments("git status # git rm later")
        assert "git rm" not in result
        assert "git status" in result

    def test_multiline_comment_stripped(self):
        cmd = "git status\n# git rm file.txt\ngit log"
        result = _m._strip_shell_comments(cmd)
        assert "git rm" not in result
        assert "git status" in result
        assert "git log" in result

    def test_git_rm_before_hash_kept(self):
        # A real command before a comment must survive.
        result = _m._strip_shell_comments("git rm file.txt # removes it")
        assert "git rm" in result


# ── _executed_command_only unit tests ─────────────────────────────────────────

class TestExecutedCommandOnly:
    def test_heredoc_body_stripped(self):
        cmd = "cat > /tmp/x.py << 'PYEOF'\nfrom hooks import is_real_git_rm_invocation\nPYEOF"
        result = _executed_command_only(cmd)
        assert "is_real_git_rm_invocation" not in result

    def test_heredoc_body_stripped_bare_marker(self):
        cmd = "python3 << 'EOF'\nimport sys\nEOF"
        result = _executed_command_only(cmd)
        assert "import sys" not in result

    def test_non_heredoc_command_preserved(self):
        cmd = "git status && git log"
        assert _executed_command_only(cmd) == cmd

    def test_comment_stripped(self):
        cmd = "# git rm equivalent\nfind /somewhere"
        result = _executed_command_only(cmd)
        assert "git rm" not in result
        assert "find" in result


# ── _is_scratch_only_rm unit tests ────────────────────────────────────────────

class TestIsScratchOnlyRm:
    def test_pr_body_txt_is_scratch(self):
        assert _is_scratch_only_rm("git rm --cached .pr-body.txt") is True

    def test_pr_body_numbered_is_scratch(self):
        assert _is_scratch_only_rm("git rm --cached pr-body-1077.txt") is True

    def test_pr_body_underscore_is_scratch(self):
        assert _is_scratch_only_rm("git -C /worktree rm pr_body.txt 2>&1 || true") is True

    def test_tmp_path_is_scratch(self):
        assert _is_scratch_only_rm("git rm --cached /tmp/foo.txt") is True

    def test_project_file_not_scratch(self):
        assert _is_scratch_only_rm("git rm --cached backend/api.py") is False

    def test_project_file_not_scratch_even_with_git_c(self):
        assert _is_scratch_only_rm(
            "git checkout HEAD -- a.sh && git rm --cached scripts/lib/two-gate-check.sh"
        ) is False

    def test_mixed_scratch_and_real_not_exempt(self):
        # One scratch path + one project file -> not exempt
        assert _is_scratch_only_rm(
            "git rm --cached .pr-body.txt backend/api.py"
        ) is False

    # --- New AC tests: basename anchoring ---

    def test_docs_pr_body_txt_not_scratch(self):
        # AC1: docs/pr_body.txt is a real tracked file — basename anchoring must
        # prevent the pr_body.txt substring from matching inside a subdir.
        assert _is_scratch_only_rm("git rm --cached docs/pr_body.txt") is False

    def test_pr_body_as_directory_segment_not_scratch(self):
        # AC9: pr-body/ is a directory, keep.py is the actual file — not exempt.
        assert _is_scratch_only_rm("git rm pr-body/keep.py") is False

    def test_bare_pr_body_txt_still_scratch(self):
        # AC3: bare pr-body.txt with no parent directory — still exempt.
        assert _is_scratch_only_rm("git rm pr-body.txt") is True

    def test_tmp_pr_body_txt_still_scratch(self):
        # AC4: /tmp/x/pr-body.txt — under /tmp AND basename is a scratch name.
        assert _is_scratch_only_rm("git rm /tmp/x/pr-body.txt") is True


# ── _is_git_rm_violation unit tests ───────────────────────────────────────────

class TestIsGitRmViolation:
    # --- False-positive bucket 2a: heredoc bodies ---

    def test_heredoc_python_script_body_not_violation(self):
        """Heredoc body referencing is_real_git_rm_invocation is not a violation."""
        cmd = "cat > /tmp/x.py << 'PYEOF'\nfrom hooks.sandbox_rules import is_real_git_rm_invocation\nPYEOF"
        assert _is_git_rm_violation(cmd) is False

    def test_heredoc_python3_body_not_violation(self):
        """python3 << 'EOF' body referencing git rm is not a violation."""
        cmd = "python3 << 'EOF'\nimport sys\nfrom hooks.sandbox_rules import classify_bash\nEOF"
        assert _is_git_rm_violation(cmd) is False

    def test_heredoc_commit_message_not_violation(self):
        """git commit -m with heredoc substitution containing 'git rm' is not a violation."""
        cmd = (
            "git -C /worktree commit -m \"$(cat <<'EOF'\n"
            "close archive_protocol guardrail gap: warn team-lead on git rm\n"
            "EOF\n)\""
        )
        assert _is_git_rm_violation(cmd) is False

    def test_heredoc_pr_body_md_not_violation(self):
        """Writing a PR body markdown file with 'git rm' in the content is not a violation."""
        cmd = (
            "cat > /tmp/body.md << 'BODYEOF'\n"
            "## Instructions\n"
            "Never use git rm on tracked files\n"
            "BODYEOF"
        )
        assert _is_git_rm_violation(cmd) is False

    # --- False-positive bucket 2b: quoted-string arguments / comments ---

    def test_shell_comment_git_rm_equivalent_not_violation(self):
        """Shell comment mentioning 'git rm equivalent' is not a violation."""
        cmd = "# These are file deletions (git rm equivalent via diff)\nfind /somewhere"
        assert _is_git_rm_violation(cmd) is False

    def test_shell_comment_only_line_not_violation(self):
        """Pure comment line with 'git rm' is not a violation."""
        cmd = "# Gate 2a: Demonstrate worktree git rm is still blocked\necho 'done'"
        assert _is_git_rm_violation(cmd) is False

    def test_json_echo_payload_not_violation(self):
        """echo '{"command":"git rm ..."}' JSON payload is not a violation."""
        cmd = (
            "echo '{\"tool_name\": \"Bash\", \"tool_input\": {\"command\": \"git rm tests/foo.py\"}}'"
            " | python3 /path/to/sandbox.py"
        )
        assert _is_git_rm_violation(cmd) is False

    def test_gh_pr_body_prose_not_violation(self):
        """gh pr comment --body 'do not git rm files' prose is not a violation."""
        cmd = "gh pr comment 123 --body 'do not git rm files' --repo owner/repo"
        assert _is_git_rm_violation(cmd) is False

    def test_git_commit_message_not_violation(self):
        """git commit -m 'fix quoted-string false positive in git rm detector' is not a violation."""
        cmd = "git commit -m 'fix quoted-string false positive in git rm detector'"
        assert _is_git_rm_violation(cmd) is False

    # --- False-positive bucket 2c: --cached scratch paths ---

    def test_cached_pr_body_txt_not_violation(self):
        """git rm --cached .pr-body.txt on a gitignored scratch file is not a violation."""
        cmd = "git rm --cached .pr-body.txt"
        assert _is_git_rm_violation(cmd) is False

    def test_cached_pr_body_numbered_not_violation(self):
        """git rm --cached pr-body-1077.txt is not a violation."""
        cmd = "git -C /worktree rm --cached pr-body-1077.txt 2>/dev/null; git status"
        assert _is_git_rm_violation(cmd) is False

    def test_worktree_pr_body_not_violation(self):
        """git -C <worktree> rm pr_body.txt (scratch file, no --cached) is not a violation."""
        cmd = "git -C /worktrees/agent-ad12 rm pr_body.txt 2>&1 || true"
        assert _is_git_rm_violation(cmd) is False

    def test_cached_pr_body_with_gitc_not_violation(self):
        """git -C <wt> rm --cached .pr-body.txt followed by rm is not a violation."""
        cmd = "git -C /worktree rm --cached .pr-body.txt 2>/dev/null; rm .pr-body.txt"
        assert _is_git_rm_violation(cmd) is False

    def test_tmp_repo_cd_not_violation(self):
        """git rm inside a /tmp throwaway repo (cd /tmp && git init ...) is not a violation."""
        cmd = "cd /tmp && mkdir -p test-repo && cd test-repo && git init -q && git rm f 2>&1; rm -rf /tmp/test-repo"
        assert _is_git_rm_violation(cmd) is False

    # --- Real violations: must still fire ---

    def test_bare_git_rm_tracked_file_fires(self):
        """git rm src/foo.py is a violation."""
        cmd = "git rm src/foo.py"
        assert _is_git_rm_violation(cmd) is True

    def test_git_c_repo_rm_fires(self):
        """git -C /repo rm a.py is a violation."""
        cmd = "git -C /repo rm a.py"
        assert _is_git_rm_violation(cmd) is True

    def test_xargs_git_rm_fires(self):
        """xargs git rm is a violation."""
        cmd = "xargs git rm"
        assert _is_git_rm_violation(cmd) is True

    def test_brace_group_git_rm_fires(self):
        """{ cd /repo; git rm a.py; } is a violation."""
        cmd = "{ cd /repo; git rm a.py; }"
        assert _is_git_rm_violation(cmd) is True

    def test_git_rm_force_tracked_file_fires(self):
        """git rm -f tests/corpus_drift/test_archive_protocol.py is a violation."""
        cmd = "git rm -f tests/corpus_drift/test_archive_protocol.py"
        assert _is_git_rm_violation(cmd) is True

    def test_git_rm_cached_project_file_fires(self):
        """git rm --cached backend/api.py (tracked project file) is a violation."""
        cmd = "git rm --cached backend/api.py backend/fleet/concurrency.py"
        assert _is_git_rm_violation(cmd) is True

    def test_git_rm_cached_project_file_with_checkout_fires(self):
        """git rm --cached scripts/lib/two-gate-check.sh (tracked file) is a violation."""
        cmd = (
            "git checkout HEAD -- tests/test_merge.sh "
            "&& git rm --cached scripts/lib/two-gate-check.sh 2>/dev/null"
        )
        assert _is_git_rm_violation(cmd) is True

    def test_docs_pr_body_txt_is_violation(self):
        # AC2: removing docs/pr_body.txt must be counted as a violation because
        # pr_body.txt here is a real tracked file path, not a scratch basename.
        assert _is_git_rm_violation("git rm --cached docs/pr_body.txt") is True


# ── Violation detection (end-to-end via evaluate()) ──────────────────────────

class TestViolationDetection:
    def test_bash_git_rm_counts_as_violation(self, tmp_transcript_dir, monkeypatch):
        """Bash command 'git rm file.txt' is flagged as a violation (score == 1)."""
        t = write_transcript(
            tmp_transcript_dir, "run-git-rm",
            [_make_transcript([_bash_tc("git rm file.txt")])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 1
        assert result.score > 0

    def test_bash_git_rm_in_comment_not_a_violation(self, tmp_transcript_dir, monkeypatch):
        """'git status # git rm later' in a Bash call is NOT a violation."""
        t = write_transcript(
            tmp_transcript_dir, "run-comment-git-rm",
            [_make_transcript([_bash_tc("git status # git rm later")])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    def test_text_turn_git_rm_not_a_violation(self, tmp_transcript_dir, monkeypatch):
        """Text turn containing 'I might git rm this' is NOT a violation."""
        t = write_transcript(
            tmp_transcript_dir, "run-text-git-rm",
            [_make_assistant_text_turn("I might git rm this file later")]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    def test_empty_transcript_no_violation(self, tmp_transcript_dir, monkeypatch):
        """Empty transcript produces zero violations."""
        t = write_transcript(
            tmp_transcript_dir, "run-empty",
            []
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    def test_no_transcripts_returns_na(self, monkeypatch):
        """No transcripts in window -> n/a."""
        _patch_glob(monkeypatch, [])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.status == "n/a"
        assert result.claim_id == CLAIM_ID
        assert result.role_scope == ROLE_SCOPE

    def test_non_bash_tool_git_rm_not_counted(self, tmp_transcript_dir, monkeypatch):
        """git rm in a non-Bash tool call input is NOT counted."""
        t = write_transcript(
            tmp_transcript_dir, "run-non-bash",
            [_make_transcript([_non_bash_tc("git rm file.txt")])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    def test_multiple_git_rm_calls_counted_individually(self, tmp_transcript_dir, monkeypatch):
        """Multiple Bash git rm calls in one transcript each add to the count."""
        t = write_transcript(
            tmp_transcript_dir, "run-multi-git-rm",
            [_make_transcript([
                _bash_tc("git rm file1.txt"),
                _bash_tc("git rm file2.txt"),
            ])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 2

    # --- False-positive classes not counted ---

    def test_heredoc_body_not_counted(self, tmp_transcript_dir, monkeypatch):
        """A Bash call that writes a script to disk via heredoc is not counted."""
        cmd = "cat > /tmp/x.py << 'PYEOF'\nfrom hooks.sandbox_rules import is_real_git_rm_invocation\nPYEOF"
        t = write_transcript(
            tmp_transcript_dir, "run-heredoc",
            [_make_transcript([_bash_tc(cmd)])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    def test_json_echo_not_counted(self, tmp_transcript_dir, monkeypatch):
        """A Bash call that echo-pipes a JSON payload is not counted."""
        cmd = "echo '{\"command\":\"git rm foo.py\"}' | python3 sandbox.py"
        t = write_transcript(
            tmp_transcript_dir, "run-json-echo",
            [_make_transcript([_bash_tc(cmd)])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    def test_gh_pr_body_not_counted(self, tmp_transcript_dir, monkeypatch):
        """gh pr comment --body prose containing 'git rm' is not counted."""
        cmd = "gh pr comment 42 --body 'do not git rm files' --repo owner/repo"
        t = write_transcript(
            tmp_transcript_dir, "run-gh-body",
            [_make_transcript([_bash_tc(cmd)])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    def test_cached_pr_body_not_counted(self, tmp_transcript_dir, monkeypatch):
        """git rm --cached .pr-body.txt on a scratch file is not counted."""
        cmd = "git rm --cached .pr-body.txt 2>/dev/null"
        t = write_transcript(
            tmp_transcript_dir, "run-cached-scratch",
            [_make_transcript([_bash_tc(cmd)])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score == 0

    # --- Real violation still flagged ---

    def test_real_git_rm_still_counted(self, tmp_transcript_dir, monkeypatch):
        """A bare git rm on a tracked project file is still counted as a violation."""
        cmd = "git rm tracked_file.py"
        t = write_transcript(
            tmp_transcript_dir, "run-real-rm",
            [_make_transcript([_bash_tc(cmd)])]
        )
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.score > 0

    def test_claim_metadata(self, tmp_transcript_dir, monkeypatch):
        """Claim metadata fields are correct when transcripts are present."""
        t = write_transcript(tmp_transcript_dir, "run-meta", [])
        _patch_glob(monkeypatch, [str(t)])
        result = evaluate(runs=[], transcripts_dir=None, window_days=30)
        assert result.claim_id == CLAIM_ID
        assert result.role_scope == ROLE_SCOPE
        assert result.score_type == "count"
        assert result.notes is not None
        assert "0" in result.notes  # "pass = 0 violations"
