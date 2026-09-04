"""
Agent spawn queue — priority-based scheduling with per-role concurrency limits.

Accepts spawn requests, enforces concurrency limits per role type, and dequeues
agents in priority order. Persists state to .autonomous-team/spawn-queue.json
using the same atomic flock+tmp-rename pattern as blackboard.py.

Also provides TTL + target-validity reaping: items older than ttl_seconds are
expired, and items whose referenced PR or Discussion no longer exists on GitHub
are removed. Call reap() (or `python backend/spawn_queue.py reap`) before
draining the queue each /loop iteration.

Usage (CLI):
    python backend/spawn_queue.py status
    python backend/spawn_queue.py pending
    python backend/spawn_queue.py active
    python backend/spawn_queue.py enqueue executor 42 "Implement feature X" --priority 20
    python backend/spawn_queue.py enqueue code-reviewer 42 "Review PR" --pr 99
    python backend/spawn_queue.py cancel a1b2c3d4
    python backend/spawn_queue.py drain
    python backend/spawn_queue.py reap
    python backend/spawn_queue.py reap --dry-run

Usage (library):
    from backend.spawn_queue import get_spawn_queue

    q = get_spawn_queue()
    req_id = q.enqueue("executor", 42, "Implement feature X")
    item = q.dequeue()
    if item:
        q.mark_active(item["id"])
        # ... spawn agent ...
        q.mark_done(item["id"])
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# Allow running as a script from repo root: `python3 backend/spawn_queue.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend._repo import REPO as _GH_REPO  # noqa: E402

# ---------------------------------------------------------------------------
# Priority defaults (lower number = higher priority)
# ---------------------------------------------------------------------------

DEFAULT_PRIORITIES: dict[str, int] = {
    "code-reviewer": 10,
    "security-reviewer": 10,
    "executor": 20,
    "project-manager": 40,
    "mission-analyst": 50,
}

# ---------------------------------------------------------------------------
# Concurrency limits defaults
# ---------------------------------------------------------------------------

DEFAULT_LIMITS: dict[str, int] = {
    "executor": 2,
    "code-reviewer": 2,
    "security-reviewer": 1,
    "project-manager": 1,
    "mission-analyst": 1,
    "_total": 6,
}

# Default timeout per role when not specified in config (minutes)
DEFAULT_TIMEOUTS: dict[str, int] = {
    "executor": 45,
    "code-reviewer": 20,
    "security-reviewer": 20,
    "project-manager": 30,
    "mission-analyst": 60,
    "_default": 60,
}

# Max history entries retained in completed/failed arrays
_MAX_HISTORY = 50

# Default TTL for queue items (seconds); overridden by policies.queue.ttl_seconds in config.
_DEFAULT_TTL_SECONDS = 7200

# Roles that validate against a PR target when a `pr` field is set.
_PR_ROLES = {"code-reviewer", "security-reviewer"}
# Roles that validate against a Discussion target when a `discussion` field is set.
_DISCUSSION_ROLES = {"project-manager", "executor"}

_REPO_ROOT = Path(__file__).resolve().parent.parent
_QUEUE_FILE = _REPO_ROOT / ".autonomous-team" / "spawn-queue.json"
_CONFIG_FILE = _REPO_ROOT / ".autonomous-team" / "config.json"
_LOCK_FILE = _REPO_ROOT / ".autonomous-team" / "spawn-queue.lock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _load_config() -> dict:
    """Load config.json, returning empty dict on any failure."""
    try:
        with _CONFIG_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


class SpawnQueue:
    """
    Priority queue for agent spawn requests with per-role concurrency limits.

    Thread-safe: all mutations acquire a threading.Lock plus an flock on the
    queue file so concurrent processes don't corrupt state.
    """

    def __init__(
        self,
        queue_file: Path | None = None,
        config_file: Path | None = None,
    ) -> None:
        self._queue_file = Path(queue_file) if queue_file else _QUEUE_FILE
        self._config_file = Path(config_file) if config_file else _CONFIG_FILE
        self._lock_file = self._queue_file.with_suffix(".lock")
        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        role: str,
        discussion: int | None,
        prompt_context: str,
        priority: int | None = None,
        requested_by: str = "team-lead",
        pr: int | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """
        Add a spawn request to the queue.

        Returns the request ID (8-char hex string).
        Priority defaults to the role's default (see DEFAULT_PRIORITIES).

        Args:
            pr: Optional PR number this request targets. Stored so the reaper can
                validate the PR still exists before spawning against it.
            ttl_seconds: How long (seconds) this item may sit in pending before
                being reaped. Defaults to policies.queue.ttl_seconds from config,
                falling back to _DEFAULT_TTL_SECONDS (7200 s = 2 hours).
        """
        if priority is None:
            priority = DEFAULT_PRIORITIES.get(role, 50)

        if ttl_seconds is None:
            ttl_seconds = self._effective_ttl_seconds()

        req_id = _new_id()

        # Capture current trace context so the spawn record can be linked back.
        _trace_id = ""
        try:
            from backend.tracing import get_current_trace_id, start_span  # noqa: PLC0415
            _trace_id = get_current_trace_id()
            with start_span(
                "spawn_queue.enqueue",
                attributes={"role": role, "discussion": str(discussion or ""), "req_id": req_id},
            ):
                pass  # span recorded on exit
        except Exception:  # noqa: BLE001
            pass

        entry: dict[str, Any] = {
            "id": req_id,
            "role": role,
            "discussion": discussion,
            "pr": pr,
            "priority": priority,
            "enqueued_at": _now_iso(),
            "ttl_seconds": ttl_seconds,
            "requested_by": requested_by,
            "prompt_context": prompt_context,
            "trace_id": _trace_id,
        }

        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                state["pending"].append(entry)
                # Keep pending sorted: lower priority number = higher urgency,
                # then earlier enqueue time as tiebreaker.
                state["pending"].sort(key=lambda r: (r["priority"], r["enqueued_at"]))
                self._save(state)

        return req_id

    def dequeue(self) -> dict | None:
        """
        Return the highest-priority request that fits within concurrency limits.

        Removes the request from pending and immediately adds it to active
        (with started_at set). The caller should call mark_active() to signal
        that the agent has actually been spawned — this is a no-op if the item
        is already active, but updates started_at for accurate timeout tracking.

        Side effect: stale active agents (past timeout_minutes) are auto-failed
        before checking limits, freeing their slots.
        """
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                limits = self._effective_limits()
                timeouts = self._effective_timeouts()

                # Clean up stale active agents first.
                state = self._cleanup_stale(state, timeouts)

                active_by_role: dict[str, int] = {}
                for a in state["active"]:
                    active_by_role[a["role"]] = active_by_role.get(a["role"], 0) + 1
                total_active = len(state["active"])

                # Check total limit first — if maxed out, nothing can go.
                total_limit = limits.get("_total", DEFAULT_LIMITS["_total"])
                if total_active >= total_limit:
                    self._save(state)
                    return None

                # Find the first pending request that fits.
                chosen = None
                chosen_idx = -1
                for idx, req in enumerate(state["pending"]):
                    role = req["role"]
                    role_limit = limits.get(role, DEFAULT_LIMITS.get(role, 1))
                    role_active = active_by_role.get(role, 0)
                    if role_active < role_limit:
                        chosen = req
                        chosen_idx = idx
                        break

                if chosen is None:
                    self._save(state)
                    return None

                state["pending"].pop(chosen_idx)

                # Move immediately to active so concurrency limits are enforced
                # for any subsequent dequeue calls before mark_active is called.
                now = _now_iso()
                active_entry: dict[str, Any] = {
                    "id": chosen["id"],
                    "role": chosen.get("role", "unknown"),
                    "discussion": chosen.get("discussion"),
                    "priority": chosen.get("priority", 50),
                    "enqueued_at": chosen.get("enqueued_at", now),
                    "requested_by": chosen.get("requested_by", "unknown"),
                    "started_at": now,
                }
                state["active"].append(active_entry)
                self._save(state)
                return chosen

    def mark_active(self, request_id: str) -> None:
        """
        Confirm a dequeued request is actively running.

        dequeue() already moves items to active; this call refreshes started_at
        for accurate timeout tracking (e.g. if there was a delay between
        dequeue and actual agent spawn). No-op if the request is not found.
        """
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                for entry in state["active"]:
                    if entry.get("id") == request_id:
                        entry["started_at"] = _now_iso()
                        self._save(state)
                        return
                # Not found — no-op (already done, failed, or unknown).

    def mark_done(self, request_id: str, result: str = "done") -> None:
        """Mark an active request as completed, freeing its concurrency slot."""
        entry = None
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                entry = self._remove_active(state, request_id)
                if entry is None:
                    # Not found in active — maybe already done or unknown.
                    return
                completed_entry = dict(entry)
                completed_entry["completed_at"] = _now_iso()
                completed_entry["result"] = result
                state["completed"].append(completed_entry)
                state["completed"] = state["completed"][-_MAX_HISTORY:]
                self._save(state)

        # Close the tracing span attached to this spawn request.
        try:
            trace_id = (entry or {}).get("trace_id", "")
            if trace_id:
                from backend.tracing import set_remote_context, start_span  # noqa: PLC0415
                set_remote_context(trace_id, "")
                with start_span(
                    "spawn_queue.done",
                    attributes={"req_id": request_id, "verdict": result},
                ):
                    pass
        except Exception:  # noqa: BLE001
            pass

    def mark_failed(self, request_id: str, reason: str = "unknown") -> None:
        """Mark a request (active or pending) as failed."""
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                # Try active first, then pending.
                entry = self._remove_active(state, request_id)
                if entry is None:
                    entry = self._remove_pending(state, request_id)
                if entry is None:
                    return
                failed_entry = dict(entry)
                failed_entry["failed_at"] = _now_iso()
                failed_entry["reason"] = reason
                state["failed"].append(failed_entry)
                state["failed"] = state["failed"][-_MAX_HISTORY:]
                self._save(state)

    def status(self) -> dict:
        """Return queue depth, active agents by role, and slot utilization."""
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                limits = self._effective_limits()
                timeouts = self._effective_timeouts()
                state = self._cleanup_stale(state, timeouts)
                self._save(state)

        active_by_role: dict[str, int] = {}
        for a in state["active"]:
            active_by_role[a["role"]] = active_by_role.get(a["role"], 0) + 1

        total_active = len(state["active"])
        total_limit = limits.get("_total", DEFAULT_LIMITS["_total"])
        utilization_pct = int(total_active / total_limit * 100) if total_limit > 0 else 0

        role_utilization = {}
        for role, limit in limits.items():
            if role == "_total":
                continue
            role_utilization[role] = {
                "active": active_by_role.get(role, 0),
                "limit": limit,
            }

        return {
            "pending": len(state["pending"]),
            "active_total": total_active,
            "total_limit": total_limit,
            "utilization_pct": utilization_pct,
            "by_role": role_utilization,
            "completed": len(state["completed"]),
            "failed": len(state["failed"]),
        }

    def list_pending(self) -> list[dict]:
        """Return all pending requests sorted by priority (ascending = urgent first)."""
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
        return list(state["pending"])

    def list_active(self) -> list[dict]:
        """Return all currently running agents."""
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
        return list(state["active"])

    def cancel(self, request_id: str) -> bool:
        """Cancel a pending request. Returns True if found and removed."""
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                entry = self._remove_pending(state, request_id)
                if entry is None:
                    return False
                failed_entry = dict(entry)
                failed_entry["failed_at"] = _now_iso()
                failed_entry["reason"] = "cancelled"
                state["failed"].append(failed_entry)
                state["failed"] = state["failed"][-_MAX_HISTORY:]
                self._save(state)
        return True

    def drain(self) -> int:
        """Mark all active agents as done (recovery helper). Returns count drained."""
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                count = len(state["active"])
                for entry in state["active"]:
                    completed_entry = dict(entry)
                    completed_entry["completed_at"] = _now_iso()
                    completed_entry["result"] = "drained"
                    state["completed"].append(completed_entry)
                state["completed"] = state["completed"][-_MAX_HISTORY:]
                state["active"] = []
                self._save(state)
        return count

    # ------------------------------------------------------------------
    # Reaper: TTL + target-validity pruning
    # ------------------------------------------------------------------

    def reap(self, dry_run: bool = False) -> dict:
        """
        Prune stale or invalid pending items.

        Two pruning passes:
        1. TTL: items older than their ttl_seconds are expired immediately
           (no network calls needed).
        2. Target validation: for remaining items that reference a PR or
           Discussion, confirm the target still exists on GitHub. Transient
           errors (rate-limit, 5xx, timeout) leave the item in pending.
           Only definitive "not found" removes it.

        The flock is held only for the snapshot and final mutation, not
        during network calls, so the drainer can run concurrently.

        Returns a dict with keys:
          pruned_ttl      — list of IDs removed for TTL expiry
          pruned_missing  — list of IDs removed for target not found
          kept            — count of items that survived reaping
          skipped_transient — list of IDs left pending due to transient errors
        """
        now = datetime.now(timezone.utc)

        # --- Step 1: snapshot pending IDs under lock ---
        with self._thread_lock:
            with self._file_locked():
                state = self._load()
                snapshot = list(state["pending"])

        pruned_ttl: list[str] = []
        pruned_missing: list[str] = []
        skipped_transient: list[str] = []

        # Separate TTL-expired from survivors to validate.
        ttl_expired_ids: set[str] = set()
        to_validate: list[dict] = []

        for item in snapshot:
            item_id = item["id"]
            enqueued_raw = item.get("enqueued_at", "")
            ttl = item.get("ttl_seconds", _DEFAULT_TTL_SECONDS)
            try:
                enqueued = datetime.fromisoformat(enqueued_raw)
                age_seconds = (now - enqueued).total_seconds()
            except (ValueError, TypeError):
                age_seconds = 0

            if age_seconds > ttl:
                ttl_expired_ids.add(item_id)
            else:
                to_validate.append(item)

        # --- Step 2: validate targets lock-free ---
        missing_ids: set[str] = set()
        transient_ids: set[str] = set()

        for item in to_validate:
            item_id = item["id"]
            role = item.get("role", "")
            pr_num = item.get("pr")
            disc_num = item.get("discussion")

            verdict = self._validate_target(role, pr_num, disc_num)
            if verdict == "missing":
                missing_ids.add(item_id)
            elif verdict == "transient":
                transient_ids.add(item_id)
            # "ok" or "skip" — leave in pending

        # --- Step 3: re-acquire lock, reload, mutate only still-pending items ---
        if not dry_run:
            with self._thread_lock:
                with self._file_locked():
                    state = self._load()
                    still_pending = []
                    now_iso = _now_iso()
                    for item in state["pending"]:
                        item_id = item["id"]
                        if item_id in ttl_expired_ids:
                            failed_entry = dict(item)
                            failed_entry["failed_at"] = now_iso
                            failed_entry["reason"] = "ttl_expired"
                            state["failed"].append(failed_entry)
                            pruned_ttl.append(item_id)
                        elif item_id in missing_ids:
                            failed_entry = dict(item)
                            failed_entry["failed_at"] = now_iso
                            failed_entry["reason"] = "target_missing"
                            state["failed"].append(failed_entry)
                            pruned_missing.append(item_id)
                        else:
                            still_pending.append(item)
                            if item_id in transient_ids:
                                skipped_transient.append(item_id)
                    state["pending"] = still_pending
                    state["failed"] = state["failed"][-_MAX_HISTORY:]
                    self._save(state)
        else:
            # dry-run: compute what would be pruned without mutating.
            pruned_ttl = list(ttl_expired_ids)
            pruned_missing = list(missing_ids)
            skipped_transient = list(transient_ids)

        # Log pruned items to team-log (best-effort).
        for item_id in pruned_ttl + pruned_missing:
            reason = "ttl_expired" if item_id in set(pruned_ttl) else "target_missing"
            # Find the item in snapshot for ref info.
            ref_item = next((i for i in snapshot if i["id"] == item_id), {})
            role = ref_item.get("role", "?")
            pr_num = ref_item.get("pr")
            disc_num = ref_item.get("discussion")
            if pr_num:
                ref = f"PR #{pr_num}"
            elif disc_num:
                ref = f"Discussion #{disc_num}"
            else:
                ref = item_id
            prefix = "[dry-run] " if dry_run else ""
            self._log_team(
                f"{prefix}queue item dropped — {role} for {ref}: {reason}"
            )

        kept_snapshot = len(snapshot) - len(ttl_expired_ids) - len(missing_ids)
        return {
            "pruned_ttl": pruned_ttl,
            "pruned_missing": pruned_missing,
            "kept": max(kept_snapshot, 0),
            "skipped_transient": skipped_transient,
        }

    def _validate_target(
        self,
        role: str,
        pr_num: int | None,
        discussion_num: int | None,
    ) -> Literal["ok", "missing", "transient"]:
        """
        Check whether the PR or Discussion referenced by a queue item exists.

        Returns:
          "ok"        — target confirmed to exist (or no target to check)
          "missing"   — definitively not found; item should be pruned
          "transient" — network/API error; leave item in pending
        """
        # PR check takes priority when both pr and discussion are set and role matches.
        if pr_num is not None and role in _PR_ROLES:
            return self._check_pr(pr_num)

        if discussion_num is not None and role in _DISCUSSION_ROLES:
            return self._check_discussion(discussion_num)

        return "ok"  # nothing to validate

    def _check_pr(self, pr_num: int) -> Literal["ok", "missing", "transient"]:
        """Validate that a PR exists on GitHub (any state)."""
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "view", str(pr_num),
                    "--repo", _GH_REPO,
                    "--json", "number,state",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return "transient"
        except OSError:
            return "transient"

        if result.returncode == 0:
            return "ok"

        stderr = result.stderr.lower()
        if self._is_transient_error(stderr):
            return "transient"
        # Non-zero + not transient → PR definitively not found.
        return "missing"

    def _check_discussion(self, discussion_num: int) -> Literal["ok", "missing", "transient"]:
        """Validate that a Discussion exists on GitHub."""
        _owner, _name = (_GH_REPO.split("/", 1) + [""])[:2]
        query = (
            f'query ($n: Int!) {{ repository(owner:"{_owner}", name:"{_name}") '
            '{ discussion(number:$n) { number } } }'
        )
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"n={discussion_num}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return "transient"
        except OSError:
            return "transient"

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if self._is_transient_error(stderr):
                return "transient"
            return "missing"

        # Parse JSON to check if discussion is null.
        try:
            data = json.loads(result.stdout)
            disc = (
                data.get("data", {})
                .get("repository", {})
                .get("discussion")
            )
            if disc is None:
                return "missing"
            return "ok"
        except (json.JSONDecodeError, AttributeError):
            # Malformed response — treat as transient to be safe.
            return "transient"

    @staticmethod
    def _is_transient_error(stderr_lower: str) -> bool:
        """Return True if the error message looks like a transient network issue."""
        transient_patterns = [
            "rate limit",
            "rate-limit",
            "timeout",
            "connection",
            "http 5",
            "502",
            "503",
            "504",
        ]
        return any(p in stderr_lower for p in transient_patterns)

    def _log_team(self, message: str) -> None:
        """Post a message to the team-log GitHub issue (best-effort; never raises)."""
        try:
            log_result = subprocess.run(
                [
                    "gh", "issue", "list",
                    "--label", "team-log",
                    "--state", "open",
                    "--json", "number",
                    "--jq", ".[0].number",
                    "--repo", _GH_REPO,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            log_num = log_result.stdout.strip()
            if not log_num:
                raise ValueError("No team-log issue found")
            subprocess.run(
                [
                    "gh", "issue", "comment", log_num,
                    "--body", f"[spawn-queue] team-lead: {message}",
                    "--repo", _GH_REPO,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            print(f"[spawn-queue] team-lead: {message}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_ttl_seconds(self) -> int:
        """Return policies.queue.ttl_seconds from config, falling back to _DEFAULT_TTL_SECONDS."""
        try:
            with self._config_file.open("r", encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, json.JSONDecodeError):
            config = {}
        return int(config.get("policies", {}).get("queue", {}).get("ttl_seconds", _DEFAULT_TTL_SECONDS))

    def _effective_limits(self) -> dict[str, int]:
        """Merge config.json spawn_limits over DEFAULT_LIMITS."""
        config = {}
        try:
            with self._config_file.open("r", encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
        overrides = config.get("settings", {}).get("spawn_limits", {})
        result = dict(DEFAULT_LIMITS)
        result.update(overrides)
        return result

    def _effective_timeouts(self) -> dict[str, int]:
        """Merge config.json policies timeout_minutes over DEFAULT_TIMEOUTS."""
        config = {}
        try:
            with self._config_file.open("r", encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
        policies = config.get("policies", {})
        result = dict(DEFAULT_TIMEOUTS)
        for role, policy in policies.items():
            if "timeout_minutes" in policy:
                result[role] = policy["timeout_minutes"]
        return result

    def _cleanup_stale(self, state: dict, timeouts: dict[str, int]) -> dict:
        """Auto-fail active agents that have exceeded their timeout. Mutates state."""
        now = datetime.now(timezone.utc)
        still_active = []
        for entry in state["active"]:
            role = entry.get("role", "unknown")
            timeout_minutes = timeouts.get(role, timeouts.get("_default", 60))
            started_raw = entry.get("started_at", "")
            try:
                started = datetime.fromisoformat(started_raw)
            except (ValueError, TypeError):
                still_active.append(entry)
                continue
            elapsed_minutes = (now - started).total_seconds() / 60
            if elapsed_minutes > timeout_minutes:
                failed_entry = dict(entry)
                failed_entry["failed_at"] = _now_iso()
                failed_entry["reason"] = "timeout"
                state["failed"].append(failed_entry)
            else:
                still_active.append(entry)
        state["failed"] = state["failed"][-_MAX_HISTORY:]
        state["active"] = still_active
        return state

    def _load(self) -> dict:
        """Load queue state from file. Returns empty state if file missing."""
        if not self._queue_file.exists():
            return {"pending": [], "active": [], "completed": [], "failed": []}
        try:
            with self._queue_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Ensure all keys exist.
            for key in ("pending", "active", "completed", "failed"):
                if key not in data:
                    data[key] = []
            return data
        except (OSError, json.JSONDecodeError):
            return {"pending": [], "active": [], "completed": [], "failed": []}

    def _save(self, state: dict) -> None:
        """Atomically write queue state using tmp-then-rename."""
        self._queue_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._queue_file.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.rename(tmp, self._queue_file)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _file_locked(self):
        """Context manager: acquire an exclusive flock on the lock file."""
        return _FileLock(self._lock_file)

    def _find_in(self, state: dict, request_id: str, exclude: str = "") -> dict | None:
        """Find a request in pending/active/completed/failed, excluding one section."""
        for section in ("pending", "active", "completed", "failed"):
            if section == exclude:
                continue
            for entry in state[section]:
                if entry.get("id") == request_id:
                    return entry
        return None

    def _remove_active(self, state: dict, request_id: str) -> dict | None:
        """Remove and return an entry from state['active'] by ID, or None."""
        for idx, entry in enumerate(state["active"]):
            if entry.get("id") == request_id:
                return state["active"].pop(idx)
        return None

    def _remove_pending(self, state: dict, request_id: str) -> dict | None:
        """Remove and return an entry from state['pending'] by ID, or None."""
        for idx, entry in enumerate(state["pending"]):
            if entry.get("id") == request_id:
                return state["pending"].pop(idx)
        return None


# ---------------------------------------------------------------------------
# flock context manager
# ---------------------------------------------------------------------------


class _FileLock:
    """Acquire an exclusive flock on *lock_file* for the duration of the with block."""

    def __init__(self, lock_file: Path) -> None:
        self._lock_file = lock_file
        self._fh = None

    def __enter__(self) -> "_FileLock":
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._lock_file.open("a", encoding="utf-8")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: object) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------


def _publish_spawned(role: str, request_id: str, discussion: int | None) -> None:
    """Publish AgentSpawnedEvent to the bus if available."""
    try:
        from backend.event_bus import Event, get_bus  # noqa: PLC0415

        @__import__("dataclasses").dataclass
        class AgentSpawnedEvent(Event):
            role: str = ""
            request_id: str = ""
            discussion: int | None = None

        get_bus().publish(AgentSpawnedEvent(
            source="spawn_queue",
            role=role,
            request_id=request_id,
            discussion=discussion,
        ))
    except Exception:  # noqa: BLE001
        pass  # never crash over event bus


def _publish_completed(role: str, request_id: str, discussion: int | None) -> None:
    """Publish AgentCompletedEvent to the bus if available."""
    try:
        from backend.event_bus import Event, get_bus  # noqa: PLC0415

        @__import__("dataclasses").dataclass
        class AgentCompletedEvent(Event):
            role: str = ""
            request_id: str = ""
            discussion: int | None = None

        get_bus().publish(AgentCompletedEvent(
            source="spawn_queue",
            role=role,
            request_id=request_id,
            discussion=discussion,
        ))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_queue_singleton: SpawnQueue | None = None


def get_spawn_queue() -> SpawnQueue:
    """Return the process-global SpawnQueue, creating it on first call."""
    global _queue_singleton
    if _queue_singleton is None:
        with _singleton_lock:
            if _queue_singleton is None:
                _queue_singleton = SpawnQueue()
    return _queue_singleton


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spawn_queue",
        description="Agent spawn queue — priority scheduling with concurrency limits.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show queue depth, active slots, and utilization")
    sub.add_parser("pending", help="List pending requests")
    sub.add_parser("active", help="List active agents")

    enq = sub.add_parser("enqueue", help="Add a spawn request to the queue")
    enq.add_argument("role", help="Agent role (e.g. executor, code-reviewer)")
    enq.add_argument(
        "discussion",
        type=int,
        nargs="?",
        default=None,
        help="Discussion number (optional)",
    )
    enq.add_argument("prompt_context", help="Prompt context string")
    enq.add_argument(
        "--priority",
        type=int,
        default=None,
        help="Priority level (lower = higher urgency, defaults by role)",
    )
    enq.add_argument(
        "--requested-by",
        default="cli",
        metavar="AGENT",
        help="Requesting agent name",
    )
    enq.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="PR_NUMBER",
        help="PR number this request targets (stored for reaper validation)",
    )
    enq.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override TTL for this item (default: policies.queue.ttl_seconds from config)",
    )

    cancel = sub.add_parser("cancel", help="Cancel a pending request by ID")
    cancel.add_argument("request_id", help="8-char request ID")

    sub.add_parser("drain", help="Mark all active agents as done (recovery)")

    reap_p = sub.add_parser("reap", help="Prune expired/invalid pending items")
    reap_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be pruned without mutating spawn-queue.json",
    )

    return p


def _fmt_status(st: dict) -> str:
    """Format status dict as a human-readable one-liner."""
    active = st["active_total"]
    total = st["total_limit"]
    pct = st["utilization_pct"]
    pending = st["pending"]
    role_parts = []
    for role, info in sorted(st["by_role"].items()):
        if info["active"] > 0:
            role_parts.append(f"{role}: {info['active']}/{info['limit']}")
    role_str = ", ".join(role_parts) if role_parts else "all slots free"
    return f"Active: {active}/{total} ({pct}%) | Pending: {pending} | {role_str}"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    q = get_spawn_queue()

    if args.command == "status":
        st = q.status()
        print(_fmt_status(st))
        print(json.dumps(st, indent=2))
        return 0

    if args.command == "pending":
        items = q.list_pending()
        if not items:
            print("No pending requests.")
            return 0
        for item in items:
            disc = f"  discussion={item['discussion']}" if item.get("discussion") else ""
            print(
                f"[{item['id']}] priority={item['priority']} role={item['role']}"
                f"{disc}  by={item.get('requested_by', '?')}  at={item.get('enqueued_at', '?')}"
            )
        return 0

    if args.command == "active":
        items = q.list_active()
        if not items:
            print("No active agents.")
            return 0
        for item in items:
            disc = f"  discussion={item['discussion']}" if item.get("discussion") else ""
            print(
                f"[{item['id']}] role={item['role']}{disc}"
                f"  started={item.get('started_at', '?')}"
            )
        return 0

    if args.command == "enqueue":
        req_id = q.enqueue(
            role=args.role,
            discussion=args.discussion,
            prompt_context=args.prompt_context,
            priority=args.priority,
            requested_by=args.requested_by,
            pr=args.pr,
            ttl_seconds=args.ttl_seconds,
        )
        print(f"Enqueued: {req_id}")
        return 0

    if args.command == "cancel":
        ok = q.cancel(args.request_id)
        if ok:
            print(f"Cancelled: {args.request_id}")
            return 0
        print(f"Not found in pending: {args.request_id}", file=sys.stderr)
        return 1

    if args.command == "drain":
        count = q.drain()
        print(f"Drained {count} active agent(s).")
        return 0

    if args.command == "reap":
        dry_run = getattr(args, "dry_run", False)
        result = q.reap(dry_run=dry_run)
        prefix = "[dry-run] " if dry_run else ""
        print(
            f"{prefix}Reaped: {len(result['pruned_ttl'])} TTL-expired, "
            f"{len(result['pruned_missing'])} target-missing, "
            f"{result['kept']} kept, "
            f"{len(result['skipped_transient'])} transient-skipped"
        )
        print(json.dumps(result, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
