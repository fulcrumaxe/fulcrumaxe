"""
next_work.py — deterministic, read-only, NO-LLM next-work ranker.

Surfaces the most actionable items from data already on disk in <3s.
No network calls, no LLM, no Agent spawns.

Three sources (in order of display):
  1. Stale registry candidates — open DISCUSSING/SPEC_READY discussions,
     ranked oldest-activity-first (by created_at), adjusted by history signal.
  2. Health reds — failed checks from health_report.run_checks().
  3. Coverage gaps — backend modules with no corresponding test file.

History signal (additive re-ranking within stale_discussion):
  Demotes discussions with chronically-failing executor runs or cost spikes.
  Neutral (0) when agent_run history is empty or unavailable.

Usage:
    python3 backend/next_work.py              # human-readable ranked list
    python3 backend/next_work.py --json       # JSON array of items
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script: python3 backend/next_work.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# History signal — pure function, NO LLM, deterministic
# ---------------------------------------------------------------------------

# Weight applied to the history penalty when sorting stale_discussion items.
# Units: equivalent age-days to add as a demotion penalty.
# Increase to make history matter more; set to 0 to disable the signal.
HISTORY_SIGNAL_WEIGHT: float = 5.0

# Thresholds for computing the signal
_FAILURE_RATE_THRESHOLD: float = 0.5   # ≥50% fail rate → full penalty
_COST_SPIKE_MULTIPLIER: float = 3.0    # token count ≥3× median → spike
_FAILURE_VERDICTS: frozenset[str] = frozenset({"fail", "needs-fix", "blocked"})


def history_signal(runs: list[dict]) -> float:
    """Compute a re-ranking penalty score from agent_run history.

    Parameters
    ----------
    runs:
        List of agent_run row dicts for a single discussion (any role).
        Typically produced by ``agent_run_reader.by_role()``.
        Empty list → returns 0.0 (neutral).

    Returns
    -------
    float in [0.0, 1.0] where:
        0.0 = neutral (no history or clean history)
        1.0 = maximum demotion (chronic failures + cost spikes)

    The caller multiplies this value by HISTORY_SIGNAL_WEIGHT to convert
    it to equivalent age-days added to the sort key.

    Signals computed (each contributes 0–0.5; capped at 1.0 total):
      - Failure rate: fraction of completed runs whose verdict is in
        {fail, needs-fix, blocked}.  Penalty = min(rate, 1.0) × 0.5
        only applied when rate ≥ _FAILURE_RATE_THRESHOLD.
      - Cost spike: whether any run's input_tok count is ≥
        _COST_SPIKE_MULTIPLIER × the median input_tok across all runs.
        Penalty = 0.5 when a spike is detected.

    No LLM calls, no network I/O, no subprocess.
    Graceful on missing/None fields.
    """
    if not runs:
        return 0.0

    # --- Failure-rate signal ---
    completed = [r for r in runs if r.get("verdict") is not None]
    if completed:
        failed = sum(
            1 for r in completed
            if str(r.get("verdict", "")).lower() in _FAILURE_VERDICTS
        )
        rate = failed / len(completed)
    else:
        rate = 0.0

    failure_penalty = 0.0
    if rate >= _FAILURE_RATE_THRESHOLD:
        failure_penalty = min(rate, 1.0) * 0.5

    # --- Cost-spike signal ---
    token_counts = [
        r["input_tok"] for r in runs
        if isinstance(r.get("input_tok"), (int, float)) and r["input_tok"] > 0
    ]
    spike_penalty = 0.0
    if len(token_counts) >= 2:
        sorted_counts = sorted(token_counts)
        median_tok = sorted_counts[len(sorted_counts) // 2]
        if median_tok > 0 and max(token_counts) >= _COST_SPIKE_MULTIPLIER * median_tok:
            spike_penalty = 0.5

    return min(failure_penalty + spike_penalty, 1.0)


# ---------------------------------------------------------------------------
# Source 1: Stale registry candidates
# ---------------------------------------------------------------------------

def stale_registry_candidates(
    discussions: list[dict] | None = None,
) -> list[dict]:
    """Return open DISCUSSING/SPEC_READY discussions ranked oldest-first.

    Args:
        discussions: injectable list for testing. If None, loads from disk.

    Returns list of dicts: {category, number, title, status, age_days, reason}
    """
    if discussions is None:
        from backend.registry import DiscussionRegistry  # noqa: PLC0415
        reg = DiscussionRegistry()
        data = reg.load()
        discussions = data.get("discussions", [])

    actionable_statuses = {"DISCUSSING", "SPEC_READY"}
    now = datetime.now(timezone.utc)

    candidates: list[dict] = []
    for d in discussions:
        # Skip closed discussions
        if d.get("closed_at") is not None:
            continue
        status = d.get("status", "")
        if status not in actionable_statuses:
            continue

        number = d.get("number")
        title = d.get("title", "")
        created_at = d.get("created_at", "")

        age_days: float | None = None
        if created_at:
            try:
                ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_days = (now - ts).total_seconds() / 86400.0
            except ValueError:
                pass

        reason = f"open {status}, "
        if age_days is not None:
            reason += f"created {age_days:.0f}d ago"
        else:
            reason += "age unknown"

        candidates.append({
            "category": "stale_discussion",
            "number": number,
            "title": title,
            "status": status,
            "age_days": round(age_days, 1) if age_days is not None else None,
            "reason": reason,
        })

    # Sort oldest first (None age goes last)
    candidates.sort(
        key=lambda c: (
            c["age_days"] is None,          # None → True → sorts last
            -(c["age_days"] or 0),          # negate: largest age_days first
        )
    )
    return candidates


# ---------------------------------------------------------------------------
# Source 2: Health reds
# ---------------------------------------------------------------------------

def health_reds(checks: list | None = None) -> list[dict]:
    """Return failed health checks as next-work candidates.

    Args:
        checks: injectable list of check functions for testing.
                If None, uses the default _CHECKS from health_report.

    Returns list of dicts: {category, name, detail, reason}
    """
    from backend.health_report import run_checks  # noqa: PLC0415

    report = run_checks(checks=checks)
    results = report.get("checks", [])

    reds: list[dict] = []
    for r in results:
        if r.get("ok"):
            continue
        reds.append({
            "category": "health_red",
            "name": r.get("name", "unknown"),
            "detail": r.get("detail", ""),
            "reason": f"health check failed: {r.get('detail', '')}",
        })
    return reds


# ---------------------------------------------------------------------------
# Source 3: Coverage gaps
# ---------------------------------------------------------------------------

def _get_git_tracked_files(repo_root: Path) -> frozenset[str] | None:
    """Return the set of git-tracked file paths (relative to repo_root), or None.

    Returns None when git is unavailable or the directory is not a git repo,
    so callers can fall back to unfiltered behaviour.
    """
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        paths = frozenset(result.stdout.splitlines())
        return paths
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def coverage_gaps(
    backend_dir: Path | None = None,
    tests_dir: Path | None = None,
    git_tracked: "frozenset[str] | None | bool" = True,
) -> list[dict]:
    """Return backend modules with no corresponding test file.

    Enumerates both top-level backend/*.py and subpackage backend/**/*.py
    (e.g. backend/orchestrator/dispatch.py → module "orchestrator.dispatch").

    Excludes: test_* files, __init__.py, __main__.py, conftest.py, _* (private),
    and any files inside the tests/ directory tree.
    Also excludes files NOT tracked by git — untracked files will not appear as gaps.

    Args:
        backend_dir: injectable path for testing. Defaults to repo's backend/.
        tests_dir: injectable path for testing. Defaults to backend/tests/.
        git_tracked: injectable frozenset of repo-relative paths reported by
            ``git ls-files``, or True (default) to auto-detect via subprocess,
            or None/False to skip git filtering (walk all files).
            Pass a frozenset in tests to make git filtering deterministic
            without touching the real git index.

    Returns list of dicts: {category, module, reason}
    """
    import os  # noqa: PLC0415
    import re  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parent.parent
    if backend_dir is None:
        backend_dir = repo_root / "backend"
    if tests_dir is None:
        tests_dir = repo_root / "backend" / "tests"

    # Determine the root for relative-path computation.
    # In normal use backend_dir lives inside repo_root (e.g. <repo>/backend/).
    # In tests backend_dir is often in a tmpdir — detect that so we can
    # compute relative paths from the right anchor.
    try:
        backend_dir.relative_to(repo_root)
        _inside_repo = True
        _tracked_root = repo_root
    except ValueError:
        _inside_repo = False
        _tracked_root = backend_dir.parent  # "repo root" for injected test sets

    # Resolve the set of git-tracked paths for filtering.
    # tracked_set=None means "no filter" (git unavailable or caller opted out).
    tracked_set: "frozenset[str] | None"
    if git_tracked is True:
        # Only run git ls-files when backend_dir is actually inside the repo.
        # If it's a tmpdir (test isolation), skip filtering automatically.
        tracked_set = _get_git_tracked_files(repo_root) if _inside_repo else None
    elif git_tracked is False or git_tracked is None:
        tracked_set = None
    else:
        tracked_set = git_tracked  # caller-supplied frozenset for testing

    def _is_tracked(abs_path: Path) -> bool:
        """Return True if the file should be included (git-tracked or no filter)."""
        if tracked_set is None:
            return True
        try:
            rel = abs_path.relative_to(_tracked_root)
        except ValueError:
            # Cannot compute relative path — include conservatively
            return True
        return str(rel) in tracked_set

    # Directories to skip entirely when recursing (by name)
    _SKIP_DIRS = {"__pycache__", ".pytest_cache", "tests", "fixtures"}

    def _should_skip_stem(stem: str) -> bool:
        return (
            stem.startswith("test_")
            or stem.startswith("__")
            or stem.startswith("_")
            or stem == "conftest"
        )

    # Collect backend module dotted paths.
    # Top-level:  backend/budget.py                  → "budget"
    # Subpackage: backend/orchestrator/dispatch.py   → "orchestrator.dispatch"
    backend_modules: list[str] = []

    for dirpath, dirnames, filenames in os.walk(backend_dir):
        cur = Path(dirpath)

        # Prune directories we never want to recurse into
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        )

        try:
            rel_dir = cur.relative_to(backend_dir)
        except ValueError:
            continue

        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            stem = fname[:-3]
            if _should_skip_stem(stem):
                continue

            abs_file = cur / fname
            if not _is_tracked(abs_file):
                continue  # skip untracked junk files

            parts = list(rel_dir.parts) + [stem]
            dotted = ".".join(parts)  # "budget" or "orchestrator.dispatch"
            backend_modules.append(dotted)

    # Collect test file stems for filename-convention matching.
    # test_dispatch.py covers any module whose last segment is "dispatch".
    existing_test_stems: set[str] = set()
    test_files: list[Path] = []

    if tests_dir.exists():
        for p in tests_dir.rglob("test_*.py"):
            if not _is_tracked(p):
                continue  # skip untracked test files too
            existing_test_stems.add(p.stem[len("test_"):])
            test_files.append(p)

    # Also check backend/test_*.py directly
    for p in backend_dir.glob("test_*.py"):
        if not _is_tracked(p):
            continue
        existing_test_stems.add(p.stem[len("test_"):])
        test_files.append(p)

    # Collect modules covered via imports in any test file.
    # Patterns match the dotted path after "backend.":
    #   "from backend.orchestrator.dispatch import X" → "orchestrator.dispatch"
    #   "import backend.orchestrator.dispatch"        → "orchestrator.dispatch"
    #   "from backend import budget"                  → "budget"
    _import_patterns = [
        re.compile(r"from\s+backend\.([\w.]+)"),
        re.compile(r"import\s+backend\.([\w.]+)"),
        re.compile(r"from\s+backend\s+import\s+(\w+)"),
    ]

    modules_set = set(backend_modules)

    def _resolve_import(capture: str) -> "str | None":
        """Return the longest prefix of capture that is a known module key."""
        if capture in modules_set:
            return capture
        # Strip trailing attribute-access words until we find a known module
        parts = capture.split(".")
        for end in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:end])
            if candidate in modules_set:
                return candidate
        return None

    import_covered: set[str] = set()
    for tf in test_files:
        try:
            text = tf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in _import_patterns:
            for match in pat.finditer(text):
                resolved = _resolve_import(match.group(1))
                if resolved is not None:
                    import_covered.add(resolved)

    gaps: list[dict] = []
    for mod in backend_modules:
        last_seg = mod.rsplit(".", 1)[-1]
        covered = last_seg in existing_test_stems or mod in import_covered
        if not covered:
            file_path = "backend/" + mod.replace(".", "/") + ".py"
            gaps.append({
                "category": "coverage_gap",
                "module": mod,
                "reason": f"no test file or import reference found for {file_path}",
            })

    return gaps


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------

def _load_discussion_runs(discussion_number: int | None) -> list[dict]:
    """Fetch recent executor runs for *discussion_number* from agent_run_reader.

    Returns empty list when agent_run is unavailable, the discussion number is
    None, or any error occurs (graceful degradation).
    """
    if discussion_number is None:
        return []
    try:
        from backend.agent_run_reader import by_role  # noqa: PLC0415
        # Look back 30 days for executor runs on this discussion.
        from datetime import timedelta  # noqa: PLC0415
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        all_runs = by_role("executor", since_iso=since)
        # Filter to this discussion
        return [r for r in all_runs if r.get("discussion") == discussion_number]
    except Exception:  # noqa: BLE001
        return []


def rank_next_work(
    discussions: list[dict] | None = None,
    health_checks: list | None = None,
    backend_dir: Path | None = None,
    tests_dir: Path | None = None,
    run_history: "dict[int, list[dict]] | None" = None,
) -> list[dict]:
    """Assemble and return all ranked next-work items.

    Items are grouped by category (stale_discussion > health_red > coverage_gap).
    Within stale_discussion, the base age ordering is adjusted by the history
    signal: discussions with chronically-failing executor runs or cost spikes
    are demoted (sorted toward the end of the stale_discussion group).

    Parameters
    ----------
    discussions:   Injectable list of discussion dicts (for testing).
    health_checks: Injectable list of check callables (for testing).
    backend_dir:   Injectable backend Path (for testing).
    tests_dir:     Injectable tests Path (for testing).
    run_history:   Injectable mapping of {discussion_number: [run_dicts]}
                   (for testing).  Pass an empty dict to simulate no history.
                   Pass None (default) to load from agent_run_reader at runtime.

    Returns a flat list of item dicts, each augmented with a
    ``history_penalty`` float field on stale_discussion items.
    """
    stale = stale_registry_candidates(discussions=discussions)

    # Apply history signal to stale_discussion items.
    # Sort key = (-effective_age) where effective_age = age_days + penalty_days.
    # Items with more history penalty sort after items with lower penalty.
    for item in stale:
        disc_num = item.get("number")
        if run_history is not None:
            runs = run_history.get(disc_num, []) if disc_num is not None else []
        else:
            runs = _load_discussion_runs(disc_num)

        penalty = history_signal(runs)
        item["history_penalty"] = round(penalty, 4)

    # Re-sort within stale: primary = age_days descending (oldest first),
    # secondary = history_penalty ascending (higher penalty = further back).
    # Items with no age go last regardless.
    stale.sort(
        key=lambda c: (
            c["age_days"] is None,                            # None → last
            -(c["age_days"] or 0) + c["history_penalty"] * HISTORY_SIGNAL_WEIGHT,
        )
    )

    items: list[dict] = []
    items.extend(stale)
    items.extend(health_reds(checks=health_checks))
    items.extend(coverage_gaps(backend_dir=backend_dir, tests_dir=tests_dir))
    return items


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _human_output(items: list[dict]) -> str:
    lines: list[str] = []
    lines.append("next-work ranked candidates")
    lines.append("=" * 60)

    categories = [
        ("stale_discussion", "Stale Discussions (open, oldest first)"),
        ("health_red", "Health Reds (failing checks)"),
        ("coverage_gap", "Coverage Gaps (untested modules)"),
    ]

    for cat_key, cat_label in categories:
        cat_items = [i for i in items if i.get("category") == cat_key]
        if not cat_items:
            continue
        lines.append(f"\n{cat_label} ({len(cat_items)})")
        lines.append("-" * 50)
        for item in cat_items:
            if cat_key == "stale_discussion":
                num = item.get("number", "?")
                title = (item.get("title") or "")[:55]
                reason = item.get("reason", "")
                lines.append(f"  D#{num:<6} {title:<55}  [{reason}]")
            elif cat_key == "health_red":
                name = item.get("name", "?")
                detail = (item.get("detail") or "")[:60]
                lines.append(f"  {name:<32}  {detail}")
            elif cat_key == "coverage_gap":
                mod = item.get("module", "?")
                file_path = "backend/" + mod.replace(".", "/") + ".py"
                lines.append(f"  {file_path}")

    if not items:
        lines.append("  (no candidates found)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="next_work",
        description=(
            "Deterministic, read-only ranker of next-work candidates. "
            "No network/LLM calls. Sources: stale discussions, health reds, coverage gaps."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit a JSON array instead of human-readable output.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    items = rank_next_work()

    if args.json_output:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        print(_human_output(items))

    return 0


if __name__ == "__main__":
    sys.exit(main())
