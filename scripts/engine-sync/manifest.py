#!/usr/bin/env python3
"""engine-sync manifest generator/verifier.

Slice A of D#1528 (cross-project update-distribution channel). Pure stdlib,
read-only with respect to the sibling repo concept: this tool only ever
touches files inside THIS repo (the framework/engine source of truth) and
writes only engine/manifest.json. It never spawns an agent, never pushes,
never opens a PR.

Subcommands:
  generate   Walk the allowlist, hash every included/non-excluded file with
             SHA-256, and write engine/manifest.json (sorted keys, no
             timestamps in the hashed or written content -> deterministic).
             Refuses (exit 2, nothing written) if the tree about to be
             hashed looks like the private/engine plane rather than the code
             plane this manifest targets -- see detect_wrong_plane() (D#2510).
  verify     Recompute hashes for every file listed in engine/manifest.json
             against the current working tree, AND recompute the live
             allowlist candidate set so a file that matches the allowlist but
             was never pinned (`added`) is reportable -- not just `drifted`
             and `missing`. Exits 0 if the pinned set exactly matches the
             candidate set and every hash agrees; exits non-zero and names
             every offending path otherwise. Also refuses to report clean
             about a manifest with an empty or absent `files` key (D#1928) --
             a checker that passes on zero pinned files is the defect it
             exists to catch, not a clean bill of health.

Manifest shape:
  {
    "engine_version": "0.1.0",
    "generated_from": "manifest.py",
    "files": {
      "scripts/some-framework-script.sh": "<64-hex sha256>",
      ...
    }
  }
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = REPO_ROOT / "scripts" / "engine-sync" / "allowlist.txt"
MANIFEST_PATH = REPO_ROOT / "engine" / "manifest.json"
VERSION_PATH = REPO_ROOT / "engine" / "VERSION"


def read_allowlist(path: Path = ALLOWLIST_PATH) -> tuple[list[str], list[str]]:
    """Parse allowlist.txt into (include_patterns, exclude_patterns)."""
    includes: list[str] = []
    excludes: list[str] = []
    section = None
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[include]":
            section = "include"
            continue
        if line == "[exclude]":
            section = "exclude"
            continue
        if section == "include":
            includes.append(line)
        elif section == "exclude":
            excludes.append(line)
    return includes, excludes


def is_excluded(relpath: str, exclude_patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relpath, pat) for pat in exclude_patterns)


def is_included(relpath: str, include_patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relpath, pat) for pat in include_patterns)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(
    root: Path, include_patterns: list[str], exclude_patterns: list[str]
) -> dict[str, str]:
    """Walk root, return {relpath: sha256} for every allowlisted, non-excluded
    regular file. Deny (exclude) always wins over include, regardless of
    matching order — this is the design-time hard gate."""
    files: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relpath = path.relative_to(root).as_posix()
        if not is_included(relpath, include_patterns):
            continue
        if is_excluded(relpath, exclude_patterns):
            # Deny-list wins even if an include glob matched. This is the
            # security-required hard gate (Spec item 5) -- never silently
            # pull in project/secret/state paths just because a future
            # include glob happens to be broad.
            continue
        files[relpath] = sha256_of(path)
    return files


def read_engine_version() -> str:
    return VERSION_PATH.read_text().strip()


def detect_wrong_plane(root: Path) -> str | None:
    """Return a refusal reason if `root` is not the code-plane tree this
    manifest targets, else None (D#2510).

    `generate` used to hash whatever tree the script file happened to sit
    in -- run it in place inside an executor's private-plane worktree and it
    silently produced a well-formed manifest, exit 0, pinned to the wrong
    repo's file contents. One measured incident re-pinned ~150 files nobody
    touched; a test that only exercises the correct-tree path cannot see
    this defect.

    `archive/` is a directory the Archive Protocol (CLAUDE.md) keeps
    populated with real files on the private/engine plane and denies
    entirely from the code plane (scripts/ci/publish-denylist.sh) -- a
    populated `archive/` at `root` is therefore a reliable, independently
    enforced signal that `root` is not the code-plane tree engine/
    manifest.json describes.
    """
    archive_dir = root / "archive"
    if archive_dir.is_dir() and any(archive_dir.iterdir()):
        return (
            f"{root} contains a populated archive/ directory -- that marks it as "
            "the private/engine plane, not the code plane this manifest targets. "
            "Generating here would pin engine/manifest.json to the wrong repo's "
            "file contents.\n"
            "Use the scratch-extraction recipe instead:\n"
            "  SC=$(mktemp -d)\n"
            '  git archive code-plane/main | tar -x -C "$SC"\n'
            '  git show <pr-head-ref>:<edited/path> > "$SC/<edited/path>"\n'
            '  python3 "$SC/scripts/engine-sync/manifest.py" generate\n'
            '  # "$SC/engine/manifest.json" is the file to add to the PR'
        )
    return None


def cmd_generate(_args: argparse.Namespace) -> int:
    wrong_plane = detect_wrong_plane(REPO_ROOT)
    if wrong_plane is not None:
        print(f"error: refusing to generate -- {wrong_plane}", file=sys.stderr)
        return 2

    includes, excludes = read_allowlist()
    files = collect_files(REPO_ROOT, includes, excludes)

    # Design-time hard gate: assert no denied path made it into the set,
    # even though collect_files() already enforces this. Belt-and-suspenders
    # so a future refactor of collect_files can't silently regress this.
    denied = [f for f in files if is_excluded(f, excludes)]
    assert not denied, f"BUG: denied paths leaked into manifest: {denied}"

    manifest = {
        "engine_version": read_engine_version(),
        "generated_from": "manifest.py",
        "files": {k: files[k] for k in sorted(files)},
    }

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"wrote {MANIFEST_PATH} ({len(files)} files, engine_version={manifest['engine_version']})")
    return 0


def cmd_verify(_args: argparse.Namespace) -> int:
    if not MANIFEST_PATH.exists():
        print(f"error: manifest not found at {MANIFEST_PATH} (0 files examined)", file=sys.stderr)
        return 2

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    # D#1928: a manifest with no `files` key at all, or an empty one, used to
    # fall straight through the loop below (nothing to iterate) and print
    # "verify: clean (0 files match)" -- exit 0. That is indistinguishable
    # from a manifest that pins everything and matches. Refuse instead: this
    # is "could not establish a verdict", which belongs on exit 2 alongside
    # the missing-manifest case above, not on the clean path.
    pinned = manifest.get("files")
    if not isinstance(pinned, dict) or not pinned:
        reason = "has no 'files' key" if "files" not in manifest else "'files' is empty"
        print(
            f"error: manifest at {MANIFEST_PATH} {reason} -- refusing to report "
            f"clean about an empty pin set (0 files examined)",
            file=sys.stderr,
        )
        return 2

    drifted: list[str] = []
    missing: list[str] = []
    for relpath, recorded_hash in sorted(pinned.items()):
        full = REPO_ROOT / relpath
        if not full.is_file():
            missing.append(relpath)
            continue
        actual_hash = sha256_of(full)
        if actual_hash != recorded_hash:
            drifted.append(relpath)

    # `added`: a file the allowlist would pin today but that has no entry in
    # the manifest at all. Previously unreportable -- verify() only ever
    # iterated manifest["files"], so a candidate with no pin was invisible to
    # it, not merely absent from its output. Call the real candidate-set
    # builder rather than re-implementing the globbing (D#1928 Implementation
    # Notes; scripts/engine-sync/tests/test_coldstart_boundary.py sets the
    # same precedent for this codebase).
    includes, excludes = read_allowlist()
    candidates = collect_files(REPO_ROOT, includes, excludes)
    added = sorted(set(candidates) - set(pinned))

    examined = len(pinned)
    if not drifted and not missing and not added:
        print(f"verify: clean ({examined} files match)")
        return 0

    if drifted:
        print(f"verify: DRIFT in {len(drifted)} file(s):", file=sys.stderr)
        for p in drifted:
            print(f"  changed: {p}", file=sys.stderr)
    if missing:
        print(f"verify: MISSING {len(missing)} file(s):", file=sys.stderr)
        for p in missing:
            print(f"  missing: {p}", file=sys.stderr)
    if added:
        print(f"verify: ADDED {len(added)} file(s) matching the allowlist but never pinned:", file=sys.stderr)
        for p in added:
            print(f"  added: {p}", file=sys.stderr)
    print(f"verify: examined {examined} pinned entries ({len(candidates)} live candidates)", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manifest.py",
        description="Generate and verify engine/manifest.json (read-only, no spawns, no writes outside engine/manifest.json).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate", help="Regenerate engine/manifest.json from the allowlist.")
    sub.add_parser("verify", help="Recompute hashes and compare against engine/manifest.json.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        return cmd_generate(args)
    if args.command == "verify":
        return cmd_verify(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
