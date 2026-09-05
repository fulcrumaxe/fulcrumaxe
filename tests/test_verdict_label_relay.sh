#!/usr/bin/env bash
# tests/test_verdict_label_relay.sh — hermetic tests for D#2031's verdict → label
# relay and the worktree-safe gh-label.sh helpers.
#
# All tests stub `gh` on PATH — no real network calls, no real repo mutation.
#
# Covers:
#   1. apply_label (worktree/GraphQL path) succeeds for a real label and the
#      applied label is visible on an independent read-back (criterion 1).
#   2. apply_label reports a REAL failure for a nonexistent label — nonzero
#      exit, not a swallowed success (criterion 2).
#   3. verdict-label.sh maps each (role, verdict) pair to the right label
#      (criterion 5).
#   4. An unmapped (role, verdict) pair applies nothing and exits 0, making
#      zero label-mutating calls (criterion 5).
#   5. Running post-agent-hook.sh's step machinery twice with the same
#      HOOK_EVENT_ID applies the label once — the second run makes zero
#      label-mutating calls (criterion 6).
#
# Usage:
#   bash tests/test_verdict_label_relay.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GH_LABEL_SH="$REPO_ROOT/scripts/lib/gh-label.sh"
RELAY_SH="$REPO_ROOT/scripts/hooks/post-agent.d/verdict-label.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — ${2:-}"; FAIL=$((FAIL + 1)); ERRORS+=("$1: ${2:-}"); }

assert_eq() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then pass "$label"; else fail "$label" "got '$actual', expected '$expected'"; fi
}

# ── Stub `gh` builder ──────────────────────────────────────────────────────────
# The stub logs every invocation, answers graphql queries for PR node id and
# label id lookups, and records addLabelsToLabelable/removeLabelsFromLabelable
# mutation calls to a separate mutations log so tests can count them precisely.
# A "definitely-not-a-label" label name resolves to a null id, simulating a
# real nonexistent label.
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
  python3 -c "
import json
names = [l.strip() for l in open('$state_file') if l.strip()]
print(json.dumps({'labels':[{'name': n} for n in names]}))
"
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
    if [[ "$LBL" == "definitely-not-a-label" ]]; then
      echo '{"data":{"repository":{"label":null}}}'
    else
      echo "{\"data\":{\"repository\":{\"label\":{\"id\":\"LBL_${LBL}\"}}}}"
    fi
    exit 0
  fi

  if [[ "$QUERY" == *"addLabelsToLabelable"* ]]; then
    pr="${GH_STUB_CURRENT_PR:-unknown}"
    label=$(echo "$QUERY" | grep -oE 'LBL_[^"]+' | head -1 | sed 's/^LBL_//')
    [[ -z "$label" ]] && label="${GH_STUB_CURRENT_LABEL:-unknown}"
    echo "MUTATE addLabelsToLabelable $label" >> "$MUT_LOG"
    echo "$label" >> "$STATE_DIR/labels-$pr.txt"
    echo '{"data":{"addLabelsToLabelable":{"clientMutationId":null}}}'
    exit 0
  fi

  if [[ "$QUERY" == *"removeLabelsFromLabelable"* ]]; then
    pr="${GH_STUB_CURRENT_PR:-unknown}"
    label=$(echo "$QUERY" | grep -oE 'LBL_[^"]+' | head -1 | sed 's/^LBL_//')
    [[ -z "$label" ]] && label="${GH_STUB_CURRENT_LABEL:-unknown}"
    echo "MUTATE removeLabelsFromLabelable $label" >> "$MUT_LOG"
    if [[ -n "${GH_STUB_FAIL_LABEL:-}" && "$label" == "${GH_STUB_FAIL_LABEL}" ]]; then
      echo "simulated remove failure for $label" >&2
      echo '{"errors":[{"message":"simulated failure"}]}'
      exit 1
    fi
    if [[ -f "$STATE_DIR/labels-$pr.txt" ]]; then
      grep -v -x -F "$label" "$STATE_DIR/labels-$pr.txt" > "$STATE_DIR/labels-$pr.txt.tmp" 2>/dev/null || true
      mv "$STATE_DIR/labels-$pr.txt.tmp" "$STATE_DIR/labels-$pr.txt" 2>/dev/null || true
    fi
    exit 0
  fi

  echo '{"data":{}}'
  exit 0
fi

exit 0
STUB
  chmod +x "$bindir/gh"
}

mutation_count() {
  local mut_log="${GH_STUB_LOG}.mutations"
  [[ -f "$mut_log" ]] && wc -l < "$mut_log" | tr -d ' ' || echo 0
}

# ── Common test env ────────────────────────────────────────────────────────────
TEST_DIR=$(mktemp -d)
STUB_BIN="$TEST_DIR/bin"
build_gh_stub "$STUB_BIN"
export PATH="$STUB_BIN:$PATH"
export GH_LABEL_FORCE_WORKTREE=1
export GH_STUB_STATE_DIR="$TEST_DIR/state"
mkdir -p "$GH_STUB_STATE_DIR"

# ── Test 1: apply_label succeeds for a real label, read-back agrees ──────────
echo "Test 1: apply_label (worktree/GraphQL path) — real label, independent read-back"
export GH_STUB_LOG="$TEST_DIR/log1"
export GH_STUB_CURRENT_PR=101
export GH_STUB_CURRENT_LABEL="code-review-passed"
(
  source "$GH_LABEL_SH"
  apply_label 101 code-review-passed
)
RC=$?
assert_eq "test1: apply_label exit 0" "$RC" "0"
READBACK=$(gh pr view 101 --json labels 2>/dev/null)
assert_eq "test1: read-back shows the label" \
  "$(echo "$READBACK" | python3 -c "import json,sys; print('code-review-passed' in [l['name'] for l in json.load(sys.stdin)['labels']])")" \
  "True"

# ── Test 2: apply_label reports a REAL failure for a nonexistent label ───────
echo "Test 2: apply_label — nonexistent label returns nonzero, not a swallowed success"
export GH_STUB_LOG="$TEST_DIR/log2"
export GH_STUB_CURRENT_PR=102
export GH_STUB_CURRENT_LABEL="definitely-not-a-label"
(
  source "$GH_LABEL_SH"
  apply_label 102 definitely-not-a-label
)
RC=$?
if [[ "$RC" -ne 0 ]]; then
  pass "test2: apply_label returns nonzero for a nonexistent label"
else
  fail "test2: apply_label returns nonzero for a nonexistent label" "got rc=0 (swallowed failure)"
fi
READBACK=$(gh pr view 102 --json labels 2>/dev/null)
ABSENT=$(echo "$READBACK" | python3 -c "import json,sys; print('definitely-not-a-label' not in [l['name'] for l in json.load(sys.stdin)['labels']])")
assert_eq "test2: exit status and observed state agree (label absent)" "$ABSENT" "True"

# ── Test 3: verdict-label.sh maps each (role, verdict) pair correctly ────────
# expected_mutations = 1 (apply) + the (role,verdict)'s exclusion-group size
# (D#2066): the relay now clears its exclusion group unconditionally before
# applying, even when the excluded label wasn't present on the PR.
echo "Test 3: verdict → label mapping"
declare -a MAPPINGS=(
  "code-reviewer:pass:code-review-passed:2"
  "code-reviewer:needs-fix:code-review-needs-fix:2"
  "security-reviewer:pass:security-review-passed:4"
  "security-reviewer:skip:security-review-passed:4"
  "acceptance-tester:pass:acceptance-passed:2"
  "acceptance-tester:fail:acceptance-failed:2"
)
PR_COUNTER=200
for m in "${MAPPINGS[@]}"; do
  IFS=':' read -r role verdict expected_label expected_mutations <<< "$m"
  PR_COUNTER=$((PR_COUNTER + 1))
  export GH_STUB_LOG="$TEST_DIR/log-map-$PR_COUNTER"
  export GH_STUB_CURRENT_PR="$PR_COUNTER"
  export GH_STUB_CURRENT_LABEL="$expected_label"
  bash "$RELAY_SH" --role "$role" --verdict "$verdict" --pr "$PR_COUNTER" >/dev/null 2>&1
  RC=$?
  MC=$(mutation_count)
  if [[ "$RC" -eq 0 && "$MC" -eq "$expected_mutations" ]]; then
    pass "test3: ${role}:${verdict} -> ${expected_label}"
  else
    fail "test3: ${role}:${verdict} -> ${expected_label}" "rc=$RC mutations=$MC (expected $expected_mutations)"
  fi
  READBACK=$(gh pr view "$PR_COUNTER" --json labels 2>/dev/null)
  HAS=$(echo "$READBACK" | python3 -c "import json,sys; print('$expected_label' in [l['name'] for l in json.load(sys.stdin)['labels']])")
  assert_eq "test3: ${role}:${verdict} label present on read-back" "$HAS" "True"
done

# ── Test 4: unmapped pair applies nothing, zero label-mutating calls ─────────
echo "Test 4: unmapped (role, verdict) pair is a no-op"
export GH_STUB_LOG="$TEST_DIR/log-unmapped"
export GH_STUB_CURRENT_PR=300
export GH_STUB_CURRENT_LABEL=""
bash "$RELAY_SH" --role code-reviewer --verdict skip --pr 300 >/dev/null 2>&1
RC=$?
MC=$(mutation_count)
assert_eq "test4: unmapped pair exits 0" "$RC" "0"
assert_eq "test4: unmapped pair makes zero label-mutating calls" "$MC" "0"

# ── Test 5: idempotent across two post-agent-hook.sh runs, same HOOK_EVENT_ID ─
echo "Test 5: relay applies once — second run with same event id makes zero calls"
export GH_STUB_LOG="$TEST_DIR/log-idem"
export GH_STUB_CURRENT_PR=400
export GH_STUB_CURRENT_LABEL="code-review-passed"

run_relay_via_hook_event() {
  # Exact copy of the production wiring: hook_event_init with the full step
  # list (verdict_label included, matching post-agent-hook.sh line 144),
  # every OTHER step pre-marked done, then the verdict_label block exactly as
  # registered in post-agent-hook.sh.
  local event_id="$1"
  (
    source "$REPO_ROOT/scripts/lib/hook-event.sh"
    export HOOK_ROLE="code-reviewer" HOOK_DISCUSSION="2031" HOOK_PR="400" HOOK_VERDICT="pass" HOOK_CALLER="post-agent-hook"
    export HOOK_EVENT_DIR="$TEST_DIR/hook-events"
    hook_event_init "post-agent-hook" \
      "agent_feed,team_substrate,budget,circuit_breaker,kpi,audit,role_verdict_metric,complete_run,verdict_label,pr_artifacts,memory,training_mine,cost_summary,post_agent_cleanup,worktree_registry,self_observe_check,scope_drift_check,anomaly_check,reap_worktrees,team_log" \
      --event-id "$event_id" >/dev/null

    for s in agent_feed team_substrate budget circuit_breaker kpi audit role_verdict_metric complete_run \
             pr_artifacts memory training_mine cost_summary post_agent_cleanup worktree_registry \
             self_observe_check scope_drift_check anomaly_check reap_worktrees team_log; do
      hook_event_has_step "$s" || hook_event_mark_step "$s"
    done

    ROLE="code-reviewer"; VERDICT="pass"; PR="400"; REPO_ROOT="$REPO_ROOT"
    if ! hook_event_has_step "verdict_label"; then
      [ -f "$REPO_ROOT/scripts/hooks/post-agent.d/verdict-label.sh" ] && { export REPO_ROOT PR ROLE VERDICT; source "$REPO_ROOT/scripts/hooks/post-agent.d/verdict-label.sh" 2>/dev/null || true; }
      hook_event_mark_step "verdict_label"
    fi
    hook_event_finish
  )
}

EVENT_ID="test-verdict-label-$$"
run_relay_via_hook_event "$EVENT_ID" >/dev/null 2>&1
MC_AFTER_1=$(mutation_count)
run_relay_via_hook_event "$EVENT_ID" >/dev/null 2>&1
MC_AFTER_2=$(mutation_count)

# D#2066: code-reviewer:pass now clears its exclusion group (code-review-needs-fix)
# before applying, so a single relay firing makes 2 mutating calls, not 1.
assert_eq "test5: first run applies the label once (clear + apply)" "$MC_AFTER_1" "2"
assert_eq "test5: second run (same event id) makes zero additional calls" "$MC_AFTER_2" "2"

# ── D#2066: exclusion-group clearing (AC-1 .. AC-9) ──────────────────────────
# Each helper below drives the real relay script and reads back state through
# the same `gh pr view --json labels` path production code uses — never the
# exit code alone (scripts/lib/gh-label.sh:12-14, D#2031 criterion).

seed_labels() {
  # seed_labels <pr> [label...] — pre-populate the stub's label state for a
  # PR, simulating labels already present before the relay runs. Zero labels
  # is valid (an explicit empty PR).
  local pr="$1"; shift
  mkdir -p "$GH_STUB_STATE_DIR"
  : > "$GH_STUB_STATE_DIR/labels-$pr.txt"
  if [[ $# -gt 0 ]]; then
    printf '%s\n' "$@" >> "$GH_STUB_STATE_DIR/labels-$pr.txt"
  fi
}

current_labels() {
  # current_labels <pr> — space-separated label names, from an independent
  # gh pr view read (not from the mutation log).
  local pr="$1"
  gh pr view "$pr" --json labels 2>/dev/null \
    | python3 -c "import json,sys; print(' '.join(sorted(l['name'] for l in json.load(sys.stdin)['labels'])))"
}

has_label() {
  local pr="$1" label="$2"
  case " $(current_labels "$pr") " in *" $label "*) return 0 ;; *) return 1 ;; esac
}

echo "Test 6 (AC-1): code-reviewer:pass clears a standing code-review-needs-fix"
export GH_STUB_LOG="$TEST_DIR/log-ac1"
unset GH_STUB_FAIL_LABEL
export GH_STUB_CURRENT_PR=2001
seed_labels 2001 "code-review-needs-fix"
bash "$RELAY_SH" --role code-reviewer --verdict pass --pr 2001 >/dev/null 2>&1
if has_label 2001 "code-review-passed" && ! has_label 2001 "code-review-needs-fix"; then
  pass "AC-1: pass present, needs-fix cleared"
else
  fail "AC-1: pass present, needs-fix cleared" "labels: $(current_labels 2001)"
fi

echo "Test 7 (AC-2): code-reviewer:needs-fix clears a standing code-review-passed"
export GH_STUB_LOG="$TEST_DIR/log-ac2"
export GH_STUB_CURRENT_PR=2002
seed_labels 2002 "code-review-passed"
bash "$RELAY_SH" --role code-reviewer --verdict needs-fix --pr 2002 >/dev/null 2>&1
if has_label 2002 "code-review-needs-fix" && ! has_label 2002 "code-review-passed"; then
  pass "AC-2: needs-fix present, passed cleared"
else
  fail "AC-2: needs-fix present, passed cleared" "labels: $(current_labels 2002)"
fi

echo "Test 8 (AC-3): two-writer ordering — reviewer's mid-run pass, then relay's needs-fix (PR #2061 incident)"
export GH_STUB_LOG="$TEST_DIR/log-ac3"
export GH_STUB_CURRENT_PR=2061
seed_labels 2061
# Writer 1: the reviewer agent's own mid-run apply_label call (not the relay).
(
  source "$GH_LABEL_SH"
  apply_label 2061 code-review-passed
) >/dev/null 2>&1
# Writer 2: the relay, firing post-completion with the real verdict.
bash "$RELAY_SH" --role code-reviewer --verdict needs-fix --pr 2061 >/dev/null 2>&1
if has_label 2061 "code-review-needs-fix" && ! has_label 2061 "code-review-passed"; then
  pass "AC-3: relay's post-completion write wins — mid-run pass label is gone"
else
  fail "AC-3: relay's post-completion write wins — mid-run pass label is gone" "labels: $(current_labels 2061)"
fi

echo "Test 9 (AC-4): acceptance pass/fail clear each other, both directions"
export GH_STUB_LOG="$TEST_DIR/log-ac4a"
export GH_STUB_CURRENT_PR=2004
seed_labels 2004 "acceptance-passed"
bash "$RELAY_SH" --role acceptance-tester --verdict fail --pr 2004 >/dev/null 2>&1
if has_label 2004 "acceptance-failed" && ! has_label 2004 "acceptance-passed"; then
  pass "AC-4a: fail clears passed"
else
  fail "AC-4a: fail clears passed" "labels: $(current_labels 2004)"
fi
export GH_STUB_LOG="$TEST_DIR/log-ac4b"
export GH_STUB_CURRENT_PR=2005
seed_labels 2005 "acceptance-failed"
bash "$RELAY_SH" --role acceptance-tester --verdict pass --pr 2005 >/dev/null 2>&1
if has_label 2005 "acceptance-passed" && ! has_label 2005 "acceptance-failed"; then
  pass "AC-4b: pass clears failed"
else
  fail "AC-4b: pass clears failed" "labels: $(current_labels 2005)"
fi

echo "Test 10 (AC-5): security-reviewer:pass clears all three NACK synonyms"
export GH_STUB_LOG="$TEST_DIR/log-ac5"
export GH_STUB_CURRENT_PR=2006
seed_labels 2006 "security-needs-fix" "security-review-needs-fix" "security-issue"
bash "$RELAY_SH" --role security-reviewer --verdict pass --pr 2006 >/dev/null 2>&1
if has_label 2006 "security-review-passed" \
  && ! has_label 2006 "security-needs-fix" \
  && ! has_label 2006 "security-review-needs-fix" \
  && ! has_label 2006 "security-issue"; then
  pass "AC-5: all three security NACK synonyms cleared"
else
  fail "AC-5: all three security NACK synonyms cleared" "labels: $(current_labels 2006)"
fi

echo "Test 11 (AC-6): labels outside the acting role's exclusion group are untouched"
export GH_STUB_LOG="$TEST_DIR/log-ac6"
export GH_STUB_CURRENT_PR=2007
seed_labels 2007 "security-review-passed" "needs_security_review"
bash "$RELAY_SH" --role code-reviewer --verdict pass --pr 2007 >/dev/null 2>&1
if has_label 2007 "code-review-passed" \
  && has_label 2007 "security-review-passed" \
  && has_label 2007 "needs_security_review"; then
  pass "AC-6: unrelated labels survive a code-review transition"
else
  fail "AC-6: unrelated labels survive a code-review transition" "labels: $(current_labels 2007)"
fi

echo "Test 12 (AC-7): a remove_label failure does not block the apply_label that follows"
export GH_STUB_LOG="$TEST_DIR/log-ac7"
export GH_STUB_CURRENT_PR=2008
export GH_STUB_FAIL_LABEL="code-review-needs-fix"
seed_labels 2008 "code-review-needs-fix"
STDERR_OUT=$(bash "$RELAY_SH" --role code-reviewer --verdict pass --pr 2008 2>&1 >/dev/null)
unset GH_STUB_FAIL_LABEL
if has_label 2008 "code-review-passed" && echo "$STDERR_OUT" | grep -qi "WARN.*code-review-needs-fix"; then
  pass "AC-7: apply proceeds despite remove failure, warning is on stderr"
else
  fail "AC-7: apply proceeds despite remove failure, warning is on stderr" "labels: $(current_labels 2008) stderr: $STDERR_OUT"
fi

echo "Test 13 (AC-8): VERDICT_LABEL_RELAY=0 disables clearing too"
export GH_STUB_LOG="$TEST_DIR/log-ac8"
export GH_STUB_CURRENT_PR=2009
seed_labels 2009 "code-review-needs-fix"
VERDICT_LABEL_RELAY=0 bash "$RELAY_SH" --role code-reviewer --verdict pass --pr 2009 >/dev/null 2>&1
MC_AC8=$(mutation_count)
if has_label 2009 "code-review-needs-fix" && ! has_label 2009 "code-review-passed"; then
  pass "AC-8: disabled relay makes no mutation, needs-fix survives"
else
  fail "AC-8: disabled relay makes no mutation, needs-fix survives" "labels: $(current_labels 2009)"
fi

echo "Test 14 (AC-9): security-reviewer:needs-fix stays unmapped — no-op, exit 0, zero mutations"
export GH_STUB_LOG="$TEST_DIR/log-ac9"
export GH_STUB_CURRENT_PR=2010
seed_labels 2010 "security-review-passed"
bash "$RELAY_SH" --role security-reviewer --verdict needs-fix --pr 2010 >/dev/null 2>&1
RC_AC9=$?
MC_AC9=$(mutation_count)
if [[ "$RC_AC9" -eq 0 ]] && [[ "$MC_AC9" -eq 0 ]] && has_label 2010 "security-review-passed"; then
  pass "AC-9: unmapped security-reviewer:needs-fix is a true no-op"
else
  fail "AC-9: unmapped security-reviewer:needs-fix is a true no-op" "rc=$RC_AC9 mutations=$MC_AC9 labels: $(current_labels 2010)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
rm -rf "$TEST_DIR"
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
