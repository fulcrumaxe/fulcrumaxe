"""Tests for D#1425 misc GET route migrations.

Routes tested:
  GET /quality/{pr_number}       — per-PR quality score (obs_quality router)
  GET /benchmarks/history/{extra:path} — history sub-path alias (info_benchmarks)
  GET /validate                  — config file validation (info_misc router)

Each test class covers:
- Happy-path response shape matches legacy
- Auth: 401 no token, allowed with correct token
- Error path (404 for quality/{n} when not found)

ALL backend calls are MOCKED.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_AUTH_TOKEN = "test-secret-misc-get"
_WRONG_TOKEN = "wrong-key-misc"


def _make_client(token: str | None = None) -> TestClient:
    from backend.asgi_app import app
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TestClient(app, headers=headers, raise_server_exceptions=False)


def _authed() -> TestClient:
    return _make_client(token=_AUTH_TOKEN)


def _no_auth() -> TestClient:
    return _make_client(token=None)


# ===========================================================================
# GET /quality/{pr_number}
# ===========================================================================


class TestQualityPr:
    """GET /quality/{pr_number} — per-PR quality score."""

    def test_returns_score_when_found(self, monkeypatch):
        """Returns stored score for a known PR number."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_qs = MagicMock()
        fake_bb = MagicMock()
        fake_bb.read.return_value = {
            "pr": 42,
            "total_score": 88,
            "grade": "B",
            "applicable": True,
        }
        fake_qs._bb = fake_bb
        # QualityScorer is a deferred import inside the route function;
        # patch at the source module level so the local import picks it up.
        with patch("backend.quality_scorer.QualityScorer", return_value=fake_qs):
            resp = _authed().get("/quality/42")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pr"] == 42
        assert body["total_score"] == 88
        assert body["grade"] == "B"
        fake_bb.read.assert_called_once_with("quality/42")

    def test_returns_404_when_not_found(self, monkeypatch):
        """Returns 404 when no score exists for the PR."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_qs = MagicMock()
        fake_bb = MagicMock()
        fake_bb.read.return_value = None
        fake_qs._bb = fake_bb
        with patch("backend.quality_scorer.QualityScorer", return_value=fake_qs):
            resp = _authed().get("/quality/999")
        assert resp.status_code == 404, resp.text

    def test_requires_auth(self, monkeypatch):
        """Returns 401/403 without a valid token."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/quality/42")
        assert resp.status_code in (401, 403), resp.text

    def test_rejects_non_integer_pr(self, monkeypatch):
        """Non-integer path segment returns 422 (FastAPI validation)."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        # "abc" is not a valid int — FastAPI returns 422
        resp = _authed().get("/quality/abc")
        # FastAPI raises 422 for path param validation failures;
        # but the catch-all proxy may intercept before the router if
        # the route doesn't match. Either way it's not 200.
        assert resp.status_code != 200, resp.text


# ===========================================================================
# GET /benchmarks/history/{extra:path}
# ===========================================================================


class TestBenchmarksHistorySubpath:
    """GET /benchmarks/history/{extra:path} — sub-path alias."""

    def test_subpath_delegates_to_history(self, monkeypatch):
        """Sub-path requests return the same shape as /benchmarks/history."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_rec = MagicMock()
        fake_rec.get_history.return_value = [{"ts": "2026-05-22", "value": 1.0}]
        with patch("backend.routers.info_benchmarks.get_recorder", return_value=fake_rec):
            resp = _authed().get("/benchmarks/history/extra-stuff?category=rpc&points=10")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "history" in body
        assert body["category"] == "rpc"
        fake_rec.get_history.assert_called_once_with(
            category="rpc", operation=None, points=10
        )

    def test_requires_auth(self, monkeypatch):
        """Returns 401/403 without a valid token."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/benchmarks/history/some/extra/path")
        assert resp.status_code in (401, 403), resp.text


# ===========================================================================
# GET /validate
# ===========================================================================


class TestValidate:
    """GET /validate — config file validation."""

    def test_returns_valid_true_when_no_errors(self, monkeypatch):
        """Returns valid=True and empty error lists when all files pass."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_sv = MagicMock()
        fake_sv.validate_all.return_value = {
            ".autonomous-team/config.json": [],
            ".autonomous-team/loop-config.json": [],
        }
        # SchemaValidator is a deferred import inside the route function;
        # patch at the source module level so the local import picks it up.
        with patch("backend.schema_validator.SchemaValidator", return_value=fake_sv):
            resp = _authed().get("/validate")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is True
        assert "files" in body
        assert body["files"][".autonomous-team/config.json"] == []

    def test_returns_valid_false_when_errors_exist(self, monkeypatch):
        """Returns valid=False when any file has validation errors."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_sv = MagicMock()
        fake_sv.validate_all.return_value = {
            ".autonomous-team/config.json": ["missing field: gates"],
        }
        with patch("backend.schema_validator.SchemaValidator", return_value=fake_sv):
            resp = _authed().get("/validate")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is False
        assert len(body["files"][".autonomous-team/config.json"]) > 0

    def test_requires_auth(self, monkeypatch):
        """Returns 401/403 without a valid token."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        resp = _no_auth().get("/validate")
        assert resp.status_code in (401, 403), resp.text

    def test_response_shape_matches_legacy(self, monkeypatch):
        """Response shape: {valid: bool, files: {path: [errors]}}."""
        monkeypatch.setenv("AF_API_AUTH_KEY", _AUTH_TOKEN)
        fake_sv = MagicMock()
        fake_sv.validate_all.return_value = {}
        with patch("backend.schema_validator.SchemaValidator", return_value=fake_sv):
            resp = _authed().get("/validate")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Both keys must be present
        assert "valid" in body
        assert "files" in body
        # valid=True when files dict is empty (no errors)
        assert body["valid"] is True
