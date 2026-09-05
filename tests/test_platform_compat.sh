#!/usr/bin/env bash
# tests/test_platform_compat.sh — Gate 2 evidence for D#2263 Phase 1 and 2.
#
# scripts/lib/platform-compat.sh and the preflight it adds to
# loop-bootstrap/bootstrap.sh exist to survive a host where `sed -i` behaves
# the BSD (macOS) way instead of the GNU (Linux) way. This suite therefore
# runs bootstrap.sh under a BSD-BEHAVIOUR SED STUB, not against real GNU
# sed — GNU sed on this host would make every one of these sites pass
# whether or not the fix is correct (D#2149), so a stub-free run proves
# nothing about the actual bug.
#
# Stub contract (matches real BSD sed's -i argument-count semantics):
#   sed -i SCRIPT FILE      (GNU form: 2 args after -i)          -> rejected,
#                            non-zero, FILE left untouched. Real BSD sed
#                            would consume SCRIPT as the -i backup suffix
#                            and then try to run FILE itself as a sed
#                            script, which fails because FILE is data.
#   sed -i '' SCRIPT FILE   (BSD form: 3 args after -i, 1st empty) -> the
#                            stub genuinely delegates to the real system
#                            sed (translated to its GNU -i syntax) and
#                            performs the edit for real — a stub that just
#                            exits 0 here would let the rewrite assertions
#                            below pass on a no-op.
#   anything without -i     -> passes through to the real sed unchanged,
#                            so bootstrap.sh's other (non -i) sed calls
#                            keep working under the stub.
#
# A second "broken" stub rejects every -i invocation, GNU or BSD style,
# simulating a host with no in-place sed at all — used for the
# preflight-refusal checks (Spec item 3).
#
# Phase 2 adds a BSD-behaviour STAT STUB and DATE STUB, same idea:
#   stat -c ...             -> rejected non-zero, nothing on stdout — real
#                               BSD stat has no -c flag at all.
#   stat -f %m / %z FILE    -> genuinely delegates to the real system stat
#                               (translated to GNU -c %Y / %s) and returns
#                               the real value.
#   date -d ...              -> rejected non-zero — real BSD date has no -d.
#   date -j -f FMT -v±Nd DATE +OUTFMT -> genuinely delegates to the real
#                               system date (translated to GNU -d "DATE ±N
#                               day"), returns the real computed date.
# The negative controls below reproduce the literal pre-fix
# `stat -c %Y ... || echo 0` and `date -d ...` call shapes directly against
# these stubs, to prove the stub is actually reproducing BSD's rejection
# and not just returning 0/failing for an unrelated reason (D#2149).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM_COMPAT="$REPO_ROOT/scripts/lib/platform-compat.sh"
BOOTSTRAP="$REPO_ROOT/loop-bootstrap/bootstrap.sh"
APPLY_TIERS="$REPO_ROOT/scripts/memory-triage/apply-tiers.sh"
AUTO_PLAN="$REPO_ROOT/scripts/auto-plan.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

REAL_SED="$(command -v sed)"
REAL_STAT="$(command -v stat)"
REAL_DATE="$(command -v date)"
SCRATCH_ROOT="$(mktemp -d)"
cleanup() { rm -rf "$SCRATCH_ROOT"; }
trap cleanup EXIT

make_target() {
  local t="$1"
  rm -rf "$t"
  mkdir -p "$t"
  git -C "$t" init -q
}

echo ""
echo "=== test_platform_compat ==="

# --- Spec check 1: new file, valid bash -------------------------------------
echo ""
echo "--- Spec check 1: scripts/lib/platform-compat.sh exists and is valid bash ---"
if [[ -f "$PLATFORM_COMPAT" ]]; then
  pass "scripts/lib/platform-compat.sh exists"
else
  fail "scripts/lib/platform-compat.sh missing"
fi
if bash -n "$PLATFORM_COMPAT" 2>"$SCRATCH_ROOT/syntax.err"; then
  pass "scripts/lib/platform-compat.sh has valid bash syntax"
else
  fail "scripts/lib/platform-compat.sh has a syntax error: $(cat "$SCRATCH_ROOT/syntax.err")"
fi

# --- Spec check 2: the 7 GNU-form substitutions at 289-295 are gone --------
echo ""
echo "--- Spec check 2: no raw GNU-form 'sed -i \"s|...\"' left in bootstrap.sh ---"
if grep -nE 'sed +-i +.s\|' "$BOOTSTRAP" >"$SCRATCH_ROOT/gnu-sites.out"; then
  fail "raw GNU-form sed -i sites remain: $(cat "$SCRATCH_ROOT/gnu-sites.out")"
else
  pass "no raw GNU-form 'sed -i \"s|...\"' remains in bootstrap.sh"
fi

# --- Build the BSD-behaviour stub sed ---------------------------------------
BSD_STUB_DIR="$SCRATCH_ROOT/bsd-stub"
mkdir -p "$BSD_STUB_DIR"
cat > "$BSD_STUB_DIR/sed" <<EOF
#!/usr/bin/env bash
# Fake sed emulating BSD sed's -i argument-count semantics: BSD's -i takes
# a MANDATORY backup-suffix argument (possibly empty), unlike GNU's, where
# it's optional and attached. A caller that writes GNU-style
# "sed -i SCRIPT FILE" hands BSD's -i the SCRIPT text as that suffix
# argument, then BSD tries to run FILE as the sed program, which fails.
REAL_SED="$REAL_SED"
if [[ "\${1:-}" == "-i" ]]; then
  shift
  if [[ "\$#" -eq 2 && -n "\$1" ]]; then
    echo "sed: BSD -i requires a backup-suffix argument (even if empty) before the script — got a GNU-style invocation" >&2
    exit 1
  elif [[ "\$#" -ge 3 && -z "\$1" ]]; then
    shift
    exec "\$REAL_SED" -i "\$@"
  else
    echo "sed: unrecognized -i invocation in BSD stub: \$*" >&2
    exit 1
  fi
else
  exec "\$REAL_SED" "\$@"
fi
EOF
chmod +x "$BSD_STUB_DIR/sed"

# --- Build the "no usable sed" stub -----------------------------------------
BROKEN_STUB_DIR="$SCRATCH_ROOT/broken-stub"
mkdir -p "$BROKEN_STUB_DIR"
cat > "$BROKEN_STUB_DIR/sed" <<EOF
#!/usr/bin/env bash
# Fake sed rejecting every -i invocation, GNU or BSD style — simulates a
# host with no in-place sed at all (e.g. a stripped-down busybox sed).
REAL_SED="$REAL_SED"
if [[ "\${1:-}" == "-i" ]]; then
  echo "sed: -i not supported by this stub" >&2
  exit 1
else
  exec "\$REAL_SED" "\$@"
fi
EOF
chmod +x "$BROKEN_STUB_DIR/sed"

# --- unit: pc_preflight mode detection under each PATH ----------------------
echo ""
echo "--- unit: pc_preflight detects bsd mode under the BSD stub ---"
(
  PATH="$BSD_STUB_DIR:$PATH"
  # shellcheck source=/dev/null
  source "$PLATFORM_COMPAT"
  if pc_preflight; then
    echo "PC_SED_I_MODE=$PC_SED_I_MODE"
  else
    echo "PC_SED_I_MODE=<refused>"
  fi
) > "$SCRATCH_ROOT/bsd-mode.out" 2>&1 || true
if grep -q '^PC_SED_I_MODE=bsd$' "$SCRATCH_ROOT/bsd-mode.out"; then
  pass "pc_preflight detects bsd mode under the BSD stub"
else
  fail "pc_preflight did not detect bsd mode under the BSD stub: $(cat "$SCRATCH_ROOT/bsd-mode.out")"
fi

echo ""
echo "--- unit: pc_sed_i genuinely executes under the BSD stub (not a no-op) ---"
PROBE_FILE="$SCRATCH_ROOT/probe-exec.txt"
printf 'hello world\n' > "$PROBE_FILE"
(
  PATH="$BSD_STUB_DIR:$PATH"
  source "$PLATFORM_COMPAT"
  pc_preflight >/dev/null
  pc_sed_i "s/hello/goodbye/" "$PROBE_FILE"
)
if [[ "$(cat "$PROBE_FILE")" == "goodbye world" ]]; then
  pass "pc_sed_i under the BSD stub actually rewrote the file"
else
  fail "pc_sed_i under the BSD stub did not rewrite the file (got: $(cat "$PROBE_FILE"))"
fi

echo ""
echo "--- unit: pc_preflight detects gnu mode with real GNU sed (no stub) ---"
(
  source "$PLATFORM_COMPAT"
  pc_preflight >/dev/null
  echo "PC_SED_I_MODE=$PC_SED_I_MODE"
) > "$SCRATCH_ROOT/gnu-mode.out" 2>&1
if grep -q '^PC_SED_I_MODE=gnu$' "$SCRATCH_ROOT/gnu-mode.out"; then
  pass "pc_preflight detects gnu mode with real GNU sed on PATH"
else
  fail "pc_preflight did not detect gnu mode with real sed: $(cat "$SCRATCH_ROOT/gnu-mode.out")"
fi

echo ""
echo "--- unit: pc_preflight refuses under the broken (no usable sed) stub ---"
BROKEN_RC=0
(
  PATH="$BROKEN_STUB_DIR:$PATH"
  source "$PLATFORM_COMPAT"
  pc_preflight
) > "$SCRATCH_ROOT/broken-mode.out" 2>&1 || BROKEN_RC=$?
if [[ "$BROKEN_RC" -ne 0 ]] && grep -qi 'sed' "$SCRATCH_ROOT/broken-mode.out"; then
  pass "pc_preflight returns non-zero and names sed under the broken stub"
else
  fail "pc_preflight did not refuse cleanly under the broken stub (rc=$BROKEN_RC): $(cat "$SCRATCH_ROOT/broken-mode.out")"
fi

# --- Spec check 4: negative control -----------------------------------------
echo ""
echo "--- Spec check 4 (negative control): the pre-fix GNU-form call fails under the BSD stub ---"
# This reproduces the literal invocation loop-bootstrap/bootstrap.sh used
# before D#2263 (confirmed against origin/main's loop-bootstrap/bootstrap.sh
# line 289: `sed -i "s|${SOURCE_REPO}|${TARGET_REPO}|g" "$f"`) directly,
# rather than checking out git history — so this stays meaningful once this
# branch merges and "the base commit" stops being a distinct ref. If this
# check ever PASSES, the stub above has stopped reproducing BSD's refusal
# and no other result in this file is evidence of anything (D#2149).
NEG_FILE="$SCRATCH_ROOT/neg-control.txt"
printf 'old-repo/name\n' > "$NEG_FILE"
NEG_RC=0
PATH="$BSD_STUB_DIR:$PATH" sed -i "s|old-repo/name|new-repo/name|g" "$NEG_FILE" \
  >"$SCRATCH_ROOT/neg-control.out" 2>"$SCRATCH_ROOT/neg-control.err" || NEG_RC=$?
if [[ "$NEG_RC" -ne 0 ]] && [[ "$(cat "$NEG_FILE")" == "old-repo/name" ]]; then
  pass "pre-fix GNU-form 'sed -i EXPR FILE' fails under the BSD stub and leaves the file untouched"
else
  fail "negative control did NOT fail (rc=$NEG_RC, file now: $(cat "$NEG_FILE")) — the BSD stub does not reproduce BSD refusal semantics"
fi

# --- Spec check 3: preflight refuses before any write to $TARGET -----------
echo ""
echo "--- Spec check 3: bootstrap.sh refuses before any write, with no usable sed on PATH ---"
T3="$SCRATCH_ROOT/target-refuse"
make_target "$T3"
T3_RC=0
PATH="$BROKEN_STUB_DIR:$PATH" bash "$BOOTSTRAP" --repo acme/refuse-test "$T3" \
  >"$SCRATCH_ROOT/refuse.out" 2>"$SCRATCH_ROOT/refuse.err" || T3_RC=$?

if [[ "$T3_RC" -ne 0 ]]; then
  pass "bootstrap exits non-zero when no usable sed is on PATH"
else
  fail "bootstrap exited 0 with no usable sed on PATH"
fi
if grep -qi 'sed' "$SCRATCH_ROOT/refuse.err"; then
  pass "stderr names the incompatible utility (sed) in plain language"
else
  fail "stderr does not name sed: $(cat "$SCRATCH_ROOT/refuse.err")"
fi
if ! grep -qi 'Traceback (most recent call last)' "$SCRATCH_ROOT/refuse.err"; then
  pass "stderr is not a stack trace"
else
  fail "stderr looks like a stack trace: $(cat "$SCRATCH_ROOT/refuse.err")"
fi
T3_FILE_COUNT=$(find "$T3" -mindepth 1 -not -path '*/.git' -not -path '*/.git/*' | wc -l | tr -d ' ')
if [[ "$T3_FILE_COUNT" -eq 0 ]]; then
  pass "target directory has zero non-.git files after refusal"
else
  fail "target directory has $T3_FILE_COUNT non-.git files after refusal (expected 0)"
fi
T3_STATUS="$(git -C "$T3" status --porcelain)"
if [[ -z "$T3_STATUS" ]]; then
  pass "git status is clean in the target after refusal"
else
  fail "git status not clean after refusal: $T3_STATUS"
fi

# --- Spec checks 5 & 6: fixed bootstrap under the stub, and with real GNU --
#
# SOURCE_REPO defaults to the pre-rename slug (autonomous-agent-7/
# autonomous-forever) because that's what's literally embedded in most of
# the do_install-reached corpus today (D#1893) -- it is a sed search key,
# not an identity claim (see bootstrap.sh's own comment at SOURCE_REPO's
# definition). rewrite_tree_identifiers is content-gated now (D#2207), not
# extension-gated, so this suite drives the real default SOURCE_REPO
# directly instead of substituting a gap-avoiding literal -- the
# `remaining -eq 0` check below is the permanent regression test for the
# fixture residue D#2207 fixed.
SOURCE_REPO_LITERAL="autonomous-agent-7/autonomous-forever"

run_bootstrap_and_check() {
  local label="$1" path_prefix="$2" target="$3"
  make_target "$target"
  local rc=0
  PATH="${path_prefix:+$path_prefix:}$PATH" \
    bash "$BOOTSTRAP" --repo acme/rewrite-test "$target" \
    >"$SCRATCH_ROOT/${label}.out" 2>"$SCRATCH_ROOT/${label}.err" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    pass "$label: bootstrap exits 0"
  else
    fail "$label: bootstrap exited $rc (see $SCRATCH_ROOT/${label}.err)"
    return
  fi
  local remaining
  remaining=$( (grep -rl "$SOURCE_REPO_LITERAL" \
    "$target/scripts" "$target/backend" "$target/hooks" \
    "$target/.claude/agents" "$target/.claude/commands" 2>/dev/null || true) | wc -l | tr -d ' ')
  if [[ "$remaining" -eq 0 ]]; then
    pass "$label: no remaining references to $SOURCE_REPO_LITERAL in the installed tree"
  else
    fail "$label: $remaining file(s) still reference $SOURCE_REPO_LITERAL"
  fi
  if grep -rl "acme/rewrite-test" "$target/scripts" >/dev/null 2>&1; then
    pass "$label: target's own repo slug (acme/rewrite-test) is present in the installed tree"
  else
    fail "$label: target's own repo slug not found anywhere under scripts/"
  fi
}

echo ""
echo "--- Spec check 5: fixed bootstrap under the BSD stub actually rewrites identifiers ---"
run_bootstrap_and_check "stub-bsd" "$BSD_STUB_DIR" "$SCRATCH_ROOT/target-stub"

echo ""
echo "--- Spec check 6: fixed bootstrap with real GNU tools (no stub) — Linux behaviour unchanged ---"
run_bootstrap_and_check "real-gnu" "" "$SCRATCH_ROOT/target-real"

# --- Spec check 9: --dry-run still exits 0 (entrypoint-smoke) --------------
echo ""
echo "--- Spec check 9: --dry-run still exits 0 ---"
T9="$SCRATCH_ROOT/target-dryrun"
make_target "$T9"
if bash "$BOOTSTRAP" --dry-run --repo acme/demo "$T9" >"$SCRATCH_ROOT/dryrun.out" 2>&1; then
  pass "bootstrap --dry-run exits 0"
else
  fail "bootstrap --dry-run failed: $(cat "$SCRATCH_ROOT/dryrun.out")"
fi

# --- Spec check 8: apply-tiers.sh insert-form is portable -------------------
echo ""
echo "--- Spec check 8: apply-tiers.sh's insert-form patch is portable, no stray backups ---"
AT_SCRATCH="$SCRATCH_ROOT/apply-tiers"
mkdir -p "$AT_SCRATCH"
cp "$APPLY_TIERS" "$AT_SCRATCH/apply-tiers.sh"
cat > "$AT_SCRATCH/sample.md" <<'FIXTURE'
---
tier: 2
---
Fixture content.
FIXTURE

FAKE_HOME="$AT_SCRATCH/fakehome"
REAL_DIR="$FAKE_HOME/.claude/projects/-home-agent-autonomous-forever/memory"
mkdir -p "$REAL_DIR"
cat > "$REAL_DIR/sample.md" <<'REALFILE'
---
name: sample
---
Real memory content, no tier yet.
REALFILE

AT_RC=0
HOME="$FAKE_HOME" bash "$AT_SCRATCH/apply-tiers.sh" >"$SCRATCH_ROOT/apply-tiers.out" 2>&1 || AT_RC=$?
if [[ "$AT_RC" -eq 0 ]]; then
  pass "apply-tiers.sh exits 0"
else
  fail "apply-tiers.sh exited $AT_RC: $(cat "$SCRATCH_ROOT/apply-tiers.out")"
fi

if grep -q '^tier: 2$' "$REAL_DIR/sample.md" 2>/dev/null; then
  pass "patched file contains 'tier: 2'"
else
  fail "patched file missing 'tier: 2': $(cat "$REAL_DIR/sample.md" 2>/dev/null)"
fi

AT_TIER_LINE=$( (grep -n '^tier: 2$' "$REAL_DIR/sample.md" 2>/dev/null || true) | head -1 | cut -d: -f1)
if [[ "$AT_TIER_LINE" == "3" ]]; then
  pass "tier line inserted at the expected position (line 3, just before the closing ---)"
else
  fail "tier line at line '$AT_TIER_LINE', expected line 3"
fi

AT_BAK_COUNT=$(find "$REAL_DIR" -name '*.bak' 2>/dev/null | wc -l | tr -d ' ')
if [[ "$AT_BAK_COUNT" -eq 1 ]]; then
  pass "exactly one .bak file exists (the cp backup) — no extra backup from the patch itself"
else
  fail "$AT_BAK_COUNT .bak files found under $REAL_DIR, expected exactly 1"
fi

AT_TMP_COUNT=$(find "$REAL_DIR" -name '*.tmp' 2>/dev/null | wc -l | tr -d ' ')
if [[ "$AT_TMP_COUNT" -eq 0 ]]; then
  pass "no stray .tmp files left behind by the awk insert"
else
  fail "$AT_TMP_COUNT stray .tmp files found under $REAL_DIR"
fi


# ==============================================================================
# Phase 2 (D#2263): pc_stat_mtime / pc_stat_size / pc_date_offset
# ==============================================================================

# --- Build the BSD-behaviour stub stat ---------------------------------------
cat > "$BSD_STUB_DIR/stat" <<EOF
#!/usr/bin/env bash
# Fake stat emulating BSD stat's flag rejection: real BSD stat has no -c
# option at all (that's GNU-only), so it fails with a non-zero exit and
# nothing useful on stdout — it does NOT print anything that could be
# mistaken for a value. BSD's real mtime/size flag is -f with %m (mtime) /
# %z (size) format specifiers; this stub delegates those to the real
# system stat, translated to its GNU spelling, and genuinely executes it.
REAL_STAT="$REAL_STAT"
if [[ "\${1:-}" == "-c" ]]; then
  echo "stat: illegal option -- c" >&2
  exit 1
elif [[ "\${1:-}" == "-f" ]]; then
  fmt="\$2"
  shift 2
  case "\$fmt" in
    %m) exec "\$REAL_STAT" -c %Y "\$@" ;;
    %z) exec "\$REAL_STAT" -c %s "\$@" ;;
    *)
      echo "stat: unrecognized BSD format in stub: \$fmt" >&2
      exit 1
      ;;
  esac
else
  exec "\$REAL_STAT" "\$@"
fi
EOF
chmod +x "$BSD_STUB_DIR/stat"

# --- Build the BSD-behaviour stub date ---------------------------------------
cat > "$BSD_STUB_DIR/date" <<EOF
#!/usr/bin/env bash
# Fake date emulating BSD date's flag rejection: real BSD date has no -d
# (GNU natural-language parsing) at all. BSD's real offset-arithmetic path
# is "-j -f INFMT -vNd BASE_DATE +OUTFMT" (-j: don't set the system clock,
# -f: parse BASE_DATE per INFMT, -v: adjust by N days). This stub delegates
# that shape to the real system date, translated to GNU's
# "date -d 'BASE_DATE N day' +OUTFMT", and genuinely executes it.
REAL_DATE="$REAL_DATE"
if [[ "\${1:-}" == "-d" ]]; then
  echo "date: illegal option -- d" >&2
  exit 1
fi
if [[ "\${1:-}" == "-j" ]]; then
  shift
  infmt="" adj="" base="" outfmt=""
  while [[ \$# -gt 0 ]]; do
    case "\$1" in
      -f) infmt="\$2"; shift 2 ;;
      -v*) adj="\${1#-v}"; shift ;;
      +*) outfmt="\$1"; shift ;;
      *) base="\$1"; shift ;;
    esac
  done
  days="\${adj%d}"
  if [[ -z "\$base" || -z "\$days" || -z "\$outfmt" ]]; then
    echo "date: unrecognized BSD -j invocation in stub: \$*" >&2
    exit 1
  fi
  exec "\$REAL_DATE" -d "\$base \$days day" "\$outfmt"
fi
exec "\$REAL_DATE" "\$@"
EOF
chmod +x "$BSD_STUB_DIR/date"

# --- Spec check 12/13: enumerate every non-archive 'stat -c' site and its
#     disposition. See the PR body for the full table this backs.
#
# Both checks below exclude this test file itself: it necessarily contains
# the literal patterns it's proving BSD-stub behaviour against (the stub
# script, the negative controls, and the doc comments describing them), the
# same way item 2's own PR-a check was scoped to bootstrap.sh alone rather
# than the whole repo. A production-code grep run for real (e.g. by a
# reviewer) should exclude tests/ for the same reason.
#
# Check 13 looks at a small window around each match rather than just the
# matched line, because two already-portable sites (triage-orphan-diffs.sh,
# loop-watchdog.sh) spell their fallback across a `\`-continued multi-line
# chain — the working BSD/python3 branch is a line or two below the
# 'stat -c' line, not on it. ---------------------------------------------
echo ""
echo "--- Spec check 13: every remaining 'stat -c' hit is inside platform-compat.sh or already carries a working BSD/python3 path ---"
STAT_C_BAD=0
while IFS=: read -r fpath lineno _rest; do
  case "$fpath" in
    */archive/*) continue ;;
    */scripts/lib/platform-compat.sh) continue ;;
    */tests/test_platform_compat.sh) continue ;;
  esac
  wstart=$((lineno > 2 ? lineno - 2 : 1))
  wend=$((lineno + 2))
  window=$(sed -n "${wstart},${wend}p" "$fpath" 2>/dev/null)
  if echo "$window" | grep -qE 'stat -f|python3'; then
    continue
  fi
  echo "  UNRESOLVED: $fpath:$lineno:$_rest"
  STAT_C_BAD=$((STAT_C_BAD + 1))
done < <(grep -rn 'stat -c' --include='*.sh' "$REPO_ROOT")
if [[ "$STAT_C_BAD" -eq 0 ]]; then
  pass "every non-archive, non-test 'stat -c' hit is inside platform-compat.sh or already carries a working BSD/python3 fallback nearby"
else
  fail "$STAT_C_BAD 'stat -c' hit(s) outside platform-compat.sh with no BSD/python3 fallback nearby"
fi

# --- Spec check 14: no silent-zero fallback remains on an mtime read -------
echo ""
echo "--- Spec check 14: no 'stat -c %Y ... || echo 0' silent-zero fallback remains ---"
ZERO_FALLBACK_LINES="$SCRATCH_ROOT/zero-fallback.out"
grep -rn 'stat -c %Y' --include='*.sh' "$REPO_ROOT" | grep -v '/archive/' | grep -v '/tests/test_platform_compat.sh' | grep 'echo 0' > "$ZERO_FALLBACK_LINES" || true
ZERO_FALLBACK_COUNT=$(wc -l < "$ZERO_FALLBACK_LINES" | tr -d ' ')
if [[ "$ZERO_FALLBACK_COUNT" -eq 0 ]]; then
  pass "no 'stat -c %Y ... || echo 0' pattern remains outside archive/ and this test file"
else
  fail "$ZERO_FALLBACK_COUNT line(s) still match the silent-zero pattern: $(cat "$ZERO_FALLBACK_LINES")"
fi

# --- Negative control: pre-fix 'stat -c %Y ... || echo 0' silently returns
#     epoch 0 under the BSD stat stub — reproduces the D#2263 bug itself. ---
echo ""
echo "--- Negative control: pre-fix 'stat -c %Y FILE 2>/dev/null || echo 0' silently returns epoch 0 under the BSD stat stub ---"
NEG_STAT_FILE="$SCRATCH_ROOT/neg-stat.txt"
printf 'x' > "$NEG_STAT_FILE"
NEG_STAT_VAL=$(PATH="$BSD_STUB_DIR:$PATH" bash -c "stat -c %Y '$NEG_STAT_FILE' 2>/dev/null || echo 0")
if [[ "$NEG_STAT_VAL" == "0" ]]; then
  pass "reproduced the bug: literal pre-fix call silently yields epoch 0 under the BSD stat stub"
else
  fail "negative control did not reproduce epoch-0 (got '$NEG_STAT_VAL') — the BSD stat stub does not reproduce BSD's rejection of -c"
fi

# --- Positive: pc_stat_mtime/pc_stat_size under the stub return the REAL
#     value, not epoch 0 / a no-op. ------------------------------------------
echo ""
echo "--- pc_stat_mtime/pc_stat_size under the BSD stat stub return real values ---"
REAL_MTIME=$(stat -c %Y "$NEG_STAT_FILE")
REAL_SIZE=$(stat -c %s "$NEG_STAT_FILE")
(
  PATH="$BSD_STUB_DIR:$PATH"
  # shellcheck source=/dev/null
  source "$PLATFORM_COMPAT"
  pc_stat_mtime "$NEG_STAT_FILE"
) > "$SCRATCH_ROOT/stub-mtime.out" 2>"$SCRATCH_ROOT/stub-mtime.err"
STUB_MTIME="$(cat "$SCRATCH_ROOT/stub-mtime.out")"
if [[ "$STUB_MTIME" == "$REAL_MTIME" ]] && [[ "$STUB_MTIME" != "0" ]]; then
  pass "pc_stat_mtime under the BSD stat stub returns the real mtime ($STUB_MTIME), not epoch 0"
else
  fail "pc_stat_mtime under the BSD stat stub returned '$STUB_MTIME', expected real mtime '$REAL_MTIME' (stderr: $(cat "$SCRATCH_ROOT/stub-mtime.err"))"
fi

(
  PATH="$BSD_STUB_DIR:$PATH"
  source "$PLATFORM_COMPAT"
  pc_stat_size "$NEG_STAT_FILE"
) > "$SCRATCH_ROOT/stub-size.out" 2>"$SCRATCH_ROOT/stub-size.err"
STUB_SIZE="$(cat "$SCRATCH_ROOT/stub-size.out")"
if [[ "$STUB_SIZE" == "$REAL_SIZE" ]]; then
  pass "pc_stat_size under the BSD stat stub returns the real size ($STUB_SIZE)"
else
  fail "pc_stat_size under the BSD stat stub returned '$STUB_SIZE', expected real size '$REAL_SIZE' (stderr: $(cat "$SCRATCH_ROOT/stub-size.err"))"
fi

# --- Positive: pc_stat_mtime with real GNU stat, no stub (Linux unchanged) -
echo ""
echo "--- pc_stat_mtime/pc_stat_size with real GNU stat (no stub) ---"
(
  source "$PLATFORM_COMPAT"
  pc_stat_mtime "$NEG_STAT_FILE"
) > "$SCRATCH_ROOT/real-mtime.out" 2>&1
if [[ "$(cat "$SCRATCH_ROOT/real-mtime.out")" == "$REAL_MTIME" ]]; then
  pass "pc_stat_mtime with real GNU stat returns the correct mtime"
else
  fail "pc_stat_mtime with real GNU stat returned '$(cat "$SCRATCH_ROOT/real-mtime.out")', expected '$REAL_MTIME'"
fi

# --- pc_stat_mtime/pc_stat_size fail (not print 0) on a missing file -------
echo ""
echo "--- pc_stat_mtime/pc_stat_size return non-zero and print nothing for a missing file ---"
MISSING_RC=0
(
  source "$PLATFORM_COMPAT"
  pc_stat_mtime "$SCRATCH_ROOT/does-not-exist-$$"
) > "$SCRATCH_ROOT/missing-mtime.out" 2>/dev/null || MISSING_RC=$?
if [[ "$MISSING_RC" -ne 0 ]] && [[ ! -s "$SCRATCH_ROOT/missing-mtime.out" ]]; then
  pass "pc_stat_mtime on a missing file returns non-zero and prints nothing"
else
  fail "pc_stat_mtime on a missing file: rc=$MISSING_RC, output='$(cat "$SCRATCH_ROOT/missing-mtime.out")'"
fi

# --- Spec check 15: scripts/auto-plan.sh:201 no longer calls 'date -d' -----
echo ""
echo "--- Spec check 15: scripts/auto-plan.sh no longer calls 'date -d' directly, uses pc_date_offset ---"
if grep -q 'date -d' "$AUTO_PLAN"; then
  fail "scripts/auto-plan.sh still contains a direct 'date -d' call"
else
  pass "scripts/auto-plan.sh no longer contains a direct 'date -d' call"
fi
if grep -qE 'pc_date_offset[[:space:]]+"\$DATE"[[:space:]]+-1' "$AUTO_PLAN"; then
  pass "scripts/auto-plan.sh computes YESTERDAY via pc_date_offset"
else
  fail "scripts/auto-plan.sh does not call pc_date_offset as expected"
fi

# --- Negative control: pre-fix 'date -d ...' fails under the BSD date stub -
echo ""
echo "--- Negative control: pre-fix 'date -d \"DATE -1 day\" +FMT' fails under the BSD date stub ---"
NEG_DATE_RC=0
PATH="$BSD_STUB_DIR:$PATH" date -d "2026-03-01 -1 day" '+%Y-%m-%d' \
  >"$SCRATCH_ROOT/neg-date.out" 2>"$SCRATCH_ROOT/neg-date.err" || NEG_DATE_RC=$?
if [[ "$NEG_DATE_RC" -ne 0 ]]; then
  pass "reproduced the bug shape: literal pre-fix 'date -d' call fails under the BSD date stub"
else
  fail "negative control did not fail (date -d succeeded under the BSD date stub, got '$(cat "$SCRATCH_ROOT/neg-date.out")') — the stub does not reproduce BSD's rejection of -d"
fi

# --- Positive: pc_date_offset under the stub and with real GNU date, ------
#     including a month-boundary case (Spec item 15). -----------------------
echo ""
echo "--- pc_date_offset under the BSD date stub computes the correct offset, including a month boundary ---"
(
  PATH="$BSD_STUB_DIR:$PATH"
  source "$PLATFORM_COMPAT"
  pc_date_offset "2026-03-01" -1
) > "$SCRATCH_ROOT/stub-date.out" 2>"$SCRATCH_ROOT/stub-date.err"
if [[ "$(cat "$SCRATCH_ROOT/stub-date.out")" == "2026-02-28" ]]; then
  pass "pc_date_offset under the BSD date stub: 2026-03-01 minus 1 day = 2026-02-28"
else
  fail "pc_date_offset under the BSD date stub gave '$(cat "$SCRATCH_ROOT/stub-date.out")', expected 2026-02-28 (stderr: $(cat "$SCRATCH_ROOT/stub-date.err"))"
fi

echo ""
echo "--- pc_date_offset with real GNU date (no stub): 2026-03-01 minus 1 day = 2026-02-28 ---"
(
  source "$PLATFORM_COMPAT"
  pc_date_offset "2026-03-01" -1
) > "$SCRATCH_ROOT/real-date.out" 2>&1
if [[ "$(cat "$SCRATCH_ROOT/real-date.out")" == "2026-02-28" ]]; then
  pass "pc_date_offset with real GNU date computes the correct cross-month offset"
else
  fail "pc_date_offset with real GNU date gave '$(cat "$SCRATCH_ROOT/real-date.out")', expected 2026-02-28"
fi

# --- Spec check 16/18: no archive/ or .github/ changes in this PR's diff ---
echo ""
echo "--- Spec check 16/18: this PR's diff touches no archive/ path and no .github/ path ---"
if git -C "$REPO_ROOT" rev-parse --verify origin/main >/dev/null 2>&1; then
  DIFF_FILES="$SCRATCH_ROOT/diff-files.out"
  git -C "$REPO_ROOT" diff --name-only origin/main...HEAD > "$DIFF_FILES" 2>/dev/null || true
  ARCHIVE_HITS=$(grep -c '^archive/' "$DIFF_FILES" || true)
  GITHUB_HITS=$(grep -c '^\.github/' "$DIFF_FILES" || true)
  if [[ "$ARCHIVE_HITS" -eq 0 ]]; then
    pass "diff touches no archive/ path"
  else
    fail "diff touches $ARCHIVE_HITS archive/ path(s)"
  fi
  if [[ "$GITHUB_HITS" -eq 0 ]]; then
    pass "diff touches no .github/ path"
  else
    fail "diff touches $GITHUB_HITS .github/ path(s)"
  fi
else
  echo "  SKIP: origin/main not available in this checkout — cannot diff (not a failure of the code under test)"
fi

# --- Summary -----------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
