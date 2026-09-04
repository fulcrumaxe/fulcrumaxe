"""Tests for backend/github_app_auth.py."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.github_app_auth as app_auth  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_rsa_key():
    """Generate a fresh RSA key for tests (avoids depending on a real .pem)."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture()
def rsa_pem(tmp_path) -> Path:
    pem_data = _make_rsa_key()
    pem_file = tmp_path / "github-app.pem"
    pem_file.write_bytes(pem_data)
    return pem_file


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the in-memory token cache between tests."""
    app_auth.clear_cache()
    yield
    app_auth.clear_cache()


# ── test_mint_jwt_format ──────────────────────────────────────────────────────


def test_mint_jwt_format(rsa_pem, monkeypatch):
    """JWT must use RS256 and carry iat/exp/iss claims."""
    import jwt

    monkeypatch.setattr(app_auth, "KEY_PATH", rsa_pem)
    token = app_auth._mint_jwt()
    # Decode without verification so we can inspect headers/claims
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"

    claims = jwt.decode(token, options={"verify_signature": False})
    assert "iat" in claims
    assert "exp" in claims
    assert "iss" in claims
    assert claims["iss"] == str(app_auth.APP_ID)
    assert claims["exp"] - claims["iat"] <= 660  # 10min + 60s back-date


# ── test_token_cache_returns_cached_when_fresh ────────────────────────────────


def test_token_cache_returns_cached_when_fresh(monkeypatch):
    """get_installation_token() returns cached token when TTL > 5 minutes."""
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    app_auth._cache = {
        "token": "ghs_cached_token",
        "expires_at": future.isoformat(),
    }

    # If the cache is fresh, no HTTP call should occur
    with patch("backend.github_app_auth.urllib.request.urlopen") as mock_open:
        tok = app_auth.get_installation_token()
        mock_open.assert_not_called()

    assert tok == "ghs_cached_token"


# ── test_token_cache_refreshes_when_near_expiry ───────────────────────────────


def test_token_cache_refreshes_when_near_expiry(rsa_pem, monkeypatch):
    """get_installation_token() fetches a new token when TTL ≤ 5 minutes."""
    monkeypatch.setattr(app_auth, "KEY_PATH", rsa_pem)
    # Cache a token expiring in 3 minutes — below the 5-min refresh threshold
    near_expiry = datetime.now(timezone.utc) + timedelta(minutes=3)
    app_auth._cache = {
        "token": "ghs_old_token",
        "expires_at": near_expiry.isoformat(),
    }

    new_exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response_body = json.dumps(
        {"token": "ghs_new_token", "expires_at": new_exp}
    ).encode()

    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("backend.github_app_auth.urllib.request.urlopen", return_value=mock_resp):
        tok = app_auth.get_installation_token()

    assert tok == "ghs_new_token"
    assert app_auth._cache["token"] == "ghs_new_token"


# ── test_gh_env_returns_token_dict ────────────────────────────────────────────


def test_gh_env_returns_token_dict(rsa_pem, monkeypatch):
    """gh_env() must return a dict with GH_TOKEN key."""
    monkeypatch.setattr(app_auth, "KEY_PATH", rsa_pem)
    new_exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response_body = json.dumps(
        {"token": "ghs_env_token", "expires_at": new_exp}
    ).encode()

    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("backend.github_app_auth.urllib.request.urlopen", return_value=mock_resp):
        env = app_auth.gh_env()

    assert "GH_TOKEN" in env
    assert env["GH_TOKEN"] == "ghs_env_token"


# ── test_cli_token_subcommand ─────────────────────────────────────────────────


def test_cli_token_subcommand(rsa_pem, monkeypatch, capsys):
    """CLI `token` subcommand prints the token to stdout."""
    monkeypatch.setattr(app_auth, "KEY_PATH", rsa_pem)
    new_exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response_body = json.dumps(
        {"token": "ghs_cli_token", "expires_at": new_exp}
    ).encode()

    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("backend.github_app_auth.urllib.request.urlopen", return_value=mock_resp):
        with patch("sys.argv", ["github_app_auth.py", "token"]):
            app_auth.main()

    captured = capsys.readouterr()
    assert "ghs_cli_token" in captured.out
    # Token must NOT appear on stderr (no accidental logging)
    assert "ghs_cli_token" not in captured.err
