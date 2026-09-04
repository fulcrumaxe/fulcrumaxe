"""active_loops.py — persistent loop state for the HTTP adapter.

Manages .autonomous-team/active-loops.json with atomic writes and dead-pid pruning.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_LOOPS_PATH = REPO_ROOT / ".autonomous-team" / "active-loops.json"


def _loop_id() -> str:
    ts = int(time.time())
    rand = secrets.token_hex(3)
    return f"loop-{ts}-{rand}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _load_raw() -> dict[str, Any]:
    """Load the raw JSON from disk; return empty structure on any error."""
    try:
        data = json.loads(ACTIVE_LOOPS_PATH.read_text())
        if isinstance(data, dict) and "loops" in data:
            return data
    except Exception:
        pass
    return {"loops": {}}


def _save_atomic(data: dict[str, Any]) -> None:
    """Write data atomically via a unique tempfile + rename."""
    ACTIVE_LOOPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=ACTIVE_LOOPS_PATH.parent,
        prefix=".active-loops-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2))
        Path(tmp_path).replace(ACTIVE_LOOPS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def prune_dead_pids() -> None:
    """Mark entries with dead PIDs as 'stopped'. Called on server start."""
    data = _load_raw()
    changed = False
    for entry in data["loops"].values():
        if entry.get("status") == "running":
            pid = entry.get("pid")
            if pid is not None and not _pid_alive(int(pid)):
                entry["status"] = "stopped"
                changed = True
    if changed:
        _save_atomic(data)


def create_loop(prompt: str, cadence_seconds: int | None, pid: int) -> dict[str, Any]:
    """Create a new loop entry and persist it. Returns the entry."""
    data = _load_raw()
    lid = _loop_id()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry: dict[str, Any] = {
        "loop_id": lid,
        "prompt": prompt,
        "cadence_seconds": cadence_seconds,
        "started_at": now,
        "pid": pid,
        "last_event_at": now,
        "status": "running",
    }
    data["loops"][lid] = entry
    _save_atomic(data)
    return entry


def stop_loop(loop_id: str) -> dict[str, Any] | None:
    """Mark a loop as stopped. Returns the updated entry or None if not found."""
    data = _load_raw()
    entry = data["loops"].get(loop_id)
    if entry is None:
        return None
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["status"] = "stopped"
    entry["last_event_at"] = now
    _save_atomic(data)
    return entry


def list_loops() -> list[dict[str, Any]]:
    """Return all running loops."""
    data = _load_raw()
    return [
        e for e in data["loops"].values()
        if e.get("status") == "running"
    ]


def touch_loop(loop_id: str) -> None:
    """Update last_event_at for a loop."""
    data = _load_raw()
    entry = data["loops"].get(loop_id)
    if entry is not None:
        entry["last_event_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_atomic(data)


def get_loop(loop_id: str) -> dict[str, Any] | None:
    return _load_raw()["loops"].get(loop_id)
