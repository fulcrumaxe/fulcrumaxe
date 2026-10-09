#!/usr/bin/env bash
# tests/test_merge_and_hook_product_pr.sh — the product-PR refusal in
# scripts/merge-and-hook.sh (D#6 S2-OWN), through the REAL wrapper.
#
# A change to a gate script once broke every merge, so this test does not
# source or re-implement any of the wrapper. It copies the real script and its
# real libraries into a throwaway git repo, puts a stub `gh` first on PATH, and
# runs the wrapper there under `set -euo pipefail` (the wrapper sets it itself;
# this file sets it too, so a stray unset variable or failed pipe in the
# harness fails loudly rather than reading as a pass).
#
# Safety, in order:
#   - Everything runs in a fresh `mktemp -d` repo. Nothing is run against any
#     real repository, and no --force flag is passed anywhere.
#   - The stub is proven to be the stub before any case runs: `command -v gh`
#     must resolve into the throwaway dir and the stub must answer a sentinel.
#     If it does not, the test aborts before invoking the wrapper.
#   - The stub logs every call. "No merge call" is asserted against that log.
#
# Run: bash tests/test_merge_and_hook_product_pr.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/fxs2own-merge-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# ── Throwaway repo holding a copy of the real wrapper and its libraries ───────
SANDBOX="$WORK/repo"
mkdir -p "$SANDBOX/scripts/lib" "$SANDBOX/bin" "$SANDBOX/logs" "$SANDBOX/state"
git init -q "$SANDBOX"

cp "$REPO_ROOT/scripts/merge-and-hook.sh"            "$SANDBOX/scripts/merge-and-hook.sh"
cp "$REPO_ROOT/scripts/check-pr-dashboard-touched.sh" "$SANDBOX/scripts/check-pr-dashboard-touched.sh"
for lib in two-gate-check repo-resolve pr-plane resolve-pr-discussion ci-status-check \
           pr-dependents merge-gate-labels gate1-receipt-check gate1-receipt; do
  cp "$REPO_ROOT/scripts/lib/$lib.sh" "$SANDBOX/scripts/lib/$lib.sh"
done
# The post-merge hook is bookkeeping after the merge and not under test.
cat > "$SANDBOX/scripts/post-merge-hook.sh" <<'EOF'
#!/usr/bin/env bash
echo "stub post-merge-hook ran"
exit 0
EOF
chmod +x "$SANDBOX/scripts/post-merge-hook.sh"

# ── Stub gh. Logs every call; HEAD_REF and HEAD_REF_RC steer the new gate. ────
CALL_LOG="$WORK/gh-calls.log"
cat > "$SANDBOX/bin/gh" <<'EOF'
#!/usr/bin/env bash
ARGS="$*"
if [[ "${1:-}" == "--fxs2own-stub-identity" ]]; then
  echo "fxs2own-gh-stub"
  exit 0
fi
echo "$ARGS" >> "${STUB_CALL_LOG:?}"
if [[ "$ARGS" == *"--json headRefName"* ]]; then
  if [[ "${STUB_HEAD_REF_RC:-0}" != "0" ]]; then exit "$STUB_HEAD_REF_RC"; fi
  echo "${STUB_HEAD_REF:?}"
  exit 0
fi
if [[ "$ARGS" == *"--json body"* ]]; then echo "Closes D#4200"; exit 0; fi
if [[ "$ARGS" == *"--json labels"* ]]; then echo "code-review-passed"; exit 0; fi
if [[ "$ARGS" == *"graphql"* && "$ARGS" == *"discussion(number:"* ]]; then echo "D_kwDOFake"; exit 0; fi
if [[ "$ARGS" == *"pr diff"* ]]; then exit 0; fi
if [[ "$ARGS" == *"--json files,changedFiles"* ]]; then echo '{"files":[],"changedFiles":0}'; exit 0; fi
if [[ "$ARGS" == *"--json mergeable"* ]]; then echo "MERGEABLE|CLEAN"; exit 0; fi
if [[ "$ARGS" == *"--json headRefOid"* ]]; then echo "deadbeefcafe0000"; exit 0; fi
if [[ "$ARGS" == *"--json baseRefName"* ]]; then echo "main"; exit 0; fi
if [[ "$ARGS" == *"check-runs"* ]]; then
  printf '%s' '[{"name":"tui","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"dashboard","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"ts-backend","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"backend (import-smoke)","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"preflight (always-on gates)","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"publish denylist","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"PR link policy","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"PR mutation evidence","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""},{"name":"open-source export audit","status":"completed","conclusion":"success","app":{"slug":"github-actions"},"html_url":""}]'
  exit 0
fi
if [[ "$ARGS" == *"issues/"*"/timeline"* ]]; then exit 0; fi
if [[ "$ARGS" == *"pr merge"* ]]; then echo "MERGE-CALLED: $ARGS"; exit 0; fi
# Any other call (pulls/<n> existence probe, etc.) succeeds with an empty body.
echo "{}"
exit 0
EOF
chmod +x "$SANDBOX/bin/gh"

# ── Prove the stub is the stub, before the wrapper ever runs ──────────────────
# python3 shim: only the external-intake provenance check is answered ("not
# required"); everything else goes to the real interpreter, by absolute path so
# the shim cannot recurse into itself.
REAL_PYTHON3="$(command -v python3)"
cat > "$SANDBOX/bin/python3" <<PYEOF
#!/usr/bin/env bash
if [[ "\$1" == *external_intake_gate.py* && "\$2" == "security-required" ]]; then
  echo "false"
  exit 1
fi
exec "$REAL_PYTHON3" "\$@"
PYEOF
chmod +x "$SANDBOX/bin/python3"

export PATH="$SANDBOX/bin:$PATH"
if [[ "$(command -v gh)" != "$SANDBOX/bin/gh" ]]; then
  echo "ABORT: gh on PATH is '$(command -v gh)', not the throwaway stub" >&2
  exit 2
fi
if [[ "$(gh --fxs2own-stub-identity)" != "fxs2own-gh-stub" ]]; then
  echo "ABORT: gh did not answer the stub sentinel" >&2
  exit 2
fi
pass "gh on PATH is the throwaway stub, and it answers the sentinel"

# The two-gate receipt the wrapper wants is test-mode plumbing; same override
# convention as tests/test_merge_and_hook.sh.
RECEIPT_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
make_receipt() {
  python3 -c "
import json, sys
pr, sha, repo = sys.argv[1:4]
print(json.dumps({
    'schema': 1,
    'caller': {
        'pr': int(pr), 'repo': repo, 'pr_head_sha': sha,
        'tree_root': '/tmp/g1-fixture-tree', 'gate1_runner_copy': '/tmp/g1-fixture-op/run-pr-tests.sh',
        'gate1_containment': 'NONE (same-uid)',
        'containment_probes': {'gh-credential': 'NOT-DENIED', 'state-dir': 'NOT-DENIED', 'operator-checkout-write': 'NOT-DENIED', 'network': 'NOT-DENIED'},
        'containment_verdict': 'UNCONTAINED',
        'env': {'AUTONOMOUS_TEAM_REPO': repo, 'AUTONOMOUS_TEAM_STATE_DIR': '/tmp/x', 'seed_files': {'.autonomous-team/config.json': False, '.autonomous-team/project.json': False}},
        'written_at': '2026-01-01T00:00:00Z', 'receipt_path': '/tmp/g1-fixture-receipt.json',
    },
    'head_reported': {'routing': [], 'tests_run': [], 'partial': False, 'measured_tree': {}},
}))
" "$1" "$RECEIPT_SHA" "$2"
}

DISC_REPO="fxs2own-owner/disc-plane"
CODE_REPO_OTHER="fxs2own-owner/code-plane"

# run_case <name> <head_ref> <head_ref_rc> <plane_name|""> <plane_repo|""> -> sets RC, OUT
run_case() {
  local name="$1" head_ref="$2" head_rc="$3" plane_name="$4" plane_repo="$5"
  : > "$CALL_LOG"
  local -a plane_env=()
  if [[ -n "$plane_name" ]]; then
    plane_env=("PR_PLANE_RESOLVE_OVERRIDE_NAME=$plane_name" "PR_PLANE_RESOLVE_OVERRIDE_REPO=$plane_repo")
  fi
  RC=0
  OUT="$(env -C "$SANDBOX" \
    PATH="$SANDBOX/bin:$PATH" \
    STUB_CALL_LOG="$CALL_LOG" STUB_HEAD_REF="$head_ref" STUB_HEAD_REF_RC="$head_rc" \
    AUTONOMOUS_TEAM_REPO="$DISC_REPO" \
    AUTONOMOUS_TEAM_LOG_FILE="$SANDBOX/logs/team.log" \
    AUTONOMOUS_TEAM_STATE_DIR="$SANDBOX/state" \
    MERGE_AND_HOOK_LOG_DIR="$SANDBOX/logs" \
    CI_STATUS_TEST_MODE=1 CI_KILL_SWITCH_OVERRIDE=HTTP_404 \
    CI_STATUS_TEST_AUDIT_FILE="$SANDBOX/state/audit.jsonl" \
    CI_MERGE_PROBE_ATTEMPTS=1 CI_MERGE_PROBE_INTERVAL=0 \
    PR_DEPENDENTS_TEST_MODE=1 PR_DEP_HEADREF_77="$head_ref" PR_DEP_OPEN_LIST_JSON='[]' \
    TWO_GATE_PR_BODY_77='Gate 1: PASS\nGate 2: PASS' \
    GATE1_RECEIPT_HEAD_SHA_77="$RECEIPT_SHA" \
    GATE1_RECEIPT_JSON_77="$(make_receipt 77 "${plane_repo:-$DISC_REPO}")" \
    ${plane_env[@]+"${plane_env[@]}"} \
    bash "$SANDBOX/scripts/merge-and-hook.sh" --pr 77 2>&1)" || RC=$?
  echo "Case: $name (rc=$RC)"
}

merge_calls() { grep -c 'pr merge' "$CALL_LOG" || true; }

# 1. discussion plane, fx/ head: refused, nothing merged.
run_case "discussion plane, head fx/x" "fx/x" 0 discussion "$DISC_REPO"
[[ "$RC" -ne 0 ]] && pass "fx/x on the discussion plane exits non-zero" || fail "fx/x exited 0"
echo "$OUT" | grep -qF "product PR: the owner merges it by hand" \
  && pass "refusal says: product PR: the owner merges it by hand" || fail "refusal message missing"
[[ "$(merge_calls)" -eq 0 ]] && pass "no merge call was made" || fail "a merge call was made"
echo "$OUT" | grep -qF "stub post-merge-hook ran" && fail "post-merge hook ran" || pass "post-merge hook did not run"

# 2. discussion plane, ordinary head: proceeds all the way to the merge.
run_case "discussion plane, normal head" "add-thing" 0 discussion "$DISC_REPO"
[[ "$RC" -eq 0 ]] && pass "a normal head exits 0" || { fail "normal head exited $RC"; echo "$OUT" | tail -15; }
[[ "$(merge_calls)" -eq 1 ]] && pass "exactly one merge call was made" || fail "expected one merge call, got $(merge_calls)"
echo "$OUT" | grep -qF "product PR" && fail "normal head hit the product refusal" || pass "normal head not refused as a product PR"

# 3. prefix match is on fx/ only.
run_case "discussion plane, head fx-docs (no slash)" "fx-docs" 0 discussion "$DISC_REPO"
[[ "$RC" -eq 0 && "$(merge_calls)" -eq 1 ]] && pass "fx-docs is not a product branch" || fail "fx-docs was refused (rc=$RC)"

# 4. unreadable head: fail closed.
run_case "discussion plane, head unreadable" "ignored" 1 discussion "$DISC_REPO"
[[ "$RC" -ne 0 && "$(merge_calls)" -eq 0 ]] && pass "unreadable head refuses, no merge call" || fail "unreadable head did not refuse (rc=$RC)"

# 5. single-repo setup: the plane resolves to "code" but is the Discussion repo.
run_case "single repo (plane resolves to code = discussion repo), head fx/x" "fx/x" 0 code "$DISC_REPO"
[[ "$RC" -ne 0 && "$(merge_calls)" -eq 0 ]] && pass "fx/x refused when the PR repo is the Discussion repo" || fail "single-repo fx/x not refused (rc=$RC)"

# 6. a separate public code plane is outside this rule.
run_case "separate code plane, head fx/x" "fx/x" 0 code "$CODE_REPO_OTHER"
[[ "$RC" -eq 0 && "$(merge_calls)" -eq 1 ]] && pass "code-plane PR on a different repo is not refused" || { fail "code-plane fx/x was refused (rc=$RC)"; echo "$OUT" | tail -10; }

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then exit 1; fi
echo "PRESUM: pass"
