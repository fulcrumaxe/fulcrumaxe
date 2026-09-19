"""Tests for scripts/task-inventory.py — aggregation, --check, --registry,
--totals-against, and the scripts/ci/task-files-guard.sh wrapper.

Hermetic: every fixture tree lives under `tmp_path`; nothing here reads or
writes AUTONOMOUS_TEAM_STATE_DIR, the real epics/ tree, or the network.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "task-inventory.py"
_GUARD_PATH = _REPO_ROOT / "scripts" / "ci" / "task-files-guard.sh"

# scripts/task-inventory.py has a hyphen in its name, so it can't be
# `import`ed as a dotted module — load it by file path, same pattern
# backend/tests/test_import_epic_tasks.py already uses for
# scripts/import-epic-tasks.py.
_spec = importlib.util.spec_from_file_location("task_inventory", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

build_inventory = _mod.build_inventory
check_mode = _mod.check_mode


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_BASE_FIELDS: dict[str, Any] = {
    "schema_version": 1,
    "type": "infra",
    "status": "draft",
    "estimated_hours": 4,
    "complexity_points": 3,
    "planned_prs": 1,
    "milestone": "stage-1",
    "security_review": False,
    "depends_on": [],
}


def _write_task(root: Path, epic: int, task: str, slug: str = "x", **overrides: Any) -> Path:
    fields = dict(_BASE_FIELDS)
    fields.update(epic=epic, task=task, title=overrides.pop("title", f"Task {task}"))
    fields.update(overrides)

    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {json.dumps(value)}")
    lines.append("---")
    lines.append("")
    lines.append("Body.")

    epic_dir = root / "epics" / f"epic-{epic}-{slug}"
    epic_dir.mkdir(parents=True, exist_ok=True)
    path = epic_dir / f"{task}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_raw(root: Path, epic: int, task: str, body: str, slug: str = "x") -> Path:
    epic_dir = root / "epics" / f"epic-{epic}-{slug}"
    epic_dir.mkdir(parents=True, exist_ok=True)
    path = epic_dir / f"{task}.md"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


# ---------------------------------------------------------------------------
# Item 1 — --json shape and required keys
# ---------------------------------------------------------------------------


def test_json_task_has_required_keys(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01")

    result = _run(["--json", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    assert set(data.keys()) == {"tasks", "epics", "milestones"}
    task = data["tasks"][0]
    for key in (
        "id",
        "title",
        "estimated_hours",
        "complexity_points",
        "planned_prs",
        "milestone",
        "security_review",
        "depends_on",
        "status",
        "repo",
        "file",
    ):
        assert key in task, f"missing key: {key}"
    assert task["id"] == "D#46:T01"


def test_depends_on_rewritten_task_refs_and_unchanged_discussion_refs(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", depends_on=["T02", "2.H13", "D#12", "#7"])
    _write_task(tmp_path, 46, "T02")
    _write_task(tmp_path, 2, "H13", slug="other")

    result = _run(["--json", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    t01 = next(t for t in data["tasks"] if t["id"] == "D#46:T01")
    assert t01["depends_on"] == ["D#46:T02", "D#2:H13", "D#12", "#7"]


def test_repo_defaults_to_autonomous_team_repo_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_task(tmp_path, 46, "T01")
    monkeypatch.setenv("AUTONOMOUS_TEAM_REPO", "acme/example")

    result = _run(["--json", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tasks"][0]["repo"] == "acme/example"


def test_repo_field_honored_when_set_in_frontmatter(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", repo="other/repo")

    result = _run(["--json", "--root", str(tmp_path)])
    data = json.loads(result.stdout)
    assert data["tasks"][0]["repo"] == "other/repo"


def test_v0_file_excluded_from_json_inventory(tmp_path: Path) -> None:
    # A v0 (no schema_version) file cannot supply the v1-only required
    # fields --json needs (milestone, complexity_points, ...), so it is
    # simply not inventoried — but it is still a valid v0 file.
    _write_raw(
        tmp_path,
        46,
        "T01",
        """\
        ---
        epic: 46
        task: T01
        title: Legacy task
        type: infra
        status: not-started
        estimated_hours: 2
        depends_on: []
        tags: []
        ---

        Body.
        """,
    )

    result = _run(["--json", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tasks"] == []

    check_result = _run(["--check", "--root", str(tmp_path)])
    assert check_result.returncode == 0, check_result.stdout + check_result.stderr


def test_invalid_v1_file_excluded_from_json_inventory(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", type="not-a-real-type")

    result = _run(["--json", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tasks"] == []


# ---------------------------------------------------------------------------
# Item 2 — sort order, determinism, byte-identity
# ---------------------------------------------------------------------------


def test_tasks_sorted_by_epic_then_task(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T02")
    _write_task(tmp_path, 2, "H13", slug="other")
    _write_task(tmp_path, 46, "T01")

    result = _run(["--json", "--root", str(tmp_path)])
    data = json.loads(result.stdout)
    ids = [t["id"] for t in data["tasks"]]
    assert ids == ["D#2:H13", "D#46:T01", "D#46:T02"]


def test_json_output_has_no_timestamp_field(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01")

    result = _run(["--json", "--root", str(tmp_path)])
    assert "time" not in result.stdout.lower()


def test_json_output_byte_identical_across_two_runs(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T02", depends_on=["T01"])
    _write_task(tmp_path, 46, "T01")
    _write_task(tmp_path, 2, "H13", slug="other")

    first = _run(["--json", "--root", str(tmp_path)])
    second = _run(["--json", "--root", str(tmp_path)])

    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout == second.stdout


# ---------------------------------------------------------------------------
# Item 3 — epics/milestones totals equal the sum over their tasks
# ---------------------------------------------------------------------------


def test_epic_and_milestone_sums_equal_sum_over_tasks(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", estimated_hours=4, complexity_points=3, planned_prs=1, milestone="stage-1")
    _write_task(tmp_path, 46, "T02", estimated_hours=6, complexity_points=5, planned_prs=2, milestone="stage-2")
    _write_task(tmp_path, 2, "H13", slug="other", estimated_hours=1, complexity_points=1, planned_prs=0, planned_prs_reason="op", milestone="stage-1")

    result = _run(["--json", "--root", str(tmp_path)])
    data = json.loads(result.stdout)

    tasks_by_epic: dict[str, list[dict]] = {}
    tasks_by_milestone: dict[str, list[dict]] = {}
    for t in data["tasks"]:
        epic_key = t["id"].split(":")[0][2:]
        tasks_by_epic.setdefault(epic_key, []).append(t)
        tasks_by_milestone.setdefault(t["milestone"], []).append(t)

    for epic_key, tasks in tasks_by_epic.items():
        bucket = data["epics"][epic_key]
        assert bucket["task_count"] == len(tasks)
        for field in ("estimated_hours", "complexity_points", "planned_prs"):
            assert bucket[field] == sum(t[field] for t in tasks)

    for milestone_key, tasks in tasks_by_milestone.items():
        bucket = data["milestones"][milestone_key]
        assert bucket["task_count"] == len(tasks)
        for field in ("estimated_hours", "complexity_points", "planned_prs"):
            assert bucket[field] == sum(t[field] for t in tasks)


# ---------------------------------------------------------------------------
# Item 4 — --check
# ---------------------------------------------------------------------------


def test_check_exits_0_on_valid_tree(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01")

    result = _run(["--check", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok: 1 tasks"


def test_check_exits_0_with_0_tasks_when_epic_dir_missing(tmp_path: Path) -> None:
    result = _run(["--check", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "0 tasks"


def test_check_reports_cycle(tmp_path: Path) -> None:
    _write_task(tmp_path, 9, "A01", depends_on=["A02"])
    _write_task(tmp_path, 9, "A02", depends_on=["A01"])

    result = _run(["--check", "--root", str(tmp_path)])
    assert result.returncode == 1
    assert "cycle: D#9:A01 -> D#9:A02 -> D#9:A01" in result.stdout.splitlines()


def test_check_reports_unknown_ref_but_never_checks_dhash_or_hash_refs(tmp_path: Path) -> None:
    _write_task(tmp_path, 9, "A01", depends_on=["ZZZ", "D#999999", "#888888"])

    result = _run(["--check", "--root", str(tmp_path)])
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert "unknown ref: ZZZ in epics/epic-9-x/A01.md" in lines
    assert not any("D#999999" in line for line in lines)
    assert not any("#888888" in line for line in lines)


def test_check_reports_invalid_file(tmp_path: Path) -> None:
    _write_raw(
        tmp_path,
        9,
        "A01",
        """\
        ---
        schema_version: 1
        epic: 9
        task: A01
        type: bogus
        depends_on: []
        ---

        Body.
        """,
    )

    result = _run(["--check", "--root", str(tmp_path)])
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert "invalid: epics/epic-9-x/A01.md: missing required field: title" in lines
    assert "invalid: epics/epic-9-x/A01.md: bad value for type: bogus" in lines


# ---------------------------------------------------------------------------
# Item 5 — --registry
# ---------------------------------------------------------------------------


def test_registry_adds_live_status_for_tasks_with_a_discussion(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", discussion=2615)
    _write_task(tmp_path, 46, "T02")  # no discussion field

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"discussions": [{"number": 2615, "status": "IMPLEMENTING"}]}),
        encoding="utf-8",
    )

    result = _run(["--json", "--registry", str(registry_path), "--root", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    t01 = next(t for t in data["tasks"] if t["id"] == "D#46:T01")
    t02 = next(t for t in data["tasks"] if t["id"] == "D#46:T02")
    assert t01["live_status"] == "IMPLEMENTING"
    assert "live_status" not in t02


def test_registry_missing_discussion_number_gives_null_live_status(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", discussion=999)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"discussions": []}), encoding="utf-8")

    result = _run(["--json", "--registry", str(registry_path), "--root", str(tmp_path)])
    data = json.loads(result.stdout)
    assert data["tasks"][0]["live_status"] is None


def test_registry_without_json_flag_is_a_usage_error(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"discussions": []}), encoding="utf-8")

    result = _run(["--check", "--registry", str(registry_path), "--root", str(tmp_path)])
    assert result.returncode != 0


def test_no_registry_flag_means_no_file_outside_epics_dir_is_read(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", discussion=2615)
    # No registry.json exists at all -- if the script tried to read one
    # without being told to, this would raise. It should just omit
    # live_status entirely.
    result = _run(["--json", "--root", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "live_status" not in data["tasks"][0]


# ---------------------------------------------------------------------------
# Item 6 — --totals-against
# ---------------------------------------------------------------------------


def test_totals_against_matching_totals_exits_0(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", estimated_hours=4, complexity_points=3, planned_prs=1)

    totals_path = tmp_path / "totals.json"
    totals_path.write_text(
        json.dumps([{"epic": 46, "estimated_hours": 4, "complexity_points": 3, "planned_prs": 1}]),
        encoding="utf-8",
    )

    result = _run(["--totals-against", str(totals_path), "--root", str(tmp_path)])
    assert result.returncode == 0, result.stdout + result.stderr


def test_totals_against_mismatch_reports_each_differing_field(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01", estimated_hours=4, complexity_points=3, planned_prs=1)

    totals_path = tmp_path / "totals.json"
    totals_path.write_text(
        json.dumps([{"epic": 46, "estimated_hours": 99, "complexity_points": 3, "planned_prs": 7}]),
        encoding="utf-8",
    )

    result = _run(["--totals-against", str(totals_path), "--root", str(tmp_path)])
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert "mismatch: D#46 estimated_hours inventory=4 expected=99" in lines
    assert "mismatch: D#46 planned_prs inventory=1 expected=7" in lines
    assert not any("complexity_points" in line for line in lines)


# ---------------------------------------------------------------------------
# Item 7 — scripts/ci/task-files-guard.sh
# ---------------------------------------------------------------------------


def _copy_guard_into(tmp_path: Path) -> Path:
    """Copy task-inventory.py, backend/task_file.py, and the guard script
    into an isolated tree, preserving their relative layout
    (scripts/task-inventory.py, backend/task_file.py,
    scripts/ci/task-files-guard.sh) so the guard's BASH_SOURCE-relative
    REPO_ROOT resolves to `tmp_path` -- no dependency on the real repo's
    epics/ tree or backend/ package.
    """
    (tmp_path / "scripts" / "ci").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "task-inventory.py").write_text(_SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "backend" / "task_file.py").write_text(
        (_REPO_ROOT / "backend" / "task_file.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    guard_copy = tmp_path / "scripts" / "ci" / "task-files-guard.sh"
    guard_copy.write_text(_GUARD_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    guard_copy.chmod(0o755)
    return guard_copy


def test_guard_script_exits_0_on_valid_tree(tmp_path: Path) -> None:
    guard_copy = _copy_guard_into(tmp_path)
    _write_task(tmp_path, 46, "T01")

    result = subprocess.run(["bash", str(guard_copy)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok: 1 tasks"


def test_guard_script_propagates_task_inventory_exit_code(tmp_path: Path) -> None:
    guard_copy = _copy_guard_into(tmp_path)
    _write_task(tmp_path, 9, "A01", depends_on=["A02"])
    _write_task(tmp_path, 9, "A02", depends_on=["A01"])

    result = subprocess.run(["bash", str(guard_copy)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "cycle: D#9:A01 -> D#9:A02 -> D#9:A01" in result.stdout


# ---------------------------------------------------------------------------
# Library-level checks (build_inventory / check_mode importability)
# ---------------------------------------------------------------------------


def test_build_inventory_importable_and_callable(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01")
    inventory = build_inventory(tmp_path / "epics", tmp_path)
    assert inventory["tasks"][0]["id"] == "D#46:T01"


def test_check_mode_importable_and_callable(tmp_path: Path) -> None:
    _write_task(tmp_path, 46, "T01")
    rc = check_mode(tmp_path / "epics", tmp_path)
    assert rc == 0
