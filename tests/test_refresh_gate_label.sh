#!/usr/bin/env bash
# tests/test_refresh_gate_label.sh — hermetic tests for
# scripts/refresh-gate-label.sh (D#2535).
#
# All tests stub `gh` on PATH — no real network calls, no real repo mutation.
# The real-GitHub-behaviour half of this Spec (a second add-label producing
# NO new `labeled` event, and this helper producing one) is demonstrated
# separately, against a real PR, per the Discussion's Gate 2 bar — a mock
# cannot prove anything about GitHub's own no-op behaviour, only about this
# script's logic given that behaviour as an assumption. That assumption is
# exactly what this stub encodes: a repeated add-label call here is a true
# no-op (it does not append to the mutation log), matching what D#2535
# measured on PR #155's real timeline.
#
# Covers:
#   1. Label absent on the PR -> plain add. One mutation, no remove.
#   2. Label already present on the PR -> remove then add. Two mutations,
#      in that order, and the label reads back present afterward.
#   3. A NACK label (scripts/lib/merge-gate-labels.sh) is refused by name —
#      nonzero exit, zero mutations, regardless of whether it was present.
#   4. Usage error (missing pr or label) exits nonzero before any gh call.
#
# Usage:
#   bash tests/test_refresh_gate_label.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REFRESH_SH="$REPO_ROOT/scripts/refresh-gate-label.sh"
MERGE_GATE_LABELS_LIB="$REPO_ROOT/scripts/lib/merge-gate-labels.sh"
# shellcheck source=../scripts/lib/merge-gate-labels.sh
source "$MERGE_GATE_LABELS_LIB"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — ${2:-}"; FAIL=$((FAIL + 1)); ERRORS+=("$1: ${2:-}"); }

assert_eq() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then pass "$label"; else fail "$label" "got '$actual', expected '$expected'"; fi
}

# ── Stub `gh` builder ──────────────────────────────────────────────────────
# Mirrors tests/test_verdict_label_relay.sh's stub: logs every call, answers
# the GraphQL PR-node-id / label-id lookups gh-label.sh needs on the
# worktree/GraphQL path (forced on via GH_LABEL_FORCE_WORKTREE=1 below, so
# this suite exercises the same code path the sandbox forces in a real
# worktree), and tracks addLabelsToLabelable / removeLabelsFromLabelable
# mutations in order so tests can assert both count and sequence.
#
# `gh pr view <pr> --json labels --jq '.labels[].name'` — the call
# refresh-gate-label.sh makes directly — returns the state file's contents
# one label per line, matching real gh --jq output for that expression.
build_gh_stub() {
  local bindir="$1"
  mkdir -p "$bindir"
  cat > "$bindir/gh" <<'STUB'
#!/usr/bin/env bash
LOG="${GH_STUB_LOG:?GH_STUB_LOG not set}"
MUT_LOG="${GH_STUB_LOG}.mutations"
STATE_DIR="${GH_STUB_STATE_DIR:?GH_STUB_STATE_DIR not set}"
echo "CALL: $*" >> "$LOG"

if [[ "${1:-}" == "pr" && "${2:-}" == "view" ]]; then
  pr="${3:-unknown}"
  state_file="$STATE_DIR/labels-$pr.txt"
  touch "$state_file"
  cat "$state_file"
  exit 0
fi

if [[ "${1:-}" == "api" && "${2:-}" == "graphql" ]]; then
  QUERY=""
  prev=""
  for a in "$@"; do
    if { [[ "$prev" == "-f" ]] || [[ "$prev" == "-F" ]]; } && [[ "$a" == query=* ]]; then
      QUERY="${a#query=}"
    fi
    prev="$a"
  done

  if [[ "$QUERY" == *"pullRequest(number:"* ]]; then
    echo '{"data":{"repository":{"pullRequest":{"id":"PR_NODE_1"}}}}'
    exit 0
  fi

  if [[ "$QUERY" == *"label(name:"* ]]; then
    LBL=""
    prev=""
    for a in "$@"; do
      if [[ "$prev" == "-f" ]] && [[ "$a" == label=* ]]; then
        LBL="${a#label=}"
      fi
      prev="$a"
    done
    echo "{\"data\":{\"repository\":{\"label\":{\"id\":\"LBL_${LBL}\"}}}}"
    exit 0
  fi

  if [[ "$QUERY" == *"addLabelsToLabelable"* ]]; then
    pr="${GH_STUB_CURRENT_PR:-unknown}"
    label="${GH_STUB_CURRENT_LABEL:-unknown}"
    echo "MUTATE addLabelsToLabelable $label" >> "$MUT_LOG"
    # A real re-add of a label already on the issue is a no-op: GitHub writes
    # no new event and the label set doesn't change. Mirror that here so a
    # test that forgets to remove first cannot pass by accident.
    if ! grep -qx -- "$label" "$STATE_DIR/labels-$pr.txt" 2>/dev/null; then
      echo "$label" >> "$STATE_DIR/labels-$pr.txt"
    fi
    echo '{"data":{"addLabelsToLabelable":{"clientMutationId":null}}}'
    exit 0
  fi

  if [[ "$QUERY" == *"removeLabelsFromLabelable"* ]]; then
    pr="${GH_STUB_CURRENT_PR:-unknown}"
    label="${GH_STUB_CURRENT_LABEL:-unknown}"
    echo "MUTATE removeLabelsFromLabelable $label" >> "$MUT_LOG"
    if [[ -f "$STATE_DIR/labels-$pr.txt" ]]; then
      grep -v -x -F "$label" "$STATE_DIR/labels-$pr.txt" > "$STATE_DIR/labels-$pr.txt.tmp" 2>/dev/null || true
      mv "$STATE_DIR/labels-$pr.txt.tmp" "$STATE_DIR/labels-$pr.txt" 2>/dev/null || true
    fi
    exit 0
  fi

  echo '{"data":{}}'
  exit 0
fi

echo '{"data":{}}'
exit 0
STUB
  chmod +x "$bindir/gh"
}

mutations_of() {
  local mut_log="${GH_STUB_LOG}.mutations"
  [[ -f "$mut_log" ]] && cat "$mut_log" || true
}

mutation_count() {
  local mut_log="${GH_STUB_LOG}.mutations"
  [[ -f "$mut_log" ]] && wc -l < "$mut_log" | tr -d ' ' || echo 0
}

# ── Common test env ──────────────────────────────────────────────────────────
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
STUB_BIN="$TEST_DIR/bin"
build_gh_stub "$STUB_BIN"
export PATH="$STUB_BIN:$PATH"
export GH_LABEL_FORCE_WORKTREE=1
export GH_STUB_STATE_DIR="$TEST_DIR/state"
mkdir -p "$GH_STUB_STATE_DIR"
# Resolve unambiguously to a fixed slug for every case below — the real
# resolver is exercised by other suites (repo-resolve.sh isn't new here).
# refresh-gate-label.sh resolves through _require_code_repo (repo-resolve.sh)
# directly, not through gh-label.sh's LABEL_REPO override, so both need
# setting: AUTONOMOUS_TEAM_REPO for the former, LABEL_REPO for the latter.
export AUTONOMOUS_TEAM_REPO="fulcrumaxe/fulcrumaxe"
export LABEL_REPO="fulcrumaxe/fulcrumaxe"

# ── Test 1: label absent -> plain add ────────────────────────────────────────
echo "Test 1: label absent on the PR -> plain add (one mutation, no remove)"
export GH_STUB_LOG="$TEST_DIR/log1"
export GH_STUB_CURRENT_PR=501
export GH_STUB_CURRENT_LABEL="code-review-passed"
bash "$REFRESH_SH" 501 code-review-passed >"$TEST_DIR/out1.txt" 2>&1
RC=$?
assert_eq "test1: exit 0" "$RC" "0"
assert_eq "test1: exactly one mutation" "$(mutation_count)" "1"
assert_eq "test1: the one mutation is an add, not a remove" \
  "$(mutations_of)" "MUTATE addLabelsToLabelable code-review-passed"
FINAL=$(cat "$GH_STUB_STATE_DIR/labels-501.txt" 2>/dev/null || true)
assert_eq "test1: label present after refresh" "$FINAL" "code-review-passed"

# ── Test 2: label already present -> remove then add ────────────────────────
echo "Test 2: label already present -> remove then add (two mutations, in order)"
export GH_STUB_LOG="$TEST_DIR/log2"
export GH_STUB_CURRENT_PR=502
export GH_STUB_CURRENT_LABEL="code-review-passed"
echo "code-review-passed" > "$GH_STUB_STATE_DIR/labels-502.txt"
bash "$REFRESH_SH" 502 code-review-passed >"$TEST_DIR/out2.txt" 2>&1
RC=$?
assert_eq "test2: exit 0" "$RC" "0"
assert_eq "test2: exactly two mutations" "$(mutation_count)" "2"
EXPECTED_SEQ=$'MUTATE removeLabelsFromLabelable code-review-passed\nMUTATE addLabelsToLabelable code-review-passed'
assert_eq "test2: remove happens before add" "$(mutations_of)" "$EXPECTED_SEQ"
FINAL=$(cat "$GH_STUB_STATE_DIR/labels-502.txt" 2>/dev/null || true)
assert_eq "test2: label still present after refresh" "$FINAL" "code-review-passed"
if grep -q "already present on PR #502 — removing then re-adding" "$TEST_DIR/out2.txt"; then
  pass "test2: helper narrates the remove-then-add path"
else
  fail "test2: helper narrates the remove-then-add path" "$(cat "$TEST_DIR/out2.txt")"
fi

# ── Test 3: NACK labels are refused by name, no mutation ────────────────────
echo "Test 3: NACK labels refused — zero mutations, nonzero exit"
PR_COUNTER=600
for nack_label in "${MERGE_GATE_NACK_LABELS[@]}"; do
  PR_COUNTER=$((PR_COUNTER + 1))
  export GH_STUB_LOG="$TEST_DIR/log-nack-$PR_COUNTER"
  export GH_STUB_CURRENT_PR="$PR_COUNTER"
  export GH_STUB_CURRENT_LABEL="$nack_label"
  bash "$REFRESH_SH" "$PR_COUNTER" "$nack_label" >"$TEST_DIR/out-nack-$PR_COUNTER.txt" 2>&1
  RC=$?
  if [[ "$RC" -ne 0 ]]; then
    pass "test3: refuses NACK label '$nack_label' (nonzero exit)"
  else
    fail "test3: refuses NACK label '$nack_label' (nonzero exit)" "got rc=0"
  fi
  assert_eq "test3: zero mutations for '$nack_label'" "$(mutation_count)" "0"
done

# ── Test 4: usage error exits nonzero before any gh call ────────────────────
echo "Test 4: missing args -> usage error, no gh call"
export GH_STUB_LOG="$TEST_DIR/log-usage"
export GH_STUB_CURRENT_PR=""
export GH_STUB_CURRENT_LABEL=""
bash "$REFRESH_SH" 700 >"$TEST_DIR/out-usage.txt" 2>&1
RC=$?
if [[ "$RC" -ne 0 ]]; then
  pass "test4: missing label argument exits nonzero"
else
  fail "test4: missing label argument exits nonzero" "got rc=0"
fi
if [[ ! -f "$GH_STUB_LOG" ]]; then
  pass "test4: no gh call made before the usage error"
else
  fail "test4: no gh call made before the usage error" "$(cat "$GH_STUB_LOG")"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
exit 0
