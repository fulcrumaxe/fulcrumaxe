"""Tests for the fleet.projects / fleet.cost / GET-/api/fleet/projects
response-boundary redaction (D#2239).

There are THREE independently-implemented "fleet projects" surfaces in this
codebase, and all three are covered here:

  1. RPC method `fleet.projects` (backend/rpc/fleet_projects.py), reading
     backend.fleet.fleet_set.resolve_fleet_set() -- the resolved union of
     both fleet-discovery mechanisms (D#2317 PR-a). Bearer-auth'd. Consumed
     by the Fleet page.
  2. REST route `GET /api/fleet/projects` on the FastAPI app
     (backend/routers/api_fleet.py, mounted by backend/asgi_app.py), still
     reading backend.fleet.runtime.discover_running_projects() alone -- this
     route is out of D#2317's scope (it is not what start-dashboard.sh
     launches; see #3). No auth. Only live when a deployment has opted into
     the asgi_app migration via scripts/cutover-dashboard.sh.
  3. backend/api.py's own inline `/api/fleet/projects` branch in `_Handler`
     -- now also reads resolve_fleet_set(), same as #1, so the two surfaces
     that actually matter (the RPC method and the endpoint every adopter's
     default coldstart path hits) can never disagree about which projects
     exist (D#2317 PR-a item 7). No auth, but this is the one that actually
     runs by default: scripts/start-dashboard.sh (what
     scripts/coldstart-project.sh tells every adopter to run) launches
     backend/api.py directly as a raw stdlib ThreadingHTTPServer. Testing
     only #2 proves nothing about what an adopter following the documented
     onboarding path is actually running.

#1 and #3 both wrap resolve_fleet_set() and share one redaction helper,
backend.fleet.fleet_set.redact_for_dashboard(), so they cannot drift apart.
#2 wraps discover_running_projects() and uses the older
backend.fleet.runtime.redact_for_unauthenticated_response() helper, unchanged
by D#2317. resolve_fleet_set() legitimately carries state_dir (an internal
join key), repo, ports and pids for internal callers -- none of those four
fields should ever appear in what any of the three surfaces hands back to a
caller.

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" pytest -x -q backend/tests/test_fleet_payload_redaction.py
"""

from __future__ import annotations

import http.client
import sys
import threading
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

FAKE_PROJECTS = [
    {
        "name": "fulcrumaxe",
        "state_dir": "/home/user/.fulcrumaxe-state",
        "dashboard_port": 5173,
        "version": 1,
        "repo": "fulcrumaxe/fulcrumaxe",
        "language": "python",
        "ports": {"vite": 5173, "api": 18099, "rpc": 18100, "sse": 18101},
        "ok": True,
    },
    {
        "name": "gatekeep",
        "state_dir": "/home/user/.gatekeep-state",
        "dashboard_port": 5102,
        "version": 1,
        "repo": "fulcrumaxe/gatekeep",
        "language": "python",
        "ok": True,
    },
    {
        "name": "corrupt",
        "state_dir": "/home/user/.corrupt-state",
        "ok": False,
        "error": "JSON parse error: unexpected token",
    },
]

FORBIDDEN_KEYS = ("state_dir", "repo", "ports")

# resolve_fleet_set() shape (D#2317 PR-a) -- the four-value measured
# ``status`` replacing the old boolean ``ok`` discover_projects() used to
# report on its own (see backend/fleet/fleet_set.py).
FAKE_RESOLVED_PROJECTS = [
    {
        "name": "fulcrumaxe",
        "state_dir": "/home/user/.fulcrumaxe-state",
        "dashboard_port": 5173,
        "version": 1,
        "repo": "fulcrumaxe/fulcrumaxe",
        "language": "python",
        "ports": {"vite": 5173, "api": 18099, "rpc": 18100, "sse": 18101},
        "pids": {"api": 1234},
        "status": "ok",
        "agents_running": 2,
    },
    {
        "name": "gatekeep",
        "state_dir": "/home/user/.gatekeep-state",
        "dashboard_port": 5102,
        "version": 1,
        "repo": "fulcrumaxe/gatekeep",
        "language": "python",
        "status": "unknown",
    },
    {
        "name": "corrupt",
        "state_dir": "/home/user/.corrupt-state",
        "status": "error",
        "error": "JSON parse error: unexpected token",
    },
]


class TestFleetProjectsRedaction:
    def test_no_forbidden_keys_in_any_record(self):
        from backend.rpc import fleet_projects

        with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=FAKE_RESOLVED_PROJECTS):
            result = fleet_projects.handle({})

        assert "projects" in result
        assert len(result["projects"]) == len(FAKE_RESOLVED_PROJECTS)
        for record in result["projects"]:
            for key in FORBIDDEN_KEYS + ("pids",):
                assert key not in record, f"{key!r} leaked in {record!r}"

    def test_safe_fields_survive_redaction(self):
        from backend.rpc import fleet_projects

        with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=FAKE_RESOLVED_PROJECTS):
            result = fleet_projects.handle({})

        by_name = {p["name"]: p for p in result["projects"]}
        assert by_name["fulcrumaxe"]["dashboard_port"] == 5173
        assert by_name["fulcrumaxe"]["status"] == "ok"
        assert by_name["fulcrumaxe"]["agents_running"] == 2
        assert by_name["gatekeep"]["status"] == "unknown"
        assert by_name["corrupt"]["status"] == "error"
        assert by_name["corrupt"]["error"] == "JSON parse error: unexpected token"

    def test_etag_is_computed_over_redacted_payload(self):
        """A client holding an old (unredacted-shape) etag must get a fresh
        one, not a stale 304 -- the etag has to reflect what's actually sent.
        """
        from backend.rpc import fleet_projects

        with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=FAKE_RESOLVED_PROJECTS):
            result = fleet_projects.handle({})

        stale_etag = "0" * 40  # fabricated etag, definitely not a match
        with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=FAKE_RESOLVED_PROJECTS):
            not_modified = fleet_projects.handle({"if_none_match": stale_etag})
        assert not_modified.get("not_modified") is not True

        # Re-requesting with the real etag from the redacted payload does 304.
        with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=FAKE_RESOLVED_PROJECTS):
            second = fleet_projects.handle({"if_none_match": result["etag"]})
        assert second == {"not_modified": True, "etag": result["etag"]}


class TestFleetCostRedaction:
    def test_no_state_dir_in_per_project(self):
        from backend.rpc import fleet_cost

        fake_summary = {
            "last_7d": [],
        }

        with (
            patch("backend.fleet.discovery.discover_projects", return_value=FAKE_PROJECTS),
            patch("backend.fleet.cost_summary.read_cost_summary", return_value=fake_summary),
        ):
            result = fleet_cost.handle({})

        assert "per_project" in result
        assert len(result["per_project"]) == len(FAKE_PROJECTS)
        for record in result["per_project"]:
            assert "state_dir" not in record, f"state_dir leaked in {record!r}"

    def test_broken_project_record_also_redacted(self):
        from backend.rpc import fleet_cost

        with (
            patch("backend.fleet.discovery.discover_projects", return_value=FAKE_PROJECTS),
            patch(
                "backend.fleet.cost_summary.read_cost_summary",
                side_effect=Exception("boom"),
            ),
        ):
            result = fleet_cost.handle({})

        by_name = {p["name"]: p for p in result["per_project"]}
        # "corrupt" is not ok, so read_cost_summary is never called for it.
        assert by_name["corrupt"]["ok"] is False
        assert "state_dir" not in by_name["corrupt"]
        # "fulcrumaxe" is ok but read_cost_summary raises here.
        assert by_name["fulcrumaxe"]["ok"] is False
        assert "state_dir" not in by_name["fulcrumaxe"]


FAKE_RUNNING_PROJECTS = [
    {
        "name": "fulcrumaxe",
        "repo": "fulcrumaxe/fulcrumaxe",
        "state_dir": "/home/user/.fulcrumaxe-state",
        "ports": {"vite": 5173, "api": 18099, "rpc": 18100, "sse": 18101},
        "pids": {"api": 1234, "server": 1235, "sse": 1236, "vite": 1237},
        "started_at": "2026-05-18T16:00:00Z",
        "alive": True,
        "last_seen": "2026-05-18T16:00:00Z",
        "ok": True,
    },
    {
        "name": "gatekeep",
        "repo": "fulcrumaxe/gatekeep",
        "state_dir": "/home/user/.gatekeep-state",
        "ports": {"vite": 5102, "api": 5202, "rpc": 5302, "sse": 5402},
        "pids": {"api": 5678},
        "started_at": "2026-05-18T16:05:00Z",
        "alive": True,
        "last_seen": "2026-05-18T16:05:00Z",
        "ok": True,
    },
    {
        "name": "corrupt",
        "state_dir": "/home/user/.corrupt-state",
        "ok": False,
        "alive": False,
        "error": "JSON parse error: unexpected token",
    },
]

REST_FORBIDDEN_KEYS = ("state_dir", "repo", "ports", "pids")


class TestApiFleetProjectsRestRedaction:
    """GET /api/fleet/projects on the FastAPI app (backend/routers/api_fleet.py).

    This route only runs when a deployment has migrated to asgi_app via
    scripts/cutover-dashboard.sh -- it is NOT what scripts/start-dashboard.sh
    launches by default. See TestApiPyLiveHandlerRedaction below for the
    handler that actually runs out of the box.

    discover_running_projects() itself is untouched (still returns
    state_dir/repo/ports/pids for internal callers); the redaction lives in
    the shared helper backend.fleet.runtime.redact_for_unauthenticated_response(),
    called from the route handler, backend.routers.api_fleet.api_fleet_projects().
    """

    def test_no_forbidden_keys_in_any_record(self):
        from backend.routers.api_fleet import api_fleet_projects

        with patch(
            "backend.fleet.runtime.discover_running_projects",
            return_value=FAKE_RUNNING_PROJECTS,
        ):
            result = api_fleet_projects()

        assert "projects" in result
        assert len(result["projects"]) == len(FAKE_RUNNING_PROJECTS)
        for record in result["projects"]:
            for key in REST_FORBIDDEN_KEYS:
                assert key not in record, f"{key!r} leaked in {record!r}"

    def test_safe_fields_survive_redaction(self):
        from backend.routers.api_fleet import api_fleet_projects

        with patch(
            "backend.fleet.runtime.discover_running_projects",
            return_value=FAKE_RUNNING_PROJECTS,
        ):
            result = api_fleet_projects()

        by_name = {p["name"]: p for p in result["projects"]}
        assert by_name["fulcrumaxe"]["alive"] is True
        assert by_name["fulcrumaxe"]["ok"] is True
        assert by_name["corrupt"]["ok"] is False
        assert by_name["corrupt"]["error"] == "JSON parse error: unexpected token"

    def test_discover_running_projects_itself_is_unmodified(self):
        """Redaction must live at the route boundary, not in discovery --
        internal callers (e.g. anything joining on state_dir) still need
        the raw fields.
        """
        from backend.fleet import runtime

        runtime.invalidate_cache()
        try:
            with patch.object(runtime, "_scan", return_value=FAKE_RUNNING_PROJECTS):
                raw = runtime.discover_running_projects()
        finally:
            runtime.invalidate_cache()

        assert raw == FAKE_RUNNING_PROJECTS
        assert all("state_dir" in p for p in raw)


class TestApiPyLiveHandlerRedaction:
    """backend/api.py's own inline `/api/fleet/projects` branch in `_Handler`.

    This is the surface that actually runs by default: scripts/start-dashboard.sh
    (what scripts/coldstart-project.sh tells every adopter to run) launches
    backend/api.py directly as a raw stdlib ThreadingHTTPServer, never
    backend/asgi_app.py. Spins up a REAL HTTPServer bound to `_Handler` and
    hits it over a real socket -- not a FastAPI TestClient, which never
    touches this code path at all.

    As of D#2317 PR-a this branch reads resolve_fleet_set() -- the same
    resolved-union helper the fleet.projects RPC method uses -- rather than
    discover_running_projects() alone, so it is tested against
    FAKE_RESOLVED_PROJECTS (the new four-value ``status`` shape), not
    FAKE_RUNNING_PROJECTS (the old ``ok``/``alive`` shape, still used by
    TestApiFleetProjectsRestRedaction above for the unrelated FastAPI route).
    """

    @pytest.fixture(autouse=True)
    def _server(self):
        from backend.api import _Handler

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def _get(self, path: str):
        import json as _json

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = _json.loads(resp.read())
        finally:
            conn.close()
        return resp.status, body

    def test_no_forbidden_keys_in_any_record(self):
        with patch(
            "backend.fleet.fleet_set.resolve_fleet_set",
            return_value=FAKE_RESOLVED_PROJECTS,
        ):
            status, body = self._get("/api/fleet/projects")

        assert status == 200
        assert len(body["projects"]) == len(FAKE_RESOLVED_PROJECTS)
        for record in body["projects"]:
            for key in REST_FORBIDDEN_KEYS:
                assert key not in record, f"{key!r} leaked in {record!r}"

    def test_safe_fields_survive_redaction(self):
        with patch(
            "backend.fleet.fleet_set.resolve_fleet_set",
            return_value=FAKE_RESOLVED_PROJECTS,
        ):
            status, body = self._get("/api/fleet/projects")

        assert status == 200
        by_name = {p["name"]: p for p in body["projects"]}
        assert by_name["fulcrumaxe"]["status"] == "ok"
        assert by_name["fulcrumaxe"]["agents_running"] == 2
        assert by_name["gatekeep"]["status"] == "unknown"
        assert by_name["corrupt"]["status"] == "error"
        assert by_name["corrupt"]["error"] == "JSON parse error: unexpected token"

    def test_same_project_names_as_the_rpc_method(self):
        """D#2317 PR-a item 7: GET /api/fleet/projects and RPC fleet.projects
        resolve from the same helper -- against the same fixture data, the
        set of names the two surfaces return must be equal.
        """
        from backend.rpc import fleet_projects

        with patch(
            "backend.fleet.fleet_set.resolve_fleet_set",
            return_value=FAKE_RESOLVED_PROJECTS,
        ):
            status, body = self._get("/api/fleet/projects")
            rpc_result = fleet_projects.handle({})

        assert status == 200
        rest_names = {p["name"] for p in body["projects"]}
        rpc_names = {p["name"] for p in rpc_result["projects"]}
        assert rest_names == rpc_names == {"fulcrumaxe", "gatekeep", "corrupt"}
