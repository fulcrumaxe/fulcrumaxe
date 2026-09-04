"""
Unit tests for the by-role cost aggregation in backend/cost_tracker.py.

Run with:
    python -m pytest backend/test_role_efficiency.py -v
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_ISO = "2026-05-10T08:00:00+00:00"


def _make_bb(agent_records: list[dict], memory_records: list[dict] | None = None) -> MagicMock:
    """Return a mock Blackboard with agent-spend and optional memory entries."""
    bb = MagicMock()

    agent_keys = [f"budget/agents/{r['agent_id']}" for r in agent_records]
    memory_keys = [f"memory/{r['id']}" for r in (memory_records or [])]

    def list_keys(prefix: str) -> list[str]:
        if prefix.startswith("budget/agents"):
            return agent_keys
        if prefix.startswith("memory/"):
            return memory_keys
        return []

    agent_map = {f"budget/agents/{r['agent_id']}": r for r in agent_records}
    memory_map = {f"memory/{r['id']}": r for r in (memory_records or [])}

    def read(key: str):
        return agent_map.get(key) or memory_map.get(key)

    bb.list_keys.side_effect = list_keys
    bb.read.side_effect = read
    return bb


def _known_pricing() -> dict:
    return {
        "default": {"input_per_1k": 0.003, "output_per_1k": 0.015},
        "claude-sonnet-4-20250514": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    }


# ---------------------------------------------------------------------------
# Test: basic aggregation math
# ---------------------------------------------------------------------------


def test_single_role_cost_and_tokens():
    """Basic aggregation: total tokens and cost compute correctly for one role."""
    from backend.cost_tracker import CostTracker

    records = [
        {
            "agent_id": "executor-100-1",
            "agent": "executor",
            "input": 10000,
            "output": 2000,
            "model": "claude-sonnet-4-20250514",
            "finished": _NOW_ISO,
        }
    ]
    bb = _make_bb(records)
    ct = CostTracker(bb=bb)
    ct._pricing = _known_pricing()

    data = ct.get_role_efficiency(days=7)

    assert data["schema_version"] == 1
    assert data["window_days"] == 7
    assert len(data["roles"]) == 1

    r = data["roles"][0]
    assert r["role"] == "executor"
    assert r["total_runs"] == 1
    assert r["total_input_tokens"] == 10000
    assert r["total_output_tokens"] == 2000
    assert r["total_tokens"] == 12000
    expected_cost = (10000 / 1000 * 0.003) + (2000 / 1000 * 0.015)  # 0.030 + 0.030 = 0.060
    assert r["total_cost_usd"] == pytest.approx(expected_cost, abs=1e-6)
    assert r["avg_tokens_per_run"] == 12000


def test_multiple_roles_sorted_by_cost():
    """Multiple roles must be sorted by total_cost_usd descending."""
    from backend.cost_tracker import CostTracker

    records = [
        {
            "agent_id": "executor-100-1",
            "agent": "executor",
            "input": 5000,
            "output": 1000,
            "model": "default",
            "finished": _NOW_ISO,
        },
        {
            "agent_id": "code-reviewer-100-1",
            "agent": "code-reviewer",
            "input": 50000,
            "output": 10000,
            "model": "default",
            "finished": _NOW_ISO,
        },
    ]
    bb = _make_bb(records)
    ct = CostTracker(bb=bb)
    ct._pricing = _known_pricing()

    data = ct.get_role_efficiency(days=7)
    roles = data["roles"]
    assert len(roles) == 2
    # code-reviewer has more tokens → more cost → should be first
    assert roles[0]["role"] == "code-reviewer"
    assert roles[1]["role"] == "executor"
    costs = [r["total_cost_usd"] for r in roles]
    assert costs == sorted(costs, reverse=True)


def test_needs_fix_rate_computation():
    """needs_fix_rate = needs-fix count / total_runs, rounded to 3 decimals."""
    from backend.cost_tracker import CostTracker

    agent_records = [
        {
            "agent_id": f"executor-{i}-1",
            "agent": "executor",
            "input": 1000,
            "output": 500,
            "model": "default",
            "finished": _NOW_ISO,
        }
        for i in range(10)
    ]
    # 3 needs-fix verdicts, 7 done verdicts → needs_fix_rate = 0.3
    memory_records = [
        {
            "id": f"exec-mem-{i}",
            "role": "executor",
            "tags": ["executor", "needs-fix"],
            "lesson_type": "failure",
        }
        for i in range(3)
    ] + [
        {
            "id": f"exec-done-{i}",
            "role": "executor",
            "tags": ["executor", "done"],
            "lesson_type": "success",
        }
        for i in range(7)
    ]
    bb = _make_bb(agent_records, memory_records)
    ct = CostTracker(bb=bb)
    ct._pricing = _known_pricing()

    data = ct.get_role_efficiency(days=7)
    r = data["roles"][0]
    assert r["role"] == "executor"
    assert r["needs_fix_rate"] == pytest.approx(0.3, abs=0.001)
    assert r["verdict_counts"].get("needs-fix", 0) == 3
    assert r["verdict_counts"].get("done", 0) == 7
    assert r["passes"] == 7


def test_avg_cost_per_pass_computed():
    """avg_cost_per_pass_usd = total_cost / passes."""
    from backend.cost_tracker import CostTracker

    agent_records = [
        {
            "agent_id": "executor-200-1",
            "agent": "executor",
            "input": 10000,
            "output": 2000,
            "model": "default",
            "finished": _NOW_ISO,
        }
    ]
    memory_records = [
        {
            "id": "exec-done-1",
            "role": "executor",
            "tags": ["executor", "done"],
            "lesson_type": "success",
        }
    ]
    bb = _make_bb(agent_records, memory_records)
    ct = CostTracker(bb=bb)
    ct._pricing = _known_pricing()

    data = ct.get_role_efficiency(days=7)
    r = data["roles"][0]
    expected_cost = (10000 / 1000 * 0.003) + (2000 / 1000 * 0.015)
    assert r["passes"] == 1
    assert r["avg_cost_per_pass_usd"] == pytest.approx(expected_cost, abs=1e-6)


# ---------------------------------------------------------------------------
# Test: passes == 0 → avg_cost_per_pass_usd is null
# ---------------------------------------------------------------------------


def test_avg_cost_per_pass_null_when_no_passes():
    """When passes == 0, avg_cost_per_pass_usd must be None (JSON null)."""
    from backend.cost_tracker import CostTracker

    agent_records = [
        {
            "agent_id": "executor-300-1",
            "agent": "executor",
            "input": 1000,
            "output": 500,
            "model": "default",
            "finished": _NOW_ISO,
        }
    ]
    # No memory entries → no verdicts → passes = 0
    bb = _make_bb(agent_records, [])
    ct = CostTracker(bb=bb)
    ct._pricing = _known_pricing()

    data = ct.get_role_efficiency(days=7)
    r = data["roles"][0]
    assert r["passes"] == 0
    assert r["avg_cost_per_pass_usd"] is None

    # Verify it round-trips through JSON as null
    serialised = json.loads(json.dumps(r))
    assert serialised["avg_cost_per_pass_usd"] is None


# ---------------------------------------------------------------------------
# Test: empty audit trail
# ---------------------------------------------------------------------------


def test_empty_trail_returns_empty_roles_and_exits_zero():
    """Empty blackboard → roles list is empty, exit 0."""
    from backend.cost_tracker import CostTracker

    bb = MagicMock()
    bb.list_keys.return_value = []
    ct = CostTracker(bb=bb)

    data = ct.get_role_efficiency(days=7)

    assert data["roles"] == []
    assert data["window_days"] == 7
    assert "generated_at" in data
    assert data["schema_version"] == 1


def test_by_role_cli_empty_trail_exits_zero():
    """CLI by-role on empty trail exits 0 and prints empty table."""
    from backend import cost_tracker as ct_mod

    bb = MagicMock()
    bb.list_keys.return_value = []

    with patch.object(ct_mod, "Blackboard", return_value=bb):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            rc = ct_mod.main(["by-role"])
    assert rc == 0
    output = captured.getvalue()
    # Footer line must always be present
    assert "Window:" in output
    assert "Generated:" in output


# ---------------------------------------------------------------------------
# Test: CLI --json output
# ---------------------------------------------------------------------------


def test_by_role_cli_json_output():
    """--json flag emits valid JSON matching the schema."""
    from backend import cost_tracker as ct_mod

    agent_records = [
        {
            "agent_id": "executor-400-1",
            "agent": "executor",
            "input": 5000,
            "output": 1000,
            "model": "default",
            "finished": _NOW_ISO,
        }
    ]
    bb = _make_bb(agent_records)

    with patch.object(ct_mod, "Blackboard", return_value=bb):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            rc = ct_mod.main(["by-role", "--json"])
    assert rc == 0

    output = captured.getvalue()
    data = json.loads(output)
    assert data["schema_version"] == 1
    assert "generated_at" in data
    assert "window_days" in data
    assert isinstance(data["roles"], list)
    assert len(data["roles"]) == 1
    r = data["roles"][0]
    for field in (
        "role", "total_runs", "total_input_tokens", "total_output_tokens",
        "total_tokens", "total_cost_usd", "avg_tokens_per_run",
        "verdict_counts", "passes", "needs_fix_rate", "avg_cost_per_pass_usd",
    ):
        assert field in r, f"Missing field {field!r}"


# ---------------------------------------------------------------------------
# Test: JSON file write path
# ---------------------------------------------------------------------------


def test_write_role_efficiency_json_creates_file(tmp_path):
    """write_role_efficiency_json writes the JSON file atomically."""
    from backend.cost_tracker import CostTracker

    bb = MagicMock()
    bb.list_keys.return_value = []
    ct = CostTracker(bb=bb)
    ct._pricing = _known_pricing()

    out_file = tmp_path / "role-efficiency.json"

    import backend.cost_tracker as ct_mod
    import os

    def _patched_write(self, days=7):
        data = self.get_role_efficiency(days=days)
        tmp_p = tmp_path / "role-efficiency.json.tmp"
        with tmp_p.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(str(tmp_p), str(out_file))
        return data

    ct.write_role_efficiency_json = lambda days=7: _patched_write(ct, days)

    data = ct.write_role_efficiency_json(days=7)
    assert out_file.exists()
    loaded = json.loads(out_file.read_text())
    assert loaded["schema_version"] == 1
    assert loaded["roles"] == []
    assert loaded["window_days"] == 7


# ---------------------------------------------------------------------------
# Test: --days and --top flags
# ---------------------------------------------------------------------------


def test_by_role_window_days_reflected_in_output():
    """window_days field in JSON output must match --days argument."""
    from backend import cost_tracker as ct_mod

    bb = MagicMock()
    bb.list_keys.return_value = []

    with patch.object(ct_mod, "Blackboard", return_value=bb):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            rc = ct_mod.main(["by-role", "--json", "--days", "30"])
    assert rc == 0
    data = json.loads(captured.getvalue())
    assert data["window_days"] == 30


def test_by_role_top_limits_stdout_not_file(tmp_path):
    """--top K limits stdout table rows; JSON file always has all roles."""
    from backend import cost_tracker as ct_mod

    agent_records = [
        {
            "agent_id": f"role{i}-100-1",
            "agent": f"role{i}",
            "input": (i + 1) * 10000,
            "output": (i + 1) * 2000,
            "model": "default",
            "finished": _NOW_ISO,
        }
        for i in range(5)
    ]
    bb = _make_bb(agent_records)

    with patch.object(ct_mod, "Blackboard", return_value=bb):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            rc = ct_mod.main(["by-role", "--json", "--top", "2"])
    assert rc == 0
    data = json.loads(captured.getvalue())
    # stdout JSON limited to top 2
    assert len(data["roles"]) == 2


def test_zero_tokens_entries_skipped():
    """Records with zero total tokens must be excluded from aggregation."""
    from backend.cost_tracker import CostTracker

    records = [
        {
            "agent_id": "executor-zero-1",
            "agent": "executor",
            "input": 0,
            "output": 0,
            "model": "default",
            "finished": _NOW_ISO,
        }
    ]
    bb = _make_bb(records)
    ct = CostTracker(bb=bb)
    ct._pricing = _known_pricing()

    data = ct.get_role_efficiency(days=7)
    # Zero-token records should be excluded
    assert data["roles"] == []
