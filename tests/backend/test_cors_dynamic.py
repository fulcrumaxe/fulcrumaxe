"""Tests for the dynamic CORS allowlist in backend/server.py and
_get_dashboard_config() in backend/api.py.

Verifies that _compute_allowed_origins():
 - always includes the static defaults (5173 / 4173)
 - picks up vite_port from ~/.*-state/dashboard-runtime.json files
 - caches results for 60 s
 - ignores malformed / missing runtime files

Verifies that _get_dashboard_config():
 - prefers the state-dir runtime file over the repo-level one
 - falls back to the repo-level file when the state-dir file is absent
 - always overrides rpcToken with the live dashboard-token file
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_server_module():
    """Import (or re-import) backend.server, resetting the cache each time.

    The origins cache lives in backend.dashboard_origins (moved there per
    D#2251) — re-importing backend.server alone no longer resets it, since
    server.py just holds a one-line alias to the already-imported module.

    Popping "backend.dashboard_origins" from sys.modules is *not* enough on
    its own: server.py re-acquires it via `from backend import
    dashboard_origins`, and that statement resolves through the `backend`
    package's own namespace, which still holds a stale attribute reference
    from the previous import even after the sys.modules entry is gone — so
    the "fresh" import silently returns the old module object with its old
    cache intact. Call reset_cache() explicitly so the cache is invalidated
    regardless of that reimport quirk.
    """
    sys.modules.pop("backend.server", None)
    sys.modules.pop("backend.dashboard_origins", None)
    import backend.server as _srv  # noqa: PLC0415
    _srv._dashboard_origins.reset_cache()
    return _srv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputeAllowedOrigins:
    def test_static_defaults_always_present(self, tmp_path, monkeypatch):
        """Static fallback origins are always in the result."""
        # Point home to a temp dir with no runtime files
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5173" in origins
        assert "http://127.0.0.1:5173" in origins
        assert "http://localhost:4173" in origins
        assert "http://127.0.0.1:4173" in origins

    def test_discovers_vite_port_from_runtime_file(self, tmp_path, monkeypatch):
        """A vite port in dashboard-runtime.json is added to allowed origins."""
        state_dir = tmp_path / ".projectb-state"
        state_dir.mkdir()
        runtime = state_dir / "dashboard-runtime.json"
        runtime.write_text(json.dumps({"ports": {"vite": 5102, "api": 5202}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5102" in origins
        assert "http://127.0.0.1:5102" in origins

    def test_unlisted_port_not_in_origins(self, tmp_path, monkeypatch):
        """An origin for a port not in any runtime file is not allowed."""
        state_dir = tmp_path / ".proj-state"
        state_dir.mkdir()
        runtime = state_dir / "dashboard-runtime.json"
        runtime.write_text(json.dumps({"ports": {"vite": 7777}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:9999" not in origins
        assert "http://127.0.0.1:9999" not in origins

    def test_malformed_json_is_ignored(self, tmp_path, monkeypatch):
        """A corrupted runtime file doesn't crash and is silently skipped."""
        state_dir = tmp_path / ".bad-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text("not-json{{")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        # Should not raise
        origins = srv._compute_allowed_origins()
        assert "http://localhost:5173" in origins  # static defaults still there

    def test_missing_ports_key_is_ignored(self, tmp_path, monkeypatch):
        """A runtime file without a 'ports' key is ignored gracefully."""
        state_dir = tmp_path / ".nop-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({"project_name": "nop"}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        # Still returns static defaults
        assert "http://localhost:5173" in origins

    def test_cache_avoids_re_scan(self, tmp_path, monkeypatch):
        """Second call within TTL returns cached result, not a fresh scan."""
        state_dir = tmp_path / ".cache-state"
        state_dir.mkdir()
        runtime = state_dir / "dashboard-runtime.json"
        runtime.write_text(json.dumps({"ports": {"vite": 6600}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        first = srv._compute_allowed_origins()
        assert "http://localhost:6600" in first

        # Mutate runtime file — second call should still return cached set
        runtime.write_text(json.dumps({"ports": {"vite": 9900}}))
        second = srv._compute_allowed_origins()
        assert second is first  # exact same object from cache
        assert "http://localhost:9900" not in second  # not re-scanned yet

    def test_multiple_runtime_files_all_added(self, tmp_path, monkeypatch):
        """Origins from multiple ~/.*-state/ directories are all included."""
        for port, name in [(5200, ".proj-a-state"), (5300, ".proj-b-state")]:
            d = tmp_path / name
            d.mkdir()
            (d / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": port}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5200" in origins
        assert "http://localhost:5300" in origins

    # -----------------------------------------------------------------------
    # Port range validation (CWE-20 fix)
    # -----------------------------------------------------------------------

    def test_port_below_1024_is_rejected(self, tmp_path, monkeypatch):
        """vite_port=80 is below the valid range and must not be added."""
        state_dir = tmp_path / ".low-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 80}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:80" not in origins
        assert "http://127.0.0.1:80" not in origins

    def test_port_above_65535_is_rejected(self, tmp_path, monkeypatch):
        """vite_port=99999 exceeds the valid range and must not be added."""
        state_dir = tmp_path / ".high-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 99999}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:99999" not in origins
        assert "http://127.0.0.1:99999" not in origins

    def test_port_1024_is_accepted(self, tmp_path, monkeypatch):
        """vite_port=1024 is the lower boundary and must be accepted."""
        state_dir = tmp_path / ".min-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 1024}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:1024" in origins
        assert "http://127.0.0.1:1024" in origins

    def test_port_65535_is_accepted(self, tmp_path, monkeypatch):
        """vite_port=65535 is the upper boundary and must be accepted."""
        state_dir = tmp_path / ".max-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 65535}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:65535" in origins
        assert "http://127.0.0.1:65535" in origins

    # -----------------------------------------------------------------------
    # Cache expiry
    # -----------------------------------------------------------------------

    def test_cache_expires_after_ttl(self, tmp_path, monkeypatch):
        """After TTL elapses, a fresh scan picks up new runtime files."""
        state_dir = tmp_path / ".ttl-state"
        state_dir.mkdir()
        runtime = state_dir / "dashboard-runtime.json"
        runtime.write_text(json.dumps({"ports": {"vite": 7700}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        srv = _reload_server_module()

        first = srv._compute_allowed_origins()
        assert "http://localhost:7700" in first

        # Force cache expiry by backdating the cached timestamp beyond TTL.
        # The cache itself lives in backend.dashboard_origins (moved there
        # per D#2251) — server.py only re-exports compute_allowed_origins.
        do = srv._dashboard_origins
        old_origins, old_ts = do._origins_cache
        do._origins_cache = (old_origins, old_ts - do._ORIGINS_CACHE_TTL - 1)

        # Update runtime file with a new port
        runtime.write_text(json.dumps({"ports": {"vite": 8800}}))
        second = srv._compute_allowed_origins()

        assert "http://localhost:8800" in second  # fresh scan picked up the new port
        assert second is not first  # cache was invalidated — new object returned


# ---------------------------------------------------------------------------
# State-dir origin discovery (D#2251) — additive source, exception-guarded
# ---------------------------------------------------------------------------


class TestStateDirOriginSource:
    """compute_allowed_origins() must also discover origins from
    state_paths.STATE_DIR, which may sit outside $HOME (AUTONOMOUS_TEAM_STATE_DIR).
    That source must never raise, even when STATE_DIR can't be resolved.
    """

    def test_state_dir_outside_home_is_discovered(self, tmp_path, monkeypatch):
        """AC-1: a state dir outside $HOME contributes its vite origin."""
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        state_dir = tmp_path / "elsewhere" / ".gatekeep-state"
        state_dir.mkdir(parents=True)
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 5188}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: empty_home))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5188" in origins
        assert "http://127.0.0.1:5188" in origins
        # AC-3: static defaults still present alongside the discovered origin.
        assert "http://localhost:5173" in origins
        assert "http://127.0.0.1:4173" in origins

    def test_home_glob_still_supplements_when_state_dir_set(self, tmp_path, monkeypatch):
        """AC-2: the home glob is additive, not replaced, by the state-dir source."""
        home = tmp_path / "home"
        home_state = home / ".proj-state"
        home_state.mkdir(parents=True)
        (home_state / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 5300}}))

        other_state = tmp_path / "elsewhere" / ".other-state"
        other_state.mkdir(parents=True)
        (other_state / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 5188}}))

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(other_state))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5300" in origins  # from the home glob
        assert "http://localhost:5188" in origins  # from the state-dir source

    def test_state_dir_unset_under_pytest_no_raise(self, tmp_path, monkeypatch):
        """AC-4a: AUTONOMOUS_TEAM_STATE_DIR unset under pytest -> static defaults, no raise."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5173" in origins

    def test_state_dir_relative_no_raise(self, tmp_path, monkeypatch):
        """AC-4b: a relative AUTONOMOUS_TEAM_STATE_DIR -> static defaults, no raise."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", ".")
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5173" in origins

    def test_state_dir_nonexistent_no_raise(self, tmp_path, monkeypatch):
        """AC-4c: a state dir that doesn't exist on disk -> static defaults, no raise."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "does-not-exist"))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5173" in origins

    def test_state_dir_malformed_json_no_raise(self, tmp_path, monkeypatch):
        """AC-5: malformed JSON in the state-dir runtime file contributes no
        origin and doesn't raise — same as the existing home-glob behaviour."""
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        state_dir = tmp_path / ".bad-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text("not-json{{")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: empty_home))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))
        srv = _reload_server_module()

        origins = srv._compute_allowed_origins()
        assert "http://localhost:5173" in origins


# ---------------------------------------------------------------------------
# Rejected-origin logging (D#2251)
# ---------------------------------------------------------------------------


class TestLogRejectedOrigin:
    """dashboard_origins.log_rejected_origin() — one sanitised WARN per
    rejected origin per cache period.
    """

    def setup_method(self):
        srv = _reload_server_module()
        self.do = srv._dashboard_origins
        self.do.reset_cache()

    def test_emits_one_warn_naming_origin_and_allowlist(self, caplog):
        """AC-6: WARN message contains the rejected origin and an allowed origin."""
        allowed = {"http://localhost:5173"}
        with caplog.at_level("WARNING", logger="backend.dashboard_origins"):
            self.do.log_rejected_origin("http://127.0.0.1:5188", allowed)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "http://127.0.0.1:5188" in warnings[0].getMessage()
        assert "http://localhost:5173" in warnings[0].getMessage()

    def test_repeated_rejection_logs_once_per_cache_period(self, caplog):
        """AC-7: the same rejected origin, requested 3 times, logs exactly once."""
        allowed = {"http://localhost:5173"}
        with caplog.at_level("WARNING", logger="backend.dashboard_origins"):
            for _ in range(3):
                self.do.log_rejected_origin("http://127.0.0.1:5188", allowed)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1

    def test_empty_origin_logs_nothing(self, caplog):
        """AC-8: no Origin header (curl / same-origin) must not generate noise."""
        allowed = {"http://localhost:5173"}
        with caplog.at_level("WARNING", logger="backend.dashboard_origins"):
            self.do.log_rejected_origin("", allowed)

        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 0

    def test_allowed_origin_is_never_passed_to_the_logger(self):
        """AC-8: an allowed origin never reaches log_rejected_origin — verified
        at the call-site level, since the function itself only decides on
        dedup/emptiness, not allow-list membership. server.py's
        _set_cors_headers only calls log_rejected_origin from its `else`
        branch (the allowed branch returns before it), so an allowed origin
        structurally can't reach it. This test locks that call-site contract
        by inspecting the source once, so a future edit that moves the call
        into the allowed branch fails loudly.
        """
        import inspect
        srv = _reload_server_module()
        src = inspect.getsource(srv._HttpHandler._set_cors_headers)
        allowed_branch, _, rejected_branch = src.partition("else:")
        assert "log_rejected_origin" not in allowed_branch
        assert "log_rejected_origin" in rejected_branch

    def test_cache_reset_clears_warn_dedup(self, caplog):
        """A fresh cache period (reset_cache()) allows the same origin to WARN again."""
        allowed = {"http://localhost:5173"}
        with caplog.at_level("WARNING", logger="backend.dashboard_origins"):
            self.do.log_rejected_origin("http://127.0.0.1:5188", allowed)
            self.do.reset_cache()
            self.do.log_rejected_origin("http://127.0.0.1:5188", allowed)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 2

    def test_crlf_injection_is_stripped(self, caplog):
        """AC-9: CR/LF in an attacker-controlled origin never reaches the log line."""
        allowed = {"http://localhost:5173"}
        malicious = "http://evil\r\nINJECTED: 1"
        with caplog.at_level("WARNING", logger="backend.dashboard_origins"):
            self.do.log_rejected_origin(malicious, allowed)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "\r" not in message
        assert "\n" not in message

    def test_long_origin_is_truncated(self, caplog):
        """AC-9: an origin longer than 200 chars is truncated in the log line."""
        allowed = {"http://localhost:5173"}
        long_origin = "http://" + "a" * 300 + ".example.com"
        with caplog.at_level("WARNING", logger="backend.dashboard_origins"):
            self.do.log_rejected_origin(long_origin, allowed)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert long_origin not in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# _get_dashboard_config: state-dir priority fix (D#1037)
# ---------------------------------------------------------------------------


class TestGetDashboardConfig:
    """_get_dashboard_config() must read rpcBaseUrl from the state-dir runtime
    file, not from the repo-level .autonomous-team/dashboard-runtime.json.

    The repo-level file can be clobbered by another project's start-dashboard.sh,
    causing the browser to be directed to the wrong RPC port and getting 401s.
    """

    def _import_api(self, monkeypatch, tmp_state_dir: Path, tmp_repo_dir: Path):
        """Return api module with STATE_DIR and _REPO_ROOT pointed at temp dirs."""
        # Force a fresh import so module-level path constants are re-evaluated.
        # We must restore the original sys.modules["backend.api"] entry at teardown
        # so that other test modules which imported `backend.api` at module level
        # continue to hold a live reference — without this, their unittest.mock
        # patches target the wrong (stale) module object.
        #
        # Pattern: ensure "backend.api" is in sys.modules first (so monkeypatch can
        # capture and restore it), then pop-and-reimport for our fresh copy.
        import importlib  # noqa: PLC0415
        if "backend.api" not in sys.modules:
            importlib.import_module("backend.api")
        # monkeypatch.setitem saves the current value and schedules restoration.
        monkeypatch.setitem(sys.modules, "backend.api", sys.modules["backend.api"])
        # Pop the entry so the next import creates a fresh module object.
        sys.modules.pop("backend.api")
        import backend.api as api_mod  # noqa: PLC0415
        # sys.modules["backend.api"] now points to api_mod (the fresh copy).
        # monkeypatch teardown will restore it to the original module object.

        monkeypatch.setattr(api_mod, "_STATE_DIR", tmp_state_dir)
        monkeypatch.setattr(api_mod, "_REPO_ROOT", tmp_repo_dir)
        return api_mod

    def test_state_dir_runtime_preferred_over_repo_level(self, tmp_path, monkeypatch):
        """State-dir runtime file wins when both files exist with different rpcBaseUrl."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        repo_dir = tmp_path / "repo"
        (repo_dir / ".autonomous-team").mkdir(parents=True)

        # State-dir file: AF's real RPC port
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:8765",
            "rpcToken": "correct-token",
        }))
        # Repo-level file: stale projectb RPC port (simulates a cross-project clobber)
        (repo_dir / ".autonomous-team" / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:5302",
            "rpcToken": "stale-token",
        }))

        api = self._import_api(monkeypatch, state_dir, repo_dir)
        cfg = api._get_dashboard_config()

        assert cfg["rpcBaseUrl"] == "http://localhost:8765", (
            "State-dir runtime file must take priority; got %s instead" % cfg["rpcBaseUrl"]
        )

    def test_falls_back_to_repo_level_when_state_dir_absent(self, tmp_path, monkeypatch):
        """When only the repo-level file exists, its values are used."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()  # Empty — no dashboard-runtime.json
        repo_dir = tmp_path / "repo"
        (repo_dir / ".autonomous-team").mkdir(parents=True)

        (repo_dir / ".autonomous-team" / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:9999",
            "rpcToken": "repo-token",
        }))

        api = self._import_api(monkeypatch, state_dir, repo_dir)
        cfg = api._get_dashboard_config()

        assert cfg["rpcBaseUrl"] == "http://localhost:9999"

    def test_runtime_token_preferred_over_token_file(self, tmp_path, monkeypatch):
        """The project-scoped runtime rpcToken wins; the repo-level dashboard-token file is fallback only."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        repo_dir = tmp_path / "repo"
        (repo_dir / ".autonomous-team").mkdir(parents=True)

        # Preferred path: both sources present — runtime token must win.
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:8765",
            "rpcToken": "runtime-token",
        }))
        (repo_dir / ".autonomous-team" / "dashboard-token").write_text("file-token\n")

        api = self._import_api(monkeypatch, state_dir, repo_dir)
        cfg = api._get_dashboard_config()

        assert cfg["rpcToken"] == "runtime-token", (
            "runtime rpcToken must be preferred; dashboard-token file is fallback only"
        )

        # Fallback path: runtime token absent — file value must be used.
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:8765",
            "rpcToken": "",
        }))

        api2 = self._import_api(monkeypatch, state_dir, repo_dir)
        cfg2 = api2._get_dashboard_config()

        assert cfg2["rpcToken"] == "file-token", (
            "dashboard-token file must be used as fallback when runtime rpcToken is empty"
        )

    def test_defaults_when_no_files_exist(self, tmp_path, monkeypatch):
        """Falls back to safe defaults when neither runtime file exists."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        repo_dir = tmp_path / "repo"
        (repo_dir / ".autonomous-team").mkdir(parents=True)

        api = self._import_api(monkeypatch, state_dir, repo_dir)
        cfg = api._get_dashboard_config()

        assert cfg["rpcBaseUrl"] == "http://localhost:8765"
        assert cfg["rpcToken"] == ""
