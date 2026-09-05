"""conftest.py — shared fixtures for corpus drift audit tests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


def _make_transcript(tool_calls: list[dict], role: str = "assistant") -> str:
    """Build a minimal JSONL transcript with one assistant turn containing tool_calls."""
    content = [
        {
            "type": "tool_use",
            "name": tc["name"],
            "id": tc.get("id", f"tu_{i}"),
            "input": tc.get("input", {}),
        }
        for i, tc in enumerate(tool_calls)
    ]
    line = json.dumps({
        "message": {
            "role": role,
            "content": content,
        }
    })
    return line + "\n"


def _make_assistant_text_turn(text: str) -> str:
    """Build a JSONL line for an assistant text turn."""
    line = json.dumps({
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        }
    })
    return line + "\n"


@pytest.fixture
def tmp_transcript_dir(tmp_path):
    """A temporary directory to hold synthetic transcript .output files."""
    d = tmp_path / "transcripts"
    d.mkdir()
    return d


def write_transcript(directory: Path, agent_id: str, lines: list[str]) -> Path:
    """Write a .output JSONL transcript file and return its path."""
    path = directory / f"{agent_id}.output"
    path.write_text("".join(lines), encoding="utf-8")
    return path
