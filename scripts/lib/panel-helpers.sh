#!/usr/bin/env bash
# scripts/lib/panel-helpers.sh — helpers for Team Lead consensus panel orchestration.
#
# Sourced by scripts/loop-phased-step5.sh (and tests) — do not execute directly.
#
# Functions exported:
#   detect_panel_needed   TITLE BODY         → 0 if panel is required, 1 if not
#   get_panel_specialists TITLE              → newline-separated specialist role names
#   extract_discussion_status BODY           → echo the STATUS:X value from body
#   set_discussion_status DISC_NUM NEW_STATUS → mutate the STATUS line in the Discussion body
#   count_specialist_comments DISC_NUM       → echo count of comments with specialist envelopes

SPECIALIST_ROLES="technical-architect security-expert cost-analyst product-owner performance-expert"

# ---------------------------------------------------------------------------
# Repo slug — resolved once, here, instead of once per query.
# Reads .autonomous-team/config.json's "repo" field; falls back to the
# hard-coded slug below only if that file is unreadable or the key is empty.
# That fallback is a safety net, not a live path — this repo's config.json
# always has the field set.
# ---------------------------------------------------------------------------
_PANEL_HELPERS_REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
_PANEL_HELPERS_REPO=$(python3 -c "
import json
try:
    with open('$_PANEL_HELPERS_REPO_ROOT/.autonomous-team/config.json') as f:
        r = json.load(f).get('repo', '')
except Exception:
    r = ''
print(r or 'autonomous-agent-7/fulcrumaxe')
" 2>/dev/null)
_PANEL_HELPERS_REPO_OWNER="${_PANEL_HELPERS_REPO%%/*}"
_PANEL_HELPERS_REPO_NAME="${_PANEL_HELPERS_REPO##*/}"

# ---------------------------------------------------------------------------
# detect_panel_needed TITLE BODY
# Returns 0 (needs panel) for [Critical] and [Feature] discussions.
# Returns 1 for everything else.
# ---------------------------------------------------------------------------
detect_panel_needed() {
  local title="$1"
  local tag
  tag=$(echo "$title" | grep -oE '\[(Critical|Feature)\]' | head -1)
  [ -n "$tag" ] && return 0 || return 1
}

# ---------------------------------------------------------------------------
# get_panel_specialists TITLE
# Emits one specialist role per line, using consensus_panel.py get-panel.
# Falls back to empty on error.
# ---------------------------------------------------------------------------
get_panel_specialists() {
  local title="$1"
  local repo_root="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  python3 "$repo_root/backend/consensus_panel.py" get-panel --title "$title" 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for s in d.get('specialists', []):
        print(s)
except Exception:
    pass
" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# extract_discussion_status BODY
# Echoes the STATUS token, e.g. "DISCUSSING", "DISCUSSING-needs-panel",
# "DISCUSSING-panel-ready", "SPEC_READY", "DONE", etc.
# Returns empty string if no STATUS marker found.
# ---------------------------------------------------------------------------
extract_discussion_status() {
  local body="$1"
  echo "$body" | grep -oE 'STATUS:[A-Za-z_-]+' | head -1 | sed 's/STATUS://'
}

# ---------------------------------------------------------------------------
# set_discussion_status DISC_NUM NEW_STATUS
# Replaces the STATUS line in the Discussion body via GraphQL mutation.
# Only touches the <!-- STATUS:... --> comment block; preserves everything else.
# Returns 0 on success, 1 on failure.
# ---------------------------------------------------------------------------
set_discussion_status() {
  local disc_num="$1"
  local new_status="$2"
  local repo_root="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

  # Fetch current body
  local current_body disc_id
  local query_result
  query_result=$(gh api graphql -f query="
    query {
      repository(owner:\"$_PANEL_HELPERS_REPO_OWNER\", name:\"$_PANEL_HELPERS_REPO_NAME\") {
        discussion(number: $disc_num) {
          id
          body
        }
      }
    }
  " 2>/dev/null) || return 1

  disc_id=$(echo "$query_result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['data']['repository']['discussion']['id'])
" 2>/dev/null) || return 1

  current_body=$(echo "$query_result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['data']['repository']['discussion']['body'])
" 2>/dev/null) || return 1

  # Delegate STATUS marker upsert to discussion_status.set_status().
  # That function handles both the replace-existing and insert-when-absent cases.
  local new_body
  new_body=$(python3 -c "
import sys
sys.path.insert(0, '$repo_root')
from backend.discussion_status import set_status
body = sys.stdin.read()
print(set_status(body, '$new_status'), end='')
" <<<"$current_body" 2>/dev/null) || return 1

  # Update via GraphQL mutation
  gh api graphql \
    -f query='mutation($id:ID!, $body:String!) {
      updateDiscussion(input:{discussionId:$id, body:$body}) {
        discussion { id }
      }
    }' \
    -f id="$disc_id" \
    -f body="$new_body" \
    >/dev/null 2>&1 || return 1

  return 0
}

# ---------------------------------------------------------------------------
# post_specialist_comment DISC_ID ROLE BODY
# Posts a Discussion comment as the specialist, signed with their role.
# DISC_ID is the GraphQL node ID (from the Discussion query).
#
# Refuses to post a body whose AGENT_OUTPUT envelope "agent" field does not
# equal ROLE — exits non-zero, posts nothing, writes the reason to stderr.
# This is the item-1 envelope-in-comment contract enforced mechanically
# instead of relying on template instruction alone (D#1924 PR-b item 13).
#
# After a successful write, re-reads the created comment by its returned
# node ID and compares the fetched body to what was sent (item 14). This is
# a diagnostic, not the correctness fix for D#1810 — panel_gate_decide in
# scripts/lib/panel-quorum.sh is that fix. Read-back exists so a retry that
# posted a stale or wrong body becomes visible here instead of passing
# silently; see D#1924's Spec for why the duplicate-vs-loss question is
# left open rather than resolved by this check.
#
# On success, echoes the verified comment ID on stdout and returns 0. On any
# failure (envelope/role mismatch, post failure, read-back query failure, or
# a body mismatch) returns non-zero with NO stdout, and writes the sent ID,
# the read-back ID, and a one-line summary to stderr.
# ---------------------------------------------------------------------------
post_specialist_comment() {
  local disc_id="$1"
  local role="$2"
  local body="$3"

  local envelope_agent
  envelope_agent=$(python3 -c "
import sys, re
m = re.search(r'\"agent\"\s*:\s*\"([^\"]+)\"', sys.stdin.read())
print(m.group(1) if m else '')
" <<<"$body" 2>/dev/null)

  if [ "$envelope_agent" != "$role" ]; then
    echo "post_specialist_comment: refusing to post — envelope agent '${envelope_agent:-<none>}' does not match role '$role'" >&2
    return 1
  fi

  local mutation_result sent_id
  mutation_result=$(gh api graphql \
    -f query='mutation($id:ID!, $body:String!) {
      addDiscussionComment(input:{discussionId:$id, body:$body}) {
        comment { id }
      }
    }' \
    -f id="$disc_id" \
    -f body="$body" 2>/dev/null) || {
    echo "post_specialist_comment: mutation failed for role '$role'" >&2
    return 1
  }

  sent_id=$(python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d['data']['addDiscussionComment']['comment']['id'])
except Exception:
    pass
" <<<"$mutation_result" 2>/dev/null)

  if [ -z "$sent_id" ]; then
    echo "post_specialist_comment: mutation returned no comment id for role '$role'" >&2
    return 1
  fi

  local readback_result readback_body
  readback_result=$(gh api graphql -f query="
    query {
      node(id: \"$sent_id\") {
        ... on DiscussionComment { body }
      }
    }
  " 2>/dev/null) || {
    echo "post_specialist_comment: read-back query failed — sent id=$sent_id" >&2
    return 1
  }

  readback_body=$(python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('data') and d['data'].get('node') and d['data']['node'].get('body') or '')
except Exception:
    print('')
" <<<"$readback_result" 2>/dev/null)

  if [ "$readback_body" != "$body" ]; then
    echo "post_specialist_comment: MISMATCH — sent id=$sent_id read-back id=$sent_id — sent ${#body} chars, read back ${#readback_body} chars" >&2
    return 1
  fi

  echo "$sent_id"
  return 0
}

# ---------------------------------------------------------------------------
# count_specialist_comments DISC_NUM
# Queries Discussion comments via GraphQL. Counts those whose body contains
# an AGENT_OUTPUT envelope with `agent` matching a known specialist role.
# Echoes an integer on success. Returns 1 with NO stdout on a failed query —
# a genuine zero-comment panel still echoes "0" with exit 0; a broken query
# must never be readable as that same "0" (D#2156).
# ---------------------------------------------------------------------------
count_specialist_comments() {
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
count = 0
for c in comments:
    body = c.get('body', '')
    # Look for AGENT_OUTPUT envelope with a specialist agent field
    m = re.search(r'\"agent\"\s*:\s*\"([^\"]+)\"', body)
    if m and m.group(1) in SPECIALIST_ROLES:
        count += 1
print(count)
" <<<"$query_result" 2>/dev/null || return 1
}

# ---------------------------------------------------------------------------
# get_discussion_id DISC_NUM
# Echoes the GraphQL node ID for a discussion number on success. Returns 1
# with NO stdout on a failed query — an empty echo used to be indistinguishable
# from exit 0 (D#2156).
# ---------------------------------------------------------------------------
get_discussion_id() {
  local disc_num="$1"
  local query_result

  query_result=$(gh api graphql -f query="
    query {
      repository(owner:\"$_PANEL_HELPERS_REPO_OWNER\", name:\"$_PANEL_HELPERS_REPO_NAME\") {
        discussion(number: $disc_num) { id }
      }
    }
  " 2>/dev/null) || return 1

  python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['data']['repository']['discussion']['id'])
" <<<"$query_result" 2>/dev/null || return 1
}
