"""
Migration script — converts flat JSON state files into the SQLite database.

Reads:
  .autonomous-team/blackboard/**/*.json   → blackboard table
  .autonomous-team/sessions/*.json        → sessions table
  .autonomous-team/notification-log.jsonl → notifications table

All inserts use INSERT OR REPLACE so running the script twice is safe.

Usage:
    python backend/migrate_to_sqlite.py [--cleanup] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _resolve_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def migrate_blackboard(db, root: Path) -> int:
    """Read all per-key JSON files from the blackboard directory and insert into DB."""
    bb_dir = root / ".autonomous-team" / "blackboard"
    if not bb_dir.exists():
        return 0

    lock_dir = bb_dir / ".locks"
    count = 0

    import os
    for json_file in bb_dir.rglob("*.json"):
        # Skip lock files
        try:
            json_file.relative_to(lock_dir)
            continue
        except ValueError:
            pass

        try:
            entry = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  [WARN] skipping {json_file}: {exc}", file=sys.stderr)
            continue

        # Derive the key from the relative path
        rel = json_file.relative_to(bb_dir)
        key = str(rel.with_suffix("")).replace(os.sep, "/")

        # Insert into blackboard table preserving the full entry
        db.put("blackboard", key, entry)
        count += 1

    return count


def migrate_sessions(db, root: Path) -> int:
    """Read all session JSON files and insert into DB."""
    sessions_dir = root / ".autonomous-team" / "sessions"
    if not sessions_dir.exists():
        return 0

    count = 0
    for json_file in sessions_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  [WARN] skipping {json_file}: {exc}", file=sys.stderr)
            continue

        if "session_id" not in data:
            print(f"  [WARN] skipping {json_file}: no session_id", file=sys.stderr)
            continue

        db.put("sessions", data["session_id"], data)
        count += 1

    return count


def migrate_notifications(db, root: Path) -> int:
    """Read notification-log.jsonl and insert rows into DB."""
    log_path = root / ".autonomous-team" / "notification-log.jsonl"
    if not log_path.exists():
        return 0

    count = 0
    with log_path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  [WARN] skipping line {lineno}: {exc}", file=sys.stderr)
                continue

            db.insert_notification(
                event_type=entry.get("event_type", "unknown"),
                channel=entry.get("channel", "unknown"),
                success=bool(entry.get("success", True)),
                message=entry.get("message"),
                error=entry.get("error"),
                timestamp=entry.get("timestamp"),
            )
            count += 1

    return count


def cleanup_json_files(root: Path) -> None:
    """Remove original JSON state files (called with --cleanup flag)."""
    import shutil

    targets = [
        root / ".autonomous-team" / "blackboard",
        root / ".autonomous-team" / "sessions",
        root / ".autonomous-team" / "notification-log.jsonl",
    ]
    for target in targets:
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
                print(f"  Removed directory: {target}")
            else:
                target.unlink()
                print(f"  Removed file: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate flat JSON state files to SQLite."
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to state.db (default: .autonomous-team/state.db)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove original JSON files after successful migration.",
    )
    args = parser.parse_args(argv)

    repo_root = _resolve_repo_root()

    # Import here so the module is importable without sqlite3 issues on import
    from backend.db import Database

    db_path = args.db or (repo_root / ".autonomous-team" / "state.db")
    db = Database(db_path)

    print(f"Migrating to: {db_path}")
    print("")

    bb_count = migrate_blackboard(db, repo_root)
    print(f"  Blackboard entries migrated: {bb_count}")

    sess_count = migrate_sessions(db, repo_root)
    print(f"  Sessions migrated:           {sess_count}")

    notif_count = migrate_notifications(db, repo_root)
    print(f"  Notifications migrated:      {notif_count}")

    print("")
    print(f"Migrated {bb_count} blackboard entries, {sess_count} sessions, {notif_count} notifications.")

    if args.cleanup:
        print("")
        print("Removing original JSON files (--cleanup)...")
        cleanup_json_files(repo_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
