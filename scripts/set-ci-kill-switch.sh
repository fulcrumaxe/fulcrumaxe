#!/usr/bin/env bash
# scripts/set-ci-kill-switch.sh — the only supported way to flip CI_DISABLED.
#
# Usage: bash scripts/set-ci-kill-switch.sh <true|false> [--reason <text>]
#
# CI_DISABLED governs whether anything about a PR is machine-verified before it
# merges. Until now the only record that it ever changed was the variable's own
# `updated_at` timestamp — one field, overwritten on every change, with no
# actor and no history. That makes it a control plane you cannot watch change,
# which is a worse trade than the loud `--force-no-ci` flag it replaces.
#
# So: every change through this script writes a `ci_kill_switch_changed` row
# to the audit trail with old, new, actor and timestamp. A no-op change writes
# nothing and exits 0 — a row that records no change is noise, and noise is
# what makes an audit trail stop being read.
#
# Honest residual: this script is the *supported* path, not the *only* path.
# `hooks/sandbox_rules.py` now denies `actions/variables` mutations from a
# sub-agent worktree, which raises the cost of going around it, but the hook
# adjudicates one tool call at a time and does not see subprocesses, and the
# operator context is not sandboxed the same way. A change made through the
# GitHub web UI leaves no row here at all. The audit row is the control; the
# deny pattern is the nudge. Reconciling the variable's `updated_at` against
# the newest row would catch the web-UI case and is not implemented here.
#
# Test mode: CI_KILL_SWITCH_MODE=echo skips both gh calls. The current value
# comes from CI_KILL_SWITCH_CURRENT and the write is printed, not performed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=scripts/lib/repo-resolve.sh
source "$SCRIPT_DIR/lib/repo-resolve.sh"
# shellcheck source=scripts/lib/ci-status-check.sh
source "$SCRIPT_DIR/lib/ci-status-check.sh"

# The CODE plane. Actions variables live with CI, and the gate that honours this
# kill switch reads it from the code plane: merge-and-hook.sh:233 and
# loop-phased-step5.sh:160 both call `check_ci_status "$PR" "$_CODE_REPO"`,
# which reaches ci-status-check.sh's _ci_kill_switch_state and reads
# repos/<code plane>/actions/variables/CI_DISABLED.
#
# Resolving the write side through _resolve_repo would put the switch and the
# gate that honours it on different repos after the cutover. That fails closed
# — the reader gets a 404, treats it as "not disabled", and CI keeps running —
# so the visible symptom is CI refusing to stand down rather than a silent
# bypass. A kill switch that cannot be read is still a kill switch that does not
# work, and this is the half that has to move.
_REPO="$(_require_code_repo "set-ci-kill-switch")" || exit 1

NEW_VALUE=""
REASON=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    true|false) NEW_VALUE="$1"; shift 1 ;;
    --reason)   REASON="${2:-}"; shift 2 || { echo "ERROR: --reason requires a value" >&2; exit 1; } ;;
    *)
      echo "[set-ci-kill-switch] unknown argument: $1" >&2
      echo "Usage: $0 <true|false> [--reason <text>]" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$NEW_VALUE" ]]; then
  echo "[set-ci-kill-switch] ERROR: a value is required — pass exactly 'true' or 'false'." >&2
  echo "Usage: $0 <true|false> [--reason <text>]" >&2
  exit 1
fi

# ── Read the current value ────────────────────────────────────────────────────
# Absent counts as "false": ci.yml's `vars.CI_DISABLED != 'true'` treats an
# unset variable as CI-enabled, and this script must agree with that reading or
# the row it writes describes a state transition that did not happen.
if [[ "${CI_KILL_SWITCH_MODE:-}" == "echo" ]]; then
  OLD_VALUE="${CI_KILL_SWITCH_CURRENT:-false}"
else
  _READ_RC=0
  OLD_RAW="$(gh api -i "repos/${_REPO}/actions/variables/CI_DISABLED" 2>&1)" || _READ_RC=$?
  if printf '%s' "$OLD_RAW" | head -n1 | grep -q ' 404 '; then
    OLD_VALUE="false"
  elif [[ "$_READ_RC" -ne 0 ]]; then
    echo "[set-ci-kill-switch] ERROR: could not read the current CI_DISABLED value (non-404 failure). Refusing to write — a change whose 'old' value is a guess is worse than no change." >&2
    exit 1
  else
    OLD_VALUE="$(printf '%s' "$OLD_RAW" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
parts = re.split(r"\r?\n\r?\n", raw, maxsplit=1)
try:
    print(json.loads(parts[1].strip()).get("value", ""))
except Exception:
    print("")
' 2>/dev/null || printf '')"
    if [[ -z "$OLD_VALUE" ]]; then
      echo "[set-ci-kill-switch] ERROR: read CI_DISABLED but could not parse its value. Refusing to write." >&2
      exit 1
    fi
  fi
fi

if [[ "$OLD_VALUE" == "$NEW_VALUE" ]]; then
  echo "[set-ci-kill-switch] CI_DISABLED is already '$NEW_VALUE' — nothing to change, no audit row written."
  exit 0
fi

# ── Write the variable ────────────────────────────────────────────────────────
if [[ "${CI_KILL_SWITCH_MODE:-}" == "echo" ]]; then
  echo "[set-ci-kill-switch] (echo mode) would set CI_DISABLED='$NEW_VALUE' on $_REPO"
else
  if ! gh variable set CI_DISABLED --repo "$_REPO" --body "$NEW_VALUE" >/dev/null 2>&1; then
    echo "[set-ci-kill-switch] ERROR: failed to set CI_DISABLED on $_REPO. No audit row written (nothing changed)." >&2
    exit 1
  fi
  echo "[set-ci-kill-switch] CI_DISABLED set to '$NEW_VALUE' on $_REPO."
fi

# ── Audit row ─────────────────────────────────────────────────────────────────
_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
_ACTOR="${CI_KILL_SWITCH_ACTOR:-${USER:-unknown}}"

_ROW="$(python3 -c '
import json, sys
print(json.dumps({
    "kind": "ci_kill_switch_changed",
    "variable": "CI_DISABLED",
    "old": sys.argv[1],
    "new": sys.argv[2],
    "actor": sys.argv[3],
    "repo": sys.argv[4],
    "reason": sys.argv[5],
    "ts": sys.argv[6],
}))
' "$OLD_VALUE" "$NEW_VALUE" "$_ACTOR" "$_REPO" "$REASON" "$_TS")"

_AUDIT_PATH="$(_ci_audit_path)"
mkdir -p "$(dirname "$_AUDIT_PATH")" 2>/dev/null || true
printf '%s\n' "$_ROW" >> "$_AUDIT_PATH"
echo "[set-ci-kill-switch] Audit row written: kind=ci_kill_switch_changed $OLD_VALUE -> $NEW_VALUE (actor=$_ACTOR)"
exit 0
