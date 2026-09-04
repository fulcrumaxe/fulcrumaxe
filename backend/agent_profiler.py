"""
Agent profiler — per-role performance metrics, failure rates, and bottleneck detection.

Collects metrics from existing data sources (blackboard memory lessons, blackboard budget
entries, registry discussions) and writes a snapshot to .autonomous-team/agent-profiles.json.

Usage (CLI):
    python backend/agent_profiler.py compute       # rebuild profiles
    python backend/agent_profiler.py show          # print human-readable table
    python backend/agent_profiler.py show --role executor

Usage (library):
    from backend.agent_profiler import AgentProfiler
    profiler = AgentProfiler()
    snapshot = profiler.compute()
    profiler.show()
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, mean
from typing import Optional

# Allow running as a script from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard  # noqa: E402
from backend.registry import DiscussionRegistry  # noqa: E402

_PROFILE_FILENAME = "agent-profiles.json"
_DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".autonomous-team"

_KNOWN_ROLES = [
    "executor",
    "code-reviewer",
    "security-reviewer",
    "project-manager",
]

_SUCCESS_VERDICTS = {"pass", "done"}
_FAIL_VERDICTS = {"fail", "needs-fix"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_role_profile() -> dict:
    return {
        "total_spawns": 0,
        "success_rate": None,
        "median_duration_seconds": None,
        "total_tokens_used": None,
        "tokens_per_success": None,
        "first_pass_rate": None,
        "avg_lines_changed": None,
        "failure_patterns": [],
    }


class AgentProfiler:
    """
    Builds per-agent-role performance profiles from existing data sources.

    Data sources:
    - Blackboard memory/ entries: per-lesson role, lesson_type, tags, recorded_at
    - Blackboard budget/agents/* entries: token usage per agent spawn (if they exist)
    - Registry discussions: timing and status
    """

    def __init__(self, state_dir: Optional[Path] = None, bb: Optional[Blackboard] = None):
        self._state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
        self._profile_path = self._state_dir / _PROFILE_FILENAME
        self._bb = bb if bb is not None else Blackboard()
        self._registry = DiscussionRegistry(state_dir=self._state_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(self) -> dict:
        """
        Rebuild profiles from current data sources and write snapshot atomically.

        Returns the full snapshot dict.
        """
        memory_lessons = self._load_memory_lessons()
        budget_entries = self._load_budget_agent_entries()
        discussions = self._load_discussions()

        role_profiles: dict[str, dict] = {}
        for role in _KNOWN_ROLES:
            role_profiles[role] = self._compute_role_profile(
                role, memory_lessons, budget_entries, discussions
            )

        aggregate = self._compute_aggregate(role_profiles)

        now_iso = _now_iso()
        snapshot = {
            "generated_at": now_iso,
            "computed_at": now_iso,
            "roles": role_profiles,
            "aggregate": aggregate,
        }

        self._write_atomic(snapshot)
        return snapshot

    def show(self, role: Optional[str] = None) -> None:
        """
        Print a human-readable table to stdout.

        If role is given, prints only that role's profile.
        Reads from the snapshot file; runs compute() first if file missing.
        """
        snapshot = self._load_snapshot()
        if snapshot is None:
            snapshot = self.compute()

        roles_data = snapshot.get("roles", {})
        computed_at = snapshot.get("computed_at", "unknown")
        print(f"Agent profiles — computed at {computed_at}")
        print()

        # Header
        col_w = [18, 12, 12, 14, 14, 17]
        headers = ["role", "spawns", "success%", "median_dur(s)", "tokens", "tok/success"]
        self._print_row(headers, col_w)
        self._print_sep(col_w)

        roles_to_show = [role] if role else list(roles_data.keys())
        for r in roles_to_show:
            if r not in roles_data:
                print(f"No data for role: {r}")
                continue
            p = roles_data[r]
            success_pct = (
                f"{p['success_rate'] * 100:.1f}" if p["success_rate"] is not None else "n/a"
            )
            median_dur = (
                f"{p['median_duration_seconds']:.1f}" if p["median_duration_seconds"] is not None else "n/a"
            )
            tokens = (
                str(p["total_tokens_used"]) if p["total_tokens_used"] is not None else "n/a"
            )
            tok_per_success = (
                f"{p['tokens_per_success']:.0f}" if p["tokens_per_success"] is not None else "n/a"
            )
            row = [r, str(p["total_spawns"]), success_pct, median_dur, tokens, tok_per_success]
            self._print_row(row, col_w)

        print()
        agg = snapshot.get("aggregate", {})
        print(f"Bottleneck role:     {agg.get('bottleneck_role', 'n/a')}")
        print(f"Most expensive role: {agg.get('most_expensive_role', 'n/a')}")
        eff = agg.get("team_efficiency")
        print(f"Team efficiency:     {f'{eff:.2f}' if eff is not None else 'n/a'}")

    def load_snapshot(self) -> Optional[dict]:
        """Load and return the snapshot from disk, or None if it doesn't exist."""
        return self._load_snapshot()

    def get_role_profile(self, role: str) -> Optional[dict]:
        """Return profile for a specific role from the snapshot, or None if missing."""
        snapshot = self._load_snapshot()
        if snapshot is None:
            return None
        return snapshot.get("roles", {}).get(role)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_memory_lessons(self) -> list[dict]:
        """Load all agent memory lessons from the blackboard."""
        keys = self._bb.list_keys("memory/")
        lessons = []
        for key in keys:
            value = self._bb.read(key)
            if isinstance(value, dict):
                lessons.append(value)
        return lessons

    def _load_budget_agent_entries(self) -> list[dict]:
        """Load per-agent token budget entries from blackboard budget/agents/* keys."""
        keys = self._bb.list_keys("budget/agents/")
        entries = []
        for key in keys:
            value = self._bb.read(key)
            if isinstance(value, dict):
                entries.append(value)
        return entries

    def _load_discussions(self) -> list[dict]:
        """Load discussions from registry (no GitHub API calls)."""
        try:
            data = self._registry.load()
            return data.get("discussions", [])
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Profile computation
    # ------------------------------------------------------------------

    def _compute_role_profile(
        self,
        role: str,
        memory_lessons: list[dict],
        budget_entries: list[dict],
        discussions: list[dict],
    ) -> dict:
        profile = _empty_role_profile()

        # Filter memory lessons for this role
        role_lessons = [l for l in memory_lessons if l.get("role") == role]

        # total_spawns: count all lessons (each lesson represents one agent invocation outcome)
        total_spawns = len(role_lessons)
        profile["total_spawns"] = total_spawns

        if total_spawns == 0:
            return profile

        # success_rate: fraction with lesson_type == "success" or "pattern" (non-failure)
        successes = [l for l in role_lessons if l.get("lesson_type") in ("success", "pattern")]
        failures = [l for l in role_lessons if l.get("lesson_type") == "failure"]
        profile["success_rate"] = len(successes) / total_spawns

        # first_pass_rate: for code-reviewer, fraction of non-failure outcomes on first attempt.
        # For executor, same interpretation — fraction that didn't produce a failure lesson.
        profile["first_pass_rate"] = len(successes) / total_spawns

        # failure_patterns: top 3 most common failure tags
        fail_tags: list[str] = []
        for l in failures:
            fail_tags.extend(l.get("tags", []))
        counter = Counter(fail_tags)
        profile["failure_patterns"] = [
            {"tag": tag, "count": cnt} for tag, cnt in counter.most_common(3)
        ]

        # median_duration_seconds: estimated from timestamps across discussions that completed.
        # Use DONE discussions timing as a proxy for the executor role; for other roles use
        # lesson recorded_at deltas across paired success/failure (not available directly),
        # so we fall back to None unless budget entries carry timestamps.
        durations = self._estimate_durations(role, role_lessons, discussions)
        if durations:
            profile["median_duration_seconds"] = median(durations)

        # Token metrics from budget entries
        role_budget_entries = [e for e in budget_entries if e.get("role") == role]
        if role_budget_entries:
            total_tokens = sum(
                (e.get("input_tokens", 0) or 0) + (e.get("output_tokens", 0) or 0)
                for e in role_budget_entries
            )
            profile["total_tokens_used"] = total_tokens
            success_count = sum(
                1 for e in role_budget_entries if e.get("verdict") in _SUCCESS_VERDICTS
            )
            if success_count > 0:
                profile["tokens_per_success"] = total_tokens / success_count

        # avg_lines_changed: executor only, from budget entries if they carry lines_changed
        if role == "executor":
            lines_list = [
                e["lines_changed"]
                for e in role_budget_entries
                if isinstance(e.get("lines_changed"), (int, float))
            ]
            if lines_list:
                profile["avg_lines_changed"] = mean(lines_list)

        return profile

    def _estimate_durations(
        self,
        role: str,
        role_lessons: list[dict],
        discussions: list[dict],
    ) -> list[float]:
        """
        Estimate durations in seconds for a role.

        For executor: use DONE discussion cycle times (created_at → closed_at).
        For other roles: not estimable from available data without spawn timestamps.
        """
        if role != "executor":
            return []

        done = [
            d for d in discussions
            if d.get("status") == "DONE" and d.get("created_at") and d.get("closed_at")
        ]
        durations = []
        for d in done:
            try:
                t_start = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
                t_end = datetime.fromisoformat(d["closed_at"].replace("Z", "+00:00"))
                delta = (t_end - t_start).total_seconds()
                if delta >= 0:
                    durations.append(delta)
            except (ValueError, KeyError):
                pass
        return durations

    def _compute_aggregate(self, role_profiles: dict[str, dict]) -> dict:
        """Compute cross-role aggregate metrics."""
        aggregate: dict = {
            "bottleneck_role": None,
            "most_expensive_role": None,
            "team_efficiency": None,
        }

        # bottleneck_role: highest median_duration_seconds
        duration_by_role = {
            role: p["median_duration_seconds"]
            for role, p in role_profiles.items()
            if p["median_duration_seconds"] is not None
        }
        if duration_by_role:
            aggregate["bottleneck_role"] = max(duration_by_role, key=lambda r: duration_by_role[r])

        # most_expensive_role: highest total_tokens_used
        token_by_role = {
            role: p["total_tokens_used"]
            for role, p in role_profiles.items()
            if p["total_tokens_used"] is not None
        }
        if token_by_role:
            aggregate["most_expensive_role"] = max(token_by_role, key=lambda r: token_by_role[r])

        # team_efficiency: successful outcomes / total spawns across all roles.
        # Recompute success counts directly from success_rate * total_spawns to avoid
        # floating-point rounding distortions (int() truncates toward zero is fine here).
        total_spawns = sum(p["total_spawns"] for p in role_profiles.values())
        if total_spawns > 0:
            total_successes = sum(
                int((p["success_rate"] or 0.0) * p["total_spawns"])
                for p in role_profiles.values()
            )
            aggregate["team_efficiency"] = total_successes / total_spawns

        return aggregate

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _load_snapshot(self) -> Optional[dict]:
        if not self._profile_path.exists():
            return None
        try:
            with self._profile_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def _write_atomic(self, data: dict) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._profile_path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.rename(tmp, self._profile_path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Table formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _print_row(cells: list[str], widths: list[int]) -> None:
        row = "  ".join(cell.ljust(w) for cell, w in zip(cells, widths))
        print(row)

    @staticmethod
    def _print_sep(widths: list[int]) -> None:
        print("  ".join("-" * w for w in widths))


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent_profiler",
        description="Per-role agent performance profiler.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("compute", help="Rebuild profiles from current data sources")

    show_p = sub.add_parser("show", help="Print human-readable profile table")
    show_p.add_argument("--role", default=None, help="Show only this role")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    profiler = AgentProfiler()

    if args.command == "compute":
        try:
            snapshot = profiler.compute()
        except Exception as exc:
            print(f"error computing profiles: {exc}", file=sys.stderr)
            return 1
        roles = list(snapshot.get("roles", {}).keys())
        print(f"profiles computed for: {', '.join(roles)}")
        print(f"snapshot written to: {profiler._profile_path}")
        return 0

    if args.command == "show":
        try:
            profiler.show(role=args.role)
        except Exception as exc:
            print(f"error showing profiles: {exc}", file=sys.stderr)
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
