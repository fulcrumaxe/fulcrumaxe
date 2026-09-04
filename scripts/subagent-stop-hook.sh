#!/usr/bin/env bash
# subagent-stop-hook.sh — SubagentStop hook wrapper for Claude Code.
#
# INPUT CONTRACT (stdin):
#   JSON object emitted by Claude Code when a subagent finishes. All six
#   fields below are ones Claude Code actually sends (D#2238 — the previous
#   version of this contract only listed four, and the hook read none of
#   the two that carry the subagent's own identity and output):
#     {
#       "hook_event_name": "SubagentStop",
#       "session_id": "<parent session id>",
#       "transcript_path": "/path/to/parent/transcript.jsonl",
#       "cwd": "/path/to/worktree",
#       "agent_id": "<subagent id>",
#       "agent_type": "<role>",
#       "last_assistant_message": "<subagent's final message text>"
#     }
#   `session_id` and `transcript_path` are the PARENT session's — identical
#   across every subagent spawned from one session. The subagent's own role,
#   verdict, and output live in `agent_type` / `agent_id` /
#   `last_assistant_message` instead. See scripts/lib/subagent_payload.py
#   for how these are resolved into an agent_run row.
#
# OUTPUT CONTRACT:
#   Always exits 0. Never blocks subagent completion.
#   Calls post-agent-hook.sh with real fields resolved from the payload —
#   the <!-- AGENT_OUTPUT --> envelope in last_assistant_message when
#   present, agent_type as a fallback role, or verdict=unknown when neither
#   is available.
#
# Idempotency:
#   event-id is derived as "<role>-<discussion>-<agent_id>" when agent_id is
#   present (D#2238 — this is what actually disambiguates concurrent
#   subagents sharing one parent session_id), falling back to
#   "<role>-<discussion>-<session_id>-<nanos>" when it is not.
#   post-agent-hook.sh's hook_event_init is a no-op on duplicate event-ids.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# SUBAGENT_STOP_REPO_ROOT_OVERRIDE is a test-only seam (unset in production):
# find_own_usage()'s ~/.claude/projects/<slug(repo_root)>/... lookup needs the
# MAIN checkout's path to match the slug Claude Code actually used when it
# wrote a subagent's own transcript -- a worktree replaying a real captured
# payload for D#2238 acceptance item 6 has a different $SCRIPT_DIR/.. than
# the main checkout the payload was originally captured under.
REPO_ROOT="${SUBAGENT_STOP_REPO_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# ── 1. Parse stdin JSON ────────────────────────────────────────────────────────
STDIN_JSON=$(cat)

# ---- 0. Debug: write stdin to /tmp for inspection (opt-in only) ----
# Set SUBAGENT_STOP_DEBUG=1 to enable. Never runs unconditionally.
if [[ -n "${SUBAGENT_STOP_DEBUG:-}" ]]; then
  _DEBUG_TS=$(date +%s)
  _DEBUG_FILE="/tmp/subagent-stop-debug-${_DEBUG_TS}.json"
  python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    with open(sys.argv[2], 'w') as f:
        json.dump(data, f, indent=2)
except Exception:
    pass
  " "$STDIN_JSON" "$_DEBUG_FILE" 2>/dev/null || true
fi

# ── 2. Resolve role / verdict / tokens from the payload ────────────────────────
# All the parsing logic lives in scripts/lib/subagent_payload.py (mirrors the
# scripts/lib/transcript_event_id.py precedent) so it's unit-testable outside
# a shell heredoc. One call resolves the payload to a JSON object; a second
# flattens it to unit-separator-delimited fields for `read` to unpack — cheap,
# and avoids any shell-quoting of agent-authored content (subagent_payload.py
# already sanitized everything here; this step never re-parses raw payload
# text). subagent_payload.py itself never raises, so on any failure here
# (e.g. python3 missing) the field list degrades to sixteen empty strings,
# which downstream still resolves to the same role=unknown/verdict=unknown
# skip path this script has always had for unreadable input.
FIELD_LIST="session_id transcript_path agent_id role verdict discussion pr files self_observed input_tokens output_tokens cache_read_tokens cache_write_tokens cache_creation_tokens first_write_turn parse_ok own_transcript_path"
FLAT=$(python3 "$SCRIPT_DIR/lib/subagent_payload.py" "$REPO_ROOT" <<< "$STDIN_JSON" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    d = {}
fields = sys.argv[1].split()
def fmt(k):
    v = d.get(k)
    if k in ('self_observed', 'parse_ok'):
        return 'true' if v else 'false'
    if k == 'first_write_turn':
        return '' if v is None else str(int(v))
    return '' if v is None else str(v)
print('\x1f'.join(fmt(k) for k in fields))
" "$FIELD_LIST" 2>/dev/null)
if [[ -z "$FLAT" ]]; then
  # Seventeen empty fields — same count as FIELD_LIST — so `read` below never
  # runs short and leaves a trailing variable unset under `set -u`.
  FLAT=$(printf '\x1f%.0s' $(seq 1 16))
fi
IFS=$'\x1f' read -r SESSION_ID TRANSCRIPT_PATH AGENT_ID ROLE VERDICT DISCUSSION PR FILES \
  SELF_OBSERVED INPUT_TOKENS OUTPUT_TOKENS CACHE_READ_TOKENS CACHE_WRITE_TOKENS \
  CACHE_CREATION_TOKENS FIRST_WRITE_TURN PARSE_OK OWN_TRANSCRIPT_PATH <<< "$FLAT"
SESSION_ID="${SESSION_ID:-unknown}"
ROLE="${ROLE:-unknown}"
VERDICT="${VERDICT:-unknown}"
INPUT_TOKENS="${INPUT_TOKENS:-0}"
OUTPUT_TOKENS="${OUTPUT_TOKENS:-0}"
CACHE_READ_TOKENS="${CACHE_READ_TOKENS:-0}"
CACHE_WRITE_TOKENS="${CACHE_WRITE_TOKENS:-0}"
CACHE_CREATION_TOKENS="${CACHE_CREATION_TOKENS:-0}"
SELF_OBSERVED="${SELF_OBSERVED:-false}"
PARSE_OK="${PARSE_OK:-false}"

# ── 3. Derive idempotent event-id ─────────────────────────────────────────────
# Canonical format: {role}-{disc}-{timestamp}  (set by spawn-agent.sh start_run)
# The spawn prompt injects "hook_event_id=..." as its last line (a user message).
# We scan user-role transcript lines for this tag so complete_run() updates the
# same row that start_run() inserted.  Falls back to {role}-{disc}-{session_id}
# when the tag is absent (legacy transcripts, non-worktree agents).
#
# EVENT_ID is always set in the if/else below. Initialize to empty here so that
# set -u cannot fire on any early-exit or unexpected code path before we reach
# POST_HOOK_ARGS.  The guard below the if/else catches the unreachable-but-belt-
# and-suspenders case where it is still empty.
EVENT_ID=""
SPAWN_EVENT_ID=""
# D#2247: TRANSCRIPT_PATH is the PARENT session's transcript, which never
# carries the hook_event_id tag — only the subagent's OWN transcript does
# (the spawn prompt reaches the agent as a file reference, and the tag shows
# up in the tool_result of the agent reading its own prompt file, which only
# lands in the subagent's own transcript). Prefer OWN_TRANSCRIPT_PATH when
# subagent_payload.py found one; fall back to TRANSCRIPT_PATH so a missing
# own transcript still yields an empty SPAWN_EVENT_ID and today's fallback
# path, never an error.
_EVID_SRC="${OWN_TRANSCRIPT_PATH:-}"
[[ -n "$_EVID_SRC" && -f "$_EVID_SRC" ]] || _EVID_SRC="$TRANSCRIPT_PATH"
if [[ -n "$_EVID_SRC" && -f "$_EVID_SRC" ]]; then
  # D#1784 Phase 2: the extraction logic (including tool_result-aware block
  # walking, since hook_event_id= arrives inside a tool_result payload, not
  # a text block) now lives in scripts/lib/transcript_event_id.py, shared
  # with scripts/cron/backfill-agent-runs.sh so the bug can't recur twice.
  SPAWN_EVENT_ID=$(python3 "$SCRIPT_DIR/lib/transcript_event_id.py" "$_EVID_SRC" 2>/dev/null || echo "")
fi
unset _EVID_SRC

DISC_PART="${DISCUSSION:-0}"
if [[ -n "$SPAWN_EVENT_ID" ]]; then
  # Canonical path: use the same event_id spawn-agent.sh wrote to start_run().
  EVENT_ID="$SPAWN_EVENT_ID"

  # Bug A fix: if AGENT_OUTPUT envelope was absent and role is still unknown,
  # extract the role from the spawn event-id.  spawn-agent.sh encodes role as
  # the first segment of "<role>-<disc>-<timestamp>", so we parse it back out.
  # This gives accurate telemetry for prose-only agents (PM, reviewers, etc.)
  # that never emit an AGENT_OUTPUT envelope.
  if [[ "$ROLE" == "unknown" ]]; then
    ROLE_FROM_EVENT_ID=$(python3 -c "
import sys, re
evid = sys.argv[1]
# Format: <role>-<disc_or_nod>-<unix_ts>
# disc and ts are both numeric (or 'nod'), role is the prefix before the
# first numeric/nod segment.  Split on '-' and collect leading non-numeric tokens.
parts = evid.split('-')
role_parts = []
for p in parts:
    if re.match(r'^[0-9]+$', p) or p == 'nod':
        break
    role_parts.append(p)
role = '-'.join(role_parts)
# Sanity: only trust the extracted role if it looks like a real agent role
# (1-25 chars, lowercase letters and hyphens, not 'unknown').
if role and re.match(r'^[a-z][a-z-]{0,24}$', role) and role != 'unknown':
    print(role)
else:
    print('unknown')
" "$SPAWN_EVENT_ID" 2>/dev/null || echo "unknown")
    if [[ "$ROLE_FROM_EVENT_ID" != "unknown" ]]; then
      ROLE="$ROLE_FROM_EVENT_ID"
    fi
  fi
else
  # Fallback path for agents not spawned via spawn-agent.sh (no hook_event_id
  # in transcript).
  #
  # Component guard: ROLE/DISC_PART are agent-authored (envelope.agent /
  # envelope.discussion) and unvalidated. Substitute in the id-forming copy
  # only — reject would drop the row (D#1784); --role "$ROLE" below still
  # carries the true value, so this can't misattribute telemetry.
  _ID_ROLE="$ROLE"; [[ "$ROLE" =~ ^[a-z][a-z-]{0,24}$ ]] || { echo "[subagent-stop-hook] WARNING: non-canonical ROLE '$ROLE' substituted for event id" >&2; _ID_ROLE="unknown"; }
  _ID_DISC="$DISC_PART"; [[ "$DISC_PART" =~ ^[0-9]+$ ]] || { echo "[subagent-stop-hook] WARNING: non-canonical DISC_PART '$DISC_PART' substituted for event id" >&2; _ID_DISC="0"; }

  if [[ -n "$AGENT_ID" ]]; then
    # D#2238: agent_id already came back from subagent_payload.py validated
    # against ^[A-Za-z0-9_-]{1,64}$, so it's deterministic AND unique per
    # subagent -- this is the actual fix for the 179 daily collisions where
    # every subagent in one parent session shared the same session_id.
    EVENT_ID="${_ID_ROLE}-${_ID_DISC}-${AGENT_ID}"
  else
    # No agent_id either (pre-D#2238 payload shape, or a fixture that omits
    # it): keep the nanosecond-timestamp fallback so concurrent unknown-role
    # agents in the same parent session still never share an event-id.  The
    # old formula was "${ROLE}-${DISC_PART}-${SESSION_ID}" -- when role was
    # "unknown" for multiple subagents in the same session they all produced
    # "unknown-0-<sessid>" and post-agent-hook.sh deduped all but the first.
    _FALLBACK_NANOS=$(python3 -c "import time; print(int(time.time_ns()))" 2>/dev/null \
                      || date +%s%N 2>/dev/null \
                      || date +%s)
    EVENT_ID="${_ID_ROLE}-${_ID_DISC}-${SESSION_ID}-${_FALLBACK_NANOS}"
    unset _FALLBACK_NANOS
  fi
  unset _ID_ROLE _ID_DISC
fi

# ── 3b. Drop pure-noise rows (unknown:unknown with no spawn context) ──────────
# Task-tool sub-agents, native sub-spawns, and the parent Team Lead session all
# fire SubagentStop without embedding a hook_event_id or AGENT_OUTPUT envelope.
# These produce role=unknown / verdict=unknown / tokens=0/0 — 94% of agent_end
# rows in the feed — with no useful info to record.  Skip the agent-feed write
# entirely and append a lightweight counter row to the stats file instead so
# the volume is observable without polluting the feed.
if [[ "$ROLE" == "unknown" && "$VERDICT" == "unknown" && -z "$SPAWN_EVENT_ID" && "$PARSE_OK" != "true" ]]; then
  _STATS_DIR="$REPO_ROOT/.autonomous-team/stats"
  _STATS_DATE=$(date -u +%Y-%m-%d 2>/dev/null || echo "unknown-date")
  _STATS_FILE="$_STATS_DIR/unknown_subagent_stops-${_STATS_DATE}.jsonl"
  mkdir -p "$_STATS_DIR" 2>/dev/null || true
  python3 -c "
import json, time, sys
row = {
    'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'session_id': sys.argv[1],
    'transcript_path': sys.argv[2],
    'agent_id': sys.argv[3],
}
print(json.dumps(row))
" "$SESSION_ID" "${TRANSCRIPT_PATH:-}" "${AGENT_ID:-}" >> "$_STATS_FILE" 2>/dev/null || true
  # Debug-level stderr trace — only visible when caller captures stderr
  echo "[subagent-stop-hook] skipped unknown:unknown row (no spawn context) session=$SESSION_ID" >&2
  exit 0
fi

# ── 4. Call post-agent-hook.sh (or dry-run for tests) ────────────────────────
# Belt-and-suspenders: EVENT_ID must be non-empty by now.  If it somehow is not
# (e.g. a future refactor breaks the if/else above), fail loudly instead of
# silently passing an empty string to --event-id and creating literal-named files.
: "${EVENT_ID:?subagent-stop-hook: EVENT_ID is unset or empty — this is a bug}"

POST_HOOK_ARGS=(
  --role     "$ROLE"
  --verdict  "$VERDICT"
  --event-id "$EVENT_ID"
  --input-tokens  "$INPUT_TOKENS"
  --output-tokens "$OUTPUT_TOKENS"
  --cache-read-tokens  "$CACHE_READ_TOKENS"
  --cache-write-tokens "$CACHE_WRITE_TOKENS"
  --self-observed "$SELF_OBSERVED"
)
[[ "${CACHE_CREATION_TOKENS:-0}" -gt 0 ]] 2>/dev/null && POST_HOOK_ARGS+=(--cache-creation-tokens "$CACHE_CREATION_TOKENS")
[[ -n "$DISCUSSION"      ]] && POST_HOOK_ARGS+=(--discussion "$DISCUSSION")
[[ -n "$PR"              ]] && POST_HOOK_ARGS+=(--pr "$PR")
[[ -n "$FILES"           ]] && POST_HOOK_ARGS+=(--files "$FILES")
[[ -n "$FIRST_WRITE_TURN" ]] && POST_HOOK_ARGS+=(--first-write-turn "$FIRST_WRITE_TURN")

# SUBAGENT_STOP_DRY_RUN=1 — test mode: write resolved args JSON to
# SUBAGENT_STOP_ARGS_FILE instead of calling post-agent-hook.sh.
if [[ "${SUBAGENT_STOP_DRY_RUN:-0}" == "1" && -n "${SUBAGENT_STOP_ARGS_FILE:-}" ]]; then
  python3 -c "
import json, sys
args = sys.argv[1:]
d = {}
i = 0
while i < len(args):
    key = args[i].lstrip('-').replace('-', '_')
    if i+1 < len(args) and not args[i+1].startswith('--'):
        d[key] = args[i+1]
        i += 2
    else:
        d[key] = True
        i += 1
# Coerce numeric fields
for k in ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','cache_creation_tokens'):
    if k in d:
        try: d[k] = int(d[k])
        except: pass
with open('${SUBAGENT_STOP_ARGS_FILE}', 'w') as f:
    json.dump(d, f, indent=2)
" "${POST_HOOK_ARGS[@]}" 2>/dev/null || true
else
  # D#2111: the old fixed-path tee target had exactly one writer and zero
  # readers — every concurrent agent clobbered it, and
  # `|| true` discarded the exit code on top of that. `set -uo pipefail`
  # (line 23) is already active here, so PIPESTATUS[0] is the real callee
  # status regardless of the tee; the only thing `|| true` was hiding was
  # that status. Scope the log by EVENT_ID (already guaranteed unique per
  # run — see the fallback-nanos comment above) so concurrent agents don't
  # overwrite each other, and name the hook, its exit status, and its own
  # tail lines when it aborts, whatever the abort cause. Tail rather than
  # the single last line: some abort sites (e.g. post-agent-hook.sh's own
  # "Unknown argument: $1" arg-parsing exit) print a second, less useful
  # "Usage:" line after the actual cause. This mirrors merge-and-hook.sh:
  # 333-341's tee + PIPESTATUS[0] + named-warning shape.
  _PAH_LOG="/tmp/post-agent-hook-${EVENT_ID}.err"
  bash "$SCRIPT_DIR/post-agent-hook.sh" "${POST_HOOK_ARGS[@]}" 2>&1 | tee "$_PAH_LOG" >/dev/null
  _PAH_RC="${PIPESTATUS[0]}"
  if [[ "$_PAH_RC" -ne 0 ]]; then
    _PAH_TAIL=$(tail -5 "$_PAH_LOG" 2>/dev/null)
    echo "[subagent-stop-hook] post-agent-hook failed (exit $_PAH_RC): ${_PAH_TAIL} (log: $_PAH_LOG)" >&2
  fi
fi

# Always exit 0 — never block subagent completion
exit 0
