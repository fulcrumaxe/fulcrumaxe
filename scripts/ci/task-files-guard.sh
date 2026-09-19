#!/usr/bin/env bash
# scripts/ci/task-files-guard.sh — CI guard for the epics/ task-file tree.
#
# Thin wrapper so a CI workflow has one stable path to depend on. All the
# actual work (schema validation, dependency-cycle detection, unknown-ref
# detection) lives in scripts/task-inventory.py --check; this script just
# runs it against the repo root and propagates its exit code.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

exec python3 "$REPO_ROOT/scripts/task-inventory.py" --check --root "$REPO_ROOT"
