#!/usr/bin/env bash
# tests/test_plugin_install_gate_isolation.sh — pins the detection power of
# open-source/checks/plugin-install-gate.sh's isolation check (D#2210 round
# 2). Round 1 shipped an "isolation-integrity canary" that only compared
# `claude plugin marketplace list` before and after `add` -- both reviewers
# demonstrated by construction that a claude build which silently ignores
# CLAUDE_CONFIG_DIR and always reads/writes one fixed location passes that
# comparison cleanly, because `list` and `add` just agree with each other
# through whatever wrong place the CLI is actually using. That is a
# self-report, not ground truth.
#
# This test runs the REAL gate script against tests/fixtures/fake-claude in
# each of its modes:
#   - FAKE_CLAUDE_MODE=noncompliant ignores the isolation override entirely
#     -- the gate MUST FAIL (the on-disk marketplace-registry check should
#     never find our marketplace where it's supposed to be).
#   - FAKE_CLAUDE_MODE=noncompliant-install (round 3: both reviewers built
#     this harder, partially-compliant adversary independently) honors the
#     override for marketplace add/list/remove but ignores it for
#     install/uninstall/details -- the gate MUST still FAIL, now via the
#     symmetric on-disk check after `plugin install`.
#   - FAKE_CLAUDE_MODE=compliant honors CLAUDE_CONFIG_DIR everywhere, like a
#     real, well-behaved build (confirmed against real claude 2.1.258 and
#     2.1.187 by the coordinator) -- the gate MUST PASS.
#
# Never invokes the real `claude` CLI: the fake is placed first on PATH,
# and every PATH entry that would otherwise resolve a real `claude` binary
# is excluded from the PATH this test constructs, so there is no entry a
# lookup could fall through to even if the prepended fake were somehow
# skipped.
#
# Usage: bash tests/test_plugin_install_gate_isolation.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATE_SH="$REPO_ROOT/open-source/checks/plugin-install-gate.sh"
FAKE_CLAUDE_SRC="$REPO_ROOT/tests/fixtures/fake-claude"

pass=0
fail=0

check() {
  local desc="$1" result="$2"
  if [ "$result" = ok ]; then
    echo "PASS: $desc"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — $result"
    fail=$((fail + 1))
  fi
}

if [[ ! -f "$GATE_SH" ]]; then
  check "gate script exists at $GATE_SH" "not found"
  echo ""
  echo "Results: $pass passed, $fail failed"
  exit 1
fi
if [[ ! -x "$FAKE_CLAUDE_SRC" ]]; then
  check "fake-claude fixture exists and is executable" "not found or not executable at $FAKE_CLAUDE_SRC"
  echo ""
  echo "Results: $pass passed, $fail failed"
  exit 1
fi

CLEANUP_DIRS=()
cleanup_all() {
  for d in "${CLEANUP_DIRS[@]}"; do
    rm -rf "$d"
  done
}
trap cleanup_all EXIT

# --- Build a tiny synthetic export tree -------------------------------
# 2 agents + 1 command so the expected-inventory numbers the gate computes
# are small and distinctive (not the real repo's 26/2), and so a passing
# run's counts can only match by the fake actually reading this directory.
build_synthetic_export() {
  local dir
  dir="$(mktemp -d)"
  mkdir -p "$dir/.claude-plugin" "$dir/agents" "$dir/commands"
  cat > "$dir/.claude-plugin/plugin.json" <<'EOF'
{
  "name": "gate-test-plugin",
  "version": "0.0.1",
  "displayName": "Gate Test Plugin",
  "description": "synthetic fixture for tests/test_plugin_install_gate_isolation.sh",
  "license": "MIT"
}
EOF
  cat > "$dir/.claude-plugin/marketplace.json" <<'EOF'
{
  "name": "gate-test-marketplace",
  "owner": {"name": "gate-test"},
  "description": "synthetic fixture marketplace",
  "plugins": [{"name": "gate-test-plugin", "source": "./"}]
}
EOF
  echo "# agent one" > "$dir/agents/agent-one.md"
  echo "# agent two" > "$dir/agents/agent-two.md"
  echo "# command one" > "$dir/commands/command-one.md"
  printf '%s' "$dir"
}

# --- Build a PATH with the fake claude first and every real claude
# excluded, so this test cannot reach the real CLI even by accident. -----
build_fake_bin_dir() {
  local dir
  dir="$(mktemp -d)"
  cp "$FAKE_CLAUDE_SRC" "$dir/claude"
  chmod +x "$dir/claude"
  printf '%s' "$dir"
}

build_no_real_claude_path() {
  local fake_bin_dir="$1" out=() d
  IFS=':' read -r -a dirs <<< "$PATH"
  for d in "${dirs[@]}"; do
    [[ -d "$d" && -x "$d/claude" ]] && continue
    out+=("$d")
  done
  (IFS=:; printf '%s' "${fake_bin_dir}:${out[*]}")
}

run_gate_with_fake() {
  local mode="$1" export_dir="$2" fixed_state_dir="$3" fake_bin_dir="$4" test_path="$5"
  env -i \
    PATH="$test_path" \
    FAKE_CLAUDE_MODE="$mode" \
    FAKE_CLAUDE_FIXED_STATE_DIR="$fixed_state_dir" \
    bash "$GATE_SH" "$export_dir" 2>&1
}

SYNTH_EXPORT="$(build_synthetic_export)"; CLEANUP_DIRS+=("$SYNTH_EXPORT")
FAKE_BIN_DIR="$(build_fake_bin_dir)"; CLEANUP_DIRS+=("$FAKE_BIN_DIR")
TEST_PATH="$(build_no_real_claude_path "$FAKE_BIN_DIR")"

# --- Case 1: noncompliant fake -- gate MUST fail --------------------------
FIXED_STATE_1="$(mktemp -d)"; CLEANUP_DIRS+=("$FIXED_STATE_1")
out1="$(run_gate_with_fake noncompliant "$SYNTH_EXPORT" "$FIXED_STATE_1" "$FAKE_BIN_DIR" "$TEST_PATH")"
rc1=$?
if [[ "$rc1" -ne 0 ]] && printf '%s' "$out1" | grep -q "known_marketplaces.json"; then
  check "noncompliant fake (ignores CLAUDE_CONFIG_DIR) makes the gate FAIL, citing the on-disk registry check" "ok"
else
  check "noncompliant fake (ignores CLAUDE_CONFIG_DIR) makes the gate FAIL, citing the on-disk registry check" "rc=$rc1 (expected nonzero); output did not mention known_marketplaces.json as expected. Output: $out1"
fi

# Confirm the real host's plugin config was never touched by mode=noncompliant
# actually reading/writing FAKE_CLAUDE_FIXED_STATE_DIR only (a filesystem
# fact, checked directly rather than trusted): the fixed state dir should
# now hold the marketplace registry the noncompliant fake wrote, proving
# the fake really did write somewhere other than any CLAUDE_CONFIG_DIR the
# gate set up.
if [[ -f "$FIXED_STATE_1/plugins/known_marketplaces.json" ]]; then
  check "noncompliant fake wrote its registry to the fixed location, not an isolated CLAUDE_CONFIG_DIR" "ok"
else
  check "noncompliant fake wrote its registry to the fixed location, not an isolated CLAUDE_CONFIG_DIR" "no known_marketplaces.json found under $FIXED_STATE_1"
fi

# --- Case 2: noncompliant-install fake (partially compliant) -- gate MUST fail
# Honors the override for marketplace subcommands (so the round-2 check
# passes) but ignores it for install/uninstall/details -- pins the round-3
# fix, the symmetric on-disk check after `plugin install`.
FIXED_STATE_2="$(mktemp -d)"; CLEANUP_DIRS+=("$FIXED_STATE_2")
out2="$(run_gate_with_fake noncompliant-install "$SYNTH_EXPORT" "$FIXED_STATE_2" "$FAKE_BIN_DIR" "$TEST_PATH")"
rc2=$?
if [[ "$rc2" -ne 0 ]] && printf '%s' "$out2" | grep -q "installed_plugins.json"; then
  check "noncompliant-install fake (marketplace isolated, install is not) makes the gate FAIL, citing the on-disk install-record check" "ok"
else
  check "noncompliant-install fake (marketplace isolated, install is not) makes the gate FAIL, citing the on-disk install-record check" "rc=$rc2 (expected nonzero); output did not mention installed_plugins.json as expected. Output: $out2"
fi

# --- Case 3: compliant fake -- gate MUST pass -----------------------------
FIXED_STATE_3="$(mktemp -d)"; CLEANUP_DIRS+=("$FIXED_STATE_3")
out3="$(run_gate_with_fake compliant "$SYNTH_EXPORT" "$FIXED_STATE_3" "$FAKE_BIN_DIR" "$TEST_PATH")"
rc3=$?
if [[ "$rc3" -eq 0 ]] && printf '%s' "$out3" | grep -q "PASS: plugin-install-gate (agents=2 skills=1)"; then
  check "compliant fake (honors CLAUDE_CONFIG_DIR everywhere) makes the gate PASS with the synthetic export's real counts" "ok"
else
  check "compliant fake (honors CLAUDE_CONFIG_DIR everywhere) makes the gate PASS with the synthetic export's real counts" "rc=$rc3 (expected 0); output: $out3"
fi

# --- Summary ---

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
