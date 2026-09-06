"""Tests for backend/stats/duckdb_writers_proc.py (D#2326).

Every test here builds a fake /proc tree in tmp_path and passes it as
``proc_root``. Nothing touches the live /proc, so the assertions are about
the reader's logic rather than about whatever happens to be running on the
machine executing the suite.

The states under test are the ones the tile has to keep apart:
  - a holder is found                       → a row
  - nothing holds it, everything was read   → determined empty, 0 uninspected
  - a pid could not be read                 → counted, not silently dropped
  - a pid was recycled mid-scan             → NOT reported as a writer
  - proc_root does not exist                → OSError, so the caller falls back
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.stats.duckdb_writers_proc import (  # noqa: E402
    scan_proc_for_holders,
)

_HZ = os.sysconf("SC_CLK_TCK")


def _make_proc(root: Path, uptime: float = 1000.0) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "uptime").write_text(f"{uptime} 900.0\n")
    return root


def _make_pid(
    root: Path,
    pid: int,
    comm: str = "python3",
    start_ticks: int = 100,
    fds: dict[str, Path] | None = None,
    flags: str = "0100002",  # O_RDWR
) -> Path:
    """Write a minimal but realistically-shaped /proc/<pid> entry."""
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")

    # "<pid> (<comm>) <state> ..." — starttime is overall field 22.
    # fields is 0-indexed over the 1-indexed stat fields, so fields[21] is
    # field 22. pid, comm and state are written literally, so the tail
    # carries field 4 onwards.
    fields = ["0"] * 52
    fields[21] = str(start_ticks)
    tail = " ".join(fields[3:])
    (d / "stat").write_text(f"{pid} ({comm}) R {tail}\n")

    fd_dir = d / "fd"
    fd_dir.mkdir(exist_ok=True)
    fdinfo_dir = d / "fdinfo"
    fdinfo_dir.mkdir(exist_ok=True)
    for fd, target in (fds or {}).items():
        (fd_dir / fd).symlink_to(target)
        (fdinfo_dir / fd).write_text(f"pos:\t0\nflags:\t{flags}\nmnt_id:\t1\n")
    return d


# ---------------------------------------------------------------------------
# holder found
# ---------------------------------------------------------------------------


def test_finds_the_holder_and_its_mode(tmp_path):
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 4242, comm="duckdb-writer", fds={"7": db})

    result = scan_proc_for_holders(str(db), proc_root=str(proc))

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["pid"] == 4242
    assert row["cmd"] == "duckdb-writer"
    assert row["fd_mode"] == "rw"  # flags 0100002 → O_RDWR
    assert row["age_seconds"] == pytest.approx(1000.0 - 100 / _HZ)
    assert result.uninspected_pids == 0
    assert result.capped is False


@pytest.mark.parametrize(
    "flags,expected",
    [("0100000", "r"), ("0100001", "w"), ("0100002", "rw")],
)
def test_fd_mode_from_fdinfo_flags(tmp_path, flags, expected):
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 11, fds={"3": db}, flags=flags)

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert result.rows[0]["fd_mode"] == expected


def test_fd_mode_is_none_when_fdinfo_unreadable(tmp_path):
    """An unknown mode must come back as None, not be guessed at."""
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    d = _make_pid(proc, 12, fds={"3": db})
    (d / "fdinfo" / "3").unlink()

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert result.rows[0]["fd_mode"] is None


def test_other_open_files_are_not_reported(tmp_path):
    db = tmp_path / "stats.duckdb"
    db.touch()
    other = tmp_path / "something-else.log"
    other.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 21, fds={"3": other})

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert result.rows == []
    assert result.inspected_pids == 1
    assert result.uninspected_pids == 0


# ---------------------------------------------------------------------------
# determined empty vs partial — the distinction this module exists for
# ---------------------------------------------------------------------------


def test_determined_empty_reports_zero_uninspected(tmp_path):
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 31)
    _make_pid(proc, 32)

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert result.rows == []
    assert result.inspected_pids == 2
    assert result.uninspected_pids == 0


def test_unreadable_pid_is_counted_not_swallowed(tmp_path):
    """The partial case. A pid whose fd dir cannot be listed is counted.

    Reporting [] with uninspected_pids == 0 here would be a confident zero
    covering a process that was never looked at.
    """
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 41)
    blocked = _make_pid(proc, 42)
    os.chmod(blocked / "fd", 0o000)
    try:
        result = scan_proc_for_holders(str(db), proc_root=str(proc))
    finally:
        os.chmod(blocked / "fd", 0o755)

    assert result.rows == []
    assert result.inspected_pids == 1
    assert result.uninspected_pids == 1


def test_missing_fd_dir_counts_as_uninspected(tmp_path):
    """A process that exits between the listing and the fd read."""
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    d = _make_pid(proc, 51)
    (d / "fd").rmdir()

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert result.uninspected_pids == 1
    assert result.inspected_pids == 0


def test_max_pids_caps_and_counts_the_remainder(tmp_path):
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    for pid in (61, 62, 63, 64):
        _make_pid(proc, pid)

    result = scan_proc_for_holders(str(db), proc_root=str(proc), max_pids=2)
    assert result.capped is True
    assert result.inspected_pids == 2
    assert result.uninspected_pids == 2


# ---------------------------------------------------------------------------
# pid recycling
# ---------------------------------------------------------------------------


def test_recycled_pid_is_not_reported_as_a_writer(tmp_path, monkeypatch):
    """If the start time changes across the fd read, the pid was reused.

    The row is dropped and counted as uninspected rather than attributed to
    whatever process now owns that pid.
    """
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 71, fds={"3": db}, start_ticks=100)

    from backend.stats import duckdb_writers_proc as mod

    real = mod._start_ticks
    calls = {"n": 0}

    def flipping(proc_root, pid):
        calls["n"] += 1
        # Second read of this pid reports a different start time.
        return real(proc_root, pid) if calls["n"] == 1 else "999999"

    monkeypatch.setattr(mod, "_start_ticks", flipping)
    result = scan_proc_for_holders(str(db), proc_root=str(proc))

    assert result.rows == []
    assert result.uninspected_pids == 1
    assert result.inspected_pids == 0


def test_stable_start_time_still_yields_a_row(tmp_path):
    """Control for the test above — same setup, no recycling, row survives."""
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 72, fds={"3": db}, start_ticks=100)

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert [r["pid"] for r in result.rows] == [72]
    assert result.uninspected_pids == 0


# ---------------------------------------------------------------------------
# comm parsing robustness
# ---------------------------------------------------------------------------


def test_comm_containing_spaces_and_parens_does_not_shift_starttime(tmp_path):
    """/proc/<pid>/stat's comm field can contain ')' and ' '.

    A naive whitespace split misreads starttime for such a process, which
    would silently corrupt both the age and the recycling check.
    """
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    _make_pid(proc, 81, comm="odd (name) here", start_ticks=500, fds={"3": db})

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert len(result.rows) == 1
    assert result.rows[0]["age_seconds"] == pytest.approx(1000.0 - 500 / _HZ)


# ---------------------------------------------------------------------------
# no /proc at all
# ---------------------------------------------------------------------------


def test_missing_proc_root_raises_so_the_caller_can_fall_back(tmp_path):
    """macOS. The scan must not return an empty list it never established."""
    db = tmp_path / "stats.duckdb"
    db.touch()
    with pytest.raises(OSError):
        scan_proc_for_holders(str(db), proc_root=str(tmp_path / "no-such-proc"))


def test_non_numeric_proc_entries_are_ignored(tmp_path):
    db = tmp_path / "stats.duckdb"
    db.touch()
    proc = _make_proc(tmp_path / "proc")
    (proc / "sys").mkdir()
    (proc / "self").mkdir()
    _make_pid(proc, 91)

    result = scan_proc_for_holders(str(db), proc_root=str(proc))
    assert result.inspected_pids == 1
    assert result.uninspected_pids == 0
