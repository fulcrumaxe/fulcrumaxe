#!/usr/bin/env bash
# scripts/bootstrap-github-labels.sh — create merge-gate and workflow labels in a GitHub repo.
#
# Usage:
#   bash scripts/bootstrap-github-labels.sh [--repo OWNER/NAME]
#
# When --repo is omitted, reads from .autonomous-team/project.json (via repo-resolve.sh).
# Idempotent: uses `gh label create --force` so existing labels are updated in-place.
#
# Labels created:
#   Merge gate labels (required before auto-merge):
#     code-review-passed       green 0E8A16
#     code-review-needs-fix    red   B60205
#     acceptance-passed        green 0E8A16
#     acceptance-failed        red   B60205
#     security-review-passed   green 0E8A16
#     security-needs-fix       red   B60205
#
#   Conditional gate labels (only required for PRs that trigger them):
#     browser-test-passed      green 0E8A16  — dashboard-conditional
#     debater-confirmed        green 0E8A16  — gate off by default (gates.debater_pass=false)
#
#   Workflow labels:
#     SPEC_READY                       green 0E8A16  — discussion is ready for implementation
#     team-log                         blue  1D76DB  — marks the team-log tracking issue
#     verification-substance-absent    yellow FBCA04  — backend PR has no verification
#                                                        substance of any recognised shape
#                                                        (flags for visibility, D#2008 —
#                                                        never blocks merge on its own)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve repo
source "$SCRIPT_DIR/lib/repo-resolve.sh"

TARGET_REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) TARGET_REPO="${2:-}"; shift 2 || { echo "ERROR: --repo requires a value" >&2; exit 1; } ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$TARGET_REPO" ]]; then
  TARGET_REPO="$(_resolve_repo)"
fi

if [[ -z "$TARGET_REPO" ]]; then
  echo "ERROR: could not determine target repo. Pass --repo OWNER/NAME or set project.json." >&2
  exit 1
fi

echo "Bootstrapping GitHub labels for repo: $TARGET_REPO"

FAIL_COUNT=0
FAILED_LABELS=()
OK_COUNT=0

create_label() {
  local name="$1"
  local color="$2"
  local description="$3"
  local err rc

  # The assignment must be the condition of the `if` (not a bare statement)
  # so a non-zero `gh` exit doesn't trip `set -e` before we get to inspect it.
  if err=$(gh label create "$name" \
      --color "$color" \
      --description "$description" \
      --repo "$TARGET_REPO" \
      --force \
      2>&1 >/dev/null); then
    rc=0
  else
    rc=$?
  fi

  if [[ $rc -eq 0 ]]; then
    echo "  [ok] $name"
  else
    echo "  [warn] could not create label: $name — $err" >&2
    return "$rc"
  fi
}

echo ""
echo "==> Merge gate labels"
create_label "code-review-passed"    "0E8A16" "Code review passed — ready for next gate"    && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("code-review-passed"); }
create_label "code-review-needs-fix" "B60205" "Code review found issues — needs fixes"       && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("code-review-needs-fix"); }
create_label "acceptance-passed"     "0E8A16" "Acceptance tests passed"                      && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("acceptance-passed"); }
create_label "acceptance-failed"     "B60205" "Acceptance tests failed"                      && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("acceptance-failed"); }
create_label "security-review-passed" "0E8A16" "Security review passed"                      && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("security-review-passed"); }
create_label "security-needs-fix"    "B60205" "Security review found issues — needs fixes"    && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("security-needs-fix"); }

echo ""
echo "==> Conditional gate labels"
create_label "browser-test-passed"   "0E8A16" "Browser test passed — dashboard-conditional gate" && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("browser-test-passed"); }
create_label "debater-confirmed"     "0E8A16" "Debater pass confirmed — gate off by default"  && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("debater-confirmed"); }

echo ""
echo "==> Workflow labels"
create_label "SPEC_READY" "0E8A16" "Discussion spec is finalized — ready for implementation" && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("SPEC_READY"); }
create_label "team-log"   "1D76DB" "Marks the team-log tracking issue"                        && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("team-log"); }
create_label "verification-substance-absent" "FBCA04" "Backend PR has no verification substance of any recognised shape — flags, does not block (D#2008)" && OK_COUNT=$((OK_COUNT+1)) || { FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_LABELS+=("verification-substance-absent"); }

TOTAL_COUNT=$((OK_COUNT + FAIL_COUNT))

echo ""
if [[ $FAIL_COUNT -eq 0 ]]; then
  echo "Label bootstrap complete for $TARGET_REPO: $OK_COUNT/$TOTAL_COUNT labels created."
else
  {
    echo "Label bootstrap FAILED for $TARGET_REPO: $OK_COUNT/$TOTAL_COUNT labels created, $FAIL_COUNT failed."
    echo "Failed labels: ${FAILED_LABELS[*]}"
    echo "See [warn] lines above for the underlying gh error on each."
  } >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# GitHub Discussions (D#2217) — a freshly created repo ships with Discussions
# turned off. The GraphQL query start-the-day.sh (and generate-initial-plan.py)
# use to list open Discussions does NOT error on a Discussions-disabled repo —
# it returns the same clean "0 nodes" shape as a genuinely empty queue, so the
# adopter reads "no work yet" when the queue can never fill at all. Enable it
# here, with the same authenticated token that just created the labels above
# (label creation and this PATCH both need push/admin on the repo, and this
# script already has that or the labels above would not have succeeded).
# Loud on failure, with the exact manual command to run — same treatment as
# a failed label above, not silently swallowed.
# ---------------------------------------------------------------------------
echo ""
echo "==> Repository settings"
if ENABLE_ERR=$(gh api -X PATCH "repos/$TARGET_REPO" -F has_discussions=true 2>&1 >/dev/null); then
  echo "  [ok] GitHub Discussions enabled for $TARGET_REPO"
else
  {
    echo "  [warn] could not enable GitHub Discussions for $TARGET_REPO — $ENABLE_ERR"
    echo "  Without this, Discussions may still be off and the queue can never fill — enable it by hand:"
    echo "    gh api -X PATCH repos/$TARGET_REPO -F has_discussions=true"
  } >&2
  exit 1
fi
