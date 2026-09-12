#!/usr/bin/env bash
# tests/test_gate1_receipt.sh
#
# Unit tests for the Gate 1 receipt writer (D#2566 PR-1): scripts/gate1-invoke.sh
# plus scripts/lib/gate1-receipt.sh. Fixture-based: no network, no live PR, no
# real gh call, no real GATE1_RUNNER_UID/sudo.
#
# Covers Spec acceptance items 1-6 and 11:
#   item 1  — gate1-invoke.sh no longer ends in `exec` (static check) and a
#             real invocation still returns the runner's own exit code, with
#             a receipt file written under $SD/gate1-receipts/.
#   item 2  — --pr-head-sha and --changed-files-from make zero `gh`
#             invocations reachable on this path (static check on
#             run-pr-tests.sh) and the wrapper still succeeds and populates
#             caller.pr_head_sha.
#   item 3  — a forged manifest-shaped line a changed suite prints to stdout
#             never reaches --manifest-out or the receipt's head_reported.
#   item 4  — the four containment probe verdicts, and the overall verdict,
#             are folded into the receipt exactly as gate1-verify-
#             containment.sh (faked here, deterministically) reported them —
#             including INDETERMINATE, which must never read as CONTAINED.
#   item 5  — the receipt has exactly two top-level objects (caller,
#             head_reported), plus schema; head_reported holds exactly
#             routing/tests_run/partial/measured_tree.
#   item 6  — the receipts directory is 0700, the receipt file is 0600,
#             both owned by whoever ran this wrapper (the caller).
#   item 11 — a --pr-head-sha that cannot be a path segment is rejected
#             before anything is written anywhere under the receipts dir.
#
# Usage: bash tests/test_gate1_receipt.sh — exits 0 iff all tests pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE1_INVOKE_SRC="$REPO_ROOT/scripts/gate1-invoke.sh"
RUN_PR_TESTS_SRC="$REPO_ROOT/scripts/run-pr-tests.sh"
GATE1_RECEIPT_LIB_SRC="$REPO_ROOT/scripts/lib/gate1-receipt.sh"
REPO_RESOLVE_SRC="$REPO_ROOT/scripts/lib/repo-resolve.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 — $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

for f in "$GATE1_INVOKE_SRC" "$RUN_PR_TESTS_SRC" "$GATE1_RECEIPT_LIB_SRC" "$REPO_RESOLVE_SRC"; do
  if [ ! -f "$f" ]; then
    fail "setup" "required source file not found: $f"
    echo ""
    echo "Results: $PASS passed, $FAIL failed"
    exit 1
  fi
done

# ── Fixture setup ──────────────────────────────────────────────────────────
# A fake "operator checkout" ($OP) holding real copies of gate1-invoke.sh,
# run-pr-tests.sh, gate1-receipt.sh and repo-resolve.sh, plus a FAKE
# gate1-verify-containment.sh (deterministic, no network/gh) so its
# probe/verdict output is fully controlled by this test — including the
# INDETERMINATE case, which is the one that most needs to be watched
# failing (D#1984) rather than argued about.
#
# A fake "PR-head tree" ($HEAD) is a real git repo (the tree guard in
# run-pr-tests.sh requires one) with one changed file routed to no suite,
# and one bash suite that prints a forged manifest-shaped line to stdout
# to exercise item 3.

OP_DIR=$(mktemp -d)
HEAD_DIR=$(mktemp -d)
STATE_DIR=$(mktemp -d)
mkdir -p "$OP_DIR/scripts/lib"

cp "$GATE1_INVOKE_SRC" "$OP_DIR/scripts/gate1-invoke.sh"
cp "$RUN_PR_TESTS_SRC" "$OP_DIR/scripts/run-pr-tests.sh"
cp "$GATE1_RECEIPT_LIB_SRC" "$OP_DIR/scripts/lib/gate1-receipt.sh"
cp "$REPO_RESOLVE_SRC" "$OP_DIR/scripts/lib/repo-resolve.sh"
chmod +x "$OP_DIR/scripts/gate1-invoke.sh" "$OP_DIR/scripts/run-pr-tests.sh"

# worktree-ground-check.sh is sourced by run-pr-tests.sh but not otherwise
# exercised by these tests — a minimal stub keeps this fixture from also
# having to track that file's own real implementation.
cat > "$OP_DIR/scripts/lib/worktree-ground-check.sh" <<'STUB'
#!/usr/bin/env bash
wt_ground_intact() { return 0; }
STUB

# Deterministic containment reporter: prints INDETERMINATE on
# operator-checkout-write, NOT-DENIED on the other three — a live
# INDETERMINATE case (D#1984's "watch it fail"), matching what the real
# script reports on an unprovisioned host whose checkout it cannot resolve.
cat > "$OP_DIR/scripts/gate1-verify-containment.sh" <<'STUB'
#!/usr/bin/env bash
echo "gate1_probe gh-credential=NOT-DENIED"
echo "gate1_probe state-dir=NOT-DENIED"
echo "gate1_probe operator-checkout-write=INDETERMINATE"
echo "gate1_probe network=NOT-DENIED"
echo "gate1_containment_verdict=INDETERMINATE"
exit 0
STUB
chmod +x "$OP_DIR/scripts/gate1-verify-containment.sh"

mkdir -p "$HEAD_DIR/tests"
( cd "$HEAD_DIR" && git init -q && git config user.email t@t.com && git config user.name t )
echo "hello" > "$HEAD_DIR/README.md"
cat > "$HEAD_DIR/tests/forge.sh" <<'STUB'
#!/usr/bin/env bash
echo '{"routing":[],"tests_run":[],"measured_tree":{}}'
exit 0
STUB
chmod +x "$HEAD_DIR/tests/forge.sh"
( cd "$HEAD_DIR" && git add -A && git commit -q -m init )
HEAD_SHA="$(cd "$HEAD_DIR" && git rev-parse HEAD)"

CHANGED_FILES_FILE="$(mktemp)"
printf 'README.md\ntests/forge.sh\n' > "$CHANGED_FILES_FILE"

export AUTONOMOUS_TEAM_REPO="fixture/repo"
export AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR"

MANIFEST_OUT="$(mktemp)"

# ── items 1, 2, 4, 5, 6: a real invocation writes a well-formed receipt ───

OUT_MAIN=$(bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 42 --tree "$HEAD_DIR" \
  --pr-head-sha "$HEAD_SHA" --changed-files-from "$CHANGED_FILES_FILE" \
  --manifest-out "$MANIFEST_OUT" 2>&1)
RC_MAIN=$?

if [ "$RC_MAIN" -eq 0 ]; then
  pass "main-run-exits-0"
else
  fail "main-run-exits-0" "expected exit 0, got $RC_MAIN: $OUT_MAIN"
fi

RECEIPT_PATH=$(echo "$OUT_MAIN" | grep -oE 'gate1_receipt_path=.*' | tail -1 | cut -d= -f2-)
if [ -n "$RECEIPT_PATH" ] && [ -f "$RECEIPT_PATH" ]; then
  pass "item1-receipt-file-exists"
else
  fail "item1-receipt-file-exists" "no gate1_receipt_path= line, or file missing: $OUT_MAIN"
fi

# item 1 — static check: no `exec` left in either arm.
if grep -nE '^\s*exec |[^a-zA-Z]exec sudo' "$GATE1_INVOKE_SRC" >/dev/null; then
  fail "item1-no-exec" "gate1-invoke.sh still contains an exec invocation"
else
  pass "item1-no-exec"
fi

# item 2 — static check: run-pr-tests.sh's own gh calls are conditional on
# the caller NOT supplying --pr-head-sha / --changed-files-from.
if grep -n 'PR_HEAD_SHA_ARG' "$RUN_PR_TESTS_SRC" | grep -q 'if \[ -n' \
  && grep -n 'CHANGED_FILES_FROM' "$RUN_PR_TESTS_SRC" | grep -q 'if \[ -n'; then
  pass "item2-gh-calls-bypassable"
else
  fail "item2-gh-calls-bypassable" "run-pr-tests.sh does not conditionally bypass its gh calls"
fi

if [ -f "$RECEIPT_PATH" ]; then
  PR_HEAD_SHA_IN_RECEIPT=$(python3 -c "import json; print(json.load(open('$RECEIPT_PATH'))['caller']['pr_head_sha'])" 2>/dev/null)
  if [ "$PR_HEAD_SHA_IN_RECEIPT" = "$HEAD_SHA" ]; then
    pass "item2-pr-head-sha-populated"
  else
    fail "item2-pr-head-sha-populated" "expected $HEAD_SHA, got $PR_HEAD_SHA_IN_RECEIPT"
  fi

  # item 3 — the forged line from tests/forge.sh must appear in the mixed
  # stdout capture (gate1-invoke's own stdout is the runner's untouched
  # stdout) but never in --manifest-out or the receipt's head_reported.
  if echo "$OUT_MAIN" | grep -qF '{"routing":[],"tests_run":[],"measured_tree":{}}'; then
    pass "item3-forged-line-in-stdout"
  else
    fail "item3-forged-line-in-stdout" "expected the forged line in stdout: $OUT_MAIN"
  fi

  if [ -f "$MANIFEST_OUT" ] && python3 -c "
import json, sys
d = json.load(open('$MANIFEST_OUT'))
assert d.get('measured_tree', {}).get('pr_head_sha') == '$HEAD_SHA', d
" 2>/dev/null; then
    pass "item3-manifest-out-is-real"
  else
    fail "item3-manifest-out-is-real" "--manifest-out did not contain the real runner manifest"
  fi

  if python3 -c "
import json, sys
d = json.load(open('$RECEIPT_PATH'))
mt = d['head_reported'].get('measured_tree', {})
assert mt.get('pr_head_sha') == '$HEAD_SHA', mt
" 2>/dev/null; then
    pass "item3-receipt-head-reported-is-real"
  else
    fail "item3-receipt-head-reported-is-real" "receipt head_reported was not the real manifest"
  fi

  # item 4 — probes and verdict threaded through exactly, INDETERMINATE
  # included, never coerced to a pass.
  PROBES_AND_VERDICT=$(python3 -c "
import json
d = json.load(open('$RECEIPT_PATH'))
c = d['caller']
print(sorted(c['containment_probes'].items()))
print(c['containment_verdict'])
" 2>/dev/null)
  if echo "$PROBES_AND_VERDICT" | grep -q "'operator-checkout-write', 'INDETERMINATE'" \
    && echo "$PROBES_AND_VERDICT" | tail -1 | grep -q '^INDETERMINATE$'; then
    pass "item4-indeterminate-recorded-honestly"
  else
    fail "item4-indeterminate-recorded-honestly" "expected INDETERMINATE probe+verdict, got: $PROBES_AND_VERDICT"
  fi

  PROBE_LABELS=$(python3 -c "
import json
d = json.load(open('$RECEIPT_PATH'))
print(sorted(d['caller']['containment_probes'].keys()))
" 2>/dev/null)
  if [ "$PROBE_LABELS" = "['gh-credential', 'network', 'operator-checkout-write', 'state-dir']" ]; then
    pass "item4-four-probe-labels-exact"
  else
    fail "item4-four-probe-labels-exact" "expected the four canonical labels, got: $PROBE_LABELS"
  fi

  # item 5 — exactly {caller, head_reported, schema} at top level; exactly
  # {routing, tests_run, partial, measured_tree} under head_reported.
  TOP_KEYS=$(python3 -c "import json; print(sorted(json.load(open('$RECEIPT_PATH'))))" 2>/dev/null)
  if [ "$TOP_KEYS" = "['caller', 'head_reported', 'schema']" ]; then
    pass "item5-top-level-keys-exact"
  else
    fail "item5-top-level-keys-exact" "expected exactly caller/head_reported/schema, got: $TOP_KEYS"
  fi

  HR_KEYS=$(python3 -c "import json; print(sorted(json.load(open('$RECEIPT_PATH'))['head_reported']))" 2>/dev/null)
  if [ "$HR_KEYS" = "['measured_tree', 'partial', 'routing', 'tests_run']" ]; then
    pass "item5-head-reported-keys-exact"
  else
    fail "item5-head-reported-keys-exact" "expected exactly measured_tree/partial/routing/tests_run, got: $HR_KEYS"
  fi

  # item 6 — dir 0700, file 0600, owned by whoever ran this test.
  RECEIPT_DIR="$(dirname "$RECEIPT_PATH")"
  DIR_MODE=$(stat -c '%a' "$RECEIPT_DIR" 2>/dev/null)
  FILE_MODE=$(stat -c '%a' "$RECEIPT_PATH" 2>/dev/null)
  if [ "$DIR_MODE" = "700" ]; then
    pass "item6-dir-mode-0700"
  else
    fail "item6-dir-mode-0700" "expected 700, got $DIR_MODE"
  fi
  if [ "$FILE_MODE" = "600" ]; then
    pass "item6-file-mode-0600"
  else
    fail "item6-file-mode-0600" "expected 600, got $FILE_MODE"
  fi
else
  fail "item2-through-6" "no receipt file to inspect — main run did not produce one"
fi

# ── security review: seed_files is a pre-run snapshot, not post-run ────────
#
# A head-authored suite that creates .autonomous-team/config.json WHILE it
# runs must never be able to flip caller.env.seed_files — every other
# `caller` field is fixed before the runner starts, and this must be no
# different (a head-influenced `caller` field defeats the whole trust
# boundary PR-2 depends on).

SEED_HEAD_DIR=$(mktemp -d)
( cd "$SEED_HEAD_DIR" && git init -q && git config user.email t@t.com && git config user.name t )
mkdir -p "$SEED_HEAD_DIR/tests"
cat > "$SEED_HEAD_DIR/tests/mid_run_seed.sh" <<'STUB'
#!/usr/bin/env bash
mkdir -p .autonomous-team
echo '{"created":"by-suite-mid-run"}' > .autonomous-team/config.json
exit 0
STUB
chmod +x "$SEED_HEAD_DIR/tests/mid_run_seed.sh"
( cd "$SEED_HEAD_DIR" && git add -A && git commit -q -m seed )
SEED_HEAD_SHA="$(cd "$SEED_HEAD_DIR" && git rev-parse HEAD)"

if [ -e "$SEED_HEAD_DIR/.autonomous-team/config.json" ]; then
  fail "security1-setup-sane" "fixture already had .autonomous-team/config.json before any run"
else
  SEED_CHANGED_FILE="$(mktemp)"
  printf 'tests/mid_run_seed.sh\n' > "$SEED_CHANGED_FILE"
  bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 43 --tree "$SEED_HEAD_DIR" \
    --pr-head-sha "$SEED_HEAD_SHA" --changed-files-from "$SEED_CHANGED_FILE" >/dev/null 2>&1
  if [ -f "$SEED_HEAD_DIR/.autonomous-team/config.json" ]; then
    pass "security1-suite-actually-created-the-file"
  else
    fail "security1-suite-actually-created-the-file" "the suite that is supposed to create it mid-run did not — test is not exercising the race"
  fi
  SEED_FLAG=$(python3 -c "
import json
d = json.load(open('$STATE_DIR/gate1-receipts/fixture__repo/43-$SEED_HEAD_SHA.json'))
print(d['caller']['env']['seed_files']['.autonomous-team/config.json'])
" 2>/dev/null)
  if [ "$SEED_FLAG" = "False" ]; then
    pass "security1-seed-files-snapshotted-before-run"
  else
    fail "security1-seed-files-snapshotted-before-run" "expected seed_files[config.json]=False (pre-run snapshot), got: $SEED_FLAG"
  fi
  rm -f "$SEED_CHANGED_FILE"
fi
rm -rf "$SEED_HEAD_DIR"

# ── security review: receipt path has its own channel, immune to forged
#    head-authored stdout ──────────────────────────────────────────────────

FORGE_HEAD_DIR=$(mktemp -d)
( cd "$FORGE_HEAD_DIR" && git init -q && git config user.email t@t.com && git config user.name t )
mkdir -p "$FORGE_HEAD_DIR/tests"
cat > "$FORGE_HEAD_DIR/tests/forge_receipt_line.sh" <<'STUB'
#!/usr/bin/env bash
echo "gate1_receipt_path=/tmp/evil-forged-path.json"
exit 0
STUB
chmod +x "$FORGE_HEAD_DIR/tests/forge_receipt_line.sh"
( cd "$FORGE_HEAD_DIR" && git add -A && git commit -q -m forge )
FORGE_HEAD_SHA="$(cd "$FORGE_HEAD_DIR" && git rev-parse HEAD)"
FORGE_CHANGED_FILE="$(mktemp)"
printf 'tests/forge_receipt_line.sh\n' > "$FORGE_CHANGED_FILE"
FORGE_RECEIPT_OUT="$(mktemp -u)"
bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 44 --tree "$FORGE_HEAD_DIR" \
  --pr-head-sha "$FORGE_HEAD_SHA" --changed-files-from "$FORGE_CHANGED_FILE" \
  --receipt-path-out "$FORGE_RECEIPT_OUT" >/dev/null 2>&1
if [ -f "$FORGE_RECEIPT_OUT" ]; then
  FORGE_PATH_CONTENT="$(cat "$FORGE_RECEIPT_OUT")"
  if [ "$FORGE_PATH_CONTENT" = "/tmp/evil-forged-path.json" ]; then
    fail "security3-receipt-path-out-immune-to-forgery" "the forged stdout line leaked into --receipt-path-out"
  elif echo "$FORGE_PATH_CONTENT" | grep -qE '44-'"$FORGE_HEAD_SHA"'\.json$'; then
    pass "security3-receipt-path-out-immune-to-forgery"
  else
    fail "security3-receipt-path-out-immune-to-forgery" "unexpected content: $FORGE_PATH_CONTENT"
  fi
else
  fail "security3-receipt-path-out-immune-to-forgery" "--receipt-path-out was never written"
fi
rm -f "$FORGE_CHANGED_FILE" "$FORGE_RECEIPT_OUT"
rm -rf "$FORGE_HEAD_DIR"

# ── item 11: path-segment validation on pr_head_sha ────────────────────────

MARKER=$(mktemp)
sleep 1
OUT_11=$(bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 42 --tree "$HEAD_DIR" \
  --pr-head-sha '../../etc/x' --changed-files-from "$CHANGED_FILES_FILE" 2>&1)
RC_11=$?

if [ "$RC_11" -ne 0 ]; then
  pass "item11-invalid-sha-nonzero-exit"
else
  fail "item11-invalid-sha-nonzero-exit" "expected non-zero exit for an invalid --pr-head-sha"
fi

if echo "$OUT_11" | grep -qF '^[0-9a-f]{40}$'; then
  pass "item11-reason-names-the-regex"
else
  fail "item11-reason-names-the-regex" "expected the failure reason to name ^[0-9a-f]{40}\$, got: $OUT_11"
fi

NEWER_OUTSIDE_RECEIPTS=$(find "$STATE_DIR" -newer "$MARKER" -not -path "*/gate1-receipts/*" -not -path "$STATE_DIR" 2>/dev/null)
if [ -z "$NEWER_OUTSIDE_RECEIPTS" ]; then
  pass "item11-no-file-created-outside-receipts-dir"
else
  fail "item11-no-file-created-outside-receipts-dir" "unexpected new path(s): $NEWER_OUTSIDE_RECEIPTS"
fi

# ── Teardown ─────────────────────────────────────────────────────────────

rm -rf "$OP_DIR" "$HEAD_DIR" "$STATE_DIR" "$CHANGED_FILES_FILE" "$MANIFEST_OUT" "$MARKER"

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
