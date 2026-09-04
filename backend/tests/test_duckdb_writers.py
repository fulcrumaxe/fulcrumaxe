"""Tests for backend/stats/duckdb_writers.py.

Covers:
  - _parse_lsof_output: real lsof -F pcfan format (separate 'a' field)
  - get_duckdb_writers: FileNotFoundError → ([], warning)
  - get_duckdb_writers: TimeoutExpired → ([], warning)
  - _process_age_seconds: mocked /proc/<pid>/stat read → float age

Real lsof 4.95.0 format verified on Ubuntu 24.04:
  Numeric fds emit f<num>, then a<mode>, then n<path>
  Special fds (mem, txt, cwd, rtd) emit f<type>, then 'a ' (space), then n<path>
  Access chars: r=read, w=write, u=read+write
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from testsupport.fixture_paths import FIXTURE_HOME  # noqa: E402

from backend.stats.duckdb_writers import (
    _parse_lsof_output,
    _process_age_seconds,
    get_duckdb_writers,
)


# ---------------------------------------------------------------------------
# _parse_lsof_output — real lsof -F pcfan format
#
# Canned lsof -F pcfan output with real format (verified on lsof 4.95.0):
#   PID 1001 (python3): fd 3 with mode u (read+write) → fd_mode='rw'
#   PID 1002 (python3): fd 5 with mode r (read-only) → fd_mode='r'
#                       fd mem (memory-mapped, a=' ') → fd_mode='mem'
# ---------------------------------------------------------------------------

_CANNED_LSOF = f"""\
p1001
cpython3
fcwd
a
n{FIXTURE_HOME}
frtd
a
n/
ftxt
a
n/usr/bin/python3.12
fmem
a
n/usr/lib/x86_64-linux-gnu/libc.so.6
f3
au
n{FIXTURE_HOME}/.fulcrumaxe-state/stats.duckdb
p1002
cpython3
fcwd
a
n{FIXTURE_HOME}
fmem
a
n{FIXTURE_HOME}/.fulcrumaxe-state/stats.duckdb
f5
ar
n{FIXTURE_HOME}/.fulcrumaxe-state/stats.duckdb
"""


def test_parse_lsof_output_rw_fd():
    """fd with access='u' → fd_mode='rw' (DuckDB writer pattern)."""
    rows = _parse_lsof_output(_CANNED_LSOF)
    rw_rows = [r for r in rows if r["fd_mode"] == "rw"]
    assert len(rw_rows) == 1
    row = rw_rows[0]
    assert row["pid"] == 1001
    assert row["cmd"] == "python3"
    assert row["fd_mode"] == "rw"


def test_parse_lsof_output_reader_row():
    """fd with access='r' → fd_mode='r'."""
    rows = _parse_lsof_output(_CANNED_LSOF)
    reader_rows = [r for r in rows if r["fd_mode"] == "r"]
    assert len(reader_rows) == 1
    row = reader_rows[0]
    assert row["pid"] == 1002
    assert row["cmd"] == "python3"
    assert row["fd_mode"] == "r"


def test_parse_lsof_output_mem_row():
    """Special fd 'mem' (access=' ') → fd_mode='mem'."""
    rows = _parse_lsof_output(_CANNED_LSOF)
    mem_rows = [r for r in rows if r["fd_mode"] == "mem"]
    assert len(mem_rows) >= 1
    assert any(r["pid"] == 1002 for r in mem_rows)


def test_parse_lsof_output_total_real_fd_rows():
    """Should have exactly one 'rw' and one 'r' real fd row."""
    rows = _parse_lsof_output(_CANNED_LSOF)
    real_mode_rows = [r for r in rows if r["fd_mode"] in ("r", "w", "rw")]
    assert len(real_mode_rows) == 2


def test_parse_lsof_output_empty_string():
    rows = _parse_lsof_output("")
    assert rows == []


def test_parse_lsof_output_write_only_fd():
    """fd with access='w' → fd_mode='w'."""
    lsof = "p999\ncfoo\nf7\naw\nn/some/path\n"
    rows = _parse_lsof_output(lsof)
    assert len(rows) == 1
    assert rows[0]["fd_mode"] == "w"
    assert rows[0]["pid"] == 999
    assert rows[0]["cmd"] == "foo"


def test_parse_lsof_output_numeric_fd_no_access_char():
    """Numeric fd with blank access char → fd_mode='' (not a named type)."""
    lsof = "p111\nctest\nf4\na \nn/some/file\n"
    rows = _parse_lsof_output(lsof)
    assert len(rows) == 1
    assert rows[0]["fd_mode"] == ""


def test_parse_lsof_output_special_fd_type_preserved():
    """Non-numeric fd type name preserved in fd_mode for non-data fds."""
    lsof = "p222\nctest\nftxt\na \nn/usr/bin/python3\n"
    rows = _parse_lsof_output(lsof)
    assert len(rows) == 1
    assert rows[0]["fd_mode"] == "txt"


# ---------------------------------------------------------------------------
# get_duckdb_writers — error paths
# ---------------------------------------------------------------------------


def test_get_duckdb_writers_lsof_not_found(tmp_path, monkeypatch):
    """FileNotFoundError from lsof → empty rows + warning text."""
    fake_db = tmp_path / "stats.duckdb"
    fake_db.touch()

    monkeypatch.setattr(
        "backend.stats.duckdb_writers._stats_db_path",
        lambda: fake_db,
    )

    with patch("subprocess.run", side_effect=FileNotFoundError("lsof not found")):
        rows, warning = get_duckdb_writers()

    assert rows == []
    assert warning is not None
    assert "lsof" in warning.lower()


def test_get_duckdb_writers_timeout(tmp_path, monkeypatch):
    """TimeoutExpired → empty rows + non-None warning."""
    fake_db = tmp_path / "stats.duckdb"
    fake_db.touch()

    monkeypatch.setattr(
        "backend.stats.duckdb_writers._stats_db_path",
        lambda: fake_db,
    )

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["lsof"], timeout=5),
    ):
        rows, warning = get_duckdb_writers()

    assert rows == []
    assert warning is not None
    assert len(warning) > 0


def test_get_duckdb_writers_db_missing(tmp_path, monkeypatch):
    """If stats.duckdb does not exist, return ([], None) immediately."""
    missing_db = tmp_path / "nonexistent.duckdb"

    monkeypatch.setattr(
        "backend.stats.duckdb_writers._stats_db_path",
        lambda: missing_db,
    )

    rows, warning = get_duckdb_writers()
    assert rows == []
    assert warning is None


def test_get_duckdb_writers_writer_fd_mode_populated(tmp_path, monkeypatch):
    """Writer FD (a=u or a=w) must produce fd_mode in {'w', 'rw'}, not ''.

    This is the acceptance gate: lsof 4.95.0 emits access in a separate 'a'
    field, not appended to the 'f' field. If this test fails, check that
    _parse_lsof_output reads the 'a' key, not the suffix of 'f'.
    """
    fake_db = tmp_path / "stats.duckdb"
    fake_db.touch()

    monkeypatch.setattr(
        "backend.stats.duckdb_writers._stats_db_path",
        lambda: fake_db,
    )

    # Simulate lsof -F pcfan output for a process with a write fd on the db
    # This is the exact format lsof 4.95.0 emits on Ubuntu 24.04.
    fake_lsof_output = (
        "p77568\n"
        "cpython3\n"
        "f3\n"
        "au\n"
        f"n{fake_db}\n"
    )

    mock_result = MagicMock()
    mock_result.stdout = fake_lsof_output

    with patch("subprocess.run", return_value=mock_result):
        rows, warning = get_duckdb_writers()

    assert warning is None
    assert len(rows) == 1
    row = rows[0]
    assert row["pid"] == 77568
    assert row["cmd"] == "python3"
    assert row["fd_mode"] in ("w", "rw"), (
        f"Expected fd_mode in ('w', 'rw'), got {row['fd_mode']!r}. "
        "lsof 4.95.0 uses separate 'a' field — check _parse_lsof_output reads key 'a'."
    )


# ---------------------------------------------------------------------------
# _process_age_seconds
# ---------------------------------------------------------------------------


def test_process_age_seconds_happy_path(tmp_path, monkeypatch):
    """Mocked /proc/<pid>/stat returns a plausible age."""
    pid = 12345
    hz = 100
    start_ticks = 100   # 100 ticks / 100 Hz = 1 second since boot
    uptime_secs = 200.0

    fields = ["0"] * 52
    fields[21] = str(start_ticks)
    stat_content = " ".join(fields)

    original_exists = Path.exists

    def fake_exists(self):
        if str(self) == f"/proc/{pid}/stat":
            return True
        return original_exists(self)

    def fake_read_text(self, *args, **kwargs):
        if str(self) == f"/proc/{pid}/stat":
            return stat_content
        if str(self) == "/proc/uptime":
            return f"{uptime_secs} 1234.5"
        return original_read_text(self, *args, **kwargs)

    original_read_text = Path.read_text

    with patch.object(Path, "exists", fake_exists), \
         patch.object(Path, "read_text", fake_read_text), \
         patch("os.sysconf", return_value=hz):
        age = _process_age_seconds(pid)

    expected_age = uptime_secs - (start_ticks / hz)
    assert age is not None
    assert abs(age - expected_age) < 0.01


def test_process_age_seconds_missing_proc(monkeypatch):
    """If /proc/<pid>/stat does not exist, return None."""
    pid = 99999

    with patch.object(Path, "exists", return_value=False):
        age = _process_age_seconds(pid)

    assert age is None
