"""
Unified CLI entry point for all fulcrumaxe backend operations.

Usage:
    python backend/cli.py <command> [args...]

Commands:
    status      Combined view: budget + queue + loop health + KPI summary
    budget      Token budget management (delegates to backend/budget.py)
    control     Control plane operations (delegates to backend/control_plane.py)
    kpi         KPI computation and display (delegates to backend/kpi_engine.py)
    health      Loop health monitoring (delegates to backend/health_monitor.py)
    registry    Discussion registry operations (delegates to backend/registry.py)
    agents      Agent card operations (delegates to backend/agent_cards.py)
    blackboard  Blackboard read/write operations (delegates to backend/blackboard.py)
    serve       Start the API server (delegates to backend/api.py)

Example:
    python backend/cli.py status
    python backend/cli.py budget status
    python backend/cli.py control gates
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is importable when running as a script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Module-level imports for status command — placed here so tests can patch them
# via `patch("backend.cli.BudgetTracker")` etc.
from backend.budget import BudgetTracker  # noqa: E402
from backend.health_monitor import check_loop_health  # noqa: E402
from backend.registry import DiscussionRegistry  # noqa: E402
from backend.kpi_engine import KPI_OUT  # noqa: E402


# ---------------------------------------------------------------------------
# status subcommand — calls library APIs directly, formats human output
# ---------------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    errors: list[str] = []

    # Budget
    budget: dict = {}
    try:
        bt = BudgetTracker()
        budget = bt.get_status()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"budget: {exc}")

    # Queue (registry stats)
    #
    # D#2310: this used to re-derive SPEC_READY by scanning reg.load()'s raw
    # rows with no closed_at filter, so it reported ~46 (all-time) instead of
    # the ~6 actually open. queue_summary() is the one open-filtered
    # implementation (backend/registry.py) — read the count off it instead
    # of re-deriving a second, unfiltered one here.
    queue: dict = {}
    queue_spec_ready = 0
    try:
        reg = DiscussionRegistry()
        queue = reg.stats()
        queue_spec_ready = reg.queue_summary()["buckets"].get("SPEC_READY", 0)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"registry: {exc}")

    # Loop health
    health: dict = {}
    try:
        health = check_loop_health()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"health: {exc}")

    # KPI
    kpi: dict = {}
    try:
        if KPI_OUT.exists():
            kpi = json.loads(KPI_OUT.read_text())
        else:
            from backend.kpi_engine import compute_all
            kpi = compute_all()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"kpi: {exc}")

    combined = {
        "budget": budget,
        "queue": {**queue, "spec_ready": queue_spec_ready},
        "health": health,
        "kpi": kpi,
    }

    if args.json:
        print(json.dumps(combined, indent=2))
        return 0

    # Human-readable output
    # Budget line
    ceiling = budget.get("ceiling", 0)
    spent = budget.get("spent", 0)
    pct = (spent * 100 // max(ceiling, 1)) if ceiling else 0
    if budget:
        print(f"Budget:    {spent:,} / {ceiling:,} tokens ({pct}%)")
    else:
        print("Budget:    unavailable")

    # Queue line
    if queue:
        total = queue.get("total", 0)
        done = queue.get("done", 0)
        in_prog = queue.get("in_progress", 0)
        spec_ready = queue_spec_ready
        print(f"Queue:     {total} total, {done} done, {in_prog} in progress, {spec_ready} spec ready")
    else:
        print("Queue:     unavailable")

    # Loop health line
    if health:
        healthy = health.get("healthy", False)
        age = health.get("age_minutes")
        status_str = "healthy" if healthy else "stale"
        if age is not None:
            age_str = f"{age:.1f} min ago"
            print(f"Loop:      {status_str} (last run {age_str})")
        else:
            reason = health.get("reason", "unknown")
            print(f"Loop:      {status_str} ({reason})")
    else:
        print("Loop:      unavailable")

    # KPI / velocity line
    if kpi:
        vel = kpi.get("velocity", {})
        ct = kpi.get("pr_cycle_time", {})
        per_day = vel.get("all_time_per_day", 0.0)
        avg_h = ct.get("mean_hours")
        if avg_h is not None:
            print(f"Velocity:  {per_day} tasks/day, {avg_h:.1f}h avg cycle time")
        else:
            print(f"Velocity:  {per_day} tasks/day, no cycle time data")
    else:
        print("Velocity:  unavailable")

    if errors:
        print("\nWarnings:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# Delegation helpers
# ---------------------------------------------------------------------------


def _delegate_budget(argv: list[str]) -> int:
    from backend.budget import main as budget_main

    result = budget_main(argv)
    return result if isinstance(result, int) else 0


def _delegate_control(argv: list[str]) -> int:
    from backend.control_plane import main as control_main

    result = control_main(argv)
    return result if isinstance(result, int) else 0


def _delegate_kpi(argv: list[str]) -> int:
    """kpi_engine.main() uses sys.argv directly — patch it temporarily."""
    import importlib

    # kpi_engine.main() reads sys.argv[1] directly
    old_argv = sys.argv[:]
    sys.argv = ["kpi_engine"] + argv
    try:
        from backend import kpi_engine
        kpi_engine.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    finally:
        sys.argv = old_argv
    return 0


def _delegate_health(argv: list[str]) -> int:
    from backend.health_monitor import main as health_main

    try:
        health_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    return 0


def _delegate_registry(argv: list[str]) -> int:
    from backend.registry import main as registry_main

    result = registry_main(argv)
    return result if isinstance(result, int) else 0


def _delegate_agents(argv: list[str]) -> int:
    from backend.agent_cards import main as agents_main

    result = agents_main(argv)
    return result if isinstance(result, int) else 0


def _delegate_blackboard(argv: list[str]) -> int:
    from backend.blackboard import main as blackboard_main

    result = blackboard_main(argv)
    return result if isinstance(result, int) else 0


def _delegate_serve(argv: list[str]) -> int:
    from backend.api import main as api_main

    try:
        api_main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    return 0


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="af",
        description="Unified CLI for fulcrumaxe backend operations.",
    )
    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = True

    # status
    status_p = sub.add_parser("status", help="Combined status: budget, queue, loop health, KPI")
    status_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # budget
    budget_p = sub.add_parser("budget", help="Token budget management")
    budget_p.add_argument("args", nargs=argparse.REMAINDER, help="Subcommand args (init|check|spend|status|reset)")

    # control
    control_p = sub.add_parser("control", help="Control plane operations")
    control_p.add_argument("args", nargs=argparse.REMAINDER, help="Subcommand args (show|get|set|gates|settings|audit)")

    # kpi
    kpi_p = sub.add_parser("kpi", help="KPI computation and display")
    kpi_p.add_argument("args", nargs=argparse.REMAINDER, help="Subcommand args (compute|show)")

    # health
    health_p = sub.add_parser("health", help="Loop health monitoring")
    health_p.add_argument("args", nargs=argparse.REMAINDER, help="Subcommand args (check|alert)")

    # registry
    registry_p = sub.add_parser("registry", help="Discussion registry operations")
    registry_p.add_argument("args", nargs=argparse.REMAINDER, help="Subcommand args (sync|show|stats)")

    # agents
    agents_p = sub.add_parser("agents", help="Agent card operations")
    agents_p.add_argument("args", nargs=argparse.REMAINDER, help="Subcommand args (list|show <role>)")

    # blackboard
    blackboard_p = sub.add_parser("blackboard", help="Blackboard read/write operations")
    blackboard_p.add_argument("args", nargs=argparse.REMAINDER, help="Subcommand args (read|write|list|cas|delete)")

    # serve
    serve_p = sub.add_parser("serve", help="Start the API server")
    serve_p.add_argument("args", nargs=argparse.REMAINDER, help="Server args (--port N --host H)")

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_DISPATCH: dict[str, str] = {
    "budget": "_delegate_budget",
    "control": "_delegate_control",
    "kpi": "_delegate_kpi",
    "health": "_delegate_health",
    "registry": "_delegate_registry",
    "agents": "_delegate_agents",
    "blackboard": "_delegate_blackboard",
    "serve": "_delegate_serve",
}


def main(argv: list[str] | None = None) -> int:
    import backend.cli as _self

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        return _cmd_status(args)

    handler_attr = _DISPATCH.get(args.command)
    if handler_attr is None:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1

    # Look up via module so monkeypatching in tests works correctly
    handler = getattr(_self, handler_attr)
    delegate_argv = getattr(args, "args", [])
    return handler(delegate_argv)


if __name__ == "__main__":
    sys.exit(main())
