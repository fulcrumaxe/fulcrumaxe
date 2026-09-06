#!/usr/bin/env bash
# scripts/lib/panel-quorum.sh — role-based panel completeness gate (D#1924 PR-b).
#
# scripts/lib/panel-helpers.sh's count_specialist_comments counts *comments*,
# not *roles*. Replay D#1810's shape — cost-analyst x1, security-expert x2,
# technical-architect x0, envelope contract satisfied on all three comments —
# and ACTUAL=3, EXPECTED=3: the gate advances with a specialist entirely
# missing, because the duplicate security-expert comment conceals the
# absent technical-architect one. This module counts distinct roles instead,
# so a duplicate can never substitute for a missing role.
#
# Sourced by scripts/loop-phased-step5.sh (and tests) — do not execute
# directly. Assumes scripts/lib/panel-helpers.sh has already been sourced
# (reuses its repo-slug resolution rather than re-deriving it); if it has
# not, this file resolves the same slug the same way as a fallback.
#
# Functions exported:
#   specialist_roles_present DISC_NUM          -> distinct specialist roles present, one per line, sorted
#   panel_gate_decide DISC_NUM "role,role,..."  -> ready | waiting:role,role | timeout:role,role
#   panel_gate_post_timeout_notice DISC_NUM "role,role" -> posts one Discussion comment naming missing roles

if [ -z "${_PANEL_HELPERS_REPO_OWNER:-}" ]; then
  _PQ_REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  _PANEL_HELPERS_REPO=$(python3 -c "
import json
try:
    with open('$_PQ_REPO_ROOT/.autonomous-team/config.json') as f:
        r = json.load(f).get('repo', '')
except Exception:
    r = ''
print(r or 'autonomous-agent-7/fulcrumaxe')
" 2>/dev/null)
  _PANEL_HELPERS_REPO_OWNER="${_PANEL_HELPERS_REPO%%/*}"
  _PANEL_HELPERS_REPO_NAME="${_PANEL_HELPERS_REPO##*/}"
fi

# Default panel timeout, used only when policies.pm.discussion_timeout_minutes
# is absent or unparseable via control_plane.py. Spec default: 30 minutes.
_PANEL_QUORUM_DEFAULT_TIMEOUT_MIN=30

# ---------------------------------------------------------------------------
# specialist_roles_present DISC_NUM
# Echoes each *distinct* specialist role whose posted comment carries an
# AGENT_OUTPUT envelope naming that role, one per line, sorted. A role
# posting twice (or five times) still echoes once — that is the fix.
#
# Reuses the same envelope regex count_specialist_comments already uses,
# verbatim — only the aggregation (set of roles, not a counter) differs.
#
# Returns 1 with NO stdout on a failed query. A genuine zero-role Discussion
# still exits 0 with empty stdout — the two must stay distinguishable
# (D#2156); callers MUST check the exit status, not just the output.
# ---------------------------------------------------------------------------
specialist_roles_present() {
  local disc_num="$1"
  local query_result

  query_result=$(gh api graphql -f query="
    query {
      repository(owner:\"$_PANEL_HELPERS_REPO_OWNER\", name:\"$_PANEL_HELPERS_REPO_NAME\") {
        discussion(number: $disc_num) {
          comments(first: 50) {
            nodes { body }
          }
        }
      }
    }
  " 2>/dev/null) || return 1

  python3 -c "
import json, sys, re

SPECIALIST_ROLES = {
    'technical-architect', 'security-expert', 'cost-analyst',
    'product-owner', 'performance-expert'
}

data = json.load(sys.stdin)
comments = data['data']['repository']['discussion']['comments']['nodes']
roles = set()
for c in comments:
    body = c.get('body', '')
    m = re.search(r'\"agent\"\s*:\s*\"([^\"]+)\"', body)
    if m and m.group(1) in SPECIALIST_ROLES:
        roles.add(m.group(1))
for r in sorted(roles):
    print(r)
" <<<"$query_result" 2>/dev/null || return 1
}

# ---------------------------------------------------------------------------
# _panel_quorum_timeout_minutes
# Reads policies.pm.discussion_timeout_minutes via control_plane.py; falls
# back to the literal default on any read/parse failure (missing key,
# control_plane.py error, non-numeric value).
# ---------------------------------------------------------------------------
_panel_quorum_timeout_minutes() {
  local repo_root="$1"
  local timeout_min
  timeout_min=$(python3 "$repo_root/backend/control_plane.py" get policies.pm.discussion_timeout_minutes 2>/dev/null)
  case "$timeout_min" in
    ''|*[!0-9]*) echo "$_PANEL_QUORUM_DEFAULT_TIMEOUT_MIN" ;;
    *) echo "$timeout_min" ;;
  esac
}

# ---------------------------------------------------------------------------
# _panel_quorum_timed_out DISC_NUM
# Returns 0 if the Discussion's current STATUS SINCE: timestamp is older than
# the configured timeout, 1 otherwise (not timed out, or indeterminate).
#
# Fails open toward "not timed out" — a missing/unparseable SINCE:, or a
# failed body query, must read as waiting, never as a false timeout or a
# false ready (item 10).
# ---------------------------------------------------------------------------
_panel_quorum_timed_out() {
  local disc_num="$1"
  local repo_root="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  local timeout_min
  timeout_min=$(_panel_quorum_timeout_minutes "$repo_root")

  local query_result body since_iso
  query_result=$(gh api graphql -f query="
    query {
      repository(owner:\"$_PANEL_HELPERS_REPO_OWNER\", name:\"$_PANEL_HELPERS_REPO_NAME\") {
        discussion(number: $disc_num) { body }
      }
    }
  " 2>/dev/null) || return 1

  body=$(python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d['data']['repository']['discussion']['body'])
except Exception:
    print('')
" <<<"$query_result" 2>/dev/null)

  since_iso=$(python3 -c "
import sys
sys.path.insert(0, '$repo_root')
from backend.discussion_status import extract_since
since = extract_since(sys.stdin.read())
print(since or '')
" <<<"$body" 2>/dev/null)

  [ -z "$since_iso" ] && return 1

  python3 -c "
import sys
from datetime import datetime, timezone
since_str = sys.argv[1]
timeout_min = float(sys.argv[2])
try:
    since = datetime.fromisoformat(since_str.replace('Z', '+00:00'))
except Exception:
    sys.exit(1)
now = datetime.now(timezone.utc)
elapsed_min = (now - since).total_seconds() / 60.0
sys.exit(0 if elapsed_min > timeout_min else 1)
" "$since_iso" "$timeout_min"
}

# ---------------------------------------------------------------------------
# panel_gate_decide DISC_NUM "role1,role2,role3"
# Echoes exactly one decision token on stdout:
#   ready               — every expected role is present
#   waiting:role,role   — roles missing, panel has not timed out
#   timeout:role,role   — roles missing, panel has timed out
# Returns 1 with NO stdout on a failed query.
#
# A duplicate role can never substitute for a missing one — this compares
# the SET of roles specialist_roles_present returns against the expected
# role list, not a count (the D#1810 fix).
# ---------------------------------------------------------------------------
panel_gate_decide() {
  local disc_num="$1"
  local expected_csv="$2"

  local present
  present=$(specialist_roles_present "$disc_num" 2>/dev/null) || return 1

  local missing_csv
  missing_csv=$(python3 -c "
import sys
expected = [r.strip() for r in sys.argv[1].split(',') if r.strip()]
present = set(sys.argv[2].splitlines()) if sys.argv[2] else set()
missing = [r for r in expected if r not in present]
print(','.join(missing))
" "$expected_csv" "$present") || return 1

  if [ -z "$missing_csv" ]; then
    echo "ready"
    return 0
  fi

  if _panel_quorum_timed_out "$disc_num"; then
    echo "timeout:${missing_csv}"
  else
    echo "waiting:${missing_csv}"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# panel_gate_post_timeout_notice DISC_NUM "role,role"
# Posts one Discussion comment naming the specialist roles that never
# posted, so the PM receives an explicitly incomplete panel instead of a
# silent one. Best-effort: a failed post here must not undo the status
# transition the caller already made.
# ---------------------------------------------------------------------------
panel_gate_post_timeout_notice() {
  local disc_num="$1"
  local missing_csv="$2"
  local disc_id
  disc_id=$(get_discussion_id "$disc_num" 2>/dev/null) || return 1

  gh api graphql \
    -f query='mutation($id:ID!, $body:String!) {
      addDiscussionComment(input:{discussionId:$id, body:$body}) {
        comment { id }
      }
    }' \
    -f id="$disc_id" \
    -f body="Panel timed out waiting for: ${missing_csv}. Proceeding to Spec with the panel as-is." \
    >/dev/null 2>&1 || return 1

  return 0
}
