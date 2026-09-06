#!/usr/bin/env python3
"""scripts/engine-sync/inbound/changeset.py -- the read-only classify
report's enumeration half.

Pure(-ish) commit enumeration: everything the code plane's `main` has that
`refs/synced/code-plane` (the marker) does not, expressed as commits and the
paths each commit touches.

THE ONE RULE THIS FILE EXISTS TO ENFORCE ("the tree-diff trap"): the change
set comes from `git rev-list marker..remote` plus a per-commit
`git show --name-status` (git's own diff-tree for a single commit, not a
comparison of two trees) and single-path `git rev-parse <ref>:<path>`
look-ups. This module NEVER calls `git diff <a> <b>` between two branch
tips. The two planes' trees differ by whatever the export filter excludes
from one side entirely -- hundreds of files and tens of thousands of lines
that were never "deleted" by any commit -- so a naive two-tree diff between
their branch tips would report that whole export filter as drift and
propose deleting it. Real drift between the marker and the remote tip is a
handful of commits; walking them one at a time, never comparing the two
trees wholesale, is what keeps this file honest.

Every git call takes an explicit `repo_dir` (default REPO_ROOT) so tests can
point this at a disposable scratch repository -- this module never assumes
the caller's cwd.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Matches a GitHub squash-merge commit subject's trailing "(#123)".
#: Deliberately anchored to the end of the string, not a bare search, so a
#: subject that merely *mentions* a PR mid-sentence is not misread as its
#: originating PR.
_PR_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$")


class GitError(RuntimeError):
    """A git plumbing call failed. Never swallowed -- an unreadable
    changeset must surface as an error, not as an empty one (fail closed,
    matching pr_intake_gate.py's own posture)."""


def _git(args: list[str], repo_dir: Path = REPO_ROOT) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}")
    return proc.stdout


def ensure_remote_fetched(remote: str, branch: str, repo_dir: Path = REPO_ROOT) -> None:
    """The one deliberate network call this tool makes: fetch a single
    branch from a single remote so the commits `list_commits`/
    `commit_name_status` below need are actually present as local objects.

    This is NOT staleness.sh's job and does not change it: staleness.sh is
    deliberately fetch-free -- one `git ls-remote`, nothing more -- because
    its whole design point is a near-zero-cost check that runs every loop
    iteration. This report runs far less often and cannot classify anything
    without the real trees, so it fetches explicitly here instead. Running
    this happens to leave the remote's objects fetched locally, which can
    incidentally make a *subsequent* staleness.sh call decidable where it
    previously reported `undecidable` -- but that is a side effect of this
    call, not a fix to staleness.sh's own script: nothing here changes its
    code, its non-fetching contract, or guarantees it will ever be re-run
    after this. Resolving that gap properly (so the alarm is decidable
    right after every merge, not just when something else happens to have
    fetched) is left to whatever applies these commits, not this report.
    """
    _git(["fetch", "--quiet", remote, branch], repo_dir=repo_dir)


def list_commits(marker: str, remote_ref: str, repo_dir: Path = REPO_ROOT) -> list[str]:
    """Commits reachable from *remote_ref* but not from *marker*, oldest
    first. `git rev-list A..B` walks commit parentage -- it is not a tree
    comparison of A's and B's endpoints."""
    out = _git(["rev-list", "--reverse", f"{marker}..{remote_ref}"], repo_dir=repo_dir)
    return [line for line in out.splitlines() if line.strip()]


def commit_subject(sha: str, repo_dir: Path = REPO_ROOT) -> str:
    return _git(["show", "-s", "--format=%s", sha], repo_dir=repo_dir).strip()


def extract_pr_number(subject: str) -> int | None:
    """A commit's subject is attacker-controlled content -- it is whatever
    the PR's own contributor typed, and the code plane's merge settings
    (`allow_merge_commit`, `allow_rebase_merge`, both true) let it reach
    `main` verbatim under either merge method. This is therefore NEVER used
    on its own to decide provenance; report.py's real commit->PR link is
    the GitHub `commits/{sha}/pulls` API, and this parse is consulted only
    as a must-agree cross-check against that -- a subject claiming a PR the
    API does not confirm is itself grounds to refuse, not evidence of
    anything."""
    m = _PR_SUBJECT_RE.search(subject)
    return int(m.group(1)) if m else None


def commit_name_status_detailed(sha: str, repo_dir: Path = REPO_ROOT) -> list[tuple[str, str, bool]]:
    """[(status, path, synthetic), ...] for one commit -- the same walk as
    `commit_name_status` below, plus one bit per entry saying whether git
    reported that path itself or whether this module manufactured the entry.

    Exactly one kind of entry is synthetic: the ("D", old_path) a rename
    contributes on top of its destination path. Everything git printed is
    synthetic=False. That bit is what lets a caller report an honest
    "files changed" figure alongside the (larger) count of paths the gates
    have to rule on -- see build_changeset's `files_changed_count`. Without
    it the two numbers are indistinguishable, and the smaller, more
    intuitive one is the one a reader assumes they are being shown."""
    raw = _git(["show", "--format=", "--name-status", sha], repo_dir=repo_dir)
    out: list[tuple[str, str, bool]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status[0] == "R" and len(parts) == 3:
            out.append((status, parts[2], False))
            out.append(("D", parts[1], True))
        elif status[0] == "C" and len(parts) == 3:
            out.append((status, parts[2], False))
        elif len(parts) >= 2:
            out.append((status, parts[1], False))
    return out


def commit_name_status(sha: str, repo_dir: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """[(status, path), ...] for one commit, via git's own per-commit
    diff-tree (`git show --name-status`), never a two-tree `git diff`.

    A rename (R###) is reported as TWO touches: the new path with its own
    status, AND the old path with status "D" -- a rename is a delete of the
    old path plus a write of the new one, and the old path's content is
    genuinely gone from that path whether or not new content landed
    elsewhere. Reporting only the new path let a delete spelled as a rename
    slip past every deletion-aware check downstream (the ceiling gate's
    out-of-surface-delete refusal in particular) with no "D" status for it
    to ever see. A copy (C###) does NOT get this treatment: the source path
    still exists after a copy, so it is not a deletion.

    Thin wrapper over commit_name_status_detailed: identical entries, with
    the synthetic-vs-real bit dropped. Callers that need to report an
    honest file count want the detailed form."""
    return [(status, path) for status, path, _synthetic in commit_name_status_detailed(sha, repo_dir=repo_dir)]


def commit_numstat(sha: str, repo_dir: Path = REPO_ROOT) -> tuple[int, int]:
    """(insertions, deletions) for one commit, via `git show --numstat`
    (per-commit), never `git diff <a> <b>`."""
    raw = _git(["show", "--format=", "--numstat", sha], repo_dir=repo_dir)
    insertions = deletions = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ins, dele = parts[0], parts[1]
        if ins != "-":
            insertions += int(ins)
        if dele != "-":
            deletions += int(dele)
    return insertions, deletions


def resolve_commit(ref: str, repo_dir: Path = REPO_ROOT) -> str | None:
    """The commit object *ref* names, or None if *ref* does not resolve at
    all. Callers that are about to treat a ref as "the tree to read" (as
    opposed to "one more path that may or may not exist there") must check
    this first: `blob_hash_at` returning None for every path in an unresolvable
    tree is indistinguishable from every path genuinely being absent, which is
    exactly the silent-wrong-answer shape this whole tool exists to avoid."""
    proc = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def blob_hash_at(ref: str, relpath: str, repo_dir: Path = REPO_ROOT) -> str | None:
    """The blob object id of *relpath* at *ref*, or None if it does not
    exist there. A single `<ref>:<path>` lookup -- never a tree walk, never
    a diff."""
    proc = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"{ref}:{relpath}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def build_changeset(marker: str, remote_ref: str, repo_dir: Path = REPO_ROOT) -> dict:
    """The full enumerated changeset: every commit from marker to
    remote_ref, and every path any of them touched, with per-path
    attribution back to the commit(s) that touched it. Read-only -- makes
    no git call that writes anything.

    TWO path counts come out of here, and they are different numbers on any
    change set containing a rename:

      gated_path_count    every distinct path a gate has to rule on. A
                          rename contributes TWO -- its destination, and the
                          synthetic delete of its source (see
                          commit_name_status). This is the figure the
                          ceilings compare against, deliberately: the gates
                          really do have that much to decide, and counting
                          the smaller number would let a rename-heavy change
                          set slip under a ceiling that was sized for the
                          work involved.

      files_changed_count git's own notion -- distinct paths that appear in
                          at least one real `--name-status` line, so a
                          rename counts once. This is what a reader means by
                          "files changed".

    Both are emitted because either one alone is read as the other. The
    field formerly called `touched_path_count` was the first of these
    wearing the second one's name, which is exactly the defect shape this
    whole channel exists to stop shipping."""
    commits = list_commits(marker, remote_ref, repo_dir=repo_dir)
    commit_infos = []
    touched: dict[str, dict] = {}
    real_paths: set[str] = set()
    total_insertions = 0
    total_deletions_lines = 0
    file_deletions = 0

    for sha in commits:
        subject = commit_subject(sha, repo_dir=repo_dir)
        subject_pr_hint = extract_pr_number(subject)  # untrusted -- see extract_pr_number's docstring
        ins, dele = commit_numstat(sha, repo_dir=repo_dir)
        total_insertions += ins
        total_deletions_lines += dele
        commit_infos.append({"sha": sha, "subject": subject, "subject_pr_hint": subject_pr_hint})

        for status, path, synthetic in commit_name_status_detailed(sha, repo_dir=repo_dir):
            entry = touched.setdefault(path, {"commits": [], "statuses": []})
            entry["commits"].append(sha)
            entry["statuses"].append(status)
            if not synthetic:
                real_paths.add(path)
            if status == "D":
                file_deletions += 1

    return {
        "marker": marker,
        "remote_ref": remote_ref,
        "commits": commit_infos,
        "commit_count": len(commits),
        "touched_paths": touched,
        "gated_path_count": len(touched),
        "files_changed_count": len(real_paths),
        "file_deletions": file_deletions,
        "total_insertions": total_insertions,
        "total_deletion_lines": total_deletions_lines,
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- thin CLI wrapper
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Enumerate the inbound changeset (read-only).")
    parser.add_argument("--marker", default="refs/synced/code-plane")
    parser.add_argument("--remote-ref", default="code-plane/main")
    args = parser.parse_args(argv)
    print(json.dumps(build_changeset(args.marker, args.remote_ref), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
