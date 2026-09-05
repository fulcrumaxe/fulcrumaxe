"""testsupport/git_tracked.py

Shared helper for dead-reference guards that scan the working tree for text
but must not assert on generated, untracked runtime state (D#2202).

`.autonomous-team/` holds a lot of locally-generated cache/state alongside the
tracked role cards and spawn templates these guards actually care about.
`.autonomous-team/registry.json` is the concrete case D#2202 fixed: a
gitignored cache of live GitHub Discussion titles, refreshed by
`sync-wiki.sh` on every merge, that legitimately quotes the dead role name
inside historical titles. A scan that walks every `*.json` in that directory
picks up both the tracked config and the untracked cache, so its verdict
depends on whether the machine running it has ever populated the cache — pass
on a fresh clone or CI, fail on any operator checkout that has run the loop.

Restricting a scan to files `git` tracks makes the guard's outcome depend on
repo content, which is the only thing these tests should be asserting on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_tracked_files(repo_root: Path, *scan_paths: Path) -> set[Path]:
    """Return the resolved absolute paths `git` tracks under repo_root.

    `scan_paths`, if given, scopes the `git ls-files` call to those pathspecs
    (e.g. the same directories a caller is about to `rglob` over) instead of
    listing the whole repo. Uses `-C repo_root` rather than relying on the
    process cwd, so this stays cwd-independent — the same reason callers
    build SCAN_DIRS from `Path(__file__).resolve().parent.parent` rather than
    a relative path.

    Fails loudly (raises RuntimeError) if git is missing or the call fails,
    rather than treating that as "no tracked files": silently returning an
    empty set here would turn every caller into a check that always passes
    when git is broken — the exact defect class D#2202 fixed.
    """
    cmd = ["git", "-C", str(repo_root), "ls-files", "-z"]
    if scan_paths:
        cmd.append("--")
        cmd.extend(str(p) for p in scan_paths)

    try:
        result = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "git executable not found — cannot determine tracked files "
            f"under {repo_root}"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"'git -C {repo_root} ls-files' failed (exit {result.returncode}): {stderr}"
        )

    return {
        (repo_root / entry).resolve()
        for entry in result.stdout.decode("utf-8", errors="replace").split("\0")
        if entry
    }


__all__ = ["git_tracked_files"]
