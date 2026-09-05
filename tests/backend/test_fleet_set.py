"""Tests for backend/fleet/fleet_set.py — D#2317 PR-a.

Covers the Spec's numbered PR-a acceptance items 1-9 (10-12 are UI/CI and
covered elsewhere: dashboard/src/pages/fleet/__tests__/ and
scripts/ci/fleet-status-honesty-guard.py).

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" python3 -m pytest -q tests/backend/test_fleet_set.py
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.fleet import discovery as fleet_discovery
from backend.fleet import runtime as fleet_runtime
from backend.fleet.fleet_set import redact_for_dashboard, resolve_fleet_set


@pytest.fixture(autouse=True)
def reset_caches():
    fleet_discovery.invalidate_cache()
    fleet_runtime.invalidate_cache()
    yield
    fleet_discovery.invalidate_cache()
    fleet_runtime.invalidate_cache()


def _write_project_json(state_dir: Path, name: str, **extra) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {"project_name": name, "version": 1, "state_dir": str(state_dir), **extra}
    pj = state_dir / "project.json"
    pj.write_text(json.dumps(data))
    return pj


def _write_runtime_json(state_dir: Path, name: str, ports: dict, **extra) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {"project_name": name, "ports": ports, "started_at": "2026-05-18T16:00:00Z", **extra}
    rj = state_dir / "dashboard-runtime.json"
    rj.write_text(json.dumps(data))
    return rj


def _fake_glob(proj_paths: list[str], runtime_paths: list[str]):
    def _glob(pattern: str) -> list[str]:
        if pattern.endswith("project.json"):
            return proj_paths
        if pattern.endswith("dashboard-runtime.json"):
            return runtime_paths
        return []
    return _glob


# ---------------------------------------------------------------------------
# Item 1 + 6 dedup — union, one record per state dir, no duplicates
# ---------------------------------------------------------------------------


def test_resolve_fleet_set_unions_and_dedups(tmp_path):
    proj_only = tmp_path / ".alpha-state"
    runtime_only = tmp_path / ".beta-state"
    both = tmp_path / ".gamma-state"

    _write_project_json(proj_only, "alpha")
    _write_runtime_json(runtime_only, "beta", {"vite": 19981, "api": 19982})
    _write_project_json(both, "gamma")
    _write_runtime_json(both, "gamma", {"vite": 19983, "api": 19984})

    fake = _fake_glob(
        [str(proj_only / "project.json"), str(both / "project.json")],
        [str(runtime_only / "dashboard-runtime.json"), str(both / "dashboard-runtime.json")],
    )
    with patch("glob.glob", side_effect=fake):
        records = resolve_fleet_set()

    assert len(records) == 3
    names = sorted(r["name"] for r in records)
    assert names == ["alpha", "beta", "gamma"]
    # Each state dir appears exactly once even though "gamma" was discovered
    # by both mechanisms.
    state_dirs = [r["state_dir"] for r in records]
    assert len(state_dirs) == len(set(state_dirs))


# ---------------------------------------------------------------------------
# Item 2 — project.json-only record is "unknown", never "ok"
# ---------------------------------------------------------------------------


def test_project_json_only_is_unknown_never_ok(tmp_path):
    state_dir = tmp_path / ".solo-state"
    _write_project_json(state_dir, "solo")

    fake = _fake_glob([str(state_dir / "project.json")], [])
    with patch("glob.glob", side_effect=fake):
        records = resolve_fleet_set()

    assert len(records) == 1
    assert records[0]["status"] == "unknown"
    assert records[0]["status"] != "ok"


# ---------------------------------------------------------------------------
# Item 3 — "ok" is unreachable without a probe
# ---------------------------------------------------------------------------


def test_ok_status_only_after_a_real_probe_call(tmp_path):
    state_dir = tmp_path / ".probed-state"
    ports = {"vite": 19985, "api": 19986}
    _write_runtime_json(state_dir, "probed", ports)

    fake = _fake_glob([], [str(state_dir / "dashboard-runtime.json")])
    prober = MagicMock(return_value=True)
    with patch("glob.glob", side_effect=fake), patch("backend.fleet.runtime._probe_ports", prober):
        records = resolve_fleet_set()

    assert len(records) == 1
    assert records[0]["status"] == "ok"
    assert prober.call_count > 0
    called_ports = prober.call_args[0][0]
    assert called_ports
    assert any(isinstance(v, int) for v in called_ports.values())


def test_unknown_status_when_no_ports_are_probeable(tmp_path):
    state_dir = tmp_path / ".noports-state"
    _write_runtime_json(state_dir, "noports", {"vite": "not-a-port"})

    fake = _fake_glob([], [str(state_dir / "dashboard-runtime.json")])
    with patch("glob.glob", side_effect=fake):
        records = resolve_fleet_set()

    assert records[0]["status"] == "unknown"


def test_status_from_ports_short_circuits_before_the_prober_when_nothing_probeable():
    """Unit-level check on the status-computation helper itself: with no
    integer ports at all, ``_probe_ports`` (the shared TCP-connect prober)
    must never be reached — distinct from the full resolve_fleet_set()
    pipeline, where discover_running_projects() separately calls the same
    prober to compute its own unrelated ``alive`` field regardless of what
    status this module derives.
    """
    from backend.fleet.fleet_set import _status_from_ports

    prober = MagicMock(return_value=True)
    with patch("backend.fleet.runtime._probe_ports", prober):
        status = _status_from_ports({"vite": "not-a-port"})

    assert status == "unknown"
    prober.assert_not_called()


# ---------------------------------------------------------------------------
# Item 4 — partial liveness reads "down", distinguishable from "unknown"
# ---------------------------------------------------------------------------


def test_partial_liveness_is_down_not_unknown(tmp_path):
    # One real, live, listening socket...
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    live_port = srv.getsockname()[1]

    try:
        state_dir = tmp_path / ".partial-state"
        # ...and one port nothing is listening on.
        _write_runtime_json(state_dir, "partial", {"vite": live_port, "api": 19987})

        fake = _fake_glob([], [str(state_dir / "dashboard-runtime.json")])
        with patch("glob.glob", side_effect=fake):
            records = resolve_fleet_set()

        assert records[0]["status"] == "down"
        assert records[0]["status"] != "unknown"
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Item 5 — runtime-only project appears, named from its own fixture data
# ---------------------------------------------------------------------------


def test_runtime_only_project_appears_by_its_own_name(tmp_path):
    state_dir = tmp_path / ".only-runtime-state"
    _write_runtime_json(state_dir, "only-in-runtime", {"vite": 19988})

    fake = _fake_glob([], [str(state_dir / "dashboard-runtime.json")])
    with patch("glob.glob", side_effect=fake):
        records = resolve_fleet_set()

    assert len(records) == 1
    assert records[0]["name"] == "only-in-runtime"


# ---------------------------------------------------------------------------
# Item 6 — redaction: forbidden keys never survive redact_for_dashboard()
# ---------------------------------------------------------------------------


def test_redact_for_dashboard_drops_forbidden_keys():
    record = {
        "name": "x",
        "state_dir": "/home/user/.x-state",
        "repo": "acme/x",
        "ports": {"vite": 5100},
        "pids": {"api": 123},
        "status": "ok",
        "dashboard_port": 5100,
        "agents_running": 2,
    }
    safe = redact_for_dashboard(record)

    for forbidden in ("state_dir", "repo", "ports", "pids"):
        assert forbidden not in safe
    assert safe["name"] == "x"
    assert safe["status"] == "ok"
    assert safe["dashboard_port"] == 5100
    assert safe["agents_running"] == 2


def test_error_record_never_carries_forbidden_keys(tmp_path):
    state_dir = tmp_path / ".corrupt-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "project.json").write_text("NOT JSON {{{")

    fake = _fake_glob([str(state_dir / "project.json")], [])
    with patch("glob.glob", side_effect=fake):
        records = resolve_fleet_set()

    assert records[0]["status"] == "error"
    safe = redact_for_dashboard(records[0])
    for forbidden in ("state_dir", "repo", "ports", "pids"):
        assert forbidden not in safe


# ---------------------------------------------------------------------------
# Item 8 — one name resolver only (source-level check)
# ---------------------------------------------------------------------------


def test_fleet_set_has_no_second_name_resolver():
    source = (Path(__file__).resolve().parent.parent.parent / "backend" / "fleet" / "fleet_set.py").read_text()
    assert "from backend.fleet.project_name import resolve_project_name" in source
    assert "config.json" not in source
    assert "autonomous-forever" not in source


# ---------------------------------------------------------------------------
# Item 9 — agents_running is present or entirely absent, never an unearned 0
# ---------------------------------------------------------------------------


def test_agents_running_omitted_when_name_is_unresolvable(tmp_path):
    """A corrupted project.json guesses a name from the directory — that
    guess is not trustworthy enough to key a fleet.db lookup on, so
    agents_running must be omitted (status is "error" either way)."""
    state_dir = tmp_path / ".broken-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "project.json").write_text("NOT JSON {{{")

    fake = _fake_glob([str(state_dir / "project.json")], [])
    with patch("glob.glob", side_effect=fake):
        records = resolve_fleet_set()

    assert "agents_running" not in records[0]


def test_agents_running_present_for_a_resolvable_non_serving_project(tmp_path):
    state_dir = tmp_path / ".other-state"
    _write_project_json(state_dir, "other-project")

    fake = _fake_glob([str(state_dir / "project.json")], [])
    with patch("glob.glob", side_effect=fake), \
         patch("backend.fleet.concurrency.count_project", return_value=5):
        records = resolve_fleet_set()

    assert records[0]["agents_running"] == 5


def test_agents_running_omitted_when_count_project_raises(tmp_path):
    state_dir = tmp_path / ".flaky-state"
    _write_project_json(state_dir, "flaky-project")

    fake = _fake_glob([str(state_dir / "project.json")], [])
    with patch("glob.glob", side_effect=fake), \
         patch("backend.fleet.concurrency.count_project", side_effect=RuntimeError("db locked")):
        records = resolve_fleet_set()

    assert "agents_running" not in records[0]


def test_serving_project_uses_resolve_project_name_for_the_join_key(tmp_path, monkeypatch):
    """The one record whose state_dir is this backend's own STATE_DIR must
    look up its agents_running under resolve_project_name()'s answer, not
    its own (possibly stale) self-reported name."""
    state_dir = tmp_path / ".serving-state"
    # Self-reports a name that deliberately does NOT match what
    # resolve_project_name() will return, to prove the override happens.
    _write_project_json(state_dir, "stale-self-reported-name")

    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

    fake = _fake_glob([str(state_dir / "project.json")], [])
    captured_names = []

    def _fake_count_project(name):
        captured_names.append(name)
        return 2

    with patch("glob.glob", side_effect=fake), \
         patch("backend.fleet.project_name.resolve_project_name", return_value="canonical-name"), \
         patch("backend.fleet.concurrency.count_project", side_effect=_fake_count_project):
        records = resolve_fleet_set()

    assert records[0]["agents_running"] == 2
    assert captured_names == ["canonical-name"]


def test_serving_project_omits_agents_running_when_name_unresolvable(tmp_path, monkeypatch):
    state_dir = tmp_path / ".serving2-state"
    _write_project_json(state_dir, "whatever")

    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

    fake = _fake_glob([str(state_dir / "project.json")], [])
    from backend.fleet.project_name import ProjectNameUnresolvable

    with patch("glob.glob", side_effect=fake), \
         patch("backend.fleet.project_name.resolve_project_name",
               side_effect=ProjectNameUnresolvable("no config")):
        records = resolve_fleet_set()

    assert "agents_running" not in records[0]
