"""tests/test_sandbox_path_token_scope.py

D#2246 acceptance matrix — the outside-worktree path-token scanner in
hooks/sandbox_rules.py must judge write intent, not path-shaped text.

Five agents in one session routed around the scanner on ordinary reads,
`cd`, and temp-file work; the PM's own reproduction found four MORE false
positives beyond the five filed. This file pins both halves of the matrix
from the frozen Spec: the permitted rows (real false positives, now fixed)
and the still-blocked rows (genuine writes/HEAD-manipulation/merges, which
must never regress) — so a later tightening fails a test here instead of
silently re-blocking `cd` or a routine `gh api` read.

WT/MAIN are derived from hooks.sandbox_rules's own worktree-prefix and
main-repo-root constants, never hardcoded — under pytest that resolves to
the synthetic fixture root testsupport/fixture_paths.py pins via the
SANDBOX_MAIN_REPO_ROOT env override (see tests/conftest.py); outside pytest
it resolves to the real checkout. Either way this file exercises the same
code the real host does.

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" python3 -m pytest tests/test_sandbox_path_token_scope.py -v

Gotcha for anyone building a base-vs-branch DIFFERENTIAL harness that
invokes hooks/sandbox.py as a real subprocess against a materialised
fixture "main repo" (the PR #2265 re-review did exactly this): root that
fixture OUTSIDE /tmp and /var/tmp. `_is_ephemeral_tmp_path` exempts
everything under /tmp from the worktree-boundary check entirely, so a
fixture rooted there makes every write come back ALLOW on both the base
and the branch under test — the harness measures nothing and looks like it
passed. This file doesn't build such a harness (it calls classify_bash /
classify_path_write in-process), so it isn't affected itself; this note
exists purely so the next person who builds one doesn't re-derive it the
hard way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.sandbox_rules import (  # noqa: E402
    MAIN_REPO_ROOT,
    _WORKTREE_PREFIXES,
    classify_bash,
    classify_path_write,
)

MAIN = str(MAIN_REPO_ROOT)
WT = _WORKTREE_PREFIXES[0] + "d2246testid"


# ---------------------------------------------------------------------------
# Permitted — must classify allow=True (items 1-11)
# ---------------------------------------------------------------------------


class TestPermitted:
    def test_1_cd_to_main_repo(self) -> None:
        d = classify_bash(f"cd {MAIN}", WT)
        assert d.allow is True, d.reason

    def test_2_gh_api_repos_route_not_a_path(self) -> None:
        # R2 — "/repos/..." is a URL route, not a filesystem path; its root
        # segment doesn't exist as a real directory.
        d = classify_bash(
            "gh api /repos/autonomous-agent-7/fulcrumaxe/pulls/1", WT
        )
        assert d.allow is True, d.reason

    def test_3_prose_body_mentioning_a_route(self) -> None:
        # R3 — a path-shaped substring inside a quoted prose argument is a
        # mention, not a write target.
        d = classify_bash(
            'gh pr create --body "fixes the /api/v1/loop route"', WT
        )
        assert d.allow is True, d.reason

    def test_4_sed_script_operand_vs_tmp_target(self) -> None:
        # R4 — the outside-worktree-looking text lives in the sed SCRIPT
        # (never a write target); the actual write target is under /tmp.
        d = classify_bash(f"sed -i 's|{MAIN}/x|y|' /tmp/scratch.txt", WT)
        assert d.allow is True, d.reason

    def test_5_heredoc_diff_header_body(self) -> None:
        # R5 — heredoc body lines are never shell syntax; a diff hunk's own
        # "--- /path" / "+++ /path" header text must not be scanned.
        d = classify_bash(
            f"cat <<EOF\n--- {MAIN}/a.py\n+++ {MAIN}/b.py\nEOF", WT
        )
        assert d.allow is True, d.reason

    def test_6_python_read_with_assignment_and_unrecognised_calls(self) -> None:
        # R6 — a genuine read (json.load(open(...))) that also assigns a
        # local and calls .get() — neither shape the narrow read-only AST
        # proof recognises, but neither is a write either. This is the
        # pairing with R7 below: same read, opposite verdict before this
        # fix, difference was spelling.
        d = classify_bash(
            f"""python3 -c "import json,sys; d=json.load(open('{MAIN}/x.json')); print(d.get('k'))" """,
            WT,
        )
        assert d.allow is True, d.reason

    def test_7_python_pure_read_stays_allowed(self) -> None:
        # R7 — already allowed before this fix; pinned so it can't regress.
        d = classify_bash(
            f"""python3 -c "print(open('{MAIN}/CLAUDE.md').read())" """, WT
        )
        assert d.allow is True, d.reason

    def test_8_echo_bare_root_fragment_not_a_write_target(self) -> None:
        # R8 — a bare "/CLAUDE.md"-shaped fragment is not a write target;
        # belt-and-braces alongside echo already being read-only.
        d = classify_bash('echo "/CLAUDE" "/CLAUDE.md"', WT)
        assert d.allow is True, d.reason

    def test_9_write_tool_to_tmp_scratch(self) -> None:
        d = classify_path_write("/tmp/scratch.txt", WT)
        assert d.allow is True, d.reason

    def test_10_write_tool_to_var_tmp_scratch(self) -> None:
        d = classify_path_write("/var/tmp/scratch.txt", WT)
        assert d.allow is True, d.reason

    @pytest.mark.parametrize(
        "command",
        [
            "mktemp -d",
            "mktemp -d -p /tmp",
            'T=$(mktemp -d) && cd "$T"',
            "cd /tmp",
            "cd ../../..",
            f"grep -n foo {MAIN}/CLAUDE.md",
            f"wc -l {MAIN}/CLAUDE.md",
            "curl -s http://localhost:8787/api/v1/loop",
        ],
    )
    def test_11_lock_in_rows_stay_allowed(self, command: str) -> None:
        # Already allowed today — lock-in, not a fix. A later tightening
        # that re-blocks any of these fails here first.
        d = classify_bash(command, WT)
        assert d.allow is True, f"{command!r}: {d.reason}"


# ---------------------------------------------------------------------------
# Still blocked — must classify allow=False (items 12-23)
# ---------------------------------------------------------------------------


class TestStillBlocked:
    def test_12_sed_in_place_outside_worktree(self) -> None:
        d = classify_bash(f"sed -i s/a/b/ {MAIN}/CLAUDE.md", WT)
        assert d.allow is False
        assert "path-token scan" in d.reason

    def test_13_python_write_open_mode_w(self) -> None:
        d = classify_bash(
            f"""python3 -c "open('{MAIN}/CLAUDE.md','w').write('x')" """, WT
        )
        assert d.allow is False

    def test_14_cd_then_relative_write_still_blocked(self) -> None:
        # cd is exempt on its own, but a write candidate reached AFTER an
        # escaping cd is not — the compensating guard item 4 requires.
        d = classify_bash(f"cd {MAIN} && sed -i s/a/b/ CLAUDE.md", WT)
        assert d.allow is False

    def test_15_output_redirect_outside_worktree(self) -> None:
        d = classify_bash(f"echo x > {MAIN}/CLAUDE.md", WT)
        assert d.allow is False
        assert "output redirect outside worktree" in d.reason

    def test_16_tee_outside_worktree(self) -> None:
        d = classify_bash(f"echo x | tee {MAIN}/CLAUDE.md", WT)
        assert d.allow is False

    def test_17_cp_outside_worktree(self) -> None:
        d = classify_bash(f"cp a.txt {MAIN}/a.txt", WT)
        assert d.allow is False

    def test_18_rm_outside_worktree(self) -> None:
        d = classify_bash(f"rm -f {MAIN}/CLAUDE.md", WT)
        assert d.allow is False

    def test_19_git_checkout_blocked(self) -> None:
        d = classify_bash("git checkout main", WT)
        assert d.allow is False

    def test_20_git_reset_hard_blocked(self) -> None:
        d = classify_bash("git reset --hard HEAD~1", WT)
        assert d.allow is False

    def test_21_gh_pr_merge_blocked(self) -> None:
        d = classify_bash("gh pr merge 123 --squash", WT)
        assert d.allow is False
        assert "merge" in d.reason

    def test_22_classify_path_write_outside_worktree(self) -> None:
        d = classify_path_write(f"{MAIN}/CLAUDE.md", WT)
        assert d.allow is False

    def test_23_git_worktree_add_still_blocked(self) -> None:
        # Scope decision 1 (frozen, not re-litigated here): a DIFFERENT
        # mechanism — the always-blocked git-verb list — blocks this, and
        # it genuinely writes to the parent repo's .git/worktrees/. Out of
        # scope for this fix; pinned so it can't be accidentally unblocked
        # by a future change to the path-token scan.
        d = classify_bash("git worktree add /tmp/wt", WT)
        assert d.allow is False


# ---------------------------------------------------------------------------
# Block message (items 24-25)
# ---------------------------------------------------------------------------


class TestBlockMessage:
    def test_24_reason_names_offending_token(self) -> None:
        d = classify_bash(f"sed -i s/a/b/ {MAIN}/CLAUDE.md", WT)
        assert f"{MAIN}/CLAUDE.md" in d.reason

    def test_25_hook_block_text_names_a_recovery_path(self, capsys) -> None:
        import hooks.sandbox as sandbox_hook

        with pytest.raises(SystemExit) as exc_info:
            sandbox_hook._block(
                "Bash", WT, "some command", "d2246testid", "test reason"
            )
        assert exc_info.value.code == 2
        stderr = capsys.readouterr().err
        assert "Recovery:" in stderr
        assert "mktemp -d" in stderr
        assert "cd ../../.." in stderr
        assert "Do not retry this operation." not in stderr


# ---------------------------------------------------------------------------
# Re-review regressions (security + code review of PR #2265) — glob-shaped
# candidates, heredoc-fed interpreters, and additional python write calls
# all went BLOCK -> ALLOW on the first pass of this fix. Pinned here so a
# later change can't silently reopen any of them.
# ---------------------------------------------------------------------------


class TestGlobCandidatesStillBlocked:
    """A glob is a PATTERN, not a literal path — `_WHOLE_TOKEN_PATH_RE` must
    admit it as a candidate instead of silently dropping it (the character
    class excluded `*`), and the exact directory it expands inside must
    still be judged for worktree containment."""

    @pytest.mark.parametrize(
        "command",
        [
            f"rm -rf {MAIN}/*",
            f"rm -f {MAIN}/*.md",
            f"rm -rf '{MAIN}'/*",
            f"shred -u {MAIN}/*",
            f"rm -f {MAIN}/{{a,b}}.md",  # brace expansion
        ],
    )
    def test_glob_write_outside_worktree_blocked(self, command: str) -> None:
        d = classify_bash(command, WT)
        assert d.allow is False, f"{command!r}: expected BLOCK, got allow"

    def test_exact_path_control_still_blocked(self) -> None:
        # Control: the non-glob sibling of the rows above must also block —
        # proves the fix didn't accidentally rely on glob-shaped text alone.
        d = classify_bash(f"rm -rf {MAIN}/.git", WT)
        assert d.allow is False

    @pytest.mark.parametrize(
        "command",
        [
            f"rm -rf {MAIN}/tui/node_modules/@types",  # npm scope
            f"rm -f '{MAIN}/notes(1).md'",  # parens
            f"rm -f {MAIN}/a+b.md",  # plus
            f"rm -f {MAIN}/a:b.md",  # colon
            f"rm -f {MAIN}/CLAUDE.md~",  # editor backup suffix
        ],
    )
    def test_extra_path_punctuation_still_blocked(self, command: str) -> None:
        d = classify_bash(command, WT)
        assert d.allow is False, f"{command!r}: expected BLOCK, got allow"


class TestHeredocFedInterpreterStillBlocked:
    """`python3 <<EOF` / `bash <<EOF` read the heredoc body AS THEIR OWN
    PROGRAM — stripping it unconditionally (the R5 fix for a heredoc fed to
    an ordinary command like `cat`) throws away the only text describing
    what an interpreter-fed heredoc will do."""

    def test_python_heredoc_write_blocked(self) -> None:
        d = classify_bash(
            f"python3 <<'EOF'\nopen('{MAIN}/CLAUDE.md','w').write('x')\nEOF",
            WT,
        )
        assert d.allow is False

    def test_python_dash_heredoc_write_blocked(self) -> None:
        # `python3 -` (explicit stdin) must be recognised the same as bare
        # `python3`.
        d = classify_bash(
            f"python3 - <<'EOF'\nimport os\nos.remove('{MAIN}/CLAUDE.md')\nEOF",
            WT,
        )
        assert d.allow is False

    def test_bash_heredoc_write_blocked(self) -> None:
        d = classify_bash(
            f"bash <<'EOF'\nrm -f {MAIN}/CLAUDE.md\nEOF", WT
        )
        assert d.allow is False

    def test_sh_heredoc_write_blocked(self) -> None:
        d = classify_bash(
            f"sh <<'EOF'\nrm -f {MAIN}/CLAUDE.md\nEOF", WT
        )
        assert d.allow is False

    def test_bash_heredoc_readonly_stays_allowed(self) -> None:
        # No new over-block: a heredoc-fed shell script that only READS is
        # judged by the same rules as the outer command (`cat` is
        # read-only), not a blind substring rescan that can't tell a read
        # from a write.
        d = classify_bash("bash <<'EOF'\ncat /etc/hostname\nEOF", WT)
        assert d.allow is True, d.reason

    def test_python_heredoc_no_write_call_stays_allowed(self) -> None:
        d = classify_bash(
            f"python3 <<'EOF'\nprint(open('{MAIN}/CLAUDE.md').read())\nEOF",
            WT,
        )
        assert d.allow is True, d.reason

    def test_cat_heredoc_diff_header_still_allowed(self) -> None:
        # R5 lock-in: a heredoc fed to an ORDINARY command (not an
        # interpreter/shell) must still have its body stripped rather than
        # captured — this is the original false positive, not a regression.
        d = classify_bash(
            f"cat <<EOF\n--- {MAIN}/a.py\n+++ {MAIN}/b.py\nEOF", WT
        )
        assert d.allow is True, d.reason


class TestAdditionalPythonWriteCallsStillBlocked:
    """Ordinary, non-obfuscated stdlib calls the first pass of
    `_PY_WRITE_CALL_NAMES` missed — both reviews reproduced these
    independently against real pathlib/os/shutil/zipfile usage."""

    @pytest.mark.parametrize(
        "payload",
        [
            f"from pathlib import Path; Path('{MAIN}/newfile.marker').touch()",
            f"from pathlib import Path; Path('{MAIN}/evil.link').symlink_to('/etc/passwd')",
            f"import os; os.utime('{MAIN}/CLAUDE.md', None)",
            f"import shutil; shutil.copy2('a', '{MAIN}/b')",
            f"import zipfile; zipfile.ZipFile('a.zip').extractall('{MAIN}')",
        ],
    )
    def test_write_call_outside_worktree_blocked(self, payload: str) -> None:
        d = classify_bash(f'''python3 -c "{payload}"''', WT)
        assert d.allow is False, f"{payload!r}: expected BLOCK, got allow"
