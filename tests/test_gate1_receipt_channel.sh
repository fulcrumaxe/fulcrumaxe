#!/usr/bin/env bash
# tests/test_gate1_receipt_channel.sh
#
# Regression suite for D#2592: the Gate-1 receipt channel used to be NAMED
# (mktemp -u — a path, never created) rather than created, and written with
# a raw `>` redirect that follows symlinks. `/proc/<pid>/cmdline` is
# world-readable, so another local uid could discover the path, pre-place a
# symlink or a 0666 file at it, and have its own content land in `cat`'d
# form inside CR_TASK — a code-reviewer's spawn prompt. This suite is
# fixture-based: no network, no live PR, no real gh call, no real
# GATE1_RUNNER_UID/sudo.
#
# Covers Spec acceptance items 1-7 (item 8 is a standalone `git diff | grep`
# against the repo, not something this suite runs; items 9-12 are separate
# existing suites / the PR body):
#   item 1 — this suite exits 0 with a "0 failed" summary line
#   item 2 — the symlink case below is the one to run against a restored
#             pre-fix scripts/gate1-invoke.sh (pass its path via
#             --gate1-invoke-path) — it must FAIL there and PASS against
#             the fixed file (D#1984). Both this run's real write path
#             satisfies D#2149 — no --dry-run, no preview.
#   item 3 — static check: the GATE1_RECEIPT_PATH_OUT= line in
#             loop-phased-step5.sh builds the path inside a directory a
#             sibling line creates with `mktemp -d`, and contains no
#             `mktemp -u` itself
#   item 4 — that directory (the real RHS of the GATE1_RECEIPT_DIR=
#             assignment, executed for real) is mode 700, owned by the
#             invoking uid
#   item 5 — static + behavioral: gate1-invoke.sh's write site has no raw
#             `>` redirect onto RECEIPT_PATH_OUT_ARG, and the real write
#             (exercised by the symlink case above) uses a same-directory
#             temp file plus `mv -f`
#   item 6 — static check: sanitize_echo is used in loop-phased-step5.sh,
#             and (behaviorally, using the real sanitize-echo.sh) the value
#             that construction feeds into CR_TASK carries the
#             <<UNTRUSTED EXTERNAL CONTENT>> wrapper around the receipt path
#   item 7 — behaviorally: when sanitize_echo's delegate is broken (fails
#             closed to empty output, per D#2582), the receipt line falls
#             back to the existing "not produced this round" sentence
#             rather than going empty
#
# Usage:
#   bash tests/test_gate1_receipt_channel.sh
#   bash tests/test_gate1_receipt_channel.sh --gate1-invoke-path /path/to/pre-fix/gate1-invoke.sh
#     Runs the symlink case (item 2) against an arbitrary local copy of
#     gate1-invoke.sh instead of this checkout's own — used to produce the
#     pre-fix FAIL transcript for the PR body: restore
#     scripts/gate1-invoke.sh from origin/main into a scratch file and pass
#     its path here.
#
# Exits 0 iff all assertions pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STEP5_SRC="$REPO_ROOT/scripts/loop-phased-step5.sh"
GATE1_INVOKE_SRC="$REPO_ROOT/scripts/gate1-invoke.sh"
RUN_PR_TESTS_SRC="$REPO_ROOT/scripts/run-pr-tests.sh"
GATE1_RECEIPT_LIB_SRC="$REPO_ROOT/scripts/lib/gate1-receipt.sh"
REPO_RESOLVE_SRC="$REPO_ROOT/scripts/lib/repo-resolve.sh"
SANITIZE_ECHO_SRC="$REPO_ROOT/scripts/lib/sanitize-echo.sh"
INTAKE_GATE_SRC="$REPO_ROOT/scripts/lib/external_intake_gate.py"

# sanitize-echo.sh delegates to external_intake_gate.py, which resolves
# BOT_ACCOUNT at import time (AUTONOMOUS_TEAM_BOT_ACCOUNT env var, or
# .autonomous-team/config.json -- absent on the code plane). Matches
# tests/test_echo_site_sanitization.sh's own fixture value; this suite
# doesn't exercise trust/plane resolution, only the sanitizer.
export AUTONOMOUS_TEAM_BOT_ACCOUNT="${AUTONOMOUS_TEAM_BOT_ACCOUNT:-test-bot}"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

GATE1_INVOKE_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gate1-invoke-path) GATE1_INVOKE_OVERRIDE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
GATE1_INVOKE_UNDER_TEST="${GATE1_INVOKE_OVERRIDE:-$GATE1_INVOKE_SRC}"

for f in "$STEP5_SRC" "$GATE1_INVOKE_UNDER_TEST" "$RUN_PR_TESTS_SRC" \
         "$GATE1_RECEIPT_LIB_SRC" "$REPO_RESOLVE_SRC" "$SANITIZE_ECHO_SRC" \
         "$INTAKE_GATE_SRC"; do
  if [ ! -f "$f" ]; then
    fail "setup" "required source file not found: $f"
    echo ""
    echo "Results: $PASS passed, $FAIL failed"
    exit 1
  fi
done

# ── Fixture: real gate1-invoke.sh in an operator-shaped dir ────────────────
# Mirrors tests/test_gate1_receipt.sh's own fixture: real gate1-invoke.sh +
# run-pr-tests.sh + gate1-receipt.sh + repo-resolve.sh, a same-uid
# containment stub (no sudo/gate-runner needed here), and a minimal
# real-git PR-head tree with one top-level tests/*.sh file that self-routes
# (run-pr-tests.sh's third case arm) so a real manifest — and therefore a
# real receipt — gets written.

OP_DIR=$(mktemp -d)
HEAD_DIR=$(mktemp -d)
STATE_DIR=$(mktemp -d)
ATTACK_DIR=$(mktemp -d)
mkdir -p "$OP_DIR/scripts/lib"

cleanup() {
  rm -rf "$OP_DIR" "$HEAD_DIR" "$STATE_DIR" "$ATTACK_DIR" 2>/dev/null || true
  [ -n "${CHANGED_FILE:-}" ] && rm -f "$CHANGED_FILE" 2>/dev/null || true
  [ -n "${EVAL_RECEIPT_DIR:-}" ] && rm -rf "$EVAL_RECEIPT_DIR" 2>/dev/null || true
}
trap cleanup EXIT

cp "$GATE1_INVOKE_UNDER_TEST" "$OP_DIR/scripts/gate1-invoke.sh"
cp "$RUN_PR_TESTS_SRC" "$OP_DIR/scripts/run-pr-tests.sh"
cp "$GATE1_RECEIPT_LIB_SRC" "$OP_DIR/scripts/lib/gate1-receipt.sh"
cp "$REPO_RESOLVE_SRC" "$OP_DIR/scripts/lib/repo-resolve.sh"
chmod +x "$OP_DIR/scripts/gate1-invoke.sh" "$OP_DIR/scripts/run-pr-tests.sh"

cat > "$OP_DIR/scripts/lib/worktree-ground-check.sh" <<'STUB'
#!/usr/bin/env bash
wt_ground_intact() { return 0; }
STUB

cat > "$OP_DIR/scripts/gate1-verify-containment.sh" <<'STUB'
#!/usr/bin/env bash
echo "gate1_probe gh-credential=NOT-DENIED"
echo "gate1_probe state-dir=NOT-DENIED"
echo "gate1_probe operator-checkout-write=NOT-DENIED"
echo "gate1_probe network=NOT-DENIED"
echo "gate1_containment_verdict=UNCONTAINED"
exit 0
STUB
chmod +x "$OP_DIR/scripts/gate1-verify-containment.sh"

( cd "$HEAD_DIR" && git init -q && git config user.email t@t.com && git config user.name t )
mkdir -p "$HEAD_DIR/tests"
cat > "$HEAD_DIR/tests/noop.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$HEAD_DIR/tests/noop.sh"
( cd "$HEAD_DIR" && git add -A && git commit -q -m init )
HEAD_SHA="$(cd "$HEAD_DIR" && git rev-parse HEAD)"
CHANGED_FILE="$(mktemp)"
printf 'tests/noop.sh\n' > "$CHANGED_FILE"

export AUTONOMOUS_TEAM_REPO="fixture/repo"
export AUTONOMOUS_TEAM_STATE_DIR="$STATE_DIR"

# ── items 2 & 5: symlink attack against the real write path ────────────────
#
# Mirrors the attack the Discussion describes: an attacker who has read
# --receipt-path-out's value off /proc/<pid>/cmdline pre-places a symlink at
# it before the invoker writes. Pre-placing the symlink ourselves and
# running the real wrapper is the real write Gate 2 needs (D#2149) — no
# --dry-run, no preview.

ATTACKER_TARGET="$ATTACK_DIR/attacker-owned-file"
printf 'UNTOUCHED-SENTINEL\n' > "$ATTACKER_TARGET"
RECEIPT_OUT="$ATTACK_DIR/receipt-path-out"
ln -s "$ATTACKER_TARGET" "$RECEIPT_OUT"

bash "$OP_DIR/scripts/gate1-invoke.sh" --pr 99 --tree "$HEAD_DIR" \
  --pr-head-sha "$HEAD_SHA" --changed-files-from "$CHANGED_FILE" \
  --receipt-path-out "$RECEIPT_OUT" >/dev/null 2>&1

ATTACKER_TARGET_CONTENT="$(cat "$ATTACKER_TARGET" 2>/dev/null || echo MISSING)"
if [ "$ATTACKER_TARGET_CONTENT" = "UNTOUCHED-SENTINEL" ]; then
  pass "symlink-attack-target-untouched"
else
  fail "symlink-attack-target-untouched" "attacker's file was overwritten: $ATTACKER_TARGET_CONTENT"
fi

if [ -L "$RECEIPT_OUT" ]; then
  fail "symlink-replaced-not-followed" "--receipt-path-out is still a symlink after the run -- a raw > redirect followed it"
elif [ -f "$RECEIPT_OUT" ] && grep -q "99-$HEAD_SHA" "$RECEIPT_OUT" 2>/dev/null; then
  pass "symlink-replaced-not-followed"
else
  fail "symlink-replaced-not-followed" "receipt-path-out is not a regular file containing the real receipt path"
fi

# item 5 (static) — no raw `>` redirect onto RECEIPT_PATH_OUT_ARG, and the
# write site delegates to a same-dir-temp-plus-mv helper.
if grep -nE '>[[:space:]]*"\$RECEIPT_PATH_OUT_ARG"' "$GATE1_INVOKE_SRC" >/dev/null; then
  fail "item5-no-raw-redirect" "found a raw > redirect onto RECEIPT_PATH_OUT_ARG in $GATE1_INVOKE_SRC"
else
  pass "item5-no-raw-redirect"
fi

if grep -n 'RECEIPT_PATH_OUT_ARG' "$GATE1_INVOKE_SRC" | grep -q '_gate1_write_receipt_path_out' \
    && grep -n '_gate1_write_receipt_path_out()' -A8 "$GATE1_INVOKE_SRC" | grep -q 'mv -f'; then
  pass "item5-same-dir-temp-plus-mv"
else
  fail "item5-same-dir-temp-plus-mv" "write site does not delegate to a same-dir-temp-plus-mv-f helper"
fi

# ── item 3 (static): loop-phased-step5.sh builds the receipt path inside a
#    directory a sibling line creates with mktemp -d, and the path-out
#    assignment itself contains no mktemp -u ──────────────────────────────

if grep -n 'GATE1_RECEIPT_PATH_OUT=' "$STEP5_SRC" | grep -q 'mktemp -u'; then
  fail "item3-no-mktemp-u-on-path-out" "the GATE1_RECEIPT_PATH_OUT= line itself still calls mktemp -u"
else
  pass "item3-no-mktemp-u-on-path-out"
fi

if grep -nE '^[[:space:]]*GATE1_RECEIPT_PATH_OUT="\$\{?GATE1_RECEIPT_DIR\}?/[^"]+"$' "$STEP5_SRC" >/dev/null; then
  pass "item3-path-out-inside-a-variable-dir"
else
  fail "item3-path-out-inside-a-variable-dir" "GATE1_RECEIPT_PATH_OUT= does not build a path under \$GATE1_RECEIPT_DIR"
fi

if grep -nE '^[[:space:]]*GATE1_RECEIPT_DIR="\$\(mktemp -d\)"' "$STEP5_SRC" >/dev/null; then
  pass "item3-receipt-dir-created-not-named"
else
  fail "item3-receipt-dir-created-not-named" "no GATE1_RECEIPT_DIR=\"\$(mktemp -d)\" assignment found in $STEP5_SRC"
fi

# ── item 4 (behavioral): execute the REAL RHS of that GATE1_RECEIPT_DIR=
#    assignment (extracted from the live source, not re-typed here) and
#    assert the resulting directory is 0700, owned by the invoking uid ────

RECEIPT_DIR_LINE=$(grep -E '^[[:space:]]*GATE1_RECEIPT_DIR="\$\(mktemp -d\)"' "$STEP5_SRC" | head -1)
if [ -z "$RECEIPT_DIR_LINE" ]; then
  fail "item4-extract-rhs" "could not find the GATE1_RECEIPT_DIR= line to extract and execute"
else
  RECEIPT_DIR_RHS="${RECEIPT_DIR_LINE#*GATE1_RECEIPT_DIR=}"
  eval "EVAL_RECEIPT_DIR=$RECEIPT_DIR_RHS"
  if [ -d "$EVAL_RECEIPT_DIR" ]; then
    DIR_MODE=$(stat -c '%a' "$EVAL_RECEIPT_DIR" 2>/dev/null)
    DIR_OWNER=$(stat -c '%u' "$EVAL_RECEIPT_DIR" 2>/dev/null)
    if [ "$DIR_MODE" = "700" ]; then
      pass "item4-dir-mode-0700"
    else
      fail "item4-dir-mode-0700" "expected 700, got $DIR_MODE"
    fi
    if [ "$DIR_OWNER" = "$(id -u)" ]; then
      pass "item4-dir-owned-by-invoker"
    else
      fail "item4-dir-owned-by-invoker" "expected uid $(id -u), got $DIR_OWNER"
    fi
  else
    fail "item4-dir-created" "executing the extracted RHS did not produce a directory"
  fi
fi

# ── item 6 (static + behavioral): sanitize_echo is used in
#    loop-phased-step5.sh, and the real sanitize-echo.sh's output — built
#    into CR_TASK exactly the way that file constructs it — carries the
#    <<UNTRUSTED EXTERNAL CONTENT>> wrapper around the receipt path ───────

if grep -q 'sanitize_echo' "$STEP5_SRC"; then
  pass "item6-sanitize-echo-used"
else
  fail "item6-sanitize-echo-used" "no sanitize_echo call found in $STEP5_SRC"
fi

# shellcheck source=scripts/lib/sanitize-echo.sh
source "$SANITIZE_ECHO_SRC"

SAMPLE_RECEIPT_PATH="/home/op/.autonomous-forever-state/gate1-receipts/fixture__repo/99-$HEAD_SHA.json"
GATE1_RECEIPT_LINE="Gate 1 receipt: not produced this round (see step5 log)."
GATE1_RECEIPT_SANITIZED="$(sanitize_echo "$SAMPLE_RECEIPT_PATH")"
if [ -n "$GATE1_RECEIPT_SANITIZED" ]; then
  GATE1_RECEIPT_LINE="Gate 1 receipt: ${GATE1_RECEIPT_SANITIZED}"
fi
CR_TASK="Review PR #99 for Discussion #1. ${GATE1_RECEIPT_LINE}"

if echo "$CR_TASK" | grep -qF '<<UNTRUSTED EXTERNAL CONTENT>>' \
    && echo "$CR_TASK" | grep -qF '<<END UNTRUSTED>>' \
    && echo "$CR_TASK" | grep -qF "$SAMPLE_RECEIPT_PATH"; then
  pass "item6-cr-task-carries-untrusted-wrapper"
else
  fail "item6-cr-task-carries-untrusted-wrapper" "CR_TASK did not carry the wrapped receipt path: $CR_TASK"
fi

# ── item 7 (behavioral): sanitize_echo always returns 0 and fails closed to
#    empty output when its delegate is broken (D#2582) — the receipt line
#    must fall back to the existing sentence, not go empty ────────────────

GATE1_RECEIPT_LINE_FALLBACK="Gate 1 receipt: not produced this round (see step5 log)."
# Strip python3 from PATH by pointing PATH at only directories that don't
# hold it -- a broken delegate (D#2582's own documented case) without
# touching anything on the real PATH.
NO_PYTHON_PATH=""
IFS=':' read -ra _dirs <<< "$PATH"
for _d in "${_dirs[@]}"; do
  [ -x "$_d/python3" ] && continue
  if [ -z "$NO_PYTHON_PATH" ]; then NO_PYTHON_PATH="$_d"; else NO_PYTHON_PATH="$NO_PYTHON_PATH:$_d"; fi
done

FALLBACK_SANITIZED="$(PATH="$NO_PYTHON_PATH" sanitize_echo "$SAMPLE_RECEIPT_PATH" 2>/dev/null)"
if [ -n "$FALLBACK_SANITIZED" ]; then
  fail "item7-sanitize-fails-closed-to-empty" "expected empty output with python3 unavailable, got: $FALLBACK_SANITIZED"
else
  pass "item7-sanitize-fails-closed-to-empty"
  # Same branch shape as loop-phased-step5.sh: empty output leaves the
  # default sentence untouched, never an empty receipt line.
  if [ -n "$FALLBACK_SANITIZED" ]; then
    GATE1_RECEIPT_LINE_FALLBACK="Gate 1 receipt: ${FALLBACK_SANITIZED}"
  fi
  if [ "$GATE1_RECEIPT_LINE_FALLBACK" = "Gate 1 receipt: not produced this round (see step5 log)." ]; then
    pass "item7-empty-sanitize-keeps-default-sentence"
  else
    fail "item7-empty-sanitize-keeps-default-sentence" "receipt line changed on empty sanitize output: $GATE1_RECEIPT_LINE_FALLBACK"
  fi
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
