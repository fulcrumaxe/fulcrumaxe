#!/usr/bin/env bash
# scripts/muse-subagent-stop.sh — record a Muse wait-ready stop (Muse twin of
# scripts/subagent-stop-hook.sh).
#
# Muse has no SubagentStop hook: the stop signal is the lane consumer seeing
# agent_exit (or the wait-ready tool result). That consumer pipes BOTH sides
# of the join to this script on stdin:
#
#   {"spawn_accept": {...}, "wait_result": {...}, "wait_is_error": bool}
#
# This script resolves the flat fields via scripts/lib/muse_subagent_payload.py
# (join key = spawn task name = assembler --event-id, looked up in the spawn
# registry — parent-side truth, never child content) and records completion
# through the SAME scripts/post-agent-hook.sh the Claude twin calls, so
# downstream rows, outbox, and cost accounting stay identical across lanes.
#
# Token counts are unknown at Muse stop time (a wait-ready carries the
# summary, not usage) and resolve to 0 — the established unknown-sentinel on
# this path (same fallback the Claude twin applies when the envelope omits
# tokens_used). Backfill from the child session log (wait_result.evidence_refs)
# replaces them later.
#
# Tokens land via backend/muse_backfill.py from the wait_result evidence_refs
# (child session log); that step runs in both modes since it only touches
# the state DB, never git.
#
# Exit 0 always: a telemetry failure must never fail the loop step that
# reports the stop. MUSE_STOP_DRY_RUN=1 with MUSE_STOP_ARGS_FILE=<path>
# captures the post-agent-hook args as JSON instead of calling it (test seam,
# mirroring SUBAGENT_STOP_DRY_RUN).
#
# Usage:
#   echo "$STOP_JSON" | bash scripts/muse-subagent-stop.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

STDIN_JSON="$(cat)"
if [[ -z "$STDIN_JSON" ]]; then
  echo "[muse-subagent-stop] empty stdin — nothing to record" >&2
  exit 0
fi

FLAT=$(python3 "$SCRIPT_DIR/lib/muse_subagent_payload.py" <<< "$STDIN_JSON" 2>/dev/null) || FLAT=""
if [[ -z "$FLAT" ]]; then
  echo "[muse-subagent-stop] payload resolve failed — nothing to record" >&2
  exit 0
fi

AGENT_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('agent_id') or '')" <<< "$FLAT" 2>/dev/null)
if [[ -z "$AGENT_ID" ]]; then
  echo "[muse-subagent-stop] unresolvable join key — nothing to record" >&2
  exit 0
fi

ROLE=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('role') or '')" <<< "$FLAT" 2>/dev/null)
VERDICT=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('verdict') or 'unknown')" <<< "$FLAT" 2>/dev/null)
DISCUSSION=$(python3 -c "import json,sys; d=json.load(sys.stdin).get('discussion'); print('' if d is None else d)" <<< "$FLAT" 2>/dev/null)
INPUT_TOK=$(python3 -c "import json,sys; v=json.load(sys.stdin).get('input_tok'); print(v if isinstance(v,int) and v>=0 else 0)" <<< "$FLAT" 2>/dev/null)
OUTPUT_TOK=$(python3 -c "import json,sys; v=json.load(sys.stdin).get('output_tok'); print(v if isinstance(v,int) and v>=0 else 0)" <<< "$FLAT" 2>/dev/null)

POST_HOOK_ARGS=(
  --role "$ROLE"
  --verdict "$VERDICT"
  --event-id "$AGENT_ID"
  --input-tokens "$INPUT_TOK"
  --output-tokens "$OUTPUT_TOK"
  --self-observed "false"
)
[[ -n "$DISCUSSION" ]] && POST_HOOK_ARGS+=(--discussion "$DISCUSSION")

if [[ -n "${MUSE_STOP_DRY_RUN:-}" && -n "${MUSE_STOP_ARGS_FILE:-}" ]]; then
  python3 -c "
import json,sys
argv = sys.argv[1:]
d = {}
i = 0
while i < len(argv):
    if argv[i].startswith('--') and i + 1 < len(argv) and not argv[i+1].startswith('--'):
        d[argv[i][2:].replace('-','_')] = argv[i+1]; i += 2
    elif argv[i].startswith('--'):
        d[argv[i][2:].replace('-','_')] = True; i += 1
    else:
        i += 1
with open('${MUSE_STOP_ARGS_FILE}', 'w') as f:
    json.dump(d, f, indent=2)
" "${POST_HOOK_ARGS[@]}" 2>/dev/null || echo "[muse-subagent-stop] dry-run args write failed" >&2
else
  bash "$SCRIPT_DIR/post-agent-hook.sh" "${POST_HOOK_ARGS[@]}" 2>/dev/null || \
    echo "[muse-subagent-stop] post-agent-hook failed for $AGENT_ID (non-fatal)" >&2
fi

# Token backfill from the child session log (runs in both modes: it only
# touches the state DB, never git). Best-effort, stderr-only.
EVIDENCE_REFS=$(python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin).get('evidence_refs') or []))" <<< "$FLAT" 2>/dev/null)
if [[ -n "$EVIDENCE_REFS" ]]; then
  BACKFILL_ARGS=(--agent-id "$AGENT_ID")
  while IFS= read -r ref; do
    [[ -n "$ref" ]] && BACKFILL_ARGS+=(--ref "$ref")
  done <<< "$EVIDENCE_REFS"
  PYTHONPATH="$REPO_ROOT" python3 -m backend.muse_backfill backfill "${BACKFILL_ARGS[@]}" >/dev/null 2>&1 || true
fi
exit 0
