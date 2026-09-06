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
