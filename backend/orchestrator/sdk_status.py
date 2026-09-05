"""backend/orchestrator/sdk_status.py — SDK orchestrator status CLI.

Reports, at a glance, the state of the dual-path SDK orchestrator:
dispatcher readiness, which backend would be selected right now, credit
state, billing regime, and routing counts from agent_run telemetry.

When the dispatcher is off (ROUTE_VIA_DISPATCHER != 1) or no SDK runs
exist yet, the report says so clearly rather than hiding that state.

No secrets are ever printed — only presence booleans for credentials.
Read-only: no spawns, no SDK calls, no network.

CLI usage::

    python3 backend/orchestrator/sdk_status.py
    python3 backend/orchestrator/sdk_status.py --json

Importable API::

    from backend.orchestrator.sdk_status import sdk_status
    report = sdk_status()       # returns a dict
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# sys.path bootstrap — lets this run directly as a script
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers — each section of the report is its own pure function so tests
# can verify sections in isolation.
# ---------------------------------------------------------------------------


def _readiness() -> dict[str, Any]:
    """Return the dispatcher readiness block.

    Reports the three env-var gates that control whether the SDK path
    is active:
      - ROUTE_VIA_DISPATCHER: "1" enables the dispatcher
      - SHADOW_MODE: "alternate" (default), "sdk", "cc", "both"
      - SDK_BACKEND: explicit backend override (if set)
    """
    route_via = os.environ.get("ROUTE_VIA_DISPATCHER", "")
    shadow_mode = os.environ.get("SHADOW_MODE", "alternate")
    sdk_backend = os.environ.get("SDK_BACKEND", "")

    dispatcher_live = route_via == "1"

    return {
        "dispatcher_live": dispatcher_live,
        "ROUTE_VIA_DISPATCHER": route_via or "(not set)",
        "SHADOW_MODE": shadow_mode,
        "SDK_BACKEND": sdk_backend or "(not set)",
    }


_CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")


def _backend_would_select(credentials_path: Optional[str] = None) -> dict[str, Any]:
    """Mirror _select_sdk_backend() logic without importing heavy runner deps.

    Returns the backend that WOULD be selected given current env, plus
    presence booleans for credentials (never their values).

    Selection precedence (mirrors dispatch._select_sdk_backend / detect_sdk_credential):
      1. SDK_BACKEND override
      2. CLAUDE_CODE_OAUTH_TOKEN present               → subscription
      3. ANTHROPIC_API_KEY present                     → apikey
      4. ~/.claude/.credentials.json exists (login)    → subscription
      5. None of the above                             → none (routes to CC)

    Both CLAUDE_CODE_OAUTH_TOKEN + ANTHROPIC_API_KEY present: subscription is
    preferred (same as selector).

    Parameters
    ----------
    credentials_path:
        Override the credentials file path (for testing). When None, uses
        the module-level _CREDENTIALS_FILE path (~/.claude/.credentials.json).
    """
    override = os.environ.get("SDK_BACKEND", "").strip().lower()
    has_oauth = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    has_apikey = bool(os.environ.get("ANTHROPIC_API_KEY"))
    creds_file = credentials_path if credentials_path is not None else _CREDENTIALS_FILE
    has_login = os.path.exists(creds_file)

    if override in ("subscription", "agent_sdk"):
        backend = "subscription"
        reason = f"SDK_BACKEND={override} override"
    elif override in ("apikey", "anthropic"):
        backend = "apikey"
        reason = f"SDK_BACKEND={override} override"
    elif override:
        # Unknown override — falls back to auto-detect in the real selector
        backend = _autodetect_backend(has_oauth, has_apikey, has_login)
        reason = f"SDK_BACKEND={override!r} unknown — auto-detect fallback"
    elif has_oauth and has_apikey:
        backend = "subscription"
        reason = "both creds present — subscription preferred (CLAUDE_CODE_OAUTH_TOKEN)"
    elif has_oauth:
        backend = "subscription"
        reason = "CLAUDE_CODE_OAUTH_TOKEN present"
    elif has_apikey:
        backend = "apikey"
        reason = "ANTHROPIC_API_KEY present"
    elif has_login:
        backend = "subscription"
        reason = "stored claude CLI login present (~/.claude/.credentials.json)"
    else:
        backend = "none"
        reason = "no SDK credential — routes to Claude Code path"

    return {
        "would_select": backend,
        "reason": reason,
        "CLAUDE_CODE_OAUTH_TOKEN": "present" if has_oauth else "absent",
        "ANTHROPIC_API_KEY": "present" if has_apikey else "absent",
        "claude_login": "present" if has_login else "absent",
    }


def _autodetect_backend(has_oauth: bool, has_apikey: bool, has_login: bool = False) -> str:
    """Return autodetect result given credential presence flags."""
    if has_oauth:
        return "subscription"
    if has_apikey:
        return "apikey"
    if has_login:
        return "subscription"
    return "none"


def _credit_state(credit_file: Optional[Path] = None) -> dict[str, Any]:
    """Return credit state from CreditTracker + billing regime.

    Uses credit_tracker.CreditTracker for remaining_usd / soft_cap_breached,
    and cost_verification.billing_regime_for_now() for the regime name.

    Falls back gracefully when the credit file is absent (fresh install).
    """
    try:
        from backend.orchestrator.credit_tracker import CreditTracker
        from backend.orchestrator.cost_verification import billing_regime_for_now

        tracker = CreditTracker(credit_file=credit_file) if credit_file else CreditTracker()
        remaining = tracker.remaining_usd()
        used = tracker.used_usd()
        soft_cap = tracker.soft_cap_breached()
        exhausted = tracker.is_exhausted()
        regime = billing_regime_for_now()

        return {
            "remaining_usd": remaining,
            "used_usd": used,
            "soft_cap_breached": soft_cap,
            "exhausted": exhausted,
            "billing_regime": regime,
            "regime_note": (
                "subscription-covered (no charge from $200 pool)"
                if regime == "subscription"
                else "draws from $200/mo dedicated credit pool (from 2026-06-15)"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "remaining_usd": None,
            "used_usd": None,
            "soft_cap_breached": None,
            "exhausted": None,
            "billing_regime": None,
            "regime_note": None,
            "error": str(exc),
        }


def _routing_counts(db_path: Optional[Path] = None) -> dict[str, Any]:
    """Return SDK vs CC routing counts from agent_run telemetry.

    Reads from the routed_via column when present (added in D#1331).
    For rows with NULL routed_via (pre-D#1331 rows written before the column
    existed), falls back to the used_usd > 0 proxy as an estimate of whether
    the SDK ran at all.

    Returns real per-route counts (sdk_runs, cc_runs) when routed_via is
    populated; falls back to the legacy estimate for old rows.

    DuckDB is queried for total run count and recent runs (last 30 days).
    If DuckDB is absent or agent_run is empty, we report 0 runs.
    """
    total_runs = 0
    recent_runs_30d = 0
    sdk_runs = 0
    cc_runs = 0
    null_route_runs = 0
    db_available = False
    sdk_runs_estimate = "unknown"
    note = ""

    try:
        state_dir = os.environ.get(
            "AUTONOMOUS_TEAM_STATE_DIR",
            str(Path.home() / ".autonomous-forever-state"),
        )
        path = db_path or Path(state_dir) / "stats.duckdb"

        if not path.exists():
            note = "stats.duckdb not found — no telemetry yet"
        else:
            try:
                import duckdb  # type: ignore[import]
                db_available = True
                conn = duckdb.connect(str(path), read_only=True)
                has_routed_via = False
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM agent_run"
                    ).fetchone()
                    total_runs = int(row[0]) if row else 0

                    row30 = conn.execute(
                        """
                        SELECT COUNT(*) FROM agent_run
                        WHERE start_ts >= NOW() - INTERVAL '30 days'
                        """
                    ).fetchone()
                    recent_runs_30d = int(row30[0]) if row30 else 0

                    # Read real routed_via counts when the column exists.
                    # The column was added in D#1331; older tables may not have it.
                    try:
                        col_names = {
                            r[0]
                            for r in conn.execute(
                                "SELECT column_name FROM information_schema.columns"
                                " WHERE table_name='agent_run'"
                            ).fetchall()
                        }
                        has_routed_via = "routed_via" in col_names
                        if has_routed_via:
                            route_rows = conn.execute(
                                """
                                SELECT routed_via, COUNT(*) AS n
                                FROM agent_run
                                GROUP BY routed_via
                                """
                            ).fetchall()
                            for rv, n in route_rows:
                                if rv == "sdk":
                                    sdk_runs = int(n)
                                elif rv == "cc":
                                    cc_runs = int(n)
                                elif rv is None:
                                    null_route_runs = int(n)
                    except Exception:  # noqa: BLE001
                        pass  # routed_via column query failed — fall through to proxy

                finally:
                    conn.close()
                note = (
                    "routed_via column present — sdk/cc counts are real per-run data; "
                    "null_route_runs are pre-D#1331 rows (no route recorded)"
                    if has_routed_via
                    else "routed_via column absent — falling back to used_usd proxy"
                )
            except ImportError:
                note = "duckdb not installed — cannot read agent_run"
            except Exception as exc:  # noqa: BLE001
                note = f"agent_run query failed: {exc}"

        # Estimate for old/NULL rows: used_usd > 0 implies SDK ran at least once.
        # When routed_via is populated, sdk_runs is authoritative; the estimate
        # supplements it for pre-D#1331 rows that have NULL routed_via.
        try:
            from backend.orchestrator.credit_tracker import CreditTracker
            tracker = CreditTracker()
            used = tracker.used_usd()
            if sdk_runs > 0:
                sdk_runs_estimate = f"{sdk_runs} SDK run(s) (routed_via column)"
            elif used > 0:
                sdk_runs_estimate = f"at least 1 (credit_tracker shows ${used:.4f} consumed; proxy for pre-D#1331 rows)"
            else:
                sdk_runs_estimate = "0 SDK runs (no credit consumed; dispatcher likely off)"
        except Exception:  # noqa: BLE001
            if sdk_runs > 0:
                sdk_runs_estimate = f"{sdk_runs} SDK run(s) (routed_via column)"
            else:
                sdk_runs_estimate = "unknown (credit_tracker unavailable)"

    except Exception as exc:  # noqa: BLE001
        note = f"routing_counts error: {exc}"

    return {
        "total_runs_all_time": total_runs,
        "total_runs_last_30d": recent_runs_30d,
        "sdk_runs": sdk_runs,
        "cc_runs": cc_runs,
        "null_route_runs": null_route_runs,
        "sdk_runs_estimate": sdk_runs_estimate,
        "db_available": db_available,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Main report function (importable)
# ---------------------------------------------------------------------------


def sdk_status(
    credit_file: Optional[Path] = None,
    db_path: Optional[Path] = None,
    credentials_path: Optional[str] = None,
) -> dict[str, Any]:
    """Return a full SDK orchestrator status report dict.

    Parameters
    ----------
    credit_file:
        Override path for sdk_credit.json (useful in tests).
    db_path:
        Override path for stats.duckdb (useful in tests).
    credentials_path:
        Override the credentials file path for backend_selection (useful in tests).
        When None, uses the module-level _CREDENTIALS_FILE path.

    Returns
    -------
    dict with sections: readiness, backend_selection, credit, routing_counts,
    generated_at.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "readiness": _readiness(),
        "backend_selection": _backend_would_select(credentials_path=credentials_path),
        "credit": _credit_state(credit_file=credit_file),
        "routing_counts": _routing_counts(db_path=db_path),
    }


# ---------------------------------------------------------------------------
# Human-readable printer
# ---------------------------------------------------------------------------


def _print_human(report: dict[str, Any]) -> None:
    """Print a human-readable SDK status report."""
    print("=" * 60)
    print("SDK Orchestrator Status")
    print(f"Generated: {report['generated_at']}")
    print("=" * 60)

    # Readiness
    r = report["readiness"]
    live = r["dispatcher_live"]
    status_str = "LIVE" if live else "OFF"
    print(f"\nDispatcher:           {status_str}")
    print(f"  ROUTE_VIA_DISPATCHER: {r['ROUTE_VIA_DISPATCHER']}")
    print(f"  SHADOW_MODE:          {r['SHADOW_MODE']}")
    print(f"  SDK_BACKEND:          {r['SDK_BACKEND']}")

    # Backend selection
    bs = report["backend_selection"]
    print(f"\nBackend would select: {bs['would_select']}")
    print(f"  Reason: {bs['reason']}")
    print(f"  CLAUDE_CODE_OAUTH_TOKEN: {bs['CLAUDE_CODE_OAUTH_TOKEN']}")
    print(f"  ANTHROPIC_API_KEY:       {bs['ANTHROPIC_API_KEY']}")
    print(f"  claude_login:            {bs.get('claude_login', 'absent')}")

    # Credit
    c = report["credit"]
    print("\nCredit state:")
    if c.get("error"):
        print(f"  Error reading credit: {c['error']}")
    else:
        rem = c["remaining_usd"]
        used = c["used_usd"]
        print(f"  Remaining:   ${rem:.2f}" if rem is not None else "  Remaining:   n/a")
        print(f"  Used:        ${used:.4f}" if used is not None else "  Used:        n/a")
        print(f"  Soft cap:    {'breached' if c['soft_cap_breached'] else 'ok'}")
        print(f"  Exhausted:   {c['exhausted']}")
        print(f"  Regime:      {c['billing_regime']}")
        if c.get("regime_note"):
            print(f"  Note:        {c['regime_note']}")

    # Routing counts
    rc = report["routing_counts"]
    print("\nRouting counts:")
    print(f"  All-time runs:      {rc['total_runs_all_time']}")
    print(f"  Last 30d runs:      {rc['total_runs_last_30d']}")
    print(f"  SDK runs (real):    {rc.get('sdk_runs', 'n/a')}")
    print(f"  CC runs (real):     {rc.get('cc_runs', 'n/a')}")
    print(f"  Pre-D#1331 rows:    {rc.get('null_route_runs', 'n/a')}")
    print(f"  SDK estimate:       {rc['sdk_runs_estimate']}")
    if rc.get("note"):
        print(f"  Note: {rc['note']}")

    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SDK orchestrator status — dispatcher readiness, backend selection, credit, routing",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of human-readable summary",
    )
    args = parser.parse_args(argv)

    report = sdk_status()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
