#!/usr/bin/env bash
# tests/test_loop_bootstrap.sh — integration test for loop-bootstrap/bootstrap.sh
#
# Creates a fresh git-init'd repo in /tmp, runs bootstrap, asserts expected files
# are present, then re-runs to verify idempotency.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP="$REPO_ROOT/loop-bootstrap/bootstrap.sh"

# All scratch paths for this suite live under one mktemp'd directory so
# concurrent runs of this suite (e.g. two reviewers in separate worktrees)
# never race on a shared fixed /tmp path (D#2254).
RUN_TMP="$(mktemp -d /tmp/test_loop_bootstrap.XXXXXX)"
trap 'rm -rf "$RUN_TMP"' EXIT

TARGET="$RUN_TMP/test-cold-start"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    pass "file exists: $path"
  else
    fail "missing file: $path"
  fi
}

assert_dir() {
  local path="$1"
  if [[ -d "$path" ]]; then
    pass "dir exists: $path"
  else
    fail "missing dir: $path"
  fi
}

echo ""
echo "--- memory_is_tier_transferable: matches only inside frontmatter (D#2598 fix-round item 3) ---"
# A bare `grep -q '^tier: transferable$' "$f"` matches that string ANYWHERE
# in the file, including a memory's own BODY prose — e.g. a note that
# happens to quote or discuss another file's tier value. Extract the real
# function from bootstrap.sh (not a hand-duplicated copy, so this test
# cannot silently drift from the implementation) and prove it looks only at
# the frontmatter block.
MEMTIER_FN_SRC=$(sed -n '/^memory_is_tier_transferable() {/,/^}/p' "$BOOTSTRAP")
if [[ -z "$MEMTIER_FN_SRC" ]]; then
  fail "could not extract memory_is_tier_transferable() from $BOOTSTRAP — has it been renamed?"
else
  eval "$MEMTIER_FN_SRC"

  MEMTIER_TMP="$RUN_TMP/memtier-fixtures"
  mkdir -p "$MEMTIER_TMP"

  # Fixture A: tier:hardwire-candidate in frontmatter, but the BODY prose
  # happens to contain the literal line "tier: transferable" (quoting or
  # discussing some other file). Must NOT be treated as transferable.
  cat > "$MEMTIER_TMP/fixture-body-mention.md" <<'EOF'
---
name: fixture-body-mention
tier: hardwire-candidate
---
This note is not itself transferable. For contrast, another memory in this
corpus carries this exact line in ITS frontmatter:
tier: transferable
That line above is body prose, not frontmatter, and must not count.
EOF
  if memory_is_tier_transferable "$MEMTIER_TMP/fixture-body-mention.md"; then
    fail "body-only 'tier: transferable' mention was wrongly treated as frontmatter tier:transferable"
  else
    pass "body-only 'tier: transferable' mention correctly NOT treated as tier:transferable"
  fi

  # Fixture B: tier:transferable genuinely in frontmatter. Must be detected.
  cat > "$MEMTIER_TMP/fixture-real-transferable.md" <<'EOF'
---
name: fixture-real-transferable
tier: transferable
---
Body text, irrelevant to tier detection.
EOF
  if memory_is_tier_transferable "$MEMTIER_TMP/fixture-real-transferable.md"; then
    pass "genuine frontmatter tier:transferable correctly detected"
  else
    fail "genuine frontmatter tier:transferable was NOT detected"
  fi
fi

echo ""
echo "--- scripts/memory-triage/: no tier:transferable file carries a session identifier (D#2598 fix-round 2 item 1) ---"
# Direct source-corpus scan, independent of running a full bootstrap: every
# tier:transferable file must be free of (1) an originSessionId frontmatter
# field and (2) a bare UUID-shaped string anywhere in the file
# ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}, case
# insensitive) -- catches a session ID narrated in prose even without the
# originSessionId key naming it. Non-transferable (tier:hardwire-candidate)
# files are deliberately NOT checked here -- they never ship, and scrubbing
# them isn't required by their lesson.
TRIAGE_SESSION_LEAKS=""
if [[ -d "$REPO_ROOT/scripts/memory-triage" ]]; then
  while IFS= read -r -d '' tf; do
    [[ "$(basename "$tf")" == "MEMORY.md" ]] && continue
    memory_is_tier_transferable "$tf" || continue
    if grep -qiE '^[[:space:]]*originSessionId:|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' "$tf" 2>/dev/null; then
      TRIAGE_SESSION_LEAKS="${TRIAGE_SESSION_LEAKS}$(basename "$tf")"$'\n'
    fi
  done < <(find "$REPO_ROOT/scripts/memory-triage" -maxdepth 1 -type f -name '*.md' -print0 2>/dev/null)
fi
if [[ -z "$TRIAGE_SESSION_LEAKS" ]]; then
  pass "no tier:transferable file in scripts/memory-triage/ carries a session identifier"
else
  fail "tier:transferable file(s) in scripts/memory-triage/ still carry a session identifier:"
  echo "$TRIAGE_SESSION_LEAKS"
fi

echo ""
echo "=== test_loop_bootstrap ==="
echo ""

# Setup: fresh git-init'd target
rm -rf "$TARGET"
mkdir -p "$TARGET"
git -C "$TARGET" init -q
echo "Created fresh git repo at $TARGET"

# --- Run bootstrap ---
echo ""
echo "--- Running bootstrap.sh ---"
bash "$BOOTSTRAP" --repo acme/test-cold-start "$TARGET"

# Derive slug the same way bootstrap.sh does
TARGET_REAL="$(realpath "$TARGET")"
TARGET_SLUG=$(echo "$TARGET_REAL" | sed 's|^/||; s|/|-|g')
MEMORY_DEST="$TARGET_REAL/.claude/projects/-${TARGET_SLUG}/memory"

echo ""
echo "--- Asserting memories ---"
assert_dir "$MEMORY_DEST"
# At least one transferable memory file must be present
MEMORY_COUNT=$(ls "$MEMORY_DEST"/*.md 2>/dev/null | wc -l || echo 0)
if [[ "$MEMORY_COUNT" -gt 0 ]]; then
  pass "memory files installed ($MEMORY_COUNT files)"
else
  fail "no memory files found in $MEMORY_DEST"
fi

# D#2598 item 13/14: memories are derived from scripts/memory-triage/ by
# tier, not hand-copied from loop-bootstrap/memories/ (that directory no
# longer exists). A tier:transferable file must be installed; a
# tier:hardwire-candidate file must NOT be.
if [[ -f "$MEMORY_DEST/feedback_no_nested_coordinator.md" ]]; then
  pass "tier:transferable memory installed (feedback_no_nested_coordinator.md)"
else
  fail "tier:transferable memory missing: feedback_no_nested_coordinator.md"
fi
if [[ -f "$MEMORY_DEST/feedback_no_runaway_loops.md" ]]; then
  fail "tier:hardwire-candidate memory was installed (should be excluded): feedback_no_runaway_loops.md"
else
  pass "tier:hardwire-candidate memory correctly excluded (feedback_no_runaway_loops.md)"
fi
if [[ -f "$MEMORY_DEST/MEMORY.md" ]]; then
  fail "scripts/memory-triage/MEMORY.md (the corpus index) was installed — it is corpus tooling, not adopter guidance"
else
  pass "scripts/memory-triage/MEMORY.md correctly not installed"
fi

# D#2598 fix-round 2 item 1: no memory bootstrap installs (i.e. every
# tier:transferable file) may carry an internal Claude session identifier.
# Two patterns, checked over every installed memory file:
#   1. An `originSessionId:` frontmatter field (any value) — this repo's own
#      session-tracking metadata, meaningless and identifying to an adopter.
#   2. A bare UUID-shaped string anywhere in the file
#      ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}, case
#      insensitive) — catches a session ID narrated in prose/a URL even
#      without the originSessionId frontmatter key naming it.
SESSION_LEAK_FILES=""
while IFS= read -r -d '' mf; do
  if grep -qiE '^[[:space:]]*originSessionId:|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' "$mf" 2>/dev/null; then
    SESSION_LEAK_FILES="${SESSION_LEAK_FILES}${mf#"$MEMORY_DEST"/}"$'\n'
  fi
done < <(find "$MEMORY_DEST" -maxdepth 1 -type f -name '*.md' -print0 2>/dev/null)
if [[ -z "$SESSION_LEAK_FILES" ]]; then
  pass "no installed memory carries an originSessionId field or a UUID-shaped session reference"
else
  fail "installed memory file(s) still carry a session identifier:"
  echo "$SESSION_LEAK_FILES"
fi

echo ""
echo "--- Asserting scripts ---"
assert_dir "$TARGET/scripts"
assert_file "$TARGET/scripts/spawn-agent.sh"
assert_file "$TARGET/scripts/pre-spawn-check.sh"
assert_file "$TARGET/scripts/post-agent-hook.sh"
assert_file "$TARGET/scripts/subagent-stop-hook.sh"
assert_dir "$TARGET/scripts/lib"
assert_file "$TARGET/scripts/lib/working-principles.sh"
assert_file "$TARGET/scripts/lib/panel-helpers.sh"
assert_file "$TARGET/scripts/lib/gh-token.sh"

echo ""
echo "--- Asserting start-the-day.sh / merge-and-hook.sh ship byte-identical (D#2598) ---"
assert_file "$TARGET/scripts/start-the-day.sh"
if cmp -s "$REPO_ROOT/scripts/start-the-day.sh" "$TARGET/scripts/start-the-day.sh"; then
  pass "installed scripts/start-the-day.sh is byte-identical to the live copy"
else
  fail "installed scripts/start-the-day.sh differs from the live copy"
fi
# The installed copy must carry both the self-heal section and the step-1b
# working-tree-divergence / HEAD-restore section — the two pieces the old
# 301-line adopter variant lacked entirely.
if grep -q '^echo "## Self-heal checks"$' "$TARGET/scripts/start-the-day.sh"; then
  pass "installed start-the-day.sh contains the self-heal section header"
else
  fail "installed start-the-day.sh is missing the self-heal section header"
fi
if grep -q '^echo "## 1b. Working-tree divergence check"$' "$TARGET/scripts/start-the-day.sh"; then
  pass "installed start-the-day.sh contains the step-1b HEAD-restore/divergence section header"
else
  fail "installed start-the-day.sh is missing the step-1b HEAD-restore/divergence section header"
fi

assert_file "$TARGET/scripts/merge-and-hook.sh"
if cmp -s "$REPO_ROOT/scripts/merge-and-hook.sh" "$TARGET/scripts/merge-and-hook.sh"; then
  pass "installed scripts/merge-and-hook.sh is byte-identical to the live copy"
else
  fail "installed scripts/merge-and-hook.sh differs from the live copy"
fi

# start-dashboard.sh is the one remaining deliberate project-agnostic
# variant — it must come from loop-bootstrap/, NOT the live copy.
assert_file "$TARGET/scripts/start-dashboard.sh"
if cmp -s "$REPO_ROOT/loop-bootstrap/scripts/start-dashboard.sh" "$TARGET/scripts/start-dashboard.sh"; then
  pass "installed scripts/start-dashboard.sh matches the loop-bootstrap project-agnostic variant"
else
  fail "installed scripts/start-dashboard.sh does not match the loop-bootstrap variant"
fi

# The old hand-maintained loop-bootstrap/scripts/ copies of start-the-day.sh
# and merge-and-hook.sh are gone from this repo entirely (D#2598 items 1-2).
if [[ -e "$REPO_ROOT/loop-bootstrap/scripts/start-the-day.sh" ]]; then
  fail "loop-bootstrap/scripts/start-the-day.sh still exists — should have been removed"
else
  pass "loop-bootstrap/scripts/start-the-day.sh no longer exists"
fi
if [[ -e "$REPO_ROOT/loop-bootstrap/scripts/merge-and-hook.sh" ]]; then
  fail "loop-bootstrap/scripts/merge-and-hook.sh still exists — should have been removed"
else
  pass "loop-bootstrap/scripts/merge-and-hook.sh no longer exists"
fi
if [[ -e "$REPO_ROOT/loop-bootstrap/memories" ]]; then
  fail "loop-bootstrap/memories/ still exists — memories should be derived from scripts/memory-triage/ by tier"
else
  pass "loop-bootstrap/memories/ no longer exists"
fi

echo ""
echo "--- Asserting no project-identity leak after a foreign-slug bootstrap (D#2598 fix-round item 2) ---"
# A reviewer's real acme/widget install had 79 mentions of
# autonomous-agent-7/fulcrumaxe in .claude/agents/ alone, because only the
# pre-rename SOURCE_REPO literal was ever being rewritten. $TARGET was
# bootstrapped above with --repo acme/test-cold-start (this repo's own
# slug nowhere in that string), so any surviving mention of either this
# project's legacy or current Discussion-plane slug is a real leak.
LEGACY_LEAK_FILES=$(grep -rl "autonomous-agent-7" "$TARGET" 2>/dev/null || true)
# Documented exceptions (D#2598 fix-round item 2a). The two test files use
# this project's own slug purely as arbitrary/illustrative test data, never
# as a functional identity a script or role file reads at runtime (see the
# PR body for the full explanation of each). config.json's "bot_account" is
# NOT a hardcoded literal at all — bootstrap.sh's step 16b resolves it from
# whichever GitHub account `gh` is actually authenticated as in the CI
# runner or dev sandbox invoking this suite; in THIS repo's own CI/sandbox
# that legitimately IS this project's own bot account, which is correct
# behavior, not a leak (a real third-party adopter's runner would resolve
# to THEIR OWN authenticated account instead).
#
# scripts/ci/commands-twin-divergence-guard.sh's bare "autonomous-agent-7"
# occurrences (fix-round 2 item 2) are its OWN regression-scan pattern —
# `grep -q "autonomous-agent-7" "$top_path"` and the FAIL message that names
# it — a bare word, not a "autonomous-agent-7/fulcrumaxe" slug or a
# owner:"autonomous-agent-7" split form, so rewrite_tree_identifiers has
# nothing there to match and correctly leaves it alone: the guard's job is
# to detect this exact string, so it has to contain it. This is the
# detector, not a leak.
KNOWN_FIXTURE_EXCEPTIONS="backend/tests/test_envelope_check.py
backend/tests/test_trust_id_resolver.py
.autonomous-team/config.json
scripts/ci/commands-twin-divergence-guard.sh"
UNEXPECTED_LEAKS=""
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  rel="${f#"$TARGET"/}"
  if ! grep -qxF "$rel" <<<"$KNOWN_FIXTURE_EXCEPTIONS"; then
    UNEXPECTED_LEAKS="${UNEXPECTED_LEAKS}${rel}"$'\n'
  fi
done <<<"$LEGACY_LEAK_FILES"
if [[ -z "$UNEXPECTED_LEAKS" ]]; then
  pass "no unexpected 'autonomous-agent-7' mention anywhere in the installed tree"
else
  fail "unexpected 'autonomous-agent-7' mention(s) found:"
  echo "$UNEXPECTED_LEAKS"
fi

# fulcrumaxe/fulcrumaxe (the code-plane slug) has two roles: this project's
# own identity (must be rewritten) and the public engine repo an update
# check must always target (must NOT be rewritten). The only place it
# should survive is .autonomous-team/engine-install.json's source_repo and
# the sed-proofed DEFAULT_ENGINE_REPO construction inside
# scripts/update-check.sh (checked separately below) — anywhere else is a
# project-identity leak.
CODEPLANE_LEAK_FILES=$(grep -rl "fulcrumaxe/fulcrumaxe" "$TARGET" 2>/dev/null || true)
UNEXPECTED_CODEPLANE_LEAKS=""
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  rel="${f#"$TARGET"/}"
  if [[ "$rel" != ".autonomous-team/engine-install.json" ]]; then
    UNEXPECTED_CODEPLANE_LEAKS="${UNEXPECTED_CODEPLANE_LEAKS}${rel}"$'\n'
  fi
done <<<"$CODEPLANE_LEAK_FILES"
if [[ -z "$UNEXPECTED_CODEPLANE_LEAKS" ]]; then
  pass "no project-identity use of 'fulcrumaxe/fulcrumaxe' outside the documented engine-update reference"
else
  fail "unexpected project-identity 'fulcrumaxe/fulcrumaxe' mention(s) found:"
  echo "$UNEXPECTED_CODEPLANE_LEAKS"
fi

# The engine-update references must still name the real engine repo after
# rewriting, not the adopter's own --repo.
if grep -q '"source_repo": "fulcrumaxe/fulcrumaxe"' "$TARGET/.autonomous-team/engine-install.json" 2>/dev/null; then
  pass "engine-install.json source_repo still names fulcrumaxe/fulcrumaxe after a foreign-slug bootstrap"
else
  fail "engine-install.json source_repo does not name fulcrumaxe/fulcrumaxe (got: $(cat "$TARGET/.autonomous-team/engine-install.json" 2>/dev/null))"
fi
if grep -q '_ENGINE_UPSTREAM_OWNER="fulcrumaxe"' "$TARGET/scripts/update-check.sh" 2>/dev/null \
   && grep -q '_ENGINE_UPSTREAM_NAME="fulcrumaxe"' "$TARGET/scripts/update-check.sh" 2>/dev/null; then
  pass "installed scripts/update-check.sh still targets fulcrumaxe/fulcrumaxe for engine updates after a foreign-slug bootstrap"
else
  fail "installed scripts/update-check.sh's engine-repo constant was rewritten away from fulcrumaxe/fulcrumaxe"
fi

echo ""
echo "--- Asserting agents ---"
assert_dir "$TARGET/.claude/agents"
AGENT_COUNT=$(ls "$TARGET/.claude/agents"/*.md 2>/dev/null | wc -l || echo 0)
if [[ "$AGENT_COUNT" -gt 0 ]]; then
  pass "agent role files installed ($AGENT_COUNT files)"
else
  fail "no agent files found in $TARGET/.claude/agents"
fi
assert_file "$TARGET/.claude/agents/executor.md"
assert_file "$TARGET/.claude/agents/code-reviewer.md"
assert_file "$TARGET/.claude/agents/project-manager.md"

echo ""
echo "--- Asserting templates ---"
assert_dir "$TARGET/backend/spawn_templates"
TMPL_COUNT=$(ls "$TARGET/backend/spawn_templates"/*.tmpl 2>/dev/null | wc -l || echo 0)
if [[ "$TMPL_COUNT" -gt 0 ]]; then
  pass "spawn templates installed ($TMPL_COUNT files)"
else
  fail "no templates found in $TARGET/backend/spawn_templates"
fi

echo ""
echo "--- Asserting CLAUDE.md ---"
assert_file "$TARGET/CLAUDE.md"

# --- Idempotency test ---
echo ""
echo "--- Idempotency: re-running bootstrap ---"

# .autonomous-team/engine-install.json (D#2335 PR 1) is deliberately excluded
# from this whole-tree comparison: it carries a wall-clock bootstrapped_at
# field that legitimately changes on every invocation by design (bootstrap.sh
# step 19a runs unconditionally). tests/test_loop_bootstrap_extended.sh
# excludes the same path for the same reason.
ENGINE_INSTALL_STAMP="$TARGET/.autonomous-team/engine-install.json"

# Snapshot checksums before
BEFORE=$(find "$TARGET" -type f -not -path "$ENGINE_INSTALL_STAMP" | sort | xargs md5sum 2>/dev/null || find "$TARGET" -type f -not -path "$ENGINE_INSTALL_STAMP" | sort | xargs sha256sum)

bash "$BOOTSTRAP" --repo acme/test-cold-start --force "$TARGET" > $RUN_TMP/bootstrap-rerun.log 2>&1

# Snapshot checksums after
AFTER=$(find "$TARGET" -type f -not -path "$ENGINE_INSTALL_STAMP" | sort | xargs md5sum 2>/dev/null || find "$TARGET" -type f -not -path "$ENGINE_INSTALL_STAMP" | sort | xargs sha256sum)

if [[ "$BEFORE" == "$AFTER" ]]; then
  pass "idempotent: re-run produced no diff (excluding the engine-install.json timestamp)"
else
  fail "not idempotent: re-run changed files"
  diff <(echo "$BEFORE") <(echo "$AFTER") | head -20 || true
fi

# --- Dry-run test ---
echo ""
echo "--- Dry-run: should produce no writes ---"
BEFORE_DRY=$(find "$TARGET" -type f | sort | xargs md5sum 2>/dev/null || find "$TARGET" -type f | sort | xargs sha256sum)
bash "$BOOTSTRAP" --repo acme/test-cold-start --dry-run --force "$TARGET" > $RUN_TMP/bootstrap-dryrun.log 2>&1
AFTER_DRY=$(find "$TARGET" -type f | sort | xargs md5sum 2>/dev/null || find "$TARGET" -type f | sort | xargs sha256sum)

if [[ "$BEFORE_DRY" == "$AFTER_DRY" ]]; then
  pass "dry-run: no files written"
else
  fail "dry-run: files were modified"
fi

# Verify dry-run output contains [dry-run] lines
if grep -q "\[dry-run\]" $RUN_TMP/bootstrap-dryrun.log; then
  pass "dry-run output contains [dry-run] annotations"
else
  fail "dry-run output missing [dry-run] annotations"
fi

# --- SubagentStop hook wiring (D#2232) ---
echo ""
echo "--- SubagentStop hook: registered command targets the adapter ---"

STOP_CMD_COUNT_ADAPTER=$(python3 -c "
import json
s = json.load(open('$TARGET/.claude/settings.json'))
n = 0
for e in s.get('hooks', {}).get('SubagentStop', []):
    for h in e.get('hooks', []) or []:
        if isinstance(h, dict) and h.get('command', '').rstrip('\"').endswith('/scripts/subagent-stop-hook.sh'):
            n += 1
print(n)
")
STOP_CMD_COUNT_STALE=$(python3 -c "
import json
s = json.load(open('$TARGET/.claude/settings.json'))
n = 0
for e in s.get('hooks', {}).get('SubagentStop', []):
    for h in e.get('hooks', []) or []:
        if isinstance(h, dict) and h.get('command', '').rstrip('\"').endswith('/scripts/post-agent-hook.sh'):
            n += 1
print(n)
")

if [[ "$STOP_CMD_COUNT_ADAPTER" -eq 1 ]]; then
  pass "SubagentStop registers exactly one scripts/subagent-stop-hook.sh entry"
else
  fail "expected exactly 1 subagent-stop-hook.sh SubagentStop entry, found $STOP_CMD_COUNT_ADAPTER"
fi

if [[ "$STOP_CMD_COUNT_STALE" -eq 0 ]]; then
  pass "SubagentStop has no post-agent-hook.sh entry"
else
  fail "found $STOP_CMD_COUNT_STALE stale post-agent-hook.sh SubagentStop entries"
fi

# --- Migration: an already-bootstrapped project with the stale wiring ---
echo ""
echo "--- Migration: stale post-agent-hook.sh entry gets replaced, not duplicated ---"

MIGRATE_TARGET="$RUN_TMP/test-cold-start-migrate"
rm -rf "$MIGRATE_TARGET"
mkdir -p "$MIGRATE_TARGET/.claude"
git -C "$MIGRATE_TARGET" init -q
cat > "$MIGRATE_TARGET/.claude/settings.json" <<'EOF'
{
  "hooks": {
    "SubagentStop": [
      {"matcher": ".*", "hooks": [{"type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR/scripts/post-agent-hook.sh\""}]}
    ]
  }
}
EOF

bash "$BOOTSTRAP" --repo acme/test-cold-start-migrate --force "$MIGRATE_TARGET" > $RUN_TMP/bootstrap-migrate.log 2>&1

MIGRATE_ADAPTER_COUNT=$(python3 -c "
import json
s = json.load(open('$MIGRATE_TARGET/.claude/settings.json'))
n = 0
for e in s.get('hooks', {}).get('SubagentStop', []):
    for h in e.get('hooks', []) or []:
        if isinstance(h, dict) and 'subagent-stop-hook.sh' in h.get('command', ''):
            n += 1
print(n)
")
MIGRATE_STALE_COUNT=$(python3 -c "
import json
s = json.load(open('$MIGRATE_TARGET/.claude/settings.json'))
n = 0
for e in s.get('hooks', {}).get('SubagentStop', []):
    for h in e.get('hooks', []) or []:
        if isinstance(h, dict) and 'post-agent-hook.sh' in h.get('command', ''):
            n += 1
print(n)
")

if [[ "$MIGRATE_ADAPTER_COUNT" -eq 1 ]]; then
  pass "migration: exactly one subagent-stop-hook.sh entry after bootstrapping a stale project"
else
  fail "migration: expected 1 subagent-stop-hook.sh entry, found $MIGRATE_ADAPTER_COUNT"
fi

if [[ "$MIGRATE_STALE_COUNT" -eq 0 ]]; then
  pass "migration: stale post-agent-hook.sh entry was removed, not left alongside"
else
  fail "migration: stale post-agent-hook.sh entry still present ($MIGRATE_STALE_COUNT)"
fi

# --- Idempotency: registering twice on a fresh target yields one entry ---
echo ""
echo "--- Idempotency: SubagentStop entry count stays at 1 across repeated bootstrap ---"

IDEMPOTENT_ADAPTER_COUNT=$(python3 -c "
import json
s = json.load(open('$TARGET/.claude/settings.json'))
n = 0
for e in s.get('hooks', {}).get('SubagentStop', []):
    for h in e.get('hooks', []) or []:
        if isinstance(h, dict) and 'subagent-stop-hook.sh' in h.get('command', ''):
            n += 1
print(n)
")
# $TARGET was already bootstrapped once above and re-run with --force earlier
# in this file (the byte-for-byte idempotency check) -- confirm the
# SubagentStop entry count specifically, not just overall file equality.
if [[ "$IDEMPOTENT_ADAPTER_COUNT" -eq 1 ]]; then
  pass "idempotency: exactly 1 subagent-stop-hook.sh entry after repeated bootstrap runs"
else
  fail "idempotency: expected 1 subagent-stop-hook.sh entry, found $IDEMPOTENT_ADAPTER_COUNT"
fi

# --- Coexistence: install-sandbox-hook.sh must not disturb the telemetry entry ---
echo ""
echo "--- Coexistence: telemetry hook and dial-audit hook both survive ---"

bash "$TARGET/scripts/install-sandbox-hook.sh" > $RUN_TMP/install-sandbox-hook.log 2>&1

COEXIST_COUNTS=$(python3 -c "
import json
s = json.load(open('$TARGET/.claude/settings.json'))
adapter = 0
dial_audit = 0
for e in s.get('hooks', {}).get('SubagentStop', []):
    for h in e.get('hooks', []) or []:
        cmd = h.get('command', '') if isinstance(h, dict) else ''
        if 'subagent-stop-hook.sh' in cmd:
            adapter += 1
        if 'subagent_stop_dial_audit.py' in cmd:
            dial_audit += 1
print(f'{adapter} {dial_audit}')
")
COEXIST_ADAPTER_COUNT=$(echo "$COEXIST_COUNTS" | cut -d' ' -f1)
COEXIST_DIAL_AUDIT_COUNT=$(echo "$COEXIST_COUNTS" | cut -d' ' -f2)

if [[ "$COEXIST_ADAPTER_COUNT" -eq 1 ]]; then
  pass "coexistence: telemetry subagent-stop-hook.sh entry still present after install-sandbox-hook.sh"
else
  fail "coexistence: expected 1 subagent-stop-hook.sh entry, found $COEXIST_ADAPTER_COUNT"
fi

if [[ "$COEXIST_DIAL_AUDIT_COUNT" -ge 1 ]]; then
  pass "coexistence: subagent_stop_dial_audit.py entry present after install-sandbox-hook.sh"
else
  fail "coexistence: subagent_stop_dial_audit.py entry missing"
fi

# --- .gitignore (D#2235) ---
echo ""
echo "--- .gitignore: installed after bootstrap ---"
assert_file "$TARGET/.gitignore"

echo ""
echo "--- .gitignore: ignores actually take effect ---"
# Commit the bootstrap output BEFORE checking git status (D#2249). In an
# uncommitted repo, the whole of an untracked backend/ collapses into a
# single "?? backend/" entry regardless of any ignore rule, so checking
# git status against an uncommitted target passes whether or not the
# .gitignore works. Committing first makes backend/ tracked, so a later
# untracked backend/__pycache__/ surfaces as its own entry that a working
# .gitignore actually has to suppress.
git -C "$TARGET" add -A
git -C "$TARGET" -c user.email=test@example.invalid -c user.name=test commit -qm 'bootstrap output'
# Precondition — without this the check below is vacuous (D#2249).
if ! git -C "$TARGET" rev-parse --verify -q HEAD >/dev/null \
   || [[ -z "$(git -C "$TARGET" ls-files backend/spawn_templates)" ]]; then
  fail "AC3 precondition: \$TARGET must have a commit with backend/ tracked, or this check cannot fail"
else
  mkdir -p "$TARGET/backend/__pycache__"
  touch "$TARGET/backend/__pycache__/x.pyc"
  if git -C "$TARGET" status --porcelain | grep -q pycache; then
    fail "__pycache__ shows up in git status despite installed .gitignore"
  else
    pass "__pycache__ is ignored by the installed .gitignore"
  fi
  # Order-independent cross-check: fails cleanly on pre-fix source without
  # depending on git status's untracked-directory collapsing behaviour.
  if git -C "$TARGET" check-ignore -q backend/__pycache__/x.pyc; then
    pass "git check-ignore confirms backend/__pycache__/x.pyc is ignored"
  else
    fail "git check-ignore does not consider backend/__pycache__/x.pyc ignored"
  fi
  rm -rf "$TARGET/backend/__pycache__"
fi

echo ""
echo "--- .gitignore: exactly one managed block after repeated bootstrap runs ---"
GI_MARKER_COUNT=$(grep -c 'AUTONOMOUS_TEAM_BOOTSTRAP_GITIGNORE_START' "$TARGET/.gitignore" || true)
if [[ "$GI_MARKER_COUNT" -eq 1 ]]; then
  pass "exactly one managed .gitignore block in $TARGET/.gitignore"
else
  fail "expected exactly 1 managed .gitignore block, found $GI_MARKER_COUNT"
fi

echo ""
echo "--- .gitignore: pre-existing adopter rules are preserved, in order ---"
GI_CUSTOM_TARGET="$RUN_TMP/test-cold-start-gitignore-custom"
rm -rf "$GI_CUSTOM_TARGET"
mkdir -p "$GI_CUSTOM_TARGET"
git -C "$GI_CUSTOM_TARGET" init -q
echo "my-custom-rule/" > "$GI_CUSTOM_TARGET/.gitignore"
bash "$BOOTSTRAP" --repo acme/test-cold-start-gitignore-custom "$GI_CUSTOM_TARGET" > $RUN_TMP/bootstrap-gitignore-custom.log 2>&1
GI_CUSTOM_FIRST_LINE=$(head -1 "$GI_CUSTOM_TARGET/.gitignore")
if [[ "$GI_CUSTOM_FIRST_LINE" == "my-custom-rule/" ]]; then
  pass "pre-existing .gitignore rule stays at line 1 after bootstrap"
else
  fail "pre-existing .gitignore rule was disturbed — line 1 is: $GI_CUSTOM_FIRST_LINE"
fi

echo ""
echo "--- .gitignore: never clobbers a target that already carries the block ---"
cp "$TARGET/.gitignore" $RUN_TMP/gitignore-before-reclobber-check
bash "$BOOTSTRAP" --repo acme/test-cold-start --force "$TARGET" > $RUN_TMP/bootstrap-gitignore-reclobber.log 2>&1
if diff -q $RUN_TMP/gitignore-before-reclobber-check "$TARGET/.gitignore" > /dev/null; then
  pass "re-running bootstrap on a target with the managed block leaves .gitignore byte-identical"
else
  fail "re-running bootstrap changed a .gitignore that already carried the managed block"
fi

echo ""
echo "--- .gitignore: --dry-run writes nothing ---"
GI_DRY_TARGET="$RUN_TMP/test-cold-start-gitignore-dry"
rm -rf "$GI_DRY_TARGET"
mkdir -p "$GI_DRY_TARGET"
git -C "$GI_DRY_TARGET" init -q
bash "$BOOTSTRAP" --repo acme/test-cold-start-gitignore-dry --dry-run "$GI_DRY_TARGET" > $RUN_TMP/bootstrap-gitignore-dry.log 2>&1
if [[ -f "$GI_DRY_TARGET/.gitignore" ]]; then
  fail "--dry-run wrote $GI_DRY_TARGET/.gitignore"
else
  pass "--dry-run wrote no .gitignore"
fi

echo ""
echo "--- .gitignore template: classification documented ---"
GITIGNORE_TEMPLATE_PATH="$REPO_ROOT/loop-bootstrap/templates/.gitignore.template"
if grep -qi 'commit' "$GITIGNORE_TEMPLATE_PATH" && grep -qi 'never commit' "$GITIGNORE_TEMPLATE_PATH"; then
  pass "gitignore template documents the commit vs. never-commit classification"
else
  fail "gitignore template is missing the commit / never-commit classification header"
fi

echo ""
echo "--- .gitignore template: symlink and anchoring spellings survive ---"
if grep -qx '\.autonomous-team/blackboard' "$GITIGNORE_TEMPLATE_PATH"; then
  pass "template carries slashless .autonomous-team/blackboard (matches symlink form)"
else
  fail "template is missing the slashless .autonomous-team/blackboard entry"
fi
if grep -qx '/blackboard' "$GITIGNORE_TEMPLATE_PATH"; then
  pass "template carries anchored /blackboard (does not swallow backend/blackboard.py)"
else
  fail "template is missing the anchored /blackboard entry"
fi

# --- Read-only source tree (D#2249) ---
echo ""
echo "--- bootstrap runs from a read-only source tree ---"
# Regression for the mode-propagation defect: cp/rsync inherit the source
# tree's permission bits, so bootstrap.sh writing into a file it just copied
# from a read-only source (a verify_tree_build tree, a Nix store path, a
# restrictive CI checkout) used to fail. A cheaper "every installed file
# ends up writable" assertion would pass vacuously against any normal
# checkout, where the sources are already writable — exactly the defect
# class this Discussion exists to stop us shipping — so this clones HEAD
# read-only and runs a real bootstrap from it, the smallest form that can
# actually fail. Cloning HEAD (not the working tree) means uncommitted local
# edits aren't exercised here — fine for CI and for this PR's own branch.
RO_SRC="$RUN_TMP/test-cold-start-ro-src"
RO_TARGET="$RUN_TMP/test-cold-start-ro-target"
rm -rf "$RO_SRC" "$RO_TARGET"
git clone --quiet --shared --revision="$(git -C "$REPO_ROOT" rev-parse HEAD)" "$REPO_ROOT" "$RO_SRC"
find "$RO_SRC" -type f -not -path '*/.git/*' -exec chmod a-w {} +
mkdir -p "$RO_TARGET"
git -C "$RO_TARGET" init -q

RO_RC=0
bash "$RO_SRC/loop-bootstrap/bootstrap.sh" --repo acme/ro-src "$RO_TARGET" > "$RUN_TMP/bootstrap-ro-src.log" 2>&1 || RO_RC=$?
if [[ "$RO_RC" -eq 0 ]]; then
  pass "bootstrap.sh exits 0 when run from a read-only source tree"
else
  fail "bootstrap.sh exited $RO_RC from a read-only source tree (see $RUN_TMP/bootstrap-ro-src.log)"
fi

RO_REPO_FIELD=$(python3 -c "
import json
try:
    print(json.load(open('$RO_TARGET/.autonomous-team/config.json'))['repo'])
except Exception as e:
    print('ERROR: ' + str(e))
" 2>/dev/null)
if [[ "$RO_REPO_FIELD" == "acme/ro-src" ]]; then
  pass "config.json is readable and carries the repo field after a read-only-source bootstrap"
else
  fail "config.json missing/unreadable after read-only-source bootstrap (got: $RO_REPO_FIELD)"
fi

# Restore write bits so /tmp cleanup (and a re-run of this suite) doesn't choke on it.
chmod -R u+w "$RO_SRC" 2>/dev/null || true

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
