"""
lessons.py — Quality-score-driven lessons feedback loop for executor spawns.

After every merge, sub-threshold quality dimensions emit one-line lessons to
.autonomous-team/lessons/<dimension>.jsonl. pre-spawn-check.sh reads recent
matching lessons and injects up to 3 into the spawn prompt so the next executor
knows what went wrong last time.

INTERNAL: written only by post-merge-hook.sh via the quality_scorer integration.
Agents MUST NOT call lessons.record() directly.

Usage (CLI):
    python3 backend/lessons.py record --pr 999 --dimension test_coverage --score 32 \\
        --lesson "Add test file for every new module" --files-pattern "dashboard/**"
    python3 backend/lessons.py list [--dimension test_coverage] [--limit 10]
    python3 backend/lessons.py clear --dimension test_coverage [--yes]
    python3 backend/lessons.py pick-for-prompt --role executor --files "dashboard/src/App.tsx" \\
        --max 3 [--json]

Usage (library):
    from backend.lessons import LessonsStore
    store = LessonsStore()
    store.record(pr=99, dimension="test_coverage", score=32, lesson="...", files_pattern="dashboard/**")
    lessons = store.pick_for_prompt(role="executor", files_globs=["dashboard/**"], max_lessons=3)
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

_DEFAULT_AGE_DAYS = 30
_DEFAULT_MAX_COUNT = 50
_LESSONS_DIR = Path(".autonomous-team/lessons")


def _repo_root() -> Path:
    """Return the repo root by walking up from this file."""
    return Path(__file__).resolve().parent.parent


def _lessons_dir() -> Path:
    return _repo_root() / _LESSONS_DIR


def _load_config() -> dict:
    """Load lessons config from .autonomous-team/config.json, with defaults."""
    config_path = _repo_root() / ".autonomous-team" / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        return raw.get("lessons", {})
    except Exception:
        return {}


def _age_days_cfg(cfg: dict) -> int:
    return int(cfg.get("age_days", _DEFAULT_AGE_DAYS))


def _max_count_cfg(cfg: dict) -> int:
    return int(cfg.get("max_count", _DEFAULT_MAX_COUNT))


# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------

class LessonEntry:
    """One lesson record."""

    def __init__(
        self,
        pr: int,
        dimension: str,
        score: float,
        lesson: str,
        files_pattern: str,
        recorded_at: Optional[str] = None,
        role: Optional[str] = None,
    ) -> None:
        self.pr = pr
        self.dimension = dimension
        self.score = score
        self.lesson = lesson
        self.files_pattern = files_pattern
        self.recorded_at = recorded_at or datetime.now(timezone.utc).isoformat()
        self.role = role or "executor"

    def to_dict(self) -> dict:
        return {
            "pr": self.pr,
            "dimension": self.dimension,
            "score": self.score,
            "lesson": self.lesson,
            "files_pattern": self.files_pattern,
            "recorded_at": self.recorded_at,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LessonEntry":
        return cls(
            pr=int(d["pr"]),
            dimension=str(d["dimension"]),
            score=float(d.get("score", 0)),
            lesson=str(d["lesson"]),
            files_pattern=str(d.get("files_pattern", "*")),
            recorded_at=d.get("recorded_at"),
            role=d.get("role", "executor"),
        )

    def age_days(self) -> float:
        try:
            recorded = datetime.fromisoformat(self.recorded_at)
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (now - recorded).total_seconds() / 86400
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class LessonsStore:
    """Read/write lessons JSONL files, one per dimension."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._base = base_dir or _lessons_dir()
        cfg = _load_config()
        self._age_days = _age_days_cfg(cfg)
        self._max_count = _max_count_cfg(cfg)

    def _dimension_path(self, dimension: str) -> Path:
        safe_dim = "".join(c if c.isalnum() or c == "_" else "_" for c in dimension)
        return self._base / f"{safe_dim}.jsonl"

    def record(
        self,
        pr: int,
        dimension: str,
        score: float,
        lesson: str,
        files_pattern: str,
        role: str = "executor",
    ) -> None:
        """Append a lesson to .autonomous-team/lessons/<dimension>.jsonl.

        INTERNAL: called only from post-merge-hook.sh / quality_scorer integration.
        Agents must not call this directly.
        """
        self._base.mkdir(parents=True, exist_ok=True)
        entry = LessonEntry(
            pr=pr,
            dimension=dimension,
            score=score,
            lesson=lesson,
            files_pattern=files_pattern,
            role=role,
        )
        path = self._dimension_path(dimension)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def _read_dimension(self, dimension: str) -> list[LessonEntry]:
        """Read all lines from a dimension file, skipping malformed JSON."""
        path = self._dimension_path(dimension)
        if not path.exists():
            return []
        entries: list[LessonEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(LessonEntry.from_dict(json.loads(line)))
            except Exception:
                pass
        return entries

    def _filter_entries(self, entries: list[LessonEntry]) -> list[LessonEntry]:
        """Apply age and count expiry filters. Newest first, then cap at max_count."""
        cutoff_days = self._age_days
        fresh = [e for e in entries if e.age_days() <= cutoff_days]
        # Sort newest first
        fresh.sort(key=lambda e: e.recorded_at, reverse=True)
        # Cap at max_count
        return fresh[: self._max_count]

    def list_lessons(
        self,
        dimension: Optional[str] = None,
        limit: int = 20,
    ) -> list[LessonEntry]:
        """Return recent lessons, optionally filtered by dimension."""
        if dimension:
            raw = self._read_dimension(dimension)
            return self._filter_entries(raw)[:limit]

        # All dimensions
        all_entries: list[LessonEntry] = []
        if self._base.exists():
            for path in sorted(self._base.glob("*.jsonl")):
                dim = path.stem
                all_entries.extend(self._read_dimension(dim))
        filtered = self._filter_entries(all_entries)
        return filtered[:limit]

    def clear(self, dimension: Optional[str] = None) -> int:
        """Truncate lesson files. Returns count of files cleared."""
        cleared = 0
        if dimension:
            path = self._dimension_path(dimension)
            if path.exists():
                path.write_text("", encoding="utf-8")
                cleared = 1
        else:
            if self._base.exists():
                for path in self._base.glob("*.jsonl"):
                    path.write_text("", encoding="utf-8")
                    cleared += 1
        return cleared

    def pick_for_prompt(
        self,
        role: str,
        files_globs: list[str],
        max_lessons: int = 3,
    ) -> list[dict]:
        """Return up to max_lessons lessons matching role and any files_glob.

        Ranking: most recent first, dimension diversity (no two from the same
        dimension unless no alternative).

        Returns dicts suitable for JSON serialisation.
        """
        all_lessons = self.list_lessons(limit=1000)

        # Filter by role
        role_filtered = [e for e in all_lessons if e.role == role]
        if not role_filtered:
            # Fall back to all lessons regardless of role
            role_filtered = all_lessons

        # Filter by file globs — a lesson matches if any glob matches its files_pattern
        # OR if no globs provided (return all)
        if files_globs:
            matched: list[LessonEntry] = []
            for entry in role_filtered:
                for glob in files_globs:
                    if (
                        fnmatch.fnmatch(entry.files_pattern, glob)
                        or fnmatch.fnmatch(glob, entry.files_pattern)
                        or entry.files_pattern == "*"
                        or glob == "*"
                    ):
                        matched.append(entry)
                        break
            # If nothing matched, fall back to all role_filtered
            if not matched:
                matched = role_filtered
        else:
            matched = role_filtered

        # Dimension diversity: pick one per dimension first, then fill remaining slots
        seen_dims: set[str] = set()
        selected: list[LessonEntry] = []
        # First pass: one per dimension (newest first within each dim via _filter_entries)
        for entry in matched:
            if entry.dimension not in seen_dims:
                selected.append(entry)
                seen_dims.add(entry.dimension)
            if len(selected) >= max_lessons:
                break

        # Second pass: fill remaining slots from any dimension
        if len(selected) < max_lessons:
            for entry in matched:
                if entry not in selected:
                    selected.append(entry)
                if len(selected) >= max_lessons:
                    break

        return [e.to_dict() for e in selected[:max_lessons]]


# ---------------------------------------------------------------------------
# Prompt rendering helper
# ---------------------------------------------------------------------------

def render_lessons_block(lessons: list[dict]) -> str:
    """Render lessons as a markdown block for injection into spawn prompts.

    Returns an empty string when lessons is empty.
    """
    if not lessons:
        return ""
    lines = ["## Recent lessons from low-scoring PRs", ""]
    for i, lesson in enumerate(lessons, 1):
        dim = lesson.get("dimension", "unknown")
        score = lesson.get("score", 0)
        text = lesson.get("lesson", "")
        pr = lesson.get("pr", "?")
        pattern = lesson.get("files_pattern", "*")
        lines.append(f"{i}. **[{dim} score={score}] PR #{pr}** (`{pattern}`): {text}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_record(args: argparse.Namespace) -> None:
    store = LessonsStore()
    store.record(
        pr=args.pr,
        dimension=args.dimension,
        score=args.score,
        lesson=args.lesson,
        files_pattern=args.files_pattern,
        role=getattr(args, "role", "executor"),
    )
    print(
        f"Recorded lesson for PR #{args.pr} dimension={args.dimension} "
        f"score={args.score} pattern={args.files_pattern}"
    )


def _cmd_list(args: argparse.Namespace) -> None:
    store = LessonsStore()
    entries = store.list_lessons(dimension=getattr(args, "dimension", None), limit=args.limit)
    if not entries:
        print("No lessons found.")
        return
    for e in entries:
        age = round(e.age_days(), 1)
        print(
            f"  PR#{e.pr} [{e.dimension}={e.score}] age={age}d pattern={e.files_pattern}: {e.lesson}"
        )


def _cmd_clear(args: argparse.Namespace) -> None:
    if not getattr(args, "yes", False):
        confirm = input(
            f"Clear lessons{f' for dimension {args.dimension}' if args.dimension else ''}? [y/N] "
        ).strip().lower()
        if confirm != "y":
            print("Aborted.")
            return
    store = LessonsStore()
    n = store.clear(dimension=getattr(args, "dimension", None))
    print(f"Cleared {n} lesson file(s).")


def _cmd_pick_for_prompt(args: argparse.Namespace) -> None:
    store = LessonsStore()
    files_globs = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else []
    lessons = store.pick_for_prompt(
        role=args.role,
        files_globs=files_globs,
        max_lessons=args.max,
    )
    if getattr(args, "json_out", False):
        print(json.dumps(lessons, indent=2))
    else:
        block = render_lessons_block(lessons)
        if block:
            print(block)
        else:
            print("No matching lessons.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quality-score-driven lessons store for executor spawns."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # record
    p_rec = sub.add_parser("record", help="Append a lesson to the store.")
    p_rec.add_argument("--pr", type=int, required=True, help="PR number")
    p_rec.add_argument("--dimension", required=True, help="Quality dimension (e.g. test_coverage)")
    p_rec.add_argument("--score", type=float, required=True, help="Dimension score")
    p_rec.add_argument("--lesson", required=True, help="One-line lesson text")
    p_rec.add_argument("--files-pattern", dest="files_pattern", default="*", help="Glob pattern for affected files")
    p_rec.add_argument("--role", default="executor", help="Target agent role (default: executor)")

    # list
    p_list = sub.add_parser("list", help="Print recent lessons.")
    p_list.add_argument("--dimension", default=None, help="Filter by dimension")
    p_list.add_argument("--limit", type=int, default=20, help="Max entries to show")

    # clear
    p_clear = sub.add_parser("clear", help="Truncate lesson files (with confirmation).")
    p_clear.add_argument("--dimension", default=None, help="Clear only this dimension")
    p_clear.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    # pick-for-prompt
    p_pick = sub.add_parser("pick-for-prompt", help="Return lessons for spawn prompt injection.")
    p_pick.add_argument("--role", required=True, help="Agent role (e.g. executor)")
    p_pick.add_argument("--files", default="", help="Comma-separated file globs from planned work")
    p_pick.add_argument("--max", type=int, default=3, help="Max lessons to return")
    p_pick.add_argument("--json", dest="json_out", action="store_true", help="Output JSON")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "record":
        _cmd_record(args)
    elif args.command == "list":
        _cmd_list(args)
    elif args.command == "clear":
        _cmd_clear(args)
    elif args.command == "pick-for-prompt":
        _cmd_pick_for_prompt(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
