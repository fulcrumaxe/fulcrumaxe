#!/usr/bin/env bash
# scripts/hooks/post-agent.d/verdict-label.sh — verdict → label relay (D#2031).
#
# Roles running in a worktree could not reliably record their own verdict as
# a PR label: their role docs named a REST spelling the sandbox blocks. This
# is defense-in-depth for that — if the agent's own `apply_label` call (see
# scripts/lib/gh-label.sh) failed for any reason, this relay applies the same
# label mechanically from the envelope the agent already reported.
#
# Set VERDICT_LABEL_RELAY=0 to disable (used to prove the agent's own write
# is what succeeded, independent of this relay).
#
# Two ways to invoke:
#   - Sourced by post-agent-hook.sh: reads ROLE, VERDICT, PR from the
#     environment (already exported by the hub, same pattern as
#     verdict-overturn.sh).
#   - Run directly: `bash verdict-label.sh --role X --verdict Y --pr N`
#     (used by tests/test_verdict_label_relay.sh).
#
# Mapping is the only thing this file knows about roles/verdicts — the hub
# (post-agent-hook.sh) contains no mapping logic, only registration.

# _verdict_label_map <role> <verdict> — echoes the label name, or empty for
# an unmapped (role, verdict) pair.
_verdict_label_map() {
  local role="$1" verdict="$2"
  case "${role}:${verdict}" in
    code-reviewer:pass)          echo "code-review-passed" ;;
    code-reviewer:needs-fix)     echo "code-review-needs-fix" ;;
    security-reviewer:pass)      echo "security-review-passed" ;;
    security-reviewer:skip)      echo "security-review-passed" ;;
    acceptance-tester:pass)      echo "acceptance-passed" ;;
    acceptance-tester:fail)      echo "acceptance-failed" ;;
    *)                           echo "" ;;
  esac
}

# _verdict_label_exclusions <role> <verdict> — echoes the space-separated set
# of labels that must be cleared before applying this (role, verdict)'s own
# label (D#2066). Each pair here is a mutually-exclusive statement about the
# same PR; accumulating both is the bug this relay exists to fix.
#
# The security group covers the three negative synonyms _NACK_LABELS already
# treats as equivalent (scripts/loop-phased-step5.sh:206-215) — clearing only
# one of them would leave a different NACK label standing and the PR stuck
# regardless.
_verdict_label_exclusions() {
  local role="$1" verdict="$2"
  case "${role}:${verdict}" in
    code-reviewer:pass)          echo "code-review-needs-fix" ;;
    code-reviewer:needs-fix)     echo "code-review-passed" ;;
    security-reviewer:pass)      echo "security-needs-fix security-review-needs-fix security-issue" ;;
    security-reviewer:skip)      echo "security-needs-fix security-review-needs-fix security-issue" ;;
    acceptance-tester:pass)      echo "acceptance-failed" ;;
    acceptance-tester:fail)      echo "acceptance-passed" ;;
    *)                           echo "" ;;
  esac
}

# _verdict_label_apply <role> <verdict> <pr> — clears the (role, verdict)'s
# exclusion group, then applies the mapped label, if any. Returns 0 for a
# no-op (unmapped pair, missing PR, or relay disabled) and the real
# apply_label exit status otherwise. A remove_label failure is reported on
# stderr but never blocks the apply_label that follows (D#2066 AC-7) — a
# missing clear is the pre-existing bug; a blocked apply would be worse.
_verdict_label_apply() {
  local role="$1" verdict="$2" pr="$3" label

  if [[ "${VERDICT_LABEL_RELAY:-1}" == "0" ]]; then
    echo "[verdict-label] VERDICT_LABEL_RELAY=0 — relay disabled, skipping" >&2
    return 0
  fi

  label="$(_verdict_label_map "$role" "$verdict")"
  if [[ -z "$label" ]]; then
    return 0
  fi

  if [[ -z "$pr" ]]; then
    echo "[verdict-label] no PR number for ${role}:${verdict} — skipping label '${label}'" >&2
    return 0
  fi

  # shellcheck source=../../lib/gh-label.sh
  source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/gh-label.sh"

  local excl x
  excl="$(_verdict_label_exclusions "$role" "$verdict")"
  for x in $excl; do
    if ! remove_label "$pr" "$x"; then
      echo "[verdict-label] WARN: failed to remove '${x}' from #${pr} while applying '${label}' (${role}:${verdict})" >&2
    fi
  done

  if apply_label "$pr" "$label"; then
    echo "[verdict-label] applied '${label}' to #${pr} (${role}:${verdict})"
    return 0
  fi
  echo "[verdict-label] WARN: failed to apply '${label}' to #${pr} (${role}:${verdict})" >&2
  return 1
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  # Run directly (tests) — parse flags.
  _VL_ROLE="" _VL_VERDICT="" _VL_PR=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --role)    _VL_ROLE="$2";    shift 2 ;;
      --verdict) _VL_VERDICT="$2"; shift 2 ;;
      --pr)      _VL_PR="$2";      shift 2 ;;
      *) shift ;;
    esac
  done
  _verdict_label_apply "$_VL_ROLE" "$_VL_VERDICT" "$_VL_PR"
  exit $?
else
  # Sourced by post-agent-hook.sh — ROLE, VERDICT, PR already exported.
  _verdict_label_apply "${ROLE:-}" "${VERDICT:-}" "${PR:-}"
fi
