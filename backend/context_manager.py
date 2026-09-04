"""
Project context manager — persistent memory for agent spawns.

Manages a structured JSON file at .autonomous-team/project-context.json.
Stores goals, decisions, milestones, tech stack, and banned approaches.
Uses flock + tmp-then-rename atomic write pattern (same as blackboard.py).

Usage (CLI):
    python backend/context_manager.py show
    python backend/context_manager.py prompt
    python backend/context_manager.py add-goal "Build interactive TUI"
    python backend/context_manager.py add-decision "Use ink for TUI" --rationale "React model"
    python backend/context_manager.py add-milestone "Ship TUI MVP"
    python backend/context_manager.py mark-done m1 --pr 42
    python backend/context_manager.py add-banned "tmux send-keys" --reason "PTY race"
    python backend/context_manager.py add-stack "TypeScript + ink (TUI)"

Usage (library):
    from backend.context_manager import ProjectContext
    ctx = ProjectContext()
    ctx.add_goal("Build interactive TUI", status="in-progress")
    print(ctx.format_for_prompt())
"""

import argparse
import copy
import fcntl
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_CONTEXT_FILENAME = "project-context.json"
_LOCK_FILENAME = ".project-context.lock"
_DEFAULT_STATE_DIR = Path(".autonomous-team")
_LOCK_TIMEOUT_SECONDS = 5

_EMPTY_SKELETON = {
    "version": 1,
    "updated_at": "",
    "updated_by": "",
    "goals": [],
    "decisions": [],
    "milestones": [],
    "stack": [],
    "banned": [],
}


class LockTimeout(TimeoutError):
    """Raised when flock cannot be acquired within the timeout."""


class ProjectContext:
    """
    Persistent project memory stored in a single JSON file.

    All write operations are atomic via flock + tmp-then-rename.
    """

    def __init__(self, state_dir: Path | str | None = None):
        if state_dir is None:
            here = Path(__file__).resolve().parent
            repo_root = here.parent
            self._state_dir = repo_root / _DEFAULT_STATE_DIR
        else:
            self._state_dir = Path(state_dir).resolve()
        self._data_path = self._state_dir / _CONTEXT_FILENAME
        self._lock_path = self._state_dir / _LOCK_FILENAME

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """Load current context. Returns empty skeleton if missing or corrupt."""
        if not self._data_path.exists():
            return copy.deepcopy(_EMPTY_SKELETON)
        try:
            with self._data_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Ensure all expected keys exist
            result = copy.deepcopy(_EMPTY_SKELETON)
            result.update(data)
            return result
        except (json.JSONDecodeError, OSError):
            return copy.deepcopy(_EMPTY_SKELETON)

    def get_goals(self) -> list:
        return self.load().get("goals", [])

    def get_decisions(self) -> list:
        return self.load().get("decisions", [])

    def get_milestones(self) -> list:
        return self.load().get("milestones", [])

    def get_stack(self) -> list:
        return self.load().get("stack", [])

    def get_banned(self) -> list:
        return self.load().get("banned", [])

    def format_for_prompt(self) -> str:
        """Return a human-readable context block suitable for prepending to agent prompts."""
        ctx = self.load()
        lines = ["## Project Context\n"]

        goals = ctx.get("goals", [])
        if goals:
            lines.append("### Goals")
            for g in goals:
                status = g.get("status", "")
                flag = " [done]" if status == "done" else " [in-progress]" if status == "in-progress" else ""
                lines.append(f"- [{g['id']}]{flag} {g['text']}")
            lines.append("")

        decisions = ctx.get("decisions", [])
        if decisions:
            lines.append("### Key Decisions")
            for d in decisions:
                line = f"- [{d['id']}] {d['text']}"
                if d.get("rationale"):
                    line += f" — {d['rationale']}"
                lines.append(line)
            lines.append("")

        stack = ctx.get("stack", [])
        if stack:
            lines.append("### Tech Stack")
            for s in stack:
                lines.append(f"- {s}")
            lines.append("")

        milestones = ctx.get("milestones", [])
        done = [m for m in milestones if m.get("status") == "done"]
        in_progress = [m for m in milestones if m.get("status") != "done"]
        if milestones:
            lines.append("### Milestones")
            for m in in_progress:
                lines.append(f"- [ ] [{m['id']}] {m['text']}")
            for m in done:
                pr_ref = f" (PR #{m['pr']})" if m.get("pr") else ""
                lines.append(f"- [x] [{m['id']}] {m['text']}{pr_ref}")
            lines.append("")

        banned = ctx.get("banned", [])
        if banned:
            lines.append("### Banned Approaches")
            for b in banned:
                reason = f": {b['reason']}" if b.get("reason") else ""
                lines.append(f"- [{b['id']}] {b['approach']}{reason}")
            lines.append("")

        result = "\n".join(lines)
        # Truncate to stay under 2000 chars, preserving structure
        if len(result) > 2000:
            result = result[:1950] + "\n... (truncated)\n"
        return result

    # ------------------------------------------------------------------
    # Write operations (all atomic via flock)
    # ------------------------------------------------------------------

    def save(self, data: dict, updated_by: str = "cli") -> None:
        """Atomically write *data* to the context file."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _now_iso()
        data["updated_by"] = updated_by
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

    def add_goal(self, text: str, status: str = "in-progress", updated_by: str = "cli") -> str:
        """Add a goal. Returns the assigned ID."""
        with self._locked():
            ctx = self.load()
            goals = ctx.setdefault("goals", [])
            new_id = f"g{len(goals) + 1}"
            goals.append({
                "id": new_id,
                "text": text,
                "status": status,
                "added": _today(),
            })
            self.save(ctx, updated_by=updated_by)
        return new_id

    def add_decision(self, text: str, rationale: str = "", discussion: int | None = None, updated_by: str = "cli") -> str:
        """Add a key decision. Returns the assigned ID."""
        with self._locked():
            ctx = self.load()
            decisions = ctx.setdefault("decisions", [])
            new_id = f"d{len(decisions) + 1}"
            entry: dict = {"id": new_id, "text": text, "date": _today()}
            if rationale:
                entry["rationale"] = rationale
            if discussion is not None:
                entry["discussion"] = discussion
            decisions.append(entry)
            self.save(ctx, updated_by=updated_by)
        return new_id

    def add_milestone(self, text: str, discussion: int | None = None, updated_by: str = "cli") -> str:
        """Add a milestone. Returns the assigned ID."""
        with self._locked():
            ctx = self.load()
            milestones = ctx.setdefault("milestones", [])
            new_id = f"m{len(milestones) + 1}"
            entry: dict = {"id": new_id, "text": text, "status": "pending"}
            if discussion is not None:
                entry["discussion"] = discussion
            milestones.append(entry)
            self.save(ctx, updated_by=updated_by)
        return new_id

    def mark_milestone_done(self, milestone_id: str, pr: int | None = None, updated_by: str = "cli") -> bool:
        """Mark a milestone as done. Returns True if found and updated."""
        with self._locked():
            ctx = self.load()
            for m in ctx.get("milestones", []):
                if m["id"] == milestone_id:
                    m["status"] = "done"
                    if pr is not None:
                        m["pr"] = pr
                    self.save(ctx, updated_by=updated_by)
                    return True
        return False

    def add_banned(self, approach: str, reason: str = "", updated_by: str = "cli") -> str:
        """Add a banned approach. Returns the assigned ID."""
        with self._locked():
            ctx = self.load()
            banned = ctx.setdefault("banned", [])
            new_id = f"b{len(banned) + 1}"
            entry: dict = {"id": new_id, "approach": approach, "date": _today()}
            if reason:
                entry["reason"] = reason
            banned.append(entry)
            self.save(ctx, updated_by=updated_by)
        return new_id

    def add_stack(self, entry: str, updated_by: str = "cli") -> None:
        """Add a tech stack entry (if not already present)."""
        with self._locked():
            ctx = self.load()
            stack = ctx.setdefault("stack", [])
            if entry not in stack:
                stack.append(entry)
                self.save(ctx, updated_by=updated_by)

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
                raise LockTimeout(f"Could not acquire project-context lock within {_LOCK_TIMEOUT_SECONDS}s")
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


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="context_manager",
        description="Persistent project memory for the autonomous team.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="Print full context as JSON")
    sub.add_parser("prompt", help="Print human-readable context for agent prompt injection")

    ag = sub.add_parser("add-goal", help="Add a project goal")
    ag.add_argument("text")
    ag.add_argument("--status", default="in-progress", choices=["in-progress", "done", "blocked"])
    ag.add_argument("--updated-by", default="cli")

    ad = sub.add_parser("add-decision", help="Add a key decision")
    ad.add_argument("text")
    ad.add_argument("--rationale", default="")
    ad.add_argument("--discussion", type=int, default=None)
    ad.add_argument("--updated-by", default="cli")

    am = sub.add_parser("add-milestone", help="Add a milestone")
    am.add_argument("text")
    am.add_argument("--discussion", type=int, default=None)
    am.add_argument("--updated-by", default="cli")

    md = sub.add_parser("mark-done", help="Mark a milestone as done")
    md.add_argument("id", help="Milestone ID (e.g. m1)")
    md.add_argument("--pr", type=int, default=None)
    md.add_argument("--updated-by", default="cli")

    ab = sub.add_parser("add-banned", help="Add a banned approach")
    ab.add_argument("approach")
    ab.add_argument("--reason", default="")
    ab.add_argument("--updated-by", default="cli")

    ast = sub.add_parser("add-stack", help="Add a tech stack entry")
    ast.add_argument("entry")
    ast.add_argument("--updated-by", default="cli")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    ctx = ProjectContext()

    if args.command == "show":
        print(json.dumps(ctx.load(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "prompt":
        print(ctx.format_for_prompt())
        return 0

    if args.command == "add-goal":
        new_id = ctx.add_goal(args.text, status=args.status, updated_by=args.updated_by)
        print(f"added goal {new_id}")
        return 0

    if args.command == "add-decision":
        new_id = ctx.add_decision(
            args.text,
            rationale=args.rationale,
            discussion=args.discussion,
            updated_by=args.updated_by,
        )
        print(f"added decision {new_id}")
        return 0

    if args.command == "add-milestone":
        new_id = ctx.add_milestone(args.text, discussion=args.discussion, updated_by=args.updated_by)
        print(f"added milestone {new_id}")
        return 0

    if args.command == "mark-done":
        ok = ctx.mark_milestone_done(args.id, pr=args.pr, updated_by=args.updated_by)
        if ok:
            print(f"milestone {args.id} marked done")
            return 0
        else:
            print(f"milestone not found: {args.id}", file=sys.stderr)
            return 1

    if args.command == "add-banned":
        new_id = ctx.add_banned(args.approach, reason=args.reason, updated_by=args.updated_by)
        print(f"added banned approach {new_id}")
        return 0

    if args.command == "add-stack":
        ctx.add_stack(args.entry, updated_by=args.updated_by)
        print(f"added stack entry: {args.entry}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
