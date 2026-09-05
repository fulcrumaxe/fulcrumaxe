#!/usr/bin/env bash
# tests/test_post_merge_hook_wiring.sh — proves scripts/post-merge-hook.sh is
# still wired to scripts/lib/auto-pull-step.sh (D#1948).
#
# Run: bash tests/test_post_merge_hook_wiring.sh
# Expects: all assertions pass, exit 0
#
# Why this file exists as well as tests/test_post_merge_hook_pull.sh: moving the
# auto_pull step into a lib makes it testable, and it also creates a brand-new
# way to be wrong. A well-tested lib that the hook no longer calls looks exactly
# as green as one it does call — the same shape of hole as the heredoc copy this
# work removed, one level up. The pull suite proves the lib behaves; this file
# proves the hook is the thing using it.
#
# Grep-based on purpose. It reads the shipping script as text and never runs it:
# post-merge-hook.sh resolves REPO_ROOT from its own location, so executing it
# would act on the operator's checkout. No temp dirs, no network, no API.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/scripts/post-merge-hook.sh"
LIB="$REPO_ROOT/scripts/lib/auto-pull-step.sh"

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi
}

echo "Wiring: hook -> scripts/lib/auto-pull-step.sh"

check "the lib exists" test -f "$LIB"
check "the lib parses" bash -n "$LIB"
check "the lib defines auto_pull_step" grep -qE '^auto_pull_step\(\)' "$LIB"
check "the lib defines the team-log seam the tests override" \
  grep -qE '^auto_pull_step_teamlog\(\)' "$LIB"

check "the hook sources the lib" \
  grep -qE '^source "\$SCRIPT_DIR/lib/auto-pull-step\.sh"' "$HOOK"
check "the hook calls auto_pull_step" \
  grep -qE '(^|[^[:alnum:]_])auto_pull_step[[:space:]]+"' "$HOOK"

# ── The auto_pull region must be step bookkeeping and nothing else ───────────
# The whole `if ! hook_event_has_step "auto_pull"` block, up to its closing `fi`
# at column 0 — not just as far as the mark, because the fatal branch that exits
# without marking sits after it. Comments are stripped first: this is a claim
# about code, and prose that happens to mention a pull should neither fail it
# nor be able to hide anything.
REGION="$(awk '
  /^if ! hook_event_has_step "auto_pull"; then$/ { inside = 1 }
  inside                                         { print }
  inside && /^fi$/                               { exit }
' "$HOOK" | sed -e 's/#.*$//')"

if [[ -n "$REGION" ]]; then
  pass "the auto_pull region is locatable in the hook"
else
  fail "the auto_pull region is locatable in the hook"
fi

# `git` in here means the pull logic has leaked back out of the lib. So does
# `rm`, which is how the pre-D#1911 recovery destroyed repo-root files.
if printf '%s' "$REGION" | grep -qE '(^|[^[:alnum:]_])git($|[^[:alnum:]_])'; then
  fail "the auto_pull region runs no git of its own"
  printf '%s' "$REGION" | grep -nE '(^|[^[:alnum:]_])git($|[^[:alnum:]_])' >&2
else
  pass "the auto_pull region runs no git of its own"
fi

if printf '%s' "$REGION" | grep -qE '(^|[^[:alnum:]_])rm($|[^[:alnum:]_])'; then
  fail "the auto_pull region removes no files"
  printf '%s' "$REGION" | grep -nE '(^|[^[:alnum:]_])rm($|[^[:alnum:]_])' >&2
else
  pass "the auto_pull region removes no files"
fi

# ── The return-code contract is honoured, arm by arm ────────────────────────
# Membership, not presence. These checks used to ask only whether
# `hook_event_mark_step` and `exit 1` appeared *somewhere* in the region, which
# is satisfied just as well by a call site that marks the step on a fatal dirty
# tree and exits on a successful pull. That inversion permanently suppresses the
# retry the whole return contract exists to protect, and the presence-only
# spelling stayed green through it — an assertion that passes regardless of the
# code, which is the exact shape of the heredoc copy this work removed.
# Mutation M5 is that arm swap, and it is what these checks are pinned against.
arm_body() {
  printf '%s\n' "$REGION" | awk -v want="$1" '
    $0 ~ "^[ \t]*" want "\\)[ \t]*$" { inarm = 1; next }
    inarm && /^[ \t]*;;[ \t]*$/      { inarm = 0 }
    inarm                            { print }
  '
}

ARM_OK="$(arm_body 0)"
ARM_FATAL="$(arm_body 2)"
ARM_MISSING="$(arm_body 127)"
ARM_REST="$(arm_body '[*]')"

# assert_arm <label> <arm body> <grep -E pattern> <yes|no>
assert_arm() {
  local label="$1" body="$2" pattern="$3" want="$4" found=no
  printf '%s' "$body" | grep -qE -- "$pattern" && found=yes
  if [[ "$found" == "$want" ]]; then pass "$label"; else fail "$label"; fi
}

check "the 0) arm is non-empty" test -n "$ARM_OK"
check "the 2) arm is non-empty" test -n "$ARM_FATAL"
check "the 127) arm is non-empty" test -n "$ARM_MISSING"

assert_arm "the 0) arm marks the step"           "$ARM_OK"      'hook_event_mark_step "auto_pull"' yes
assert_arm "the 0) arm does not exit"            "$ARM_OK"      '^[[:space:]]*exit ' no
assert_arm "the 2) arm exits"                    "$ARM_FATAL"   '^[[:space:]]*exit 1[[:space:]]*$' yes
assert_arm "the 2) arm does not mark the step"   "$ARM_FATAL"   'hook_event_mark_step' no
assert_arm "the 127) arm does not mark the step" "$ARM_MISSING" 'hook_event_mark_step' no
assert_arm "the *) arm does not mark the step"   "$ARM_REST"    'hook_event_mark_step' no
assert_arm "the *) arm does not exit"            "$ARM_REST"    '^[[:space:]]*exit ' no

check "the region branches on the return code" \
  grep -qE 'AUTO_PULL_RC' <<<"$REGION"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Failures:"
  for e in "${ERRORS[@]}"; do
    echo "  - $e"
  done
  exit 1
fi
echo "PRESUM: pass"
exit 0
