#!/usr/bin/env bash
# tests/test_pr_plane.sh — unit tests for scripts/lib/pr-plane.sh (D#2563)
#
# Run: bash tests/test_pr_plane.sh   (expects exit 0)
#
# Everything runs against a hermetic fixture: a copied scripts/lib/{repo-
# resolve.sh,pr-plane.sh} plus a synthetic .autonomous-team/config.json with
# distinct code_repo / repo (Discussion) slugs, and a stub `gh` on PATH that
# answers `gh api repos/<repo>/pulls/<N>` from env vars — same PATH-prepend +
# args-aware-stub convention as tests/test_merge_and_hook.sh.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_REPO_RESOLVE="$REPO_ROOT/scripts/lib/repo-resolve.sh"
REAL_PR_PLANE="$REPO_ROOT/scripts/lib/pr-plane.sh"
REAL_MERGE_AND_HOOK="$REPO_ROOT/scripts/merge-and-hook.sh"
# shellcheck source=tests/lib/script-fixture.sh
source "$REPO_ROOT/tests/lib/script-fixture.sh"

PASS=0
FAIL=0

ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; shift; [ $# -gt 0 ] && echo "        $*"; FAIL=$((FAIL + 1)); }
assert_rc() {
  if [ "$3" -eq "$2" ]; then ok "$1 (exit $3)"; else bad "$1" "expected exit $2, got $3"; fi
}
assert_nonzero() {
  if [ "$2" -ne 0 ]; then ok "$1 (exit $2)"; else bad "$1" "expected non-zero exit, got 0"; fi
}
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain: $2 — got: $3"; fi
}
assert_empty() {
  if [ -z "$2" ]; then ok "$1"; else bad "$1" "expected empty, got: $2"; fi
}
assert_not() { local l="$1"; shift; if "$@"; then bad "$l" "expected false: $*"; else ok "$l"; fi; }

# ── Fixture: a repo root with its own config.json + stub gh ─────────────────
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/scripts/lib" "$WORK/.autonomous-team" "$WORK/bin"
cp "$REAL_REPO_RESOLVE" "$WORK/scripts/lib/repo-resolve.sh"
cp "$REAL_PR_PLANE" "$WORK/scripts/lib/pr-plane.sh"

CODE_REPO_FIXTURE="fulcrumaxe/fulcrumaxe-fixture"
DISC_REPO_FIXTURE="autonomous-agent-7/fulcrumaxe-fixture"

cat > "$WORK/.autonomous-team/config.json" <<EOF
{
  "repo": "$DISC_REPO_FIXTURE",
  "code_repo": "$CODE_REPO_FIXTURE"
}
EOF

# Stub gh: answers `gh api repos/<repo>/pulls/<N> --jq <expr>` from env vars
# named after the repo slug (slashes/dots/dashes -> underscores) so a single
# stub serves every test case below without per-case rewriting. Every call is
# appended to GH_CALL_LOG (when set) so a test can assert gh was never
# invoked at all (AC-1b, AC-8).
cat > "$WORK/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
if [[ -n "${GH_CALL_LOG:-}" ]]; then
  echo "$*" >> "$GH_CALL_LOG"
fi
if [[ "$1" == "api" ]]; then
  APIPATH="$2"
  REPO="$(printf '%s' "$APIPATH" | sed -E 's#^repos/(.*)/pulls/[0-9]+$#\1#')"
  N="$(printf '%s' "$APIPATH" | sed -E 's#.*/pulls/([0-9]+)$#\1#')"
  KEY="$(printf '%s' "$REPO" | tr './-' '___')"
  HIT_VAR="PRP_HIT_${KEY}"
  SHA_VAR="PRP_SHA_${KEY}"
  JQEXPR=""
  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --jq) JQEXPR="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ "${!HIT_VAR:-0}" != "1" ]]; then
    exit 1
  fi
  case "$JQEXPR" in
    .number)   echo "$N" ;;
    .head.sha) echo "${!SHA_VAR:-deadbeefsha}" ;;
    *)         echo "" ;;
  esac
  exit 0
fi
echo "unstubbed gh call: $*" >&2
exit 1
GHEOF
chmod +x "$WORK/bin/gh"

run_resolve() {
  # run_resolve <pr> <plane_arg> — invokes pr_plane_resolve in a hermetic
  # subshell (its own repo-resolve.sh/pr-plane.sh, its own PATH) and prints
  # "rc<TAB>name<TAB>repo" on FD 3 so stderr/stdout stay clean for the
  # per-test assertions below.
  local pr="$1" plane="$2"
  env PATH="$WORK/bin:$PATH" \
    bash -c '
      source "'"$WORK"'/scripts/lib/pr-plane.sh"
      pr_plane_resolve "$1" "$2"
      printf "%s\t%s\t%s\n" "$?" "$PR_PLANE_NAME" "$PR_PLANE_REPO"
    ' _ "$pr" "$plane"
}

echo "=== AC-1(a): accepts only the plane NAMES code/discussion ==="
export PRP_HIT_fulcrumaxe_fulcrumaxe_fixture=1
export PRP_HIT_autonomous_agent_7_fulcrumaxe_fixture=0
OUT_A1="$(run_resolve 100 code 2>err.log)"
ERR_A1="$(cat err.log 2>/dev/null)"; rm -f err.log
RC_A1="$(printf '%s' "$OUT_A1" | cut -f1)"
NAME_A1="$(printf '%s' "$OUT_A1" | cut -f2)"
REPO_A1="$(printf '%s' "$OUT_A1" | cut -f3)"
assert_rc "explicit --plane code resolves" 0 "$RC_A1"
assert_contains "PR_PLANE_NAME is 'code'" "code" "$NAME_A1"
assert_contains "PR_PLANE_REPO is the code fixture slug" "$CODE_REPO_FIXTURE" "$REPO_A1"

export PRP_HIT_fulcrumaxe_fulcrumaxe_fixture=0
export PRP_HIT_autonomous_agent_7_fulcrumaxe_fixture=1
OUT_A2="$(run_resolve 101 discussion 2>err.log)"
rm -f err.log
RC_A2="$(printf '%s' "$OUT_A2" | cut -f1)"
NAME_A2="$(printf '%s' "$OUT_A2" | cut -f2)"
REPO_A2="$(printf '%s' "$OUT_A2" | cut -f3)"
assert_rc "explicit --plane discussion resolves" 0 "$RC_A2"
assert_contains "PR_PLANE_NAME is 'discussion'" "discussion" "$NAME_A2"
assert_contains "PR_PLANE_REPO is the discussion fixture slug" "$DISC_REPO_FIXTURE" "$REPO_A2"

echo "=== AC-1(b): rejects a free-form slug outright, nothing on stdout, gh never called ==="
CALL_LOG="$WORK/gh-calls-b.log"
: > "$CALL_LOG"
export GH_CALL_LOG="$CALL_LOG"
OUT_B="$(env GH_CALL_LOG="$CALL_LOG" PATH="$WORK/bin:$PATH" bash -c '
  source "'"$WORK"'/scripts/lib/pr-plane.sh"
  pr_plane_resolve 102 "some/other-repo"
  echo "RC=$?"
' 2>err.log)"
ERR_B="$(cat err.log 2>/dev/null)"; rm -f err.log
unset GH_CALL_LOG
RC_B="$(printf '%s' "$OUT_B" | grep -oE 'RC=[0-9]+' | cut -d= -f2)"
STDOUT_BEFORE_RC="$(printf '%s' "$OUT_B" | grep -v '^RC=')"
assert_nonzero "free-form slug 'some/other-repo' is rejected" "${RC_B:-1}"
assert_empty "nothing on stdout besides our own RC= marker" "$STDOUT_BEFORE_RC"
assert_contains "stderr says a slug is never accepted" "never accepted" "$ERR_B"
assert_empty "gh was never invoked" "$(cat "$CALL_LOG")"

echo "=== AC-1(c): refuses, naming both slugs + remedy, when the PR exists on BOTH planes ==="
export PRP_HIT_fulcrumaxe_fulcrumaxe_fixture=1
export PRP_HIT_autonomous_agent_7_fulcrumaxe_fixture=1
export PRP_SHA_fulcrumaxe_fulcrumaxe_fixture=codeSHA111
export PRP_SHA_autonomous_agent_7_fulcrumaxe_fixture=discSHA222
OUT_C="$(run_resolve 103 "" 2>err.log)"
ERR_C="$(cat err.log 2>/dev/null)"; rm -f err.log
RC_C="$(printf '%s' "$OUT_C" | cut -f1)"
assert_nonzero "ambiguous PR (both planes) is refused" "$RC_C"
assert_contains "names the code slug" "$CODE_REPO_FIXTURE" "$ERR_C"
assert_contains "names the discussion slug" "$DISC_REPO_FIXTURE" "$ERR_C"
assert_contains "names the code head sha" "codeSHA111" "$ERR_C"
assert_contains "names the discussion head sha" "discSHA222" "$ERR_C"
assert_contains "names the --plane remedy" "--plane" "$ERR_C"

echo "=== AC-1(d): refuses when the PR exists on NEITHER plane ==="
export PRP_HIT_fulcrumaxe_fulcrumaxe_fixture=0
export PRP_HIT_autonomous_agent_7_fulcrumaxe_fixture=0
OUT_D="$(run_resolve 104 "" 2>err.log)"
ERR_D="$(cat err.log 2>/dev/null)"; rm -f err.log
RC_D="$(printf '%s' "$OUT_D" | cut -f1)"
assert_nonzero "PR on neither plane is refused" "$RC_D"
assert_contains "reason names 'either plane'" "either plane" "$ERR_D"

echo "=== unambiguous probe: exactly one plane has it, no --plane needed ==="
export PRP_HIT_fulcrumaxe_fulcrumaxe_fixture=1
export PRP_HIT_autonomous_agent_7_fulcrumaxe_fixture=0
OUT_E="$(run_resolve 105 "" 2>err.log)"
rm -f err.log
RC_E="$(printf '%s' "$OUT_E" | cut -f1)"
NAME_E="$(printf '%s' "$OUT_E" | cut -f2)"
assert_rc "unambiguous probe resolves without --plane" 0 "$RC_E"
assert_contains "resolves to 'code' (the only plane that had it)" "code" "$NAME_E"

unset PRP_HIT_fulcrumaxe_fulcrumaxe_fixture PRP_HIT_autonomous_agent_7_fulcrumaxe_fixture
unset PRP_SHA_fulcrumaxe_fulcrumaxe_fixture PRP_SHA_autonomous_agent_7_fulcrumaxe_fixture

echo "=== AC-8: an empty resolved plane aborts merge-and-hook.sh BEFORE gh runs ==="
# Build a minimal copy of merge-and-hook.sh's own directory shape so its
# SCRIPT_DIR-relative `source` lines resolve, mirroring
# tests/test_merge_and_hook.sh's setup_stubs() convention.
#
# stage_script_with_libs (tests/lib/script-fixture.sh, D#2163), not a
# hand-maintained cp list: merge-and-hook.sh runs under `set -euo pipefail`,
# so a SOURCED lib this fixture forgot to stage doesn't just leave a function
# undefined — the bare `source` line itself fails and `-e` kills the script
# right there, before the plane guard this test exists to exercise ever
# runs. A hand-maintained list missing even one lib (this one missed
# scripts/lib/merge-gate-labels.sh, added by a later merge-gate feature)
# produces a fixture that dies early and still reports a mostly-passing
# suite — the exact silent-vacuous-pass shape this test is supposed to be
# proof against. Transitive staging can't go stale the same way: it reads
# the real script's own `source` lines, so a newly added lib is picked up
# automatically.
mkdir -p "$WORK/mh/logs" "$WORK/mh/state" "$WORK/mh/bin"
stage_script_with_libs "$REPO_ROOT" "merge-and-hook.sh" "$WORK/mh/scripts"

SENTINEL="$WORK/mh/sentinel-gh-ran"
rm -f "$SENTINEL"
cat > "$WORK/mh/bin/gh" <<GHEOF2
#!/usr/bin/env bash
touch "$SENTINEL"
echo "GH SHOULD NEVER HAVE RUN: \$*" >&2
exit 0
GHEOF2
chmod +x "$WORK/mh/bin/gh"

AC8_OUT=$(env PATH="$WORK/mh/bin:$PATH" \
  AUTONOMOUS_TEAM_REPO="autonomous-agent-7/fulcrumaxe" \
  AUTONOMOUS_TEAM_STATE_DIR="$WORK/mh/state" \
  MERGE_AND_HOOK_LOG_DIR="$WORK/mh/logs" \
  PR_PLANE_RESOLVE_OVERRIDE_NAME="code" \
  PR_PLANE_RESOLVE_OVERRIDE_REPO="" \
  bash "$WORK/mh/scripts/merge-and-hook.sh" --pr 999999 2>&1)
AC8_RC=$?
assert_nonzero "merge-and-hook.sh aborts on an empty resolved plane" "$AC8_RC"
assert_contains "abort reason names the unresolved-plane guard" "plane unresolved" "$AC8_OUT"
assert_not "the gh sentinel was NEVER created — gh never ran" test -e "$SENTINEL"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
