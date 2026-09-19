"""
backend/task_file.py — task-file schema v1 parser and validator.

A task file lives at `epics/epic-<N>-<slug>/<TASK>.md` (the reserved
non-task names in an epic directory are `epic.md` and `README.md`). It is a
Markdown file with YAML frontmatter above a Markdown body. This module
parses that frontmatter and validates it against schema v1.

A file with no `schema_version` field is a v0 file (the format
`scripts/import-epic-tasks.py` has always read). It is validated against the
smaller v0 field set documented in `scripts/coldstart-templates/epic/README.md`
and never fails just for lacking v1-only fields — those are reported as
`warning:` lines instead, so old task files keep validating cleanly.

CLI:
    python3 backend/task_file.py validate <path>   # exit 0 valid, 1 invalid

Library:
    from backend.task_file import parse_task_file, validate_task_file
    frontmatter, body = parse_task_file(path)
    problems = validate_task_file(frontmatter, stem)   # [] means valid
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Schema v1
# ---------------------------------------------------------------------------
# Each entry describes one recognized field. `required=True` means it is
# required unconditionally; the two conditionally-required fields
# (`planned_prs_reason`, `acceptance_files`) are `required=False` here and
# handled by their own rule in `validate_task_file`. `enum`, when present,
# lists the only allowed values.

SCHEMA_V1: dict[str, dict[str, Any]] = {
    "schema_version": {"required": True, "enum": (1,)},
    "epic": {"required": True},
    "task": {"required": True},
    "title": {"required": True},
    "type": {
        "required": True,
        "enum": ("feature", "bug", "doc", "infra", "process", "security"),
    },
    "status": {
        "required": True,
        "enum": ("draft", "ready", "superseded", "completed"),
    },
    "estimated_hours": {"required": True},
    "complexity_points": {"required": True, "enum": (1, 2, 3, 5, 8)},
    "planned_prs": {"required": True},
    "planned_prs_reason": {"required": False},
    "milestone": {
        "required": True,
        "enum": ("stage-1", "stage-2", "launch", "post-launch"),
    },
    "security_review": {"required": True, "enum": (True, False)},
    "depends_on": {"required": True},
    "acceptance_files": {"required": False},
    "repo": {"required": False},
    "discussion": {"required": False},
    "tags": {"required": False},
    "priority": {"required": False},
    "parallel": {"required": False},
    "conflicts_with": {"required": False},
    "parent_task": {"required": False},
    "supersedes": {"required": False},
    "created": {"required": False},
}

_REQUIRED_V1_FIELDS = [name for name, spec in SCHEMA_V1.items() if spec["required"]]
_ALL_V1_FIELDS = set(SCHEMA_V1)

# Fields validated on a v0 (no schema_version) file — the set the importer
# has always read, documented in scripts/coldstart-templates/epic/README.md.
_V0_REQUIRED_FIELDS = [
    "epic",
    "task",
    "title",
    "type",
    "status",
    "estimated_hours",
    "depends_on",
    "tags",
]

# v1-required fields a v0 file cannot have, warned about rather than failed.
_V1_ONLY_REQUIRED_FIELDS = [
    name for name in _REQUIRED_V1_FIELDS if name not in _V0_REQUIRED_FIELDS
]

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_CROSS_EPIC_RE = re.compile(r"^\d+\.[A-Za-z0-9][A-Za-z0-9-]*$")
_D_HASH_RE = re.compile(r"^D#\d+$")
_HASH_RE = re.compile(r"^#\d+$")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from a Markdown body.

    Returns ({}, text) unchanged when `text` doesn't open with a `---`
    frontmatter fence. An integer `task` value is coerced to a string, since
    the schema requires `task` to be a string matching the filename stem.
    """
    if not text.startswith("---"):
        return {}, text

    rest = text[3:]
    match = re.search(r"^---\s*$", rest, re.MULTILINE)
    if not match:
        return {}, text

    fm_text = rest[: match.start()].strip()
    body = rest[match.end():].lstrip("\n")

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}

    if isinstance(fm, dict) and isinstance(fm.get("task"), int) and not isinstance(
        fm.get("task"), bool
    ):
        fm["task"] = str(fm["task"])

    return fm, body


def parse_task_file(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read *path* and split it into (frontmatter, body)."""
    text = Path(path).read_text(encoding="utf-8")
    return _parse_frontmatter(text)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_depends_on_item(item: Any) -> bool:
    if not isinstance(item, str):
        return False
    return bool(
        _TASK_ID_RE.match(item)
        or _CROSS_EPIC_RE.match(item)
        or _D_HASH_RE.match(item)
        or _HASH_RE.match(item)
    )


def _validate_v1(fm: dict[str, Any], stem: str) -> list[str]:
    problems: list[str] = []

    for key in fm:
        if key not in _ALL_V1_FIELDS:
            problems.append(f"unknown field: {key}")

    for field in _REQUIRED_V1_FIELDS:
        if field not in fm:
            problems.append(f"missing required field: {field}")

    if "schema_version" in fm and fm["schema_version"] not in SCHEMA_V1["schema_version"]["enum"]:
        problems.append(f"bad value for schema_version: {fm['schema_version']}")

    if "epic" in fm:
        epic = fm["epic"]
        if not (_is_int(epic) and epic >= 1):
            problems.append(f"bad value for epic: {epic}")

    if "task" in fm:
        task = fm["task"]
        if not (isinstance(task, str) and _TASK_ID_RE.match(task)):
            problems.append(f"bad value for task: {task}")
        elif task != stem:
            problems.append(f"task '{task}' does not match filename stem '{stem}'")

    if "title" in fm:
        title = fm["title"]
        if not (isinstance(title, str) and title.strip() != ""):
            problems.append(f"bad value for title: {title}")

    if "type" in fm and fm["type"] not in SCHEMA_V1["type"]["enum"]:
        problems.append(f"bad value for type: {fm['type']}")

    if "status" in fm and fm["status"] not in SCHEMA_V1["status"]["enum"]:
        problems.append(f"bad value for status: {fm['status']}")

    if "estimated_hours" in fm:
        eh = fm["estimated_hours"]
        if not (_is_number(eh) and 0 < eh <= 40):
            problems.append(f"bad value for estimated_hours: {eh}")

    if "complexity_points" in fm:
        cp = fm["complexity_points"]
        if isinstance(cp, bool) or cp not in SCHEMA_V1["complexity_points"]["enum"]:
            problems.append(f"bad value for complexity_points: {cp}")

    if "planned_prs" in fm:
        pp = fm["planned_prs"]
        if not (_is_int(pp) and pp >= 0):
            problems.append(f"bad value for planned_prs: {pp}")

    if "milestone" in fm and fm["milestone"] not in SCHEMA_V1["milestone"]["enum"]:
        problems.append(f"bad value for milestone: {fm['milestone']}")

    if "security_review" in fm and not isinstance(fm["security_review"], bool):
        problems.append(f"bad value for security_review: {fm['security_review']}")

    if "depends_on" in fm:
        deps = fm["depends_on"]
        if not isinstance(deps, list):
            problems.append(f"bad value for depends_on: {deps}")
        else:
            for item in deps:
                if not _valid_depends_on_item(item):
                    problems.append(f"bad value for depends_on: {item}")

    planned_prs = fm.get("planned_prs")
    if _is_int(planned_prs) and planned_prs == 0:
        reason = fm.get("planned_prs_reason")
        if not (isinstance(reason, str) and reason.strip() != ""):
            problems.append("missing required field: planned_prs_reason")

    status = fm.get("status")
    if status == "ready" and _is_int(planned_prs) and planned_prs >= 1:
        acceptance_files = fm.get("acceptance_files")
        if not (isinstance(acceptance_files, list) and len(acceptance_files) > 0):
            problems.append("missing required field: acceptance_files")

    return problems


def _validate_v0(fm: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in _V0_REQUIRED_FIELDS:
        if field not in fm:
            problems.append(f"missing required field: {field}")
    for field in _V1_ONLY_REQUIRED_FIELDS:
        if field not in fm:
            problems.append(f"warning: missing field: {field}")
    return problems


def validate_task_file(frontmatter: dict[str, Any], stem: str) -> list[str]:
    """Validate a parsed task file's frontmatter.

    Returns a list of problem lines. A line starting with `warning:` is
    advisory (v0-file-vs-v1-field gap) and never fails validation; every
    other line is a hard error.
    """
    if "schema_version" not in frontmatter:
        return _validate_v0(frontmatter)
    return _validate_v1(frontmatter, stem)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_validate(path_str: str) -> int:
    path = Path(path_str)
    frontmatter, _body = parse_task_file(path)
    problems = validate_task_file(frontmatter, path.stem)
    for line in problems:
        print(line)
    errors = [p for p in problems if not p.startswith("warning:")]
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task_file", description="Validate a task-file schema v1 (or v0) file."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate_p = sub.add_parser("validate", help="Validate a task file")
    validate_p.add_argument("path", help="Path to the task file")
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _cmd_validate(args.path)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
