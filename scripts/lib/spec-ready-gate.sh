#!/usr/bin/env bash
# scripts/lib/spec-ready-gate.sh — shared SPEC_READY gate for the executor spawn path.
#
# Extracted from scripts/spawn-agent.sh (D#1798): the previous inline check was a
# substring grep for 'STATUS:\s*SPEC_READY' over the whole Discussion body, which
# matches the marker anywhere — inside a fenced code block, a quoted PM rejection
# note, a duplicate stale marker — not just the authoritative one. Anchoring to the
# first non-empty line and handing that single line to the canonical parser
# (backend/discussion_status.py extract_status()) closes that class without
# touching extract_status()'s own semantics, which other readers depend on.
#
# scripts/spawn-agent.sh is on the PreToolUse forbidden-command list, so it cannot
# be executed to test it directly. This file is sourceable specifically so
# tests/test_spec_ready_gate.sh can exercise the real gate logic instead of a
# paraphrase of it.
#
# Usage (source, then call):
#   source "$REPO_ROOT/scripts/lib/spec-ready-gate.sh"
#   if ! REASON=$(spec_ready_gate_check "$DISC_BODY" "$DISCUSSION" 2>&1); then
#     echo "$REASON" >&2
#     exit 1
#   fi

_SPEC_READY_GATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_SPEC_READY_GATE_REPO_ROOT="${REPO_ROOT:-$(cd "$_SPEC_READY_GATE_DIR/../.." && pwd)}"

# spec_ready_gate_check <body> [discussion_number]
#   Returns 0 (prints nothing) when the authoritative status is SPEC_READY.
#   Returns 1 and prints the reason to stderr otherwise.
spec_ready_gate_check() {
  local body="$1"
  local disc_num="${2:-}"
  local disc_label="Discussion"
  if [[ -n "$disc_num" ]]; then
    disc_label="Discussion #$disc_num"
  fi

  # Anchor to the leading non-blank record of the body — this is the
  # authoritative status line by convention (backend/discussion_status.py's
  # set_status() always writes the marker there). Everything after it is
  # prose and must not be read as status.
  #
  # The whole body is handed to the parser's --anchored mode, which picks that
  # anchor record itself (extract_status_anchored, backend-side), instead of
  # this file pre-selecting a record with a bash-only, newline-only text tool.
  # The two used to disagree: this file's old approach broke on '\n' only,
  # while the Python side's record-splitting also breaks on \x0b, \x0c,
  # \x1c-\x1e, \x85, U+2028 and U+2029 — so a body opening with one of those
  # eight characters used to read as SPEC_READY here while every other reader
  # of is_spec_ready() read it as UNKNOWN (D#1941). Do not reintroduce any
  # record-selection step in this file; that would recreate the exact defect
  # this fixed.
  local status
  status=$(printf '%s' "$body" | python3 "$_SPEC_READY_GATE_REPO_ROOT/backend/discussion_status.py" extract-status --stdin --anchored 2>/dev/null)

  case "$status" in
    DONE|CLOSED)
      echo "Spawn blocked: $disc_label status is $status — work is already complete." >&2
      return 1
      ;;
    SPEC_READY)
      # SPEC_READY says the Spec is written. BLOCKED-BY says whether it may be
      # started yet — the two are separate questions (D#1755). Refs are read from
      # the same authoritative line, so a BLOCKED-BY quoted in prose is inert.
      local blockers
      if ! blockers=$(printf '%s\n' "$body" \
            | python3 "$_SPEC_READY_GATE_REPO_ROOT/backend/blocked_by.py" check --stdin 2>/dev/null); then
        echo "Spawn blocked: $disc_label is SPEC_READY but has unresolved BLOCKED-BY refs: ${blockers:-<resolution failed>}" >&2
        echo "  The Spec is finished; implementation must wait until those refs clear. Do not override." >&2
        return 1
      fi
      return 0
      ;;
    UNKNOWN|"")
      echo "Spawn blocked: $disc_label has no authoritative STATUS marker — run project-manager first to write a Spec." >&2
      return 1
      ;;
    *)
      echo "Spawn blocked: $disc_label is not SPEC_READY — run project-manager first to write a Spec." >&2
      echo "  (Override with SPAWN_AGENT_ALLOW_NO_SPEC=1 only when a sub-PR inherits an umbrella spec — document the parent in the spawn context.)" >&2
      return 1
      ;;
  esac
}
