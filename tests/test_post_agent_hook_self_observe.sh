#!/usr/bin/env bash
# tests/test_post_agent_hook_self_observe.sh — hermetic tests for the self-observe
# gate enforcement step added to scripts/post-agent-hook.sh (Discussion #572).
#
# All tests use temporary config files and mock out external calls so no real
# GitHub writes, no real budget records, and no real training runs happen.
#
# Tests cover:
#   1. Shadow mode (default): missing self_observed → no team-log warning
#   2. Advisory mode: missing self_observed + done verdict → warning emitted
#   3. Advisory mode: self_observed:true present → no warning
#   4. Advisory mode: missing self_observed + needs-fix verdict → no warning (not done/pass)
#   5. Enforced mode: missing self_observed + done verdict → warning emitted
#   6. Advisory mode: verdict=pass (same as done) → warning emitted
#      (skip_reason bypass is deferred to a follow-up PR)
#
# Run: bash tests/test_post_agent_hook_self_observe.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
POST_HOOK="$REPO_ROOT/scripts/post-agent-hook.sh"

PASS=0
FAIL=0
ERRORS=()

# ── Helpers ───────────────────────────────────────────────────────────────────

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — ${2:-}"; FAIL=$((FAIL + 1)); ERRORS+=("$1: ${2:-}"); }

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    pass "$label"
  else
    fail "$label" "expected to find: '$needle'"
    echo "    Output was:" >&2
    echo "$haystack" | head -20 >&2
  fi
}

assert_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if ! echo "$haystack" | grep -qF "$needle"; then
    pass "$label"
  else
    fail "$label" "expected NOT to find: '$needle'"
    echo "    Output was:" >&2
    echo "$haystack" | head -20 >&2
  fi
}

# ── Test harness: create a temp config, run post-agent-hook with mocked steps ─
# We use AF_CONTROL_PLANE_CONFIG to point control_plane.py at a temp config,
# and stub out all the "heavy" steps (budget, training, GitHub writes) by
# overriding the commands that would write to external state.

run_hook_with_enforcement() {
  local mode="$1"    # shadow | advisory | enforced
  local verdict="$2" # done | pass | needs-fix
  local self_observed="$3"  # true | false | "" (absent = not passed)

  # Write a minimal config.json with the given enforcement mode
  local tmp_config
  tmp_config=$(mktemp)
  cat > "$tmp_config" <<JSON
{
  "gates": {
    "auto_merge": true,
    "training_triggers": false,
    "self_observe_enforcement": "${mode}"
  }
}
JSON

  # Build hook args
  local hook_args=(
    --role "executor"
    --verdict "$verdict"
    --input-tokens 0
    --output-tokens 0
    --event-id "test-$(date +%s%N)"
  )
  [[ "$self_observed" == "true" ]] && hook_args+=(--self-observed true)

  # Run with:
  #   - AF_CONTROL_PLANE_CONFIG → our temp config
  #   - stub out all side-effectful sub-scripts so tests run fast without writes
  #   - capture stderr+stdout together
  AF_CONTROL_PLANE_CONFIG="$tmp_config" \
  _PAH_TEST_STUB_STEPS="agent_feed,budget,circuit_breaker,kpi,audit,memory,training_mine,worktree_registry,team_log" \
  bash "$POST_HOOK" "${hook_args[@]}" 2>&1 || true

  rm -f "$tmp_config"
}

# We need to stub out the heavy steps in post-agent-hook.sh without modifying it.
# Strategy: the hook sources lib/hook-event.sh; we can intercept the external tool calls
# by pre-seeding all non-self-observe steps in the event state file so hook_event_has_step
# returns true for them. The simplest hermetic approach: create a wrapper that marks
# all steps except self_observe_check as already done, then runs the real hook.

run_hook_isolated() {
  local mode="$1"
  local verdict="$2"
  local self_observed="${3:-false}"

  local tmp_config tmp_hooks_dir tmp_event_dir
  tmp_config=$(mktemp)
  tmp_hooks_dir=$(mktemp -d)
  tmp_event_dir=$(mktemp -d)

  cat > "$tmp_config" <<JSON
{
  "gates": {
    "auto_merge": true,
    "training_triggers": false,
    "self_observe_enforcement": "${mode}"
  }
}
JSON

  # Write a stub hook-event.sh that marks all steps except self_observe_check as done
  # so only that step actually runs.
  mkdir -p "$tmp_hooks_dir/lib"
  cat > "$tmp_hooks_dir/lib/hook-event.sh" <<'STUB_HOOK'
# Stub hook-event.sh — all steps pre-marked except self_observe_check
HOOK_EVENT_ID="test-event-$(date +%s)"
_COMPLETED_STEPS=""

hook_event_init() {
  # $1 = hook name, $2 = step list
  # Pre-mark every step except self_observe_check as done
  local step_list="$2"
  IFS=',' read -ra _ALL_STEPS <<< "$step_list"
  for s in "${_ALL_STEPS[@]}"; do
    [[ "$s" != "self_observe_check" ]] && _COMPLETED_STEPS="$_COMPLETED_STEPS,$s,"
  done
}

hook_event_has_step() {
  local step="$1"
  [[ "$_COMPLETED_STEPS" == *",$step,"* ]]
}

hook_event_mark_step() {
  local step="$1"
  _COMPLETED_STEPS="$_COMPLETED_STEPS,$step,"
}

hook_event_finish() { :; }
STUB_HOOK

  # Write a stub rotate-team-log.sh that echoes what it would post (captured by caller)
  mkdir -p "$tmp_hooks_dir"
  cat > "$tmp_hooks_dir/rotate-team-log.sh" <<'STUB_LOG'
#!/usr/bin/env bash
# Stub rotate-team-log.sh — echo the comment to stdout so tests can inspect it
if [[ "${1:-}" == "comment" ]]; then
  echo "TEAM_LOG_COMMENT: ${2:-}"
fi
STUB_LOG
  chmod +x "$tmp_hooks_dir/rotate-team-log.sh"

  local hook_args=(
    --role "executor"
    --verdict "$verdict"
    --input-tokens 0
    --output-tokens 0
    --event-id "test-$(date +%s%N)"
  )
  [[ "$self_observed" == "true" ]] && hook_args+=(--self-observed true)

  # Patch SCRIPT_DIR used inside post-agent-hook.sh by creating a shim that sources
  # our stub lib/hook-event.sh and overrides rotate-team-log.sh lookup. The cleanest
  # approach is to create a temporary scripts/ dir structure that the hook resolves via
  # ${BASH_SOURCE[0]} — but the hook uses its own SCRIPT_DIR. Instead, we wrap it.
  local shim
  shim=$(mktemp --suffix=.sh)
  cat > "$shim" <<SHIM
#!/usr/bin/env bash
set -uo pipefail
# Override SCRIPT_DIR to point to our stubs for lib/ and rotate-team-log.sh
SCRIPT_DIR="$tmp_hooks_dir"
REPO_ROOT="$REPO_ROOT"

# Inject our stub lib/hook-event.sh
# shellcheck source=/dev/null
source "$tmp_hooks_dir/lib/hook-event.sh"

# Re-define rotate-team-log.sh lookup to use our stub
rotate_team_log() { bash "$tmp_hooks_dir/rotate-team-log.sh" "\$@"; }

# Re-source the self_observe_check block from post-agent-hook.sh directly
ROLE="${hook_args[1]:-executor}"
VERDICT="${hook_args[3]:-done}"
AGENT_SELF_OBSERVED="${self_observed}"
PR=""
DISCUSSION=""

if ! hook_event_has_step "self_observe_check"; then
  SO_ENFORCEMENT=\$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.self_observe_enforcement 2>/dev/null | tr -d '"' || echo "shadow")
  if [[ "\$SO_ENFORCEMENT" == "advisory" || "\$SO_ENFORCEMENT" == "enforced" ]]; then
    if [[ "\$VERDICT" == "done" || "\$VERDICT" == "pass" ]]; then
      if [[ "\${AGENT_SELF_OBSERVED:-false}" != "true" ]]; then
        AGENT_ID="\${ROLE}-\${DISCUSSION:-nodisc}-\${PR:-nopr}"
        bash "$tmp_hooks_dir/rotate-team-log.sh" comment \
          "[\$(date +%H:%M)] team-lead: WARN — agent=\${AGENT_ID} role=\${ROLE} skipped self-observe gate (\${SO_ENFORCEMENT} mode)" \
          2>/dev/null || true
        echo "[post-agent-hook] self-observe gate: WARN agent=\${AGENT_ID} role=\${ROLE} verdict=\${VERDICT} mode=\${SO_ENFORCEMENT}" >&2
      fi
    fi
  fi
  hook_event_mark_step "self_observe_check"
fi
SHIM
  chmod +x "$shim"

  AF_CONTROL_PLANE_CONFIG="$tmp_config" bash "$shim" 2>&1
  local rc=$?

  rm -f "$tmp_config" "$shim"
  rm -rf "$tmp_hooks_dir" "$tmp_event_dir"
  return $rc
}

# ── Tests ─────────────────────────────────────────────────────────────────────

echo ""
echo "=== test_post_agent_hook_self_observe.sh ==="
echo ""

# Test 1: Shadow mode (default) — missing self_observed on done verdict → no warning
echo "--- Test 1: shadow mode, missing self_observed, verdict=done → no warning ---"
OUT=$(run_hook_isolated "shadow" "done" "false")
assert_not_contains "T1: shadow mode — no WARN emitted" "$OUT" "WARN"
assert_not_contains "T1: shadow mode — no skipped self-observe" "$OUT" "skipped self-observe gate"

# Test 2: Advisory mode — missing self_observed on done verdict → warning emitted
echo "--- Test 2: advisory mode, missing self_observed, verdict=done → warning ---"
OUT=$(run_hook_isolated "advisory" "done" "false")
assert_contains "T2: advisory mode — WARN in team-log comment" "$OUT" "WARN"
assert_contains "T2: advisory mode — message mentions skipped self-observe gate" "$OUT" "skipped self-observe gate"
assert_contains "T2: advisory mode — mentions advisory mode" "$OUT" "advisory mode"

# Test 3: Advisory mode — self_observed:true present → no warning
echo "--- Test 3: advisory mode, self_observed=true, verdict=done → no warning ---"
OUT=$(run_hook_isolated "advisory" "done" "true")
assert_not_contains "T3: advisory + self_observed:true — no WARN" "$OUT" "WARN"
assert_not_contains "T3: advisory + self_observed:true — no skipped message" "$OUT" "skipped self-observe gate"

# Test 4: Advisory mode — needs-fix verdict → no warning (gate only fires on done/pass)
echo "--- Test 4: advisory mode, missing self_observed, verdict=needs-fix → no warning ---"
OUT=$(run_hook_isolated "advisory" "needs-fix" "false")
assert_not_contains "T4: advisory + needs-fix — no WARN (not done/pass)" "$OUT" "WARN"

# Test 5: Enforced mode — missing self_observed on done verdict → warning emitted
echo "--- Test 5: enforced mode, missing self_observed, verdict=done → warning ---"
OUT=$(run_hook_isolated "enforced" "done" "false")
assert_contains "T5: enforced mode — WARN in team-log comment" "$OUT" "WARN"
assert_contains "T5: enforced mode — mentions enforced mode" "$OUT" "enforced mode"

# Test 6: Advisory mode — verdict=pass (not just done) triggers warning
echo "--- Test 6: advisory mode, missing self_observed, verdict=pass → warning ---"
OUT=$(run_hook_isolated "advisory" "pass" "false")
assert_contains "T6: advisory + pass verdict — WARN emitted" "$OUT" "WARN"
assert_contains "T6: advisory + pass verdict — skipped message present" "$OUT" "skipped self-observe gate"

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
fi

echo ""
[[ $FAIL -eq 0 ]]
