"""Tests for scripts/engine-sync/inbound/changeset.py.

Builds disposable scratch git repos (never the real checkout) to exercise
commit enumeration deterministically, and asserts the enumeration NEVER
calls `git diff <a> <b>` between two branch tips -- the tree-diff trap that
makes commit replay the only safe way to compute this change set.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_INBOUND_DIR = _THIS_DIR.parent / "inbound"
sys.path.insert(0, str(_INBOUND_DIR))

import changeset  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _commit(repo: Path, message: str, files: dict[str, str | None]) -> str:
    """Write/remove *files* ({relpath: content|None-to-delete}) and commit."""
    for relpath, content in files.items():
        full = repo / relpath
        if content is None:
            full.unlink()
            _git(repo, "rm", "-q", relpath)
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def scratch_repo(tmp_path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    # Pin the initial branch name -- git's own default (init.defaultBranch,
    # unset here) is "master" on this host, and at least one test below
    # checks a literal "main".
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_list_commits_and_pr_extraction(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"README.md": "seed\n"})
    _commit(scratch_repo, "add fragments (#3)", {"a.md": "a\n", "b.md": "b\n"})
    tip = _commit(scratch_repo, "fix resolver (#2)", {"c.py": "c\n"})

    commits = changeset.list_commits(seed, "HEAD", repo_dir=scratch_repo)
    assert len(commits) == 2
    assert commits[-1] == tip

    subjects = [changeset.commit_subject(sha, repo_dir=scratch_repo) for sha in commits]
    assert subjects == ["add fragments (#3)", "fix resolver (#2)"]
    assert [changeset.extract_pr_number(s) for s in subjects] == [3, 2]


def test_extract_pr_number_absent():
    assert changeset.extract_pr_number("a commit with no pr reference") is None
    # Must not fire on a PR number merely mentioned mid-sentence.
    assert changeset.extract_pr_number("see (#3) for context, unrelated change") is None


def test_build_changeset_two_commits_eleven_paths_no_deletions(scratch_repo):
    """Two commits touching disjoint path sets: the changeset reports both
    commits, the union of their touched paths, and zero deletions."""
    seed = _commit(scratch_repo, "seed", {"README.md": "seed\n"})
    _commit(
        scratch_repo,
        "tell specialists where the envelope goes (#3)",
        {f"pr3/f{i}.md": f"content {i}\n" for i in range(7)},
    )
    _commit(
        scratch_repo,
        "fail-closed collaborator fetch (#2)",
        {f"pr2/f{i}.py": f"content {i}\n" for i in range(4)},
    )

    cs = changeset.build_changeset(seed, "HEAD", repo_dir=scratch_repo)
    assert cs["commit_count"] == 2
    # No renames here, so the two path counts agree -- which is the point of
    # asserting both: they diverge only where a rename is involved.
    assert cs["gated_path_count"] == 11
    assert cs["files_changed_count"] == 11
    assert cs["file_deletions"] == 0
    assert len([p for p in cs["touched_paths"] if p.startswith("pr3/")]) == 7
    assert len([p for p in cs["touched_paths"] if p.startswith("pr2/")]) == 4


def test_build_changeset_counts_deletions(scratch_repo):
    _commit(scratch_repo, "seed", {"keep.txt": "x\n", "gone.txt": "y\n"})
    seed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(scratch_repo), capture_output=True, text=True
    ).stdout.strip()
    _commit(scratch_repo, "delete one file (#9)", {"gone.txt": None, "new.txt": "z\n"})

    cs = changeset.build_changeset(seed, "HEAD", repo_dir=scratch_repo)
    assert cs["file_deletions"] == 1
    assert cs["touched_paths"]["gone.txt"]["statuses"] == ["D"]


def test_rename_reports_old_path_as_deleted_too(scratch_repo):
    """A rename must not let a delete slip past every deletion-aware check
    downstream: git's --name-status reports a plain rename as R### against
    the NEW path only, with no "D" status anywhere. Without special
    handling, a delete spelled as a rename would carry no "D" status at all
    -- the ceiling gate's out-of-surface-delete refusal, which keys off "D"
    in a path's statuses, would never see it, so a rename is as good as
    deleting a guard file for free."""
    content = "line one\nline two\nline three\nline four\nline five\n"
    _commit(scratch_repo, "seed", {"old.txt": content})
    seed = _git(scratch_repo, "rev-parse", "HEAD").strip()
    _git(scratch_repo, "mv", "old.txt", "new.txt")
    _git(scratch_repo, "commit", "-q", "-m", "rename old to new (#1)")
    tip = _git(scratch_repo, "rev-parse", "HEAD").strip()

    statuses = changeset.commit_name_status(tip, repo_dir=scratch_repo)
    status_by_path: dict[str, list[str]] = {}
    for status, path in statuses:
        status_by_path.setdefault(path, []).append(status)

    assert "D" in status_by_path.get("old.txt", []), f"no D status for the renamed-away path: {status_by_path}"
    assert any(s.startswith("R") for s in status_by_path.get("new.txt", [])), status_by_path

    cs = changeset.build_changeset(seed, "HEAD", repo_dir=scratch_repo)
    assert "old.txt" in cs["touched_paths"]
    assert "D" in cs["touched_paths"]["old.txt"]["statuses"]
    assert cs["file_deletions"] >= 1

    # The honest-headline half. A rename is ONE file changed by git's own
    # count, but TWO paths the gates must rule on. Emitting only the larger
    # figure under a name a reader hears as "files changed" is the defect
    # this asserts against: the two numbers must both be present AND must
    # differ here, so a future change that collapses them back into one is
    # a test failure rather than a silently misleading report.
    assert cs["gated_path_count"] == 2, cs["touched_paths"]
    assert cs["files_changed_count"] == 1, cs["touched_paths"]
    assert cs["gated_path_count"] != cs["files_changed_count"]


def test_copy_does_not_report_source_as_deleted(scratch_repo):
    """The mirror check: a COPY (source still exists afterward) must NOT
    get the rename's synthetic "D" treatment -- only a rename actually
    removes the old path."""
    content = "line one\nline two\nline three\nline four\nline five\n"
    _commit(scratch_repo, "seed", {"src.txt": content})
    seed = _git(scratch_repo, "rev-parse", "HEAD").strip()
    (scratch_repo / "copy.txt").write_text(content)
    _git(scratch_repo, "add", "copy.txt")
    _git(scratch_repo, "commit", "-q", "-m", "copy src to copy (#2)")
    tip = _git(scratch_repo, "rev-parse", "HEAD").strip()

    statuses = changeset.commit_name_status(tip, repo_dir=scratch_repo)
    status_by_path: dict[str, list[str]] = {}
    for status, path in statuses:
        status_by_path.setdefault(path, []).append(status)

    # Whether git detects this as a copy (C###) or a plain add (A) depends
    # on similarity-detection settings; either way "src.txt" (the
    # still-existing source) must never show a "D".
    assert "D" not in status_by_path.get("src.txt", [])


def test_blob_hash_at_present_and_absent(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"a.txt": "hello\n"})
    tip = _commit(scratch_repo, "add b (#1)", {"b.txt": "world\n"})

    assert changeset.blob_hash_at(seed, "a.txt", repo_dir=scratch_repo) is not None
    assert changeset.blob_hash_at(seed, "b.txt", repo_dir=scratch_repo) is None
    assert changeset.blob_hash_at(tip, "b.txt", repo_dir=scratch_repo) is not None
    # Same content at the same path must hash identically across refs.
    assert changeset.blob_hash_at(seed, "a.txt", repo_dir=scratch_repo) == changeset.blob_hash_at(
        tip, "a.txt", repo_dir=scratch_repo
    )


def test_resolve_commit_present_and_absent(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"a.txt": "hello\n"})
    assert changeset.resolve_commit(seed, repo_dir=scratch_repo) == seed
    assert changeset.resolve_commit("main", repo_dir=scratch_repo) == seed
    assert changeset.resolve_commit("refs/heads/definitely-not-a-real-branch", repo_dir=scratch_repo) is None
    assert changeset.resolve_commit("0" * 40, repo_dir=scratch_repo) is None


# ---------------------------------------------------------------------------
# D#2454 PR 2 -- shared-history check (marker_is_ancestor / merge_base)
# ---------------------------------------------------------------------------


def _orphan_root(repo: Path, branch: str, message: str, files: dict[str, str]) -> str:
    """A second, disjoint root commit in the same scratch repo -- models the
    real marker/remote-tip pair, which share no history at all."""
    _git(repo, "checkout", "-q", "--orphan", branch)
    _git(repo, "rm", "-rq", "--cached", ".")
    for p in list(repo.glob("*")):
        if p.name != ".git":
            if p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        sub.unlink()
            else:
                p.unlink()
    return _commit(repo, message, files)


def test_marker_is_ancestor_true_for_healthy_marker(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"a.txt": "1\n"})
    _commit(scratch_repo, "add b (#1)", {"b.txt": "1\n"})
    assert changeset.marker_is_ancestor(seed, "HEAD", repo_dir=scratch_repo) is True


def test_marker_is_ancestor_false_for_disjoint_history(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"a.txt": "1\n"})
    _orphan_root(scratch_repo, "unrelated", "unrelated root", {"z.txt": "z\n"})
    assert changeset.marker_is_ancestor(seed, "unrelated", repo_dir=scratch_repo) is False


def test_marker_is_ancestor_false_when_marker_diverged_from_shared_ancestor(scratch_repo):
    """Shared history is not sufficient -- the marker must be REACHABLE from
    remote_ref. A common ancestor with the marker off on its own branch
    still collapses `A..B` to 'all of B' the same way a wholly disjoint pair
    does, so this must refuse identically."""
    common = _commit(scratch_repo, "common ancestor", {"a.txt": "1\n"})
    _git(scratch_repo, "checkout", "-q", "-b", "marker-branch")
    marker = _commit(scratch_repo, "marker's own commit (#9)", {"marker-only.txt": "m\n"})
    _git(scratch_repo, "checkout", "-q", "main")
    _commit(scratch_repo, "main's own commit (#1)", {"main-only.txt": "n\n"})

    assert changeset.merge_base(marker, "main", repo_dir=scratch_repo) == common
    assert changeset.marker_is_ancestor(marker, "main", repo_dir=scratch_repo) is False


def test_merge_base_present_for_healthy_history(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"a.txt": "1\n"})
    _commit(scratch_repo, "add b (#1)", {"b.txt": "1\n"})
    assert changeset.merge_base(seed, "HEAD", repo_dir=scratch_repo) == seed


def test_merge_base_none_for_disjoint_history(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"a.txt": "1\n"})
    _orphan_root(scratch_repo, "unrelated", "unrelated root", {"z.txt": "z\n"})
    assert changeset.merge_base(seed, "unrelated", repo_dir=scratch_repo) is None


def test_never_calls_two_tree_diff(scratch_repo, tmp_path, monkeypatch):
    """Tree-diff-trap regression guard: a fake `git` earlier on PATH fails
    loudly if invoked with `diff <ref-a> <ref-b>` OR `diff-tree <ref-a>
    <ref-b>` naming two distinct non-triple-dot refs -- both are the same
    two-tree comparison the shape `git diff main code-plane/main` takes,
    and either would manufacture the same phantom-deletion trap. Everything
    else is delegated to the real git so the fixture repo still works."""
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    sentinel = tmp_path / "diff_was_called"

    fake_git_dir = tmp_path / "fakebin"
    fake_git_dir.mkdir()
    fake_git = fake_git_dir / "git"
    fake_git.write_text(
        f"""#!/usr/bin/env bash
if [ "$1" = "diff" ] || [ "$1" = "diff-tree" ]; then
  # Any two-positional-ref invocation (excluding range syntax like A..B or
  # A...B, which is a single argument) is the trap shape, for either
  # subcommand -- diff-tree <a> <b> is the same two-tree comparison as
  # diff <a> <b>, just spelled differently.
  args=("$@")
  positional=()
  for a in "${{args[@]:1}}"; do
    case "$a" in
      -*) ;;
      *) positional+=("$a") ;;
    esac
  done
  if [ "${{#positional[@]}}" -ge 2 ]; then
    touch "{sentinel}"
    echo "FAKE GIT: refusing two-tree diff: $*" >&2
    exit 1
  fi
fi
exec "{real_git}" "$@"
"""
    )
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake_git_dir}:{os.environ['PATH']}")

    seed = _commit(scratch_repo, "seed", {"a.txt": "1\n"})
    _commit(scratch_repo, "change (#1)", {"a.txt": "2\n", "b.txt": "1\n"})

    changeset.build_changeset(seed, "HEAD", repo_dir=scratch_repo)
    assert not sentinel.exists(), "changeset.py invoked a two-tree `git diff` between branch tips"
