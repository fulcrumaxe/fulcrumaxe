#!/usr/bin/env python3
"""
sweep-premature-closes.py — read-only retrospective detector for Discussions
that scripts/post-merge-hook.sh closed too early (D#2021).

This is a REPORT, not a repair tool. It has no --repair flag and no --reopen
flag by design: auto-reopening based on a detector's own (possibly wrong)
reading is exactly the class of bug this Spec exists to stop reproducing. A
human reads the report and decides.

Three independent detectors:

  1. Log-based (primary): a Discussion whose number appears in two or more
     "Closing Discussion #N" lines across the hook's own logs was closed at
     least once too early — a correct close happens exactly once. Enumerates
     every log source the hook is known to write to (currently one: manual
     merges via scripts/merge-and-hook.sh log to
     .autonomous-team/dashboard-logs/manual-merge-<PR>.log). Loop-driven
     merges may log this information elsewhere today — see the module-level
     LOG_SOURCE_GLOBS comment — so a clean report here is a floor on the
     problem, not a total; the sources scanned and file count are always
     printed so that gap is visible rather than assumed.

  2. Body-based (secondary): a CLOSED Discussion whose body contains the DONE
     marker value more than once, or contains it inside a fenced code block —
     the fingerprint left by the old unanchored, global `sed` rewrite this
     Spec removes. Requires a live (read-only) GraphQL query.

  3. Open-with-done-marker (D#2020): an OPEN Discussion whose first line is a
     single, well-formed `<!-- STATUS:DONE ... -->` marker. Detectors 1 and 2
     both miss this shape structurally: log-based wants repeated close
     lines, which this Discussion never had (it was never closed at all);
     body-based only inspects CLOSED Discussions. A stale DONE marker left on
     an open Discussion is the shape that actually blocks work, because the
     spawn gate reads the marker, not the closed state (D#1908, corrected by
     hand — see that Discussion's history). This detector is report-only
     like the other two: it never closes, reopens, or edits anything.

Usage:
    python3 scripts/sweep-premature-closes.py            # human-readable
    python3 scripts/sweep-premature-closes.py --json      # machine-readable

Bash + Python stdlib only. No new dependency.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every log source the hook is currently known to write "Closing Discussion
# #N" lines to, relative to the LOG ROOT (see resolve_log_root() below,
# NOT necessarily REPO_ROOT). This list is the single place to widen coverage
# when a new log destination is identified (e.g. if loop-driven merges start
# logging to a file instead of an unredirected stdout stream).
LOG_SOURCE_GLOBS = [
    ".autonomous-team/dashboard-logs/manual-merge-*.log",
]

_CLOSING_LINE_RE = re.compile(r"Closing Discussion #(\d+)")

# Matches a fenced code block (``` ... ```), DOTALL so it spans lines.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def resolve_log_root(fallback: Path = REPO_ROOT) -> Path:
    """Return the checkout whose .autonomous-team/dashboard-logs/ actually
    holds the hook's manual-merge logs.

    dashboard-logs is untracked runtime state written only by
    scripts/merge-and-hook.sh, which — per CLAUDE.md's "Team Lead
    direct-merge exception" — only ever runs from the canonical checkout,
    never from inside a linked worktree. A worktree's own
    .autonomous-team/dashboard-logs/ is therefore always empty, even though
    REPO_ROOT (this file's own two-parents-up) resolves to the worktree when
    the script is run from one. Using backend.repo_root.main_repo_root()
    instead answers "where is the MAIN checkout", which is git-common-dir
    aware and correct from inside a worktree.

    Falls back to *fallback* (REPO_ROOT by default) if backend.repo_root
    can't be imported at all — e.g. this file was copied out of the repo
    tree on its own, with no sibling backend/ package alongside it. That is
    a degraded-but-honest fallback, not a silent wrong answer: find_log_files
    still reports exactly what it looked at, and run_sweep's zero-files
    warning still fires if that location turns out to be empty.
    """
    try:
        repo_dir = Path(__file__).resolve().parent.parent
        if str(repo_dir) not in sys.path:
            sys.path.insert(0, str(repo_dir))
        from backend.repo_root import main_repo_root  # noqa: E402

        return main_repo_root()
    except Exception:
        return fallback


def find_log_files(repo_root: Path = REPO_ROOT, globs: list[str] = LOG_SOURCE_GLOBS) -> list[Path]:
    """Return every log file matched by LOG_SOURCE_GLOBS, sorted for determinism.

    *repo_root* names the root to scan under — callers that already resolved
    a specific root (tests, resolve_log_root()) pass it explicitly. It is
    NOT re-resolved here.
    """
    found: list[Path] = []
    for pattern in globs:
        found.extend(Path(p) for p in glob.glob(str(repo_root / pattern)))
    return sorted(set(found))


def scan_logs_for_multi_close(log_files: list[Path]) -> dict[int, int]:
    """Count "Closing Discussion #N" lines per Discussion across *log_files*.

    Returns {discussion_number: close_count}. A discussion closed exactly once
    is not reported by the caller — count == 1 is the correct, intended path.
    """
    counts: dict[int, int] = {}
    for path in log_files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for m in _CLOSING_LINE_RE.finditer(text):
            n = int(m.group(1))
            counts[n] = counts.get(n, 0) + 1
    return counts


def has_duplicate_done_marker(body: str, done_value: str = "DONE") -> bool:
    """True if *body* carries the fingerprint of the old unanchored global
    `sed` rewrite: a real, HTML-comment-shaped ``<!-- STATUS:DONE ... -->``
    marker appears more than once anywhere in the body, or one appears at all
    inside a fenced code block.

    Deliberately anchored to the full ``<!-- STATUS:X -->`` comment shape
    (the same shape ``backend/discussion_status.py``'s writer uses), NOT a
    bare substring search for "STATUS:DONE" — this repo's own Discussions
    routinely discuss this marker convention in prose (this Spec's body is
    one of them), and a bare substring match flags every one of those as
    "corrupted", which is a false-positive machine, not a detector. A
    correctly-closed Discussion has the marker exactly once, on line 1, and
    never inside a fence — the old `sed` at post-merge-hook.sh:336 rewrote
    every occurrence anywhere, fences included, which is the fingerprint this
    function looks for.
    """
    if not body:
        return False
    marker_re = re.compile(r"<!--\s*STATUS:([A-Za-z_]+)[^>]*-->")
    all_matches = marker_re.findall(body)
    done_count = sum(1 for v in all_matches if v == done_value)
    if done_count >= 2:
        return True
    for fence in _FENCE_RE.findall(body):
        if done_value in marker_re.findall(fence):
            return True
    return False


_FIRST_LINE_MARKER_RE = re.compile(r"^<!--\s*STATUS:([A-Za-z_]+)[^>]*-->\s*$")


def has_open_done_marker(body: str, done_value: str = "DONE") -> bool:
    """True if *body*'s first line is a single, well-formed
    ``<!-- STATUS:DONE ... -->`` marker.

    Body-only, like has_duplicate_done_marker() above — the caller decides
    which population (open vs. closed) to run this against. A well-formed
    single first-line marker is exactly what a *correct* close leaves behind
    on a CLOSED Discussion; this detector exists because the identical shape
    can also be left, wrongly, on a Discussion that was never closed at all
    (D#1908: one clean marker, never a repeated close line, so the log
    detector never saw it; open, so the body detector's closed-only filter
    never saw it either). Anchored to line 1 and the full line — a marker
    later in the body, or trailing prose after it on the same line, does not
    match; that is prose discussing the convention, not the marker itself.
    """
    if not body:
        return False
    first_line = body.splitlines()[0].strip()
    m = _FIRST_LINE_MARKER_RE.match(first_line)
    return bool(m and m.group(1) == done_value)


def _resolve_repo(repo_root: Path = REPO_ROOT) -> Optional[str]:
    """Mirror scripts/lib/repo-resolve.sh's resolution order: config.json
    "repo" field, then AUTONOMOUS_TEAM_REPO env var. Returns None (never
    raises) if neither resolves — callers treat that as "skip the live
    GraphQL query", consistent with this script's fail-closed posture.
    """
    cfg_path = repo_root / ".autonomous-team" / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text())
            repo = cfg.get("repo", "")
            if repo:
                return repo
        except (OSError, json.JSONDecodeError):
            pass
    return os.environ.get("AUTONOMOUS_TEAM_REPO") or None


def fetch_closed_discussion_bodies(repo: str, max_pages: int = 20, page_size: int = 50) -> dict[int, dict]:
    """Fetch {number: {"body": str, "closed": bool}} for EVERY Discussion
    (open and closed) via paginated, read-only GraphQL. Returns {} on any
    failure (fail closed — a network hiccup must not be mistaken for "no
    Discussions").

    Widened for D#2020's third detector, which needs OPEN bodies —
    detector 2 (duplicate_done_marker) still only ever looks at entries
    whose "closed" is True, so it reads exactly the population it read
    before this change; nothing here changes what detector 2 sees.

    Bounded to max_pages * page_size Discussions as a safety cap against a
    runaway loop; this is a report tool, not a crawler.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return {}

    results: dict[int, dict] = {}
    cursor = "null"
    for _ in range(max_pages):
        after_clause = f'after:"{cursor}"' if cursor != "null" else ""
        query = (
            "query { repository(owner:\"%s\", name:\"%s\") { "
            "discussions(first:%d, %s) { "
            "pageInfo { hasNextPage endCursor } "
            "nodes { number closed body } } } }"
        ) % (owner, name, page_size, after_clause)
        try:
            proc = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        if proc.returncode != 0:
            break
        try:
            data = json.loads(proc.stdout)
            disc = data["data"]["repository"]["discussions"]
        except (json.JSONDecodeError, KeyError, TypeError):
            break

        for node in disc.get("nodes", []):
            results[node["number"]] = {
                "body": node.get("body", "") or "",
                "closed": bool(node.get("closed")),
            }

        page_info = disc.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    return results


def run_sweep(
    repo_root: Optional[Path] = None,
    fetch_closed_fn: Optional[Callable[[], dict[int, dict]]] = None,
) -> dict:
    """Run all three detectors and return the combined report as a dict.

    *repo_root*, when given explicitly (tests do this), is used AS-IS for
    both log scanning and repo-slug resolution — no further resolution is
    applied, so a fixture directory means exactly what it says. Left at the
    default None, log scanning resolves through resolve_log_root() (the main
    checkout, not necessarily where this file happens to live — see that
    function's docstring for why those differ inside a worktree) while
    repo-slug resolution keeps using REPO_ROOT, since .autonomous-team/
    config.json is a tracked file identical in both.

    *fetch_closed_fn* is an injection point for tests: a callable returning
    {discussion_number: {"body": str, "closed": bool}}. Production callers
    leave it None, which resolves the repo and calls the real (network)
    fetch. Despite the name (kept from before this fetch was widened to
    cover open Discussions too), it now returns both open and closed
    entries — the "closed" key is what tells each detector which population
    it is looking at.
    """
    if repo_root is None:
        log_root = resolve_log_root(fallback=REPO_ROOT)
        slug_root = REPO_ROOT
    else:
        log_root = repo_root
        slug_root = repo_root

    log_files = find_log_files(log_root)
    close_counts = scan_logs_for_multi_close(log_files)

    if fetch_closed_fn is None:
        repo = _resolve_repo(slug_root)
        all_discussions = fetch_closed_discussion_bodies(repo) if repo else {}
    else:
        all_discussions = fetch_closed_fn()

    flagged: dict[int, dict] = {}

    for number, count in close_counts.items():
        if count >= 2:
            flagged.setdefault(number, {"discussion": number, "close_count": None, "detectors": []})
            flagged[number]["close_count"] = count
            flagged[number]["detectors"].append("log_multi_close")

    for number, info in all_discussions.items():
        body = info.get("body", "") or ""
        is_closed = bool(info.get("closed"))
        if is_closed:
            # Detector 2 reads exactly the population it read before this
            # fetch was widened: closed Discussions only.
            if has_duplicate_done_marker(body):
                flagged.setdefault(number, {"discussion": number, "close_count": None, "detectors": []})
                flagged[number]["detectors"].append("duplicate_done_marker")
        else:
            # Detector 3 (D#2020): the shape the other two structurally
            # cannot see — an OPEN Discussion with a stale DONE marker.
            if has_open_done_marker(body):
                flagged.setdefault(number, {"discussion": number, "close_count": None, "detectors": []})
                flagged[number]["detectors"].append("open_with_done_marker")

    discussions = sorted(flagged.values(), key=lambda d: d["discussion"])

    # sources_scanned names the configured glob PATTERNS (always non-empty as
    # long as at least one source is registered), independent of files_read —
    # the number of files that actually matched, which can legitimately be 0
    # on a fresh checkout. Keeping these separate is what makes coverage
    # visible instead of assumed: an empty repo and an unscanned repo must not
    # look the same in the report.
    report: dict = {
        "sources_scanned": list(LOG_SOURCE_GLOBS),
        "log_root": str(log_root),
        "files_read": len(log_files),
        "discussions": discussions,
    }

    # Zero files read must never read as a clean result — that is literally
    # the "no evidence read as evidence of absence" bug this whole Spec is
    # about, one layer up: a naive reader sees "No premature closes detected"
    # and stops, exactly like "0 reaped" over an empty registry (D#2001).
    # Kept as a loud warning rather than a nonzero exit: this script's own
    # acceptance criteria require `--json` to exit 0 so the JSON is always
    # parseable, so the signal has to live in the payload and on stderr, not
    # in $?.
    if not log_files:
        report["warning"] = (
            f"0 log files matched under {log_root} ({', '.join(LOG_SOURCE_GLOBS)}). "
            "This does NOT mean no premature closes occurred — it means nothing was "
            "scanned. dashboard-logs is untracked runtime state written only by the "
            "canonical checkout (scripts/merge-and-hook.sh never runs inside a "
            "worktree); if this path looks like a worktree, point "
            "AUTONOMOUS_TEAM_REPO_ROOT at the main checkout, or re-run from there."
        )
        print(f"[sweep-premature-closes] WARNING: {report['warning']}", file=sys.stderr)

    return report


def _format_human(report: dict) -> str:
    lines = []
    lines.append("Log sources scanned:")
    for src in report["sources_scanned"]:
        lines.append(f"  - {src}")
    lines.append(f"Log root: {report['log_root']}")
    lines.append(f"Files read: {report['files_read']}")
    lines.append("")
    if "warning" in report:
        lines.append(f"!!! WARNING: {report['warning']}")
        lines.append("")
    if not report["discussions"]:
        if "warning" in report:
            lines.append("0 Discussion(s) flagged — but see the warning above: this is an UNSCANNED result, not a clean one.")
        else:
            lines.append("No premature closes detected by either detector.")
    else:
        lines.append(f"{len(report['discussions'])} Discussion(s) flagged:")
        for d in report["discussions"]:
            close_count = d["close_count"] if d["close_count"] is not None else "n/a"
            detectors = ", ".join(d["detectors"])
            lines.append(f"  - Discussion #{d['discussion']}: close_count={close_count} detectors=[{detectors}]")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only retrospective sweep for Discussions closed prematurely "
            "by scripts/post-merge-hook.sh (D#2021). Reports only — a human "
            "decides whether to reopen or repair."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    # Deliberately no --repair / --reopen. If someone passes them, argparse's
    # own "unrecognized arguments" error is the correct behaviour.
    args = parser.parse_args(argv)

    report = run_sweep()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_format_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
