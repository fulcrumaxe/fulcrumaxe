"""Tests for scripts/import-epic-tasks.py.

Hermetic: every GraphQL call goes through FakeGraphQLClient (no `gh`, no
network). Each test builds its own tmp_path tree of epic/task files.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend import task_specs  # noqa: E402
from backend.discussion_status import is_spec_ready  # noqa: E402
from backend.spec_file_list import extract_file_list  # noqa: E402

_SCRIPT_PATH = _REPO_ROOT / "scripts" / "import-epic-tasks.py"
_spec = importlib.util.spec_from_file_location("import_epic_tasks", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

run_import = _mod.run_import
run_parent_index = _mod.run_parent_index
build_parent_index = _mod.build_parent_index
find_task_files = _mod.find_task_files
format_title = _mod.format_title
load_epic_dir_name = _mod.load_epic_dir_name


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class FakeGraphQLClient:
    """In-memory stand-in for GraphQLClient — no network, no `gh`."""

    def __init__(self, existing: Optional[dict[int, dict[str, str]]] = None):
        self._discussions: dict[int, dict[str, str]] = dict(existing or {})
        self._next_number = (max(self._discussions.keys()) if self._discussions else 100) + 1
        self.create_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[int, str]] = []

    def list_discussion_titles(self) -> dict[str, int]:
        return {d["title"]: n for n, d in self._discussions.items()}

    def get_discussion(self, number: int) -> Optional[dict[str, Any]]:
        d = self._discussions.get(number)
        if d is None:
            return None
        return {"id": f"node{number}", "body": d["body"]}

    def create_discussion(self, title: str, body: str) -> tuple[int, str]:
        number = self._next_number
        self._next_number += 1
        self._discussions[number] = {"title": title, "body": body}
        self.create_calls.append((title, body))
        return number, f"node{number}"

    def update_discussion_body(self, number: int, new_body: str) -> None:
        self._discussions[number]["body"] = new_body
        self.update_calls.append((number, new_body))


def v1_text(
    epic: int,
    task: str,
    title: str = "Sample task",
    status: str = "ready",
    depends_on: Optional[list[str]] = None,
    planned_prs: int = 1,
    acceptance_files: Optional[list[str]] = None,
    milestone: str = "stage-1",
    type_: str = "feature",
    tags: Optional[list[str]] = None,
    repo: Optional[str] = None,
    discussion: Optional[int] = None,
    omit_fields: Optional[list[str]] = None,
    body_text: str = "## Overview\nBody content.\n",
) -> str:
    depends_on = depends_on if depends_on is not None else []
    acceptance_files = acceptance_files if acceptance_files is not None else ["src/example.py"]
    tags = tags if tags is not None else ["backend"]
    omit_fields = set(omit_fields or [])

    fields: list[tuple[str, str]] = [
        ("schema_version", "1"),
        ("epic", str(epic)),
        ("task", task),
        ("title", title),
        ("type", type_),
        ("status", status),
        ("estimated_hours", "3"),
        ("complexity_points", "3"),
        ("planned_prs", str(planned_prs)),
    ]
    if planned_prs == 0:
        fields.append(("planned_prs_reason", "no PR needed for this task"))
    fields.append(("milestone", milestone))
    fields.append(("security_review", "false"))
    fields.append(("depends_on", json.dumps(depends_on)))
    if status == "ready" and planned_prs >= 1:
        fields.append(("acceptance_files", json.dumps(acceptance_files)))
    fields.append(("tags", json.dumps(tags)))
    if repo is not None:
        fields.append(("repo", repo))
    if discussion is not None:
        fields.append(("discussion", str(discussion)))

    lines = ["---"]
    for key, value in fields:
        if key in omit_fields:
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body_text)
    return "\n".join(lines)


def v0_text(
    epic: int,
    task: str,
    title: str = "Sample task",
    status: str = "not-started",
    depends_on: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    type_: str = "enhancement",
    estimated_hours: int = 3,
    body_text: str = "## Overview\nBody content.\n",
) -> str:
    depends_on = depends_on if depends_on is not None else []
    tags = tags if tags is not None else ["x"]
    lines = [
        "---",
        f"epic: {epic}",
        f"task: {task}",
        f"title: {title}",
        f"type: {type_}",
        f"status: {status}",
        f"estimated_hours: {estimated_hours}",
        f"depends_on: {json.dumps(depends_on)}",
        f"tags: {json.dumps(tags)}",
        "---",
        "",
        body_text,
    ]
    return "\n".join(lines)


def write_task(epics_root: Path, epic_dirname: str, task_id: str, text: str) -> Path:
    d = epics_root / epic_dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{task_id}.md"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# AC1 — v1 ready-file body format
# ---------------------------------------------------------------------------


def test_v1_ready_body_format_and_consumers(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-7-widgets", "3", v1_text(7, "3", title="Do a thing", acceptance_files=["a.py", "b.py"]))

    client = FakeGraphQLClient()
    exit_code = run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert exit_code == 0
    assert len(client.create_calls) == 1
    title, body = client.create_calls[0]

    lines = body.splitlines()
    assert re.match(
        r"^<!-- STATUS:SPEC_READY SINCE:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -->$", lines[0]
    ), lines[0]
    assert lines[1] == "---"
    assert is_spec_ready(body) is True

    parsed_fm = task_specs._parse_frontmatter(body)
    assert parsed_fm["estimated_hours"] == 3
    assert parsed_fm["complexity_points"] == 3
    assert parsed_fm["planned_prs"] == 1
    assert parsed_fm["acceptance_files"] == ["a.py", "b.py"]
    assert parsed_fm["type"] == "feature"
    assert parsed_fm["depends_on"] == []
    assert parsed_fm["tags"] == ["backend"]

    assert extract_file_list(body) == ["a.py", "b.py"]
    assert "<!-- TASK-FILE-SHA:" in body


# ---------------------------------------------------------------------------
# AC2 — v1/v0 selection rules
# ---------------------------------------------------------------------------


def test_v1_not_ready_is_skipped(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v1_text(1, "1", status="draft"))
    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)
    assert client.create_calls == []


def test_v1_with_discussion_field_is_skipped(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v1_text(1, "1", discussion=42))
    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)
    assert client.create_calls == []


def test_v1_with_mismatched_repo_is_skipped(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v1_text(1, "1", repo="other-org/other-repo"))
    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)
    assert client.create_calls == []


def test_v1_matching_repo_is_imported(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v1_text(1, "1", repo="example-org/testproj"))
    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)
    assert len(client.create_calls) == 1


def test_v1_excluded_id_is_skipped(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v1_text(1, "1"))
    client = FakeGraphQLClient()
    run_import(
        tmp_path,
        "example-org/testproj",
        client,
        status_filter=set(),
        dry_run=False,
        epic_filter=None,
        exclude_ids={"1.1"},
    )
    assert client.create_calls == []


def test_status_flag_applies_only_to_v0(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v1_text(1, "1", status="ready"))
    write_task(epics, "epic-1-x", "2", v0_text(1, "2", status="not-started"))
    client = FakeGraphQLClient()
    # --status filter set to something that matches neither v0's status nor
    # "ready" — the v1 file must still import; the v0 one must not.
    run_import(
        tmp_path,
        "example-org/testproj",
        client,
        status_filter={"in_progress"},
        dry_run=False,
        epic_filter=None,
    )
    titles = [t for t, _ in client.create_calls]
    assert any("1.1" in t for t in titles)
    assert not any("1.2" in t for t in titles)


def test_v0_body_format(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v0_text(1, "1", status="not-started"))
    client = FakeGraphQLClient()
    run_import(
        tmp_path,
        "example-org/testproj",
        client,
        status_filter={"not-started"},
        dry_run=False,
        epic_filter=None,
    )
    assert len(client.create_calls) == 1
    _title, body = client.create_calls[0]
    lines = body.splitlines()
    assert re.match(r"^<!-- STATUS:DISCUSSING SINCE:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -->$", lines[0])
    assert lines[1] == "---"


# ---------------------------------------------------------------------------
# AC3 — backfill pass is gone
# ---------------------------------------------------------------------------


def test_backfill_pass_removed() -> None:
    result = subprocess.run(
        ["grep", "-n", "depends_on backfill", str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout
    assert result.stdout == ""

    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert "Depends on:" not in source


# ---------------------------------------------------------------------------
# AC4 — dependency order + BLOCKED-BY chain
# ---------------------------------------------------------------------------


def test_dependency_chain_created_in_order(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    # Written to disk in reverse order so file-order alone cannot explain a
    # correct creation order.
    write_task(epics, "epic-9-chain", "c", v1_text(9, "c", title="C", depends_on=["b"]))
    write_task(epics, "epic-9-chain", "b", v1_text(9, "b", title="B", depends_on=["a"]))
    write_task(epics, "epic-9-chain", "a", v1_text(9, "a", title="A", depends_on=[]))

    client = FakeGraphQLClient()
    exit_code = run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert exit_code == 0
    order = [t for t, _ in client.create_calls]
    assert [t.split(" — ")[-1] for t in order] == ["A", "B", "C"]

    a_number = next(n for n, d in client._discussions.items() if d["title"].endswith("— A"))
    b_number = next(n for n, d in client._discussions.items() if d["title"].endswith("— B"))
    b_body = next(body for t, body in client.create_calls if t.endswith("— B"))
    c_body = next(body for t, body in client.create_calls if t.endswith("— C"))

    assert f"BLOCKED-BY:D#{a_number}" in b_body.splitlines()[0]
    assert f"BLOCKED-BY:D#{b_number}" in c_body.splitlines()[0]


def test_absolute_and_completed_deps(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-9-chain", "done", v1_text(9, "done", title="Done", status="completed", planned_prs=0))
    write_task(
        epics,
        "epic-9-chain",
        "x",
        v1_text(9, "x", title="X", depends_on=["done", "D#500", "#7"]),
    )

    client = FakeGraphQLClient()
    exit_code = run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert exit_code == 0
    # The completed dep never gets a Discussion; only "x" is created.
    assert len(client.create_calls) == 1
    _title, body = client.create_calls[0]
    line1 = body.splitlines()[0]
    assert "D#500" in line1
    assert "#7" in line1
    assert "done" not in line1.lower()


# ---------------------------------------------------------------------------
# AC5 — unresolved dependency / validate-before-create
# ---------------------------------------------------------------------------


def test_unresolved_dependency_blocks_only_that_child(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-9-chain", "orphan", v1_text(9, "orphan", title="Orphan", depends_on=["999"]))
    write_task(epics, "epic-9-chain", "indep", v1_text(9, "indep", title="Indep", depends_on=[]))

    client = FakeGraphQLClient()
    exit_code = run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert exit_code == 1
    titles = [t for t, _ in client.create_calls]
    assert any(t.endswith("— Indep") for t in titles)
    assert not any(t.endswith("— Orphan") for t in titles)

    out = capsys.readouterr().out
    assert "unresolved dependency 999 for 9.orphan" in out


def test_invalid_file_in_selection_blocks_entire_run(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-9-chain", "good", v1_text(9, "good", title="Good"))
    write_task(
        epics,
        "epic-9-chain",
        "bad",
        v1_text(9, "bad", title="Bad", omit_fields=["security_review"]),
    )

    client = FakeGraphQLClient()
    exit_code = run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert exit_code == 1
    assert client.create_calls == []


# ---------------------------------------------------------------------------
# AC6 — idempotency via title prefix
# ---------------------------------------------------------------------------


def test_retitle_creates_no_second_discussion(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    path = write_task(epics, "epic-7-widgets", "3", v1_text(7, "3", title="Original title"))

    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)
    assert len(client.create_calls) == 1

    path.write_text(v1_text(7, "3", title="Renamed title"), encoding="utf-8")
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert len(client.create_calls) == 1  # still just the one create
    assert len(client._discussions) == 1


# ---------------------------------------------------------------------------
# AC7 — SHA drift
# ---------------------------------------------------------------------------


def test_unchanged_file_is_not_updated(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-7-widgets", "3", v1_text(7, "3"))

    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert client.update_calls == []


def test_drift_updates_when_spec_ready(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    path = write_task(epics, "epic-7-widgets", "3", v1_text(7, "3", body_text="original body"))

    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)
    number = next(iter(client._discussions))
    original_line1 = client._discussions[number]["body"].splitlines()[0]

    path.write_text(v1_text(7, "3", body_text="changed body"), encoding="utf-8")
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert len(client.update_calls) == 1
    updated_number, updated_body = client.update_calls[0]
    assert updated_number == number
    assert updated_body.splitlines()[0] == original_line1
    assert "changed body" in updated_body


def test_drift_left_alone_when_not_spec_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_task(tmp_path / "epics", "epic-7-widgets", "3", v1_text(7, "3"))

    stale_sha = "0" * 64
    seeded_body = (
        "<!-- STATUS:IMPLEMENTING SINCE:2020-01-01T00:00:00Z -->\n---\nx: 1\n---\n\nold\n\n"
        f"<!-- TASK-FILE-SHA:{stale_sha} -->"
    )
    seed_title = format_title({"type": "feature", "epic": 7, "task": "3", "title": "Sample task"})
    client = FakeGraphQLClient(existing={50: {"title": seed_title, "body": seeded_body}})

    exit_code = run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert exit_code == 0
    assert client.update_calls == []
    assert client.create_calls == []
    out = capsys.readouterr().out
    assert "drift: 7.3 changed after spawn (D#50)" in out


# ---------------------------------------------------------------------------
# AC8 — epic_dir config, --epic matching, reserved stems, --milestone
# ---------------------------------------------------------------------------


def test_epic_dir_from_project_json(tmp_path: Path) -> None:
    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir()
    (team_dir / "project.json").write_text(json.dumps({"task_source": {"epic_dir": "tasks"}}))

    write_task(tmp_path / "tasks", "epic-1-x", "1", v1_text(1, "1"))

    assert load_epic_dir_name(tmp_path) == "tasks"
    files = find_task_files(tmp_path, "tasks")
    assert len(files) == 1


def test_epic_filter_prefix_boundary(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-2", "1", v1_text(2, "1"))
    write_task(epics, "epic-2-slug", "1", v1_text(2, "1"))
    write_task(epics, "epic-20-slug", "1", v1_text(20, "1"))

    files = find_task_files(tmp_path, "epics", epic_filter=2)
    dirnames = {f.parent.name for f in files}
    assert dirnames == {"epic-2", "epic-2-slug"}
    assert "epic-20-slug" not in dirnames


def test_epic_md_and_readme_never_read_as_tasks(tmp_path: Path) -> None:
    epic_dir = tmp_path / "epics" / "epic-1-x"
    epic_dir.mkdir(parents=True)
    (epic_dir / "epic.md").write_text("# Epic overview\n")
    (epic_dir / "README.md").write_text("# Readme\n")
    (epic_dir / "1.md").write_text(v1_text(1, "1"))

    files = find_task_files(tmp_path, "epics")
    stems = {f.stem for f in files}
    assert stems == {"1"}


def test_milestone_filter_limits_v1_only(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    write_task(epics, "epic-1-x", "1", v1_text(1, "1", title="Launch task", milestone="launch"))
    write_task(epics, "epic-1-x", "2", v1_text(1, "2", title="Stage task", milestone="stage-1"))

    client = FakeGraphQLClient()
    run_import(
        tmp_path,
        "example-org/testproj",
        client,
        status_filter=set(),
        dry_run=False,
        epic_filter=None,
        milestone_filter={"launch"},
    )
    titles = [t for t, _ in client.create_calls]
    assert any(t.endswith("— Launch task") for t in titles)
    assert not any(t.endswith("— Stage task") for t in titles)


# ---------------------------------------------------------------------------
# AC9 — frontmatter parsing only through backend.task_file
# ---------------------------------------------------------------------------


def test_no_local_yaml_parsing() -> None:
    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert source.count("yaml.safe_load") == 0
    result = subprocess.run(
        ["grep", "-n", "def parse_frontmatter", str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# AC10 — --parent-index
# ---------------------------------------------------------------------------


def test_parent_index_format(tmp_path: Path) -> None:
    epic_dir = tmp_path / "epics" / "epic-4-parent-demo"
    epic_dir.mkdir(parents=True)
    (epic_dir / "epic.md").write_text("---\nparent_discussion: 900\n---\n# Epic overview\n")
    write_task(tmp_path / "epics", "epic-4-parent-demo", "1", v1_text(4, "1", title="First task", status="completed", planned_prs=0))
    write_task(tmp_path / "epics", "epic-4-parent-demo", "2", v1_text(4, "2", title="Second task"))

    parent_body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\nold parent body"
    client = FakeGraphQLClient(existing={900: {"title": "[Epic] epic-4 — Parent demo", "body": parent_body}})
    # Second task already imported earlier.
    client._discussions[901] = {
        "title": "[Feature] epic-4.2 — Second task",
        "body": "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->",
    }
    client._next_number = 902

    exit_code = run_parent_index(tmp_path, client, 900, dry_run=False)

    assert exit_code == 0
    assert len(client.update_calls) == 1
    updated_number, index_body = client.update_calls[0]
    assert updated_number == 900

    lines = index_body.splitlines()
    assert lines[0] == "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->"
    assert lines[1] == "---"
    assert "planned_prs: 0" in index_body
    assert "epic index: the Team Lead closes it" in index_body
    assert "Tasks: epics/epic-4-parent-demo/" in index_body
    assert "- 1 — First task — completed" in index_body
    assert "- 2 — Second task — D#901" in index_body
    assert extract_file_list(index_body) == []


# ---------------------------------------------------------------------------
# AC11 — Parent: D#<N> line
# ---------------------------------------------------------------------------


def test_parent_discussion_line_in_child_body(tmp_path: Path) -> None:
    epic_dir = tmp_path / "epics" / "epic-4-parent-demo"
    epic_dir.mkdir(parents=True)
    (epic_dir / "epic.md").write_text("---\nparent_discussion: 900\n---\n# Epic overview\n")
    write_task(tmp_path / "epics", "epic-4-parent-demo", "1", v1_text(4, "1"))

    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    assert len(client.create_calls) == 1
    _title, body = client.create_calls[0]
    assert "Parent: D#900" in body


def test_no_parent_line_when_epic_md_has_no_parent_discussion(tmp_path: Path) -> None:
    epic_dir = tmp_path / "epics" / "epic-4-parent-demo"
    epic_dir.mkdir(parents=True)
    (epic_dir / "epic.md").write_text("# Epic overview\n")
    write_task(tmp_path / "epics", "epic-4-parent-demo", "1", v1_text(4, "1"))

    client = FakeGraphQLClient()
    run_import(tmp_path, "example-org/testproj", client, status_filter=set(), dry_run=False, epic_filter=None)

    _title, body = client.create_calls[0]
    assert "Parent: D#" not in body
