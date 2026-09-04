"""
repo_divergence.py — detect working-tree content divergence from origin/main.

Motivation (D#1763): D#1759 silently reverted 37 tracked files in the primary
checkout — including hooks/sandbox_rules.py — while HEAD stayed exactly at
origin/main. `git rev-parse HEAD` matched. `git rev-list --count HEAD..origin/main`
returned 0. Every commit-level freshness check passed, because the reversion
rewrote working-tree *content* without moving HEAD. Only `git diff` against a
real ref revealed it. This module makes that comparison a first-class check.

Two independent axes:

  - ALARM candidates come from `git diff --name-only HEAD -- <pathspec>`
    (uncommitted working-tree drift). This is fetch-free and immune to a
    stale `origin/main` ref — every one of D#1759's 37 files would land here
    by construction, because HEAD was correct and the tree was not.
  - INFO candidates are files that differ from `origin/main` but not from
    HEAD (committed-but-unpushed work — normal, never alarming), plus any
    ALARM-candidate file outside the critical-path set (uncommitted drift
    that isn't in a path where lying content is dangerous).

A third axis (D#1912) catches a checkout that is simply BEHIND origin/main —
"nobody pulled" — in a critical path. `build_report` used to fold that case
into INFO alongside "committed but unpushed", because a diff against
origin/main alone cannot tell behind from ahead. `behind_count`
(`git rev-list --count HEAD..origin/main`) resolves the ambiguity without a
diff. When the checkout is on `main` and is behind origin/main by one or
more commits touching a critical path, those paths are reported as
`stale_files` and the tier is "stale" — distinct from "info" because it is
the exact operability gap that let hooks/sandbox.py run inert for 11 commits
(see the module docstring's motivation and D#1912). Gated to the `main`
branch only: a worktree or feature branch behind origin/main is normal and
must not be failed for it.

Within the ALARM candidates, severity splits by path: divergence in
hooks/, .claude/, scripts/, backend/, tests/, open-source/, or CLAUDE.md is
a hard failure (tier "alarm", ok=False) because those are exactly the paths
D#1759 hit and where wrong content invalidates everything else that runs
that day. Divergence anywhere else is a loud, non-blocking warning
(tier "info", ok=True).

A fixed set of known-dirty tracked paths (.autonomous-team/, the two
generated wiki pages) is excluded from both diffs — the parent checkout is
permanently dirty there by design (runtime state files, auto-generated
docs) and a check that fires on them gets ignored within a day. Excluded
paths that DO differ are counted, not silently dropped: `blind_spots` in
the report makes the residual visible instead of burying it in a docstring.

No network calls. This module never runs `git fetch` — callers that want a
fresh `origin/main` ref must fetch before calling (scripts/start-the-day.sh
already does, at its "sync to fresh main" step; backend/health_report.py
runs on a loop and must not fetch, so its origin/main comparison is only as
fresh as the last fetch anyone did).

CLI:
    python3 backend/repo_divergence.py check [--repo-root PATH]
    python3 backend/repo_divergence.py check --force-tier {clean,info,alarm}

`--force-tier` skips git entirely and returns a synthetic report of the
requested tier. It exists so callers (start-the-day.sh) can be tested for
correct exit-code wiring without contaminating a real checkout to manufacture
an actual divergence.

Exit code: 1 if tier is "alarm" or "stale", else 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Known-dirty tracked paths, excluded from both diffs. These are either
# hook-managed runtime state (.autonomous-team/) or auto-generated docs
# (the wiki pair). A bare `git diff --quiet origin/main` fires on these
# every single run on the real checkout; excluding them is what keeps this
# check from crying wolf. Files here that DO differ are still counted, via
# `blind_spots`, so the exclusion doesn't quietly hide anything.
_EXCLUDE_PATHSPECS: tuple[str, ...] = (
    ":!.autonomous-team",
    ":!wiki/Changelog.md",
    ":!wiki/Project-Status.md",
)

# Paths where uncommitted content drift is a hard failure, not a warning.
# This is exactly the D#1759 file set: hooks/sandbox_rules.py, all 19
# .claude/agents/*.md, CLAUDE.md, open-source/export.sh + MANIFEST.md, and
# ten files under scripts/. If content here is lying, nothing downstream
# that runs today is trustworthy.
_CRITICAL_PREFIXES: tuple[str, ...] = (
    "hooks/",
    ".claude/",
    "scripts/",
    "backend/",
    "tests/",
    "open-source/",
)
_CRITICAL_EXACT: frozenset[str] = frozenset({"CLAUDE.md"})

_GIT_TIMEOUT_SECS = 30


def _is_critical(path: str) -> bool:
    if path in _CRITICAL_EXACT:
        return True
    return path.startswith(_CRITICAL_PREFIXES)


# ---------------------------------------------------------------------------
# Git plumbing — pure subprocess wrappers, no side effects, no fetch
# ---------------------------------------------------------------------------


def _run_git(repo_root: str, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _ref_exists(repo_root: str, ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECS,
    )
    return result.returncode == 0


def _diff_names(repo_root: str, ref: str, *, exclude: bool) -> list[str]:
    """git diff --name-only <ref> -- . [exclude pathspecs]"""
    args = ["diff", "--name-only", ref, "--", "."]
    if exclude:
        args.extend(_EXCLUDE_PATHSPECS)
    out = _run_git(repo_root, args)
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def _diff_names_between(repo_root: str, ref_a: str, ref_b: str, *, exclude: bool) -> list[str]:
    """git diff --name-only <ref_a> <ref_b> -- . [exclude pathspecs]

    Commit-to-commit diff (not working-tree-to-ref) — used to name which
    files changed between HEAD and origin/main once `_behind_count` has
    already established the direction (behind vs ahead).
    """
    args = ["diff", "--name-only", ref_a, ref_b, "--", "."]
    if exclude:
        args.extend(_EXCLUDE_PATHSPECS)
    out = _run_git(repo_root, args)
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def _behind_count(repo_root: str, ref: str = "origin/main") -> int:
    """How many commits `ref` has that HEAD does not.

    Uses `git rev-list --count HEAD..<ref>`, not a diff — a diff against
    `ref` cannot distinguish behind from ahead (both make the working tree
    differ from `ref`), and that distinction is exactly what the "stale"
    tier needs. Fails safe: a `ref` that has never been fetched only ever
    *shrinks* this number (or leaves it 0), never invents one.
    """
    if not _ref_exists(repo_root, ref):
        return 0
    out = _run_git(repo_root, ["rev-list", "--count", f"HEAD..{ref}"])
    return int(out.strip())


def _current_branch(repo_root: str) -> str:
    """Empty string in detached HEAD — never raises."""
    return _run_git(repo_root, ["branch", "--show-current"]).strip()


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def format_file_list(files: list[str], cap: int = 20) -> str:
    """Human-readable, capped file list — 'Report the file list, not a boolean.'"""
    if not files:
        return ""
    shown = files[:cap]
    text = ", ".join(shown)
    remaining = len(files) - len(shown)
    if remaining > 0:
        text += f", … and {remaining} more"
    return text


def build_report(repo_root: str | Path) -> dict[str, Any]:
    """Compare the working tree against HEAD and (if available) origin/main.

    Never fetches. Never raises on a missing origin/main ref — that ref is
    optional; only HEAD comparison (the fetch-free, stale-ref-immune ALARM
    signal) is required to exist.
    """
    repo_root = str(repo_root)

    head_all = set(_diff_names(repo_root, "HEAD", exclude=False))
    head_filtered = set(_diff_names(repo_root, "HEAD", exclude=True))

    origin_all: set[str] = set()
    origin_filtered: set[str] = set()
    origin_sha: str | None = None
    origin_date: str | None = None
    behind_count = 0
    stale_files: list[str] = []
    if _ref_exists(repo_root, "origin/main"):
        origin_all = set(_diff_names(repo_root, "origin/main", exclude=False))
        origin_filtered = set(_diff_names(repo_root, "origin/main", exclude=True))
        origin_sha = _run_git(repo_root, ["rev-parse", "origin/main"]).strip()
        origin_date = _run_git(repo_root, ["log", "-1", "--format=%cI", "origin/main"]).strip()
        behind_count = _behind_count(repo_root, "origin/main")
        if behind_count > 0:
            behind_diff = _diff_names_between(repo_root, "HEAD", "origin/main", exclude=True)
            stale_files = sorted(f for f in behind_diff if _is_critical(f))

    alarm_files = sorted(f for f in head_filtered if _is_critical(f))
    warn_uncommitted = sorted(f for f in head_filtered if not _is_critical(f))
    origin_only = origin_filtered - head_filtered

    # Gate the "stale" tier on `main`: being behind origin/main in a critical
    # path has exactly one cause there (nobody pulled). On any other branch
    # it is routine and must not be failed — that would cry wolf on every
    # un-rebased worktree.
    is_stale = bool(stale_files) and _current_branch(repo_root) == "main"
    if not is_stale:
        stale_files = []
    else:
        # Split the behind subset out of origin_only/info_files — it now has
        # its own tier and shouldn't also be reported as merely "info".
        origin_only = origin_only - set(stale_files)

    info_files = sorted(set(warn_uncommitted) | origin_only)

    blind = (head_all - head_filtered) | (origin_all - origin_filtered)

    if alarm_files:
        tier = "alarm"
        ok = False
    elif is_stale:
        tier = "stale"
        ok = False
    elif info_files:
        tier = "info"
        ok = True
    else:
        tier = "clean"
        ok = True

    return {
        "ok": ok,
        "tier": tier,
        "alarm_files": alarm_files,
        "info_files": info_files,
        "stale_files": stale_files,
        "behind_count": behind_count,
        "blind_spots": len(blind),
        "origin_sha": origin_sha,
        "origin_date": origin_date,
    }


def _forced_report(tier: str) -> dict[str, Any]:
    """Synthetic report for testing exit-code wiring without touching git."""
    base: dict[str, Any] = {
        "ok": tier not in ("alarm", "stale"),
        "tier": tier,
        "alarm_files": ["<forced>"] if tier == "alarm" else [],
        "info_files": ["<forced>"] if tier == "info" else [],
        "stale_files": ["<forced>"] if tier == "stale" else [],
        "behind_count": 1 if tier == "stale" else 0,
        "blind_spots": 0,
        "origin_sha": None,
        "origin_date": None,
    }
    return base


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_repo_root() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECS,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return str(Path(__file__).resolve().parent.parent)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect working-tree content divergence from origin/main (D#1763)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check_p = sub.add_parser("check", help="Report divergence; exit 1 if tier is alarm or stale, else 0")
    check_p.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to check (default: git toplevel of cwd)",
    )
    check_p.add_argument(
        "--force-tier",
        choices=("clean", "info", "stale", "alarm"),
        default=None,
        help="Skip git entirely and return a synthetic report of this tier "
        "— for testing caller exit-code wiring, never for real checks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.force_tier:
        report = _forced_report(args.force_tier)
    else:
        repo_root = args.repo_root or _default_repo_root()
        report = build_report(repo_root)

    print(json.dumps(report, indent=2))
    return 1 if report["tier"] in ("alarm", "stale") else 0


if __name__ == "__main__":
    sys.exit(main())
