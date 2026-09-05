"""
Tests for backend/status_page.py — render_status_page().

Calls the renderer directly with mock dicts/lists — no subprocess calls,
no file I/O, no GitHub API.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.status_page import render_status_page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _basic_registry(discussions=None):
    return {"discussions": discussions or []}


def _done_discussion(number, title, pr=None, closed_at="2026-01-10T12:00:00Z"):
    d = {"number": number, "title": title, "status": "DONE", "closed_at": closed_at}
    if pr:
        d["pr"] = pr
    return d


def _active_discussion(number, title, status="IMPLEMENTING"):
    return {"number": number, "title": title, "status": status}


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

def test_render_returns_string():
    out = render_status_page({}, [], [], {})
    assert isinstance(out, str)


def test_render_contains_project_status_heading():
    out = render_status_page({}, [], [], {})
    assert "# Project Status" in out


def test_render_contains_all_sections():
    out = render_status_page(_basic_registry(), [], [], {})
    assert "## Project Health" in out
    assert "## Active Work" in out
    assert "## Recent Activity" in out
    assert "## Loop Health" in out


def test_render_empty_registry_shows_no_registry_message():
    out = render_status_page({}, [], [], {})
    assert "No registry data" in out


# ---------------------------------------------------------------------------
# Project Health table
# ---------------------------------------------------------------------------

def test_project_health_table_shows_counts():
    registry = _basic_registry([
        _done_discussion(1, "First task"),
        _active_discussion(2, "Second task", "IMPLEMENTING"),
    ])
    out = render_status_page(registry, [], [], {})
    assert "Total discussions" in out
    assert "2" in out
    assert "1" in out


def test_project_health_completion_rate():
    registry = _basic_registry([
        _done_discussion(1, "Done"),
        _done_discussion(2, "Done2"),
        _active_discussion(3, "Active"),
        _active_discussion(4, "Active2"),
    ])
    out = render_status_page(registry, [], [], {})
    assert "50%" in out


def test_project_health_all_done():
    registry = _basic_registry([
        _done_discussion(1, "One"),
        _done_discussion(2, "Two"),
    ])
    out = render_status_page(registry, [], [], {})
    assert "100%" in out


# ---------------------------------------------------------------------------
# Active Work table
# ---------------------------------------------------------------------------

def test_active_work_shows_implementing_discussions():
    registry = _basic_registry([
        _active_discussion(10, "Feature X", "IMPLEMENTING"),
    ])
    out = render_status_page(registry, [], [], {})
    assert "Feature X" in out
    assert "IMPLEMENTING" in out


def test_active_work_no_active_shows_message():
    registry = _basic_registry([_done_discussion(1, "Done")])
    out = render_status_page(registry, [], [], {})
    assert "No active discussions" in out


def test_active_work_escapes_pipe_in_title():
    registry = _basic_registry([
        _active_discussion(11, "Feature A|B", "SPEC_READY"),
    ])
    out = render_status_page(registry, [], [], {})
    assert "Feature A\\|B" in out


# ---------------------------------------------------------------------------
# Recent Activity
# ---------------------------------------------------------------------------

def test_recent_activity_shows_done_discussions():
    registry = _basic_registry([
        _done_discussion(5, "Shipped feature", pr=42),
    ])
    out = render_status_page(registry, [], [], {})
    assert "Shipped feature" in out
    assert "PR #42" in out


def test_recent_activity_shows_commits():
    commits = ["abc1234 Add feature", "def5678 Fix bug"]
    out = render_status_page({}, [], commits, {})
    assert "abc1234 Add feature" in out
    assert "def5678 Fix bug" in out


def test_recent_activity_no_commits_shows_message():
    out = render_status_page({}, [], [], {})
    assert "No commit history available" in out


# ---------------------------------------------------------------------------
# Loop Health
# ---------------------------------------------------------------------------

def test_loop_health_no_metrics():
    out = render_status_page({}, [], [], {})
    assert "No loop metrics available" in out


def test_loop_health_shows_last_run_timestamp():
    metrics = [
        {"timestamp": "2026-01-10T09:00:00Z", "duration_seconds": 45, "agents_spawned": 2, "prs_merged": 1, "idle": False},
    ]
    out = render_status_page({}, metrics, [], {})
    assert "2026-01-10T09:00:00Z" in out
    assert "45" in out


def test_loop_health_idle_ratio():
    metrics = [
        {"timestamp": "T1", "duration_seconds": 10, "agents_spawned": 0, "prs_merged": 0, "idle": True},
        {"timestamp": "T2", "duration_seconds": 10, "agents_spawned": 0, "prs_merged": 0, "idle": True},
        {"timestamp": "T3", "duration_seconds": 10, "agents_spawned": 1, "prs_merged": 0, "idle": False},
        {"timestamp": "T4", "duration_seconds": 10, "agents_spawned": 1, "prs_merged": 0, "idle": False},
    ]
    out = render_status_page({}, metrics, [], {})
    assert "50%" in out


# ---------------------------------------------------------------------------
# Config Gates
# ---------------------------------------------------------------------------

def test_gates_section_shown_when_config_has_gates():
    config = {"gates": {"auto_merge": True, "security_review": False}}
    out = render_status_page({}, [], [], config)
    assert "## Active Gates" in out
    assert "auto_merge" in out
    assert "enabled" in out
    assert "disabled" in out


def test_gates_section_absent_when_config_empty():
    out = render_status_page({}, [], [], {})
    assert "## Active Gates" not in out
