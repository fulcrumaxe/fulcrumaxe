#!/usr/bin/env bash
# scripts/lib/role-capabilities.sh — role-card capability declarations (D#2153).
#
# Single source of truth for "is this role read-only for the purposes of the
# --touchpoints file-scope claim gate" in scripts/spawn-agent.sh section 0c.
#
# Declared on the role card itself (.claude/agents/<role>.md frontmatter,
# `read_only: true`), not hardcoded here or in the wrapper — the card is the
# file whoever adds a role is already editing (D#2153 Decision 1). Reuses the
# exact frontmatter-read shape spawn-agent.sh already uses for `model:`: a
# head-bounded read of the first 512 bytes, anchored regex, non-fatal on a
# missing card (scripts/spawn-agent.sh:565-585 is the pattern this copies).
#
# Absent declaration means write-capable (D#2153 Decision 3): a role card
# with no `read_only:` field, or no card at all, is NOT read-only. An
# undeclared new role keeps the gate's existing (loud, visible,
# one-line-fixable) block rather than a silently weakened gate.
#
# This does NOT create a claim registry (D#2153 Decision 2) — read-only
# roles skip write-claim *enforcement*, they never register a read claim.
# This module only answers "is this role read-only"; it never stores or
# derives claims itself.
#
# Runnable standalone:
#   bash scripts/lib/role-capabilities.sh is-read-only <role>
#     -> exit 0 if the role card declares read_only: true, exit 1 otherwise
#        (including: no such card, or the card has no read_only: field).

set -uo pipefail

_RC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_RC_REPO_ROOT="${REPO_ROOT:-$(cd "$_RC_LIB_DIR/../.." && pwd)}"

# role_is_read_only <role> — returns 0 if the role card declares
# `read_only: true` in its frontmatter (first 512 bytes), 1 otherwise.
role_is_read_only() {
  local role="$1"
  local card="$_RC_REPO_ROOT/.claude/agents/${role}.md"
  [[ -f "$card" ]] || return 1

  local flag
  flag=$(python3 -c "
import re, sys
try:
    with open(sys.argv[1]) as f:
        text = f.read(512)   # frontmatter is always in the first 512 bytes
    m = re.search(r'^read_only:\s*(\S+)', text, re.MULTILINE)
    print(m.group(1).strip() if m else '')
except Exception:
    print('')
" "$card" 2>/dev/null || echo "")

  [[ "$flag" == "true" ]]
}

# rc_report_claim <role> <touchpoint> <holder-ref>
# Prints "CONFLICT: ..." to stderr and returns 0 (caller should block) for a
# write-capable role. Prints "NOTE: ..." to stderr and returns 1 (caller
# should NOT block) for a role declared read_only: true — the overlap is
# still reported so the information isn't lost (D#2153 Decision 2), it just
# doesn't refuse the spawn. Centralizes the message text so spawn-agent.sh's
# 0c gate stays a thin caller (module-per-feature).
rc_report_claim() {
  local role="$1" tp="$2" holder="$3"
  if role_is_read_only "$role"; then
    echo "NOTE: $role spawn — $tp already claimed by $holder (read-only role, not enforced)" >&2
    return 1
  fi
  echo "CONFLICT: $role spawn blocked — $tp already claimed by $holder" >&2
  return 0
}

# ── standalone entry point ───────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    is-read-only)
      role_is_read_only "${2:-}"
      exit $?
      ;;
    *)
      echo "usage: role-capabilities.sh is-read-only <role>" >&2
      exit 1
      ;;
  esac
fi
