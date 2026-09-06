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

echo ""
echo "pytest_baseline contract tests: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
  exit 0
else
  exit 1
fi
