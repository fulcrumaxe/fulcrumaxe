"""Tests for backend/fleet/cost_summary.py and backend/rpc/fleet_cost.py.

Acceptance Criteria:
1. update_cost_summary records input+output tokens into cost_summary.json.
2. cache_read_tokens are NOT included in billable totals.
3. Rolling 7-day window — oldest entries are pruned.
4. read_cost_summary returns correct today/7d/projected_eod totals.
5. fleet_cost RPC aggregates 3 projects' cost_summary.json files correctly.
6. ETag/304: replaying the same request with matching etag returns not_modified.
7. An unreadable project is included in per_project with no token fields
   at all — never a fabricated zero (D#2317 PR-b).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.fleet.cost_summary import update_cost_summary, read_cost_summary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_project_json(state_dir: Path, name: str, port: int = 5100) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "project.json").write_text(
        json.dumps({"project_name": name, "dashboard_port": port, "version": 1}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AC1: update_cost_summary records tokens
# ---------------------------------------------------------------------------


def test_update_creates_cost_summary_json(tmp_path):
    state_dir = tmp_path / ".alpha-state"
    update_cost_summary(state_dir, input_tokens=1000, output_tokens=500)

    path = state_dir / "cost_summary.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert "last_7d" in data
    today_entry = data["last_7d"][0]
    assert today_entry["input_tokens"] == 1000
    assert today_entry["output_tokens"] == 500


def test_update_accumulates_across_multiple_calls(tmp_path):
    state_dir = tmp_path / ".beta-state"
    update_cost_summary(state_dir, input_tokens=1000, output_tokens=200)
    update_cost_summary(state_dir, input_tokens=500, output_tokens=100)

    data = json.loads((state_dir / "cost_summary.json").read_text())
    today_entry = data["last_7d"][0]
    assert today_entry["input_tokens"] == 1500
    assert today_entry["output_tokens"] == 300


# ---------------------------------------------------------------------------
# AC2: cache_read_tokens are excluded
# ---------------------------------------------------------------------------


def test_cache_read_tokens_excluded_from_billable(tmp_path):
    state_dir = tmp_path / ".cache-test"
    update_cost_summary(
        state_dir,
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=99999,  # free — must NOT appear in summary
    )
    summary = read_cost_summary(state_dir)
    # today's total = input + output only, not cache_read
    assert summary["tokens_today_utc"] == 1200  # 1000 + 200


# ---------------------------------------------------------------------------
# AC3: rolling 7-day window pruned
# ---------------------------------------------------------------------------


def test_rolling_window_prunes_beyond_7_days(tmp_path):
    state_dir = tmp_path / ".window-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Write 10 days of history directly
    entries = []
    for i in range(10):
        day = (datetime.now(timezone.utc) - timedelta(days=9 - i)).strftime("%Y-%m-%d")
        entries.append({"date": day, "input_tokens": 100, "output_tokens": 50})

    path = state_dir / "cost_summary.json"
    path.write_text(
        json.dumps({"updated_at": "", "last_7d": entries}),
        encoding="utf-8",
    )

    # One more update should prune old entries
    update_cost_summary(state_dir, input_tokens=0, output_tokens=0)

    data = json.loads(path.read_text())
    assert len(data["last_7d"]) <= 7


# ---------------------------------------------------------------------------
# AC4: read_cost_summary totals
# ---------------------------------------------------------------------------


def test_read_cost_summary_totals(tmp_path):
    state_dir = tmp_path / ".totals-state"
    update_cost_summary(state_dir, input_tokens=10_000, output_tokens=2_000)

    summary = read_cost_summary(state_dir)
    assert summary["tokens_today_utc"] == 12_000
    assert summary["tokens_7d"] >= 12_000
    assert "projected_eod_tokens" in summary
    assert isinstance(summary["projected_eod_tokens"], int)


def test_read_cost_summary_empty_state_dir(tmp_path):
    """Empty state dir reports no signal, not zeroes (D#2317 PR-b item 5)."""
    state_dir = tmp_path / ".empty-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    summary = read_cost_summary(state_dir)
    assert summary["tokens_today_utc"] is None
    assert summary["tokens_7d"] is None
    assert summary["projected_eod_tokens"] is None


# ---------------------------------------------------------------------------
# AC5: fleet_cost RPC aggregates 3 projects
# ---------------------------------------------------------------------------


def test_fleet_cost_rpc_aggregates_projects(tmp_path):
    # Set up 3 project state dirs
    alpha = tmp_path / ".alpha-state"
    beta = tmp_path / ".beta-state"
    gamma = tmp_path / ".gamma-state"

    _write_project_json(alpha, "alpha")
    _write_project_json(beta, "beta", port=5101)
    _write_project_json(gamma, "gamma", port=5102)

    update_cost_summary(alpha, input_tokens=10_000, output_tokens=2_000)
    update_cost_summary(beta, input_tokens=5_000, output_tokens=1_000)
    update_cost_summary(gamma, input_tokens=3_000, output_tokens=500)

    fake_projects = [
        {"name": "alpha", "state_dir": str(alpha), "status": "ok"},
        {"name": "beta", "state_dir": str(beta), "status": "unknown"},
        {"name": "gamma", "state_dir": str(gamma), "status": "down"},
    ]

    from backend.rpc import fleet_cost

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=fake_projects):
        result = fleet_cost.handle({})

    assert result["total_today_utc"] == 21_500  # 12000 + 6000 + 3500
    assert result["total_7d"] >= result["total_today_utc"]
    assert len(result["per_project"]) == 3
    assert "etag" in result
    # All 3 projects should be ok
    assert all(p["ok"] for p in result["per_project"])


# ---------------------------------------------------------------------------
# AC6: ETag/304
# ---------------------------------------------------------------------------


def test_fleet_cost_etag_304(tmp_path):
    alpha = tmp_path / ".alpha-state"
    _write_project_json(alpha, "alpha")
    update_cost_summary(alpha, input_tokens=1000, output_tokens=200)

    fake_projects = [{"name": "alpha", "state_dir": str(alpha), "status": "ok"}]

    from backend.rpc import fleet_cost

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=fake_projects):
        first = fleet_cost.handle({})

    etag = first["etag"]

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=fake_projects):
        second = fleet_cost.handle({"if_none_match": etag})

    assert second.get("not_modified") is True
    assert second["etag"] == etag
    assert "total_today_utc" not in second


# ---------------------------------------------------------------------------
# AC7: unreadable project included, with no token fields at all
# ---------------------------------------------------------------------------


def test_fleet_cost_corrupted_project_included(tmp_path):
    good = tmp_path / ".good-state"
    _write_project_json(good, "good")
    update_cost_summary(good, input_tokens=5000, output_tokens=1000)

    fake_projects = [
        {"name": "good", "state_dir": str(good), "status": "ok"},
        {"name": "bad", "state_dir": str(tmp_path / ".bad-state"),
         "status": "error", "error": "JSON parse error"},
    ]

    from backend.rpc import fleet_cost

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=fake_projects):
        result = fleet_cost.handle({})

    assert len(result["per_project"]) == 2
    bad_entry = next(p for p in result["per_project"] if p["name"] == "bad")
    assert bad_entry["ok"] is False
    # A project we could not read has no spend measurement — reporting 0
    # here is what made every dead fixture look like a live, quiet project.
    assert not [k for k in bad_entry if k.startswith("tokens_")], bad_entry
