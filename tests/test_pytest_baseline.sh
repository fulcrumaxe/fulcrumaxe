#!/usr/bin/env bash
# tests/test_pytest_baseline.sh — contract tests for the pytest-baseline
# harness (D#2403 PR 2 of 5). Fixture-only: this suite never invokes a real
# full-suite pytest run.
#
# Run: bash tests/test_pytest_baseline.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/pytest_baseline.py"
HARNESS="$REPO_ROOT/scripts/measure-pytest-baseline.sh"
FIXTURES="$REPO_ROOT/tests/fixtures/pytest_baseline"
FIXTURES_PR3="$FIXTURES/pr3"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_exit() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" -eq "$expected" ]; then
    pass "$label (exit $actual)"
  else
    fail "$label (expected exit $expected, got $actual)"
  fi
}

assert_true() {
  local label="$1" cond="$2"
  if [ "$cond" = "1" ]; then
    pass "$label"
  else
    fail "$label"
  fi
}

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

# ── Item 2: record contract — required top-level keys ─────────────────────
echo "-- item 2: record produces required top-level keys --"
RECORD_OUT=$(python3 "$LIB" record \
  --junit-xml "$FIXTURES/sample-junit.xml" \
  --context "$FIXTURES/sample-context.json")
RC=$?
assert_exit "record exits 0" 0 "$RC"

echo "$RECORD_OUT" > "$SCRATCH/sample-record.json"
python3 - "$SCRATCH/sample-record.json" <<'PYEOF'
import json, sys
record = json.load(open(sys.argv[1]))
required = ["path_scope", "dedup_convention", "load", "checkout", "host",
            "duration_seconds", "complete", "outcomes"]
missing = [k for k in required if k not in record]
sys.exit(1 if missing else 0)
PYEOF
assert_exit "record has all required top-level keys" 0 "$?"

# ── Item 3: round-trips all three outcome kinds ────────────────────────────
echo "-- item 3: outcomes round-trip failure/error/collection_error --"
python3 - "$SCRATCH/sample-record.json" <<'PYEOF'
import json, sys
record = json.load(open(sys.argv[1]))
kinds = {o["outcome"] for o in record["outcomes"]}
sys.exit(0 if kinds == {"failure", "error", "collection_error"} else 1)
PYEOF
assert_exit "all three outcome kinds present" 0 "$?"

# ── Item 4: diff refuses to merge mismatched fixed terms ───────────────────
echo "-- item 4: diff refuses records with mismatched path_scope --"
DIFF_ERR=$(python3 "$LIB" diff --records "$FIXTURES/mismatched/" 2>&1 >/dev/null)
DIFF_RC=$?
if [ "$DIFF_RC" -ne 0 ]; then
  pass "diff exits non-zero on mismatched fixed terms"
else
  fail "diff exits non-zero on mismatched fixed terms (got 0)"
fi
case "$DIFF_ERR" in
  *path_scope*) pass "stderr names the differing field (path_scope)" ;;
  *) fail "stderr names the differing field (path_scope) — got: $DIFF_ERR" ;;
esac

# ── Item 5: kill-after=5s wrapper present, and a killed run still writes
#    a record with complete:false ──────────────────────────────────────────
echo "-- item 5: kill-after=5s wrapper + killed-run record --"
KILLAFTER_COUNT=$(grep -c 'kill-after=5s' "$HARNESS")
if [ "$KILLAFTER_COUNT" -ge 1 ]; then
  pass "scripts/measure-pytest-baseline.sh uses --kill-after=5s (count=$KILLAFTER_COUNT)"
else
  fail "scripts/measure-pytest-baseline.sh uses --kill-after=5s (count=$KILLAFTER_COUNT)"
fi

RUNS_DIR="$SCRATCH/runs"
mkdir -p "$RUNS_DIR"
PYTEST_BASELINE_TEST_ARGV="sleep 10" \
PYTEST_BASELINE_TEST_CAP_SECONDS=2 \
PYTEST_BASELINE_SKIP_SERIALIZE_GUARD=1 \
  timeout 30 bash "$HARNESS" --arm idle --out "$RUNS_DIR" >/dev/null 2>&1
HARNESS_RC=$?
assert_exit "harness exits 0 even when the stub is killed" 0 "$HARNESS_RC"

KILLED_RECORD=$(ls "$RUNS_DIR"/*.json 2>/dev/null | head -1)
if [ -z "$KILLED_RECORD" ]; then
  fail "harness wrote a record for the killed stub run"
else
  pass "harness wrote a record for the killed stub run"
  python3 - "$KILLED_RECORD" <<'PYEOF'
import json, sys
record = json.load(open(sys.argv[1]))
sys.exit(0 if record.get("complete") is False else 1)
PYEOF
  assert_exit "killed run's record has complete:false" 0 "$?"
fi

# ── Item 6: state_dir is never production state ────────────────────────────
echo "-- item 6: state_dir never points at ~/.autonomous-forever-state --"
if [ -n "$KILLED_RECORD" ]; then
  python3 - "$KILLED_RECORD" <<'PYEOF'
import json, os, sys
record = json.load(open(sys.argv[1]))
prod = os.path.expanduser("~/.autonomous-forever-state")
sys.exit(0 if not record.get("state_dir", "").startswith(prod) else 1)
PYEOF
  assert_exit "state_dir does not start with ~/.autonomous-forever-state" 0 "$?"
fi

# ── Item 7: load field carries 3 samples + nproc/arm/contention_method ─────
echo "-- item 7: load field shape on a real emitted record --"
if [ -n "$KILLED_RECORD" ]; then
  python3 - "$KILLED_RECORD" <<'PYEOF'
import json, sys
record = json.load(open(sys.argv[1]))
load = record.get("load", {})
samples = load.get("samples", [])
ok = (
    len(samples) == 3
    and all({"at", "one_min", "five_min"} <= set(s.keys()) for s in samples)
    and "nproc" in load
    and load.get("arm") in ("idle", "contended")
    and "contention_method" in load
)
sys.exit(0 if ok else 1)
PYEOF
  assert_exit "load has 3 samples + nproc/arm/contention_method" 0 "$?"
fi

# ── A second contended-arm smoke run, to exercise the contention lifecycle
#    (spinners started + reaped) without a real full-suite run ─────────────
echo "-- contended arm: contention lifecycle runs and reaps cleanly --"
PYTEST_BASELINE_TEST_ARGV="sleep 1" \
PYTEST_BASELINE_TEST_CAP_SECONDS=10 \
PYTEST_BASELINE_SKIP_SERIALIZE_GUARD=1 \
  timeout 20 bash "$HARNESS" --arm contended --out "$RUNS_DIR" >/dev/null 2>&1
assert_exit "harness exits 0 on contended-arm stub run" 0 "$?"

CONTENDED_RECORD=$(ls "$RUNS_DIR"/run-contended-*.json 2>/dev/null | head -1)
if [ -n "$CONTENDED_RECORD" ]; then
  python3 - "$CONTENDED_RECORD" <<'PYEOF'
import json, sys
record = json.load(open(sys.argv[1]))
load = record.get("load", {})
sys.exit(0 if load.get("arm") == "contended" and load.get("contention_method") else 1)
PYEOF
  assert_exit "contended record carries a non-empty contention_method" 0 "$?"
else
  fail "contended-arm run produced a record"
fi


# ── D#1900 PR 1: dedup_convention/outcome_kinds rename ─────────────────────
# analysis.json and the six run records used the SAME key, "dedup_convention",
# for two different vocabularies (capture-time "junit-structural-v1" vs.
# diff-time "failure+error"). build_analysis's output field is renamed to
# "outcome_kinds"; the record-side field, read by _check_fixed_terms, is
# untouched.
echo "-- D#1900 PR 1: analysis output uses outcome_kinds, not dedup_convention --"
ANALYSIS_OUT=$(python3 "$LIB" diff --records "$REPO_ROOT/tests/baselines/pytest/runs")
RC=$?
assert_exit "diff over the committed baseline runs exits 0" 0 "$RC"

echo "$ANALYSIS_OUT" > "$SCRATCH/analysis-regen.json"
python3 - "$SCRATCH/analysis-regen.json" <<'PYEOF'
import json, sys
analysis = json.load(open(sys.argv[1]))
sys.exit(0 if "outcome_kinds" in analysis and "dedup_convention" not in analysis else 1)
PYEOF
assert_exit "analysis top level has outcome_kinds and not dedup_convention" 0 "$?"

echo "-- D#1900 PR 1: record-side dedup_convention is untouched by the rename --"
python3 - "$SCRATCH/sample-record.json" <<'PYEOF'
import json, sys
record = json.load(open(sys.argv[1]))
sys.exit(0 if record.get("dedup_convention") == "junit-structural-v1" else 1)
PYEOF
assert_exit "record dedup_convention is still the capture-side constant" 0 "$?"

echo "-- D#1900 PR 1: committed analysis.json reproduces byte-for-byte from the six records --"
diff -q "$REPO_ROOT/tests/baselines/pytest/analysis.json" "$SCRATCH/analysis-regen.json" >/dev/null 2>&1
assert_exit "committed analysis.json == diff --records tests/baselines/pytest/runs" 0 "$?"

# ── D#1900 PR 3: manifest and check subcommands ─────────────────────────────
# Two new subcommands, library and tests only — no CI wiring, no full-suite
# run. Each case below pins one PR 3 acceptance item (1-9); item 10 is this
# script's own exit code and growth in check count.

# -- item 1: manifest emits the required fields, node ids sorted -----------
echo "-- D#1900 PR 3 item 1: manifest emits node_ids/dedup_convention/outcome_kinds/generated_* --"
MANIFEST_OUT=$(python3 "$LIB" manifest --junit-xml "$FIXTURES/sample-junit.xml")
assert_exit "manifest exits 0" 0 "$?"

echo "$MANIFEST_OUT" > "$SCRATCH/manifest-out.json"
python3 - "$SCRATCH/manifest-out.json" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
required = ["node_ids", "dedup_convention", "outcome_kinds", "generated_at",
            "generated_sha", "generated_on"]
missing = [k for k in required if k not in m]
sys.exit(1 if missing else 0)
PYEOF
assert_exit "manifest has all six required top-level keys" 0 "$?"

python3 - "$SCRATCH/manifest-out.json" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
expected = ["tests.test_sample::test_errors", "tests.test_sample::test_fails"]
sys.exit(0 if m["node_ids"] == expected else 1)
PYEOF
assert_exit "manifest node_ids is the sorted failure+error set (collection_error excluded by default)" 0 "$?"

python3 - "$SCRATCH/manifest-out.json" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
sys.exit(0 if m["dedup_convention"] == "junit-structural-v1" and m["outcome_kinds"] == "failure+error" else 1)
PYEOF
assert_exit "manifest dedup_convention is capture-side constant, outcome_kinds default is failure+error" 0 "$?"

python3 - "$SCRATCH/manifest-out.json" <<'PYEOF'
import datetime, json, sys
m = json.load(open(sys.argv[1]))
try:
    datetime.datetime.fromisoformat(m["generated_at"].replace("Z", "+00:00"))
except (ValueError, AttributeError):
    sys.exit(1)
sys.exit(0 if m["generated_sha"] and "Python" in m["generated_on"] else 1)
PYEOF
assert_exit "generated_at is ISO8601, generated_sha non-empty, generated_on names Python" 0 "$?"

# -- item 2: check exits 0 when Y's bad set is covered by the manifest ------
echo "-- D#1900 PR 3 item 2: check exits 0 when the run's bad set is within the manifest --"
CHECK_OUT=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-valid.json" --junit-xml "$FIXTURES/sample-junit.xml")
assert_exit "check exits 0 on a covered bad set" 0 "$?"
case "$CHECK_OUT" in
  *"size=2"*"sha="*"age="*) pass "check prints manifest size/sha/age on the pass path too" ;;
  *) fail "check prints manifest size/sha/age on the pass path too — got: $CHECK_OUT" ;;
esac

# -- item 3: check exits non-zero on a genuinely new failure ----------------
echo "-- D#1900 PR 3 item 3: check exits non-zero and names a new failure + repro command --"
CHECK_NEW=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-valid.json" --junit-xml "$FIXTURES_PR3/check-new-failure.xml")
NEW_RC=$?
if [ "$NEW_RC" -ne 0 ]; then
  pass "check exits non-zero on a new failure not in the manifest"
else
  fail "check exits non-zero on a new failure not in the manifest (got 0)"
fi
case "$CHECK_NEW" in
  *"tests.test_sample::test_new_break"*) pass "check names the new failing node id" ;;
  *) fail "check names the new failing node id — got: $CHECK_NEW" ;;
esac
case "$CHECK_NEW" in
  *"python3 -m pytest tests/test_sample.py::test_new_break"*) pass "check prints a runnable reproduction command" ;;
  *) fail "check prints a runnable reproduction command — got: $CHECK_NEW" ;;
esac

# -- item 4: node-id translation, executed against all three shapes in the
#    committed 93's pattern set, not merely string-compared ----------------
# This is the one place in this file that shells out to a real `pytest
# --collect-only` subprocess against the real tree, and that subprocess
# otherwise inherits this shell's environment. The root conftest chain
# imports backend.spawn_templates, which needs a resolvable repo slug
# (AUTONOMOUS_TEAM_REPO, or a "repo" field in .autonomous-team/project.json)
# to import at all — absent on a clean shell and on the code plane itself
# (D#1900's own D#2515 addendum: 0 files under .autonomous-team/ there). Pin
# an explicit env for this one subprocess rather than relying on whatever
# the caller's shell happens to have set, matching the per-command pattern
# in scripts/lib/code-plane-pr.sh (AUTONOMOUS_TEAM_REPO / STATE_DIR set only
# for the one call that needs them, never exported to the whole shell).
echo "-- D#1900 PR 3 item 4: translation executed under --collect-only -q, all three shapes --"
mkdir -p "$SCRATCH/item4-state"
python3 - "$LIB" "$REPO_ROOT" "$SCRATCH/item4-state" <<'PYEOF'
import importlib.util
import os
import subprocess
import sys

lib_path, repo_root, state_dir = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("pytest_baseline", lib_path)
pb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pb)

cases = [
    ("backend.tests.test_blackboard::test_file_write_returns_true",
     "backend/tests/test_blackboard.py::test_file_write_returns_true"),
    ("tests.test_data_layer_duckdb_readers.TestGetRunDetail::test_returns_dict",
     "tests/test_data_layer_duckdb_readers.py::TestGetRunDetail::test_returns_dict"),
    ("backend.tests.test_executor_template_pr_categories::"
     "test_template_contains_category_guide[loop-bootstrap-snapshot]",
     "backend/tests/test_executor_template_pr_categories.py::"
     "test_template_contains_category_guide[loop-bootstrap-snapshot]"),
]

# Explicit env for the collect-only subprocess only — a placeholder slug
# when the caller's shell has none, so this check is self-sufficient in a
# clean shell or on a bare code-plane checkout, and never a real
# AUTONOMOUS_TEAM_REPO this test happens to inherit.
subprocess_env = dict(os.environ)
subprocess_env["AUTONOMOUS_TEAM_REPO"] = os.environ.get("AUTONOMOUS_TEAM_REPO") or "fixture/repo"
subprocess_env["AUTONOMOUS_TEAM_STATE_DIR"] = state_dir

ok = True
for junit_id, expected in cases:
    got = pb.junit_id_to_pytest_nodeid(junit_id)
    if got != expected:
        print(f"translation mismatch: {junit_id!r} -> {got!r}, expected {expected!r}", file=sys.stderr)
        ok = False
        continue
    result = subprocess.run(
        [sys.executable, "-m", "pytest", got, "--collect-only", "-q"],
        cwd=repo_root, capture_output=True, text=True, timeout=60,
        env=subprocess_env,
    )
    if result.returncode != 0 or "1 test collected" not in result.stdout:
        print(f"collect-only did not collect exactly one test for {got!r}:\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
assert_exit "plain-function, class-based, and parametrized node ids each translate and collect exactly one test" 0 "$?"

# -- item 5: a manifest entry that now passes is reported, still exit 0 ----
echo "-- D#1900 PR 3 item 5: a quarantined test that now passes leaves check green with a message --"
CHECK_QUAR=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-valid.json" --junit-xml "$FIXTURES_PR3/check-quarantine.xml")
assert_exit "check exits 0 when a manifest entry no longer fails" 0 "$?"
case "$CHECK_QUAR" in
  *"quarantined tests now pass"*) pass "check reports the quarantined-now-passing count" ;;
  *) fail "check reports the quarantined-now-passing count — got: $CHECK_QUAR" ;;
esac

# -- item 6: no manifest (missing path, or omitted) is report-only, exit 0 --
echo "-- D#1900 PR 3 item 6: missing/omitted manifest is report-only, never blocked --"
CHECK_MISSING=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/does-not-exist.json" --junit-xml "$FIXTURES/sample-junit.xml")
assert_exit "check exits 0 when --manifest points at a missing file" 0 "$?"
case "$CHECK_MISSING" in
  *"report-only"*) pass "check states report-only when the manifest path is missing" ;;
  *) fail "check states report-only when the manifest path is missing — got: $CHECK_MISSING" ;;
esac

CHECK_OMITTED=$(python3 "$LIB" check --junit-xml "$FIXTURES/sample-junit.xml")
assert_exit "check exits 0 when --manifest is omitted entirely" 0 "$?"
case "$CHECK_OMITTED" in
  *"report-only"*) pass "check states report-only when --manifest is omitted" ;;
  *) fail "check states report-only when --manifest is omitted — got: $CHECK_OMITTED" ;;
esac

# -- item 7: a manifest entry that is not a fully-qualified node id is a
#    load-time error, before any comparison — bare module, glob, trailing * --
echo "-- D#1900 PR 3 item 7: non-node-id manifest entries refuse at load time, naming the entry --"
CHECK_BARE_ERR=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-bare-module.json" --junit-xml "$FIXTURES/sample-junit.xml" 2>&1 >/dev/null)
CHECK_BARE_RC=$?
if [ "$CHECK_BARE_RC" -ne 0 ]; then
  pass "check refuses a bare-module manifest entry"
else
  fail "check refuses a bare-module manifest entry (got 0)"
fi
case "$CHECK_BARE_ERR" in
  *"backend.tests.test_blackboard"*) pass "check names the offending bare-module entry" ;;
  *) fail "check names the offending bare-module entry — got: $CHECK_BARE_ERR" ;;
esac

CHECK_GLOB_ERR=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-glob.json" --junit-xml "$FIXTURES/sample-junit.xml" 2>&1 >/dev/null)
CHECK_GLOB_RC=$?
if [ "$CHECK_GLOB_RC" -ne 0 ]; then
  pass "check refuses a glob manifest entry"
else
  fail "check refuses a glob manifest entry (got 0)"
fi

CHECK_STAR_ERR=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-trailing-star.json" --junit-xml "$FIXTURES/sample-junit.xml" 2>&1 >/dev/null)
CHECK_STAR_RC=$?
if [ "$CHECK_STAR_RC" -ne 0 ]; then
  pass "check refuses a trailing-wildcard manifest entry"
else
  fail "check refuses a trailing-wildcard manifest entry (got 0)"
fi

# -- item 8: manifest dedup_convention mismatch is a refusal, reusing the
#    _check_fixed_terms refusal shape ---------------------------------------
echo "-- D#1900 PR 3 item 8: manifest dedup_convention mismatch refuses to compare --"
CHECK_DEDUP_ERR=$(python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-bad-dedup.json" --junit-xml "$FIXTURES/sample-junit.xml" 2>&1 >/dev/null)
CHECK_DEDUP_RC=$?
if [ "$CHECK_DEDUP_RC" -ne 0 ]; then
  pass "check refuses a manifest whose dedup_convention disagrees with the capture constant"
else
  fail "check refuses a manifest whose dedup_convention disagrees with the capture constant (got 0)"
fi
case "$CHECK_DEDUP_ERR" in
  *"dedup_convention"*) pass "check names dedup_convention as the differing term" ;;
  *) fail "check names dedup_convention as the differing term — got: $CHECK_DEDUP_ERR" ;;
esac

# -- item 9: an unreadable manifest (bad JSON, empty, missing node_ids) is
#    a hard, fail-closed refusal — never silently report-only --------------
echo "-- D#1900 PR 3 item 9: an unparseable/empty/nodeless manifest fails closed --"
python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-unparseable.json" --junit-xml "$FIXTURES/sample-junit.xml" >/dev/null 2>&1
CHECK_UNPARSEABLE_RC=$?
if [ "$CHECK_UNPARSEABLE_RC" -ne 0 ]; then
  pass "check fails closed on unparseable JSON"
else
  fail "check fails closed on unparseable JSON (got 0)"
fi

python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-empty.json" --junit-xml "$FIXTURES/sample-junit.xml" >/dev/null 2>&1
CHECK_EMPTY_RC=$?
if [ "$CHECK_EMPTY_RC" -ne 0 ]; then
  pass "check fails closed on an empty manifest file"
else
  fail "check fails closed on an empty manifest file (got 0)"
fi

python3 "$LIB" check --manifest "$FIXTURES_PR3/manifest-no-node-ids.json" --junit-xml "$FIXTURES/sample-junit.xml" >/dev/null 2>&1
CHECK_NO_IDS_RC=$?
if [ "$CHECK_NO_IDS_RC" -ne 0 ]; then
  pass "check fails closed on a manifest missing node_ids"
else
  fail "check fails closed on a manifest missing node_ids (got 0)"
fi

echo ""
echo "pytest_baseline contract tests: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
  exit 0
else
  exit 1
fi
