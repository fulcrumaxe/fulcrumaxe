#!/usr/bin/env bash
# scripts/refresh-gate-label.sh — force a fresh `labeled` timeline event for
# a gate label, whether or not it is already on the PR (D#2535).
#
# GitHub's add-label call is a no-op when the label is already present: no
# new `labeled` event is written, so the event's timestamp does not move.
# merge-and-hook.sh's pass-label freshness check (gate 0-b) reads that
# timestamp to confirm a passing review covers the current head. A reviewer
# who re-reviews a fix-round commit and re-applies an already-present pass
# label produces no event the gate can see, so the gate keeps refusing on a
# stale timestamp no amount of re-reviewing can move — and its own remedy
# text ("get the label re-applied") told the operator to do the one thing
# that cannot work.
#
# This script closes that gap without touching the gate itself: if the
# label is absent it does a plain add; if present it removes then re-adds,
# so a fresh `labeled` event is produced either way.
#
# Usage:
#   bash scripts/refresh-gate-label.sh <pr_number> <label>
#
# Exit codes:
#   0  label freshly applied (or added for the first time)
#   1  usage error, unresolved code plane, refused NACK label, or a real
#      gh/API failure
#
# NACK labels (scripts/lib/merge-gate-labels.sh's MERGE_GATE_NACK_LABELS)
# are refused by name. They are fail-closed and deliberately survive a
# force-push — a remove-then-add on one would open a real window, however
# short, where the PR carries no blocking label at all. A NACK label needs
# no freshness refresh in the first place: gate 0-b's freshness check only
# iterates MERGE_GATE_REQUIRED_PASS_LABELS, so a NACK label's timestamp is
# never read — only its presence is. Apply a NACK label directly
# (apply_label, or `gh pr edit --add-label`) instead of through this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# shellcheck source=scripts/lib/gh-label.sh
source "$SCRIPT_DIR/lib/gh-label.sh"
# shellcheck source=scripts/lib/merge-gate-labels.sh
source "$SCRIPT_DIR/lib/merge-gate-labels.sh"

PR="${1:-}"
LABEL="${2:-}"

if [[ -z "$PR" || -z "$LABEL" ]]; then
  echo "usage: refresh-gate-label.sh <pr_number> <label>" >&2
  exit 1
fi

for _rgl_nack in "${MERGE_GATE_NACK_LABELS[@]}"; do
  if [[ "$_rgl_nack" == "$LABEL" ]]; then
    echo "refresh-gate-label: refusing to refresh '$LABEL' — it is a NACK label (scripts/lib/merge-gate-labels.sh). NACK labels are fail-closed and must survive a force-push; a remove-then-add would open a window where the merge gate sees it absent, and gate 0-b never reads a NACK label's timestamp anyway. Apply it directly instead: apply_label <pr> $LABEL, or gh pr edit --add-label." >&2
    exit 1
  fi
done

# Resolved here (not just relied on inside gh-label.sh) so an unresolved
# plane is refused before the `gh pr view` read below runs at all — the same
# fail-loud discipline every code-plane call site in this repo follows,
# because `gh --repo ""` is not an error: it exits 0 after silently
# resolving from the checkout's git remote.
CODE_REPO="$(_require_code_repo "refresh-gate-label")" || exit 1

_RGL_LABELS=""
if ! _RGL_LABELS="$(gh pr view "$PR" --repo "$CODE_REPO" --json labels --jq '.labels[].name' 2>&1)"; then
  echo "refresh-gate-label: could not read labels for PR #$PR in $CODE_REPO: $_RGL_LABELS" >&2
  exit 1
fi

if grep -qx -- "$LABEL" <<<"$_RGL_LABELS"; then
  echo "refresh-gate-label: '$LABEL' already present on PR #$PR — removing then re-adding to produce a fresh 'labeled' event."
  if ! remove_label "$PR" "$LABEL"; then
    echo "refresh-gate-label: failed to remove '$LABEL' from PR #$PR — stopping before the add, rather than risk a partial state." >&2
    exit 1
  fi
else
  echo "refresh-gate-label: '$LABEL' not present on PR #$PR — adding."
fi

if ! apply_label "$PR" "$LABEL"; then
  echo "refresh-gate-label: failed to add '$LABEL' to PR #$PR." >&2
  exit 1
fi

echo "refresh-gate-label: '$LABEL' applied fresh on PR #$PR."
