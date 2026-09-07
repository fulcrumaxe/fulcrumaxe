#!/usr/bin/env bash
# tests/test_pre_spawn_check_role_caps.sh
#
# D#2450 PR-b: this test used to assert the effective per-project cap via its
# own resolve_effective_cap() function, which reimplemented the exact
# two-step (role-key, then executor-fallback) resolution logic
# scripts/pre-spawn-check.sh itself performs -- the defect class this whole
# Discussion is about, one level in (flagged as non-blocking in PR-a's
# review, closed here). This version invokes scripts/pre-spawn-check.sh
# directly, end to end, against a real scratch fleet.db and a real scratch
# control-plane config, and reads ALLOWED/BLOCKED off its actual exit code
# and stderr. There is no second implementation of the resolution rule left
# in this file.
#
# Strategy per role: seed the resolved project's fleet.db with
# (expected_cap - 1) filler agents (any role -- the per-project check counts
# the whole project), then run the real script twice:
#   1. at N-1 active -> must be ALLOWED (and that spawn itself registers,
#      bringing the count to N)
#   2. at N active   -> must be BLOCKED with blocked_reason=per_project_cap_exceeded
#
# Prove-it-fails control: PRE_SPAWN_ROLE_CAP_NO_FALLBACK=1 is a test-only
# knob in the real script (see the comment at its per-project-cap check) that
# disables the executor-fallback for the five roles with no max_concurrent of
# their own. At exactly their fallback-cap count, those five must flip from
# BLOCKED to ALLOWED under that flag -- executor and code-reviewer (their own
# key) must not be affected, since they never reach the fallback branch.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/pre-spawn-check.sh"

PASS=0
FAIL=0
ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

# Resolve the SAME project name the real script resolves for itself (its own
# CLI, not a reimplementation), so seeding lands under the count it actually
# reads.
REAL_PROJECT="$(python3 -m backend.fleet.project_name "$REPO_ROOT" 2>/dev/null || true)"
if [[ -z "$REAL_PROJECT" ]]; then
  echo "SKIP: could not resolve fleet project name for this checkout -- nothing to seed against"
  exit 0
fi

new_scratch() {
  local d
  d=$(mktemp -d)
  mkdir -p "$d/fleet" "$d/state" "$d/home"
  cat > "$d/config.json" <<JSON
{
  "gates": { "budget_check": false, "subscription_throttle": false },
  "policies": { "executor": { "max_concurrent": 3 } }
}
JSON
  echo "$d"
}

seed_fillers() {
  local fleet_dir="$1" count="$2" i
  for ((i = 0; i < count; i++)); do
    AUTONOMOUS_FLEET_STATE_DIR="$fleet_dir" \
      python3 -m backend.fleet.concurrency register "$REAL_PROJECT" "filler-$$-$RANDOM-$i" "filler" "$$" >/dev/null 2>&1
  done
}

# run_gate <fleet_dir> <scratch_dir> <role> <event_id> <no_fallback 0|1>
# Prints the real script's exit code on stdout; stderr/stdout of the run
# itself land in $scratch_dir/last-{stderr,stdout}.txt.
run_gate() {
  local fleet_dir="$1" scratch="$2" role="$3" event_id="$4" no_fallback="$5"
  PRE_SPAWN_ROLE_CAP_NO_FALLBACK="$no_fallback" \
    AF_CONTROL_PLANE_CONFIG="$scratch/config.json" \
    AUTONOMOUS_FLEET_STATE_DIR="$fleet_dir" \
    AUTONOMOUS_TEAM_STATE_DIR="$scratch/state" \
    HOME="$scratch/home" \
    bash "$SCRIPT" --role "$role" --event-id "$event_id" \
    >"$scratch/last-stdout.txt" 2>"$scratch/last-stderr.txt"
  echo $?
}

echo "=== boundary check per role (fixture: policies.executor.max_concurrent=3, real project=$REAL_PROJECT) ==="
declare -A EXPECTED=(
  [executor]="3"
  [code-reviewer]="4"
  [security-reviewer]="3"
  [project-manager]="3"
  [incident_commander]="3"
  [debater]="3"
  [researcher]="3"
)

for role in executor code-reviewer security-reviewer project-manager incident_commander debater researcher; do
  cap="${EXPECTED[$role]}"
  s=$(new_scratch)
  seed_fillers "$s/fleet" "$((cap - 1))"

  rc=$(run_gate "$s/fleet" "$s" "$role" "boundary-$role-1" 0)
  if [[ "$rc" == "0" ]]; then
    ok "role=$role at $((cap - 1))/$cap active -> ALLOWED (real script)"
  else
    fail "role=$role at $((cap - 1))/$cap active -> expected ALLOWED, got exit=$rc: $(cat "$s/last-stderr.txt")"
  fi

  rc2=$(run_gate "$s/fleet" "$s" "$role" "boundary-$role-2" 0)
  if [[ "$rc2" == "1" ]] && grep -q "per_project_cap_exceeded" "$s/last-stderr.txt"; then
    ok "role=$role at $cap/$cap active -> BLOCKED per_project_cap_exceeded (real script)"
  else
    fail "role=$role at $cap/$cap active -> expected BLOCKED per_project_cap_exceeded, got exit=$rc2: $(cat "$s/last-stderr.txt")"
  fi
  rm -rf "$s"
done

echo ""
echo "=== control: PRE_SPAWN_ROLE_CAP_NO_FALLBACK=1 must unblock exactly the five no-own-key roles ==="
NO_FALLBACK_UNBLOCKED=()
for role in security-reviewer project-manager incident_commander debater researcher; do
  cap="${EXPECTED[$role]}"
  s=$(new_scratch)
  seed_fillers "$s/fleet" "$cap"   # exactly at the fallback cap -- normally blocks
  rc=$(run_gate "$s/fleet" "$s" "$role" "nofallback-$role" 1)
  if [[ "$rc" == "0" ]]; then
    NO_FALLBACK_UNBLOCKED+=("$role")
  fi
  rm -rf "$s"
done
if [[ "${#NO_FALLBACK_UNBLOCKED[@]}" -eq 5 ]]; then
  ok "no-fallback control: all 5 no-own-key roles go unbounded without the fallback (${NO_FALLBACK_UNBLOCKED[*]})"
else
  fail "no-fallback control: expected exactly 5 roles to go unbounded, got ${#NO_FALLBACK_UNBLOCKED[@]} (${NO_FALLBACK_UNBLOCKED[*]:-none}) -- this test would not have caught the regression"
fi

for role in executor code-reviewer; do
  cap="${EXPECTED[$role]}"
  s=$(new_scratch)
  seed_fillers "$s/fleet" "$cap"
  rc=$(run_gate "$s/fleet" "$s" "$role" "nofallback-own-$role" 1)
  if [[ "$rc" == "1" ]]; then
    ok "no-fallback control: role=$role keeps its own cap [$cap] even without the fallback"
  else
    fail "no-fallback control: role=$role unexpectedly went unbounded without the fallback"
  fi
  rm -rf "$s"
done

echo ""
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
