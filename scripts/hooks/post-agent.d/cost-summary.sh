#!/usr/bin/env bash
# scripts/hooks/post-agent.d/cost-summary.sh
#
# Updates ~/.autonomous-forever-state/cost_summary.json (or $AUTONOMOUS_TEAM_STATE_DIR)
# after each agent run. Sourced by post-agent-hook.sh — variables INPUT_TOKENS,
# OUTPUT_TOKENS, CACHE_READ_TOKENS, and REPO_ROOT are expected to be set by the caller.
#
# cache_read_tokens are FREE under Anthropic pricing — excluded from billable totals.
# Only input_tokens + output_tokens are recorded.

STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-${HOME}/.autonomous-forever-state}"

if command -v python3 >/dev/null 2>&1; then
    python3 "${REPO_ROOT}/backend/fleet/cost_summary.py" record \
        --state-dir "${STATE_DIR}" \
        --input-tokens "${INPUT_TOKENS:-0}" \
        --output-tokens "${OUTPUT_TOKENS:-0}" \
        --cache-read-tokens "${CACHE_READ_TOKENS:-0}" \
        2>/dev/null || true
fi
