#!/usr/bin/env bash
# scripts/hooks/pre-merge.d/cloud-decide.sh --pr N --head-sha SHA [--discussion D]
#
# Asks the cloud whether this merge may go ahead (decide engine_merge), when the
# engine's config.json has cloud_bridge.operate true. With operate false or absent
# it exits 0 and does nothing else.
#
# Exit: 0 go ahead (act, or announce plus a team-log line naming the receipt)
#       3 a person must answer in the cloud first
#       4 the engine is halted
#       5 the cloud could not be asked or answered badly: refused (fail closed)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_SCRIPTS="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$ENGINE_SCRIPTS/.." && pwd)"
# shellcheck source=../../lib/state-dir.sh
source "$ENGINE_SCRIPTS/lib/state-dir.sh" 2>/dev/null || true

PR="" SHA="" DISC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr) PR="${2:-}"; shift 2 ;;
    --head-sha) SHA="${2:-}"; shift 2 ;;
    --discussion) DISC="${2:-}"; shift 2 ;;
    *) shift 1 ;;
  esac
done

_unreachable() { echo "cloud unreachable; merge refused (fail-closed)" >&2; exit 5; }
# The arguments are validated inside cloud_decide.py, after the operate check, so that an
# engine with operate off is never affected by them.

ARGS=(decide --type engine_merge --key "merge:${PR}:${SHA}" --pr "$PR" --head-sha "$SHA" --repo-root "$REPO_ROOT")
[[ "$DISC" =~ ^[1-9][0-9]{0,15}$ ]] && ARGS+=(--discussion "$DISC")

OUT="$(timeout 15 python3 "$ENGINE_SCRIPTS/lib/cloud_decide.py" "${ARGS[@]}" 2>/dev/null)"
RC=$?
OUTCOME="" RECEIPT="" ASK="" ANSWERED=""
read -r OUTCOME RECEIPT ASK ANSWERED <<<"$OUT"
RECEIPT="${RECEIPT#receipt=}"; ASK="${ASK#ask=}"; ANSWERED="${ANSWERED#answered=}"

case "$RC" in
  0)
    if [[ "$OUTCOME" == "announce" ]]; then
      bash "$ENGINE_SCRIPTS/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] merge-gate: PR #${PR} merging under the cloud's announce (receipt ${RECEIPT})" >/dev/null 2>&1 \
        || echo "[cloud-decide] warning: team-log line for the announce failed (non-fatal)" >&2
    fi
    exit 0 ;;
  3)
    if [[ "$ANSWERED" == "decline" ]]; then
      echo "declined by a person in the cloud (ask ${ASK})" >&2
    else
      echo "waiting for a person in the cloud (ask ${ASK})" >&2
    fi
    exit 3 ;;
  4)
    echo "engine halted; merge refused" >&2
    exit 4 ;;
  *) _unreachable ;;
esac
