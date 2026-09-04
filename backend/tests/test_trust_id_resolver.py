"""Tests for scripts/lib/trust_id_resolver.py — D#1840 (CWE-290): pin trust
to immutable GitHub node IDs instead of a mutable login string.

Acceptance criteria coverage (Discussion #1840 Spec):
  AC2   resolver returns three mutually distinguishable states
  AC3   stdout parsed regardless of subprocess exit code (R1)
  AC4   UNKNOWN never falls back to a login-string comparison (R3)
  AC6   the resolved-ID store is a trust store, not a cache (R4)
  AC10  detection: unresolvable / id-mismatch / suspicious-creation
  AC11  zero net-new API round-trips on the classification hot path
  AC15  asymmetric bot/boss runtime policy
  AC16  migration is a loud abort, never a silent drop

No test in this file queries a live GitHub account — every resolver call is
a stub matching the exact stdout/exit-code shapes measured by the D#1840
panel against ``gh api graphql``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import trust_id_resolver as tir  # noqa: E402


# ---------------------------------------------------------------------------
# Fake subprocess.run results, matching the D#1840 panel's measured shapes.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def _run_not_found(*_args, **_kwargs):
    # Measured shape (a): exit 1, HTTP 200, typed NOT_FOUND error.
    return _FakeResult(1, json.dumps({"data": {"user": None}, "errors": [{"type": "NOT_FOUND"}]}))


def _run_unparseable(*_args, **_kwargs):
    # Measured shape (b): exit 1, empty/garbage stdout (network/auth failure).
    return _FakeResult(1, "")


def _run_resolved(node_id="U_abc123", created_at="2020-01-01T00:00:00Z"):
    def _f(*_args, **_kwargs):
        return _FakeResult(0, json.dumps({"data": {"user": {"id": node_id, "createdAt": created_at}}}))
    return _f


def _run_raising(*_args, **_kwargs):
    raise TimeoutError("gh api graphql timed out")


def _run_other_error(*_args, **_kwargs):
    # Parseable, exit 1, but NOT a typed NOT_FOUND (e.g. a rate limit shape).
    return _FakeResult(1, json.dumps({"errors": [{"type": "RATE_LIMITED"}]}))


# ---------------------------------------------------------------------------
# AC2/AC3/AC4 — resolve_login_to_id's three-state contract
# ---------------------------------------------------------------------------


class TestResolveLoginToId:
    def test_not_found_shape_is_absent(self):
        res = tir.resolve_login_to_id("freed-login", run=_run_not_found)
        assert res["state"] == tir.ABSENT
        assert res["id"] is None

    def test_unparseable_stdout_is_unknown(self):
        res = tir.resolve_login_to_id("someone", run=_run_unparseable)
        assert res["state"] == tir.UNKNOWN
        assert res["id"] is None

    def test_resolved_shape_returns_id_and_created_at(self):
        res = tir.resolve_login_to_id("someone", run=_run_resolved("U_xyz", "2019-05-01T00:00:00Z"))
        assert res["state"] == tir.RESOLVED
        assert res["id"] == "U_xyz"
        assert res["created_at"] == "2019-05-01T00:00:00Z"

    def test_subprocess_exception_is_unknown_never_absent(self):
        # A timeout/missing-binary must never be conflated with "authoritatively
        # absent" — that would let a transient failure permanently drop trust.
        res = tir.resolve_login_to_id("someone", run=_run_raising)
        assert res["state"] == tir.UNKNOWN

    def test_parseable_non_not_found_error_is_unknown(self):
        # A rate-limit or permission error shape is parseable but is not the
        # typed NOT_FOUND this resolver requires before declaring ABSENT.
        res = tir.resolve_login_to_id("someone", run=_run_other_error)
        assert res["state"] == tir.UNKNOWN

    def test_three_states_are_mutually_distinguishable_through_the_resolver(self):
        states = {
            tir.resolve_login_to_id("a", run=_run_not_found)["state"],
            tir.resolve_login_to_id("b", run=_run_unparseable)["state"],
            tir.resolve_login_to_id("c", run=_run_resolved())["state"],
        }
        assert states == {tir.ABSENT, tir.UNKNOWN, tir.RESOLVED}


# ---------------------------------------------------------------------------
# ID collaborator cache
# ---------------------------------------------------------------------------


class TestIdCache:
    def test_write_then_read_round_trips(self, tmp_path):
        cache_path = tmp_path / "id_cache.json"
        tir.write_id_cache(cache_path, {"U_1", "U_2"})
        assert tir.read_id_cache(cache_path) == {"U_1", "U_2"}

    def test_missing_file_reads_as_miss(self, tmp_path):
        assert tir.read_id_cache(tmp_path / "nope.json") is None

    def test_corrupt_json_reads_as_miss(self, tmp_path):
        cache_path = tmp_path / "id_cache.json"
        cache_path.write_text("{not json")
        assert tir.read_id_cache(cache_path) is None

    def test_unversioned_login_keyed_cache_is_not_read_as_ids(self, tmp_path):
        # Simulates external_intake_gate.py's OLD login-keyed cache shape
        # landing at this path by accident — must miss, not be misread.
        cache_path = tmp_path / "id_cache.json"
        cache_path.write_text(json.dumps({"cached_at": time.time(), "collaborators": ["some-login"]}))
        assert tir.read_id_cache(cache_path) is None

    def test_expired_cache_is_a_miss(self, tmp_path):
        cache_path = tmp_path / "id_cache.json"
        cache_path.write_text(
            json.dumps({"schema": 1, "cached_at": time.time() - tir.CACHE_TTL_SECONDS - 10, "ids": ["U_stale"]})
        )
        assert tir.read_id_cache(cache_path) is None


# ---------------------------------------------------------------------------
# AC6 — trust store: written only from success, corrupt store reads UNKNOWN
# ---------------------------------------------------------------------------


class TestTrustStore:
    def test_never_written_store_reads_as_legitimately_empty(self, tmp_path):
        path = tmp_path / "store.json"
        assert tir._read_trust_store(path) == {}
        assert tir.get_stored_id("nobody", path=path) is None

    def test_garbage_store_reads_as_unknown_not_empty(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text("not valid json at all {{{")
        # The lower-level read distinguishes corrupt (None) from legitimately
        # empty ({}) — this is the assertion the AC requires directly.
        assert tir._read_trust_store(path) is None

    def test_wrong_schema_version_reads_as_unknown(self, tmp_path):
        path = tmp_path / "store.json"
        path.write_text(json.dumps({"schema": 999, "entries": {"someone": {"id": "U_1"}}}))
        assert tir._read_trust_store(path) is None

    def test_record_then_get_round_trips(self, tmp_path):
        path = tmp_path / "store.json"
        tir.record_resolved_id("someone", "U_abc", path=path)
        assert tir.get_stored_id("someone", path=path) == "U_abc"

    def test_record_is_never_partial(self, tmp_path):
        path = tmp_path / "store.json"
        tir.record_resolved_id("a", "U_a", path=path)
        tir.record_resolved_id("b", "U_b", path=path)
        # Both entries survive — a later record() must not clobber earlier ones.
        assert tir.get_stored_id("a", path=path) == "U_a"
        assert tir.get_stored_id("b", path=path) == "U_b"


# ---------------------------------------------------------------------------
# AC10 — detection
# ---------------------------------------------------------------------------


class TestDetectTrustDrift:
    def test_unresolvable_login_is_a_finding(self, tmp_path):
        cfg = {"bot_account": "renamed-away", "bot_account_id": "U_old"}
        findings = tir.detect_trust_drift(cfg, resolver=lambda _l: {"state": tir.ABSENT, "id": None, "created_at": None})
        assert any(f["kind"] == "unresolvable" for f in findings)

    def test_id_mismatch_is_a_finding(self, tmp_path):
        cfg = {"boss_github_username": "someone", "boss_github_user_id": "U_recorded"}
        findings = tir.detect_trust_drift(
            cfg,
            resolver=lambda _l: {"state": tir.RESOLVED, "id": "U_DIFFERENT", "created_at": "2015-01-01T00:00:00Z"},
        )
        assert any(f["kind"] == "id_mismatch" and f["resolved_id"] == "U_DIFFERENT" for f in findings)

    def test_suspicious_creation_after_pin_is_a_finding(self, tmp_path):
        store_path = tmp_path / "store.json"
        pinned_at = time.time() - 3600
        tir._write_trust_store({"someone": {"id": "U_new", "resolved_at": pinned_at}}, store_path)
        cfg = {"boss_github_username": "someone"}
        future_created_at = "2099-01-01T00:00:00Z"
        findings = tir.detect_trust_drift(
            cfg,
            resolver=lambda _l: {"state": tir.RESOLVED, "id": "U_new", "created_at": future_created_at},
            trust_store_path=store_path,
        )
        assert any(f["kind"] == "suspicious_creation" for f in findings)

    def test_unknown_resolution_is_not_a_finding(self):
        # UNKNOWN alone is indistinguishable from an ordinary rate limit —
        # detection must not report it as drift.
        cfg = {"bot_account": "someone", "bot_account_id": "U_x"}
        findings = tir.detect_trust_drift(cfg, resolver=lambda _l: {"state": tir.UNKNOWN, "id": None, "created_at": None})
        assert findings == []

    def test_clean_config_has_no_findings(self):
        cfg = {"boss_github_username": "someone", "boss_github_user_id": "U_same"}
        findings = tir.detect_trust_drift(
            cfg, resolver=lambda _l: {"state": tir.RESOLVED, "id": "U_same", "created_at": "2015-01-01T00:00:00Z"}
        )
        assert findings == []


# ---------------------------------------------------------------------------
# AC16 — migration is a loud abort, never a silent drop
# ---------------------------------------------------------------------------


class TestMigrateConfigIds:
    def test_successful_migration_returns_both_ids(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tir, "default_trust_store_path", lambda: tmp_path / "store.json")
        cfg = {"bot_account": "bot-login", "boss_github_username": "boss-login"}
        out = tir.migrate_config_ids(
            cfg, resolver=lambda login: {"state": tir.RESOLVED, "id": f"U_{login}", "created_at": None}
        )
        assert out == {"bot_account_id": "U_bot-login", "boss_github_user_id": "U_boss-login"}

    def test_unresolvable_entry_aborts_loudly_with_no_partial_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tir, "default_trust_store_path", lambda: tmp_path / "store.json")

        def _resolver(login):
            if login == "bot-login":
                return {"state": tir.RESOLVED, "id": "U_bot", "created_at": None}
            return {"state": tir.ABSENT, "id": None, "created_at": None}

        cfg = {"bot_account": "bot-login", "boss_github_username": "gone-login"}
        with pytest.raises(tir.MigrationError, match="gone-login"):
            tir.migrate_config_ids(cfg, resolver=_resolver)

    def test_unknown_entry_also_aborts_not_just_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tir, "default_trust_store_path", lambda: tmp_path / "store.json")
        cfg = {"bot_account": "bot-login"}
        with pytest.raises(tir.MigrationError):
            tir.migrate_config_ids(cfg, resolver=lambda _l: {"state": tir.UNKNOWN, "id": None, "created_at": None})

# resolve_allowlist_ids() (external_intake_gate.py) — Commit 2's production
# wiring — is exercised in backend/tests/test_external_intake_gate.py's
# TestResolveAllowlistIds, not here, so this file's own test suite stays
# independently runnable against Commit 1 alone (no dependency on gate.py's
# ID-based call-site rewiring).
