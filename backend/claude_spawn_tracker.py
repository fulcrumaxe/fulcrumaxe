"""
claude_spawn_tracker.py — Repo-wide Claude Code subprocess spawn budget + circuit breaker.

Tracks Claude Code subprocess spawn count and estimated USD spend over rolling 1h / 24h
windows in the blackboard. Trips when any of three configurable thresholds are crossed and
refuses further spawns until manual or auto reset.

Integration contract
--------------------
Every call site that exec's `claude -p` (or equivalent subprocess) MUST call
``record()`` BEFORE ``subprocess.Popen``. If ``SpawnBlocked`` is raised, abort the spawn
and surface the error to the caller. Example::

    from backend.claude_spawn_tracker import record, SpawnBlocked
    try:
        record(source="loop_run")
    except SpawnBlocked as exc:
        return {"status": "error", "error": "spawn_breaker_tripped", "exit_code": 503}
    proc = subprocess.Popen([claude_bin, "-p", instruction], ...)

Currently instrumented call sites:
- ``backend/api.py`` ``_start_loop_run()`` — wraps every dashboard-triggered loop run.

Usage (CLI):
    python3 backend/claude_spawn_tracker.py status
    python3 backend/claude_spawn_tracker.py summary --json
    python3 backend/claude_spawn_tracker.py reset
    python3 backend/claude_spawn_tracker.py record <source> [--est-tokens N] [--est-cost-usd F]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root: `python backend/claude_spawn_tracker.py ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "spawns_per_hour_max": 50,
    "spend_per_hour_usd_max": 5.00,
    "spawns_24h_max": 200,
    "auto_reset_idle_seconds": 3600,
    "cost_per_spawn_usd_default": 0.05,
}

_EVENTS_KEY = "spawn/claude_events"
_TRIPPED_KEY = "spawn_breaker/tripped"
_META_KEY = "spawn_breaker/tripped_meta"
_BANNER_KEY = "dashboard/banner/spawn_breaker"

# Sources whose real cost is tracked in agent_runs / audit.jsonl rather than
# at spawn-time.  For these sources, est_tokens and est_cost_usd are stored as
# None instead of the config default (0.05) so the spawn tracker stays a
# reliable counter without injecting fake spend into the budget dashboard.
#
# The spawn-cap counter still increments normally — each tick costs 1 against
# the per-hour cap regardless of token accounting.
_UNMETERED_SOURCES: frozenset[str] = frozenset({
    "innovate_tick_internal",
})

_bb = Blackboard()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class SpawnBlocked(Exception):
    """Raised by ``record()`` when the spawn breaker is tripped."""


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Load spawn_breaker config from .autonomous-team/config.json."""
    config_path = Path(__file__).resolve().parent.parent / ".autonomous-team" / "config.json"
    try:
        raw = json.loads(config_path.read_text())
        cfg = raw.get("spawn_breaker") or {}
        merged = dict(_DEFAULT_CONFIG)
        merged.update({k: v for k, v in cfg.items() if k in _DEFAULT_CONFIG})
        return merged
    except Exception:  # noqa: BLE001
        return dict(_DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts_to_dt(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def _load_events() -> list[dict]:
    """Load event list from blackboard, returning [] on any error."""
    raw = _bb.read(_EVENTS_KEY)
    if isinstance(raw, list):
        return raw
    return []


def _trim_events(events: list[dict], window_hours: float = 24.0) -> list[dict]:
    """Remove events older than *window_hours* from now."""
    cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
    return [e for e in events if _ts_to_dt(e["ts"]).timestamp() >= cutoff]


def _window_events(events: list[dict], window_hours: float) -> list[dict]:
    """Return events within the last *window_hours*."""
    cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
    return [e for e in events if _ts_to_dt(e["ts"]).timestamp() >= cutoff]


def _post_team_log(message: str) -> None:
    """Post a warning to team-log via rotate-team-log.sh. Non-fatal."""
    import subprocess as _sp
    repo_root = Path(__file__).resolve().parent.parent
    try:
        _sp.run(
            ["bash", "scripts/rotate-team-log.sh", "comment", message],
            capture_output=True,
            timeout=10,
            cwd=str(repo_root),
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def record(source: str, est_tokens: int = 0, est_cost_usd: float | None = None) -> None:
    """Record a Claude Code subprocess spawn.

    Atomically increments rolling-window counters and checks thresholds.
    Raises ``SpawnBlocked`` if the breaker is currently tripped (after updating
    ``last_attempt_at`` so idle-auto-reset is postponed).

    Parameters
    ----------
    source:
        Caller identifier (e.g. ``"loop_run"``).
    est_tokens:
        Estimated token count for the spawn (informational; 0 if unknown).
    est_cost_usd:
        Estimated USD cost. If ``None`` and source is not in ``_UNMETERED_SOURCES``,
        uses ``cost_per_spawn_usd_default`` from config. For unmetered sources the
        value stays ``None`` — real cost is tracked in ``agent_runs``.
    """
    with _lock:
        cfg = _load_config()

        # Unmetered sources (e.g. innovate_tick_internal) store None for cost/tokens
        # so the spawn tracker stays a clean counter; agent_runs is their cost source.
        unmetered = source in _UNMETERED_SOURCES
        if unmetered:
            stored_tokens: int | None = None
            stored_cost: float | None = None
        else:
            stored_tokens = int(est_tokens)
            if est_cost_usd is None:
                stored_cost = float(cfg["cost_per_spawn_usd_default"])
            else:
                stored_cost = float(est_cost_usd)

        now_str = _now_iso()

        # Check auto-reset before doing anything else
        _maybe_auto_reset(cfg)

        # Update last_attempt_at in meta regardless of tripped state (keeps
        # idle-reset postponed while spawns keep coming)
        _update_last_attempt(now_str)

        # If already tripped, refuse
        if _bb.read(_TRIPPED_KEY):
            raise SpawnBlocked("Claude spawn breaker is tripped — call reset() or wait for auto-reset")

        # Append event
        events = _load_events()
        events.append({
            "ts": now_str,
            "source": source,
            "est_tokens": stored_tokens,
            "est_cost_usd": stored_cost,
        })
        # Trim to last 24h before persisting
        events = _trim_events(events, window_hours=24.0)
        _bb.write(_EVENTS_KEY, events, updated_by="claude_spawn_tracker")

        # Check thresholds — skip None est_cost_usd entries (unmetered sources)
        events_1h = _window_events(events, 1.0)
        events_24h = events  # already trimmed to 24h

        spawns_1h = len(events_1h)
        spend_1h = sum(e["est_cost_usd"] for e in events_1h if e["est_cost_usd"] is not None)
        spawns_24h = len(events_24h)

        def _trip(threshold_name: str, value: float | int) -> None:
            meta = {
                "tripped_at": now_str,
                "reason": f"{threshold_name} exceeded: {value}",
                "threshold_name": threshold_name,
                "value": value,
                "last_attempt_at": now_str,
            }
            _bb.write(_TRIPPED_KEY, True, updated_by="claude_spawn_tracker")
            _bb.write(_META_KEY, meta, updated_by="claude_spawn_tracker")
            _bb.write(_BANNER_KEY, {
                "level": "error",
                "message": f"Spawn breaker tripped: {threshold_name}={value}",
                "dismissable": False,
                "set_at": now_str,
            }, updated_by="claude_spawn_tracker")
            # One-time team-log warning (idempotent — _trip only called when previously false)
            _post_team_log(
                f"[{now_str[:16]}] spawn-breaker: TRIPPED — "
                f"{threshold_name}={value} (source={source})"
            )
            raise SpawnBlocked(
                f"Claude spawn breaker tripped: {threshold_name}={value}"
            )

        if spawns_1h > cfg["spawns_per_hour_max"]:
            _trip("spawns_per_hour_max", spawns_1h)
        if spend_1h > cfg["spend_per_hour_usd_max"]:
            _trip("spend_per_hour_usd_max", round(spend_1h, 4))
        if spawns_24h > cfg["spawns_24h_max"]:
            _trip("spawns_24h_max", spawns_24h)


def _update_last_attempt(now_str: str) -> None:
    """Update last_attempt_at in tripped_meta (non-fatal)."""
    meta = _bb.read(_META_KEY)
    if isinstance(meta, dict):
        meta["last_attempt_at"] = now_str
        _bb.write(_META_KEY, meta, updated_by="claude_spawn_tracker")


def _maybe_auto_reset(cfg: dict) -> None:
    """If tripped and idle for auto_reset_idle_seconds, reset silently."""
    if not _bb.read(_TRIPPED_KEY):
        return
    meta = _bb.read(_META_KEY)
    if not isinstance(meta, dict):
        return
    last_attempt = meta.get("last_attempt_at")
    if not last_attempt:
        return
    idle_secs = cfg["auto_reset_idle_seconds"]
    try:
        last_dt = _ts_to_dt(last_attempt)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed >= idle_secs:
            _do_reset()
    except Exception:  # noqa: BLE001
        pass


def _do_reset() -> None:
    """Internal reset — clears tripped state and banner. Does NOT reset event counters."""
    _bb.write(_TRIPPED_KEY, False, updated_by="claude_spawn_tracker/reset")
    _bb.delete(_META_KEY)
    _bb.delete(_BANNER_KEY)


def is_tripped() -> bool:
    """Return True if the spawn breaker is currently tripped."""
    with _lock:
        cfg = _load_config()
        _maybe_auto_reset(cfg)
        return bool(_bb.read(_TRIPPED_KEY))


def get_state() -> dict:
    """Return current state dict: counts, spend, tripped flag, trip metadata."""
    with _lock:
        cfg = _load_config()
        _maybe_auto_reset(cfg)

        events = _load_events()
        events_1h = _window_events(events, 1.0)
        events_24h = _trim_events(events, 24.0)

        per_source: dict[str, int] = {}
        for e in events_24h:
            src = e.get("source", "unknown")
            per_source[src] = per_source.get(src, 0) + 1

        return {
            "tripped": bool(_bb.read(_TRIPPED_KEY)),
            "spawns_1h": len(events_1h),
            "spawns_24h": len(events_24h),
            "spend_1h_usd": round(
                sum(e["est_cost_usd"] for e in events_1h if e.get("est_cost_usd") is not None), 4
            ),
            "spend_24h_usd": round(
                sum(e["est_cost_usd"] for e in events_24h if e.get("est_cost_usd") is not None), 4
            ),
            "per_source": per_source,
            "thresholds": {
                "spawns_per_hour_max": cfg["spawns_per_hour_max"],
                "spend_per_hour_usd_max": cfg["spend_per_hour_usd_max"],
                "spawns_24h_max": cfg["spawns_24h_max"],
            },
            "tripped_meta": _bb.read(_META_KEY) or None,
        }


def reset() -> None:
    """Manual reset — clears tripped state, meta, and dashboard banner."""
    with _lock:
        _do_reset()


def check_auto_reset() -> bool:
    """Check and apply auto-reset if idle. Returns True if reset was applied."""
    with _lock:
        before = bool(_bb.read(_TRIPPED_KEY))
        cfg = _load_config()
        _maybe_auto_reset(cfg)
        after = bool(_bb.read(_TRIPPED_KEY))
        return before and not after


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude_spawn_tracker",
        description="Global Claude Code spawn budget + circuit breaker.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Human-readable: counts, spend, tripped state, per-source breakdown")

    summary_p = sub.add_parser("summary", help="Structured summary (machine-readable JSON)")
    summary_p.add_argument("--json", action="store_true", dest="json_output",
                           help="Output JSON (required for machine consumers)")

    sub.add_parser("reset", help="Manual reset: clear tripped state and dashboard banner")

    record_p = sub.add_parser("record", help="Record a spawn (for testing / shell scripts)")
    record_p.add_argument("source", help="Caller identifier (e.g. loop_run)")
    record_p.add_argument("--est-tokens", type=int, default=0, dest="est_tokens")
    record_p.add_argument("--est-cost-usd", type=float, default=None, dest="est_cost_usd")

    return p


def _age_str(ts_str: str | None) -> str:
    if not ts_str:
        return ""
    try:
        dt = _ts_to_dt(ts_str)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        state = get_state()
        tripped = state["tripped"]
        meta = state.get("tripped_meta") or {}
        print(f"Spawn breaker: {'TRIPPED' if tripped else 'closed'}")
        if tripped and meta:
            print(f"  Reason:        {meta.get('reason', 'unknown')}")
            print(f"  Tripped at:    {meta.get('tripped_at', '?')} ({_age_str(meta.get('tripped_at'))})")
            print(f"  Last attempt:  {meta.get('last_attempt_at', '?')} ({_age_str(meta.get('last_attempt_at'))})")
        print(f"Spawns  1h:     {state['spawns_1h']}  / {state['thresholds']['spawns_per_hour_max']}")
        print(f"Spawns 24h:     {state['spawns_24h']}  / {state['thresholds']['spawns_24h_max']}")
        print(f"Spend   1h:     ${state['spend_1h_usd']:.4f}  / ${state['thresholds']['spend_per_hour_usd_max']:.2f}")
        print(f"Spend  24h:     ${state['spend_24h_usd']:.4f}")
        per_source = state.get("per_source") or {}
        if per_source:
            print("Per-source (24h):")
            for src, count in sorted(per_source.items(), key=lambda x: -x[1]):
                print(f"  {src:<30} {count}")
        return 0

    if args.command == "summary":
        state = get_state()
        if args.json_output:
            print(json.dumps(state, indent=2))
        else:
            print(json.dumps(state, indent=2))
        return 0

    if args.command == "reset":
        reset()
        print("Spawn breaker reset. Tripped state cleared.")
        return 0

    if args.command == "record":
        try:
            record(source=args.source, est_tokens=args.est_tokens, est_cost_usd=args.est_cost_usd)
            print(f"Recorded spawn from source={args.source!r}")
        except SpawnBlocked as exc:
            print(f"SpawnBlocked: {exc}", file=sys.stderr)
            return 1
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
