#!/usr/bin/env bash
# tests/test_guard_registry_check.sh — exercises scripts/ci/guard-registry-check.py
# against fixture repo trees (D#2339 PR-a, repointed at the runner by PR-b).
#
# The checker resolves its repo root from __file__, so each case builds a
# throwaway tree (scripts/ci/ + .github/workflows/ci.yml) and runs the real
# checker source with __file__ pointed into that tree. Running it that way
# rather than copying the file in keeps the checker out of its own subject
# set, which is what lets the empty case present a genuinely empty scripts/ci/.
# Each fixture also gets a real copy of scripts/ci/run-guards.sh, because the
# checker asks the runner what it discovers rather than reimplementing its
# discovery — a reimplementation that drifted would reconcile against a set
# nothing actually runs.
# No state dir, no network, no stubs. The last case runs the checker on the
# real tree the way .github/workflows/ci.yml runs it.
#
# Usage: bash tests/test_guard_registry_check.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECKER="$REPO_ROOT/scripts/ci/guard-registry-check.py"
RUNNER="$REPO_ROOT/scripts/ci/run-guards.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

for f in "$CHECKER" "$RUNNER"; do
  if [ ! -f "$f" ]; then
    echo "FAIL: not found: $f" >&2
    exit 1
  fi
done

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

SHIM="$TMPROOT/shim.py"
cat > "$SHIM" <<'PY'
"""Run guard-registry-check.py as though it lived under a fixture repo root."""
import sys

src, fake_path = sys.argv[1], sys.argv[2]
sys.argv = ["guard-registry-check.py"] + sys.argv[3:]
with open(src) as fh:
    code = compile(fh.read(), src, "exec")
exec(code, {"__name__": "__main__", "__file__": fake_path})
PY

# make_tree <name> — build $TMPROOT/<name> with scripts/ci/ (carrying a real
# run-guards.sh) and .github/workflows/.
make_tree() {
  local root="$TMPROOT/$1"
  mkdir -p "$root/scripts/ci" "$root/.github/workflows"
  cp "$RUNNER" "$root/scripts/ci/run-guards.sh"
  echo "$root"
}

# workflow <root> [extra-run-target...] — a minimal, real-triggered workflow
# (top-level `on:`) that runs the guard runner, plus a `run:` line for each
# named own-step file.
workflow() {
  local root="$1"; shift
  {
    echo "on:"
    echo "  pull_request:"
    echo "jobs:"
    echo "  backend:"
    echo "    name: backend (import-smoke)"
    echo "    steps:"
    echo "      - name: Behavioral guards"
    echo "        run: bash scripts/ci/run-guards.sh"
    local p
    for p in "$@"; do
      echo "      - name: step for $p"
      echo "        run: python3 scripts/ci/$p"
    done
  } > "$root/.github/workflows/ci.yml"
}

# workflow_without_runner <root> [extra-run-target...] — the same, minus the
# runner step. This is the post-PR-b shape of a dropped guard: there is no
# per-guard step left to lose, only the one step that runs all of them.
workflow_without_runner() {
  local root="$1"; shift
  {
    echo "on:"
    echo "  pull_request:"
    echo "jobs:"
    echo "  backend:"
    echo "    name: backend (import-smoke)"
    echo "    steps:"
    local p
    for p in "$@"; do
      echo "      - name: step for $p"
      echo "        run: python3 scripts/ci/$p"
    done
    echo "      - name: something else"
    echo "        run: true"
  } > "$root/.github/workflows/ci.yml"
}

# workflow_no_trigger <root> <filename> [extra-run-target...] — a second
# workflow file with jobs but NO top-level `on:` key: GitHub Actions can
# never run it, so its references must not count as wiring (D#2388).
workflow_no_trigger() {
  local root="$1" fname="$2"; shift 2
  {
    echo "jobs:"
    echo "  extra:"
    echo "    steps:"
    local p
    for p in "$@"; do
      echo "      - name: step for $p"
      echo "        run: python3 scripts/ci/$p"
    done
  } > "$root/.github/workflows/$fname"
}

# workflow_empty_on <root> <filename> [extra-run-target...] — a top-level
# `on:` key present but with nothing under it: same as no trigger at all.
workflow_empty_on() {
  local root="$1" fname="$2"; shift 2
  {
    echo "on:"
    echo "jobs:"
    echo "  extra:"
    echo "    steps:"
    local p
    for p in "$@"; do
      echo "      - name: step for $p"
      echo "        run: python3 scripts/ci/$p"
    done
  } > "$root/.github/workflows/$fname"
}

# ledger <root> <json>
ledger() {
  printf '%s\n' "$2" > "$1/scripts/ci/guard-ledger.json"
}

# run_checker <root> [args...] — echo "<exit>|<stdout+stderr on one line>"
run_checker() {
  local root="$1"; shift
  local out rc
  out="$(python3 "$SHIM" "$CHECKER" "$root/scripts/ci/guard-registry-check.py" "$@" 2>&1)"
  rc=$?
  printf '%s|%s' "$rc" "$(printf '%s' "$out" | tr '\n' ' ')"
}

expect() {
  local label="$1" want_rc="$2" want_sub="$3" got="$4"
  local rc="${got%%|*}" out="${got#*|}"
  if [ "$rc" != "$want_rc" ]; then
    fail "$label" "expected exit $want_rc, got $rc — output: $out"
    return
  fi
  if [ -n "$want_sub" ] && [[ "$out" != *"$want_sub"* ]]; then
    fail "$label" "exit $rc as expected but output never mentioned '$want_sub' — output: $out"
    return
  fi
  pass "$label"
}

echo "== guard-registry-check =="

# 1. Happy path: a guard the runner picks up, an own-step guard the workflow
#    invokes directly, and a ledgered non-guard nothing runs.
R="$(make_tree happy)"
touch "$R/scripts/ci/alpha-guard.py" "$R/scripts/ci/own-guard.py" "$R/scripts/ci/local-tool.sh"
workflow "$R" own-guard.py
ledger "$R" '{"exempt": {"local-tool.sh": "run by hand on a dev host, never in CI"}, "own_step": {"own-guard.py": "needs a PR event payload the runner cannot give it"}}'
expect "runner-discovered + own_step + exempt reconciles clean" 0 "PASS  alpha-guard.py  run by run-guards.sh" "$(run_checker "$R")"

# 2. The post-PR-b dropped-guard case: the runner step is gone from the
#    workflow, so every guard it discovers gates nothing.
R="$(make_tree runner_unwired)"
touch "$R/scripts/ci/alpha-guard.py"
workflow_without_runner "$R"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "an unwired runner fails and is named" 1 "run-guards.sh is the guard runner but is referenced by none of" "$(run_checker "$R")"

# 3. An own_step entry no workflow actually references is a claim the build
#    can check, and does.
R="$(make_tree own_step_lies)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R"
ledger "$R" '{"exempt": {}, "own_step": {"alpha-guard.py": "claims a workflow runs this directly"}}'
expect "an own_step entry nothing references fails" 1 "it runs nowhere and gates nothing" "$(run_checker "$R")"

# 4. An exempt entry that IS referenced is stale in the other direction.
R="$(make_tree exempt_lies)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R" alpha-guard.py
ledger "$R" '{"exempt": {"alpha-guard.py": "claims nothing runs this"}, "own_step": {}}'
expect "an exempt entry a workflow does reference fails" 1 "one of the two is stale" "$(run_checker "$R")"

# 5. A file the runner discovers that ALSO has its own `run:` step runs twice.
#    This is precisely the residue a bad conflict resolution leaves — keep the
#    hand-written step from the other side AND let the runner pick the file up
#    — and it used to pass in silence: green job, working guard, one duplicated
#    block in a log nobody reads.
R="$(make_tree double_run)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R" alpha-guard.py
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "a guard both discovered and hand-wired fails" 1 "it would run twice" "$(run_checker "$R")"

# 6. A blank reason is not a decision.
R="$(make_tree blank_reason)"
touch "$R/scripts/ci/local-tool.sh"
workflow "$R"
ledger "$R" '{"exempt": {"local-tool.sh": "   "}, "own_step": {}}'
expect "blank ledger reason fails" 1 "empty or non-string reason" "$(run_checker "$R")"

# 7. A ledger entry naming a file that no longer exists is stale.
R="$(make_tree stale)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R"
ledger "$R" '{"exempt": {"deleted-tool.sh": "a real-looking reason"}, "own_step": {}}'
expect "stale ledger entry fails and is named" 1 "deleted-tool.sh" "$(run_checker "$R")"

# 8. One file cannot be both exempt and own_step.
R="$(make_tree both_sections)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R" alpha-guard.py
ledger "$R" '{"exempt": {"alpha-guard.py": "r1"}, "own_step": {"alpha-guard.py": "r2"}}'
expect "a file in both ledger sections fails" 1 "it cannot be both" "$(run_checker "$R")"

# 9. Discovering nothing is a failure, not a pass — the item that keeps this
#    check from becoming the thing it guards against.
R="$(make_tree empty)"
rm -f "$R/scripts/ci/run-guards.sh"
workflow "$R"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "empty scripts/ci/ fails rather than reporting all-clear" 1 "discovered zero files" "$(run_checker "$R")"

# 10. A guard named only in a YAML comment is not wired. Same rule as before
#    PR-b, applied now to the runner and to own-step files.
R="$(make_tree comment_only)"
touch "$R/scripts/ci/own-guard.py"
workflow "$R"
printf '      # see scripts/ci/own-guard.py for why\n' >> "$R/.github/workflows/ci.yml"
ledger "$R" '{"exempt": {}, "own_step": {"own-guard.py": "supposedly its own step"}}'
expect "a comment mention does not count as wired" 1 "own-guard.py" "$(run_checker "$R")"

# 11. Discovery is a directory listing, not a mode-bit filter.
R="$(make_tree modebit)"
touch "$R/scripts/ci/no-x-bit-guard.py"
chmod 644 "$R/scripts/ci/no-x-bit-guard.py"
workflow "$R"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "a non-executable file is still discovered" 0 "no-x-bit-guard.py" "$(run_checker "$R")"
expect "--list includes the non-executable file" 0 "no-x-bit-guard.py" "$(run_checker "$R" --list)"
expect "--list counts the runner too" 0 "count: 2" "$(run_checker "$R" --list)"

# 12. A missing or malformed ledger is a hard failure, not an empty exemption set.
R="$(make_tree no_ledger)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R"
expect "missing ledger fails" 1 "guard-ledger.json is missing" "$(run_checker "$R")"
ledger "$R" '{"exempt": {}, "own_step": {}, "typo_key": 1}'
expect "unknown top-level ledger key fails" 1 "unknown top-level key" "$(run_checker "$R")"
ledger "$R" '{"note": "no exempt object here", "own_step": {}}'
expect "ledger without an exempt object fails" 1 "missing its required 'exempt' object" "$(run_checker "$R")"
ledger "$R" '{"exempt": {}}'
expect "ledger without an own_step object fails" 1 "missing its required 'own_step' object" "$(run_checker "$R")"

# 13. If the runner cannot be asked what it runs, the checker must say so
#     rather than reconcile against an empty set and report all-clear.
R="$(make_tree runner_broken)"
touch "$R/scripts/ci/alpha-guard.py"
printf '#!/usr/bin/env bash\nexit 9\n' > "$R/scripts/ci/run-guards.sh"
workflow "$R"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "an unusable runner fails the check" 1 "--list exited 9" "$(run_checker "$R")"

# 14. A guard referenced ONLY by a workflow with no top-level `on:` key
#     fails — that workflow can never run, so its reference isn't wiring
#     (D#2388, the defect this file exists to fix). A real, triggered ci.yml
#     is also present so this isn't masked by the "nothing can ever run"
#     tree-wide failure below.
R="$(make_tree trigger_gap)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R"
workflow_no_trigger "$R" dead.yml alpha-guard.py
ledger "$R" '{"exempt": {}, "own_step": {"alpha-guard.py": "needs a PR event payload the runner cannot give it"}}'
expect "a guard wired only to an untriggerable workflow fails" 1 "it runs nowhere and gates nothing" "$(run_checker "$R")"

# 15. The mirror image: a guard referenced by a workflow WITH a real
#     top-level `on:` key still passes, even when that workflow is not
#     ci.yml — the #2383 widening this file preserves. pr-gates.yml's two
#     guards are the live case this stands in for.
R="$(make_tree cross_file_trigger)"
touch "$R/scripts/ci/beta-guard.py"
workflow "$R"
{
  echo "on:"
  echo "  pull_request:"
  echo "jobs:"
  echo "  gate:"
  echo "    steps:"
  echo "      - run: python3 scripts/ci/beta-guard.py"
} > "$R/.github/workflows/other-gates.yml"
ledger "$R" '{"exempt": {}, "own_step": {"beta-guard.py": "needs a separate trigger set"}}'
expect "a guard wired via a non-ci.yml workflow with a real trigger still passes" 0 "PASS  beta-guard.py  own step in other-gates.yml" "$(run_checker "$R")"

# 16. A top-level `on:` key with nothing under it is handled deliberately:
#     treated the same as no `on:` key at all, not as a parse error and not
#     as a silent pass. This is the "empty value" half of D#2388's acceptance.
R="$(make_tree empty_on_value)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R"
workflow_empty_on "$R" only.yml alpha-guard.py
ledger "$R" '{"exempt": {}, "own_step": {"alpha-guard.py": "needs a PR event payload the runner cannot give it"}}'
expect "an on: key with no value counts as no trigger, same as a missing one" 1 "it runs nowhere and gates nothing" "$(run_checker "$R")"

# 17. A workflow file this scan cannot even read (bad encoding) is a hard
#     failure, not a silent skip — the other half of D#2388's acceptance.
R="$(make_tree bad_encoding)"
touch "$R/scripts/ci/alpha-guard.py"
workflow "$R"
printf '\xff\xfeon:\njobs:\n' > "$R/.github/workflows/bad.yml"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "an unreadable workflow file fails loud rather than silently skipping" 1 "bad.yml" "$(run_checker "$R")"

# 18. The real tree, run the way ci.yml runs it.
expect "the real repo reconciles clean" 0 "guard-registry-check: OK" "$(run_checker "$REPO_ROOT")"

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
