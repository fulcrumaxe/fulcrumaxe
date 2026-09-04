"""backend/fleet/fleet_set.py — the one resolved fleet set (D#2317 PR-a).

Background
----------
There used to be two disjoint fleet-discovery mechanisms and no code that
ever unioned them:

  - ``backend.fleet.discovery.discover_projects()`` globs
    ``~/.*-state/project.json``. It never probes a port -- its ``ok`` field
    only ever meant "project.json parsed", and the Fleet Projects table
    (``fleet.projects``) rendered that boolean as a status.
  - ``backend.fleet.runtime.discover_running_projects()`` globs
    ``~/.*-state/dashboard-runtime.json`` and TCP-probes the ports it finds
    there. ``GET /api/fleet/projects`` read only this one.

On a real host these can (and did) return completely different projects: a
state dir with a ``project.json`` and no ``dashboard-runtime.json`` was
invisible to the second mechanism, and vice versa. ``resolve_fleet_set()``
is the union of both, deduplicated by the state directory's resolved
realpath (both source functions already realpath their own ``state_dir``,
so no second resolution step is needed here), with one *measured* status
per project.

Status values
-------------
``status`` replaces the old boolean ``ok`` with four values, all of them
either measured or an honest "nothing to measure":

  - ``"ok"``      -- a dashboard-runtime.json was found, it carried at
                     least one integer port, and every probed port
                     answered.
  - ``"down"``    -- same as above, but at least one probed port failed.
  - ``"unknown"`` -- nothing was probeable: either no dashboard-runtime.json
                     exists for this project (a project.json-only record),
                     or its ``ports`` carried no integer values.
  - ``"error"``   -- the file that would have carried the measurement
                     (dashboard-runtime.json when present, else
                     project.json) failed to parse or read.

``"ok"`` is never reachable without a probe: it is only assigned in the
branch that just called ``backend.fleet.runtime._probe_ports()`` and got
``True`` back. This module reuses that function rather than re-implementing
the TCP-connect -- see ``_status_from_ports()``.

Name resolution
----------------
Every discovered record already carries a self-reported ``name`` (read
from ``project.json``'s or ``dashboard-runtime.json``'s ``project_name``
field, or guessed from the state-dir basename as a last resort by the two
discovery modules themselves). This module does not re-derive that -- the
one exception is the single record whose state directory *is* this
backend's own ``STATE_DIR`` (i.e. the project this backend is actually
serving): for that record, and only that one, the fleet.db join key comes
from ``backend.fleet.project_name.resolve_project_name()`` -- the same
resolver ``scripts/pre-spawn-check.sh`` registers agents under (D#2314).
Any other discovered project's own self-reported name is the best signal
available, per the same reasoning already documented at
``backend.api._resolve_fleet_project_name``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Fields that may never leave a dashboard-facing response (D#2239): a
# filesystem path, a repo slug, port numbers, and process ids.
_FORBIDDEN_KEYS = ("state_dir", "repo", "ports", "pids")

# Fields safe to hand back to any dashboard-facing surface (RPC or REST).
_SAFE_KEYS = ("name", "status", "dashboard_port", "version", "language", "error", "agents_running")


def resolve_fleet_set() -> list[dict[str, Any]]:
    """Return one record per project discovered by either fleet mechanism.

    Union of ``discover_projects()`` (project.json) and
    ``discover_running_projects()`` (dashboard-runtime.json), deduplicated
    by the resolved realpath of the state directory -- a project visible to
    either or both mechanisms appears exactly once. Sorted by name for
    stable ordering.

    Every record carries ``state_dir`` (and, when known, ``repo``,
    ``ports``, ``pids``) for internal callers that need the join key --
    those four fields must be stripped by ``redact_for_dashboard()`` before
    the record reaches any host-wide, unauthenticated surface.
    """
    from backend.fleet.discovery import discover_projects  # noqa: PLC0415
    from backend.fleet.runtime import discover_running_projects  # noqa: PLC0415

    proj_by_dir = {r["state_dir"]: r for r in discover_projects()}
    runtime_by_dir = {r["state_dir"]: r for r in discover_running_projects()}

    all_dirs = set(proj_by_dir) | set(runtime_by_dir)

    records = [
        _merge_record(state_dir, proj_by_dir.get(state_dir), runtime_by_dir.get(state_dir))
        for state_dir in all_dirs
    ]
    records.sort(key=lambda r: r.get("name") or r.get("state_dir", ""))
    return records


def redact_for_dashboard(record: dict[str, Any]) -> dict[str, Any]:
    """Project a resolve_fleet_set() record down to fields safe to expose.

    Shared by both dashboard-facing surfaces that wrap resolve_fleet_set()
    -- the ``fleet.projects`` RPC handler and ``backend/api.py``'s
    ``GET /api/fleet/projects`` handler -- so the two can never drift apart
    (mirrors the D#2239 pattern already used for
    ``backend.fleet.runtime.redact_for_unauthenticated_response()``).

    Drops state_dir, repo, ports and pids -- an adopter's dashboard hitting
    either surface must never learn another project's filesystem path,
    repo slug, or port/process assignments.
    """
    return {k: record[k] for k in _SAFE_KEYS if k in record}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _merge_record(state_dir: str, proj: dict[str, Any] | None, runtime: dict[str, Any] | None) -> dict[str, Any]:
    """Combine one state directory's project.json record and/or
    dashboard-runtime.json record into a single resolved record.
    """
    name = (
        (runtime.get("name") if runtime else None)
        or (proj.get("name") if proj else None)
        or Path(state_dir).name
    )

    record: dict[str, Any] = {"name": name, "state_dir": state_dir}

    # The runtime record (dashboard-runtime.json) is the more complete
    # measurement source when it exists at all -- it is what carries ports
    # to probe. Prefer it for status; fall back to the project.json record
    # only when no runtime record exists.
    if runtime is not None:
        if not runtime.get("ok", True):
            record["status"] = "error"
            record["error"] = runtime.get("error", "unknown error reading dashboard-runtime.json")
        else:
            ports = runtime.get("ports") or {}
            record["status"] = _status_from_ports(ports)
            record["ports"] = ports
            record["pids"] = runtime.get("pids")
            record["started_at"] = runtime.get("started_at")
            record["last_seen"] = runtime.get("last_seen")
            record["dashboard_port"] = ports.get("vite")
        if runtime.get("repo"):
            record["repo"] = runtime["repo"]
    elif proj is not None:
        if not proj.get("ok", True):
            record["status"] = "error"
            record["error"] = proj.get("error", "unknown error reading project.json")
        else:
            # A project.json-only record has nothing to probe: no
            # dashboard-runtime.json exists for it, so status is "unknown",
            # never "ok" (D#2317 PR-a item 2).
            record["status"] = "unknown"
    else:  # pragma: no cover — resolve_fleet_set() never calls with both None
        record["status"] = "unknown"

    # Merge in project.json's own fields where the runtime side didn't
    # already supply something more authoritative.
    if proj is not None and proj.get("ok"):
        record.setdefault("repo", proj.get("repo"))
        if proj.get("dashboard_port") is not None:
            record["dashboard_port"] = proj["dashboard_port"]
        if proj.get("version") is not None:
            record["version"] = proj["version"]
        if proj.get("language") is not None:
            record["language"] = proj["language"]

    agents_running = _resolve_agents_running(name, record["status"], state_dir)
    if agents_running is not None:
        record["agents_running"] = agents_running

    return record


def _status_from_ports(ports: dict[str, Any]) -> str:
    """Return "ok" / "down" / "unknown" for *ports*, reusing
    ``backend.fleet.runtime._probe_ports()`` for the actual TCP-connect
    rather than re-implementing it.

    "unknown" is returned (and the prober is never called) when *ports*
    carries no integer value -- nothing is probeable. Once at least one
    integer port is present, ``_probe_ports()`` is called exactly once:
    its own semantics are "True only if every integer port answers", which
    -- now that "nothing probeable" has already been ruled out above -- can
    only mean "at least one probed port failed" on a False return. That is
    what keeps "down" (partial liveness) distinguishable from "unknown"
    (nothing to probe at all): D#2317 PR-a item 4.
    """
    if not any(isinstance(v, int) for v in ports.values()):
        return "unknown"

    from backend.fleet.runtime import _probe_ports  # noqa: PLC0415

    return "ok" if _probe_ports(ports) else "down"


def _resolve_agents_running(name: str, status: str, state_dir: str) -> int | None:
    """Return the live agent count for *name*, or None when it cannot be
    trusted.

    Returns None (never 0) when:
      - *status* is "error" -- the name came from a directory-name guess,
        not a file the discovery mechanism actually parsed.
      - *name* is empty.
      - this record is the project this backend is itself serving, and
        ``resolve_project_name()`` raises ``ProjectNameUnresolvable`` --
        never fall back to the record's own (possibly stale) self-reported
        name for the serving project, and never render a 0 in its place.
      - the fleet.db read itself raises for any reason.
    """
    if status == "error" or not name:
        return None

    resolved_name = name
    if _is_serving_project(state_dir):
        try:
            from backend.fleet.project_name import resolve_project_name  # noqa: PLC0415

            # Raises ProjectNameUnresolvable when this checkout's team
            # config can't be read or has no usable name (D#2314 Spec item
            # 2's loud-failure guarantee). Caught below along with every
            # other reason this lookup might fail, since the outcome is
            # identical either way: omit agents_running.
            resolved_name = resolve_project_name()
        except Exception:
            return None

    try:
        from backend.fleet.concurrency import count_project  # noqa: PLC0415

        return int(count_project(resolved_name))
    except Exception:
        return None


def _is_serving_project(state_dir: str) -> bool:
    """True when *state_dir* (a realpath) is this backend's own STATE_DIR.

    Never raises: ``backend.state_paths.STATE_DIR`` can raise under pytest
    when ``AUTONOMOUS_TEAM_STATE_DIR`` is unset, or on a relative value --
    either way, "can't tell" means "not the serving project", not a crash.
    """
    try:
        from backend.state_paths import STATE_DIR  # noqa: PLC0415

        return Path(state_dir).resolve() == Path(STATE_DIR).resolve()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    import json

    print(json.dumps(resolve_fleet_set(), indent=2))


if __name__ == "__main__":
    main()
