"""
Tests for backend/lessons.py — quality-score-driven lessons feedback loop.

Covers:
  - record + list + clear round trips
  - Expiry by age and by count
  - pick_for_prompt: files globs, max, dimension diversity
  - Lesson injection appears in pre-spawn-check JSON for executor role
"""

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

# Allow import from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.lessons import LessonsStore, LessonEntry, render_lessons_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path) -> LessonsStore:
    return LessonsStore(base_dir=tmp_path)


def record_lesson(
    store: LessonsStore,
    pr: int = 99,
    dimension: str = "test_coverage",
    score: float = 12.0,
    lesson: str = "Add test file for every new module",
    files_pattern: str = "dashboard/**",
    role: str = "executor",
) -> None:
    store.record(
        pr=pr,
        dimension=dimension,
        score=score,
        lesson=lesson,
        files_pattern=files_pattern,
        role=role,
    )


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

def test_record_creates_jsonl_file(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store)
    dim_file = tmp_path / "test_coverage.jsonl"
    assert dim_file.exists(), "JSONL file should be created after record()"
    lines = [ln for ln in dim_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["pr"] == 99
    assert data["dimension"] == "test_coverage"
    assert data["score"] == 12.0
    assert data["files_pattern"] == "dashboard/**"


def test_record_multiple_appends_to_same_file(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store, pr=1)
    record_lesson(store, pr=2)
    record_lesson(store, pr=3)
    dim_file = tmp_path / "test_coverage.jsonl"
    lines = [ln for ln in dim_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3


def test_list_by_dimension(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store, dimension="test_coverage", pr=1)
    record_lesson(store, dimension="complexity", pr=2)
    record_lesson(store, dimension="test_coverage", pr=3)
    lessons = store.list_lessons(dimension="test_coverage")
    assert len(lessons) == 2
    assert all(e.dimension == "test_coverage" for e in lessons)


def test_list_all_dimensions(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store, dimension="test_coverage", pr=1)
    record_lesson(store, dimension="complexity", pr=2)
    lessons = store.list_lessons()
    assert len(lessons) == 2


def test_clear_single_dimension(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store, dimension="test_coverage")
    record_lesson(store, dimension="complexity")
    n = store.clear(dimension="test_coverage")
    assert n == 1
    assert store.list_lessons(dimension="test_coverage") == []
    assert len(store.list_lessons(dimension="complexity")) == 1


def test_clear_all_dimensions(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store, dimension="test_coverage")
    record_lesson(store, dimension="complexity")
    n = store.clear()
    assert n == 2
    assert store.list_lessons() == []


# ---------------------------------------------------------------------------
# Expiry tests
# ---------------------------------------------------------------------------

def _write_old_lesson(base_dir: Path, dimension: str, days_old: float, pr: int = 100) -> None:
    """Write a lesson entry with a timestamp `days_old` days in the past."""
    base_dir.mkdir(parents=True, exist_ok=True)
    recorded_at = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    entry = {
        "pr": pr,
        "dimension": dimension,
        "score": 10.0,
        "lesson": "old lesson",
        "files_pattern": "*",
        "recorded_at": recorded_at,
        "role": "executor",
    }
    path = base_dir / f"{dimension}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def test_expiry_by_age(tmp_path):
    store = make_store(tmp_path)
    store._age_days = 30

    # Write a lesson 31 days old — should be excluded
    _write_old_lesson(tmp_path, "test_coverage", days_old=31)
    # Write a fresh lesson — should be included
    record_lesson(store, dimension="test_coverage", pr=200)

    lessons = store.list_lessons(dimension="test_coverage")
    assert len(lessons) == 1, "Only fresh lesson should survive age filter"
    assert lessons[0].pr == 200


def test_expiry_by_count(tmp_path):
    store = make_store(tmp_path)
    store._max_count = 3  # only keep last 3

    for i in range(1, 6):
        record_lesson(store, dimension="test_coverage", pr=i)

    lessons = store.list_lessons(dimension="test_coverage")
    assert len(lessons) == 3, "Should be capped at max_count=3"
    prs = {e.pr for e in lessons}
    assert prs == {3, 4, 5}


def test_both_age_and_count_filters(tmp_path):
    store = make_store(tmp_path)
    store._age_days = 30
    store._max_count = 2

    _write_old_lesson(tmp_path, "complexity", days_old=35, pr=1)  # expires by age
    record_lesson(store, dimension="complexity", pr=2)
    record_lesson(store, dimension="complexity", pr=3)
    record_lesson(store, dimension="complexity", pr=4)

    lessons = store.list_lessons(dimension="complexity")
    assert len(lessons) == 2
    prs = {e.pr for e in lessons}
    assert prs == {3, 4}


# ---------------------------------------------------------------------------
# pick_for_prompt tests
# ---------------------------------------------------------------------------

def test_pick_respects_max(tmp_path):
    store = make_store(tmp_path)
    for i in range(5):
        record_lesson(store, dimension=f"dim_{i}", pr=i, files_pattern="backend/**")

    results = store.pick_for_prompt(role="executor", files_globs=["backend/**"], max_lessons=2)
    assert len(results) == 2


def test_pick_matches_files_glob(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store, dimension="test_coverage", pr=10, files_pattern="dashboard/**")
    record_lesson(store, dimension="complexity", pr=11, files_pattern="backend/**")

    results = store.pick_for_prompt(role="executor", files_globs=["dashboard/**"], max_lessons=3)
    prs = {r["pr"] for r in results}
    assert 10 in prs


def test_pick_dimension_diversity(tmp_path):
    store = make_store(tmp_path)
    # 3 lessons from test_coverage, 1 from complexity
    for i in range(3):
        record_lesson(store, dimension="test_coverage", pr=i, files_pattern="*")
    record_lesson(store, dimension="complexity", pr=100, files_pattern="*")

    results = store.pick_for_prompt(role="executor", files_globs=["*"], max_lessons=3)
    dims = [r["dimension"] for r in results]
    assert "complexity" in dims


def test_pick_empty_when_no_lessons(tmp_path):
    store = make_store(tmp_path)
    results = store.pick_for_prompt(role="executor", files_globs=["dashboard/**"], max_lessons=3)
    assert results == []


def test_pick_fallback_when_no_glob_match(tmp_path):
    """When no glob matches, fall back to all role-filtered lessons."""
    store = make_store(tmp_path)
    record_lesson(store, dimension="test_coverage", pr=1, files_pattern="backend/**")
    # Ask for tui files — no match, fallback returns backend/** lesson
    results = store.pick_for_prompt(role="executor", files_globs=["tui/**"], max_lessons=3)
    assert len(results) == 1


def test_pick_no_globs_returns_all(tmp_path):
    store = make_store(tmp_path)
    record_lesson(store, dimension="test_coverage", pr=1, files_pattern="backend/**")
    record_lesson(store, dimension="complexity", pr=2, files_pattern="dashboard/**")
    results = store.pick_for_prompt(role="executor", files_globs=[], max_lessons=5)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# render_lessons_block
# ---------------------------------------------------------------------------

def test_render_lessons_block_empty():
    assert render_lessons_block([]) == ""


def test_render_lessons_block_nonempty():
    lessons = [
        {
            "dimension": "test_coverage",
            "score": 12,
            "lesson": "Add tests",
            "pr": 99,
            "files_pattern": "dashboard/**",
        },
    ]
    block = render_lessons_block(lessons)
    assert "## Recent lessons from low-scoring PRs" in block
    assert "test_coverage" in block
    assert "Add tests" in block
    assert "PR #99" in block


# ---------------------------------------------------------------------------
# CLI integration test
# ---------------------------------------------------------------------------

def test_cli_pick_for_prompt_json_output(tmp_path):
    """pick-for-prompt --json returns parseable JSON with expected lesson."""
    script = tmp_path / "run.py"
    repo_root = Path(__file__).resolve().parent.parent.parent
    lessons_dir = tmp_path / "lessons"
    script.write_text(f"""
import sys, json
sys.path.insert(0, '{repo_root}')
from backend.lessons import LessonsStore
from pathlib import Path
store = LessonsStore(base_dir=Path('{lessons_dir}'))
store.record(pr=42, dimension='complexity', score=5.0,
             lesson='Split large functions', files_pattern='backend/**')
results = store.pick_for_prompt(role='executor', files_globs=['backend/**'], max_lessons=3)
print(json.dumps(results))
""")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["dimension"] == "complexity"
    assert data[0]["pr"] == 42


# ---------------------------------------------------------------------------
# pre-spawn-check integration test
# ---------------------------------------------------------------------------

def test_pre_spawn_check_includes_lessons():
    """
    Verify that when matching lessons exist, pre-spawn-check.sh --dry-run returns
    JSON containing a non-empty 'lessons' array for the executor role.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    script = repo_root / "scripts" / "pre-spawn-check.sh"
    if not script.exists():
        pytest.skip("pre-spawn-check.sh not found — skipping integration test")

    # Write a lesson to the real lessons dir so pre-spawn-check can find it
    lessons_dir = repo_root / ".autonomous-team" / "lessons"
    store = LessonsStore(base_dir=lessons_dir)
    store.record(
        pr=9999,
        dimension="test_coverage",
        score=8.0,
        lesson="[pytest-integration] Add test file for every new backend module",
        files_pattern="backend/**",
        role="executor",
    )

    result = subprocess.run(
        ["bash", str(script), "--role", "executor", "--discussion", "392", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )

    # Clean up the test lesson
    try:
        lesson_file = lessons_dir / "test_coverage.jsonl"
        if lesson_file.exists():
            lines = lesson_file.read_text().splitlines()
            remaining = [ln for ln in lines if "[pytest-integration]" not in ln]
            lesson_file.write_text(
                "\n".join(remaining) + ("\n" if remaining else "")
            )
    except Exception:
        pass

    assert result.returncode == 0, f"pre-spawn-check.sh failed:\n{result.stderr}"

    # Extract first JSON object from stdout
    stdout = result.stdout
    lines = stdout.splitlines()
    json_start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("{")), None)
    if json_start is None:
        pytest.skip("Could not find JSON output in pre-spawn-check.sh output")

    json_text = "\n".join(lines[json_start:])
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        # Find first complete JSON object
        depth = 0
        end = 0
        for i, ch in enumerate(json_text):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        data = json.loads(json_text[:end])

    assert "lessons" in data, (
        f"'lessons' key missing from pre-spawn-check JSON. Keys: {list(data.keys())}"
    )
    assert isinstance(data["lessons"], list), "'lessons' should be a list"
    assert len(data["lessons"]) > 0, (
        "'lessons' array should be non-empty when matching lessons exist"
    )
