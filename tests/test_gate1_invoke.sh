#!/usr/bin/env bash
# tests/test_gate1_invoke.sh
#
# Unit tests for scripts/gate1-invoke.sh (D#2560 PR-A, extended by D#2566
# PR-1 for the non-exec / caller-side-resolution / receipt-writing shape).
# Fixture-based: no network, no live PR, no real gh call.
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
#   pr-validation (security review, non-blocking hardening item) — a --pr
#             value that isn't a bare positive integer is rejected before it
#             can reach `gh pr view` as a flag.
#   item 10 — a missing wrapper must degrade to the bare runner, not to a
#             hard failure. Exercises the exact fallback shape prescribed to
#             callers (an `if [ -x gate1-invoke.sh ]` guard) against a
#             fixture where the wrapper has been renamed aside.
#   D#2566 no-exec — neither arm of gate1-invoke.sh ends in `exec`; this
#             process survives the runner so it can write a receipt.
#
# Fixture layout: a fake "operator checkout" ($OP) holding a real copy of
# gate1-invoke.sh (plus its lib/ dependencies and a deterministic, no-
# network gate1-verify-containment.sh stub) alongside a STUB
# run-pr-tests.sh (no gh/network calls), and a fake "PR-head tree" ($HEAD)
# holding a stub run-pr-tests.sh that plants a sentinel. This never touches
# the real scripts/run-pr-tests.sh and never calls gh or the network. Every
# invocation below passes --pr-head-sha and --changed-files-from so this
# wrapper's own gh-resolution path is never reached either.
#
# Usage: bash tests/test_gate1_invoke.sh — exits 0 iff all tests pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE1_INVOKE_SRC="$REPO_ROOT/scripts/gate1-invoke.sh"
GATE1_RECEIPT_LIB_SRC="$REPO_ROOT/scripts/lib/gate1-receipt.sh"
REPO_RESOLVE_SRC="$REPO_ROOT/scripts/lib/repo-resolve.sh"

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
STATE_DIR=$(mktemp -d)
mkdir -p "$OP_DIR/scripts" "$HEAD_DIR/scripts" "$OP_DIR/scripts/lib"

export AUTONOMOUS_TEAM_REPO="fixture/repo"
export AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR"

# The operator's own copy of the wrapper — a real, unmodified copy — plus
# its real lib/ dependencies, so this test exercises the actual
# argument-parsing, sha-validation and receipt-writing code paths, not a
# reimplementation of them.
cp "$GATE1_INVOKE_SRC" "$OP_DIR/scripts/gate1-invoke.sh"
chmod +x "$OP_DIR/scripts/gate1-invoke.sh"
cp "$GATE1_RECEIPT_LIB_SRC" "$OP_DIR/scripts/lib/gate1-receipt.sh"
cp "$REPO_RESOLVE_SRC" "$OP_DIR/scripts/lib/repo-resolve.sh"

# Deterministic, no-network containment reporter — this suite is about
# gate1-invoke.sh's own argument handling and process control, not about
# gate1-verify-containment.sh (covered by tests/test_gate1_verify_containment.sh
# and by tests/test_gate1_receipt.sh's INDETERMINATE case).
cat > "$OP_DIR/scripts/gate1-verify-containment.sh" <<'STUB'
#!/usr/bin/env bash
echo "gate1_probe gh-credential=NOT-DENIED"
echo "gate1_probe state-dir=NOT-DENIED"
echo "gate1_probe operator-checkout-write=NOT-DENIED"
echo "gate1_probe network=NOT-DENIED"
echo "gate1_containment_verdict=UNCONTAINED"
exit 0
STUB
chmod +x "$OP_DIR/scripts/gate1-verify-containment.sh"

# The operator's own stub runner: proves it ran (distinct marker), proves it
# saw the tree root the wrapper passed, and exits with a distinguishable code
# so exit-code passthrough (item 4) is verifiable. Accepts (and ignores) the
# --manifest-out/--pr-head-sha/--changed-files-from flags gate1-invoke.sh
# now always passes, and always writes a minimal valid manifest to
# --manifest-out so the wrapper's own receipt-write step has something to
# read (item 1: a real invocation must produce a receipt file).
cat > "$OP_DIR/scripts/run-pr-tests.sh" <<'STUB'
#!/usr/bin/env bash
echo "OPERATOR_COPY_RAN pr=$1 tree=${RUN_PR_TESTS_TREE_ROOT:-<unset>}"
shift
MANIFEST_OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --manifest-out) MANIFEST_OUT="$2"; shift 2 ;;
    --pr-head-sha) shift 2 ;;
    --changed-files-from) shift 2 ;;
    *) shift ;;
  esac
done
if [ -n "$MANIFEST_OUT" ]; then
  echo '{"routing":[],"tests_run":[],"measured_tree":{}}' > "$MANIFEST_OUT"
fi
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

FAKE_SHA="0000000000000000000000000000000000000042"
CHANGED_FILES_FILE="$(mktemp)"
printf 'README.md\n' > "$CHANGED_FILES_FILE"
GATE1_ARGS=(--pr-head-sha "$FAKE_SHA" --changed-files-from "$CHANGED_FILES_FILE")

# ── D#2566: neither arm of gate1-invoke.sh ends in `exec` ──────────────────

if grep -nE '^\s*exec |[^a-zA-Z]exec sudo' "$GATE1_INVOKE_SRC" >/dev/null; then
  fail "no-exec" "gate1-invoke.sh still contains an exec invocation — a receipt can never be written after it"
else
  pass "no-exec"
fi

# ── item 3: operator's copy runs, head's copy never does ──────────────────

OUT_3=$(bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" "${GATE1_ARGS[@]}" 2>&1)

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

bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" "${GATE1_ARGS[@]}" >/dev/null 2>&1
RC_4=$?

if [ "$RC_4" -eq 42 ]; then
  pass "item4-exit-code-passthrough"
else
  fail "item4-exit-code-passthrough" "expected rc=42 (the stub runner's exit code), got rc=$RC_4"
fi

# ── item 5: identity is caller-imposed and the default is honest ──────────

ERR_5_UNSET=$(bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" "${GATE1_ARGS[@]}" 2>&1 1>/dev/null)

if echo "$ERR_5_UNSET" | grep -qF "gate1_containment=NONE (same-uid)"; then
  pass "item5-unset-uid-reports-none"
else
  fail "item5-unset-uid-reports-none" "expected literal 'gate1_containment=NONE (same-uid)' on stderr, got: $ERR_5_UNSET"
fi

NONEXISTENT_USER="gate1_no_such_user_$$_$RANDOM"
OUT_5_ABSENT=$(GATE1_RUNNER_UID="$NONEXISTENT_USER" bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 999 --tree "$HEAD_DIR" "${GATE1_ARGS[@]}" 2>&1)
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

# Static check on item 5's own hard rule: the line that actually RESOLVES
# the identity value (assigns it to RUNNER_UID) must come from the process
# environment only, never from anything under the tree root. Scoped to that
# one assignment line rather than every line mentioning the env var name —
# a doc comment naming both GATE1_RUNNER_UID and RUN_PR_TESTS_TREE_ROOT in
# the same sentence (e.g. describing what sudo preserves) is not a
# derivation and must not fail this check.
RESOLUTION_LINE=$(grep -n 'RUNNER_UID=.*GATE1_RUNNER_UID' "$GATE1_INVOKE_SRC")
if [ -z "$RESOLUTION_LINE" ]; then
  fail "item5-uid-never-derived-from-tree" "no line assigns RUNNER_UID from \$GATE1_RUNNER_UID at all"
elif echo "$RESOLUTION_LINE" | grep -qi 'TREE_ROOT\|HEAD\|\$2'; then
  fail "item5-uid-never-derived-from-tree" "the RUNNER_UID resolution line references the tree root: $RESOLUTION_LINE"
else
  pass "item5-uid-never-derived-from-tree"
fi

# ── pr-validation: a --pr value that could be mistaken for a flag is rejected ──

OUT_PRVAL=$(bash "$OP_DIR/scripts/gate1-invoke.sh" --pr -123 --tree "$HEAD_DIR" "${GATE1_ARGS[@]}" 2>&1)
RC_PRVAL=$?

if [ "$RC_PRVAL" -ne 0 ]; then
  pass "prval-rejects-non-numeric-exit"
else
  fail "prval-rejects-non-numeric-exit" "expected non-zero exit for --pr -123, got 0"
fi

if echo "$OUT_PRVAL" | grep -q "OPERATOR_COPY_RAN"; then
  fail "prval-rejects-before-running-suite" "the runner executed despite an invalid --pr value: $OUT_PRVAL"
else
  pass "prval-rejects-before-running-suite"
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
  OUT_10=$(bash "$WRAPPER_PATH" --pr 999 --tree "$HEAD_DIR" "${GATE1_ARGS[@]}" 2>&1)
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

rm -rf "$OP_DIR" "$HEAD_DIR" "$STATE_DIR" "$CHANGED_FILES_FILE"

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
