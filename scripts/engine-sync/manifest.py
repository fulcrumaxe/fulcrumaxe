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
  verify     Recompute hashes for every file listed in engine/manifest.json
             against the current working tree. Exits 0 if all match; exits
             non-zero and names every drifted path otherwise.

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


def cmd_generate(_args: argparse.Namespace) -> int:
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
        print(f"error: manifest not found at {MANIFEST_PATH}", file=sys.stderr)
        return 2

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    drifted: list[str] = []
    missing: list[str] = []
    for relpath, recorded_hash in sorted(manifest.get("files", {}).items()):
        full = REPO_ROOT / relpath
        if not full.is_file():
            missing.append(relpath)
            continue
        actual_hash = sha256_of(full)
        if actual_hash != recorded_hash:
            drifted.append(relpath)

    if not drifted and not missing:
        print(f"verify: clean ({len(manifest.get('files', {}))} files match)")
        return 0

    if drifted:
        print(f"verify: DRIFT in {len(drifted)} file(s):", file=sys.stderr)
        for p in drifted:
            print(f"  changed: {p}", file=sys.stderr)
    if missing:
        print(f"verify: MISSING {len(missing)} file(s):", file=sys.stderr)
        for p in missing:
            print(f"  missing: {p}", file=sys.stderr)
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
