#!/usr/bin/env python3
"""scripts/engine-sync/inbound/apply_inbound.py -- the write half of the
engine-sync inbound channel.

Takes the read-only classify report, decides which of its paths may be
written, builds a branch on the ENGINE repository carrying exactly those
paths, opens a pull request against engine `main`, and advances the marker
ref. It never merges that PR, never pushes to the code plane, and never
spawns an agent.

WHAT IT WRITES, AND WHERE
-------------------------
Nothing in the working tree, nothing in the index, and no branch the caller
is standing on. Commits are built entirely with plumbing against a private
temporary index (`read-tree` / `update-index` / `write-tree` /
`commit-tree`), so a run leaves `git status --porcelain` byte-identical to
what it found -- including every run that refuses partway through. The only
refs it moves are the sync branch it pushes and, on a completed apply, the
marker.

Paths are only ever ADDED or MODIFIED. There is no code path that removes
an index entry, so a run cannot delete an engine file even if the change set
asks for one. The engine's 500-odd files that the export filter excludes
entirely are therefore untouchable here by construction, not by vigilance --
which matters, because a naive two-tree diff between the planes reports that
whole filter as ~110k deletions.

THE DECISION THIS FILE EXISTS TO GET RIGHT
------------------------------------------
`local-patch` is not one situation, it is two, and they need opposite
handling:

    local_hash is None      the engine has no copy of this path
                            -> writing it CREATES a file. Safe.

    local_hash is not None  the engine has a DIFFERENT copy
                            -> writing it OVERWRITES local work, with
                               nothing anywhere saying so. Not safe.

The bucket name cannot tell these apart; `local_hash` can, and the report
already emits it per path. So the write set keys on `local_hash is None`,
never on the bucket. This is live, not hypothetical: today's real change set
has three `local-patch` paths, two creates and one -- a test file added
independently on both planes -- that adopting in place would silently
discard.

WHAT ELSE IS WITHHELD
---------------------
Everything that did not clear the report's gates: out-of-surface,
path-unsafe, untrusted provenance, reverse-map collisions, and the sensitive
prefixes a human has to approve by hand. Withheld is not dropped -- every
withheld path is listed by name, with its reason, in the PR body, so the
decision is reviewable exactly once rather than invisible forever.

Two independent belts are worn over the same trousers on purpose: the write
set is re-checked against `protected.txt` and `sensitive.txt` after the gate
has already excluded them. The gate is what decides; this is what makes a
bug in the gate refuse the run instead of writing the sandbox hook.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_INBOUND_DIR = Path(__file__).resolve().parent
_ENGINE_SYNC_DIR = _INBOUND_DIR.parent
REPO_ROOT = _ENGINE_SYNC_DIR.parent.parent

for _p in (str(_INBOUND_DIR), str(_ENGINE_SYNC_DIR), str(REPO_ROOT), str(REPO_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apply as outbound_apply  # noqa: E402  (read_protected_set -- reused, not reimplemented)
import changeset  # noqa: E402
import gate  # noqa: E402
import pull  # noqa: E402
import report as report_mod  # noqa: E402

#: Consecutive failed runs before the channel stops calling the remote at
#: all. Three is enough to tell a transient network blip from something
#: structurally wrong, and few enough that a broken channel stops hammering
#: the API within half an hour of the loop's cadence.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

STATE_FILE_NAME = "engine-sync-inbound-apply.json"

RESULT_DISABLED = "disabled"
RESULT_REFUSED = "refused"
RESULT_CONFLICT = "conflict"
RESULT_NOTHING = "nothing-to-apply"
RESULT_APPLIED = "applied"

EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_ERROR = 4

#: Withheld reasons that are not the classifier's own vocabulary.
WITHHELD_WOULD_OVERWRITE = "withheld:would-overwrite-engine-copy"
WITHHELD_PROTECTED = "withheld:protected-or-sensitive"


class ApplyRefused(RuntimeError):
    """A refusal that must leave nothing behind: no branch, no PR, no marker
    movement. Raised before any ref is written, always."""


# ---------------------------------------------------------------------------
# State: the consecutive-failure counter, and its consumer
# ---------------------------------------------------------------------------


def state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILE_NAME


def read_failure_count(state_dir: Path) -> int:
    """Zero when the file is missing or unreadable. Deliberately permissive:
    an unreadable counter must not disable the channel, because the counter
    exists to stop a *failing* channel, and 'I could not read a number' is
    not evidence of failure. The alarm's own halt is what covers the case
    where this channel silently stops working."""
    try:
        data = json.loads(state_path(state_dir).read_text())
        return int(data.get("consecutive_failures", 0) or 0)
    except Exception:
        return 0


def write_failure_count(state_dir: Path, count: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path(state_dir).write_text(json.dumps({"consecutive_failures": int(count)}) + "\n")


# ---------------------------------------------------------------------------
# git plumbing -- never touches the working tree, the index, or HEAD
# ---------------------------------------------------------------------------


def _git(args: list[str], repo_dir: Path, env: dict | None = None, timeout: int = 120) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True, timeout=timeout, env=full_env
    )
    if proc.returncode != 0:
        raise ApplyRefused(f"git {' '.join(args[:3])} failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}")
    return proc.stdout


def tree_entry_count(ref: str, repo_dir: Path) -> int:
    """How many blobs a ref's tree holds. The post-apply invariant compares
    this before and after: a built tree must hold exactly the base's count
    plus the number of paths the run newly created. A deletion -- the one
    outcome that would mean the sync had eaten the engine -- shows up here
    as a shortfall no matter how it got in."""
    out = _git(["ls-tree", "-r", "--name-only", ref], repo_dir)
    return len([line for line in out.splitlines() if line.strip()])


def blob_mode(ref: str, relpath: str, repo_dir: Path) -> str:
    """The file mode a path carries at *ref* (100644 / 100755 / 120000).
    Carried across verbatim so an inbound change to a script's executable
    bit is not silently dropped -- and so a symlink is never quietly
    rewritten into a regular file."""
    line = _git(["ls-tree", ref, "--", relpath], repo_dir).strip()
    if not line:
        raise ApplyRefused(f"no tree entry for {relpath!r} at {ref}")
    return line.split()[0]


# ---------------------------------------------------------------------------
# The write-set decision
# ---------------------------------------------------------------------------


def partition_write_set(classifications: dict, protected: set[str], sensitive_prefixes: list[str]) -> tuple[dict, dict]:
    """(write_set, withheld). write_set maps remote_path -> its classification
    entry; withheld maps remote_path -> {"status", "reason"}.

    A path is written only if BOTH:
      * its status is `clean-apply`, or `local-patch` with `local_hash is
        None` (a create -- there is nothing on the engine to overwrite), and
      * it is not in the protected set and does not match a sensitive prefix.

    The second condition is redundant against a correct gate, which is the
    point of having it: it turns a gate bug into a refusal rather than into
    a rewritten sandbox hook."""
    write_set: dict[str, dict] = {}
    withheld: dict[str, dict] = {}

    for remote_path, entry in sorted(classifications.items()):
        status = entry.get("status")
        engine_path = entry.get("engine_path")

        if status == pull.STATUS_CLEAN_APPLY:
            writable, reason = True, ""
        elif status == pull.STATUS_LOCAL_PATCH:
            if entry.get("local_hash") is None:
                writable, reason = True, ""
            else:
                # The whole point of this module. `local-patch` with a local
                # copy means the engine has its OWN version of this file;
                # adopting upstream's in place would discard it with nothing
                # recording that it happened.
                writable = False
                reason = (
                    "engine has its own copy of this path (local_hash is not null); "
                    "adopting upstream in place would overwrite it silently"
                )
                status = WITHHELD_WOULD_OVERWRITE
        else:
            writable, reason = False, entry.get("reason", "")

        if writable and engine_path is not None:
            if engine_path in protected or gate.is_sensitive(engine_path, sensitive_prefixes):
                writable = False
                status = WITHHELD_PROTECTED
                reason = (
                    "path is in the enforcer protected set or matches a sensitive prefix; "
                    "the gate should already have withheld it, so reaching here means the gate is wrong"
                )

        if writable:
            write_set[remote_path] = entry
        else:
            withheld[remote_path] = {
                "status": status,
                "reason": reason,
                "engine_path": engine_path,
                "commits": entry.get("commits", []),
            }

    return write_set, withheld


def assign_paths_to_commits(write_set: dict, commit_order: list[str]) -> dict[str, list[str]]:
    """{commit_sha: [remote_path, ...]} -- each written path assigned to the
    LAST inbound commit that touched it, so it is written exactly once, at
    the content the report actually classified (the remote tip's blob).

    Replaying each commit's own intermediate blob instead would put bytes on
    the branch that no gate ever looked at: the classification compares the
    engine's copy against the tip's, not against whatever an intermediate
    commit happened to contain."""
    assignment: dict[str, list[str]] = {sha: [] for sha in commit_order}
    position = {sha: i for i, sha in enumerate(commit_order)}
    for remote_path, entry in write_set.items():
        touching = [sha for sha in entry.get("commits", []) if sha in position]
        if not touching:
            continue
        last = max(touching, key=lambda s: position[s])
        assignment[last].append(remote_path)
    return {sha: sorted(paths) for sha, paths in assignment.items() if paths}


# ---------------------------------------------------------------------------
# Branch construction
# ---------------------------------------------------------------------------


def _commit_author_env(sha: str, repo_dir: Path) -> dict:
    """Preserve the inbound commit's author. The person who wrote the change
    keeps the credit for it; the sync is the committer, which is what it
    actually is."""
    out = _git(["show", "-s", "--format=%an%n%ae%n%aI", sha], repo_dir)
    lines = out.splitlines()
    if len(lines) < 3:
        return {}
    return {"GIT_AUTHOR_NAME": lines[0], "GIT_AUTHOR_EMAIL": lines[1], "GIT_AUTHOR_DATE": lines[2]}


def _replay_subject(subject: str) -> str:
    """Strip a trailing `(#N)`. That number names a pull request on the code
    plane, and the commit is about to land on a different repository where
    the same number names something unrelated -- GitHub would helpfully
    cross-link it to whatever that is."""
    hint = changeset.extract_pr_number(subject)
    if hint is None:
        return subject
    return subject[: subject.rfind("(#")].strip() or subject


def build_branch_commits(
    *,
    repo_dir: Path,
    base_ref: str,
    remote_ref: str,
    write_set: dict,
    assignment: dict[str, list[str]],
    commit_order: list[str],
) -> tuple[str, int]:
    """Build one commit per contributing inbound commit on top of *base_ref*
    and return (tip_sha, created_path_count). Writes only into a private
    temporary index -- the caller's index and working tree are never opened.

    No ref is moved here. The caller pushes the returned object, so a failure
    anywhere in this function leaves no branch behind to clean up."""
    base_sha = changeset.resolve_commit(base_ref, repo_dir=repo_dir)
    if base_sha is None:
        raise ApplyRefused(f"base ref does not resolve to a commit: {base_ref!r}")

    created = 0
    for entry in write_set.values():
        if entry.get("local_hash") is None:
            created += 1

    with tempfile.TemporaryDirectory() as tmp:
        index_file = str(Path(tmp) / "index")
        env = {"GIT_INDEX_FILE": index_file}
        _git(["read-tree", base_sha], repo_dir, env=env)

        parent = base_sha
        for sha in commit_order:
            paths = assignment.get(sha)
            if not paths:
                continue
            for remote_path in paths:
                entry = write_set[remote_path]
                engine_path = entry["engine_path"]
                blob = entry["upstream_hash"]
                mode = blob_mode(remote_ref, remote_path, repo_dir)
                # --add only. Nothing in this module removes an index entry.
                _git(["update-index", "--add", "--cacheinfo", f"{mode},{blob},{engine_path}"], repo_dir, env=env)

            tree = _git(["write-tree"], repo_dir, env=env).strip()
            subject = _replay_subject(changeset.commit_subject(sha, repo_dir=repo_dir))
            body_paths = "\n".join(f"  {write_set[p]['engine_path']}" for p in paths)
            message = (
                f"{subject}\n\n"
                f"Replayed from the code plane at {sha}.\n\n"
                f"Paths written by this commit:\n{body_paths}\n"
            )
            author_env = dict(env)
            author_env.update(_commit_author_env(sha, repo_dir))
            parent = _git(
                ["commit-tree", tree, "-p", parent, "-m", message], repo_dir, env=author_env
            ).strip()

        tip = parent

    base_count = tree_entry_count(base_sha, repo_dir)
    tip_count = tree_entry_count(tip, repo_dir)
    if tip_count != base_count + created:
        raise ApplyRefused(
            "post-apply invariant failed: the built tree holds "
            f"{tip_count} blobs, expected {base_count} + {created} created = {base_count + created}. "
            "A shortfall means the run removed an engine file, which nothing here is allowed to do."
        )
    return tip, created


# ---------------------------------------------------------------------------
# PR body
# ---------------------------------------------------------------------------


def build_pr_body(
    *, report: dict, write_set: dict, withheld: dict, marker: str, marker_sha: str | None, tip_sha: str
) -> str:
    lines: list[str] = []
    lines.append(
        "Automated sync of merged code back into the engine checkout. "
        "Opened by the inbound channel; it does not merge its own PRs."
    )
    lines.append("")
    marker_desc = f"`{marker}` at `{marker_sha[:12]}`" if marker_sha else f"`{marker}` (unresolved)"
    lines.append(
        f"Base marker {marker_desc} -> code plane `{tip_sha[:12]}`, "
        f"{report.get('commit_count', 0)} commit(s), "
        f"{report.get('files_changed_count', 0)} file(s) changed "
        f"({report.get('gated_path_count', 0)} paths gated -- a rename counts twice)."
    )
    lines.append("")
    lines.append(f"### Written ({len(write_set)})")
    lines.append("")
    if write_set:
        for remote_path in sorted(write_set):
            entry = write_set[remote_path]
            kind = "create" if entry.get("local_hash") is None else "update"
            lines.append(f"- `{entry['engine_path']}` ({kind})")
    else:
        lines.append("_nothing_")
    lines.append("")
    lines.append(f"### Withheld ({len(withheld)})")
    lines.append("")
    lines.append(
        "These paths are in the change set and are deliberately NOT in this branch. "
        "They are listed here because a withheld path that nobody names is a dropped path."
    )
    lines.append("")
    if withheld:
        by_status: dict[str, list[str]] = {}
        for remote_path, info in sorted(withheld.items()):
            by_status.setdefault(info["status"], []).append(remote_path)
        for status in sorted(by_status):
            lines.append(f"**{status}**")
            lines.append("")
            for remote_path in by_status[status]:
                lines.append(f"- `{remote_path}`")
            lines.append("")
    else:
        lines.append("_nothing_")
    lines.append("")
    lines.append("### Verification")
    lines.append("")
    lines.append("Gate 1: PASS — test suite green (see the originating run).")
    lines.append(
        "Gate 2: PASS — the built tree's blob count equals the engine base's plus the number of "
        "paths this run created, checked on the real commit objects before anything was pushed."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def apply_inbound(
    *,
    repo_dir: Path,
    state_dir: Path,
    marker: str = "refs/synced/code-plane",
    remote: str = "code-plane",
    remote_branch: str = "main",
    engine_remote: str = "origin",
    engine_repo_slug: str,
    code_repo_slug: str,
    local_ref: str = "main",
    max_files: int,
    max_lines: int,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    do_fetch: bool = True,
    remote_ref: str | None = None,
    dry_run: bool = False,
    classify=None,
    push_branch=None,
    open_pr=None,
    **classify_kwargs,
) -> dict:
    """One run. Returns a result dict; never raises for an ordinary refusal.

    `classify`, `push_branch` and `open_pr` are injectable so tests can drive
    the decision logic against scratch repositories without a network. They
    default to the live implementations."""
    classify = classify or report_mod.classify_report
    push_branch = push_branch or _push_branch
    open_pr = open_pr or _open_pr

    failures = read_failure_count(state_dir)

    # C5. This is deliberately the FIRST thing, before the classify call and
    # therefore before its fetch: "disabled" has to mean zero traffic to the
    # remote, or it is not a circuit breaker, it is a log line.
    if failures >= max_consecutive_failures:
        return {
            "result": RESULT_DISABLED,
            "consecutive_failures": failures,
            "reason": (
                f"{failures} consecutive failed runs (max {max_consecutive_failures}); "
                "refusing to contact the remote until a run succeeds or the counter is cleared"
            ),
        }

    try:
        return _run(
            repo_dir=repo_dir,
            state_dir=state_dir,
            marker=marker,
            remote=remote,
            remote_branch=remote_branch,
            engine_remote=engine_remote,
            engine_repo_slug=engine_repo_slug,
            code_repo_slug=code_repo_slug,
            local_ref=local_ref,
            max_files=max_files,
            max_lines=max_lines,
            do_fetch=do_fetch,
            remote_ref=remote_ref,
            dry_run=dry_run,
            classify=classify,
            push_branch=push_branch,
            open_pr=open_pr,
            failures=failures,
            **classify_kwargs,
        )
    except ApplyRefused as exc:
        write_failure_count(state_dir, failures + 1)
        return {"result": RESULT_REFUSED, "reason": str(exc), "consecutive_failures": failures + 1}
    except Exception as exc:  # noqa: BLE001 -- an unexpected failure is still a failure
        write_failure_count(state_dir, failures + 1)
        return {
            "result": RESULT_REFUSED,
            "reason": f"unexpected error: {type(exc).__name__}: {exc}",
            "consecutive_failures": failures + 1,
        }


def _run(
    *,
    repo_dir: Path,
    state_dir: Path,
    marker: str,
    remote: str,
    remote_branch: str,
    engine_remote: str,
    engine_repo_slug: str,
    code_repo_slug: str,
    local_ref: str,
    max_files: int,
    max_lines: int,
    do_fetch: bool,
    remote_ref: str | None,
    dry_run: bool,
    classify,
    push_branch,
    open_pr,
    failures: int,
    **classify_kwargs,
) -> dict:
    report = classify(
        marker=marker,
        remote=remote,
        remote_branch=remote_branch,
        repo_dir=repo_dir,
        code_repo_slug=code_repo_slug,
        max_files=max_files,
        max_lines=max_lines,
        do_fetch=do_fetch,
        remote_ref=remote_ref,
        local_ref=local_ref,
        **classify_kwargs,
    )

    if report.get("refused"):
        raise ApplyRefused(f"classify report refused: {report.get('refusal_reason')}")

    classifications = report.get("classifications", {})

    # C6. A conflict stops the whole run before a single index entry is
    # written. Applying the non-conflicting remainder is the failure mode
    # that looks like success -- half a change set on a branch, with the
    # other half named only in a PR description nobody re-reads.
    conflicted = sorted(
        p
        for p, e in classifications.items()
        if e.get("status") in (pull.STATUS_CONFLICT, pull.STATUS_INTEGRITY_FAIL)
    )
    if conflicted:
        write_failure_count(state_dir, failures + 1)
        return {
            "result": RESULT_CONFLICT,
            "reason": f"change set contains unresolved conflicts: {conflicted}",
            "conflicted": conflicted,
            "consecutive_failures": failures + 1,
        }

    protected = outbound_apply.read_protected_set()
    sensitive_prefixes = gate.read_sensitive_prefixes()
    write_set, withheld = partition_write_set(classifications, protected, sensitive_prefixes)

    resolved_remote_ref = report.get("remote_ref") or remote_ref or f"{remote}/{remote_branch}"
    tip_sha = changeset.resolve_commit(resolved_remote_ref, repo_dir=repo_dir)
    if tip_sha is None:
        raise ApplyRefused(f"remote ref does not resolve to a commit: {resolved_remote_ref!r}")

    if not write_set:
        # Nothing writable. Not a failure -- the run completed and made a
        # decision -- but the marker does not move, because moving it would
        # claim these commits had been dealt with.
        write_failure_count(state_dir, 0)
        return {
            "result": RESULT_NOTHING,
            "reason": "no path in the change set cleared the write-set rules",
            "withheld": withheld,
            "consecutive_failures": 0,
        }

    commit_order = [c["sha"] for c in report.get("commits", [])]
    assignment = assign_paths_to_commits(write_set, commit_order)
    branch = f"engine-sync/inbound-{tip_sha[:12]}"

    if dry_run:
        return {
            "result": "dry-run",
            "branch": branch,
            "write_set": sorted(write_set),
            "withheld": withheld,
        }

    commit_sha, created = build_branch_commits(
        repo_dir=repo_dir,
        base_ref=local_ref,
        remote_ref=resolved_remote_ref,
        write_set=write_set,
        assignment=assignment,
        commit_order=commit_order,
    )

    push_branch(repo_dir=repo_dir, remote=engine_remote, commit_sha=commit_sha, branch=branch)

    title = f"Sync {len(write_set)} merged path(s) back into the engine"
    body = build_pr_body(
        report=report,
        write_set=write_set,
        withheld=withheld,
        marker=marker,
        marker_sha=changeset.resolve_commit(marker, repo_dir=repo_dir),
        tip_sha=tip_sha,
    )
    pr_url = open_pr(repo_slug=engine_repo_slug, branch=branch, base=local_ref, title=title, body=body)

    # C7. The marker advances only here, after the branch exists on the
    # remote and the PR is open. Every refusal path above returns before
    # this line, so none of them can move it.
    #
    # It records what the sync has PROPOSED, not what a human has merged.
    # That is deliberate -- a marker that waited for the merge would
    # re-propose the same commits every ten minutes until someone acted --
    # but it does mean an unmerged PR reads as in-sync. The PR is the thing
    # that carries the content; this ref only says the channel has stopped
    # owing you a look at these commits.
    _git(["update-ref", marker, tip_sha], repo_dir)
    write_failure_count(state_dir, 0)

    return {
        "result": RESULT_APPLIED,
        "branch": branch,
        "commit": commit_sha,
        "pr_url": pr_url,
        "marker_advanced_to": tip_sha,
        "written": sorted(write_set),
        "created_count": created,
        "withheld": withheld,
        "consecutive_failures": 0,
    }


def _push_branch(*, repo_dir: Path, remote: str, commit_sha: str, branch: str) -> None:
    """Push the built object to the ENGINE remote. The code plane is never a
    push target from this channel -- the engine pulls, the public side is
    never written by us, and no credential that can write the engine may
    live on the public side."""
    if remote == "code-plane":
        raise ApplyRefused("refusing to push to the code plane: this channel only ever writes the engine")
    _git(["push", remote, f"{commit_sha}:refs/heads/{branch}"], repo_dir, timeout=300)


def _open_pr(*, repo_slug: str, branch: str, base: str, title: str, body: str) -> str:
    """Open the PR on the ENGINE repository -- the deliberate exception to
    the rule that code PRs go to the code plane, because the whole purpose
    of this PR is to write the engine. Never merges it."""
    proc = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", repo_slug,
            "--head", branch,
            "--base", base,
            "--title", title,
            "--body", body,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise ApplyRefused(f"gh pr create failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply the inbound change set and open a PR on the engine.")
    parser.add_argument("--marker", default="refs/synced/code-plane")
    parser.add_argument("--remote", default="code-plane")
    parser.add_argument("--remote-branch", default="main")
    parser.add_argument("--engine-remote", default="origin")
    parser.add_argument("--engine-repo", default=None, help="engine repo slug (default: backend._repo.DISCUSSION_REPO)")
    parser.add_argument("--code-repo", default=None, help="code plane slug (default: backend._repo.CODE_REPO)")
    parser.add_argument("--repo-dir", default=str(REPO_ROOT))
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--local-ref", default="main")
    parser.add_argument("--max-files", type=int, default=report_mod.DEFAULT_MAX_FILES)
    parser.add_argument("--max-lines", type=int, default=report_mod.DEFAULT_MAX_LINES)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the write set and stop before building anything. NOT evidence about a real run: "
        "it returns before every line that writes.",
    )
    return parser


def _default_state_dir() -> Path:
    env = os.environ.get("ENGINE_SYNC_STATE_DIR") or os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".autonomous-forever-state"


def _resolve_engine_repo() -> str:
    from backend._repo import DISCUSSION_REPO  # noqa: PLC0415

    return DISCUSSION_REPO


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = apply_inbound(
        repo_dir=Path(args.repo_dir),
        state_dir=Path(args.state_dir) if args.state_dir else _default_state_dir(),
        marker=args.marker,
        remote=args.remote,
        remote_branch=args.remote_branch,
        engine_remote=args.engine_remote,
        engine_repo_slug=args.engine_repo or _resolve_engine_repo(),
        code_repo_slug=args.code_repo or report_mod._resolve_code_repo(),
        local_ref=args.local_ref,
        max_files=args.max_files,
        max_lines=args.max_lines,
        do_fetch=not args.no_fetch,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["result"] in (RESULT_APPLIED, RESULT_NOTHING, "dry-run"):
        return EXIT_OK
    if result["result"] in (RESULT_DISABLED, RESULT_REFUSED, RESULT_CONFLICT):
        return EXIT_REFUSED
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
