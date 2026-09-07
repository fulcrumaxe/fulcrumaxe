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
WITHHELD_BAD_MODE = "withheld:unsupported-file-mode"
WITHHELD_UNRENDERABLE = "withheld:unrenderable-path"

#: The only two blob modes this channel will write. Everything else is
#: refused BY NAME rather than carried across verbatim.
#:
#: 120000 is a symlink: the path gate inspects the path, but a symlink's
#: payload is its TARGET, which no path check ever looks at, and
#: pull.validate_path's own symlink defence tests `is_symlink()` on the engine
#: filesystem -- False for a path that does not exist yet, so it stops a write
#: *through* an existing engine symlink and never sees an incoming one.
#: 160000 is a gitlink (submodule), which points at an entire other repository.
#: Both would have been reported to a human as an ordinary "(create)".
ALLOWED_BLOB_MODES = frozenset({"100644", "100755"})

#: Characters a path may not contain if it is going to be named in a PR body.
#: A backtick closes the code span the path is rendered inside; `<` opens HTML
#: that GitHub may never terminate, swallowing every entry after it. Since the
#: PR body is the human checkpoint this design leans on, a path that can hide
#: its neighbours from that list is refused rather than written -- no
#: legitimate path in this repo needs one of these.
_PATH_FORBIDDEN_CHARS = frozenset("`<>\n\r\t|")


def path_is_renderable(remote_path: str) -> tuple[bool, str]:
    """(ok, reason). Refuses a path that could break out of, or hide, the PR
    body list it will be named in."""
    for ch in remote_path:
        if ch in _PATH_FORBIDDEN_CHARS or not ch.isprintable():
            return False, (
                f"path contains character U+{ord(ch):04X}, which can break out of the PR body's "
                "path list and hide the entries after it"
            )
    return True, ""


def render_path(remote_path: str) -> str:
    """A path rendered so it cannot alter the markdown around it.

    Every character outside a conservative allowlist becomes a visible
    `[U+XXXX]` token -- visible, because silently dropping the character would
    make two different paths render identically, and this list is what a human
    approves from. Applied to EVERY entry, written or withheld: refusing
    unrenderable paths from the write set is not enough on its own, since
    quarantined paths from an untrusted stranger are listed here too."""
    out = []
    for ch in remote_path:
        if ch.isalnum() and ch.isascii() or ch in "._/-+@":
            out.append(ch)
        else:
            out.append(f"[U+{ord(ch):04X}]")
    return "`" + "".join(out) + "`"


class ApplyRefused(RuntimeError):
    """A refusal that must leave nothing behind: no branch, no PR, no marker
    movement. Raised before any ref is written, always."""


# ---------------------------------------------------------------------------
# State: the consecutive-failure counter, and its consumer
# ---------------------------------------------------------------------------


def state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILE_NAME


def read_state(state_dir: Path) -> dict:
    """The module's whole persisted state: the failure counter and the
    withheld-path debt.

    Missing or unreadable reads as empty. Deliberately permissive for the
    counter: it exists to stop a *failing* channel, and "I could not read a
    number" is not evidence of failure. Permissive for `pending` too, but for
    the opposite reason -- an unreadable debt file must not be able to refuse
    the run, because the debt's whole job is to keep being re-offered, and a
    channel that refuses instead offers nothing at all."""
    try:
        data = json.loads(state_path(state_dir).read_text())
    except Exception:
        return {"consecutive_failures": 0, "pending": {}}
    pending = data.get("pending")
    if not isinstance(pending, dict):
        pending = {}
    return {"consecutive_failures": int(data.get("consecutive_failures", 0) or 0), "pending": pending}


def write_state(state_dir: Path, *, consecutive_failures: int, pending: dict | None = None) -> None:
    """Write the counter, and `pending` only when the caller supplies it.

    The default of None means "leave the debt exactly as it is". Every path
    that touches only the counter goes through here, so none of them can
    truncate the debt as a side effect -- which is how a persisted-debt design
    quietly reverts to the lossy one it replaced."""
    state_dir.mkdir(parents=True, exist_ok=True)
    current = read_state(state_dir)
    payload = {
        "consecutive_failures": int(consecutive_failures),
        "pending": current["pending"] if pending is None else pending,
    }
    state_path(state_dir).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_failure_count(state_dir: Path) -> int:
    return read_state(state_dir)["consecutive_failures"]


def write_failure_count(state_dir: Path, count: int) -> None:
    write_state(state_dir, consecutive_failures=count)


def read_pending(state_dir: Path) -> dict:
    return read_state(state_dir)["pending"]


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
    mode = line.split()[0]
    if mode not in ALLOWED_BLOB_MODES:
        raise ApplyRefused(
            f"refusing mode {mode} for {relpath!r}: only {sorted(ALLOWED_BLOB_MODES)} are written by this channel"
        )
    return mode


# ---------------------------------------------------------------------------
# The write-set decision
# ---------------------------------------------------------------------------


def partition_write_set(
    classifications: dict,
    protected: set[str],
    sensitive_prefixes: list[str],
    *,
    resolve_mode=None,
) -> tuple[dict, dict]:
    """(write_set, withheld). write_set maps remote_path -> its classification
    entry; withheld maps remote_path -> {"status", "reason"}.

    A path is written only if ALL of:
      * its status is `clean-apply`, or `local-patch` with `local_hash is
        None` (a create -- there is nothing on the engine to overwrite);
      * it is not in the protected set and does not match a sensitive prefix;
      * it is renderable in a PR body without hiding its neighbours;
      * its blob mode is one this channel writes (`resolve_mode`, when given).

    Everything after the first bullet is redundant against a correct gate,
    which is the point of having it: this function's job is to turn an
    upstream bug into a refusal. That is also why nothing in here `continue`s
    past a shape it did not expect -- a skip in the one function whose purpose
    is refusing is the wrong shape, however unreachable it looks today."""
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

        if writable:
            # No `and engine_path is not None` guard here. A writable entry
            # carrying a null engine_path is an upstream bug, and skipping the
            # protected/sensitive re-check for it would send `None` on to the
            # cacheinfo format string -- writing a file literally named "None"
            # past the one belt meant to catch exactly this.
            if engine_path is None:
                raise ApplyRefused(
                    f"classifier marked {remote_path!r} writable with a null engine_path; "
                    "refusing rather than skipping the protected/sensitive re-check for it"
                )
            if engine_path in protected or gate.is_sensitive(engine_path, sensitive_prefixes):
                writable = False
                status = WITHHELD_PROTECTED
                reason = (
                    "path is in the enforcer protected set or matches a sensitive prefix; "
                    "the gate should already have withheld it, so reaching here means the gate is wrong"
                )

        if writable:
            ok, why = path_is_renderable(remote_path)
            if not ok:
                writable = False
                status = WITHHELD_UNRENDERABLE
                reason = why

        if writable and resolve_mode is not None:
            try:
                resolve_mode(remote_path)
            except ApplyRefused as exc:
                # Named in the report rather than aborting the run: a single
                # inbound symlink should not stop every other path, and a
                # silent skip is what let it be reported as an ordinary
                # "(create)" in the first place.
                writable = False
                status = WITHHELD_BAD_MODE
                reason = str(exc)

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


#: Withholdings that are still OWED: a human (or a later change on either
#: plane) can still resolve them, so they are carried forward and re-offered
#: every run until they do.
#:
#: `needs-human-approval` and `quarantined` are the whole point -- those are
#: the sensitive prefixes and the untrusted-provenance paths, the two sets the
#: design most wants a human to actually see. `out-of-surface` is here because
#: today it is dominated by `tests/`, which is out of surface only because
#: MANIFEST.md has no entry for it yet; the day that entry lands, these should
#: sync rather than have been forgotten. `quarantined` also covers a purely
#: transient cause -- a GitHub read that failed closed -- which must not cost
#: a path permanently.
PENDING_STATUSES = frozenset(
    {
        gate.CAT_NEEDS_APPROVAL,
        gate.CAT_QUARANTINED,
        gate.CAT_OUT_OF_SURFACE,
        gate.CAT_COLLISION,
        pull.STATUS_CONFLICT,
        pull.STATUS_INTEGRITY_FAIL,
        WITHHELD_WOULD_OVERWRITE,
        WITHHELD_PROTECTED,
        WITHHELD_BAD_MODE,
        WITHHELD_UNRENDERABLE,
    }
)

#: Withholdings that are DECISIONS, not deferrals, and so do not accumulate:
#: a traversal never becomes safe, an export-generated artifact never gains an
#: engine-side source, `already-applied` owes nothing, and this channel never
#: proposes a deletion. Recorded in the run's report, not carried.
TERMINAL_STATUSES = frozenset(
    {
        gate.CAT_PATH_UNSAFE,
        gate.CAT_GENERATED,
        pull.STATUS_ALREADY_APPLIED,
        pull.STATUS_REJECTED,
    }
)


def build_pending(
    classifications: dict,
    withheld: dict,
    report: dict,
    previous: dict | None = None,
    is_settled_on_engine=None,
) -> dict:
    """The debt to carry into the next run.

    This is the fix for the channel's sharpest defect: withheld paths used to
    be named in exactly one PR body and then never re-enter a change set,
    because the marker had moved past the commits that carried them. 27 paths
    on the live change set, all of them the sensitive prefixes -- so the
    human-approval gate had no queue behind it, only a one-shot notice, after
    which the alarm went quiet. A gate whose backlog empties itself is not a
    gate.

    Carrying the debt separately from the marker is what lets the marker keep
    advancing. Blocking the marker on unapplied paths instead was measured
    against the live backlog and cannot work: every one of the 13 commits
    carries at least one withheld path, so the marker would never move at all,
    the change set would grow without bound past the ceiling, and the channel
    would refuse until it disabled itself.

    A path leaves the debt only by being resolved -- applied, already present
    on the engine, deleted upstream, or ruled terminal. Nothing here drops a
    path for being old.

    `is_settled_on_engine(remote_path, engine_path)` is how a debt entry
    clears, and it exists because the classifier alone cannot clear one. A
    sensitive path never reaches hash classification at all: `path_gate`
    short-circuits it to `needs-human-approval` before any hash is computed,
    so it can never come back `already-applied` however faithfully a human
    applies it. Without this check the debt would be unable to empty for the
    27 paths it matters most for, and an inbox that only grows is read exactly
    as often as one that silently empties. The check is a read-only hash
    comparison; it does not let the channel WRITE anything the gate withheld."""
    previous = previous or {}
    pending: dict[str, dict] = {}

    for remote_path, info in withheld.items():
        status = info.get("status")
        # Only a TERMINAL status drops a path. An unrecognised status is
        # carried, deliberately: forgetting a path because its status was not
        # on a list is the exact failure this function exists to stop.
        if status in TERMINAL_STATUSES:
            continue

        # ...and a path the engine has already ended up with is not owed,
        # whoever put it there.
        if is_settled_on_engine is not None and is_settled_on_engine(remote_path, info.get("engine_path")):
            continue

        entry = classifications.get(remote_path, {})
        prior = previous.get(remote_path, {})

        # Cache each touching commit's provenance verdict so a carried path
        # never re-pays a GitHub round trip. Verdicts resolved THIS run win;
        # anything else is inherited from the previous record, because a
        # carried path's commits are not in this run's enumeration at all.
        commit_trust = dict(prior.get("commit_trust") or {})
        touching = list(entry.get("commits") or prior.get("commits") or [])
        for c in report.get("commits", []):
            if c["sha"] in touching:
                commit_trust[c["sha"]] = [bool(c.get("trusted")), ""]

        pending[remote_path] = {
            "commits": touching,
            "statuses": list(entry.get("statuses") or prior.get("statuses") or []),
            "status": status,
            "reason": info.get("reason", ""),
            "engine_path": info.get("engine_path") or prior.get("engine_path"),
            "commit_trust": commit_trust,
            "first_seen": prior.get("first_seen") or report.get("remote_ref", ""),
            "runs_owed": int(prior.get("runs_owed", 0) or 0) + 1,
        }
    return pending


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
            # Structurally unreachable while write_set and commit_order come
            # from the same report -- but a `continue` here drops the path from
            # the branch while the PR body still lists it under "Written". A
            # create would be caught by the blob-count invariant; an UPDATE
            # would not, and would read as applied when it was not. That is the
            # silent-loss shape this whole change is about.
            raise ApplyRefused(
                f"{remote_path!r} is in the write set but none of its commits {entry.get('commits', [])!r} "
                "are in this run's commit order; refusing rather than dropping it from the branch"
            )
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
    *,
    report: dict,
    write_set: dict,
    withheld: dict,
    marker: str,
    marker_sha: str | None,
    tip_sha: str,
    resolved: list | None = None,
    carried: set | None = None,
    blob_modes: dict | None = None,
) -> str:
    """The human checkpoint, rendered so that no entry in it can hide another.

    Every path goes through `render_path`. Path names are contributor-
    controlled and reach here even from an untrusted stranger, since
    quarantined paths are listed too; a backtick closes the code span and a
    `<` opens HTML that GitHub never terminates, and either one swallows the
    entries that follow. The withheld list is exactly what a human is supposed
    to act on, so an unprivileged actor being able to hide entries from it
    defeats the checkpoint the design leans on."""
    carried = carried or set()
    resolved = resolved or []
    blob_modes = blob_modes or {}
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
        lines.append("| path | change | mode |")
        lines.append("|---|---|---|")
        for remote_path in sorted(write_set):
            entry = write_set[remote_path]
            kind = "create" if entry.get("local_hash") is None else "update"
            mode = blob_modes.get(remote_path, "?")
            # The mode is shown because "create" alone said the same thing for
            # a regular file, a symlink pointing anywhere on the box, and a
            # submodule. Only 100644/100755 can reach this list at all now,
            # but the reviewer should be able to see that rather than trust it.
            lines.append(f"| {render_path(entry['engine_path'])} | {kind} | `{mode}` |")
    else:
        lines.append("_nothing_")
    lines.append("")

    lines.append(f"### Withheld ({len(withheld)})")
    lines.append("")
    lines.append(
        "In the change set and deliberately NOT in this branch. Every one of these is "
        "carried forward and re-offered on the next run until it is resolved -- the debt "
        "lives in the channel's state file, not in this page, so nothing here is a "
        "one-shot notice."
    )
    lines.append("")
    if withheld:
        by_status: dict[str, list[str]] = {}
        for remote_path, info in sorted(withheld.items()):
            by_status.setdefault(info["status"], []).append(remote_path)
        for status in sorted(by_status):
            lines.append(f"**{status}** ({len(by_status[status])})")
            lines.append("")
            for remote_path in by_status[status]:
                age = " _(carried from an earlier run)_" if remote_path in carried else ""
                lines.append(f"- {render_path(remote_path)}{age}")
            lines.append("")
    else:
        lines.append("_nothing_")
    lines.append("")

    if resolved:
        lines.append(f"### Resolved since the last run ({len(resolved)})")
        lines.append("")
        lines.append("Previously withheld, no longer owed -- applied, already present on the engine, or gone upstream.")
        lines.append("")
        for remote_path in resolved:
            lines.append(f"- {render_path(remote_path)}")
        lines.append("")

    lines.append("### Verification")
    lines.append("")
    # Gate 1 is N/A, not PASS. This channel runs no test suite: it replays
    # commits that were already tested on the code plane before they merged.
    # A constant "Gate 1: PASS" written by the tool that opens the PR would
    # satisfy the two-gate check for every PR it ever opens while naming no
    # run at all -- the channel manufacturing its own gate satisfaction.
    tested_prs = sorted({c["resolved_pr"] for c in report.get("commits", []) if c.get("resolved_pr")})
    pr_list = ", ".join(f"#{n}" for n in tested_prs) if tested_prs else "none resolved"
    lines.append(
        f"Gate 1: N/A — the sync runs no test suite of its own. Every commit replayed here was "
        f"tested and reviewed on the code plane before it merged ({pr_list}); this branch adds "
        f"no new code, only the classified blobs from those commits."
    )
    lines.append("")
    created = sum(1 for e in write_set.values() if e.get("local_hash") is None)
    lines.append(
        f"Gate 2: PASS — post-apply invariant, checked on the real commit objects before the push: "
        f"the built tree holds the engine base's blob count plus exactly the {created} path(s) this "
        f"run created, so no engine file was removed. Modes were allowlisted to 100644/100755, and "
        f"{len(withheld)} path(s) were withheld and recorded as owed."
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
        # --dry-run promises to return before every line that writes (see the
        # --dry-run help text below). A classify/ceiling refusal is raised
        # here, upstream of the `if dry_run` branch inside `_run`, so without
        # this guard a dry run would still bump the circuit breaker.
        if not dry_run:
            write_failure_count(state_dir, failures + 1)
        return {
            "result": RESULT_REFUSED,
            "reason": str(exc),
            "consecutive_failures": failures if dry_run else failures + 1,
        }
    except Exception as exc:  # noqa: BLE001 -- an unexpected failure is still a failure
        # Same --dry-run promise as the two guards above. This handler wraps
        # the entire `_run()` call, so it is also reachable for a dry run: a
        # git subprocess failure, a bad protected.txt/sensitive.txt parse, or
        # a remote timeout anywhere before `_run`'s own `if dry_run` early
        # return would otherwise still spend a strike on the circuit breaker.
        if not dry_run:
            write_failure_count(state_dir, failures + 1)
        return {
            "result": RESULT_REFUSED,
            "reason": f"unexpected error: {type(exc).__name__}: {exc}",
            "consecutive_failures": failures if dry_run else failures + 1,
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
    previous_pending = read_pending(state_dir)

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
        extra_paths={k: v for k, v in previous_pending.items()},
        known_commit_trust={
            sha: tuple(verdict)
            for entry in previous_pending.values()
            for sha, verdict in (entry.get("commit_trust") or {}).items()
        },
        **classify_kwargs,
    )

    if report.get("refused"):
        raise ApplyRefused(f"classify report refused: {report.get('refusal_reason')}")

    classifications = report.get("classifications", {})
    carried = set(report.get("carried_forward") or ())

    # A conflict stops the whole run before a single index entry is written.
    # Applying the non-conflicting remainder is the failure mode that looks
    # like success -- half a change set on a branch, the other half named only
    # in a PR description nobody re-reads.
    #
    # Scoped by the DEBT, not by whether the path is in this run's
    # enumeration. A path already recorded as owed is one an earlier run
    # deliberately withheld -- the engine's own copy is the copy we chose to
    # keep -- so when the plane later touches it again and the hashes come
    # back `conflict`, that is the known state re-stating itself, not a new
    # surprise. Letting it refuse the run is what turned a single withheld
    # would-overwrite path into a channel-wide landmine: every subsequent run
    # aborts over it, including unrelated writable work, and three aborts
    # later the channel disables itself and stops contacting the remote.
    #
    # Keying on "is it in this run's commit list" does NOT work and was tried:
    # the landmine fires precisely because a NEW commit touches the path, so
    # it is in the enumeration every time.
    #
    # A conflict on a path that is NOT in the debt is unexplained, and still
    # refuses the whole run untouched.
    known_debt = set(previous_pending)
    conflicted = sorted(
        path
        for path, e in classifications.items()
        if e.get("status") in (pull.STATUS_CONFLICT, pull.STATUS_INTEGRITY_FAIL) and path not in known_debt
    )
    if conflicted:
        # Same --dry-run promise as the classify/ceiling refusal above: this
        # is the second of the two write_failure_count call sites that ran
        # ahead of the `if dry_run` branch further down.
        if not dry_run:
            write_failure_count(state_dir, failures + 1)
        return {
            "result": RESULT_CONFLICT,
            "reason": f"change set contains unresolved conflicts: {conflicted}",
            "conflicted": conflicted,
            "consecutive_failures": failures if dry_run else failures + 1,
        }

    protected = outbound_apply.read_protected_set()
    sensitive_prefixes = gate.read_sensitive_prefixes()
    resolved_remote_ref_for_mode = report.get("remote_ref") or remote_ref or f"{remote}/{remote_branch}"
    write_set, withheld = partition_write_set(
        classifications,
        protected,
        sensitive_prefixes,
        resolve_mode=lambda rp: blob_mode(resolved_remote_ref_for_mode, rp, repo_dir),
    )

    def _settled_on_engine(remote_path: str, engine_path: str | None) -> bool:
        """True when the engine's copy already equals the code plane's, or the
        path is gone from the plane entirely -- in both cases nothing is owed."""
        upstream = changeset.blob_hash_at(resolved_remote_ref_for_mode, remote_path, repo_dir=repo_dir)
        if upstream is None:
            return True
        if engine_path is None:
            return False
        return changeset.blob_hash_at(local_ref, engine_path, repo_dir=repo_dir) == upstream

    next_pending = build_pending(
        classifications,
        withheld,
        report,
        previous=previous_pending,
        is_settled_on_engine=_settled_on_engine,
    )
    resolved_paths = sorted(set(previous_pending) - set(next_pending))

    resolved_remote_ref = report.get("remote_ref") or remote_ref or f"{remote}/{remote_branch}"
    tip_sha = changeset.resolve_commit(resolved_remote_ref, repo_dir=repo_dir)
    if tip_sha is None:
        raise ApplyRefused(f"remote ref does not resolve to a commit: {resolved_remote_ref!r}")

    if not write_set:
        # Nothing writable. Not a failure -- the run completed and made a
        # decision -- and the marker does not move, because no PR was opened,
        # so nothing was put in front of a human. The debt IS persisted: this
        # run classified every carried path, and dropping that result would
        # lose any path that resolved since the last run.
        #
        # Under --dry-run this branch must not persist that decision at all
        # -- it is a real write to `pending`, not just to the failure counter,
        # and it runs ahead of `_run`'s own `if dry_run` check further down.
        # The debt-tracking fields below (`pending_count`, `resolved`) describe
        # what WOULD be persisted, which is meaningless once nothing is -- so
        # a dry run reports the same shape as the other dry-run exit below
        # (result/write_set/withheld) instead of a half-true RESULT_NOTHING.
        if dry_run:
            return {
                "result": "dry-run",
                "reason": "no path in the change set cleared the write-set rules",
                "write_set": [],
                "withheld": withheld,
            }
        write_state(state_dir, consecutive_failures=0, pending=next_pending)
        return {
            "result": RESULT_NOTHING,
            "reason": "no path in the change set cleared the write-set rules",
            "withheld": withheld,
            "pending_count": len(next_pending),
            "resolved": resolved_paths,
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

    blob_modes = {rp: blob_mode(resolved_remote_ref, rp, repo_dir) for rp in write_set}

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
        resolved=resolved_paths,
        carried=carried,
        blob_modes=blob_modes,
    )
    pr_url = open_pr(repo_slug=engine_repo_slug, branch=branch, base=local_ref, title=title, body=body)

    # The debt is written BEFORE the marker, and that order is the point.
    #
    # If the debt lands and the marker does not, the withheld paths are simply
    # offered twice -- duplicate, harmless, self-correcting. If the marker
    # landed first and the debt write failed, the marker would have moved past
    # commits whose withheld paths nothing is holding any more, which is the
    # permanent silent loss this whole mechanism exists to prevent. Between an
    # extra offer and a lost one, take the extra offer.
    write_state(state_dir, consecutive_failures=0, pending=next_pending)

    # The marker advances only here, after the branch exists on the remote and
    # the PR is open. Every refusal path above returns before this line.
    #
    # It records which commits the channel has ENUMERATED AND RULED ON -- not
    # which content the engine has taken. Those differ whenever anything was
    # withheld, and the difference is carried by the debt above rather than by
    # this ref, plus reported as `withheld_debt` in the alarm every loop
    # iteration. Holding the marker back instead was measured against the live
    # backlog and cannot work: all 13 commits carry at least one withheld
    # path, so it would never advance, the change set would grow past the
    # ceiling, and the channel would refuse until it disabled itself.
    _git(["update-ref", marker, tip_sha], repo_dir)

    return {
        "result": RESULT_APPLIED,
        "branch": branch,
        "commit": commit_sha,
        "pr_url": pr_url,
        "marker_advanced_to": tip_sha,
        "written": sorted(write_set),
        "created_count": created,
        "withheld": withheld,
        "pending_count": len(next_pending),
        "resolved": resolved_paths,
        "consecutive_failures": 0,
    }


def _push_branch(*, repo_dir: Path, remote: str, commit_sha: str, branch: str) -> None:
    """Push the built object to the ENGINE remote. The code plane is never a
    push target from this channel -- the engine pulls, the public side is
    never written by us, and no credential that can write the engine may
    live on the public side."""
    _refuse_if_code_plane(repo_dir, remote)
    _git(["push", remote, f"{commit_sha}:refs/heads/{branch}"], repo_dir, timeout=300)


def _remote_urls(repo_dir: Path, remote: str) -> set[str]:
    """Every URL configured for *remote*, fetch and push, normalised."""
    out = set()
    for key in (f"remote.{remote}.url", f"remote.{remote}.pushurl"):
        proc = subprocess.run(
            ["git", "config", "--get-all", key], cwd=str(repo_dir), capture_output=True, text=True, timeout=30
        )
        for line in proc.stdout.splitlines():
            url = line.strip()
            if url:
                out.add(_normalise_remote_url(url))
    return out


def _normalise_remote_url(url: str) -> str:
    """Enough normalisation to compare two spellings of the same remote:
    strip credentials, a trailing `.git`, and a trailing slash, and lowercase.
    Not a general URL parser -- it only has to make `https://x@host/a/b.git`
    and `https://host/a/b` compare equal."""
    u = url.strip().lower()
    if "://" in u:
        scheme, rest = u.split("://", 1)
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
        u = f"{scheme}://{rest}"
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u


def _refuse_if_code_plane(repo_dir: Path, remote: str, code_plane_remote: str = "code-plane") -> None:
    """Refuse to push to whatever the code plane actually IS, not to whatever
    happens to be spelled `code-plane`.

    Guarding on the remote's NAME was the weaker check: a second remote
    pointing at the same URL under any other name walked straight past it, and
    the name is the one part of a remote that carries no authority at all. The
    engine pulls and the public side is never written by this channel, so the
    comparison that matters is the URL."""
    if remote == code_plane_remote:
        raise ApplyRefused("refusing to push to the code plane: this channel only ever writes the engine")
    code_plane_urls = _remote_urls(repo_dir, code_plane_remote)
    if not code_plane_urls:
        return
    target_urls = _remote_urls(repo_dir, remote)
    shared = code_plane_urls & target_urls
    if shared:
        raise ApplyRefused(
            f"refusing to push: remote {remote!r} resolves to the code plane's own URL ({sorted(shared)[0]}); "
            "this channel only ever writes the engine"
        )


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
