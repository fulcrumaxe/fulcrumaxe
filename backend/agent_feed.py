"""
backend/agent_feed.py — Canonical JSONL agent-feed writer/reader/rotator.

The agent-feed is the primary, full-fidelity event log for all agent activity.
GitHub team-log Issue is a terse human-readable mirror (one line per event).

Canonical event schema (one JSON object per line, UTF-8, newline-terminated):
  Required: ts (ISO8601 UTC), event_type (str), role (str), message (str ≤ 280 chars)
  Optional: discussion (int), pr (int), verdict (str), tokens ({input,output}),
            files ([str]), model (str), extra (dict — open-ended for forward-compat)
"""
from __future__ import annotations

import fcntl
import gzip
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TEAM_DIR = _REPO_ROOT / ".autonomous-team"
_FEED_PATH = _TEAM_DIR / "agent-feed.jsonl"
_ARCHIVE_DIR = _REPO_ROOT / "archive" / "agent-feed"

# Event types supported by the schema
VALID_EVENT_TYPES = frozenset(
    {"agent_start", "agent_end", "spawn", "spawn_attempt", "merge", "heartbeat", "log"}
)

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _validate_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate required fields and normalize the event dict.

    Adds ``ts`` if missing. Returns a new dict with ts first for readability.
    Raises ValueError on schema violations.
    """
    # Required fields
    for field in ("event_type", "role", "message"):
        if field not in event:
            raise ValueError(f"agent_feed: required field missing: {field!r}")
        if not isinstance(event[field], str):
            raise ValueError(f"agent_feed: field {field!r} must be a string")

    msg = event["message"]
    if len(msg) > 280:
        raise ValueError(
            f"agent_feed: message exceeds 280 chars ({len(msg)} chars): {msg[:50]!r}..."
        )

    if event["event_type"] not in VALID_EVENT_TYPES:
        # Allow unknown types for forward-compat — warn but don't reject
        pass  # future extension point

    # Optional field type checks
    # Normalize empty strings to None for optional fields so stored JSON has null not "".
    for str_field in ("verdict",):
        if str_field in event and event[str_field] == "":
            event = {**event, str_field: None}

    for int_field in ("discussion", "pr"):
        if int_field in event and event[int_field] is not None:
            # Empty string means "not provided" — normalize to None.
            if event[int_field] == "":
                event = {**event, int_field: None}
                continue
            try:
                event = {**event, int_field: int(event[int_field])}
            except (TypeError, ValueError):
                raise ValueError(
                    f"agent_feed: field {int_field!r} must be an integer, got {event[int_field]!r}"
                )

    if "tokens" in event and event["tokens"] is not None:
        t = event["tokens"]
        if not isinstance(t, dict) or not all(k in t for k in ("input", "output")):
            raise ValueError(
                "agent_feed: 'tokens' must be a dict with 'input' and 'output' keys"
            )

    if "files" in event and event["files"] is not None:
        if not isinstance(event["files"], list):
            raise ValueError("agent_feed: 'files' must be a list of strings")

    # Inject ts if absent
    ts = event.get("ts") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build ordered dict: ts first, then required fields, then the rest
    ordered: dict[str, Any] = {"ts": ts}
    for f in ("event_type", "role", "message"):
        ordered[f] = event[f]
    for k, v in event.items():
        if k not in ordered:
            ordered[k] = v

    return ordered


# ---------------------------------------------------------------------------
# Write (append)
# ---------------------------------------------------------------------------


def append(event: dict[str, Any]) -> None:
    """Validate *event* and atomically append it to the agent-feed JSONL file.

    Uses ``fcntl.flock(LOCK_EX)`` for multi-process safety.
    POSIX O_APPEND guarantees atomicity for writes smaller than PIPE_BUF.

    Raises ValueError on schema violations.
    Raises OSError on unrecoverable I/O errors (disk full, file unwritable).
    """
    validated = _validate_event(event)
    line = json.dumps(validated, separators=(",", ":"), ensure_ascii=False) + "\n"
    encoded = line.encode("utf-8")

    _TEAM_DIR.mkdir(parents=True, exist_ok=True)

    # Open in append mode — O_APPEND is POSIX-atomic for small writes
    with open(_FEED_PATH, "ab") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Read (tail)
# ---------------------------------------------------------------------------


def tail(n: int = 50) -> list[dict[str, Any]]:
    """Return the last *n* events from the active feed file.

    Returns an empty list if the file does not exist.
    Lines that are not valid JSON are silently skipped.
    """
    if not _FEED_PATH.exists():
        return []

    with open(_FEED_PATH, "rb") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            raw = fh.read()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    lines = raw.decode("utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(events) >= n:
            break

    events.reverse()
    return events


# ---------------------------------------------------------------------------
# Read (filter / stream)
# ---------------------------------------------------------------------------


def filter(  # noqa: A001 — intentional override of builtin for DX
    predicate: Callable[[dict[str, Any]], bool],
    since: datetime | None = None,
    path: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream events matching *predicate* from *path* (default: active feed).

    Args:
        predicate: Called with each parsed event dict; yield when True.
        since:     If given, skip events whose ``ts`` is older than this datetime.
                   Must be timezone-aware.
        path:      Read from this file instead of the active feed. Useful for
                   reading archived gz files (caller should decompress first).

    Yields parsed event dicts.
    Lines that fail to parse are silently skipped.
    """
    target = path or _FEED_PATH
    if not target.exists():
        return

    with open(target, "rb") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if since is not None:
            ts_str = event.get("ts", "")
            try:
                # Parse ISO8601 UTC — handle both +00:00 and Z suffix
                ts_str_norm = ts_str.replace("Z", "+00:00")
                event_ts = datetime.fromisoformat(ts_str_norm)
                if event_ts < since:
                    continue
            except (ValueError, AttributeError):
                # Malformed ts — include the event anyway (don't silently drop)
                pass

        if predicate(event):
            yield event


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def rotate(today: datetime | None = None) -> dict[str, Any]:
    """Split yesterday's events out of the active feed, gzip them, archive old files.

    Called once per /loop iteration from ``scripts/agent-feed-rotate.sh``.

    Steps:
    1. Split events in agent-feed.jsonl by date. Events with ts < today stay in
       a per-date staging file; events >= today stay in the active file.
    2. Gzip each staging file → agent-feed-YYYY-MM-DD.jsonl.gz
    3. Move gz files older than 30 days to archive/agent-feed/ using git mv.
    4. Ensure archive/agent-feed/README.md exists.

    Returns a summary dict with keys: rotated_dates, archived_files, skipped.
    """
    if today is None:
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    if not _FEED_PATH.exists():
        return {"rotated_dates": [], "archived_files": [], "skipped": "feed_not_found"}

    # -- Step 1: Split active feed by date -----------------------------------
    with open(_FEED_PATH, "rb") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            raw = fh.read()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    lines_by_date: dict[str, list[str]] = {}
    today_lines: list[str] = []

    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            ts_str = event.get("ts", "")
            ts_norm = ts_str.replace("Z", "+00:00")
            event_ts = datetime.fromisoformat(ts_norm)
            event_date = event_ts.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        except Exception:
            # Malformed — keep in active file
            today_lines.append(line)
            continue

        if event_date >= today:
            today_lines.append(line)
        else:
            date_key = event_ts.strftime("%Y-%m-%d")
            lines_by_date.setdefault(date_key, []).append(line)

    rotated_dates: list[str] = []

    if lines_by_date:
        # Write per-date staging files and gzip them
        for date_key, date_lines in sorted(lines_by_date.items()):
            staging = _TEAM_DIR / f"agent-feed-{date_key}.jsonl"
            with open(staging, "w", encoding="utf-8") as fh:
                fh.write("\n".join(date_lines) + "\n")

            gz_path = _TEAM_DIR / f"agent-feed-{date_key}.jsonl.gz"
            with open(staging, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            staging.unlink()
            rotated_dates.append(date_key)

        # Rewrite active feed with only today's events
        with open(_FEED_PATH, "wb") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(("\n".join(today_lines) + ("\n" if today_lines else "")).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # -- Step 2: Archive gz files older than 30 days -------------------------
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_archive_readme()

    archived_files: list[str] = []
    cutoff = today.timestamp() - 30 * 86400

    for gz in sorted(_TEAM_DIR.glob("agent-feed-*.jsonl.gz")):
        # Parse date from filename
        m = re.search(r"agent-feed-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$", gz.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        if file_date.timestamp() < cutoff:
            # Use git mv to move to archive (never delete)
            dest = _ARCHIVE_DIR / gz.name
            # Try git mv first; fall back to plain mv if not in a git repo
            git_result = subprocess.run(
                ["git", "mv", str(gz), str(dest)],
                cwd=str(_REPO_ROOT),
                capture_output=True,
            )
            if git_result.returncode != 0:
                shutil.move(str(gz), str(dest))
            archived_files.append(gz.name)

    return {
        "rotated_dates": rotated_dates,
        "archived_files": archived_files,
        "skipped": None,
    }


def _ensure_archive_readme() -> None:
    """Create archive/agent-feed/README.md if it doesn't exist."""
    readme = _ARCHIVE_DIR / "README.md"
    if readme.exists():
        return
    readme.write_text(
        """\
# archive/agent-feed/

## What is here

Gzipped JSONL event log files rotated out of `.autonomous-team/agent-feed.jsonl`
after exceeding the 30-day hot-window retention policy.

## Retention policy

| Window | Location |
|--------|----------|
| 0–30 days (hot) | `.autonomous-team/agent-feed-YYYY-MM-DD.jsonl.gz` |
| 30+ days (cold)  | `archive/agent-feed/agent-feed-YYYY-MM-DD.jsonl.gz` (this dir) |

Files in `archive/agent-feed/` are kept forever. They are never deleted from the
working tree — only moved here from the hot window using `git mv`.

## When removed

Files are moved here automatically by `scripts/agent-feed-rotate.sh` (called from
`/loop` step 7.5) when their date is more than 30 days before today (UTC).

## Why archived

Hot-window files live in `.autonomous-team/` for fast access. Once older than 30
days they are unlikely to be queried in normal operation and move here to keep the
`.autonomous-team/` directory tidy.

## How to restore

To move a file back to the hot window for ad-hoc querying:
```bash
git mv archive/agent-feed/agent-feed-YYYY-MM-DD.jsonl.gz .autonomous-team/
```

## Schema reference

Each line in a `.jsonl.gz` file is a JSON object:
- `ts`         (string, required) — ISO8601 UTC timestamp
- `event_type` (string, required) — one of: agent_start, agent_end, spawn,
                spawn_attempt, merge, heartbeat, log
- `role`       (string, required) — agent role (executor, code-reviewer, …)
- `message`    (string, required, ≤ 280 chars) — human-readable summary
- `discussion` (int, optional)   — GitHub Discussion number
- `pr`         (int, optional)   — GitHub PR number
- `verdict`    (string, optional) — pass, fail, needs-fix, done, skip
- `tokens`     (object, optional) — `{"input": N, "output": N}`
- `files`      (array, optional)  — list of file paths touched
- `model`      (string, optional) — model ID (e.g. claude-sonnet-4-20250514)
- `extra`      (object, optional) — forward-compatible open-ended fields

## Query examples

```bash
# Show last 50 events (human-readable):
scripts/agent-feed-tail.sh

# Show events for a specific discussion:
scripts/agent-feed-tail.sh --filter discussion=335

# Read a cold archive file:
zcat archive/agent-feed/agent-feed-2026-04-01.jsonl.gz | jq 'select(.role=="executor")'

# Count merges in a cold archive:
zcat archive/agent-feed/agent-feed-2026-04-01.jsonl.gz | jq 'select(.event_type=="merge")' | wc -l
```
""",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI entry point (python3 -m backend.agent_feed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 -m backend.agent_feed <append|tail|rotate> [args]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "append":
        # Read JSON from stdin
        data = json.load(sys.stdin)
        append(data)
        print("appended")
    elif cmd == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        events = tail(n)
        for ev in events:
            print(json.dumps(ev))
    elif cmd == "rotate":
        result = rotate()
        print(json.dumps(result, indent=2))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
