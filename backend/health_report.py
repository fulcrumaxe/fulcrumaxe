"""
health_report.py — unified subsystem health checker.

Runs all individual health checks in one place and produces a structured
pass/fail report. Each check is an independent function returning a dict
with keys: name, ok, detail. Exceptions are caught per-check so a single
broken subsystem never crashes the whole report.

Usage:
    python3 backend/health_report.py check           # JSON, exit 0 if all pass
    python3 backend/health_report.py check --human   # colored terminal output
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure repo root is importable regardless of invocation cwd
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import repo_divergence
from backend import state_paths as _state_paths

# ---------------------------------------------------------------------------
# STATE_DB / STATS_DB / BLACKBOARD_DIR — resolved at call time (D#1810)
# ---------------------------------------------------------------------------
# These used to be `from backend.state_paths import BLACKBOARD_DIR, STATE_DB,
# STATS_DB, STATE_DIR` at module scope, which froze each value at import time
# and defeated a later AUTONOMOUS_TEAM_STATE_DIR override. Module __getattr__
# (PEP 562) makes external access (`hr_mod.STATS_DB`) resolve fresh on every
# read, UNLESS a caller — several tests do this — assigns/patches the name
# directly (`monkeypatch.setattr(hr_mod, "STATS_DB", ...)`), which shadows
# __getattr__ exactly like any other module attribute. `_attr()` routes this
# module's own internal references through the same globals-first-else-
# resolve-fresh logic so both call sites see one consistent value.

_HR_RESOLVERS = {
    "STATE_DB": lambda: _state_paths.STATE_DB,
    "STATS_DB": lambda: _state_paths.STATS_DB,
    "BLACKBOARD_DIR": lambda: _state_paths.BLACKBOARD_DIR,
}


def __getattr__(name: str):
    resolver = _HR_RESOLVERS.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()


def _attr(name: str):
    if name in globals():
        return globals()[name]
    return __getattr__(name)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_TEAM_DIR = _REPO_ROOT / ".autonomous-team"
_WORKTREES_DIR = _REPO_ROOT / ".claude" / "worktrees"
_HOOKS_DIR = _REPO_ROOT / "scripts" / "hooks"
_POST_AGENT_D = _HOOKS_DIR / "post-agent.d"

# Max age (seconds) before a check is considered stale
_LOOP_STALE_SECS = 30 * 60      # 30 minutes
_DUCKDB_STALE_SECS = 60 * 60    # 1 hour
_ORPHAN_AGE_SECS = 4 * 60 * 60  # 4 hours

# JSONL size limit
_JSONL_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def check_state_db_writable() -> dict[str, Any]:
    """Verify state.db exists and is writable (open with sqlite3)."""
    name = "state_db_writable"
    try:
        state_db = _attr("STATE_DB")
        if not state_db.exists():
            return {"name": name, "ok": False, "detail": f"state.db not found: {state_db}"}
        # Actually open the DB to confirm it is not locked/corrupt
        conn = sqlite3.connect(str(state_db), timeout=1.0)
        conn.execute("SELECT 1")
        conn.close()
        return {"name": name, "ok": True, "detail": str(state_db)}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def _git_worktree_paths() -> set[Path] | None:
    """Return the set of absolute worktree paths registered with git.

    Runs ``git worktree list --porcelain`` and extracts the ``worktree <path>``
    line from each block.  Each path is resolved to an absolute Path so callers
    can compare against ``entry.resolve()``.

    Returns None on any error (non-zero exit, timeout, git not found) to signal
    that the caller should fall back to mtime-only behavior.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        paths: set[Path] = set()
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                raw = line[len("worktree "):].strip()
                try:
                    paths.add(Path(raw).resolve())
                except Exception:  # noqa: BLE001
                    pass
        return paths
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Orphan worktree classification
# ---------------------------------------------------------------------------

# Disposable path prefixes (relative to the worktree root).  An untracked
# entry whose path starts with any of these prefixes is considered runtime
# noise that can be ignored when deciding whether a stale worktree has real
# developer work left in it.
_DISPOSABLE_PREFIXES: tuple[str, ...] = (
    ".autonomous-team/",
    "node_modules/",
    ".vite/",
    "blackboard/",
    "wiki/",
    "archive/",
    "training-data/",
)

# Disposable file suffixes (case-insensitive not needed — all lowercase here).
_DISPOSABLE_SUFFIXES: tuple[str, ...] = (
    ".lock",
    ".pr",
    ".test",
    ".duckdb",
    ".db",
    ".pid",
    ".log",
    ".output",
    ".jsonl",
)

# Specific disposable filenames (exact basename match).
# NOTE: "config.json" is intentionally absent — a bare basename match would
# classify config.json at any depth as disposable.  .autonomous-team/config.json
# is handled by _TRACKED_DISPOSABLE instead (it is a tracked generated file).
_DISPOSABLE_NAMES: frozenset[str] = frozenset(
    {
        "kpi.json",
        "dial-registry.json",
        "discussion_cache.db",
        "state.db",
        "stats.duckdb",
        "now.md",
        "status_page.py.pr",
        "api.py.test",
    }
)

# Closed, exact-path set of tracked (M/A/D/R) repo-relative paths that are
# generated from a source-of-truth and are never hand-authored deliverables.
# An orphan worktree is classified "disposable" only when EVERY tracked-change
# path falls in this set AND every untracked path passes _is_disposable_path().
#
# Conservative rule: use exact paths only — no prefixes or globs.
# A prefix like "wiki/" could silently swallow a hand-written wiki page.
_TRACKED_DISPOSABLE: frozenset[str] = frozenset(
    {
        ".autonomous-team/now.md",
        ".autonomous-team/config.json",
        ".autonomous-team/agent-profiles.json",
        "wiki/Project-Status.md",
        "wiki/Corpus-Drift-Report.md",
        "wiki/PR-Index.md",
        "wiki/Changelog.md",
    }
)


def _is_disposable_path(rel_path: str) -> bool:
    """Return True if *rel_path* (relative to worktree root) is disposable noise.

    Accepts the raw path string as reported by ``git status --porcelain`` for
    an untracked entry (the leading ``?? `` has already been stripped).  A path
    is disposable when it matches a prefix, suffix, or exact-name rule.  The
    check is intentionally tight: anything not matched is treated as real work.
    """
    # Normalise separators (git on Windows may use backslashes, unlikely here
    # but harmless to handle).
    rel_path = rel_path.replace("\\", "/").strip().rstrip("/")
    basename = rel_path.split("/")[-1]

    # Prefix match — covers whole subtrees like .autonomous-team/, wiki/, etc.
    for prefix in _DISPOSABLE_PREFIXES:
        if rel_path.startswith(prefix) or rel_path + "/" == prefix:
            return True

    # Exact basename match
    if basename in _DISPOSABLE_NAMES:
        return True

    # Suffix match on the full relative path (catches e.g. foo/bar.jsonl)
    for suffix in _DISPOSABLE_SUFFIXES:
        if rel_path.endswith(suffix):
            return True

    # scripts/test-sandbox*.txt — path-level check
    if rel_path.startswith("scripts/test-sandbox") and rel_path.endswith(".txt"):
        return True

    return False


def _classify_orphan(path: Path) -> str:
    """Classify a stale worktree dir as ``"disposable"`` or ``"has-content"``.

    Runs ``git -C <path> status --porcelain`` and inspects every output line:

    * Lines whose first two chars are NOT ``??`` represent tracked changes
      (modifications, staged files, deletions, renames).  The repo-relative
      path is extracted and tested against ``_TRACKED_DISPOSABLE``.  Any
      tracked-change path NOT in that closed allowlist → ``"has-content"``.
    * ``??`` lines are untracked entries.  Each is tested against the
      untracked disposable allowlist via ``_is_disposable_path``.  Any
      untracked entry that does not pass → ``"has-content"``.

    Conservative invariant: returns ``"has-content"`` on any git error,
    when the worktree is not a git repo, or when any path is ambiguous.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            # git not available or path isn't a git repo — be conservative
            return "has-content"
        for line in result.stdout.splitlines():
            if not line:
                continue
            xy = line[:2]
            rest = line[3:]  # path starts at column 3 in porcelain v1
            if xy == "??":
                # Untracked — check untracked disposable allowlist
                # git may quote paths with spaces: strip surrounding quotes
                rest = rest.strip().strip('"')
                if not _is_disposable_path(rest):
                    return "has-content"
            else:
                # Tracked change (M, A, D, R, C, U, …).
                # For rename lines (R), porcelain v1 format is:
                #   "R  old -> new"  or  "RM old -> new"
                # We check the DESTINATION path (after "->") when present.
                # For all other tracked lines, rest IS the path.
                tracked_path = rest.strip().strip('"')
                if " -> " in tracked_path:
                    tracked_path = tracked_path.split(" -> ", 1)[1].strip().strip('"')
                if tracked_path not in _TRACKED_DISPOSABLE:
                    return "has-content"
        return "disposable"
    except Exception:  # noqa: BLE001
        return "has-content"


def check_orphan_worktrees() -> dict[str, Any]:
    """Count worktrees in .claude/worktrees/ older than 4 hours; ok if 0.

    A directory is an orphan only when it is BOTH absent from
    ``git worktree list --porcelain`` AND older than _ORPHAN_AGE_SECS.
    Directories present in the porcelain list are live registered worktrees
    and are never flagged regardless of age.

    Stale orphans are further classified:

    * ``disposable`` — only runtime/scratch files; no tracked changes.
      These do NOT flip ok to False (non-blocking noise).
    * ``has-content`` — tracked changes OR untracked files outside the
      disposable allowlist.  At least one such orphan → ok=False.

    If ``git worktree list`` is unavailable, falls back to the prior
    mtime-only behavior (all stale dirs treated as ``has-content``) and
    appends a visible suffix to the detail string.
    """
    name = "orphan_worktrees"
    try:
        if not _WORKTREES_DIR.exists():
            return {"name": name, "ok": True, "detail": "worktrees dir absent (clean)"}
        live = _git_worktree_paths()
        git_unavailable = live is None
        now = time.time()
        disposable: list[str] = []
        has_content: list[str] = []
        for entry in _WORKTREES_DIR.iterdir():
            if not entry.is_dir():
                continue
            # If git is available and the dir is a registered worktree, skip it.
            if not git_unavailable and entry.resolve() in live:
                continue
            age = now - entry.stat().st_mtime
            if age <= _ORPHAN_AGE_SECS:
                continue
            # Classify — fall back to has-content when git is unavailable
            if git_unavailable:
                has_content.append(entry.name)
            else:
                kind = _classify_orphan(entry)
                if kind == "disposable":
                    disposable.append(entry.name)
                else:
                    has_content.append(entry.name)
        fallback_suffix = " (git unavailable — mtime-only fallback)" if git_unavailable else ""
        if has_content:
            names = ", ".join(has_content[:5])
            extra = f" (+{len(has_content) - 5} more)" if len(has_content) > 5 else ""
            return {
                "name": name,
                "ok": False,
                "detail": (
                    f"{len(has_content)} orphan(s) with real content: "
                    f"{names}{extra}"
                    + (f"; {len(disposable)} disposable-only (non-blocking)" if disposable else "")
                    + fallback_suffix
                ),
            }
        if disposable:
            return {
                "name": name,
                "ok": True,
                "detail": (
                    f"{len(disposable)} disposable-only orphan(s) older than 4h (non-blocking)"
                    + fallback_suffix
                ),
            }
        return {"name": name, "ok": True, "detail": f"0 orphan worktrees found{fallback_suffix}"}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_jsonl_sizes() -> dict[str, Any]:
    """Scan .autonomous-team/*.jsonl for any file > 50 MB; ok if none."""
    name = "jsonl_sizes"
    try:
        if not _TEAM_DIR.exists():
            return {"name": name, "ok": True, "detail": ".autonomous-team dir absent"}
        oversized: list[str] = []
        for p in _TEAM_DIR.glob("*.jsonl"):
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > _JSONL_MAX_BYTES:
                mb = size / (1024 * 1024)
                oversized.append(f"{p.name} ({mb:.1f} MB)")
        if oversized:
            return {
                "name": name,
                "ok": False,
                "detail": f"oversized JSONL files: {', '.join(oversized)}",
            }
        return {"name": name, "ok": True, "detail": "all JSONL files within 50 MB limit"}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_loop_staleness() -> dict[str, Any]:
    """Check last entry in loop-metrics.jsonl; ok if within 30 min or loop gate is off."""
    name = "loop_staleness"
    try:
        # Check control plane gate — if loop gate is off, staleness is expected
        try:
            from backend.control_plane import check_gate as _check_gate  # noqa: PLC0415
            if not _check_gate("loop_enabled"):
                return {"name": name, "ok": True, "detail": "loop gate is off — staleness expected"}
        except Exception:  # noqa: BLE001
            pass  # control_plane unavailable — proceed with check

        from backend.health_monitor import get_loop_metrics  # noqa: PLC0415
        metrics = get_loop_metrics()
        last_run = metrics.get("loop_last_run")

        if last_run is None:
            return {"name": name, "ok": False, "detail": "no loop metrics recorded yet"}

        # Parse ISO timestamp -- shared with every other loop-metrics.jsonl
        # reader (D#2315) rather than a private .replace("Z", ...) call.
        from backend.loop_metrics_ts import parse_loop_metrics_ts  # noqa: PLC0415
        ts = parse_loop_metrics_ts(last_run)
        if ts is None:
            return {"name": name, "ok": False, "detail": f"unparseable timestamp: {last_run}"}
        age_secs = (datetime.now(tz=timezone.utc) - ts).total_seconds()

        age_min = round(age_secs / 60, 1)
        if age_secs <= _LOOP_STALE_SECS:
            return {"name": name, "ok": True, "detail": f"last loop run {age_min}m ago"}
        return {
            "name": name,
            "ok": False,
            "detail": f"loop last ran {age_min}m ago (threshold 30m)",
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_hook_dirs_present() -> dict[str, Any]:
    """Verify scripts/hooks/post-agent.d/ and scripts/hooks/ exist."""
    name = "hook_dirs_present"
    try:
        missing: list[str] = []
        for d in (_HOOKS_DIR, _POST_AGENT_D):
            if not d.exists():
                missing.append(str(d.relative_to(_REPO_ROOT)))
        if missing:
            return {"name": name, "ok": False, "detail": f"missing dirs: {', '.join(missing)}"}
        return {"name": name, "ok": True, "detail": "scripts/hooks/ and post-agent.d/ present"}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_duckdb_freshness() -> dict[str, Any]:
    """Check mtime of stats.duckdb; ok if within 1 hour."""
    name = "duckdb_freshness"
    try:
        stats_db = _attr("STATS_DB")
        if not stats_db.exists():
            return {"name": name, "ok": False, "detail": f"stats.duckdb not found: {stats_db}"}
        age_secs = time.time() - stats_db.stat().st_mtime
        age_min = round(age_secs / 60, 1)
        if age_secs <= _DUCKDB_STALE_SECS:
            return {"name": name, "ok": True, "detail": f"stats.duckdb updated {age_min}m ago"}
        return {
            "name": name,
            "ok": False,
            "detail": f"stats.duckdb last updated {age_min}m ago (threshold 60m)",
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_circuit_breakers() -> dict[str, Any]:
    """Check if any circuit breakers are tripped; ok if none active (non-stale)."""
    name = "circuit_breakers"
    try:
        from backend.circuit_breaker import (  # noqa: PLC0415
            _collect_tripped,
            _discussion_state,
            STALE_BREAKER_DAYS,
        )
        from datetime import timedelta  # noqa: PLC0415

        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(days=STALE_BREAKER_DAYS)

        active: list[dict] = []
        for e in _collect_tripped():
            if not e.get("blocked"):
                continue

            # Age check
            # Fail-safe: a missing/unparseable timestamp is NOT age-stale.
            # Only a real parseable timestamp older than the cutoff counts.
            # A missing-ts + open/unknown Discussion falls through to the
            # disc_dead check -> NOT skipped -> health report returns ok:False.
            # This mirrors the expire_stale() semantics in circuit_breaker.py.
            updated_at_raw = e.get("updated_at")
            age_stale = False
            if updated_at_raw is not None:
                try:
                    ts = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
                    age_stale = ts < cutoff
                except Exception:  # noqa: BLE001
                    pass  # unparseable timestamp -> not age-stale; rely on disc_dead

            # Discussion state check
            state = _discussion_state(e["discussion"])
            disc_dead = state in ("closed", "absent")

            # Skip (treat as expired) only when:
            #   - Discussion is definitively dead (closed/absent), OR
            #   - Real parseable timestamp >= STALE_BREAKER_DAYS old.
            # Missing/unparseable ts + open/unknown -> NOT skipped -> ok:False (fail-safe).
            if disc_dead or age_stale:
                continue  # treat as expired — don't count against health

            active.append(e)

        if active:
            disc_list = ", ".join(f"#{e['discussion']}" for e in active[:5])
            return {
                "name": name,
                "ok": False,
                "detail": f"{len(active)} tripped circuit breaker(s): {disc_list}",
            }
        return {"name": name, "ok": True, "detail": "no tripped circuit breakers"}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_blackboard_writable() -> dict[str, Any]:
    """Verify blackboard directory exists and is writable."""
    name = "blackboard_writable"
    try:
        blackboard_dir = _attr("BLACKBOARD_DIR")
        if not blackboard_dir.exists():
            return {"name": name, "ok": False, "detail": f"blackboard dir not found: {blackboard_dir}"}
        # Write a probe file to confirm write access
        probe = blackboard_dir / ".health_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"name": name, "ok": True, "detail": str(blackboard_dir)}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_dial_chain_integrity() -> dict[str, Any]:
    """Verify the dial-registry hash chain in audit.jsonl is intact.

    Shells out to scripts/audit-replay.sh (no-arg mode). Exit 0 means the
    dial-row hash chain is intact; non-zero means a genuine chain break was
    detected and the first broken link is reported in the detail field.
    """
    import subprocess  # noqa: PLC0415

    name = "dial_chain_integrity"
    try:
        script = _REPO_ROOT / "scripts" / "audit-replay.sh"
        if not script.exists():
            return {"name": name, "ok": False, "detail": f"audit-replay.sh not found: {script}"}
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode == 0:
            return {"name": name, "ok": True, "detail": stdout or "chain intact"}
        detail = stdout or stderr or f"exit {result.returncode}"
        return {"name": name, "ok": False, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc)}


def check_worktree_matches_origin() -> dict[str, Any]:
    """Detect working-tree content divergence from origin/main (D#1763).

    Delegates to backend.repo_divergence, which distinguishes uncommitted
    working-tree drift (diff vs HEAD — fetch-free, immune to a stale
    origin/main ref) from committed-but-unpushed drift (diff vs
    origin/main). Uncommitted drift in a critical path (hooks/, .claude/,
    scripts/, backend/, tests/, open-source/, CLAUDE.md) is the D#1759
    signature — HEAD correct, tree content wrong — and fails this check;
    everything else is reported but does not fail it. This module never
    calls `git fetch`; origin/main is only as fresh as the last fetch
    someone else ran (e.g. scripts/start-the-day.sh).

    A checkout on `main` that is simply behind origin/main in a critical
    path (D#1912 — nobody pulled) is a separate "stale" tier and also fails
    this check. This is the path that matters here: unlike
    scripts/start-the-day.sh, this function runs on a loop and never resets
    HEAD first, so a stale checkout used to read as "info" (or "clean") —
    the hook could go inert for days and this check would call it healthy.
    """
    name = "worktree_matches_origin"
    try:
        report = repo_divergence.build_report(_REPO_ROOT)
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": f"divergence check errored: {exc}"}

    tier = report["tier"]
    if tier == "alarm":
        detail = f"ALARM: {repo_divergence.format_file_list(report['alarm_files'])}"
    elif tier == "stale":
        detail = (
            f"STALE (behind_count={report['behind_count']}): "
            f"{repo_divergence.format_file_list(report['stale_files'])}"
        )
    elif tier == "info":
        detail = f"info: {repo_divergence.format_file_list(report['info_files'])}"
    else:
        detail = f"clean (blind_spots={report['blind_spots']})"
    return {"name": name, "ok": report["ok"], "detail": detail}


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

_CHECKS = [
    check_state_db_writable,
    check_orphan_worktrees,
    check_jsonl_sizes,
    check_loop_staleness,
    check_hook_dirs_present,
    check_duckdb_freshness,
    check_circuit_breakers,
    check_blackboard_writable,
    check_dial_chain_integrity,
    check_worktree_matches_origin,
]


def run_checks(checks: list | None = None) -> dict[str, Any]:
    """Run all check functions and return a structured report.

    Each check is called independently; exceptions are caught and reported
    as failed checks — they are never propagated to the caller.

    Returns:
        {
            "ts": <ISO timestamp>,
            "checks": [{name, ok, detail}, ...],
            "overall": <bool — True if every check passed>,
        }
    """
    fns = checks if checks is not None else _CHECKS
    results: list[dict[str, Any]] = []
    for fn in fns:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            result = {"name": fn.__name__, "ok": False, "detail": f"unexpected exception: {exc}"}
        results.append(result)

    overall = all(r.get("ok", False) for r in results)
    return {
        "ts": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": results,
        "overall": overall,
    }


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _human_output(report: dict[str, Any]) -> None:
    """Print a colored human-readable summary to stdout."""
    ts = report.get("ts", "")
    print(f"Health report — {ts}")
    print("-" * 50)
    for check in report.get("checks", []):
        icon = f"{_GREEN}ok {_RESET}" if check.get("ok") else f"{_RED}FAIL{_RESET}"
        name = check.get("name", "?")
        detail = check.get("detail", "")
        print(f"  [{icon}] {name:<28} {detail}")
    print("-" * 50)
    overall = report.get("overall", False)
    verdict = f"{_GREEN}ALL CHECKS PASSED{_RESET}" if overall else f"{_RED}SOME CHECKS FAILED{_RESET}"
    print(f"  Overall: {verdict}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified subsystem health report"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check_p = sub.add_parser("check", help="Run all checks; exit 0 if all pass, 1 otherwise")
    check_p.add_argument(
        "--human",
        action="store_true",
        default=False,
        help="Print colored terminal output instead of JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    report = run_checks()

    if args.human:
        _human_output(report)
    else:
        print(json.dumps(report))

    sys.exit(0 if report["overall"] else 1)


if __name__ == "__main__":
    main()
