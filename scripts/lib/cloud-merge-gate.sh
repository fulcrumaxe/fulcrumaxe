#!/usr/bin/env bash
# scripts/lib/cloud-merge-gate.sh: source it, then `cloud_merge_gate <pr> <head_sha> [discussion]`.
# The one place an engine merge asks the cloud (decide engine_merge, key merge:<pr>:<head sha>) before
# it squash-merges, so merge-and-hook.sh, loop-phased-step5.sh and team-lead-iteration.sh cannot drift.
# Returns 0 go ahead, 3 ask, 4 halted, 5 cloud unusable (fail closed). Never exits the caller's shell.
# With cloud_bridge.operate false or absent in .autonomous-team/config.json it returns 0 and does
# nothing else. On a refusal it emits merge_lane refused; the hook's own stderr line says why, and the
# caller says which merge it refused.
# The repo root is the checkout this file sits in, or CLOUD_MERGE_GATE_ROOT (callers with a REPO_ROOT
# override, and tests).

# 0 when config.json asks for the cloud gate (operate present and not plainly false).
cloud_merge_gate_operate_on() {
  local root="${CLOUD_MERGE_GATE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  grep -qE '"operate"[[:space:]]*:[[:space:]]*(true|"|[^f[:space:]])' "$root/.autonomous-team/config.json" 2>/dev/null
}

cloud_merge_gate() {
  local pr="$1" sha="${2:-}" disc="${3:-}" rc=0 here root hook
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  root="${CLOUD_MERGE_GATE_ROOT:-$(cd "$here/../.." && pwd)}"
  hook="$here/../hooks/pre-merge.d/cloud-decide.sh"
  if [[ -f "$hook" ]]; then
    bash "$hook" --pr "$pr" --head-sha "$sha" ${disc:+--discussion "$disc"} || rc=$?
    [[ "$rc" -eq 0 || "$rc" -eq 3 || "$rc" -eq 4 ]] || rc=5
  elif cloud_merge_gate_operate_on; then
    # The hook is missing but the config asks for the cloud gate: refuse rather than skip it.
    echo "cloud unreachable; merge refused (fail-closed)" >&2
    rc=5
  fi
  if [[ "$rc" -ne 0 ]]; then
    timeout 10 bash "$root/scripts/engine-emit.sh" merge_lane --pr "$pr" --state refused \
      ${disc:+--discussion "$disc"} >/dev/null 2>&1 || true
  fi
  return "$rc"
}
