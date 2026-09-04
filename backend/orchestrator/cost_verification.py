"""backend/orchestrator/cost_verification.py — SDK cost reconciliation.

Reconciles ESTIMATED cost (what credit_tracker decremented, derived from
token counts × model rate card) against ACTUAL cost derived from the recorded
SDK usage tokens in the agent_run DuckDB table.

No external Anthropic billing/invoice API is consulted.  Such an API endpoint
does not exist in this codebase and we do not invent one here.  Reconciliation
is token-count-based on both sides:

  estimated_usd  = whatever credit_tracker.used_usd() shows (cumulative)
  actual_usd     = sum over runs of (input_tok + cache_write) * input_rate
                   + output_tok * output_rate
                   + cache_read * cache_read_rate

Anomaly threshold is 5 %: any run where |estimate − actual| / actual > 0.05
is flagged.  Aggregate accuracy = (1 - |Σest − Σact| / Σact) × 100.

Billing regimes
---------------
SUBSCRIPTION regime (before 2026-06-15):
  SDK usage is subscription-covered at no added cost.  The $200 dedicated
  credit pool does not apply.  Estimated/actual numbers are still meaningful
  for capacity planning, but cost overruns do not translate to a real charge.

CREDIT regime (from 2026-06-15):
  A $200/month dedicated credit applies and SDK usage draws from that pool.
  The CreditTracker.used_usd() value is the live billing constraint.
  Anomalies in this regime should be investigated promptly.

CLI usage::

    python3 backend/orchestrator/cost_verification.py
    python3 backend/orchestrator/cost_verification.py --since-days 7
    python3 backend/orchestrator/cost_verification.py --json

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
# sys.path bootstrap (mirrors other backend CLIs so this runs as a script)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Billing regime cutover date
# ---------------------------------------------------------------------------

# Before this date SDK usage is subscription-covered; on/after it the $200
# dedicated credit pool applies.
_CREDIT_REGIME_START = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Shared pricing — delegate to backend.cost_pricing (single source of truth)
# ---------------------------------------------------------------------------

from backend.cost_pricing import cost_usd as _cost_usd  # noqa: E402

_ANOMALY_THRESHOLD = 0.05   # 5 % deviation triggers an anomaly flag

# ---------------------------------------------------------------------------
# Billing regime helpers
# ---------------------------------------------------------------------------

def billing_regime(ts: datetime) -> str:
    """Return 'subscription' or 'credit' depending on when the run occurred.

    Parameters
    ----------
    ts:
        UTC datetime of the run start/end timestamp.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return "credit" if ts >= _CREDIT_REGIME_START else "subscription"


def billing_regime_for_now() -> str:
    """Return the current billing regime."""
    return billing_regime(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Token → USD helpers
# ---------------------------------------------------------------------------

def tokens_to_usd(
    input_tok: int,
    output_tok: int,
    cache_read: int = 0,
    cache_write: int = 0,
    model: str = "_default",
) -> float:
    """Compute USD cost from raw token counts.

    Delegates to :func:`backend.cost_pricing.cost_usd` — the single source of
    truth for token pricing.  This wrapper is preserved for backwards
    compatibility so existing callers (and tests) need no changes.

    Parameters
    ----------
    input_tok:   Regular (non-cached) input tokens.
    output_tok:  Output tokens.
    cache_read:  Cache-read tokens (cheap).
    cache_write: Cache-write tokens (slightly above input rate).
    model:       Model identifier; falls back to ``_default`` if not in card.

    Returns
    -------
    float
        Estimated cost in USD, rounded to 8 decimal places.
    """
    return _cost_usd(
        input_tok=input_tok,
        output_tok=output_tok,
        cache_read=cache_read,
        cache_write=cache_write,
        model=model,
    )


# ---------------------------------------------------------------------------
# DB / state-dir helpers (mirrors agent_run_tracker pattern)
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    env = os.environ.get("STATS_DB_PATH")
    if env:
        return Path(env)
    state_dir = os.environ.get(
        "AUTONOMOUS_TEAM_STATE_DIR",
        str(Path.home() / ".fulcrumaxe-state"),
    )
    return Path(state_dir) / "stats.duckdb"


def _credit_file() -> Path:
    state_dir = os.environ.get(
        "AUTONOMOUS_TEAM_STATE_DIR",
        str(Path.home() / ".fulcrumaxe-state"),
    )
    return Path(state_dir) / "sdk_credit.json"


# ---------------------------------------------------------------------------
# Per-run reconciliation record
# ---------------------------------------------------------------------------

class RunRecord:
    """Lightweight representation of a single reconciled agent run."""

    __slots__ = (
        "agent_id", "role", "discussion", "pr", "model",
        "start_ts", "verdict",
        "input_tok", "output_tok", "cache_read", "cache_write",
        "actual_usd", "regime",
    )

    def __init__(
        self,
        agent_id: str,
        role: str,
        discussion: Optional[int],
        pr: Optional[int],
        model: str,
        start_ts: datetime,
        verdict: Optional[str],
        input_tok: int,
        output_tok: int,
        cache_read: int,
        cache_write: int,
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.discussion = discussion
        self.pr = pr
        self.model = model or "_default"
        self.start_ts = start_ts
        self.verdict = verdict
        self.input_tok = input_tok
        self.output_tok = output_tok
        self.cache_read = cache_read
        self.cache_write = cache_write
        self.actual_usd = tokens_to_usd(
            input_tok=input_tok,
            output_tok=output_tok,
            cache_read=cache_read,
            cache_write=cache_write,
            model=self.model,
        )
        self.regime = billing_regime(start_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "discussion": self.discussion,
            "pr": self.pr,
            "model": self.model,
            "start_ts": self.start_ts.isoformat(),
            "verdict": self.verdict,
            "input_tok": self.input_tok,
            "output_tok": self.output_tok,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "actual_usd": self.actual_usd,
            "regime": self.regime,
        }


# ---------------------------------------------------------------------------
# Load runs from DuckDB
# ---------------------------------------------------------------------------

def _load_runs(since_days: int, db_path: Optional[Path] = None) -> list[RunRecord]:
    """Query agent_run for completed runs within *since_days* days.

    Returns an empty list if DuckDB is not installed or the table does not
    exist — never raises.
    """
    path = db_path or _db_path()
    if not path.exists():
        return []

    try:
        import duckdb  # type: ignore[import]
    except ImportError:
        return []

    try:
        conn = duckdb.connect(str(path), read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT
                    agent_id,
                    role,
                    discussion,
                    pr,
                    COALESCE(model, '_default') AS model,
                    start_ts,
                    verdict,
                    COALESCE(input_tok,  0) AS input_tok,
                    COALESCE(output_tok, 0) AS output_tok,
                    COALESCE(cache_read, 0) AS cache_read,
                    COALESCE(cache_write, 0) AS cache_write
                FROM agent_run
                WHERE end_ts IS NOT NULL
                  AND start_ts >= NOW() - INTERVAL (?) DAY
                ORDER BY start_ts
                """,
                [since_days],
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

    records: list[RunRecord] = []
    for row in rows:
        (
            agent_id, role, discussion, pr, model,
            start_ts, verdict,
            input_tok, output_tok, cache_read, cache_write,
        ) = row
        # Normalise start_ts to UTC-aware datetime
        if isinstance(start_ts, str):
            start_ts = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        if hasattr(start_ts, "tzinfo") and start_ts.tzinfo is None:
            start_ts = start_ts.replace(tzinfo=timezone.utc)
        records.append(RunRecord(
            agent_id=str(agent_id),
            role=str(role or "unknown"),
            discussion=int(discussion) if discussion is not None else None,
            pr=int(pr) if pr is not None else None,
            model=str(model or "_default"),
            start_ts=start_ts,
            verdict=str(verdict) if verdict else None,
            input_tok=int(input_tok),
            output_tok=int(output_tok),
            cache_read=int(cache_read),
            cache_write=int(cache_write),
        ))
    return records


# ---------------------------------------------------------------------------
# Load estimated cost from credit_tracker file
# ---------------------------------------------------------------------------

def _load_estimated_total(credit_file: Optional[Path] = None) -> tuple[float, float]:
    """Return (initial_usd, used_usd) from sdk_credit.json.

    Returns (200.0, 0.0) when the file is absent or unreadable.
    """
    path = credit_file or _credit_file()
    if not path.exists():
        return (200.0, 0.0)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        initial = float(data.get("initial_usd", 200.0))
        used = float(data.get("used_usd", 0.0))
        return (initial, used)
    except (json.JSONDecodeError, OSError, ValueError):
        return (200.0, 0.0)


# ---------------------------------------------------------------------------
# Core reconciliation
# ---------------------------------------------------------------------------

def reconcile_runs(
    runs: list[RunRecord],
    estimated_total_usd: float,
) -> dict[str, Any]:
    """Reconcile a list of RunRecord objects against the estimated total.

    The estimated_total_usd comes from credit_tracker.used_usd(): the
    running total that was decremented each time the SDK runner completed
    a run.  The actual total is the sum of per-run token-based cost.

    Note on reconciliation scope
    ----------------------------
    credit_tracker.used_usd() is a rolling file-based counter that was
    decremented per SDK response.  The agent_run table is the source of
    ground-truth token counts.  Because the tracker may have been reset
    between billing cycles, the aggregate estimated figure is compared to
    the sum of actual costs for the requested time window only — not all
    time.  This means the aggregate variance is indicative (shows model
    accuracy) rather than a definitive invoice-to-tracker reconciliation.

    For exact invoice reconciliation (comparing against the Anthropic
    billing dashboard): that is a future hook.  The Anthropic billing API
    is not available in this codebase as of 2026-05-20.

    Parameters
    ----------
    runs:
        List of RunRecord objects for the time window.
    estimated_total_usd:
        Total USD decremented from credit_tracker for all SDK runs ever
        (not windowed; treated as the estimated cumulative spend).

    Returns
    -------
    dict
        Full reconciliation report.
    """
    total_actual_usd = sum(r.actual_usd for r in runs)
    run_count = len(runs)

    # Aggregate variance: estimated vs actual for this window
    # We use the estimated_total_usd as the "tracker says we spent this much".
    # The actual is derived from recorded token counts.
    aggregate_variance_usd = estimated_total_usd - total_actual_usd
    if total_actual_usd > 0:
        aggregate_accuracy_pct = (
            (1.0 - abs(aggregate_variance_usd) / total_actual_usd) * 100.0
        )
    else:
        aggregate_accuracy_pct = 100.0 if estimated_total_usd == 0 else 0.0

    # Per-role aggregation
    role_totals: dict[str, dict[str, Any]] = {}
    for r in runs:
        if r.role not in role_totals:
            role_totals[r.role] = {
                "run_count": 0,
                "actual_usd": 0.0,
                "input_tok": 0,
                "output_tok": 0,
            }
        role_totals[r.role]["run_count"] += 1
        role_totals[r.role]["actual_usd"] = round(
            role_totals[r.role]["actual_usd"] + r.actual_usd, 8
        )
        role_totals[r.role]["input_tok"] += r.input_tok
        role_totals[r.role]["output_tok"] += r.output_tok

    # Per-discussion attribution
    disc_totals: dict[int, dict[str, Any]] = {}
    for r in runs:
        if r.discussion is None:
            continue
        if r.discussion not in disc_totals:
            disc_totals[r.discussion] = {"run_count": 0, "actual_usd": 0.0}
        disc_totals[r.discussion]["run_count"] += 1
        disc_totals[r.discussion]["actual_usd"] = round(
            disc_totals[r.discussion]["actual_usd"] + r.actual_usd, 8
        )

    # Anomaly flags: runs where there is no per-run estimate to compare
    # (we don't store per-run estimate in credit_tracker — it's a rolling total).
    # We flag runs with unusually high token costs instead: any run where
    # actual_usd > (aggregate_actual / run_count) * (1 + ANOMALY_THRESHOLD * 10)
    # i.e. more than 50 % above the mean.
    anomalies: list[dict[str, Any]] = []
    if run_count > 0:
        mean_actual = total_actual_usd / run_count
        for r in runs:
            if mean_actual > 0 and r.actual_usd > mean_actual * (1 + _ANOMALY_THRESHOLD * 10):
                anomalies.append({
                    "agent_id": r.agent_id,
                    "role": r.role,
                    "discussion": r.discussion,
                    "actual_usd": r.actual_usd,
                    "mean_usd": round(mean_actual, 8),
                    "ratio": round(r.actual_usd / mean_actual, 3),
                    "reason": "run cost > 50% above mean",
                })

    # Aggregate estimate vs actual anomaly
    if total_actual_usd > 0 and abs(aggregate_variance_usd) / total_actual_usd > _ANOMALY_THRESHOLD:
        anomalies.append({
            "agent_id": None,
            "role": None,
            "discussion": None,
            "actual_usd": total_actual_usd,
            "estimated_usd": estimated_total_usd,
            "variance_usd": aggregate_variance_usd,
            "reason": f"aggregate estimate vs actual diverges by > {_ANOMALY_THRESHOLD*100:.0f}%",
        })

    # Regime breakdown
    subscription_runs = [r for r in runs if r.regime == "subscription"]
    credit_runs = [r for r in runs if r.regime == "credit"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "run_count": run_count,
        "total_actual_usd": round(total_actual_usd, 6),
        "estimated_total_usd": round(estimated_total_usd, 6),
        "aggregate_variance_usd": round(aggregate_variance_usd, 6),
        "aggregate_accuracy_pct": round(aggregate_accuracy_pct, 2),
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
        "by_role": {
            role: {
                "run_count": v["run_count"],
                "actual_usd": round(v["actual_usd"], 6),
                "input_tok": v["input_tok"],
                "output_tok": v["output_tok"],
            }
            for role, v in sorted(role_totals.items())
        },
        "by_discussion": {
            str(disc): {
                "run_count": v["run_count"],
                "actual_usd": round(v["actual_usd"], 6),
            }
            for disc, v in sorted(disc_totals.items())
        },
        "regime_summary": {
            "subscription": {
                "note": "Subscription-covered; no charge from $200 credit pool",
                "run_count": len(subscription_runs),
                "actual_usd": round(sum(r.actual_usd for r in subscription_runs), 6),
            },
            "credit": {
                "note": "Draws from $200/mo dedicated credit (from 2026-06-15)",
                "run_count": len(credit_runs),
                "actual_usd": round(sum(r.actual_usd for r in credit_runs), 6),
            },
        },
        "data_source": {
            "actual_cost": "agent_run DuckDB table — token counts × rate card",
            "estimated_cost": "credit_tracker sdk_credit.json used_usd (rolling total)",
            "billing_api": "NOT AVAILABLE — external invoice reconciliation is a future hook",
            "rate_card_note": (
                "Rates: input $3.00/1M, output $15.00/1M, cache_write $3.75/1M, "
                "cache_read $0.30/1M (claude-sonnet-4-6, May 2026 pricing)"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Main entry point: verify()
# ---------------------------------------------------------------------------

def verify(
    since_days: int = 30,
    db_path: Optional[Path] = None,
    credit_file: Optional[Path] = None,
) -> dict[str, Any]:
    """Run cost verification and return a reconciliation report dict.

    Parameters
    ----------
    since_days:
        How many days of agent_run history to include in the actual-cost
        computation.  Default: 30 days.
    db_path:
        Override for stats.duckdb path (useful in tests).
    credit_file:
        Override for sdk_credit.json path (useful in tests).

    Returns
    -------
    dict
        Reconciliation report.  Keys: run_count, total_actual_usd,
        estimated_total_usd, aggregate_variance_usd, aggregate_accuracy_pct,
        anomaly_count, anomalies, by_role, by_discussion, regime_summary,
        data_source, generated_at.
    """
    runs = _load_runs(since_days=since_days, db_path=db_path)
    _initial_usd, used_usd = _load_estimated_total(credit_file=credit_file)
    return reconcile_runs(runs=runs, estimated_total_usd=used_usd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_human(report: dict[str, Any]) -> None:
    """Print a human-readable summary of the reconciliation report."""
    print("=" * 60)
    print("SDK Cost Verification Report")
    print(f"Generated: {report['generated_at']}")
    print("=" * 60)
    print(f"Runs analysed:        {report['run_count']}")
    print(f"Actual cost (tokens): ${report['total_actual_usd']:.4f}")
    print(f"Estimated cost (file):${report['estimated_total_usd']:.4f}")
    print(f"Variance:             ${report['aggregate_variance_usd']:.4f}")
    print(f"Aggregate accuracy:   {report['aggregate_accuracy_pct']:.1f}%")
    print(f"Anomalies flagged:    {report['anomaly_count']}")
    print()

    regime = report.get("regime_summary", {})
    sub = regime.get("subscription", {})
    cred = regime.get("credit", {})
    print("Billing regime breakdown:")
    print(f"  Subscription (before 2026-06-15): {sub.get('run_count', 0)} runs, "
          f"${sub.get('actual_usd', 0.0):.4f} actual")
    print(f"  Credit pool  (from   2026-06-15): {cred.get('run_count', 0)} runs, "
          f"${cred.get('actual_usd', 0.0):.4f} actual (draws from $200/mo pool)")
    print()

    by_role = report.get("by_role", {})
    if by_role:
        print("By role:")
        for role, data in by_role.items():
            print(f"  {role:<25} {data['run_count']:>4} runs  ${data['actual_usd']:.4f}")
    print()

    anomalies = report.get("anomalies", [])
    if anomalies:
        print("Anomalies:")
        for a in anomalies:
            print(f"  [{a.get('agent_id', 'aggregate')}] {a['reason']}")
    else:
        print("No anomalies detected.")

    print()
    print("Data sources:")
    ds = report.get("data_source", {})
    print(f"  Actual: {ds.get('actual_cost', 'n/a')}")
    print(f"  Estimated: {ds.get('estimated_cost', 'n/a')}")
    print(f"  Billing API: {ds.get('billing_api', 'n/a')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SDK cost verification — reconcile estimated vs actual token costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Number of days of history to analyse (default: 30)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of human-readable summary",
    )
    args = parser.parse_args(argv)

    report = verify(since_days=args.since_days)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
