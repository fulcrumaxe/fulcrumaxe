"""backend/fleet/port_claim.py — scan-and-claim dashboard_port for a project.

Usage::

    from backend.fleet.port_claim import claim_port
    port = claim_port("projectb", "<home>/.projectb-state")

Algorithm:
1. Read existing project.json in the state dir — if dashboard_port is already set,
   return it unchanged (idempotent re-run, AC5).
2. Acquire an O_EXCL lockfile in the system temp dir to serialise concurrent
   coldstarts. This is deliberately NOT under ~/.autonomous-fleet-state/ (D#2216)
   — that directory is the persistent cross-project fleet registry
   (backend/rpc/fleet_discovery_ack.py's known.json, backend/fleet/concurrency.py's
   fleet.db), and should only ever be created by something writing to it for
   real. A lock file that is created and deleted again within the same
   claim_port() call is not that; using $HOME for it just left every fresh
   coldstart with an empty, junk-looking ~/.autonomous-fleet-state/ before
   anything had actually used the fleet registry.
3. Scan ~/.*-state/*/project.json for taken dashboard_port values.
4. Pick the first free port in range PORT_MIN..PORT_MAX (5100..5999).
5. Write the chosen port into <state_dir>/project.json.
6. Release the lock.

Edge cases:
- If the range is exhausted, raise RuntimeError with a clear message.
- If project.json is malformed JSON, skip that file (don't crash the scan).
- If locking fails after retries, raise RuntimeError.

CLI entrypoint::

    python3 -m backend.fleet.port_claim <project_name> <state_dir>
"""

from __future__ import annotations

import errno
import fcntl
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PORT_MIN = 5100
PORT_MAX = 5999

# System temp dir, not ~/.autonomous-fleet-state/ -- see module docstring
# point 2 (D#2216). tempfile.gettempdir() always exists, so no directory
# needs to be created just to take this lock.
_LOCK_PATH = Path(tempfile.gettempdir()) / "autonomous-fleet-coldstart.lock"
_LOCK_TIMEOUT_S = 30
_LOCK_RETRY_INTERVAL_S = 0.25


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def claim_port(project_name: str, state_dir: str | Path) -> int:
    """Return the dashboard_port for this project, claiming one if needed.

    Parameters
    ----------
    project_name:
        Logical project name (used for logging only).
    state_dir:
        The project's state directory (~/.<project>-state/).

    Returns
    -------
    int
        The port number in PORT_MIN..PORT_MAX (inclusive).

    Raises
    ------
    RuntimeError
        If the port range is exhausted or the lock cannot be acquired.
    """
    state_dir = Path(state_dir)
    project_json_path = state_dir / "project.json"

    # --- Idempotency: return existing port without taking the lock ----------
    existing_port = _read_existing_port(project_json_path)
    if existing_port is not None:
        return existing_port

    # --- Take the O_EXCL lock -----------------------------------------------
    # _LOCK_PATH.parent is the system temp dir by default (already exists);
    # this mkdir only does real work for a test-monkeypatched _LOCK_PATH.
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    lock_fd = _acquire_lock()
    try:
        # Re-check after lock in case another coldstart just wrote our port
        existing_port = _read_existing_port(project_json_path)
        if existing_port is not None:
            return existing_port

        # --- Scan taken ports -----------------------------------------------
        taken = _scan_taken_ports()

        # --- Pick first free port -------------------------------------------
        port = _pick_free_port(taken)

        # --- Persist to project.json ----------------------------------------
        _write_port(project_json_path, port, project_name)

    finally:
        _release_lock(lock_fd)

    return port


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_existing_port(project_json_path: Path) -> int | None:
    """Return the dashboard_port already in project.json, or None."""
    if not project_json_path.exists():
        return None
    try:
        data = json.loads(project_json_path.read_text())
        port = data.get("dashboard_port")
        if isinstance(port, int) and PORT_MIN <= port <= PORT_MAX:
            return port
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _scan_taken_ports() -> set[int]:
    """Glob ~/.*-state/*/project.json and collect all taken dashboard_port values."""
    taken: set[int] = set()
    pattern = str(Path.home() / ".*-state" / "project.json")
    for path_str in glob.glob(pattern):
        try:
            data = json.loads(Path(path_str).read_text())
            port = data.get("dashboard_port")
            if isinstance(port, int) and PORT_MIN <= port <= PORT_MAX:
                taken.add(port)
        except (json.JSONDecodeError, OSError):
            # Corrupted project.json — skip, don't crash the scan
            pass
    return taken


def _pick_free_port(taken: set[int]) -> int:
    """Return the first free port in PORT_MIN..PORT_MAX not in *taken*."""
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port not in taken:
            return port
    raise RuntimeError(
        f"All dashboard ports {PORT_MIN}..{PORT_MAX} are taken by existing projects. "
        "Remove stale state directories or widen the port range."
    )


def _write_port(project_json_path: Path, port: int, project_name: str = "") -> None:
    """Write dashboard_port into project.json (merge with existing content).

    Ensures sentinel fields (project_name, version) are present so that
    fleet.discovery does not reject the file as an unknown format.
    """
    data: dict = {}
    if project_json_path.exists():
        try:
            data = json.loads(project_json_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    data["dashboard_port"] = port
    # Add sentinel fields if missing so fleet.discovery accepts this project.
    if "version" not in data and "project_name" not in data:
        name = project_name or project_json_path.parent.name.lstrip(".").removesuffix("-state")
        data.setdefault("project_name", name)
        data.setdefault("version", 1)
    project_json_path.write_text(json.dumps(data, indent=2) + "\n")


def _acquire_lock() -> int:
    """Open the O_EXCL lock file and return its fd.

    Retries for up to _LOCK_TIMEOUT_S seconds, then raises RuntimeError.
    Creates parent directory if needed.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            return fd
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            # Lock file exists — check if it's stale (>60s old)
            try:
                age = time.time() - _LOCK_PATH.stat().st_mtime
                if age > 60:
                    _LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Could not acquire fleet lock at {_LOCK_PATH} after "
                    f"{_LOCK_TIMEOUT_S}s. Another coldstart may be running."
                ) from exc
            time.sleep(_LOCK_RETRY_INTERVAL_S)


def _release_lock(fd: int) -> None:
    """Close and remove the lock file."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: python3 -m backend.fleet.port_claim <project_name> <state_dir>", file=sys.stderr)
        sys.exit(1)
    project_name = sys.argv[1]
    state_dir = sys.argv[2]
    try:
        port = claim_port(project_name, state_dir)
        print(port)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
