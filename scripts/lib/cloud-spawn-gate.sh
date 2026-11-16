#!/usr/bin/env bash
# scripts/lib/cloud-spawn-gate.sh: source it, then `cloud_spawn_gate <repo_root> <cloud_engine_gate.py args...>`.
# Runs the cloud decide gate for a spawn and normalises the result to 0 (go ahead), 3 (ask), 4 (halted)
# or 5 (unusable). A crash or timeout of the gate itself counts as 5 when config.json asks for the cloud
# gate (operate not plainly false), and as 0 when it does not, so operate off or absent changes nothing.
# stdout: "announce receipt=<uuid>" on an announce. stderr: the gate's one fixed reason line.
cloud_spawn_gate() {
  local root="$1" rc=0 here out
  shift
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  out="$(timeout 15 python3 "$here/cloud_engine_gate.py" "$@" --repo-root "$root")" || rc=$?
  [[ -n "$out" ]] && printf '%s\n' "$out"
  case "$rc" in
    0|3|4|5) return "$rc" ;;
  esac
  if grep -qE '"operate"[[:space:]]*:[[:space:]]*(true|"|[^f[:space:]])' "$root/.autonomous-team/config.json" 2>/dev/null; then
    echo "cloud unreachable; spawn refused (fail-closed)" >&2
    return 5
  fi
  return 0
}
