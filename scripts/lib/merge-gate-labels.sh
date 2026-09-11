#!/usr/bin/env bash
# scripts/lib/merge-gate-labels.sh — which labels gate a merge, defined once.
#
# There are two ways a PR reaches main: the loop auto-merge path
# (scripts/loop-phased-step5.sh, merging phase) and the manual path
# (scripts/merge-and-hook.sh). Both consume the arrays below. Neither restates
# them, and a label added here changes both behaviours with no second edit.
#
# Why one file rather than two lists that agree
# ---------------------------------------------
# They did not agree. The manual path read two label literals and none of the
# nine below: a PR carrying do-not-merge — a label with no purpose other than
# stopping a merge — was refused by the loop and merged by hand, silently, and
# code-review-passed was required by the loop, asserted as required in
# CLAUDE.md, and never read by the wrapper at all. Two implementations of
# "which labels gate a merge" is what produced that, and a fix that added the
# missing literals to the wrapper would have produced it again the next time a
# tenth label was added to one side.
#
# Why scripts/lib/
# ----------------
# Both readers are bash, and the wrapper already sources five libraries from
# here, so this costs one more `source` line and no new mechanism. A JSON or
# text data file would have needed a parser in each reader; a Python module
# would have needed a subprocess per read on a path that is already careful
# about round trips. The TypeScript port in ts-backend/src/loop/ carries its
# own mirrored copy of this vocabulary and is deliberately NOT changed here —
# it is a third implementation with its own parity test, and pointing it at a
# bash array is a separate change.
#
# What is NOT here, and why
# -------------------------
# security-review-passed and browser-test-passed are conditional gates: each is
# required only when its own predicate fires (provenance:external or a live
# security trigger; a PR touching dashboard/). The condition, not the label, is
# where those two paths still differ — see
# scripts/ci/merge-gate-parity-ledger.json. Putting them in a flat "always
# required" array would state something false. They stay as literals at their
# own call sites until the conditions themselves are shared.
#
# acceptance-passed is not here either: no gate on either path reads it. The
# real veto is acceptance-failed, below.

# NACK labels — any one present blocks the merge on both paths, regardless of
# which pass labels are also on the PR. Fail-closed by design: these survive a
# force-push (they are deliberately absent from the loop's stale-pass-label
# invalidation list) and there is no override flag for them on either path.
#
# Canonical vocabulary:
#   security-needs-fix        — security reviewer found issues (canonical name)
#   security-issue            — deprecated alias; kept so legacy labels block too
#   security-review-needs-fix — synonym used by some reviewer versions; all three
#                               are treated as equivalent NACK signals
#   code-review-needs-fix     — code reviewer found issues
#   needs-re-review           — code reviewer requested changes after a fix round;
#                               executor must push fixes and remove this label, then
#                               code-reviewer re-reviews and applies code-review-passed
#   acceptance-failed         — acceptance tests failed
#   do-not-merge              — manual hold
#   wip                       — work in progress, not ready
MERGE_GATE_NACK_LABELS=(
  "security-needs-fix"
  "security-issue"
  "security-review-needs-fix"
  "code-review-needs-fix"
  "needs-re-review"
  "acceptance-failed"
  "do-not-merge"
  "wip"
)

# Pass labels required on every PR, unconditionally, on both paths. Absence
# blocks the merge; no predicate guards the requirement.
MERGE_GATE_REQUIRED_PASS_LABELS=(
  "code-review-passed"
)

# ── Label lane map (D#2529) ─────────────────────────────────────────────────
#
# D#2529: security-review-passed and code-review-passed were applied 18
# seconds apart on PR #147 by the shared bot account every role authenticates
# as. The security-reviewer on that PR reported its own two write attempts
# were refused before execution, so something else applied its label — and
# nothing in the tree could say what, because the gate only ever checked
# whether a label string was present, never who was allowed to put it there.
#
# This is a lane map: one designated role per gate PASS label. It covers
# every pass label a reviewer role's own card instructs it to apply for
# itself — code-review-passed and acceptance-passed unconditionally,
# security-review-passed and browser-test-passed conditionally (see the "What
# is NOT here" note above for why those two are absent from
# MERGE_GATE_REQUIRED_PASS_LABELS; that absence is about when the label is
# *required*, not about who may *apply* it, so both belong here regardless).
#
# team-lead is always permitted to apply any label in this map. D#2529 itself
# names the reason: browser-tester.md's own MCP-unreachable skip path has the
# Team Lead apply browser-test-passed by hand, and elsewhere today the Team
# Lead applied two review-passed labels after their reviewers' own writes
# were refused by the permission classifier. That is a documented, legitimate
# escape valve — not the anomaly this map exists to catch — so it is
# exempted by name rather than by accident.
#
# A label with no entry here is a hard error at check time (see
# merge_gate_check_label_lane), never a default-allow: an unmapped label is a
# gap in this file, not evidence that anyone may apply it.
declare -A MERGE_GATE_LABEL_LANE=(
  ["code-review-passed"]="code-reviewer"
  ["security-review-passed"]="security-reviewer"
  ["browser-test-passed"]="browser-tester"
  ["acceptance-passed"]="acceptance-tester"
)

# merge_gate_check_label_lane <label> <role>
#
# Refuses an apply attempt from outside a label's lane, loudly — the message
# goes to the caller's own stderr, not only to a log, and names both the
# label and the role that attempted it (D#2529 acceptance item 5).
#
# Deliberately reads no GitHub actor, login, or identity of any kind — every
# role authenticates as the same bot account, and a check that depended on
# telling them apart that way could never work (D#2529 acceptance item 8).
# <role> is supplied by the caller instead. Investigated and found no
# non-spoofable role signal reachable from a worktree-isolated agent's own
# shell today — WORKTREE_ID is not set by the live spawn pipeline (only by
# tests), and the worktree registry that maps a worktree to the role it was
# spawned for lives in the main checkout, not inside the worktree a reviewer
# actually runs in. Given that, this is a guardrail against a role drifting
# onto another role's label by mistake, the same class of protection
# hooks/sandbox.py already documents itself as providing (CLAUDE.md: "a
# guardrail, not a security boundary") — not a defense against a role that
# deliberately misreports itself, which no purely in-repo check can be
# without a change to how roles authenticate. See the PR body for the D#2529
# Part 1 findings this rests on.
#
# Returns:
#   0  role is the label's designated role, or "team-lead"
#   1  role is set but not permitted for this label (refused)
#   2  usage error: missing label/role, or the label has no lane entry
merge_gate_check_label_lane() {
  local label="${1:-}" role="${2:-}"
  if [[ -z "$label" || -z "$role" ]]; then
    echo "merge_gate_check_label_lane: usage: merge_gate_check_label_lane <label> <role>" >&2
    return 2
  fi

  local permitted="${MERGE_GATE_LABEL_LANE[$label]:-}"
  if [[ -z "$permitted" ]]; then
    echo "merge_gate_check_label_lane: '$label' has no lane entry in MERGE_GATE_LABEL_LANE (scripts/lib/merge-gate-labels.sh) — refusing rather than defaulting to allow. Add an entry for it there." >&2
    return 2
  fi

  if [[ "$role" == "team-lead" || "$role" == "$permitted" ]]; then
    return 0
  fi

  echo "merge_gate_check_label_lane: refused — role '$role' may not apply label '$label'. '$label' is $permitted's lane (or team-lead's)." >&2
  return 1
}
