#!/usr/bin/env bash
# tests/test_spec_ready_gate.sh — fixture suite for scripts/lib/spec-ready-gate.sh (D#1798).
#
# Sources the real gate function (not a paraphrase — scripts/spawn-agent.sh is on the
# PreToolUse forbidden-command list, so this library is the only way to exercise the
# actual gate code) and drives it over nine fixtures A-I. Each fixture's expected
# outcome is the rightmost ("first-line + extract_status()") column of the table in
# D#1798's Implementation Notes.
#
# Also runs the same nine fixtures through OLD_BUG_check(), a byte-for-byte replica of
# the pre-fix substring grep that lived inline in scripts/spawn-agent.sh (see D#1798's
# Discussion body). This is the negative-control comparison the Spec's criterion 3 asks
# for: B, D, E, H, I must flip from OPEN under the old logic to BLOCK under the new one;
# A, C, F, G must be unaffected. spawn-agent.sh itself cannot be executed to produce this
# comparison directly (forbidden-command list), and it is also a brand-new file with no
# git history to check out a "pre-fix" version of — OLD_BUG_check is the literal old
# regex, inlined here for comparison only, never used by production code.
#
# Usage: bash tests/test_spec_ready_gate.sh
# Exit 0 = all assertions passed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/spec-ready-gate.sh
source "$REPO_ROOT/scripts/lib/spec-ready-gate.sh"

PASS=0
FAIL=0
_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Literal replica of the pre-fix inline check from scripts/spawn-agent.sh:294
# (grep -qE 'STATUS:\s*SPEC_READY' <<< "$DISC_BODY"). Comparison-only, not production code.
OLD_BUG_check() {
  local body="$1"
  grep -qE 'STATUS:\s*SPEC_READY' <<< "$body"
}

# ── Fixtures A-I ──────────────────────────────────────────────────────────────
FIXTURE_A=$(cat <<'EOF'
<!-- STATUS:SPEC_READY SINCE:2026-07-29T00:00:00Z -->

## A real Spec

This body genuinely carries the marker on line 1.
EOF
)

FIXTURE_B=$(cat <<'EOF'
<!-- STATUS:NEEDS_REVISION SINCE:2026-07-29T00:00:00Z -->

## PM rejection note

Rejected because it claimed STATUS: SPEC_READY prematurely.
EOF
)

FIXTURE_C=$(cat <<'EOF'
<!-- STATUS:DRAFT SINCE:2026-07-29T00:00:00Z -->

## Reproducing the gate bug

```
if ! grep -qE 'STATUS:\s*SPEC_READY' <<< "$DISC_BODY"; then
```
EOF
)

FIXTURE_D=$(cat <<'EOF'
<!-- STATUS:DRAFT SINCE:2026-07-29T00:00:00Z -->

## Update

<!-- STATUS:SPEC_READY SINCE:2026-07-30T00:00:00Z -->

Body now ready (duplicate marker further down).
EOF
)

FIXTURE_E=$(cat <<'EOF'
<!-- STATUS:DRAFT SINCE:2026-07-29T00:00:00Z -->

## Notes

> Someone quoted: STATUS: SPEC_READY was claimed earlier but reverted.
EOF
)

FIXTURE_F=$(cat <<'EOF'
## What

Just a plain discussion with no status marker at all.
EOF
)

FIXTURE_G=$(cat <<'EOF'
<!-- STATUS:DONE SINCE:2026-07-29T00:00:00Z -->

## Merged

This is already done.
EOF
)

FIXTURE_H=$(cat <<'EOF'
## Acceptance criteria

Example marker to test against, quoted for illustration:

```
<!-- STATUS:SPEC_READY -->
```

No real marker anywhere else in this body.
EOF
)

FIXTURE_I=$(cat <<'EOF'
## Status update

STATUS: SPEC_READY as discussed in the meeting, no HTML comment form used.
EOF
)

# name : body-var : expected (OPEN|BLOCK) : old-bug-expected (OPEN|BLOCK)
CASES="
A FIXTURE_A OPEN OPEN
B FIXTURE_B BLOCK OPEN
C FIXTURE_C BLOCK BLOCK
D FIXTURE_D BLOCK OPEN
E FIXTURE_E BLOCK OPEN
F FIXTURE_F BLOCK BLOCK
G FIXTURE_G BLOCK BLOCK
H FIXTURE_H BLOCK OPEN
I FIXTURE_I BLOCK OPEN
"

echo "== New gate (spec_ready_gate_check) vs Implementation Notes table =="
FLIPPED=""
while read -r name varname expected old_expected; do
  [ -z "$name" ] && continue
  body="${!varname}"

  if spec_ready_gate_check "$body" >/dev/null 2>&1; then
    actual="OPEN"
  else
    actual="BLOCK"
  fi

  if [ "$actual" = "$expected" ]; then
    _pass "fixture $name: new gate -> $actual (expected $expected)"
  else
    _fail "fixture $name: new gate -> $actual (expected $expected)"
  fi

  if OLD_BUG_check "$body"; then
    old_actual="OPEN"
  else
    old_actual="BLOCK"
  fi

  if [ "$old_actual" = "$old_expected" ]; then
    _pass "fixture $name: old bug -> $old_actual (expected $old_expected)"
  else
    _fail "fixture $name: old bug -> $old_actual (expected $old_expected)"
  fi

  if [ "$old_actual" != "$actual" ]; then
    FLIPPED="$FLIPPED $name"
  fi
done <<< "$CASES"

echo ""
echo "== Negative control (Spec criterion 3) =="
echo "Fixtures that flip OPEN(old) -> BLOCK(new):$FLIPPED"
if [ "$(echo "$FLIPPED" | xargs -n1 | sort | xargs)" = "B D E H I" ]; then
  _pass "flip set is exactly {B, D, E, H, I}"
else
  _fail "flip set was '$FLIPPED', expected exactly B D E H I"
fi

# ── Message-shape checks (D#1778 lesson: don't misdirect the error) ───────────
echo ""
echo "== Message shape =="
DONE_MSG=$(spec_ready_gate_check "$FIXTURE_G" 42 2>&1 >/dev/null)
if [[ "$DONE_MSG" == *"status is DONE"* && "$DONE_MSG" == *"already complete"* ]]; then
  _pass "DONE fixture message states status + already-complete"
else
  _fail "DONE fixture message wrong: $DONE_MSG"
fi

UNKNOWN_MSG=$(spec_ready_gate_check "$FIXTURE_F" 42 2>&1 >/dev/null)
if [[ "$UNKNOWN_MSG" == *"no authoritative STATUS marker"* ]]; then
  _pass "no-marker fixture message states marker is absent"
else
  _fail "no-marker fixture message wrong (must say marker absent, not 'not SPEC_READY'): $UNKNOWN_MSG"
fi
if [[ "$UNKNOWN_MSG" == *"is not SPEC_READY"* ]]; then
  _fail "no-marker fixture message wrongly asserts 'not SPEC_READY' instead of marker-absent"
fi

# ── BLOCKED-BY fixtures (D#1755) ─────────────────────────────────────────────
# Only the two network-free cases live here on purpose: a malformed ref and a
# prose-only mention both short-circuit before any ref resolution, so these stay
# deterministic offline. Ref resolution itself (PR/Discussion states, not-found,
# timeout, batching) is covered against mocks in
# backend/tests/test_discussion_status.py.
echo ""
echo "== BLOCKED-BY gate (D#1755) =="

BB_MALFORMED=$(cat <<'EOF'
<!-- STATUS:SPEC_READY BLOCKED-BY:banana SINCE:2026-07-29T00:00:00Z -->

Spec is finished; the ref is unparseable, so this must fail closed.
EOF
)

BB_PROSE_ONLY=$(cat <<'EOF'
<!-- STATUS:SPEC_READY SINCE:2026-07-29T00:00:00Z -->

The PM considered writing BLOCKED-BY:#1691 here, but prose is not a constraint.
EOF
)

BB_MSG=$(spec_ready_gate_check "$BB_MALFORMED" 42 2>&1 >/dev/null)
if [ -n "$BB_MSG" ]; then
  _pass "malformed BLOCKED-BY ref blocks the spawn (fail closed)"
else
  _fail "malformed BLOCKED-BY ref did NOT block the spawn"
fi
if [[ "$BB_MSG" == *"BLOCKED-BY"* && "$BB_MSG" == *"banana"* ]]; then
  _pass "blocked message names BLOCKED-BY and the offending ref"
else
  _fail "blocked message must name BLOCKED-BY and the ref: $BB_MSG"
fi

if spec_ready_gate_check "$BB_PROSE_ONLY" 42 2>/dev/null; then
  _pass "BLOCKED-BY in prose only does not block (not on the authoritative line)"
else
  _fail "BLOCKED-BY in prose wrongly blocked the spawn"
fi

if spec_ready_gate_check "$FIXTURE_A" 42 2>/dev/null; then
  _pass "backward compatible: no BLOCKED-BY still opens the gate"
else
  _fail "regression: a body with no BLOCKED-BY was blocked"
fi

# ── A field that is PRESENT must never read as absent ────────────────────────
# The first version of the parser used one regex for both presence and value,
# so each of these opened the gate silently. They stay network-free on purpose:
# a malformed ref (and an empty value) is rejected before any ref resolution,
# so the whole block runs offline like the rest of this suite.
_gate_must_block() {
  local label="$1" body="$2" want="$3"
  local msg
  msg=$(spec_ready_gate_check "$body" 42 2>&1 >/dev/null)
  if [ -z "$msg" ]; then
    _fail "$label: gate ALLOWED — a present BLOCKED-BY read as absent"
    return
  fi
  if [[ "$msg" == *"$want"* ]]; then
    _pass "$label: gate blocks and says why"
  else
    _fail "$label: blocked, but the message never mentions '$want': $msg"
  fi
}

_gate_must_block "space after the colon" \
  "$(printf '<!-- STATUS:SPEC_READY BLOCKED-BY: banana SINCE:2026-07-29T00:00:00Z -->\n\nSpec.\n')" \
  "banana"

_gate_must_block "lowercase field name" \
  "$(printf '<!-- STATUS:SPEC_READY blocked-by:banana SINCE:2026-07-29T00:00:00Z -->\n\nSpec.\n')" \
  "banana"

_gate_must_block "present but empty value" \
  "$(printf '<!-- STATUS:SPEC_READY BLOCKED-BY: SINCE:2026-07-29T00:00:00Z -->\n\nSpec.\n')" \
  "BLOCKED-BY"

_gate_must_block "space-separated refs are not silently truncated" \
  "$(printf '<!-- STATUS:SPEC_READY BLOCKED-BY:banana D#1746 SINCE:2026-07-29T00:00:00Z -->\n\nSpec.\n')" \
  "D#1746"

echo ""
echo "== Summary: $PASS passed, $FAIL failed =="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
