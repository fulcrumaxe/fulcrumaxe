"""Tests for backend.task_file — task-file schema v1 parser and validator."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.task_file import SCHEMA_V1, parse_task_file, validate_task_file

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TASK_FILE_SCRIPT = _REPO_ROOT / "backend" / "task_file.py"

_REQUIRED_FIELDS = [name for name, spec in SCHEMA_V1.items() if spec["required"]]
# schema_version's absence has its own meaning (switches to v0 validation,
# see test_missing_schema_version_falls_back_to_v0) rather than producing a
# "missing required field" line, so it is covered separately.
_REQUIRED_FIELDS_EXCEPT_SCHEMA_VERSION = [f for f in _REQUIRED_FIELDS if f != "schema_version"]

_ENUM_FIELDS_WITH_BAD_VALUES = {
    "type": "hotfix",
    "status": "in-progress",
    "complexity_points": 4,
    "milestone": "beta",
    "security_review": "yes",
}

BASE_V1: dict[str, Any] = {
    "schema_version": 1,
    "epic": 46,
    "task": "T01",
    "title": "Sample task",
    "type": "feature",
    "status": "draft",
    "estimated_hours": 4,
    "complexity_points": 3,
    "planned_prs": 1,
    "milestone": "stage-1",
    "security_review": False,
    "depends_on": [],
}

STEM = "T01"


def _write(tmp_path: Path, fm: dict[str, Any], stem: str = STEM, body: str = "Body text.\n") -> Path:
    path = tmp_path / f"{stem}.md"
    fm_text = yaml.safe_dump(fm, sort_keys=False)
    path.write_text(f"---\n{fm_text}---\n\n{body}", encoding="utf-8")
    return path


def _valid_fm(**overrides: Any) -> dict[str, Any]:
    fm = dict(BASE_V1)
    fm.update(overrides)
    return fm


# ---------------------------------------------------------------------------
# parse_task_file
# ---------------------------------------------------------------------------


def test_parse_task_file_coerces_integer_task_to_string(tmp_path: Path) -> None:
    fm_text = "schema_version: 1\ntask: 3\n"
    path = tmp_path / "3.md"
    path.write_text(f"---\n{fm_text}---\n\nBody\n", encoding="utf-8")

    fm, body = parse_task_file(path)

    assert fm["task"] == "3"
    assert isinstance(fm["task"], str)
    assert body.strip() == "Body"


def test_parse_task_file_no_frontmatter_returns_empty_dict(tmp_path: Path) -> None:
    text = "# Just a heading\n\nNo frontmatter here.\n"
    path = tmp_path / "plain.md"
    path.write_text(text, encoding="utf-8")

    fm, body = parse_task_file(path)

    assert fm == {}
    assert body == text


# ---------------------------------------------------------------------------
# CLI — validate
# ---------------------------------------------------------------------------


def test_cli_validate_exits_0_on_valid_v1_file(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_fm())
    result = subprocess.run(
        [sys.executable, str(_TASK_FILE_SCRIPT), "validate", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_validate_exits_1_and_prints_one_line_per_problem(tmp_path: Path) -> None:
    fm = _valid_fm()
    del fm["title"]
    fm["bogus_key"] = "oops"
    path = _write(tmp_path, fm)

    result = subprocess.run(
        [sys.executable, str(_TASK_FILE_SCRIPT), "validate", str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    lines = result.stdout.strip().splitlines()
    assert "missing required field: title" in lines
    assert "unknown field: bogus_key" in lines


# ---------------------------------------------------------------------------
# Item 4 — every required v1 field, missing, fails with its own line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", _REQUIRED_FIELDS_EXCEPT_SCHEMA_VERSION)
def test_missing_required_field(field: str) -> None:
    fm = _valid_fm()
    del fm[field]

    problems = validate_task_file(fm, STEM)

    assert f"missing required field: {field}" in problems


def test_missing_schema_version_falls_back_to_v0() -> None:
    fm = _valid_fm()
    del fm["schema_version"]
    # A well-formed v0 file needs `tags` too (not part of BASE_V1's v1 set).
    fm["tags"] = []

    problems = validate_task_file(fm, STEM)

    errors = [p for p in problems if not p.startswith("warning:")]
    assert errors == []
    assert "warning: missing field: schema_version" in problems


# ---------------------------------------------------------------------------
# Item 5 — enum fields, out-of-range value, each fails with "bad value for"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,bad_value", sorted(_ENUM_FIELDS_WITH_BAD_VALUES.items()))
def test_enum_field_bad_value(field: str, bad_value: Any) -> None:
    fm = _valid_fm(**{field: bad_value})

    problems = validate_task_file(fm, STEM)

    assert f"bad value for {field}: {bad_value}" in problems


# ---------------------------------------------------------------------------
# Item 6 — acceptance_files / planned_prs_reason conditional requirements
# ---------------------------------------------------------------------------


def test_ready_with_one_pr_and_no_acceptance_files_fails() -> None:
    fm = _valid_fm(status="ready", planned_prs=1)

    problems = validate_task_file(fm, STEM)

    assert "missing required field: acceptance_files" in problems


def test_ready_with_zero_prs_and_reason_passes_without_acceptance_files() -> None:
    fm = _valid_fm(status="ready", planned_prs=0, planned_prs_reason="operational only")

    problems = validate_task_file(fm, STEM)

    assert problems == []


def test_zero_prs_without_reason_fails() -> None:
    fm = _valid_fm(planned_prs=0)

    problems = validate_task_file(fm, STEM)

    assert "missing required field: planned_prs_reason" in problems


# ---------------------------------------------------------------------------
# Item 7 — depends_on: only the four allowed forms are valid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("item", ["H-8!", "D#x"])
def test_depends_on_bad_item_fails(item: str) -> None:
    fm = _valid_fm(depends_on=[item])

    problems = validate_task_file(fm, STEM)

    assert f"bad value for depends_on: {item}" in problems


@pytest.mark.parametrize("item", ["H08", "2.H13", "D#12", "#12"])
def test_depends_on_allowed_forms_pass(item: str) -> None:
    fm = _valid_fm(depends_on=[item])

    problems = validate_task_file(fm, STEM)

    assert problems == []


# ---------------------------------------------------------------------------
# task/filename-stem agreement
# ---------------------------------------------------------------------------


def test_task_not_matching_stem_fails() -> None:
    fm = _valid_fm(task="T99")

    problems = validate_task_file(fm, STEM)

    assert "task 'T99' does not match filename stem 'T01'" in problems


# ---------------------------------------------------------------------------
# Item 8 — v0 (no schema_version) files pass with warnings, not errors
# ---------------------------------------------------------------------------


def test_v0_file_passes_with_warnings_for_missing_v1_fields() -> None:
    fm = {
        "epic": 46,
        "task": "T01",
        "title": "Sample task",
        "type": "feature",
        "status": "not-started",
        "estimated_hours": 4,
        "depends_on": [],
        "tags": [],
    }

    problems = validate_task_file(fm, STEM)

    errors = [p for p in problems if not p.startswith("warning:")]
    assert errors == []
    for field in ("schema_version", "complexity_points", "planned_prs", "milestone", "security_review"):
        assert f"warning: missing field: {field}" in problems


def test_v0_file_missing_v0_required_field_fails() -> None:
    fm = {
        "epic": 46,
        "task": "T01",
        "title": "Sample task",
        "type": "feature",
        "status": "not-started",
        "estimated_hours": 4,
        "depends_on": [],
        # "tags" omitted
    }

    problems = validate_task_file(fm, STEM)

    assert "missing required field: tags" in problems
