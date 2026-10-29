#!/usr/bin/env bash
# scripts/test-run-pr-tests-product-routing.sh — Gate 1 product-routing self-test
# (D#370 PR2: R7 fixtures + R8 prefix-ledger fixtures, hermetic via --changed-files-from).
#
#   R7  product arms route apps/web, apps/mobile, workers/api, packages/* and
#       containers/render to `npm run test` in the workspace — only when its
#       package.json carries a `test` script in the MEASURED tree (fail-closed:
#       packages/theme and package.json-less workspaces stay null)
#   R8  `/**` ledger keys match whole subtrees in both readers; apps/roku/**
#       is seeded and accepted, .py-matching prefixes are rejected
#
# Execution assertions run stub npm workspaces in scratch git trees
# (RUN_PR_TESTS_TREE_ROOT), so this suite never runs a real product suite and
# never dirties the checkout. The one real-tree fixture (packages/theme)
# routes null and runs nothing.
#
# Exit 0 on pass, exit 1 on any failure. No network, no gh.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROUTER="$REPO_ROOT/scripts/run-pr-tests.sh"
LEDGER_CHECK="$REPO_ROOT/scripts/ci/gate1-routing-ledger-check.py"
PASS=true
FAILURES=()

pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1"; PASS=false; FAILURES+=("$1"); }

TMPD="$(mktemp -d /tmp/test-g1-product-routing-XXXXXX)"
_cleanup() { rm -rf "$TMPD"; }
trap _cleanup EXIT

SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"

# route_in <tree> <changed-files> <manifest-out> <stderr-out> — same contract
# as the PR1 shell-routing suite: retries once on a missing manifest (a
# transiently exhausted /tmp fails the --manifest-out write while the run
# itself was fine), returns 0 when a manifest exists.
route_in() {
  local tree="$1" files="$2" manifest="$3" stderr="$4"
  local sha first_err
  sha="$(git -C "$tree" rev-parse HEAD)"
  _route_once "$tree" "$files" "$manifest" "$stderr" "$sha"
  [ -f "$manifest" ] && return 0
  first_err="$(tail -c 300 "$stderr" 2>/dev/null | tr '\n' '|')"
  sleep 2
  _route_once "$tree" "$files" "$manifest" "$stderr" "$sha"
  if [ -f "$manifest" ]; then
    echo "WARN: route for $files needed a retry (first stderr: ${first_err:-empty})"
    return 0
  fi
  return 1
}

_route_once() {
  local tree="$1" files="$2" manifest="$3" stderr="$4" sha="$5"
  if [ "$tree" = "$REPO_ROOT" ]; then
    timeout --kill-after=5s 180 bash "$ROUTER" 1 --pr-head-sha "$sha" \
      --changed-files-from "$files" --manifest-out "$manifest" 2>"$stderr"
  else
    RUN_PR_TESTS_TREE_ROOT="$tree" timeout --kill-after=5s 180 bash "$ROUTER" 1 \
      --pr-head-sha "$sha" --changed-files-from "$files" \
      --manifest-out "$manifest" 2>"$stderr"
  fi
  return 0
}

# suite_of <manifest> <file> — print the routing suite or NULL. Pure printer.
suite_of() {
  python3 - "$1" "$2" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
for r in m["routing"]:
    if r["file"] == sys.argv[2]:
        print(r["suite"] if r["suite"] is not None else "NULL")
        break
PYEOF
}

# require_manifest <manifest> <stderr> <what> — fail loudly when a route
# produced no manifest, so a router crash never reads as a routing mismatch.
require_manifest() {
  local manifest="$1" stderr="$2" what="$3"
  if [ -f "$manifest" ]; then return 0; fi
  fail "$what: router produced no manifest (stderr: $(tail -c 400 "$stderr" 2>/dev/null | tr '\n' '|'))"
  return 1
}

# run_exit <manifest> <command> — print the tests_run exit code or NOENTRY.
run_exit() {
  python3 - "$1" "$2" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
for t in m["tests_run"]:
    if t["command"] == sys.argv[2]:
        print(t["exit_code"])
        break
else:
    print("NOENTRY")
PYEOF
}

# count_runs <manifest> <command> — number of tests_run entries for <command>.
count_runs() {
  python3 - "$1" "$2" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
print(sum(1 for t in m["tests_run"] if t["command"] == sys.argv[2]))
PYEOF
}

# new_scratch — print a fresh git repo path. Pure printer (locals only).
new_scratch() {
  local d
  d="$(mktemp -d "$TMPD/scratch-XXXXXX")"
  git init -q "$d"
  printf '%s' "$d"
}

# commit_scratch <dir> — commit everything (test identity, local flags only).
commit_scratch() {
  local d="$1"
  git -C "$d" add -A
  git -C "$d" -c user.email=t@t -c user.name=t commit -qm fixture
}

# stub_suite <dir> <relpath> — a passing executable .sh stub.
stub_suite() {
  local d="$1" rel="$2"
  mkdir -p "$d/$(dirname "$rel")"
  printf '#!/usr/bin/env bash\necho stub-ok: %s\n' "$rel" > "$d/$rel"
  chmod +x "$d/$rel"
}

# stub_npm_ws <dir> <workspace> [no-test] — a package.json with a green `test`
# script, or with scripts-but-no-test when $3 is set.
stub_npm_ws() {
  local d="$1" ws="$2" variant="${3:-}"
  mkdir -p "$d/$ws"
  if [ "$variant" = "no-test" ]; then
    printf '{"name":"stub","private":true,"scripts":{"build":"exit 0"}}' > "$d/$ws/package.json"
  else
    printf '{"name":"stub","private":true,"scripts":{"test":"exit 0"}}' > "$d/$ws/package.json"
  fi
}

# ── R7 product arms (scratch tree, stub npm workspaces) ──────────────────────
S7="$(new_scratch)"
stub_npm_ws "$S7" apps/web
stub_npm_ws "$S7" apps/mobile
stub_npm_ws "$S7" workers/api
stub_npm_ws "$S7" packages/core
stub_npm_ws "$S7" packages/theme no-test
# packages/bare deliberately has no package.json at all (fail-closed).
stub_npm_ws "$S7" containers/render
mkdir -p "$S7/apps/web/src" "$S7/apps/mobile/src" "$S7/workers/api/src" \
  "$S7/packages/core/src" "$S7/packages/theme/src" "$S7/packages/bare/src" \
  "$S7/containers/render"
touch "$S7/apps/web/src/x.tsx" "$S7/apps/web/src/y.tsx" "$S7/apps/web/src/ordered.spec.ts" \
  "$S7/apps/mobile/src/x.ts" "$S7/workers/api/src/index.ts" "$S7/packages/core/src/x.ts" \
  "$S7/packages/theme/src/x.ts" "$S7/packages/bare/src/x.ts" "$S7/containers/render/server.js"
commit_scratch "$S7"
cat > "$TMPD/f7.txt" <<'EOF'
apps/web/src/x.tsx
apps/web/src/y.tsx
apps/web/src/ordered.spec.ts
apps/mobile/src/x.ts
workers/api/src/index.ts
packages/core/src/x.ts
packages/theme/src/x.ts
packages/bare/src/x.ts
containers/render/server.js
EOF
route_in "$S7" "$TMPD/f7.txt" "$TMPD/m7.json" "$TMPD/e7.txt" >/dev/null 2>&1
require_manifest "$TMPD/m7.json" "$TMPD/e7.txt" "R7" || true
[ "$(suite_of "$TMPD/m7.json" apps/web/src/x.tsx)" = "cd apps/web && npm run test" ] \
  && pass "R7: apps/web routes to its npm suite" \
  || fail "R7: apps/web label was: $(suite_of "$TMPD/m7.json" apps/web/src/x.tsx)"
[ "$(run_exit "$TMPD/m7.json" "cd apps/web && npm run test")" = "0" ] \
  && pass "R7: apps/web suite runs green" \
  || fail "R7: apps/web suite did not run green"
[ "$(count_runs "$TMPD/m7.json" "cd apps/web && npm run test")" = "1" ] \
  && pass "R7: one workspace run for three apps/web files (dedup)" \
  || fail "R7: apps/web ran $(count_runs "$TMPD/m7.json" "cd apps/web && npm run test")x, want 1"
[ "$(suite_of "$TMPD/m7.json" apps/web/src/ordered.spec.ts)" = "cd apps/web && npm run test" ] \
  && pass "R7: workspace spec file routes to its workspace, not tui" \
  || fail "R7: spec file label was: $(suite_of "$TMPD/m7.json" apps/web/src/ordered.spec.ts)"
[ "$(suite_of "$TMPD/m7.json" apps/mobile/src/x.ts)" = "cd apps/mobile && npm run test" ] \
  && [ "$(run_exit "$TMPD/m7.json" "cd apps/mobile && npm run test")" = "0" ] \
  && pass "R7: apps/mobile routes and runs green" \
  || fail "R7: apps/mobile routing/execution wrong"
[ "$(suite_of "$TMPD/m7.json" workers/api/src/index.ts)" = "cd workers/api && npm run test" ] \
  && [ "$(run_exit "$TMPD/m7.json" "cd workers/api && npm run test")" = "0" ] \
  && pass "R7: workers/api routes and runs green" \
  || fail "R7: workers/api routing/execution wrong"
[ "$(suite_of "$TMPD/m7.json" packages/core/src/x.ts)" = "cd packages/core && npm run test" ] \
  && [ "$(run_exit "$TMPD/m7.json" "cd packages/core && npm run test")" = "0" ] \
  && pass "R7: packages/core routes and runs green" \
  || fail "R7: packages/core routing/execution wrong"
[ "$(suite_of "$TMPD/m7.json" containers/render/server.js)" = "cd containers/render && npm run test" ] \
  && [ "$(run_exit "$TMPD/m7.json" "cd containers/render && npm run test")" = "0" ] \
  && pass "R7: containers/render routes and runs green" \
  || fail "R7: containers/render routing/execution wrong"
[ "$(suite_of "$TMPD/m7.json" packages/theme/src/x.ts)" = "NULL" ] \
  && pass "R7: no-test-script workspace stays null" \
  || fail "R7: no-test-script workspace was not null"
[ "$(suite_of "$TMPD/m7.json" packages/bare/src/x.ts)" = "NULL" ] \
  && pass "R7: package.json-less workspace stays null" \
  || fail "R7: package.json-less workspace was not null"
grep -q "no suite claims" "$TMPD/e7.txt" \
  && grep -q "packages/theme/src/x.ts" "$TMPD/e7.txt" \
  && grep -q "packages/bare/src/x.ts" "$TMPD/e7.txt" \
  && pass "R7: stderr names both null files" \
  || fail "R7: stderr does not name both null files"

# R7 real tree: packages/theme has no test script, so its files stay null and
# run nothing (the one real-tree fixture — every non-null route would execute
# a real product suite).
printf 'packages/theme/src/tokens.ts\n' > "$TMPD/f7r.txt"
route_in "$REPO_ROOT" "$TMPD/f7r.txt" "$TMPD/m7r.json" "$TMPD/e7r.txt" >/dev/null 2>&1
require_manifest "$TMPD/m7r.json" "$TMPD/e7r.txt" "R7-real" || true
[ "$(suite_of "$TMPD/m7r.json" packages/theme/src/tokens.ts)" = "NULL" ] \
  && pass "R7: real-tree packages/theme file routes null" \
  || fail "R7: real-tree packages/theme file was not null"
python3 -c "import json; assert json.load(open('$TMPD/m7r.json'))['tests_run'] == [], 'tests ran'" \
  && pass "R7: real-tree null claim runs nothing" \
  || fail "R7: real-tree null claim tests_run non-empty"

# ── R8 prefix ledger ─────────────────────────────────────────────────────────
# The seeded apps/roku/** entry is accepted in situ on the real tree.
python3 "$LEDGER_CHECK" >/dev/null 2>&1 \
  && pass "R8: ledger check exits 0 with the seeded prefix entry" \
  || fail "R8: ledger check non-zero on the real tree"
# A prefix matching .py paths is rejected outright (check e, subtree-shaped).
printf '{"ledger": {"scripts/**": "sweeps every helper"}}' > "$TMPD/ledger-py.json"
if GATE1_LEDGER_PATH="$TMPD/ledger-py.json" python3 "$LEDGER_CHECK" >"$TMPD/ledger-py.out" 2>&1; then
  fail "R8: .py-matching prefix passed the ledger check"
else
  grep -q "never eligible" "$TMPD/ledger-py.out" \
    && pass "R8: .py-matching prefix rejected" \
    || fail "R8: .py-matching prefix failed for the wrong reason"
fi
# A prefix naming no directory is rejected (check c, subtree-shaped).
printf '{"ledger": {"nope/**": "reason"}}' > "$TMPD/ledger-missing.json"
if GATE1_LEDGER_PATH="$TMPD/ledger-missing.json" python3 "$LEDGER_CHECK" >/dev/null 2>&1; then
  fail "R8: missing-dir prefix passed the ledger check"
else
  pass "R8: missing-dir prefix rejected"
fi
# A prefix covering a router-claimed file rots (check d, subtree-shaped):
# scratch tree where scripts/lib/foo.sh is stem-claimed and no .py sits
# under scripts/lib, so only the claimed-direction can fire.
S8="$(new_scratch)"
mkdir -p "$S8/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S8/scripts/lib/foo.sh"
stub_suite "$S8" scripts/test-foo.sh
commit_scratch "$S8"
printf '{"ledger": {"scripts/lib/**": "covers foo"}}' > "$TMPD/ledger-stale-prefix.json"
if GATE1_LEDGER_REPO_ROOT="$S8" GATE1_LEDGER_PATH="$TMPD/ledger-stale-prefix.json" \
    python3 "$LEDGER_CHECK" >"$TMPD/ledger-stale.out" 2>&1; then
  fail "R8: claimed-covering prefix passed the ledger check"
else
  grep -q "router-claimed" "$TMPD/ledger-stale.out" \
    && pass "R8: claimed-covering prefix rejected" \
    || fail "R8: claimed-covering prefix failed for the wrong reason"
fi

# Receipt classification over prefix-ledgered vs unledgered paths. Same
# all-null receipt shape and N/A body in both cases — only the file differs,
# so the ledger (not the body) is the decider.
# shellcheck source=scripts/lib/gate1-receipt-check.sh
source "$REPO_ROOT/scripts/lib/gate1-receipt-check.sh"
_receipt_for() {
  python3 - "$1" "$SHA" "$REPO_ROOT" <<'PYEOF'
import json, sys
routed_file, sha, tree_root = sys.argv[1:4]
receipt = {
    "schema": 1,
    "caller": {
        "pr": 1, "repo": "parity-test/gate1", "pr_head_sha": sha,
        "tree_root": tree_root,
        "gate1_runner_copy": "/tmp/parity-runner/run-pr-tests.sh",
        "gate1_containment": "NONE (same-uid)", "containment_probes": {},
        "containment_verdict": "INDETERMINATE", "env": {},
        "written_at": "2026-01-01T00:00:00Z",
        "receipt_path": "/tmp/parity-receipt.json",
    },
    "head_reported": {
        "routing": [{"file": routed_file, "suite": None}], "tests_run": [],
        "partial": False,
        "measured_tree": {"path": tree_root, "head_sha": sha, "pr_head_sha": sha},
    },
}
print(json.dumps(receipt))
PYEOF
}
export GATE1_RECEIPT_HEAD_SHA_1="$SHA"
export GATE1_RECEIPT_PR_BODY_1="Gate 1: N/A — no automatable suite for this family"
GATE1_RECEIPT_JSON_1="$(_receipt_for apps/roku/x.brs)"
export GATE1_RECEIPT_JSON_1
if gate1_receipt_check 1 "parity-test/gate1"; then
  [ "$GATE1_RECEIPT_CHECK_STATE" = "ok" ] \
    && pass "R8: ledgered-prefix receipt classifies ok" \
    || fail "R8: ledgered-prefix receipt authorized with state $GATE1_RECEIPT_CHECK_STATE, want ok"
else
  fail "R8: ledgered-prefix receipt rejected (state=$GATE1_RECEIPT_CHECK_STATE)"
fi
GATE1_RECEIPT_JSON_1="$(_receipt_for scripts/lib/two-gate-check.sh)"
export GATE1_RECEIPT_JSON_1
if gate1_receipt_check 1 "parity-test/gate1"; then
  fail "R8: unledgered receipt authorized (state=$GATE1_RECEIPT_CHECK_STATE)"
else
  [ "$GATE1_RECEIPT_CHECK_STATE" = "unrouted" ] \
    && pass "R8: unledgered receipt classifies unrouted" \
    || fail "R8: unledgered receipt state was $GATE1_RECEIPT_CHECK_STATE, want unrouted"
fi
unset GATE1_RECEIPT_JSON_1 GATE1_RECEIPT_HEAD_SHA_1 GATE1_RECEIPT_PR_BODY_1

# ── Verdict ──────────────────────────────────────────────────────────────────
if [ "$PASS" = "true" ]; then
  echo "test-run-pr-tests-product-routing.sh: ALL CHECKS PASSED"
  exit 0
else
  echo "FAIL: ${#FAILURES[@]} check(s) failed: ${FAILURES[*]}"
  exit 1
fi
