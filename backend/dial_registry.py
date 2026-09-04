"""
dial_registry.py — runtime autonomy dial system.

Each "dial class" has a numeric level 1–5. check() allows/denies an
action based on whether the current dial level meets the requested
threshold. set_dial() mutates the level and records a hash-chained
audit row. Directives can carry TTLs and expire automatically on the
next check().

Hardcoded ceilings (cannot be raised even by an allowlisted source):
  sandbox.modify  → 1
  methodology.change → 2
  external.system → 2

All other classes ceiling = 5.

Default dial state lives in <STATE_DIR>/dial-registry.json (written on
first use if absent). Mutations are recorded in <STATE_DIR>/audit.jsonl
as hash-chained rows with kind="dial_change" (accepted) or
kind="dial_directive_rejected" (rejected calls).

Usage (library)::

    from backend.dial_registry import check, set_dial, list_directives

    allowed, reason = check("agent.spawn")
    if allowed:
        ...

    set_dial("agent.spawn", 3, ttl="for-today",
             source={"kind": "github_user", "login": "ian"})

    set_dial("sandbox.modify", 2, ...)   # raises DialCeilingExceeded — ceiling=1

Usage (CLI)::

    python backend/dial_registry.py list
    python backend/dial_registry.py check agent.spawn 1
    python backend/dial_registry.py set agent.spawn 3
    python backend/dial_registry.py revert-expired
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DialCeilingExceeded(ValueError):
    """Raised when a set_dial() call requests a level above the class ceiling."""


# ---------------------------------------------------------------------------
# Hardcoded ceilings — enforced even if allowlist permits the source
# ---------------------------------------------------------------------------

_CEILINGS: dict[str, int] = {
    "sandbox.modify": 1,
    "methodology.change": 2,
    "external.system": 2,
}
_DEFAULT_CEILING = 5

# ---------------------------------------------------------------------------
# Default dial state — written to dial-registry.json on first use
# ---------------------------------------------------------------------------

_DEFAULT_DIALS: list[dict] = [
    {"class": "docs.write",         "level": 5, "ceiling": 5},
    {"class": "tests.add",          "level": 4, "ceiling": 5},
    {"class": "deps.bump",          "level": 3, "ceiling": 5},
    {"class": "agent.spawn",        "level": 4, "ceiling": 5},
    {"class": "merge.standard",     "level": 4, "ceiling": 5},
    {"class": "merge.fast-path",    "level": 2, "ceiling": 5},
    {"class": "intent.generate",    "level": 1, "ceiling": 5},
    {"class": "methodology.change", "level": 1, "ceiling": 2},
    {"class": "external.system",    "level": 1, "ceiling": 2},
    {"class": "sandbox.modify",     "level": 1, "ceiling": 1},
    {"class": "cost.spend",         "level": 2, "ceiling": 5},
    {"class": "memory.write",       "level": 3, "ceiling": 5},
    {"class": "archive.move",       "level": 4, "ceiling": 5},
]

# ---------------------------------------------------------------------------
# Role-to-dial-class mapping — used by spawn sites to look up the dial class
# for a given agent role.  Consumed by PR2 spawn-site wiring.
# ---------------------------------------------------------------------------

_ROLE_TO_DIAL_CLASS: dict[str, str] = {
    "executor":             "agent.spawn",
    "code-reviewer":        "agent.spawn",
    "security-reviewer":    "agent.spawn",
    "acceptance-tester":    "agent.spawn",
    "project-manager":      "agent.spawn",
    "technical-architect":  "agent.spawn",
    "security-expert":      "agent.spawn",
    "cost-analyst":         "agent.spawn",
    "product-owner":        "agent.spawn",
    "performance-expert":   "agent.spawn",
    "run-analyst":          "agent.spawn",
    "feedback-scanner":     "agent.spawn",
    "quality-sweep":        "agent.spawn",
    "docs-writer":          "agent.spawn",
    "browser-tester":       "agent.spawn",
    "researcher":           "agent.spawn",
    "mission-analyst":      "agent.spawn",
    "visual-verifier":      "agent.spawn",
    "incident-commander":   "agent.spawn",
    "release-manager":      "agent.spawn",
}


# ---------------------------------------------------------------------------
# State directory resolution
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    from backend.state_paths import STATE_DIR  # noqa: PLC0415
    return STATE_DIR


def _registry_path() -> Path:
    return _state_dir() / "dial-registry.json"


def _allowlist_path() -> Path:
    return _state_dir() / "dial-directive-allowlist.json"


def _audit_path() -> Path:
    return _state_dir() / "audit.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local_midnight_utc() -> str:
    """Return tomorrow's local midnight expressed as a UTC ISO-8601 timestamp.

    "for-today" means "expires at end of today", so the TTL must be a future
    timestamp.  Using today's 00:00 (past) would make the directive immediately
    expired.  We use tomorrow's 00:00 local time, which is always in the future.
    """
    now_local = datetime.now()
    tomorrow_local = datetime.combine(now_local.date() + timedelta(days=1), time.min)
    # Express local time as UTC by reading the system's UTC offset.
    import time as _time  # noqa: PLC0415
    utc_offset_s = -_time.timezone if _time.daylight == 0 else -_time.altzone
    utc_midnight = tomorrow_local.replace(
        tzinfo=timezone(timedelta(seconds=utc_offset_s))
    ).astimezone(timezone.utc)
    return utc_midnight.isoformat(timespec="seconds")


def _parse_ttl(ttl: str | None) -> str | None:
    """Convert ttl argument to an ISO-8601 UTC expiry string or None."""
    if ttl is None:
        return None
    if ttl == "for-today":
        return _local_midnight_utc()
    # Already an ISO string — normalise to UTC
    try:
        dt = datetime.fromisoformat(ttl)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError as exc:
        raise ValueError(f"Invalid ttl format {ttl!r}: {exc}") from exc


def _is_expired(ttl_until: str | None) -> bool:
    """Return True if ttl_until is set and has passed."""
    if ttl_until is None:
        return False
    try:
        expiry = datetime.fromisoformat(ttl_until)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expiry
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Registry I/O  (flock-protected, atomic write)
# ---------------------------------------------------------------------------

def _load_registry() -> dict[str, dict]:
    """
    Load the registry from disk.

    Returns a dict keyed by class name.  Each value has at minimum:
      {"level": int, "ceiling": int, "directives": [...]}

    If the file is absent, writes the defaults and returns them.
    """
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        return _init_defaults()

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _init_defaults()

    # data may be either a list (legacy) or a dict keyed by class name
    if isinstance(data, list):
        registry = {}
        for entry in data:
            cls = entry.get("class") or entry.get("class_name")
            if cls:
                registry[cls] = {
                    "level": int(entry.get("level", 1)),
                    "ceiling": _effective_ceiling(cls, int(entry.get("ceiling", _DEFAULT_CEILING))),
                    "directives": entry.get("directives", []),
                }
        return registry
    if isinstance(data, dict):
        # Normalise
        registry = {}
        for cls, val in data.items():
            if isinstance(val, dict):
                registry[cls] = {
                    "level": int(val.get("level", 1)),
                    "ceiling": _effective_ceiling(cls, int(val.get("ceiling", _DEFAULT_CEILING))),
                    "directives": val.get("directives", []),
                }
        # Migrate legacy key executor.spawn → agent.spawn (D#1143).
        # Idempotent: only runs when executor.spawn is present in the file.
        if "executor.spawn" in registry:
            legacy_directives = registry.pop("executor.spawn").get("directives", [])
            if "agent.spawn" not in registry:
                default_entry = next(
                    (e for e in _DEFAULT_DIALS if e["class"] == "agent.spawn"), None
                )
                registry["agent.spawn"] = {
                    "level": default_entry["level"] if default_entry else 4,
                    "ceiling": _effective_ceiling("agent.spawn", default_entry["ceiling"] if default_entry else _DEFAULT_CEILING),
                    "directives": [],
                }
            # Concat legacy directives first, then existing ones
            existing = registry["agent.spawn"].get("directives", [])
            registry["agent.spawn"]["directives"] = legacy_directives + existing
            # Atomically persist migrated state
            _save_registry(registry)
            # Emit one audit row for the migration
            prev_hash = _read_last_audit_hash()
            row = {
                "kind": "dial_state_migration",
                "prev_hash": prev_hash,
                "legacy_class": "executor.spawn",
                "target_class": "agent.spawn",
                "directives_moved": len(legacy_directives),
                "timestamp": _now_iso(),
            }
            _append_audit(row)
        return registry

    return _init_defaults()


def _init_defaults() -> dict[str, dict]:
    """Write the default dial set and return it as a class-keyed dict."""
    registry: dict[str, dict] = {}
    for entry in _DEFAULT_DIALS:
        cls = entry["class"]
        registry[cls] = {
            "level": entry["level"],
            "ceiling": _effective_ceiling(cls, entry["ceiling"]),
            "directives": [],
        }
    _save_registry(registry)
    return registry


def _save_registry(registry: dict[str, dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with path.open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            tmp.write_text(
                json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.rename(path)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _effective_ceiling(class_name: str, stored_ceiling: int) -> int:
    """Return the ceiling that actually applies — hardcoded wins."""
    hardcoded = _CEILINGS.get(class_name)
    if hardcoded is not None:
        # The hardcoded ceiling is absolute; stored value cannot exceed it.
        return hardcoded
    return stored_ceiling


# ---------------------------------------------------------------------------
# Allowlist auth
# ---------------------------------------------------------------------------

def _load_allowlist() -> list[dict]:
    """Return the list of allowed directive sources.

    Empty list means no sources are authorised — all set_dial calls refused.
    If the file is absent, it is created (empty) and an empty list returned.
    """
    path = _allowlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        path.write_text(json.dumps([], indent=2) + "\n", encoding="utf-8")
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _authenticate_source(source: dict | None) -> bool:
    """
    Return True if *source* is in the allowlist, False otherwise.

    Always returns False when the allowlist is empty.
    """
    if not isinstance(source, dict):
        return False

    allowlist = _load_allowlist()
    if not allowlist:
        return False

    kind = source.get("kind")
    for entry in allowlist:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != kind:
            continue
        # The entry is the constraint and the source must satisfy it — every
        # key the entry requires must be present and equal on the source. An
        # entry can never be more specific than the source it authorizes
        # (D#1883 Decision 2): {"kind":"system"} must NOT match an entry that
        # also requires "reason":"dashboard_rpc". This single check subsumes
        # the old "kind"-specific shortcuts (github_user/login, system/reason)
        # — those returned True on a kind+one-key match and skipped any other
        # key the entry carried, which is the exact inverse of this invariant
        # (SEC-2, D#1883 security review round 2): an operator who *narrows*
        # an entry by adding a third key got a constraint that silently did
        # nothing.
        if all(source.get(k) == v for k, v in entry.items()):
            return True
    return False


# ---------------------------------------------------------------------------
# Audit log (hash-chained)
# ---------------------------------------------------------------------------

def _read_last_audit_hash() -> str:
    """Return SHA-256 of the last audit.jsonl row, or 'genesis'."""
    path = _audit_path()
    if not path.exists():
        return "genesis"

    try:
        with path.open("rb") as fh:
            # Read the whole file (audit.jsonl is small) and get the last non-empty line
            content = fh.read()
    except OSError:
        return "genesis"

    lines = [l.strip() for l in content.split(b"\n") if l.strip()]
    if not lines:
        return "genesis"

    return hashlib.sha256(lines[-1]).hexdigest()


def _append_audit(row: dict) -> None:
    """Append a JSON-Lines row to the audit log (no lock — append is atomic on Linux)."""
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _emit_dial_change(
    class_name: str,
    prev_level: int,
    new_level: int,
    source: dict | None,
    ttl_until: str | None,
) -> None:
    prev_hash = _read_last_audit_hash()
    row = {
        "kind": "dial_change",
        "prev_hash": prev_hash,
        "class": class_name,
        "prev_level": prev_level,
        "new_level": new_level,
        "source": source,
        "ttl_until": ttl_until,
        "timestamp": _now_iso(),
    }
    _append_audit(row)


def _emit_dial_rejection(
    class_name: str,
    attempted_level: int,
    source: dict | None,
    reason: str,
) -> None:
    """Append an audit row for a rejected set_dial() call.

    Uses the same hash-chain algorithm as accepted rows so the chain remains
    contiguous across mixed accept/reject sequences.
    """
    prev_hash = _read_last_audit_hash()
    row = {
        "kind": "dial_directive_rejected",
        "prev_hash": prev_hash,
        "class": class_name,
        "level": attempted_level,
        "source": source,
        "reason": reason,
        "timestamp": _now_iso(),
    }
    _append_audit(row)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def revert_expired() -> int:
    """
    Check all directives and revert any that have passed their TTL.

    Returns the count of classes that were reverted.
    """
    registry = _load_registry()
    reverted = 0

    for class_name, state in registry.items():
        directives = state.get("directives", [])
        if not directives:
            continue

        live = [d for d in directives if not _is_expired(d.get("ttl_until"))]
        expired = [d for d in directives if _is_expired(d.get("ttl_until"))]

        if not expired:
            continue

        # Recompute level: use the highest non-expired directive, or fall back
        # to the default level for this class.
        if live:
            new_level = max(d["level"] for d in live)
        else:
            # Restore default level for this class
            default_entry = next(
                (e for e in _DEFAULT_DIALS if e["class"] == class_name), None
            )
            new_level = default_entry["level"] if default_entry else 1

        old_level = state["level"]
        state["directives"] = live
        state["level"] = new_level

        if new_level != old_level:
            _emit_dial_change(class_name, old_level, new_level, None, None)
            reverted += 1

    _save_registry(registry)
    return reverted


def check(class_name: str, requested_level: int = 1) -> tuple[bool, str]:
    """
    Check whether an action at *requested_level* is permitted.

    Calls revert_expired() first (lazy TTL cleanup).

    Returns (allowed: bool, reason: str).
    """
    revert_expired()
    registry = _load_registry()

    if class_name not in registry:
        # Unknown class: default to allow at level 1, deny above
        if requested_level <= 1:
            return (True, f"unknown class {class_name!r} — default allow at level 1")
        return (False, f"unknown class {class_name!r} — requested level {requested_level} > default 1")

    state = registry[class_name]
    current = state["level"]
    ceiling = state["ceiling"]

    if requested_level < 1:
        return (False, f"requested_level must be >= 1, got {requested_level}")

    if requested_level > ceiling:
        return (False, f"requested level {requested_level} exceeds ceiling {ceiling} for {class_name!r}")

    if current >= requested_level:
        return (True, f"dial {class_name!r} at {current} >= requested {requested_level}")
    else:
        return (False, f"dial {class_name!r} at {current} < requested {requested_level}")


def set_dial(
    class_name: str,
    level: int,
    ttl: str | None = None,
    source: dict | None = None,
) -> dict:
    """
    Set the dial for *class_name* to *level*.

    Params
    ------
    class_name : str
        One of the 13 registered classes.
    level : int
        Target level, 1–5 (subject to class ceiling).
    ttl : str | None
        Expiry: ISO-8601 string, or "for-today" (parsed to local midnight UTC).
    source : dict | None
        Source descriptor: {"kind": "github_user", "login": "..."}
        or {"kind": "system", "reason": "..."}.
        Must appear in the allowlist; empty allowlist = all refused.

    Returns the updated state dict.
    Raises ValueError on validation failure.
    """
    # Ceiling/validity checks run BEFORE auth so error precedence is:
    # invalid_level > ceiling_violation > unauthenticated_source.
    # Every rejected call appends an audit row before raising.
    if level < 1:
        _emit_dial_rejection(class_name, level, source, "invalid_level")
        raise ValueError(f"level must be >= 1, got {level}")

    ceiling = _CEILINGS.get(class_name, _DEFAULT_CEILING)
    if level > ceiling:
        _emit_dial_rejection(class_name, level, source, "ceiling_violation")
        raise DialCeilingExceeded(
            f"level {level} exceeds ceiling {ceiling} for class {class_name!r}"
        )

    if not _authenticate_source(source):
        _emit_dial_rejection(class_name, level, source, "unauthenticated_source")
        # SEC-1 (D#1883 security review round 2): worded at the operator, not
        # the caller. The prior text told a rejected caller to go run the
        # provisioning script itself — inside a worktree sub-agent, the
        # sandbox hook now blocks that exact invocation (see
        # hooks/sandbox_rules.py _DIAL_PROTECTED_SUFFIXES), but the message
        # shouldn't invite a rejected call to try self-authorizing at all.
        raise ValueError(
            f"source {source!r} is not in the directive allowlist. "
            "A caller cannot authorize itself — ask an operator to run "
            "`bash scripts/provision-dial-allowlist.sh`, or to add an entry to "
            "<STATE_DIR>/dial-directive-allowlist.json by hand. Ceilings stay "
            "enforced either way."
        )

    # Reject unknown class names — only the 13 registered classes are valid.
    _known_classes = {entry["class"] for entry in _DEFAULT_DIALS}
    if class_name not in _known_classes:
        _emit_dial_rejection(class_name, level, source, "unknown_class")
        raise ValueError(
            f"unknown dial class {class_name!r} — "
            f"registered classes: {sorted(_known_classes)}"
        )

    ttl_until = _parse_ttl(ttl)

    registry = _load_registry()

    if class_name not in registry:
        # Initialise from defaults (registry may be empty on first use)
        registry[class_name] = {
            "level": 1,
            "ceiling": ceiling,
            "directives": [],
        }

    state = registry[class_name]
    prev_level = state["level"]

    directive = {
        "level": level,
        "source": source,
        "set_at": _now_iso(),
        "ttl_until": ttl_until,
    }
    state["directives"].append(directive)
    state["level"] = level

    _save_registry(registry)
    _emit_dial_change(class_name, prev_level, level, source, ttl_until)

    return dict(state)


def list_directives() -> list[dict]:
    """
    Return current dial state for all registered classes.

    Each entry is a dict with keys: class, level, ceiling, directives.
    """
    revert_expired()
    registry = _load_registry()
    result = []
    for class_name, state in sorted(registry.items()):
        result.append({
            "class": class_name,
            "level": state["level"],
            "ceiling": state["ceiling"],
            "directives": state.get("directives", []),
        })
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dial_registry",
        description="Autonomy dial registry — check/set dial levels.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all dial classes and their current levels")

    chk = sub.add_parser("check", help="Check if requested level is permitted")
    chk.add_argument("class_name")
    chk.add_argument("requested_level", type=int, nargs="?", default=1)

    set_p = sub.add_parser("set", help="Set a dial level")
    set_p.add_argument("class_name")
    set_p.add_argument("level", type=int)
    set_p.add_argument("--ttl", default=None, help="Expiry: ISO-8601 or 'for-today'")
    set_p.add_argument(
        "--source",
        default=None,
        help='JSON source descriptor, e.g. \'{"kind":"system","reason":"test"}\'',
    )

    sub.add_parser("revert-expired", help="Revert all expired directives")

    return p


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    if args.command == "list":
        directives = list_directives()
        for d in directives:
            cls = d["class"]
            lvl = d["level"]
            ceil = d["ceiling"]
            n = len(d["directives"])
            print(f"  {cls:<25}  level={lvl}  ceiling={ceil}  directives={n}")
        return 0

    elif args.command == "check":
        allowed, reason = check(args.class_name, args.requested_level)
        status = "ALLOW" if allowed else "DENY"
        print(f"{status}: {reason}")
        return 0 if allowed else 1

    elif args.command == "set":
        source = None
        if args.source:
            try:
                source = json.loads(args.source)
            except json.JSONDecodeError as exc:
                print(f"error: --source must be valid JSON: {exc}", file=sys.stderr)
                return 1
        try:
            result = set_dial(args.class_name, args.level, ttl=args.ttl, source=source)
            print(f"set {args.class_name} level={result['level']} ceiling={result['ceiling']}")
        except DialCeilingExceeded as exc:
            print(f"DialCeilingExceeded: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    elif args.command == "revert-expired":
        count = revert_expired()
        print(f"reverted {count} class(es)")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
