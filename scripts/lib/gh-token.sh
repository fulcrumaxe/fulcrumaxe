#!/usr/bin/env bash
# scripts/lib/gh-token.sh — source this to set GH_TOKEN from GitHub App if .pem is present.
#
# If the App key exists, mints an installation token and exports GH_TOKEN.
# Otherwise leaves existing GH_TOKEN/GITHUB_TOKEN unchanged and exits silently.
#
# Usage:
#   source scripts/lib/gh-token.sh

_GH_TOKEN_PEM="${GITHUB_APP_KEY_PATH:-${HOME}/.autonomous-forever-state/secrets/github-app.pem}"
_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -f "$_GH_TOKEN_PEM" ]]; then
  _APP_TOKEN=$(python3 "$_REPO_ROOT/backend/github_app_auth.py" token 2>/dev/null)
  _APP_EXIT=$?
  if [[ $_APP_EXIT -eq 0 && -n "$_APP_TOKEN" ]]; then
    export GH_TOKEN="$_APP_TOKEN"
    echo "[gh-token] installation token active (15000/hr graphql)" >&2
  else
    echo "[gh-token] app auth failed (exit=$_APP_EXIT) — keeping existing token" >&2
  fi
else
  echo "[gh-token] no app key at $_GH_TOKEN_PEM — using existing GH_TOKEN/GITHUB_TOKEN" >&2
fi

unset _GH_TOKEN_PEM _REPO_ROOT _APP_TOKEN _APP_EXIT
