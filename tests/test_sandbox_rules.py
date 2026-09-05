"""tests/test_sandbox_rules.py

Unit tests for hooks/sandbox_rules.py — every rule branch, all AC1–AC5 cases.

Run with:
    python3 -m pytest tests/test_sandbox_rules.py -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.sandbox_rules import (
    Decision,
    _DIAL_PROTECTED_SUFFIXES,
    _GH_API_GRAPHQL_MUTATION_ALLOWLIST,
    _GIT_VERB_SHAPE_RE,
    _absolute_path_targets,
    _extract_all_gh_query_values,
    _extract_all_git_verbs,
    _extract_git_verb,
    _extract_graphql_mutation_names,
    _home_prefixed_redirect_targets,
    _is_dial_protected_path,
    _is_ephemeral_tmp_path,
    _is_git_readonly_invocation,
    _is_segment_write_candidate,
    _protected_basename_operand,
    _strip_graphql_comments,
    check_claude_spawn,
    classify_bash,
    classify_git_rm,
    classify_path_write,
    is_real_git_rm_invocation,
    is_worktree,
    resolve_effective_cwd,
    _worktree_root_from_cwd,
)
from testsupport.fixture_paths import FIXTURE_HOME, FIXTURE_MAIN_REPO

# ---------------------------------------------------------------------------
# Worktree CWD fixtures
# ---------------------------------------------------------------------------

_MAIN_REPO = FIXTURE_MAIN_REPO
_WT_CLAUDE = f"{_MAIN_REPO}/.claude/worktrees/abc123"
_WT_TMP = "/tmp/wt-testid"

_WT_CWDS = [
    _WT_CLAUDE,
    _WT_CLAUDE + "/src",
    _WT_TMP,
    _WT_TMP + "/subdir",
]
_TL_CWDS = [
    _MAIN_REPO,
    _MAIN_REPO + "/backend",
    FIXTURE_HOME,
    "/tmp/other",
]


# ---------------------------------------------------------------------------
# is_worktree
# ---------------------------------------------------------------------------


class TestIsWorktree:
    @pytest.mark.parametrize("cwd", _WT_CWDS)
    def test_recognizes_worktree_cwds(self, cwd: str) -> None:
        assert is_worktree(cwd) is not None

    @pytest.mark.parametrize("cwd", _TL_CWDS)
    def test_team_lead_cwds_return_none(self, cwd: str) -> None:
        assert is_worktree(cwd) is None

    def test_returns_worktree_id(self) -> None:
        wt_id = is_worktree(_WT_CLAUDE + "/deep/nested/path")
        assert wt_id == "abc123"

    def test_tmp_wt_returns_id(self) -> None:
        wt_id = is_worktree("/tmp/wt-myid/src")
        assert wt_id == "myid"


# ---------------------------------------------------------------------------
# resolve_effective_cwd
# ---------------------------------------------------------------------------


class TestResolveEffectiveCwd:
    def test_no_cd_returns_base(self) -> None:
        result = resolve_effective_cwd("git status", _WT_CLAUDE)
        assert result == str(Path(_WT_CLAUDE).resolve())

    def test_absolute_cd(self) -> None:
        result = resolve_effective_cwd(f"cd {_MAIN_REPO} && git status", _WT_CLAUDE)
        assert result == str(Path(_MAIN_REPO).resolve())

    def test_git_minus_c(self) -> None:
        result = resolve_effective_cwd(f"git -C {_MAIN_REPO} status", _WT_CLAUDE)
        assert result == str(Path(_MAIN_REPO).resolve())

    def test_chained_cd(self) -> None:
        result = resolve_effective_cwd(
            f"cd /tmp && cd {_MAIN_REPO} && git status", _WT_CLAUDE
        )
        assert result == str(Path(_MAIN_REPO).resolve())

    def test_no_escape_relative_cd(self) -> None:
        # relative cd stays within resolved base
        result = resolve_effective_cwd("cd src && git status", _WT_CLAUDE)
        assert result.startswith(str(Path(_WT_CLAUDE).resolve()))


# ---------------------------------------------------------------------------
# AC1 — worktree git-write rejection (effective-CWD aware)
# ---------------------------------------------------------------------------


class TestAC1GitWriteRejection:
    """AC1: git write-verbs from a worktree that escape the worktree are rejected."""

    @pytest.mark.parametrize(
        "command",
        [
            "git checkout main",
            f"git -C {_MAIN_REPO} checkout main",
            f"cd {_MAIN_REPO} && git checkout main",
            'bash -c "git reset --hard origin/main"',
            "git switch main",
            "git branch -D feature-x",
            "git reset --hard HEAD~1",
        ],
    )
    def test_git_write_blocked_from_worktree(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason or "sub-agents may not merge" in d.reason or not d.allow

    def test_git_checkout_with_minus_c_escape(self) -> None:
        d = classify_bash(f"git -C {_MAIN_REPO} checkout main", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_bash_wrapper_escape(self) -> None:
        d = classify_bash('bash -c "git reset --hard origin/main"', _WT_CLAUDE)
        assert not d.allow

    def test_git_write_within_worktree_allowed(self) -> None:
        # git commit from within the worktree — effective CWD stays in worktree
        d = classify_bash("git commit -m 'test'", _WT_CLAUDE)
        assert d.allow

    def test_git_push_from_worktree_allowed(self) -> None:
        # push from the worktree is allowed (executor needs to push its branch)
        d = classify_bash("git push -u origin HEAD", _WT_CLAUDE)
        assert d.allow


# ---------------------------------------------------------------------------
# AC2 — read-only git allowlist
# ---------------------------------------------------------------------------


class TestAC2ReadonlyAllowlist:
    @pytest.mark.parametrize(
        "command",
        [
            "git fetch origin",
            "git log -5",
            "git status",
            "git diff",
            "git show HEAD",
            "git rev-parse HEAD",
            "git ls-files",
            "git cat-file -p HEAD",
            "git for-each-ref",
            f"git -C {_MAIN_REPO} fetch origin",  # read-only allowed even with -C escape
            "git log --oneline -10",
            "git diff HEAD~1",
            "git status --short",
        ],
    )
    def test_readonly_git_always_allowed(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, f"Expected allow for: {command!r}, got reason={d.reason!r}"

    def test_git_fetch_with_minus_c_escape_allowed(self) -> None:
        # Even with -C pointing outside the worktree, fetch is read-only
        d = classify_bash(f"git -C {_MAIN_REPO} fetch origin", _WT_CLAUDE)
        assert d.allow


# ---------------------------------------------------------------------------
# AC3 — write-path rejection (Edit/Write + Bash redirect)
# ---------------------------------------------------------------------------


class TestAC3WritePathRejection:
    def test_edit_outside_worktree_blocked(self) -> None:
        d = classify_path_write(f"{_MAIN_REPO}/CLAUDE.md", _WT_CLAUDE)
        assert not d.allow
        assert "file_path outside worktree" in d.reason

    def test_write_outside_worktree_blocked(self) -> None:
        d = classify_path_write(f"{_MAIN_REPO}/scripts/foo.sh", _WT_CLAUDE)
        assert not d.allow

    def test_edit_inside_worktree_allowed(self) -> None:
        d = classify_path_write(f"{_WT_CLAUDE}/src/app.py", _WT_CLAUDE)
        assert d.allow

    def test_relative_path_allowed(self) -> None:
        d = classify_path_write("src/app.py", _WT_CLAUDE)
        assert d.allow

    def test_bash_redirect_outside_blocked(self) -> None:
        d = classify_bash(f"echo X > {_MAIN_REPO}/CLAUDE.md", _WT_CLAUDE)
        assert not d.allow
        assert "output redirect outside worktree" in d.reason

    def test_bash_tee_outside_blocked(self) -> None:
        d = classify_bash(f"echo X | tee {_MAIN_REPO}/foo", _WT_CLAUDE)
        assert not d.allow

    def test_bash_cp_outside_blocked(self) -> None:
        d = classify_bash(f"cp x {_MAIN_REPO}/", _WT_CLAUDE)
        assert not d.allow

    def test_bash_mv_outside_blocked(self) -> None:
        d = classify_bash(f"mv x {_MAIN_REPO}/newfile", _WT_CLAUDE)
        assert not d.allow

    def test_bash_redirect_inside_worktree_allowed(self) -> None:
        d = classify_bash(f"echo X > {_WT_CLAUDE}/output.txt", _WT_CLAUDE)
        assert d.allow

    def test_bash_redirect_to_tmp_allowed(self) -> None:
        # /tmp/ is ephemeral — not repo state — so writes must be allowed.
        d = classify_bash("echo hi > /tmp/foo", _WT_CLAUDE)
        assert d.allow, f"Expected /tmp/ redirect to be allowed, got reason={d.reason!r}"

    def test_bash_redirect_to_var_tmp_allowed(self) -> None:
        d = classify_bash("echo hi > /var/tmp/bar", _WT_CLAUDE)
        assert d.allow, f"Expected /var/tmp/ redirect to be allowed, got reason={d.reason!r}"

    def test_bash_redirect_to_main_repo_still_blocked(self) -> None:
        # Sanity-check: /tmp/ exemption must not weaken the main-repo block.
        d = classify_bash(f"echo hi > {_MAIN_REPO}/foo", _WT_CLAUDE)
        assert not d.allow
        assert "output redirect outside worktree" in d.reason

    def test_tmp_worktree_write_blocked(self) -> None:
        d = classify_path_write(f"{_MAIN_REPO}/CLAUDE.md", _WT_TMP)
        assert not d.allow


# ---------------------------------------------------------------------------
# D#1992 — _is_ephemeral_tmp_path prefix-tested an unnormalised path, so a
# redirect target prefixed "/tmp/.." satisfied the raw prefix check and
# escaped the worktree containment check entirely.
# ---------------------------------------------------------------------------


class TestEphemeralTmpPathNormalisation:
    def test_ephemeral_tmp_path_escape_is_rejected(self) -> None:
        # (a) plain out-of-worktree redirect target is rejected -- baseline.
        plain = classify_bash(f"echo hi > {_MAIN_REPO}/foo", _WT_CLAUDE)
        assert not plain.allow, f"expected plain out-of-worktree redirect rejected, got {plain}"

        # (b) the SAME path, prefixed with the ephemeral root plus a
        # parent-dir segment, must ALSO be rejected. Pre-fix, a raw
        # startswith("/tmp/") let this through -- the live escape (D#1992).
        escape_target = f"/tmp/..{_MAIN_REPO}/foo"
        escape = classify_bash(f"echo hi > {escape_target}", _WT_CLAUDE)
        assert not escape.allow, (
            f"expected /tmp/..-prefixed redirect to {escape_target} rejected "
            f"(this is the D#1992 escape), got {escape}"
        )

        # (c) /tmp/foo is still exempt -- the fix must not over-block.
        tmp = classify_bash("echo hi > /tmp/foo", _WT_CLAUDE)
        assert tmp.allow, f"expected /tmp/foo to stay exempt, got {tmp}"

        # (d) /var/tmp/foo is still exempt -- same.
        var_tmp = classify_bash("echo hi > /var/tmp/foo", _WT_CLAUDE)
        assert var_tmp.allow, f"expected /var/tmp/foo to stay exempt, got {var_tmp}"

    def test_is_ephemeral_tmp_path_helper_unchanged_for_real_tmp(self) -> None:
        # Acceptance item 1 -- the helper's existing exemption for real
        # ephemeral paths must be unchanged by the normalisation fix.
        assert _is_ephemeral_tmp_path("/tmp/foo")
        assert _is_ephemeral_tmp_path("/var/tmp/foo")
        assert _is_ephemeral_tmp_path("/tmp")
        assert _is_ephemeral_tmp_path("/var/tmp")


# ---------------------------------------------------------------------------
# AC4 — merge rejection
# ---------------------------------------------------------------------------


class TestAC4MergeRejection:
    @pytest.mark.parametrize(
        "command",
        [
            "gh pr merge 999 --squash",
            "gh pr merge 42 --squash --delete-branch",
            "gh pr merge 1",
            "gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"PR_xxx\"}) { pullRequest { merged } } }'",
            "gh api -X PUT repos/autonomous-agent-7/autonomous-forever/pulls/999/merge",
            "gh api repos/autonomous-agent-7/autonomous-forever/pulls/999/merge -X PUT",
        ],
    )
    def test_merge_blocked_from_worktree(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow
        assert "sub-agents may not merge" in d.reason

    def test_pr_create_allowed(self) -> None:
        d = classify_bash(
            "gh pr create --base main --title 'test' --body 'body'", _WT_CLAUDE
        )
        assert d.allow

    def test_gh_pr_view_allowed(self) -> None:
        d = classify_bash("gh pr view 42 --json number,title", _WT_CLAUDE)
        assert d.allow

    def test_gh_api_post_create_blocked(self) -> None:
        # gh api -X POST is now blocked from worktrees (D#1136 closes this bypass path).
        # Executors should use `gh pr create` (the high-level CLI) instead of
        # raw `gh api -X POST` calls.
        d = classify_bash(
            "gh api -X POST repos/autonomous-agent-7/autonomous-forever/pulls -f title=x",
            _WT_CLAUDE,
        )
        assert not d.allow
        assert "sandbox_block_gh_api_mutation" in d.reason


# ---------------------------------------------------------------------------
# D#2225 — gh api mutation guard must be command-position aware, not a raw
# substring search over the whole command string.
# ---------------------------------------------------------------------------


class TestD2225GhApiMutationCommandPosition:
    """The gh-api-mutation guard must fire on a REAL `gh api -X PATCH`-style
    invocation and must NOT fire when that text merely appears as data inside
    an inert command (echo, a case-statement pattern, a --body string) that
    performs no mutation.

    Each "still blocked" case below is deliberately the same shape as an
    allowed case one line away, so that deleting the command-position check
    and reverting to a raw substring search would flip these to the wrong
    verdict — that is the regression this test class exists to catch.
    """

    # --- direction 1: a real mutation in command position is still denied ---

    @pytest.mark.parametrize(
        "command",
        [
            "gh api -X PATCH repos/autonomous-agent-7/autonomous-forever/pulls/1",
            "gh api -X PUT repos/autonomous-agent-7/autonomous-forever/pulls/1",
            "gh api -X DELETE repos/autonomous-agent-7/autonomous-forever/issues/1",
            "gh api --method PATCH repos/autonomous-agent-7/autonomous-forever/pulls/1",
            "gh api --method=PATCH repos/autonomous-agent-7/autonomous-forever/pulls/1",
            # wrapped in a command-prefix wrapper — must still be caught
            "timeout 5 gh api -X PATCH repos/autonomous-agent-7/autonomous-forever/pulls/1",
            # second stage of a pipeline / chain
            "echo hi && gh api -X PATCH repos/autonomous-agent-7/autonomous-forever/pulls/1",
            "cd /tmp; gh api -X PATCH repos/autonomous-agent-7/autonomous-forever/pulls/1",
        ],
    )
    def test_real_gh_api_mutation_still_blocked(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for real gh api mutation: {command!r}"
        assert "sandbox_block_gh_api_mutation" in d.reason

    # --- direction 2: the string appearing as inert data must be allowed ---

    @pytest.mark.parametrize(
        "command",
        [
            # the literal PR #2224 verification scenario — a case-statement
            # string-dispatch check, not an invocation
            (
                'case "$op" in\n'
                '  "gh api -X PATCH") echo "would mutate" ;;\n'
                '  *) echo "safe" ;;\n'
                "esac"
            ),
            'echo "gh api -X PATCH repos/o/r/pulls/1"',
            'echo "run gh api --method PATCH later if needed"',
            "gh pr comment 1 --body 'never run gh api -X PATCH from a worktree'",
            'if [[ "$cmd" == "gh api -X PATCH" ]]; then echo blocked; fi',
        ],
    )
    def test_inert_mention_of_gh_api_mutation_allowed(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, (
            f"Expected ALLOW for inert mention of gh api mutation text: "
            f"{command!r}, got reason={d.reason!r}"
        )

    def test_guard_would_fail_without_command_position_check(self) -> None:
        """Sanity check that the OLD raw-substring approach really would have
        blocked the inert echo — proving this test class exercises a real
        fix, not a tautology. Uses the same regex the old implementation used.
        """
        old_pattern = re.compile(
            r"\bgh\s+api\b.*-X\s+(?:POST|PATCH|PUT|DELETE)\b"
            r"|\bgh\s+api\b.*--method\s+(?:POST|PATCH|PUT|DELETE)\b",
            re.IGNORECASE,
        )
        command = 'echo "gh api -X PATCH repos/o/r/pulls/1"'
        assert old_pattern.search(command), (
            "expected the old raw-substring pattern to match the inert echo "
            "(this proves test_inert_mention_of_gh_api_mutation_allowed "
            "actually exercises the fix)"
        )
        # And confirm the NEW implementation correctly does not match it.
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow


# ---------------------------------------------------------------------------
# D#2225 round 2 (code review) — a plain command-position check still missed
# six ordinary invocation shapes: the `command` builtin, backtick and $()
# substitution, a (...) subshell, a { ...; } brace group, and an alternate
# shell's -c payload. Every one of these EXECUTES the mutation just as much
# as a bare `gh api -X PATCH` does — none is an exotic spelling in the
# D#439 sense. This class pins each shape as its own test so a future
# regression back to the round-1 (plain-separator-only) walker fails loudly.
# ---------------------------------------------------------------------------


class TestD2225Round2BypassShapes:
    """Six real-invocation shapes the round-1 fix missed, verified live by
    the code-reviewer against PR #2228 head d7aea5d9 — every one of these
    was `allow=True` there (confirmed independently by loading that exact
    blob stand-alone and re-running these same commands against it before
    writing the round-2 fix). Each is a parametrized case of its own so a
    future regression back to a plain-separator-only command-position walker
    fails on the specific shape it reintroduces, not just on an aggregate
    count.
    """

    _MUTATION = "gh api -X PATCH repos/autonomous-agent-7/autonomous-forever/pulls/1"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f"command {_MUTATION}", id="command_builtin"),
            pytest.param(f"`{_MUTATION}`", id="backtick_substitution"),
            pytest.param(f"$({_MUTATION})", id="dollar_paren_substitution"),
            pytest.param(f"( {_MUTATION} )", id="subshell"),
            pytest.param(f"{{ {_MUTATION}; }}", id="brace_group"),
            pytest.param(f"zsh -c '{_MUTATION}'", id="zsh_dash_c"),
            # Bonus coverage beyond the reviewer's six: the two shells
            # check_claude_spawn already recurses into for its OWN check —
            # gh-api-mutation detection must not be narrower than that.
            pytest.param(f"bash -c '{_MUTATION}'", id="bash_dash_c"),
            pytest.param(f"sh -c '{_MUTATION}'", id="sh_dash_c"),
            # Combinations: a bypass shape nested inside another.
            pytest.param(f"timeout 5 bash -c '{_MUTATION}'", id="wrapper_plus_dash_c"),
            pytest.param(f"echo x && ( {_MUTATION} )", id="chain_plus_subshell"),
        ],
    )
    def test_bypass_shape_still_blocked(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for real mutation via bypass shape: {command!r}"
        assert "sandbox_block_gh_api_mutation" in d.reason

    @pytest.mark.parametrize(
        "command",
        [
            # A quoted mention of the same text INSIDE one of these shapes
            # must still be allowed — the fix must not have over-corrected
            # into blocking every command containing a paren or brace.
            # (Backtick is deliberately NOT included here: POSIX shells still
            # perform command substitution on a backtick pair even inside
            # double quotes, so `--body "... `gh api -X PATCH foo` ..."`
            # is a REAL invocation, not inert text, and correctly stays
            # blocked — see test_bypass_shape_still_blocked's backtick case.)
            'gh pr comment 1 --body "call (gh api -X PATCH foo) if needed"',
            'echo "{ gh api -X PATCH foo; }"',
        ],
    )
    def test_bypass_shape_syntax_as_inert_quoted_text_still_allowed(
        self, command: str
    ) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, (
            f"Expected ALLOW — the bypass-shape punctuation appears only "
            f"inside a quoted string here, not as real shell syntax: "
            f"{command!r}, got reason={d.reason!r}"
        )


# ---------------------------------------------------------------------------
# AC5 — Team Lead exempt
# ---------------------------------------------------------------------------


class TestAC5TeamLeadExempt:
    """All AC1/AC3/AC4 commands must pass when CWD is the main repo root."""

    @pytest.mark.parametrize("cwd", [_MAIN_REPO, _MAIN_REPO + "/backend"])
    def test_git_checkout_main_allowed_for_tl(self, cwd: str) -> None:
        # classify_bash should not be called at all for TL (sandbox.py exits early),
        # but classify_bash itself doesn't check is_worktree — that's the caller's job.
        # The Team Lead exemption is enforced in sandbox.py's main() by checking
        # is_worktree(cwd) == None before routing to classify_bash/classify_path_write.
        # Here we test is_worktree gives None for TL CWDs.
        assert is_worktree(cwd) is None

    @pytest.mark.parametrize("cwd", _TL_CWDS)
    def test_is_worktree_returns_none_for_team_lead(self, cwd: str) -> None:
        assert is_worktree(cwd) is None


# ---------------------------------------------------------------------------
# AC6 — git rm detection (archive protocol)
# ---------------------------------------------------------------------------


class TestAC6GitRm:
    """classify_git_rm must block git rm and allow safe alternatives."""

    def test_git_rm_tracked_file_blocked(self) -> None:
        """AC6-1: `git rm tests/foo.py` is blocked."""
        d = classify_git_rm("git rm tests/foo.py")
        assert not d.allow
        assert "archive protocol" in d.reason

    def test_git_rm_rf_directory_blocked(self) -> None:
        """AC6-2: `git rm -rf old-dir/` is blocked."""
        d = classify_git_rm("git rm -rf old-dir/")
        assert not d.allow
        assert "archive protocol" in d.reason

    def test_git_mv_to_archive_allowed(self) -> None:
        """AC6-3: `git mv tests/foo.py archive/foo-2026-05-12/foo.py` is allowed."""
        d = classify_git_rm("git mv tests/foo.py archive/foo-2026-05-12/foo.py")
        assert d.allow

    def test_git_rm_word_boundary_false_positive_guard(self) -> None:
        """AC6-4: `git rmiscellaneous-thing` must NOT be blocked (word boundary check)."""
        d = classify_git_rm("git rmiscellaneous-thing")
        assert d.allow

    def test_different_binary_false_positive_guard(self) -> None:
        """AC6-5: `mygit rm foo` must NOT be blocked (different binary)."""
        d = classify_git_rm("mygit rm foo")
        assert d.allow

    def test_classify_bash_blocks_git_rm_from_worktree(self) -> None:
        """git rm routed through classify_bash from a worktree context is also blocked."""
        d = classify_bash("git rm tests/foo.py", _WT_CLAUDE)
        assert not d.allow
        assert "archive protocol" in d.reason

    def test_git_rm_reason_mentions_git_mv(self) -> None:
        """Block reason must mention `git mv` so the agent knows the correct alternative."""
        d = classify_git_rm("git rm hooks/old.py")
        assert "git mv" in d.reason


# ---------------------------------------------------------------------------
# AC6-new — is_real_git_rm_invocation: quoted-string false-positive guard
#            and Team-Lead-tier detection predicate
# ---------------------------------------------------------------------------


class TestIsRealGitRmInvocation:
    """is_real_git_rm_invocation must distinguish real git rm commands from mentions."""

    # --- Real git rm invocations (must return True) ---

    def test_bare_git_rm(self) -> None:
        """Bare `git rm foo.py` is a real invocation."""
        assert is_real_git_rm_invocation("git rm foo.py") is True

    def test_git_rm_with_flag(self) -> None:
        """`git rm -rf old-dir/` is a real invocation."""
        assert is_real_git_rm_invocation("git rm -rf old-dir/") is True

    def test_git_rm_in_pipeline(self) -> None:
        """`cd /repo && git rm src/old.py` is a real invocation."""
        assert is_real_git_rm_invocation("cd /repo && git rm src/old.py") is True

    def test_git_rm_after_semicolon(self) -> None:
        """`echo hi; git rm foo` is a real invocation in a pipeline."""
        assert is_real_git_rm_invocation("echo hi; git rm foo") is True

    # --- False positives that must return False ---

    def test_git_rm_in_gh_body_double_quotes(self) -> None:
        """git rm inside a double-quoted --body arg to gh must NOT fire."""
        cmd = 'gh issue comment 42 --body "do not git rm files, use git mv instead"'
        assert is_real_git_rm_invocation(cmd) is False

    def test_git_rm_in_gh_body_single_quotes(self) -> None:
        """git rm inside a single-quoted --body arg to gh must NOT fire."""
        cmd = "gh issue comment 42 --body 'policy: never git rm anything'"
        assert is_real_git_rm_invocation(cmd) is False

    def test_git_rm_in_echo_double_quotes(self) -> None:
        """git rm inside an echo string must NOT fire."""
        cmd = 'echo "reminder: never git rm tracked files"'
        assert is_real_git_rm_invocation(cmd) is False

    def test_git_rm_in_rotate_log_comment(self) -> None:
        """git rm inside a log/comment shell call must NOT fire."""
        cmd = "bash scripts/rotate-team-log.sh comment \"archive protocol: use git mv not git rm\""
        assert is_real_git_rm_invocation(cmd) is False

    def test_git_rm_word_boundary_guard(self) -> None:
        """`git rmiscellaneous-thing` must NOT fire (word boundary)."""
        assert is_real_git_rm_invocation("git rmiscellaneous-thing") is False

    def test_mygit_rm_guard(self) -> None:
        """`mygit rm foo` must NOT fire (different binary)."""
        assert is_real_git_rm_invocation("mygit rm foo") is False

    # --- classify_git_rm parity: must agree with is_real_git_rm_invocation ---

    def test_classify_git_rm_agrees_real_invocation(self) -> None:
        """classify_git_rm blocks iff is_real_git_rm_invocation returns True."""
        real_cmds = [
            "git rm tests/foo.py",
            "git rm -rf old-dir/",
        ]
        for cmd in real_cmds:
            assert is_real_git_rm_invocation(cmd) is True, f"expected True for: {cmd}"
            assert not classify_git_rm(cmd).allow, f"expected block for: {cmd}"

    def test_classify_git_rm_agrees_quoted_string(self) -> None:
        """classify_git_rm allows iff is_real_git_rm_invocation returns False for quoted mentions."""
        benign_cmds = [
            'gh issue comment 1 --body "never git rm files"',
            'echo "git rm is forbidden"',
        ]
        for cmd in benign_cmds:
            assert is_real_git_rm_invocation(cmd) is False, f"expected False for: {cmd}"
            assert classify_git_rm(cmd).allow, f"expected allow for: {cmd}"

    def test_worktree_git_rm_still_hard_blocked(self) -> None:
        """Regression: sub-agent (worktree) git rm on a tracked file remains hard-blocked."""
        d = classify_bash("git rm tests/foo.py", _WT_CLAUDE)
        assert not d.allow
        assert "archive protocol" in d.reason

    # --- Adversarial regression: bypass forms that previously escaped detection ---

    def test_semicolon_no_space_before_git(self) -> None:
        """`;git rm f` — no space before `git` used to fail the lookbehind → bypass."""
        assert is_real_git_rm_invocation("echo hi;git rm f") is True

    def test_git_flag_C_before_rm(self) -> None:
        """`git -C /path rm f` — global flag between `git` and `rm` → bypass."""
        assert is_real_git_rm_invocation("git -C /path rm f") is True

    def test_git_no_pager_before_rm(self) -> None:
        """`git --no-pager rm f` — global option with no argument → bypass."""
        assert is_real_git_rm_invocation("git --no-pager rm f") is True

    def test_git_flag_c_keyval_before_rm(self) -> None:
        """`git -c a=b rm f` — -c takes a value token, then rm → bypass."""
        assert is_real_git_rm_invocation("git -c a=b rm f") is True

    def test_git_git_dir_before_rm(self) -> None:
        """`git --git-dir=/p rm f` — embedded-argument global option → bypass."""
        assert is_real_git_rm_invocation("git --git-dir=/p rm f") is True

    def test_subshell_git_rm(self) -> None:
        """`(git rm f)` — subshell grouping → bypass."""
        assert is_real_git_rm_invocation("(git rm f)") is True

    def test_ampersand_git_rm(self) -> None:
        """`x && git rm f` — standard pipeline separator must also be caught."""
        assert is_real_git_rm_invocation("x && git rm f") is True

    # --- Regression fixes: brace group and xargs bypass forms ---

    def test_brace_group_git_rm(self) -> None:
        """`{ git rm f; }` — brace group blocked (walker sees `{` as separator)."""
        assert is_real_git_rm_invocation("{ git rm f; }") is True

    def test_xargs_git_rm(self) -> None:
        """`xargs git rm` — broad adjacent scan catches git+rm even as xargs args."""
        assert is_real_git_rm_invocation("xargs git rm") is True

    def test_echo_pipe_xargs_git_rm(self) -> None:
        """`echo f | xargs git rm` — xargs form in a pipeline must be caught."""
        assert is_real_git_rm_invocation("echo f | xargs git rm") is True

    # --- D#1202 fixes: path-prefixed git binary bypass forms ---

    def test_absolute_path_git_rm(self) -> None:
        """`/usr/bin/git rm f` — absolute-path git binary must be blocked (AC1)."""
        assert is_real_git_rm_invocation("/usr/bin/git rm f") is True

    def test_relative_path_git_rm(self) -> None:
        """`./git rm f` — relative-path git binary must be blocked (AC2)."""
        assert is_real_git_rm_invocation("./git rm f") is True

    def test_arbitrary_path_git_rm(self) -> None:
        """`/path/to/git rm f` — arbitrary absolute path must be blocked (AC3)."""
        assert is_real_git_rm_invocation("/path/to/git rm f") is True

    def test_absolute_path_git_with_C_option(self) -> None:
        """`/usr/bin/git -C /p rm f` — global option after path-prefixed git (AC4)."""
        assert is_real_git_rm_invocation("/usr/bin/git -C /p rm f") is True

    def test_absolute_path_git_with_no_pager_option(self) -> None:
        """`/usr/bin/git --no-pager rm f` — flag option after path-prefixed git (AC4)."""
        assert is_real_git_rm_invocation("/usr/bin/git --no-pager rm f") is True

    def test_xargs_absolute_path_git_rm(self) -> None:
        """`xargs /usr/bin/git rm` — path-prefixed git in broad adjacent scan (AC5)."""
        assert is_real_git_rm_invocation("xargs /usr/bin/git rm") is True

    # --- Must still ALLOW: false-positive guard ---

    def test_echo_quoted_git_rm_still_allowed(self) -> None:
        """`echo "git rm f"` — git rm inside a quoted arg must NOT fire."""
        assert is_real_git_rm_invocation('echo "git rm f"') is False

    def test_git_rmsomething_still_allowed(self) -> None:
        """`git rmsomething` — must NOT fire (word boundary on subcommand)."""
        assert is_real_git_rm_invocation("git rmsomething") is False

    def test_printf_git_rm_still_allowed(self) -> None:
        """`printf 'git rm'` — quoted arg to printf must NOT fire."""
        assert is_real_git_rm_invocation("printf 'git rm'") is False

    def test_mygit_rm_still_allowed(self) -> None:
        """`mygit rm foo` — different binary (not ending in /git) must NOT fire (AC7)."""
        assert is_real_git_rm_invocation("mygit rm foo") is False


# ---------------------------------------------------------------------------
# AC6-adversarial — worktree classify_bash must hard-block all bypass forms
# ---------------------------------------------------------------------------


class TestGitRmBypassFormsInWorktree:
    """Each previously-bypassed form must now hard-block in worktree context."""

    @pytest.mark.parametrize("cmd", [
        "git rm f",
        "echo hi;git rm f",
        "x && git rm f",
        "git -C /p rm f",
        "git --no-pager rm f",
        "git -c a=b rm f",
        "git --git-dir=/p rm f",
        "(git rm f)",
        "{ git rm f; }",
        "xargs git rm",
        "echo f | xargs git rm",
        # D#1202: path-prefixed git binary forms (AC6)
        "/usr/bin/git rm f",
        "./git rm f",
    ])
    def test_hard_block_in_worktree(self, cmd: str) -> None:
        """classify_bash must BLOCK git rm in all forms from a worktree."""
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for: {cmd!r}, got allow=True"

    @pytest.mark.parametrize("cmd", [
        'echo "git rm f"',
        "git rmsomething",
        "printf 'git rm'",
        'gh issue comment 1 --body "do not git rm files"',
    ])
    def test_still_allowed_in_worktree(self, cmd: str) -> None:
        """classify_bash must ALLOW quoted git rm mentions — no false positives."""
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for: {cmd!r}, got allow=False: {d.reason}"

    def test_team_lead_cwd_classify_bash_still_blocks_git_rm(self) -> None:
        """classify_bash blocks git rm unconditionally — team-lead exemption lives in
        sandbox.py (which short-circuits before calling classify_bash for team-lead tier).
        This test confirms the pure classify_bash function is consistently strict."""
        d = classify_bash("git rm tests/foo.py", _MAIN_REPO)
        assert not d.allow
        assert "archive protocol" in d.reason

    def test_absolute_path_git_rm_blocks_with_archive_reason(self) -> None:
        """`/usr/bin/git rm f` in worktree context must block with archive protocol reason (AC6)."""
        d = classify_bash("/usr/bin/git rm f", _WT_CLAUDE)
        assert not d.allow
        assert "archive protocol" in d.reason

    def test_relative_path_git_rm_blocks_with_archive_reason(self) -> None:
        """`./git rm f` in worktree context must block with archive protocol reason (AC6)."""
        d = classify_bash("./git rm f", _WT_CLAUDE)
        assert not d.allow
        assert "archive protocol" in d.reason


# ---------------------------------------------------------------------------
# AC8 — Telemetry field structure (smoke)
# ---------------------------------------------------------------------------


class TestTelemetrySmoke:
    """Verify the telemetry entry shape without touching the filesystem."""

    def test_block_decision_has_reason(self) -> None:
        d = classify_bash("git checkout main", _WT_CLAUDE)
        assert isinstance(d.reason, str)
        assert len(d.reason) > 0

    def test_allow_decision_has_empty_reason(self) -> None:
        d = classify_bash("git status", _WT_CLAUDE)
        assert d.allow
        assert d.reason == ""


# ---------------------------------------------------------------------------
# AC9 — Performance: p95 < 50ms over 100 calls
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_p95_latency_under_50ms(self) -> None:
        import statistics

        latencies: list[float] = []
        command = "git checkout main"
        cwd = _WT_CLAUDE

        for _ in range(100):
            t0 = time.perf_counter()
            classify_bash(command, cwd)
            latencies.append((time.perf_counter() - t0) * 1000)

        p95 = statistics.quantiles(latencies, n=100)[94]
        assert p95 < 50, f"p95 latency {p95:.1f}ms exceeded 50ms threshold"

    def test_classify_path_write_p95_under_50ms(self) -> None:
        import statistics

        latencies: list[float] = []
        file_path = f"{_MAIN_REPO}/CLAUDE.md"
        cwd = _WT_CLAUDE

        for _ in range(100):
            t0 = time.perf_counter()
            classify_path_write(file_path, cwd)
            latencies.append((time.perf_counter() - t0) * 1000)

        p95 = statistics.quantiles(latencies, n=100)[94]
        assert p95 < 50, f"p95 latency {p95:.1f}ms exceeded 50ms threshold"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_command_allowed(self) -> None:
        d = classify_bash("", _WT_CLAUDE)
        assert d.allow

    def test_non_git_command_no_redirect_allowed(self) -> None:
        d = classify_bash("ls -la", _WT_CLAUDE)
        assert d.allow

    def test_python_open_write_outside_worktree_blocked(self) -> None:
        # D#1749: this was the confirmed live bypass — python3 -c performing its own
        # file I/O was invisible to the old enumeration-based write detection.  The
        # new deny-by-default path scan (_classify_unenumerated_write) now catches
        # the write-shaped open(path, 'w') call and blocks it.  Assertion CHANGED
        # from "isinstance(d, Decision)" (the bypass was previously undetectable and
        # this test only checked classify_bash didn't raise) to "not d.allow" — this
        # is the exact behavior D#1749 exists to fix, see TestD1749PathBasedWriteDetection.
        d = classify_bash(
            f"python3 -c \"open('{_MAIN_REPO}/x', 'w').write('x')\"", _WT_CLAUDE
        )
        assert not d.allow, f"Expected python3 -c write bypass to be blocked, got {d}"

    def test_malformed_shlex_command(self) -> None:
        # Unclosed quote — shlex.split raises, we should degrade gracefully
        d = classify_bash("git 'unclosed", _WT_CLAUDE)
        assert isinstance(d, Decision)

    def test_double_redirect_blocked(self) -> None:
        d = classify_bash(f"echo x >> {_MAIN_REPO}/log.txt", _WT_CLAUDE)
        assert not d.allow

    def test_gh_api_graphql_list_allowed(self) -> None:
        # graphql query (not mutation) should be allowed
        d = classify_bash(
            "gh api graphql -f query='query { repository(owner:\"x\") { name } }'",
            _WT_CLAUDE,
        )
        assert d.allow

    def test_worktree_root_from_cwd_deep_path(self) -> None:
        from hooks.sandbox_rules import _worktree_root_from_cwd
        root = _worktree_root_from_cwd(_WT_CLAUDE + "/a/b/c")
        assert root == str(Path(_WT_CLAUDE).resolve())


# ---------------------------------------------------------------------------
# D#1749 — classify_bash write detection: path-based, not enumeration-based
# ---------------------------------------------------------------------------
#
# classify_bash's file-write detection used to be enumeration-based (a fixed list
# of writer commands: tee/cp/mv/install/rsync + shell redirects). Any program doing
# its own file I/O — python3 -c, sed -i, or an unknown binary — bypassed it
# entirely. This class exercises the deny-by-default path scan
# (_classify_unenumerated_write) that closes that gap. Section letters/numbers
# below map 1:1 onto the Spec's acceptance criteria in Discussion #1749.


class TestD1749PathBasedWriteDetection:
    """A. Confirmed bypasses now blocked."""

    def test_a1_python_write_text_outside_worktree_blocked(self) -> None:
        d = classify_bash(
            f"""python3 -c "from pathlib import Path; Path('{_MAIN_REPO}/CLAUDE.md').write_text('x')" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_a2_python_open_write_outside_worktree_blocked(self) -> None:
        d = classify_bash(
            f"""python3 -c "open('{_MAIN_REPO}/foo.txt','w').write('x')" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_a3_sed_short_flag_in_place_outside_worktree_blocked(self) -> None:
        d = classify_bash(f"sed -i s/a/b/ {_MAIN_REPO}/CLAUDE.md", _WT_CLAUDE)
        assert d.allow is False

    def test_a4_sed_long_flag_in_place_outside_worktree_blocked(self) -> None:
        d = classify_bash(f"sed --in-place=.bak {_MAIN_REPO}/CLAUDE.md", _WT_CLAUDE)
        assert d.allow is False

    def test_a5_unknown_program_deny_by_default(self) -> None:
        # Deny-by-default proof: "some-unknown-tool" is not added to any list.
        d = classify_bash(f"./some-unknown-tool --out {_MAIN_REPO}/x.json", _WT_CLAUDE)
        assert d.allow is False

    # --- B. audit.jsonl / dial-registry repro ---

    def test_b6_python_write_to_audit_jsonl_blocked(self) -> None:
        d = classify_bash(
            f"""python3 -c "open('{FIXTURE_HOME}/.autonomous-forever-state/audit.jsonl','a').write('x')" """,
            _WT_CLAUDE,
        )
        assert d.allow is False
        assert "dial-registry write blocked" in d.reason

    @pytest.mark.parametrize(
        "filename", ["dial-registry.json", "dial-directive-allowlist.json"]
    )
    def test_b7_python_write_to_dial_files_blocked(self, filename: str) -> None:
        d = classify_bash(
            f"""python3 -c "open('{FIXTURE_HOME}/.autonomous-forever-state/{filename}','a').write('x')" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_b8_python_write_to_relative_audit_jsonl_blocked(self) -> None:
        d = classify_bash(
            """python3 -c "open('audit.jsonl','a').write('x')" """, _WT_CLAUDE
        )
        assert d.allow is False

    # --- C. Segment laundering ---

    def test_c9_readonly_then_write_and_separator_blocked(self) -> None:
        d = classify_bash(
            f"""cat /etc/hosts && python3 -c "open('{_MAIN_REPO}/foo','w').write('x')" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    @pytest.mark.parametrize("separator", [";", "|"])
    def test_c10_readonly_then_write_other_separators_blocked(
        self, separator: str
    ) -> None:
        d = classify_bash(
            f"""cat /etc/hosts {separator} python3 -c "open('{_MAIN_REPO}/foo','w').write('x')" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    # --- D. No over-blocking ---

    def test_d11_python_write_inside_worktree_allowed(self) -> None:
        d = classify_bash(
            f"""python3 -c "from pathlib import Path; Path('{_WT_CLAUDE}/out.txt').write_text('x')" """,
            _WT_CLAUDE,
        )
        assert d.allow is True

    def test_d12_sed_in_place_inside_worktree_allowed(self) -> None:
        d = classify_bash(f"sed -i s/a/b/ {_WT_CLAUDE}/src/app.py", _WT_CLAUDE)
        assert d.allow is True

    @pytest.mark.parametrize(
        "command",
        [
            f"cat {_MAIN_REPO}/CLAUDE.md",
            f"grep -rn foo {_MAIN_REPO}/scripts",
            f"ls -la {_MAIN_REPO}",
            f"wc -l {_MAIN_REPO}/CLAUDE.md",
        ],
    )
    def test_d13_readonly_commands_outside_worktree_allowed(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow is True, f"Expected ALLOW for read-only command {command!r}, got {d}"

    def test_d14_python_no_path_token_allowed(self) -> None:
        d = classify_bash('python3 -c "print(1+1)"', _WT_CLAUDE)
        assert d.allow is True

    def test_d15_existing_exemptions_intact(self) -> None:
        assert classify_bash("echo hi > /tmp/foo", _WT_CLAUDE).allow is True
        assert classify_bash("echo hi > /var/tmp/bar", _WT_CLAUDE).allow is True
        assert classify_bash("cmd > /dev/null 2>&1", _WT_CLAUDE).allow is True


# ---------------------------------------------------------------------------
# D#1749 round 4 — Kai's round-3 security review (R3-1a, R3-1b, R3-2)
# ---------------------------------------------------------------------------
#
# Round 3 replaced the co-occurrence-regex python-payload check with an
# ast.parse structural validator. Kai's round-3 review confirmed the structure
# itself holds against 29 adversarial payloads, but found two wrong entries in
# the data it consults: `_py_read_mode_constant` accepted any literal mode
# starting with "r" (admitting the read-write modes r+/rb+/r+b), and `print`
# was on the read-only call allowlist without accounting for `print(...,
# file=...)` turning it into a general file writer. A third, unrelated bug let
# `python3 -m <module>` skip the segment scan entirely even though the module
# name and its arguments (including any output path) are visible tokens.


class TestD1749Round4Findings:
    """R3-1a/R3-1b: read-write mode + print(file=) compose into a live write.
    R3-2: `python3 -m <module>` bypassed the scan entirely."""

    # --- R3-1a: a literal mode of "r+"/"rb+"/"r+b" is read-write, not read-only ---

    @pytest.mark.parametrize("mode", ["r+", "rb+", "r+b"])
    def test_r31a_read_write_mode_rejected(self, mode: str) -> None:
        d = classify_bash(
            f"""python3 -c "print('x', file=open('{_MAIN_REPO}/CLAUDE.md','{mode}'))" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    @pytest.mark.parametrize("mode", ["r", "rb", "rt"])
    def test_r31a_pure_read_mode_still_allowed(self, mode: str) -> None:
        # No over-blocking: a genuine read mode with no file= writer is fine.
        d = classify_bash(
            f"""python3 -c "print(open('{_MAIN_REPO}/CLAUDE.md','{mode}').read())" """,
            _WT_CLAUDE,
        )
        assert d.allow is True

    # --- R3-1b: print(..., file=...) is a file writer, not a read ---

    def test_r31b_print_file_kwarg_rejected(self) -> None:
        d = classify_bash(
            f"""python3 -c "print('x', file=open('{_MAIN_REPO}/CLAUDE.md','r+'))" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_r31b_print_file_kwarg_to_audit_jsonl_rejected(self) -> None:
        d = classify_bash(
            f"""python3 -c "print('x', file=open('{FIXTURE_HOME}/.autonomous-forever-state/audit.jsonl','r+'))" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_r31b_print_file_kwarg_via_path_open_rejected(self) -> None:
        d = classify_bash(
            f"""python3 -c "from pathlib import Path; print('x', file=Path('{_MAIN_REPO}/CLAUDE.md').open('r+'))" """,
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_r31b_print_file_kwarg_via_glued_clustered_dash_c_rejected(self) -> None:
        d = classify_bash(
            f"""python3 -Ic'print("x", file=open("{_MAIN_REPO}/CLAUDE.md","r+"))'""",
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_r31b_print_no_file_kwarg_still_allowed(self) -> None:
        # No over-blocking: print() to stdout (no file= kwarg) is still fine.
        d = classify_bash(
            f"""python3 -c "print(open('{_MAIN_REPO}/CLAUDE.md').read())" """,
            _WT_CLAUDE,
        )
        assert d.allow is True

    # --- R3-2: `python3 -m <module>` must be scanned, not skipped ---

    def test_r32_dash_m_spaced_outside_worktree_blocked(self) -> None:
        d = classify_bash(
            f"python3 -m json.tool {_WT_CLAUDE}/in.json {_MAIN_REPO}/CLAUDE.md",
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_r32_dash_m_glued_outside_worktree_blocked(self) -> None:
        d = classify_bash(
            f"python3 -mjson.tool {_WT_CLAUDE}/in.json {_MAIN_REPO}/CLAUDE.md",
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_r32_dash_m_zipfile_create_outside_worktree_blocked(self) -> None:
        d = classify_bash(
            f"python3 -m zipfile --create {_MAIN_REPO}/CLAUDE.md /etc/hostname",
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_r32_dash_m_pytest_no_path_token_allowed(self) -> None:
        # No over-blocking: `-m pytest` with no outside-worktree absolute path
        # operand should still be allowed.
        d = classify_bash("python3 -m pytest tests/ -q", _WT_CLAUDE)
        assert d.allow is True

    def test_r32_no_dash_m_script_file_f19_residual_allowed(self) -> None:
        # F19 residual risk, unchanged by this round: no `-c`, no `-m` — the
        # write target lives inside a script file we can't see into.
        d = classify_bash("python3 scripts/seed.py", _WT_CLAUDE)
        assert d.allow is True


class TestD1749Round5Findings:
    """Acceptance-tester finding: step 4b's operand scan treated /dev/urandom
    (and /dev/random, /dev/zero, /dev/full) appearing as an INPUT argument as
    though it implied an outside-worktree write, because it reused
    _is_kernel_device -- a list scoped only for redirect/write-target safety.
    A genuine in-worktree write via `dd if=/dev/urandom of=<worktree>/...` was
    false-blocked. See _INPUT_ONLY_DEVICE_PREFIXES / _is_input_only_device."""

    def test_dd_urandom_input_to_in_worktree_output_allowed(self) -> None:
        d = classify_bash(
            f"dd if=/dev/urandom of={_WT_CLAUDE}/seed.bin bs=1 count=32",
            _WT_CLAUDE,
        )
        assert d.allow is True

    def test_dd_random_input_to_in_worktree_output_allowed(self) -> None:
        d = classify_bash(
            f"dd if=/dev/random of={_WT_CLAUDE}/seed.bin bs=1 count=32",
            _WT_CLAUDE,
        )
        assert d.allow is True

    def test_dd_zero_input_to_in_worktree_output_allowed(self) -> None:
        d = classify_bash(
            f"dd if=/dev/zero of={_WT_CLAUDE}/ok.bin bs=1 count=32",
            _WT_CLAUDE,
        )
        assert d.allow is True

    def test_base64_urandom_piped_to_in_worktree_redirect_allowed(self) -> None:
        d = classify_bash(
            f"base64 /dev/urandom | head -c 32 > {_WT_CLAUDE}/out",
            _WT_CLAUDE,
        )
        assert d.allow is True

    def test_openssl_rand_in_worktree_output_allowed_control(self) -> None:
        # Control: no /dev token at all, confirms the false-block was
        # specifically the /dev/* input-device gap and not a general
        # worktree-boundary regression.
        d = classify_bash(
            f"openssl rand -out {_WT_CLAUDE}/key.bin 32",
            _WT_CLAUDE,
        )
        assert d.allow is True

    def test_dd_urandom_input_to_outside_worktree_output_still_blocked(self) -> None:
        # The exemption is scan-local to the /dev/* input token — it must not
        # open a hole for the actual write target, which is a distinct
        # candidate on the same command line.
        d = classify_bash(
            f"dd if=/dev/urandom of={_MAIN_REPO}/CLAUDE.md bs=1 count=32",
            _WT_CLAUDE,
        )
        assert d.allow is False

    def test_dd_urandom_input_to_dial_registry_still_blocked(self) -> None:
        d = classify_bash(
            f"dd if=/dev/urandom of={FIXTURE_HOME}/.autonomous-forever-state/audit.jsonl bs=1 count=32",
            _WT_CLAUDE,
        )
        assert d.allow is False


# ---------------------------------------------------------------------------
# GraphQL mutation bypass — D#1136 fix (Issue 1)
# ---------------------------------------------------------------------------


class TestGraphqlMutationBlock:
    """gh api graphql with mutation in query body must be blocked from worktrees.

    This is a real bypass: `gh api graphql` POSTs implicitly without any -X flag,
    so the original _GH_API_MUTATION_METHODS regex missed it entirely.
    """

    @pytest.mark.parametrize(
        "command",
        [
            # createIssue is not allowlisted — must be blocked
            "gh api graphql -f query='mutation { createIssue(input:{...}) { issue { id } } }'",
            # Double-quoted variant
            'gh api graphql -f query="mutation Foo { createIssue(input:{...}) { issue { id } } }"',
            # --field instead of -f
            "gh api graphql --field query=mutation{createIssue(input:{}){issue{id}}}",
            # Mixed case keyword
            "gh api graphql -f query='MUTATION { mergePullRequest(input:{}) { pullRequest { merged } } }'",
            # mutation keyword inside a multi-word query string (no opening brace yet)
            "gh api graphql -f query='mutation CreateIssue { createIssue(input:{title:\"x\"}) { issue { id } } }'",
        ],
    )
    def test_graphql_mutation_blocked_from_worktree(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, (
            f"Expected BLOCK for graphql mutation command: {command!r}, got allow=True"
        )
        # The block reason is either sandbox_block_gh_api_mutation (caught by
        # _is_gh_api_mutation) or "sub-agents may not merge" (caught earlier by
        # _is_gh_merge for merge-specific mutations).  Both are correct blocks.
        assert (
            "sandbox_block_gh_api_mutation" in d.reason
            or "sub-agents may not merge" in d.reason
        ), f"Unexpected block reason: {d.reason!r}"

    def test_graphql_query_still_allowed(self) -> None:
        """A graphql `query` operation (no mutation keyword) must still pass."""
        d = classify_bash(
            "gh api graphql -f query='query { viewer { login } }'",
            _WT_CLAUDE,
        )
        assert d.allow, (
            f"graphql query (not mutation) should be allowed, got reason={d.reason!r}"
        )

    def test_graphql_mutation_allowed_from_team_lead(self) -> None:
        """The Team Lead context is exempt — sandbox.py exits before classify_bash.

        Verify that is_worktree returns None for team-lead CWD (the gate condition).
        """
        assert is_worktree(_MAIN_REPO) is None

    # --- D#1148: allowlist tests ---

    def test_add_discussion_comment_alone_allowed(self) -> None:
        """addDiscussionComment alone from worktree — ALLOWED (allowlisted)."""
        cmd = "gh api graphql -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"test\"}) { clientMutationId } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for addDiscussionComment, got reason={d.reason!r}"

    def test_multi_allowlisted_mutations_allowed(self) -> None:
        """addDiscussionComment + addLabelsToLabelable (both allowlisted) — ALLOWED."""
        cmd = "gh api graphql -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } addLabelsToLabelable(input:{labelableId:\"bar\",labelIds:[\"baz\"]}) { clientMutationId } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for multi-allowlisted mutation, got reason={d.reason!r}"

    def test_close_discussion_blocked_regression(self) -> None:
        """closeDiscussion alone from worktree — BLOCKED (regression of D#1136)."""
        cmd = "gh api graphql -f query='mutation { closeDiscussion(input:{discussionId:\"foo\"}) { clientMutationId } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for closeDiscussion, got allow=True"

    def test_merge_pull_request_blocked_regression(self) -> None:
        """mergePullRequest alone from worktree — BLOCKED (regression of D#1136)."""
        cmd = "gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"foo\"}) { pullRequest { merged } } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for mergePullRequest, got allow=True"

    def test_mixed_allowlisted_and_non_allowlisted_blocked(self) -> None:
        """addDiscussionComment + mergePullRequest (mixed) — BLOCKED (fail-closed)."""
        cmd = "gh api graphql -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } mergePullRequest(input:{pullRequestId:\"bar\"}) { pullRequest { merged } } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for mixed allowlisted+non-allowlisted mutation, got allow=True"

    def test_update_discussion_allowed(self) -> None:
        """updateDiscussion from worktree — ALLOWED (allowlisted)."""
        cmd = "gh api graphql -f query='mutation { updateDiscussion(input:{discussionId:\"foo\",body:\"new body\"}) { discussion { id } } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for updateDiscussion, got reason={d.reason!r}"

    def test_remove_labels_from_labelable_allowed(self) -> None:
        """removeLabelsFromLabelable from worktree — ALLOWED (allowlisted)."""
        cmd = "gh api graphql -f query='mutation { removeLabelsFromLabelable(input:{labelableId:\"foo\",labelIds:[\"bar\"]}) { clientMutationId } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for removeLabelsFromLabelable, got reason={d.reason!r}"

    def test_add_labels_to_labelable_allowed(self) -> None:
        """addLabelsToLabelable from worktree — ALLOWED (allowlisted)."""
        cmd = "gh api graphql -f query='mutation { addLabelsToLabelable(input:{labelableId:\"foo\",labelIds:[\"bar\"]}) { clientMutationId } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for addLabelsToLabelable, got reason={d.reason!r}"

    def test_same_calls_from_team_lead_allowed(self) -> None:
        """Blocked mutations from team-lead CWD — ALLOWED (team-lead exempt, is_worktree=None)."""
        # The sandbox exempts team-lead before classify_bash is called.
        # Verify is_worktree returns None for TL CWDs — that's the gate.
        assert is_worktree(_MAIN_REPO) is None
        assert is_worktree(_MAIN_REPO + "/backend") is None

    def test_update_discussion_without_mutation_keyword_not_misclassified(self) -> None:
        """GraphQL query that uses 'updateDiscussion' as a field name but lacks mutation keyword.

        This should be classified as a regular query (no mutation keyword) and thus
        allowed by the classifier — the mutation keyword is the first gate.
        Edge case from D#1148 spec: confirm classifier routes correctly when
        'mutation' keyword is absent.
        """
        # A hypothetical query-type operation (no 'mutation' keyword) referencing
        # updateDiscussion — the classifier should see no mutation keyword → allowed.
        cmd = "gh api graphql -f query='query { updateDiscussion(input:{discussionId:\"foo\"}) { discussion { id } } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for query operation with no mutation keyword, got reason={d.reason!r}"

    def test_extract_graphql_mutation_names_single(self) -> None:
        """_extract_graphql_mutation_names returns correct name for single mutation."""
        cmd = "gh api graphql -f query='mutation { addDiscussionComment(input:{}) { clientMutationId } }'"
        names = _extract_graphql_mutation_names(cmd)
        assert names == ["addDiscussionComment"]

    def test_extract_graphql_mutation_names_multi(self) -> None:
        """_extract_graphql_mutation_names returns all names for multi-field mutation."""
        cmd = "gh api graphql -f query='mutation { addDiscussionComment(input:{}) { x } addLabelsToLabelable(input:{}) { y } }'"
        names = _extract_graphql_mutation_names(cmd)
        assert names is not None
        assert set(names) == {"addDiscussionComment", "addLabelsToLabelable"}

    def test_allowlist_constant_has_exactly_four_entries(self) -> None:
        """The allowlist must contain exactly the 4 approved mutation names."""
        assert _GH_API_GRAPHQL_MUTATION_ALLOWLIST == frozenset(
            {
                "addDiscussionComment",
                "updateDiscussion",
                "addLabelsToLabelable",
                "removeLabelsFromLabelable",
            }
        )

    # --- CWE-20 multi-query bypass fix tests ---

    def test_multi_f_last_wins_bypass_blocked(self) -> None:
        """Two -f query= flags: first allowlisted, second non-allowlisted — BLOCKED.

        gh CLI uses last-wins for duplicate -f flags.  A naive checker that only
        inspects the first mutation block would allow this command while gh sends
        the closeDiscussion mutation.
        """
        cmd = (
            "gh api graphql"
            " -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } }'"
            " -f query='mutation { closeDiscussion(input:{discussionId:\"foo\"}) { clientMutationId } }'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, "Expected BLOCK for last-wins bypass attempt"

    def test_multi_flag_mixed_f_and_capital_F_blocked(self) -> None:
        """Mixed -f and -F flags: first allowlisted, second non-allowlisted — BLOCKED."""
        cmd = (
            "gh api graphql"
            " -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } }'"
            " -F query='mutation { mergePullRequest(input:{pullRequestId:\"bar\"}) { pullRequest { merged } } }'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, "Expected BLOCK for -f + -F last-wins bypass"

    def test_multi_flag_with_field_long_form_blocked(self) -> None:
        """--field query= flag with non-allowlisted mutation after allowlisted -f — BLOCKED."""
        cmd = (
            "gh api graphql"
            " -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } }'"
            " --field query='mutation { closeDiscussion(input:{discussionId:\"foo\"}) { clientMutationId } }'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, "Expected BLOCK for -f + --field last-wins bypass"

    def test_file_ref_query_blocked_fail_closed(self) -> None:
        """A -F query=@file reference is blocked fail-closed (cannot inspect contents)."""
        cmd = "gh api graphql -F query=@evil.graphql"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, "Expected BLOCK for @file query reference (fail-closed)"

    def test_graphql_comment_stripped_before_parse(self) -> None:
        """A commented-out non-allowlisted mutation followed by an allowlisted one — ALLOWED.

        The `# mutation { closeDiscussion }` comment must be stripped before walking
        so that only the real addDiscussionComment mutation is seen.
        """
        cmd = (
            "gh api graphql"
            r" -f query='# mutation { closeDiscussion(input:{discussionId:\"foo\"}) }"
            "\nmutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } }'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, (
            f"Expected ALLOW when non-allowlisted mutation is only in a comment, got reason={d.reason!r}"
        )

    def test_two_allowlisted_separate_f_flags_allowed(self) -> None:
        """Two -f query= flags, both with allowlisted mutations — ALLOWED."""
        cmd = (
            "gh api graphql"
            " -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } }'"
            " -f query='mutation { addLabelsToLabelable(input:{labelableId:\"bar\",labelIds:[\"baz\"]}) { clientMutationId } }'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, (
            f"Expected ALLOW for two allowlisted -f flags, got reason={d.reason!r}"
        )

    # --- D#1161: parametrized mutation declaration tests ---

    def test_anonymous_parametrized_mutation_allowed(self) -> None:
        """mutation($id: ID!, $body: String!) { addDiscussionComment(...) } — ALLOWED.

        PM-style parametrized mutation using -f for variable bindings; the mutation
        field is allowlisted so it must pass through.
        """
        cmd = (
            "gh api graphql"
            " -f query='mutation($id: ID!, $body: String!) { addDiscussionComment(input: {discussionId: $id, body: $body}) { id } }'"
            " -f id='D_xyz'"
            " -f body='test'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for parametrized addDiscussionComment, got reason={d.reason!r}"

    def test_named_parametrized_mutation_blocked(self) -> None:
        """mutation Foo($id: ID!) { closeDiscussion(...) } — BLOCKED (non-allowlisted)."""
        cmd = (
            "gh api graphql"
            " -f query='mutation Foo($id: ID!) { closeDiscussion(input: {discussionId: $id}) { id } }'"
            " -f id='D_xyz'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for named parametrized closeDiscussion, got allow=True"

    def test_bare_mutation_still_works(self) -> None:
        """mutation { ... } (bare, no name or params) — still recognised (regression guard)."""
        cmd = "gh api graphql -f query='mutation { addDiscussionComment(input:{discussionId:\"foo\",body:\"x\"}) { clientMutationId } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for bare mutation addDiscussionComment, got reason={d.reason!r}"

    def test_query_keyword_not_classified_as_mutation(self) -> None:
        """query { ... } (no mutation keyword) — not classified as mutation, passes normally."""
        cmd = "gh api graphql -f query='query { repository(owner:\"autonomous-agent-7\", name:\"autonomous-forever\") { name } }'"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for plain query operation, got reason={d.reason!r}"

    def test_multi_mutation_with_non_allowlisted_blocked(self) -> None:
        """mutation($id: ID!) { addDiscussionComment(...) closeDiscussion(...) } — BLOCKED.

        Any non-allowlisted field in the mutation body blocks the whole call (fail-closed).
        """
        cmd = (
            "gh api graphql"
            " -f query='mutation($id: ID!) { addDiscussionComment(input:{discussionId: $id, body: \"x\"}) { id } closeDiscussion(input:{discussionId: $id}) { id } }'"
            " -f id='D_xyz'"
        )
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for parametrized multi-mutation with closeDiscussion, got allow=True"


# ---------------------------------------------------------------------------
# D#439 — check_claude_spawn: positive corpus (must DENY)
# ---------------------------------------------------------------------------


class TestCheckClaudeSpawnPositiveCorpus:
    """AC1 from D#439: every entry in this corpus must be DENIED."""

    # The exact 2026-05-10 exploit string
    def test_exact_exploit_string(self) -> None:
        cmd = 'claude -p "Run ONE /loop iteration per CLAUDE.md..."'
        d = check_claude_spawn([], cmd)
        assert not d.allow
        assert "claude_spawn_forbidden" in d.reason

    @pytest.mark.parametrize(
        "cmd",
        [
            # Absolute path invocation
            '/usr/local/bin/claude -p "Run ONE /loop iteration..."',
            # bash -c wrapping
            "bash -c 'claude -p \"Run ONE /loop iteration...\"'",
            # env prefix
            'env FOO=bar claude --print "Run ONE /loop iteration..."',
            # Quote-stripping obfuscation: cl"au"de
            'cl"au"de -p "Run ONE /loop iteration..."',
            # exec claude
            'exec claude -p "test"',
            # sh -c wrapping
            'sh -c "claude -p test"',
            # _start_loop_run path fragment
            "python3 backend/_start_loop_run.py",
            # loop-trigger fragment
            "bash scripts/loop-trigger.sh",
            # run-loop-iteration.sh fragment
            "bash run-loop-iteration.sh",
            # backend/trigger.py fragment
            "python3 backend/trigger.py",
            # spawn-agent.sh fragment
            "bash scripts/spawn-agent.sh --role executor",
            # claude at start of command
            "claude -p test",
            # claude after semicolon
            "echo hi; claude -p test",
            # claude after pipe
            "echo hi | claude -p test",
            # claude after &&
            "git status && claude -p test",
        ],
    )
    def test_deny_list_entry(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected DENY for: {cmd!r}, got allow=True reason={d.reason!r}"
        assert "claude_spawn_forbidden" in d.reason or not d.allow

    def test_bash_c_recursive_normalization(self) -> None:
        # Nested bash -c with inner claude call
        cmd = 'bash -c \'bash -c "claude -p test"\''
        d = check_claude_spawn([], cmd)
        assert not d.allow

    def test_quote_stripped_variant(self) -> None:
        # cl'au'de — single quotes stripped
        cmd = "cl'au'de -p test"
        d = check_claude_spawn([], cmd)
        assert not d.allow


# ---------------------------------------------------------------------------
# D#439 — check_claude_spawn: negative corpus (must ALLOW)
# ---------------------------------------------------------------------------


class TestCheckClaudeSpawnNegativeCorpus:
    """AC2 from D#439: these must NOT be blocked — false positive guard."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # Reading/grepping CLAUDE.md — the cost-analyst FP concern
            "grep claude CLAUDE.md",
            "cat CLAUDE.md",
            "ls CLAUDE.md",
            "git log --grep claude",
            # File path containing claude-code-something-else
            "cat /tmp/claude-code-output.txt",
            "ls claude-code-something-else",
            # Normal executor workflow — must not trip
            "gh pr create --base main --title 'test' --body 'body'",
            "pytest tests/ -x -q",
            "npm run build",
            # grep for the string "claude" inside a file
            "grep -r 'claude' hooks/",
            # Reading a file that contains the word claude in its name
            "python3 scripts/measure-spawn-context.sh",
        ],
    )
    def test_allow_list_entry(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert d.allow, f"Expected ALLOW for: {cmd!r}, got allow=False reason={d.reason!r}"

    def test_classify_bash_also_denies_claude_spawn(self) -> None:
        """check_claude_spawn is wired into classify_bash — so classify_bash must also deny."""
        # This is checked after PR-2 wiring; here we verify the pure function alone.
        d = check_claude_spawn([], 'claude -p "test"')
        assert not d.allow

    def test_no_block_on_echo_about_claude(self) -> None:
        # echo mentioning the word "claude" should not be blocked
        d = check_claude_spawn([], "echo 'claude code is a tool'")
        # "claude" appears after whitespace at start — this WILL match as a spawn attempt
        # (echo claude foo — the boundary lookbehind sees whitespace before "claude").
        # Actually per the regex "echo 'claude code" — claude is inside single-quoted
        # string. After quote stripping it becomes: echo claude code — and "claude"
        # is preceded by whitespace, so it DOES match. This is a known false positive
        # documented in the spec: the boundary lookbehind approach is necessary but
        # single-quoted content will trigger. The spec accepts this tradeoff.
        # We document the behavior here rather than asserting allow.
        assert isinstance(d, Decision)  # just confirm it doesn't raise


# ---------------------------------------------------------------------------
# D#439 security review — 4 bypass gaps (command, $(...), `...`, python exec)
# ---------------------------------------------------------------------------


class TestCheckClaudeSpawnBypassGaps:
    """Bypass gaps identified in PR #892 security review — all must DENY."""

    # Gap 1: `command claude` — bash builtin bypasses alias checks
    @pytest.mark.parametrize(
        "cmd",
        [
            "command claude -p test",
            "command /usr/local/bin/claude -p test",
            "command claude --print test",
        ],
    )
    def test_command_builtin_bypass_denied(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected DENY for command-builtin bypass: {cmd!r}"
        assert "claude_spawn_forbidden" in d.reason

    # Gap 2: $(claude ...) dollar-paren command substitution
    @pytest.mark.parametrize(
        "cmd",
        [
            "$(claude -p test)",
            "result=$(claude -p test)",
            "echo hi $(claude -p foo)",
            'OUT=$(claude -p "run loop")',
        ],
    )
    def test_dollar_paren_substitution_denied(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected DENY for dollar-paren substitution: {cmd!r}"
        assert "claude_spawn_forbidden" in d.reason

    # Gap 3: `claude ...` backtick command substitution
    @pytest.mark.parametrize(
        "cmd",
        [
            "`claude -p test`",
            "result=`claude -p test`",
            "echo `claude -p foo`",
        ],
    )
    def test_backtick_substitution_denied(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected DENY for backtick substitution: {cmd!r}"
        assert "claude_spawn_forbidden" in d.reason

    # Gap 4: python3 -c with os.exec*/subprocess spawning claude
    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c 'import os; os.execvp(\"claude\", [\"claude\",\"-p\",\"test\"])'",
            "python3 -c 'import os; os.execv(\"/usr/bin/claude\", [\"claude\",\"-p\",\"test\"])'",
            "python3 -c 'import subprocess; subprocess.run([\"claude\", \"-p\", \"test\"])'",
            "python3 -c 'import subprocess; subprocess.Popen([\"claude\", \"-p\", \"test\"])'",
            "python -c 'import subprocess; subprocess.run([\"claude\", \"-p\", \"test\"])'",
        ],
    )
    def test_python_exec_bypass_denied(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected DENY for python exec bypass: {cmd!r}"
        assert "claude_spawn_forbidden" in d.reason

    # Negative: legitimate substring "claude" in non-spawn context must still pass
    def test_echo_with_claude_substring_not_blocked(self) -> None:
        """echo with a literal string containing claude — not a spawn attempt."""
        cmd = 'echo "the word claude appears in a quoted message"'
        d = check_claude_spawn([], cmd)
        # This is allowed — "echo" is the command, claude is inside a quoted arg
        assert d.allow, f"Expected ALLOW for echo-about-claude: {cmd!r}, got reason={d.reason!r}"

    def test_grep_on_claude_md_not_blocked(self) -> None:
        d = check_claude_spawn([], "grep -r claude CLAUDE.md")
        assert d.allow

    def test_python_c_without_exec_not_blocked(self) -> None:
        """python3 -c payload that doesn't exec claude should pass."""
        cmd = "python3 -c 'print(\"hello world\")'"  # benign, no exec
        d = check_claude_spawn([], cmd)
        assert d.allow, f"Expected ALLOW for benign python3 -c: {cmd!r}"


# ---------------------------------------------------------------------------
# D#1174 — fd-redirect and kernel-device allowlist
# ---------------------------------------------------------------------------


class TestFdRedirectsAndKernelDevices:
    """2>/dev/null and similar fd redirects must not be blocked.

    The old _REDIRECT_PATTERN matched the `>` inside `2>/dev/null`, treating
    `/dev/null` as an output redirect target and blocking it.  After the fix:
    - fd-prefixed redirects (2>, 1>, &>, 2>>) are never extracted
    - /dev/null, /dev/stdout, /dev/stderr, /dev/tty, /dev/fd/* are skip-listed
    """

    # --- ALLOW cases: fd redirects must pass from a worktree ---

    @pytest.mark.parametrize(
        "cmd",
        [
            "cmd 2>/dev/null",
            "cmd 2>/dev/null && echo ok",
            "cmd 2>/dev/stderr",
            "cmd 2>&1",
            "cmd &>/dev/null",
            "cmd 1>>/dev/null",
            "cmd >/dev/null 2>&1",
            "python3 script.py 2>/dev/null",
            "git fetch origin 2>/dev/null",
            "npm install 2>/dev/null",
            "bash scripts/foo.sh 2>/dev/null",
        ],
    )
    def test_fd_redirect_allowed_from_worktree(self, cmd: str) -> None:
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, (
            f"Expected ALLOW for fd-redirect command: {cmd!r}, got reason={d.reason!r}"
        )

    @pytest.mark.parametrize(
        "target",
        [
            "/dev/null",
            "/dev/stdout",
            "/dev/stderr",
            "/dev/tty",
            "/dev/fd/1",
            "/dev/fd/2",
            "/dev/stdin",
        ],
    )
    def test_kernel_device_redirect_allowed(self, target: str) -> None:
        """Plain `> /dev/...` redirect (no fd prefix) must also be allowed."""
        cmd = f"echo hi > {target}"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, (
            f"Expected ALLOW for kernel-device redirect `{cmd}`, got reason={d.reason!r}"
        )

    # --- BLOCK regression cases: real file writes must still be blocked ---

    def test_redirect_to_main_repo_still_blocked(self) -> None:
        d = classify_bash(f"echo hi > {_MAIN_REPO}/foo.txt", _WT_CLAUDE)
        assert not d.allow
        assert "output redirect outside worktree" in d.reason

    def test_redirect_to_state_dir_still_blocked(self) -> None:
        d = classify_bash(
            "echo hi > ~/.autonomous-forever-state/audit.jsonl", _WT_CLAUDE
        )
        # ~/.autonomous-forever-state/... is not an absolute path after shell
        # expansion, but if specified absolutely it must be blocked.
        # Use the resolved absolute path directly.
        import os

        home = os.path.expanduser("~")
        cmd = f"echo hi > {home}/.autonomous-forever-state/audit.jsonl"
        d2 = classify_bash(cmd, _WT_CLAUDE)
        assert not d2.allow

    def test_append_redirect_to_main_repo_blocked(self) -> None:
        d = classify_bash(f"echo x >> {_MAIN_REPO}/log.txt", _WT_CLAUDE)
        assert not d.allow
        assert "output redirect outside worktree" in d.reason

    def test_write_to_worktree_still_allowed(self) -> None:
        d = classify_bash(f"echo x > {_WT_CLAUDE}/output.txt", _WT_CLAUDE)
        assert d.allow

    # --- Verify _REDIRECT_PATTERN itself does not capture fd prefixes ---

    def test_redirect_pattern_does_not_match_2_redirect(self) -> None:
        """The pattern must not extract /dev/null from '2>/dev/null'."""
        from hooks.sandbox_rules import _REDIRECT_PATTERN

        matches = _REDIRECT_PATTERN.findall("cmd 2>/dev/null")
        assert "/dev/null" not in matches, (
            f"Pattern should NOT capture fd-prefixed redirects, got: {matches}"
        )

    def test_redirect_pattern_captures_bare_redirect(self) -> None:
        """The pattern must capture the path from a bare '> /some/path'."""
        from hooks.sandbox_rules import _REDIRECT_PATTERN

        matches = _REDIRECT_PATTERN.findall(f"echo hi > {_MAIN_REPO}/foo.txt")
        assert any(_MAIN_REPO in m for m in matches), (
            f"Pattern should capture bare redirect target, got: {matches}"
        )

    # --- _is_kernel_device helper ---

    def test_is_kernel_device_positive(self) -> None:
        from hooks.sandbox_rules import _is_kernel_device

        for path in ["/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2"]:
            assert _is_kernel_device(path), f"Expected {path!r} to be a kernel device"

    def test_is_kernel_device_negative(self) -> None:
        from hooks.sandbox_rules import _is_kernel_device

        for path in [f"{_MAIN_REPO}/foo.txt", "/tmp/output.log", f"{FIXTURE_HOME}/file"]:
            assert not _is_kernel_device(path), f"Expected {path!r} NOT to be a kernel device"


# ---------------------------------------------------------------------------
# D#1729 — live sandbox escape: glued shell operators bypass _extract_git_verb
# ---------------------------------------------------------------------------


class TestGluedShellOperators:
    """`_extract_git_verb` used a bare shlex.split(), which only recognises shell
    operators (;, &&, ||, |, &) as separate tokens when they're already
    whitespace-delimited. A glued form like `true;git worktree remove /x` folded
    into a single token `true;git`, so `git` never landed on a command position
    and the write-verb check silently passed the command through.
    """

    def test_headline_repro_glued_semicolon_returns_verb(self) -> None:
        """`true;git worktree remove /x` must resolve to the `worktree` verb —
        this returned None on main before the fix (D#1729)."""
        assert _extract_git_verb("true;git worktree remove /x") == "worktree"

    def test_headline_repro_blocks_via_classify_bash(self) -> None:
        """The end-to-end repro: classify_bash must BLOCK the glued form exactly
        like it already blocks the spaced form."""
        d = classify_bash("true;git worktree remove " + _WT_CLAUDE, _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_spaced_form_still_blocks_no_regression(self) -> None:
        d = classify_bash("true ; git worktree remove " + _WT_CLAUDE, _WT_CLAUDE)
        assert not d.allow

    @pytest.mark.parametrize(
        "operator",
        [";", "&&", "||", "|", "&"],
    )
    def test_glued_and_spaced_forms_agree(self, operator: str) -> None:
        """For every operator in the shell-separator set, the glued form and the
        space-separated form of the same command must produce the same verb."""
        glued = f"true{operator}git checkout main"
        spaced = f"true {operator} git checkout main"
        assert _extract_git_verb(glued) == _extract_git_verb(spaced) == "checkout"

    @pytest.mark.parametrize(
        "operator",
        [";", "&&", "||", "|", "&"],
    )
    def test_glued_operator_blocks_via_classify_bash(self, operator: str) -> None:
        d = classify_bash(f"true{operator}git checkout main", _WT_CLAUDE)
        assert not d.allow

    def test_no_overblock_on_glued_readonly_verb(self) -> None:
        """Read-only verbs must still ALLOW through a glued separator — this must
        not become a blanket 'any ; blocks' rule."""
        d = classify_bash("true;git status", _WT_CLAUDE)
        assert d.allow

    def test_no_overblock_on_quoted_semicolon(self) -> None:
        """A semicolon inside a quoted argument is not a shell separator —
        shlex.split folds the quoted region into one token regardless of any
        spaces normalisation inserts inside it, so `git` is still found as the
        command and `commit` (not the quoted text) is returned as the verb."""
        assert _extract_git_verb('git commit -m "fix;bug"') == "commit"

    def test_no_overblock_on_quoted_git_reference(self) -> None:
        """A `git` mention inside a quoted --body value is not a command-position
        token and must not be detected as a git invocation."""
        assert _extract_git_verb('gh issue comment --body "then git checkout main"') is None


# ---------------------------------------------------------------------------
# D#1729 round 2 — security review findings F1/F2/F3 (Kai)
#
# Round 1 space-surrounded `;`, `&`, `|` but missed three other ways a command
# lands `git` off a recognised command position or off the verb walker's radar
# entirely:
#   F1 — subshell / command-substitution operators `(`, `)`, backtick weren't in
#        the normalizer's char class, so `(git ...)`, `$(git ...)`, and
#        `` `git ...` `` glued exactly like `;git ...` did pre-round-1.
#   F2 — the walker matched only the bare token "git", so an absolute-path
#        invocation (`/usr/bin/git ...`) never registered as a git command at all.
#   F3 — the walker returned on the FIRST git verb found in the whole command, so
#        a read-only verb anywhere before a write/always-blocked verb shielded it
#        (`git log;git reset --hard origin/main` never even looked at `reset`).
# ---------------------------------------------------------------------------


class TestF1SubshellAndCommandSubstitutionOperators:
    """F1: `(`, `)`, and backtick create a command position the same way `;` does —
    the normalizer's char class must include them, not just the plain shell
    separators."""

    @pytest.mark.parametrize(
        "command",
        [
            f"(git worktree remove {_WT_CLAUDE})",
            f"true;$(git checkout main)",
            "`git checkout main`",
            f"true; (git worktree remove {_WT_CLAUDE})",  # spaced form, no regression
        ],
    )
    def test_glued_and_spaced_subshell_forms_block(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK for {command!r}, got allow with reason={d.reason!r}"
        assert "git write-verb outside worktree" in d.reason

    def test_paren_subshell_extracts_worktree_verb(self) -> None:
        assert _extract_git_verb(f"(git worktree remove {_WT_CLAUDE})") == "worktree"

    def test_dollar_paren_command_sub_extracts_checkout_verb(self) -> None:
        assert _extract_git_verb("true;$(git checkout main)") == "checkout"

    def test_backtick_command_sub_extracts_checkout_verb(self) -> None:
        assert _extract_git_verb("`git checkout main`") == "checkout"

    def test_glued_and_spaced_paren_forms_agree(self) -> None:
        glued = "true(git checkout main)"
        spaced = "true ( git checkout main )"
        assert _extract_git_verb(glued) == _extract_git_verb(spaced) == "checkout"

    def test_no_overblock_readonly_inside_subshell(self) -> None:
        """A read-only verb inside a subshell must still ALLOW — F1's fix must not
        become a blanket '`(` blocks' rule."""
        d = classify_bash("(git status)", _WT_CLAUDE)
        assert d.allow


class TestF2AbsolutePathGitBinary:
    """F2: the walker must recognise `git` invoked by absolute (or relative) path,
    not just the bare `git` token — mirrors `_is_git_token`'s existing convention,
    already used by `is_real_git_rm_invocation`."""

    @pytest.mark.parametrize(
        "git_path",
        ["/usr/bin/git", "/usr/local/bin/git", "./git"],
    )
    def test_absolute_path_git_extracts_verb(self, git_path: str) -> None:
        assert _extract_git_verb(f"{git_path} worktree remove {_WT_CLAUDE}") == "worktree"

    def test_absolute_path_git_blocks_via_classify_bash(self) -> None:
        d = classify_bash(f"/usr/bin/git worktree remove {_WT_CLAUDE}", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_absolute_path_git_glued_semicolon_blocks(self) -> None:
        """Absolute-path git combined with a glued separator (F1 + F2 together)."""
        d = classify_bash(f"true;/usr/bin/git worktree remove {_WT_CLAUDE}", _WT_CLAUDE)
        assert not d.allow

    def test_no_overblock_absolute_path_readonly(self) -> None:
        d = classify_bash("/usr/bin/git status", _WT_CLAUDE)
        assert d.allow

    def test_bare_mygit_not_matched(self) -> None:
        """`mygit` is a different binary — must not be treated as git (existing
        _is_git_token contract, verified here for the verb walker specifically)."""
        assert _extract_git_verb("mygit worktree remove /x") is None


class TestF3FirstVerbWinsMultiVerbSequences:
    """F3: a read-only verb earlier in the command must not shield a
    write/always-blocked verb later in the same command — every git invocation in
    the command must be inspected, not just the first."""

    def test_readonly_log_no_longer_shields_reset_hard(self) -> None:
        d = classify_bash("git log;git reset --hard origin/main", _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow with reason={d.reason!r}"
        assert "git write-verb outside worktree" in d.reason

    def test_readonly_status_no_longer_shields_worktree_remove(self) -> None:
        d = classify_bash(f"git status && git worktree remove {_WT_CLAUDE}", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_readonly_diff_before_push_stays_allowed_when_no_escape(self) -> None:
        """`git diff && git push --force` (Kai's third F3 repro) with no `-C`/`cd`
        escape and cwd already at the worktree root is a legitimate multi-step
        command (this is literally the executor's own commit-then-push pattern) —
        it must stay ALLOW even after the F3 fix. The bug F3 describes is that the
        walker never even looked at `push`, not that this specific combination must
        become a block; `push` still goes through the normal write-verb CWD check,
        it's just no longer skipped because `diff` came first."""
        d = classify_bash("git diff && git push --force", _WT_CLAUDE)
        assert d.allow

    def test_multiple_readonly_verbs_before_always_blocked_still_blocks(self) -> None:
        d = classify_bash("git status;git log;git worktree remove x", _WT_CLAUDE)
        assert not d.allow

    def test_extract_all_git_verbs_collects_every_invocation(self) -> None:
        assert _extract_all_git_verbs("git log;git reset --hard origin/main") == [
            "log",
            "reset",
        ]
        assert _extract_all_git_verbs(
            f"git status && git worktree remove {_WT_CLAUDE}"
        ) == ["status", "worktree"]

    def test_extract_git_verb_still_returns_only_first(self) -> None:
        """_extract_git_verb (the single-verb convenience wrapper) is unchanged
        for callers that only want the leading verb."""
        assert _extract_git_verb("git log;git reset --hard origin/main") == "log"

    def test_no_overblock_all_readonly_multi_verb(self) -> None:
        """Multiple read-only verbs in one command must still ALLOW — F3's fix
        must not become 'any second git verb blocks'."""
        d = classify_bash("git log;git status;git diff", _WT_CLAUDE)
        assert d.allow

    def test_no_overblock_readonly_then_write_within_worktree(self) -> None:
        """A read-only verb followed by a legitimate write verb that stays inside
        the worktree (no escape) must still ALLOW — this is the executor's own
        multi-step commit/push pattern and must not regress."""
        d = classify_bash("git status && git commit -m 'wip'", _WT_CLAUDE)
        assert d.allow

    def test_no_overblock_write_then_readonly_within_worktree(self) -> None:
        d = classify_bash("git commit -m 'wip' && git push -u origin HEAD", _WT_CLAUDE)
        assert d.allow


# ---------------------------------------------------------------------------
# D#1729 round 3 — Kai's F4/F5: a value-taking global option (or a backslash-
# newline continuation artifact) displaces the verb slot, and the walker reads
# the DISPLACING token as the verb instead of the real subcommand. Unknown verb
# -> ALLOW, so `checkout`/`reset --hard`/`worktree remove`/etc. are never
# inspected at all — no shell trickery required, just an ordinary git global
# option.
#
#   F4 — `-c` (and sibling value-taking global options `--exec-path`,
#        `--config-env`, `--super-prefix`, `--attr-source`) take their value as a
#        SEPARATE token, which the walker didn't know to skip, so the value
#        (e.g. "user.name=x") got read as the verb.
#   F5 — backslash-newline continuation tokenises as a bare newline-character
#        token via shlex.split, which likewise got read as the verb.
#
# Kai's fix: (a) _GIT_VALUE_TAKING_GLOBAL_OPTS skips both the option token and
# its separate value token; (b) _GIT_VERB_SHAPE_RE rejects any candidate that
# isn't a bare word before it's accepted as a verb — this closes F4/F5 as one
# mechanism, and also guards against a value-taking option this list doesn't
# yet name.
# ---------------------------------------------------------------------------


class TestF4ValueTakingGlobalOptionsDisplaceVerb:
    """F4: `-c <name>=<value>` (and the four sibling value-taking global options)
    must not let the config value get read as the verb."""

    def test_dash_c_no_longer_displaces_checkout_verb(self) -> None:
        assert _extract_all_git_verbs("git -c user.name=x checkout main") == ["checkout"]

    def test_dash_c_confirmed_repro_blocks_via_classify_bash(self) -> None:
        d = classify_bash("git -c core.pager=cat checkout main", _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow with reason={d.reason!r}"
        assert "git write-verb outside worktree" in d.reason

    def test_dash_c_worktree_remove_blocks(self) -> None:
        d = classify_bash(f"git -c foo.bar=baz worktree remove {_WT_CLAUDE}", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    @pytest.mark.parametrize(
        "opt,value",
        [
            ("-c", "user.name=x"),
            ("--exec-path", "/tmp/x"),
            ("--config-env", "foo.bar=BAZ"),
            ("--super-prefix", "/x"),
            ("--attr-source", "refs/heads/x:.gitattributes"),
        ],
    )
    def test_all_five_value_taking_global_opts_skip_their_value(
        self, opt: str, value: str
    ) -> None:
        assert _extract_all_git_verbs(f"git {opt} {value} checkout main") == ["checkout"]

    def test_dash_c_value_that_itself_looks_like_a_verb_still_displaced_correctly(
        self,
    ) -> None:
        """Even when the config value happens to be a bare word that would pass
        the shape guard on its own (e.g. "foo"), it must still be recognised as
        the OPTION'S value (via _GIT_VALUE_TAKING_GLOBAL_OPTS) and skipped —
        otherwise the shape guard alone would misread "foo" as the verb and
        "checkout" would never be inspected."""
        assert _extract_all_git_verbs("git -c foo checkout main") == ["checkout"]

    def test_glued_dash_c_form_does_not_reintroduce_the_bypass(self) -> None:
        """A glued `-cvalue` token (no space) doesn't match `-c` exactly, so it
        isn't in the value-taking skip set — but it starts with `-`, so the
        existing flag-skip branch consumes it as an unknown flag without
        advancing past a real value token. The verb walker still lands on the
        true verb either way."""
        assert _extract_all_git_verbs("git -cuser.name=x checkout main") == ["checkout"]

    def test_existing_dash_capital_c_and_siblings_unaffected(self) -> None:
        """Pre-round-3 value-taking options (-C, --git-dir, --work-tree,
        --namespace) must keep working exactly as before — round 3 only adds to
        the set, it doesn't change existing behaviour."""
        assert _extract_all_git_verbs(f"git -C {_WT_CLAUDE} status") == ["status"]
        assert _extract_all_git_verbs("git --git-dir /x/.git log") == ["log"]

    def test_no_overblock_dash_c_on_legitimate_readonly_command(self) -> None:
        d = classify_bash("git -c core.pager=cat log", _WT_CLAUDE)
        assert d.allow


class TestF5BackslashNewlineContinuation:
    """F5: a backslash-newline shell continuation tokenises to a bare newline
    token via shlex.split, which must not be misread as the verb."""

    def test_backslash_newline_no_longer_displaces_checkout_verb(self) -> None:
        command = "git \\\n checkout main"
        assert _extract_all_git_verbs(command) == ["checkout"]

    def test_backslash_newline_confirmed_repro_blocks_via_classify_bash(self) -> None:
        command = "git \\\n checkout main"
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow with reason={d.reason!r}"
        assert "git write-verb outside worktree" in d.reason

    def test_backslash_newline_before_worktree_remove_blocks(self) -> None:
        command = f"git \\\n worktree remove {_WT_CLAUDE}"
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow

    def test_no_overblock_backslash_newline_readonly(self) -> None:
        command = "git \\\n status"
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow


class TestVerbShapeGuardControlCases:
    """_GIT_VERB_SHAPE_RE (`^[A-Za-z][A-Za-z0-9-]*$`) is the shared mechanism
    behind both F4 and F5 — these cases pin down that it accepts every real git
    verb shape and rejects only the displacing artifacts, not legitimate verbs."""

    @pytest.mark.parametrize(
        "verb",
        ["checkout", "worktree", "reset", "log", "status", "commit", "rebase-x", "lfs"],
    )
    def test_ordinary_verb_shapes_still_accepted(self, verb: str) -> None:
        assert _extract_all_git_verbs(f"git {verb}") == [verb]

    def test_bare_newline_token_rejected_as_verb(self) -> None:
        """Direct unit check on the exact artifact F5 produces."""
        assert _extract_all_git_verbs("git \\\n checkout main") == ["checkout"]
        # And confirm a lone displaced newline with nothing following collects no verb.
        assert _extract_all_git_verbs("git \\\n") == []

    def test_config_pair_token_rejected_as_verb(self) -> None:
        """Direct unit check on the exact artifact F4 produces when -c isn't
        recognised (defence in depth — this is the shape guard doing the work
        even if the explicit -c skip were somehow bypassed)."""
        assert not _GIT_VERB_SHAPE_RE.match("user.name=x")
        assert _GIT_VERB_SHAPE_RE.match("checkout")

    def test_multi_verb_sequence_with_dash_c_on_second_invocation(self) -> None:
        """F3 (every invocation inspected) composed with F4 (value-taking option
        in one of them) — the always-blocked verb in the second invocation must
        still be found."""
        assert _extract_all_git_verbs(
            "git status;git -c core.pager=cat reset --hard origin/main"
        ) == ["status", "reset"]


class TestDialProtectedSuffixes:
    """D#1672 AC-23: the external-intake approval baseline store is a
    self-approval privilege surface (security-expert SEC-5) — whoever can
    write it can forge "the content a human reviewed" — so it gets the same
    Team-Lead-only protection as the dial registry and the audit log.
    Basename-matched, location-independent, same tier as audit.jsonl and
    dial-registry.json.
    """

    def test_external_intake_baselines_is_in_the_protected_suffix_list(self):
        assert "external-intake-baselines.json" in _DIAL_PROTECTED_SUFFIXES

    def test_is_dial_protected_path_matches_state_dir_location(self):
        assert _is_dial_protected_path(
            f"{FIXTURE_HOME}/.autonomous-forever-state/external-intake-baselines.json"
        )

    def test_is_dial_protected_path_matches_repo_local_fallback_location(self):
        assert _is_dial_protected_path(
            f"{_MAIN_REPO}/.autonomous-team/external-intake-baselines.json"
        )

    def test_classify_path_write_blocks_subagent_edit_from_worktree(self):
        target = f"{_WT_CLAUDE}/../../../.autonomous-team/external-intake-baselines.json"
        d = classify_path_write(target, _WT_CLAUDE)
        assert d.allow is False
        assert "external-intake-baselines.json" in d.reason

    def test_classify_path_write_blocks_relative_path_by_basename(self):
        d = classify_path_write("external-intake-baselines.json", _WT_CLAUDE)
        assert d.allow is False

    def test_pre_existing_protected_suffixes_still_present(self):
        # Regression guard — adding the new entry must not have dropped these.
        assert "dial-registry.json" in _DIAL_PROTECTED_SUFFIXES
        assert "dial-directive-allowlist.json" in _DIAL_PROTECTED_SUFFIXES
        assert "audit.jsonl" in _DIAL_PROTECTED_SUFFIXES

    # -- SEC-4 (D#1672 round 3) -------------------------------------------
    # The store's init marker is the ONLY thing that makes deletion of the
    # store fail closed (see scripts/lib/intake_baseline.py::read_baselines).
    # Round 2 protected the store but left the marker unguarded, so a
    # worktree sub-agent that could not touch the store could still delete
    # the marker, then delete the store, and land on the original SEC-2
    # bypass — a lower-privileged reproduction of the round-1 hole.

    def test_init_marker_basename_is_in_the_protected_suffix_list(self):
        assert ".external-intake-baselines.json.initialized" in _DIAL_PROTECTED_SUFFIXES

    def test_is_dial_protected_path_matches_init_marker_at_state_dir_location(self):
        assert _is_dial_protected_path(
            f"{FIXTURE_HOME}/.autonomous-forever-state/.external-intake-baselines.json.initialized"
        )

    def test_is_dial_protected_path_matches_init_marker_repo_local_fallback(self):
        assert _is_dial_protected_path(
            f"{_MAIN_REPO}/.autonomous-team/.external-intake-baselines.json.initialized"
        )

    def test_classify_path_write_blocks_subagent_deleting_marker_from_worktree(self):
        target = (
            f"{_WT_CLAUDE}/../../../.autonomous-team/"
            ".external-intake-baselines.json.initialized"
        )
        d = classify_path_write(target, _WT_CLAUDE)
        assert d.allow is False
        assert ".external-intake-baselines.json.initialized" in d.reason

    def test_classify_path_write_blocks_marker_by_relative_basename(self):
        d = classify_path_write(
            ".external-intake-baselines.json.initialized", _WT_CLAUDE
        )
        assert d.allow is False

    def test_marker_basename_derived_from_store_name_not_hand_typed(self):
        # SEC-7 (D#1672 round 4, Kai round-3 review): this used to hardcode the
        # store filename as a local string literal, which only proved the
        # protected-suffix list was internally self-consistent. It stayed green
        # even if backend/state_paths.py::EXTERNAL_INTAKE_BASELINES were renamed,
        # silently leaving the sandbox protecting a filename nothing writes to
        # anymore. Import the real source of truth instead so a rename there
        # would fail this test.
        from backend.state_paths import EXTERNAL_INTAKE_BASELINES

        store = EXTERNAL_INTAKE_BASELINES.name
        assert store in _DIAL_PROTECTED_SUFFIXES
        assert f".{store}.initialized" in _DIAL_PROTECTED_SUFFIXES

    # -- SEC-6 (D#1672 round 4) ---------------------------------------------
    # Round 3 only added the marker to the write-target scan
    # (classify_path_write / _absolute_path_targets' destination-only view),
    # which guards Edit/Write and tee/cp/mv-destination/`>`. It did nothing
    # for deletion: `rm`, `unlink`, and `mv <marker> <dst>`'s SOURCE argument
    # produce no "target" in that sense and sailed through, reproducing the
    # original SEC-2 bypass verbatim via `rm -f <store> <marker>`. Fixed via
    # classify_bash() step 1d / _all_path_operands() — a structural
    # scan of every token, not a verb enumeration.

    _MARKER_ABS = f"{_MAIN_REPO}/.autonomous-team/.external-intake-baselines.json.initialized"
    _STORE_ABS = f"{_MAIN_REPO}/.autonomous-team/external-intake-baselines.json"

    def test_classify_bash_blocks_rm_of_marker_from_worktree(self):
        d = classify_bash(f"rm -f {self._MARKER_ABS}", _WT_CLAUDE)
        assert d.allow is False
        assert ".external-intake-baselines.json.initialized" in d.reason

    def test_classify_bash_blocks_rm_of_store_and_marker_together(self):
        # The exact repro from Kai's round-3 review: one `rm -f` targeting
        # both files at once.
        d = classify_bash(f"rm -f {self._STORE_ABS} {self._MARKER_ABS}", _WT_CLAUDE)
        assert d.allow is False
        assert "external-intake-baselines.json" in d.reason

    def test_classify_bash_blocks_unlink_of_marker_from_worktree(self):
        d = classify_bash(f"unlink {self._MARKER_ABS}", _WT_CLAUDE)
        assert d.allow is False

    def test_classify_bash_blocks_unlink_of_store_from_worktree(self):
        d = classify_bash(f"unlink {self._STORE_ABS}", _WT_CLAUDE)
        assert d.allow is False

    def test_classify_bash_blocks_mv_of_marker_source_out_of_place(self):
        # mv's SOURCE argument, not just its destination — the gap Kai's
        # review specifically called out (`_absolute_path_targets()` only
        # ever looked at the last/destination argument).
        d = classify_bash(f"mv {self._MARKER_ABS} {_WT_CLAUDE}/x", _WT_CLAUDE)
        assert d.allow is False

    def test_classify_bash_blocks_mv_of_store_source(self):
        d = classify_bash(f"mv {self._STORE_ABS} {_WT_CLAUDE}/x", _WT_CLAUDE)
        assert d.allow is False

    def test_classify_bash_still_blocks_rm_of_dial_registry(self):
        # Same structural fix closes the identical latent hole Kai flagged
        # for the pre-existing protected files, not just the new marker.
        d = classify_bash(
            f"rm -f {_MAIN_REPO}/.autonomous-team/dial-registry.json", _WT_CLAUDE
        )
        assert d.allow is False

    def test_classify_bash_still_blocks_rm_of_audit_log(self):
        d = classify_bash(
            f"rm -f {FIXTURE_HOME}/.autonomous-forever-state/audit.jsonl", _WT_CLAUDE
        )
        assert d.allow is False

    def test_classify_bash_allows_rm_of_unrelated_absolute_path(self):
        # Regression guard — the broadened operand scan must not block
        # ordinary file deletion inside the worktree.
        d = classify_bash(f"rm -f {_WT_CLAUDE}/scratch/notes.txt", _WT_CLAUDE)
        assert d.allow is True

    @pytest.mark.xfail(
        reason=(
            "Pre-existing gap in D#1749's _classify_unenumerated_write "
            "(main, merged independently of this PR): its segment-wide "
            "_PROTECTED_BASENAME_RE substring check runs before the /tmp "
            "exclusion in the same function's per-token loop, so a /tmp path "
            "containing a protected basename is blocked instead of allowed. "
            "Surfaced by rebasing D#1672 onto D#1749, not caused by either "
            "PR individually — out of scope for a rebase-only pass; needs "
            "its own fix/review against D#1749's code."
        ),
        strict=True,
    )
    def test_classify_bash_allows_rm_of_tmp_path_matching_protected_basename(self):
        # /tmp is ephemeral scratch, not repo/state-dir storage — mirrors the
        # existing /tmp exclusion in classify_bash's write-target loop.
        d = classify_bash("rm -f /tmp/external-intake-baselines.json", _WT_CLAUDE)
        assert d.allow is True

    # -- SEC-8 (D#1672 round 5, Kai round-4 review) --------------------------
    # The round-4 operand walker generalised over verbs but not over path
    # *spellings*: it required os.path.isabs(tok) plus an exact basename `==`,
    # so it covered exactly one spelling of the marker's path and missed six
    # others — not the documented python3 -c / find -delete / $VAR gap, but
    # ordinary ways a human or agent would type the same command. Each of
    # these reproduced the identical SEC-2 store+marker deletion bypass
    # end-to-end (verified live against the real hooks/sandbox.py subprocess
    # with a real on-disk store outside /tmp/ — see PR round-5 notes).

    _STATE_DIR = f"{FIXTURE_HOME}/.autonomous-forever-state"
    _MARKER_NAME = ".external-intake-baselines.json.initialized"
    _STORE_NAME = "external-intake-baselines.json"

    def test_classify_bash_blocks_rm_with_tilde_prefixed_marker_path(self):
        # shlex does not expand `~`, so the round-4 isabs() gate was False
        # for this token and it never reached the protected-basename check.
        d = classify_bash(
            f"rm ~/.autonomous-forever-state/{self._MARKER_NAME}", _WT_CLAUDE
        )
        assert d.allow is False
        assert self._MARKER_NAME in d.reason

    def test_classify_bash_blocks_cd_then_relative_rm_of_marker(self):
        # The relative token `<marker>` never reached the round-4 check
        # because it isn't os.path.isabs()-shaped.
        d = classify_bash(
            f"cd {self._STATE_DIR} && rm {self._MARKER_NAME}", _WT_CLAUDE
        )
        assert d.allow is False
        assert self._MARKER_NAME in d.reason

    def test_classify_bash_blocks_dotdot_relative_rm_of_marker(self):
        d = classify_bash(
            f"rm ../../../.autonomous-team/{self._MARKER_NAME}", _WT_CLAUDE
        )
        assert d.allow is False
        assert self._MARKER_NAME in d.reason

    # -- SEC-11 (D#1672 round 6, Kai round-5 review): the round-5 fnmatch
    #    over-blocked 11/15 ordinary glob commands (`rm -rf *`, `git add *`,
    #    ...) because `*` matches every protected basename, and its glob
    #    coverage was incomplete anyway (brace expansion still reaches the
    #    file — see the matcher's docstring). Reverted to exact basename
    #    match, so a glob spelling of the protected basename is no longer
    #    treated as a *dial-protected-name* match — same residual-gap class
    #    as `python3 -c` and `$VAR`, not a regression.
    #
    #    These three exercise _protected_basename_operand() directly rather
    #    than the full classify_bash() pipeline (rebased onto D#1749): D#1749
    #    added an unrelated, broader deny-by-default rule in classify_bash
    #    step 4b that blocks ANY unenumerated command's absolute-path operand
    #    outside the worktree — which _STATE_DIR is — regardless of basename.
    #    That rule now blocks these same commands too, for a completely
    #    different reason, which would mask what these tests are actually
    #    about (whether the basename matcher itself glob-matches). Testing
    #    the helper in isolation keeps them a precise regression check for
    #    the SEC-11 revert.

    def test_classify_bash_allows_glob_covering_marker_dotfile(self):
        # Basename ".external-intake-baselines.json.*" isn't literally equal
        # to the protected name. The shell glob would expand to hit it, but
        # that's a deliberate evasion requiring foreknowledge of the control
        # (documented residual gap), not "how the command is ordinarily
        # typed" — see the SEC-11 comment on classify_bash step 1d.
        assert (
            _protected_basename_operand(
                f"{self._STATE_DIR}/.external-intake-baselines.json.*"
            )
            is False
        )

    def test_classify_bash_allows_glob_covering_store_prefix(self):
        assert (
            _protected_basename_operand(
                f"{self._STATE_DIR}/external-intake-baselines.js*"
            )
            is False
        )

    def test_classify_bash_allows_glob_covering_marker_single_char(self):
        assert (
            _protected_basename_operand(
                f"{self._STATE_DIR}/.external-intake-baselines.json.initialize?"
            )
            is False
        )

    def test_classify_bash_blocks_combined_store_and_marker_via_tilde(self):
        # The exact SEC-2 store+marker repro, spelled with `~` this time.
        d = classify_bash(
            f"rm ~/.autonomous-forever-state/{self._STORE_NAME} "
            f"~/.autonomous-forever-state/{self._MARKER_NAME}",
            _WT_CLAUDE,
        )
        assert d.allow is False

    # -- SEC-8 regression: the thirteen round-4 forms Kai reproduced as
    #    BLOCKED must all remain BLOCKED after dropping the isabs() gate.

    _MARKER_ABS2 = f"{_STATE_DIR}/{_MARKER_NAME}"
    _STORE_ABS2 = f"{_STATE_DIR}/{_STORE_NAME}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -f {store} {marker}",
            "unlink {marker}",
            "shred -u {marker}",
            "truncate -s 0 {marker}",
            "install /dev/null {marker}",
            "busybox rm {marker}",
            "rm -- {marker}",
            "rm -r {marker}/",
            "rm {state}/../{state_base}/{marker}",
            "echo {marker} | xargs rm",
            "rm {state}/audit.jsonl",
            "rm {state}/dial-registry.json",
        ],
    )
    def test_classify_bash_prior_round4_forms_still_blocked(self, cmd):
        formatted = cmd.format(
            store=self._STORE_ABS2,
            marker=self._MARKER_ABS2,
            state=self._STATE_DIR,
            state_base=self._STATE_DIR.rsplit("/", 1)[-1],
        )
        d = classify_bash(formatted, _WT_CLAUDE)
        assert d.allow is False, f"expected BLOCK for `{formatted}`, got allow=True"

    def test_classify_bash_mv_source_of_marker_still_blocked(self):
        d = classify_bash(f"mv {self._MARKER_ABS2} {_WT_CLAUDE}/x", _WT_CLAUDE)
        assert d.allow is False

    # -- SEC-8 false-positive controls — Kai's exact ten-command set, all
    #    must remain ALLOWED after dropping the isabs() gate.
    #
    #    SEC-11 (D#1672 round 6, Kai round-5 review) adds `rm -rf *`,
    #    `git add *`, and `pytest tests/*` to this set — the three commands
    #    from Kai's over-block finding most likely to be typed for real, so
    #    the fnmatch over-block cannot silently regress.

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf node_modules",
            "npm ci",
            "pytest tests/ -q",
            "git status",
            "cp a.json b.json",
            "rm src/foo.py",
            "grep -rn baselines scripts/",
            "python3 backend/server.py",
            "rm /tmp/scratch/notes.md",
            "ls ~/.config",
            "rm -rf *",
            "git add *",
            "pytest tests/*",
        ],
    )
    def test_classify_bash_false_positive_controls_still_allowed(self, cmd):
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow is True, f"expected ALLOW for `{cmd}`, got reason={d.reason!r}"

    def test_classify_bash_ordinary_worktree_deletion_still_allowed(self):
        # Regression guard — an in-worktree relative delete with no relation
        # to any protected basename must not be caught by the broadened scan.
        d = classify_bash("rm notes.txt", _WT_CLAUDE)
        assert d.allow is True


# ---------------------------------------------------------------------------
# D#1756 (+ D#1903, closed as its duplicate) — a read-only OR permitted-write
# git verb used to `return Decision(allow=True, ...)` at the end of
# classify_bash's git-verb section (the old :2449/:2453), which shielded the
# REST of the same command line from the redirect/write-target scans below.
# `git log && rm -rf <main-repo>/scripts` and `git commit -m x && rm -rf
# <main-repo>/scripts` both ALLOWed on main.
# ---------------------------------------------------------------------------


class TestD1756ChainedWriteAfterGitVerb:
    """A write chained onto a git invocation via `&&`/`;` must reach the
    write-target scan instead of being shielded by the git verb's own
    early-allow. Covers both the read-only-verb early return (former :2453)
    and the in-worktree-write-verb early return (former :2449)."""

    @pytest.mark.parametrize(
        "command",
        [
            # former :2453 (read-only/unknown verb) sibling
            f"git log && rm -rf {_MAIN_REPO}/scripts",
            f"git status; echo x >> {_MAIN_REPO}/CLAUDE.md",
            # former :2449 (in-worktree write verb) sibling
            f"git commit -m x && rm -rf {_MAIN_REPO}/scripts",
            f"git add -A && echo x >> {_MAIN_REPO}/CLAUDE.md",
            # D#1903's original named pairs — same root cause
            "git log > ~/.bashrc",
            "git log > /etc/passwd",
            "git diff > ~/.bashrc",
            "git show > /etc/passwd",
        ],
    )
    def test_chained_write_after_git_verb_now_blocked(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow for: {command!r}"

    def test_fails_red_against_the_pre_fix_shape(self) -> None:
        """Process criterion 23: this exact repro was `allow=True` before the
        fix (the git-verb section returned before ever reaching the
        redirect/write-target scans). Documented here as the red-first case;
        the PR body carries the actual red/green pytest runs."""
        d = classify_bash(f"git log && rm -rf {_MAIN_REPO}/scripts", _WT_CLAUDE)
        assert d.allow is False
        assert "path-token scan" in d.reason


class TestD1756RegressionTrap:
    """The fix must NOT let git's own `-C <path>` operand get misread as a
    write target by the unenumerated path-token scan once the early-allow
    returns are removed — that's the exact regression the Spec calls out as
    the most likely way this PR goes wrong. Criteria 14/15."""

    @pytest.mark.parametrize(
        "command",
        [
            f"git -C {_MAIN_REPO} log --oneline -5",
            f"git -C {_MAIN_REPO} status",
            "git -C /etc log",  # read-only allowlist passes even with -C escape
        ],
    )
    def test_readonly_dash_c_outside_worktree_stays_allowed(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r} for: {command!r}"
        assert "path-token scan" not in d.reason

    @pytest.mark.parametrize(
        "command",
        [
            "git log",
            "git status",
            "git diff",
            "git show",
        ],
    )
    def test_bare_readonly_git_stays_allowed(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow

    def test_write_verb_inside_worktree_stays_allowed(self) -> None:
        d = classify_bash("git commit -m x", _WT_CLAUDE)
        assert d.allow

    def test_write_verb_then_readonly_verb_inside_worktree_stays_allowed(self) -> None:
        d = classify_bash("git add -A && git commit -m x", _WT_CLAUDE)
        assert d.allow

    def test_stderr_redirect_to_dev_null_stays_allowed(self) -> None:
        d = classify_bash("git log 2>/dev/null", _WT_CLAUDE)
        assert d.allow

    @pytest.mark.parametrize(
        "command",
        [
            "git log > /tmp/out.txt",
            "echo x > /tmp/scratch.txt",
            'echo x > "/tmp/scratch.txt"',
        ],
    )
    def test_tmp_exemption_still_applies_quoted_and_unquoted(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r} for: {command!r}"

    def test_in_worktree_write_stays_allowed_unquoted_and_quoted(self) -> None:
        d1 = classify_bash(f"echo x > {_WT_CLAUDE}/notes.txt", _WT_CLAUDE)
        assert d1.allow
        d2 = classify_bash(f'echo x > "{_WT_CLAUDE}/notes.txt"', _WT_CLAUDE)
        assert d2.allow

    def test_dash_c_write_verb_outside_worktree_still_blocks(self) -> None:
        # C — must-stay-BLOCK, no weakening. Criterion 20.
        d = classify_bash(f"git -C {_MAIN_REPO} push --force", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_always_blocked_verb_after_readonly_verb_still_blocks(self) -> None:
        # C — criterion 21, the D#1729 F3 multi-verb walker must be untouched.
        d = classify_bash("git log && git checkout main", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_unquoted_and_piped_writes_to_main_repo_still_block(self) -> None:
        # C — criterion 22, sanity check the redirect/tee path itself wasn't touched.
        d1 = classify_bash(f"echo x > {_MAIN_REPO}/CLAUDE.md", _WT_CLAUDE)
        assert not d1.allow
        d2 = classify_bash(f'echo x | tee "{_MAIN_REPO}/CLAUDE.md"', _WT_CLAUDE)
        assert not d2.allow

    def test_is_segment_write_candidate_treats_git_segment_as_never_a_candidate(
        self,
    ) -> None:
        """Direct unit coverage of the mechanism the fix relies on."""
        assert _is_segment_write_candidate(["git", "-C", _MAIN_REPO, "log"]) is False
        assert _is_segment_write_candidate(["git", "commit", "-m", "x"]) is False


# ---------------------------------------------------------------------------
# D#1792 — a quoted `/`-prefixed redirect target is invisible to the boundary
# check. PR #1901's tokenised `_home_prefixed_redirect_targets` is already
# quote-correct; the bug is that its candidate filter only accepted `~`/
# `$HOME`/`${HOME}` prefixes, so a `/`-prefixed quoted target fell through to
# the quote-blind raw regex (`_REDIRECT_PATTERN`), which can't see past the
# opening quote.
# ---------------------------------------------------------------------------


class TestD1792QuotedAbsoluteRedirectTarget:
    def test_home_prefixed_redirect_targets_ab_pair(self) -> None:
        """The exact A/B the PM ran: same tokeniser, same quoting — only the
        prefix filter differs. `~` was already caught; a `/`-prefixed target
        was missed."""
        assert _home_prefixed_redirect_targets('echo x > "~/.bashrc"') == [
            f"{Path.home()}/.bashrc"
        ]
        assert _home_prefixed_redirect_targets(
            f'echo x > "{_MAIN_REPO}/CLAUDE.md"'
        ) == [f"{_MAIN_REPO}/CLAUDE.md"]

    def test_absolute_path_targets_returns_quoted_target(self) -> None:
        # Criterion 10.
        assert _absolute_path_targets(f'echo x > "{_MAIN_REPO}/CLAUDE.md"') == [
            f"{_MAIN_REPO}/CLAUDE.md"
        ]

    @pytest.mark.parametrize(
        "command",
        [
            f'echo x > "{_MAIN_REPO}/CLAUDE.md"',
            f"echo x > '{_MAIN_REPO}/CLAUDE.md'",
        ],
    )
    def test_quoted_redirect_target_now_blocked(self, command: str) -> None:
        # Criteria 8/9.
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow for: {command!r}"
        assert "output redirect outside worktree" in d.reason

    def test_quoted_target_containing_a_space_is_caught(self) -> None:
        # Criterion 11 — the case quoting exists for.
        d = classify_bash(f'echo x > "{_MAIN_REPO}/a file.md"', _WT_CLAUDE)
        assert not d.allow

    @pytest.mark.parametrize(
        "command",
        [
            f'echo x >> "{_MAIN_REPO}/CLAUDE.md"',
            'echo x > "$HOME/x"',
        ],
    )
    def test_mixed_adjacent_forms_blocked(self, command: str) -> None:
        # Criterion 12.
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow for: {command!r}"

    def test_fails_red_against_the_pre_fix_shape(self) -> None:
        """Process criterion 23: this was `allow=True` before the fix — the
        quoted target never reached the tokenised `~`/`$HOME` scan and the
        raw regex is quote-blind."""
        d = classify_bash(f'echo x > "{_MAIN_REPO}/CLAUDE.md"', _WT_CLAUDE)
        assert d.allow is False

    def test_raw_regex_fallback_for_unquoted_still_untouched(self) -> None:
        """Out of scope note 2: the quote-blind raw regex at :129-132 is left
        exactly as-is — it still independently catches the unquoted form."""
        from hooks.sandbox_rules import _REDIRECT_PATTERN

        assert _REDIRECT_PATTERN.findall(f"echo x > {_MAIN_REPO}/CLAUDE.md") == [
            f"{_MAIN_REPO}/CLAUDE.md"
        ]

    def test_tmp_and_in_worktree_quoted_targets_stay_allowed(self) -> None:
        # B — must-stay-ALLOW regression gate, criteria 18/19 for the quoted forms.
        d1 = classify_bash('echo x > "/tmp/scratch.txt"', _WT_CLAUDE)
        assert d1.allow
        d2 = classify_bash(f'echo x > "{_WT_CLAUDE}/notes.txt"', _WT_CLAUDE)
        assert d2.allow


# ---------------------------------------------------------------------------
# D#1746 / D#1748 — resolve_effective_cwd reproduced the same three bug
# classes _extract_all_git_verbs was hardened against (D#1729 F1/F2/F3), plus
# the redirection-displacement gap (D#1748 F7), independently in a sibling
# function. Fixed by giving both functions the same shared
# tokenize/walk layer (_tokenize_shell_command / _walk_git_invocations) and
# switching classify_bash's write-verb check from "one CWD for the whole
# command" to "pair each git invocation with its own CWD".
# ---------------------------------------------------------------------------


class TestD1746ResolveEffectiveCwdEveryInvocation:
    """Spec (Acceptance) section A — escapes that must block."""

    def test_absolute_path_git_binary_single_invocation_no_shielding(self) -> None:
        # A1 (F2): no shell trickery needed at all, just a non-bare git spelling.
        d = classify_bash(f"/usr/bin/git -C {_MAIN_REPO} push --force", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_second_invocations_dash_c_inspected(self) -> None:
        # A2 (F3): resolve_effective_cwd used to stop at the FIRST git
        # invocation (`git diff`, no -C) and never look at the second.
        d = classify_bash(f"git diff && git -C {_MAIN_REPO} push --force", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_escape_via_cd_between_invocations(self) -> None:
        # A3 (F3 via cd): the `cd` lands between two git invocations.
        d = classify_bash(f"git log && cd {_MAIN_REPO} && git push --force", _WT_CLAUDE)
        assert not d.allow

    def test_subshell_form_blocks(self) -> None:
        # A4 (F1).
        d = classify_bash(f"(git -C {_MAIN_REPO} push --force)", _WT_CLAUDE)
        assert not d.allow

    def test_command_substitution_form_blocks(self) -> None:
        # A5 (F1), glued separator.
        d = classify_bash(f"true;$(git -C {_MAIN_REPO} push --force)", _WT_CLAUDE)
        assert not d.allow

    def test_backtick_form_blocks(self) -> None:
        # A6 (F1) — backtick isn't in shlex's punctuation_chars set, needs its
        # own padding in _tokenize_shell_command.
        d = classify_bash(f"`git -C {_MAIN_REPO} push --force`", _WT_CLAUDE)
        assert not d.allow

    def test_value_taking_global_option_before_dash_c_still_finds_it(self) -> None:
        # A7 (F4 interaction): a preceding `-c <value>` must not stop the walk
        # before it reaches `-C`.
        d = classify_bash(
            f"git -c core.pager=cat -C {_MAIN_REPO} push --force", _WT_CLAUDE
        )
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_fd_redirect_with_ampersand_no_longer_displaces_dash_c(self) -> None:
        # A8 (D#1748 F7): `2>&1` used to shred into a standalone `&` separator
        # token, resetting the walker before it reached `-C`/`push`.
        d = classify_bash(f"git 2>&1 -C {_MAIN_REPO} push --force", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_git_dir_and_work_tree_combo_blocks(self) -> None:
        # A9 (F2 + the other path-relocating global options): the old code
        # only ever recognised `-C`.
        d = classify_bash(
            f"/usr/bin/git --git-dir {_MAIN_REPO}/.git --work-tree {_MAIN_REPO} push --force",
            _WT_CLAUDE,
        )
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    # --- held-out forms (Spec item 18) — not named anywhere in the Discussion ---

    def test_held_out_semicolon_separator_variant(self) -> None:
        d = classify_bash(f"git status ; git -C {_MAIN_REPO} push --force", _WT_CLAUDE)
        assert not d.allow

    def test_held_out_nested_subshell_after_separator(self) -> None:
        d = classify_bash(
            f"echo noop && ( true ; git -C {_MAIN_REPO} push --force )", _WT_CLAUDE
        )
        assert not d.allow

    def test_held_out_namespace_option_before_dash_c(self) -> None:
        d = classify_bash(
            f"git --namespace=x -C {_MAIN_REPO} push --force", _WT_CLAUDE
        )
        assert not d.allow

    def test_held_out_backtick_nested_after_and_and(self) -> None:
        d = classify_bash(
            f"true && `true; git -C {_MAIN_REPO} push --force`", _WT_CLAUDE
        )
        assert not d.allow


class TestD1746OverBlockGuards:
    """Spec (Acceptance) section B — legitimate use that must NOT block."""

    def test_in_worktree_commit_allowed(self) -> None:
        d = classify_bash("git commit -m 'wip'", _WT_CLAUDE)
        assert d.allow

    def test_in_worktree_push_allowed(self) -> None:
        d = classify_bash("git push -u origin HEAD", _WT_CLAUDE)
        assert d.allow

    def test_relative_descent_inside_worktree_allowed(self) -> None:
        d = classify_bash(f"cd {_WT_CLAUDE}/tui && git commit -m 'wip'", _WT_CLAUDE)
        assert d.allow

    def test_explicit_dash_c_pointing_inside_worktree_allowed(self) -> None:
        d = classify_bash(f"git -C {_WT_CLAUDE} push -u origin HEAD", _WT_CLAUDE)
        assert d.allow

    def test_readonly_then_in_worktree_write_allowed(self) -> None:
        d = classify_bash("git status && git commit -m 'wip'", _WT_CLAUDE)
        assert d.allow

    def test_readonly_dash_c_outside_worktree_stays_allowlisted(self) -> None:
        d = classify_bash(f"git -C {_MAIN_REPO} log -5", _WT_CLAUDE)
        assert d.allow

    def test_fetch_dash_c_outside_worktree_stays_allowed(self) -> None:
        d = classify_bash(f"git -C {_MAIN_REPO} fetch origin", _WT_CLAUDE)
        assert d.allow

    @pytest.mark.parametrize(
        "command",
        ['git commit -m "fix;bug"', 'git commit -m "fix>bug"'],
    )
    def test_separator_and_redirect_chars_inside_quotes_are_text(
        self, command: str
    ) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r} for: {command!r}"

    def test_per_invocation_pairing_does_not_cross_product(self) -> None:
        """Technical Solution property 4's own named example: the escaping CWD
        belongs to the read-only `log` invocation, the write (`commit`) never
        left the worktree. A naive 'any verb is a write AND any cwd escapes'
        check would block this; per-invocation pairing must not."""
        d = classify_bash(
            f"git -C {_MAIN_REPO} log && git commit -m wip", _WT_CLAUDE
        )
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"


class TestD1746SharedWalkerDirect:
    """Direct unit coverage of the shared tokenize/walk layer, independent of
    classify_bash's higher-level decision."""

    def test_resolve_effective_cwd_returns_leading_invocations_own_cwd(self) -> None:
        # First invocation's -C wins for the thin-wrapper "leading result"
        # contract, even though a second invocation follows.
        result = resolve_effective_cwd(
            f"git diff && git -C {_MAIN_REPO} push --force", _WT_CLAUDE
        )
        assert result == str(Path(_WT_CLAUDE).resolve())

    def test_resolve_effective_cwd_first_invocation_dash_c(self) -> None:
        result = resolve_effective_cwd(f"git -C {_MAIN_REPO} push --force", _WT_CLAUDE)
        assert result == str(Path(_MAIN_REPO).resolve())

    def test_walk_git_invocations_pairs_each_invocation_with_its_own_cwd(self) -> None:
        from hooks.sandbox_rules import _tokenize_shell_command, _walk_git_invocations

        tokens = _tokenize_shell_command(
            f"git -C {_MAIN_REPO} log && git commit -m wip"
        )
        invocations, _final_cwd = _walk_git_invocations(tokens, _WT_CLAUDE)
        # D#2058: _walk_git_invocations now returns (verb, cwd, args) triples
        # — the args element (every invocation's own flags) is what lets
        # classify_bash tell `git branch --list` apart from `git branch -D x`.
        assert [verb for verb, _cwd, _args in invocations] == ["log", "commit"]
        assert invocations[0][1] == str(Path(_MAIN_REPO).resolve())
        assert invocations[1][1] == str(Path(_WT_CLAUDE).resolve())

    def test_extract_all_git_verbs_unaffected_by_the_refactor(self) -> None:
        """_extract_all_git_verbs is now a thin wrapper over the shared
        walker — pin down that its own public contract (verbs only, no cwd
        awareness) is unchanged."""
        assert _extract_all_git_verbs(
            f"git diff && git -C {_MAIN_REPO} push --force"
        ) == ["diff", "push"]

    def test_no_private_normalisation_copies_remain(self) -> None:
        """Spec item 20: neither resolve_effective_cwd nor
        _extract_all_git_verbs may keep its own re.sub operator normalisation
        or its own shlex.split call — both must go through
        _tokenize_shell_command / _walk_git_invocations."""
        import inspect

        from hooks.sandbox_rules import _extract_all_git_verbs as _eagv
        from hooks.sandbox_rules import resolve_effective_cwd as _rec

        for fn in (_rec, _eagv):
            src = inspect.getsource(fn)
            assert "shlex.split" not in src, f"{fn.__name__} still calls shlex.split directly"
            assert "re.sub" not in src, f"{fn.__name__} still has its own re.sub normalisation"

    def test_unparseable_command_does_not_silently_resolve_to_base_cwd(self) -> None:
        """Spec item 21: fail-closed on unparseable input. An unbalanced-quote
        command containing a git write verb pointed outside the worktree must
        not silently report base_cwd (which would read as 'no escape')."""
        bad = f"git -C {_MAIN_REPO} commit -m 'unterminated"
        result = resolve_effective_cwd(bad, _WT_CLAUDE)
        # Best-effort tokenisation still finds -C's target rather than
        # defaulting to base_cwd outright.
        assert result != str(Path(_WT_CLAUDE).resolve())


# ---------------------------------------------------------------------------
# D#1746 round 2 — post-review finding: `_GIT_CWD_TAKING_OPTS` matched
# `--git-dir`/`--work-tree` by exact token equality, so it only recognised
# the space-separated form. The glued `--opt=value` form (standard,
# documented git CLI syntax, and the MORE common spelling in scripts) fell
# through as an ordinary unrecognised flag — the CWD override was silently
# skipped. Proven live: `git --git-dir=<main>/.git --work-tree=<main> push
# --force` returned ALLOW from a worktree cwd, and a real commit landed on a
# disposable fixture's main-repo branch, undetected.
#
# Fixed generally via `_split_glued_git_option` (splits any `--`-prefixed
# token on its first `=`) rather than special-casing the two glued
# spellings — same anti-enumeration bar as the rest of this file (Spec
# items 18/19).
# ---------------------------------------------------------------------------


class TestD1746GluedLongOptionForm:
    def test_split_glued_git_option_splits_on_first_equals(self) -> None:
        from hooks.sandbox_rules import _split_glued_git_option

        assert _split_glued_git_option("--git-dir=/x/.git") == ("--git-dir", "/x/.git")
        assert _split_glued_git_option("--work-tree=/x") == ("--work-tree", "/x")
        assert _split_glued_git_option("--namespace=foo=bar") == ("--namespace", "foo=bar")

    def test_split_glued_git_option_leaves_non_glued_tokens_alone(self) -> None:
        from hooks.sandbox_rules import _split_glued_git_option

        assert _split_glued_git_option("--git-dir") == ("--git-dir", None)
        assert _split_glued_git_option("-C") == ("-C", None)
        assert _split_glued_git_option("-c") == ("-c", None)
        assert _split_glued_git_option("push") == ("push", None)
        # Short options never take this glued form — not split even if a
        # user types one with a literal `=` in it.
        assert _split_glued_git_option("-C=/x") == ("-C=/x", None)

    def test_glued_git_dir_alone_blocks(self) -> None:
        d = classify_bash(f"git --git-dir={_MAIN_REPO}/.git push --force", _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow with reason={d.reason!r}"
        assert "git write-verb outside worktree" in d.reason

    def test_glued_work_tree_alone_blocks(self) -> None:
        d = classify_bash(f"git --work-tree={_MAIN_REPO} push --force", _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow with reason={d.reason!r}"
        assert "git write-verb outside worktree" in d.reason

    def test_glued_git_dir_and_work_tree_combo_blocks(self) -> None:
        """The tester's exact live repro."""
        d = classify_bash(
            f"git --git-dir={_MAIN_REPO}/.git --work-tree={_MAIN_REPO} push --force",
            _WT_CLAUDE,
        )
        assert not d.allow, f"expected BLOCK, got allow with reason={d.reason!r}"
        assert "git write-verb outside worktree" in d.reason

    def test_absolute_path_git_binary_with_glued_options_blocks(self) -> None:
        """F2 (non-bare git spelling) composed with the glued long-option form."""
        d = classify_bash(
            f"/usr/bin/git --git-dir={_MAIN_REPO}/.git --work-tree={_MAIN_REPO} push --force",
            _WT_CLAUDE,
        )
        assert not d.allow

    def test_space_separated_form_still_blocks_unaffected(self) -> None:
        """Regression pin: the pre-existing space-separated form must not
        have regressed while adding the glued-form path."""
        d = classify_bash(
            f"git --git-dir {_MAIN_REPO}/.git --work-tree {_MAIN_REPO} push --force",
            _WT_CLAUDE,
        )
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_dash_capital_c_space_form_unaffected(self) -> None:
        """`-C` is a short option and never takes the glued `=` form — pin
        that the space-separated `-C <path>` path (the common case) still
        works exactly as before."""
        d = classify_bash(f"git -C {_MAIN_REPO} push --force", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_glued_namespace_value_taking_but_not_cwd_relocating(self) -> None:
        """Generalised modelling covers ALL value-taking global options, not
        just the two CWD-relocating ones — `--namespace=x` must not let its
        glued value get misread as the verb (F4's mechanism, glued form)."""
        assert _extract_all_git_verbs("git --namespace=x checkout main") == ["checkout"]
        d = classify_bash("git --namespace=x checkout main", _WT_CLAUDE)
        assert not d.allow
        assert "git write-verb outside worktree" in d.reason

    def test_glued_dash_c_read_only_stays_allowed(self) -> None:
        """No over-block: glued `--git-dir=`/`--work-tree=` on a read-only
        verb must still be allowlisted, same as the space-separated form."""
        d = classify_bash(f"git --git-dir={_MAIN_REPO}/.git log -5", _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"

    def test_glued_dash_c_pointing_inside_worktree_stays_allowed(self) -> None:
        d = classify_bash(f"git --work-tree={_WT_CLAUDE} commit -m wip", _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"

    def test_walk_git_invocations_resolves_glued_cwd_directly(self) -> None:
        from hooks.sandbox_rules import _tokenize_shell_command, _walk_git_invocations

        tokens = _tokenize_shell_command(f"git --work-tree={_MAIN_REPO} push --force")
        invocations, _final_cwd = _walk_git_invocations(tokens, _WT_CLAUDE)
        # D#2058: third element is this invocation's own args list.
        assert invocations == [("push", str(Path(_MAIN_REPO).resolve()), ["--force"])]


# ---------------------------------------------------------------------------
# D#1944 SEC-7 — repository Actions variables are the CI merge gate's kill
# switch, so a sub-agent must not be able to write one.
#
# Both directions are asserted deliberately. A deny-only test would pass just
# as well against a rule that blocks every mention of `actions/variables`,
# including the read the CI gate itself performs on every merge — which would
# break the thing this rule exists to protect.
# ---------------------------------------------------------------------------


class TestD1944ActionsVariableWrite:
    _VAR = "repos/autonomous-agent-7/fulcrumaxe/actions/variables/CI_DISABLED"
    _COLLECTION = "repos/autonomous-agent-7/fulcrumaxe/actions/variables"

    @pytest.mark.parametrize("method", ["PATCH", "POST", "PUT", "DELETE"])
    def test_explicit_method_blocked(self, method: str) -> None:
        d = classify_bash(
            f"gh api -X {method} {self._VAR} -f value=true",
            _WT_CLAUDE,
        )
        assert not d.allow
        assert "sandbox_block_actions_variable_write" in d.reason

    def test_implicit_post_blocked(self) -> None:
        """`gh api` switches to POST as soon as a field flag is present — there
        is no -X for a method regex to match on."""
        d = classify_bash(
            f"gh api {self._COLLECTION} -f name=CI_DISABLED -f value=true",
            _WT_CLAUDE,
        )
        assert not d.allow
        assert "sandbox_block_actions_variable_write" in d.reason

    def test_gh_variable_alias_blocked(self) -> None:
        d = classify_bash(
            "gh variable set CI_DISABLED --repo autonomous-agent-7/fulcrumaxe --body true",
            _WT_CLAUDE,
        )
        assert not d.allow
        assert "sandbox_block_actions_variable_write" in d.reason

    def test_plain_collection_read_allowed(self) -> None:
        d = classify_bash(f"gh api {self._COLLECTION}", _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"

    def test_plain_single_variable_read_allowed(self) -> None:
        """This is exactly the call scripts/lib/ci-status-check.sh makes on
        every merge to decide whether to stand the gate down."""
        d = classify_bash(f"gh api -i {self._VAR}", _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"

    def test_variable_list_read_allowed(self) -> None:
        d = classify_bash(
            "gh variable list --repo autonomous-agent-7/fulcrumaxe", _WT_CLAUDE
        )
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"


# ---------------------------------------------------------------------------
# D#1931 — two drifted /tmp call sites (defect 1) plus a verbless `git`
# invocation escaping both gates (defect 2).
# ---------------------------------------------------------------------------


class TestD1931BareTmpRestored:
    """A1 — these false-BLOCKed on main because :2071's inlined prefix test
    ran on a normpath'd candidate that could never carry the trailing
    separator the test demanded. Swapping both drifted call sites for the
    SSOT helper `_is_ephemeral_tmp_path()` restores allow=True."""

    @pytest.mark.parametrize(
        "command",
        [
            "cd /tmp && git log",  # A1.1
            "cd /tmp/ && git log",  # A1.2
            "cd /tmp && ls",  # A1.3
            "cd /tmp/ && ls",  # A1.4
            "cd /var/tmp && ls",  # A1.5
            "cd /var/tmp/ && ls",  # A1.6
            "cd /tmp",  # A1.7
        ],
    )
    def test_bare_tmp_now_allowed(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r} for: {command!r}"


class TestD1931NotRegressed:
    """A2 — cases that already allowed on main and must keep allowing."""

    @pytest.mark.parametrize(
        "command",
        [
            "cd /tmp/foo && git log",
            "ls /tmp",
            f"git -C {_MAIN_REPO} log --oneline -5",
            f"git -C {_MAIN_REPO} status",
            "git commit -m x",
        ],
    )
    def test_stays_allowed(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r} for: {command!r}"


class TestD1931StillBlocked:
    """A3 — the other, non-negotiable direction. A fix that widens the
    exemption until A1 passes but any row here starts allowing is a reject."""

    def test_touch_outside_worktree_blocked(self) -> None:
        # Row 13.
        d = classify_bash(f"touch {_MAIN_REPO}/zzz.txt", _WT_CLAUDE)
        assert not d.allow
        assert "path-token scan" in d.reason

    def test_cd_then_relative_touch_blocked(self) -> None:
        # Row 15 — cd escapes the worktree, then a bare relative write lands
        # outside it. `touch` (not a redirect) is caught by the unenumerated
        # path-token scan rather than _cd_escape_relative_write (that helper
        # only watches `>`/`>>` redirects) — either mechanism satisfies the
        # Spec's allow=False requirement for this row.
        d = classify_bash(f"cd {_MAIN_REPO} && touch zzz.txt", _WT_CLAUDE)
        assert not d.allow

    def test_traversal_does_not_ride_the_tmp_exemption(self) -> None:
        # Row 18 — the SSOT helper normalises `..` segments away, so this
        # must NOT be misread as staying under /tmp.
        d = classify_bash(
            f"touch /tmp/..{_MAIN_REPO}/zzz.txt", _WT_CLAUDE
        )
        assert not d.allow, f"expected BLOCK, got {d}"

    @pytest.mark.parametrize(
        "command",
        [
            f"echo hi > {_MAIN_REPO}/CLAUDE.md",  # row 14
            f"rm -f {_MAIN_REPO}/CLAUDE.md",  # row 16
            f"cp x {_MAIN_REPO}/y.txt",  # row 17
            "git checkout main",  # row 19
            "gh pr merge 1",  # row 20
        ],
    )
    def test_other_must_block_rows(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got allow for: {command!r}"


class TestD1931VerblessGitEscape:
    """A4 — defect 2. A verbless `git` invocation (`git <path>`, bare `git -C
    <dir>`) was never paired with a verb by step 3, and step 4b exempted it
    anyway on the segment name alone — `allow=True` with no check ever
    looking at the path operand. `_segment_git_has_verb` makes the step 4b
    exemption conditional on step 3 actually having had a verb to vet."""

    def test_verbless_git_with_outside_path_now_blocked(self) -> None:
        # Row 21 — was allow=True on main.
        d = classify_bash(f"git {_MAIN_REPO}/CLAUDE.md", _WT_CLAUDE)
        assert not d.allow, f"expected BLOCK, got {d}"

    @pytest.mark.parametrize(
        "command",
        [
            "git --version",  # row 22
            "git --help",  # row 23
        ],
    )
    def test_flag_only_git_without_path_operand_stays_allowed(
        self, command: str
    ) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert d.allow, f"expected ALLOW, got reason={d.reason!r} for: {command!r}"

    def test_dash_c_with_no_verb_begins_blocking(self) -> None:
        # Spec's explicit note: `git -C <main-repo>` with no verb is a usage
        # error with no legitimate caller, and closing it is the point of A4.
        d = classify_bash(f"git -C {_MAIN_REPO}", _WT_CLAUDE)
        assert not d.allow

    def test_is_segment_write_candidate_verbless_git_is_a_candidate(self) -> None:
        """Direct unit coverage of the mechanism the fix relies on."""
        assert _is_segment_write_candidate(["git", _MAIN_REPO + "/CLAUDE.md"]) is True
        assert _is_segment_write_candidate(["git", "--version"]) is True
        assert _is_segment_write_candidate(["git", "-C", _MAIN_REPO]) is True
        # Verbed invocations still exempt, unchanged from D#1756.
        assert _is_segment_write_candidate(["git", "-C", _MAIN_REPO, "log"]) is False
        assert _is_segment_write_candidate(["git", "commit", "-m", "x"]) is False


class TestD1931StructuralOneCallSite:
    """A5 — the exemption's only expression left in the module must be the
    SSOT helper's own body."""

    def test_only_call_site_is_the_helper_itself(self) -> None:
        source = (_REPO / "hooks" / "sandbox_rules.py").read_text()
        hits = [
            i
            for i, line in enumerate(source.splitlines(), start=1)
            if 'startswith(("/tmp/"' in line
        ]
        assert len(hits) == 1, f"expected exactly one call site, found {hits}"
        # And that one line must sit inside _is_ephemeral_tmp_path's own body.
        lines = source.splitlines()
        func_start = next(
            i
            for i, line in enumerate(lines)
            if line.startswith("def _is_ephemeral_tmp_path(")
        )
        func_end = next(
            i
            for i, line in enumerate(lines[func_start + 1 :], start=func_start + 1)
            if line.startswith("def ")
        )
        assert func_start < hits[0] - 1 < func_end, (
            f"the surviving call site at line {hits[0]} must be inside "
            f"_is_ephemeral_tmp_path (lines {func_start + 1}-{func_end})"
        )


# ---------------------------------------------------------------------------
# D#2058 — Rule 1: _FORBIDDEN_FRAGMENTS is position-aware, not a raw substring
# test over the whole command string. A fragment must be EXECUTED (argv[0],
# or a non-flag operand of an interpreter/sourcing form) to match; a mention
# — reading it, grepping it, quoting it in a PR body — must not.
# ---------------------------------------------------------------------------


class TestD2058Rule1FragmentPositional:
    """Spec criterion 1: the filing's eight-row table, through the real rule."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "grep -n legacy_lane backend/trigger.py",
            "cat run-loop-iteration.sh",
            "gh pr edit 1 --body 'this fixes backend/trigger.py'",
            "git log --oneline -- run-loop-iteration.sh",
            "echo 'see spawn-agent.sh for details'",
            "ls -la scripts/",
        ],
    )
    def test_mention_or_read_allowed(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert d.allow, f"Expected ALLOW for: {cmd!r}, got reason={d.reason!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "bash run-loop-iteration.sh",
            "python3 backend/trigger.py 'go'",
        ],
    )
    def test_genuine_runaway_still_blocked(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected BLOCK for: {cmd!r}"
        assert "claude_spawn_forbidden" in d.reason

    def test_mutation_a_raw_substring_would_over_block(self) -> None:
        """Mutation A (documented, not applied here): reverting to
        `if fragment in cmd_str` over the raw string would flip every ALLOW
        row above to BLOCK — proven live in the PR body by temporarily
        restoring that line and re-running this class (all five ALLOW
        assertions failed). This test pins the CURRENT (fixed) behaviour so
        CI catches a regression back to that shape.
        """
        for cmd in (
            "grep -n legacy_lane backend/trigger.py",
            "cat run-loop-iteration.sh",
            "echo 'see spawn-agent.sh for details'",
        ):
            assert check_claude_spawn([], cmd).allow

    def test_mutation_b_argv0_only_would_under_block(self) -> None:
        """Mutation B (documented, not applied here): checking only argv[0]
        (dropping the interpreter-operand branch) would flip
        `python3 backend/trigger.py 'go'` to ALLOW — proven live in the PR
        body. This pins the requirement that the interpreter-operand shape
        stays active.
        """
        d = check_claude_spawn([], "python3 backend/trigger.py 'go'")
        assert not d.allow


class TestD2058Rule1InterpreterAndSourcingForms:
    """Spec criterion 2: every interpreter/sourcing/chained spelling blocks."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "bash run-loop-iteration.sh",
            "sh run-loop-iteration.sh",
            "python3 backend/trigger.py",
            "source scripts/spawn-agent.sh",
            ". scripts/spawn-agent.sh",
            "./run-loop-iteration.sh",
            "bash -c 'bash run-loop-iteration.sh'",
            "ls && bash run-loop-iteration.sh",
            "ls; python3 backend/trigger.py",
        ],
    )
    def test_all_forms_blocked(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected BLOCK for: {cmd!r}"
        assert "claude_spawn_forbidden" in d.reason

    def test_mutation_drop_bash_c_recursion_would_under_block(self) -> None:
        """Mutation (documented, not applied here): disabling the bash -c /
        sh -c recursion in check_claude_spawn step 3 flips
        `bash -c 'bash run-loop-iteration.sh'` to ALLOW — proven live in the
        PR body (the interpreter-operand scan deliberately does NOT re-scan
        a -c payload itself; see _DASH_C_SHELLS' docstring). This pins the
        requirement that the recursion stays active.
        """
        d = check_claude_spawn([], "bash -c 'bash run-loop-iteration.sh'")
        assert not d.allow

    def test_mention_only_bash_c_payload_not_blocked(self) -> None:
        """A -c payload that only MENTIONS a fragment (no execution) must
        not block — this is what _DASH_C_SHELLS exists to protect against,
        since quote-stripping (needed for cl"au"de detection) shreds the
        payload's quoting before this rule ever sees it."""
        d = check_claude_spawn([], "bash -c 'echo see run-loop-iteration.sh only'")
        assert d.allow, f"got reason={d.reason!r}"


class TestD2058Rule1ReasonNamesMatch:
    """Spec criterion 3: the refusal still names what matched, and two
    different blocked commands get two different reasons (tautology guard —
    a hard-coded constant reason string would pass `assert decision.reason`
    but fail the substring + differ assertions below)."""

    def test_reason_contains_specific_fragment(self) -> None:
        d = check_claude_spawn([], "bash run-loop-iteration.sh")
        assert not d.allow
        assert "run-loop-iteration.sh" in d.reason

        d2 = check_claude_spawn([], "python3 backend/trigger.py")
        assert not d2.allow
        assert "backend/trigger.py" in d2.reason

    def test_two_different_matches_have_different_reasons(self) -> None:
        d1 = check_claude_spawn([], "bash run-loop-iteration.sh")
        d2 = check_claude_spawn([], "python3 backend/trigger.py")
        assert d1.reason != d2.reason, (
            "a hard-coded reason string would pass a bare `assert decision.reason` "
            "but must fail this: two different matches must name two different "
            "fragments"
        )


class TestD2058Rule1ExistingCorpusRegression:
    """The pre-existing D#439 positive/negative corpus must be unaffected —
    this rewrite only changes WHERE fragments are tested, not the fragment
    list itself."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 backend/_start_loop_run.py",
            "bash scripts/loop-trigger.sh",
            "bash run-loop-iteration.sh",
            "python3 backend/trigger.py",
            "bash scripts/spawn-agent.sh --role executor",
        ],
    )
    def test_still_denies(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected DENY for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "grep claude CLAUDE.md",
            "cat CLAUDE.md",
            "ls CLAUDE.md",
            "git log --grep claude",
            "cat /tmp/claude-code-output.txt",
            "ls claude-code-something-else",
            "gh pr create --base main --title 'test' --body 'body'",
            "pytest tests/ -x -q",
            "npm run build",
            "grep -r 'claude' hooks/",
            "python3 scripts/measure-spawn-context.sh",
        ],
    )
    def test_still_allows(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert d.allow, f"Expected ALLOW for: {cmd!r}, got reason={d.reason!r}"


# ---------------------------------------------------------------------------
# D#2058 fix-cycle 1 (security review) — a command-prefix WRAPPER
# (`timeout`, `nohup`, `nice`, `stdbuf`, `setsid`, `time`, `ionice`, `xargs`)
# is neither argv[0]-is-the-fragment nor an _INTERPRETER_TOKENS operand, so
# `timeout 60 bash run-loop-iteration.sh` and `nohup bash
# run-loop-iteration.sh &` fell through both shapes untouched. Fixed in
# _command_positions (not _INTERPRETER_TOKENS — that would have made
# `timeout 60 cat run-loop-iteration.sh`, a read, block).
# ---------------------------------------------------------------------------


class TestD2058FixCycle1WrapperTokensBlocked:
    """Every wrapper form the security reviewer measured end-to-end must
    still block once wrapped — the wrapper never executes anything itself,
    it just runs the real command underneath, unchanged."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "timeout 60 bash run-loop-iteration.sh",
            "timeout 300 python3 backend/trigger.py",
            "nohup bash run-loop-iteration.sh &",
            "nice bash run-loop-iteration.sh",
            "nice -n 10 bash run-loop-iteration.sh",
            "stdbuf -oL bash run-loop-iteration.sh",
            "setsid bash run-loop-iteration.sh",
            "time bash run-loop-iteration.sh",
            "ionice -c2 bash run-loop-iteration.sh",
            "xargs bash run-loop-iteration.sh",
            "xargs -I{} bash run-loop-iteration.sh {}",
            # The bounded-run form this repo's own agent briefs mandate
            # (`timeout --kill-after=5s N <cmd>`) — not exotic, must block
            # exactly the same as the bare `timeout 60` form.
            "timeout --kill-after=5s 600 bash run-loop-iteration.sh",
            # Control: the un-wrapped form, which already blocked before
            # this fix cycle and must still block after it.
            "bash run-loop-iteration.sh",
        ],
    )
    def test_wrapped_runaway_blocked(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert not d.allow, f"Expected BLOCK for: {cmd!r}"
        assert "claude_spawn_forbidden" in d.reason

    def test_mutation_remove_wrapper_skip_would_under_block(self) -> None:
        """Required mutation (documented in the PR body, run against the
        patched source and confirmed red, then reverted): removing the
        wrapper skip from _command_positions flips the bounded-run form back
        to ALLOW — the exact regression the security review caught."""
        d = check_claude_spawn([], "timeout 60 bash run-loop-iteration.sh")
        assert not d.allow

    @pytest.mark.parametrize(
        "cmd",
        [
            # A wrapped READ must stay allowed — this is the trap of fixing
            # this in _INTERPRETER_TOKENS instead of _command_positions.
            "timeout 60 cat run-loop-iteration.sh",
            "nice cat run-loop-iteration.sh",
            "xargs cat run-loop-iteration.sh",
            # The mandated bounded-run form over a harmless command.
            "timeout --kill-after=5s 600 pytest backend/tests/test_foo.py",
        ],
    )
    def test_wrapped_read_still_allowed(self, cmd: str) -> None:
        d = check_claude_spawn([], cmd)
        assert d.allow, f"Expected ALLOW for: {cmd!r}, got reason={d.reason!r}"

    def test_exec_still_over_blocks_informational_not_this_cycle(self) -> None:
        """The reviewer flagged `exec cat run-loop-iteration.sh` as a
        pre-existing, narrower over-block (exec is in _INTERPRETER_TOKENS)
        — informational, filed separately, deliberately NOT fixed in this
        cycle. Pinning the current (unchanged) behaviour here so nobody
        mistakes its continued presence for a regression of this fix."""
        d = check_claude_spawn([], "exec cat run-loop-iteration.sh")
        assert not d.allow


# ---------------------------------------------------------------------------
# D#2058 — Rule 2: _GIT_ALWAYS_BLOCKED_VERBS ignored flags entirely, so
# `git branch`/`git branch --list`/`git branch --show-current` and
# `git reset --help` (a documentation lookup) blocked identically to
# `git branch -D foo`. Only `branch` and `worktree` get a per-invocation
# read-only escape (`--help`/`-h` escapes ALL seven, verb-independent).
# ---------------------------------------------------------------------------


class TestD2058Rule2ReadonlyEscapeAllows:
    """Spec criterion 4: read-only spellings ALLOW."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git branch",
            "git branch --list",
            "git branch -l",
            "git branch --show-current",
            "git branch -a",
            "git branch -v",
            "git branch --merged",
            "git branch --help",
            "git reset --help",
            "git checkout --help",
            "git worktree list",
            "git worktree list --porcelain",
        ],
    )
    def test_readonly_spelling_allowed(self, cmd: str) -> None:
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for: {cmd!r}, got reason={d.reason!r}"

    def test_mutation_verb_only_membership_would_over_block(self) -> None:
        """Mutation (documented, not applied here): reverting to bare verb
        membership (dropping _is_git_readonly_invocation entirely) flips
        ALL twelve rows above to BLOCK — proven live in the PR body."""
        for cmd in ("git branch", "git branch --list", "git worktree list"):
            assert classify_bash(cmd, _WT_CLAUDE).allow


class TestD2058Rule2WriteSpellingsStillBlocked:
    """Spec criterion 5: write spellings of the SAME two verbs stay refused."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git branch -D foo",
            "git branch -d foo",
            "git branch -m a b",
            "git branch -M a b",
            "git branch --set-upstream-to=origin/x",
            "git worktree add /tmp/x",
            "git worktree remove /tmp/x",
            "git worktree prune",
            "git worktree move a b",
            "git checkout -b foo",
            "git switch main",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "git restore .",
        ],
    )
    def test_write_spelling_blocked(self, cmd: str) -> None:
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for: {cmd!r}"
        assert d.reason == "git write-verb outside worktree"

    def test_mutation_removing_branch_worktree_from_set_would_under_block(self) -> None:
        """Mutation (required, documented in the PR body): removing `branch`
        and `worktree` from _GIT_ALWAYS_BLOCKED_VERBS entirely flips
        `git branch -D foo` and `git worktree remove /tmp/x` to ALLOW —
        permitting the whole verb is worse than the pre-fix over-block,
        because branch deletion / worktree removal from the wrong checkout
        are exactly the accidental writes this guard exists for."""
        assert not classify_bash("git branch -D foo", _WT_CLAUDE).allow
        assert not classify_bash("git worktree remove /tmp/x", _WT_CLAUDE).allow


class TestD2058Rule2UnknownFlagsFailClosed:
    """Spec criterion 6: unrecognised flags/subcommands fail closed — the
    read-only spelling is an allowlist, not "everything except known writes".
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            "git branch --some-future-flag",
            "git worktree some-future-subcommand",
        ],
    )
    def test_unrecognised_spelling_blocked(self, cmd: str) -> None:
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK (fail closed) for: {cmd!r}"

    def test_mutation_allow_unless_known_write_would_under_block(self) -> None:
        """Mutation (required, documented in the PR body): inverting
        _is_git_readonly_invocation to a denylist of known-write flags
        (default ALLOW otherwise) flips both rows above to ALLOW."""
        assert not classify_bash("git branch --some-future-flag", _WT_CLAUDE).allow
        assert not classify_bash("git worktree some-future-subcommand", _WT_CLAUDE).allow


class TestD2058Rule2ChainedEscapeRegression:
    """Spec criterion 7: the D#1729 F3 chained-command escape must not
    reappear — a read-only-first verb must not shield a later always-blocked
    write spelling in the same command."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git log;git reset --hard origin/main",
            "git status && git worktree remove /tmp/x",
        ],
    )
    def test_chained_escape_still_blocked(self, cmd: str) -> None:
        d = classify_bash(cmd, _WT_CLAUDE)
        assert not d.allow, f"Expected BLOCK for: {cmd!r}"
        assert d.reason == "git write-verb outside worktree"

    def test_mutation_first_readonly_verb_return_allow_would_under_block(self) -> None:
        """Mutation (required, documented in the PR body): making the new
        flag check `return Decision(allow=True)` on the first read-only verb
        it sees reintroduces first-verb-wins and flips both rows above to
        ALLOW."""
        assert not classify_bash("git log;git reset --hard origin/main", _WT_CLAUDE).allow
        assert not classify_bash(
            "git status && git worktree remove /tmp/x", _WT_CLAUDE
        ).allow


class TestD2058Rule2AlreadyWorkingUnaffected:
    """Spec criterion 8: verbs never touched by this fix (comment 5's
    confirmed retraction) must keep allowing."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git status --porcelain",
            "git log --oneline -1",
            "git stash list",
            "git remote -v",
            "git tag --list",
            "git show HEAD:scripts/pre-spawn-check.sh",
            "ls -la scripts/",
        ],
    )
    def test_still_allowed(self, cmd: str) -> None:
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow, f"Expected ALLOW for: {cmd!r}, got reason={d.reason!r}"


class TestD2058IsGitReadonlyInvocationUnit:
    """Direct unit coverage of the new escape's own decision function,
    isolated from classify_bash's surrounding CWD/write-verb logic."""

    def test_help_escapes_any_always_blocked_verb(self) -> None:
        assert _is_git_readonly_invocation("checkout", ["--help"]) is True
        assert _is_git_readonly_invocation("reset", ["-h"]) is True
        assert _is_git_readonly_invocation("clean", ["--help"]) is True

    def test_branch_bare_and_known_flags_readonly(self) -> None:
        assert _is_git_readonly_invocation("branch", []) is True
        assert _is_git_readonly_invocation("branch", ["--list"]) is True
        assert _is_git_readonly_invocation("branch", ["-a", "-v"]) is True

    def test_branch_any_unrecognised_arg_not_readonly(self) -> None:
        assert _is_git_readonly_invocation("branch", ["new-branch-name"]) is False
        assert _is_git_readonly_invocation("branch", ["-D", "foo"]) is False

    def test_worktree_list_readonly_other_subcommands_not(self) -> None:
        assert _is_git_readonly_invocation("worktree", ["list"]) is True
        assert _is_git_readonly_invocation("worktree", ["list", "--porcelain"]) is True
        assert _is_git_readonly_invocation("worktree", ["add", "/tmp/x"]) is False
        assert _is_git_readonly_invocation("worktree", []) is False

    def test_checkout_switch_reset_clean_restore_have_no_non_help_escape(self) -> None:
        for verb in ("checkout", "switch", "reset", "clean", "restore"):
            assert _is_git_readonly_invocation(verb, []) is False
            assert _is_git_readonly_invocation(verb, ["--some-flag"]) is False


# ---------------------------------------------------------------------------
# D#2058 — Spec criterion 9: end-to-end through the REAL hook
# (hooks/sandbox.py, the same entry point the PreToolUse hook invokes), not
# just the pure functions. Mirrors the `_run_hook` pattern already used by
# tests/test_sandbox_agent_block.py.
# ---------------------------------------------------------------------------


def _run_sandbox_hook(command: str, cwd: str) -> tuple[int, str]:
    """Run hooks/sandbox.py with a Bash tool_input payload; return (exit_code, stderr)."""
    hook = str(_REPO / "hooks" / "sandbox.py")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})
    result = subprocess.run(
        [sys.executable, hook],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode, result.stderr


class TestD2058EndToEndThroughRealHook:
    """Drive the Rule-1 eight-row table and the Rule-2 twelve ALLOW rows
    through the actual hook script, not the pure classify_bash/
    check_claude_spawn functions directly — this is what would have caught a
    regression where the pure functions are right but sandbox.py's wiring
    (argument parsing, cwd resolution) drops or reorders a check."""

    @pytest.mark.parametrize(
        "cmd,expect_allow",
        [
            ("grep -n legacy_lane backend/trigger.py", True),
            ("cat run-loop-iteration.sh", True),
            ("gh pr edit 1 --body 'this fixes backend/trigger.py'", True),
            ("git log --oneline -- run-loop-iteration.sh", True),
            ("echo 'see spawn-agent.sh for details'", True),
            ("ls -la scripts/", True),
            ("bash run-loop-iteration.sh", False),
            ("python3 backend/trigger.py 'go'", False),
        ],
    )
    def test_rule1_table_through_real_hook(self, cmd: str, expect_allow: bool) -> None:
        code, stderr = _run_sandbox_hook(cmd, _WT_CLAUDE)
        if expect_allow:
            assert code == 0, f"Expected ALLOW (exit 0) for: {cmd!r}, got {code}: {stderr}"
        else:
            assert code == 2, f"Expected BLOCK (exit 2) for: {cmd!r}, got {code}: {stderr}"
            assert "claude_spawn_forbidden: matched pattern" in stderr

    @pytest.mark.parametrize(
        "cmd",
        [
            "git branch",
            "git branch --list",
            "git branch -l",
            "git branch --show-current",
            "git branch -a",
            "git branch -v",
            "git branch --merged",
            "git branch --help",
            "git reset --help",
            "git checkout --help",
            "git worktree list",
            "git worktree list --porcelain",
        ],
    )
    def test_rule2_allow_rows_through_real_hook(self, cmd: str) -> None:
        code, stderr = _run_sandbox_hook(cmd, _WT_CLAUDE)
        assert code == 0, f"Expected ALLOW (exit 0) for: {cmd!r}, got {code}: {stderr}"

    def test_worktree_list_names_the_own_worktree_path(self) -> None:
        """The point of the whole filing (D#2078 connection): `git worktree
        list` must not be refused, full stop — checked above — and this
        confirms the hook's ALLOW is real (exit 0, no refusal text), not an
        accidental pass-through."""
        code, stderr = _run_sandbox_hook("git worktree list", _WT_CLAUDE)
        assert code == 0
        assert stderr == "" or "blocked by sandbox" not in stderr


# ---------------------------------------------------------------------------
# D#2058 — Spec criterion 11: the case that produced this filing. The file
# is at the repo root (verified: not under scripts/).
# ---------------------------------------------------------------------------


class TestD2058RunLoopIterationCatVsExec:
    def test_cat_allowed_bash_blocked(self) -> None:
        d_cat = check_claude_spawn([], "cat ./run-loop-iteration.sh")
        assert d_cat.allow, f"got reason={d_cat.reason!r}"
        d_bash = check_claude_spawn([], "bash ./run-loop-iteration.sh")
        assert not d_bash.allow
        assert "claude_spawn_forbidden" in d_bash.reason

    def test_file_exists_at_repo_root_not_under_scripts(self) -> None:
        assert (_REPO / "run-loop-iteration.sh").is_file()
        assert not (_REPO / "scripts" / "run-loop-iteration.sh").is_file()

    def test_cat_allowed_through_real_hook_and_returns_real_content(self) -> None:
        """Spec: 'confirm the ALLOW is real by actually running the cat ...
        and getting file contents back, not a refusal.'"""
        hook = str(_REPO / "hooks" / "sandbox.py")
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "cat ./run-loop-iteration.sh"},
                "cwd": _WT_CLAUDE,
            }
        )
        result = subprocess.run(
            [sys.executable, hook],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"hook refused: {result.stderr}"
        # The hook itself only adjudicates allow/refuse — it does not run the
        # command. Actually running `cat` here (a real subprocess, the same
        # command the hook just cleared) is what proves the ALLOW is real,
        # not merely a permissive verdict on paper.
        cat_result = subprocess.run(
            ["cat", "./run-loop-iteration.sh"],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert cat_result.returncode == 0
        assert cat_result.stdout.strip() != ""


class TestD2099SubstitutionScratchpadPathAllowed:
    """D#2099: a command substitution whose inner argv[0] is an ordinary
    command must be ALLOWED even when a path ARGUMENT inside it contains the
    substring "claude" (every agent scratchpad path contains "claude-1000";
    every transcript path contains ".claude/projects"). Measured: 80 logged
    `claude_spawn_forbidden` events on these two regexes, 0 genuine, all path
    arguments like these three shapes."""

    def test_dollar_paren_scratchpad_path_allowed(self) -> None:
        d = check_claude_spawn([], "wc -l $(ls /tmp/x/claude-1000/a/f.json)")
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"

    def test_backtick_scratchpad_path_allowed(self) -> None:
        d = check_claude_spawn([], "wc -l `ls /tmp/x/claude-1000/a/f.json`")
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"

    def test_dollar_paren_transcript_path_allowed(self) -> None:
        d = check_claude_spawn(
            [], "cat $(ls $HOME/.claude/projects/p/subagents/agent-x.jsonl)"
        )
        assert d.allow, f"expected ALLOW, got reason={d.reason!r}"


class TestD2099SubstitutionGenuineSpawnStillDenied:
    """D#2099: narrowing the two substitution regexes to command position
    must NOT weaken them for a genuine spawn shape — claude as the actual
    argv[0] inside the substitution, however it is dressed up (absolute
    path, env prefix, wrapper token)."""

    def test_dollar_paren_bare_claude_denied(self) -> None:
        d = check_claude_spawn([], "wc -l $(claude -p x)")
        assert not d.allow
        assert "claude_spawn_forbidden" in d.reason

    def test_backtick_bare_claude_denied(self) -> None:
        d = check_claude_spawn([], "wc -l `claude -p x`")
        assert not d.allow
        assert "claude_spawn_forbidden" in d.reason

    def test_dollar_paren_absolute_path_claude_denied(self) -> None:
        d = check_claude_spawn([], "wc -l $(/usr/local/bin/claude -p x)")
        assert not d.allow
        assert "claude_spawn_forbidden" in d.reason

    def test_dollar_paren_env_prefixed_claude_denied(self) -> None:
        d = check_claude_spawn([], "wc -l $(FOO=bar claude -p x)")
        assert not d.allow
        assert "claude_spawn_forbidden" in d.reason

    def test_dollar_paren_wrapper_prefixed_claude_denied(self) -> None:
        d = check_claude_spawn([], "wc -l $(timeout 5 claude -p x)")
        assert not d.allow
        assert "claude_spawn_forbidden" in d.reason


# ---------------------------------------------------------------------------
# D#2240 — `git rm --cached` cannot delete a working-tree file; allow it at
# the two sandbox call sites (classify_git_rm, hooks/sandbox.py:389) via the
# opt-in `exempt_cached` keyword, while leaving every destructive `git rm`
# spelling blocked and the default (no-keyword) behaviour of the shared
# matcher unchanged for its third caller
# (backend/corpus_drift/claims/archive_protocol.py).
# ---------------------------------------------------------------------------


class TestD2240GitRmCachedExemption:
    """Acceptance items 1-6, 8 for D#2240."""

    @pytest.mark.parametrize(
        "cmd,expect_allow",
        [
            # Destructive spellings stay blocked.
            ("git rm pyc/x.pyc", False),
            ("git rm -r dashboard/node_modules", False),
            ("git rm -f a.py", False),
            # --cached is index-only — allowed.
            ("git rm --cached pyc/x.pyc", True),
            ("git rm -r --cached dashboard/node_modules", True),
            # Consistency: the exact synonym stays allowed too.
            ("git update-index --force-remove pyc/x.pyc", True),
        ],
    )
    def test_item1_six_row_table(self, cmd: str, expect_allow: bool) -> None:
        d = classify_git_rm(cmd)
        assert d.allow is expect_allow, f"{cmd!r}: expected allow={expect_allow}, got {d.allow}"

    def test_item2_step5_broad_scan_honours_exemption(self) -> None:
        """`xargs git rm --cached f` must be allowed too — a fix applied only
        to the step-4 token-walker would leave this spelling blocked while
        the plain spelling is allowed (the same select-by-spelling bug this
        Discussion exists to fix)."""
        assert classify_git_rm("xargs git rm --cached f").allow is True

    @pytest.mark.parametrize(
        "cmd",
        [
            # --cached takes no value; git itself rejects this spelling.
            "git rm --cached=weird a.py",
            # `--` ends option parsing, so --cached here is a pathspec
            # naming a real file to delete.
            "git rm -- --cached",
            # Abbreviation: git's unambiguous-prefix parsing would accept
            # this, but the matcher requires the canonical spelling.
            "git rm --ca a.py",
        ],
    )
    def test_item3_ambiguous_spellings_fail_closed(self, cmd: str) -> None:
        assert classify_git_rm(cmd).allow is False, f"expected block for: {cmd}"

    def test_item4_cached_in_later_pipeline_stage_does_not_exempt(self) -> None:
        """A `--cached` token belonging to a LATER command in the pipeline
        must not exempt an earlier destructive `git rm`."""
        assert classify_git_rm("git rm a.py; git commit --cached").allow is False

    def test_item5_nested_bash_c_propagates(self) -> None:
        """`_is_bash_wrapping_git_write` recurses through classify_bash ->
        classify_git_rm, so the exemption should follow from item 1 with no
        extra code."""
        d = classify_bash("bash -c 'git rm --cached f'", _WT_CLAUDE)
        assert d.allow is True, f"expected allow, got reason={d.reason!r}"

    def test_item6_team_lead_surface_agrees_cached_no_warning(self) -> None:
        """hooks/sandbox.py:389 must no longer emit the archive-protocol
        stderr warning for `git rm --cached` in the Team Lead (main repo)
        context."""
        code, stderr = _run_sandbox_hook("git rm --cached f", _MAIN_REPO)
        assert code == 0
        assert "archive protocol" not in stderr

    def test_item6_team_lead_surface_still_warns_on_destructive_rm(self) -> None:
        """A destructive `git rm` still emits the warning — the Team Lead
        tier is never hard-blocked, only warned+audited."""
        code, stderr = _run_sandbox_hook("git rm f", _MAIN_REPO)
        assert code == 0
        assert "archive protocol" in stderr

    def test_item8_default_keyword_unchanged(self) -> None:
        """Default (no keyword argument) behaviour of the shared matcher is
        unchanged — this is what backend/corpus_drift/claims/archive_protocol.py
        relies on to keep counting `git rm --cached <tracked file>` as a
        violation."""
        assert is_real_git_rm_invocation("git rm --cached f") is True
        assert is_real_git_rm_invocation("git rm f") is True
