"""
Tests for backend.jsonl_rotator — rotation triggers, no-op behavior,
archive pruning, and graceful handling of lock/permission errors.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.jsonl_rotator import (
    _archive_glob,
    _count_lines,
    _file_age_days,
    _file_size_mb,
    rotate_if_needed,
    main as rotator_main,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _write_file(path: Path, size_bytes: int = 0, lines: int = 0) -> None:
    """Create a file at path with given byte size or number of lines."""
    with open(path, "w") as f:
        if lines:
            for i in range(lines):
                f.write(f'{{"n":{i}}}\n')
        elif size_bytes:
            f.write("x" * size_bytes)
        else:
            pass  # empty file


# ── No-op tests ──────────────────────────────────────────────────────────────

def test_noop_all_thresholds_none(tmp_path: Path) -> None:
    """When all thresholds are None, no rotation happens even for a large file."""
    p = tmp_path / "test.jsonl"
    _write_file(p, size_bytes=200 * 1024 * 1024)  # 200 MB
    result = rotate_if_needed(str(p))
    assert result["rotated"] is False
    assert result["archive"] is None
    assert p.exists()


def test_noop_file_does_not_exist(tmp_path: Path) -> None:
    """Missing file → no-op, no error."""
    p = tmp_path / "missing.jsonl"
    result = rotate_if_needed(str(p), max_size_mb=1)
    assert result["rotated"] is False
    assert result["error"] is None


def test_noop_below_size_threshold(tmp_path: Path) -> None:
    """File below max_size_mb → not rotated."""
    p = tmp_path / "small.jsonl"
    _write_file(p, size_bytes=1024)  # 1 KB
    result = rotate_if_needed(str(p), max_size_mb=10)
    assert result["rotated"] is False
    assert p.exists()


def test_noop_below_line_threshold(tmp_path: Path) -> None:
    """File below max_lines → not rotated."""
    p = tmp_path / "few_lines.jsonl"
    _write_file(p, lines=50)
    result = rotate_if_needed(str(p), max_lines=100)
    assert result["rotated"] is False
    assert p.exists()


def test_noop_below_age_threshold(tmp_path: Path) -> None:
    """Freshly created file is below max_age_days → not rotated."""
    p = tmp_path / "fresh.jsonl"
    _write_file(p, lines=10)
    result = rotate_if_needed(str(p), max_age_days=7)
    assert result["rotated"] is False
    assert p.exists()


# ── Rotation trigger tests ────────────────────────────────────────────────────

def test_rotate_on_size(tmp_path: Path) -> None:
    """File exceeding max_size_mb triggers rotation."""
    p = tmp_path / "big.jsonl"
    _write_file(p, size_bytes=60 * 1024 * 1024)  # 60 MB
    result = rotate_if_needed(str(p), max_size_mb=50)
    assert result["rotated"] is True
    assert result["archive"] is not None
    assert os.path.exists(result["archive"])
    # Original recreated as empty
    assert p.exists()
    assert p.stat().st_size == 0


def test_rotate_on_lines(tmp_path: Path) -> None:
    """File exceeding max_lines triggers rotation."""
    p = tmp_path / "many.jsonl"
    _write_file(p, lines=200)
    result = rotate_if_needed(str(p), max_lines=100)
    assert result["rotated"] is True
    assert result["archive"] is not None
    assert os.path.exists(result["archive"])
    assert p.exists()
    assert p.stat().st_size == 0


def test_rotate_on_age(tmp_path: Path) -> None:
    """File older than max_age_days triggers rotation."""
    p = tmp_path / "old.jsonl"
    _write_file(p, lines=10)
    # Set mtime to 10 days ago
    old_mtime = time.time() - (10 * 86400)
    os.utime(str(p), (old_mtime, old_mtime))
    result = rotate_if_needed(str(p), max_age_days=7)
    assert result["rotated"] is True
    assert result["archive"] is not None
    assert p.exists()
    assert p.stat().st_size == 0


def test_rotate_first_threshold_wins(tmp_path: Path) -> None:
    """When multiple thresholds are set, exceeding any one triggers rotation."""
    p = tmp_path / "combo.jsonl"
    _write_file(p, size_bytes=60 * 1024 * 1024)  # 60 MB, but only 1 line
    # Only max_size_mb exceeded — max_lines and max_age_days are fine
    result = rotate_if_needed(str(p), max_size_mb=50, max_lines=1000, max_age_days=30)
    assert result["rotated"] is True


def test_rotate_archive_name_format(tmp_path: Path) -> None:
    """Archive filename must match <path>.<YYYY-MM-DD-HHMMSS> format."""
    import re
    p = tmp_path / "fmt.jsonl"
    _write_file(p, size_bytes=60 * 1024 * 1024)
    result = rotate_if_needed(str(p), max_size_mb=50)
    assert result["archive"] is not None
    suffix = result["archive"][len(str(p)):]
    assert re.match(r"\.\d{4}-\d{2}-\d{2}-\d{6}$", suffix), f"Bad suffix: {suffix!r}"


# ── Archive pruning tests ────────────────────────────────────────────────────

def test_prune_keeps_N_archives(tmp_path: Path) -> None:
    """After rotation, at most keep_archives archives are retained."""
    p = tmp_path / "rolling.jsonl"

    # Create 5 pre-existing archives (simulate past rotations)
    for i in range(5):
        arc = tmp_path / f"rolling.jsonl.2026-01-0{i+1}-120000"
        arc.write_text("")
        # Stagger mtimes so oldest-first order is deterministic
        old_mtime = time.time() - (100 - i)
        os.utime(str(arc), (old_mtime, old_mtime))

    _write_file(p, size_bytes=60 * 1024 * 1024)
    result = rotate_if_needed(str(p), max_size_mb=50, keep_archives=3)
    assert result["rotated"] is True
    # After rotation: 5 pre-existing + 1 new = 6, prune to keep 3 → pruned 3
    archives = _archive_glob(str(p))
    assert len(archives) <= 3
    assert result["pruned"] == 3


def test_prune_default_keep_5(tmp_path: Path) -> None:
    """Default keep_archives=5 is respected."""
    p = tmp_path / "default_keep.jsonl"

    # Create 7 archives
    for i in range(7):
        arc = tmp_path / f"default_keep.jsonl.2026-01-{i+1:02d}-120000"
        arc.write_text("")
        old_mtime = time.time() - (200 - i * 10)
        os.utime(str(arc), (old_mtime, old_mtime))

    _write_file(p, size_bytes=60 * 1024 * 1024)
    result = rotate_if_needed(str(p), max_size_mb=50)
    assert result["rotated"] is True
    archives = _archive_glob(str(p))
    assert len(archives) <= 5


# ── Error handling tests ─────────────────────────────────────────────────────

def test_permission_error_returns_gracefully(tmp_path: Path) -> None:
    """OSError during rename returns error dict, does not raise."""
    p = tmp_path / "locked.jsonl"
    _write_file(p, size_bytes=60 * 1024 * 1024)

    with mock.patch("os.rename", side_effect=OSError("Permission denied")):
        result = rotate_if_needed(str(p), max_size_mb=50)

    assert result["rotated"] is False
    assert result["error"] is not None
    assert "Permission denied" in result["error"]


def test_error_does_not_raise(tmp_path: Path) -> None:
    """Any OS error during rotation must never propagate as exception."""
    p = tmp_path / "err.jsonl"
    _write_file(p, size_bytes=60 * 1024 * 1024)

    with mock.patch("os.rename", side_effect=PermissionError("read-only")):
        try:
            result = rotate_if_needed(str(p), max_size_mb=50)
        except Exception as exc:
            pytest.fail(f"rotate_if_needed raised unexpectedly: {exc}")

    assert result["rotated"] is False


# ── CLI tests ────────────────────────────────────────────────────────────────

def test_cli_rotate_triggers(tmp_path: Path) -> None:
    """CLI 'rotate' command triggers rotation and prints JSON."""
    import io
    from contextlib import redirect_stdout

    p = tmp_path / "cli.jsonl"
    _write_file(p, size_bytes=60 * 1024 * 1024)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = rotator_main(["rotate", str(p), "--max-size-mb", "50"])

    assert rc == 0
    import json as _json
    out = _json.loads(buf.getvalue())
    assert out["rotated"] is True


def test_cli_noop(tmp_path: Path) -> None:
    """CLI 'rotate' returns rotated=false when threshold not exceeded."""
    import io
    from contextlib import redirect_stdout

    p = tmp_path / "small.jsonl"
    _write_file(p, size_bytes=1024)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = rotator_main(["rotate", str(p), "--max-size-mb", "50"])

    assert rc == 0
    import json as _json
    out = _json.loads(buf.getvalue())
    assert out["rotated"] is False


def test_cli_no_command_prints_help(capsys: pytest.CaptureFixture) -> None:
    """CLI with no subcommand returns non-zero exit code."""
    rc = rotator_main([])
    assert rc != 0
