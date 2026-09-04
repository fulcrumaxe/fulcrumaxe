"""backend/agent_teams_substrate.py — helpers for the ~/.claude/teams/ coordination substrate.

Provides four functions used by spawn-agent.sh and post-agent-hook.sh to adopt
~/.claude/teams/{team}/inboxes/ and ~/.claude/tasks/{team}/ as the primary
coordination layer while legacy blackboard continues to receive dual-writes
during the 14-day deprecation window.

Team name defaults to the repo directory basename ("fulcrumaxe").
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEAMS_DIR = Path(os.environ.get("CLAUDE_TEAMS_DIR", Path.home() / ".claude" / "teams"))
TASKS_DIR = Path(os.environ.get("CLAUDE_TASKS_DIR", Path.home() / ".claude" / "tasks"))
DEFAULT_TEAM = "fulcrumaxe"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically using a temp file + rename."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def ensure_team_exists(team: str = DEFAULT_TEAM) -> Path:
    """Create ~/.claude/teams/{team}/ with config.json and inboxes/ if absent.

    Returns the team directory path. Idempotent.
    """
    team_dir = TEAMS_DIR / team
    inboxes_dir = team_dir / "inboxes"
    config_path = team_dir / "config.json"

    team_dir.mkdir(parents=True, exist_ok=True)
    inboxes_dir.mkdir(exist_ok=True)

    if not config_path.exists():
        config: dict = {
            "team": team,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "members": [],
        }
        _atomic_write(config_path, json.dumps(config, indent=2))

    return team_dir


def append_team_member(
    agent_id: str,
    role: str,
    discussion: str | int | None = None,
    team: str = DEFAULT_TEAM,
) -> None:
    """Append an agent entry to ~/.claude/teams/{team}/config.json members array.

    Reads config.json, deduplicates by agent_id, appends if new, writes back
    atomically. Skips silently if the file cannot be read/written — non-fatal.
    """
    config_path = TEAMS_DIR / team / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return  # non-fatal: team dir may not exist yet

    members = config.get("members") or []

    # Dedup: skip if an entry with the same agent_id already exists.
    if any(m.get("agent_id") == agent_id for m in members):
        return

    member: dict = {
        "agent_id": agent_id,
        "role": role,
        "joined_at": datetime.now(timezone.utc).isoformat(),
    }
    if discussion is not None:
        member["discussion"] = str(discussion)

    members.append(member)
    config["members"] = members

    try:
        _atomic_write(config_path, json.dumps(config, indent=2))
    except Exception:  # noqa: BLE001
        pass  # non-fatal


def write_task(
    task_id: str,
    task: dict | None = None,
    # Legacy positional args kept for backward compat during transition:
    _owner: str | None = None,
    _status: str | None = None,
    discussion: str | int | None = None,
    pr: str | int | None = None,
    team: str = DEFAULT_TEAM,
) -> None:
    """Write or update a task record at ~/.claude/tasks/{team}/{task_id}.json.

    Two-phase lifecycle:
    - Spawn-time call: pass task dict with status='pending' — creates the full record.
    - Completion call: pass task dict with status='done' (or other final status) —
      loads the existing record, merges the new fields, writes back.

    If task is not provided (legacy call), the function builds a minimal record
    from the provided keyword arguments.

    Creates the directory if needed. The filename key aligns with task_id vocabulary.
    """
    tasks_team_dir = TASKS_DIR / team
    tasks_team_dir.mkdir(parents=True, exist_ok=True)

    task_path = tasks_team_dir / f"{task_id}.json"

    if task is not None:
        # New-style call: task dict provided directly.
        status = task.get("status", "")
        if status in ("pending",) or not task_path.exists():
            # Spawn-time write: create full record.
            record: dict = {
                "task_id": task_id,
                "status": status or "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            # Merge in any extra fields from the provided dict.
            for k, v in task.items():
                if k not in ("task_id", "created_at"):
                    record[k] = v
        else:
            # Completion update: load existing record and merge.
            try:
                record = json.loads(task_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                record = {"task_id": task_id, "created_at": datetime.now(timezone.utc).isoformat()}
            record.update(task)
            record["task_id"] = task_id  # ensure canonical field present
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
    else:
        # Legacy call: build from keyword args (transition shim).
        owner = _owner or ""
        status = _status or "done"
        if task_path.exists():
            # Completion update.
            try:
                record = json.loads(task_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                record = {}
            record["status"] = status
            record["task_id"] = task_id
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
        else:
            # New record.
            record = {
                "task_id": task_id,
                "owner": owner,
                "status": status,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        if discussion is not None:
            record["discussion"] = str(discussion)
        if pr is not None:
            record["pr"] = str(pr)

    try:
        _atomic_write(task_path, json.dumps(record, indent=2))
    except Exception:  # noqa: BLE001
        pass  # non-fatal


def read_team_status(team: str = DEFAULT_TEAM) -> list[dict]:
    """Read task records from ~/.claude/tasks/{team}/.

    Returns a list of task dicts, newest-first by 'created_at' (or 'ts') field.
    Returns [] when the directory does not exist or is empty.
    """
    tasks_team_dir = TASKS_DIR / team
    if not tasks_team_dir.is_dir():
        return []

    tasks: list[dict] = []
    for task_file in tasks_team_dir.glob("*.json"):
        try:
            data = json.loads(task_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                tasks.append(data)
        except Exception:  # noqa: BLE001
            pass

    tasks.sort(key=lambda t: t.get("created_at") or t.get("ts", ""), reverse=True)
    return tasks


# Terminal statuses: tasks in these states are considered finished.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "fail", "pass", "needs-fix", "skip"})


def prune_terminal_substrate_tasks(
    team: str = DEFAULT_TEAM,
    days: int = 7,
) -> int:
    """Remove terminal-status task records older than *days* days.

    Only removes files whose ``status`` is in _TERMINAL_STATUSES AND whose
    ``created_at`` (or ``updated_at``) timestamp is more than *days* days old.
    Recent tasks and any task not in a terminal state are never touched.

    This is routine log-rotation for runtime state — equivalent to JSONL
    rotation elsewhere in the project.  It does NOT touch repo files (no git
    operations), so the archive/ git-mv rule does not apply.

    Returns the number of files removed.
    """
    tasks_team_dir = TASKS_DIR / team
    if not tasks_team_dir.is_dir():
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0

    for task_file in list(tasks_team_dir.glob("*.json")):
        try:
            data = json.loads(task_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue  # skip unreadable files — leave them alone

        if not isinstance(data, dict):
            continue

        status = data.get("status", "")
        if status not in _TERMINAL_STATUSES:
            continue  # live or unknown status — never prune

        # Use updated_at if present (more accurate finish time), else created_at.
        ts_str = data.get("updated_at") or data.get("created_at") or ""
        if not ts_str:
            continue  # no timestamp — leave it alone

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue  # unparseable timestamp — leave it alone

        if ts >= cutoff:
            continue  # recent terminal task — keep it

        try:
            task_file.unlink()
            removed += 1
        except OSError:
            pass  # non-fatal

    return removed
