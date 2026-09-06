"""backend/stats/duckdb_writers_proc.py — /proc-based DuckDB writer scan.

Answers the same question as the ``lsof`` path in
``backend/stats/duckdb_writers.py`` — "which processes hold an open file
descriptor on stats.duckdb?" — by reading ``/proc/<pid>/fd`` directly.

Why: ``lsof`` is not present on every Linux host (it is absent on NixOS
unless explicitly added to the environment), and the tile that consumes this
answer was therefore permanently unavailable there. ``/proc`` is part of the
kernel's interface on Linux, needs no dependency, and costs no subprocess on
a page that polls.

Rows come back in the same shape ``_parse_lsof_output`` produces — ``pid``,
``cmd``, ``age_seconds``, ``fd_mode`` — so the tile renders both sources
identically.

Three things this module reports that a naive scan would silently swallow:

* **Uninspectable pids.** ``/proc/<pid>/fd`` is readable only for processes
  owned by the same user. A scan run as a normal user cannot see root's
  processes at all. That is not an error, and it is not "no writers" either —
  it is an incomplete answer, and the count is returned so the caller can say
  so. Silently omitting them and reporting a confident empty list is the
  exact defect this module exists not to have.
* **PID recycling.** A pid listed in ``/proc`` and then resolved may be a
  different process by the time the fd is read. Each candidate's start time
  (field 22 of ``/proc/<pid>/stat``) is read before and after the fd scan; a
  row is only emitted when the two agree. A pid that fails that check is
  counted as uninspected rather than reported as a writer.
* **A cap, if one is applied.** ``max_pids`` bounds the scan; anything not
  reached is counted as uninspected and ``capped`` is set.

Linux only. ``/proc`` does not exist on macOS — callers check for the
directory and fall back to ``lsof`` there.
"""
from __future__ import annotations

import os
from typing import Any, NamedTuple

# /proc/<pid>/stat: "<pid> (<comm>) <state> <ppid> ...". comm can itself
# contain spaces and parentheses, so fields are split after the LAST ')'.
# starttime is overall field 22; after that split, index 22 - 3 = 19.
_STARTTIME_INDEX_AFTER_COMM = 19

# O_ACCMODE mask over /proc/<pid>/fdinfo/<fd>'s octal `flags:` value.
_ACCMODE_MASK = 0o3
_ACCMODE_NAMES = {0: "r", 1: "w", 2: "rw"}


class ProcScanResult(NamedTuple):
    """Outcome of one ``/proc`` sweep.

    rows              — writer rows, same shape as the lsof path produces.
    inspected_pids    — pids whose fd table was read in full.
    uninspected_pids  — pids that could NOT be inspected: not owned by this
                        user, exited mid-scan, failed the recycling check, or
                        skipped by ``max_pids``. A non-zero value means the
                        answer is partial, not that anything went wrong.
    capped            — True when ``max_pids`` cut the sweep short.
    """

    rows: list[dict[str, Any]]
    inspected_pids: int
    uninspected_pids: int
    capped: bool


def _read_text(path: str) -> str | None:
    """Read a small /proc file. Returns None for any OS-level failure.

    PermissionError (another user's process) and FileNotFoundError (a process
    that exited between the listing and the read) are both normal here, not
    error paths.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _start_ticks(proc_root: str, pid: str) -> str | None:
    """Process start time in clock ticks, as a string, for identity checks.

    Kept as the raw string: it is only ever compared for equality with itself,
    so parsing it would add a failure mode for no gain.
    """
    raw = _read_text(os.path.join(proc_root, pid, "stat"))
    if raw is None:
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 1:].split()
    if len(fields) <= _STARTTIME_INDEX_AFTER_COMM:
        return None
    return fields[_STARTTIME_INDEX_AFTER_COMM]


def _uptime_seconds(proc_root: str) -> float | None:
    raw = _read_text(os.path.join(proc_root, "uptime"))
    if raw is None:
        return None
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


def _age_seconds(start_ticks: str, uptime: float | None) -> float | None:
    """Process age derived from the start time already read for the identity
    check — one file read serves both, and it honours ``proc_root`` so a
    fixture tree gives a real age.
    """
    if uptime is None:
        return None
    try:
        hz = os.sysconf("SC_CLK_TCK")
        return max(uptime - (int(start_ticks) / hz), 0.0)
    except (ValueError, OSError, ZeroDivisionError):
        return None


def _command(proc_root: str, pid: str) -> str:
    """Command name, matching what lsof's `c` field reports."""
    raw = _read_text(os.path.join(proc_root, pid, "comm"))
    return raw.strip() if raw else ""


def _fd_mode(proc_root: str, pid: str, fd: str) -> str | None:
    """Access mode of one fd, from /proc/<pid>/fdinfo/<fd>'s octal flags.

    Only ever called for an fd already known to point at the target file, so
    this costs one extra read per *match*, not per fd scanned.

    Returns None when the mode cannot be determined — callers must render
    that as unknown rather than guessing "r".
    """
    raw = _read_text(os.path.join(proc_root, pid, "fdinfo", fd))
    if raw is None:
        return None
    for line in raw.splitlines():
        if not line.startswith("flags:"):
            continue
        try:
            flags = int(line.split(":", 1)[1].strip(), 8)
        except ValueError:
            return None
        return _ACCMODE_NAMES.get(flags & _ACCMODE_MASK)
    return None


def scan_proc_for_holders(
    target_path: str,
    proc_root: str = "/proc",
    max_pids: int | None = None,
) -> ProcScanResult:
    """Find processes holding an open fd on ``target_path``.

    ``proc_root`` is a parameter so tests can point at a fixture tree instead
    of the live ``/proc``, and so the no-/proc case can be exercised for real.

    Raises OSError when ``proc_root`` cannot be listed at all — that is
    "no answer from this source", and the caller falls back rather than
    reporting an empty list.
    """
    target = os.path.realpath(target_path)
    pids = sorted(
        (entry for entry in os.listdir(proc_root) if entry.isdigit()),
        key=int,
    )

    capped = False
    uninspected = 0
    if max_pids is not None and len(pids) > max_pids:
        uninspected += len(pids) - max_pids
        pids = pids[:max_pids]
        capped = True

    uptime = _uptime_seconds(proc_root)
    rows: list[dict[str, Any]] = []
    inspected = 0

    for pid in pids:
        start_before = _start_ticks(proc_root, pid)
        fd_dir = os.path.join(proc_root, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            # Another user's process, or one that exited mid-scan. Either way
            # this pid's answer is unknown, and saying so is the point.
            uninspected += 1
            continue
        inspected += 1

        match_fd = None
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) == target:
                    match_fd = fd
                    break  # one hit per process is enough — stop early
            except OSError:
                continue  # a single fd closed under us; the pid is still fine

        if match_fd is None:
            continue

        # Recycling check: same process at the end of the scan as at the start?
        start_after = _start_ticks(proc_root, pid)
        if start_before is None or start_after is None or start_before != start_after:
            inspected -= 1
            uninspected += 1
            continue

        rows.append({
            "pid": int(pid),
            "cmd": _command(proc_root, pid),
            "age_seconds": _age_seconds(start_before, uptime),
            "fd_mode": _fd_mode(proc_root, pid, match_fd),
        })

    return ProcScanResult(
        rows=rows,
        inspected_pids=inspected,
        uninspected_pids=uninspected,
        capped=capped,
    )
