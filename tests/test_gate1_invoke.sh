#!/usr/bin/env bash
# tests/test_gate1_invoke.sh
#
# Unit tests for scripts/gate1-invoke.sh (D#2560 PR-A). Fixture-based: no
# network, no live PR, no real gh call.
#
# Covers:
#   item 3  — the OPERATOR's copy of run-pr-tests.sh executes, never the
#             head's copy, even when --tree points at a head worktree whose
#             own scripts/run-pr-tests.sh has been edited.
#   item 4  — the wrapper's exit code is exactly the invoked runner's exit
#             code (captured on the line immediately following, per
#             CLAUDE.md's Merge Gate Protocol).
#   item 5  — identity is caller-imposed and honest: GATE1_RUNNER_UID unset
#             reports "gate1_containment=NONE (same-uid)" on stderr and
#             still runs the suite; GATE1_RUNNER_UID set to an absent user
#             exits non-zero and runs NO suite (fail closed, never a silent
#             same-uid fallback).
#   item 10 — a missing wrapper must degrade to the bare runner, not to a
#             hard failure. Exercises the exact fallback shape prescribed to
#             callers (an `if [ -x gate1-invoke.sh ]` guard) against a
#             fixture where the wrapper has been renamed aside.
#
# Fixture layout: a fake "operator checkout" ($OP) holding a real copy of
# gate1-invoke.sh alongside a STUB run-pr-tests.sh (no gh/network calls), and
# a fake "PR-head tree" ($HEAD) holding a stub run-pr-tests.sh that plants a
# sentinel. This never touches the real scripts/run-pr-tests.sh and never
# calls gh or the network.
#
# Usage: bash tests/test_gate1_invoke.sh — exits 0 iff all tests pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE1_INVOKE_SRC="$REPO_ROOT/scripts/gate1-invoke.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

if [ ! -f "$GATE1_INVOKE_SRC" ]; then
  fail "setup" "scripts/gate1-invoke.sh not found at $GATE1_INVOKE_SRC"
  echo ""
  echo "Results: $PASS passed, $FAIL failed"
  exit 1
fi

# ── Fixture setup ──────────────────────────────────────────────────────────

OP_DIR=$(mktemp -d)
HEAD_DIR=$(mktemp -d)
mkdir -p "$OP_DIR/scripts" "$HEAD_DIR/scripts"

# The operator's own copy of the wrapper — a real, unmodified copy.
cp "$GATE1_INVOKE_SRC" "$OP_DIR/scripts/gate1-invoke.sh"
chmod +x "$OP_DIR/scripts/gate1-invoke.sh"

# The operator's own stub runner: proves it ran (distinct marker), proves it
# saw the tree root the wrapper passed, and exits with a distinguishable code
# so exit-code passthrough (item 4) is verifiable.
cat > "$OP_DIR/scripts/run-pr-tests.sh" <<'STUB'
#!/usr/bin/env bash
echo "OPERATOR_COPY_RAN pr=$1 tree=${RUN_PR_TESTS_TREE_ROOT:-<unset>}"
exit 42
STUB
chmod +x "$OP_DIR/scripts/run-pr-tests.sh"

# The head's own (edited) copy — this must NEVER execute. A benign sentinel
# only, never a working exploit, per the Spec's own instruction.
cat > "$HEAD_DIR/scripts/run-pr-tests.sh" <<'STUB'
#!/usr/bin/env bash
echo "GATE1_HEAD_COPY_RAN"
exit 1
STUB
chmod +x "$HEAD_DIR/scripts/run-pr-tests.sh"

# ── item 3: operator's copy runs, head's copy never does ──────────────────

OUT_3=$(bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" 2>&1)

if echo "$OUT_3" | grep -q "GATE1_HEAD_COPY_RAN"; then
  fail "item3-no-head-copy" "head's sentinel appeared in output: $OUT_3"
else
  pass "item3-no-head-copy"
fi

if echo "$OUT_3" | grep -qF "gate1_runner_copy=$OP_DIR/scripts/run-pr-tests.sh"; then
  pass "item3-runner-copy-is-operators"
else
  fail "item3-runner-copy-is-operators" "expected gate1_runner_copy=$OP_DIR/scripts/run-pr-tests.sh, got: $OUT_3"
fi

if echo "$OUT_3" | grep -qF "gate1_tree_root=$HEAD_DIR"; then
  pass "item3-tree-root-is-head"
else
  fail "item3-tree-root-is-head" "expected gate1_tree_root=$HEAD_DIR, got: $OUT_3"
fi

if echo "$OUT_3" | grep -qF "OPERATOR_COPY_RAN pr=999 tree=$HEAD_DIR"; then
  pass "item3-operator-copy-ran-against-head-tree"
else
  fail "item3-operator-copy-ran-against-head-tree" "expected OPERATOR_COPY_RAN pr=999 tree=$HEAD_DIR, got: $OUT_3"
fi

# ── item 4: exit code passes through exactly ───────────────────────────────

bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" >/dev/null 2>&1
RC_4=$?

if [ "$RC_4" -eq 42 ]; then
  pass "item4-exit-code-passthrough"
else
  fail "item4-exit-code-passthrough" "expected rc=42 (the stub runner's exit code), got rc=$RC_4"
fi

# ── item 5: identity is caller-imposed and the default is honest ──────────

ERR_5_UNSET=$(bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" 2>&1 1>/dev/null)

if echo "$ERR_5_UNSET" | grep -qF "gate1_containment=NONE (same-uid)"; then
  pass "item5-unset-uid-reports-none"
else
  fail "item5-unset-uid-reports-none" "expected literal 'gate1_containment=NONE (same-uid)' on stderr, got: $ERR_5_UNSET"
fi

NONEXISTENT_USER="gate1_no_such_user_$$_$RANDOM"
OUT_5_ABSENT=$(GATE1_RUNNER_UID="$NONEXISTENT_USER" bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" 2>&1)
RC_5_ABSENT=$?

if [ "$RC_5_ABSENT" -ne 0 ]; then
  pass "item5-absent-uid-fails-closed-exit"
else
  fail "item5-absent-uid-fails-closed-exit" "expected non-zero exit with GATE1_RUNNER_UID set to an absent user, got rc=0"
fi

if echo "$OUT_5_ABSENT" | grep -q "OPERATOR_COPY_RAN"; then
  fail "item5-absent-uid-no-suite-ran" "the runner executed even though GATE1_RUNNER_UID named an absent user: $OUT_5_ABSENT"
else
  pass "item5-absent-uid-no-suite-ran"
fi

# Static check on item 5's own hard rule: the value must come from the
# process environment only, never from anything under the tree root.
if grep -n 'GATE1_RUNNER_UID' "$GATE1_INVOKE_SRC" | grep -qi 'TREE_ROOT\|HEAD\|\$2'; then
  fail "item5-uid-never-derived-from-tree" "a GATE1_RUNNER_UID line references the tree root"
else
  pass "item5-uid-never-derived-from-tree"
fi

# ── item 10: a missing wrapper degrades to the bare runner, not a hard failure ──
#
# Exercises the exact fallback shape prescribed to callers in
# .claude/agents/code-reviewer.md / backend/spawn_templates/code-reviewer.tmpl:
# when the wrapper is absent, fall back to invoking run-pr-tests.sh directly.

WRAPPER_PATH="$OP_DIR/scripts/gate1-invoke.sh"
WRAPPER_MOVED="$OP_DIR/scripts/gate1-invoke.sh.movedaside"
mv "$WRAPPER_PATH" "$WRAPPER_MOVED"

if [ -x "$WRAPPER_PATH" ]; then
  OUT_10=$(bash "$WRAPPER_PATH" --pr 999 --tree "$HEAD_DIR" 2>&1)
else
  OUT_10=$(bash "$OP_DIR/scripts/run-pr-tests.sh" 999 2>&1)
fi
RC_10=$?

mv "$WRAPPER_MOVED" "$WRAPPER_PATH"

if echo "$OUT_10" | grep -q "OPERATOR_COPY_RAN"; then
  pass "item10-fallback-reaches-runner"
else
  fail "item10-fallback-reaches-runner" "fallback path never reached run-pr-tests.sh: $OUT_10"
fi

if [ "$RC_10" -eq 42 ]; then
  pass "item10-fallback-still-produces-pass-fail"
else
  fail "item10-fallback-still-produces-pass-fail" "expected the runner's own exit code (42) to still surface, got rc=$RC_10"
fi

# ── Teardown ─────────────────────────────────────────────────────────────

rm -rf "$OP_DIR" "$HEAD_DIR"

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
exit 0
