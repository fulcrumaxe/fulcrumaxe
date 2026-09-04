#!/usr/bin/env bash
# scripts/hooks/post-agent.d/anomaly-check.sh
#
# Runs the stat regression detector after each agent completes.
# Flags any metric that swings >10x (configurable per metric) between the
# two most recent readings in stats.duckdb.
#
# Environment expected from post-agent-hook.sh:
#   REPO_ROOT     — absolute path to repository root
#   _REPO         — "owner/name" slug for team-log comments
#
# Non-fatal: exits 0 on any error so it never blocks the hook pipeline.

REPO="${_REPO:-}"

if command -v python3 >/dev/null 2>&1; then
    python3 -m backend.stats.anomaly_detector \
        --repo "${REPO}" \
        2>/dev/null || true
fi
