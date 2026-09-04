#!/usr/bin/env bash
# scripts/lib/planned-prs-label.sh — the `needs-planned-prs` backstop label
# for scripts/post-merge-hook.sh (D#2272).
#
# Coverage note, stated rather than glossed over: scripts/lib/planned-prs-gate.sh
# only fires on the executor spawn path through scripts/spawn-agent.sh. A Team
# Lead spawning an executor directly via Agent() does not pass through that
# script. This label is the backstop for that path — it reports the same
# missing-field condition after the fact, at merge time, via the guard that
# already runs there (scripts/lib/discussion-close-guard.sh). It never closes
# a Discussion and never blocks anything; it is reporting only.
#
# planned_prs_label_action() is a pure function (no network, no side effects)
# so it can be fixture-tested with plain decision strings instead of a real
# Discussion or a mocked `gh`. The actual label mutation is a thin GraphQL
# wrapper below it, used only by scripts/post-merge-hook.sh.
#
# Usage:
#   source scripts/lib/planned-prs-label.sh
#   ACTION=$(planned_prs_label_action "$CLOSE_DECISION")
#   case "$ACTION" in
#     apply) planned_prs_label_apply "$owner" "$name" "$disc_node_id" ;;
#     clear) planned_prs_label_clear "$owner" "$name" "$disc_node_id" ;;
#     noop)  : ;;
#   esac

PLANNED_PRS_LABEL_NAME="needs-planned-prs"

# planned_prs_label_action <close_decision>
#   close_decision - close | hold | unknown, exactly as returned by
#                    discussion_close_decision (discussion-close-guard.sh).
#
# Prints exactly one of: apply | clear | noop
#   unknown -> apply  (no planned_prs field could be resolved anywhere in the
#                       Spec — precisely the condition the spawn gate blocks
#                       on; flag it after the fact here.)
#   close   -> clear  (the Discussion just closed — a stale backstop label
#                       must not linger on a resolved Discussion.)
#   hold    -> noop   (planned_prs WAS declared — either N>1 awaiting more
#                       merges, or 0's deliberate hold-open. Nothing is
#                       missing, so nothing to flag.)
planned_prs_label_action() {
  local close_decision="${1:-}"
  case "$close_decision" in
    unknown) echo "apply" ;;
    close)   echo "clear" ;;
    *)       echo "noop" ;;
  esac
}

# _planned_prs_label_id <owner> <name>
#   Echoes the needs-planned-prs label's GraphQL node id, or empty if it
#   doesn't exist yet in <owner>/<name>.
_planned_prs_label_id() {
  local owner="$1" name="$2"
  gh api graphql -f query="query { repository(owner:\"${owner}\", name:\"${name}\") { label(name:\"${PLANNED_PRS_LABEL_NAME}\") { id } } }" \
    2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    lbl = d.get('data', {}).get('repository', {}).get('label')
    print(lbl['id'] if lbl else '')
except Exception:
    print('')
" 2>/dev/null
}

# planned_prs_label_apply <owner> <name> <discussion_node_id>
#   Creates the label idempotently (gh label create --force, non-fatal) then
#   adds it to the Discussion via addLabelsToLabelable. Non-fatal: reports to
#   stderr and returns the real exit status, never silently swallowed.
planned_prs_label_apply() {
  local owner="$1" name="$2" node_id="$3"
  gh label create "$PLANNED_PRS_LABEL_NAME" \
    --color "FBCA04" \
    --description "Spec has no anchored planned_prs declaration — Discussion may never auto-close (D#2272)" \
    --repo "${owner}/${name}" \
    --force \
    >/dev/null 2>&1 || true

  local lbl_id
  lbl_id=$(_planned_prs_label_id "$owner" "$name")
  if [[ -z "$lbl_id" ]]; then
    echo "planned_prs_label_apply: could not resolve '${PLANNED_PRS_LABEL_NAME}' label id in ${owner}/${name} (non-fatal)" >&2
    return 1
  fi

  gh api graphql -f query="mutation { addLabelsToLabelable(input:{labelableId:\"${node_id}\", labelIds:[\"${lbl_id}\"]}) { clientMutationId } }" \
    >/dev/null 2>&1
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "planned_prs_label_apply: addLabelsToLabelable failed for ${node_id} (non-fatal)" >&2
  fi
  return $rc
}

# planned_prs_label_clear <owner> <name> <discussion_node_id>
#   Removes the label if present. Absence of the label (never created, or
#   already cleared) is success, not an error — idempotent removal.
planned_prs_label_clear() {
  local owner="$1" name="$2" node_id="$3"
  local lbl_id
  lbl_id=$(_planned_prs_label_id "$owner" "$name")
  if [[ -z "$lbl_id" ]]; then
    return 0
  fi

  gh api graphql -f query="mutation { removeLabelsFromLabelable(input:{labelableId:\"${node_id}\", labelIds:[\"${lbl_id}\"]}) { clientMutationId } }" \
    >/dev/null 2>&1
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "planned_prs_label_clear: removeLabelsFromLabelable failed for ${node_id} (non-fatal)" >&2
  fi
  return $rc
}
