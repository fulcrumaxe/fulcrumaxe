#!/usr/bin/env python3
"""team_status.py — at-a-glance status of the autonomous team.

Usage:
    python3 backend/team_status.py              # human-readable, ~25 lines
    python3 backend/team_status.py --json       # machine-readable JSON
    python3 backend/team_status.py --watch      # auto-refresh every 5s
    python3 backend/team_status.py --watch --interval 10

Replaces chaining budget.py + kpi_engine.py + cost_tracker.py for quick checks.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root: `python3 backend/team_status.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend._repo import REPO
from backend.snapshot_path import SNAPSHOT_PATH
from backend.gate_streak import current_streak, render_line as _gate_streak_render_line

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
# SNAPSHOT_PATH is imported from backend.snapshot_path — the one definition.

# Lazy import of subscription_usage — only when gate is on
def _subscription_data() -> dict | None:
    """Return subscription usage dict if gate is on, else None.

    When gates.subscription_throttle_weekly is also on, a 'weekly' sub-dict
    is merged in from subscription_usage.py --weekly --json.
    """
    try:
        gate_rc, gate_out = _run([sys.executable, "backend/control_plane.py", "get", "gates.subscription_throttle"])
        gate_val = gate_out.strip().strip('"')
        if gate_val != "true":
            return None
        rc, out = _run([sys.executable, "backend/subscription_usage.py", "--json"])
        if rc == 0:
            data = _json_or(out)
            if isinstance(data, dict):
                target_rc, target_out = _run([
                    sys.executable, "backend/control_plane.py", "get",
                    "policies.subscription.target_percent"
                ])
                target_pct = None
                try:
                    target_pct = float(target_out.strip().strip('"'))
                except (ValueError, AttributeError):
                    target_pct = 80.0
                data["target_percent"] = target_pct

                # Optionally add weekly numbers when that gate is also on
                wgate_rc, wgate_out = _run([
                    sys.executable, "backend/control_plane.py", "get",
                    "gates.subscription_throttle_weekly"
                ])
                if wgate_out.strip().strip('"') == "true":
                    wrc, wout = _run([
                        sys.executable, "backend/subscription_usage.py", "--weekly", "--json"
                    ])
                    if wrc == 0:
                        weekly_data = _json_or(wout)
                        if isinstance(weekly_data, dict):
                            data["weekly"] = weekly_data

                return data
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _json_or(text: str, default=None):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _snapshot_age(snapshot: dict | None) -> float | None:
    if not snapshot:
        return None
    raw = snapshot.get("generated_at")
    if not raw:
        return None
    try:
        from datetime import datetime, timezone
        generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - generated).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _load_snapshot() -> tuple[dict | None, str | None]:
    """Return (snapshot_dict, stale_message). stale_message is set if snapshot unusable."""
    try:
        # Import from backend (same package)
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from loop_snapshot import load, SnapshotStale
        snapshot = load(path=str(SNAPSHOT_PATH), max_age_seconds=900)
        return snapshot, None
    except Exception as exc:  # noqa: BLE001
        # Try raw load to show age info
        p = Path(SNAPSHOT_PATH)
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                age = _snapshot_age(raw)
                age_str = f"{age:.0f}s old" if age is not None else "age unknown"
                return None, f"snapshot stale ({age_str}) — run /loop or wait for next iteration"
            except Exception:  # noqa: BLE001
                pass
        return None, f"snapshot stale/missing — run /loop or wait for next iteration"


def _discussions_summary(snapshot: dict | None) -> dict:
    """Count discussions by status from snapshot.

    Returns flat counts at the top level (backward compatible) PLUS a nested
    ``by_status`` dict with the same counts keyed by status string.
    """
    status_keys = ("DISCUSSING", "SPEC_READY", "IMPLEMENTING", "REVIEWING", "DONE", "OTHER")
    counts: dict[str, int] = {"total": 0}
    for k in status_keys:
        counts[k] = 0

    if snapshot:
        discussions = snapshot.get("discussions", []) or []
        counts["total"] = len(discussions)
        for d in discussions:
            status = (d.get("status") or "UNKNOWN").strip()
            # status may be "SPEC_READY SINCE:..." — extract leading word
            key = status.split()[0] if status else "OTHER"
            if key in counts:
                counts[key] += 1
            else:
                counts["OTHER"] += 1

    # Add nested by_status view (same data, additive — does not change flat keys).
    counts["by_status"] = {k: counts[k] for k in status_keys}
    return counts


def _count_stuck_prs() -> int:
    """Count open PRs that are stuck (code-review-needs-fix, age >30min).

    Shells out to scripts/lib/stuck-pr-detect.sh via list_stuck_prs() so
    the detection logic is defined once and shared with sweep-stuck-prs.sh.
    Returns 0 on any error (non-fatal).
    """
    detect_script = REPO_ROOT / "scripts" / "lib" / "stuck-pr-detect.sh"
    if not detect_script.exists():
        return 0
    try:
        threshold = int(os.environ.get("STUCK_PR_THRESHOLD_MINUTES", "30"))
        cmd = [
            "bash", "-c",
            f"source {detect_script} && list_stuck_prs {threshold}",
        ]
        rc, out = _run(cmd, timeout=20)
        if rc != 0:
            return 0
        data = _json_or(out, []) or []
        return len(data) if isinstance(data, list) else 0
    except Exception:  # noqa: BLE001
        return 0


def _prs_summary() -> dict:
    """Fetch open PRs and group by gate-label state."""
    rc, out = _run([
        "gh", "pr", "list",
        "--repo", REPO,
        "--state", "open",
        "--json", "number,title,labels",
    ])
    if rc != 0:
        return {"error": out.strip()[:200], "items": []}

    prs = _json_or(out, []) or []
    groups: dict[str, list] = {
        "no-review": [],
        "code-review-passed": [],
        "security-review-triggered": [],
        "ready-to-merge": [],
    }

    for pr in prs:
        num = pr.get("number")
        title = pr.get("title", "")
        labels = [lb["name"] for lb in (pr.get("labels") or [])]

        has_code_passed = "code-review-passed" in labels
        has_sec_triggered = "security-review-triggered" in labels
        has_sec_passed = "security-review-passed" in labels
        has_needs_fix = any("needs-fix" in lb for lb in labels)

        entry = {"number": num, "title": title, "labels": labels}

        if has_code_passed and (not has_sec_triggered or has_sec_passed):
            groups["ready-to-merge"].append(entry)
        elif has_code_passed:
            groups["code-review-passed"].append(entry)
        elif has_sec_triggered:
            groups["security-review-triggered"].append(entry)
        else:
            groups["no-review"].append(entry)

    return {"groups": groups, "total": len(prs)}


def _team_tasks_summary() -> list[dict]:
    """Primary: read recent tasks from ~/.claude/tasks/fulcrumaxe/.

    Falls back to [] on any error — callers use agent-feed.jsonl as fallback.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from agent_teams_substrate import read_team_status  # noqa: PLC0415
        return read_team_status()
    except Exception:  # noqa: BLE001
        return []


def _read_spawn_guard_live_count() -> int | None:
    """Read global_in_flight from .autonomous-team/spawn-guard-stats.json.

    This is the authoritative count of actually-running claude subprocesses —
    maintained by SpawnGuard.acquire()/release() in api.py.  Returns None when
    the file is absent or unreadable (e.g. api.py is not running).
    """
    stats_file = REPO_ROOT / ".autonomous-team" / "spawn-guard-stats.json"
    try:
        data = json.loads(stats_file.read_text(encoding="utf-8"))
        val = data.get("global_in_flight")
        if isinstance(val, int) and val >= 0:
            return val
    except Exception:  # noqa: BLE001
        pass
    return None


def _agents_summary(snapshot: dict | None) -> dict:
    """Return in-flight agents and queue depth.

    In-flight count comes from spawn_guard (authoritative — reflects real running
    subprocesses).  Queue depth is derived from substrate tasks that are recent
    (created within the last 20 minutes) and in a non-terminal status, capped at
    the known running count so it can never exceed the concurrency cap.

    Fallback when spawn_guard stats unavailable: substrate recent non-terminal tasks only.
    Legacy blackboard fallback retained for environments without the substrate.
    """
    _TERMINAL = frozenset({"done", "fail", "pass", "needs-fix", "skip"})
    _RECENT_WINDOW_SECS = 20 * 60  # 20 minutes — slightly > loop interval

    import time as _time  # noqa: PLC0415

    now_ts = _time.time()

    # Primary: spawn_guard's persisted stats file (updated by api.py on every acquire/release)
    live_count = _read_spawn_guard_live_count()

    # Secondary: substrate tasks — but ONLY recent AND non-terminal ones
    team_tasks = _team_tasks_summary()
    recent_active: list[dict] = []
    for t in team_tasks:
        if t.get("status") in _TERMINAL:
            continue
        ts_str = t.get("created_at") or t.get("ts") or ""
        if not ts_str:
            continue
        try:
            from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
            ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if now_ts - ts <= _RECENT_WINDOW_SECS:
            recent_active.append(t)

    if live_count is not None:
        # Authoritative count from spawn_guard.  Queue = tasks waiting beyond live slots.
        queue_depth = max(0, len(recent_active) - live_count)
        return {
            "in_flight": recent_active[:live_count],  # display slice
            "in_flight_count": live_count,            # authoritative number
            "queue_depth": queue_depth,
            "team_tasks_total": len(team_tasks),
        }

    if recent_active:
        return {
            "in_flight": recent_active,
            "in_flight_count": len(recent_active),
            "queue_depth": 0,
            "team_tasks_total": len(team_tasks),
        }

    # Fallback: legacy blackboard
    if not snapshot:
        return {"in_flight": [], "in_flight_count": 0, "queue_depth": 0}
    bb = snapshot.get("blackboard") or {}
    queue_pending = bb.get("queue_pending") or []
    queue_active = bb.get("queue_active") or []
    return {
        "in_flight": queue_active,
        "in_flight_count": len(queue_active),
        "queue_depth": len(queue_pending),
    }


def _budget_summary(project: str | None = None) -> dict:
    """Return {ceiling, spent, remaining, no_agents_recorded} by reading BudgetTracker directly.

    Previously this shelled out to `python3 backend/budget.py status` via _run(), which
    concatenates stdout+stderr.  Any warning line in stderr contaminated the JSON and
    caused json.loads to fail silently, returning no `spent` key (rendered as 0 in the
    Loop Controller Budget tile).  Using the library in-process avoids that fragility.

    When *project* is provided, reads from that project's blackboard rather than the
    default (AF's) blackboard.  This ensures the Budget tile shows the correct spent
    value when the dashboard is viewing a non-AF project.

    Returns ``no_agents_recorded: True`` when the project blackboard has no agent spend
    records, so the UI can distinguish "genuinely zero spend" from a data error.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from budget import BudgetTracker  # noqa: PLC0415
        from blackboard import Blackboard  # noqa: PLC0415

        bb: Blackboard | None = None
        if project:
            try:
                from backend.state_paths import for_project as _fp  # noqa: PLC0415
                project_paths = _fp(project)
                bb_dir = project_paths.state_dir / "blackboard"
                bb = Blackboard(root=bb_dir)
            except Exception:  # noqa: BLE001
                bb = None  # fall back to default

        tracker = BudgetTracker(bb=bb) if bb is not None else BudgetTracker()
        status = tracker.get_status()
        agents = status.get("agents") or []
        return {
            "ceiling": status.get("ceiling"),
            "spent": status.get("spent"),
            "remaining": status.get("remaining"),
            "no_agents_recorded": len(agents) == 0,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def _cost_summary() -> dict:
    """Return cost totals and top-5 by-Discussion ledger.

    Never raises — on any failure returns an error-annotated dict with safe defaults.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from cost_tracker import CostTracker  # noqa: PLC0415
        ct = CostTracker()
        full = ct.get_session_cost()
        top5 = sorted(
            full.get("by_discussion", []),
            key=lambda x: x.get("total_cost_usd", 0.0),
            reverse=True,
        )[:5]
        return {
            "total_cost_usd": full.get("total_cost_usd", 0.0),
            "by_discussion_top_5": top5,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "total_cost_usd": 0.0,
            "by_discussion_top_5": [],
            "error": str(exc)[:200],
        }


def _top_cost_role_line() -> str:
    """Read .autonomous-team/role-efficiency.json (if fresh) and return one-line summary.

    Returns a display string — never raises.
    """
    role_json = REPO_ROOT / ".autonomous-team" / "role-efficiency.json"
    try:
        if not role_json.exists():
            return "top-cost role: n/a"
        import time as _time  # noqa: PLC0415
        age_seconds = _time.time() - role_json.stat().st_mtime
        if age_seconds > 86400:  # 24 hours
            return "top-cost role: n/a (stale)"
        with role_json.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        roles = data.get("roles") or []
        if not roles:
            return "top-cost role: n/a (no data)"
        top = roles[0]
        role = top.get("role", "?")
        cost = top.get("total_cost_usd", 0.0)
        runs = top.get("total_runs", 0)
        nfr = round(top.get("needs_fix_rate", 0.0) * 100, 1)
        window = data.get("window_days", 7)
        return (
            f"top-cost role ({window}d): {role} — ${cost:.2f}"
            f" ({runs} runs, needs-fix {nfr}%)"
        )
    except Exception:  # noqa: BLE001
        return "top-cost role: n/a"


def _circuit_breakers_summary() -> dict:
    """Call circuit_breaker.py summary --json and return parsed result.

    Never raises — on any failure returns an error-annotated dict with safe defaults.
    """
    try:
        rc, out = _run([sys.executable, "backend/circuit_breaker.py", "summary", "--json"], timeout=2)
        if rc == 0:
            data = _json_or(out)
            if isinstance(data, dict):
                return data
        return {"tripped": [], "warnings": [], "threshold": 3, "error": out.strip()[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"tripped": [], "warnings": [], "threshold": 3, "error": str(exc)[:200]}


def _spawn_breaker_summary() -> dict:
    """Call claude_spawn_tracker.py summary --json and return parsed result.

    Never raises — on any failure returns an error-annotated dict with safe defaults.
    """
    try:
        rc, out = _run(
            [sys.executable, "backend/claude_spawn_tracker.py", "summary", "--json"], timeout=2
        )
        if rc == 0:
            data = _json_or(out)
            if isinstance(data, dict):
                return data
        return {
            "tripped": False, "spawns_1h": 0, "spawns_24h": 0, "spend_24h_usd": 0.0,
            "per_source": {}, "thresholds": {}, "tripped_meta": None,
            "error": out.strip()[:200],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "tripped": False, "spawns_1h": 0, "spawns_24h": 0, "spend_24h_usd": 0.0,
            "per_source": {}, "thresholds": {}, "tripped_meta": None,
            "error": str(exc)[:200],
        }


def _kpi_summary() -> dict:
    rc, out = _run([sys.executable, "backend/kpi_engine.py", "show"])
    # Try to find "tasks completed" line
    tasks_24h = None
    for line in out.splitlines():
        low = line.lower()
        if "task" in low and ("24h" in low or "completed" in low):
            # extract number
            parts = line.split()
            for p in parts:
                try:
                    tasks_24h = int(p)
                    break
                except ValueError:
                    continue
            if tasks_24h is not None:
                break
    return {"tasks_24h": tasks_24h, "raw": out.strip()[:300] if rc == 0 else None}


def _recent_merges() -> list[dict]:
    rc, out = _run([
        "gh", "pr", "list",
        "--repo", REPO,
        "--state", "merged",
        "--limit", "3",
        "--json", "number,title,mergedAt",
    ])
    if rc != 0:
        return []
    prs = _json_or(out, []) or []
    return [{"number": p.get("number"), "title": p.get("title"), "merged_at": p.get("mergedAt")} for p in prs]


def _gate_streak_summary() -> dict:
    """Thin glue onto backend/gate_streak.py — counting logic lives there
    (Module-per-Feature); this hub only reads the one number."""
    try:
        return {"count": current_streak()}
    except Exception:
        return {"count": 0}


def _errors_from_snapshot(snapshot: dict | None) -> list[str]:
    if not snapshot:
        return []
    meta = snapshot.get("meta") or {}
    if isinstance(meta, dict):
        errs = meta.get("errors") or []
    else:
        errs = []
    warnings = snapshot.get("warnings") or []
    return list(errs) + list(warnings)


def _retros_summary() -> dict:
    """Load recent retro summary from agent-retros.jsonl."""
    retros_path = Path(".autonomous-team/agent-retros.jsonl")
    if not retros_path.exists():
        return {"total": 0, "recent_24h": 0, "corrected": 0, "shadow": 0, "top_classifiers": []}
    entries = []
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        with open(retros_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return {"total": 0, "recent_24h": 0, "corrected": 0, "shadow": 0, "top_classifiers": []}

    total = len(entries)
    corrected = sum(1 for e in entries if e.get("work_corrected"))
    shadow = sum(1 for e in entries if e.get("shadow_mode"))
    recent_24h = 0
    clf_counts: dict[str, int] = {}
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for e in entries:
            ts_str = e.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts >= cutoff:
                    recent_24h += 1
                    clf = e.get("classifier", "unknown")
                    clf_counts[clf] = clf_counts.get(clf, 0) + 1
            except (ValueError, AttributeError):
                pass
    except Exception:
        pass
    top = sorted(clf_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    return {
        "total": total,
        "recent_24h": recent_24h,
        "corrected": corrected,
        "shadow": shadow,
        "top_classifiers": [{"classifier": c, "count": n} for c, n in top],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _freshness_label(age: float | None) -> str:
    if age is None:
        return "unknown"
    if age < 120:
        return "fresh"
    if age < 600:
        return "ok"
    if age < 900:
        return "aging"
    return "stale"


def _human_output(data: dict, stale_message: str | None) -> str:
    lines: list[str] = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"fulcrumaxe team status  —  {now_str}")
    lines.append("=" * 60)

    # Snapshot freshness
    age = data.get("snapshot_age_seconds")
    if stale_message:
        lines.append(f"  SNAPSHOT  {stale_message}")
    else:
        label = _freshness_label(age)
        age_str = f"{age:.0f}s" if age is not None else "?"
        lines.append(f"  snapshot  age={age_str}  ({label})")

    lines.append("")

    # Discussions
    disc = data.get("discussions") or {}
    total = disc.get("total", 0)
    lines.append(f"  DISCUSSIONS  total={total}")
    for key in ("DISCUSSING", "SPEC_READY", "IMPLEMENTING", "REVIEWING", "DONE"):
        count = disc.get(key, 0)
        if count:
            lines.append(f"    {key:<14} {count}")

    lines.append("")

    # PRs
    prs = data.get("prs") or {}
    pr_total = prs.get("total", 0)
    lines.append(f"  OPEN PRs  total={pr_total}")
    groups = prs.get("groups") or {}
    for grp, items in groups.items():
        if items:
            lines.append(f"    {grp:<28} {len(items)}")

    stuck_count = data.get("stuck_prs", 0)
    if stuck_count:
        lines.append(f"  Stuck PRs: {stuck_count}  (code-review-needs-fix >30min)")
    else:
        lines.append(f"  Stuck PRs: 0")

    lines.append("")

    # Agents
    agents = data.get("agents") or {}
    in_flight_count = agents.get("in_flight_count", len(agents.get("in_flight") or []))
    queue_depth = agents.get("queue_depth", 0)
    lines.append(f"  AGENTS  in-flight={in_flight_count}  queue={queue_depth}")

    lines.append("")

    # Budget
    budget = data.get("budget") or {}
    if "error" in budget:
        lines.append(f"  BUDGET  (unavailable)")
    else:
        spent = budget.get("spent", 0)
        ceiling = budget.get("ceiling", 0)
        remaining = budget.get("remaining", 0)
        pct = round(100 * spent / ceiling, 1) if ceiling else 0
        lines.append(f"  BUDGET  spent={spent:,}  ceiling={ceiling:,}  remaining={remaining:,}  ({pct}%)")

    lines.append("")

    # Cost by Discussion (top 5)
    top5 = (data.get("cost") or {}).get("by_discussion_top_5") or []
    if top5:
        lines.append("  COST BY DISCUSSION (top 5)")
        for entry in top5:
            lines.append(
                f"    Discussion #{entry['discussion']:<5} "
                f"${entry['total_cost_usd']:.4f}  "
                f"({entry.get('agent_count', len(entry.get('agents', [])))} agents)"
            )
        lines.append("")

    # Top-cost role (from role-efficiency.json)
    lines.append(f"  {_top_cost_role_line()}")
    lines.append("")

    # Circuit breakers
    cb = data.get("circuit_breakers") or {}
    tripped = cb.get("tripped") or []
    if not tripped:
        lines.append("  Circuit breakers: none")
    else:
        parts = []
        for entry in tripped:
            disc = entry.get("discussion", "?")
            agent = entry.get("agent") or "unknown"
            reason = entry.get("reason") or "unknown"
            parts.append(f"#{disc} {agent}: {reason}")
        lines.append(f"  Circuit breakers: {len(tripped)} tripped ({', '.join(parts)})")

    lines.append("")

    # Spawn breaker
    sb = data.get("spawn_breaker") or {}
    sb_tripped = sb.get("tripped", False)
    sb_meta = sb.get("tripped_meta") or {}
    sb_reason = sb_meta.get("reason", "") if sb_tripped else ""
    sb_1h = sb.get("spawns_1h", 0)
    sb_24h = sb.get("spawns_24h", 0)
    sb_spend = sb.get("spend_24h_usd", 0.0)
    if sb_tripped:
        lines.append(
            f"  Spawn breaker: TRIPPED ({sb_reason})"
            f"  spawns 1h={sb_1h}  24h={sb_24h}  spend 24h=${sb_spend:.4f}"
        )
    else:
        lines.append(
            f"  Spawn breaker: closed"
            f"  spawns 1h={sb_1h}  24h={sb_24h}  spend 24h=${sb_spend:.4f}"
        )

    lines.append("")

    # KPI
    kpi = data.get("kpi") or {}
    tasks = kpi.get("tasks_24h")
    tasks_str = str(tasks) if tasks is not None else "?"
    lines.append(f"  KPI  tasks_24h={tasks_str}")

    lines.append("")

    # Recent merges
    merges = data.get("recent_merges") or []
    lines.append(f"  RECENT MERGES  (last {len(merges)})")
    for m in merges:
        num = m.get("number", "?")
        title = (m.get("title") or "")[:45]
        lines.append(f"    PR #{num}  {title}")

    lines.append("")

    # Self-observe retros
    retros = data.get("retros") or {}
    retro_total = retros.get("total", 0)
    retro_24h = retros.get("recent_24h", 0)
    retro_corrected = retros.get("corrected", 0)
    retro_shadow = retros.get("shadow", 0)
    top_clf = retros.get("top_classifiers") or []
    retro_line = (
        f"  SELF-OBSERVE RETROS  total={retro_total}"
        f"  last_24h={retro_24h}  corrected={retro_corrected}  shadow={retro_shadow}"
    )
    lines.append(retro_line)
    if top_clf:
        clf_parts = ", ".join(f"{c['classifier']}={c['count']}" for c in top_clf)
        lines.append(f"    top classifiers (24h): {clf_parts}")

    lines.append("")

    # CI gate streak (D#2271) — nothing printed at streak=0
    gate_streak_line = _gate_streak_render_line((data.get("gate_streak") or {}).get("count", 0))
    if gate_streak_line:
        lines.append(gate_streak_line)
        lines.append("")

    # Subscription quota (only shown when gate is on)
    sub = data.get("subscription")
    if sub and isinstance(sub, dict):
        pct = sub.get("percent", 0.0)
        target = sub.get("target_percent", 80.0)
        wh = sub.get("window_hours", 5)
        plan = sub.get("plan", "unknown")
        sub_line = (
            f"  SUBSCRIPTION  used={pct:.1f}%  target={target:.0f}%"
            f"  window={wh:.0f}h  plan={plan}"
        )
        # Append weekly numbers if present (gates.subscription_throttle_weekly)
        weekly = sub.get("weekly")
        if weekly and isinstance(weekly, dict):
            spct = weekly.get("weekly_pct_sonnet", 0.0)
            opct = weekly.get("weekly_pct_opus", 0.0)
            ttr = weekly.get("time_to_reset", "?")
            sub_line += f"  |  weekly sonnet={spct:.1f}%  opus={opct:.1f}%  reset_in={ttr}"
        lines.append(sub_line)
        lines.append("")

    # Errors
    errors = data.get("errors") or []
    if errors:
        lines.append(f"  ERRORS  ({len(errors)})")
        for e in errors[:5]:
            lines.append(f"    {str(e)[:80]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

def _gather(snapshot: dict | None, stale_message: str | None, project: str | None = None) -> dict:
    age = _snapshot_age(snapshot) if snapshot else None
    agents = _agents_summary(snapshot)
    sub_data = _subscription_data()
    result = {
        "snapshot_age_seconds": age,
        "discussions": _discussions_summary(snapshot),
        "prs": _prs_summary(),
        "stuck_prs": _count_stuck_prs(),
        "agents": agents,
        "queue": {"depth": agents.get("queue_depth", 0), "pending": agents.get("in_flight", [])},
        "budget": _budget_summary(project=project),
        "circuit_breakers": _circuit_breakers_summary(),
        "spawn_breaker": _spawn_breaker_summary(),
        "kpi": _kpi_summary(),
        "cost": _cost_summary(),
        "recent_merges": _recent_merges(),
        "retros": _retros_summary(),
        "gate_streak": _gate_streak_summary(),
        "errors": _errors_from_snapshot(snapshot),
    }
    # Only include subscription key when gate is on
    if sub_data is not None:
        result["subscription"] = sub_data
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="At-a-glance status of the autonomous team.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output machine-readable JSON instead of human text.")
    parser.add_argument("--watch", action="store_true",
                        help="Refresh status in place repeatedly (ignored with --json).")
    parser.add_argument("--interval", type=int, default=5, metavar="N",
                        help="Refresh interval in seconds for --watch (default 5, min 1).")
    args = parser.parse_args()

    if args.json_output and args.watch:
        print("ERROR: --watch cannot be combined with --json", file=sys.stderr)
        sys.exit(2)

    if args.watch and args.interval < 1:
        print("ERROR: --interval must be >= 1", file=sys.stderr)
        sys.exit(1)

    def _once() -> None:
        snapshot, stale_message = _load_snapshot()
        # Best-effort maintenance: prune stale terminal substrate task files.
        # This keeps ~/.claude/tasks/fulcrumaxe/ from accumulating indefinitely.
        try:
            sys.path.insert(0, str(REPO_ROOT / "backend"))
            from agent_teams_substrate import prune_terminal_substrate_tasks  # noqa: PLC0415
            prune_terminal_substrate_tasks()
        except Exception:  # noqa: BLE001
            pass  # non-fatal — never let reaper errors break the status display
        data = _gather(snapshot, stale_message)

        if args.json_output:
            print(json.dumps(data, indent=2))
        else:
            text = _human_output(data, stale_message)
            print(text)

    if args.watch:
        while True:
            # Clear screen
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()
            _once()
            time.sleep(args.interval)
    else:
        _once()


if __name__ == "__main__":
    main()
