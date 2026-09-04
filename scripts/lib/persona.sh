#!/usr/bin/env bash
# scripts/lib/persona.sh — persona voice block helper for spawn-prompt injection.
#
# Usage (source or call directly):
#   source scripts/lib/persona.sh
#   persona_voice_block <role>       # prints ## Voice block, or nothing if no persona
#
# The ## Voice block is injected into spawn prompts by pre-spawn-check.sh so each
# agent receives persona context on every spawn (mitigates persona drift per arXiv 2511.00222).
#
# Voice applies ONLY to prose surfaces (Discussion comments, PR review prose, team-log).
# It does NOT apply to AGENT_OUTPUT envelopes, code, commit messages, or any machine output.
#
# See wiki/Persona-Layer.md for full documentation.

PERSONA_DIR="${PERSONA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.autonomous-team/personas}"

# persona_voice_block <role>
# Prints the ## Voice markdown block to stdout, or nothing if no persona file exists.
persona_voice_block() {
  local role="${1:-}"
  if [[ -z "$role" ]]; then
    return 0
  fi

  local persona_file="${PERSONA_DIR}/${role}.json"
  if [[ ! -f "$persona_file" ]]; then
    return 0
  fi

  # Extract fields with jq — if jq is missing or file is malformed, emit nothing.
  local name big_five_o big_five_c big_five_e big_five_a big_five_n values style conflict sign_off
  if ! command -v jq &>/dev/null; then
    return 0
  fi

  name=$(jq -r '.name // empty' "$persona_file" 2>/dev/null) || return 0
  if [[ -z "$name" ]]; then
    return 0
  fi

  big_five_o=$(jq -r '.big_five.openness // empty'          "$persona_file" 2>/dev/null)
  big_five_c=$(jq -r '.big_five.conscientiousness // empty' "$persona_file" 2>/dev/null)
  big_five_e=$(jq -r '.big_five.extraversion // empty'      "$persona_file" 2>/dev/null)
  big_five_a=$(jq -r '.big_five.agreeableness // empty'     "$persona_file" 2>/dev/null)
  big_five_n=$(jq -r '.big_five.neuroticism // empty'       "$persona_file" 2>/dev/null)
  values=$(jq -r '.values | join(", ")' "$persona_file" 2>/dev/null)
  style=$(jq -r '.style // empty'           "$persona_file" 2>/dev/null)
  conflict=$(jq -r '.conflict_pattern // empty' "$persona_file" 2>/dev/null)
  sign_off=$(jq -r '.sign_off // empty'      "$persona_file" 2>/dev/null)

  # Build the block
  printf '## Voice\n\n'
  printf 'You are **%s**. %s\n\n' "$name" "$style"
  printf 'Big Five (informs how you communicate, not what you decide): O=%s C=%s E=%s A=%s N=%s.\n\n' \
    "$big_five_o" "$big_five_c" "$big_five_e" "$big_five_a" "$big_five_n"
  printf 'Your values: %s.\n\n' "$values"
  printf 'Conflict pattern: %s\n\n' "$conflict"
  printf 'This voice applies ONLY to prose surfaces — Discussion comments, PR review prose, team-log lines. It does NOT apply to AGENT_OUTPUT envelope JSON, code, commit messages, structured data files, or any machine-clean output. Code and commit messages follow CLAUDE.md "human voice" rules unchanged.\n'
  if [[ -n "$sign_off" ]]; then
    printf '\nSign off your final prose surface with: %s\n' "$sign_off"
  fi
}

# Allow direct invocation: bash scripts/lib/persona.sh persona_voice_block <role>
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-}"
  shift || true
  case "$cmd" in
    persona_voice_block)
      persona_voice_block "$@"
      ;;
    *)
      echo "Usage: $0 persona_voice_block <role>" >&2
      exit 1
      ;;
  esac
fi
