#!/usr/bin/env bash
# scripts/lib/sanitize-echo.sh — wrap externally-influenceable text before it
# is echoed into a comment our own bot signs (D#2415 PR-b).
#
# scripts/lib/pr_comment_trust.py partitions PR comments by AUTHENTICATED
# AUTHOR LOGIN — is_trusted_author(login, allowlist) never sees a comment
# body. That partitions AUTHORSHIP, not PROVENANCE: when our own bot posts a
# comment that echoes bytes it read from somewhere else (a PR diff's own file
# paths, a scorer's diff-derived detail string, a CI check name the head's own
# workflow file defines), those bytes land in the TRUSTED half purely because
# the account posting them is trusted — not because the bytes themselves are.
# The partition cannot defend against its own side by construction; the fix
# has to sit at each site that echoes such text into a bot-signed comment, on
# write, which is what this wraps.
#
# Delegates entirely to external_intake_gate.py's "sanitize" CLI subcommand
# (HG-5): strip known control-plane tokens (SPAWN_REQUEST:, TERMINATE_REQUEST:,
# forged AGENT_OUTPUT/STATUS: HTML comments), then wrap the result in an
# explicit untrusted-content delimiter with the delimiter itself neutralized
# inside the body first, so a payload carrying a literal close-delimiter can't
# end the wrapper early. One implementation of that logic, not a second one
# drifting here.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/sanitize-echo.sh"
#   safe="$(sanitize_echo "$untrusted_value")"

_SANITIZE_ECHO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# sanitize_echo <text>
# Prints the sanitized, delimiter-wrapped text on stdout. Fails closed on a
# broken delegate (missing python3, a crashing external_intake_gate.py, ...):
# the caller gets empty output rather than the raw untrusted text, but that
# would otherwise happen silently -- an inert sanitizer is worse when nobody
# can tell it went inert. A warning on stderr is the cheapest way to make
# that observable without changing the fail-closed behavior itself.
sanitize_echo() {
  local out rc
  out="$(printf '%s' "${1:-}" | python3 "$_SANITIZE_ECHO_DIR/external_intake_gate.py" sanitize)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "sanitize_echo: external_intake_gate.py sanitize failed (exit $rc) -- printing empty, not the raw input" >&2
  fi
  printf '%s' "$out"
}
