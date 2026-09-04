"""
Token budget tracker — per-session and per-agent token ceilings.

Tracks token usage in the blackboard under the `budget/` namespace.
Used by the Team Lead to gate agent spawns and log spend after completion.

Usage (CLI):
    python backend/budget.py init [--ceiling N]
    python backend/budget.py check
    python backend/budget.py spend <agent_id> <role> <input_tokens> <output_tokens> [--discussion N]
    python backend/budget.py status
    python backend/budget.py reset

Usage (library):
    from backend.budget import BudgetTracker
    bt = BudgetTracker()
    bt.init_session()
    result = bt.check_budget("executor")
    if result["allowed"]:
        bt.record_spend("executor-14-1712700000", "executor", 45000, 3200, discussion=14)
    print(bt.get_status())
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root: `python backend/budget.py ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard, LockTimeout  # noqa: E402


# ---------------------------------------------------------------------------
# Dedup helper — sqlite-backed per-event-id idempotency
# ---------------------------------------------------------------------------

_SEEN_DB_PATH = Path(".autonomous-team/hook-events/seen.sqlite")


def _resolve_seen_db() -> Path:
    """Resolve seen.sqlite path relative to repo root (tolerant of cwd)."""
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    return repo_root / _SEEN_DB_PATH


def _check_seen(event_id: str, hook: str) -> bool:
    """
    Return True if this event_id has already been recorded (i.e. it's a duplicate).
    Return False if it's new (and register it atomically).

    Prunes records older than 7 days on every call to bound table size.
    """
    if not event_id:
        return False
    db_path = _resolve_seen_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_events "
            "(event_id TEXT PRIMARY KEY, hook TEXT, ts TEXT)"
        )
        conn.execute(
            "DELETE FROM seen_events WHERE ts < datetime('now','-7 days')"
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_events VALUES (?,?,datetime('now'))",
            (event_id, hook),
        )
        conn.commit()
        conn.close()
        return cur.rowcount == 0  # True == already seen (no-op)
    except sqlite3.Error:
        # If sqlite fails, don't block budget recording — treat as unseen
        return False


_DEFAULT_CONFIG_PATH = Path(".autonomous-team/config.json")

_DEFAULT_BUDGET = {
    "session_ceiling": 5_000_000,
    "per_agent_ceiling": 500_000,
    "warn_threshold_pct": 80,
}

# Blackboard key constants
_KEY_SESSION_CEILING = "budget/session_ceiling"
_KEY_SESSION_SPENT = "budget/session_spent"
_KEY_PER_AGENT_CEILING = "budget/per_agent_ceiling"
_AGENTS_PREFIX = "budget/agents/"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_config() -> dict:
    """Load budget defaults from .autonomous-team/config.json, falling back to hardcoded defaults."""
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    config_path = repo_root / _DEFAULT_CONFIG_PATH
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        budget_cfg = cfg.get("budget", {})
        return {
            "session_ceiling": budget_cfg.get("session_ceiling", _DEFAULT_BUDGET["session_ceiling"]),
            "per_agent_ceiling": budget_cfg.get("per_agent_ceiling", _DEFAULT_BUDGET["per_agent_ceiling"]),
            "warn_threshold_pct": budget_cfg.get("warn_threshold_pct", _DEFAULT_BUDGET["warn_threshold_pct"]),
        }
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_BUDGET)


class BudgetTracker:
    """
    Token budget tracking backed by the blackboard.

    All budget state lives under the `budget/` namespace in the blackboard.
    Atomic increments to session_spent use CAS with one retry on conflict.
    """

    def __init__(self, bb: Blackboard | None = None):
        self._bb = bb if bb is not None else Blackboard()
        self._config = _load_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def init_session(self, ceiling: int | None = None) -> None:
        """
        Initialize budget blackboard keys for a new session.

        Writes session_ceiling (from arg or config default), resets session_spent
        to 0, and writes per_agent_ceiling from config.
        """
        effective_ceiling = ceiling if ceiling is not None else self._config["session_ceiling"]
        per_agent_ceiling = self._config["per_agent_ceiling"]
        self._bb.write(_KEY_SESSION_CEILING, effective_ceiling, updated_by="budget-tracker")
        self._bb.write(_KEY_SESSION_SPENT, 0, updated_by="budget-tracker")
        self._bb.write(_KEY_PER_AGENT_CEILING, per_agent_ceiling, updated_by="budget-tracker")

        # Emit audit event for session initialization (best-effort).
        try:
            from backend.audit_trail import get_audit_trail  # noqa: PLC0415
            get_audit_trail().emit(
                "budget", "init", "session",
                None,
                {"ceiling": effective_ceiling, "per_agent_ceiling": per_agent_ceiling},
                "budget-tracker",
            )
        except Exception:  # noqa: BLE001
            pass

    def check_budget(self, agent_role: str) -> dict:
        """
        Check whether there is enough budget remaining to spawn an agent.

        Returns:
            {
                "allowed": bool,       # False if remaining < per_agent_ceiling
                "remaining": int,
                "ceiling": int,
                "spent": int,
                "warn": bool,          # True if spent > warn_threshold_pct of ceiling
            }
        """
        ceiling = self._read_int(_KEY_SESSION_CEILING, self._config["session_ceiling"])
        per_agent_ceiling = self._read_int(_KEY_PER_AGENT_CEILING, self._config["per_agent_ceiling"])
        warn_pct = self._config["warn_threshold_pct"]

        # Derive spent from agents[] — session_spent is unreliable under concurrent
        # CAS races (each post-agent-hook reads the same version, one wins, rest
        # silently drop). Summing from agents[] is always accurate.
        spent = self._sum_agent_tokens()

        remaining = max(0, ceiling - spent)
        allowed = remaining >= per_agent_ceiling
        warn = spent > (warn_pct / 100) * ceiling

        return {
            "allowed": allowed,
            "remaining": remaining,
            "ceiling": ceiling,
            "spent": spent,
            "warn": warn,
            "agent_role": agent_role,
        }

    def record_spend(
        self,
        agent_id: str,
        agent_role: str,
        input_tokens: int,
        output_tokens: int,
        discussion: int | None = None,
        model: str = "default",
        event_id: str | None = None,
        pr: int | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """
        Atomically increment session_spent and write the per-agent spend record.

        Uses CAS with one retry on conflict. If both attempts fail (extremely
        rare), CAS increment is best-effort; RuntimeError silently ignored. Publishes a BudgetSpendEvent to the bus
        after recording.

        Args:
            model: Model name used by this agent (e.g. 'claude-sonnet-4-20250514').
                   Used by CostTracker to compute dollar amounts. Defaults to 'default'.
            event_id: Optional idempotency key. If provided and already seen in
                      the sqlite dedup table, this call is a no-op (returns silently).
            pr: Optional PR number to tag this spend record for per-PR cost rollup.
            cache_read_tokens: Tokens read from cache (stored for cost_tracker pricing).
            cache_write_tokens: Tokens written to cache (stored for cost_tracker pricing).
        """
        # Idempotency check — skip if this event_id has already been recorded
        if event_id and _check_seen(event_id, "budget"):
            return

        total = input_tokens + output_tokens

        # Write per-agent record FIRST so _sum_agent_tokens() is always accurate
        # even when the session_spent CAS increment below fails under concurrency.
        # Previously the CAS was first, so a CAS failure (RuntimeError) prevented
        # the agent record from being written — leaving spent=0 in get_status().
        record: dict = {
            "agent": agent_role,
            "agent_id": agent_id,
            "input": input_tokens,
            "output": output_tokens,
            "total": total,
            "model": model,
            "finished": _now_iso(),
        }
        if discussion is not None:
            record["discussion"] = discussion
        if pr is not None:
            record["pr"] = pr
        if cache_read_tokens:
            record["cache_read_tokens"] = cache_read_tokens
        if cache_write_tokens:
            record["cache_write_tokens"] = cache_write_tokens

        agent_key = f"{_AGENTS_PREFIX}{agent_id}"
        self._bb.write(agent_key, record, updated_by="budget-tracker")

        # Increment session_spent (best-effort — get_status() derives spent from agents[],
        # so a CAS failure here does not affect the reported total).
        try:
            self._cas_increment(_KEY_SESSION_SPENT, total)
        except RuntimeError:
            pass  # agents[] already written above; spent sum remains correct

        # Publish spend event to the bus (best-effort — never raise on failure).
        try:
            from backend.event_bus import BudgetSpendEvent, get_bus  # noqa: PLC0415
            get_bus().publish_async(BudgetSpendEvent(
                source="budget-tracker",
                agent_id=agent_id,
                role=agent_role,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                discussion=discussion,
                model=model,
            ))
        except Exception:  # noqa: BLE001
            pass

    def get_status(self) -> dict:
        """
        Return current budget state as a dict.

        Returns:
            {
                "ceiling": int,
                "spent": int,
                "remaining": int,
                "warn_threshold_pct": int,
                "agents": [<per-agent records>],
            }
        """
        ceiling = self._read_int(_KEY_SESSION_CEILING, self._config["session_ceiling"])
        per_agent_ceiling = self._read_int(_KEY_PER_AGENT_CEILING, self._config["per_agent_ceiling"])

        agent_keys = self._bb.list_keys(_AGENTS_PREFIX)
        agents = []
        for key in agent_keys:
            val = self._bb.read(key)
            if val is not None:
                agents.append(val)

        # Derive spent by summing tokens across all agent entries. The session_spent
        # blackboard key silently drops increments under concurrent CAS conflicts —
        # post-agent-hook.sh swallows RuntimeError, so the counter stays at 0 even
        # after hundreds of recorded runs. The agents[] array is always correct.
        spent = sum(
            (a.get("input", 0) or 0) + (a.get("output", 0) or 0)
            for a in agents
        )
        remaining = max(0, ceiling - spent)

        return {
            "ceiling": ceiling,
            "spent": spent,
            "remaining": remaining,
            "per_agent_ceiling": per_agent_ceiling,
            "warn_threshold_pct": self._config["warn_threshold_pct"],
            "agents": agents,
        }

    def reset(self) -> None:
        """Delete all budget/ keys from the blackboard (for session restart)."""
        keys = self._bb.list_keys("budget/")
        for key in keys:
            self._bb.delete(key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sum_agent_tokens(self) -> int:
        """Sum input + output tokens across all recorded agent entries."""
        agent_keys = self._bb.list_keys(_AGENTS_PREFIX)
        total = 0
        for key in agent_keys:
            val = self._bb.read(key)
            if val is not None:
                total += (val.get("input", 0) or 0) + (val.get("output", 0) or 0)
        return total

    def _read_int(self, key: str, default: int) -> int:
        """Read a blackboard key as int, returning default if missing or wrong type."""
        val = self._bb.read(key)
        if isinstance(val, int):
            return val
        return default

    def _cas_increment(self, key: str, delta: int, max_retries: int = 1) -> None:
        """
        Atomically increment the int value stored at *key* by *delta*.

        Reads current value + version, computes new total, attempts CAS.
        Retries once on conflict. Raises RuntimeError if both attempts fail.

        If key doesn't exist yet, initializes to delta.
        """
        for attempt in range(max_retries + 1):
            entry = self._bb.read_entry(key)
            if entry is None:
                # Key doesn't exist — write initial value.
                ok = self._bb.write(key, delta, updated_by="budget-tracker")
                if ok:
                    return
                # write() doesn't fail the same way CAS does; if we're here it worked
                return

            current_value = entry.get("value", 0)
            if not isinstance(current_value, int):
                current_value = 0
            new_value = current_value + delta
            current_version = entry.get("version", 1)

            ok = self._bb.cas(key, new_value, current_version, updated_by="budget-tracker")
            if ok:
                return

            if attempt < max_retries:
                # Brief pause before retry to reduce contention.
                time.sleep(0.05)

        raise RuntimeError(
            f"CAS conflict on '{key}' after {max_retries + 1} attempts — could not increment"
        )


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="budget",
        description="Token budget tracking for the autonomous team.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # init
    init_p = sub.add_parser("init", help="Initialize session budget from config (or --ceiling)")
    init_p.add_argument(
        "--ceiling",
        type=int,
        default=None,
        metavar="N",
        help="Override session ceiling (default: from config)",
    )

    # check
    sub.add_parser(
        "check",
        help="Print budget status; exit 0 if under budget, exit 1 if over",
    )

    # spend
    spend_p = sub.add_parser("spend", help="Record token usage for a completed agent")
    spend_p.add_argument("agent_id", help="Unique agent identifier (e.g. executor-14-1712700000)")
    spend_p.add_argument("role", help="Agent role (e.g. executor, code-reviewer)")
    spend_p.add_argument("input_tokens", type=int)
    spend_p.add_argument("output_tokens", type=int)
    spend_p.add_argument("--discussion", type=int, default=None, metavar="N")
    spend_p.add_argument("--model", default="default", metavar="MODEL",
                         help="Model name for cost tracking (default: 'default')")
    spend_p.add_argument("--event-id", default=None, metavar="ID",
                         help="Idempotency key — skip recording if this event_id was already seen")
    spend_p.add_argument("--pr", type=int, default=None, metavar="N",
                         help="Optional PR number to tag this spend record for per-PR cost rollup")
    spend_p.add_argument("--cache-read-tokens", type=int, default=0, metavar="N",
                         help="Tokens read from cache (stored for cost_tracker pricing)")
    spend_p.add_argument("--cache-write-tokens", type=int, default=0, metavar="N",
                         help="Tokens written to cache (stored for cost_tracker pricing)")

    # record — idempotent version of spend with named args (preferred for hook scripts)
    record_p = sub.add_parser("record", help="Idempotent token recording (use --event-id for dedup)")
    record_p.add_argument("--input-tokens", type=int, default=0, metavar="N")
    record_p.add_argument("--output-tokens", type=int, default=0, metavar="N")
    record_p.add_argument("--role", default="unknown", metavar="ROLE")
    record_p.add_argument("--discussion", type=int, default=None, metavar="N")
    record_p.add_argument("--model", default="default", metavar="MODEL")
    record_p.add_argument("--event-id", default=None, metavar="ID",
                          help="Idempotency key — skip if already seen in seen.sqlite")

    # status
    sub.add_parser("status", help="Print current budget state as JSON")

    # reset
    sub.add_parser("reset", help="Remove all budget/ keys from blackboard")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    bt = BudgetTracker()

    if args.command == "init":
        try:
            bt.init_session(ceiling=args.ceiling)
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        status = bt.get_status()
        print(f"initialized: ceiling={status['ceiling']}, spent=0, per_agent_ceiling={status['per_agent_ceiling']}")
        return 0

    if args.command == "check":
        try:
            result = bt.check_budget("cli")
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        if result["warn"]:
            print(
                f"WARNING: spent {result['spent']:,} of {result['ceiling']:,} tokens "
                f"({result['spent'] * 100 // max(result['ceiling'], 1)}%)",
                file=sys.stderr,
            )
        if not result["allowed"]:
            print(
                f"BUDGET EXCEEDED: remaining {result['remaining']:,} < per_agent_ceiling",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.command == "spend":
        try:
            bt.record_spend(
                agent_id=args.agent_id,
                agent_role=args.role,
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
                discussion=args.discussion,
                model=getattr(args, "model", "default"),
                event_id=getattr(args, "event_id", None),
                pr=getattr(args, "pr", None),
                cache_read_tokens=getattr(args, "cache_read_tokens", 0) or 0,
                cache_write_tokens=getattr(args, "cache_write_tokens", 0) or 0,
            )
        except (LockTimeout, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        total = args.input_tokens + args.output_tokens
        print(f"recorded: {args.agent_id} spent {total:,} tokens ({args.input_tokens:,} in + {args.output_tokens:,} out)")
        return 0

    if args.command == "record":
        event_id = getattr(args, "event_id", None)
        agent_id = f"record-{getattr(args, 'role', 'unknown')}-{int(time.time())}"
        # record_spend handles event_id dedup internally via _check_seen
        # — do NOT call _check_seen here, it would insert the event_id and
        #   then record_spend would see it as a duplicate and skip the increment.
        try:
            # Capture whether it was a dedup-skip by checking before/after session_spent
            was_seen = event_id and _check_seen(event_id, "budget")
            if was_seen:
                print(f"skipped (duplicate event_id={event_id})")
                return 0
            bt.record_spend(
                agent_id=agent_id,
                agent_role=getattr(args, "role", "unknown"),
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
                discussion=args.discussion,
                model=getattr(args, "model", "default"),
                # Don't pass event_id here — _check_seen was already called above
                # and would double-check, causing a false duplicate on first call.
                # event_id dedup is handled by the was_seen check above.
            )
        except (LockTimeout, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        total = args.input_tokens + args.output_tokens
        print(f"recorded: {agent_id} spent {total:,} tokens ({args.input_tokens:,} in + {args.output_tokens:,} out)")
        return 0

    if args.command == "status":
        try:
            status = bt.get_status()
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(status, indent=2))
        return 0

    if args.command == "reset":
        try:
            bt.reset()
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("reset: all budget/ keys removed")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
