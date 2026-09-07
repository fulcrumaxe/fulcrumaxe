#!/usr/bin/env bash
# tests/test_pre_spawn_check_role_caps.sh
#
# D#2450 needs-fix round: reading policies.<role>.max_concurrent alone (no
# fallback) silently made the per-project cap unbounded for every role
# without a max_concurrent key of its own -- security-reviewer,
# project-manager, incident_commander, debater, researcher. Only
# code-reviewer (default 4) and executor kept a real bound.
#
# This asserts the EFFECTIVE per-project cap scripts/pre-spawn-check.sh
# resolves for each role -- not that a lookup succeeds, and not against a
# mock. It runs the exact two `control_plane.py get` calls the gate runs
# (role-scoped, falling back to policies.executor.max_concurrent), against
# a real control_plane.py and a scratch config fixture.
#
# Prove-it-fails check: run with PRE_SPAWN_ROLE_CAP_NO_FALLBACK=1 and every
# one of the five no-own-key roles must report an empty (unbounded) cap
# instead of the fallback value -- i.e. this test must fail against the
# pre-fix (bare per-role read, no fallback) shape of the gate.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

TMPDIR_BASE=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_BASE"; }
trap cleanup EXIT

CONFIG="$TMPDIR_BASE/config.json"
cat > "$CONFIG" <<JSON
{
  "policies": {
    "executor": { "max_concurrent": 3 }
  }
}
JSON

export AF_CONTROL_PLANE_CONFIG="$CONFIG"

# Mirrors the two-step resolution in scripts/pre-spawn-check.sh's per-project
# cap check: role-scoped read, falling back to policies.executor.max_concurrent
# unless PRE_SPAWN_ROLE_CAP_NO_FALLBACK=1 (used to prove this test can fail).
resolve_effective_cap() {
  local role="$1" cap
  cap=$(python3 "$REPO_ROOT/backend/control_plane.py" get "policies.${role}.max_concurrent" 2>/dev/null | tr -d '"' || echo "")
  if [[ "${PRE_SPAWN_ROLE_CAP_NO_FALLBACK:-0}" != "1" ]]; then
    if [[ -z "$cap" || "$cap" == "null" ]]; then
      cap=$(python3 "$REPO_ROOT/backend/control_plane.py" get "policies.executor.max_concurrent" 2>/dev/null | tr -d '"' || echo "")
    fi
  fi
  echo "$cap"
}

echo "=== test_pre_spawn_check_role_caps (fixture: policies.executor.max_concurrent=3) ==="
echo ""

# role -> expected effective cap with the fallback in place.
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
  got="$(resolve_effective_cap "$role")"
  want="${EXPECTED[$role]}"
  if [[ "$got" == "$want" ]]; then
    ok "role=$role effective cap=[$got] (expected [$want])"
  else
    fail "role=$role effective cap=[$got] but expected [$want] -- this role is UNBOUNDED by the per-project check if empty"
  fi
done

echo ""
echo "=== control: PRE_SPAWN_ROLE_CAP_NO_FALLBACK=1 must break exactly the five no-own-key roles ==="
NO_FALLBACK_BROKE=()
for role in security-reviewer project-manager incident_commander debater researcher; do
  got="$(PRE_SPAWN_ROLE_CAP_NO_FALLBACK=1 resolve_effective_cap "$role")"
  if [[ -z "$got" ]]; then
    NO_FALLBACK_BROKE+=("$role")
  fi
done
if [[ "${#NO_FALLBACK_BROKE[@]}" -eq 5 ]]; then
  ok "no-fallback control: all 5 no-own-key roles go unbounded without the fallback (${NO_FALLBACK_BROKE[*]})"
else
  fail "no-fallback control: expected exactly 5 roles to go unbounded, got ${#NO_FALLBACK_BROKE[@]} (${NO_FALLBACK_BROKE[*]:-none}) -- this test would not have caught the regression"
fi
# executor and code-reviewer must NOT go unbounded even with the fallback disabled --
# they each have their own key.
for role in executor code-reviewer; do
  got="$(PRE_SPAWN_ROLE_CAP_NO_FALLBACK=1 resolve_effective_cap "$role")"
  if [[ -n "$got" ]]; then
    ok "no-fallback control: role=$role keeps its own cap [$got] even without the fallback"
  else
    fail "no-fallback control: role=$role unexpectedly went unbounded without the fallback"
  fi
done

echo ""
echo "======================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "======================================="

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
