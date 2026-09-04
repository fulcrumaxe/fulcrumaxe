"""
Locked blackboard — atomic shared state for the autonomous team.

Replaces ad-hoc reads/writes to now.md with a structured, flock-protected
key-value store. Each key lives in its own JSON file under
.autonomous-team/blackboard/, so agents never stomp on each other.

Usage (CLI):
    python backend/blackboard.py read loop/status
    python backend/blackboard.py write loop/status '"idle"'
    python backend/blackboard.py list
    python backend/blackboard.py list loop/
    python backend/blackboard.py cas loop/status '"running"' 3
    python backend/blackboard.py delete loop/status

Usage (library):
    from backend.blackboard import Blackboard
    bb = Blackboard()
    bb.write("loop/status", "idle", updated_by="team-lead")
    val = bb.read("loop/status")
"""

import argparse
import fcntl
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root: `python3 backend/blackboard.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Default blackboard root.
#
# state_paths.py is the single source of truth (D#1908 PR 3) — no legacy
# in-repo fallback here any more. Resolved at call time, not at import time:
# a module-level `_DEFAULT_ROOT = _resolve_default_root()` would freeze the
# result for the life of the process and defeat a later
# AUTONOMOUS_TEAM_STATE_DIR override, the exact D#1810 freeze pattern this
# file used to carry (see backend/state_paths.py's "Known residual freeze"
# note, now resolved).
# ---------------------------------------------------------------------------
def _resolve_default_root() -> Path:
    from backend.state_paths import BLACKBOARD_DIR  # noqa: PLC0415
    return BLACKBOARD_DIR


def _emit_audit(action: str, key: str, old_value: object, new_value: object, actor: str) -> None:
    """Best-effort audit emit — never raises, never blocks the caller."""
    try:
        from backend.audit_trail import get_audit_trail  # noqa: PLC0415
        get_audit_trail().emit("blackboard", action, key, old_value, new_value, actor)
    except Exception:  # noqa: BLE001
        pass
_LOCK_DIR_NAME = ".locks"
_LOCK_TIMEOUT_SECONDS = 5


class LockTimeout(TimeoutError):
    """Raised when we cannot acquire an flock within the timeout."""


class Blackboard:
    """
    Atomic key-value store backed by per-key JSON files.

    Key naming convention: use forward-slash separators, e.g. "loop/status".
    Keys map to files: <root>/<key>.json
    Lock files live at: <root>/.locks/<key>.lock
    """

    def __init__(self, root: Path | str | None = None):
        if root is None:
            self._root = _resolve_default_root()
        else:
            self._root = Path(root).resolve()
        self._lock_dir = self._root / _LOCK_DIR_NAME

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(self, key: str) -> object:
        """Return the stored value for *key*, or None if key does not exist."""
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("value")
        except (json.JSONDecodeError, OSError):
            return None

    def read_entry(self, key: str) -> dict | None:
        """Return the full entry dict (value, version, updated_at, updated_by) or None."""
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def write(self, key: str, value: object, updated_by: str = "unknown") -> bool:
        """
        Atomically write *value* under *key*.

        Increments version on each call. Returns True on success.
        Raises LockTimeout if flock cannot be acquired within 5 seconds.
        """
        with self._locked(key):
            current = self._load_raw(key)
            old_value = current.get("value") if current else None
            version = (current.get("version", 0) + 1) if current else 1
            entry = {
                "value": value,
                "version": version,
                "updated_at": _now_iso(),
                "updated_by": updated_by,
            }
            self._atomic_write(key, entry)
        _emit_audit("write", key, old_value, value, updated_by)
        return True

    def cas(
        self,
        key: str,
        value: object,
        expected_version: int,
        updated_by: str = "unknown",
    ) -> bool:
        """
        Compare-and-swap: write *value* only if current version == *expected_version*.

        Returns True on success, False on version conflict or missing key.
        Raises LockTimeout if flock cannot be acquired within 5 seconds.
        """
        old_value: object = None
        with self._locked(key):
            current = self._load_raw(key)
            if current is None:
                return False
            if current.get("version") != expected_version:
                return False
            old_value = current.get("value")
            entry = {
                "value": value,
                "version": expected_version + 1,
                "updated_at": _now_iso(),
                "updated_by": updated_by,
            }
            self._atomic_write(key, entry)
        _emit_audit("cas", key, old_value, value, updated_by)
        return True

    def list_keys(self, prefix: str = "") -> list[str]:
        """
        Return all keys stored in the blackboard, optionally filtered by *prefix*.

        Keys are returned in sorted order.
        """
        if not self._root.exists():
            return []

        keys = []
        for json_file in self._root.rglob("*.json"):
            # Skip files inside .locks
            try:
                json_file.relative_to(self._lock_dir)
                continue  # it IS inside lock dir — skip
            except ValueError:
                pass  # not inside lock dir — keep

            rel = json_file.relative_to(self._root)
            # Convert path separators to forward slashes and strip .json suffix
            key = str(rel.with_suffix("")).replace(os.sep, "/")
            if not prefix or key.startswith(prefix):
                keys.append(key)

        return sorted(keys)

    def delete(self, key: str) -> bool:
        """
        Remove the stored value for *key*.

        Returns True if the key existed and was removed, False if it didn't exist.
        """
        path = self._key_path(key)
        old_value: object = None
        with self._locked(key):
            if not path.exists():
                return False
            current = self._load_raw(key)
            if current:
                old_value = current.get("value")
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return False
        # Clean up empty parent directories (but never the root itself).
        self._prune_empty_dirs(path.parent)
        _emit_audit("delete", key, old_value, None, "unknown")
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _key_path(self, key: str) -> Path:
        """Map a key string to its JSON file path."""
        _validate_key(key)
        return self._root / (key + ".json")

    def _lock_path(self, key: str) -> Path:
        """Map a key string to its lockfile path."""
        _validate_key(key)
        return self._lock_dir / (key + ".lock")

    def _load_raw(self, key: str) -> dict | None:
        """Load existing entry without acquiring a lock (caller must hold lock)."""
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def _atomic_write(self, key: str, entry: dict) -> None:
        """Write *entry* to the key file via a tmp-then-rename dance."""
        dest = self._key_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(entry, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.rename(tmp, dest)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _prune_empty_dirs(self, directory: Path) -> None:
        """Walk upward removing empty dirs until we hit the blackboard root."""
        while directory != self._root:
            try:
                directory.rmdir()  # only succeeds if empty
            except OSError:
                break
            directory = directory.parent

    def _locked(self, key: str):
        return Blackboard._LockedCtx(self, key)

    class _LockedCtx:
        def __init__(self, bb: "Blackboard", key: str):
            self._bb = bb
            self._key = key
            self._fh = None
            self._old_handler = None

        def __enter__(self):
            lock_path = self._bb._lock_path(self._key)
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            is_main = threading.current_thread() is threading.main_thread()

            if is_main:
                def _timeout_handler(signum, frame):
                    raise LockTimeout(
                        f"Could not acquire lock for key '{self._key}' "
                        f"within {_LOCK_TIMEOUT_SECONDS}s"
                    )

                self._old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(_LOCK_TIMEOUT_SECONDS)

            try:
                fh = lock_path.open("a", encoding="utf-8")
                fcntl.flock(fh, fcntl.LOCK_EX)
                self._fh = fh
            except BaseException:
                if is_main:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, self._old_handler)
                raise

            if is_main:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, self._old_handler)
            return self

        def __exit__(self, *_):
            if self._fh:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
                self._fh = None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_key(key: str) -> None:
    """Raise ValueError for keys that would escape the blackboard root."""
    if not key:
        raise ValueError("Key must not be empty")
    if ".." in key.split("/"):
        raise ValueError(f"Key must not contain '..': {key!r}")
    if key.startswith("/"):
        raise ValueError(f"Key must not be absolute: {key!r}")


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="blackboard",
        description="Atomic shared-state blackboard for the autonomous team.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("read", help="Print value for KEY as JSON")
    r.add_argument("key")

    w = sub.add_parser("write", help="Write JSON-VALUE under KEY")
    w.add_argument("key")
    w.add_argument("value", help="JSON-encoded value (e.g. '\"hello\"' or '42')")
    w.add_argument("--updated-by", default="cli", metavar="AGENT")

    l = sub.add_parser("list", help="List stored keys, optionally filtered by PREFIX")
    l.add_argument("prefix", nargs="?", default="")

    c = sub.add_parser("cas", help="Compare-and-swap VALUE under KEY at EXPECTED_VERSION")
    c.add_argument("key")
    c.add_argument("value", help="JSON-encoded value")
    c.add_argument("expected_version", type=int)
    c.add_argument("--updated-by", default="cli", metavar="AGENT")

    d = sub.add_parser("delete", help="Remove KEY from the blackboard")
    d.add_argument("key")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    bb = Blackboard()

    if args.command == "read":
        val = bb.read(args.key)
        if val is None:
            print(f"key not found: {args.key}", file=sys.stderr)
            return 1
        print(json.dumps(val))
        return 0

    if args.command == "write":
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON value: {exc}", file=sys.stderr)
            return 1
        try:
            bb.write(args.key, value, updated_by=args.updated_by)
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        entry = bb.read_entry(args.key)
        print(f"version {entry['version']}" if entry else "ok")
        return 0

    if args.command == "list":
        keys = bb.list_keys(args.prefix)
        for key in keys:
            print(key)
        return 0

    if args.command == "cas":
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON value: {exc}", file=sys.stderr)
            return 1
        try:
            ok = bb.cas(args.key, value, args.expected_version, updated_by=args.updated_by)
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if ok:
            print("ok")
            return 0
        else:
            print("conflict")
            return 1

    if args.command == "delete":
        removed = bb.delete(args.key)
        if removed:
            print("deleted")
            return 0
        else:
            print(f"key not found: {args.key}", file=sys.stderr)
            return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())


# ------------------------------------------------------------------
# SQLite-backed implementation
# ------------------------------------------------------------------


class SqliteBlackboard:
    """
    Drop-in replacement for Blackboard backed by SQLite via backend.db.

    Implements the same read / write / cas / delete / list_keys interface.
    Lock operations (lock/unlock) use an atomic UPDATE on locked_by to avoid
    the flock-based approach needed by the file backend.
    """

    def __init__(self, db=None) -> None:
        if db is None:
            from backend.db import get_db  # local import to avoid circular deps
            db = get_db()
        self._db = db

    # ------------------------------------------------------------------
    # Public API (mirrors Blackboard)
    # ------------------------------------------------------------------

    def _get_entry(self, key: str) -> dict | None:
        """
        Return the full entry dict stored for *key*, or None.

        db.put() stores the full entry dict (value, version, updated_at, …) as
        the JSON blob in the 'value' column. db.get() decodes it back, so
        row["value"] is the entry dict.
        """
        row = self._db.get("blackboard", key)
        if row is None:
            return None
        entry = row.get("value")
        # If stored as a plain dict (full entry), return it directly.
        if isinstance(entry, dict):
            return entry
        # Fallback: treat the scalar as the entry's value field.
        return {"value": entry, "version": 0}

    def read(self, key: str) -> object:
        """Return the stored value for *key*, or None if key does not exist."""
        _validate_key(key)
        entry = self._get_entry(key)
        if entry is None:
            return None
        return entry.get("value")

    def read_entry(self, key: str) -> dict | None:
        """Return the full entry dict (value, version, updated_at, updated_by) or None."""
        _validate_key(key)
        return self._get_entry(key)

    def write(self, key: str, value: object, updated_by: str = "unknown") -> bool:
        """Atomically write *value* under *key*."""
        _validate_key(key)
        existing = self._get_entry(key)
        version = (existing.get("version", 0) + 1) if existing else 1
        entry = {
            "value": value,
            "version": version,
            "updated_at": _now_iso(),
            "updated_by": updated_by,
        }
        self._db.put("blackboard", key, entry)
        return True

    def cas(
        self,
        key: str,
        value: object,
        expected_version: int,
        updated_by: str = "unknown",
    ) -> bool:
        """Compare-and-swap with optimistic version locking."""
        _validate_key(key)
        current = self._get_entry(key)
        if current is None or current.get("version") != expected_version:
            return False
        entry = {
            "value": value,
            "version": expected_version + 1,
            "updated_at": _now_iso(),
            "updated_by": updated_by,
        }
        self._db.put("blackboard", key, entry)
        return True

    def list_keys(self, prefix: str = "") -> list[str]:
        """Return all keys, optionally filtered by prefix."""
        return self._db.list_keys("blackboard", prefix or None)

    def delete(self, key: str) -> bool:
        """Remove *key*. Returns True if it existed."""
        _validate_key(key)
        return self._db.delete("blackboard", key)

    def lock(self, key: str, locked_by: str, timeout: float = 5.0) -> bool:
        """
        Acquire a logical lock on *key* using an atomic SQL UPDATE.

        Returns True if lock was acquired, False if already held by another agent.
        The row must already exist (write the key first if needed).
        """
        _validate_key(key)
        conn = self._db._conn()
        cursor = conn.execute(
            """
            UPDATE blackboard
               SET locked_by = ?, locked_at = ?
             WHERE key = ? AND locked_by IS NULL
            """,
            (locked_by, _now_iso(), key),
        )
        conn.commit()
        return cursor.rowcount > 0

    def unlock(self, key: str, locked_by: str) -> bool:
        """
        Release a logical lock on *key*, but only if held by *locked_by*.

        Returns True if the lock was released.
        """
        _validate_key(key)
        conn = self._db._conn()
        cursor = conn.execute(
            """
            UPDATE blackboard
               SET locked_by = NULL, locked_at = NULL
             WHERE key = ? AND locked_by = ?
            """,
            (key, locked_by),
        )
        conn.commit()
        return cursor.rowcount > 0


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def get_blackboard(prefer_sqlite: bool = True) -> "Blackboard | SqliteBlackboard":
    """
    Return the appropriate Blackboard implementation.

    Returns SqliteBlackboard when state.db exists (and prefer_sqlite is True),
    otherwise falls back to the file-based Blackboard for backward compatibility.
    """
    if prefer_sqlite:
        from backend.db import state_db_exists  # local import
        if state_db_exists():
            return SqliteBlackboard()
    return Blackboard()
