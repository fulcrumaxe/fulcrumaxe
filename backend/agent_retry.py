"""
Agent retry with exponential backoff — layer on top of the circuit breaker.

When an agent returns a fail/needs-fix verdict, the retry manager decides
whether to re-attempt before recording a failure with the circuit breaker.
Retry attempts use exponential backoff with a configurable policy, loaded
from config.json under ``policies.<role>.retry``.

Usage (CLI):
    python backend/agent_retry.py check <discussion> <role>
    python backend/agent_retry.py clear <discussion>

Usage (library):
    from backend.agent_retry import should_retry, record_attempt, clear_retries, load_retry_policy
    policy = load_retry_policy("executor")
    state = record_attempt(14, "executor")
    decision = should_retry(14, "executor", state["attempt"] - 1, policy)
    if decision.retry:
        time.sleep(decision.delay_seconds)
        # re-spawn agent
    else:
        record_failure(14, "executor", "max retries exceeded")
        clear_retries(14)
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root: `python backend/agent_retry.py ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard, LockTimeout  # noqa: E402

_DEFAULT_CONFIG_PATH = Path(".autonomous-team/config.json")

_bb = Blackboard()


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Configuration for retry behaviour for a single agent role."""

    max_retries: int = 2
    base_delay_seconds: float = 30.0
    max_delay_seconds: float = 300.0
    backoff_factor: float = 2.0


@dataclass
class RetryDecision:
    """Result of a should_retry() call."""

    retry: bool
    delay_seconds: float
    reason: str


# ------------------------------------------------------------------
# Core logic
# ------------------------------------------------------------------


def should_retry(
    discussion: int,
    agent_role: str,
    attempt: int,
    policy: RetryPolicy,
) -> RetryDecision:
    """
    Decide whether to retry based on the current attempt index and policy.

    *attempt* is zero-based: 0 means "we already made one attempt and it
    failed; should we make a first retry?".  When attempt >= max_retries
    we return retry=False.

    Delay calculation: min(base_delay * backoff_factor^attempt, max_delay).
    For attempt=0 that gives base_delay (2.0^0 == 1).
    For attempt=1 that gives base_delay * backoff_factor.
    """
    if attempt >= policy.max_retries:
        return RetryDecision(
            retry=False,
            delay_seconds=0.0,
            reason=f"max_retries ({policy.max_retries}) reached for {agent_role} on Discussion #{discussion}",
        )

    raw_delay = policy.base_delay_seconds * (policy.backoff_factor ** attempt)
    delay = min(raw_delay, policy.max_delay_seconds)

    return RetryDecision(
        retry=True,
        delay_seconds=delay,
        reason=(
            f"retry {attempt + 1}/{policy.max_retries} for {agent_role} "
            f"on Discussion #{discussion} — wait {delay:.0f}s"
        ),
    )


def load_retry_policy(role: str) -> RetryPolicy:
    """
    Load the RetryPolicy for *role* from config.json.

    Looks under ``policies.<role>.retry`` for any of the four fields.
    Falls back to RetryPolicy defaults for any key that is absent or if
    the config file cannot be read.
    """
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    config_path = repo_root / _DEFAULT_CONFIG_PATH

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        retry_cfg = cfg.get("policies", {}).get(role, {}).get("retry", {})
    except (OSError, json.JSONDecodeError):
        retry_cfg = {}

    return RetryPolicy(
        max_retries=retry_cfg.get("max_retries", RetryPolicy.max_retries),
        base_delay_seconds=retry_cfg.get("base_delay_seconds", RetryPolicy.base_delay_seconds),
        max_delay_seconds=retry_cfg.get("max_delay_seconds", RetryPolicy.max_delay_seconds),
        backoff_factor=retry_cfg.get("backoff_factor", RetryPolicy.backoff_factor),
    )


# ------------------------------------------------------------------
# Blackboard tracking
# ------------------------------------------------------------------


def _retry_key(discussion: int) -> str:
    return f"retries/{discussion}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_attempt(discussion: int, agent_role: str) -> dict:
    """
    Increment the attempt counter for *discussion* in the blackboard.

    Blackboard key: ``retries/{discussion}``
    Value: ``{"attempt": N, "last_attempt": "ISO", "agent": "role"}``

    Returns the updated record dict.
    """
    key = _retry_key(discussion)
    current = _bb.read(key) or {}
    new_attempt = current.get("attempt", 0) + 1
    record = {
        "attempt": new_attempt,
        "last_attempt": _now_iso(),
        "agent": agent_role,
    }
    _bb.write(key, record, updated_by=f"agent-retry/{agent_role}")
    return record


def clear_retries(discussion: int) -> None:
    """
    Remove retry tracking for *discussion* from the blackboard.

    Called on success so stale attempt counts don't carry over.
    """
    _bb.delete(_retry_key(discussion))


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent_retry",
        description="Agent retry manager with exponential backoff.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser(
        "check",
        help="Print whether a retry should happen and the delay for DISCUSSION / ROLE",
    )
    check_p.add_argument("discussion", type=int)
    check_p.add_argument("role", help="Agent role (e.g. executor)")

    clear_p = sub.add_parser("clear", help="Clear retry state for DISCUSSION")
    clear_p.add_argument("discussion", type=int)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        try:
            current = _bb.read(_retry_key(args.discussion)) or {}
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1

        attempt = current.get("attempt", 0)
        policy = load_retry_policy(args.role)
        decision = should_retry(args.discussion, args.role, attempt, policy)

        if decision.retry:
            print(f"retry: yes — delay {decision.delay_seconds:.0f}s — {decision.reason}")
        else:
            print(f"retry: no — {decision.reason}")
        return 0

    if args.command == "clear":
        try:
            clear_retries(args.discussion)
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"cleared: retry state for Discussion #{args.discussion} removed")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
