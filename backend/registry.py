"""
Project registry — syncs Discussion state from GitHub and computes velocity metrics.

Fetches all Discussions (open + closed) via gh api graphql, parses STATUS lines
from Discussion bodies, stores results in .autonomous-team/registry.json.
Uses flock + tmp-then-rename atomic write pattern (same as context_manager.py).

Usage (CLI):
    python backend/registry.py sync
    python backend/registry.py show            # full dump, open + closed
    python backend/registry.py stats
    python backend/registry.py queue-summary    # open-only bucket counts

Usage (library):
    from backend.registry import DiscussionRegistry
    reg = DiscussionRegistry()
    reg.sync()
    print(reg.stats())
"""

if __name__ == '__main__' and __package__ is None:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import copy
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_REGISTRY_FILENAME = "registry.json"
_LOCK_FILENAME = ".registry.lock"
_DEFAULT_STATE_DIR = Path(".autonomous-team")
_LOCK_TIMEOUT_SECONDS = 10

from backend._repo import REPO as _REPO  # noqa: E402 (after Path constants)
from backend._repo import REPO_OWNER as _REPO_OWNER, REPO_NAME as _REPO_NAME  # noqa: E402

# Matches <!-- STATUS:XXX --> or <!-- STATUS:XXX SINCE:... --> or <!-- STATUS:XXX PR:#N SINCE:... -->
_STATUS_RE = re.compile(r"<!--\s*STATUS:(\w+)(?:[^>]*)-->")
_PR_RE = re.compile(r"<!--\s*STATUS:[^>]*\bPR:#?(\d+)[^>]*-->")

_EMPTY_REGISTRY = {
    "version": 1,
    "synced_at": "",
    "discussions": [],
    "velocity": {},
}


class LockTimeout(TimeoutError):
    """Raised when flock cannot be acquired within the timeout."""


class DiscussionRegistry:
    """
    Project registry backed by a single JSON file.

    Syncs GitHub Discussion state via gh api graphql.
    All write operations are atomic via flock + tmp-then-rename.
    """

    def __init__(self, state_dir: Path | str | None = None):
        if state_dir is None:
            here = Path(__file__).resolve().parent
            repo_root = here.parent
            self._state_dir = repo_root / _DEFAULT_STATE_DIR
        else:
            self._state_dir = Path(state_dir).resolve()
        self._data_path = self._state_dir / _REGISTRY_FILENAME
        self._lock_path = self._state_dir / _LOCK_FILENAME
        # Tracks whether the last _fetch_all_discussions() call completed all pages.
        # Set to False when pagination is interrupted by an error so sync() can
        # skip the write and preserve the previous registry instead of truncating it.
        self._last_fetch_complete: bool = True

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """Load registry from disk. Returns empty skeleton if missing or corrupt."""
        if not self._data_path.exists():
            return copy.deepcopy(_EMPTY_REGISTRY)
        try:
            with self._data_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            result = copy.deepcopy(_EMPTY_REGISTRY)
            result.update(data)
            return result
        except (json.JSONDecodeError, OSError):
            return copy.deepcopy(_EMPTY_REGISTRY)

    def show(self) -> dict:
        """Return current registry data (from disk)."""
        return self.load()

    def stats(self) -> dict:
        """
        Compute velocity metrics from the current registry.

        Returns:
            {
                "total": int,
                "done": int,
                "in_progress": int,
                "tasks_per_day": float,
                "avg_days_to_complete": float | None,
                "completion_count": int,
            }
        """
        reg = self.load()
        discussions = reg.get("discussions", [])

        # Only count open discussions (closed_at is None) for active status metrics.
        # DONE discussions are always closed, so include them for velocity.
        open_discussions = self._open_only(discussions)
        total = len(open_discussions)
        done_items = [d for d in discussions if d.get("status") == "DONE"]
        in_progress = [d for d in open_discussions if d.get("status") in ("IMPLEMENTING", "REVIEWING")]
        spec_ready = [d for d in open_discussions if d.get("status") == "SPEC_READY"]
        done_count = len(done_items)

        # Compute average days to complete (created_at → closed_at or synced_at)
        durations: list[float] = []
        for d in done_items:
            created = d.get("created_at")
            closed = d.get("closed_at") or d.get("synced_at")
            if created and closed:
                try:
                    t_start = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    t_end = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    delta = (t_end - t_start).total_seconds() / 86400.0
                    if delta >= 0:
                        durations.append(delta)
                except ValueError:
                    pass

        avg_days = (sum(durations) / len(durations)) if durations else None

        # Tasks per day: done_count / days since oldest created_at
        tasks_per_day = 0.0
        all_dates = [d.get("created_at") for d in discussions if d.get("created_at")]
        if all_dates and done_count > 0:
            try:
                oldest = min(
                    datetime.fromisoformat(dt.replace("Z", "+00:00")) for dt in all_dates
                )
                now = datetime.now(timezone.utc)
                span_days = (now - oldest).total_seconds() / 86400.0
                if span_days > 0:
                    tasks_per_day = round(done_count / span_days, 3)
            except ValueError:
                pass

        return {
            "total": total,
            "done": done_count,
            "in_progress": len(in_progress),
            "spec_ready": len(spec_ready),
            "tasks_per_day": tasks_per_day,
            "avg_days_to_complete": round(avg_days, 2) if avg_days is not None else None,
            "completion_count": done_count,
        }

    def queue_summary(self) -> dict:
        """
        Open-only status-bucket counts for "what's actionable right now"
        reporting (D#2310).

        Distinct from show() (the full, unfiltered dump — that stays the
        contract of `registry show`) and stats() (lifetime/velocity metrics
        that intentionally count DONE and total lifetime work). This is the
        single implementation of the open-row filter for every queue-count
        consumer: `backend/cli.py`'s `status` command and
        `scripts/loop-preflight.sh`'s registry summary both read this instead
        of re-deriving their own (unfiltered) count.

        Returns:
            {
                "total": int,            # every row on disk, open + closed —
                                          # same population `show` prints
                "open_total": int,       # rows with closed_at is None
                "excluded_closed": int,  # total - open_total: rows dropped
                                          # from `buckets` below, named so a
                                          # reader can see what was excluded
                                          # rather than an unexplained drop
                "buckets": {status: count, ...},  # OPEN-ONLY, one entry per
                                          # distinct status seen among open
                                          # rows (a status with zero open
                                          # rows is simply absent)
                "done": int,             # DONE rows counted over ALL rows —
                                          # DONE discussions are always
                                          # closed, so open-only would
                                          # (wrongly) read ~0
                "synced_at": str,        # passthrough from the registry file
            }
        """
        reg = self.load()
        discussions = reg.get("discussions", [])
        total = len(discussions)
        open_discussions = self._open_only(discussions)

        buckets: dict[str, int] = {}
        for d in open_discussions:
            status = d.get("status") or "UNKNOWN"
            buckets[status] = buckets.get(status, 0) + 1

        done_count = sum(1 for d in discussions if d.get("status") == "DONE")

        return {
            "total": total,
            "open_total": len(open_discussions),
            "excluded_closed": total - len(open_discussions),
            "buckets": buckets,
            "done": done_count,
            "synced_at": reg.get("synced_at", ""),
        }

    @staticmethod
    def _open_only(discussions: list[dict]) -> list[dict]:
        """The one open-row filter every queue-count consumer must share
        (D#2310): a Discussion counts as open iff `closed_at` is None.
        Reused by stats(), _compute_velocity(), and queue_summary() so the
        rule is defined in exactly one place.
        """
        return [d for d in discussions if d.get("closed_at") is None]

    # ------------------------------------------------------------------
    # Sync operation
    # ------------------------------------------------------------------

    def sync(self) -> dict:
        """
        Fetch all Discussions from GitHub and write registry.json atomically.

        Returns the updated registry dict.
        """
        # Load previous state to detect status transitions.
        prev_registry = self.load()
        prev_by_number: dict[int, dict] = {
            d["number"]: d for d in prev_registry.get("discussions", [])
        }

        raw_discussions = self._fetch_all_discussions()

        if not self._last_fetch_complete:
            # Pagination was interrupted by a transient error — the result is partial.
            # Writing it would silently delete all discussions that weren't fetched yet.
            # Return the previous registry unchanged so callers see consistent data.
            print(
                "registry: fetch incomplete (mid-pagination error); "
                "skipping write to preserve existing registry",
                file=sys.stderr,
            )
            return prev_registry

        parsed = [self._parse_discussion(d) for d in raw_discussions]

        now = _now_iso()
        velocity = self._compute_velocity(parsed, now)

        registry = {
            "version": 1,
            "synced_at": now,
            "discussions": parsed,
            "velocity": velocity,
        }

        with self._locked():
            self._write(registry)

        # Emit audit events for status transitions (best-effort).
        try:
            from backend.audit_trail import get_audit_trail  # noqa: PLC0415
            at = get_audit_trail()
            for disc in parsed:
                number = disc["number"]
                new_status = disc.get("status")
                prev = prev_by_number.get(number)
                old_status = prev.get("status") if prev else None
                if old_status != new_status:
                    at.emit(
                        "registry", "transition",
                        f"discussion/{number}",
                        old_status,
                        new_status,
                        "registry-sync",
                    )
        except Exception:  # noqa: BLE001
            pass

        return registry

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

    def _fetch_all_discussions(self) -> list[dict]:
        """
        Fetch all Discussions (open + closed) via gh api graphql, paginating as needed.
        Returns raw GraphQL node dicts.

        Sets self._last_fetch_complete = True on a clean run, False when a
        mid-pagination error forces an early return. sync() reads this flag to
        decide whether writing the result is safe.
        """
        # Assume success; set to False on any early-exit error path.
        self._last_fetch_complete = True
        results = []
        after: str | None = None

        while True:
            after_arg = f', after: "{after}"' if after else ""
            query = f"""
            query {{
              repository(owner: "{_REPO_OWNER}", name: "{_REPO_NAME}") {{
                discussions(first: 50{after_arg}) {{
                  pageInfo {{
                    hasNextPage
                    endCursor
                  }}
                  nodes {{
                    number
                    title
                    body
                    createdAt
                    closedAt
                    isAnswered
                    category {{
                      name
                    }}
                    labels(first: 10) {{
                      nodes {{
                        name
                      }}
                    }}
                  }}
                }}
              }}
            }}
            """
            try:
                output = subprocess.check_output(
                    ["gh", "api", "graphql", "-f", f"query={query}"],
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                print(
                    f"registry: gh api graphql failed (page after={after!r}): {exc}",
                    file=sys.stderr,
                )
                self._last_fetch_complete = False
                return results  # return whatever we fetched so far

            try:
                data = json.loads(output)
                disc_data = data["data"]["repository"]["discussions"]
                nodes = disc_data["nodes"]
                has_next = disc_data["pageInfo"]["hasNextPage"]
                end_cursor = disc_data["pageInfo"]["endCursor"]
            except (json.JSONDecodeError, KeyError) as exc:
                print(
                    f"registry: malformed gh api response (page after={after!r}): {exc}",
                    file=sys.stderr,
                )
                self._last_fetch_complete = False
                return results  # return whatever we fetched so far

            results.extend(nodes)
            if not has_next:
                break
            after = end_cursor

        return results

    def _parse_discussion(self, node: dict) -> dict:
        """Parse a raw GraphQL Discussion node into a registry entry."""
        body = node.get("body") or ""
        status = self._parse_status(body)
        pr = self._parse_pr(body)

        labels = [lbl["name"] for lbl in node.get("labels", {}).get("nodes", [])]
        category = node.get("category", {}) or {}

        entry: dict = {
            "number": node["number"],
            "title": node["title"],
            "status": status,
            "category": category.get("name", ""),
            "created_at": node.get("createdAt", ""),
            "closed_at": node.get("closedAt") or None,
            "pr": pr,
            "labels": labels,
        }

        frontmatter = self._parse_frontmatter(body)
        if frontmatter:
            entry["frontmatter"] = frontmatter

        completion = self._parse_completion_summary(body)
        if completion:
            entry["completion"] = completion

        return entry

    def _parse_frontmatter(self, body: str) -> dict:
        """Parse YAML frontmatter from a Discussion body. Returns {} if absent."""
        try:
            from backend.task_specs import _parse_frontmatter as _ts_parse  # noqa: PLC0415
        except ModuleNotFoundError:
            from task_specs import _parse_frontmatter as _ts_parse  # type: ignore[no-redef]  # noqa: PLC0415
        return _ts_parse(body)

    def _parse_completion_summary(self, body: str) -> dict:
        """Parse completion summary block from a Discussion body. Returns {} if absent."""
        try:
            from backend.task_specs import _parse_completion_summary as _ts_cs  # noqa: PLC0415
        except ModuleNotFoundError:
            from task_specs import _parse_completion_summary as _ts_cs  # type: ignore[no-redef]  # noqa: PLC0415
        return _ts_cs(body)

    def _parse_status(self, body: str) -> str:
        """Extract STATUS value from Discussion body HTML comment, or infer from content."""
        m = _STATUS_RE.search(body)
        if m:
            return m.group(1)
        # No STATUS comment — treat as DISCUSSING (no spec yet)
        return "DISCUSSING"

    def _parse_pr(self, body: str) -> int | None:
        """Extract PR number from STATUS comment if present."""
        m = _PR_RE.search(body)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return None

    # ------------------------------------------------------------------
    # Velocity computation
    # ------------------------------------------------------------------

    def _compute_velocity(self, discussions: list[dict], now: str) -> dict:
        """Compute velocity metrics from parsed discussion list."""
        # Count only open discussions for active work metrics; all for done/velocity.
        open_discussions = self._open_only(discussions)
        total = len(open_discussions)
        done = [d for d in discussions if d.get("status") == "DONE"]
        in_progress = [d for d in open_discussions if d.get("status") in ("IMPLEMENTING", "REVIEWING")]
        done_count = len(done)

        durations: list[float] = []
        for d in done:
            created = d.get("created_at")
            closed = d.get("closed_at") or now
            if created:
                try:
                    t_start = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    t_end = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    delta = (t_end - t_start).total_seconds() / 86400.0
                    if delta >= 0:
                        durations.append(delta)
                except ValueError:
                    pass

        avg_days = round(sum(durations) / len(durations), 2) if durations else None

        tasks_per_day = 0.0
        all_dates = [d.get("created_at") for d in discussions if d.get("created_at")]
        if all_dates and done_count > 0:
            try:
                oldest = min(
                    datetime.fromisoformat(dt.replace("Z", "+00:00")) for dt in all_dates
                )
                now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
                span_days = (now_dt - oldest).total_seconds() / 86400.0
                if span_days > 0:
                    tasks_per_day = round(done_count / span_days, 3)
            except ValueError:
                pass

        return {
            "total": total,
            "done": done_count,
            "in_progress": len(in_progress),
            "tasks_per_day": tasks_per_day,
            "avg_days_to_complete": avg_days,
        }

    # ------------------------------------------------------------------
    # Atomic write
    # ------------------------------------------------------------------

    def _write(self, data: dict) -> None:
        """Atomically write data to registry.json via tmp-then-rename."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        dest = self._data_path
        tmp = dest.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.rename(tmp, dest)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locked(self):
        return _LockedCtx(self._lock_path)


class _LockedCtx:
    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._fh = None

    def __enter__(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        is_main = threading.current_thread() is threading.main_thread()
        if is_main:
            def _timeout(signum, frame):
                raise LockTimeout(f"Could not acquire registry lock within {_LOCK_TIMEOUT_SECONDS}s")
            old = signal.signal(signal.SIGALRM, _timeout)
            signal.alarm(_LOCK_TIMEOUT_SECONDS)
        try:
            fh = self._lock_path.open("a", encoding="utf-8")
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._fh = fh
        except BaseException:
            if is_main:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
            raise
        if is_main:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
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


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="registry",
        description="Project registry — Discussion state and velocity metrics.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="Fetch all Discussions from GitHub and update registry.json")
    sub.add_parser(
        "show",
        help=(
            "Print the full registry dump as JSON — every Discussion row, "
            "open AND closed. Not filtered to queue state; use "
            "'queue-summary' for open-only counts."
        ),
    )
    sub.add_parser("stats", help="Print velocity metrics in human-readable form")
    sub.add_parser(
        "queue-summary",
        help=(
            "Print open-only status-bucket counts (queue state) as JSON, "
            "plus how many rows were excluded as closed. See 'show' for the "
            "full open+closed dump."
        ),
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    reg = DiscussionRegistry()

    if args.command == "sync":
        try:
            result = reg.sync()
        except subprocess.CalledProcessError as exc:
            print(f"gh api graphql failed: {exc}", file=sys.stderr)
            return 1
        except LockTimeout as exc:
            print(str(exc), file=sys.stderr)
            return 1
        v = result.get("velocity") or {}
        print(
            f"synced: {v.get('total', 0)} discussions "
            f"({v.get('done', 0)} done, {v.get('in_progress', 0)} in-progress)"
        )
        return 0

    if args.command == "show":
        data = reg.show()
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if args.command == "queue-summary":
        try:
            data = reg.queue_summary()
        except Exception as exc:  # noqa: BLE001
            print(f"error computing queue summary: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if args.command == "stats":
        try:
            s = reg.stats()
        except Exception as exc:
            print(f"error computing stats: {exc}", file=sys.stderr)
            return 1
        print(f"Total discussions:       {s['total']}")
        print(f"Completed (DONE):        {s['done']}")
        print(f"In progress:             {s['in_progress']}")
        print(f"Tasks per day:           {s['tasks_per_day']}")
        if s["avg_days_to_complete"] is not None:
            print(f"Avg days to complete:    {s['avg_days_to_complete']}")
        else:
            print("Avg days to complete:    n/a (no completed tasks with timestamps)")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
