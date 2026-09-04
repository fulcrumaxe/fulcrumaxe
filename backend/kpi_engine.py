"""KPI engine — compute and display team performance metrics.

Usage:
    python backend/kpi_engine.py compute   # update KPI snapshot
    python backend/kpi_engine.py show      # print human-readable summary
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean, median

# Regex for the <!-- COMPLETION --> ... <!-- /COMPLETION --> block written by
# post-merge-hook.sh.  Parses plain "key: value" lines (no leading dash).
_COMPLETION_BLOCK_RE = re.compile(
    r"<!--\s*COMPLETION\s*-->(.*?)<!--\s*/COMPLETION\s*-->",
    re.DOTALL,
)
_KV_PLAIN_RE = re.compile(r"^(\w+):\s*(.+)")

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = REPO_ROOT / ".autonomous-team" / "registry.json"
METRICS = REPO_ROOT / ".autonomous-team" / "loop-metrics.jsonl"
KPI_OUT = REPO_ROOT / ".autonomous-team" / "kpi.json"

logger = logging.getLogger(__name__)


def load_registry() -> list[dict]:
    if not REGISTRY.exists():
        logger.warning("registry not found at %s — velocity and cycle time will be zero", REGISTRY)
        return []
    try:
        data = json.loads(REGISTRY.read_text())
        discussions = data.get("discussions", [])
        if not isinstance(discussions, list):
            logger.warning("registry.json missing 'discussions' list")
            return []
        return discussions
    except json.JSONDecodeError as exc:
        logger.warning("could not parse registry.json: %s", exc)
        return []


def load_loop_metrics() -> list[dict]:
    if not METRICS.exists():
        logger.warning("loop-metrics.jsonl not found — idle rate will show no data")
        return []
    records: list[dict] = []
    for lineno, line in enumerate(METRICS.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("skipping malformed line %s: %s", lineno, exc)
    return records


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def extract_actual_hours_from_body(body: str) -> float | None:
    """
    Extract ``actual_hours`` from a Discussion body.

    Tries two formats in priority order:
    1. ``<!-- COMPLETION --> ... <!-- /COMPLETION -->`` block (post-merge-hook format)
    2. Falls back to returning None (registry-based path handles older formats)

    Returns a float if found and parseable, otherwise None.
    """
    m = _COMPLETION_BLOCK_RE.search(body)
    if not m:
        return None
    for line in m.group(1).splitlines():
        kv = _KV_PLAIN_RE.match(line.strip())
        if kv and kv.group(1).strip() == "actual_hours":
            try:
                return float(kv.group(2).strip())
            except ValueError:
                return None
    return None


def compute_velocity(discussions: list[dict]) -> dict:
    done = [d for d in discussions if d.get("status") == "DONE"]
    cutoff = _now_utc() - timedelta(hours=24)
    last_24h = 0
    earliest: datetime | None = None
    for d in done:
        closed = _parse_iso(d.get("closed_at"))
        if closed:
            if closed >= cutoff:
                last_24h += 1
            if earliest is None or closed < earliest:
                earliest = closed
    total_done = len(done)
    if earliest and total_done > 0:
        span_days = max((_now_utc() - earliest).total_seconds() / 86400, 1.0)
        all_time_per_day = round(total_done / span_days, 2)
    else:
        all_time_per_day = 0.0
    return {"last_24h": last_24h, "all_time_per_day": all_time_per_day, "total_done": total_done}


def compute_estimation_accuracy(discussions: list[dict]) -> dict:
    """Compute mean absolute error and within-1.5x rate.

    Mirrors the same three-source lookup as compute_estimation_metrics:
      frontmatter.estimated_hours → top-level estimated_hours → completion.estimated_hours
      completion.actual_hours → top-level actual_hours → COMPLETION body block
    """
    errors: list[float] = []
    within_1_5x: list[bool] = []
    for d in discussions:
        fm = d.get("frontmatter") or {}
        comp = d.get("completion") or {}

        estimated = fm.get("estimated_hours") if fm else None
        if estimated is None:
            estimated = d.get("estimated_hours")
        if estimated is None:
            estimated = comp.get("estimated_hours") if comp else None

        actual = comp.get("actual_hours") if comp else None
        if actual is None:
            actual = d.get("actual_hours")
        if actual is None:
            body = d.get("body") or ""
            if body:
                actual = extract_actual_hours_from_body(body)

        if estimated is None or actual is None:
            continue
        try:
            est, act = float(estimated), float(actual)
        except (TypeError, ValueError):
            continue
        if est <= 0:
            continue
        errors.append(abs(est - act))
        within_1_5x.append(act <= est * 1.5)
    n = len(errors)
    return {
        "tasks_with_estimates": n,
        "mean_absolute_error_hours": round(mean(errors), 3) if errors else None,
        "within_1_5x_pct": round(sum(within_1_5x) / n * 100, 1) if n else None,
    }


def compute_idle_rate(metrics: list[dict]) -> dict:
    if not metrics:
        return {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0}
    cutoff = _now_utc() - timedelta(hours=24)
    recent = [m for m in metrics if (_parse_iso(m.get("timestamp")) or _now_utc()) >= cutoff]

    def _pct(rows: list[dict]) -> float | None:
        if not rows:
            return None
        return round(sum(1 for r in rows if r.get("idle") is True) / len(rows) * 100, 1)

    return {"last_24h_pct": _pct(recent), "all_time_pct": _pct(metrics), "total_iterations": len(metrics)}


def compute_pr_cycle_time(discussions: list[dict]) -> dict:
    hours: list[float] = []
    for d in discussions:
        if d.get("status") != "DONE":
            continue
        created, closed = _parse_iso(d.get("created_at")), _parse_iso(d.get("closed_at"))
        if created and closed and closed > created:
            hours.append((closed - created).total_seconds() / 3600)
    return {
        "mean_hours": round(mean(hours), 2) if hours else None,
        "median_hours": round(median(hours), 2) if hours else None,
        "total_measured": len(hours),
    }


def compute_estimation_metrics(discussions: list[dict], min_samples: int = 5) -> dict:
    """
    Compute structured estimation metrics from registry discussions.

    Uses frontmatter.estimated_hours and completion.actual_hours when present,
    falling back to top-level estimated_hours / actual_hours for backwards compat.
    Also reads ``actual_hours`` from a ``<!-- COMPLETION -->`` block in a ``body``
    field if neither of the above is populated — this handles the common case
    where the registry entry carries the raw Discussion body.

    Returns a dict with:
      total_measured       -- number of discussions with both estimated and actual hours
      accuracy             -- mean per-discussion accuracy score (0..1), or None when
                              total_measured < min_samples (insufficient sample size)
      complexity_velocity  -- complexity_points completed per week
      bias                 -- average (actual - estimated); positive = under-estimating
      min_samples          -- the threshold below which accuracy is null
    """
    accuracy_scores: list[float] = []
    bias_values: list[float] = []
    complexity_points_done: float = 0.0
    oldest_closed: datetime | None = None

    for d in discussions:
        fm = d.get("frontmatter") or {}
        comp = d.get("completion") or {}

        estimated = fm.get("estimated_hours") if fm else None
        if estimated is None:
            estimated = d.get("estimated_hours")
        if estimated is None:
            estimated = comp.get("estimated_hours") if comp else None

        actual = comp.get("actual_hours") if comp else None
        if actual is None:
            actual = d.get("actual_hours")
        # Last resort: parse the COMPLETION block from the raw body if present
        if actual is None:
            body = d.get("body") or ""
            if body:
                actual = extract_actual_hours_from_body(body)

        if estimated is not None and actual is not None:
            try:
                est_f, act_f = float(estimated), float(actual)
            except (TypeError, ValueError):
                continue
            if est_f <= 0:
                continue
            denom = max(est_f, act_f)
            accuracy_scores.append(1.0 - abs(est_f - act_f) / denom)
            bias_values.append(act_f - est_f)

        if d.get("status") == "DONE" and fm:
            cp = fm.get("complexity_points")
            if cp is not None:
                try:
                    complexity_points_done += float(cp)
                except (TypeError, ValueError):
                    pass
            closed = _parse_iso(d.get("closed_at"))
            if closed:
                if oldest_closed is None or closed < oldest_closed:
                    oldest_closed = closed

    n = len(accuracy_scores)
    # Return null accuracy when sample size is below the minimum threshold —
    # a mean over 0–4 samples is statistically meaningless for dashboard display.
    mean_accuracy: float | None
    if n >= min_samples:
        mean_accuracy = round(sum(accuracy_scores) / n, 3)
    else:
        mean_accuracy = None
    mean_bias = round(sum(bias_values) / len(bias_values), 3) if bias_values else None

    if oldest_closed and complexity_points_done > 0:
        span_weeks = max((_now_utc() - oldest_closed).total_seconds() / (86400 * 7), 1.0)
        complexity_velocity = round(complexity_points_done / span_weeks, 2)
    else:
        complexity_velocity = None

    return {
        "tasks_with_estimates": n,
        "total_measured": n,
        "accuracy": mean_accuracy,
        "complexity_velocity": complexity_velocity,
        "bias": mean_bias,
        "min_samples": min_samples,
    }


def compute_all() -> dict:
    discussions = load_registry()
    metrics = load_loop_metrics()
    kpis: dict = {
        "version": 1,
        "computed_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "velocity": compute_velocity(discussions),
        "estimation_accuracy": compute_estimation_accuracy(discussions),
        "estimation": compute_estimation_metrics(discussions),
        "idle_rate": compute_idle_rate(metrics),
        "pr_cycle_time": compute_pr_cycle_time(discussions),
    }
    KPI_OUT.write_text(json.dumps(kpis, indent=2))
    logger.info("KPI snapshot written to %s", KPI_OUT)
    return kpis


def _fmt(value: float | int | None, suffix: str = "") -> str:
    return "no data" if value is None else f"{value}{suffix}"


def show() -> None:
    kpis = compute_all() if not KPI_OUT.exists() else json.loads(KPI_OUT.read_text())
    computed_at = kpis.get("computed_at", "unknown")
    v = kpis.get("velocity", {})
    e = kpis.get("estimation_accuracy", {})
    est = kpis.get("estimation", {})
    ir = kpis.get("idle_rate", {})
    ct = kpis.get("pr_cycle_time", {})

    sep = "=" * 52
    print(f"\n{sep}\n  Team KPI Snapshot  —  {computed_at}\n{sep}")

    print("\n  VELOCITY")
    print(f"    Tasks done (last 24h)   : {_fmt(v.get('last_24h'))}")
    print(f"    Tasks/day (all-time avg): {_fmt(v.get('all_time_per_day'))}")
    print(f"    Total tasks done        : {_fmt(v.get('total_done'))}")

    print("\n  ESTIMATION ACCURACY")
    if e.get("tasks_with_estimates", 0) == 0:
        print("    No estimation data available")
    else:
        print(f"    Tasks with estimates    : {e['tasks_with_estimates']}")
        print(f"    Mean absolute error     : {_fmt(e.get('mean_absolute_error_hours'), 'h')}")
        print(f"    Within 1.5x estimate    : {_fmt(e.get('within_1_5x_pct'), '%')}")

    print("\n  ESTIMATION (STRUCTURED)")
    if est.get("tasks_with_estimates", 0) == 0:
        print("    No structured estimation data yet")
    else:
        print(f"    Tasks measured          : {est['tasks_with_estimates']}")
        print(f"    Mean accuracy (0-1)     : {_fmt(est.get('accuracy'))}")
        print(f"    Complexity velocity     : {_fmt(est.get('complexity_velocity'), ' pts/wk')}")
        bias = est.get("bias")
        if bias is not None:
            sign = "+" if bias >= 0 else ""
            print(f"    Estimation bias         : {sign}{bias}h (+ = under-estimated)")
        else:
            print("    Estimation bias         : no data")

    print("\n  IDLE RATE")
    print(f"    Last 24h                : {_fmt(ir.get('last_24h_pct'), '%')}")
    print(f"    All-time                : {_fmt(ir.get('all_time_pct'), '%')}")
    print(f"    Total loop iterations   : {_fmt(ir.get('total_iterations'))}")

    print("\n  PR CYCLE TIME")
    if ct.get("total_measured", 0) == 0:
        print("    No completed cycle data available")
    else:
        print(f"    Mean                    : {_fmt(ct.get('mean_hours'), 'h')}")
        print(f"    Median                  : {_fmt(ct.get('median_hours'), 'h')}")
        print(f"    Tasks measured          : {_fmt(ct.get('total_measured'))}")

    print(f"\n{sep}\n")


def history(days: int = 30, repo_root: "Path | None" = None) -> list[dict]:
    """Return merged-PRs-per-day for the last *days* days.

    This repo uses squash-merges, so ``git log --merges`` returns nothing
    (squash commits have a single parent).  Instead we walk all commits and
    count those whose subject line matches the ``#N: title (#PR)`` pattern
    that the autonomous team writes for every squash-merged PR.

    Returns a list of ``{"date": "YYYY-MM-DD", "count": int}`` dicts, one per
    day that had at least one squash-merge, sorted chronologically.

    Raises ``ValueError`` when *days* < 1 (mapped to JSON-RPC -32602 upstream).

    Args:
        days: Number of days to look back.
        repo_root: Override the repo root for git log (used for per-project
            scoping).  Defaults to REPO_ROOT (the AF repo).
    """
    if days < 1:
        raise ValueError("days must be >= 1")

    import re
    import subprocess

    effective_root = repo_root if repo_root is not None else REPO_ROOT

    # Squash-merge pattern: subject starts with "#N:" (our standard title format)
    # or ends with "(#N)" (GitHub's squash format).  Both indicate a PR merge.
    _PR_SUBJECT = re.compile(r"^#\d+:|\(#\d+\)\s*$")

    cutoff = _now_utc() - timedelta(days=days)
    since_str = cutoff.strftime("%Y-%m-%d")

    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={since_str}",
                "--pretty=format:%cd\t%s",
                "--date=format:%Y-%m-%d",
            ],
            capture_output=True,
            text=True,
            cwd=effective_root,
            timeout=30,
        )
        raw_lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception as exc:
        logger.warning("git log failed in history(): %s", exc)
        raw_lines = []

    counts: dict[str, int] = {}
    for raw in raw_lines:
        parts = raw.split("\t", 1)
        if len(parts) != 2:
            continue
        date_str, subject = parts
        if _PR_SUBJECT.search(subject):
            counts[date_str] = counts.get(date_str, 0) + 1

    return [{"date": d, "count": c} for d, c in sorted(counts.items())]


def cycle_time_histogram(days: int = 90, repo_root: "Path | None" = None) -> list[dict]:
    """Return cycle-time distribution across merged PRs in the last *days* days.

    Buckets: ``0-2h``, ``2-6h``, ``6-24h``, ``24h+``.

    Spawn time is taken from ``blackboard/discussions/<N>.json`` if available,
    otherwise falls back to the discussion ``created_at`` field in the registry.

    Returns a list of ``{"bucket": str, "count": int}`` dicts for all four
    buckets (count may be 0).

    Args:
        days: Number of days to look back (default 90).  Must be >= 1.
        repo_root: Override the repo root for registry / blackboard lookups
            (used for per-project scoping).  Defaults to REPO_ROOT (AF).
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    _BUCKETS = ["0-2h", "2-6h", "6-24h", "24h+"]
    counts: dict[str, int] = {b: 0 for b in _BUCKETS}

    effective_root = repo_root if repo_root is not None else REPO_ROOT
    # When using the default repo root, delegate to load_registry() so that
    # callers (and tests) can patch a single well-known symbol.  For an
    # explicit repo_root override (per-project scoping) we read directly from
    # that path because load_registry() is hardcoded to REPO_ROOT.
    if repo_root is None:
        discussions = load_registry()
    else:
        registry_path = effective_root / ".autonomous-team" / "registry.json"
        if not registry_path.exists():
            discussions = []
        else:
            try:
                data = json.loads(registry_path.read_text())
                discussions = data.get("discussions", []) if isinstance(data, dict) else []
            except (OSError, json.JSONDecodeError):
                discussions = []

    cutoff = _now_utc() - timedelta(days=days)

    for d in discussions:
        if d.get("status") != "DONE":
            continue
        closed = _parse_iso(d.get("closed_at"))
        if closed is None or closed < cutoff:
            continue

        # Prefer blackboard spawn time
        spawn_time: datetime | None = None
        disc_num = d.get("number") or d.get("id")
        if disc_num is not None:
            bb_path = effective_root / ".autonomous-team" / "blackboard" / "discussions" / f"{disc_num}.json"
            if bb_path.exists():
                try:
                    bb = json.loads(bb_path.read_text())
                    spawn_time = _parse_iso(bb.get("spawned_at") or bb.get("created_at"))
                except Exception:
                    pass

        if spawn_time is None:
            spawn_time = _parse_iso(d.get("created_at"))

        if spawn_time is None or closed <= spawn_time:
            continue

        hours = (closed - spawn_time).total_seconds() / 3600
        if hours < 2:
            counts["0-2h"] += 1
        elif hours < 6:
            counts["2-6h"] += 1
        elif hours < 24:
            counts["6-24h"] += 1
        else:
            counts["24h+"] += 1

    return [{"bucket": b, "count": counts[b]} for b in _BUCKETS]


def main() -> None:
    # Allow running as a script: python backend/kpi_engine.py
    import os as _os  # noqa: F401
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.log import setup_logging
    setup_logging()

    if len(sys.argv) < 2 or sys.argv[1] not in {"compute", "show"}:
        print("Usage: python backend/kpi_engine.py [compute|show]", file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] == "compute":
        compute_all()
    else:
        show()


if __name__ == "__main__":
    main()
