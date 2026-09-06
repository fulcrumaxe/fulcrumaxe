#!/usr/bin/env python3
"""scripts/engine-sync/inbound/report.py -- the read-only classify report
CLI for the engine-sync inbound channel.

Given the marker ref (`refs/synced/code-plane` by default) and the code
plane's current `main` tip, this classifies every changed path into one of:

  clean-apply / local-patch / already-applied / conflict / rejected /
  integrity-fail          (pull.classify_against_baseline's own vocabulary)

  plus gate outcomes that stop a path before it ever reaches that table:
  quarantined:untrusted-provenance, rejected:out-of-surface,
  rejected:path-unsafe, rejected:reverse-map-collision, needs-human-approval,
  and one purely-informational bucket: generated (an export-generated path
  with no engine-side source).

APPLIES NOTHING. WRITES NOTHING to the working tree, the index, or
`refs/synced/code-plane`. This tool makes two kinds of network call: one
`git fetch` of the code-plane remote's tracked branch (see
changeset.ensure_remote_fetched's docstring for why, and why that does not
change the existing staleness check's own fetch-free contract), and, per
touched commit, GitHub API reads to resolve provenance -- `GET
/repos/<repo>/commits/<sha>/pulls` is the SOLE commit->PR link (never a
commit's own subject line, which is attacker-controlled content that the
code plane's merge settings let reach `main` verbatim), followed by one PR
read to resolve that PR's GitHub-authenticated author (via
pr_intake_gate.fetch_pr_meta). A commit resolving to zero PRs, more than one
PR, or a PR the subject's own `(#N)` hint disagrees with all refuse that
commit's paths rather than guess.

Note for anyone checking "does this call the network": the ceiling refusal
and the deletion-refusal both run AFTER the one `git fetch` above, so
neither makes a GitHub API call, but neither is "no network" either -- the
fetch has already written objects and `FETCH_HEAD` by the time either
refusal is evaluated. Only the `--local-ref` resolution check, which runs
before the fetch, is genuinely network-free.

Refuses (nonzero exit, no partial report) when the computed change set
exceeds the file/line ceiling, or when it contains a deletion of a path the
engine actually has that resolves outside the export surface -- the
tree-diff trap this whole module exists to avoid falling into. Any other
outcome -- including every path rejected or quarantined -- is a
*successful* report and exits 0: the tool's job is to classify and print,
not to have an opinion about what it finds.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_INBOUND_DIR = Path(__file__).resolve().parent
_ENGINE_SYNC_DIR = _INBOUND_DIR.parent
REPO_ROOT = _ENGINE_SYNC_DIR.parent.parent

for _p in (str(_INBOUND_DIR), str(_ENGINE_SYNC_DIR), str(REPO_ROOT), str(REPO_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import changeset  # noqa: E402
import gate  # noqa: E402
import pull  # noqa: E402

DEFAULT_MAX_FILES = 50
DEFAULT_MAX_LINES = 500

EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_ERROR = 4


def _resolve_code_repo() -> str:
    from backend._repo import CODE_REPO  # noqa: PLC0415

    return CODE_REPO


def _resolve_trust_allowlist():
    from external_intake_gate import resolve_allowlist  # noqa: PLC0415

    return resolve_allowlist()


def _prs_for_commit(sha: str, repo_slug: str) -> list[int]:
    """The SOLE commit->PR link: `GET /repos/<repo_slug>/commits/<sha>/pulls`.

    A commit's own subject line is attacker-controlled -- it is whatever
    text the contributor typed, and the code plane's merge settings
    (`allow_merge_commit` and `allow_rebase_merge`, both true) let that
    subject reach `main` verbatim under either merge method. Parsing a
    trailing `(#N)` out of it and trusting that number was the whole
    provenance boundary defeated by typing a number into a commit message:
    an untrusted contributor's own commit, subject `Tidy up imports (#4)`,
    would resolve PR #4's (someone else's, trusted) author and inherit
    their trust. This function never reads the subject at all -- it asks
    GitHub which PR(s) the commit actually belongs to.

    Fails closed: a read that errors, or a response that is not a JSON
    list, returns [] (unresolvable) rather than raising or guessing. Empty
    or ambiguous (more than one PR) are both refused by the caller -- this
    function only fetches and parses, it does not decide."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo_slug}/commits/{sha}/pulls", "--jq", "[.[].number]"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return []
    try:
        result = json.loads(proc.stdout.strip() or "[]")
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(result, list) or not all(isinstance(n, int) for n in result):
        return []
    return result


def _pr_author(pr_number: int, repo_slug: str) -> str | None:
    from pr_intake_gate import fetch_pr_meta  # noqa: PLC0415

    meta = fetch_pr_meta(pr_number, repo_slug)
    if not meta.get("fetch_ok"):
        return None
    return meta.get("author")


def _is_trusted_author(login, allowlist):
    from pr_comment_trust import is_trusted_author  # noqa: PLC0415

    return is_trusted_author(login, allowlist)


def classify_report(
    *,
    marker: str,
    remote: str,
    remote_branch: str,
    repo_dir: Path,
    code_repo_slug: str,
    max_files: int,
    max_lines: int,
    do_fetch: bool = True,
    remote_ref: str | None = None,
    local_ref: str = "main",
    resolve_trust_allowlist=None,
    resolve_pr_author=None,
    is_trusted_author=None,
    resolve_surface_patterns=None,
    resolve_sensitive_prefixes=None,
    resolve_prs_for_commit=None,
    extra_paths: dict | None = None,
    known_commit_trust: dict | None = None,
) -> dict:
    """The full pipeline. Injectable seams (resolve_trust_allowlist,
    resolve_pr_author, is_trusted_author, resolve_surface_patterns,
    resolve_sensitive_prefixes, resolve_prs_for_commit) default to the live,
    real-file-backed implementations; tests supply stubs so provenance and
    surface/sensitivity matching can be exercised without a network call or
    a real open-source/MANIFEST.md on disk. `remote_ref` overrides the
    `{remote}/{remote_branch}` join for tests that model "the code plane's
    tip" as a plain local branch rather than a configured git remote.

    `local_ref` is what "the engine's own copy" means when hash-classifying
    -- it defaults to `main`, not `HEAD`, specifically so that running this
    tool from a feature branch does not silently shift every classification
    against a tree nobody else is looking at. `repo_dir` is expected to be
    the ENGINE checkout, not merely "some git repo" -- it is where
    `local_ref` and the marker ref live, and `path_gate`'s target_root
    (symlink/on-disk-casing checks) is also resolved there. That coupling
    is why local_ref's resolvability is checked against `repo_dir` up front:
    an unresolvable local_ref is usually the signal that repo_dir is not
    the engine checkout at all."""
    resolve_trust_allowlist = resolve_trust_allowlist or _resolve_trust_allowlist
    resolve_pr_author = resolve_pr_author or (lambda pr: _pr_author(pr, code_repo_slug))
    is_trusted_author = is_trusted_author or _is_trusted_author
    resolve_surface_patterns = resolve_surface_patterns or gate.load_export_surface_patterns
    resolve_sensitive_prefixes = resolve_sensitive_prefixes or gate.read_sensitive_prefixes
    resolve_prs_for_commit = resolve_prs_for_commit or (lambda sha: _prs_for_commit(sha, code_repo_slug))

    # Resolve local_ref up front and refuse if it does not resolve at all.
    # Without this, every blob_hash_at(local_ref, ...) call below silently
    # returns None for a nonexistent ref -- indistinguishable from every
    # engine path genuinely being absent -- and every clean-apply path comes
    # back misclassified conflict instead. Checked before the fetch and
    # before anything else runs, so a bad --local-ref fails fast and never
    # produces a confident-looking wrong report.
    if changeset.resolve_commit(local_ref, repo_dir=repo_dir) is None:
        return {
            "marker": marker,
            "refused": True,
            "refusal_reason": f"local_ref does not resolve to a commit: {local_ref!r}",
        }

    remote_ref = remote_ref or f"{remote}/{remote_branch}"

    if do_fetch:
        changeset.ensure_remote_fetched(remote, remote_branch, repo_dir=repo_dir)

    cs = changeset.build_changeset(marker, remote_ref, repo_dir=repo_dir)

    # --- Ceiling check: a change set this large, from commit enumeration
    # alone, means something is wrong upstream of this tool (or the marker
    # is badly stale) -- refuse rather than print a report nobody asked for
    # at this size. ---
    if cs["gated_path_count"] > max_files or cs["total_insertions"] + cs["total_deletion_lines"] > max_lines:
        return {
            "marker": marker,
            "remote_ref": remote_ref,
            "refused": True,
            # Names the figure it actually compared. `gated_path_count` counts a
            # rename twice (destination + synthetic source delete), so it can
            # exceed the ceiling while git's own "files changed" is under it --
            # saying "N files" without saying which N is how a reader concludes
            # the tool is miscounting.
            "refusal_reason": (
                f"change set exceeds ceiling: {cs['gated_path_count']} gated paths "
                f"(max {max_files}; {cs['files_changed_count']} files changed by git's count), "
                f"{cs['total_insertions'] + cs['total_deletion_lines']} lines (max {max_lines})"
            ),
            "commit_count": cs["commit_count"],
            "gated_path_count": cs["gated_path_count"],
            "files_changed_count": cs["files_changed_count"],
        }

    # --- Deletion-refusal check: the tree-diff trap this whole module
    # exists to avoid. Commit enumeration cannot manufacture a phantom
    # deletion of a path that was never really deleted (unlike a two-tree
    # diff, which would report the entire export filter as one), but a
    # REAL per-commit deletion of a path the engine actually has, that is
    # not something the export surface covers, is exactly the shape this
    # refuses: a real delete this report should never wave through as an
    # ordinary classification. Only spends the (cheap, but non-zero)
    # surface-pattern read when there is at least one real deletion to
    # check. ---
    deleted_paths = [p for p, info in cs["touched_paths"].items() if "D" in info["statuses"]]
    if deleted_paths:
        surface_patterns_for_deletes = resolve_surface_patterns()
        unsafe_deletions = []
        for remote_path in sorted(deleted_paths):
            engine_path, category = gate.reverse_map_path(remote_path)
            if category == gate.CAT_GENERATED:
                continue  # no engine-side path to delete in the first place
            engine_exists = changeset.blob_hash_at(local_ref, engine_path, repo_dir=repo_dir) is not None
            in_surface = gate.is_in_export_surface(engine_path, surface_patterns_for_deletes)
            if engine_exists and not in_surface:
                unsafe_deletions.append(remote_path)
        if unsafe_deletions:
            return {
                "marker": marker,
                "remote_ref": remote_ref,
                "refused": True,
                "refusal_reason": (
                    "change set deletes path(s) the engine has that resolve outside the "
                    f"export surface: {unsafe_deletions}"
                ),
                "commit_count": cs["commit_count"],
                "gated_path_count": cs["gated_path_count"],
                "files_changed_count": cs["files_changed_count"],
            }

    trust_allowlist = resolve_trust_allowlist()

    # Resolve provenance per commit, once each (not per path). The SOLE
    # commit->PR link is resolve_prs_for_commit (GitHub's commits/pulls API
    # in production) -- a commit's subject is never used to select a PR.
    # The subject's own `(#N)` hint, if present, is consulted only as a
    # must-agree cross-check: a subject claiming a PR the API does not
    # confirm is refused, not trusted either way.
    # Seeded from the caller's cache of already-resolved provenance. Carried-
    # forward paths (extra_paths) reference commits that are no longer in this
    # run's enumeration, and re-resolving them would mean a GitHub round trip
    # per carried commit on every single run, forever. A commit's author never
    # changes, so the verdict is cacheable; the loop below still overwrites any
    # sha it resolves itself, so a cached entry can never shadow a fresh one.
    commit_trust: dict[str, tuple[bool, str]] = dict(known_commit_trust or {})
    commits_out = []
    for c in cs["commits"]:
        sha = c["sha"]
        subject_hint = c.get("subject_pr_hint")
        prs = resolve_prs_for_commit(sha)
        author = None
        resolved_pr = None
        if len(prs) == 0:
            trusted, reason = False, f"commit {sha[:8]} resolves to no PR via commits/pulls; provenance unresolvable"
        elif len(prs) > 1:
            trusted, reason = False, (
                f"commit {sha[:8]} resolves to multiple PRs {sorted(prs)} via commits/pulls; ambiguous, fail closed"
            )
        elif subject_hint is not None and subject_hint != prs[0]:
            trusted, reason = False, (
                f"commit {sha[:8]} subject claims PR #{subject_hint} but commits/pulls resolves PR #{prs[0]} "
                "-- disagreement between subject and API is itself refused"
            )
        else:
            resolved_pr = prs[0]
            author = resolve_pr_author(resolved_pr)
            if author is None:
                trusted, reason = False, f"PR #{resolved_pr} author unreadable"
            else:
                trusted, reason = gate.check_provenance(author, trust_allowlist, is_trusted_author=is_trusted_author)
        commit_trust[sha] = (trusted, reason)
        commits_out.append({**c, "resolved_pr": resolved_pr, "author": author, "trusted": trusted})

    # Carried-forward paths: withheld by an earlier run, not applied, and
    # therefore still owed. They re-enter here so the SAME classifier rules on
    # them -- a second implementation for "re-check the ones we skipped" is
    # exactly the pair of code paths that eventually disagree.
    #
    # Merged AFTER both refusal checks above and non-destructively:
    #   * after the ceiling, because a carried path is not newly-arrived work
    #     and counting it would let a growing backlog refuse every run until
    #     the channel disables itself;
    #   * after the deletion check, so a status recorded in an older run can
    #     never re-trigger a refusal about a deletion already ruled on;
    #   * non-destructively, so a path that is BOTH carried forward and touched
    #     again by a new commit keeps this run's fresh enumeration.
    carried_forward: set[str] = set()
    for remote_path, info in (extra_paths or {}).items():
        if remote_path in cs["touched_paths"]:
            continue
        cs["touched_paths"][remote_path] = {
            "commits": list(info.get("commits", [])),
            "statuses": list(info.get("statuses", [])),
        }
        carried_forward.add(remote_path)

    surface_patterns = resolve_surface_patterns()
    sensitive_prefixes = resolve_sensitive_prefixes()

    # Every status this report can ever assign is pre-seeded here, so a
    # bucket the classifications happen not to hit still appears (empty)
    # in the output rather than being silently absent -- a consumer
    # iterating `buckets` should never have to guess which keys can show up.
    buckets: dict[str, list[str]] = {
        gate.CAT_GENERATED: [],
        gate.CAT_QUARANTINED: [],
        gate.CAT_PATH_UNSAFE: [],
        gate.CAT_OUT_OF_SURFACE: [],
        gate.CAT_NEEDS_APPROVAL: [],
        gate.CAT_COLLISION: [],
        pull.STATUS_CLEAN_APPLY: [],
        pull.STATUS_LOCAL_PATCH: [],
        pull.STATUS_ALREADY_APPLIED: [],
        pull.STATUS_CONFLICT: [],
        pull.STATUS_INTEGRITY_FAIL: [],
        pull.STATUS_REJECTED: [],
    }
    classifications: dict[str, dict] = {}

    # Reverse-map collisions: two different remote paths landing on the
    # same engine path (today only possible via a bug in a future mirror,
    # not the current two mirrors -- see gate.find_reverse_map_collisions'
    # own docstring). Computed once over every touched path so a colliding
    # pair is refused regardless of which one sorts first in the loop below.
    collisions = gate.find_reverse_map_collisions(list(cs["touched_paths"].keys()))

    for remote_path, info in sorted(cs["touched_paths"].items()):
        touching_commits = info["commits"]

        # Gate 1: provenance. Fails closed if ANY touching commit is
        # untrusted -- a path is only as trustworthy as its least-trusted
        # contributor.
        # .get, not [] -- a carried-forward path can name a commit this run
        # never enumerated and whose cached verdict is missing. Unknown
        # provenance is untrusted provenance; it must never be an exception
        # that aborts the whole report, and never a pass.
        _unknown = (False, "provenance unknown for this commit (not resolved in this run, no cached verdict)")
        untrusted_reasons = [
            commit_trust.get(sha, _unknown)[1] for sha in touching_commits if not commit_trust.get(sha, _unknown)[0]
        ]
        if untrusted_reasons:
            classifications[remote_path] = {
                "status": gate.CAT_QUARANTINED,
                "reason": untrusted_reasons[0],
                "commits": touching_commits,
            }
            buckets[gate.CAT_QUARANTINED].append(remote_path)
            continue

        engine_path, pre_category = gate.reverse_map_path(remote_path)
        if pre_category == gate.CAT_GENERATED:
            classifications[remote_path] = {
                "status": gate.CAT_GENERATED,
                "reason": "export-generated artifact with no engine-side source; regenerated by export.sh, never synced",
                "commits": touching_commits,
            }
            buckets[gate.CAT_GENERATED].append(remote_path)
            continue

        if engine_path in collisions:
            classifications[remote_path] = {
                "status": gate.CAT_COLLISION,
                "reason": f"reverse-maps to {engine_path!r} along with {collisions[engine_path]!r}; refusing rather than let one silently win",
                "engine_path": engine_path,
                "commits": touching_commits,
            }
            buckets[gate.CAT_COLLISION].append(remote_path)
            continue

        # Gate 2+3: path safety and export-surface membership.
        category, reason = gate.path_gate(
            remote_path, engine_path, surface_patterns, sensitive_prefixes, target_root=repo_dir
        )
        if category:
            classifications[remote_path] = {
                "status": category,
                "reason": reason,
                "engine_path": engine_path,
                "commits": touching_commits,
            }
            buckets[category].append(remote_path)
            continue

        # Gate 4 cleared -- hash-classify.
        base_hash = changeset.blob_hash_at(marker, remote_path, repo_dir=repo_dir)
        upstream_hash = changeset.blob_hash_at(remote_ref, remote_path, repo_dir=repo_dir)
        local_hash = changeset.blob_hash_at(local_ref, engine_path, repo_dir=repo_dir)

        if upstream_hash is None:
            # The path was deleted on the code plane after the marker (a D
            # status is already in info["statuses"]) and cleared the
            # deletion-refusal check above -- so either the engine has no
            # copy at all, or the copy it has is inside the export surface.
            # Either way this read-only report never proposes the delete
            # itself; it names the status so a human (or the apply step) can
            # decide, using pull.py's own vocabulary rather than a bespoke one.
            status, reason = pull.STATUS_REJECTED, "upstream deletes this path; a read-only report never proposes a delete"
        else:
            status, reason = pull.classify_against_baseline(local_hash, base_hash, upstream_hash)

        classifications[remote_path] = {
            "status": status,
            "reason": reason,
            "engine_path": engine_path,
            "local_hash": local_hash,
            "base_hash": base_hash,
            "upstream_hash": upstream_hash,
            "commits": touching_commits,
        }
        buckets.setdefault(status, []).append(remote_path)

    for remote_path in carried_forward:
        if remote_path in classifications:
            classifications[remote_path]["carried_forward"] = True

    return {
        "marker": marker,
        "remote_ref": remote_ref,
        "refused": False,
        "carried_forward": sorted(carried_forward),
        "commit_count": cs["commit_count"],
        "commits": commits_out,
        "gated_path_count": cs["gated_path_count"],
        "files_changed_count": cs["files_changed_count"],
        "file_deletions": cs["file_deletions"],
        "classifications": classifications,
        "buckets": {k: sorted(v) for k, v in buckets.items()},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", default="refs/synced/code-plane")
    parser.add_argument("--remote", default="code-plane")
    parser.add_argument("--remote-branch", default="main")
    parser.add_argument("--repo-dir", default=str(REPO_ROOT))
    parser.add_argument("--code-repo", default=None, help="code plane slug (default: resolved via backend._repo.CODE_REPO)")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--no-fetch", action="store_true", help="skip the one-ref git fetch (assumes objects already present)")
    parser.add_argument(
        "--local-ref",
        default="main",
        help="what 'the engine's own copy' means when hash-classifying (default: main, not HEAD -- "
        "running from a feature branch must not silently change what gets reported)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code_repo_slug = args.code_repo or _resolve_code_repo()
    try:
        report = classify_report(
            marker=args.marker,
            remote=args.remote,
            remote_branch=args.remote_branch,
            repo_dir=Path(args.repo_dir),
            code_repo_slug=code_repo_slug,
            max_files=args.max_files,
            max_lines=args.max_lines,
            do_fetch=not args.no_fetch,
            local_ref=args.local_ref,
        )
    except changeset.GitError as exc:
        # Fail closed, but readably: an operator (or the loop) reading this
        # tool's exit path should get a reason, not a stack trace, when a
        # fetch or another git call hits a network blip or a bad ref.
        print(json.dumps({"refused": True, "refusal_reason": f"git operation failed: {exc}"}, indent=2, sort_keys=True))
        return EXIT_ERROR
    print(json.dumps(report, indent=2, sort_keys=True))
    return EXIT_REFUSED if report.get("refused") else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
