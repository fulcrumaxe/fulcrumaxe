"""Tests for backend/spawn_diff.py."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import spawn_diff  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture-repo helper (used by TestFidelity and TestRenderForRefIntegration)
# ---------------------------------------------------------------------------

# A minimal but REAL render CLI: reads <role>.tmpl from its own on-disk
# spawn_templates/ dir (relative to itself), substitutes {{key}} tokens, and
# prints the result. This is what proves fidelity -- it is executed via
# `git worktree add` + subprocess against each commit's own checkout, so its
# output can only reflect that commit's own .tmpl content.
_FIXTURE_TEMPLATES_SCRIPT = textwrap.dedent(
    """\
    import argparse
    import sys
    from pathlib import Path

    KNOWN_ROLES = {"executor"}

    def _main():
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        render_cmd = sub.add_parser("render")
        render_cmd.add_argument("role")
        render_cmd.add_argument("--var", action="append", dest="vars", default=[])
        args = parser.parse_args()
        if args.command != "render":
            return 1
        tmpl_path = Path(__file__).parent / "spawn_templates" / f"{args.role}.tmpl"
        body = tmpl_path.read_text()
        for item in args.vars:
            k, _, v = item.partition("=")
            body = body.replace("{{" + k + "}}", v)
        sys.stdout.write(body)
        return 0

    if __name__ == "__main__":
        sys.exit(_main())
    """
)


def _run_git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args], capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _make_fixture_repo(tmp_path: Path, mark_trusted: bool = True):
    """Build a throwaway git repo with two commits differing only in a
    .tmpl file. Returns (repo_dir, parent_sha, child_sha).

    When mark_trusted, a fake refs/remotes/origin/main is pointed at the
    child commit so both commits pass the ref-trust gate without needing
    --allow-untrusted-ref -- used by tests that are about fidelity, not
    about the gate itself.
    """
    repo_dir = tmp_path / "fixture-repo"
    repo_dir.mkdir()
    _run_git(repo_dir, "init", "-q")
    _run_git(repo_dir, "config", "user.email", "test@example.com")
    _run_git(repo_dir, "config", "user.name", "Test")

    backend_dir = repo_dir / "backend"
    backend_dir.mkdir()
    (backend_dir / "spawn_templates.py").write_text(_FIXTURE_TEMPLATES_SCRIPT)
    tmpl_dir = backend_dir / "spawn_templates"
    tmpl_dir.mkdir()
    (tmpl_dir / "executor.tmpl").write_text("VERSION=parent\n")

    _run_git(repo_dir, "add", "-A")
    _run_git(repo_dir, "commit", "-q", "-m", "parent")
    parent_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    (tmpl_dir / "executor.tmpl").write_text("VERSION=child\n")
    _run_git(repo_dir, "add", "-A")
    _run_git(repo_dir, "commit", "-q", "-m", "child")
    child_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    if mark_trusted:
        _run_git(repo_dir, "update-ref", "refs/remotes/origin/main", child_sha)

    return repo_dir, parent_sha, child_sha


# ---------------------------------------------------------------------------
# Existing surface: args and context
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_defaults(self):
        args = spawn_diff._parse_args(["--role", "executor"])
        assert args.role == "executor"
        assert args.base == "main"
        assert args.head == "HEAD"
        assert args.context_file is None
        assert args.allow_untrusted_ref is False

    def test_custom_refs(self):
        args = spawn_diff._parse_args(
            ["--role", "code-reviewer", "--base", "abc123", "--head", "def456"]
        )
        assert args.base == "abc123"
        assert args.head == "def456"

    def test_allow_untrusted_ref_flag(self):
        args = spawn_diff._parse_args(
            ["--role", "executor", "--allow-untrusted-ref"]
        )
        assert args.allow_untrusted_ref is True


class TestLoadContext:
    def test_returns_default_fixture_when_no_file(self):
        ctx = spawn_diff._load_context(None)
        assert "discussion_number" in ctx
        assert "project_context" in ctx

    def test_loads_json_file(self, tmp_path):
        data = {"project_context": "from file", "discussion_number": "1"}
        p = tmp_path / "ctx.json"
        p.write_text(json.dumps(data))
        ctx = spawn_diff._load_context(str(p))
        assert ctx["project_context"] == "from file"

    def test_exits_1_missing_file(self):
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._load_context("/nonexistent/path.json")
        assert exc_info.value.code == 1

    def test_exits_1_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not-json")
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._load_context(str(p))
        assert exc_info.value.code == 1

    def test_exits_1_on_nested_value(self, tmp_path):
        p = tmp_path / "nested.json"
        p.write_text(json.dumps({"project_context": {"nested": "value"}}))
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._load_context(str(p))
        assert exc_info.value.code == 1

    def test_exits_1_on_nested_list_value(self, tmp_path):
        p = tmp_path / "nested_list.json"
        p.write_text(json.dumps({"project_context": ["a", "b"]}))
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._load_context(str(p))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Item 6: credential-scrubbed subprocess environment
# ---------------------------------------------------------------------------


class TestBuildSubprocessEnv:
    def test_scrubs_credential_and_state_dir_keys(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "shh")
        monkeypatch.setenv("GITHUB_TOKEN", "also-shh")
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", "/some/state/dir")
        env = spawn_diff._build_subprocess_env()
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert "AUTONOMOUS_TEAM_STATE_DIR" not in env

    def test_sets_fixture_repo_slug(self):
        env = spawn_diff._build_subprocess_env()
        assert env["AUTONOMOUS_TEAM_REPO"] == spawn_diff._FIXTURE_REPO_SLUG

    def test_does_not_scrub_unrelated_keys(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        env = spawn_diff._build_subprocess_env()
        assert env.get("PATH") == "/usr/bin"


# ---------------------------------------------------------------------------
# Item 1/2: the ref-trust gate, and its ordering relative to any git
# operation that would materialize the untrusted ref's content.
# ---------------------------------------------------------------------------


class TestRefTrustGate:
    def test_resolve_sha_exits_1_on_bad_ref(self):
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff._resolve_sha("not-a-real-ref-xyz")
        assert exc_info.value.code == 1

    def test_is_trusted_ref_true_when_ancestor_of_remote(self, tmp_path):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            assert spawn_diff._is_trusted_ref(parent_sha) is True
            assert spawn_diff._is_trusted_ref(child_sha) is True

    def test_is_trusted_ref_false_when_no_remote_contains_it(self, tmp_path):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(
            tmp_path, mark_trusted=False
        )
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            assert spawn_diff._is_trusted_ref(child_sha) is False

    def test_untrusted_ref_refused_without_reaching_worktree_or_render(self, tmp_path):
        """Ordering proof: the gate must run and refuse BEFORE any
        git operation that would materialize the untrusted ref's content.
        We patch subprocess.run so that any 'worktree' or the render CLI
        invocation raises -- if the gate is bypassed, this test fails loudly
        instead of silently passing.
        """
        repo_dir, parent_sha, child_sha = _make_fixture_repo(
            tmp_path, mark_trusted=False
        )

        real_run = subprocess.run

        def guarded_run(cmd, *a, **kw):
            if "worktree" in cmd or cmd[0] == sys.executable:
                raise AssertionError(
                    f"gate was bypassed -- reached worktree/render for an "
                    f"untrusted ref: {cmd}"
                )
            return real_run(cmd, *a, **kw)

        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir), \
                patch("backend.spawn_diff.subprocess.run", side_effect=guarded_run):
            with pytest.raises(SystemExit) as exc_info:
                spawn_diff._render_for_ref(
                    child_sha, "executor", {}, "untrusted-head",
                    allow_untrusted_ref=False,
                )
        assert exc_info.value.code == 3

    def test_allow_untrusted_ref_warns_and_proceeds(self, tmp_path, capsys):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(
            tmp_path, mark_trusted=False
        )
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            result = spawn_diff._render_for_ref(
                child_sha, "executor", {}, "untrusted-head",
                allow_untrusted_ref=True,
            )
        assert "VERSION=child" in result
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert child_sha in captured.err


# ---------------------------------------------------------------------------
# Item 4: disk arm is labelled, not silently inherited.
# ---------------------------------------------------------------------------


class TestDiskArm:
    def test_head_prints_unchecked_note_and_no_gate_call(self, tmp_path, capsys):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir), \
                patch.object(spawn_diff, "_is_trusted_ref") as mock_trust:
            result = spawn_diff._render_for_ref(
                "HEAD", "executor", {}, "HEAD (working tree)",
                allow_untrusted_ref=False,
            )
        mock_trust.assert_not_called()
        assert "VERSION=child" in result  # disk state is the child commit
        captured = capsys.readouterr()
        assert "NOT checked against the ref-trust gate" in captured.err

    def test_working_tree_alias_also_unchecked(self, tmp_path, capsys):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir), \
                patch.object(spawn_diff, "_is_trusted_ref") as mock_trust:
            spawn_diff._render_for_ref(
                "working-tree", "executor", {}, "working-tree",
                allow_untrusted_ref=False,
            )
        mock_trust.assert_not_called()
        captured = capsys.readouterr()
        assert "NOT checked against the ref-trust gate" in captured.err

    def test_git_ref_path_does_not_print_disk_note(self, tmp_path, capsys):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            spawn_diff._render_for_ref(
                child_sha, "executor", {}, "child",
                allow_untrusted_ref=False,
            )
        captured = capsys.readouterr()
        assert "NOT checked against the ref-trust gate" not in captured.err


# ---------------------------------------------------------------------------
# Item 5/7: process boundary, cleanup, and no silent fallback.
# ---------------------------------------------------------------------------


class TestProcessBoundary:
    def test_trusted_ref_renders_via_worktree_and_cleans_up(self, tmp_path):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            result = spawn_diff._render_for_ref(
                child_sha, "executor", {}, "child",
                allow_untrusted_ref=False,
            )
            assert "VERSION=child" in result
            worktree_list = subprocess.run(
                ["git", "-C", str(repo_dir), "worktree", "list"],
                capture_output=True, text=True,
            ).stdout
        # Only the main worktree should remain -- spawn_diff's own detached
        # worktree must have been removed.
        assert worktree_list.strip().count("\n") == 0

    def test_render_cli_failure_exits_2_no_fallback(self, tmp_path):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        # Break the fixture's render CLI so the subprocess exits non-zero.
        (repo_dir / "backend" / "spawn_templates.py").write_text("import sys; sys.exit(1)")
        _run_git(repo_dir, "add", "-A")
        _run_git(repo_dir, "commit", "-q", "-m", "broken render CLI")
        broken_sha = _run_git(repo_dir, "rev-parse", "HEAD")
        _run_git(repo_dir, "update-ref", "refs/remotes/origin/main", broken_sha)

        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            with pytest.raises(SystemExit) as exc_info:
                spawn_diff._render_for_ref(
                    broken_sha, "executor", {}, "broken",
                    allow_untrusted_ref=False,
                )
        assert exc_info.value.code == 2

    def test_worktree_removed_even_on_render_failure(self, tmp_path):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        (repo_dir / "backend" / "spawn_templates.py").write_text("import sys; sys.exit(1)")
        _run_git(repo_dir, "add", "-A")
        _run_git(repo_dir, "commit", "-q", "-m", "broken render CLI")
        broken_sha = _run_git(repo_dir, "rev-parse", "HEAD")
        _run_git(repo_dir, "update-ref", "refs/remotes/origin/main", broken_sha)

        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            with pytest.raises(SystemExit):
                spawn_diff._render_for_ref(
                    broken_sha, "executor", {}, "broken",
                    allow_untrusted_ref=False,
                )
        worktree_list = subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "list"],
            capture_output=True, text=True,
        ).stdout
        assert worktree_list.strip().count("\n") == 0


# ---------------------------------------------------------------------------
# Item 8: fidelity -- output tracks the commit's own template, never the
# working tree's.
# ---------------------------------------------------------------------------


class TestFidelity:
    def test_render_follows_commit_not_working_tree(self, tmp_path, capsys):
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        # The on-disk working tree currently holds the CHILD .tmpl content
        # (that's what _make_fixture_repo leaves checked out). Diffing
        # parent vs child by sha must show the .tmpl change even though
        # neither side is "HEAD" / the working tree.
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            spawn_diff.main(
                ["--role", "executor", "--base", parent_sha, "--head", child_sha]
            )
        captured = capsys.readouterr()
        assert "-VERSION=parent" in captured.out
        assert "+VERSION=child" in captured.out

    def test_head_vs_parent_matches_working_tree_state(self, tmp_path, capsys):
        """Sanity check on the same fixture: HEAD (disk) equals the child
        commit's content, proving the earlier fixture is set up as intended.
        """
        repo_dir, parent_sha, child_sha = _make_fixture_repo(tmp_path)
        with patch.object(spawn_diff, "_REPO_ROOT", repo_dir):
            spawn_diff.main(
                ["--role", "executor", "--base", parent_sha, "--head", "HEAD"]
            )
        captured = capsys.readouterr()
        assert "-VERSION=parent" in captured.out
        assert "+VERSION=child" in captured.out


# ---------------------------------------------------------------------------
# main(): diff plumbing, unaffected by the security rework.
# ---------------------------------------------------------------------------


class TestMain:
    """Integration tests that stub _render_for_ref directly."""

    def _run_main(self, argv: list[str], base_out: str, head_out: str) -> None:
        # Dispatch on the ref value passed in argv, matching how main() calls
        # _render_for_ref once for --base and once for --head.
        base_ref = None
        head_ref = None
        for i, a in enumerate(argv):
            if a == "--base":
                base_ref = argv[i + 1]
            if a == "--head":
                head_ref = argv[i + 1]
        base_ref = base_ref or "main"
        head_ref = head_ref or "HEAD"

        def dispatch(ref, role, context, ref_label, allow_untrusted_ref):
            return base_out if ref == base_ref and ref != head_ref else head_out

        with patch("backend.spawn_diff._render_for_ref", side_effect=dispatch):
            spawn_diff.main(argv)

    def test_diff_when_templates_differ(self, capsys):
        self._run_main(
            ["--role", "executor", "--base", "main", "--head", "HEAD"],
            "ROLE=executor\nVERSION=base\n",
            "ROLE=executor\nVERSION=head\n",
        )
        captured = capsys.readouterr()
        assert "-VERSION=base" in captured.out
        assert "+VERSION=head" in captured.out

    def test_diff_shows_plus_minus_markers(self, capsys):
        self._run_main(
            ["--role", "executor", "--base", "main", "--head", "HEAD"],
            "ROLE=executor\nVERSION=base\n",
            "ROLE=executor\nVERSION=head\n",
        )
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        has_plus = any(line.startswith("+") and not line.startswith("+++") for line in lines)
        has_minus = any(line.startswith("-") and not line.startswith("---") for line in lines)
        assert has_plus and has_minus

    def test_empty_diff_when_identical(self, capsys):
        same = "ROLE=executor\nVERSION=same\n"
        self._run_main(
            ["--role", "executor", "--base", "main", "--head", "HEAD"], same, same
        )
        captured = capsys.readouterr()
        assert "(no diff" in captured.out

    def test_unknown_role_exits_1(self):
        with pytest.raises(SystemExit) as exc_info:
            spawn_diff.main(["--role", "not-a-role"])
        assert exc_info.value.code == 1

    def test_context_file_passed_through_to_render_for_ref(self, tmp_path, capsys):
        ctx_data = dict(spawn_diff._DEFAULT_FIXTURE)
        ctx_data["project_context"] = "custom-ctx-marker"
        ctx_file = tmp_path / "ctx.json"
        ctx_file.write_text(json.dumps(ctx_data))

        seen_contexts = []

        def fake_render(ref, role, context, ref_label, allow_untrusted_ref):
            seen_contexts.append(context)
            return "same\n"

        with patch("backend.spawn_diff._render_for_ref", side_effect=fake_render):
            spawn_diff.main(
                [
                    "--role", "executor",
                    "--base", "main",
                    "--head", "HEAD",
                    "--context-file", str(ctx_file),
                ]
            )
        captured = capsys.readouterr()
        assert "(no diff" in captured.out
        assert all(c["project_context"] == "custom-ctx-marker" for c in seen_contexts)


# ---------------------------------------------------------------------------
# Item 3: no in-process execution of ref-derived source anywhere in the file.
# ---------------------------------------------------------------------------


class TestNoInProcessExecution:
    def test_no_exec_module_or_dynamic_import_symbols(self):
        source = (_REPO_ROOT / "backend" / "spawn_diff.py").read_text()
        for banned in ("exec_module", "spec_from_file_location", "_render_from_source"):
            assert banned not in source, f"found banned symbol '{banned}' in spawn_diff.py"

    def test_no_importlib_or_types_import(self):
        source = (_REPO_ROOT / "backend" / "spawn_diff.py").read_text()
        assert "import importlib" not in source
        assert "import types" not in source


# ---------------------------------------------------------------------------
# Item 9: "no automated callers" is a mechanical fact.
#
# Expiry condition: the case for treating a missed caller here as low-risk
# rather than actively dangerous rests on 194 PRs on the resolved code plane
# (fulcrumaxe/fulcrumaxe) with 194 isCrossRepository:false, measured
# 2026-09-12 via `gh pr list --state all --limit 300` from the operator
# checkout. That count is void once the first fork PR lands -- after that
# day this test (and the gate it protects) still has to hold, but the
# "nobody automated this yet" half of the reasoning does not.
# ---------------------------------------------------------------------------


class TestNoAutomatedCallers:
    _ALLOWED_PREFIXES = (
        "backend/spawn_diff.py",
        "backend/tests/test_spawn_diff.py",
        "archive/",
        "open-source/",
    )

    def test_spawn_diff_has_no_unexpected_callers(self):
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "grep", "-l", "spawn_diff"],
            capture_output=True, text=True,
        )
        # git grep exits 1 when there are no matches at all -- that is a pass.
        assert result.returncode in (0, 1), f"git grep failed: {result.stderr}"
        matches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        unexpected = [
            m for m in matches
            if not any(m == p or m.startswith(p) for p in self._ALLOWED_PREFIXES)
        ]
        assert not unexpected, f"unexpected spawn_diff caller(s): {unexpected}"


# ---------------------------------------------------------------------------
# Item 10: expiry condition carried in the module docstring.
# ---------------------------------------------------------------------------


class TestExpiryConditionInDocstring:
    def test_docstring_carries_measured_count_scope_date_and_source(self):
        doc = spawn_diff.__doc__ or ""
        assert "194" in doc
        assert "isCrossRepository" in doc
        assert "2026-09-12" in doc
        assert "fork PR" in doc
