"""Tests for scripts/engine-sync/inbound/changeset.py -- D#2439 Slice B.

Builds disposable scratch git repos (never the real checkout) to exercise
commit enumeration deterministically, and asserts the enumeration NEVER
calls `git diff <a> <b>` between two branch tips -- the tree-diff trap B2
calls "the single most important test in the Spec."
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
    _git(repo, "init", "-q")
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


def test_build_changeset_matches_b1_shape(scratch_repo):
    """Mirrors the Spec's B1: 2 commits, 11 touched paths (7 + 4), 0 deletions."""
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
    assert cs["touched_path_count"] == 11
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


def test_never_calls_two_tree_diff(scratch_repo, tmp_path, monkeypatch):
    """B2's tree-diff-trap regression guard: a fake `git` earlier on PATH
    fails loudly if invoked with `diff <ref-a> <ref-b>` naming two distinct
    non-triple-dot refs -- the shape `git diff main code-plane/main` takes.
    Everything else is delegated to the real git so the fixture repo still
    works."""
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    sentinel = tmp_path / "diff_was_called"

    fake_git_dir = tmp_path / "fakebin"
    fake_git_dir.mkdir()
    fake_git = fake_git_dir / "git"
    fake_git.write_text(
        f"""#!/usr/bin/env bash
if [ "$1" = "diff" ]; then
  # Any two-positional-ref diff invocation (excluding range syntax like
  # A..B or A...B, which is a single argument) is the trap shape.
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
