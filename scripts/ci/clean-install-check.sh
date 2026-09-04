#!/usr/bin/env bash
# Clean-install guard for undeclared backend dependencies (D#1617).
#
# Reproduces CI's clean-install behavior locally: builds a throwaway venv,
# installs ONLY the given requirements file, and runs the backend
# import-smoke check inside that venv (using the venv's own interpreter, not
# ambient python3). This catches a dep that's present in the ambient/dev
# environment but missing from requirements.txt before it reaches CI.
#
# Usage: scripts/ci/clean-install-check.sh [path/to/requirements.txt]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REQ_FILE="${1:-$REPO_ROOT/requirements.txt}"

VENV="$(mktemp -d)"
trap 'rm -rf "$VENV"' EXIT

echo "clean-install-check: building clean venv from ${REQ_FILE}..."
if ! python3 -m venv "$VENV/env" 2>/dev/null || [ ! -x "$VENV/env/bin/pip" ]; then
  # Some minimal environments ship python3-venv without ensurepip (no
  # python3-venv system package, no sudo to install it). Fall back to
  # bootstrapping pip directly rather than failing the whole check.
  echo "clean-install-check: ensurepip unavailable, bootstrapping pip via get-pip.py..."
  python3 -m venv --without-pip "$VENV/env"
  curl -sS -o "$VENV/get-pip.py" https://bootstrap.pypa.io/get-pip.py
  "$VENV/env/bin/python" "$VENV/get-pip.py" --quiet
fi
"$VENV/env/bin/pip" install --quiet --disable-pip-version-check -r "$REQ_FILE"

echo "clean-install-check: running backend import-smoke inside the clean venv..."
cd "$REPO_ROOT"
if "$VENV/env/bin/python" "$REPO_ROOT/scripts/ci/backend-import-smoke.py"; then
  echo "clean-install-check: PASS — backend imports cleanly from ${REQ_FILE}"
  exit 0
else
  status=$?
  echo "clean-install-check: FAIL — backend import-smoke failed against ${REQ_FILE}"
  exit "$status"
fi
