"""backend/stats/sdk_vs_cc.py — Per-role SDK vs CC comparison from agent_run data.

Queries agent_run grouped by (role, routed_via) and computes:
  - run_count
  - median input_tok, median output_tok
  - pass_rate (verdict IN ('done','pass') / total)
  - cost_per_success_usd — average USD cost for runs that ended with a pass
    verdict, computed from token counts via backend.cost_pricing.cost_usd

Read-only DuckDB access. Handles no-data and absent routed_via column gracefully.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.cost_pricing import cost_usd as _cost_usd

logger = logging.getLogger(__name__)

# Verdicts that count as a "pass"
_PASS_VERDICTS = frozenset(["done", "pass"])


def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


def sdk_vs_cc_by_role(db_path: Path | None = None) -> dict[str, Any]:
    """Return per-role SDK vs CC comparison.

    Returns
    -------
    dict with:
      "rows": list of per-(role, route) dicts
      "has_routed_via": bool — False when column is absent (all rows show route=null)
      "generated_at": ISO-8601 string
      "error": str or None

    Each row dict:
      role, route, run_count, median_input_tok, median_output_tok, pass_rate
    """
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    path = db_path or _db_path()
    if not path.exists():
        return {
            "rows": [],
            "has_routed_via": False,
            "generated_at": generated_at,
            "error": None,
        }

    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return {
            "rows": [],
            "has_routed_via": False,
            "generated_at": generated_at,
            "error": "duckdb not installed",
        }

    try:
        conn = duckdb.connect(str(path), read_only=True)
    except Exception as exc:  # noqa: BLE001
        return {
            "rows": [],
            "has_routed_via": False,
            "generated_at": generated_at,
            "error": f"cannot open stats.duckdb: {exc}",
        }

    try:
        # Check if routed_via column exists
        col_rows = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='agent_run'"
        ).fetchall()
        col_names = {r[0] for r in col_rows}
        has_routed_via = "routed_via" in col_names

        if not has_routed_via:
            return {
                "rows": [],
                "has_routed_via": False,
                "generated_at": generated_at,
                "error": None,
            }

        # Build query adaptively — older schemas may lack cache_read/cache_write/model.
        # These columns were added progressively; fall back to 0 / NULL when absent.
        has_cache_read  = "cache_read"  in col_names
        has_cache_write = "cache_write" in col_names
        has_model       = "model"       in col_names

        cache_read_expr  = "COALESCE(cache_read,  0)" if has_cache_read  else "0"
        cache_write_expr = "COALESCE(cache_write, 0)" if has_cache_write else "0"
        model_expr       = "FIRST(model)"             if has_model       else "NULL"

        # Query per-(role, routed_via) aggregates.
        # Also collects per-pass-run token sums so we can compute
        # cost_per_success_usd using the shared cost_pricing module.
        query = f"""
            SELECT
                role,
                routed_via,
                COUNT(*)                                         AS run_count,
                MEDIAN(input_tok)                               AS median_input_tok,
                MEDIAN(output_tok)                              AS median_output_tok,
                AVG(CASE WHEN verdict IN ('done', 'pass') THEN 1.0 ELSE 0.0 END)
                                                                AS pass_rate,
                -- Token totals for successful runs (needed for cost_per_success)
                SUM(CASE WHEN verdict IN ('done', 'pass')
                         THEN COALESCE(input_tok,  0) ELSE 0 END) AS pass_input_tok,
                SUM(CASE WHEN verdict IN ('done', 'pass')
                         THEN COALESCE(output_tok, 0) ELSE 0 END) AS pass_output_tok,
                SUM(CASE WHEN verdict IN ('done', 'pass')
                         THEN {cache_read_expr}         ELSE 0 END) AS pass_cache_read,
                SUM(CASE WHEN verdict IN ('done', 'pass')
                         THEN {cache_write_expr}        ELSE 0 END) AS pass_cache_write,
                SUM(CASE WHEN verdict IN ('done', 'pass') THEN 1 ELSE 0 END)
                                                                AS pass_count,
                {model_expr}                                    AS model_sample
            FROM agent_run
            WHERE routed_via IS NOT NULL
              AND role IS NOT NULL
            GROUP BY role, routed_via
            ORDER BY role, routed_via
        """
        rows = conn.execute(query).fetchall()

        result_rows = []
        for (
            role, route, run_count,
            med_in, med_out, pass_rate,
            pass_input_tok, pass_output_tok, pass_cache_read, pass_cache_write,
            pass_count, model_sample,
        ) in rows:
            # Compute real cost-per-success from token counts via shared pricing module.
            # None when there are no successful runs in this group.
            if pass_count and pass_count > 0:
                total_pass_cost = _cost_usd(
                    input_tok=int(pass_input_tok or 0),
                    output_tok=int(pass_output_tok or 0),
                    cache_read=int(pass_cache_read or 0),
                    cache_write=int(pass_cache_write or 0),
                    model=model_sample or None,
                )
                cost_per_success = round(total_pass_cost / pass_count, 8)
            else:
                cost_per_success = None

            result_rows.append({
                "role": role or "unknown",
                "route": route or "unknown",
                "run_count": int(run_count) if run_count is not None else 0,
                "median_input_tok": int(med_in) if med_in is not None else None,
                "median_output_tok": int(med_out) if med_out is not None else None,
                "pass_rate": round(float(pass_rate), 4) if pass_rate is not None else None,
                "cost_per_success_usd": cost_per_success,
            })

        return {
            "rows": result_rows,
            "has_routed_via": True,
            "generated_at": generated_at,
            "error": None,
        }

    except Exception as exc:  # noqa: BLE001
        logger.warning("sdk_vs_cc_by_role query failed: %s", exc)
        return {
            "rows": [],
            "has_routed_via": False,
            "generated_at": generated_at,
            "error": str(exc),
        }
    finally:
        conn.close()
