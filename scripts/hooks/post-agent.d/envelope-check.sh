#!/usr/bin/env bash
# scripts/hooks/post-agent.d/envelope-check.sh — envelope fabrication
# detector (D#1791 PR 2).
#
# Detector, not gate: SubagentStop fires after the Agent() result has
# already reached whoever spawned the agent, so this can only leave a
# finding for a later reader — it never changes VERDICT, never blocks, and
# (matching anomaly-check.sh's contract) never exits non-zero.
#
# Two ways this runs:
#   1. Sourced by post-agent-hook.sh (production) — REPO_ROOT, ROLE,
#      DISCUSSION, PR, TOOL_USES, CONTENT are already ambient shell vars
#      from the caller.
#   2. Invoked standalone (`bash scripts/hooks/post-agent.d/envelope-check.sh`,
#      the way this Discussion's acceptance criteria exercise it) — the same
#      names are read from the environment instead, and REPO_ROOT falls back
#      to a path computed from this file's own location.
#
# CONTENT carries the raw AGENT_OUTPUT envelope text only when the caller
# has it (post-agent-hook.sh's --content). That is empty on today's default
# SubagentStop path: no consumer threads a live envelope's `sources` array
# that far yet — doing so would touch scripts/lib/subagent_payload.py and
# scripts/subagent-stop-hook.sh, both deliberately out of scope for this PR
# (see the PR body for why). Empty CONTENT resolves sources_count to 0,
# which check 1 reads as "no finding" — never as an accusation from data
# this hook never actually had.
set -uo pipefail

_EC_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$_EC_HERE/../../.." && pwd)}"
ROLE="${ROLE:-unknown}"
DISCUSSION="${DISCUSSION:-}"
PR="${PR:-}"
TOOL_USES="${TOOL_USES:-}"
CONTENT="${CONTENT:-}"

# Pull sources_count and any claimed Discussion-comment artifact out of
# CONTENT, when present. Parses only the fenced AGENT_OUTPUT JSON block (or,
# failing that, the whole of CONTENT as a JSON object) and hands the PARSED
# dict to backend.envelope_check's own extract_sources_count /
# extract_claimed_artifact — never scans raw surrounding prose. Scanning raw
# text would let an envelope that merely *mentions* a comment permalink,
# without asserting it as a machine-readable field, trigger a network call,
# an audit row, and a team-log line over an incidental reference. Reusing
# the module's functions (rather than re-deriving this here) also means the
# hook's notion of "claimed" can never drift looser than what
# resolve_claimed_artifact actually recognises.
_EC_SIGNALS=$(python3 - "$CONTENT" "$REPO_ROOT" <<'PYEOF' 2>/dev/null
import json
import re
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
repo_root = sys.argv[2] if len(sys.argv) > 2 else ""
if repo_root and repo_root not in sys.path:
    sys.path.insert(0, repo_root)
from backend.envelope_check import extract_claimed_artifact, extract_sources_count  # noqa: E402

count = 0
artifact = ""
if raw:
    m = re.search(
        r'<!--\s*AGENT_OUTPUT\s*-->\s*```json\s*(.*?)\s*```\s*<!--\s*/AGENT_OUTPUT\s*-->',
        raw,
        re.DOTALL,
    )
    candidate = m.group(1) if m else raw
    try:
        env = json.loads(candidate)
    except Exception:
        env = None
    if isinstance(env, dict):
        count = extract_sources_count(env)
        artifact = extract_claimed_artifact(env) or ""

print(json.dumps({"sources_count": count, "claimed_artifact": artifact}))
PYEOF
) || _EC_SIGNALS='{"sources_count": 0, "claimed_artifact": ""}'
[[ -z "$_EC_SIGNALS" ]] && _EC_SIGNALS='{"sources_count": 0, "claimed_artifact": ""}'

_EC_SOURCES_COUNT=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('sources_count', 0))" "$_EC_SIGNALS" 2>/dev/null) || _EC_SOURCES_COUNT=0
_EC_ARTIFACT=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('claimed_artifact', ''))" "$_EC_SIGNALS" 2>/dev/null) || _EC_ARTIFACT=""

_EC_ARGS=(--tool-uses "$TOOL_USES" --sources-count "$_EC_SOURCES_COUNT" --json --record
          --role "$ROLE" --discussion "$DISCUSSION" --pr "$PR")
[[ -n "$_EC_ARTIFACT" ]] && _EC_ARGS+=(--claimed-artifact "$_EC_ARTIFACT")

python3 "$REPO_ROOT/backend/envelope_check.py" "${_EC_ARGS[@]}" || true
