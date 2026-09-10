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

# ── D#2372: cost_comment / completion_block / stats_metrics targeting ────────
# Unlike everything above, this section actually RUNS the hook (hermetically
# — see tests/lib/script-fixture.sh) against a stubbed multi-Discussion PR
# shaped exactly like the real PR #2224 (three "Closes D#" references,
# ascending), with every outbound `gh` call captured at the argv boundary. It
# proves cost_comment/completion_block/stats_metrics target the FIRST
# Discussion (matching the `DISCUSSIONS[0]` assignment and its comment above
# the loop), not the LAST one the discussion_close loop happens to finish on.
#
# D#2369: the needle checked below (`--repo`) starts with a dash.
# `assert_not_contains` is vacuous for exactly that shape in some suites —
# `grep -qF -- "$needle"` (the `--` before the needle) is what keeps this one
# honest, and the self-test right after proves it.
echo ""
echo "Discussion targeting (cost_comment / completion_block / stats_metrics)"

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tests/lib/script-fixture.sh
source "$TESTS_DIR/lib/script-fixture.sh"

assert_log_contains() {
  local label="$1" needle="$2" logfile="$3"
  if grep -qF -- "$needle" "$logfile"; then
    pass "$label"
  else
    fail "$label — expected a captured gh call containing: $needle"
  fi
}

assert_log_absent() {
  local label="$1" needle="$2" logfile="$3"
  if grep -qF -- "$needle" "$logfile"; then
    fail "$label — unexpectedly found in captured gh calls: $needle"
  else
    pass "$label"
  fi
}

# Self-test: prove grep -qF -- "--repo" actually DETECTS the flag, rather
# than passing vacuously the way a bare `assert_not_contains` would for a
# dash-prefixed needle (D#2369). If this ever stops detecting it, the
# absence assertions below would be silently meaningless.
if grep -qF -- "--repo" <<<"gh api graphql -f query=x --repo owner/name"; then
  pass "selftest: grep -qF -- '--repo' detects the flag when present (not vacuous, D#2369)"
else
  fail "selftest: grep -qF -- '--repo' failed to detect '--repo' in a haystack that has it — vacuous assertion, do not trust the assertions below"
fi

# run_hook_fixture <hook_src> <out_dir>
# Stages <hook_src> as the shipping post-merge-hook.sh (with its real
# scripts/lib/*.sh deps) into a fresh hermetic fixture, stubs `gh`,
# cost_tracker.py and cost_formatter.py, and runs it once against a PR
# closing three Discussions: 4001 (first), 4002, 4003 (last). Writes
# <out_dir>/gh-calls.txt (every outbound gh invocation, argv boundary),
# <out_dir>/output.txt and <out_dir>/rc.
run_hook_fixture() {
  local hook_src="$1" out_dir="$2"
  local tmpdir stageroot

  tmpdir="$(mktemp -d)"
  git -C "$tmpdir" init -q
  git -C "$tmpdir" symbolic-ref HEAD refs/heads/main
  git -C "$tmpdir" config user.email "test@test.com"
  git -C "$tmpdir" config user.name "Test"
  mkdir -p "$tmpdir/.autonomous-team/hook-events" "$tmpdir/scripts/lib" "$tmpdir/backend"

  # stage_script_with_libs copies "post-merge-hook.sh" plus the libs it
  # sources, by name, from <repo_root>/scripts — so the candidate hook (real
  # or mutated) has to be staged under that literal name first.
  stageroot="$(mktemp -d)"
  mkdir -p "$stageroot/scripts"
  cp "$hook_src" "$stageroot/scripts/post-merge-hook.sh"
  cp -r "$TESTS_DIR/../scripts/lib" "$stageroot/scripts/lib"
  stage_script_with_libs "$stageroot" "post-merge-hook.sh" "$tmpdir/scripts"
  rm -rf "$stageroot"

  local s
  for s in rotate-team-log.sh post-merge-wiki.sh agent-feed-append.sh; do
    printf '#!/usr/bin/env bash\nexit 0\n' > "$tmpdir/scripts/$s"
    chmod +x "$tmpdir/scripts/$s"
  done
  cat > "$tmpdir/scripts/lib/worktree-registry.sh" <<'SH'
#!/usr/bin/env bash
worktree_registry() { return 0; }
SH
  chmod +x "$tmpdir/scripts/lib/worktree-registry.sh"

  : > "$tmpdir/backend/__init__.py"
  cat > "$tmpdir/backend/lessons.py" <<'PY'
class LessonsStore:
    def record(self, **kwargs): pass
PY
  cat > "$tmpdir/backend/blackboard.py" <<'PY'
def get_blackboard():
    class BB:
        def get(self, key): return None
    return BB()
PY

  # Cost > 0 ONLY for Discussion 4001 (the intended FIRST/target Discussion).
  # A cost comment posting at all is itself evidence the right Discussion was
  # resolved — if the leaked-loop-var bug is present, $DISCUSSION is 4003 at
  # this point, total_cost is 0 for 4003, and cost_comment silently skips.
  cat > "$tmpdir/backend/cost_tracker.py" <<'PY'
import sys
if "--discussion" in sys.argv:
    n = sys.argv[sys.argv.index("--discussion") + 1]
    if n == "4001":
        print('{"total_cost_usd": 3.5, "source": "agent_run"}')
    else:
        print('{"total_cost_usd": 0, "source": "none"}')
PY
  cat > "$tmpdir/backend/cost_formatter.py" <<'PY'
import sys
sys.stdin.read()
print("| Discussion | Cost |\n|---|---|\n| fixture | \\$3.50 |")
PY

  mkdir -p "$tmpdir/mockbin"
  local gh_call_log="$tmpdir/gh-calls.txt"
  : > "$gh_call_log"
  cat > "$tmpdir/mockbin/gh" <<'GH'
#!/usr/bin/env bash
# Mock gh for the D#2372 discussion-targeting fixture. Every invocation is
# logged at the argv boundary FIRST, unconditionally — that log is the
# evidence, independent of which branch below (if any) matches.
ARGS="$*"
echo "$ARGS" >> "${GH_CALL_LOG:?GH_CALL_LOG must be set}"

if [[ "$ARGS" == *"pr view"* && "$ARGS" == *"--json body"* ]]; then
  printf 'Closes D#4001\nCloses D#4002\nCloses D#4003\n'
  exit 0
fi
if [[ "$ARGS" == *"pr view"* && "$ARGS" == *"--json createdAt"* ]]; then
  echo "2026-01-01T00:00:00Z"; exit 0
fi
if [[ "$ARGS" == *"pr view"* && "$ARGS" == *"--json mergedAt"* ]]; then
  echo "2026-01-10T00:00:00Z"; exit 0
fi
if [[ "$ARGS" == *"pr view"* && "$ARGS" == *"--json files"* ]]; then
  if [[ "$ARGS" == *"length"* ]]; then echo "0"; else echo "[]"; fi
  exit 0
fi
if [[ "$ARGS" == *"pr view"* && "$ARGS" == *"--json labels"* ]]; then
  echo ""; exit 0
fi
if [[ "$ARGS" == *"pr list"* ]]; then
  echo "[]"; exit 0
fi
if [[ "$ARGS" == *"issues/"*"/timeline"* ]]; then
  echo "[]"; exit 0
fi

if [[ "$ARGS" == *"graphql"* ]]; then
  # resolve_pr_discussion --all's per-candidate validation: "{ id } }" only.
  if [[ "$ARGS" == *"{ id } }"* ]]; then
    for n in 4001 4002 4003; do
      if [[ "$ARGS" == *"discussion(number:$n)"* ]]; then
        echo "D_disc${n}id"; exit 0
      fi
    done
    echo "null"; exit 0
  fi
  # The close-loop's per-iteration full fetch.
  if [[ "$ARGS" == *"id body comments"* ]]; then
    for n in 4001 4002 4003; do
      if [[ "$ARGS" == *"discussion(number:$n)"* ]]; then
        printf '{"id":"D_disc%sid","body":"---\\nplanned_prs: 1\\n---\\n","comments":{"pageInfo":{"hasNextPage":false},"nodes":[]}}' "$n"
        exit 0
      fi
    done
    echo ""; exit 0
  fi
  # stats_metrics DISC_TAG title query.
  if [[ "$ARGS" == *"{ title } }"* ]]; then
    for n in 4001 4002 4003; do
      if [[ "$ARGS" == *"discussion(number:$n)"* ]]; then
        echo "[Bug] fixture discussion $n"; exit 0
      fi
    done
    echo ""; exit 0
  fi
  # completion_block createdAt query.
  if [[ "$ARGS" == *"{ createdAt } }"* ]]; then
    for n in 4001 4002 4003; do
      if [[ "$ARGS" == *"discussion(number:$n)"* ]]; then
        echo "2026-02-0${n: -1}T00:00:00Z"; exit 0
      fi
    done
    echo ""; exit 0
  fi
  # Every other graphql call (mutations: close/update/addComment, label
  # lookups) — accepted, empty body. The argv log above already captured it;
  # these callers only branch on exit status, never on stdout.
  exit 0
fi
exit 0
GH
  chmod +x "$tmpdir/mockbin/gh"

  (
    export GH_CALL_LOG="$gh_call_log"
    export PATH="$tmpdir/mockbin:$PATH"
    REPO_ROOT="$tmpdir" AUTONOMOUS_TEAM_REPO="test-org/test-repo" \
      AUTONOMOUS_TEAM_STATE_DIR="$tmpdir/state" \
      bash "$tmpdir/scripts/post-merge-hook.sh" --pr 4242 > "$tmpdir/output.txt" 2>&1
    echo "$?" > "$tmpdir/rc.txt"
  )

  mkdir -p "$out_dir"
  cp "$gh_call_log" "$out_dir/gh-calls.txt"
  cp "$tmpdir/output.txt" "$out_dir/output.txt"
  cp "$tmpdir/rc.txt" "$out_dir/rc.txt"
  rm -rf "$tmpdir"
}

FIXTURE_ROOT="$(mktemp -d)"

# ── GREEN: the shipping (fixed) hook ─────────────────────────────────────────
run_hook_fixture "$HOOK" "$FIXTURE_ROOT/fixed"
FIXED_LOG="$FIXTURE_ROOT/fixed/gh-calls.txt"

assert_log_contains "fixed: completion_block queries createdAt for Discussion #4001 (first)" \
  'discussion(number:4001) { createdAt }' "$FIXED_LOG"
assert_log_absent "fixed: completion_block does NOT query createdAt for Discussion #4003 (last)" \
  'discussion(number:4003) { createdAt }' "$FIXED_LOG"

assert_log_contains "fixed: stats_metrics queries title for Discussion #4001 (first)" \
  'discussion(number:4001) { title }' "$FIXED_LOG"
assert_log_absent "fixed: stats_metrics does NOT query title for Discussion #4003 (last)" \
  'discussion(number:4003) { title }' "$FIXED_LOG"

assert_log_contains "fixed: cost_comment mutation targets Discussion #4001's node id (first)" \
  'addDiscussionComment(input:{discussionId:"D_disc4001id"' "$FIXED_LOG"
assert_log_absent "fixed: cost_comment mutation does NOT target Discussion #4003's node id (last)" \
  'addDiscussionComment(input:{discussionId:"D_disc4003id"' "$FIXED_LOG"

if grep -qF -- 'addDiscussionComment(input:{discussionId:"D_disc4001id"' "$FIXED_LOG"; then
  COST_LINE="$(grep -F -- 'addDiscussionComment(input:{discussionId:"D_disc4001id"' "$FIXED_LOG")"
  # Pure-bash comparison (D#2369): a dash-prefixed needle like "--repo" would
  # make `assert_not_contains` pass vacuously in some suites. This is a plain
  # glob comparison, not a grep flag that can be misread.
  if [[ "$COST_LINE" != *"--repo"* ]]; then
    pass "fixed: the cost_comment mutation call itself carries no --repo"
  else
    fail "fixed: the cost_comment mutation call unexpectedly carries --repo: $COST_LINE"
  fi
else
  fail "fixed: cost_comment mutation call not found — cannot check for --repo"
fi

# ── RED: the same hook with the D#2372 fix mechanically reverted ───────────
# Strips only the lines this PR added (each wrapped in its own BEGIN/END
# D#2372 marker) — restoring the leaked-loop-variable bug byte-for-byte, per
# item 1's "then break the fix — restore the leaked loop variable" — and
# confirms the exact same assertions now go red, naming Discussion #4003
# (the LAST one) instead of #4001.
MUTANT_HOOK="$FIXTURE_ROOT/mutant-post-merge-hook.sh"
sed '/# BEGIN D#2372 discussion-targeting-fix/,/# END D#2372 discussion-targeting-fix/d' \
  "$HOOK" > "$MUTANT_HOOK"

if bash -n "$MUTANT_HOOK"; then
  pass "mutant hook still parses after the mechanical revert"
else
  fail "mutant hook does not parse — the revert produced broken bash"
fi

run_hook_fixture "$MUTANT_HOOK" "$FIXTURE_ROOT/mutant"
MUTANT_LOG="$FIXTURE_ROOT/mutant/gh-calls.txt"

if grep -qF -- 'discussion(number:4003) { createdAt }' "$MUTANT_LOG"; then
  pass "RED (expected): with the fix reverted, completion_block now wrongly targets Discussion #4003 (last)"
else
  fail "RED (expected) did not reproduce: reverting the fix should have flipped completion_block to Discussion #4003 — the test is not actually sensitive to this bug"
fi
if grep -qF -- 'discussion(number:4001) { createdAt }' "$MUTANT_LOG"; then
  fail "RED (expected) did not reproduce: reverted hook still queried Discussion #4001 for completion_block — the test is not actually sensitive to this bug"
else
  pass "RED (expected): with the fix reverted, completion_block no longer queries Discussion #4001 (first)"
fi

if grep -qF -- 'discussion(number:4003) { title }' "$MUTANT_LOG"; then
  pass "RED (expected): with the fix reverted, stats_metrics now wrongly targets Discussion #4003 (last)"
else
  fail "RED (expected) did not reproduce: reverting the fix should have flipped stats_metrics to Discussion #4003"
fi

echo ""
echo "Discussion targeting: fixture root was $FIXTURE_ROOT (removed below)"
rm -rf "$FIXTURE_ROOT"

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
