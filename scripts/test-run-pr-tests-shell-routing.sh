#!/usr/bin/env bash
# scripts/test-run-pr-tests-shell-routing.sh — Gate 1 shell-routing self-test
# (D#370 PR1: R0–R6 fixtures, hermetic via --changed-files-from).
#
#   R0  claimed-but-missing registry label routes suite:null + stderr, and a
#       receipt built from that manifest classifies `unrouted`, never `ok`
#   R1  scripts/test-*.sh self-routes ("bash <file>") and runs bounded
#   R2  stem mapping claims scripts/<s>.sh|py + scripts/lib/<s>.sh|py when a
#       scripts/test-<stem>.sh|-*.sh exists in the measured tree, else null
#   R3  scripts/ci/** routes to `bash scripts/ci/run-guards.sh` and runs it
#   R4  PR-carried .gate1-routing.json overlay (strict, never permissive)
#   R5  router and gate1-routing-ledger-check.py agree on a fixture set
#   R6  first-coverage process (incl. negative control) is documented
#
# Real-tree fixtures only run suites proven tree-clean on this host; every
# execution assertion that needs a green suite runs a stub in a scratch git
# tree instead (RUN_PR_TESTS_TREE_ROOT), so this suite never depends on the
# tree-wide guard baseline and never dirties the checkout.
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

TMPD="$(mktemp -d /tmp/test-g1-shell-routing-XXXXXX)"
_cleanup() { rm -rf "$TMPD"; }
trap _cleanup EXIT

SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"

# route_in <tree> <changed-files> <manifest-out> <stderr-out> — run the
# router against <tree> (real tree or a scratch git repo). Pure w.r.t.
# shell state (locals only). Retries ONCE when no manifest appears: the
# router's --manifest-out write depends on mktemp, so a transiently
# exhausted /tmp (this host is a shared multi-agent box) fails the write
# while the run itself was fine — a retry that succeeds still prints a loud
# WARN so the flake stays visible instead of silent. Returns 0 when a
# manifest exists, 1 otherwise (the router's own exit code is NOT the
# signal: red suites are expected in some fixtures).
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
    timeout --kill-after=5s 120 bash "$ROUTER" 1 --pr-head-sha "$sha" \
      --changed-files-from "$files" --manifest-out "$manifest" 2>"$stderr"
  else
    RUN_PR_TESTS_TREE_ROOT="$tree" timeout --kill-after=5s 120 bash "$ROUTER" 1 \
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

# require_manifest <manifest> <stderr> <what> — fail loudly (with the
# router's own stderr tail) when a route produced no manifest, so a router
# crash can never read as a mere routing mismatch.
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

# ── R0 fail-closed (real tree) ─────────────────────────────────────────────
printf 'scripts/lib/pr-plane.sh\n' > "$TMPD/f0.txt"
route_in "$REPO_ROOT" "$TMPD/f0.txt" "$TMPD/m0.json" "$TMPD/e0.txt" >/dev/null 2>&1
require_manifest "$TMPD/m0.json" "$TMPD/e0.txt" "R0" || true
[ "$(suite_of "$TMPD/m0.json" scripts/lib/pr-plane.sh)" = "NULL" ] \
  && pass "R0: claimed-but-missing registry label routes null" \
  || fail "R0: pr-plane.sh suite was not null"
grep -q "no suite claims" "$TMPD/e0.txt" \
  && pass "R0: stderr names the unrouted file" \
  || fail "R0: stderr lacks the 'no suite claims' line"
python3 -c "import json; assert json.load(open('$TMPD/m0.json'))['tests_run'] == [], 'tests ran'" \
  && pass "R0: nothing runs for a nulled claim" \
  || fail "R0: tests_run non-empty for a nulled claim"

# A receipt built from that manifest must classify `unrouted`, never `ok`.
# shellcheck source=scripts/lib/gate1-receipt-check.sh
source "$REPO_ROOT/scripts/lib/gate1-receipt-check.sh"
export GATE1_RECEIPT_HEAD_SHA_1="$SHA"
GATE1_RECEIPT_JSON_1="$(python3 - "$TMPD/m0.json" "$SHA" "$REPO_ROOT" <<'PYEOF'
import json, sys
manifest_path, sha, tree_root = sys.argv[1:4]
m = json.load(open(manifest_path))
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
        "routing": m["routing"], "tests_run": m["tests_run"],
        "partial": bool(m.get("partial", False)),
        "measured_tree": m["measured_tree"],
    },
}
print(json.dumps(receipt))
PYEOF
)"
export GATE1_RECEIPT_JSON_1
if gate1_receipt_check 1 "parity-test/gate1"; then
  fail "R0: all-null receipt authorized (state=$GATE1_RECEIPT_CHECK_STATE)"
else
  [ "$GATE1_RECEIPT_CHECK_STATE" = "unrouted" ] \
    && pass "R0: all-null receipt classifies unrouted" \
    || fail "R0: receipt state was $GATE1_RECEIPT_CHECK_STATE, want unrouted"
fi
unset GATE1_RECEIPT_JSON_1 GATE1_RECEIPT_HEAD_SHA_1

# Sibling-without-pull (post-merge-hook is single-guard: the run block
# executes only when test_post_merge_hook_pull.sh exists). A tree with a
# sibling suite but no pull file must route null on BOTH sides — the old
# any-of-six liveness lists claimed it non-null with an empty tests_run.
S0b="$(new_scratch)"
mkdir -p "$S0b/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S0b/scripts/lib/auto-pull-step.sh"
stub_suite "$S0b" tests/test_post_merge_hook_wiring.sh
commit_scratch "$S0b"
printf 'scripts/lib/auto-pull-step.sh\n' > "$TMPD/f0b.txt"
route_in "$S0b" "$TMPD/f0b.txt" "$TMPD/m0b.json" "$TMPD/e0b.txt" >/dev/null 2>&1
require_manifest "$TMPD/m0b.json" "$TMPD/e0b.txt" "R0-sibling" || true
[ "$(suite_of "$TMPD/m0b.json" scripts/lib/auto-pull-step.sh)" = "NULL" ] \
  && pass "R0: sibling-without-pull routes null on the router side" \
  || fail "R0: sibling-without-pull was non-null on the router side"
python3 -c "import json; assert json.load(open('$TMPD/m0b.json'))['tests_run'] == [], 'tests ran'" \
  && pass "R0: sibling-without-pull runs nothing" \
  || fail "R0: sibling-without-pull tests_run non-empty"
[ "$(GATE1_LEDGER_REPO_ROOT="$S0b" python3 "$LEDGER_CHECK" --is-claimed scripts/lib/auto-pull-step.sh | cut -d: -f1)" = "unclaimed" ] \
  && pass "R0: sibling-without-pull unclaimed on the ledger side" \
  || fail "R0: sibling-without-pull claimed on the ledger side"
grep -q "no suite claims" "$TMPD/e0b.txt" \
  && pass "R0: sibling-without-pull stderr names the unrouted file" \
  || fail "R0: sibling-without-pull stderr silent"

# ── R1 self-route ──────────────────────────────────────────────────────────
# Real tree: AC2's exact file. Label assertions only — the suite itself is
# red on this host for pre-existing env reasons, which must not fail routing.
printf 'scripts/test-muse-spawn-prompt.sh\n' > "$TMPD/f1.txt"
route_in "$REPO_ROOT" "$TMPD/f1.txt" "$TMPD/m1.json" "$TMPD/e1.txt" >/dev/null 2>&1
require_manifest "$TMPD/m1.json" "$TMPD/e1.txt" "R1" || true
[ "$(suite_of "$TMPD/m1.json" scripts/test-muse-spawn-prompt.sh)" = "bash scripts/test-muse-spawn-prompt.sh" ] \
  && pass "R1: scripts/test-*.sh self-routes" \
  || fail "R1: self-route label wrong"
[ "$(run_exit "$TMPD/m1.json" "bash scripts/test-muse-spawn-prompt.sh")" != "NOENTRY" ] \
  && pass "R1: self-routed suite runs (entry present)" \
  || fail "R1: self-routed suite has no tests_run entry"
# Scratch: a green stub proves the execution path passes when the suite does.
S1="$(new_scratch)"
stub_suite "$S1" scripts/test-stub.sh
commit_scratch "$S1"
printf 'scripts/test-stub.sh\n' > "$TMPD/f1s.txt"
route_in "$S1" "$TMPD/f1s.txt" "$TMPD/m1s.json" "$TMPD/e1s.txt" >/dev/null 2>&1
require_manifest "$TMPD/m1s.json" "$TMPD/e1s.txt" "R1-scratch" || true
[ "$(suite_of "$TMPD/m1s.json" scripts/test-stub.sh)" = "bash scripts/test-stub.sh" ] \
  && pass "R1: stub self-routes in scratch tree" \
  || fail "R1: stub self-route label wrong"
[ "$(run_exit "$TMPD/m1s.json" "bash scripts/test-stub.sh")" = "0" ] \
  && pass "R1: stub self-route passes with exit 0" \
  || fail "R1: stub self-route did not pass"

# ── R2 stem mapping ────────────────────────────────────────────────────────
# Real tree, hit (AC3's file; label only — runs the red suite above).
printf 'scripts/lib/muse-spawn-prompt.sh\n' > "$TMPD/f2.txt"
route_in "$REPO_ROOT" "$TMPD/f2.txt" "$TMPD/m2.json" "$TMPD/e2.txt" >/dev/null 2>&1
require_manifest "$TMPD/m2.json" "$TMPD/e2.txt" "R2" || true
case "$(suite_of "$TMPD/m2.json" scripts/lib/muse-spawn-prompt.sh)" in
  *"scripts/test-muse-spawn-prompt.sh"*)
    pass "R2: stem hit names the stem suite" ;;
  *) fail "R2: stem hit did not name test-muse-spawn-prompt.sh" ;;
esac
# Real tree, miss (AC3's file; runs nothing).
printf 'scripts/lib/stuck-pr-detect.sh\n' > "$TMPD/f2m.txt"
route_in "$REPO_ROOT" "$TMPD/f2m.txt" "$TMPD/m2m.json" "$TMPD/e2m.txt" >/dev/null 2>&1
require_manifest "$TMPD/m2m.json" "$TMPD/e2m.txt" "R2-miss" || true
[ "$(suite_of "$TMPD/m2m.json" scripts/lib/stuck-pr-detect.sh)" = "NULL" ] \
  && pass "R2: stem miss routes null" \
  || fail "R2: stem miss was not null"
# Real tree, R0-then-R2 ordering: post-merge-hook.sh has only DEAD registry
# claims (tests/ absent) but a live stem suite — the stem suite must win,
# and it is green and tree-clean here, so assert exit 0.
printf 'scripts/post-merge-hook.sh\n' > "$TMPD/f2o.txt"
route_in "$REPO_ROOT" "$TMPD/f2o.txt" "$TMPD/m2o.json" "$TMPD/e2o.txt" >/dev/null 2>&1
require_manifest "$TMPD/m2o.json" "$TMPD/e2o.txt" "R2-order" || true
[ "$(suite_of "$TMPD/m2o.json" scripts/post-merge-hook.sh)" = "bash scripts/test-post-merge-hook-autoclose.sh" ] \
  && pass "R2: dead registry label yields to live stem suite" \
  || fail "R2: ordering case did not yield the stem suite"
[ "$(run_exit "$TMPD/m2o.json" "bash scripts/test-post-merge-hook-autoclose.sh")" = "0" ] \
  && pass "R2: ordering case passes with exit 0" \
  || fail "R2: ordering case did not pass"
# Scratch: multi-match label shape (+N more) and .py twin coverage.
S2="$(new_scratch)"
mkdir -p "$S2/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S2/scripts/lib/widget.sh"
printf 'x = 1\n' > "$S2/scripts/lib/gizmo.py"
stub_suite "$S2" scripts/test-widget.sh
stub_suite "$S2" scripts/test-widget-extra.sh
stub_suite "$S2" scripts/test-gizmo.sh
commit_scratch "$S2"
printf 'scripts/lib/widget.sh\nscripts/lib/gizmo.py\nscripts/lib/gadget.sh\n' > "$TMPD/f2s.txt"
printf '#!/usr/bin/env bash\n' > "$S2/scripts/lib/gadget.sh"
commit_scratch "$S2"
route_in "$S2" "$TMPD/f2s.txt" "$TMPD/m2s.json" "$TMPD/e2s.txt" >/dev/null 2>&1
require_manifest "$TMPD/m2s.json" "$TMPD/e2s.txt" "R2-scratch" || true
[ "$(suite_of "$TMPD/m2s.json" scripts/lib/widget.sh)" = "bash scripts/test-widget.sh (+1 more)" ] \
  && pass "R2: multi-match label names first + count" \
  || fail "R2: multi-match label was: $(suite_of "$TMPD/m2s.json" scripts/lib/widget.sh)"
[ "$(run_exit "$TMPD/m2s.json" "bash scripts/test-widget.sh")" = "0" ] \
  && [ "$(run_exit "$TMPD/m2s.json" "bash scripts/test-widget-extra.sh")" = "0" ] \
  && pass "R2: all stem matches run" \
  || fail "R2: not all stem matches ran green"
[ "$(suite_of "$TMPD/m2s.json" scripts/lib/gizmo.py)" = "bash scripts/test-gizmo.sh" ] \
  && pass "R2: .py twin maps by stem" \
  || fail "R2: .py twin did not map"
[ "$(suite_of "$TMPD/m2s.json" scripts/lib/gadget.sh)" = "NULL" ] \
  && pass "R2: scratch miss routes null" \
  || fail "R2: scratch miss was not null"

# ── R3 guards arm (scratch stub — the real sweep is host-baseline red) ────
S3="$(new_scratch)"
mkdir -p "$S3/scripts/ci"
printf 'x = 1\n' > "$S3/scripts/ci/fake-guard.py"
stub_suite "$S3" scripts/ci/run-guards.sh
commit_scratch "$S3"
printf 'scripts/ci/fake-guard.py\n' > "$TMPD/f3.txt"
route_in "$S3" "$TMPD/f3.txt" "$TMPD/m3.json" "$TMPD/e3.txt" >/dev/null 2>&1
require_manifest "$TMPD/m3.json" "$TMPD/e3.txt" "R3" || true
[ "$(suite_of "$TMPD/m3.json" scripts/ci/fake-guard.py)" = "bash scripts/ci/run-guards.sh" ] \
  && pass "R3: scripts/ci/** routes to run-guards.sh" \
  || fail "R3: guards label wrong"
[ "$(run_exit "$TMPD/m3.json" "bash scripts/ci/run-guards.sh")" = "0" ] \
  && pass "R3: guards entry runs green" \
  || fail "R3: guards entry did not run green"

# ── R4 overlay (scratch trees) ─────────────────────────────────────────────
# Positive: otherwise-null file routes via the overlay and runs.
S4="$(new_scratch)"
mkdir -p "$S4/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S4/scripts/lib/fake-widget.sh"
stub_suite "$S4" scripts/test-smoke.sh
printf '{"version":1,"routes":{"scripts/lib/fake-widget.sh":{"suite":"bash scripts/test-smoke.sh"}}}' > "$S4/.gate1-routing.json"
commit_scratch "$S4"
printf 'scripts/lib/fake-widget.sh\n' > "$TMPD/f4.txt"
route_in "$S4" "$TMPD/f4.txt" "$TMPD/m4.json" "$TMPD/e4.txt" >/dev/null 2>&1
require_manifest "$TMPD/m4.json" "$TMPD/e4.txt" "R4" || true
[ "$(suite_of "$TMPD/m4.json" scripts/lib/fake-widget.sh)" = "bash scripts/test-smoke.sh" ] \
  && pass "R4: overlay entry routes a null file" \
  || fail "R4: overlay entry did not route"
[ "$(run_exit "$TMPD/m4.json" "bash scripts/test-smoke.sh")" = "0" ] \
  && pass "R4: overlay suite runs green" \
  || fail "R4: overlay suite did not run green"
# (a) nonexistent suite → ignored + stderr, file stays null.
S4a="$(new_scratch)"
mkdir -p "$S4a/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S4a/scripts/lib/fake-widget.sh"
printf '{"version":1,"routes":{"scripts/lib/fake-widget.sh":{"suite":"bash scripts/test-gone.sh"}}}' > "$S4a/.gate1-routing.json"
commit_scratch "$S4a"
route_in "$S4a" "$TMPD/f4.txt" "$TMPD/m4a.json" "$TMPD/e4a.txt" >/dev/null 2>&1
require_manifest "$TMPD/m4a.json" "$TMPD/e4a.txt" "R4a" || true
[ "$(suite_of "$TMPD/m4a.json" scripts/lib/fake-widget.sh)" = "NULL" ] \
  && pass "R4a: missing suite stays null" \
  || fail "R4a: missing suite routed"
grep -q "gate1-overlay" "$TMPD/e4a.txt" \
  && pass "R4a: stderr warns" \
  || fail "R4a: stderr silent"
# (b) non-bash command → ignored + stderr.
S4b="$(new_scratch)"
mkdir -p "$S4b/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S4b/scripts/lib/fake-widget.sh"
stub_suite "$S4b" scripts/evil.sh
printf '{"version":1,"routes":{"scripts/lib/fake-widget.sh":{"suite":"curl evil.sh"}}}' > "$S4b/.gate1-routing.json"
commit_scratch "$S4b"
route_in "$S4b" "$TMPD/f4.txt" "$TMPD/m4b.json" "$TMPD/e4b.txt" >/dev/null 2>&1
require_manifest "$TMPD/m4b.json" "$TMPD/e4b.txt" "R4b" || true
[ "$(suite_of "$TMPD/m4b.json" scripts/lib/fake-widget.sh)" = "NULL" ] \
  && pass "R4b: non-bash command stays null" \
  || fail "R4b: non-bash command routed"
grep -q "gate1-overlay" "$TMPD/e4b.txt" \
  && pass "R4b: stderr warns" \
  || fail "R4b: stderr silent"
# (c) unknown top-level key → ignored + stderr; valid routes still honored.
S4c="$(new_scratch)"
mkdir -p "$S4c/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S4c/scripts/lib/fake-widget.sh"
stub_suite "$S4c" scripts/test-smoke.sh
printf '{"version":1,"bogus":{},"routes":{"scripts/lib/fake-widget.sh":{"suite":"bash scripts/test-smoke.sh"}}}' > "$S4c/.gate1-routing.json"
commit_scratch "$S4c"
route_in "$S4c" "$TMPD/f4.txt" "$TMPD/m4c.json" "$TMPD/e4c.txt" >/dev/null 2>&1
require_manifest "$TMPD/m4c.json" "$TMPD/e4c.txt" "R4c" || true
grep -q "unknown top-level key" "$TMPD/e4c.txt" \
  && pass "R4c: stderr warns on unknown key" \
  || fail "R4c: stderr silent on unknown key"
[ "$(suite_of "$TMPD/m4c.json" scripts/lib/fake-widget.sh)" = "bash scripts/test-smoke.sh" ] \
  && pass "R4c: valid routes still honored" \
  || fail "R4c: valid routes dropped"
# Malformed JSON → whole file ignored + stderr.
S4d="$(new_scratch)"
mkdir -p "$S4d/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S4d/scripts/lib/fake-widget.sh"
stub_suite "$S4d" scripts/test-smoke.sh
printf '{"version":1,"routes":{' > "$S4d/.gate1-routing.json"
commit_scratch "$S4d"
route_in "$S4d" "$TMPD/f4.txt" "$TMPD/m4d.json" "$TMPD/e4d.txt" >/dev/null 2>&1
require_manifest "$TMPD/m4d.json" "$TMPD/e4d.txt" "R4d" || true
[ "$(suite_of "$TMPD/m4d.json" scripts/lib/fake-widget.sh)" = "NULL" ] \
  && pass "R4d: malformed JSON stays null" \
  || fail "R4d: malformed JSON routed"
grep -q "not valid JSON" "$TMPD/e4d.txt" \
  && pass "R4d: stderr warns" \
  || fail "R4d: stderr silent"
# Wrong version → whole file ignored + stderr.
S4e="$(new_scratch)"
mkdir -p "$S4e/scripts/lib"
printf '#!/usr/bin/env bash\n' > "$S4e/scripts/lib/fake-widget.sh"
stub_suite "$S4e" scripts/test-smoke.sh
printf '{"version":2,"routes":{"scripts/lib/fake-widget.sh":{"suite":"bash scripts/test-smoke.sh"}}}' > "$S4e/.gate1-routing.json"
commit_scratch "$S4e"
route_in "$S4e" "$TMPD/f4.txt" "$TMPD/m4e.json" "$TMPD/e4e.txt" >/dev/null 2>&1
require_manifest "$TMPD/m4e.json" "$TMPD/e4e.txt" "R4e" || true
[ "$(suite_of "$TMPD/m4e.json" scripts/lib/fake-widget.sh)" = "NULL" ] \
  && pass "R4e: wrong version stays null" \
  || fail "R4e: wrong version routed"
grep -q "only version 1" "$TMPD/e4e.txt" \
  && pass "R4e: stderr warns" \
  || fail "R4e: stderr silent"

# ── R5 parity ──────────────────────────────────────────────────────────────
python3 "$LEDGER_CHECK" >/dev/null 2>&1 \
  && pass "R5: ledger check exits 0 on main" \
  || fail "R5: ledger check non-zero on main"
# Router-vs-predicate agreement on a purpose-built scratch tree (stubs run
# green and instant, so every claimed path also executes here).
S5="$(new_scratch)"
mkdir -p "$S5/scripts/lib" "$S5/scripts/ci" "$S5/tests"
printf '#!/usr/bin/env bash\n' > "$S5/scripts/lib/pr-plane.sh"
printf '#!/usr/bin/env bash\n' > "$S5/scripts/lib/orphan-triage.sh"
printf '#!/usr/bin/env bash\n' > "$S5/scripts/lib/widget.sh"
printf '#!/usr/bin/env bash\n' > "$S5/scripts/lib/gadget.sh"
# Post-merge-hook liveness probe: sibling suite present, pull file absent —
# both sides must agree it is unclaimed (single-guard registry).
printf '#!/usr/bin/env bash\n' > "$S5/scripts/lib/auto-pull-step.sh"
printf 'x = 1\n' > "$S5/scripts/ci/foo.py"
stub_suite "$S5" tests/test_pr_plane.sh
stub_suite "$S5" tests/test_post_merge_hook_wiring.sh
stub_suite "$S5" scripts/test-foo.sh
stub_suite "$S5" scripts/test-widget.sh
stub_suite "$S5" scripts/ci/run-guards.sh
commit_scratch "$S5"
printf 'scripts/lib/pr-plane.sh\nscripts/lib/orphan-triage.sh\nscripts/test-foo.sh\nscripts/lib/widget.sh\nscripts/lib/gadget.sh\nscripts/ci/foo.py\nscripts/lib/auto-pull-step.sh\n' > "$TMPD/f5.txt"
route_in "$S5" "$TMPD/f5.txt" "$TMPD/m5.json" "$TMPD/e5.txt" >/dev/null 2>&1
require_manifest "$TMPD/m5.json" "$TMPD/e5.txt" "R5" || true
_parity_fail=0
# require_manifest above already failed when the router crashed; comparing
# against a missing manifest here would only emit misleading lines.
if [ -f "$TMPD/m5.json" ]; then
while IFS= read -r _pf; do
  [ -n "$_pf" ] || continue
  _suite="$(suite_of "$TMPD/m5.json" "$_pf")"
  if [ "$_suite" = "NULL" ]; then _router_says="unclaimed"; else _router_says="claimed"; fi
  _ledger_says="$(GATE1_LEDGER_REPO_ROOT="$S5" python3 "$LEDGER_CHECK" --is-claimed "$_pf" | cut -d: -f1)"
  if [ "$_router_says" = "$_ledger_says" ]; then
    pass "R5 parity: $_pf ($_router_says)"
  else
    fail "R5 parity: $_pf router=$_router_says ledger=$_ledger_says"
    _parity_fail=1
  fi
done < "$TMPD/f5.txt"
fi
# R3-missing-runner agreement needs its own tree (no run-guards.sh at all).
S5b="$(new_scratch)"
mkdir -p "$S5b/scripts/ci"
printf 'x = 1\n' > "$S5b/scripts/ci/bar.py"
commit_scratch "$S5b"
printf 'scripts/ci/bar.py\n' > "$TMPD/f5b.txt"
route_in "$S5b" "$TMPD/f5b.txt" "$TMPD/m5b.json" "$TMPD/e5b.txt" >/dev/null 2>&1
require_manifest "$TMPD/m5b.json" "$TMPD/e5b.txt" "R5b" || true
_router5b="claimed"
if [ -f "$TMPD/m5b.json" ]; then
  [ "$(suite_of "$TMPD/m5b.json" scripts/ci/bar.py)" = "NULL" ] && _router5b="unclaimed"
  _ledger5b="$(GATE1_LEDGER_REPO_ROOT="$S5b" python3 "$LEDGER_CHECK" --is-claimed scripts/ci/bar.py | cut -d: -f1)"
  [ "$_router5b" = "$_ledger5b" ] && [ "$_router5b" = "unclaimed" ] \
    && pass "R5 parity: missing runner unclaimed on both sides" \
    || fail "R5 parity: missing runner router=$_router5b ledger=$_ledger5b"
fi
# Ledger rot direction: a now-claimed entry fails, an unclaimed one passes.
printf '{"ledger": {"scripts/lib/muse-spawn-prompt.sh": "stale reason"}}' > "$TMPD/ledger-stale.json"
if GATE1_LEDGER_PATH="$TMPD/ledger-stale.json" python3 "$LEDGER_CHECK" >/dev/null 2>&1; then
  fail "R5: stale claimed entry passed the ledger check"
else
  pass "R5: stale claimed entry fails the ledger check"
fi
printf '{"ledger": {"scripts/lib/stuck-pr-detect.sh": "no suite covers this helper; tracked in D#370 follow-up"}}' > "$TMPD/ledger-fine.json"
if GATE1_LEDGER_PATH="$TMPD/ledger-fine.json" python3 "$LEDGER_CHECK" >/dev/null 2>&1; then
  pass "R5: unclaimed entry passes the ledger check"
else
  fail "R5: unclaimed entry failed the ledger check"
fi

# ── R6 process docs ────────────────────────────────────────────────────────
grep -q "negative control" "$REPO_ROOT/scripts/lib/gate1-routing-overlay.sh" \
  && pass "R6: overlay lib documents the negative control" \
  || fail "R6: overlay lib lacks the negative control"
grep -q "negative control" "$REPO_ROOT/scripts/run-pr-tests.sh" \
  && pass "R6: router header documents the negative control" \
  || fail "R6: router header lacks the negative control"
grep -q "FIRST-COVERAGE PROCESS" "$REPO_ROOT/scripts/lib/gate1-routing-overlay.sh" \
  && grep -q "First-coverage process" "$REPO_ROOT/scripts/run-pr-tests.sh" \
  && pass "R6: first-coverage steps in both headers" \
  || fail "R6: first-coverage steps missing somewhere"

# ── Verdict ────────────────────────────────────────────────────────────────
if [ "$PASS" = "true" ]; then
  echo "test-run-pr-tests-shell-routing.sh: ALL CHECKS PASSED"
  exit 0
else
  echo "FAIL: ${#FAILURES[@]} check(s) failed: ${FAILURES[*]}"
  exit 1
fi
