"""Tests for loop_idle_ratio_24h (Discussion #540 P2 metric #2)."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from backend.stats_writer import loop_idle_ratio_24h


def _write_metrics(rows: list[dict], path: Path) -> None:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _ts(offset_hours: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=offset_hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_ratio_four_of_ten():
    rows = (
        [{"timestamp": _ts(i * 0.5), "agents_spawned": 0, "idle": True} for i in range(4)]
        + [{"timestamp": _ts(i * 0.5 + 3), "agents_spawned": 2, "idle": False} for i in range(6)]
    )
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        path = Path(fh.name)
    _write_metrics(rows, path)
    result = loop_idle_ratio_24h(metrics_path=str(path))
    assert result["sample_size"] == 10
    assert result["idle_count"] == 4
    assert result["ratio"] == pytest.approx(0.4)
    path.unlink(missing_ok=True)


def test_small_sample_returns_none_ratio():
    rows = [{"timestamp": _ts(0.1 * i), "agents_spawned": 1, "idle": False} for i in range(4)]
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        path = Path(fh.name)
    _write_metrics(rows, path)
    result = loop_idle_ratio_24h(metrics_path=str(path))
    assert result["sample_size"] == 4
    assert result["ratio"] is None
    path.unlink(missing_ok=True)


def test_test_origin_excluded():
    rows = (
        [{"timestamp": _ts(0.1 * i), "agents_spawned": 0, "idle": True} for i in range(5)]
        + [{"timestamp": _ts(0.1 * i + 1), "origin": "test", "agents_spawned": 3, "idle": False} for i in range(10)]
    )
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        path = Path(fh.name)
    _write_metrics(rows, path)
    result = loop_idle_ratio_24h(metrics_path=str(path))
    assert result["sample_size"] == 5
    assert result["idle_count"] == 5
    assert result["ratio"] == pytest.approx(1.0)
    path.unlink(missing_ok=True)


def test_old_rows_excluded():
    rows = (
        [{"timestamp": _ts(0.1 * i), "agents_spawned": 0, "idle": True} for i in range(3)]
        + [{"timestamp": _ts(25 + i), "agents_spawned": 5, "idle": False} for i in range(20)]
    )
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        path = Path(fh.name)
    _write_metrics(rows, path)
    result = loop_idle_ratio_24h(metrics_path=str(path))
    assert result["sample_size"] == 3
    assert result["ratio"] is None
    path.unlink(missing_ok=True)


def test_fallback_to_agents_spawned_zero():
    rows = (
        [{"timestamp": _ts(0.1 * i), "agents_spawned": 0} for i in range(4)]
        + [{"timestamp": _ts(0.1 * i + 1), "agents_spawned": 3} for i in range(6)]
    )
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        path = Path(fh.name)
    _write_metrics(rows, path)
    result = loop_idle_ratio_24h(metrics_path=str(path))
    assert result["sample_size"] == 10
    assert result["idle_count"] == 4
    assert result["ratio"] == pytest.approx(0.4)
    path.unlink(missing_ok=True)


def test_missing_file_returns_none_ratio():
    result = loop_idle_ratio_24h(metrics_path="/tmp/__nonexistent_loop_metrics__.jsonl")
    assert result["ratio"] is None
    assert result["sample_size"] == 0
    assert result["idle_count"] == 0


def test_malformed_lines_skipped():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        path = Path(fh.name)
        for i in range(6):
            fh.write(json.dumps({"timestamp": _ts(0.1 * i), "agents_spawned": 1}) + "\n")
        fh.write("not-json\n")
        fh.write("{broken\n")
    result = loop_idle_ratio_24h(metrics_path=str(path))
    assert result["sample_size"] == 6
    path.unlink(missing_ok=True)
