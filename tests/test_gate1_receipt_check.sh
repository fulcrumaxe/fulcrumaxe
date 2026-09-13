#!/usr/bin/env bash
# tests/test_gate1_receipt_check.sh — Unit tests for
# scripts/lib/gate1-receipt-check.sh (D#2566 PR-2). Fixture-based: no
# network, no live PR, no real gh call — every scenario supplies the PR
# body, head sha, and receipt JSON via env-var overrides (mirrors
# tests/test_two_gate_check.sh's TWO_GATE_PR_BODY_<PR> convention).
#
# Covers Spec acceptance items 13-22 directly; items 23 and 25 are covered
# by tests/test_two_gate_check.sh and tests/test_loop_phased_step5.sh /
# scripts/merge-and-hook.sh's own call sites, not re-tested here.
#
# Usage: bash tests/test_gate1_receipt_check.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/gate1-receipt-check.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

if [ ! -f "$LIB" ]; then
  fail "setup" "scripts/lib/gate1-receipt-check.sh not found at $LIB"
  echo ""
  echo "Results: $PASS passed, $FAIL failed"
  exit 1
fi

REPO="test/repo"
LEDGER_EMPTY="$(mktemp)"
printf '{"note":"t","ledger":{}}' > "$LEDGER_EMPTY"
LEDGER_WITH_SH="$(mktemp)"
printf '{"note":"t","ledger":{"scripts/lib/some-untested.sh":"covered by a manual runbook step, reviewed 2026-09-12"}}' > "$LEDGER_WITH_SH"

# _build_receipt PR_HEAD_SHA TREE_ROOT RUNNER_COPY ROUTING_JSON TESTS_RUN_JSON PARTIAL
_build_receipt() {
  python3 -c "
import json, sys
pr_head_sha, tree_root, runner_copy, routing_json, tests_run_json, partial = sys.argv[1:7]
receipt = {
    'schema': 1,
    'caller': {
        'pr': 1,
        'repo': 'test/repo',
        'pr_head_sha': pr_head_sha,
        'tree_root': tree_root,
        'gate1_runner_copy': runner_copy,
        'gate1_containment': 'NONE (same-uid)',
        'containment_probes': {
            'gh-credential': 'NOT-DENIED', 'state-dir': 'NOT-DENIED',
            'operator-checkout-write': 'NOT-DENIED', 'network': 'NOT-DENIED',
        },
        'containment_verdict': 'UNCONTAINED',
        'env': {
            'AUTONOMOUS_TEAM_REPO': 'test/repo',
            'AUTONOMOUS_TEAM_STATE_DIR': '/tmp/x',
            'seed_files': {'.autonomous-team/config.json': False, '.autonomous-team/project.json': False},
        },
        'written_at': '2026-01-01T00:00:00Z',
        'receipt_path': '/tmp/fake-receipt.json',
    },
    'head_reported': {
        'routing': json.loads(routing_json),
        'tests_run': json.loads(tests_run_json),
        'partial': partial == 'true',
        'measured_tree': {'path': tree_root, 'head_sha': pr_head_sha, 'pr_head_sha': pr_head_sha},
    },
}
print(json.dumps(receipt))
" "$@"
}

GOOD_SHA="1111111111111111111111111111111111111111"
OTHER_SHA="2222222222222222222222222222222222222222"

# A clean, otherwise-passing receipt: one real suite ran, nothing partial,
# runner copy outside the tree.
GOOD_ROUTING='[{"file":"backend/foo.py","suite":"pytest"}]'
GOOD_TESTS_RUN='[{"command":"pytest","exit_code":0,"duration_seconds":5}]'
GOOD_RECEIPT="$(_build_receipt "$GOOD_SHA" "/tmp/pr-head-tree" "/tmp/op/scripts/run-pr-tests.sh" "$GOOD_ROUTING" "$GOOD_TESTS_RUN" false)"

# _run PR BODY HEAD_SHA RECEIPT_JSON LEDGER_PATH — sources the lib fresh in
# a subshell per call (state isolation), prints STATE:<state> then
# REASON:<reason>, returns gate1_receipt_check's own exit code.
_run() {
  local pr="$1" body="$2" head_sha="$3" receipt_json="$4" ledger_path="$5"
  # The fixtures above build one receipt and reuse it across several `_run`
  # calls with different PR numbers; patch caller.pr/caller.repo to match
  # THIS call's PR (security review: caller.pr/caller.repo are now
  # cross-checked against the arguments gate1_receipt_check was actually
  # called with). Left untouched — deliberately, via the exception — when
  # receipt_json isn't valid JSON at all: that's the empty/truncated/
  # wrong-schema malformed-detection cases, which must reach the checker
  # exactly as broken as the test intends.
  if [ -n "$receipt_json" ]; then
    receipt_json="$(python3 -c "
import json, sys
try:
    r = json.loads(sys.argv[1])
    r['caller']['pr'] = int(sys.argv[2])
    r['caller']['repo'] = 'test/repo'
    print(json.dumps(r))
except Exception:
    print(sys.argv[1])
" "$receipt_json" "$pr" 2>/dev/null || printf '%s' "$receipt_json")"
  fi
  (
    # shellcheck disable=SC1090
    source "$LIB"
    export "GATE1_RECEIPT_PR_BODY_${pr}=${body}"
    export "GATE1_RECEIPT_HEAD_SHA_${pr}=${head_sha}"
    export "GATE1_RECEIPT_JSON_${pr}=${receipt_json}"
    export GATE1_RECEIPT_LEDGER_PATH="$ledger_path"
    if gate1_receipt_check "$pr" "test/repo"; then
      echo "STATE:$GATE1_RECEIPT_CHECK_STATE"
      echo "REASON:"
      exit 0
    else
      echo "STATE:$GATE1_RECEIPT_CHECK_STATE"
      echo "REASON:$GATE1_RECEIPT_CHECK_REASON"
      exit 1
    fi
  )
}

# ── item 13: marker present, receipt absent ────────────────────────────────
OUT_13=$(_run 20013 "Gate 1: PASS" "$GOOD_SHA" "" "$LEDGER_EMPTY" 2>&1)
RC_13=$?
if [ "$RC_13" -ne 0 ]; then pass "item13-absent-rejects"; else fail "item13-absent-rejects" "expected non-zero, got 0: $OUT_13"; fi
echo "$OUT_13" | grep -q "STATE:absent" && pass "item13-state-is-absent" || fail "item13-state-is-absent" "$OUT_13"
echo "$OUT_13" | grep -q "gate1-invoke.sh" && pass "item13-reason-quotes-the-command" || fail "item13-reason-quotes-the-command" "$OUT_13"

# ── item 20: malformed receipts never read as N/A or a pass ───────────────
for label_json in "empty:" "truncated:{\"schema\":1,\"caller\":{" "wrong-schema:{\"foo\":1}"; do
  label="${label_json%%:*}"
  json="${label_json#*:}"
  OUT=$(_run 20020 "Gate 1: N/A — nothing to test" "$GOOD_SHA" "$json" "$LEDGER_EMPTY" 2>&1)
  RC=$?
  if [ "$RC" -ne 0 ] && echo "$OUT" | grep -qE "STATE:(malformed|absent)"; then
    pass "item20-$label-rejects-never-na"
  else
    fail "item20-$label-rejects-never-na" "rc=$RC out=$OUT"
  fi
done

# wrong top-level key set on an otherwise well-formed receipt
BAD_KEYS_RECEIPT=$(python3 -c "
import json
r = json.loads('''$GOOD_RECEIPT''')
r['extra_top_level_field'] = 'sneaky'
print(json.dumps(r))
")
OUT_BADKEYS=$(_run 20021 "Gate 1: PASS" "$GOOD_SHA" "$BAD_KEYS_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_BADKEYS=$?
if [ "$RC_BADKEYS" -ne 0 ] && echo "$OUT_BADKEYS" | grep -q "STATE:malformed"; then
  pass "item20-extra-top-level-key-rejected"
else
  fail "item20-extra-top-level-key-rejected" "rc=$RC_BADKEYS out=$OUT_BADKEYS"
fi

# ── item 15: sha binding ───────────────────────────────────────────────────
OUT_15=$(_run 20015 "Gate 1: PASS" "$OTHER_SHA" "$GOOD_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_15=$?
if [ "$RC_15" -ne 0 ] && echo "$OUT_15" | grep -q "STATE:sha_mismatch" && echo "$OUT_15" | grep -q "$GOOD_SHA" && echo "$OUT_15" | grep -q "$OTHER_SHA"; then
  pass "item15-sha-mismatch-names-both"
else
  fail "item15-sha-mismatch-names-both" "rc=$RC_15 out=$OUT_15"
fi

# re-push (regenerate a different sha) without regenerating the receipt —
# same rejection, not staled.
OUT_15B=$(_run 20015 "Gate 1: PASS" "3333333333333333333333333333333333333333" "$GOOD_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_15B=$?
if [ "$RC_15B" -ne 0 ] && echo "$OUT_15B" | grep -q "STATE:sha_mismatch"; then
  pass "item15-repush-invalidates-not-stales"
else
  fail "item15-repush-invalidates-not-stales" "rc=$RC_15B out=$OUT_15B"
fi

# ── item 16: runner copy resolves inside the tree under test ───────────────
BAD_RUNNER_RECEIPT="$(_build_receipt "$GOOD_SHA" "/tmp/pr-head-tree" "/tmp/pr-head-tree/scripts/run-pr-tests.sh" "$GOOD_ROUTING" "$GOOD_TESTS_RUN" false)"
OUT_16=$(_run 20016 "Gate 1: PASS" "$GOOD_SHA" "$BAD_RUNNER_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_16=$?
if [ "$RC_16" -ne 0 ] && echo "$OUT_16" | grep -q "STATE:runner_copy_in_tree"; then
  pass "item16-runner-copy-in-tree-rejected"
else
  fail "item16-runner-copy-in-tree-rejected" "rc=$RC_16 out=$OUT_16"
fi

# ── item 21: partial run ───────────────────────────────────────────────────
PARTIAL_RECEIPT="$(_build_receipt "$GOOD_SHA" "/tmp/pr-head-tree" "/tmp/op/scripts/run-pr-tests.sh" "$GOOD_ROUTING" "$GOOD_TESTS_RUN" true)"
OUT_21=$(_run 20021 "Gate 1: PASS" "$GOOD_SHA" "$PARTIAL_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_21=$?
if [ "$RC_21" -ne 0 ] && echo "$OUT_21" | grep -q "STATE:partial"; then
  pass "item21-partial-rejected"
else
  fail "item21-partial-rejected" "rc=$RC_21 out=$OUT_21"
fi

# ── item 14: declared N/A but routing names a real suite (the D#2571 shape) ──
OUT_14=$(_run 20014 "Gate 1: N/A — the sync runs no test suite of its own" "$GOOD_SHA" "$GOOD_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_14=$?
if [ "$RC_14" -ne 0 ] && echo "$OUT_14" | grep -q "STATE:na_but_routed" && echo "$OUT_14" | grep -q "pytest"; then
  pass "item14-na-but-routed-rejected"
else
  fail "item14-na-but-routed-rejected" "rc=$RC_14 out=$OUT_14"
fi

# ── item 17: only changed file is scripts/lib/two-gate-check.sh — routes
#    null, not ledgered — rejected as unrouted, never N/A ──────────────────
UNROUTED_ROUTING='[{"file":"scripts/lib/two-gate-check.sh","suite":null}]'
EMPTY_TESTS_RUN='[]'
UNROUTED_RECEIPT="$(_build_receipt "$GOOD_SHA" "/tmp/pr-head-tree" "/tmp/op/scripts/run-pr-tests.sh" "$UNROUTED_ROUTING" "$EMPTY_TESTS_RUN" false)"
OUT_17=$(_run 20017 "Gate 1: N/A — no suite for this file" "$GOOD_SHA" "$UNROUTED_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_17=$?
if [ "$RC_17" -ne 0 ]; then pass "item17-unrouted-nonzero"; else fail "item17-unrouted-nonzero" "$OUT_17"; fi
echo "$OUT_17" | grep -q "STATE:unrouted" && pass "item17-state-is-unrouted" || fail "item17-state-is-unrouted" "$OUT_17"
echo "$OUT_17" | grep -q "two-gate-check.sh" && pass "item17-reason-names-the-path" || fail "item17-reason-names-the-path" "$OUT_17"
echo "$OUT_17" | grep -qi "N/A" && fail "item17-reason-excludes-na-token" "$OUT_17" || pass "item17-reason-excludes-na-token"

# Same PR declaring PASS instead of N/A — still rejected as unrouted
# (unrouted is checked before what the body claims).
OUT_17B=$(_run 20017 "Gate 1: PASS" "$GOOD_SHA" "$UNROUTED_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_17B=$?
if [ "$RC_17B" -ne 0 ] && echo "$OUT_17B" | grep -q "STATE:unrouted"; then
  pass "item17-unrouted-rejected-regardless-of-declared-token"
else
  fail "item17-unrouted-rejected-regardless-of-declared-token" "rc=$RC_17B out=$OUT_17B"
fi

# ── coordinator finding (D#2577/D#2515): a .py file routing null is NEVER
#    ledgerable, even if someone adds it to the ledger by mistake ─────────
LEDGER_WITH_PY="$(mktemp)"
printf '{"note":"t","ledger":{"scripts/lib/external_intake_gate.py":"no suite covers this"}}' > "$LEDGER_WITH_PY"
PY_ROUTING='[{"file":"scripts/lib/external_intake_gate.py","suite":null}]'
PY_RECEIPT="$(_build_receipt "$GOOD_SHA" "/tmp/pr-head-tree" "/tmp/op/scripts/run-pr-tests.sh" "$PY_ROUTING" "$EMPTY_TESTS_RUN" false)"
OUT_PY=$(_run 20018 "Gate 1: N/A — python trust module, no suite" "$GOOD_SHA" "$PY_RECEIPT" "$LEDGER_WITH_PY" 2>&1)
RC_PY=$?
if [ "$RC_PY" -ne 0 ] && echo "$OUT_PY" | grep -q "STATE:unrouted" && echo "$OUT_PY" | grep -q "external_intake_gate.py"; then
  pass "py-trust-module-unrouted-even-if-ledgered"
else
  fail "py-trust-module-unrouted-even-if-ledgered" "rc=$RC_PY out=$OUT_PY"
fi

# ── item 18: all changed files route null, all ledgered — accepted as N/A ──
LEDGERED_ROUTING='[{"file":"scripts/lib/some-untested.sh","suite":null}]'
LEDGERED_RECEIPT="$(_build_receipt "$GOOD_SHA" "/tmp/pr-head-tree" "/tmp/op/scripts/run-pr-tests.sh" "$LEDGERED_ROUTING" "$EMPTY_TESTS_RUN" false)"
OUT_18=$(_run 20019 "Gate 1: N/A — no suite, exempted" "$GOOD_SHA" "$LEDGERED_RECEIPT" "$LEDGER_WITH_SH" 2>&1)
RC_18=$?
if [ "$RC_18" -eq 0 ]; then
  pass "item18-ledgered-na-accepted"
else
  fail "item18-ledgered-na-accepted" "rc=$RC_18 out=$OUT_18"
fi

# ── security review (this PR): value-shape validation on head_reported ────
#
# Reproduces the exact regression the reviewer measured against the real
# function (D#1984: watched failing first, on the pre-fix code, before this
# fix landed) — six routing shapes and two partial shapes that each turned
# a correct `unrouted` REJECT into AUTHORIZED, because the old code checked
# `suite is None` / `partial is True` (identity) instead of validating the
# field's TYPE first. Baseline for the routing shapes matches the
# reviewer's own repro exactly: one changed file
# (scripts/lib/two-gate-check.sh), routing suite:null, unledgered, body
# "Gate 1: PASS" — must reject as unrouted/malformed, never authorize.
for bad_routing in \
  '{}' \
  '"scripts/lib/gate1-receipt-check.sh"' \
  '[["file","scripts/lib/gate1-receipt-check.sh"]]' \
  '[null]' \
  '[{"file":"scripts/lib/gate1-receipt-check.sh","suite":""}]' \
  '[{"file":"scripts/lib/gate1-receipt-check.sh","suite":false}]'
do
  MUTATED=$(python3 -c "
import json
r = json.loads('''$UNROUTED_RECEIPT''')
r['head_reported']['routing'] = json.loads('''$bad_routing''')
print(json.dumps(r))
")
  OUT_M=$(_run 20025 "Gate 1: PASS" "$GOOD_SHA" "$MUTATED" "$LEDGER_EMPTY" 2>&1)
  RC_M=$?
  if [ "$RC_M" -ne 0 ]; then
    pass "security-routing-shape-rejected (routing=$bad_routing)"
  else
    fail "security-routing-shape-rejected (routing=$bad_routing)" "AUTHORIZED — this exact shape used to flip the D#2571 example into a pass: $OUT_M"
  fi
done

# The two partial shapes: `"true"` (string) and `1` (int), on an otherwise-
# passing receipt. Old code: `partial is True` — neither string "true" nor
# int 1 is the literal True, so both used to skip item 21's rejection
# entirely and authorize a killed run.
for bad_partial in '"true"' '1'; do
  MUTATED=$(python3 -c "
import json
r = json.loads('''$GOOD_RECEIPT''')
r['head_reported']['partial'] = json.loads('''$bad_partial''')
print(json.dumps(r))
")
  OUT_M=$(_run 20026 "Gate 1: PASS" "$GOOD_SHA" "$MUTATED" "$LEDGER_EMPTY" 2>&1)
  RC_M=$?
  if [ "$RC_M" -ne 0 ]; then
    pass "security-partial-shape-rejected (partial=$bad_partial)"
  else
    fail "security-partial-shape-rejected (partial=$bad_partial)" "AUTHORIZED — item 21's rejection was skipped entirely: $OUT_M"
  fi
done

# ── item 22: authorization reads caller only ───────────────────────────────
# Start from an otherwise-FAILING receipt (sha mismatch) and mutate every
# head_reported field toward "looks like a pass" one at a time — the
# verdict must never become "ok".
for mutation in \
  'r["head_reported"]["tests_run"] = [{"command":"x","exit_code":0,"duration_seconds":0}]' \
  'r["head_reported"]["routing"] = [{"file":"x","suite":None}]' \
  'r["head_reported"]["partial"] = False'
do
  MUTATED=$(python3 -c "
import json
r = json.loads('''$GOOD_RECEIPT''')
r['caller']['pr_head_sha'] = '$OTHER_SHA'  # force an otherwise-failing receipt
$mutation
print(json.dumps(r))
")
  OUT_M=$(_run 20022 "Gate 1: PASS" "$GOOD_SHA" "$MUTATED" "$LEDGER_EMPTY" 2>&1)
  RC_M=$?
  if [ "$RC_M" -ne 0 ]; then
    pass "item22-head-mutation-never-flips-to-pass ($mutation)"
  else
    fail "item22-head-mutation-never-flips-to-pass ($mutation)" "verdict became a pass after mutating head_reported: $OUT_M"
  fi
done

# Conversely: start from the GOOD (passing) receipt and mutate one caller
# field at a time — the verdict must change (never stay "ok").
for mutation in \
  'r["caller"]["pr_head_sha"] = "'"$OTHER_SHA"'"' \
  'r["caller"]["gate1_runner_copy"] = r["caller"]["tree_root"] + "/run-pr-tests.sh"'
do
  MUTATED=$(python3 -c "
import json
r = json.loads('''$GOOD_RECEIPT''')
$mutation
print(json.dumps(r))
")
  OUT_M=$(_run 20023 "Gate 1: PASS" "$GOOD_SHA" "$MUTATED" "$LEDGER_EMPTY" 2>&1)
  RC_M=$?
  if [ "$RC_M" -ne 0 ]; then
    pass "item22-caller-mutation-changes-verdict ($mutation)"
  else
    fail "item22-caller-mutation-changes-verdict ($mutation)" "verdict stayed ok after mutating caller: $OUT_M"
  fi
done

# Sanity: the unmutated GOOD_RECEIPT against its own sha really does pass —
# otherwise the mutation tests above are vacuous.
OUT_SANITY=$(_run 20024 "Gate 1: PASS" "$GOOD_SHA" "$GOOD_RECEIPT" "$LEDGER_EMPTY" 2>&1)
RC_SANITY=$?
if [ "$RC_SANITY" -eq 0 ]; then
  pass "sanity-good-receipt-passes"
else
  fail "sanity-good-receipt-passes" "rc=$RC_SANITY out=$OUT_SANITY"
fi

# ── Teardown ─────────────────────────────────────────────────────────────
rm -f "$LEDGER_EMPTY" "$LEDGER_WITH_SH" "$LEDGER_WITH_PY"

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
