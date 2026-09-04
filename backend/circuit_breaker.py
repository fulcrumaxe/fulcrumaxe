"""
Agent failure circuit breaker — stop re-spawning after repeated failures.

Tracks consecutive agent failures per Discussion in the blackboard under the
`failures/` namespace. After 3 consecutive failures the circuit opens and the
Team Lead skips spawning agents for that Discussion until manually reset.

State transitions are persisted to `.autonomous-team/circuit-breaker-history.jsonl`
so operators can see the timeline of trips and resets over time.

Usage (CLI):
    python backend/circuit_breaker.py status [discussion_number]
    python backend/circuit_breaker.py reset <discussion_number>
    python backend/circuit_breaker.py record <discussion_number> <agent> <reason>
    python backend/circuit_breaker.py list
    python backend/circuit_breaker.py summary --json
    python backend/circuit_breaker.py history --role <role> [--limit 20]
    python backend/circuit_breaker.py expire [--dry-run]

Usage (library):
    from backend.circuit_breaker import record_failure, record_success, is_blocked
    record_failure(97, "executor", "could not implement")
    record_success(97)
    if is_blocked(97):
        ...
"""

import argparse
import fcntl
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a script from repo root: `python backend/circuit_breaker.py ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard, LockTimeout  # noqa: E402
from backend._repo import REPO_OWNER as _REPO_OWNER, REPO_NAME as _REPO_NAME  # noqa: E402

DEFAULT_THRESHOLD = 3

# Number of days after which a tripped breaker is considered age-stale and
# eligible for auto-expiry, regardless of the Discussion's open/closed state.
STALE_BREAKER_DAYS = 7

_bb = Blackboard()

# Path to the append-only transition history log
_HISTORY_FILE = Path(__file__).resolve().parent.parent / ".autonomous-team" / "circuit-breaker-history.jsonl"


def _key(discussion: int) -> str:
    return f"failures/{discussion}"


def _meta_key(discussion: int) -> str:
    return f"failures_meta/{discussion}"


def _append_history(
    role: str,
    from_state: str,
    to_state: str,
    reason: str,
    context: dict,
    last_pr: int | None = None,
) -> None:
    """Append one transition line to the history JSONL file (atomic via O_APPEND + flock)."""
    line = {
        "role": role,
        "from_state": from_state,
        "to_state": to_state,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "reason": reason,
        "context": context,
        "last_pr": last_pr,
    }
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_APPEND for atomic multi-process writes; flock provides extra safety
    with open(_HISTORY_FILE, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(line) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def history(role: str, limit: int = 20) -> list[dict]:
    """Return up to *limit* most-recent transitions for *role* (newest last)."""
    if not _HISTORY_FILE.exists():
        return []
    matches: list[dict] = []
    with open(_HISTORY_FILE) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("role") == role:
                matches.append(entry)
    # Return the last *limit* entries (already chronological order)
    return matches[-limit:]


def record_failure(discussion: int, agent: str, reason: str, last_pr: int | None = None) -> int:
    """
    Increment the consecutive failure counter for *discussion*.

    Returns the new failure count. The blackboard keys are:
    - ``failures/{discussion}`` — integer count (existing)
    - ``failures_meta/{discussion}`` — {count, agent, reason, updated_at} (new)

    Emits a JSONL transition line when the count first crosses DEFAULT_THRESHOLD
    (healthy → tripped).
    """
    key = _key(discussion)
    current = _bb.read(key) or 0
    new_count = current + 1
    _bb.write(key, new_count, updated_by=f"circuit-breaker/{agent}")
    # Write metadata sidecar for operator-facing displays
    meta = {
        "count": new_count,
        "agent": agent,
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    _bb.write(_meta_key(discussion), meta, updated_by=f"circuit-breaker/{agent}")
    # Emit history transition on threshold crossing
    if current < DEFAULT_THRESHOLD <= new_count:
        _append_history(
            role=agent,
            from_state="healthy",
            to_state="tripped",
            reason=reason,
            context={"recent_errors": [reason], "trip_count_24h": new_count},
            last_pr=last_pr,
        )
    return new_count


def record_success(discussion: int, agent: str = "unknown", last_pr: int | None = None) -> None:
    """Reset the failure counter for *discussion* (delete both keys).

    Emits a JSONL transition line only if the circuit was previously tripped
    (tripped → healthy).
    """
    was_tripped = is_blocked(discussion)
    _bb.delete(_key(discussion))
    _bb.delete(_meta_key(discussion))
    if was_tripped:
        _append_history(
            role=agent,
            from_state="tripped",
            to_state="healthy",
            reason="reset after success",
            context={"recent_errors": []},
            last_pr=last_pr,
        )


def is_blocked(discussion: int, threshold: int = DEFAULT_THRESHOLD) -> bool:
    """Return True if the failure count for *discussion* >= *threshold*."""
    count = _bb.read(_key(discussion)) or 0
    return count >= threshold


def get_latest_failure(discussion: int) -> dict | None:
    """Return the most recent failure record for *discussion*, or None if none exists.

    The returned dict contains: {count, agent, reason, updated_at}.
    Returns None when no failures have been recorded for this Discussion.
    """
    count = _bb.read(_key(discussion)) or 0
    if count == 0:
        return None
    meta = _bb.read(_meta_key(discussion))
    if not meta or not isinstance(meta, dict):
        # Count exists but metadata is missing — return minimal record.
        return {"count": count, "agent": None, "reason": None, "updated_at": None}
    return {
        "count": meta.get("count", count),
        "agent": meta.get("agent"),
        "reason": meta.get("reason"),
        "updated_at": meta.get("updated_at"),
    }


def _collect_tripped(threshold: int = DEFAULT_THRESHOLD) -> list[dict]:
    """Return a list of dicts for every Discussion with an active failure counter.

    Each dict contains: {discussion, count, agent, reason, updated_at}.
    Fields from the metadata sidecar may be None if the sidecar is missing.
    """
    keys = _bb.list_keys("failures/")
    result: list[dict] = []
    for key in keys:
        count = _bb.read(key) or 0
        disc_str = key.split("/", 1)[1]
        try:
            disc_num = int(disc_str)
        except ValueError:
            continue
        meta = _bb.read(_meta_key(disc_num)) or {}
        if isinstance(meta, dict):
            agent = meta.get("agent")
            reason = meta.get("reason")
            updated_at = meta.get("updated_at")
        else:
            agent = reason = updated_at = None
        result.append({
            "discussion": disc_num,
            "count": count,
            "agent": agent,
            "reason": reason,
            "updated_at": updated_at,
            "blocked": count >= threshold,
        })
    return result


def _age_str(updated_at: str | None) -> str:
    """Return a human-readable age like '5m ago' or '' when updated_at is None."""
    if not updated_at:
        return ""
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------
# Auto-expiry helpers
# ------------------------------------------------------------------


def _discussion_state(discussion: int) -> str:
    """Return the open/closed/absent state of a GitHub Discussion.

    Return values:
        "open"    — Discussion exists and is open.
        "closed"  — Discussion exists and is closed.
        "absent"  — Discussion does not exist (NOT_FOUND).
        "unknown" — GitHub unreachable, no auth, or any other error.

    Designed to be monkeypatched in tests so no live GitHub calls are needed.
    """
    query = """
    query($owner:String!, $repo:String!, $num:Int!) {
      repository(owner:$owner, name:$repo) {
        discussion(number:$num) { closed }
      }
    }
    """
    try:
        result = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", f"query={query}",
                "-f", f"owner={_REPO_OWNER}",
                "-f", f"repo={_REPO_NAME}",
                "-F", f"num={discussion}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Parse stdout BEFORE checking returncode.  When a discussion does not
        # exist, gh exits 1 but still emits a valid JSON body with
        # data.repository.discussion=null and errors[].type=NOT_FOUND.
        # We need to read that body to distinguish "confirmed absent" from a
        # genuine lookup failure (network down, no auth, timeout, etc.).
        stdout = (result.stdout or "").strip()
        if not stdout:
            # Nothing to parse — genuine network/auth failure.
            return "unknown"
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # Unparseable output — treat as lookup failure, not confirmed absent.
            return "unknown"

        # GraphQL errors surface in the "errors" key (e.g. NOT_FOUND).
        errors = data.get("errors") or []
        not_found_errors = [
            err for err in errors
            if "NOT_FOUND" in str(err.get("type", ""))
            or "Could not resolve" in str(err.get("message", ""))
        ]
        other_errors = [err for err in errors if err not in not_found_errors]

        if not_found_errors:
            # GitHub confirmed the discussion does not exist.
            return "absent"

        if other_errors:
            # Non-NOT_FOUND errors (rate-limit, permission, …) alongside a null
            # discussion node — we cannot confirm absence; hold fail-safe.
            return "unknown"

        # No errors: inspect the discussion node.
        disc = (data.get("data") or {}).get("repository", {}).get("discussion")
        if disc is None:
            # Null node with no errors is also confirmed absent.
            return "absent"
        return "closed" if disc.get("closed") else "open"
    except Exception:  # noqa: BLE001
        return "unknown"


def expire_stale(
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Scan all tripped breakers and clear those that are stale by policy.

    Expiry rule (fail-safe direction — on uncertainty, HOLD):

        expire IFF (real parseable timestamp ≥ STALE_BREAKER_DAYS old)
                 OR (Discussion is closed or absent)

    A missing or unparseable ``updated_at`` timestamp is NOT treated as
    age-eligible when the Discussion is open or unknown.  The timestamp alone
    provides no reliable evidence the failure is resolved; a still-open
    Discussion is evidence it may still be active.  In that case the breaker
    is held until the Discussion closes or a real timestamp ages out.

    The only exception: when the Discussion is definitively closed or absent,
    expiry fires regardless of the timestamp (closed Discussion is independent,
    reliable evidence the breaker is stale).

    Parameters
    ----------
    now:
        Reference timestamp (UTC-aware). Defaults to ``datetime.now(timezone.utc)``.
        Provided in tests to make age calculations deterministic.
    dry_run:
        If True, return the eligible list without mutating any blackboard keys.

    Returns
    -------
    list[dict]
        One entry per expired breaker: ``{discussion, reason}``.
        ``reason`` ∈ ``age``, ``closed``, ``absent``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now - timedelta(days=STALE_BREAKER_DAYS)
    expired: list[dict] = []

    # Cache per-discussion state lookups across multiple breakers.
    _state_cache: dict[int, str] = {}

    for entry in _collect_tripped():
        if not entry.get("blocked"):
            continue

        disc = entry["discussion"]
        updated_at_raw: str | None = entry.get("updated_at")

        # --- Age check ---
        # Fail-safe: a missing/unparseable timestamp is NOT age-eligible when the
        # Discussion is open/unknown — we have no reliable evidence the failure is
        # resolved.  Only a definitively closed/absent Discussion overrides that.
        age_eligible = False
        if updated_at_raw is not None:
            try:
                ts = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
                age_eligible = ts < cutoff
            except Exception:  # noqa: BLE001
                pass  # unparseable timestamp → not age-eligible; rely on Discussion state

        # --- Discussion state check ---
        if disc not in _state_cache:
            _state_cache[disc] = _discussion_state(disc)
        state = _state_cache[disc]

        # Determine expiry reason (highest priority wins: closed/absent > age).
        # Per the expiry rule: missing/malformed timestamp → not age_eligible,
        # so a missing-ts + open/unknown Discussion falls through to else → HOLD.
        if state == "closed":
            reason = "closed"
        elif state == "absent":
            reason = "absent"
        elif age_eligible:
            # Age-only expiry: real parseable timestamp older than STALE_BREAKER_DAYS.
            # Discussion is open or unknown — age threshold overrides fail-safe.
            reason = "age"
        else:
            # Not age-eligible (recent or missing/malformed ts) + open/unknown → HOLD.
            continue

        expired.append({"discussion": disc, "reason": reason})

        if not dry_run:
            _bb.delete(_key(disc))
            _bb.delete(_meta_key(disc))
            _append_history(
                role="circuit-breaker/auto-expire",
                from_state="tripped",
                to_state="expired",
                reason=f"auto-expired: {reason}",
                context={"expiry_reason": reason, "updated_at": updated_at_raw},
            )

    return expired


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="circuit_breaker",
        description="Agent failure circuit breaker for the autonomous team.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    status_p = sub.add_parser(
        "status",
        help="Print failure count. With DISCUSSION: single counter. Without: all active counters.",
    )
    status_p.add_argument("discussion", type=int, nargs="?", default=None)

    reset_p = sub.add_parser("reset", help="Clear failure counter for DISCUSSION")
    reset_p.add_argument("discussion", type=int)

    record_p = sub.add_parser("record", help="Record a failure for DISCUSSION")
    record_p.add_argument("discussion", type=int)
    record_p.add_argument("agent", help="Agent role that failed (e.g. executor)")
    record_p.add_argument("reason", help="Short reason string (e.g. the verdict)")

    sub.add_parser("list", help="List all Discussions with active failure counters")

    summary_p = sub.add_parser("summary", help="Emit structured summary (machine-readable)")
    summary_p.add_argument("--json", action="store_true", dest="json_output",
                           help="Output JSON (required for machine consumers)")

    history_p = sub.add_parser("history", help="Print recent state transitions for a role")
    history_p.add_argument("--role", required=True,
                           help="Agent role to filter (e.g. executor, code-reviewer)")
    history_p.add_argument("--limit", type=int, default=20,
                           help="Maximum transitions to display, newest last (default: 20)")

    expire_p = sub.add_parser(
        "expire",
        help=(
            f"Auto-expire stale breakers (older than {STALE_BREAKER_DAYS}d "
            "or tied to a closed/absent Discussion)"
        ),
    )
    expire_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview without clearing any breakers",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        if args.discussion is not None:
            # Single-counter mode (existing behaviour)
            count = _bb.read(_key(args.discussion)) or 0
            blocked = count >= DEFAULT_THRESHOLD
            print(count)
            if blocked:
                print(
                    f"circuit open: Discussion #{args.discussion} has {count} consecutive failures",
                    file=sys.stderr,
                )
            return 0
        # No-arg mode: list all active counters
        entries = _collect_tripped()
        if not entries:
            print("no active failure counters")
            return 0
        for e in entries:
            age = _age_str(e.get("updated_at"))
            age_part = f" ({age})" if age else ""
            agent_part = f"{e['agent']}: {e['reason']}" if e.get("agent") else "unknown"
            blocked_part = " [BLOCKED]" if e["blocked"] else ""
            print(f"#{e['discussion']}: {e['count']} failures — {agent_part}{age_part}{blocked_part}")
        return 0

    if args.command == "reset":
        try:
            removed = _bb.delete(_key(args.discussion))
            _bb.delete(_meta_key(args.discussion))
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if removed:
            print(f"reset: Discussion #{args.discussion} failure counter cleared")
        else:
            print(f"ok: Discussion #{args.discussion} had no active failure counter")
        return 0

    if args.command == "record":
        try:
            new_count = record_failure(args.discussion, args.agent, args.reason)
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"failures/{args.discussion} = {new_count}")
        if new_count >= DEFAULT_THRESHOLD:
            print(
                f"circuit open: Discussion #{args.discussion} now has {new_count} consecutive failures",
                file=sys.stderr,
            )
        return 0

    if args.command == "list":
        keys = _bb.list_keys("failures/")
        if not keys:
            print("no active failure counters")
            return 0
        for key in keys:
            count = _bb.read(key) or 0
            disc_num = key.split("/", 1)[1]
            blocked = " [BLOCKED]" if count >= DEFAULT_THRESHOLD else ""
            print(f"Discussion #{disc_num}: {count} consecutive failure(s){blocked}")
        return 0

    if args.command == "summary":
        tripped = _collect_tripped()
        # Only include actually-tripped entries (count >= threshold) in the
        # "tripped" list; warnings contains near-threshold ones.
        tripped_list = [e for e in tripped if e["blocked"]]
        warnings_list = [
            e for e in tripped if not e["blocked"] and e["count"] > 0
        ]
        output = {
            "tripped": [
                {
                    "discussion": e["discussion"],
                    "count": e["count"],
                    "agent": e["agent"],
                    "reason": e["reason"],
                    "updated_at": e["updated_at"],
                }
                for e in tripped_list
            ],
            "warnings": [
                {
                    "discussion": e["discussion"],
                    "count": e["count"],
                    "agent": e["agent"],
                    "reason": e["reason"],
                    "updated_at": e["updated_at"],
                }
                for e in warnings_list
            ],
            "threshold": DEFAULT_THRESHOLD,
        }
        print(json.dumps(output))
        return 0

    if args.command == "history":
        entries = history(args.role, args.limit)
        if not entries:
            print(f"no transitions recorded for role {args.role}")
            return 0
        # Pretty table: time | from → to | reason | last_pr
        header = f"{'time':<25} {'transition':<22} {'reason':<45} {'last_pr'}"
        print(header)
        print("-" * len(header))
        for e in entries:
            ts = e.get("timestamp", "")[:19]  # trim sub-second / timezone
            transition = f"{e.get('from_state','?')} → {e.get('to_state','?')}"
            reason = (e.get("reason") or "")[:44]
            pr = str(e.get("last_pr") or "")
            print(f"{ts:<25} {transition:<22} {reason:<45} {pr}")
        return 0

    if args.command == "expire":
        results = expire_stale(dry_run=args.dry_run)
        if not results:
            print("no stale breakers found")
            return 0
        mode = "[dry-run] " if args.dry_run else ""
        for item in results:
            print(f"{mode}expired: #{item['discussion']} ({item['reason']})")
        print(f"{mode}{len(results)} breaker(s) expired")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
