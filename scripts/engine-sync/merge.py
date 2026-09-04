#!/usr/bin/env python3
"""engine-sync three-way merge — Slice B2 Batch B2c of D#1586 (final batch;
follow-on to Batch B2a's verified fetch and Batch B2b's apply/seed/PR path).

Deterministic, zero-LLM wrapper around the real `git merge-file` three-way
merge. Given the base (common-ancestor) blob, the sibling's local blob, and
the verified upstream blob for one conflict-bucket path, this tool merges
non-overlapping hunks automatically and leaves raw
`<<<<<<< / ======= / >>>>>>>` conflict markers only where local and upstream
genuinely overlap.

This module NEVER invokes an LLM, NEVER writes to a sibling's working tree,
NEVER pushes, and NEVER opens a PR -- it only ever writes to a private temp
scratch dir (unconditionally cleaned up) and returns merged bytes to its
caller. `resolver.py` is the only consumer that decides what to do with the
result (batch it into a NEEDS-REVIEW PR, optionally ask the advisory
resolver about any residual markers).

G10 -- subprocess minimization: `git merge-file` is the ONLY subprocess this
module ever invokes, always with an argv list (a shell is never involved),
always on temp files it created itself, with output captured via `-p`
(stdout) -- never interpolating file *content* into a shell command.

Subcommands:
  merge   Three-way merge --base/--local/--upstream into --out (or stdout).
          Exit 0 = clean merge (no residual markers), 1 = residual conflict
          markers remain (--out is still written -- that's the whole point:
          the NEEDS-REVIEW PR commits exactly this content), 2 = usage/IO
          error (nothing written).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

CONFLICT_MARKERS = (b"<<<<<<< ", b"=======\n", b">>>>>>> ")


class MergeError(Exception):
    """Raised on a genuine tool failure (git missing, I/O error) -- never
    raised merely because the merge produced conflict markers. A residual
    conflict is a normal, expected outcome of a three-way merge, not an
    error."""


def has_conflict_markers(content: bytes) -> bool:
    """Detect raw git conflict markers in merged content. Used both to
    decide whether the advisory resolver is worth invoking for a path, and
    (in tests) to assert merge.py leaves markers only on true conflicts."""
    return any(marker in content for marker in CONFLICT_MARKERS)


def three_way_merge(base: bytes, local: bytes, upstream: bytes) -> tuple[bytes, int]:
    """Run `git merge-file -p` on three temp files. Returns
    (merged_content, conflict_count) where conflict_count == 0 means a clean
    merge (non-overlapping hunks applied automatically, no markers) and
    conflict_count > 0 is the number of hunks git could not reconcile
    (merged_content then contains raw markers for each). Raises MergeError
    only on a genuine tool failure (negative return code / git missing),
    never merely because conflict_count > 0.

    `-p` writes the merge result to stdout instead of mutating the "local"
    temp file in place -- this module owns all three temp files and never
    needs the in-place-edit form, so capturing stdout keeps the three inputs
    trivially immutable and the subprocess call fully argv-based (no shell
    interpolation of any file content)."""
    with tempfile.TemporaryDirectory(prefix="engine-sync-merge-") as tmp:
        tmp_path = Path(tmp)
        local_f = tmp_path / "local"
        base_f = tmp_path / "base"
        upstream_f = tmp_path / "upstream"
        local_f.write_bytes(local)
        base_f.write_bytes(base)
        upstream_f.write_bytes(upstream)

        result = subprocess.run(
            ["git", "merge-file", "-p", "--", str(local_f), str(base_f), str(upstream_f)],
            capture_output=True,
        )
        if result.returncode < 0:
            raise MergeError(
                f"git merge-file terminated abnormally (signal {-result.returncode}): "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
        # returncode == 0 -> clean merge. returncode > 0 -> that many hunks
        # conflicted (git merge-file's own convention); either way stdout is
        # the merge result and is exactly what we want to return.
        return result.stdout, max(result.returncode, 0)


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        base = Path(args.base).read_bytes()
        local = Path(args.local).read_bytes()
        upstream = Path(args.upstream).read_bytes()
    except OSError as exc:
        print(f"error: could not read input file: {exc}", file=sys.stderr)
        return 2

    try:
        merged, conflict_count = three_way_merge(base, local, upstream)
    except MergeError as exc:
        print(f"error: merge failed: {exc}", file=sys.stderr)
        return 2

    if args.out:
        Path(args.out).write_bytes(merged)
    else:
        sys.stdout.buffer.write(merged)

    if conflict_count > 0 or has_conflict_markers(merged):
        print(f"merge: {conflict_count} conflicting hunk(s) -- residual markers present", file=sys.stderr)
        return 1
    print("merge: clean (no residual conflict markers)", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge.py",
        description=(
            "Slice B2c: deterministic `git merge-file` three-way merge of one "
            "conflict-bucket path. Zero LLM, zero writes outside a private temp dir "
            "and --out. Exit 0 = clean merge, 1 = residual conflict markers, 2 = error."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    merge_parser = sub.add_parser("merge", help="Three-way merge --base/--local/--upstream.")
    merge_parser.add_argument("--base", required=True, help="Path to the common-ancestor blob.")
    merge_parser.add_argument("--local", required=True, help="Path to the sibling's current (local) blob.")
    merge_parser.add_argument("--upstream", required=True, help="Path to the verified upstream blob.")
    merge_parser.add_argument(
        "--out", default=None, help="Path to write the merge result to (default: stdout)."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "merge":
        return cmd_merge(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
