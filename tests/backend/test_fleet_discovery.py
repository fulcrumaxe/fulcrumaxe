"""Tests for backend/fleet/discovery.py.

Acceptance Criteria:
1. Happy path — 1 project: discover_projects() returns exactly one record with ok=True.
2. Happy path — N projects: returns one record per project, sorted by name.
3. Happy path — no projects: returns an empty list (no ~/.*-state/ dirs with project.json).
4. Corruption path — invalid project.json returns {ok: False, error: "..."}, not silent-drop.
5. Other projects still listed normally when one is corrupted.
6. 5-second cache: second call within 5s returns same object; call after cache expires re-scans.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.fleet import discovery as fleet_discovery
from backend.fleet.discovery import discover_projects, invalidate_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_project_json(state_dir: Path, data: dict | str) -> Path:
    """Write a project.json into state_dir/project.json."""
    state_dir.mkdir(parents=True, exist_ok=True)
    pj = state_dir / "project.json"
    if isinstance(data, dict):
        pj.write_text(json.dumps(data, indent=2))
    else:
        pj.write_text(data)  # raw string — for corruption tests
    return pj


def _valid_project_json(name: str, port: int = 5100, state_dir: str = "") -> dict:
    return {
        "project_name": name,
        "state_dir": state_dir,
        "dashboard_port": port,
        "version": 1,
        "repo": f"acme/{name}",
        "language": "python",
    }


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the discovery cache before and after each test."""
    invalidate_cache()
    yield
    invalidate_cache()


# ---------------------------------------------------------------------------
# Happy path — 1 project
# ---------------------------------------------------------------------------


def test_one_project(tmp_path):
    state_dir = tmp_path / ".alpha-state"
    _write_project_json(state_dir, _valid_project_json("alpha", port=5100, state_dir=str(state_dir)))

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        results = discover_projects()

    assert len(results) == 1
    r = results[0]
    assert r["ok"] is True
    assert r["name"] == "alpha"
    assert r["dashboard_port"] == 5100


# ---------------------------------------------------------------------------
# Happy path — N projects
# ---------------------------------------------------------------------------


def test_multiple_projects(tmp_path):
    dirs = {}
    for name, port in [("beta", 5101), ("alpha", 5100), ("gamma", 5102)]:
        sd = tmp_path / f".{name}-state"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "project.json").write_text(json.dumps(_valid_project_json(name, port=port, state_dir=str(sd))))
        dirs[name] = sd

    glob_results = [str(sd / "project.json") for sd in dirs.values()]

    with patch("glob.glob", return_value=glob_results):
        results = discover_projects()

    assert len(results) == 3
    # Must be sorted by name
    names = [r["name"] for r in results]
    assert names == sorted(names)
    assert all(r["ok"] for r in results)


# ---------------------------------------------------------------------------
# Happy path — no projects
# ---------------------------------------------------------------------------


def test_no_projects():
    with patch("glob.glob", return_value=[]):
        results = discover_projects()

    assert results == []


# ---------------------------------------------------------------------------
# Corruption path — invalid JSON returns {ok: False, error: ...}
# ---------------------------------------------------------------------------


def test_corrupted_project_json_returns_error_record(tmp_path):
    state_dir = tmp_path / ".corrupt-state"
    _write_project_json(state_dir, "THIS IS NOT JSON {{{")

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        results = discover_projects()

    assert len(results) == 1
    r = results[0]
    assert r["ok"] is False
    assert "error" in r
    assert "JSON" in r["error"] or "parse" in r["error"].lower()
    # Never silently drops — returns the record, not []
    assert r["name"]  # name was guessed from directory


def test_corrupted_json_does_not_drop_good_projects(tmp_path):
    good_dir = tmp_path / ".good-state"
    _write_project_json(good_dir, _valid_project_json("good", port=5100, state_dir=str(good_dir)))

    bad_dir = tmp_path / ".bad-state"
    _write_project_json(bad_dir, "CORRUPT")

    glob_results = [str(good_dir / "project.json"), str(bad_dir / "project.json")]

    with patch("glob.glob", return_value=glob_results):
        results = discover_projects()

    assert len(results) == 2
    good_records = [r for r in results if r.get("ok")]
    bad_records = [r for r in results if not r.get("ok")]
    assert len(good_records) == 1
    assert len(bad_records) == 1
    assert good_records[0]["name"] == "good"


# ---------------------------------------------------------------------------
# 5-second cache validation
# ---------------------------------------------------------------------------


def test_cache_returns_same_object_within_ttl(tmp_path):
    state_dir = tmp_path / ".alpha-state"
    _write_project_json(state_dir, _valid_project_json("alpha", state_dir=str(state_dir)))

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        first = discover_projects()

    # Modify the on-disk file — cache should not reflect this
    (state_dir / "project.json").write_text(json.dumps(_valid_project_json("alpha-modified", state_dir=str(state_dir))))

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        second = discover_projects()

    # Same list object returned (cache hit)
    assert first is second
    assert first[0]["name"] == "alpha"  # Not "alpha-modified"


def test_cache_expires_after_ttl(tmp_path, monkeypatch):
    state_dir = tmp_path / ".alpha-state"
    _write_project_json(state_dir, _valid_project_json("alpha", state_dir=str(state_dir)))

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        first = discover_projects()

    # Simulate time passing beyond TTL
    monkeypatch.setattr(fleet_discovery, "_cache_ts", time.monotonic() - fleet_discovery._CACHE_TTL_S - 1)

    # Update the file to return a different name
    (state_dir / "project.json").write_text(json.dumps(_valid_project_json("alpha-v2", state_dir=str(state_dir))))

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        second = discover_projects()

    assert first is not second
    assert second[0]["name"] == "alpha-v2"


def test_invalidate_cache_forces_rescan(tmp_path):
    state_dir = tmp_path / ".alpha-state"
    _write_project_json(state_dir, _valid_project_json("alpha", state_dir=str(state_dir)))

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        first = discover_projects()

    invalidate_cache()

    (state_dir / "project.json").write_text(json.dumps(_valid_project_json("alpha-v3", state_dir=str(state_dir))))

    with patch("glob.glob", return_value=[str(state_dir / "project.json")]):
        second = discover_projects()

    assert second[0]["name"] == "alpha-v3"


# ---------------------------------------------------------------------------
# Deduplication via realpath
# ---------------------------------------------------------------------------


def test_symlink_dedup(tmp_path):
    """Two paths pointing to the same realpath via symlink return only one record."""
    state_dir = tmp_path / ".real-state"
    _write_project_json(state_dir, _valid_project_json("real", state_dir=str(state_dir)))

    symlink_dir = tmp_path / ".sym-state"
    symlink_dir.symlink_to(state_dir)

    glob_results = [
        str(state_dir / "project.json"),
        str(symlink_dir / "project.json"),
    ]

    with patch("glob.glob", return_value=glob_results):
        results = discover_projects()

    # Both paths resolve to the same realpath — only one record
    assert len(results) == 1
    assert results[0]["ok"] is True
