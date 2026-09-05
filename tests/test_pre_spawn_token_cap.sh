#!/usr/bin/env bash
# tests/test_pre_spawn_token_cap.sh
# Verifies that pre-spawn-check.sh blocks researcher spawns when budget remaining
# is below policies.researcher.token_cap.
#
# HARD RULE: UNDER NO CIRCUMSTANCES may this test invoke `claude`, `claude -p`,
# `_start_loop_run`, or trigger /loop. Budget and control_plane are mocked.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"

PASS=0
FAIL=0

ok()   { echo "  [OK]   $1"; ((PASS++)) || true; }
fail() { echo "  [FAIL] $1"; ((FAIL++)) || true; }

TMPDIR_BASE=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_BASE"; }
trap cleanup EXIT

# ── Build a minimal workspace with mock backend scripts ───────────────────────
make_ws() {
  local ws
  ws=$(mktemp -d "$TMPDIR_BASE/ws-XXXXXX")
  mkdir -p "$ws/backend" "$ws/scripts/lib" "$ws/.autonomous-team"

  # Minimal config so control_plane.py doesn't error
  cp "$REPO_ROOT/.autonomous-team/config.json" "$ws/.autonomous-team/config.json" 2>/dev/null || \
    echo '{"gates":{},"policies":{"researcher":{"token_cap":50000}}}' > "$ws/.autonomous-team/config.json"

  # Copy required scripts
  cp "$SCRIPTS_DIR/pre-spawn-check.sh" "$ws/scripts/"
  cp -r "$SCRIPTS_DIR/lib/"           "$ws/scripts/lib/"
  cp "$SCRIPTS_DIR/rotate-team-log.sh" "$ws/scripts/" 2>/dev/null || \
    printf '#!/bin/bash\nexit 0\n' > "$ws/scripts/rotate-team-log.sh"
  cp "$SCRIPTS_DIR/agent-feed-append.sh" "$ws/scripts/" 2>/dev/null || \
    printf '#!/bin/bash\nexit 0\n' > "$ws/scripts/agent-feed-append.sh"

  chmod +x "$ws/scripts/"*.sh "$ws/scripts/lib/"*.sh 2>/dev/null || true

  # Copy backend Python modules needed by pre-spawn-check
  for mod in budget.py control_plane.py circuit_breaker.py context_manager.py \
              agent_memory.py agent_cards.py lessons.py state_paths.py; do
    cp "$REPO_ROOT/backend/$mod" "$ws/backend/" 2>/dev/null || true
  done

  echo "$ws"
}

# ── Mock budget.py: returns remaining=30000 (below 50000 cap) ─────────────────
mock_budget_low() {
  local ws="$1"
  cat > "$ws/backend/budget.py" <<'EOF'
import sys, json
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "check":
    print(json.dumps({"allowed": True, "remaining": 30000}))
elif __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "status":
    print(json.dumps({"allowed": True, "remaining": 30000}))
else:
    print(json.dumps({"allowed": True, "remaining": 30000}))
EOF
}

# ── Mock budget.py: returns remaining=100000 (above 50000 cap) ────────────────
mock_budget_high() {
  local ws="$1"
  cat > "$ws/backend/budget.py" <<'EOF'
import sys, json
if __name__ == "__main__":
    print(json.dumps({"allowed": True, "remaining": 100000}))
EOF
}

# ── Mock control_plane.py: returns 50000 for policies.researcher.token_cap ────
mock_control_plane() {
  local ws="$1"
  cat > "$ws/backend/control_plane.py" <<'PYEOF'
import sys, json
if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "get" and len(args) > 1:
        key = args[1]
        if key == "policies.researcher.token_cap":
            print("50000")
        else:
            print("null")
    elif args and args[0] == "show":
        print(json.dumps({"gates": {}, "policies": {"researcher": {"token_cap": 50000}}}))
    else:
        print("null")
PYEOF
}

# ── Minimal stubs for modules pre-spawn-check optionally calls ────────────────
stub_optional_modules() {
  local ws="$1"
  for mod in context_manager agent_memory agent_cards lessons circuit_breaker; do
    cat > "$ws/backend/${mod}.py" <<'EOF'
import sys, json
if __name__ == "__main__":
    print(json.dumps([]))
EOF
  done
}

# ── Minimal git repo so worktree detection works ──────────────────────────────
init_git() {
  local ws="$1"
  git -C "$ws" init -q
  git -C "$ws" commit --allow-empty -m "init" -q
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: budget remaining 30K < cap 50K → spawn blocked
# ─────────────────────────────────────────────────────────────────────────────
WS=$(make_ws)
init_git "$WS"
mock_budget_low "$WS"
mock_control_plane "$WS"
stub_optional_modules "$WS"

# Minimal hook-event stubs so the script doesn't fail on missing lib
mkdir -p "$WS/scripts/lib"
cat > "$WS/scripts/lib/hook-event.sh" <<'EOF'
hook_event_init() { :; }
hook_event_has_step() { return 1; }
hook_event_mark_step() { :; }
hook_event_finish() { :; }
EOF
cat > "$WS/scripts/lib/persona.sh"            <<'EOF'
persona_voice_block() { echo ""; }
EOF
cat > "$WS/scripts/lib/working-principles.sh" <<'EOF'
working_principles_block() { echo ""; }
EOF
cat > "$WS/scripts/lib/self-observe-gate.sh"  <<'EOF'
self_observe_gate_block() { echo ""; }
EOF

OUTPUT=$(cd "$WS" && bash scripts/pre-spawn-check.sh \
  --role researcher --discussion 647 --event-id "test-cap-1" \
  --dry-run 2>&1 || true)

# In dry-run the cap block is skipped — test the non-dry-run path
RC=0
OUTPUT2=$(cd "$WS" && bash scripts/pre-spawn-check.sh \
  --role researcher --discussion 647 --event-id "test-cap-2" 2>&1) || RC=$?

if [[ "$RC" -ne 0 && "$OUTPUT2" =~ "token_cap" ]]; then
  ok "low-budget researcher spawn blocked by per-role token_cap"
else
  fail "expected spawn to be blocked but got RC=$RC output=${OUTPUT2:0:200}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Test 2: budget remaining 100K >= cap 50K → spawn allowed (exit 0, JSON output)
# ─────────────────────────────────────────────────────────────────────────────
WS2=$(make_ws)
init_git "$WS2"
mock_budget_high "$WS2"
mock_control_plane "$WS2"
stub_optional_modules "$WS2"

mkdir -p "$WS2/scripts/lib"
cat > "$WS2/scripts/lib/hook-event.sh" <<'EOF'
hook_event_init() { :; }
hook_event_has_step() { return 1; }
hook_event_mark_step() { :; }
hook_event_finish() { :; }
EOF
cat > "$WS2/scripts/lib/persona.sh"            <<'EOF'
persona_voice_block() { echo ""; }
EOF
cat > "$WS2/scripts/lib/working-principles.sh" <<'EOF'
working_principles_block() { echo ""; }
EOF
cat > "$WS2/scripts/lib/self-observe-gate.sh"  <<'EOF'
self_observe_gate_block() { echo ""; }
EOF

RC2=0
OUTPUT3=$(cd "$WS2" && bash scripts/pre-spawn-check.sh \
  --role researcher --discussion 647 --event-id "test-cap-3" 2>&1) || RC2=$?

if [[ "$RC2" -eq 0 ]]; then
  ok "sufficient-budget researcher spawn allowed"
else
  fail "expected spawn to be allowed but got RC=$RC2 output=${OUTPUT3:0:200}"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
