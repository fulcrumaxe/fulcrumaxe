#!/usr/bin/env bash
# tests/test_no_heredoc_hook_copies.sh — fails when a test pastes the
# post-merge-hook auto_pull step into a heredoc instead of calling it (D#1948).
#
# Run: bash tests/test_no_heredoc_hook_copies.sh
# Expects: all assertions pass, exit 0
#
# The defect this guards: tests/test_post_merge_hook_pull.sh spent 754 commits
# asserting against a heredoc paste of auto_pull. The paste kept passing while
# the real step was rewritten around it — by the end it asserted a `rm -f` the
# hook no longer does and a message string the hook has never emitted, and it
# was cited as coverage on three briefs. A copy is invisible: nothing in the
# suite's own output says it is testing a copy.
#
# What counts as an offence, and why it is narrow: a heredoc body containing a
# fast-forward pull, or a `hook_event_mark_step` for the **auto_pull** step
# specifically. Not `hook_event_mark_step` in general — several suites legitimately
# stub or drive other steps from a heredoc (test_hooks_idempotency.sh,
# test_pre_spawn_token_cap.sh, test_post_agent_hook_recovery.sh,
# test_post_agent_hook_self_observe.sh). Flagging those would make this guard
# noise, and a noisy guard gets deleted. The two patterns below are the signature
# of a re-implementation of the step that runs after every merge, which is the
# thing that actually went wrong.
#
# Surviving copies go in the allowlist with the Discussion that will remove them,
# so a copy that cannot be fixed today is *registered* rather than silent — and a
# new one still fails on sight. A stale entry fails too: once the copy is gone,
# the entry has to go with it.
#
# Reads files as text. No temp dirs, no network, no API.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_DIR="$REPO_ROOT/tests"

# Each entry is "<path relative to repo root>|D#<number> — why it is still here".
# Removing an entry whose copy still exists must turn this suite red; that is the
# negative self-check D#1948 item 11 asks for.
ALLOWLIST=(
  "tests/test_post_merge_hook_unmerged_paths.sh|D#1976 — same heredoc copy, plus a git shim that hardcodes /usr/bin/git and is red under nix. Left byte-unmodified by the D#1948 PR on purpose, so it stayed a usable control for that change. D#1976 removes the copy; this entry goes with it."
)

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

# Prints one "file:line: text" per offending line inside a heredoc body.
scan_file() {
  awk '
    BEGIN { delim = "" }
    {
      if (delim != "") {
        t = $0
        sub(/^[ \t]+/, "", t)
        if (t == delim || $0 == delim) { delim = ""; next }
        if ($0 ~ /pull --ff-only/ || $0 ~ /hook_event_mark_step[ \t]*"?auto_pull/) {
          printf "%s:%d: %s\n", FILENAME, FNR, $0
        }
        next
      }
      line = $0
      # Blind the herestring operator first, or <<<"$x" reads as a heredoc whose
      # delimiter never arrives and the rest of the file is scanned as a body.
      gsub(/<<</, "@@@", line)
      if (match(line, /<<-?[ \t]*["'"'"']?[A-Za-z_][A-Za-z0-9_]*["'"'"']?/)) {
        d = substr(line, RSTART, RLENGTH)
        sub(/^<<-?[ \t]*/, "", d)
        gsub(/["'"'"']/, "", d)
        delim = d
      }
    }
  ' "$1"
}

allowlisted() {
  local path="$1" entry
  for entry in "${ALLOWLIST[@]}"; do
    [[ "${entry%%|*}" == "$path" ]] && return 0
  done
  return 1
}

echo "Heredoc copies of the post-merge-hook auto_pull step"

# ── The allowlist itself has to be well-formed ──────────────────────────────
for entry in "${ALLOWLIST[@]}"; do
  entry_path="${entry%%|*}"
  entry_note="${entry#*|}"
  if [[ -f "$REPO_ROOT/$entry_path" ]]; then
    pass "allowlisted file exists: $entry_path"
  else
    fail "allowlisted file does not exist (remove the entry): $entry_path"
  fi
  if [[ "$entry_note" =~ D#[0-9]+ ]]; then
    pass "allowlist entry names a Discussion: $entry_path"
  else
    fail "allowlist entry names no Discussion (D#<n>): $entry_path"
  fi
done

# ── Scan every shell suite ──────────────────────────────────────────────────
OFFENDERS=()
for f in "$TESTS_DIR"/*.sh; do
  [[ -f "$f" ]] || continue
  rel="tests/$(basename "$f")"
  hits="$(scan_file "$f")"
  [[ -z "$hits" ]] && continue
  OFFENDERS+=("$rel")
  if allowlisted "$rel"; then
    echo "  note: $rel carries a known copy (allowlisted)"
  else
    fail "$rel pastes the auto_pull step into a heredoc — call scripts/lib/auto-pull-step.sh instead"
    printf '%s\n' "$hits" | sed 's/^/        /' >&2
  fi
done

if [[ ${#OFFENDERS[@]} -eq ${#ALLOWLIST[@]} ]]; then
  pass "no unregistered heredoc copy of the auto_pull step"
else
  # Already reported per-file above; this keeps the count honest in the summary.
  fail "found ${#OFFENDERS[@]} file(s) with a heredoc copy, ${#ALLOWLIST[@]} allowlisted"
fi

# ── A stale allowlist entry is a failure too ────────────────────────────────
for entry in "${ALLOWLIST[@]}"; do
  entry_path="${entry%%|*}"
  still_offends=false
  for o in "${OFFENDERS[@]:-}"; do
    [[ "$o" == "$entry_path" ]] && still_offends=true
  done
  if [[ "$still_offends" == "true" ]]; then
    pass "allowlist entry is still load-bearing: $entry_path"
  else
    fail "allowlist entry is stale — the copy is gone, delete the entry: $entry_path"
  fi
done

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
