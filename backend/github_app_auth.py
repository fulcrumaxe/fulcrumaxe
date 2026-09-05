#!/usr/bin/env python3
"""GitHub App authentication client.

Mints RS256 JWTs and exchanges them for installation access tokens.
Installation tokens have a 15000/hr GraphQL rate limit vs 5000/hr for user PATs.

Config (env vars with constant fallbacks):
  GITHUB_APP_ID            — App ID (default: 3701553)
  GITHUB_INSTALLATION_ID   — Installation ID (default: 132036881)
  GITHUB_APP_KEY_PATH      — Path to .pem (default: ~/.autonomous-forever-state/secrets/github-app.pem)
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import jwt  # PyJWT >= 2.7.0

# ── Config ────────────────────────────────────────────────────────────────────

APP_ID = int(os.environ.get("GITHUB_APP_ID", "3701553"))
INSTALLATION_ID = int(os.environ.get("GITHUB_INSTALLATION_ID", "132036881"))
KEY_PATH = Path(
    os.environ.get(
        "GITHUB_APP_KEY_PATH",
        os.path.expanduser("~/.autonomous-forever-state/secrets/github-app.pem"),
    )
)

# ── In-memory token cache ─────────────────────────────────────────────────────

_cache: dict = {}  # keys: "token", "expires_at" (ISO8601 string)


def _key_path() -> Path:
    return KEY_PATH


def _mint_jwt() -> str:
    """Read the PEM key and sign an RS256 JWT valid for 10 minutes."""
    pem = _key_path().read_bytes()
    now = int(time.time())
    payload = {
        "iat": now - 60,   # issued 60s ago to handle clock skew
        "exp": now + 600,  # 10-minute TTL
        "iss": str(APP_ID),
    }
    return jwt.encode(payload, pem, algorithm="RS256")


def get_installation_token() -> str:
    """Return a cached installation token, refreshing when <5 min TTL remains."""
    global _cache

    if _cache:
        exp_str = _cache.get("expires_at", "")
        if exp_str:
            try:
                exp = datetime.fromisoformat(exp_str.rstrip("Z")).replace(
                    tzinfo=timezone.utc
                )
                remaining = (exp - datetime.now(timezone.utc)).total_seconds()
                if remaining > 300:
                    return _cache["token"]
            except ValueError:
                pass

    token = _mint_jwt()
    url = (
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    )
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    _cache = {"token": data["token"], "expires_at": data["expires_at"]}
    return _cache["token"]


def gh_env() -> dict:
    """Return env dict with GH_TOKEN set to the current installation token."""
    return {"GH_TOKEN": get_installation_token()}


def clear_cache() -> None:
    global _cache
    _cache = {}


# ── CLI ───────────────────────────────────────────────────────────────────────


def _graceful_check() -> bool:
    """Return True if .pem exists; print message and exit(2) if not."""
    if not _key_path().exists():
        print("no app key — falling back to user PAT", file=sys.stderr)
        sys.exit(2)
    return True


def _cmd_token(_args) -> None:
    _graceful_check()
    tok = get_installation_token()
    # Print token but never log it to debug/info streams
    print(tok)


def _cmd_rate_limit(_args) -> None:
    _graceful_check()
    installation_token = get_installation_token()
    url = "https://api.github.com/rate_limit"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {installation_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
    graphql = data["resources"]["graphql"]
    print(f"graphql: {graphql['remaining']}/{graphql['limit']}")


def _cmd_clear_cache(_args) -> None:
    clear_cache()
    print("cache cleared", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub App auth client")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("token", help="Print an installation access token")
    sub.add_parser("rate-limit", help="Print remaining GraphQL rate limit")
    sub.add_parser("clear-cache", help="Clear the in-memory token cache")

    args = parser.parse_args()

    dispatch = {
        "token": _cmd_token,
        "rate-limit": _cmd_rate_limit,
        "clear-cache": _cmd_clear_cache,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
