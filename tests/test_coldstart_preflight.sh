#!/usr/bin/env bash
# tests/test_coldstart_preflight.sh — the GNU coreutils prerequisite check in
# scripts/lib/coldstart-preflight.sh.
#
# The point of this suite is that the check discriminates. A prerequisite check
# is only worth anything if it goes red on a host that lacks the prerequisite
# and green on one that has it, so every case here is one half of a pair:
#
#   CASE 1  no coreutils at all        → refuses, naming GNU coreutils
#   CASE 2  same run                   → also names gh/node/python3, so the new
#                                        check feeds the shared `missing`
#                                        counter instead of short-circuiting
#   CASE 3  everything present except  → still refuses, still names GNU
#           a realpath that rejects -m   coreutils, and does NOT claim
#                                        gh/node/python3 are missing
#   CASE 4  this host, untouched PATH  → does not name GNU coreutils
#   CASE 5  the refusal in CASE 1      → plain prose, no traceback, no bash
#                                        stack trace
#
# CASE 3 is the one that matters most: CASE 1 alone would also pass if the
# check were `[[ -n "$PATH" ]]`. CASE 3 leaves the whole real PATH in place and
# breaks exactly one construct, so only a check that runs the construct can
# tell the difference.
#
# HARD RULE: Do NOT call `claude`, `claude -p`, `_start_loop_run`, or trigger /loop.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PREFLIGHT="$REPO_ROOT/scripts/lib/coldstart-preflight.sh"

PASS=0
FAIL=0
SHIM_DIR=""

ok()   { echo "  [OK] $1";   PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

cleanup() { [[ -n "$SHIM_DIR" ]] && rm -rf "$SHIM_DIR"; return 0; }
trap cleanup EXIT

echo "=== test_coldstart_preflight ==="
echo ""

# ── CASE 1 + 2 + 5: nothing on PATH ───────────────────────────────────────────
# /nonexistent-gnu holds no coreutils, so date/realpath/readlink are all
# unresolvable and the probes fail the way they would on a host that simply
# doesn't ship them.
#
# The interpreter is named by absolute path, resolved before PATH is replaced.
# `PATH=/nonexistent-gnu bash <script>` does not work: the assignment is in
# effect for the command lookup too, so bash itself is not found, the script
# never runs, and the shell exits 127 with empty stdout — which would pass an
# exit-code assertion while proving nothing at all about the check.
echo "--- CASE 1/2/5: PATH with no coreutils ---"

BASH_ABS="$(command -v bash)"
OUT_NOPATH="$(PATH=/nonexistent-gnu "$BASH_ABS" "$PREFLIGHT" 2>/dev/null)"
RC_NOPATH=$?

if [[ "$RC_NOPATH" -ne 0 ]]; then
  ok "CASE 1 — exits non-zero (rc=$RC_NOPATH)"
else
  fail "CASE 1 — expected non-zero exit, got 0"
fi

if [[ "$OUT_NOPATH" == *"missing prerequisite: GNU coreutils"* ]]; then
  ok "CASE 1 — stdout names 'missing prerequisite: GNU coreutils'"
else
  fail "CASE 1 — stdout did not name GNU coreutils; got: $OUT_NOPATH"
fi

# Aggregation: the new check must add to the same `missing` counter the three
# existing checks use, not return early. If it short-circuited, the gh/node/
# python3 lines would be missing from this same run.
for tool in gh node python3; do
  if [[ "$OUT_NOPATH" == *"missing prerequisite: $tool"* ]]; then
    ok "CASE 2 — same run also reports '$tool' (aggregates, no short-circuit)"
  else
    fail "CASE 2 — '$tool' not reported in the same run; the checks are short-circuiting"
  fi
done

if [[ "$OUT_NOPATH" == *"Traceback"* ]]; then
  fail "CASE 5 — refusal contains a Python traceback"
else
  ok "CASE 5 — refusal contains no Python traceback"
fi

if [[ "$OUT_NOPATH" == *"coldstart-preflight.sh: line "* ]]; then
  fail "CASE 5 — refusal contains a bash stack trace"
else
  ok "CASE 5 — refusal contains no bash stack trace"
fi

echo ""

# ── CASE 3: one broken construct, everything else real ────────────────────────
# A realpath that rejects -m, exactly as BSD/macOS realpath does, in front of
# an otherwise untouched PATH. gh, node and python3 all still resolve.
echo "--- CASE 3: real PATH, realpath without -m ---"

SHIM_DIR="$(mktemp -d)"
REAL_REALPATH="$(command -v realpath || true)"
# The forward target is baked in as an absolute path on purpose: the shim sits
# first on PATH while it runs, so resolving "realpath" by name from inside it
# would just find the shim again.
cat > "$SHIM_DIR/realpath" <<SHIM
#!/usr/bin/env bash
# Stands in for BSD/macOS realpath: no -m, everything else forwarded.
if [[ "\${1:-}" == "-m" ]]; then
  echo "realpath: illegal option -- m" >&2
  exit 1
fi
exec "$REAL_REALPATH" "\$@"
SHIM
chmod +x "$SHIM_DIR/realpath"

OUT_SHIM="$(PATH="$SHIM_DIR:$PATH" bash "$PREFLIGHT" 2>/dev/null)"
RC_SHIM=$?

if [[ "$RC_SHIM" -ne 0 ]]; then
  ok "CASE 3 — exits non-zero (rc=$RC_SHIM)"
else
  fail "CASE 3 — a realpath without -m was accepted; the check is not running the construct"
fi

if [[ "$OUT_SHIM" == *"missing prerequisite: GNU coreutils"* ]]; then
  ok "CASE 3 — stdout names GNU coreutils"
else
  fail "CASE 3 — stdout did not name GNU coreutils; got: $OUT_SHIM"
fi

if [[ "$OUT_SHIM" == *"realpath -m"* ]]; then
  ok "CASE 3 — refusal names the construct that failed"
else
  fail "CASE 3 — refusal did not name 'realpath -m'; got: $OUT_SHIM"
fi

# Nothing else was removed from PATH, so nothing else may be reported missing.
# This is what separates "probes the construct" from "notices PATH is short".
for tool in node python3; do
  if [[ "$OUT_SHIM" == *"missing prerequisite: $tool"* ]]; then
    fail "CASE 3 — falsely reported '$tool' missing on an otherwise intact PATH"
  else
    ok "CASE 3 — did not falsely report '$tool' missing"
  fi
done

echo ""

# ── CASE 4: this host, untouched ──────────────────────────────────────────────
# The green half. Asserted against what the host actually is rather than an
# assumption about it: the probes are re-run here directly, and the check has
# to agree with them.
echo "--- CASE 4: untouched PATH on this host ---"

HOST_IS_GNU=1
date -d '1970-01-02 -1 day' '+%Y-%m-%d' >/dev/null 2>&1 || HOST_IS_GNU=0
realpath -m . >/dev/null 2>&1 || HOST_IS_GNU=0
readlink -f . >/dev/null 2>&1 || HOST_IS_GNU=0

OUT_HOST="$(bash "$PREFLIGHT" 2>/dev/null)"
RC_HOST=$?

if [[ "$HOST_IS_GNU" -eq 1 ]]; then
  if [[ "$OUT_HOST" == *"missing prerequisite: GNU coreutils"* ]]; then
    fail "CASE 4 — host passes all three probes directly, but the check reported GNU coreutils missing"
  else
    ok "CASE 4 — host has GNU coreutils and the check does not report it missing"
  fi
  # Exit code here also depends on gh/node/python3, which is not this suite's
  # subject, so it is only asserted when those are genuinely satisfied.
  if [[ "$OUT_HOST" != *"missing prerequisite:"* ]]; then
    if [[ "$RC_HOST" -eq 0 ]]; then
      ok "CASE 4 — no gaps reported and exit is 0"
    else
      fail "CASE 4 — no gaps reported but exit was $RC_HOST"
    fi
  else
    echo "  [INFO] CASE 4 — other prerequisites missing on this host; exit code not asserted"
  fi
else
  if [[ "$OUT_HOST" == *"missing prerequisite: GNU coreutils"* ]]; then
    ok "CASE 4 — host fails a probe directly and the check reports GNU coreutils missing"
  else
    fail "CASE 4 — host fails a probe directly, but the check stayed silent"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
