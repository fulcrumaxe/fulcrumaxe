#!/usr/bin/env bash
# tests/test_repo_resolve.sh — bash-native unit tests for
# scripts/lib/repo-resolve.sh's whitespace-only handling (D#2536).
#
# This suite exists alongside tests/test_repo_resolve.py and
# tests/test_repo_resolve_planes.py, which already cover the resolvers'
# precedence order end-to-end via pytest+subprocess. This file did not exist
# before D#2536 (see that Discussion's item 10); it is bash-native rather
# than pytest-wrapped so a whitespace-only value can be asserted against
# directly, without a subprocess round-trip, for the two properties D#2536
# is actually about:
#
#   1. A whitespace-only value is treated as absent at every precedence step
#      — it must never be echoed back as a "resolved" slug.
#   2. _require_code_repo, the chokepoint every code-plane call site relies
#      on, must independently reject a whitespace-only value: print nothing
#      on stdout, an actionable message on stderr, and return 1. This is
#      true even though nothing upstream can produce one anymore after (1) —
#      the chokepoint's own guard must not depend on that.
#
# Each test sources repo-resolve.sh directly (no fake-repo tree needed for
# the AUTONOMOUS_TEAM_REPO-only cases; the config.json cases build one under
# a throwaway TMPDIR, mirroring tests/test_repo_resolve_planes.py's _fake_repo
# helper).
#
# Run: bash tests/test_repo_resolve.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/repo-resolve.sh"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# Build a throwaway repo tree with repo-resolve.sh at its real relative path,
# and an optional .autonomous-team/config.json.
_fake_repo() {
  local dir="$1" config_json="${2:-}"
  mkdir -p "$dir/scripts/lib" "$dir/.autonomous-team"
  cp "$LIB" "$dir/scripts/lib/repo-resolve.sh"
  if [[ -n "$config_json" ]]; then
    printf '%s' "$config_json" > "$dir/.autonomous-team/config.json"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- 1. _resolve_repo: whitespace-only AUTONOMOUS_TEAM_REPO, no config.json ---

fake1="$WORK/fake1"
_fake_repo "$fake1"
out="$(cd "$fake1" && AUTONOMOUS_TEAM_REPO='   ' bash -c 'source scripts/lib/repo-resolve.sh; _resolve_repo')"
rc=$?
if [[ "$rc" -ne 0 && -z "$out" ]]; then
  pass "_resolve_repo: whitespace-only AUTONOMOUS_TEAM_REPO fails loudly (rc=$rc, no stdout)"
else
  fail "_resolve_repo: whitespace-only AUTONOMOUS_TEAM_REPO should fail loudly (got rc=$rc out=[$out])"
fi

# --- 2. _resolve_repo: whitespace-only AUTONOMOUS_TEAM_REPO with well-formed config.json still wins ---

fake2="$WORK/fake2"
_fake_repo "$fake2" '{"repo": "acme/widgets"}'
out="$(cd "$fake2" && AUTONOMOUS_TEAM_REPO='   ' bash -c 'source scripts/lib/repo-resolve.sh; _resolve_repo')"
rc=$?
if [[ "$rc" -eq 0 && "$out" == "acme/widgets" ]]; then
  pass "_resolve_repo: config.json \"repo\" still wins over a whitespace-only env var"
else
  fail "_resolve_repo: expected acme/widgets rc=0, got rc=$rc out=[$out]"
fi

# --- 3. _resolve_repo: whitespace-only config.json "repo" falls through to env ---

fake3="$WORK/fake3"
_fake_repo "$fake3" '{"repo": "   "}'
out="$(cd "$fake3" && AUTONOMOUS_TEAM_REPO='org/from-env' bash -c 'source scripts/lib/repo-resolve.sh; _resolve_repo')"
rc=$?
if [[ "$rc" -eq 0 && "$out" == "org/from-env" ]]; then
  pass "_resolve_repo: whitespace-only config.json \"repo\" falls through to env"
else
  fail "_resolve_repo: expected org/from-env rc=0, got rc=$rc out=[$out]"
fi

# --- 4. THE TRAP: an absent discussion_repo is empty-and-fine ---

fake4="$WORK/fake4"
_fake_repo "$fake4" '{}'
out="$(cd "$fake4" && bash -c 'unset AUTONOMOUS_TEAM_REPO; source scripts/lib/repo-resolve.sh; _resolve_discussion_repo')"
rc=$?
if [[ "$rc" -eq 0 && -z "$out" ]]; then
  pass "_resolve_discussion_repo: absent key is empty-and-fine (rc=0, no stdout) — not an error"
else
  fail "_resolve_discussion_repo: absent key should be empty-and-fine, got rc=$rc out=[$out]"
fi

# --- 5. THE TRAP: a whitespace-only discussion_repo is rejected the same way (falls through) ---

fake5="$WORK/fake5"
_fake_repo "$fake5" '{"discussion_repo": "   "}'
out="$(cd "$fake5" && bash -c 'unset AUTONOMOUS_TEAM_REPO; source scripts/lib/repo-resolve.sh; _resolve_discussion_repo')"
rc=$?
if [[ "$rc" -eq 0 && -z "$out" ]]; then
  pass "_resolve_discussion_repo: whitespace-only discussion_repo also resolves empty-and-fine, not as a literal '   '"
else
  fail "_resolve_discussion_repo: whitespace-only discussion_repo mishandled, got rc=$rc out=[$out]"
fi

# --- 6. Absent and whitespace-only stay distinguishable: both fall through to "repo" when it is set ---

fake6="$WORK/fake6"
_fake_repo "$fake6" '{"repo": "acme/legacy"}'
out_absent="$(cd "$fake6" && bash -c 'unset AUTONOMOUS_TEAM_REPO; source scripts/lib/repo-resolve.sh; _resolve_discussion_repo')"
rc_absent=$?
fake6b="$WORK/fake6b"
_fake_repo "$fake6b" '{"repo": "acme/legacy", "discussion_repo": "   "}'
out_ws="$(cd "$fake6b" && bash -c 'unset AUTONOMOUS_TEAM_REPO; source scripts/lib/repo-resolve.sh; _resolve_discussion_repo')"
rc_ws=$?
if [[ "$rc_absent" -eq 0 && "$rc_ws" -eq 0 && "$out_absent" == "acme/legacy" && "$out_ws" == "acme/legacy" ]]; then
  pass "_resolve_discussion_repo: absent and whitespace-only discussion_repo both fall through identically to \"repo\""
else
  fail "_resolve_discussion_repo: fallthrough mismatch (absent=[$out_absent]/$rc_absent, whitespace=[$out_ws]/$rc_ws)"
fi

# --- 7. THE BINDING ITEM: _require_code_repo rejects a whitespace-only resolution ---
#
# Given every upstream gate is now also fixed, _resolve_code_repo can no
# longer produce a whitespace-only value on its own — so testing this
# end-to-end the same way as the other cases would pass even if
# _require_code_repo's own guard at :150 were still the un-trimmed `-z "$r"`
# from before D#2536 (upstream normalization alone would already prevent a
# whitespace-only "$r" from reaching it). D#2536 item 3 is explicit that this
# chokepoint must reject the value independently: "the chokepoint must stop
# being passable" — not "is currently unreachable because something upstream
# happens to filter it first". So this overrides _resolve_code_repo after
# sourcing, to hand _require_code_repo a whitespace-only "$r" directly and
# prove its OWN guard, not its caller's.

fake7="$WORK/fake7"
_fake_repo "$fake7"
out="$(cd "$fake7" && bash -c '
  source scripts/lib/repo-resolve.sh
  _resolve_code_repo() { echo "   "; }
  _require_code_repo ctx
' 2>/dev/null)"
rc=$?
if [[ "$rc" -eq 1 && -z "$out" ]]; then
  pass "_require_code_repo: whitespace-only resolution is refused on its own (rc=1, no stdout)"
else
  fail "_require_code_repo: expected rc=1 no stdout, got rc=$rc out=[$out]"
fi

err="$(cd "$fake7" && bash -c '
  source scripts/lib/repo-resolve.sh
  _resolve_code_repo() { echo "   "; }
  _require_code_repo ctx
' 2>&1 1>/dev/null)"
if echo "$err" | grep -q "could not resolve the code repo"; then
  pass "_require_code_repo: whitespace-only resolution prints the actionable error to stderr"
else
  fail "_require_code_repo: expected actionable stderr message, got [$err]"
fi

# --- 8. _require_code_repo still accepts a well-formed resolution ---

fake8="$WORK/fake8"
_fake_repo "$fake8" '{"code_repo": "acme/public"}'
out="$(cd "$fake8" && bash -c 'unset AUTONOMOUS_TEAM_REPO; source scripts/lib/repo-resolve.sh; _require_code_repo ctx')"
rc=$?
if [[ "$rc" -eq 0 && "$out" == "acme/public" ]]; then
  pass "_require_code_repo: a well-formed resolution still passes through unchanged"
else
  fail "_require_code_repo: expected acme/public rc=0, got rc=$rc out=[$out]"
fi

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
