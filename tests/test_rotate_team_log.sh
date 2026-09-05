#!/usr/bin/env bash
# tests/test_rotate_team_log.sh — unit tests for rotate-team-log.sh
# Mocks the gh CLI to verify success and failure paths without real API calls.

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/rotate-team-log.sh"
PASS=0
FAIL=0

_pass() { echo "PASS: $1"; ((PASS++)); }
_fail() { echo "FAIL: $1"; ((FAIL++)); }

FAKE_BIN=$(mktemp -d)
trap 'rm -rf "$FAKE_BIN"' EXIT

# ── Test 1: fresh create (no existing log) — URL parsed, number returned ──────
cat > "$FAKE_BIN/gh" << 'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "issue list")   echo "null" ;;
  "issue create") echo "https://github.com/autonomous-agent-7/autonomous-forever/issues/101"; exit 0 ;;
  *)              exit 0 ;;
esac
EOF
chmod +x "$FAKE_BIN/gh"

result=$(PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" rotate 2>/dev/null) && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && [ "$result" = "101" ]; then
  _pass "fresh create: number extracted from URL"
else
  _fail "fresh create: expected exit 0 and '101', got rc=$rc result='$result'"
fi

# ── Test 2: rotate with existing log — logs 'rotated to issue #N (from #old)' ─
# gh issue list returns 55 for all calls (single open issue).
# _close_old_logs: jq filter .[1:][] on a single-item set → empty → no closes.
# _current_number: jq filter .[0].number → 55.
# Our fake gh ignores --jq and just prints "55", which both callers accept.
cat > "$FAKE_BIN/gh" << 'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "issue list")    echo "55" ;;
  "issue create")  echo "https://github.com/autonomous-agent-7/autonomous-forever/issues/200"; exit 0 ;;
  "issue comment") exit 0 ;;
  "issue close")   exit 0 ;;
  *)               exit 0 ;;
esac
EOF
chmod +x "$FAKE_BIN/gh"

result=$(PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" rotate 2>"$FAKE_BIN/rotate_t2_stderr") && rc=0 || rc=$?
stderr_t2=$(cat "$FAKE_BIN/rotate_t2_stderr" 2>/dev/null || true)
rm -f "$FAKE_BIN/rotate_t2_stderr"

if [ "$rc" -eq 0 ] && [ "$result" = "200" ]; then
  _pass "rotate with existing log: successor number returned"
else
  _fail "rotate with existing log: expected exit 0 and '200', got rc=$rc result='$result'"
fi
if echo "$stderr_t2" | grep -q "rotated to issue #200 (from #55)"; then
  _pass "rotate with existing log: success message logged to stderr"
else
  _fail "rotate with existing log: expected 'rotated to issue #200 (from #55)' in stderr"
  echo "  stderr was: $stderr_t2"
fi

# ── Test 3: gh issue create fails — error surfaced, exit non-zero ─────────────
cat > "$FAKE_BIN/gh" << 'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "issue list")   echo "null" ;;
  "issue create")
    echo "GraphQL: Could not resolve to a Repository" >&2
    echo "GraphQL: Could not resolve to a Repository"
    exit 1
    ;;
  *)              exit 0 ;;
esac
EOF
chmod +x "$FAKE_BIN/gh"

stderr_t3=$(PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" rotate 2>&1 >/dev/null) && rc=0 || rc=$?
if [ "${rc:-0}" -ne 0 ]; then
  _pass "gh create failure: exits non-zero"
else
  _fail "gh create failure: expected non-zero exit, got 0"
fi
if echo "$stderr_t3" | grep -q "ERROR"; then
  _pass "gh create failure: ERROR message surfaced in stderr"
else
  _fail "gh create failure: expected 'ERROR' in stderr, got: $stderr_t3"
fi

# ── Test 4: create succeeds but returns no parseable number — explicit error ───
cat > "$FAKE_BIN/gh" << 'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "issue list")   echo "null" ;;
  "issue create") echo "Done."; exit 0 ;;
  *)              exit 0 ;;
esac
EOF
chmod +x "$FAKE_BIN/gh"

stderr_t4=$(PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" rotate 2>&1 >/dev/null) && rc=0 || rc=$?
if [ "${rc:-0}" -ne 0 ]; then
  _pass "no number in output: exits non-zero"
else
  _fail "no number in output: expected non-zero exit, got 0"
fi
if echo "$stderr_t4" | grep -q "did not contain an issue number"; then
  _pass "no number in output: diagnostic message in stderr"
else
  _fail "no number in output: expected 'did not contain an issue number' in stderr, got: $stderr_t4"
fi

# ── Test 5: timestamped title — contains date in YYYY-MM-DD format ────────────
TITLE_FILE=$(mktemp)
cat > "$FAKE_BIN/gh" << GHEOF
#!/usr/bin/env bash
case "\$1 \$2" in
  "issue list") echo "null" ;;
  "issue create")
    while [ \$# -gt 0 ]; do
      if [ "\$1" = "--title" ]; then printf '%s' "\$2" > "$TITLE_FILE"; break; fi
      shift
    done
    echo "https://github.com/autonomous-agent-7/autonomous-forever/issues/300"
    exit 0
    ;;
  *) exit 0 ;;
esac
GHEOF
chmod +x "$FAKE_BIN/gh"

PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" rotate >/dev/null 2>/dev/null && true
title=$(cat "$TITLE_FILE" 2>/dev/null || echo "")
rm -f "$TITLE_FILE"
if echo "$title" | grep -qE "Team Activity Log [0-9]{4}-[0-9]{2}-[0-9]{2}"; then
  _pass "timestamped title: YYYY-MM-DD date present in title"
else
  _fail "timestamped title: expected date in title, got: '$title'"
fi

# ── Test 6: index-lag race — comment uses the just-created issue number ───────
# instead of re-querying the (eventually-consistent) label index. Regresses the
# create-then-post race: 'issue list' always returns null (as if the label
# index hasn't caught up yet), but 'issue create' succeeds and returns a URL.
# cmd_current must use that created number directly rather than dropping the
# message.
COMMENT_NUM_FILE=$(mktemp)
cat > "$FAKE_BIN/gh" << GHEOF
#!/usr/bin/env bash
case "\$1 \$2" in
  "issue list")   echo "null" ;;
  "issue create") echo "https://github.com/autonomous-agent-7/autonomous-forever/issues/501"; exit 0 ;;
  "issue comment")
    printf '%s' "\$3" > "$COMMENT_NUM_FILE"
    exit 0
    ;;
  "issue close")  exit 0 ;;
  "api "*)        echo "0" ;;
  *)              exit 0 ;;
esac
GHEOF
chmod +x "$FAKE_BIN/gh"

stderr_t6=$(PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" comment "hello world" 2>&1 >/dev/null) && rc=0 || rc=$?
comment_num=$(cat "$COMMENT_NUM_FILE" 2>/dev/null || echo "")
rm -f "$COMMENT_NUM_FILE"

if [ "$comment_num" = "501" ]; then
  _pass "index-lag race: comment posted to newly-created issue #501 (not re-queried)"
else
  _fail "index-lag race: expected comment posted to #501, got '$comment_num'"
fi
if echo "$stderr_t6" | grep -q "message dropped"; then
  _fail "index-lag race: message was dropped despite issue #501 having been created (stderr: $stderr_t6)"
else
  _pass "index-lag race: message NOT dropped"
fi

# ── Test 7: backlog dedup at scale — 61 open issues, one pass closes 60 ───────
# Regresses D#1725's secondary finding: _close_old_logs()'s gh issue list call
# had no --limit, so it silently capped at gh's default page size (30) and a
# 61-issue backlog needed a second pass to fully dedup. With --limit 200 in
# place, a single `rotate-team-log.sh current` must close all 60 extras and
# keep only the highest-numbered issue open.
CLOSE_LOG_FILE=$(mktemp)
LIMIT_LOG_FILE=$(mktemp)
cat > "$FAKE_BIN/gh" << GHEOF
#!/usr/bin/env bash
jqval=""
limitval=""
args=("\$@")
i=0
while [ \$i -lt \${#args[@]} ]; do
  if [ "\${args[\$i]}" = "--jq" ]; then jqval="\${args[\$((i+1))]}"; fi
  if [ "\${args[\$i]}" = "--limit" ]; then limitval="\${args[\$((i+1))]}"; fi
  i=\$((i+1))
done

case "\$1 \$2" in
  "issue list")
    echo "\$limitval" >> "$LIMIT_LOG_FILE"
    if [[ "\$jqval" == *"sort_by"* ]]; then
      # _current_number's post-close query: highest surviving number
      echo "61"
    else
      # _close_old_logs' query: jq-filtered output IS the 60 extras
      # (post-filter — real filter is 'map(.number)|sort|reverse|.[1:][]',
      # which for 1..61 drops 61 and yields 60 down to 1)
      seq 60 -1 1
    fi
    ;;
  "issue close")
    echo "\$3" >> "$CLOSE_LOG_FILE"
    exit 0
    ;;
  *) exit 0 ;;
esac
GHEOF
chmod +x "$FAKE_BIN/gh"

result=$(PATH="$FAKE_BIN:$PATH" bash "$SCRIPT" current 2>/dev/null) && rc=0 || rc=$?
close_count=$(wc -l < "$CLOSE_LOG_FILE" | tr -d ' ')
limits=$(sort -u "$LIMIT_LOG_FILE" | tr '\n' ',' )
rm -f "$CLOSE_LOG_FILE" "$LIMIT_LOG_FILE"

if [ "$rc" -eq 0 ] && [ "$close_count" -eq 60 ]; then
  _pass "backlog dedup at scale: exactly 60 gh issue close calls for a 61-issue backlog"
else
  _fail "backlog dedup at scale: expected rc=0 and 60 closes, got rc=$rc closes=$close_count"
fi
if [ "$result" = "61" ]; then
  _pass "backlog dedup at scale: highest-numbered issue (61) kept"
else
  _fail "backlog dedup at scale: expected '61' kept, got '$result'"
fi
min_limit=""
if [ -n "$limits" ]; then
  min_limit=$(echo "$limits" | tr ',' '\n' | grep -v '^$' | sort -n | head -1)
fi
if [ -n "$min_limit" ] && [ "$min_limit" -ge 61 ] 2>/dev/null; then
  _pass "backlog dedup at scale: gh issue list called with --limit >= 61 (got: $limits)"
else
  _fail "backlog dedup at scale: expected every gh issue list call to carry --limit >= 61, got: '${limits:-<none>}'"
fi

# ── Test 8: repo-consistency guard ─────────────────────────────────────────────
# What this actually proves: _resolve_repo() is called exactly once per script
# run (REPO="$(_resolve_repo)" at the top of rotate-team-log.sh), and every gh
# call — list AND create — reuses that single value. With a fixture config.json
# naming the live slug, this test asserts the script never queries under the
# hardcoded stale fallback slug for one call while using something else for
# another. That's a real regression it would catch: e.g. a future edit that
# hardcodes a slug in a new call site instead of threading $REPO through.
#
# The fixture's LIVE_SLUG is deliberately NOT the same string as
# repo-resolve.sh's hardcoded fallback (autonomous-agent-7/fulcrumaxe). If
# _resolve_repo() ever stops reading the fixture's config.json — wrong path,
# wrong key, fixture write bug, whatever — it falls through to that hardcoded
# fallback, which is a *different* string than LIVE_SLUG, so the assertions
# below fail loudly instead of coincidentally matching. That's the structural
# fix for the round-2 finding: LIVE_SLUG used to equal the hardcoded fallback,
# so a broken read path and a working read path were indistinguishable — this
# test could go vacuously green forever. Picking a fixture slug that can never
# collide with the fallback closes that gap for good, not just for today.
#
# What this does NOT prove: the real D#1725 incident — gh issue create
# silently following a GitHub repo-rename redirect while gh issue list does
# not — is a behavior of the live GitHub API / gh CLI, not of this script's
# logic, and isn't reproducible in a local mock without just asserting
# whatever we hardcode into the mock. Because REPO is resolved once and reused
# everywhere, the STALE_SLUG branch below is deliberately never entered while
# config.json names the live slug (that's the passing condition, not a gap
# in the fixture) — the actual asymmetry only bites when config.json is
# missing/stale, which is the pre-mitigation state this PR doesn't touch (see
# Out of scope: scripts/lib/repo-resolve.sh and config.json belong to D#1635
# Wave 1). Runs the script from an isolated fixture tree (its own copy of
# rotate-team-log.sh + repo-resolve.sh + a fixture .autonomous-team/config.json)
# because _resolve_repo() resolves config.json relative to the SCRIPT's own
# path — an AUTONOMOUS_TEAM_REPO env override would not take effect while the
# real repo's config.json exists.
FIXTURE_DIR=$(mktemp -d)
mkdir -p "$FIXTURE_DIR/scripts/lib" "$FIXTURE_DIR/.autonomous-team"
cp "$SCRIPT" "$FIXTURE_DIR/scripts/rotate-team-log.sh"
cp "$(dirname "$SCRIPT")/lib/repo-resolve.sh" "$FIXTURE_DIR/scripts/lib/repo-resolve.sh"

LIVE_SLUG="fixture-org/fixture-live-repo"
STALE_SLUG="fixture-org/fixture-stale-repo"
cat > "$FIXTURE_DIR/.autonomous-team/config.json" << JSONEOF
{"repo": "$LIVE_SLUG"}
JSONEOF

REPO_LOG_FILE=$(mktemp)
CREATE_LOG_FILE=$(mktemp)
cat > "$FAKE_BIN/gh" << GHEOF
#!/usr/bin/env bash
LIVE_SLUG="$LIVE_SLUG"
STALE_SLUG="$STALE_SLUG"
repo=""
args=("\$@")
i=0
while [ \$i -lt \${#args[@]} ]; do
  if [ "\${args[\$i]}" = "--repo" ]; then repo="\${args[\$((i+1))]}"; fi
  i=\$((i+1))
done

case "\$1 \$2" in
  "issue list")
    echo "\$repo" >> "$REPO_LOG_FILE"
    if [ "\$repo" = "\$STALE_SLUG" ]; then
      echo "null"
    else
      echo "777"
    fi
    ;;
  "issue create")
    echo "CALLED --repo=\$repo" >> "$CREATE_LOG_FILE"
    echo "https://github.com/\$repo/issues/999"
    exit 0
    ;;
  *) exit 0 ;;
esac
GHEOF
chmod +x "$FAKE_BIN/gh"

result=$(PATH="$FAKE_BIN:$PATH" bash "$FIXTURE_DIR/scripts/rotate-team-log.sh" current 2>/dev/null) && rc=0 || rc=$?
create_calls=$(wc -l < "$CREATE_LOG_FILE" | tr -d ' ')
repos_listed=$(sort -u "$REPO_LOG_FILE" | tr '\n' ',')
rm -rf "$FIXTURE_DIR" "$REPO_LOG_FILE" "$CREATE_LOG_FILE"

if [ "$rc" -eq 0 ] && [ "$result" = "777" ]; then
  _pass "repo-consistency guard: existing issue #777 found under the live slug"
else
  _fail "repo-consistency guard: expected rc=0 and '777', got rc=$rc result='$result'"
fi
if [ "$create_calls" -eq 0 ]; then
  _pass "repo-consistency guard: zero gh issue create calls"
else
  _fail "repo-consistency guard: expected zero gh issue create calls, got $create_calls"
fi
if [ "$repos_listed" = "$LIVE_SLUG," ]; then
  _pass "repo-consistency guard: gh issue list --repo matched fixture config.json's live slug ($LIVE_SLUG) on every call, never the hardcoded stale fallback (single-resolution threading, not a live-redirect reproduction — see header comment)"
else
  _fail "repo-consistency guard: expected gh issue list --repo to equal '$LIVE_SLUG' only, got: $repos_listed"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
