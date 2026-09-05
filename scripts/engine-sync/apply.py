#!/usr/bin/env python3
"""engine-sync apply + PR generation + seed — Slice B2 Batch B2b of D#1586
(follow-on to Batch B2a's verified fetch, scripts/engine-sync/fetch.py).

Given a verified fetch-out directory produced by `fetch.py fetch` (a
target/ tree, an optional base/ tree, and fetch-report.json) and a sibling
repo's working tree (--target), this tool:

  1. Classifies every upstream-tracked path via pull.py's deterministic
     classifier (imported as a library -- pull.py itself is not modified).
  2. Writes the clean-apply subset to a new `engine-sync/<ver>` branch in
     the sibling's OWN checkout, re-validating path + integrity immediately
     before every write (G7 -- never trust the classify-time bucket alone).
  3. Never writes a file in the enforcer self-protection set (G4), even when
     it would otherwise classify clean-apply -- those paths are flagged and
     surfaced in the PR body instead. This closes T-B2-SELFUPDATE: the
     update mechanism's own guard code can only ever land via human review.
  4. Opens a PR with `gh pr create` using whatever ambient `gh`/`git`
     credentials the SIBLING's own environment already provides. This
     script never reads, stores, or references any sibling access token --
     see the G9 grep-guard test. It never merges a PR (also G9): the only
     `gh pr` subcommand ever invoked is `create`.
  5. Commits/updates engine/applied.json (the lockfile living in the
     SIBLING repo) at apply time, recording a durable per-tag processed
     marker so a re-fired trigger for an already-processed tag is a no-op
     even if a prior PR for that tag was closed unmerged (Spec item 14).
  6. Handles first-adoption (no engine/ dir yet) as an explicit SEED
     operation: no content is written (pull.py never clean-applies on first
     adoption -- see its classify_against_baseline), only
     engine/applied.json is seeded from the sibling's CURRENT tree and a
     seed PR is opened. This adopts the already-drifted tree as the new
     baseline going forward -- documented, intended behavior, not a bug.

The conflict-classified path (real three-way merge + advisory LLM resolver)
is explicitly OUT of scope here -- that is Batch B2c (merge.py / resolver.py).
Any conflict paths found by classify are left completely untouched by this
tool and are only named in the PR body as deferred/needs-review; B2b never
attempts to resolve or write them.

Subcommands:
  apply   Classify + clean-apply (or seed) + commit + push + `gh pr create`
          in the sibling's own checkout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
import manifest as manifest_mod  # noqa: E402  (read_allowlist, sha256_of)
import pull as pull_mod  # noqa: E402  (classify, validate_path, status constants)

PROTECTED_LIST_PATH = _THIS_DIR / "protected.txt"

# Strict charset + length bound for manifest-sourced version/tag strings
# (security-review finding, D#1586 Batch B2b fix round). `engine_version`
# and `target_tag` come straight from the fetched manifest / fetch-report
# -- attacker-controlled, newlines allowed -- and are f-string-interpolated
# into branch names, commit messages, and PR titles/bodies. A semver-ish
# allowlist (no whitespace, no control chars, bounded length) closes the
# prompt-injection / social-engineering vector without needing full semver
# parsing (tags like "v0.2.0" or "seed-0.2.0" are not strict semver).
_VERSION_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def validate_version_tag_string(value: str | None, label: str) -> str:
    """Fail-closed: reject anything that isn't a bounded, single-line,
    semver-charset token before it is used anywhere in a branch name,
    commit message, or PR title/body. Raises ApplyError on rejection --
    never silently truncates or sanitizes."""
    if not isinstance(value, str) or not _VERSION_TAG_RE.fullmatch(value):
        raise ApplyError(
            f"{label} fails strict charset/length validation, refusing to use it in "
            f"a branch name / commit message / PR title / PR body: {value!r}"
        )
    return value


class ApplyError(Exception):
    """Any failure that must abort the apply run. Caller (cmd_apply) treats
    this as fail-closed: non-zero exit. Whatever was already committed only
    ever lives in the sibling's local worktree branch unless/until this
    function itself pushes and opens the PR -- an ApplyError raised before
    that point means no PR is ever opened for the attempted change."""


# --------------------------------------------------------------------------
# Protected set (G4)
# --------------------------------------------------------------------------


def read_protected_set(path: Path = PROTECTED_LIST_PATH) -> set[str]:
    """Fail closed: a missing or unreadable protected-set file is a hard
    error, NEVER an empty set. An empty set would silently disable G4."""
    if not path.is_file():
        raise ApplyError(
            f"enforcer self-protection list is missing (refusing to apply with no G4 "
            f"protection at all): {path}"
        )
    lines = [line.strip() for line in path.read_text().splitlines()]
    protected = {line for line in lines if line and not line.startswith("#")}
    if not protected:
        raise ApplyError(f"enforcer self-protection list is empty (refusing to apply): {path}")
    return protected


# --------------------------------------------------------------------------
# applied.json (sibling-side lockfile)
# --------------------------------------------------------------------------


def load_applied(path: Path) -> dict:
    if path.is_file():
        with open(path) as f:
            data = json.load(f)
        data.setdefault("files", {})
        data.setdefault("processed_tags", [])
        data.setdefault("conflict_resolved_tags", [])
        return data
    return {"engine_version": None, "files": {}, "processed_tags": [], "conflict_resolved_tags": []}


def write_applied(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def already_processed(applied: dict, tag: str) -> bool:
    """Durable de-dup key (Spec item 14): a tag recorded here is skipped on
    every future re-fire, even if the PR opened for it was later closed
    unmerged. This is deliberately NOT "does an open PR currently exist" --
    that check is neither necessary (a closed-unmerged PR must still count
    as processed) nor sufficient (two concurrent triggers could both see no
    open PR yet). A genuinely new attempt for the SAME tag requires a human
    to remove the tag from processed_tags -- that is a human-gated action,
    not something this tool ever does automatically.

    NOTE: this tracks ONLY the clean-apply/seed step (this module). A tag
    can be in processed_tags while conflict-bucket paths for that SAME tag
    are still unresolved -- see already_resolved() below, which is the
    separate durable marker resolver.py owns. Gating resolver.py on this
    function would silently drop pending conflicts on a mixed tag forever
    (a tag with both clean-apply files and conflict files); that was a
    real bug, fixed by keeping the two markers independent."""
    return tag in applied.get("processed_tags", [])


def already_resolved(applied: dict, tag: str) -> bool:
    """Durable de-dup key for resolver.py's conflict-resolution step,
    tracked SEPARATELY from processed_tags (this module's clean-apply/seed
    completion marker) in the same sibling-owned engine/applied.json. A
    mixed tag (some clean-apply files, some conflict files) is marked in
    processed_tags by _do_clean_apply as soon as its clean subset lands,
    while its conflict subset remains unresolved until resolver.py opens
    a NEEDS-REVIEW PR and marks conflict_resolved_tags here -- resolver.py
    must gate on THIS function, never on already_processed()."""
    return tag in applied.get("conflict_resolved_tags", [])


def branch_name_for(engine_version: str | None, seed: bool = False) -> str:
    ver = engine_version or "unknown"
    return f"engine-sync/seed-{ver}" if seed else f"engine-sync/{ver}"


# --------------------------------------------------------------------------
# git / gh subprocess helpers -- all executed with cwd=target_root, i.e. IN
# THE SIBLING'S OWN CHECKOUT. This module never authenticates as a sibling
# itself: `git push` and `gh pr create` rely entirely on whatever ambient
# credentials the sibling's own environment (its own auth token / git
# credential helper) already provides. Grepped by the G9 test.
#
# Credential isolation (security-review finding, D#1586 Batch B2b fix
# round -- CWE-668): every subprocess below is invoked with an EXPLICIT
# env= that has autonomous-forever's own gh/git credential env vars
# stripped. Without this, `gh pr create` in the sibling checkout would
# silently inherit af's ambient GH_TOKEN/GITHUB_TOKEN (gh prefers an
# env-var token over any stored credential helper) and open the PR AS
# autonomous-forever instead of as the sibling. Structurally incapable of
# acting as af, not just "documented as never doing so".
# --------------------------------------------------------------------------

# Credential env vars that must never reach a sibling's own git/gh
# subprocess call -- popped from a COPY of os.environ, never mutated in
# place (os.environ itself, and this process's own ambient auth, must
# stay untouched for anything af itself still needs to do).
_AF_CREDENTIAL_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST")


def _sibling_env() -> dict:
    """A copy of the current environment with af's own gh/git credential
    vars removed, safe to pass to any subprocess that touches the
    sibling's checkout."""
    env = dict(os.environ)
    for var in _AF_CREDENTIAL_ENV_VARS:
        env.pop(var, None)
    return env


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=_sibling_env())
    if check and result.returncode != 0:
        raise ApplyError(f"`{' '.join(cmd)}` failed: {result.stderr.strip()}")
    return result


def remote_branch_exists(target_root: Path, branch: str) -> bool:
    result = _run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], target_root, check=False)
    return result.returncode == 0


def establish_clean_base(target_root: Path, default_branch: str) -> None:
    """Pre-flight state check (Spec item 15): detect a dirty / detached /
    non-default branch (the lafk-demo `studio-note-pulse-animation` case)
    and explicitly target the sibling's default branch. Only ever discards
    UNTRACKED-BUT-COMMITTED local divergence by moving the branch pointer
    to match origin/<default_branch> -- never discards uncommitted work
    (the dirty-tree check below aborts before any of that is possible)."""
    status = _run(["git", "status", "--porcelain"], target_root, check=False)
    if status.returncode != 0:
        raise ApplyError("could not read sibling git status; refusing to apply")
    if status.stdout.strip():
        raise ApplyError(
            "sibling working tree is dirty (uncommitted changes present); refusing to "
            "apply onto an unclean base"
        )

    fetch = _run(["git", "fetch", "origin", default_branch], target_root, check=False)
    if fetch.returncode != 0:
        raise ApplyError(f"could not fetch origin/{default_branch}: {fetch.stderr.strip()}")

    switch = _run(["git", "switch", default_branch], target_root, check=False)
    if switch.returncode != 0:
        switch = _run(["git", "switch", "-c", default_branch, f"origin/{default_branch}"], target_root, check=False)
        if switch.returncode != 0:
            raise ApplyError(
                f"could not establish a clean base on default branch {default_branch!r}: "
                f"{switch.stderr.strip()}"
            )

    # Tree is already confirmed clean above, so this only ever moves the
    # branch pointer -- it cannot discard any uncommitted work.
    reset = _run(["git", "reset", "--hard", f"origin/{default_branch}"], target_root, check=False)
    if reset.returncode != 0:
        raise ApplyError(f"could not reset to origin/{default_branch}: {reset.stderr.strip()}")


def create_branch(target_root: Path, branch: str, default_branch: str) -> None:
    _run(["git", "switch", "-c", branch, default_branch], target_root)


def commit_and_push(target_root: Path, branch: str, relpaths: list[str], message: str) -> None:
    _run(["git", "add", "--", *relpaths], target_root)
    staged = _run(["git", "diff", "--cached", "--name-only"], target_root, check=False)
    if not staged.stdout.strip():
        raise ApplyError("nothing staged to commit -- refusing to push an empty change")
    _run(["git", "commit", "-m", message], target_root)

    # Re-checked IMMEDIATELY before push (Spec item 14 concurrency guard):
    # two concurrent triggers for the same tag must not both succeed in
    # pushing a branch and opening a duplicate PR.
    if remote_branch_exists(target_root, branch):
        raise ApplyError(
            f"branch {branch!r} already exists on origin immediately before push -- a "
            f"concurrent apply run for this tag is already in flight, aborting to avoid "
            f"a duplicate PR"
        )
    push = _run(["git", "push", "-u", "origin", branch], target_root, check=False)
    if push.returncode != 0:
        raise ApplyError(f"git push failed for {branch!r}: {push.stderr.strip()}")


def create_pr(target_root: Path, branch: str, default_branch: str, title: str, body: str) -> str:
    """The ONLY `gh pr` subcommand this module ever invokes is `create`.
    A PR is never auto-merged -- opened for human review, clean-apply and
    protected/conflict-flagged alike (G9)."""
    result = subprocess.run(
        ["gh", "pr", "create", "--base", default_branch, "--head", branch, "--title", title, "--body", body],
        cwd=target_root,
        capture_output=True,
        text=True,
        env=_sibling_env(),
    )
    if result.returncode != 0:
        raise ApplyError(f"gh pr create failed: {result.stderr.strip()}")
    return result.stdout.strip()


# --------------------------------------------------------------------------
# G7 -- apply-time re-validation (TOCTOU / symlink defense on write)
# --------------------------------------------------------------------------


def safe_write_blob(relpath: str, src_blob: Path, target_root: Path, claimed_hash: str) -> None:
    """Never trust the B1 classify-time bucket alone: re-run validate_path
    AND re-verify SHA-256 immediately before writing, on the write target,
    refusing to follow symlinks anywhere in the target or its parent
    chain."""
    includes, excludes = manifest_mod.read_allowlist()
    valid, reason = pull_mod.validate_path(relpath, target_root, includes, excludes)
    if not valid:
        raise ApplyError(f"apply-time path validation failed for {relpath!r}: {reason}")

    if not src_blob.is_file():
        raise ApplyError(f"apply-time integrity check failed for {relpath!r}: verified blob is missing")
    actual_hash = manifest_mod.sha256_of(src_blob)
    if actual_hash != claimed_hash:
        raise ApplyError(
            f"apply-time integrity re-check failed for {relpath!r}: blob content changed "
            f"between classify and write (manifest claims {claimed_hash}, now {actual_hash})"
        )

    dest = target_root / relpath

    # No-follow: reject if the write target itself, or any existing parent
    # directory between it and target_root, is a symlink.
    node = dest
    while node != target_root:
        if node.exists() and node.is_symlink():
            raise ApplyError(f"apply-time symlink rejection: {node} is a symlink")
        node = node.parent

    try:
        resolved_parent = dest.parent.resolve()
        # dest.parent may not exist yet -- resolve() with strict=False (the
        # default) still normalizes the existing prefix, which is what the
        # symlink walk above already covers component-by-component.
        resolved_parent.relative_to(target_root.resolve())
    except ValueError:
        raise ApplyError(f"apply-time path resolves outside --target root: {relpath!r}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        raise ApplyError(f"apply-time symlink rejection: write target is a symlink: {relpath}")
    dest.write_bytes(src_blob.read_bytes())


# --------------------------------------------------------------------------
# PR body rendering
# --------------------------------------------------------------------------


def _seed_pr_body(engine_version: str, target_tag: str, class_report: dict) -> str:
    lines = [
        "First engine-sync adoption for this repo -- no `engine/` directory existed yet.",
        "",
        f"This seeds `engine/applied.json` from the CURRENT tree (hashes of whatever is "
        f"on disk right now for every upstream-tracked path), tagged as engine "
        f"`{engine_version}` (release `{target_tag}`). No file content is changed by this "
        "PR -- only the lockfile is added.",
        "",
        "That means any files that already drifted from upstream before this channel "
        "existed are adopted IN PLACE as the new baseline going forward, not silently "
        "overwritten. Future engine-sync runs will diff against this baseline, so drift "
        "from this point forward is what gets classified clean-apply / conflict.",
        "",
        f"Files seeded into the lockfile: {len(class_report['already_applied']) + len(class_report['local_patch'])}",
    ]
    return "\n".join(lines)


def _clean_apply_pr_body(
    engine_version: str,
    target_tag: str,
    written: list[str],
    protected_flagged: list[str],
    conflicts: list[str],
) -> str:
    lines = [
        f"engine-sync update to `{engine_version}` (release `{target_tag}`).",
        "",
        f"Applied {len(written)} clean-apply file(s) -- local content matched the "
        "recorded baseline exactly, so upstream's version was written with no risk of "
        "clobbering local changes.",
    ]
    if written:
        lines.append("")
        lines.append("Applied:")
        lines.extend(f"- `{p}`" for p in sorted(written))
    if protected_flagged:
        lines += [
            "",
            "**Needs human review -- not applied automatically:**",
            "The following files are in the engine-sync self-protection set (they are "
            "the update mechanism's own guard code / trust anchor / lockfile). They are "
            "never auto-applied even when the content would otherwise be a clean, "
            "conflict-free update -- review and merge the change yourself if you want it.",
        ]
        lines.extend(f"- `{p}`" for p in sorted(protected_flagged))
    if conflicts:
        lines += [
            "",
            "**Conflicts deferred:**",
            "The following paths diverged both locally and upstream. This tool "
            "(engine-sync apply, Batch B2b) does not attempt to merge or resolve "
            "conflicts -- that lands in a follow-up batch with a real three-way merge "
            "and an advisory-only LLM resolver. These paths were left completely "
            "untouched:",
        ]
        lines.extend(f"- `{p}`" for p in sorted(conflicts))
    lines += ["", "This PR was opened automatically and will never be auto-merged."]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# apply orchestration
# --------------------------------------------------------------------------


def _do_seed(
    target_root: Path,
    default_branch: str,
    class_report: dict,
    applied: dict,
    applied_path: Path,
    target_tag: str,
    engine_version: str,
) -> int:
    seeded_files: dict[str, str] = {}
    for relpath, info in class_report["classifications"].items():
        if info["status"] in (pull_mod.STATUS_REJECTED, pull_mod.STATUS_INTEGRITY_FAIL):
            continue
        local_path = target_root / relpath
        if local_path.is_file() and not local_path.is_symlink():
            seeded_files[relpath] = manifest_mod.sha256_of(local_path)

    branch = branch_name_for(engine_version, seed=True)
    if remote_branch_exists(target_root, branch):
        print(f"branch {branch!r} already exists on origin -- concurrent seed apply in progress, skipping")
        return 0

    create_branch(target_root, branch, default_branch)

    new_applied = {
        "engine_version": engine_version,
        "files": seeded_files,
        "processed_tags": sorted(set(applied.get("processed_tags", [])) | {target_tag}),
    }
    write_applied(applied_path, new_applied)

    commit_and_push(
        target_root,
        branch,
        [str(applied_path.relative_to(target_root))],
        f"engine-sync: seed baseline at {engine_version}",
    )
    body = _seed_pr_body(engine_version, target_tag, class_report)
    create_pr(
        target_root,
        branch,
        default_branch,
        title=f"engine-sync: adopt engine {engine_version} (first adoption)",
        body=body,
    )
    print(f"opened seed PR for engine {engine_version} on branch {branch}")
    return 0


def _do_clean_apply(
    target_root: Path,
    default_branch: str,
    manifest_dir: Path,
    class_report: dict,
    protected: set[str],
    applied: dict,
    applied_path: Path,
    target_tag: str,
    engine_version: str,
) -> int:
    protected_flagged: list[str] = []
    to_write: list[str] = []
    for relpath in class_report["clean_apply"]:
        if relpath in protected:
            protected_flagged.append(relpath)
        else:
            to_write.append(relpath)

    conflicts = list(class_report["conflict"])  # deferred to Batch B2c, never written here

    if not to_write and not protected_flagged:
        print(
            f"tag {target_tag!r}: no clean-apply or protected candidates "
            f"({len(conflicts)} conflict path(s) deferred); no PR opened, tag not marked "
            f"processed so a future conflict-aware run can still act on it"
        )
        return 0

    branch = branch_name_for(engine_version)
    if remote_branch_exists(target_root, branch):
        print(f"branch {branch!r} already exists on origin -- concurrent apply in progress, skipping")
        return 0

    create_branch(target_root, branch, default_branch)

    manifest = json.loads((manifest_dir / "manifest.json").read_text())
    upstream_files = manifest.get("files", {})

    written: list[str] = []
    for relpath in sorted(to_write):
        claimed_hash = upstream_files[relpath]
        src_blob = manifest_dir / relpath
        safe_write_blob(relpath, src_blob, target_root, claimed_hash)
        written.append(relpath)

    new_applied = dict(applied)
    new_applied["processed_tags"] = sorted(set(applied.get("processed_tags", [])) | {target_tag})
    if written:
        files = dict(applied.get("files", {}))
        for relpath in written:
            files[relpath] = upstream_files[relpath]
        new_applied["files"] = files
        new_applied["engine_version"] = engine_version
    write_applied(applied_path, new_applied)

    commit_paths = written + [str(applied_path.relative_to(target_root))]
    commit_and_push(target_root, branch, commit_paths, f"engine-sync: apply {engine_version}")

    body = _clean_apply_pr_body(engine_version, target_tag, written, protected_flagged, conflicts)
    create_pr(
        target_root,
        branch,
        default_branch,
        title=f"engine-sync: apply {engine_version}",
        body=body,
    )
    print(f"opened PR for engine {engine_version} on branch {branch} ({len(written)} file(s) applied)")
    return 0


def run_apply(target: Path, fetch_out: Path, default_branch: str) -> int:
    target_root = target.resolve()
    if not target_root.is_dir():
        raise ApplyError(f"--target is not a directory: {target_root}")

    report_path = fetch_out / "fetch-report.json"
    if not report_path.is_file():
        raise ApplyError(f"no fetch-report.json under --fetch-out: {fetch_out}")
    fetch_report = json.loads(report_path.read_text())
    # Validate at the trust boundary, before either value is ever used in a
    # branch name, commit message, or PR title/body (security-review finding).
    target_tag = validate_version_tag_string(fetch_report["target"]["tag"], "target_tag")
    engine_version = validate_version_tag_string(fetch_report["target"]["engine_version"], "engine_version")
    manifest_dir = fetch_out / "target"
    if not manifest_dir.is_dir():
        raise ApplyError(f"no target/ tree under --fetch-out: {manifest_dir}")

    establish_clean_base(target_root, default_branch)

    applied_path = target_root / "engine" / "applied.json"
    applied = load_applied(applied_path)

    if already_processed(applied, target_tag):
        print(f"tag {target_tag!r} is already recorded as processed for this sibling -- nothing to do")
        return 0

    protected = read_protected_set()
    class_report = pull_mod.classify(target_root, manifest_dir)

    if class_report["first_adoption"]:
        return _do_seed(target_root, default_branch, class_report, applied, applied_path, target_tag, engine_version)
    return _do_clean_apply(
        target_root,
        default_branch,
        manifest_dir,
        class_report,
        protected,
        applied,
        applied_path,
        target_tag,
        engine_version,
    )


def cmd_apply(args: argparse.Namespace) -> int:
    try:
        return run_apply(Path(args.target), Path(args.fetch_out), args.default_branch)
    except ApplyError as exc:
        print(f"error: apply failed: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apply.py",
        description=(
            "Slice B2b: classify + clean-apply (or first-adoption seed) + commit + push "
            "+ `gh pr create` in the sibling's own checkout, using only the sibling's own "
            "ambient git/gh credentials. Never merges a PR. Enforcer self-protection set "
            "(G4) files are never clean-applied. Conflict paths are deferred to Batch B2c."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    apply_parser = sub.add_parser(
        "apply",
        help="Apply a verified fetch-out directory's clean-apply subset (or seed) into --target.",
    )
    apply_parser.add_argument(
        "--target",
        required=True,
        help="Path to the sibling repo's working tree (a checkout with an 'origin' remote "
        "reachable via ambient git/gh credentials).",
    )
    apply_parser.add_argument(
        "--fetch-out",
        required=True,
        help="Path to a fetch.py `fetch --out` directory (target/, optional base/, "
        "fetch-report.json).",
    )
    apply_parser.add_argument(
        "--default-branch",
        required=True,
        help="The sibling's default branch (e.g. 'main'). apply.py explicitly "
        "establishes a clean base on this branch before doing anything else.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "apply":
        return cmd_apply(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
