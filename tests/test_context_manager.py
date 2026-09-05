"""
Tests for backend/context_manager.py — ProjectContext class.
"""

from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.context_manager import ProjectContext


def test_load_empty(ctx):
    data = ctx.load()
    assert data["goals"] == []
    assert data["decisions"] == []
    assert data["milestones"] == []
    assert data["stack"] == []
    assert data["banned"] == []


def test_add_goal_and_load(ctx):
    gid = ctx.add_goal("Build interactive TUI", status="in-progress")
    data = ctx.load()
    assert len(data["goals"]) == 1
    goal = data["goals"][0]
    assert goal["id"] == gid
    assert goal["text"] == "Build interactive TUI"
    assert goal["status"] == "in-progress"


def test_add_decision_with_rationale(ctx):
    did = ctx.add_decision("Use ink for TUI", rationale="React model fits well")
    data = ctx.load()
    assert len(data["decisions"]) == 1
    decision = data["decisions"][0]
    assert decision["id"] == did
    assert decision["text"] == "Use ink for TUI"
    assert decision["rationale"] == "React model fits well"


def test_add_milestone_and_mark_done(ctx):
    mid = ctx.add_milestone("Ship TUI MVP")
    data = ctx.load()
    assert len(data["milestones"]) == 1
    assert data["milestones"][0]["status"] == "pending"

    ok = ctx.mark_milestone_done(mid, pr=42)
    assert ok is True

    data2 = ctx.load()
    m = data2["milestones"][0]
    assert m["status"] == "done"
    assert m["pr"] == 42


def test_mark_done_nonexistent(ctx):
    ok = ctx.mark_milestone_done("m999")
    assert ok is False


def test_add_banned(ctx):
    bid = ctx.add_banned("tmux send-keys", reason="PTY race conditions")
    data = ctx.load()
    assert len(data["banned"]) == 1
    b = data["banned"][0]
    assert b["id"] == bid
    assert b["approach"] == "tmux send-keys"
    assert b["reason"] == "PTY race conditions"


def test_add_stack_deduplicates(ctx):
    ctx.add_stack("TypeScript + ink (TUI)")
    ctx.add_stack("TypeScript + ink (TUI)")
    data = ctx.load()
    assert data["stack"].count("TypeScript + ink (TUI)") == 1


def test_format_for_prompt_includes_sections(ctx):
    ctx.add_goal("Build TUI")
    ctx.add_decision("Use ink", rationale="React model")
    ctx.add_milestone("Ship MVP")
    output = ctx.format_for_prompt()
    assert "Goals" in output
    assert "Key Decisions" in output
    assert "Milestones" in output
    assert "Build TUI" in output
    assert "Use ink" in output
    assert "Ship MVP" in output


def test_format_for_prompt_truncates(ctx):
    # Add enough items to push past 2000 chars
    for i in range(50):
        ctx.add_goal(f"Goal number {i}: " + "x" * 50)
    output = ctx.format_for_prompt()
    assert len(output) <= 2100  # allow a small margin for the truncation suffix


def test_save_load_round_trip(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = ProjectContext(state_dir=state_dir)

    ctx.add_goal("Alpha goal")
    ctx.add_decision("Key decision", rationale="Because")
    ctx.add_milestone("First milestone")
    ctx.add_banned("Bad pattern", reason="Causes bugs")
    ctx.add_stack("Python 3.12")

    # Re-create from same state_dir — data should persist
    ctx2 = ProjectContext(state_dir=state_dir)
    data = ctx2.load()
    assert any(g["text"] == "Alpha goal" for g in data["goals"])
    assert any(d["text"] == "Key decision" for d in data["decisions"])
    assert any(m["text"] == "First milestone" for m in data["milestones"])
    assert any(b["approach"] == "Bad pattern" for b in data["banned"])
    assert "Python 3.12" in data["stack"]
