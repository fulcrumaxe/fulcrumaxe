"""Tests for backend.rpc.sdk_status (stats.sdk_lane RPC handler).

Verifies the expected response shape and confirms the handler is read-only
(no state mutations — no writes to audit.jsonl, state.db, or stats.duckdb).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_handler():
    import backend.rpc.sdk_status as m
    importlib.reload(m)
    return m


# ---------------------------------------------------------------------------
# Test 1: response shape
# ---------------------------------------------------------------------------

class TestResponseShape:
    def test_top_level_keys_present(self):
        m = _import_handler()
        result = m.handle({})
        assert "generated_at" in result
        assert "readiness" in result
        assert "backend_selection" in result
        assert "credit" in result
        assert "routing_counts" in result

    def test_readiness_keys(self):
        m = _import_handler()
        result = m.handle({})
        r = result["readiness"]
        assert "dispatcher_live" in r
        assert "ROUTE_VIA_DISPATCHER" in r
        assert "SHADOW_MODE" in r
        assert "SDK_BACKEND" in r

    def test_readiness_dispatcher_live_is_bool(self):
        m = _import_handler()
        result = m.handle({})
        assert isinstance(result["readiness"]["dispatcher_live"], bool)

    def test_backend_selection_keys(self):
        m = _import_handler()
        result = m.handle({})
        bs = result["backend_selection"]
        assert "would_select" in bs
        assert "reason" in bs
        assert "CLAUDE_CODE_OAUTH_TOKEN" in bs
        assert "ANTHROPIC_API_KEY" in bs

    def test_backend_selection_no_secrets(self):
        """Credential fields must be presence strings only — never actual values."""
        m = _import_handler()
        result = m.handle({})
        bs = result["backend_selection"]
        assert bs["CLAUDE_CODE_OAUTH_TOKEN"] in ("present", "absent")
        assert bs["ANTHROPIC_API_KEY"] in ("present", "absent")

    def test_credit_keys(self):
        m = _import_handler()
        result = m.handle({})
        c = result["credit"]
        # Must have at least these — error key appears only on failure
        assert "remaining_usd" in c
        assert "used_usd" in c
        assert "billing_regime" in c

    def test_routing_counts_keys(self):
        m = _import_handler()
        result = m.handle({})
        rc = result["routing_counts"]
        assert "total_runs_all_time" in rc
        assert "total_runs_last_30d" in rc
        assert "sdk_runs" in rc
        assert "cc_runs" in rc
        assert "null_route_runs" in rc
        assert "db_available" in rc

    def test_routing_counts_are_non_negative_ints(self):
        m = _import_handler()
        result = m.handle({})
        rc = result["routing_counts"]
        for key in ("total_runs_all_time", "total_runs_last_30d", "sdk_runs", "cc_runs", "null_route_runs"):
            assert isinstance(rc[key], int), f"{key} should be int, got {type(rc[key])}"
            assert rc[key] >= 0, f"{key} should be >= 0, got {rc[key]}"

    def test_generated_at_is_iso_string(self):
        m = _import_handler()
        result = m.handle({})
        ts = result["generated_at"]
        assert isinstance(ts, str)
        assert "T" in ts  # ISO 8601 datetime separator


# ---------------------------------------------------------------------------
# Test 2: dispatcher off when ROUTE_VIA_DISPATCHER is not set
# ---------------------------------------------------------------------------

class TestDispatcherOff:
    def test_dispatcher_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ROUTE_VIA_DISPATCHER", raising=False)
        m = _import_handler()
        result = m.handle({})
        assert result["readiness"]["dispatcher_live"] is False

    def test_dispatcher_live_when_set(self, monkeypatch):
        monkeypatch.setenv("ROUTE_VIA_DISPATCHER", "1")
        m = _import_handler()
        result = m.handle({})
        assert result["readiness"]["dispatcher_live"] is True

    def test_backend_none_when_no_credentials(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Patch _CREDENTIALS_FILE so the login-file check doesn't pick up the
        # real machine credentials and falsely report "subscription".
        with patch("backend.orchestrator.sdk_status._CREDENTIALS_FILE",
                   str(tmp_path / "no-credentials.json")):
            m = _import_handler()
            result = m.handle({})
        assert result["backend_selection"]["would_select"] == "none"
        assert result["backend_selection"]["CLAUDE_CODE_OAUTH_TOKEN"] == "absent"
        assert result["backend_selection"]["ANTHROPIC_API_KEY"] == "absent"


# ---------------------------------------------------------------------------
# Test 3: read-only — no state mutations
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_no_audit_writes(self, tmp_path, monkeypatch):
        """Calling handle() must not append to audit.jsonl."""
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        audit_file = tmp_path / "audit.jsonl"

        m = _import_handler()
        m.handle({})

        # audit.jsonl must not exist (handler wrote nothing)
        assert not audit_file.exists(), "handler must not write to audit.jsonl"

    def test_no_stats_db_writes(self, tmp_path, monkeypatch):
        """Calling handle() must not create or modify stats.duckdb (no write)."""
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        stats_db = tmp_path / "stats.duckdb"

        m = _import_handler()
        m.handle({})

        # stats.duckdb should not be created by a read-only call on a fresh dir
        # (duckdb creates the file only when it's opened for writing)
        # If it exists, it must only be from a prior read_only=True open
        # which duckdb allows without creating the file when it's absent.
        assert not stats_db.exists(), "handler must not create stats.duckdb on a fresh state dir"

    def test_idempotent(self):
        """Calling handle() twice returns consistent shape."""
        m = _import_handler()
        r1 = m.handle({})
        r2 = m.handle({})
        # Both calls should return the same keys
        assert set(r1.keys()) == set(r2.keys())
        assert set(r1["readiness"].keys()) == set(r2["readiness"].keys())
        assert r1["readiness"]["dispatcher_live"] == r2["readiness"]["dispatcher_live"]
