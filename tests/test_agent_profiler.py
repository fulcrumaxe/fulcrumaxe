"""
Tests for backend/agent_profiler.py — AgentProfiler class.

Covers:
- Empty data sources
- Single-role computation
- Aggregate computation
- CLI output format
- API response shape
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
from backend.agent_profiler import AgentProfiler, _KNOWN_ROLES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_profiler(tmp_path: Path) -> AgentProfiler:
    """Return an AgentProfiler backed entirely by tmp_path (no real state dir)."""
    bb = Blackboard(root=tmp_path / "blackboard")
    return AgentProfiler(state_dir=tmp_path / "state", bb=bb)


_seed_counter: list[int] = [0]


def _seed_lesson(
    profiler: AgentProfiler,
    role: str,
    lesson_type: str = "success",
    tags: list[str] | None = None,
    discussion: int | None = None,
) -> None:
    """Write one memory lesson into the profiler's blackboard.

    Uses a monotonically-increasing discussion number to avoid key collisions
    (AgentMemory keys are keyed on discussion+timestamp, which collides within
    the same second).
    """
    from backend.agent_memory import AgentMemory
    _seed_counter[0] += 1
    disc = discussion if discussion is not None else _seed_counter[0]
    mem = AgentMemory(bb=profiler._bb)
    mem.record_lesson(
        discussion=disc,
        role=role,
        lesson_type=lesson_type,
        content=f"{role} {lesson_type}",
        files=[],
        tags=tags or [],
    )


# ---------------------------------------------------------------------------
# 1. Empty data sources
# ---------------------------------------------------------------------------


def test_compute_with_empty_sources(tmp_path):
    """Profiler must not crash and must return zeroed metrics for all known roles."""
    profiler = _make_profiler(tmp_path)
    snapshot = profiler.compute()

    assert "computed_at" in snapshot
    assert "roles" in snapshot
    assert "aggregate" in snapshot

    for role in _KNOWN_ROLES:
        assert role in snapshot["roles"], f"role {role} missing"
        p = snapshot["roles"][role]
        assert p["total_spawns"] == 0
        assert p["success_rate"] is None
        assert p["median_duration_seconds"] is None
        assert p["total_tokens_used"] is None
        assert p["tokens_per_success"] is None
        assert p["failure_patterns"] == []


def test_snapshot_written_to_disk(tmp_path):
    """compute() must write agent-profiles.json."""
    profiler = _make_profiler(tmp_path)
    profiler.compute()

    profile_path = tmp_path / "state" / "agent-profiles.json"
    assert profile_path.exists(), "agent-profiles.json was not created"

    with profile_path.open() as fh:
        data = json.load(fh)
    assert "roles" in data


def test_snapshot_idempotent(tmp_path):
    """Running compute() twice should produce consistent results."""
    profiler = _make_profiler(tmp_path)
    s1 = profiler.compute()
    s2 = profiler.compute()
    assert s1["roles"].keys() == s2["roles"].keys()


# ---------------------------------------------------------------------------
# 2. Single-role computation
# ---------------------------------------------------------------------------


def test_executor_success_rate(tmp_path):
    profiler = _make_profiler(tmp_path)
    _seed_lesson(profiler, "executor", "success")
    _seed_lesson(profiler, "executor", "failure")

    snapshot = profiler.compute()
    p = snapshot["roles"]["executor"]
    assert p["total_spawns"] == 2
    assert p["success_rate"] == pytest.approx(0.5)


def test_failure_patterns_top_3(tmp_path):
    """failure_patterns must contain at most 3 entries with tag and count."""
    profiler = _make_profiler(tmp_path)
    for _ in range(5):
        _seed_lesson(profiler, "executor", "failure", tags=["type-error"])
    for _ in range(3):
        _seed_lesson(profiler, "executor", "failure", tags=["import-error"])
    for _ in range(1):
        _seed_lesson(profiler, "executor", "failure", tags=["lint"])
    for _ in range(1):
        _seed_lesson(profiler, "executor", "failure", tags=["build-error"])

    snapshot = profiler.compute()
    patterns = snapshot["roles"]["executor"]["failure_patterns"]
    assert len(patterns) <= 3
    for entry in patterns:
        assert "tag" in entry
        assert "count" in entry

    # Most frequent tag must be first
    if patterns:
        assert patterns[0]["tag"] == "type-error"
        assert patterns[0]["count"] == 5


def test_success_only_lessons(tmp_path):
    """All-success lessons should give success_rate=1.0 and empty failure_patterns."""
    profiler = _make_profiler(tmp_path)
    for _ in range(4):
        _seed_lesson(profiler, "code-reviewer", "success")

    snapshot = profiler.compute()
    p = snapshot["roles"]["code-reviewer"]
    assert p["success_rate"] == pytest.approx(1.0)
    assert p["failure_patterns"] == []


def test_no_token_metrics_when_no_budget_entries(tmp_path):
    """When budget/agents/* is empty, token metrics must be None."""
    profiler = _make_profiler(tmp_path)
    _seed_lesson(profiler, "executor", "success")

    snapshot = profiler.compute()
    p = snapshot["roles"]["executor"]
    assert p["total_tokens_used"] is None
    assert p["tokens_per_success"] is None


# ---------------------------------------------------------------------------
# 3. Aggregate computation
# ---------------------------------------------------------------------------


def test_aggregate_bottleneck_role(tmp_path):
    """bottleneck_role should be the role with the highest median_duration_seconds.

    We seed the executor role via a synthetic registry file to get timing data.
    """
    # Create a minimal registry with DONE discussions that have timing data
    import json as _json
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Write registry.json with one DONE discussion
    registry = {
        "version": 1,
        "synced_at": "2026-01-01T00:00:00+00:00",
        "discussions": [
            {
                "number": 1,
                "title": "Test",
                "status": "DONE",
                "category": "Feature",
                "created_at": "2026-01-01T00:00:00+00:00",
                "closed_at": "2026-01-01T01:00:00+00:00",  # 3600s later
                "pr": None,
                "labels": [],
            }
        ],
        "velocity": {},
    }
    (state_dir / "registry.json").write_text(_json.dumps(registry))

    bb = Blackboard(root=tmp_path / "blackboard")
    profiler = AgentProfiler(state_dir=state_dir, bb=bb)
    _seed_lesson(profiler, "executor", "success")

    snapshot = profiler.compute()
    agg = snapshot["aggregate"]
    # Executor should be the only role with duration data
    assert agg["bottleneck_role"] == "executor"


def test_aggregate_team_efficiency(tmp_path):
    """team_efficiency must equal successes/total across all roles."""
    profiler = _make_profiler(tmp_path)
    _seed_lesson(profiler, "executor", "success")
    _seed_lesson(profiler, "executor", "failure")
    _seed_lesson(profiler, "code-reviewer", "success")

    snapshot = profiler.compute()
    agg = snapshot["aggregate"]
    # 2 successes out of 3 total spawns (non-zero roles)
    assert agg["team_efficiency"] == pytest.approx(2 / 3)


def test_aggregate_null_when_no_data(tmp_path):
    """With no lessons at all, aggregate fields should be None."""
    profiler = _make_profiler(tmp_path)
    snapshot = profiler.compute()
    agg = snapshot["aggregate"]
    assert agg["bottleneck_role"] is None
    assert agg["most_expensive_role"] is None
    assert agg["team_efficiency"] is None


# ---------------------------------------------------------------------------
# 4. CLI output format
# ---------------------------------------------------------------------------


def test_cli_compute(tmp_path):
    """python backend/agent_profiler.py compute must exit 0 and write the file."""
    result = subprocess.run(
        [sys.executable, "backend/agent_profiler.py", "compute"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert "profiles computed for" in result.stdout


def test_cli_show(tmp_path):
    """python backend/agent_profiler.py show must exit 0 and print a table header."""
    # First compute so the snapshot exists
    subprocess.run(
        [sys.executable, "backend/agent_profiler.py", "compute"],
        capture_output=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    result = subprocess.run(
        [sys.executable, "backend/agent_profiler.py", "show"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    # Must have at least a header row
    assert "role" in result.stdout
    assert "spawns" in result.stdout


def test_cli_show_role(tmp_path):
    """python backend/agent_profiler.py show --role executor must mention executor."""
    subprocess.run(
        [sys.executable, "backend/agent_profiler.py", "compute"],
        capture_output=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    result = subprocess.run(
        [sys.executable, "backend/agent_profiler.py", "show", "--role", "executor"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert "executor" in result.stdout


# ---------------------------------------------------------------------------
# 5. API response shape
# ---------------------------------------------------------------------------


def test_api_profiles_shape(tmp_path):
    """GET /agents/profiles must return roles and aggregate keys."""
    profiler = _make_profiler(tmp_path)
    snapshot = profiler.compute()

    # Simulate what the API handler returns
    assert "roles" in snapshot
    assert "aggregate" in snapshot
    assert isinstance(snapshot["roles"], dict)
    assert isinstance(snapshot["aggregate"], dict)


def test_api_role_profile_shape(tmp_path):
    """Per-role profile must include all expected metric keys."""
    profiler = _make_profiler(tmp_path)
    _seed_lesson(profiler, "executor", "success")
    snapshot = profiler.compute()

    p = snapshot["roles"]["executor"]
    expected_keys = {
        "total_spawns",
        "success_rate",
        "median_duration_seconds",
        "total_tokens_used",
        "tokens_per_success",
        "first_pass_rate",
        "avg_lines_changed",
        "failure_patterns",
    }
    assert expected_keys.issubset(set(p.keys()))


def test_api_role_not_found(tmp_path):
    """get_role_profile for an unknown role returns None (caller maps to 404)."""
    profiler = _make_profiler(tmp_path)
    profiler.compute()
    result = profiler.get_role_profile("nonexistent-role")
    assert result is None


def test_api_aggregate_summary(tmp_path):
    """load_snapshot then get aggregate must have bottleneck_role, most_expensive_role, team_efficiency."""
    profiler = _make_profiler(tmp_path)
    profiler.compute()
    snapshot = profiler.load_snapshot()
    assert snapshot is not None
    agg = snapshot.get("aggregate", {})
    assert "bottleneck_role" in agg
    assert "most_expensive_role" in agg
    assert "team_efficiency" in agg
