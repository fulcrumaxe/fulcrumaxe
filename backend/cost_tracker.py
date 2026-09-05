"""
Cost tracking module — maps token usage to dollar amounts per model.

Loads pricing from .autonomous-team/config.json under the `pricing` key.
Reads per-agent spend records from the blackboard and computes aggregate costs.

Usage (CLI):
    python backend/cost_tracker.py summary
    python backend/cost_tracker.py by-discussion
    python backend/cost_tracker.py by-discussion --top 5
    python backend/cost_tracker.py by-discussion --discussion 367
    python backend/cost_tracker.py by-discussion --text
    python backend/cost_tracker.py by-role
    python backend/cost_tracker.py by-role --days 14
    python backend/cost_tracker.py by-role --json
    python backend/cost_tracker.py by-role --top 3

Usage (library):
    from backend.cost_tracker import CostTracker
    ct = CostTracker()
    breakdown = ct.get_session_cost()
    print(breakdown["total_cost_usd"])
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root: `python backend/cost_tracker.py ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard  # noqa: E402

_DEFAULT_CONFIG_PATH = Path(".autonomous-team/config.json")

# Pricing table verified against https://www.anthropic.com/pricing (2026-05-11).
# anthropic.com/pricing redirects to claude.com/pricing (consumer plans) which does
# not list API rates; rates below come from the D#570 spec table, cross-referenced
# with Anthropic's published API pricing documentation.
#
# Cache pricing fields:
#   cache_read_per_1k       — per 1K cache-read tokens (~10% of input rate)
#   cache_write_5m_per_1k   — per 1K cache-write tokens, 5-min TTL (~1.25× input rate)
#   cache_write_1h_per_1k   — per 1K cache-write tokens, 1-hr TTL (~2× input rate)
#
# 1M-context premium:
#   input_per_1k_above_200k — rate applied per 1K tokens above the 200K boundary
#                             (claude-opus-4-7[1m] only; stored for reference)
_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # Fallback for unknown models — Sonnet rates; emits a one-time warning on first use.
    "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    # ── Current models (as of 2026-05-11) ────────────────────────────────────
    "claude-opus-4-7": {
        "input_per_1k": 0.015,
        "output_per_1k": 0.075,
        "cache_read_per_1k": 0.0015,
        "cache_write_5m_per_1k": 0.01875,
        "cache_write_1h_per_1k": 0.03,
    },
    # 1M-context Opus variant — higher flat rate for all tokens
    "claude-opus-4-7[1m]": {
        "input_per_1k": 0.030,
        "output_per_1k": 0.150,
        "input_per_1k_above_200k": 0.030,
        "cache_read_per_1k": 0.003,
        "cache_write_5m_per_1k": 0.0375,
    },
    "claude-sonnet-4-6": {
        "input_per_1k": 0.003,
        "output_per_1k": 0.015,
        "cache_read_per_1k": 0.0003,
        "cache_write_5m_per_1k": 0.00375,
    },
    "claude-sonnet-4-5-20250929": {
        "input_per_1k": 0.003,
        "output_per_1k": 0.015,
    },
    "claude-haiku-4-5-20251001": {
        "input_per_1k": 0.0008,
        "output_per_1k": 0.004,
    },
    # ── Legacy models (kept for historical backfill accuracy) ─────────────────
    # cache_read_per_1k / cache_write_5m_per_1k backfilled from claude-sonnet-4-6
    # (D#2294): identical input/output rates in this table, so its cache rates
    # transfer on the same basis. This is the only legacy entry that carries
    # recorded tokens today (see backend/tests/test_cost_pricing.py::TestModelPricingCompleteness).
    "claude-sonnet-4-20250514": {
        "input_per_1k": 0.003,
        "output_per_1k": 0.015,
        "cache_read_per_1k": 0.0003,
        "cache_write_5m_per_1k": 0.00375,
    },
    "claude-opus-4-20250514": {"input_per_1k": 0.015, "output_per_1k": 0.075},
    "kimi-k2-0711": {"input_per_1k": 0.0006, "output_per_1k": 0.002},
}

# Blackboard fallback prefix. cost_tracker no longer reads this directly (see
# _agent_records / backend/stats/agent_spend.py) — kept as a module constant
# because existing tests import it to build fixture keys.
_AGENTS_PREFIX = "budget/agents/"

# Track unknown models so we warn only once per process lifetime.
_WARNED_UNKNOWN_MODELS: set[str] = set()

# Track (model, rate_key) pairs already warned about missing pricing keys,
# so a rate table that's missing e.g. cache_read_per_1k for a model doesn't
# emit one warning per row (D#2294) — mirrors _WARNED_UNKNOWN_MODELS above.
_WARNED_MISSING_KEYS: set[tuple[str, str]] = set()


def _warn_missing_rate_key(model: str, key: str) -> None:
    """Warn once per (model, key) that a populated token class has no rate.

    Only called when the row actually contributes tokens of that class —
    an absent key nothing populates is not a defect worth reporting.
    """
    dedupe_key = (model, key)
    if dedupe_key in _WARNED_MISSING_KEYS:
        return
    _WARNED_MISSING_KEYS.add(dedupe_key)
    warnings.warn(
        f"cost_tracker: model '{model}' pricing entry is missing '{key}' — "
        "tokens of that class are being priced at $0.00. Add this key to "
        "_DEFAULT_PRICING and .autonomous-team/config.json.",
        stacklevel=3,
    )
    print(
        f"[cost_tracker] WARNING: model '{model}' pricing entry missing '{key}' "
        "for a populated token class — priced at $0.00. "
        "Update _DEFAULT_PRICING or .autonomous-team/config.json.",
        file=sys.stderr,
    )


def _load_pricing() -> dict[str, dict[str, float]]:
    """Load pricing table from config.json, falling back to hardcoded defaults."""
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    config_path = repo_root / _DEFAULT_CONFIG_PATH
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        pricing = cfg.get("pricing", {})
        if not pricing:
            return dict(_DEFAULT_PRICING)
        # Ensure a 'default' entry always exists.
        if "default" not in pricing:
            pricing["default"] = _DEFAULT_PRICING["default"]
        return pricing
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_PRICING)


def _compute_cost(
    input_tokens: int,
    output_tokens: int,
    model: str,
    pricing: dict[str, dict[str, float]],
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Compute USD cost for given token counts using the pricing table.

    Args:
        input_tokens: Regular (non-cached) input tokens.
        output_tokens: Output tokens.
        model: Model ID string.
        pricing: Pricing table (model → rate dict).
        cache_read_tokens: Tokens read from cache (priced at cache_read_per_1k).
        cache_write_tokens: Tokens written to cache (priced at cache_write_5m_per_1k).

    Falls back to the 'default' entry when the model is not found, emitting a
    one-time stderr warning so callers can detect new unpriced model IDs.
    The warning only fires when the row actually contributes tokens — a
    model with zero recorded tokens is not affecting any figure (D#2294),
    so warning about it is noise rather than signal.
    """
    contributes_tokens = bool(
        input_tokens or output_tokens or cache_read_tokens or cache_write_tokens
    )

    if model not in pricing:
        if contributes_tokens and model not in _WARNED_UNKNOWN_MODELS:
            _WARNED_UNKNOWN_MODELS.add(model)
            warnings.warn(
                f"cost_tracker: unknown model '{model}' — falling back to 'default' pricing. "
                "Add this model to _DEFAULT_PRICING or config.json pricing section.",
                stacklevel=2,
            )
            print(
                f"[cost_tracker] WARNING: unknown model '{model}' — using default pricing. "
                "Update _DEFAULT_PRICING or .autonomous-team/config.json.",
                file=sys.stderr,
            )

    rates = pricing.get(model) or pricing.get("default") or _DEFAULT_PRICING["default"]
    input_rate = rates.get("input_per_1k", 0.003)
    output_rate = rates.get("output_per_1k", 0.015)

    cost = (input_tokens / 1000.0 * input_rate) + (output_tokens / 1000.0 * output_rate)

    if cache_read_tokens > 0:
        if "cache_read_per_1k" not in rates:
            _warn_missing_rate_key(model, "cache_read_per_1k")
        cache_read_rate = rates.get("cache_read_per_1k", 0.0)
        cost += cache_read_tokens / 1000.0 * cache_read_rate

    if cache_write_tokens > 0:
        if "cache_write_5m_per_1k" not in rates:
            _warn_missing_rate_key(model, "cache_write_5m_per_1k")
        cache_write_rate = rates.get("cache_write_5m_per_1k", 0.0)
        cost += cache_write_tokens / 1000.0 * cache_write_rate

    return cost


class CostTracker:
    """
    Computes dollar costs from token spend records stored on the blackboard.

    Pricing is loaded from config.json. Falls back to hardcoded defaults when
    the config is unavailable or the pricing section is missing.
    """

    def __init__(self, bb: Blackboard | None = None) -> None:
        self._bb = bb if bb is not None else Blackboard()
        self._pricing = _load_pricing()

    def _agent_records(
        self,
        discussion: int | None = None,
        pr: int | None = None,
    ) -> list[dict]:
        """Single seam for per-agent spend records — every cost surface goes
        through here rather than reading the blackboard directly.

        Delegates to backend.stats.agent_spend, which sources from `agent_run`
        (authoritative) with `budget/agents/` blackboard as a precedence
        fallback — never a union — used only when `agent_run` has no rows for
        the requested scope. See backend/stats/agent_spend.py for the full
        precedence rationale.
        """
        from backend.stats import agent_spend  # noqa: PLC0415 — heavy (duckdb) import deferred

        if pr is not None:
            return agent_spend.records_for_pr(pr, bb=self._bb)
        if discussion is not None:
            return agent_spend.records_for_discussion(discussion, bb=self._bb)
        return agent_spend.all_records(bb=self._bb)

    def compute_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str = "default",
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Return the dollar cost for a single spend record."""
        return _compute_cost(
            input_tokens,
            output_tokens,
            model,
            self._pricing,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

    def get_session_cost(self) -> dict:
        """
        Aggregate all per-agent spend records from the blackboard and compute costs.

        Returns:
            {
                "total_cost_usd": float,
                "by_agent": [{"agent_id": str, "role": str, "model": str,
                               "input": int, "output": int, "cost_usd": float, ...}],
                "by_discussion": [{"discussion": int, "cost_usd": float, "agents": [str]}],
                "model_breakdown": [{"model": str, "input": int, "output": int,
                                     "cost_usd": float, "agent_count": int}],
            }
        """
        records = self._agent_records()

        by_agent: list[dict] = []
        model_totals: dict[str, dict] = {}
        discussion_totals: dict[int, dict] = {}

        for record in records:
            input_tokens = int(record.get("input", 0))
            output_tokens = int(record.get("output", 0))
            cache_read_tokens = int(record.get("cache_read_tokens", 0))
            cache_write_tokens = int(record.get("cache_write_tokens", 0))
            model = record.get("model", "default") or "default"
            agent_id = record.get("agent_id") or "unknown"
            role = record.get("agent", "unknown")
            discussion = record.get("discussion")
            source = record.get("source", "budget_blackboard")

            cost = _compute_cost(
                input_tokens,
                output_tokens,
                model,
                self._pricing,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )

            by_agent.append({
                "agent_id": agent_id,
                "role": role,
                "model": model,
                "input": input_tokens,
                "output": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "cost_usd": round(cost, 6),
                "finished": record.get("finished"),
                "discussion": discussion,
            })

            # Accumulate model totals.
            if model not in model_totals:
                model_totals[model] = {
                    "model": model,
                    "input": 0,
                    "output": 0,
                    "cost_usd": 0.0,
                    "agent_count": 0,
                }
            model_totals[model]["input"] += input_tokens
            model_totals[model]["output"] += output_tokens
            model_totals[model]["cost_usd"] += cost
            model_totals[model]["agent_count"] += 1

            # Accumulate discussion totals.
            if discussion is not None:
                disc_key = int(discussion)
                if disc_key not in discussion_totals:
                    discussion_totals[disc_key] = {
                        "discussion": disc_key,
                        "cost_usd": 0.0,
                        "agents": [],
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "agent_count": 0,
                        "source": source,
                        "_agent_breakdown": {},
                        "_pr_breakdown": {},
                    }
                discussion_totals[disc_key]["cost_usd"] += cost
                discussion_totals[disc_key]["agents"].append(agent_id)
                discussion_totals[disc_key]["input_tokens"] += input_tokens
                discussion_totals[disc_key]["output_tokens"] += output_tokens
                discussion_totals[disc_key]["agent_count"] += 1
                # Per-role breakdown
                ab = discussion_totals[disc_key]["_agent_breakdown"]
                ab[role] = ab.get(role, 0.0) + cost
                # Per-PR breakdown (only if record has a pr field)
                pr_val = record.get("pr")
                if pr_val is not None:
                    pb = discussion_totals[disc_key]["_pr_breakdown"]
                    pr_str = str(pr_val)
                    pb[pr_str] = pb.get(pr_str, 0.0) + cost

        total_cost = float(sum(a["cost_usd"] for a in by_agent))

        # Round model totals.
        model_breakdown = []
        for entry in model_totals.values():
            entry["cost_usd"] = round(entry["cost_usd"], 6)
            model_breakdown.append(entry)

        # Round discussion totals and add convenience aliases.
        by_discussion = []
        for entry in discussion_totals.values():
            entry["cost_usd"] = round(entry["cost_usd"], 6)
            # total_cost_usd is an alias for cost_usd (cost_usd kept for backward compat).
            entry["total_cost_usd"] = entry["cost_usd"]
            entry["total_input_tokens"] = entry.pop("input_tokens")
            entry["total_output_tokens"] = entry.pop("output_tokens")
            # Promote and round breakdown dicts; remove internal underscore keys.
            raw_ab = entry.pop("_agent_breakdown", {})
            raw_pb = entry.pop("_pr_breakdown", {})
            entry["agent_breakdown"] = {k: round(v, 6) for k, v in raw_ab.items()}
            entry["pr_breakdown"] = {k: round(v, 6) for k, v in raw_pb.items()}
            by_discussion.append(entry)

        return {
            "total_cost_usd": float(round(total_cost, 4)),
            "by_agent": sorted(by_agent, key=lambda x: x.get("finished") or "", reverse=True),
            "by_discussion": sorted(by_discussion, key=lambda x: x["discussion"]),
            "model_breakdown": sorted(model_breakdown, key=lambda x: x["cost_usd"], reverse=True),
        }

    def per_pr_summary(self, pr_number: int) -> dict | None:
        """
        Aggregate spend for a specific PR via the `_agent_records` seam —
        agent_run rows tagged with this PR, or (only when agent_run has none)
        blackboard records matched by linked Discussion or by PR number
        appearing in agent_id (see backend/stats/agent_spend.py).

        Returns a dict with keys:
            input_tokens, output_tokens, total_tokens, usd, source,
            by_role: [{role, input_tokens, output_tokens, usd}]
        Returns None when no matching records are found.
        """
        records = self._agent_records(pr=pr_number)
        role_totals: dict[str, dict] = {}
        source = None

        for record in records:
            input_tokens = int(record.get("input", 0))
            output_tokens = int(record.get("output", 0))
            cache_read_tokens = int(record.get("cache_read_tokens", 0))
            cache_write_tokens = int(record.get("cache_write_tokens", 0))
            model = record.get("model", "default") or "default"
            role = record.get("agent", "unknown")
            source = record.get("source", "budget_blackboard")
            cost = _compute_cost(
                input_tokens,
                output_tokens,
                model,
                self._pricing,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )

            if role not in role_totals:
                role_totals[role] = {
                    "role": role,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "usd": 0.0,
                }
            role_totals[role]["input_tokens"] += input_tokens
            role_totals[role]["output_tokens"] += output_tokens
            role_totals[role]["usd"] += cost

        if not role_totals:
            return None

        total_input = sum(r["input_tokens"] for r in role_totals.values())
        total_output = sum(r["output_tokens"] for r in role_totals.values())
        total_usd = sum(r["usd"] for r in role_totals.values())

        by_role = []
        for entry in role_totals.values():
            by_role.append({
                "role": entry["role"],
                "input_tokens": entry["input_tokens"],
                "output_tokens": entry["output_tokens"],
                "usd": round(entry["usd"], 6),
            })

        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "usd": round(total_usd, 6),
            "source": source,
            "by_role": sorted(by_role, key=lambda x: x["usd"], reverse=True),
        }

    def aggregate_daily_monthly_spend(self, now: datetime | None = None) -> dict:
        """Return real USD spend for today (UTC) and the current calendar month.

        Sums cost_usd from get_session_cost().by_agent records whose 'finished'
        timestamp falls within today-UTC or month-start-UTC.  Records with
        missing or unparseable 'finished' are skipped.

        Args:
            now: Current time override for testing. Defaults to UTC now.

        Returns:
            {"daily_usd": float, "monthly_usd": float}
        """
        if now is None:
            now = datetime.now(timezone.utc)

        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today_midnight.replace(day=1)

        session = self.get_session_cost()
        daily = 0.0
        monthly = 0.0

        for record in session.get("by_agent", []):
            finished_raw = record.get("finished")
            if not finished_raw:
                continue
            try:
                ts = finished_raw.strip()
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                finished_dt = datetime.fromisoformat(ts)
                if finished_dt.tzinfo is None:
                    finished_dt = finished_dt.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue

            cost = record.get("cost_usd", 0.0)
            if finished_dt >= month_start:
                monthly += cost
            if finished_dt >= today_midnight:
                daily += cost

        return {"daily_usd": round(daily, 6), "monthly_usd": round(monthly, 6)}

    def get_summary(self) -> dict:
        """Return a lightweight summary: total cost and model breakdown only."""
        full = self.get_session_cost()
        return {
            "total_cost_usd": full["total_cost_usd"],
            "model_breakdown": full["model_breakdown"],
        }

    def get_role_efficiency(self, days: int = 7) -> dict:
        """
        Aggregate per-role cost and verdict stats from the blackboard.

        Reads ``budget/agents/*`` blackboard entries (written by post-agent-hook.sh)
        within the last *days* days, then correlates ``memory/*`` entries for verdict
        data (tags contain the verdict string, e.g. ["executor", "done"]).

        Returns a dict matching the role-efficiency JSON schema::

            {
                "schema_version": 1,
                "generated_at": "<ISO8601>",
                "window_days": 7,
                "roles": [
                    {
                        "role": "executor",
                        "total_runs": 34,
                        "total_input_tokens": 950000,
                        "total_output_tokens": 250000,
                        "total_tokens": 1200000,
                        "total_cost_usd": 42.18,
                        "avg_tokens_per_run": 35294,
                        "verdict_counts": {"done": 30, "needs-fix": 4},
                        "passes": 30,
                        "needs_fix_rate": 0.118,
                        "avg_cost_per_pass_usd": 1.406,
                    }
                ],
            }
        """
        from datetime import timedelta  # noqa: PLC0415

        now = datetime.now(timezone.utc)
        generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        cutoff = now - timedelta(days=days)

        # ------------------------------------------------------------------ #
        # 1. Pull per-agent spend records (agent_run, with blackboard        #
        #    fallback) within the time window, via the _agent_records seam.  #
        # ------------------------------------------------------------------ #
        records = self._agent_records()
        # role -> aggregation bucket
        role_buckets: dict[str, dict] = {}

        for record in records:
            input_tokens = int(record.get("input", 0))
            output_tokens = int(record.get("output", 0))
            if input_tokens + output_tokens == 0:
                continue

            role = record.get("agent") or record.get("role") or ""
            if not role:
                continue

            # Time window filter using the 'finished' timestamp
            finished = record.get("finished", "")
            if finished:
                try:
                    finished_dt = datetime.fromisoformat(
                        finished.replace("Z", "+00:00")
                    )
                    if finished_dt < cutoff:
                        continue
                except ValueError:
                    pass

            model = record.get("model", "default") or "default"
            cache_read_tokens = int(record.get("cache_read_tokens", 0))
            cache_write_tokens = int(record.get("cache_write_tokens", 0))
            cost = _compute_cost(
                input_tokens,
                output_tokens,
                model,
                self._pricing,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )

            if role not in role_buckets:
                role_buckets[role] = {
                    "role": role,
                    "total_runs": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_usd": 0.0,
                    "verdict_counts": {},
                }
            bucket = role_buckets[role]
            bucket["total_runs"] += 1
            bucket["total_input_tokens"] += input_tokens
            bucket["total_output_tokens"] += output_tokens
            bucket["total_cost_usd"] += cost

        # ------------------------------------------------------------------ #
        # 2. Pull memory/* entries to extract verdict tags                    #
        # ------------------------------------------------------------------ #
        try:
            memory_keys = self._bb.list_keys("memory/")
            for mkey in memory_keys:
                mem = self._bb.read(mkey)
                if not isinstance(mem, dict):
                    continue
                role = mem.get("role", "")
                if not role or role not in role_buckets:
                    continue
                tags = mem.get("tags") or []
                # Derive verdict from tags — last non-role tag is the verdict
                # e.g. ["executor", "done"] → verdict = "done"
                verdict = None
                for tag in reversed(tags):
                    if tag != role:
                        verdict = tag
                        break
                if verdict is None:
                    # Fall back to lesson_type: "success" → "pass", "failure" → "fail"
                    lt = mem.get("lesson_type", "")
                    if lt == "success":
                        verdict = "pass"
                    elif lt == "failure":
                        verdict = "fail"
                if verdict:
                    vc = role_buckets[role]["verdict_counts"]
                    vc[verdict] = vc.get(verdict, 0) + 1
        except Exception:  # noqa: BLE001
            pass

        # ------------------------------------------------------------------ #
        # 3. Compute derived fields                                           #
        # ------------------------------------------------------------------ #
        _PASS_VERDICTS = {"pass", "done"}
        roles_out: list[dict] = []

        for bucket in role_buckets.values():
            total_runs = bucket["total_runs"]
            total_tokens = bucket["total_input_tokens"] + bucket["total_output_tokens"]
            total_cost = round(bucket["total_cost_usd"], 6)
            verdict_counts: dict[str, int] = bucket["verdict_counts"]
            passes = sum(verdict_counts.get(v, 0) for v in _PASS_VERDICTS)
            needs_fix = verdict_counts.get("needs-fix", 0)
            needs_fix_rate = round(needs_fix / total_runs, 3) if total_runs > 0 else 0.0
            avg_tokens_per_run = round(total_tokens / total_runs) if total_runs > 0 else 0
            avg_cost_per_pass = (
                round(total_cost / passes, 6) if passes > 0 else None
            )

            roles_out.append({
                "role": bucket["role"],
                "total_runs": total_runs,
                "total_input_tokens": bucket["total_input_tokens"],
                "total_output_tokens": bucket["total_output_tokens"],
                "total_tokens": total_tokens,
                "total_cost_usd": total_cost,
                "avg_tokens_per_run": avg_tokens_per_run,
                "verdict_counts": verdict_counts,
                "passes": passes,
                "needs_fix_rate": needs_fix_rate,
                "avg_cost_per_pass_usd": avg_cost_per_pass,
            })

        roles_out.sort(key=lambda x: x["total_cost_usd"], reverse=True)

        return {
            "schema_version": 1,
            "generated_at": generated_at,
            "window_days": days,
            "roles": roles_out,
        }

    def write_role_efficiency_json(self, days: int = 7) -> dict:
        """Compute role efficiency data and atomically write to the JSON file.

        Returns the computed data dict.
        """
        data = self.get_role_efficiency(days=days)
        here = Path(__file__).resolve().parent
        repo_root = here.parent
        out_path = repo_root / ".autonomous-team" / "role-efficiency.json"
        tmp_path = out_path.with_suffix(".json.tmp")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(str(tmp_path), str(out_path))
        except OSError:
            pass
        return data

    def summarize_team_lead(self, window_seconds: int = 18000) -> dict:
        """Return Team Lead (parent session) token and cost summary.

        Reads JSONL transcripts from ~/.claude/projects/-home-agent-autonomous-forever/
        (the exact project root — sub-agent worktree dirs are excluded).

        Args:
            window_seconds: Rolling window in seconds (default 18000 = 5h).

        Returns:
            {
                "input_tokens":          int,
                "output_tokens":         int,
                "cache_read":            int,
                "cache_write":           int,
                "cost_usd_equivalent":   float,  # estimated — not billed
                "p50_tokens_per_turn":   int,
                "p95_tokens_per_turn":   int,
                "sessions_count":        int,
            }
        """
        try:
            from backend.subscription_usage import team_lead_usage  # noqa: PLC0415
        except ImportError:
            # Fallback for direct-script invocation
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from backend.subscription_usage import team_lead_usage  # noqa: PLC0415

        tl = team_lead_usage(window_seconds=window_seconds)

        # Cost uses Opus 4.7 pricing (Team Lead runs on Opus 4.7)
        opus_pricing_key = "claude-opus-4-7"
        cost = _compute_cost(
            input_tokens=tl["input"],
            output_tokens=tl["output"],
            model=opus_pricing_key,
            pricing=self._pricing,
            cache_read_tokens=tl["cache_read"],
            cache_write_tokens=tl["cache_write"],
        )

        return {
            "input_tokens": tl["input"],
            "output_tokens": tl["output"],
            "cache_read": tl["cache_read"],
            "cache_write": tl["cache_write"],
            "cost_usd_equivalent": round(cost, 6),
            "p50_tokens_per_turn": tl["p50_tokens_per_turn"],
            "p95_tokens_per_turn": tl["p95_tokens_per_turn"],
            "sessions_count": tl["sessions_count"],
        }

    def print_summary(self) -> None:
        """Print a human-readable cost summary table to stdout."""
        summary = self.get_session_cost()
        total = summary["total_cost_usd"]

        print("Cost Summary")
        print("=" * 60)
        print(f"{'Model':<35} {'Input':>10} {'Output':>10} {'Cost (USD)':>12}")
        print("-" * 60)
        for entry in summary["model_breakdown"]:
            print(
                f"{entry['model']:<35} "
                f"{entry['input']:>10,} "
                f"{entry['output']:>10,} "
                f"${entry['cost_usd']:>11.4f}"
            )
        print("-" * 60)
        print(f"{'TOTAL':<35} {'':>10} {'':>10} ${total:>11.4f}")
        print()

        if summary["by_discussion"]:
            print("By Discussion")
            print("-" * 40)
            for entry in summary["by_discussion"]:
                print(
                    f"  Discussion #{entry['discussion']}: "
                    f"${entry['cost_usd']:.4f} "
                    f"({len(entry['agents'])} agent(s))"
                )

        # Team Lead section — always printed; shows 0 when no JSONL found
        print()
        try:
            tl = self.summarize_team_lead()
            print("Team Lead (parent session)")
            print("-" * 60)
            print(f"  Input:       {tl['input_tokens']:>12,} tokens")
            print(f"  Output:      {tl['output_tokens']:>12,} tokens")
            print(f"  Cache read:  {tl['cache_read']:>12,}")
            print(f"  Cache write: {tl['cache_write']:>12,}")
            print(f"  Est. $-equivalent (Opus pricing): ${tl['cost_usd_equivalent']:.4f}  [estimated, not billed]")
            print(f"  p50 tokens/turn: {tl['p50_tokens_per_turn']:,}   p95: {tl['p95_tokens_per_turn']:,}")
        except Exception as exc:  # noqa: BLE001
            print(f"  (Team Lead data unavailable: {exc})")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _print_by_role_table(
    roles: list[dict],
    top: int | None,
    days: int,
    generated_at: str,
) -> None:
    """Print a human-readable per-role efficiency table to stdout."""
    display = roles[:top] if top is not None else roles

    header = (
        f"{'Role':<20} {'Runs':>6} {'Tokens':>10} {'Cost($)':>10}"
        f" {'Avg$/pass':>10} {'NeedsFix%':>10}"
    )
    print(header)
    print("-" * len(header))

    for entry in display:
        role = entry["role"]
        runs = entry["total_runs"]
        tokens = entry["total_tokens"]
        cost = entry["total_cost_usd"]
        avg_pass = entry["avg_cost_per_pass_usd"]
        nfr = entry["needs_fix_rate"] * 100

        tokens_str = _fmt_tokens(tokens)
        avg_pass_str = f"{avg_pass:.4f}" if avg_pass is not None else "  n/a  "
        print(
            f"  {role:<18} {runs:>6,} {tokens_str:>10}"
            f" ${cost:>9.4f} {avg_pass_str:>10} {nfr:>9.1f}%"
        )

    print("-" * len(header))
    print(f"Window: {days} days | Generated: {generated_at}")
    print()


def _fmt_tokens(n: int) -> str:
    """Format token count as e.g. '1.2M' or '480K' or '35K'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _print_by_discussion_table(entries: list[dict]) -> None:
    """Print a human-readable table of by-discussion entries."""
    print(f"{'Discussion':<12} {'Cost (USD)':>12} {'Agents':>8} {'Input Tok':>12} {'Output Tok':>12}")
    print("-" * 60)
    for entry in entries:
        disc = entry.get("discussion", "?")
        cost = entry.get("total_cost_usd", 0.0)
        agent_count = entry.get("agent_count", len(entry.get("agents", [])))
        inp = entry.get("total_input_tokens", 0)
        out = entry.get("total_output_tokens", 0)
        print(f"  #{disc:<10} ${cost:>11.4f} {agent_count:>8,} {inp:>12,} {out:>12,}")
        # Show top 3 roles and top 3 PRs when breakdowns are present.
        ab = entry.get("agent_breakdown", {})
        pb = entry.get("pr_breakdown", {})
        if ab:
            top_roles = sorted(ab.items(), key=lambda x: x[1], reverse=True)[:3]
            roles_str = ", ".join(f"{r} ${v:.4f}" for r, v in top_roles)
            print(f"    top roles: {roles_str}")
        if pb:
            top_prs = sorted(pb.items(), key=lambda x: x[1], reverse=True)[:3]
            prs_str = ", ".join(f"PR#{p} ${v:.4f}" for p, v in top_prs)
            print(f"    top PRs:   {prs_str}")
    print()


def main(argv: list[str] | None = None) -> int:
    raw_args = argv if argv is not None else sys.argv[1:]

    if not raw_args or raw_args[0] == "summary":
        ct = CostTracker()
        ct.print_summary()
        return 0

    if raw_args[0] == "by-discussion":
        parser = argparse.ArgumentParser(
            prog="cost_tracker.py by-discussion",
            description="Show cost breakdown by Discussion number.",
        )
        parser.add_argument(
            "--top", type=int, default=None, metavar="N",
            help="Truncate to top N entries by cost (after sorting).",
        )
        parser.add_argument(
            "--discussion", type=int, default=None, metavar="N",
            help="Filter to a single Discussion number.",
        )
        parser.add_argument(
            "--text", action="store_true",
            help="Print a human-readable table instead of JSON.",
        )
        parser.add_argument(
            "--json", action="store_true", dest="json_output",
            help="Emit JSON output (default when --text is not passed; accepted for scripting).",
        )

        try:
            args = parser.parse_args(raw_args[1:])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1

        if args.top is not None and args.top <= 0:
            print("ERROR: --top must be a positive integer", file=sys.stderr)
            return 1

        ct = CostTracker()
        full = ct.get_session_cost()
        entries = sorted(
            full.get("by_discussion", []),
            key=lambda x: x.get("total_cost_usd", 0.0),
            reverse=True,
        )

        if args.discussion is not None:
            matched = next((e for e in entries if e.get("discussion") == args.discussion), None)
            if args.text:
                if matched is None:
                    print("(no record found)")
                else:
                    _print_by_discussion_table([matched])
            else:
                print(json.dumps(matched, indent=2))
            return 0

        if args.top is not None:
            entries = entries[: args.top]

        if args.text:
            _print_by_discussion_table(entries)
        else:
            print(json.dumps(entries, indent=2))
        return 0

    if raw_args[0] == "per-discussion":
        # Alias for `by-discussion --discussion N` — re-dispatch with translated args.
        translated: list[str] = ["by-discussion"]
        i = 1
        while i < len(raw_args):
            if raw_args[i] == "--discussion" and i + 1 < len(raw_args):
                translated += ["--discussion", raw_args[i + 1]]
                i += 2
            elif raw_args[i] == "--text":
                translated.append("--text")
                i += 1
            else:
                i += 1
        return main(translated)

    if raw_args[0] == "top":
        # Alias for `by-discussion --top N` with optional --limit N (default 10).
        translated = ["by-discussion"]
        i = 1
        limit_val = "10"
        while i < len(raw_args):
            if raw_args[i] == "--limit" and i + 1 < len(raw_args):
                limit_val = raw_args[i + 1]
                i += 2
            elif raw_args[i] == "--text":
                translated.append("--text")
                i += 1
            else:
                i += 1
        translated += ["--top", limit_val]
        return main(translated)

    if raw_args[0] == "by-role":
        parser = argparse.ArgumentParser(
            prog="cost_tracker.py by-role",
            description="Show per-role cost and needs-fix-rate over a rolling window.",
        )
        parser.add_argument(
            "--days", type=int, default=7, metavar="N",
            help="Aggregation window in days (default 7).",
        )
        parser.add_argument(
            "--json", action="store_true", dest="json_output",
            help="Emit machine-readable JSON to stdout instead of a table.",
        )
        parser.add_argument(
            "--top", type=int, default=None, metavar="K",
            help="Limit table to top K roles by total cost (JSON file always has all).",
        )

        try:
            args = parser.parse_args(raw_args[1:])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1

        if args.days <= 0:
            print("ERROR: --days must be a positive integer", file=sys.stderr)
            return 1
        if args.top is not None and args.top <= 0:
            print("ERROR: --top must be a positive integer", file=sys.stderr)
            return 1

        ct = CostTracker()
        data = ct.write_role_efficiency_json(days=args.days)
        all_roles = data["roles"]

        if args.json_output:
            # For --json, still limit stdout by --top but JSON file has all roles
            out_roles = all_roles[:args.top] if args.top is not None else all_roles
            print(json.dumps({**data, "roles": out_roles}, indent=2))
        else:
            _print_by_role_table(all_roles, args.top, args.days, data["generated_at"])
        return 0

    print(f"Unknown subcommand: {raw_args[0]!r}", file=sys.stderr)
    print("Usage: python backend/cost_tracker.py [summary|by-discussion|per-discussion|top|by-role]", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Cost spike detection (Discussion #540 metric #22)
# ---------------------------------------------------------------------------


def detect_cost_spike(series: list[float] | None = None) -> dict:
    """Detect whether the latest per-iteration cost exceeds μ + 3σ of the 24h baseline.

    Args:
        series: Optional list of cost values to use instead of querying DuckDB.
                Provided as [oldest, ..., newest]; the last value is the current
                iteration cost being evaluated.
                When None, reads 'iteration_cost_usd' metric_event rows from DuckDB.

    Returns:
        {
            "spike":             bool — True if current value > mu + 3*sigma
            "value":             float — current iteration cost
            "mu":                float — rolling 24h mean (excluding current)
            "sigma":             float — rolling 24h std dev (excluding current)
            "threshold":         float — mu + 3*sigma
            "sample_size":       int — number of data points in the baseline window
            "insufficient_data": bool — True when sample_size < 10
        }

    Rules:
    - If sample_size < 10: returns spike=False, insufficient_data=True
    - If sigma == 0 and value > mu: spike=True (all-equal baseline, one outlier)
    - Current value (last in series) is excluded from the baseline calculation
    """
    if series is None:
        series = _load_iteration_cost_series()

    if len(series) < 2:
        # Need at least one baseline point + one current point
        return {
            "spike": False,
            "value": series[-1] if series else 0.0,
            "mu": 0.0,
            "sigma": 0.0,
            "threshold": 0.0,
            "sample_size": 0,
            "insufficient_data": True,
        }

    current = series[-1]
    baseline = series[:-1]  # all but the current iteration

    if len(baseline) < 10:
        return {
            "spike": False,
            "value": current,
            "mu": 0.0,
            "sigma": 0.0,
            "threshold": 0.0,
            "sample_size": len(baseline),
            "insufficient_data": True,
        }

    n = len(baseline)
    mu = sum(baseline) / n
    variance = sum((x - mu) ** 2 for x in baseline) / n
    sigma = variance ** 0.5

    threshold = mu + 3.0 * sigma
    spike = current > threshold

    return {
        "spike": spike,
        "value": round(current, 6),
        "mu": round(mu, 6),
        "sigma": round(sigma, 6),
        "threshold": round(threshold, 6),
        "sample_size": n,
        "insufficient_data": False,
    }


def _load_iteration_cost_series() -> list[float]:
    """Load per-iteration cost values from DuckDB over the last 24h + current.

    Queries 'iteration_cost_usd' rows from metric_event, ordered by ts ascending.
    Returns an empty list if DuckDB is unavailable or no rows exist.
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return []

    from backend.stats_writer import _db_path  # noqa: PLC0415
    db = _db_path()
    if not db.exists():
        return []

    try:
        from datetime import timedelta  # noqa: PLC0415
        # Use a Python-computed cutoff to avoid DuckDB NOW() local-tz drift.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=25)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        conn = duckdb.connect(str(db))
        try:
            rows = conn.execute(
                """
                SELECT value
                FROM metric_event
                WHERE metric = 'iteration_cost_usd'
                  AND ts >= CAST(? AS TIMESTAMP)
                ORDER BY ts ASC
                """,
                [cutoff_str],
            ).fetchall()
        finally:
            conn.close()
        return [float(r[0]) for r in rows]
    except Exception:  # noqa: BLE001
        return []


if __name__ == "__main__":
    sys.exit(main())
