#!/usr/bin/env bash
# Loop pre-flight: runs coordination module CLIs before each loop iteration.
# Outputs a JSON summary to stdout. Exits 1 if the loop should be skipped.
#
# Usage: bash scripts/loop-preflight.sh
# Exit codes:
#   0 — loop should proceed
#   1 — loop should be skipped (gate disabled or budget exhausted)
#
# Fail-closed contract (D#2063): every gate/budget read that cannot be
# completed must resolve to a BLOCKING value, never a permissive one. A
# missing key inside otherwise-valid JSON still defaults permissive (that is
# a real "gate not configured" case) — only a failed *read* defaults closed.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ERRORS='[]'
# GATES_JSON/BUDGET_JSON default to BLOCKING values, not '{}'. If control_plane
# or budget can't be read at all, that is a failed read, not an absent key —
# it must not fall through to the "key absent -> permissive" default below.
# REGISTRY_JSON stays '{}': the registry isn't a blocking gate, just informational.
GATES_JSON='{"loop_enabled": false}'
BUDGET_JSON='{"allowed": false}'
REGISTRY_JSON='{}'

# Helper: append an error string to ERRORS json array
add_error() {
  local msg="$1"
  ERRORS=$(python3 -c "
import json, sys
errs = json.loads(sys.argv[1])
errs.append(sys.argv[2])
print(json.dumps(errs))
" "$ERRORS" "$msg" 2>/dev/null || echo "$ERRORS")
}

# Step 1: Initialize budget (idempotent)
if INIT_OUT=$(python3 backend/budget.py init 2>&1); then
  : # success
else
  add_error "budget.py init failed: $INIT_OUT"
fi

# Step 2: Sync registry with latest Discussion state, then summarize it.
#
# D#2310: this used to capture `registry.py show`'s full dump (routinely
# several hundred KB) and aggregate bucket counts over EVERY row with no
# `closed_at` filter — reporting e.g. "304 DISCUSSING" when only 17 were
# actually open. It's fixed by reading the summary from `registry.py
# queue-summary` instead — a thin CLI wrapper around
# DiscussionRegistry.queue_summary() (backend/registry.py), the one shared
# open-filter implementation `backend/cli.py`'s `status` command also reads
# from — instead of re-deriving a second, unfiltered count here.
#
# queue-summary's own stdout is small (aggregate counts, not the registry
# dump), but the stdin-not-argv contract below is kept anyway: the payload
# is captured to a file and piped in via redirection, exactly as `show`'s
# output was before, so a future subcommand that DOES grow with registry
# size inherits the same safe handling rather than a maybe-safe-today one.
#
# The registry dump MUST NOT be handed to a child python3 as a single argv
# element — a single argv string is capped well under its routine size on
# Linux (MAX_ARG_STRLEN, historically 128 KiB), and blowing past it fails
# the exec itself with E2BIG/"Argument list too long" before Python ever
# starts. That failure has nothing to do with JSON parsing, so it must not
# be recorded as a parse error, and it must not be swallowed by a stderr
# redirect that leaves no trace anywhere.
if SYNC_OUT=$(python3 backend/registry.py sync 2>&1); then
  QS_FILE=$(mktemp)
  QS_ERR=$(mktemp)
  if python3 backend/registry.py queue-summary > "$QS_FILE" 2>"$QS_ERR"; then
    REG_PARSE_ERR=$(mktemp)
    if REG_SUMMARY=$(python3 -c "
import json, sys
qs = json.load(sys.stdin)
buckets = qs.get('buckets', {})
summary = {
    'total': qs.get('total', 0),
    'excluded_closed': qs.get('excluded_closed', 0),
    'discussing': buckets.get('DISCUSSING', 0),
    'spec_ready': buckets.get('SPEC_READY', 0),
    'implementing': buckets.get('IMPLEMENTING', 0),
    'reviewing': buckets.get('REVIEWING', 0),
    'done': qs.get('done', 0),
    'synced_at': qs.get('synced_at', ''),
}
print(json.dumps(summary))
" < "$QS_FILE" 2>"$REG_PARSE_ERR"); then
      REGISTRY_JSON="$REG_SUMMARY"
    else
      # The producer (registry.py queue-summary) succeeded — this is a
      # genuine parse failure of its output, so "parse failed" is accurate.
      REGISTRY_JSON='{}'
      add_error "registry.py queue-summary parse failed: $(tr '\n' ' ' < "$REG_PARSE_ERR" | cut -c1-200)"
    fi
    rm -f "$REG_PARSE_ERR"
  else
    # The producer itself failed — never label this a parse failure, no
    # parse was attempted.
    REGISTRY_JSON='{}'
    add_error "registry.py queue-summary failed: $(tr '\n' ' ' < "$QS_ERR" | cut -c1-200)"
  fi
  rm -f "$QS_FILE" "$QS_ERR"
else
  add_error "registry.py sync failed: $SYNC_OUT"
fi

# Step 2.5: Warm up context manager cache (ensures context file exists before agents read it)
python3 backend/context_manager.py show > /dev/null 2>&1 || add_error "context_manager warmup failed"

# Step 3: Read feature gate states via control_plane show
if CP_OUT=$(python3 backend/control_plane.py show 2>/dev/null); then
  if PARSED_GATES=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
print(json.dumps(data.get('gates', {})))
" "$CP_OUT" 2>/dev/null); then
    GATES_JSON="$PARSED_GATES"
  else
    GATES_JSON='{"loop_enabled": false}'
    add_error "control_plane.py gates parse failed"
  fi
else
  add_error "control_plane.py show failed"
fi

# Step 4: Get current budget status
if BUDGET_OUT=$(python3 backend/budget.py status 2>/dev/null); then
  if PARSED_BUDGET=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
# Compute 'allowed': budget not exceeded
ceiling = data.get('ceiling', data.get('session_ceiling', 0))
spent = data.get('spent', data.get('session_spent', 0))
remaining = ceiling - spent if ceiling > 0 else 0
allowed = (spent < ceiling) if ceiling > 0 else True
summary = {
    'ceiling': ceiling,
    'spent': spent,
    'remaining': remaining,
    'allowed': allowed,
}
print(json.dumps(summary))
" "$BUDGET_OUT" 2>/dev/null); then
    BUDGET_JSON="$PARSED_BUDGET"
  else
    BUDGET_JSON='{"allowed": false}'
    add_error "budget.py status parse failed"
  fi
else
  add_error "budget.py status failed"
fi

# Assemble final JSON summary. GATES_JSON/BUDGET_JSON/REGISTRY_JSON/ERRORS are
# each guaranteed valid JSON by the steps above, so this should never fail —
# but if it somehow does, fall back to a summary that reads as BLOCKING
# (gates.loop_enabled=false, budget.allowed=false), never a permissive one.
SUMMARY=$(python3 -c "
import json, sys
print(json.dumps({
    'timestamp': sys.argv[1],
    'gates':     json.loads(sys.argv[2]),
    'budget':    json.loads(sys.argv[3]),
    'registry':  json.loads(sys.argv[4]),
    'errors':    json.loads(sys.argv[5]),
}, indent=2))
" "$TIMESTAMP" "$GATES_JSON" "$BUDGET_JSON" "$REGISTRY_JSON" "$ERRORS" 2>/dev/null)

if [ -z "$SUMMARY" ]; then
  add_error "summary assembly failed — falling back to a blocking summary"
  SUMMARY=$(python3 -c "
import json, sys
errs = sys.argv[2]
try:
    errs = json.loads(errs)
except Exception:
    errs = ['summary assembly failed']
print(json.dumps({
    'timestamp': sys.argv[1],
    'gates': {'loop_enabled': False},
    'budget': {'allowed': False},
    'registry': {},
    'errors': errs,
}, indent=2))
" "$TIMESTAMP" "$ERRORS" 2>/dev/null)
  if [ -z "$SUMMARY" ]; then
    # Last-resort literal fallback — still blocking, still valid JSON.
    SUMMARY='{"timestamp":"'"$TIMESTAMP"'","gates":{"loop_enabled":false},"budget":{"allowed":false},"registry":{},"errors":["summary assembly failed"]}'
  fi
fi

echo "$SUMMARY"

# Check loop_enabled gate (key may vary by control_plane version).
# A successful read that finds the key absent still defaults to true (an
# unconfigured gate is "on" by convention) — only a FAILED read defaults to
# false, so a broken pipeline stops the loop instead of waving it through.
if LOOP_ENABLED=$(echo "$SUMMARY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
gates = d.get('gates', {})
val = gates.get('loop_enabled', gates.get('loop', True))
print('true' if val else 'false')
" 2>/dev/null); then
  :
else
  LOOP_ENABLED="false"
fi

if [ "$LOOP_ENABLED" = "false" ]; then
  echo "[loop-preflight] loop_enabled gate is false — skipping iteration" >&2
  exit 1
fi

# Check budget allowed (same fail-closed rule as above).
if BUDGET_ALLOWED=$(echo "$SUMMARY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
budget = d.get('budget', {})
val = budget.get('allowed', True)
print('true' if val else 'false')
" 2>/dev/null); then
  :
else
  BUDGET_ALLOWED="false"
fi

if [ "$BUDGET_ALLOWED" = "false" ]; then
  echo "[loop-preflight] budget exhausted — skipping iteration" >&2
  exit 1
fi

exit 0
