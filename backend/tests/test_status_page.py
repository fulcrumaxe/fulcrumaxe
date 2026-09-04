"""Tests for backend.status_page."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.status_page import (
    _DEFAULT_OUTPUT,
    load_config,
    load_metrics,
    load_registry,
    get_recent_commits,
    render_status_page,
    main,
)


# ---------------------------------------------------------------------------
# load_registry
# ---------------------------------------------------------------------------


def test_load_registry_valid_file(tmp_path):
    data = {"version": 1, "discussions": [{"number": 1, "title": "T", "status": "DONE"}]}
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(data))
    result = load_registry(p)
    assert result["version"] == 1
    assert len(result["discussions"]) == 1


def test_load_registry_missing_file(tmp_path):
    result = load_registry(tmp_path / "nonexistent.json")
    assert result == {}


def test_load_registry_corrupt_json(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text("{bad json")
    result = load_registry(p)
    assert result == {}


# ---------------------------------------------------------------------------
# load_metrics
# ---------------------------------------------------------------------------


def test_load_metrics_valid_file(tmp_path):
    p = tmp_path / "loop-metrics.jsonl"
    lines = [
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "duration_seconds": 10}),
        json.dumps({"timestamp": "2026-01-01T00:10:00Z", "duration_seconds": 15}),
    ]
    p.write_text("\n".join(lines))
    result = load_metrics(p, n=10)
    assert len(result) == 2
    assert result[0]["duration_seconds"] == 10


def test_load_metrics_missing_file(tmp_path):
    result = load_metrics(tmp_path / "no-metrics.jsonl")
    assert result == []


def test_load_metrics_respects_n(tmp_path):
    p = tmp_path / "loop-metrics.jsonl"
    lines = [json.dumps({"timestamp": f"2026-01-0{i}T00:00:00Z", "duration_seconds": i}) for i in range(1, 6)]
    p.write_text("\n".join(lines))
    result = load_metrics(p, n=3)
    assert len(result) == 3


def test_load_metrics_skips_blank_lines(tmp_path):
    p = tmp_path / "loop-metrics.jsonl"
    p.write_text('\n{"timestamp":"2026-01-01T00:00:00Z","duration_seconds":5}\n\n')
    result = load_metrics(p)
    assert len(result) == 1


def test_load_metrics_skips_invalid_json(tmp_path):
    p = tmp_path / "loop-metrics.jsonl"
    p.write_text('{"valid":true}\nnot-json\n{"also":"valid"}\n')
    result = load_metrics(p)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# get_recent_commits
# ---------------------------------------------------------------------------


def test_get_recent_commits_parses_output():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "abc1234 First commit\ndef5678 Second commit\n"
    with patch("backend.status_page.subprocess.run", return_value=mock_result):
        commits = get_recent_commits(n=2)
    assert len(commits) == 2
    assert commits[0] == "abc1234 First commit"


def test_get_recent_commits_nonzero_returncode():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("backend.status_page.subprocess.run", return_value=mock_result):
        commits = get_recent_commits()
    assert commits == []


def test_get_recent_commits_oserror():
    with patch("backend.status_page.subprocess.run", side_effect=OSError("git not found")):
        commits = get_recent_commits()
    assert commits == []


def test_get_recent_commits_timeout():
    import subprocess
    with patch("backend.status_page.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
        commits = get_recent_commits()
    assert commits == []


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_valid_file(tmp_path):
    data = {"version": "2.0", "boss_github_username": "user"}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    result = load_config(p)
    assert result["version"] == "2.0"


def test_load_config_missing_file(tmp_path):
    result = load_config(tmp_path / "no-config.json")
    assert result == {}


def test_load_config_corrupt_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{broken")
    result = load_config(p)
    assert result == {}


# ---------------------------------------------------------------------------
# render_status_page
# ---------------------------------------------------------------------------


def test_render_status_page_full_data():
    registry = {
        "version": 1,
        "discussions": [
            {"number": 10, "title": "Feature A", "status": "DONE"},
            {"number": 11, "title": "Bug B", "status": "IMPLEMENTING"},
        ],
        "velocity": {"tasks_per_day": 2.5, "avg_days_to_complete": 1.2},
    }
    metrics = [{"timestamp": "2026-01-01T00:00:00Z", "duration_seconds": 20, "agents_spawned": 3, "prs_merged": 1}]
    commits = ["abc1234 Add tests", "def5678 Fix bug"]
    config = {"gates": {"auto_merge": True, "security_review": False}}

    content = render_status_page(registry, metrics, commits, config)

    assert "# Project Status" in content
    assert "Feature A" in content
    assert "Bug B" in content
    assert "abc1234 Add tests" in content
    assert "auto_merge" in content
    assert "enabled" in content


def test_render_status_page_missing_data():
    content = render_status_page({}, [], [], {})
    assert "# Project Status" in content
    assert "No registry data available." in content
    assert "No loop metrics available." in content
    assert "No commit history available." in content


def test_render_status_page_contains_header():
    content = render_status_page({}, [], [], {})
    assert "<!-- generated:" in content


def test_render_status_page_active_work_section():
    registry = {
        "discussions": [
            {"number": 5, "title": "Ongoing", "status": "REVIEWING"},
        ]
    }
    content = render_status_page(registry, [], [], {})
    assert "## Active Work" in content
    assert "Ongoing" in content


def test_render_status_page_recently_completed():
    registry = {
        "discussions": [
            {"number": 3, "title": "Done task", "status": "DONE", "pr": 55},
        ]
    }
    content = render_status_page(registry, [], [], {})
    assert "Done task" in content
    assert "PR #55" in content


def test_render_status_page_loop_health_avg_duration():
    metrics = [
        {"timestamp": "2026-01-01T00:00:00Z", "duration_seconds": 10},
        {"timestamp": "2026-01-01T00:10:00Z", "duration_seconds": 20},
    ]
    content = render_status_page({}, metrics, [], {})
    assert "## Loop Health" in content
    assert "15" in content  # avg of 10 and 20


def test_render_status_page_actions_as_int_does_not_crash():
    # Reproduces the TypeError: 'int' object is not iterable crash.
    # The api.py producer writes "actions": 1 (an int count), not a list.
    metrics = [
        {
            "timestamp": "2026-05-18T22:26:23Z",
            "duration_seconds": 45,
            "agents_spawned": 5,
            "prs_merged": 1,
            "actions": 1,
        }
    ]
    content = render_status_page({}, metrics, [], {})
    assert "## Loop Health" in content
    assert "**Last loop actions:** 1" in content


def test_render_status_page_actions_as_list_still_renders():
    # Backward-compatibility: legacy rows with actions as a list of strings.
    metrics = [
        {
            "timestamp": "2026-05-18T22:26:23Z",
            "duration_seconds": 45,
            "agents_spawned": 2,
            "prs_merged": 0,
            "actions": ["spawned executor", "spawned code-reviewer"],
        }
    ]
    content = render_status_page({}, metrics, [], {})
    assert "- spawned executor" in content
    assert "- spawned code-reviewer" in content


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


def test_main_preview_prints_to_stdout(capsys):
    with patch("backend.status_page.load_registry", return_value={}), \
         patch("backend.status_page.load_metrics", return_value=[]), \
         patch("backend.status_page.get_recent_commits", return_value=[]), \
         patch("backend.status_page.load_config", return_value={}):
        rc = main(["preview"])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "# Project Status" in out


def test_main_generate_writes_to_output_file(tmp_path, capsys):
    output_file = tmp_path / "status.md"
    with patch("backend.status_page.load_registry", return_value={}), \
         patch("backend.status_page.load_metrics", return_value=[]), \
         patch("backend.status_page.get_recent_commits", return_value=[]), \
         patch("backend.status_page.load_config", return_value={}):
        rc = main(["generate", "--output", str(output_file)])
    assert rc == 0
    assert output_file.exists()
    content = output_file.read_text()
    assert "# Project Status" in content


def test_main_default_command_is_generate(tmp_path, capsys):
    output_file = tmp_path / "out.md"
    with patch("backend.status_page.load_registry", return_value={}), \
         patch("backend.status_page.load_metrics", return_value=[]), \
         patch("backend.status_page.get_recent_commits", return_value=[]), \
         patch("backend.status_page.load_config", return_value={}):
        rc = main(["--output", str(output_file)])
    assert rc == 0
    assert output_file.exists()


def test_main_output_dir_writes_only_under_given_dir_never_repo_wiki(tmp_path, capsys):
    """--output-dir must write only under the given dir, never under
    _REPO_ROOT/wiki — derived artifacts belong in the GitHub Wiki clone,
    not the source tree (D#1908)."""
    out_dir = tmp_path / "wiki-clone"

    before_mtime = _DEFAULT_OUTPUT.stat().st_mtime if _DEFAULT_OUTPUT.exists() else None

    with patch("backend.status_page.load_registry", return_value={}), \
         patch("backend.status_page.load_metrics", return_value=[]), \
         patch("backend.status_page.get_recent_commits", return_value=[]), \
         patch("backend.status_page.load_config", return_value={}):
        rc = main(["generate", "--output-dir", str(out_dir)])
    out, _ = capsys.readouterr()

    written_file = out_dir / "Project-Status.md"
    assert rc == 0
    assert written_file.exists()
    assert str(out_dir) in out
    assert str(_DEFAULT_OUTPUT.parent) not in out

    # The source-tree wiki/ default output must be completely untouched.
    if before_mtime is None:
        assert not _DEFAULT_OUTPUT.exists()
    else:
        assert _DEFAULT_OUTPUT.stat().st_mtime == before_mtime


def test_main_output_dir_takes_precedence_over_output(tmp_path, capsys):
    out_dir = tmp_path / "wiki-clone"
    decoy_output = tmp_path / "decoy.md"
    with patch("backend.status_page.load_registry", return_value={}), \
         patch("backend.status_page.load_metrics", return_value=[]), \
         patch("backend.status_page.get_recent_commits", return_value=[]), \
         patch("backend.status_page.load_config", return_value={}):
        rc = main(["generate", "--output", str(decoy_output), "--output-dir", str(out_dir)])
    assert rc == 0
    assert (out_dir / "Project-Status.md").exists()
    assert not decoy_output.exists()
