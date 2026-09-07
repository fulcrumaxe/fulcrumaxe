#!/usr/bin/env python3
"""scripts/engine-sync/inbound/unmerged.py -- read-only: how many paths an
open `engine-sync/inbound-*` branch already carries that the engine's own
`local_ref` does not yet have.

Exists because `engine-sync-inbound-apply.json`'s `pending` only ever
records paths apply_inbound.py explicitly WITHHELD. A path it applied --
committed to a branch, pushed, PR opened -- never enters that file at all,
so a run that writes nine paths into a sync PR nobody merges leaves no
trace in `pending`. Those nine paths are exactly as absent from the engine
as anything withheld; this module answers "how many, and which", from git
alone, without reading or writing apply_inbound.py's state file and without
importing anything from apply_inbound.py itself (D#2445; the apply path's
own fix is D#2454, a separate, concurrent change to the same directory).

NEVER a `git diff <branch tip> <local_ref tip>` of the two full trees. A
sync branch is built against whatever `local_ref` pointed at when
apply_inbound.py ran; by the time this checks, `local_ref` may have moved
forward on completely unrelated work, and a tip-to-tip diff would report
every one of those unrelated commits as "missing" too -- a false positive
in the other direction from the tree-diff trap changeset.py's own docstring
describes, but the same shape of mistake. Every comparison here is instead
merge-base-relative (what the branch's OWN commits actually touched),
followed by a per-path blob comparison against `local_ref`'s CURRENT tip.
Re-derives blob comparison via changeset.blob_hash_at rather than a second
implementation of the same one-path git lookup.

Writes no ref, no branch, no HEAD, and touches neither the working tree nor
the index -- the same contract alarm.sh's own header claims for itself.
Fetching a branch tip's objects is unavoidable (their content has to be
read) and is the one thing this module does that staleness.sh's sibling
check does not; see `fetch_commit`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_INBOUND_DIR = Path(__file__).resolve().parent
for _p in (str(_INBOUND_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import changeset  # noqa: E402

BRANCH_PATTERN = "engine-sync/inbound-*"


class GitError(RuntimeError):
    """A git plumbing call failed, or came back in a shape this module
    cannot read as an answer. Never swallowed into a guessed zero -- an
    undecidable comparison must surface as an error to the caller, which
    decides what "we could not tell" means for its own output."""


def _git(args: list[str], repo_dir: Path, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}")
    return proc


def list_open_branch_tips(remote: str, repo_dir: Path) -> dict[str, str]:
    """{branch_name: sha} for every `engine-sync/inbound-*` head *remote*
    currently advertises. `ls-remote` fetches nothing -- this is exactly as
    cheap as staleness.sh's own check."""
    proc = _git(["ls-remote", "--heads", remote, BRANCH_PATTERN], repo_dir=repo_dir)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        sha, ref = line.split("\t", 1)
        out[ref[len("refs/heads/") :]] = sha
    return out


def fetch_commit(remote: str, sha: str, repo_dir: Path) -> None:
    """Fetch one commit's objects by sha, writing no ref. `sha` is always
    one `list_open_branch_tips` just returned -- an advertised branch tip
    the remote already offered -- never an arbitrary, unadvertised sha."""
    _git(["fetch", "--quiet", remote, sha], repo_dir=repo_dir)


def missing_paths_on_branch(local_ref: str, branch_sha: str, repo_dir: Path) -> list[str]:
    """Paths *branch_sha* carries that *local_ref* does not yet have,
    relative to where the branch actually diverged from *local_ref* --
    never a straight tip-to-tip diff (see module docstring)."""
    base = _git(["merge-base", local_ref, branch_sha], repo_dir=repo_dir, check=False)
    if base.returncode != 0 or not base.stdout.strip():
        raise GitError(f"no merge-base between {local_ref!r} and {branch_sha!r}")
    merge_base = base.stdout.strip()

    candidates = _git(["diff", "--name-only", merge_base, branch_sha, "--"], repo_dir=repo_dir).stdout.splitlines()
    missing = []
    for path in candidates:
        path = path.strip()
        if not path:
            continue
        branch_blob = changeset.blob_hash_at(branch_sha, path, repo_dir=repo_dir)
        local_blob = changeset.blob_hash_at(local_ref, path, repo_dir=repo_dir)
        if branch_blob != local_blob:
            missing.append(path)
    return missing


def compute_unmerged_debt(*, remote: str, local_ref: str, repo_dir: Path) -> dict:
    """{"paths": [...], "count": N} for every path an open
    `engine-sync/inbound-*` branch carries that `local_ref` does not.
    Raises GitError on any failure rather than guessing zero."""
    if changeset.resolve_commit(local_ref, repo_dir=repo_dir) is None:
        raise GitError(f"local_ref does not resolve to a commit: {local_ref!r}")
    tips = list_open_branch_tips(remote, repo_dir=repo_dir)
    missing: set[str] = set()
    for sha in tips.values():
        fetch_commit(remote, sha, repo_dir=repo_dir)
        missing.update(missing_paths_on_branch(local_ref, sha, repo_dir=repo_dir))
    return {"paths": sorted(missing), "count": len(missing), "branches": sorted(tips)}


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(description="Count paths an open engine-sync/inbound-* branch has that local_ref does not.")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--local-ref", default="main")
    parser.add_argument("--repo-dir", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    import json

    args = build_parser().parse_args(argv)
    try:
        result = compute_unmerged_debt(remote=args.remote, local_ref=args.local_ref, repo_dir=Path(args.repo_dir))
    except GitError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
