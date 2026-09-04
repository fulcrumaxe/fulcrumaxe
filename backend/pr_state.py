"""
PR lifecycle state machine backed by the blackboard.

Tracks each open PR's phase from initial queuing through merging.
Stored under blackboard key ``pr_state/{pr_number}``.

Mutation locus
--------------
All mutation entry points (``set``, ``advance``, ``record-envelope``, ``init``)
are **Team-Lead-inline only** — they are invoked from /loop iteration shell,
never from inside a sub-agent context. Agent-context writes are forbidden
because they risk opaque flock contention on the blackboard SQLite WAL and
produce non-deterministic phase advances. Sub-agents return envelopes; Team
Lead parses envelopes and writes pr_state. Read-only ``get`` and ``list``
from agent contexts are permitted.

CLI usage
---------
    python3 backend/pr_state.py init 547 --discussion 559
    python3 backend/pr_state.py get 547
    python3 backend/pr_state.py list
    python3 backend/pr_state.py list --phase executing
    python3 backend/pr_state.py list --blocked
    python3 backend/pr_state.py list --stale
    python3 backend/pr_state.py advance 547 --to code_review
    python3 backend/pr_state.py set 547 --phase merging --field needs_security_review=true
    python3 backend/pr_state.py record-envelope 547 --role executor --verdict done \\
        --input-tokens 50000 --output-tokens 8000

Schema (stored as blackboard value)
-------------------------------------
{
    "pr": 547,
    "discussion": 559,
    "phase": "executing",
    "spawned_phases": [{"role": "executor", "at": "ISO8601", "event_id": "..."}],
    "completed_phases": [{"role": "executor", "at": "ISO8601", "verdict": "done"}],
    "needs_security_review": false,
    "fix_cycle_count": 0,
    "debate_cycle_count": 0,
    "respawn_count": 0,
    "last_envelope": {},
    "blocked_reason": null,
    "created_at": "ISO8601",
    "updated_at": "ISO8601"
}

Phase machine
-------------
Valid transitions:
    queued -> executing
    executing -> code_review
    executing -> blocked
    code_review -> debate
    code_review -> security_review
    code_review -> merging
    code_review -> executing   (fix cycle)
    code_review -> blocked
    debate -> security_review
    debate -> executing   (fix cycle after debater needs-fix)
    debate -> blocked
    security_review -> merging
    security_review -> executing   (fix cycle)
    security_review -> blocked
    merging -> merged
    merging -> blocked
    * -> blocked   (from any phase)

Terminal phases: merged, blocked
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard  # noqa: E402

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

_KEY_PREFIX = "pr_state"
_STALE_THRESHOLD_SECONDS = 60 * 60  # 60 minutes

VALID_PHASES = frozenset(
    [
        "queued",
        "executing",
        "code_review",
        "debate",
        "security_review",
        "merging",
        "merged",
        "blocked",
    ]
)

TERMINAL_PHASES = frozenset(["merged", "blocked"])

# Allowed transitions: {from_phase: set_of_to_phases}
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued":          frozenset(["executing", "blocked"]),
    "executing":       frozenset(["code_review", "blocked"]),
    "code_review":     frozenset(["debate", "security_review", "merging", "executing", "blocked"]),
    "debate":          frozenset(["security_review", "executing", "blocked"]),
    "security_review": frozenset(["merging", "executing", "blocked"]),
    "merging":         frozenset(["merged", "blocked"]),
    "merged":          frozenset(),
    "blocked":         frozenset(),
}


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bb_key(pr: int) -> str:
    return f"{_KEY_PREFIX}/{pr}"


def _get_bb() -> Blackboard:
    """Return a file-backed Blackboard; import-path safe."""
    return Blackboard()


# -----------------------------------------------------------------------
# Library API
# -----------------------------------------------------------------------

def init_entry(pr: int, discussion: int, bb: Blackboard | None = None) -> dict:
    """
    Create a new pr_state entry in the queued phase.

    Raises ValueError if an entry already exists for this PR.
    """
    if bb is None:
        bb = _get_bb()
    key = _bb_key(pr)
    existing = bb.read(key)
    if existing is not None:
        raise ValueError(f"pr_state entry already exists for PR #{pr}")

    now = _now_iso()
    entry: dict[str, Any] = {
        "pr": pr,
        "discussion": discussion,
        "phase": "queued",
        "spawned_phases": [],
        "completed_phases": [],
        "needs_security_review": False,
        "fix_cycle_count": 0,
        "debate_cycle_count": 0,
        "respawn_count": 0,
        "last_envelope": {},
        "blocked_reason": None,
        "created_at": now,
        "updated_at": now,
    }
    bb.write(key, entry, updated_by="pr_state.init")
    return entry


def get_entry(pr: int, bb: Blackboard | None = None) -> dict | None:
    """Return the pr_state entry for *pr*, or None if it doesn't exist."""
    if bb is None:
        bb = _get_bb()
    return bb.read(_bb_key(pr))


def advance(pr: int, to_phase: str, bb: Blackboard | None = None) -> dict:
    """
    Advance the phase of PR *pr* to *to_phase*.

    Raises ValueError on invalid transition or missing entry.
    SystemExit(1) is raised by the CLI handler — library callers get ValueError.
    """
    if bb is None:
        bb = _get_bb()
    if to_phase not in VALID_PHASES:
        raise ValueError(f"Unknown phase: {to_phase!r}. Valid phases: {sorted(VALID_PHASES)}")

    key = _bb_key(pr)
    entry = bb.read(key)
    if entry is None:
        raise ValueError(f"No pr_state entry found for PR #{pr}")

    current = entry["phase"]
    allowed = _TRANSITIONS.get(current, frozenset())
    if to_phase not in allowed:
        raise ValueError(
            f"Invalid transition for PR #{pr}: {current!r} -> {to_phase!r}. "
            f"Allowed from {current!r}: {sorted(allowed) or '(none — terminal phase)'}"
        )

    entry["phase"] = to_phase
    entry["updated_at"] = _now_iso()
    bb.write(key, entry, updated_by="pr_state.advance")
    return entry


def set_fields(
    pr: int,
    phase: str | None = None,
    fields: dict[str, Any] | None = None,
    bb: Blackboard | None = None,
) -> dict:
    """
    Directly set the phase and/or arbitrary top-level fields on a pr_state entry.

    Unlike ``advance``, this does NOT validate the phase transition — use only
    for programmatic corrections or field updates (e.g. needs_security_review).
    """
    if bb is None:
        bb = _get_bb()
    key = _bb_key(pr)
    entry = bb.read(key)
    if entry is None:
        raise ValueError(f"No pr_state entry found for PR #{pr}")

    if phase is not None:
        if phase not in VALID_PHASES:
            raise ValueError(f"Unknown phase: {phase!r}")
        entry["phase"] = phase

    if fields:
        for k, v in fields.items():
            entry[k] = v

    entry["updated_at"] = _now_iso()
    bb.write(key, entry, updated_by="pr_state.set")
    return entry


def record_envelope(
    pr: int,
    role: str,
    verdict: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    event_id: str = "",
    bb: Blackboard | None = None,
) -> dict:
    """
    Record an agent envelope for PR *pr*.

    Appends to ``completed_phases`` and updates ``last_envelope``.
    If verdict is ``needs-fix``, increments ``fix_cycle_count``.
    """
    if bb is None:
        bb = _get_bb()
    key = _bb_key(pr)
    entry = bb.read(key)
    if entry is None:
        raise ValueError(f"No pr_state entry found for PR #{pr}")

    now = _now_iso()
    record = {
        "role": role,
        "at": now,
        "verdict": verdict,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if event_id:
        record["event_id"] = event_id

    entry.setdefault("completed_phases", []).append(record)
    entry["last_envelope"] = record

    if verdict == "needs-fix":
        entry["fix_cycle_count"] = entry.get("fix_cycle_count", 0) + 1

    entry["updated_at"] = now
    bb.write(key, entry, updated_by="pr_state.record-envelope")
    return entry


def list_entries(
    phase: str | None = None,
    blocked: bool = False,
    stale: bool = False,
    discussion: int | None = None,
    bb: Blackboard | None = None,
    now_ts: float | None = None,
) -> list[dict]:
    """
    Return all pr_state entries, with optional filters.

    ``phase``      -- only entries with this phase
    ``blocked``    -- only entries in the ``blocked`` phase
    ``stale``      -- entries where updated_at > 60 min ago AND not terminal
    ``discussion`` -- only entries linked to this Discussion number
    ``now_ts``     -- override for current time (seconds since epoch); used in tests
    """
    if bb is None:
        bb = _get_bb()
    keys = bb.list_keys(_KEY_PREFIX + "/")
    entries = []
    for key in keys:
        entry = bb.read(key)
        if entry is not None:
            entries.append(entry)

    if discussion is not None:
        entries = [e for e in entries if e.get("discussion") == discussion]

    if blocked:
        entries = [e for e in entries if e.get("phase") == "blocked"]
    elif phase is not None:
        entries = [e for e in entries if e.get("phase") == phase]

    if stale:
        if now_ts is None:
            now_ts = time.time()
        result = []
        for e in entries:
            if e.get("phase") in TERMINAL_PHASES:
                continue
            updated = e.get("updated_at", "")
            try:
                # Parse ISO8601 with timezone
                dt = datetime.fromisoformat(updated)
                age = now_ts - dt.timestamp()
                if age > _STALE_THRESHOLD_SECONDS:
                    result.append(e)
            except (ValueError, AttributeError):
                pass
        entries = result

    return sorted(entries, key=lambda e: e.get("pr", 0))


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pr_state",
        description="PR lifecycle phase state machine (Team-Lead-inline mutations only).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # init
    ini = sub.add_parser("init", help="Create a new pr_state entry in 'queued' phase")
    ini.add_argument("pr", type=int, help="PR number")
    ini.add_argument("--discussion", type=int, required=True, help="Discussion number")

    # get
    g = sub.add_parser("get", help="Print pr_state entry as JSON (null if missing)")
    g.add_argument("pr", type=int, help="PR number")

    # set
    s = sub.add_parser("set", help="Directly set phase and/or fields (no transition validation)")
    s.add_argument("pr", type=int, help="PR number")
    s.add_argument("--phase", default=None, help="New phase value")
    s.add_argument(
        "--field",
        action="append",
        dest="fields",
        metavar="key=value",
        help="Set key=value on the entry (value parsed as JSON if possible). Repeatable.",
    )

    # list
    ls = sub.add_parser("list", help="List pr_state entries")
    ls.add_argument("--phase", default=None, help="Filter by phase")
    ls.add_argument("--blocked", action="store_true", help="Show only blocked entries")
    ls.add_argument("--stale", action="store_true", help="Show entries stale > 60 min")
    ls.add_argument("--discussion", type=int, default=None, help="Filter by Discussion number")

    # advance
    adv = sub.add_parser("advance", help="Advance PR to a new phase (validates transition)")
    adv.add_argument("pr", type=int, help="PR number")
    adv.add_argument("--to", dest="to_phase", required=True, help="Target phase")

    # record-envelope
    rec = sub.add_parser("record-envelope", help="Record an agent envelope for a PR")
    rec.add_argument("pr", type=int, help="PR number")
    rec.add_argument("--role", required=True, help="Agent role (e.g. executor)")
    rec.add_argument("--verdict", required=True, help="Envelope verdict")
    rec.add_argument("--input-tokens", type=int, default=0)
    rec.add_argument("--output-tokens", type=int, default=0)
    rec.add_argument("--event-id", default="")

    return p


def _parse_field(kv: str) -> tuple[str, Any]:
    """Parse a key=value string; value is JSON-decoded if possible."""
    if "=" not in kv:
        raise ValueError(f"--field must be key=value, got: {kv!r}")
    k, v = kv.split("=", 1)
    try:
        return k.strip(), json.loads(v.strip())
    except json.JSONDecodeError:
        return k.strip(), v.strip()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    bb = _get_bb()

    if args.command == "init":
        try:
            entry = init_entry(args.pr, args.discussion, bb=bb)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(entry, indent=2))
        return 0

    if args.command == "get":
        entry = get_entry(args.pr, bb=bb)
        print(json.dumps(entry, indent=2))
        return 0

    if args.command == "set":
        fields: dict[str, Any] = {}
        for kv in (args.fields or []):
            try:
                k, v = _parse_field(kv)
                fields[k] = v
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        try:
            entry = set_fields(args.pr, phase=args.phase, fields=fields, bb=bb)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(entry, indent=2))
        return 0

    if args.command == "list":
        entries = list_entries(
            phase=args.phase,
            blocked=args.blocked,
            stale=args.stale,
            discussion=args.discussion,
            bb=bb,
        )
        print(json.dumps(entries, indent=2))
        return 0

    if args.command == "advance":
        try:
            entry = advance(args.pr, args.to_phase, bb=bb)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(entry, indent=2))
        return 0

    if args.command == "record-envelope":
        try:
            entry = record_envelope(
                args.pr,
                role=args.role,
                verdict=args.verdict,
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
                event_id=args.event_id,
                bb=bb,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(entry, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
