#!/usr/bin/env bash
# scripts/lib/pr-dependents.sh — detect open PRs whose base branch is the
# branch of the PR currently being merged (D#2020).
#
# NOTE: this is about PR dependency chains, not git worktrees — see
# scripts/lib/pr-tree.sh for that unrelated thing. The name collision was
# flagged during D#2020's review specifically so this file doesn't get
# confused with, or merged into, pr-tree.sh.
#
# Mechanism only, no policy: this lib never mutates a PR, never aborts, and
# never decides whether the caller should merge. It answers exactly one
# question — "does deleting this branch have to break something?" — and
# leaves the decision (skip --delete-branch, or not) to each merge host
# (scripts/merge-and-hook.sh, scripts/loop-phased-step5.sh).
#
# Why not retarget the dependents instead of keeping the branch: D#2020's
# panel found that a retarget changes a PR's effective diff without moving
# its head SHA, and the D#1777 stale-pass invalidator
# (scripts/loop-phased-step5.sh's _invalidate_stale_pass_labels) only
# watches `labeled` and `head_ref_force_pushed` timeline events —
# `base_ref_changed` is not among them. Automating a retarget would carry
# `code-review-passed` onto an unreviewed diff on the unattended merge
# path. Suppressing --delete-branch touches nothing but the merge call
# already in flight.
#
# Public contract:
#   pr_dependents_list <pr> <repo>
#     Sets PR_DEP_LIST   — newline-separated open PR numbers whose
#                          baseRefName equals <pr>'s headRefName (empty
#                          string if there are none)
#     Sets PR_DEP_REASON — one-line human-readable explanation, including
#                          the real gh/API error text on a lookup failure
#                          (not just an exit code)
#     Sets PR_DEP_BRANCH — the headRefName that was checked (used by
#                          pr_dependents_report)
#     Returns 0 = the answer above is known — including a clean "no
#                 dependents" result. An empty PR_DEP_LIST is NOT itself a
#                 failure signal; callers must branch on the return code,
#                 never infer failure from an empty list.
#     Returns 1 = unknown (a lookup failed for any reason, INCLUDING the
#                 open-PR page coming back exactly at its --limit cap —
#                 see D#2020 fix-cycle 1 below). Callers must treat this
#                 the same as "dependents may exist" and keep the branch
#                 — see each host's fail-safe handling.
#
#   pr_dependents_report <pr> [repo]
#     Prints (to stdout) a human-facing warning naming each open dependent,
#     the branch being kept, and the `gh pr edit <n> --base main` command
#     an operator would run to unstick it, plus why that matters (the
#     kept branch is on a base no longer in main's line). Callers redirect
#     this to wherever their own warnings go (stderr, team-log, etc).
#     Only meaningful after a pr_dependents_list call for the same <pr>
#     left PR_DEP_LIST non-empty. Never fails; always returns 0. <repo>
#     defaults to _resolve_repo when omitted (used only to format the
#     suggested retarget command).
#
# D#2020 fix-cycle 1 (security review): `gh pr list` defaults to --limit 30
# and truncates SILENTLY past that — `rc=0` with exactly `limit` entries is
# byte-indistinguishable from a genuine empty/short result. With more than
# the configured limit of open PRs, a real dependent sitting outside the
# page would have made this function report "no dependents", which is
# exactly the incident D#2020 exists to prevent, reintroduced inside the
# detector built to prevent it. Fixed two ways:
#   1. Pass an explicit high --limit (default 1000 — see PR_DEP_LIST_LIMIT
#      below; matches house convention, e.g. backend/release_backfill.py's
#      `--limit 1000` for the same "enumerate the whole PR list in one
#      call" shape; backend/cost_per_outcome.py:44 uses --limit 200 for a
#      narrower merged-only query).
#   2. Treat the raw (pre-filter) open-PR count landing exactly on that
#      limit as unknown (return 1), not "no more entries" — so hitting the
#      cap again can never again read as an empty answer. Raising the
#      number alone only moves the cliff; this makes hitting it loud.
#
# Test mode (PR_DEPENDENTS_TEST_MODE=1) — no network calls:
#   PR_DEP_LOOKUP_FAIL=1   — force pr_dependents_list to return 1 (unknown)
#   PR_DEP_HEADREF_<pr>    — mock headRefName for PR <pr>
#   PR_DEP_OPEN_LIST_JSON  — mock `gh pr list` JSON array of
#                            {"number": N, "baseRefName": "..."} objects
#                            (default: "[]", i.e. no open PRs at all)
#   PR_DEP_LIST_LIMIT      — override the --limit / truncation-detection
#                            threshold (default 1000); the truncation check
#                            applies in test mode too, against whatever
#                            PR_DEP_OPEN_LIST_JSON's raw length is, so a
#                            low override here is how the truncation path
#                            gets exercised without a live 1000+-PR repo.

# shellcheck source=repo-resolve.sh
source "$(dirname "${BASH_SOURCE[0]}")/repo-resolve.sh"

PR_DEP_LIST=""
PR_DEP_REASON=""
PR_DEP_BRANCH=""

pr_dependents_list() {
  local pr="$1" repo="$2"
  PR_DEP_LIST=""
  PR_DEP_REASON=""
  PR_DEP_BRANCH=""

  local headref="" open_json="[]" rc=0
  local list_limit="${PR_DEP_LIST_LIMIT:-1000}"

  if [ "${PR_DEPENDENTS_TEST_MODE:-}" = "1" ]; then
    if [ "${PR_DEP_LOOKUP_FAIL:-}" = "1" ]; then
      PR_DEP_REASON="test mode: PR_DEP_LOOKUP_FAIL=1 (simulated lookup failure)"
      return 1
    fi
    local headref_var="PR_DEP_HEADREF_${pr}"
    headref="${!headref_var:-}"
    if [ -z "$headref" ]; then
      PR_DEP_REASON="test mode: \$${headref_var} not set — cannot resolve head ref"
      return 1
    fi
    open_json="${PR_DEP_OPEN_LIST_JSON:-[]}"
  else
    # Capture stderr separately (house convention — see D#2111's fix in
    # loop-phased-step5.sh) so a real failure carries the actual gh/API
    # error text in PR_DEP_REASON, not just an exit code.
    local err_file
    err_file=$(mktemp)
    headref=$(gh pr view "$pr" --repo "$repo" --json headRefName --jq .headRefName 2>"$err_file")
    rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$headref" ]; then
      local err_text
      err_text=$(tail -5 "$err_file" 2>/dev/null)
      rm -f "$err_file"
      PR_DEP_REASON="could not resolve headRefName for PR #$pr (gh pr view exit=$rc)${err_text:+: $err_text}"
      return 1
    fi
    rm -f "$err_file"

    err_file=$(mktemp)
    open_json=$(gh pr list --repo "$repo" --state open --json number,baseRefName --limit "$list_limit" 2>"$err_file")
    rc=$?
    if [ "$rc" -ne 0 ]; then
      local err_text
      err_text=$(tail -5 "$err_file" 2>/dev/null)
      rm -f "$err_file"
      PR_DEP_REASON="gh pr list --state open failed (exit=$rc)${err_text:+: $err_text}"
      return 1
    fi
    rm -f "$err_file"
  fi

  PR_DEP_BRANCH="$headref"

  # Single parse: first line is the RAW (pre-filter) entry count, used for
  # the truncation guard below; remaining lines are the filtered dependent
  # PR numbers. One python3 call rather than two so the truncation check
  # and the filter can never disagree about which JSON they read.
  local parsed
  parsed=$(printf '%s' "$open_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    if not isinstance(data, list):
        raise ValueError("open-PR list JSON is not an array")
except Exception:
    sys.exit(1)
headref = sys.argv[1]
mypr = sys.argv[2]
print("COUNT:" + str(len(data)))
for entry in data:
    if entry.get("baseRefName") == headref and str(entry.get("number")) != str(mypr):
        print(entry["number"])
' "$headref" "$pr" 2>/dev/null)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    PR_DEP_REASON="failed to parse open-PR list JSON"
    PR_DEP_LIST=""
    return 1
  fi

  local count_line raw_count deps
  count_line=$(printf '%s\n' "$parsed" | head -1)
  raw_count="${count_line#COUNT:}"
  deps=$(printf '%s\n' "$parsed" | tail -n +2)

  # Truncation guard (D#2020 fix-cycle 1): the raw count landing exactly on
  # the configured limit means there may be more open PRs beyond this page
  # — a real dependent could be sitting just outside the window, which
  # would otherwise look identical to a genuine "no dependents" answer.
  if [ "$raw_count" -eq "$list_limit" ] 2>/dev/null; then
    PR_DEP_REASON="open-PR list returned exactly $list_limit entries (the configured limit) — cannot rule out a dependent beyond the page, treating as unknown"
    PR_DEP_LIST=""
    return 1
  fi

  PR_DEP_LIST="$deps"
  if [ -n "$PR_DEP_LIST" ]; then
    local dep_count
    dep_count=$(printf '%s\n' "$PR_DEP_LIST" | grep -c .)
    PR_DEP_REASON="$dep_count open PR(s) based on branch '$headref' (of $raw_count open total)"
  else
    PR_DEP_REASON="no open PRs based on branch '$headref' (of $raw_count open total)"
  fi
  return 0
}

pr_dependents_report() {
  local pr="$1" repo="${2:-$(_resolve_repo)}"
  [ -z "${PR_DEP_LIST:-}" ] && return 0

  echo "[pr-dependents] WARNING: PR #$pr has open PR(s) based on its branch '${PR_DEP_BRANCH:-<unknown>}' — the branch is being KEPT (not deleted) so those PRs are not closed:"
  local dep
  while IFS= read -r dep; do
    [ -z "$dep" ] && continue
    echo "[pr-dependents]   PR #$dep — retarget when ready: gh pr edit $dep --base main --repo $repo"
  done <<< "$PR_DEP_LIST"
  echo "[pr-dependents] ACTION REQUIRED: the kept branch is now on a base that is no longer part of main's line. If the dependent(s) above are left un-retargeted, a future merge could land their content on this stale branch instead of main. Retarget them with the command(s) above before relying on this chain again."
  return 0
}
