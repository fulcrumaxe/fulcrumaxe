#!/usr/bin/env python3
"""engine-sync pull mechanism — Slice B1 of D#1535 (cross-project
update-distribution channel, follow-on to Slice A / D#1528).

Deterministic, side-effect-free classifier. Given a sibling repo's working
tree (--target) and an upstream engine manifest+blob mirror (--manifest),
this tool classifies every upstream-tracked path as one of:

  clean-apply      local == baseline               -> safe to pull upstream
  local-patch      local diverged, upstream did not -> skip, don't clobber
  already-applied  local already matches upstream    -> no-op
  conflict         local AND upstream both diverged  -> hold for a human /
                                                         Slice B2 resolver
  rejected         path fails the allowlist / path-traversal gate
  integrity-fail   recomputed blob sha256 != manifest-claimed hash

It NEVER writes to any file inside --target. The only artifact it may write
is the JSON report at an explicitly-passed --report-out path. It never
generates a PR, never runs `git merge-file`, never installs a package, and
never invokes an LLM. All of that is Slice B2 (see the seam comment below).

Subcommands:
  classify   Compute the classification report described above and print it
             (and optionally write it to --report-out). Requires --dry-run;
             Slice B1 has no apply/write mode.

B2 seam (NOT implemented here, by design — see D#1535 Spec, "Slice B2"):
  Live read-only fetch of the upstream manifest+blobs would use
  `gh api repos/<owner>/<repo>/contents?ref=<pinned-sha>`, gated by a
  `git verify-tag`-style signed-tag -> pinned-key -> SHA chain before any
  fetched content is trusted. That fetch/verify machinery, PR generation
  (`gh pr create`, `git push`, `git switch -c`), the real `git merge-file`
  three-way merge, and the advisory-only conflict-resolver LLM are all
  deferred to Slice B2. This file deliberately contains no `subprocess`,
  `os.system`, `pip install`, `npm install`/`npm ci`, `git merge-file`, or
  `postinstall` invocation — Slice B1's grep guard test (Spec item 9)
  asserts that. In this file, --manifest only ever points at a local
  directory (a fixture, or a pre-fetched mirror produced by some future,
  separately-reviewed B2 fetch step) — never a live ref.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Import Slice A's manifest.py as a library (per D#1535 Implementation Notes:
# do NOT modify manifest.py, do NOT add pull-specific helpers to it).
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
import manifest as manifest_mod  # noqa: E402  (read_allowlist, is_included, is_excluded, sha256_of)

STATUS_CLEAN_APPLY = "clean-apply"
STATUS_LOCAL_PATCH = "local-patch"
STATUS_ALREADY_APPLIED = "already-applied"
STATUS_CONFLICT = "conflict"
STATUS_REJECTED = "rejected"
STATUS_INTEGRITY_FAIL = "integrity-fail"

ALL_STATUSES = (
    STATUS_CLEAN_APPLY,
    STATUS_LOCAL_PATCH,
    STATUS_ALREADY_APPLIED,
    STATUS_CONFLICT,
    STATUS_REJECTED,
    STATUS_INTEGRITY_FAIL,
)


def load_manifest(path: Path) -> dict:
    """Read a manifest.json (or engine/applied.json — same shape) from disk.

    This is deliberately a `pull.py`-local helper, not added to manifest.py,
    per D#1535 Implementation Notes ("If a manifest-load helper is genuinely
    needed in the shared library, note the reasoning in the PR description" —
    it isn't needed there; pull.py is the only consumer that needs to load an
    *arbitrary* path, not always REPO_ROOT/engine/manifest.json).
    """
    with open(path) as f:
        return json.load(f)


def canonicalize_relpath(relpath: str) -> tuple[str | None, str]:
    """Fail-closed manifest-path canonicalization (security-review finding,
    D#1586 Batch B2b fix round — CWE-706/494 G4 bypass).

    The G4 protected-set gate in apply.py is an EXACT-STRING membership
    check (`relpath in protected`). Every filesystem write, however,
    normalizes the path (`.`/`//`/trailing-slash all collapse to the same
    real file). A manifest key using a non-canonical form of a protected
    path (e.g. `scripts/engine-sync/./pull.py`) would pass the old
    traversal-only checks, miss the protected-set string match, and land
    on the real file — defeating G4 entirely.

    This allows exactly ONE canonical form per real path: the input is
    canonicalized once (os.path.normpath) and REJECTED outright if it
    differs from its own canonical form in any way — no silent rewriting
    of the incoming string. That covers `.` segments, `//`, and trailing
    slashes. Backslashes and leading/trailing whitespace are rejected
    directly since normpath does not touch those on POSIX.

    Returns (canonical_path, reason). canonical_path is None (reason
    non-empty) when the input is rejected."""
    if not relpath or not relpath.strip():
        return None, "empty path"
    if relpath != relpath.strip():
        return None, "leading/trailing whitespace rejected"
    if "\\" in relpath:
        return None, "backslash path separator rejected"

    normalized = os.path.normpath(relpath)
    if relpath != normalized:
        return None, (
            "non-canonical path form rejected (contains '.', '//', a trailing "
            "slash, or similar) -- fail closed, no silent normalization"
        )
    return normalized, ""


def _matches_disk_casing(candidate: Path, target_root: Path) -> bool:
    """Case-insensitive-filesystem guard: walk each path component from
    target_root down to candidate and confirm any EXISTING directory/file
    entry matches the requested name byte-for-byte. On a case-insensitive
    filesystem (e.g. macOS default), a case-variant manifest key could
    otherwise resolve to an already-protected file on disk while still
    evading the protected-set's exact-string comparison. Only flags a
    genuine case-differing collision — a path that is simply new (no
    existing entry of any casing) is not rejected here."""
    current = target_root
    try:
        parts = candidate.relative_to(target_root).parts
    except ValueError:
        return True  # outside target_root -- handled by the caller's own check
    for part in parts:
        if not current.is_dir():
            return True  # nothing on disk at this level yet -- no collision possible
        try:
            entries = {e.name for e in os.scandir(current)}
        except OSError:
            return True
        if part not in entries:
            if any(e.lower() == part.lower() for e in entries):
                return False
            return True
        current = current / part
    return True


def validate_path(
    relpath: str,
    target_root: Path,
    includes: list[str],
    excludes: list[str],
) -> tuple[bool, str]:
    """Sibling-side allowlist + path-traversal enforcement (security-expert,
    design-time requirement). Never trust the manifest's own path claims —
    a compromised upstream manifest.json/allowlist.txt could otherwise smuggle
    an escape. Returns (is_valid, reason). reason is empty when valid."""
    canonical, reason = canonicalize_relpath(relpath)
    if canonical is None:
        return False, reason
    relpath = canonical

    p = Path(relpath)
    if p.is_absolute():
        return False, "absolute path rejected"
    if ".." in p.parts:
        return False, "path traversal ('..') rejected"

    if not manifest_mod.is_included(relpath, includes):
        return False, "not covered by any allowlist include pattern"
    if manifest_mod.is_excluded(relpath, excludes):
        # Deny always wins, same as Slice A's collect_files() -- a manifest
        # entry can never override the sibling's own exclude patterns.
        return False, "denied by allowlist exclude pattern (deny wins)"

    candidate = target_root / relpath
    try:
        resolved = candidate.resolve()
        resolved.relative_to(target_root.resolve())
    except ValueError:
        return False, "resolves outside --target root"

    if candidate.is_symlink():
        return False, "symlink rejected"

    if not _matches_disk_casing(candidate, target_root.resolve()):
        return False, "case-variant path rejected (does not match on-disk casing)"

    return True, ""


def classify_against_baseline(
    local_hash: str | None, base_hash: str | None, upstream_hash: str
) -> tuple[str, str]:
    """Deterministic hash-classification decision table (D#1535 core).
    base_hash is None only when a baseline lockfile exists but has no entry
    for this specific path yet (treated the same as whole-lockfile
    first-adoption for that one path -- adopt-in-place, never clean-apply
    over an unknown tree)."""
    if base_hash is None:
        if local_hash == upstream_hash:
            return STATUS_ALREADY_APPLIED, "no recorded baseline; local already matches upstream"
        return STATUS_LOCAL_PATCH, "no recorded baseline (first adoption); adopt-in-place, not auto-applied"

    if local_hash == base_hash:
        return STATUS_CLEAN_APPLY, ""
    if upstream_hash == base_hash:
        return STATUS_LOCAL_PATCH, "local diverged from baseline; upstream unchanged"
    if local_hash == upstream_hash:
        return STATUS_ALREADY_APPLIED, "local already matches upstream"
    return STATUS_CONFLICT, "local and upstream both diverged from baseline"


def build_report(
    target_root: Path,
    manifest_dir: Path,
    upstream_manifest: dict,
    first_adoption: bool,
    classifications: dict[str, dict[str, str]],
) -> dict:
    buckets: dict[str, list[str]] = {status: [] for status in ALL_STATUSES}
    for relpath, info in classifications.items():
        buckets[info["status"]].append(relpath)
    for status in buckets:
        buckets[status].sort()

    return {
        "target": str(target_root),
        "manifest_source": str(manifest_dir),
        "upstream_engine_version": upstream_manifest.get("engine_version"),
        "first_adoption": first_adoption,
        "classifications": classifications,
        "clean_apply": buckets[STATUS_CLEAN_APPLY],
        "local_patch": buckets[STATUS_LOCAL_PATCH],
        "already_applied": buckets[STATUS_ALREADY_APPLIED],
        "conflict": buckets[STATUS_CONFLICT],
        "rejected": buckets[STATUS_REJECTED],
        "integrity_fail": buckets[STATUS_INTEGRITY_FAIL],
    }


def classify(target_root: Path, manifest_dir: Path) -> dict:
    """Pure, side-effect-free: only ever reads from target_root and
    manifest_dir. Never writes anything. Caller decides whether/where to
    write the returned report."""
    includes, excludes = manifest_mod.read_allowlist()

    manifest_path = manifest_dir / "manifest.json"
    upstream_manifest = load_manifest(manifest_path)

    applied_path = target_root / "engine" / "applied.json"
    first_adoption = not applied_path.is_file()
    applied = load_manifest(applied_path) if not first_adoption else {"files": {}}
    applied_files = applied.get("files", {})

    classifications: dict[str, dict[str, str]] = {}
    for relpath in sorted(upstream_manifest.get("files", {}).keys()):
        claimed_hash = upstream_manifest["files"][relpath]

        valid, reason = validate_path(relpath, target_root, includes, excludes)
        if not valid:
            classifications[relpath] = {"status": STATUS_REJECTED, "reason": reason}
            continue

        blob_path = manifest_dir / relpath
        if not blob_path.is_file():
            classifications[relpath] = {
                "status": STATUS_INTEGRITY_FAIL,
                "reason": "upstream blob missing for manifest-claimed path",
            }
            continue

        actual_upstream_hash = manifest_mod.sha256_of(blob_path)
        if actual_upstream_hash != claimed_hash:
            # Integrity gate: a fetched blob's real content must match what
            # the manifest claims before it can ever reach clean-apply.
            classifications[relpath] = {
                "status": STATUS_INTEGRITY_FAIL,
                "reason": "recomputed sha256 does not match manifest-claimed hash",
            }
            continue

        local_path = target_root / relpath
        local_hash = manifest_mod.sha256_of(local_path) if local_path.is_file() else None
        base_hash = applied_files.get(relpath) if not first_adoption else None

        status, reason = classify_against_baseline(local_hash, base_hash, claimed_hash)
        classifications[relpath] = {"status": status, "reason": reason} if reason else {"status": status}

    return build_report(target_root, manifest_dir, upstream_manifest, first_adoption, classifications)


def cmd_classify(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print(
            "error: Slice B1 only supports --dry-run classification; "
            "apply mode is a Slice B2 feature and does not exist yet.",
            file=sys.stderr,
        )
        return 2

    target_root = Path(args.target)
    if not target_root.is_dir():
        print(f"error: --target is not a directory: {target_root}", file=sys.stderr)
        return 2
    target_root = target_root.resolve()

    manifest_dir = Path(args.manifest)
    if not manifest_dir.is_dir():
        print(
            f"error: --manifest must be a local directory in Slice B1 (got: {manifest_dir}). "
            "Live ref/tag fetch is a Slice B2 feature (see the B2 seam comment "
            "at the top of pull.py) — pass a pre-fetched local mirror instead.",
            file=sys.stderr,
        )
        return 2
    manifest_dir = manifest_dir.resolve()

    manifest_path = manifest_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"error: no manifest.json found under --manifest dir: {manifest_dir}", file=sys.stderr)
        return 2

    report = classify(target_root, manifest_dir)

    print(json.dumps(report, indent=2, sort_keys=True))

    if args.report_out:
        report_out = Path(args.report_out)
        report_out.parent.mkdir(parents=True, exist_ok=True)
        with open(report_out, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
            f.write("\n")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pull.py",
        description=(
            "Slice B1: deterministic, side-effect-free hash-classification of a "
            "sibling's working tree against an upstream engine manifest. "
            "Dry-run only -- never writes to --target, never opens a PR, never "
            "invokes an LLM."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    classify_parser = sub.add_parser(
        "classify",
        help="Classify every upstream-tracked path as clean-apply / local-patch / "
        "already-applied / conflict / rejected / integrity-fail.",
    )
    classify_parser.add_argument(
        "--target",
        required=True,
        help="Path to the sibling repo's working tree (reads engine/applied.json and "
        "the tracked files under it; never writes here).",
    )
    classify_parser.add_argument(
        "--manifest",
        required=True,
        help="Path to a local directory containing manifest.json plus the upstream "
        "blob files at their manifest-relative paths. Slice B1 does not support a "
        "live ref/tag -- see the B2 seam comment at the top of this file.",
    )
    classify_parser.add_argument(
        "--report-out",
        default=None,
        help="Optional path to write the JSON classification report to. This is the "
        "ONLY file this tool ever writes, and only when this flag is explicitly passed.",
    )
    classify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Required in Slice B1 -- classify-only, no apply mode exists yet.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "classify":
        return cmd_classify(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
