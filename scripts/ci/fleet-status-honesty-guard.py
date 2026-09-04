#!/usr/bin/env python3
"""fleet-status-honesty-guard.py — behavioral guard for the Fleet page's
resolved project set and measured status (D#2317 PR-a).

Background
----------
Before this fix there were two disjoint fleet-discovery mechanisms
(``backend.fleet.discovery.discover_projects()`` globbing
``~/.*-state/project.json``, and ``backend.fleet.runtime.discover_running_projects()``
globbing ``~/.*-state/dashboard-runtime.json``) and nothing that unioned
them: on the operator's own host, the Fleet Projects table (fed by the
first mechanism) and ``GET /api/fleet/projects`` (fed by the second)
disagreed about which projects existed, and the serving project itself
(which had a runtime file but no project.json) was invisible to the table.
Every row that WAS visible read a boolean ``ok`` that only ever meant
"project.json parsed" — never "this project is actually up". All 7
surviving projects on the operator's host read ``ok`` with nothing
listening on any advertised port.

``backend.fleet.fleet_set.resolve_fleet_set()`` is the fix: one resolved
union of both mechanisms, with a four-value ``status`` (``ok`` / ``down`` /
``unknown`` / ``error``) that is either measured or an honest "nothing to
measure" — never asserted.

This guard is not a lint over source text (except for one specific,
narrow check: that the one-resolver constraint holds by reading
fleet_set.py's own source — see ``check_one_resolver_only()``, which isn't
independently observable through behavior alone). Every other check here
builds a real fixture ``HOME``, binds a real listening socket, and calls
the real ``resolve_fleet_set()`` — no mocking of the TCP-connect prober.
``backend.fleet.concurrency.count_project`` (the agents_running lookup) IS
patched to a fixed value in every fixture cycle below, for the same reason
project-liveness-guard.py isolates ``AUTONOMOUS_FLEET_STATE_DIR``: without
it, resolving agents_running would open (and, on a cold host, create)
the operator's real ``~/.autonomous-fleet-state/fleet.db`` as a side effect
of a CI guard that has nothing to do with fleet.db.

Per D#2317 PR-a item 12: stdlib-only, no network beyond loopback, no
GH_TOKEN, and — critically — never touches the operator's actual
``$HOME``. ``HOME`` is overridden to a throwaway directory for the
duration of each fixture cycle and always restored, even on failure.

Run from the repo root:

    python3 scripts/ci/fleet-status-honesty-guard.py

Exit 0: every check passes.
Exit 1: a check failed — prints one `FAIL <detail>` line per failure.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

FAILURES: list[str] = []


def _fail(detail: str) -> None:
    FAILURES.append(detail)
    print(f"FAIL {detail}")


def _write_project_json(state_dir: Path, name: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "project.json").write_text(json.dumps({
        "project_name": name,
        "version": 1,
        "state_dir": str(state_dir),
    }))


def _write_runtime_json(state_dir: Path, name: str, ports: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "dashboard-runtime.json").write_text(json.dumps({
        "project_name": name,
        "ports": ports,
        "started_at": "2026-05-18T16:00:00Z",
    }))


def _open_listening_socket() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


@contextmanager
def _fixture_home(fake_home: Path):
    """Point $HOME at *fake_home* for the duration of the block, cold the
    two discovery caches on entry, restore $HOME (even on error) on exit,
    and never let agents_running resolution touch the real fleet.db.
    """
    from backend.fleet import discovery as fleet_discovery
    from backend.fleet import runtime as fleet_runtime

    real_home = os.environ.get("HOME")
    os.environ["HOME"] = str(fake_home)
    fleet_discovery.invalidate_cache()
    fleet_runtime.invalidate_cache()
    try:
        with patch("backend.fleet.concurrency.count_project", return_value=0):
            yield
    finally:
        if real_home is not None:
            os.environ["HOME"] = real_home
        else:
            os.environ.pop("HOME", None)
        fleet_discovery.invalidate_cache()
        fleet_runtime.invalidate_cache()


def check_resolved_set_and_status(fake_home: Path) -> None:
    """Spec items 1-5, all against one fixture HOME.

    Fixture shape:
      .alpha-state/    project.json only               -> "unknown"
      .beta-state/     dashboard-runtime.json only      -> visible by its own name (item 5)
      .gamma-state/    both project.json AND runtime    -> appears exactly once (dedup, item 1/6)
      .partial-state/  runtime, one live port, one dead -> "down" (item 4)
      .probed-state/   runtime, all ports live          -> "ok" (item 2)
    """
    from backend.fleet import fleet_set

    alpha = fake_home / ".alpha-state"
    beta = fake_home / ".beta-state"
    gamma = fake_home / ".gamma-state"
    partial = fake_home / ".partial-state"
    probed = fake_home / ".probed-state"

    _write_project_json(alpha, "alpha")
    _write_runtime_json(beta, "beta-runtime-only", {"vite": 19921})
    _write_project_json(gamma, "gamma")
    _write_runtime_json(gamma, "gamma", {"vite": 19922})

    live_srv, live_port = _open_listening_socket()
    ok_srv, ok_port = _open_listening_socket()
    try:
        _write_runtime_json(partial, "partial-liveness", {"vite": live_port, "api": 19923})
        _write_runtime_json(probed, "fully-probed-ok", {"vite": ok_port})

        with _fixture_home(fake_home):
            records = fleet_set.resolve_fleet_set()
    finally:
        live_srv.close()
        ok_srv.close()

    by_name = {r["name"]: r for r in records}

    # Item 1: one record per state dir, five distinct projects.
    if len(records) != 5:
        _fail(f"resolved-set-count: expected 5 records, got {len(records)}: {sorted(by_name)}")

    # Item 2: project.json-only is "unknown", never "ok".
    alpha_status = by_name.get("alpha", {}).get("status")
    if alpha_status != "unknown":
        _fail(f"project-json-only-status: expected 'unknown' for alpha, got {alpha_status!r}")

    # Item 5: a project visible only to the runtime mechanism appears, named
    # from its own fixture data (not a literal).
    if "beta-runtime-only" not in by_name:
        _fail("runtime-only-visibility: 'beta-runtime-only' missing from resolved set")

    # Item 1/6 dedup: gamma (both sources) appears exactly once.
    gamma_records = [r for r in records if r["name"] == "gamma"]
    if len(gamma_records) != 1:
        _fail(f"dedup: expected exactly one 'gamma' record, got {len(gamma_records)}")

    # Item 4: partial liveness reads "down", distinguishable from "unknown".
    partial_status = by_name.get("partial-liveness", {}).get("status")
    if partial_status != "down":
        _fail(f"partial-liveness-status: expected 'down', got {partial_status!r}")
    if partial_status == "unknown":
        _fail("partial-liveness-status: 'down' must never collapse into 'unknown'")

    # Item 2: a fully-live project reads "ok".
    probed_status = by_name.get("fully-probed-ok", {}).get("status")
    if probed_status != "ok":
        _fail(f"fully-probed-status: expected 'ok' with a real live socket, got {probed_status!r}")


def check_probe_recorded_for_ok_status(fake_home: Path) -> None:
    """Spec item 3: with the port-prober patched to record its calls, for
    every returned record where status == "ok" the prober was invoked with
    a non-empty integer port set and returned True; the recorded call list
    is non-empty.
    """
    from backend.fleet import fleet_set, runtime as fleet_runtime

    live_srv, live_port = _open_listening_socket()
    try:
        ok_dir = fake_home / ".recorded-ok-state"
        _write_runtime_json(ok_dir, "recorded-ok", {"vite": live_port})

        calls: list[dict] = []
        real_probe_ports = fleet_runtime._probe_ports

        def _recording_probe(ports, timeout_s=1.0):
            calls.append(dict(ports))
            return real_probe_ports(ports, timeout_s=timeout_s)

        with _fixture_home(fake_home), patch(
            "backend.fleet.runtime._probe_ports", side_effect=_recording_probe
        ):
            records = fleet_set.resolve_fleet_set()
    finally:
        live_srv.close()

    ok_records = [r for r in records if r.get("name") == "recorded-ok"]
    if not ok_records or ok_records[0].get("status") != "ok":
        _fail(f"probe-recorded: expected a 'recorded-ok' record with status 'ok', got {ok_records!r}")
        return

    if not calls:
        _fail("probe-recorded: expected the prober to have been called at least once, call list was empty")
        return

    matching = [c for c in calls if c == {"vite": live_port}]
    if not matching:
        _fail(f"probe-recorded: expected a call with ports={{'vite': {live_port}}}, got {calls!r}")
    elif not any(isinstance(v, int) for v in matching[0].values()):
        _fail(f"probe-recorded: expected a non-empty integer port set, got {matching[0]!r}")


def check_one_resolver_only() -> None:
    """D#2317 PR-a item 8: the module must consume
    backend.fleet.project_name.resolve_project_name rather than re-deriving
    a second name resolver -- the one check in this file that reads source
    text rather than running code, because "no second resolver exists"
    isn't independently observable through resolve_fleet_set()'s behavior
    alone (a second resolver that happens to agree today would pass every
    behavioral check and still be the exact defect D#2314 was caused by).
    """
    source = (REPO_ROOT / "backend" / "fleet" / "fleet_set.py").read_text()
    if "from backend.fleet.project_name import resolve_project_name" not in source:
        _fail("one-resolver: fleet_set.py does not import resolve_project_name from backend.fleet.project_name")
    if "config.json" in source:
        _fail("one-resolver: fleet_set.py contains a 'config.json' string literal")
    if "fulcrumaxe" in source:
        _fail("one-resolver: fleet_set.py contains an 'fulcrumaxe' fallback literal")


def check_never_touches_real_home() -> None:
    """Verify this guard's own runs never wrote under the operator's real
    $HOME by comparing the real ~/.autonomous-fleet-state directory's mtime
    (if it exists) before and after a full fixture cycle.
    """
    real_fleet_dir = Path.home() / ".autonomous-fleet-state"
    if not real_fleet_dir.exists():
        return  # nothing to compare against on this host — not a failure

    before = real_fleet_dir.stat().st_mtime_ns
    with tempfile.TemporaryDirectory(prefix="fleet-status-honesty-guard-") as td:
        check_resolved_set_and_status(Path(td))
    after = real_fleet_dir.stat().st_mtime_ns
    if before != after:
        _fail("real-home-touched: ~/.autonomous-fleet-state mtime changed during this guard's run")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fleet-status-honesty-guard-") as td:
        fake_home = Path(td)
        check_resolved_set_and_status(fake_home)

    with tempfile.TemporaryDirectory(prefix="fleet-status-honesty-guard-") as td2:
        check_probe_recorded_for_ok_status(Path(td2))

    check_one_resolver_only()
    check_never_touches_real_home()

    if FAILURES:
        print(f"fleet-status-honesty-guard: {len(FAILURES)} check(s) failed")
        return 1

    print("fleet-status-honesty-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
