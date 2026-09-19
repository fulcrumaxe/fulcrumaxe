#!/usr/bin/env python3
"""
scripts/task-inventory.py — aggregate schema v1 epic task files into one
inventory, and check the epics/ tree for structural problems.

An epic directory lives at `epics/epic-<N>-<slug>/` and holds task files
(`epic.md` and `README.md` are the two reserved non-task names in it — see
`scripts/coldstart-templates/epic/README.md`). This script never parses a
task file itself; every read goes through `backend.task_file`
(`parse_task_file` / `validate_task_file`), which is the schema v1 (and
legacy v0) source of truth.

Modes (exactly one required):
    --json                Print one JSON object with keys `tasks`, `epics`
                           and `milestones`, built from every schema v1 task
                           file that validates cleanly. Deterministic: sorted
                           keys, sorted task order, no timestamps, no field
                           depends on wall-clock or hostname.
    --check                Validate every task file under the epics/ tree
                           (v0 and v1 alike) and report, one line each:
                             invalid: <file>: <error>
                             unknown ref: <ref> in <file>
                             cycle: <a> -> <b> -> <a>
                           Exits 0 with no problems (printing `0 tasks` when
                           the epics/ tree has none, `ok: <n> tasks`
                           otherwise), 1 otherwise.
    --totals-against PATH  Compare computed per-epic totals (estimated_hours,
                           complexity_points, planned_prs) against an
                           expected JSON array of
                           `{"epic": N, "estimated_hours": ..., ...}`
                           objects, printing one `mismatch: ...` line per
                           differing field and exiting 1 if any differ.

--registry PATH (only with --json) adds a `live_status` field — the STATUS
phase from that registry file (backend.registry's `{"discussions": [...]}`
shape), or null — to every task that names a `discussion`. No file outside
the epics/ tree is read unless --registry or --totals-against is given.

--root PATH overrides the repo root that `epics/` is resolved under
(default: the current working directory).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.task_file import (  # noqa: E402
    _CROSS_EPIC_RE,
    _TASK_ID_RE,
    parse_task_file,
    validate_task_file,
)

_RESERVED_NAMES = {"epic.md", "README.md"}
_SUM_FIELDS = ("estimated_hours", "complexity_points", "planned_prs")


# ---------------------------------------------------------------------------
# Discovery and parsing
# ---------------------------------------------------------------------------


def discover_task_paths(epics_dir: Path) -> list[Path]:
    """Return every task file under `epics_dir`, sorted (epic dir, filename)."""
    if not epics_dir.is_dir():
        return []
    paths: list[Path] = []
    for epic_dir in sorted(p for p in epics_dir.iterdir() if p.is_dir() and p.name.startswith("epic-")):
        for f in sorted(epic_dir.iterdir()):
            if f.is_file() and f.suffix == ".md" and f.name not in _RESERVED_NAMES:
                paths.append(f)
    return paths


def load_task_files(epics_dir: Path, root: Path) -> list[dict[str, Any]]:
    """Parse + validate every task file, one record per file.

    `node_id` (and `epic`/`task`, kept as their native types) is set only
    when the file's own `epic`/`task` fields are usable enough to place it
    in the dependency graph — a plain int `epic` and a `task` string equal
    to the filename stem. A file that fails even that is still reported by
    `--check` (via `errors`), just never referenced as a dependency target.
    """
    records: list[dict[str, Any]] = []
    for path in discover_task_paths(epics_dir):
        frontmatter, _body = parse_task_file(path)
        stem = path.stem
        problems = validate_task_file(frontmatter, stem)
        errors = [p for p in problems if not p.startswith("warning:")]

        epic = frontmatter.get("epic")
        task = frontmatter.get("task")
        node_id = None
        if (
            isinstance(epic, int)
            and not isinstance(epic, bool)
            and isinstance(task, str)
            and task == stem
        ):
            node_id = f"D#{epic}:{task}"

        records.append(
            {
                "path": path,
                "relpath": path.relative_to(root).as_posix(),
                "frontmatter": frontmatter,
                "errors": errors,
                "epic": epic if node_id else None,
                "task": task if node_id else None,
                "node_id": node_id,
            }
        )
    return records


# ---------------------------------------------------------------------------
# depends_on ref handling
# ---------------------------------------------------------------------------


def _is_task_ref(item: str) -> bool:
    """True for a same-epic (`H08`) or cross-epic (`2.H13`) task ref.

    False for `D#<n>` / `#<n>` (Discussion refs — format-checked elsewhere,
    never resolved here) and for anything else.
    """
    return bool(_CROSS_EPIC_RE.match(item) or _TASK_ID_RE.match(item))


def _ref_key(item: str, own_epic: int) -> tuple[int, str]:
    """(epic, task) lookup key for a task ref already passing `_is_task_ref`."""
    if _CROSS_EPIC_RE.match(item):
        epic_str, task_id = item.split(".", 1)
        return int(epic_str), task_id
    return own_epic, item


def _rewrite_dep(item: str, own_epic: int) -> str:
    """Rewrite a depends_on item to canonical `D#<epic>:<task>` form.

    Purely syntactic — it never checks whether the target file exists
    (that's `--check`'s job). `D#<n>` / `#<n>` pass through unchanged.
    """
    if _CROSS_EPIC_RE.match(item):
        epic_str, task_id = item.split(".", 1)
        return f"D#{epic_str}:{task_id}"
    if _TASK_ID_RE.match(item):
        return f"D#{own_epic}:{item}"
    return item


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def _find_cycles(edges: dict[str, list[str]]) -> list[str]:
    """Small DFS cycle finder. `edges[node] = [dependency, ...]`."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in edges}
    cycles: list[str] = []

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in edges.get(node, []):
            if nxt not in color:
                color[nxt] = WHITE
            if color[nxt] == WHITE:
                visit(nxt, stack)
            elif color[nxt] == GRAY:
                idx = stack.index(nxt)
                cycles.append("cycle: " + " -> ".join(stack[idx:] + [nxt]))
        stack.pop()
        color[node] = BLACK

    for node in sorted(edges):
        if color.get(node, WHITE) == WHITE:
            visit(node, [])

    return cycles


def check_mode(epics_dir: Path, root: Path) -> int:
    records = load_task_files(epics_dir, root)

    by_id: dict[tuple[int, str], str] = {
        (r["epic"], r["task"]): r["node_id"] for r in records if r["node_id"]
    }

    lines: list[str] = []
    for r in records:
        for err in r["errors"]:
            lines.append(f"invalid: {r['relpath']}: {err}")

    edges: dict[str, list[str]] = {}
    for r in records:
        if not r["node_id"]:
            continue
        deps = r["frontmatter"].get("depends_on")
        if not isinstance(deps, list):
            continue
        own_edges: list[str] = []
        for item in deps:
            if not isinstance(item, str) or not _is_task_ref(item):
                continue
            resolved = by_id.get(_ref_key(item, r["epic"]))
            if resolved is None:
                lines.append(f"unknown ref: {item} in {r['relpath']}")
            else:
                own_edges.append(resolved)
        edges[r["node_id"]] = own_edges

    lines.extend(_find_cycles(edges))

    if lines:
        for line in lines:
            print(line)
        return 1

    task_count = len(records)
    print("0 tasks" if task_count == 0 else f"ok: {task_count} tasks")
    return 0


# ---------------------------------------------------------------------------
# --json / inventory build
# ---------------------------------------------------------------------------


def build_inventory(
    epics_dir: Path, root: Path, registry_path: Path | None = None
) -> dict[str, Any]:
    records = load_task_files(epics_dir, root)

    registry_status_by_number: dict[int, Any] = {}
    if registry_path is not None:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        for d in data.get("discussions", []):
            number = d.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                registry_status_by_number[number] = d.get("status")

    default_repo = os.environ.get("AUTONOMOUS_TEAM_REPO", "")

    valid = [
        r
        for r in records
        if r["node_id"] and not r["errors"] and r["frontmatter"].get("schema_version") == 1
    ]
    valid.sort(key=lambda r: (r["epic"], r["task"]))

    tasks: list[dict[str, Any]] = []
    epics: dict[str, dict[str, Any]] = {}
    milestones: dict[str, dict[str, Any]] = {}

    for r in valid:
        fm = r["frontmatter"]
        own_epic = r["epic"]
        deps = fm.get("depends_on") or []
        deps_out = [_rewrite_dep(item, own_epic) if isinstance(item, str) else item for item in deps]

        entry: dict[str, Any] = {
            "id": r["node_id"],
            "title": fm.get("title"),
            "estimated_hours": fm.get("estimated_hours"),
            "complexity_points": fm.get("complexity_points"),
            "planned_prs": fm.get("planned_prs"),
            "milestone": fm.get("milestone"),
            "security_review": fm.get("security_review"),
            "depends_on": deps_out,
            "status": fm.get("status"),
            "repo": fm.get("repo") or default_repo,
            "file": r["relpath"],
        }

        if registry_path is not None:
            discussion = fm.get("discussion")
            if isinstance(discussion, int) and not isinstance(discussion, bool):
                entry["live_status"] = registry_status_by_number.get(discussion)

        tasks.append(entry)

        epic_bucket = epics.setdefault(
            str(own_epic), {"task_count": 0, "estimated_hours": 0, "complexity_points": 0, "planned_prs": 0}
        )
        milestone_bucket = milestones.setdefault(
            str(entry["milestone"]),
            {"task_count": 0, "estimated_hours": 0, "complexity_points": 0, "planned_prs": 0},
        )
        for bucket in (epic_bucket, milestone_bucket):
            bucket["task_count"] += 1
            for field in _SUM_FIELDS:
                bucket[field] += entry[field]

    return {"tasks": tasks, "epics": epics, "milestones": milestones}


# ---------------------------------------------------------------------------
# --totals-against
# ---------------------------------------------------------------------------


def totals_against_mode(epics_dir: Path, root: Path, totals_path: Path) -> int:
    inventory = build_inventory(epics_dir, root)
    expected_list = json.loads(totals_path.read_text(encoding="utf-8"))

    empty_bucket = {field: 0 for field in _SUM_FIELDS}
    mismatches: list[str] = []
    for expected in expected_list:
        epic = expected.get("epic")
        actual = inventory["epics"].get(str(epic), empty_bucket)
        for field in _SUM_FIELDS:
            expected_value = expected.get(field, 0)
            actual_value = actual.get(field, 0)
            if actual_value != expected_value:
                mismatches.append(
                    f"mismatch: D#{epic} {field} inventory={actual_value} expected={expected_value}"
                )

    if mismatches:
        for line in mismatches:
            print(line)
        return 1

    print("ok: totals match")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task-inventory", description="Aggregate and validate epic task files (schema v1)."
    )
    parser.add_argument("--json", action="store_true", help="Print the aggregated inventory as JSON.")
    parser.add_argument(
        "--check", action="store_true", help="Validate the epics/ tree; print problems and exit 1 if any."
    )
    parser.add_argument(
        "--registry",
        metavar="PATH",
        help="Registry JSON (backend.registry sync shape) to add live_status to --json output.",
    )
    parser.add_argument(
        "--totals-against",
        metavar="PATH",
        help="Compare computed per-epic totals against an expected JSON array.",
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="Repository root to resolve epics/ under (default: current working directory).",
    )
    args = parser.parse_args(argv)

    if not (args.check or args.json or args.totals_against):
        parser.error("one of --json, --check, or --totals-against is required")
    if args.check and (args.json or args.totals_against):
        parser.error("--check cannot be combined with --json or --totals-against")
    if args.json and args.totals_against:
        parser.error("--json cannot be combined with --totals-against")
    if args.registry and not args.json:
        parser.error("--registry requires --json")

    root = Path(args.root).resolve() if args.root else Path.cwd()
    epics_dir = root / "epics"

    if args.check:
        return check_mode(epics_dir, root)

    if args.totals_against:
        return totals_against_mode(epics_dir, root, Path(args.totals_against))

    registry_path = Path(args.registry) if args.registry else None
    inventory = build_inventory(epics_dir, root, registry_path)
    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
