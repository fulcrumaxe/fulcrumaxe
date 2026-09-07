"""Tests for scripts/engine-sync/inbound/unmerged.py (D#2445).

Every test drives the real module against disposable scratch repositories:
an "engine" checkout (what `local_ref` means) and a separate "remote" bare
repo standing in for the engine's own `origin` -- exactly the two-repo
shape apply_inbound.py pushes `engine-sync/inbound-*` branches into.

Nothing here imports or exercises apply_inbound.py itself -- D#2445's scope
excludes it (see unmerged.py's own module docstring); these branches are
built by hand to look like ones it would have produced.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_INBOUND_DIR = _THIS_DIR.parent / "inbound"
sys.path.insert(0, str(_INBOUND_DIR))

import unmerged  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _write(repo: Path, relpath: str, content: str) -> None:
    full = repo / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for relpath, content in files.items():
        _write(repo, relpath, content)
        _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def engine(tmp_path) -> dict:
    """`engine` (a checkout, local_ref="main") plus `origin` (a bare repo
    engine's own `origin` remote points at) with nothing pushed yet."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))

    repo = tmp_path / "engine"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "engine@example.com")
    _git(repo, "config", "user.name", "Engine")
    base_sha = _commit(repo, "seed", {"backend/shared.py": "shared v1\n", "backend/other.py": "other v1\n"})
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")

    return {"repo": repo, "origin": origin, "base_sha": base_sha}


def _push_sync_branch(engine_repo: Path, origin: Path, branch: str, base_sha: str, files: dict[str, str]) -> str:
    """Build a commit on top of *base_sha* carrying *files* and push it to
    *origin* as *branch* -- via a private temporary index, never a
    checkout, exactly the shape apply_inbound.py's own build_branch_commits
    uses to avoid touching engine_repo's real working tree, index, or HEAD
    (see its module docstring)."""
    index_file = engine_repo.parent / f"index-{branch.replace('/', '-')}"
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index_file)

    def run(*args: str, input_text: str | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=str(engine_repo), env=env, input=input_text, capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
        return proc.stdout

    run("read-tree", base_sha)
    for relpath, content in files.items():
        blob = run("hash-object", "-w", "--stdin", input_text=content).strip()
        run("update-index", "--add", "--cacheinfo", f"100644,{blob},{relpath}")
    tree = run("write-tree").strip()
    branch_sha = run("commit-tree", tree, "-p", base_sha, "-m", f"sync {branch}").strip()
    _git(engine_repo, "push", "-q", str(origin), f"{branch_sha}:refs/heads/{branch}")
    index_file.unlink(missing_ok=True)
    return branch_sha


# ---------------------------------------------------------------------------


def test_no_open_branches_is_zero_not_undecidable(engine):
    result = unmerged.compute_unmerged_debt(remote="origin", local_ref="main", repo_dir=engine["repo"])
    assert result == {"paths": [], "count": 0, "branches": []}


def test_one_open_branch_one_missing_path(engine):
    _push_sync_branch(
        engine["repo"],
        engine["origin"],
        "engine-sync/inbound-aaa111",
        engine["base_sha"],
        {"backend/new.py": "brand new from the sync branch\n"},
    )
    result = unmerged.compute_unmerged_debt(remote="origin", local_ref="main", repo_dir=engine["repo"])
    assert result["count"] == 1
    assert result["paths"] == ["backend/new.py"]
    assert result["branches"] == ["engine-sync/inbound-aaa111"]


def test_path_already_resolved_on_local_ref_does_not_count(engine):
    """The branch offered `backend/new.py`; local_ref independently ended up
    with the SAME content (someone applied it by hand, say). Missing means
    'different content', not 'touched by the branch'."""
    _push_sync_branch(
        engine["repo"],
        engine["origin"],
        "engine-sync/inbound-bbb222",
        engine["base_sha"],
        {"backend/new.py": "identical content\n"},
    )
    _commit(engine["repo"], "someone applied it independently", {"backend/new.py": "identical content\n"})
    result = unmerged.compute_unmerged_debt(remote="origin", local_ref="main", repo_dir=engine["repo"])
    assert result == {"paths": [], "count": 0, "branches": ["engine-sync/inbound-bbb222"]}


def test_local_ref_moved_forward_on_unrelated_work_is_not_counted(engine):
    """This is the tree-diff-trap-shaped failure mode this module's
    docstring exists to avoid: a naive tip-to-tip diff between the branch
    and a since-advanced local_ref would report `backend/other.py` (touched
    only by unrelated, later engine work) as 'missing' too. It must not."""
    _push_sync_branch(
        engine["repo"],
        engine["origin"],
        "engine-sync/inbound-ccc333",
        engine["base_sha"],
        {"backend/new.py": "brand new from the sync branch\n"},
    )
    _commit(engine["repo"], "unrelated later engine work", {"backend/other.py": "other v2, nothing to do with sync\n"})
    result = unmerged.compute_unmerged_debt(remote="origin", local_ref="main", repo_dir=engine["repo"])
    assert result["count"] == 1
    assert result["paths"] == ["backend/new.py"]


def test_two_open_branches_aggregate_unique_paths(engine):
    _push_sync_branch(
        engine["repo"], engine["origin"], "engine-sync/inbound-ddd444", engine["base_sha"], {"backend/a.py": "a\n"}
    )
    _push_sync_branch(
        engine["repo"], engine["origin"], "engine-sync/inbound-eee555", engine["base_sha"], {"backend/b.py": "b\n"}
    )
    result = unmerged.compute_unmerged_debt(remote="origin", local_ref="main", repo_dir=engine["repo"])
    assert result["count"] == 2
    assert result["paths"] == ["backend/a.py", "backend/b.py"]
    assert set(result["branches"]) == {"engine-sync/inbound-ddd444", "engine-sync/inbound-eee555"}


def test_unresolvable_local_ref_raises_rather_than_guessing_zero(engine):
    with pytest.raises(unmerged.GitError):
        unmerged.compute_unmerged_debt(remote="origin", local_ref="does-not-exist", repo_dir=engine["repo"])


def test_cli_prints_json_and_exits_zero(engine, capsys):
    rc = unmerged.main(["--remote", "origin", "--local-ref", "main", "--repo-dir", str(engine["repo"])])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"count": 0' in out


def test_cli_reports_error_and_exits_nonzero_on_bad_local_ref(engine, capsys):
    rc = unmerged.main(["--remote", "origin", "--local-ref", "nope", "--repo-dir", str(engine["repo"])])
    assert rc == 1
    out = capsys.readouterr().out
    assert '"error"' in out
