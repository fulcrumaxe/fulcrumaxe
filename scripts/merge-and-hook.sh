#!/usr/bin/env bash
# scripts/merge-and-hook.sh — Team Lead merge wrapper.
#
# Usage: bash scripts/merge-and-hook.sh --pr <PR_NUMBER> [--discussion <DISC_NUMBER>]
#                                        [--force-no-two-gate [--bypass-reason <text>]]
#                                        [--force-no-ci [--bypass-reason <text>]]
#
# 1. Checks that the PR body contains Two-Gate markers (Gate 1 + Gate 2).
#    Abort with exit 1 if markers are absent.
#    --force-no-two-gate bypasses the check but logs loudly + writes an audit row.
# 2. CI-status gate (D#1614): blocks the merge unless every required GitHub
#    Actions check-run (tui, dashboard, ts-backend, backend (import-smoke)) is
#    present and green on the current head. --force-no-ci bypasses this but
#    logs loudly + writes an audit row (kind: manual_merge_ci_bypass), and
#    now REQUIRES a non-empty --bypass-reason. The gate runs before the
#    bypass is applied, so that row records the head SHA and failing checks
#    that were actually overridden instead of three empty strings.
#    When the repo variable CI_DISABLED is 'true' the gate stands down
#    (kind: ci_gate_stood_down) and the merge proceeds without a CI signal —
#    a distinct outcome from both "green" and "overridden" (D#1944).
# 3. Merges the PR via squash, SHA-pinned to the head that was CI-gate-
#    evaluated; re-gates once on a 409 head-moved conflict. Deletes the
#    branch unless scripts/lib/pr-dependents.sh finds an open PR still
#    based on it (D#2020) — deleting a branch an open PR depends on closes
#    that PR, so the branch is kept instead and a warning is printed
#    naming the dependent(s) and how to retarget them.
# 4. On success, runs post-merge-hook.sh with the same PR/discussion args.
# 5. Tees hook output to .autonomous-team/dashboard-logs/manual-merge-<PR>.log.
# 6. Exits with the hook's exit code so callers can detect failure.
#
# This ensures that manual merges by Team Lead always run the same post-merge
# bookkeeping (stats, team-log, wiki sync, etc.) as the loop auto-merge path.
#
# ⚠ BACKGROUND EXECUTION REQUIRED (D#1614 AC-11): the CI-status gate blocks up
# to CI_MAX_WAIT_SECONDS (default 1200s / 20 min) waiting on GitHub Actions.
# When Team Lead invokes this script directly via Agent()/Bash, it MUST pass
# run_in_background: true — a foreground call now stalls the whole session for
# up to 20 minutes (see memory feedback_run_agents_in_background). This was
# already true pre-D#1614 for the post-merge-hook tail; it is now the dominant
# cost of a call to this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# Every gh call in this script is PR-side — labels, head SHA, base ref, the
# merge itself, the dependents lookup, the CI check-runs — so they all take the
# code slug. The one Discussion-side read is the HG-7 resolution below, which
# needs both: the PR body lives in the code repo and the Discussion it names
# lives in the Discussion repo. _DISCUSSION_REPO is legitimately empty in a
# fork with no private twin; resolve_pr_discussion falls back to the code slug
# in that case, which is what it did before there were two names for this.
_CODE_REPO="$(_resolve_code_repo)"
_DISCUSSION_REPO="$(_resolve_discussion_repo)"

# shellcheck source=scripts/lib/two-gate-check.sh
source "$SCRIPT_DIR/lib/two-gate-check.sh"
# shellcheck source=scripts/lib/resolve-pr-discussion.sh
source "$SCRIPT_DIR/lib/resolve-pr-discussion.sh"
# shellcheck source=scripts/lib/ci-status-check.sh
source "$SCRIPT_DIR/lib/ci-status-check.sh"
# shellcheck source=scripts/lib/pr-dependents.sh
source "$SCRIPT_DIR/lib/pr-dependents.sh"

# ── Argument parsing ──────────────────────────────────────────────────────────
PR=""
DISC=""
FORCE_NO_TWO_GATE=false
FORCE_NO_CI=false
BYPASS_REASON=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)               PR="$2";            shift 2 ;;
    --discussion)       DISC="$2";          shift 2 ;;
    --force-no-two-gate) FORCE_NO_TWO_GATE=true; shift 1 ;;
    --force-no-ci)      FORCE_NO_CI=true;    shift 1 ;;
    --bypass-reason)    BYPASS_REASON="$2"; shift 2 ;;
    *)
      echo "[merge-and-hook] unknown argument: $1" >&2
      echo "Usage: $0 --pr <PR_NUMBER> [--discussion <DISC_NUMBER>] [--force-no-two-gate [--bypass-reason <text>]] [--force-no-ci [--bypass-reason <text>]]" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "[merge-and-hook] --pr is required" >&2
  exit 1
fi

# --force-no-ci suppresses the only machine signal that anything was verified.
# An unexplained one leaves an audit row that records the act and nothing about
# why, which is what made the existing rows uninterpretable. Validated here,
# before ANY side effect, so a rejected invocation leaves no audit row at all.
if [[ "$FORCE_NO_CI" == "true" && -z "${BYPASS_REASON//[[:space:]]/}" ]]; then
  echo "[merge-and-hook] ERROR: --force-no-ci requires --bypass-reason <text> (non-empty). Refusing to merge PR #$PR." >&2
  echo "[merge-and-hook] Say what you are overriding and why — the audit row is the only record this merge was not CI-verified." >&2
  exit 1
fi

LOG_DIR="${MERGE_AND_HOOK_LOG_DIR:-$REPO_ROOT/.autonomous-team/dashboard-logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/manual-merge-${PR}.log"

# ── Step 0: Two-Gate marker check ─────────────────────────────────────────────
if [[ "$FORCE_NO_TWO_GATE" == "true" ]]; then
  # Escape hatch — log loudly and write audit row, then continue.
  echo "[merge-and-hook] WARNING: --force-no-two-gate used for PR #$PR — bypassing Two-Gate check!" >&2
  echo "[merge-and-hook] WARNING: This bypass is logged to the audit trail. Use sparingly." >&2
  if [[ -n "$BYPASS_REASON" ]]; then
    echo "[merge-and-hook] Bypass reason: $BYPASS_REASON" >&2
  fi

  # Write audit row to <state_dir>/audit.jsonl
  _AUDIT_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
  _AUDIT_FILE="$_AUDIT_DIR/audit.jsonl"
  _AUDIT_USER="${USER:-unknown}"
  _AUDIT_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
  _AUDIT_REASON="${BYPASS_REASON:-}"
  mkdir -p "$_AUDIT_DIR"
  printf '%s\n' "{\"kind\":\"manual_merge_two_gate_bypass\",\"pr\":$PR,\"user\":\"$_AUDIT_USER\",\"timestamp\":\"$_AUDIT_TS\",\"reason\":\"$_AUDIT_REASON\"}" >> "$_AUDIT_FILE"
  echo "[merge-and-hook] Audit row written: kind=manual_merge_two_gate_bypass pr=$PR" >&2
else
  if ! check_two_gate_markers "$PR" "$_CODE_REPO"; then
    echo "[merge-and-hook] Two-Gate check FAILED for PR #$PR: $TWO_GATE_FAIL_REASON" >&2
    echo "[merge-and-hook] Add Gate 1 and Gate 2 markers to the PR body, or use --force-no-two-gate to bypass." >&2
    exit 1
  fi
  echo "[merge-and-hook] Two-Gate check passed for PR #$PR."
fi

# ── Step 0b: HG-7 (D#1588 Batch B) — external-provenance forces security review ──
# A PR whose originating Discussion is provenance:external is NOT eligible for
# the Team-Lead direct-merge exception (CLAUDE.md "Merge Gate Protocol") —
# security-review-passed is a hard requirement here too, regardless of PR size
# or whether the diff itself looks trivial. --force-no-two-gate does not bypass
# this check; it only bypasses the Two-Gate marker check above.
#
# Security-needs-fix (D#1588 Batch B round 2): this check used to be skippable
# just by omitting --discussion — an optional flag on this script. Now, when
# --discussion isn't passed, we derive the Discussion number from the PR body's
# own Closes/Fixes/Resolves D#N reference (same resolver post-merge-hook.sh
# uses). If the Discussion genuinely cannot be resolved, we FAIL CLOSED and
# refuse the direct-merge outright — "no discussion found" is never treated as
# "check not applicable", because that just moves the bypass one layer down.
_RESOLVED_DISC="$DISC"
if [[ -z "$_RESOLVED_DISC" ]]; then
  _RESOLVED_DISC="$(resolve_pr_discussion "$PR" "$_CODE_REPO" "$_DISCUSSION_REPO" || true)"
  if [[ -n "$_RESOLVED_DISC" ]]; then
    echo "[merge-and-hook] Auto-detected Discussion #$_RESOLVED_DISC from PR #$PR body for the HG-7 check." >&2
  fi
fi

if [[ -z "$_RESOLVED_DISC" ]]; then
  echo "[merge-and-hook] ERROR: could not resolve a Discussion number for PR #$PR (no --discussion flag given, and no resolvable 'Closes/Fixes/Resolves D#N' reference in the PR body). Refusing the direct-merge shortcut — HG-7 (external-provenance forces security review) cannot be verified without a Discussion. Pass --discussion explicitly, or add a resolvable closing reference to the PR body." >&2
  exit 1
fi

_SEC_REQUIRED_RC=0
python3 "$REPO_ROOT/scripts/lib/external_intake_gate.py" security-required "$_RESOLVED_DISC" >/dev/null 2>&1 || _SEC_REQUIRED_RC=$?
# Exit-code contract (external_intake_gate.py security-required):
#   0 = required (provenance:external label confirmed present)
#   1 = confirmed NOT required (fetch succeeded, label confirmed absent)
#   3 = unknown/fetch failed — fail closed, treat as required (HG-1 invariant)
#   4 = required AND the originating Discussion's intake approval has been
#       dismissed by a post-approval edit since (R6, D#1672 merge-gate
#       re-check) — hard-blocked below regardless of PR size or the
#       Team-Lead direct-merge exception. An un-updated caller that only
#       checks `rc == 1` for "not required" falls into the existing
#       `else -> required` branch for rc=4 too, so this is backward-safe.
#   anything else = fail closed, treat as required
if [[ "$_SEC_REQUIRED_RC" -eq 4 ]]; then
  echo "[merge-and-hook] ERROR: Discussion #$_RESOLVED_DISC's intake approval was dismissed by a post-approval edit — the content a human approved is not necessarily the content this PR was built from. Refusing to merge. A maintainer must review the current Discussion body and re-apply intake-approved before this PR can merge." >&2
  exit 1
fi
if [[ "$_SEC_REQUIRED_RC" -eq 1 ]]; then
  _EXTERNAL_FORCES_SEC="false"
else
  _EXTERNAL_FORCES_SEC="true"
fi

if [[ "$_EXTERNAL_FORCES_SEC" == "true" ]]; then
  if [[ "$_SEC_REQUIRED_RC" -eq 0 ]]; then
    echo "[merge-and-hook] Discussion #$_RESOLVED_DISC is provenance:external — security-review-passed is a hard requirement (HG-7); the direct-merge exception does not apply." >&2
  else
    echo "[merge-and-hook] Could not confirm Discussion #$_RESOLVED_DISC's provenance label (rc=$_SEC_REQUIRED_RC, GitHub API fetch failed/unknown) — failing closed and treating security-review-passed as required (HG-1)." >&2
  fi
  _PR_LABELS="$(gh pr view "$PR" --repo "$_CODE_REPO" --json labels --jq '.labels[].name' 2>/dev/null || echo "")"
  if ! grep -qx "security-review-passed" <<<"$_PR_LABELS"; then
    echo "[merge-and-hook] ERROR: PR #$PR traces back to Discussion #$_RESOLVED_DISC but lacks the security-review-passed label. Refusing to merge." >&2
    exit 1
  fi
  echo "[merge-and-hook] security-review-passed present — HG-7 requirement satisfied."
fi

# ── Step 0c: CI-status gate (D#1614) ──────────────────────────────────────────
# Real GitHub Actions CI must gate the merge, not run decoratively after it —
# GitHub-native required-status-checks is unavailable on this repo tier (403).
_CI_GREEN_SHA=""
# D#2271 PR-a: true once a decline-reason row (ci_gate_stood_down /
# manual_merge_ci_bypass) has actually been written for this run, so the
# ci_note_merge_if_unverified call at the bottom of this script knows not to
# add a second, redundant fallback row on top of one that already exists.
_CI_AUDIT_WRITTEN=false

# Provenance ordering (AC-15): an external-provenance PR touching
# .github/workflows/** cannot self-certify its own CI result until the
# D#1588 intake-approved human gate has cleared. --force-no-ci has never
# bypassed this and still does not.
if [[ "$FORCE_NO_CI" != "true" ]]; then
  if ! check_ci_provenance_gate "$PR" "$_CODE_REPO" "$_RESOLVED_DISC"; then
    echo "[merge-and-hook] ERROR: CI-status gate refused for PR #$PR: $CI_STATUS_FAIL_REASON" >&2
    ci_write_audit "ci_gate_block" "$PR" "" "" "" "$CI_STATUS_FAIL_REASON"
    exit 1
  fi
fi

# The gate runs even when --force-no-ci is set, and the override is applied to
# its RESULT rather than skipping it. That ordering is the whole point: before
# this, the bypass short-circuited first, so the audit row it wrote had nothing
# to record — head_sha, failing_checks and run_url went in as empty strings in
# every stored row, and you could not tell "checks absent because CI is off"
# from "something was genuinely red". No --wait under the bypass: there is no
# reason to sit on a 20-minute poll for a result we are about to override.
if [[ "$FORCE_NO_CI" != "true" ]]; then
  echo "[merge-and-hook] waiting on CI status for PR #$PR (bounded, up to ${CI_MAX_WAIT_SECONDS}s)..."
fi
_CI_RC=0
if [[ "$FORCE_NO_CI" == "true" ]]; then
  check_ci_status "$PR" "$_CODE_REPO" || _CI_RC=$?
else
  check_ci_status "$PR" "$_CODE_REPO" --wait || _CI_RC=$?
fi

if [[ "$FORCE_NO_CI" == "true" ]]; then
  echo "[merge-and-hook] WARNING: --force-no-ci used for PR #$PR — bypassing CI-status gate!" >&2
  echo "[merge-and-hook] WARNING: This bypass is logged to the audit trail. Use sparingly." >&2
  echo "[merge-and-hook] Bypass reason: $BYPASS_REASON" >&2
  echo "[merge-and-hook] Overriding CI state=${CI_STATUS_STATE:-unknown} at sha=${CI_STATUS_HEAD_SHA:-unknown}: ${CI_STATUS_FAIL_REASON:-none}" >&2
  ci_write_audit "manual_merge_ci_bypass" "$PR" "$CI_STATUS_HEAD_SHA" "$CI_STATUS_FAILING_CHECKS" "$CI_STATUS_RUN_URL" "$BYPASS_REASON"
  _CI_AUDIT_WRITTEN=true
  echo "[merge-and-hook] Audit row written: kind=manual_merge_ci_bypass pr=$PR" >&2
elif [[ "$_CI_RC" -eq 2 ]]; then
  # Stand-down, not a pass. CI is switched off at the repo variable, so no
  # check-run can exist for this head and no amount of waiting will change
  # that. We proceed — but this merge carries NO CI signal, and the audit row
  # says exactly that in its own kind so `manual_merge_ci_bypass` keeps
  # meaning "a human overrode a real signal".
  echo "[merge-and-hook] CI gate STOOD DOWN for PR #$PR — repo variable CI_DISABLED is 'true', so CI did not run and there is nothing to verify." >&2
  echo "[merge-and-hook] This merge is NOT CI-verified. Proceeding." >&2
  ci_write_audit "ci_gate_stood_down" "$PR" "$CI_STATUS_HEAD_SHA" "" "" "CI_DISABLED=true — CI did not run, merge proceeding with no CI signal"
  _CI_AUDIT_WRITTEN=true
  echo "[merge-and-hook] Audit row written: kind=ci_gate_stood_down pr=$PR" >&2
elif [[ "$_CI_RC" -ne 0 ]]; then
  echo "[merge-and-hook] CI-status gate FAILED for PR #$PR: $CI_STATUS_FAIL_REASON" >&2
  [[ -n "$CI_STATUS_FAILING_CHECKS" ]] && echo "[merge-and-hook] failing check(s): $CI_STATUS_FAILING_CHECKS" >&2
  [[ -n "$CI_STATUS_RUN_URL" ]] && echo "[merge-and-hook] run: $CI_STATUS_RUN_URL" >&2
  echo "[merge-and-hook] Refusing to merge. Use --force-no-ci to override (audited, use sparingly)." >&2
  ci_write_audit "ci_gate_block" "$PR" "$CI_STATUS_HEAD_SHA" "$CI_STATUS_FAILING_CHECKS" "$CI_STATUS_RUN_URL" "$CI_STATUS_FAIL_REASON"
  exit 1
else
  _CI_GREEN_SHA="$CI_STATUS_HEAD_SHA"
  echo "[merge-and-hook] CI-status gate passed for PR #$PR at sha=$_CI_GREEN_SHA."
fi

# ── Step 1: Merge (SHA-pinned, TOCTOU-safe) ───────────────────────────────────
# Re-read the current head immediately before merging. If it moved since the
# CI-gate evaluation, re-run the gate against the new head rather than merging
# a stale-green result (D#1614 AC-8).
_CUR_HEAD="$(gh pr view "$PR" --repo "$_CODE_REPO" --json headRefOid --jq .headRefOid 2>/dev/null || echo "")"
if [[ -z "$_CUR_HEAD" ]]; then
  echo "[merge-and-hook] ERROR: could not resolve current head SHA for PR #$PR." >&2
  exit 1
fi
if [[ -n "$_CI_GREEN_SHA" && "$_CUR_HEAD" != "$_CI_GREEN_SHA" ]]; then
  echo "[merge-and-hook] head moved since CI check ($_CI_GREEN_SHA -> $_CUR_HEAD) — re-gating before merge." >&2
  if ! check_ci_status "$PR" "$_CODE_REPO"; then
    echo "[merge-and-hook] CI-status gate FAILED after re-gate for PR #$PR: $CI_STATUS_FAIL_REASON" >&2
    ci_write_audit "ci_gate_block" "$PR" "$CI_STATUS_HEAD_SHA" "$CI_STATUS_FAILING_CHECKS" "$CI_STATUS_RUN_URL" "$CI_STATUS_FAIL_REASON"
    exit 1
  fi
  _CI_GREEN_SHA="$CI_STATUS_HEAD_SHA"
fi
_MERGE_SHA="${_CI_GREEN_SHA:-$_CUR_HEAD}"

# ── Dependents check (D#2020) ────────────────────────────────────────────────
# Never delete a branch an open PR is still based on — deletion is what
# closes it (base_ref_deleted -> dependent closed, one second apart in the
# recorded incident). Mechanism lives in pr-dependents.sh; this host owns the
# policy branch and the same errexit-safe guard the merge call below needs,
# for the same reason: this script runs under `set -euo pipefail` (see the
# top of the file, not a line number that will drift on the next edit).
_DELETE_BRANCH_MODE="delete"
_DEP_RC=0
pr_dependents_list "$PR" "$_CODE_REPO" || _DEP_RC=$?
if [[ "$_DEP_RC" -ne 0 ]]; then
  echo "[merge-and-hook] pr-dependents lookup failed (${PR_DEP_REASON:-unknown reason}) — keeping branch as a precaution, merge proceeds." >&2
  _DELETE_BRANCH_MODE="keep"
elif [[ -n "${PR_DEP_LIST:-}" ]]; then
  pr_dependents_report "$PR" "$_CODE_REPO" >&2
  _DELETE_BRANCH_MODE="keep"
fi

echo "[merge-and-hook] merging PR #$PR (squash, delete-branch=$([[ "$_DELETE_BRANCH_MODE" == "delete" ]] && echo yes || echo no), sha=$_MERGE_SHA)..."
# Bounded: at most one 409 re-gate retry (two attempts total), never open-ended.
_MERGE_OK=false
for _MERGE_ATTEMPT in 1 2; do
  # `|| _MRC=$?` is load-bearing: `set -euo pipefail` is on (line 32), so a
  # bare `ci_merge_sha_pinned ...` returning non-zero aborts the shell right
  # here -- `_MRC=$?` never runs and neither error branch below can fire. That
  # is exactly the silent `exit 1` this guard fixes, and it also revives the
  # D#1614 409 head-moved retry, which used to abort with exit 9 instead.
  # Do NOT replace this with `set +e`; that would disarm the rest of the file.
  _MRC=0
  ci_merge_sha_pinned "$PR" "$_CODE_REPO" "$_MERGE_SHA" "$_DELETE_BRANCH_MODE" || _MRC=$?
  if [[ "$_MRC" -eq 0 ]]; then
    _MERGE_OK=true
    break
  elif [[ "$_MRC" -eq 9 && "$_MERGE_ATTEMPT" -eq 1 ]]; then
    echo "[merge-and-hook] merge returned a head-moved conflict — re-gating once and retrying: $CI_STATUS_FAIL_REASON" >&2
    _MERGE_SHA="$(gh pr view "$PR" --repo "$_CODE_REPO" --json headRefOid --jq .headRefOid 2>/dev/null || echo "")"
    # rc=2 is the CI_DISABLED stand-down, not a block — the merge was already
    # allowed to proceed without a CI signal above, and a head move does not
    # turn CI back on.
    _REGATE_RC=0
    check_ci_status "$PR" "$_CODE_REPO" || _REGATE_RC=$?
    if [[ -z "$_MERGE_SHA" || ( "$_REGATE_RC" -ne 0 && "$_REGATE_RC" -ne 2 ) ]]; then
      echo "[merge-and-hook] CI-status gate FAILED after 409 re-gate for PR #$PR: ${CI_STATUS_FAIL_REASON:-could not resolve new head}" >&2
      ci_write_audit "ci_gate_block" "$PR" "${_MERGE_SHA:-}" "$CI_STATUS_FAILING_CHECKS" "$CI_STATUS_RUN_URL" "${CI_STATUS_FAIL_REASON:-head unresolved}"
      exit 1
    fi
    _MERGE_SHA="$CI_STATUS_HEAD_SHA"
  else
    echo "[merge-and-hook] ERROR: merge failed for PR #$PR: $CI_STATUS_FAIL_REASON" >&2
    if [[ "${CI_STATUS_FAIL_KIND:-}" == "conflict" ]]; then
      echo "[merge-and-hook] cause: this branch conflicts with its base and is not mergeable." >&2
      echo "[merge-and-hook] remedy: merge main into the branch and resolve the conflicts, then re-run this script." >&2
      _BASE_REF="$(gh pr view "$PR" --repo "$_CODE_REPO" --json baseRefName --jq .baseRefName 2>/dev/null || true)"
      if [[ -z "$_BASE_REF" ]]; then
        _BASE_REF="main"
      fi
      # Best-effort and non-fatal. When the paths cannot be computed we say so
      # AND say why -- a silently missing list reads as "no conflicts".
      # `|| true` for the same reason as the guard on the merge call above:
      # nothing in this diagnostic path may abort before the operator reads it.
      ci_conflicting_files "$_BASE_REF" "$_MERGE_SHA" || true
      if [[ -n "$CI_CONFLICT_FILES" ]]; then
        echo "[merge-and-hook] conflicting files:" >&2
        printf '%s\n' "$CI_CONFLICT_FILES" | sed 's/^/[merge-and-hook]   /' >&2
      else
        echo "[merge-and-hook] conflicting files: unavailable (${CI_CONFLICT_FILES_REASON:-reason unknown})" >&2
      fi
    fi
    exit 1
  fi
done
if [[ "$_MERGE_OK" != "true" ]]; then
  echo "[merge-and-hook] ERROR: merge did not succeed for PR #$PR after re-gate retry." >&2
  exit 1
fi
echo "[merge-and-hook] PR #$PR merged."

# D#2271 PR-a: no-op when this merge was actually CI-verified or when a
# decline reason was already recorded above; otherwise leaves the fallback
# marker backend/gate_streak.py counts. See ci_note_merge_if_unverified's
# doc comment in ci-status-check.sh.
ci_note_merge_if_unverified "$PR" "$_MERGE_SHA" "$_CI_AUDIT_WRITTEN"

# ── Step 2: Post-merge hook ───────────────────────────────────────────────────
HOOK_ARGS=(--pr "$PR")
if [[ -n "$DISC" ]]; then
  HOOK_ARGS+=(--discussion "$DISC")
fi

echo "[merge-and-hook] running post-merge-hook.sh for PR #$PR (log: $LOG_FILE)"
set +e
bash "$SCRIPT_DIR/post-merge-hook.sh" "${HOOK_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
HOOK_EXIT="${PIPESTATUS[0]}"
set -e

if [[ $HOOK_EXIT -ne 0 ]]; then
  echo "[merge-and-hook] WARNING: post-merge-hook.sh exited $HOOK_EXIT — see $LOG_FILE" >&2
fi

exit "$HOOK_EXIT"
