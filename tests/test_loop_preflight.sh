#!/usr/bin/env bash
# tests/test_loop_preflight.sh — D#2063 (PR-b) acceptance tests for
# scripts/loop-preflight.sh's fail-closed behavior on an oversized registry.
#
# Background: the registry dump routinely runs several hundred KB. The old
# code handed it to a child `python3 -c` as a single argv element, which
# blows past the OS single-argv-element ceiling (E2BIG / "Argument list too
# long") on real hosts. The failure was swallowed by a stderr redirect and
# mislabeled downstream as a JSON parse error, and BOTH the loop_enabled gate
# and the budget gate came out permissive (fail-open) as a result.
#
# HOST-DEPENDENCE NOTE: the argv ceiling is a per-host OS limit (historically
# ~128 KiB on Linux via MAX_ARG_STRLEN) and is NOT portable. This suite does
# NOT hardcode that number or assert any specific byte threshold. Instead it
# builds a fixture payload sized well past whatever this host's real ceiling
# is (derived at test time from `getconf ARG_MAX`) and asserts BEHAVIOR: the
# payload is never handed to a child process via argv, so the failure mode
# structurally cannot recur regardless of what a given host's limit is.
#
# All fixtures run against a synthetic fixture repo (a temp dir with stub
# backend/*.py CLIs) rather than the real backend/, because:
#   - the real budget.py's `init` subcommand unconditionally resets
#     session_spent to 0 on every call (pre-existing behavior, out of scope
#     for this PR) — that makes "budget already exhausted when preflight
#     runs" impossible to reproduce end-to-end against the real tool, since
#     loop-preflight.sh's own step 1 calls `budget.py init` before checking
#     status.
#   - the real registry.py requires live GitHub sync to reach an oversized
#     payload, and we need a payload we can size deterministically and scale
#     past this host's real argv ceiling.
# The real end-to-end proof (unmodified backend/, real GitHub-synced
# registry, real config gate) was reproduced by hand once, before any code
# change, and is recorded in the PR body — this suite is the repeatable
# regression harness.
#
# HARD RULE: do NOT call claude, _start_loop_run, or trigger /loop.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REAL_SCRIPT="$REPO_ROOT/scripts/loop-preflight.sh"

PASS=0
FAIL=0

ok()   { echo "  [OK] $1";   PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=== test_loop_preflight ==="

# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------
# Builds a fixture repo at $1 containing:
#   scripts/loop-preflight.sh  — a copy of $2 (so we can point the SAME test
#                                 harness at either the fixed script or a
#                                 deliberately-mutated one for the mutation
#                                 proof)
#   backend/budget.py          — stub: init=no-op; status=prints $BUDGET_JSON_FIXTURE
#   backend/registry.py        — stub: sync=no-op; show=prints a fixture
#                                 payload sized $REG_FIXTURE_BYTES, or fails
#                                 if $REG_SHOW_SHOULD_FAIL=1
#   backend/control_plane.py   — stub: show=prints {"gates":{"loop_enabled":$GATE_LOOP_ENABLED}}
#   backend/context_manager.py — stub: show=no-op
#
# The stub CLIs read their behavior from env vars so each test case can
# reconfigure them without regenerating the fixture tree.
build_fixture() {
  local fixture_dir="$1" script_src="$2"
  mkdir -p "$fixture_dir/scripts" "$fixture_dir/backend"
  cp "$script_src" "$fixture_dir/scripts/loop-preflight.sh"
  chmod +x "$fixture_dir/scripts/loop-preflight.sh"

  cat > "$fixture_dir/backend/budget.py" << 'BUDGET_EOF'
#!/usr/bin/env python3
import os, sys
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "init":
    print("initialized: stub")
    sys.exit(0)
elif cmd == "status":
    if os.environ.get("BUDGET_STATUS_SHOULD_FAIL") == "1":
        sys.stderr.write("stub budget.py status: simulated failure\n")
        sys.exit(1)
    # NOTE: loop-preflight.sh recomputes 'allowed' itself from ceiling/spent
    # (Spec F5 — it does not trust the real budget.py's own 'allowed' field
    # either). Any 'allowed' key in BUDGET_JSON_FIXTURE below is therefore
    # decorative/for-humans-reading-the-test only — only ceiling/spent drive
    # the script's actual decision. Keep them consistent so the fixture reads
    # sanely, but do not treat 'allowed' as load-bearing when editing tests.
    print(os.environ.get("BUDGET_JSON_FIXTURE", '{"ceiling":5000000,"spent":0,"remaining":5000000}'))
    sys.exit(0)
sys.exit(1)
BUDGET_EOF

  cat > "$fixture_dir/backend/control_plane.py" << 'CP_EOF'
#!/usr/bin/env python3
import os, sys
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "show":
    if os.environ.get("CP_SHOW_SHOULD_FAIL") == "1":
        sys.stderr.write("stub control_plane.py show: simulated failure\n")
        sys.exit(1)
    gate = os.environ.get("GATE_LOOP_ENABLED", "true")
    print('{"gates": {"loop_enabled": %s}}' % gate)
    sys.exit(0)
sys.exit(1)
CP_EOF

  cat > "$fixture_dir/backend/context_manager.py" << 'CTX_EOF'
#!/usr/bin/env python3
import sys
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "show":
    print("{}")
    sys.exit(0)
sys.exit(1)
CTX_EOF

  # registry.py is generated per-test (needs to embed a byte count and a
  # known discussion list), see write_registry_stub() below.
  chmod +x "$fixture_dir/backend/budget.py" "$fixture_dir/backend/control_plane.py" "$fixture_dir/backend/context_manager.py"
}

# Writes backend/registry.py into the fixture. $2 = target payload byte size
# for `show` (padded via a filler field); $3 = 1 to make `show`/`queue-summary`
# fail outright (simulating a genuine producer failure, for the F2
# mislabeling check).
#
# D#2310: scripts/loop-preflight.sh's registry step now calls `queue-summary`
# (a thin CLI wrapper around DiscussionRegistry.queue_summary()'s open-filter
# logic) instead of `show` + inline bash-embedded aggregation. `show` stays
# in this stub too — it is still a real registry.py subcommand and other
# fixtures/checks in this file (e.g. AC12's pinned pre-fix script) invoke it
# directly — but the loop-preflight.sh flow this suite drives now reads
# `queue-summary`'s output. The stub's `queue-summary` mirrors the SAME fixed
# discussion set as `show` (all open — no closed_at field at all, which is
# open by the real `_open_only` filter's `.get("closed_at") is None` rule
# too), so registry.total / bucket counts stay identical either way.
write_registry_stub() {
  local fixture_dir="$1" target_bytes="$2" should_fail="$3"
  python3 - "$fixture_dir/backend/registry.py" "$target_bytes" "$should_fail" << 'PYEOF'
import sys

out_path, target_bytes, should_fail = sys.argv[1], int(sys.argv[2]), sys.argv[3]

script = '''#!/usr/bin/env python3
import json, os, sys

cmd = sys.argv[1] if len(sys.argv) > 1 else ""

if cmd == "sync":
    print("synced: stub")
    sys.exit(0)

# Fixed, known discussion set so callers can assert on registry.total. All
# rows are open (no closed_at key at all) — matches the real registry.py's
# `_open_only` rule (`d.get("closed_at") is None`), so open-only counts
# below equal the raw counts.
_DISCUSSIONS = (
    [{"status": "DISCUSSING"}] * 3
    + [{"status": "SPEC_READY"}] * 2
    + [{"status": "IMPLEMENTING"}] * 1
    + [{"status": "REVIEWING"}] * 1
    + [{"status": "DONE"}] * 4
)
_SYNCED_AT = "2026-08-22T00:00:00+00:00"

if cmd == "show":
    if os.environ.get("REG_SHOW_SHOULD_FAIL") == "1":
        sys.stderr.write("stub registry.py show: simulated producer failure\\n")
        sys.exit(1)
    payload = {"discussions": _DISCUSSIONS, "synced_at": _SYNCED_AT}
    body = json.dumps(payload)
    pad_needed = max(0, TARGET_BYTES - len(body))
    payload["_pad"] = "x" * pad_needed
    sys.stdout.write(json.dumps(payload))
    sys.exit(0)

if cmd == "queue-summary":
    if os.environ.get("REG_SHOW_SHOULD_FAIL") == "1":
        sys.stderr.write("stub registry.py queue-summary: simulated producer failure\\n")
        sys.exit(1)
    buckets = {}
    for d in _DISCUSSIONS:
        if d["status"] == "DONE":
            continue  # DONE is reported via `done`, not `buckets` — see queue_summary()
        buckets[d["status"]] = buckets.get(d["status"], 0) + 1
    payload = {
        "total": len(_DISCUSSIONS),
        "open_total": len(_DISCUSSIONS),
        "excluded_closed": 0,
        "buckets": buckets,
        "done": sum(1 for d in _DISCUSSIONS if d["status"] == "DONE"),
        "synced_at": _SYNCED_AT,
    }
    sys.stdout.write(json.dumps(payload))
    sys.exit(0)

sys.exit(1)
'''
script = script.replace("TARGET_BYTES", str(target_bytes))
with open(out_path, "w") as fh:
    fh.write(script)
import os
os.chmod(out_path, 0o755)
PYEOF
}

# Runs the fixture's loop-preflight.sh and captures rc/stdout/stderr into the
# named files under $1 (a directory).
run_fixture() {
  local fixture_dir="$1" out_dir="$2"
  ( cd "$fixture_dir" && bash "$fixture_dir/scripts/loop-preflight.sh" ) \
    > "$out_dir/stdout" 2> "$out_dir/stderr"
  echo $? > "$out_dir/rc"
}

# ---------------------------------------------------------------------------
# Compute a host-independent "definitely oversized" fixture size.
# Do NOT hardcode the discovered ~128 KiB boundary — derive a size from this
# host's own ARG_MAX so the fixture is oversized on whatever host runs this.
# ---------------------------------------------------------------------------
HOST_ARG_MAX=$(getconf ARG_MAX 2>/dev/null || echo 2097152)
OVERSIZE_BYTES=$((HOST_ARG_MAX * 2))
echo "  (host ARG_MAX=$HOST_ARG_MAX, fixture payload target=$OVERSIZE_BYTES bytes)"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

FIXED_FIXTURE="$WORKDIR/fixed"
build_fixture "$FIXED_FIXTURE" "$REAL_SCRIPT"
write_registry_stub "$FIXED_FIXTURE" "$OVERSIZE_BYTES" "0"

# ---------------------------------------------------------------------------
# Check 5 / AC5 — oversized registry, gate disabled -> exit 1
# ---------------------------------------------------------------------------
echo ""
echo "--- AC5: oversized registry + loop_enabled=false -> exit 1 ---"
mkdir -p "$WORKDIR/case5"
GATE_LOOP_ENABLED=false BUDGET_JSON_FIXTURE='{"ceiling":5000000,"spent":0,"remaining":5000000}' \
  run_fixture "$FIXED_FIXTURE" "$WORKDIR/case5"
RC5=$(cat "$WORKDIR/case5/rc")
if [ "$RC5" -eq 1 ]; then
  ok "AC5 — exits 1 when gate disabled, registry oversized"
else
  fail "AC5 — expected exit 1, got $RC5"
fi

# ---------------------------------------------------------------------------
# Check 6 / AC6 — oversized registry, gate enabled, budget exhausted -> exit 1
# ---------------------------------------------------------------------------
echo ""
echo "--- AC6: oversized registry + gate enabled + budget exhausted -> exit 1 ---"
mkdir -p "$WORKDIR/case6"
GATE_LOOP_ENABLED=true BUDGET_JSON_FIXTURE='{"ceiling":1,"spent":1,"remaining":0}' \
  run_fixture "$FIXED_FIXTURE" "$WORKDIR/case6"
RC6=$(cat "$WORKDIR/case6/rc")
if [ "$RC6" -eq 1 ]; then
  ok "AC6 — exits 1 when budget exhausted, registry oversized"
else
  fail "AC6 — expected exit 1, got $RC6"
fi

# ---------------------------------------------------------------------------
# Check 7 / AC7 — oversized registry, gate enabled, budget healthy -> exit 0,
# stdout is JSON, registry.total matches the fixture's discussion count.
# ---------------------------------------------------------------------------
echo ""
echo "--- AC7: oversized registry + gate enabled + budget healthy -> exit 0 ---"
mkdir -p "$WORKDIR/case7"
GATE_LOOP_ENABLED=true BUDGET_JSON_FIXTURE='{"ceiling":5000000,"spent":0,"remaining":5000000}' \
  run_fixture "$FIXED_FIXTURE" "$WORKDIR/case7"
RC7=$(cat "$WORKDIR/case7/rc")
if [ "$RC7" -eq 0 ]; then
  ok "AC7 — exits 0 when gate enabled and budget healthy, registry oversized"
else
  fail "AC7 — expected exit 0, got $RC7 (this is the check that catches a fix that fails closed on everything)"
fi

EXPECTED_TOTAL=11
if python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if d.get('registry', {}).get('total') == int(sys.argv[2]) else 1)
" "$WORKDIR/case7/stdout" "$EXPECTED_TOTAL" 2>/dev/null; then
  ok "AC7b — registry.total ($EXPECTED_TOTAL) matches the fixture's discussion count"
else
  fail "AC7b — registry.total did not match expected $EXPECTED_TOTAL"
  cat "$WORKDIR/case7/stdout"
fi

# ---------------------------------------------------------------------------
# Check 8 / AC8 — in all three cases, stdout is non-empty and valid JSON
# ---------------------------------------------------------------------------
echo ""
echo "--- AC8: stdout is non-empty valid JSON in all three cases ---"
for c in case5 case6 case7; do
  BYTES=$(wc -c < "$WORKDIR/$c/stdout")
  if [ "$BYTES" -gt 1 ] && python3 -c "import json,sys; json.load(sys.stdin)" < "$WORKDIR/$c/stdout" 2>/dev/null; then
    ok "AC8 — $c stdout is non-empty ($BYTES bytes) and valid JSON"
  else
    fail "AC8 — $c stdout is not valid non-trivial JSON ($BYTES bytes)"
  fi
done

# ---------------------------------------------------------------------------
# Check 9 / AC9 — "Argument list too long" never appears on stderr
# ---------------------------------------------------------------------------
echo ""
echo "--- AC9: no 'Argument list too long' on stderr in any case ---"
for c in case5 case6 case7; do
  COUNT=$(grep -c "Argument list too long" "$WORKDIR/$c/stderr" || true)
  if [ "${COUNT:-0}" -eq 0 ]; then
    ok "AC9 — $c stderr has no 'Argument list too long'"
  else
    fail "AC9 — $c stderr contains 'Argument list too long' ($COUNT occurrences)"
  fi
done

# ---------------------------------------------------------------------------
# Check 10 / AC10 — trigger.py's header-injection condition is True for the
# healthy-case stdout (F1: header must not silently disappear on a non-empty
# summary). We assert the CONDITION trigger.py uses, without importing or
# modifying trigger.py itself (out of scope for this PR).
# ---------------------------------------------------------------------------
echo ""
echo "--- AC10: trigger.py header-injection condition is True for healthy stdout ---"
if python3 -c "
import sys
summary = open(sys.argv[1]).read()
ok = bool(summary.strip() and summary.strip() != '{}')
sys.exit(0 if ok else 1)
" "$WORKDIR/case7/stdout" 2>/dev/null; then
  ok "AC10 — summary.strip() and summary.strip() != '{}' is True (header would be injected)"
else
  fail "AC10 — trigger.py's header-injection condition is False for the healthy-case summary"
fi

# ---------------------------------------------------------------------------
# Check 11 / AC11 — F2: a genuine producer (queue-summary) failure must not
# be mislabeled as a parse failure.
# ---------------------------------------------------------------------------
echo ""
echo "--- AC11: genuine queue-summary failure is not mislabeled as a parse failure ---"
FORCED_FIXTURE="$WORKDIR/forced-fail"
build_fixture "$FORCED_FIXTURE" "$REAL_SCRIPT"
write_registry_stub "$FORCED_FIXTURE" "$OVERSIZE_BYTES" "0"
mkdir -p "$WORKDIR/case11"
REG_SHOW_SHOULD_FAIL=1 GATE_LOOP_ENABLED=true BUDGET_JSON_FIXTURE='{"ceiling":5000000,"spent":0,"remaining":5000000}' \
  run_fixture "$FORCED_FIXTURE" "$WORKDIR/case11"

if python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
errs = d.get('errors', [])
has_producer_failed = any('registry.py queue-summary failed' in e for e in errs)
has_bad_label = any('queue-summary parse failed' in e for e in errs)
sys.exit(0 if (errs and has_producer_failed and not has_bad_label) else 1)
" "$WORKDIR/case11/stdout" 2>/dev/null; then
  ok "AC11 — errors array is non-empty, labeled 'queue-summary failed', not mislabeled 'parse failed'"
else
  fail "AC11 — error labeling incorrect for a genuine producer failure"
  cat "$WORKDIR/case11/stdout"
fi

# ---------------------------------------------------------------------------
# Check 12 / AC12 — MUTATION PROOF (required): reintroduce the argv handoff
# and re-run the AC5 scenario; it must FAIL (i.e. must NOT correctly exit 1 —
# the old code exits 0 due to the swallowed E2BIG + downstream JSONDecodeError
# collapsing the summary to permissive defaults).
#
# The "mutation" is a LITERAL, PINNED copy of the pre-fix script, embedded
# below as a heredoc. Do NOT derive this from git history (e.g. `git show
# HEAD:...` or a commit SHA) — HEAD contains the FIXED script the moment this
# PR itself is committed, which would make this check compare the fix against
# itself and pass/fail for the wrong reason forever after. Pinning the exact
# vulnerable text here means the proof's "before" state can never drift.
# ---------------------------------------------------------------------------
echo ""
echo "--- AC12: mutation proof — old argv-handoff code fails this same check ---"
OLD_SCRIPT="$WORKDIR/old-loop-preflight.sh"
cat > "$OLD_SCRIPT" << 'OLD_SCRIPT_EOF'
#!/usr/bin/env bash
# Loop pre-flight: runs coordination module CLIs before each loop iteration.
# Outputs a JSON summary to stdout. Exits 1 if the loop should be skipped.
#
# Usage: bash scripts/loop-preflight.sh
# Exit codes:
#   0 — loop should proceed
#   1 — loop should be skipped (gate disabled or budget exhausted)

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ERRORS='[]'
GATES_JSON='{}'
BUDGET_JSON='{}'
REGISTRY_JSON='{}'

# Helper: append an error string to ERRORS json array
add_error() {
  local msg="$1"
  ERRORS=$(python3 -c "
import json, sys
errs = json.loads(sys.argv[1])
errs.append(sys.argv[2])
print(json.dumps(errs))
" "$ERRORS" "$msg" 2>/dev/null || echo "$ERRORS")
}

# Step 1: Initialize budget (idempotent)
if INIT_OUT=$(python3 backend/budget.py init 2>&1); then
  : # success
else
  add_error "budget.py init failed: $INIT_OUT"
fi

# Step 2: Sync registry with latest Discussion state
if SYNC_OUT=$(python3 backend/registry.py sync 2>&1); then
  # Use 'show' to get JSON, then extract a compact summary
  if RAW_REG=$(python3 backend/registry.py show 2>/dev/null); then
    REGISTRY_JSON=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
discussions = data.get('discussions', [])
statuses = [d.get('status', 'UNKNOWN') for d in discussions]
summary = {
    'total': len(discussions),
    'discussing': sum(1 for s in statuses if s == 'DISCUSSING'),
    'spec_ready': sum(1 for s in statuses if s == 'SPEC_READY'),
    'implementing': sum(1 for s in statuses if s == 'IMPLEMENTING'),
    'reviewing': sum(1 for s in statuses if s == 'REVIEWING'),
    'done': sum(1 for s in statuses if s == 'DONE'),
    'synced_at': data.get('synced_at', ''),
}
print(json.dumps(summary))
" "$RAW_REG" 2>/dev/null) || { add_error "registry.py show parse failed"; }
  else
    add_error "registry.py show failed"
  fi
else
  add_error "registry.py sync failed: $SYNC_OUT"
fi

# Step 2.5: Warm up context manager cache (ensures context file exists before agents read it)
python3 backend/context_manager.py show > /dev/null 2>&1 || add_error "context_manager warmup failed"

# Step 3: Read feature gate states via control_plane show
if CP_OUT=$(python3 backend/control_plane.py show 2>/dev/null); then
  GATES_JSON=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
print(json.dumps(data.get('gates', {})))
" "$CP_OUT" 2>/dev/null) || { add_error "control_plane.py gates parse failed"; }
else
  add_error "control_plane.py show failed"
fi

# Step 4: Get current budget status
if BUDGET_OUT=$(python3 backend/budget.py status 2>/dev/null); then
  BUDGET_JSON=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
# Compute 'allowed': budget not exceeded
ceiling = data.get('ceiling', data.get('session_ceiling', 0))
spent = data.get('spent', data.get('session_spent', 0))
remaining = ceiling - spent if ceiling > 0 else 0
allowed = (spent < ceiling) if ceiling > 0 else True
summary = {
    'ceiling': ceiling,
    'spent': spent,
    'remaining': remaining,
    'allowed': allowed,
}
print(json.dumps(summary))
" "$BUDGET_OUT" 2>/dev/null) || { add_error "budget.py status parse failed"; }
else
  add_error "budget.py status failed"
fi

# Assemble final JSON summary
SUMMARY=$(python3 -c "
import json, sys
print(json.dumps({
    'timestamp': sys.argv[1],
    'gates':     json.loads(sys.argv[2]),
    'budget':    json.loads(sys.argv[3]),
    'registry':  json.loads(sys.argv[4]),
    'errors':    json.loads(sys.argv[5]),
}, indent=2))
" "$TIMESTAMP" "$GATES_JSON" "$BUDGET_JSON" "$REGISTRY_JSON" "$ERRORS")

echo "$SUMMARY"

# Check loop_enabled gate (key may vary by control_plane version)
LOOP_ENABLED=$(echo "$SUMMARY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
gates = d.get('gates', {})
val = gates.get('loop_enabled', gates.get('loop', True))
print('true' if val else 'false')
" 2>/dev/null || echo "true")

if [ "$LOOP_ENABLED" = "false" ]; then
  echo "[loop-preflight] loop_enabled gate is false — skipping iteration" >&2
  exit 1
fi

# Check budget allowed
BUDGET_ALLOWED=$(echo "$SUMMARY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
budget = d.get('budget', {})
val = budget.get('allowed', True)
print('true' if val else 'false')
" 2>/dev/null || echo "true")

if [ "$BUDGET_ALLOWED" = "false" ]; then
  echo "[loop-preflight] budget exhausted — skipping iteration" >&2
  exit 1
fi

exit 0
OLD_SCRIPT_EOF

if [ -s "$OLD_SCRIPT" ]; then
  MUTATED_FIXTURE="$WORKDIR/mutated"
  build_fixture "$MUTATED_FIXTURE" "$OLD_SCRIPT"
  write_registry_stub "$MUTATED_FIXTURE" "$OVERSIZE_BYTES" "0"
  mkdir -p "$WORKDIR/case12"
  GATE_LOOP_ENABLED=false BUDGET_JSON_FIXTURE='{"ceiling":5000000,"spent":0,"remaining":5000000}' \
    run_fixture "$MUTATED_FIXTURE" "$WORKDIR/case12"
  RC12=$(cat "$WORKDIR/case12/rc")
  echo "  old code, same fixture: rc=$RC12 (expected: NOT 1, i.e. it fails to fail closed)"
  if [ "$RC12" -ne 1 ]; then
    ok "AC12 — mutation proof: pre-fix script does NOT exit 1 here (rc=$RC12), confirming the test catches the real bug"
  else
    fail "AC12 — mutation proof: pre-fix script unexpectedly exited 1 too — this check would not have caught the bug"
  fi
else
  fail "AC12 — could not write the pinned pre-fix script literal to disk"
fi

# ---------------------------------------------------------------------------
# AC13 — structural check: the fixed script never hands the registry payload
# to a child process as a positional argv element. (Behavioral, not a byte
# threshold: greps for the specific vulnerable call shape.)
# ---------------------------------------------------------------------------
echo ""
echo "--- AC13: registry payload is never passed via argv ---"
if grep -qE '"\$RAW_REG"' "$REAL_SCRIPT" 2>/dev/null; then
  fail "AC13 — scripts/loop-preflight.sh still passes a captured registry payload as an argv element"
else
  ok "AC13 — no argv-element handoff of the registry payload found in scripts/loop-preflight.sh"
fi

# ---------------------------------------------------------------------------
# AC14 — a genuinely failed budget/gate READ (the underlying CLI errors out
# entirely, not just a large-payload parse issue) must also fail closed. This
# goes slightly beyond the Spec's literal checks 5-12 (which are about the
# argv/E2BIG defect specifically) but is required by the Spec's stated
# Failure Condition: "Any gate read that cannot be completed results in a
# permissive value" — found while reproducing check 7 against the real repo
# with a misconfigured state dir (budget.py status genuinely erroring), where
# the pre-fix fallback of BUDGET_JSON='{}' let 'allowed' default permissive.
# ---------------------------------------------------------------------------
echo ""
echo "--- AC14: a genuinely failed control_plane/budget read fails closed, not open ---"
mkdir -p "$WORKDIR/case14a" "$WORKDIR/case14b"

CP_SHOW_SHOULD_FAIL=1 GATE_LOOP_ENABLED=true BUDGET_JSON_FIXTURE='{"ceiling":5000000,"spent":0,"remaining":5000000}' \
  run_fixture "$FIXED_FIXTURE" "$WORKDIR/case14a"
RC14A=$(cat "$WORKDIR/case14a/rc")
if [ "$RC14A" -eq 1 ]; then
  ok "AC14a — control_plane.py show failing outright fails closed (exit 1), not permissive"
else
  fail "AC14a — expected exit 1 when control_plane.py show fails outright, got $RC14A"
fi

BUDGET_STATUS_SHOULD_FAIL=1 GATE_LOOP_ENABLED=true \
  run_fixture "$FIXED_FIXTURE" "$WORKDIR/case14b"
RC14B=$(cat "$WORKDIR/case14b/rc")
if [ "$RC14B" -eq 1 ]; then
  ok "AC14b — budget.py status failing outright fails closed (exit 1), not permissive"
else
  fail "AC14b — expected exit 1 when budget.py status fails outright, got $RC14B"
fi

# Mutation proof for AC14: reverting the blocking defaults back to '{}'
# (the shape of the original code's fallback) must make this check fail.
MUTANT_OPEN_FALLBACK="$WORKDIR/mutant-open-fallback.sh"
sed -e "s/GATES_JSON='{\"loop_enabled\": false}'/GATES_JSON='{}'/g" \
    -e "s/BUDGET_JSON='{\"allowed\": false}'/BUDGET_JSON='{}'/g" \
    "$REAL_SCRIPT" > "$MUTANT_OPEN_FALLBACK"
if ! diff -q "$MUTANT_OPEN_FALLBACK" "$REAL_SCRIPT" > /dev/null 2>&1; then
  MUTANT_FIXTURE="$WORKDIR/mutant-open-fallback-fixture"
  build_fixture "$MUTANT_FIXTURE" "$MUTANT_OPEN_FALLBACK"
  write_registry_stub "$MUTANT_FIXTURE" "$OVERSIZE_BYTES" "0"
  mkdir -p "$WORKDIR/case14mut"
  BUDGET_STATUS_SHOULD_FAIL=1 GATE_LOOP_ENABLED=true \
    run_fixture "$MUTANT_FIXTURE" "$WORKDIR/case14mut"
  RC14MUT=$(cat "$WORKDIR/case14mut/rc")
  echo "  mutant (permissive fallback), same scenario as AC14b: rc=$RC14MUT (expected: NOT 1)"
  if [ "$RC14MUT" -ne 1 ]; then
    ok "AC14 mutation proof — reverting to '{}' fallbacks breaks AC14b (rc=$RC14MUT), confirming the check is not vacuous"
  else
    fail "AC14 mutation proof — mutant still exited 1; AC14b would not catch a regression back to permissive fallbacks"
  fi
else
  fail "AC14 mutation proof — sed substitution did not change the script; mutation was not applied"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
