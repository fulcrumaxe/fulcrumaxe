"""backend/stats/duckdb_writers.py — DuckDB writer audit reader.

Fleet audit (D#944 PR1): path is read from state_paths.STATS_DB which reads
AUTONOMOUS_TEAM_STATE_DIR at import time. No hardcoded paths — per-project
dashboards just export a different AUTONOMOUS_TEAM_STATE_DIR before starting
the backend. No code changes required.

Returns a list of processes currently holding an open file descriptor on
stats.duckdb, so the dashboard can surface lock-holder visibility without
requiring the operator to run lsof manually.

Two sources answer the same question, tried in order (D#2326):

  1. /proc/<pid>/fd, via backend.stats.duckdb_writers_proc — preferred on
     Linux. No dependency, no subprocess per poll, and it reports how many
     pids it could not inspect so a partial answer is visible as partial.
  2. lsof -F pcfan -- <abs_path>, parsed line-by-line — the fallback where
     /proc does not exist, which is the macOS case.
  3. Neither available → ([], warning) naming both missing sources.

lsof is deliberately kept: the two platforms are disjoint, and removing it
would break the tile on macOS to fix it on Linux.

Note that lsof shares /proc's permission blind spot — run as a normal user
it cannot see another user's processes either — but its output gives no way
to count what it missed, so the lsof path reports uninspected_pids as None
("not reported") rather than a misleading 0.

Age is derived from /proc/<pid>/stat start-time on Linux; on non-Linux
systems the field is omitted (returned as None).

lsof 4.95.0 format (verified on Ubuntu 24.04):
  Special fds (cwd, txt, mem, rtd): f<type>\na \nn<path>
  Real numeric fds:                 f<num>\na<mode>\nn<path>
  where <mode> is 'r', 'w', or 'u' (read+write).
The access mode lives in the separate 'a' field, not appended to 'f'.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.stats.duckdb_writers_proc import scan_proc_for_holders

log = logging.getLogger(__name__)


def _stats_db_path() -> Path:
    """Absolute path to stats.duckdb from state_paths (single source of truth)."""
    from backend.state_paths import STATS_DB  # noqa: PLC0415
    return STATS_DB


def _process_age_seconds(pid: int) -> float | None:
    """Best-effort process age from /proc/<pid>/stat on Linux.

    Returns None when unavailable (non-Linux, permission error, etc.).
    """
    proc_stat = Path(f"/proc/{pid}/stat")
    if not proc_stat.exists():
        return None
    try:
        fields = proc_stat.read_text().split()
        # Field 22 (index 21) is starttime in clock ticks since boot.
        hz = os.sysconf("SC_CLK_TCK")
        boot_time_path = Path("/proc/uptime")
        uptime_secs = float(boot_time_path.read_text().split()[0])
        start_ticks = int(fields[21])
        start_secs_since_boot = start_ticks / hz
        age = uptime_secs - start_secs_since_boot
        return max(age, 0.0)
    except Exception:
        return None


def _parse_lsof_output(output: str) -> list[dict[str, Any]]:
    """Parse lsof -F pcfan output into a list of writer dicts.

    lsof -F pcfan emits lines prefixed:
      p<pid>
      c<cmd>
      f<fd_or_type>   — numeric fd (e.g. '3') or type name ('mem', 'txt', 'cwd', 'rtd')
      a<access_mode>  — 'r', 'w', 'u', or ' ' (space) for non-data fds
      n<name>         — file path or description

    The access mode is in the 'a' field, NOT appended to 'f'.
    Mapping: u → 'rw', r → 'r', w → 'w', anything else (space, empty) → ''

    Multiple (f, a, n) tuples can appear per process block.
    We emit one row per (fd, access, path) triple.
    """
    rows: list[dict[str, Any]] = []
    current_pid: int | None = None
    current_cmd: str = ""
    current_fd: str = ""
    current_access: str = ""

    for line in output.splitlines():
        if not line:
            continue
        key, val = line[0], line[1:]
        if key == "p":
            current_pid = int(val) if val.isdigit() else None
            current_cmd = ""
            current_fd = ""
            current_access = ""
        elif key == "c":
            current_cmd = val
        elif key == "f":
            current_fd = val
            current_access = ""  # reset access for new fd
        elif key == "a":
            # val is the access char: 'r', 'w', 'u', or ' ' for non-data fds
            current_access = val.strip()
        elif key == "n" and current_pid is not None:
            # Translate lsof access char to fd_mode
            if current_access == "u":
                fd_mode = "rw"
            elif current_access == "r":
                fd_mode = "r"
            elif current_access == "w":
                fd_mode = "w"
            else:
                # Non-data fd (mem, txt, cwd, rtd, etc.) — preserve type name
                fd_mode = current_fd if not current_fd.isdigit() else ""

            age = _process_age_seconds(current_pid)
            rows.append({
                "pid": current_pid,
                "cmd": current_cmd,
                "age_seconds": age,
                "fd_mode": fd_mode,
            })

    return rows


def _meta(
    source: str | None,
    inspected: int | None = None,
    uninspected: int | None = None,
    capped: bool = False,
) -> dict[str, Any]:
    """Provenance for one answer: which source produced it, and how complete.

    ``uninspected`` is None — "not reported" — rather than 0 whenever the
    source cannot count what it missed. 0 is a claim of completeness and must
    only be made by a source that can back it.
    """
    return {
        "source": source,
        "inspected_pids": inspected,
        "uninspected_pids": uninspected,
        "capped": capped,
    }


def _lsof_writers(abs_path: str) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Fallback source: shell out to lsof. Unchanged parsing (D#2326)."""
    try:
        result = subprocess.run(
            ["lsof", "-F", "pcfan", "--", abs_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # lsof exits 1 when no processes hold the file — that's fine.
        return _parse_lsof_output(result.stdout), None, _meta("lsof")

    except FileNotFoundError:
        # lsof not on PATH — non-fatal, return empty
        return [], "lsof not found on PATH", _meta(None)
    except subprocess.TimeoutExpired:
        log.warning("duckdb_writers: lsof timed out")
        return [], "lsof timed out", _meta(None)
    except Exception as exc:
        log.warning("duckdb_writers: unexpected error: %s", exc)
        return [], str(exc), _meta(None)


def get_duckdb_writers(
    proc_root: str = "/proc",
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Return (rows, warning, meta).

    rows    — list of dicts with pid/cmd/age_seconds/fd_mode.
    warning — non-None exactly when the answer could NOT be determined, i.e.
              no source was available. A partial answer is not a warning: it
              is a determined answer plus a count of what it could not see,
              carried in meta["uninspected_pids"].
    meta    — {"source", "inspected_pids", "uninspected_pids", "capped"}.

    ``proc_root`` is a parameter so the no-/proc platform (macOS) can be
    exercised for real rather than mocked.
    """
    db_path = _stats_db_path()
    if not db_path.exists():
        # No database, so nothing can hold it open. This is a determined
        # answer about the file, not a failure to look.
        return [], None, _meta(None)

    abs_path = str(db_path.resolve())

    if os.path.isdir(proc_root):
        try:
            scan = scan_proc_for_holders(abs_path, proc_root=proc_root)
        except OSError as exc:
            # /proc exists but could not be listed — fall through to lsof
            # rather than reporting an empty list we never established.
            log.warning("duckdb_writers: /proc scan failed (%s); trying lsof", exc)
        else:
            return scan.rows, None, _meta(
                "proc",
                inspected=scan.inspected_pids,
                uninspected=scan.uninspected_pids,
                capped=scan.capped,
            )

    rows, warning, meta = _lsof_writers(abs_path)
    if warning == "lsof not found on PATH" and not os.path.isdir(proc_root):
        warning = f"no source available: {proc_root} does not exist and lsof is not on PATH"
    return rows, warning, meta
