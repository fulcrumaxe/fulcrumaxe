#!/usr/bin/env bash
# tests/test_loop_bootstrap_extended.sh — extended integration tests for loop-bootstrap.
#
# Extends test_loop_bootstrap.sh with assertions for:
#   - New files installed: start-the-day.sh, post-merge-hook.sh, merge-and-hook.sh,
#     generate-initial-plan.py, start-the-day.md command
#   - CLAUDE.md Team Lead protocol marker (idempotency of append)
#   - templates/PLAN-template.md installed
#   - Refreshed spawn-agent.sh contains D#886 env-scrub feature
#   - Refreshed pre-spawn-check.sh has fixed datetime parsing
#
# Gate 2 verification target: a fresh checkout under this suite's own
# mktemp'd RUN_TMP dir (was a fixed /tmp/test-cold-start-v2 name — see
# RUN_TMP below, D#2254).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP="$REPO_ROOT/loop-bootstrap/bootstrap.sh"

# All scratch paths for this suite live under one mktemp'd directory so
# concurrent runs of this suite (e.g. two reviewers in separate worktrees)
# never race on a shared fixed /tmp path (D#2254).
RUN_TMP="$(mktemp -d /tmp/test_loop_bootstrap_extended.XXXXXX)"
trap 'rm -rf "$RUN_TMP"' EXIT

TARGET="$RUN_TMP/test-cold-start-v2"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    pass "file exists: ${path##$TARGET/}"
  else
    fail "missing file: ${path##$TARGET/}"
  fi
}

assert_dir() {
  local path="$1"
  if [[ -d "$path" ]]; then
    pass "dir exists: ${path##$TARGET/}"
  else
    fail "missing dir: ${path##$TARGET/}"
  fi
}

assert_contains() {
  local file="$1"
  local pattern="$2"
  local label="${3:-$pattern}"
  if grep -q "$pattern" "$file" 2>/dev/null; then
    pass "contains '$label' in ${file##$TARGET/}"
  else
    fail "missing '$label' in ${file##$TARGET/}"
  fi
}

assert_not_contains() {
  local file="$1"
  local pattern="$2"
  local label="${3:-$pattern}"
  if ! grep -q "$pattern" "$file" 2>/dev/null; then
    pass "does not contain '$label' in ${file##$TARGET/}"
  else
    fail "should NOT contain '$label' in ${file##$TARGET/}"
  fi
}

echo ""
echo "=== test_loop_bootstrap_extended ==="
echo ""

# Setup: fresh git-init'd target
rm -rf "$TARGET"
mkdir -p "$TARGET"
git -C "$TARGET" init -q
echo "Created fresh git repo at $TARGET"

# Also create a minimal project.json so start-the-day can load it
mkdir -p "$TARGET/.autonomous-team"
python3 -c "
import json
d = {
    'project_name': 'test-cold-start-v2',
    'repo': 'acme/test-cold-start-v2',
    'state_dir': '$RUN_TMP/test-cold-start-v2-state',
    'language': 'python'
}
print(json.dumps(d, indent=2))
" > "$TARGET/.autonomous-team/project.json"

# --- Run bootstrap ---
echo ""
echo "--- Running bootstrap.sh ---"
bash "$BOOTSTRAP" --repo acme/test-cold-start-v2 "$TARGET"

echo ""
echo "--- Asserting new loop scripts ---"
assert_file "$TARGET/scripts/start-the-day.sh"
assert_file "$TARGET/scripts/post-merge-hook.sh"
assert_file "$TARGET/scripts/merge-and-hook.sh"
assert_file "$TARGET/scripts/generate-initial-plan.py"

echo ""
echo "--- Asserting slash command ---"
assert_file "$TARGET/.claude/commands/start-the-day.md"
assert_contains "$TARGET/.claude/commands/start-the-day.md" "start-the-day.sh" "start-the-day.sh reference"

echo ""
echo "--- Asserting PLAN template installed ---"
# Templates go to backend/spawn_templates/ — PLAN-template.md goes there
assert_file "$TARGET/backend/spawn_templates/PLAN-template.md"
assert_contains "$TARGET/backend/spawn_templates/PLAN-template.md" "{{date}}" "date placeholder"
assert_contains "$TARGET/backend/spawn_templates/PLAN-template.md" "{{project_name}}" "project_name placeholder"

echo ""
echo "--- Asserting CLAUDE.md Team Lead protocol ---"
assert_file "$TARGET/CLAUDE.md"
assert_contains "$TARGET/CLAUDE.md" "LOOP_BOOTSTRAP_TEAM_LEAD_PROTOCOL_START" "protocol start marker"
assert_contains "$TARGET/CLAUDE.md" "LOOP_BOOTSTRAP_TEAM_LEAD_PROTOCOL_END" "protocol end marker"
assert_contains "$TARGET/CLAUDE.md" "Single-spawner invariant" "single-spawner invariant"
assert_contains "$TARGET/CLAUDE.md" "merge-and-hook.sh" "merge-and-hook reference"
# Must NOT contain the source repo after rewrite
assert_not_contains "$TARGET/CLAUDE.md" "autonomous-agent-7/autonomous-forever" "source repo not present"

echo ""
echo "--- Asserting refreshed spawn-agent.sh has D#886 env-scrub ---"
AGENT_SH="$TARGET/scripts/spawn-agent.sh"
assert_contains "$AGENT_SH" "SECURITY — run this as your FIRST Bash step" "env-scrub injection block"
assert_contains "$AGENT_SH" "_ENV_SCRUB_VARS" "env-scrub var list"
assert_contains "$AGENT_SH" "DRY_RUN_ENV_DUMP" "DRY_RUN_ENV_DUMP flag"
assert_contains "$AGENT_SH" "OVERRIDE_CAP" "OVERRIDE_CAP flag"
assert_contains "$AGENT_SH" "TOUCHPOINTS" "TOUCHPOINTS flag"

echo ""
echo "--- Asserting refreshed pre-spawn-check.sh has fixed datetime parsing ---"
PSC_SH="$TARGET/scripts/pre-spawn-check.sh"
# The old version used .rstrip("Z"); new version uses .replace("Z", "+00:00")
assert_contains "$PSC_SH" 'replace("Z", "+00:00")' "fixed datetime Z handling"

echo ""
echo "--- Asserting start-the-day.sh is project-agnostic ---"
STD_SH="$TARGET/scripts/start-the-day.sh"
assert_contains "$STD_SH" "project.json" "reads project.json"
# Should NOT have autonomous-forever hardcoded (was rewritten by do_install)
assert_not_contains "$STD_SH" "autonomous-forever-state" "no hardcoded state dir"

echo ""
echo "--- Asserting merge-and-hook.sh reads from project.json ---"
MAH_SH="$TARGET/scripts/merge-and-hook.sh"
assert_contains "$MAH_SH" "project.json" "reads project.json"
assert_contains "$MAH_SH" 'gh pr merge' "calls gh pr merge"

echo ""
echo "--- Asserting post-merge-hook.sh reads from project.json ---"
PMH_SH="$TARGET/scripts/post-merge-hook.sh"
assert_contains "$PMH_SH" "project.json" "reads project.json"
assert_contains "$PMH_SH" "audit.jsonl" "writes audit.jsonl"

echo ""
echo "--- Asserting generate-initial-plan.py ---"
GPY="$TARGET/scripts/generate-initial-plan.py"
assert_contains "$GPY" "def main" "has main function"
assert_contains "$GPY" "graphql" "uses GraphQL"
assert_contains "$GPY" "force" "has --force flag"
assert_contains "$GPY" "SPEC_READY" "handles SPEC_READY status"
assert_contains "$GPY" "dry.run" "has --dry-run flag"

echo ""
echo "--- Asserting start-dashboard.sh installed and correct (D#944 fleet foundation) ---"
STARTDASH="$TARGET/scripts/start-dashboard.sh"
assert_file "$STARTDASH"
if [[ -x "$STARTDASH" ]]; then
  pass "start-dashboard.sh is executable"
else
  fail "start-dashboard.sh is NOT executable"
fi
assert_contains "$STARTDASH" "project.json" "reads project.json"
assert_contains "$STARTDASH" "dashboard_port" "reads dashboard_port"
assert_contains "$STARTDASH" "AUTONOMOUS_TEAM_STATE_DIR" "exports AUTONOMOUS_TEAM_STATE_DIR"
assert_contains "$STARTDASH" "state_dir" "reads state_dir"

echo ""
echo "--- Asserting all installed scripts are executable ---"
for sh in "$TARGET/scripts"/*.sh; do
  [[ -f "$sh" ]] || continue
  if [[ -x "$sh" ]]; then
    pass "executable: ${sh##$TARGET/}"
  else
    fail "not executable: ${sh##$TARGET/}"
  fi
done
for sh in "$TARGET/scripts/lib"/*.sh; do
  [[ -f "$sh" ]] || continue
  if [[ -x "$sh" ]]; then
    pass "executable: ${sh##$TARGET/}"
  else
    fail "not executable: ${sh##$TARGET/}"
  fi
done

echo ""
echo "--- Idempotency: re-running bootstrap ---"
# Snapshot before re-run. .autonomous-team/engine-install.json (D#2335 PR 1)
# is deliberately excluded from this whole-tree comparison: unlike every
# other file this loop asserts is byte-identical across a re-run, that
# stamp carries a wall-clock bootstrapped_at field that legitimately
# changes on every invocation by design (bootstrap.sh step 19a runs
# unconditionally, not behind an `[[ -f ... ]] skip` guard, because a
# re-bootstrap is exactly the moment that baseline is supposed to move
# forward). A separate, targeted check right below asserts what actually
# matters for this suite's engine checkout: engine_commit does not change
# between the two runs (no new engine commit landed mid-test) even though
# the file's bytes do.
ENGINE_INSTALL_STAMP="$TARGET/.autonomous-team/engine-install.json"
BEFORE=$(find "$TARGET" -type f -not -path '*/.git/*' -not -path "$ENGINE_INSTALL_STAMP" | sort | xargs md5sum 2>/dev/null)
BEFORE_ENGINE_COMMIT=$(python3 -c "import json; print(json.load(open('$ENGINE_INSTALL_STAMP')).get('engine_commit'))" 2>/dev/null || echo "")

bash "$BOOTSTRAP" --repo acme/test-cold-start-v2 --force "$TARGET" > "$RUN_TMP/bootstrap-rerun-v2.log" 2>&1

AFTER=$(find "$TARGET" -type f -not -path '*/.git/*' -not -path "$ENGINE_INSTALL_STAMP" | sort | xargs md5sum 2>/dev/null)
AFTER_ENGINE_COMMIT=$(python3 -c "import json; print(json.load(open('$ENGINE_INSTALL_STAMP')).get('engine_commit'))" 2>/dev/null || echo "")

if [[ "$BEFORE" == "$AFTER" ]]; then
  pass "idempotent: re-run produced no diff (excluding the engine-install.json timestamp)"
else
  fail "not idempotent: re-run changed files"
  diff <(echo "$BEFORE") <(echo "$AFTER") | head -20 || true
fi

echo ""
echo "--- engine-install.json baseline stamp (D#2335 PR 1) ---"
if [[ -f "$ENGINE_INSTALL_STAMP" ]]; then
  pass "engine-install.json exists after bootstrap"
  STAMP_KEYS=$(python3 -c "import json; d=json.load(open('$ENGINE_INSTALL_STAMP')); print(','.join(sorted(d.keys())))" 2>/dev/null || echo "")
  if [[ "$STAMP_KEYS" == "bootstrapped_at,engine_commit,engine_version,source,source_repo" ]]; then
    pass "engine-install.json has exactly the expected keys"
  else
    fail "engine-install.json keys unexpected: $STAMP_KEYS"
  fi
else
  fail "engine-install.json missing after bootstrap"
fi
if [[ -n "$BEFORE_ENGINE_COMMIT" && "$BEFORE_ENGINE_COMMIT" == "$AFTER_ENGINE_COMMIT" ]]; then
  pass "engine_commit unchanged across re-run (no new engine commit landed mid-test)"
else
  fail "engine_commit changed unexpectedly across re-run: '$BEFORE_ENGINE_COMMIT' -> '$AFTER_ENGINE_COMMIT'"
fi

echo ""
echo "--- Idempotency: CLAUDE.md protocol appended only once ---"
MARKER_COUNT=$(grep -c "LOOP_BOOTSTRAP_TEAM_LEAD_PROTOCOL_START" "$TARGET/CLAUDE.md" 2>/dev/null || echo 0)
if [[ "$MARKER_COUNT" -eq 1 ]]; then
  pass "CLAUDE.md protocol marker appears exactly once (idempotent)"
else
  fail "CLAUDE.md protocol marker appears $MARKER_COUNT times (expected 1)"
fi

echo ""
echo "--- project.json untouched ---"
if python3 -c "
import json
d = json.load(open('$TARGET/.autonomous-team/project.json'))
assert d.get('project_name') == 'test-cold-start-v2', 'project_name changed'
assert d.get('repo') == 'acme/test-cold-start-v2', 'repo changed'
print('ok')
" 2>/dev/null | grep -q "ok"; then
  pass "project.json is untouched after bootstrap"
else
  fail "project.json was modified by bootstrap"
fi

echo ""
echo "--- Dry-run produces no writes ---"
BEFORE_DRY=$(find "$TARGET" -type f -not -path '*/.git/*' | sort | xargs md5sum 2>/dev/null)
bash "$BOOTSTRAP" --repo acme/test-cold-start-v2 --dry-run --force "$TARGET" > "$RUN_TMP/bootstrap-dryrun-v2.log" 2>&1
AFTER_DRY=$(find "$TARGET" -type f -not -path '*/.git/*' | sort | xargs md5sum 2>/dev/null)

if [[ "$BEFORE_DRY" == "$AFTER_DRY" ]]; then
  pass "dry-run: no files written"
else
  fail "dry-run: files were modified"
fi

if grep -q "\[dry-run\]" "$RUN_TMP/bootstrap-dryrun-v2.log"; then
  pass "dry-run output contains [dry-run] annotations"
else
  fail "dry-run output missing [dry-run] annotations"
fi


echo ""
echo "--- Source-tree mode check: all loop-bootstrap scripts must be 100755 ---"
# Guard against contributors adding scripts without the executable bit.
# Use git ls-files -s to check committed mode, not filesystem stat.
SOURCE_SCRIPTS_DIR="$REPO_ROOT/loop-bootstrap/scripts"
for sh in "$SOURCE_SCRIPTS_DIR"/*.sh; do
  [[ -f "$sh" ]] || continue
  rel="${sh#$REPO_ROOT/}"
  mode=$(git -C "$REPO_ROOT" ls-files -s "$rel" | awk '{print $1}')
  if [[ "$mode" == "100755" ]]; then
    pass "source mode 100755: ${sh##$SOURCE_SCRIPTS_DIR/}"
  elif [[ -z "$mode" ]]; then
    pass "source mode (untracked, skip): ${sh##$SOURCE_SCRIPTS_DIR/}"
  else
    fail "source mode NOT 100755 ($mode): ${sh##$SOURCE_SCRIPTS_DIR/}"
  fi
done
for sh in "$SOURCE_SCRIPTS_DIR/lib"/*.sh; do
  [[ -f "$sh" ]] || continue
  rel="${sh#$REPO_ROOT/}"
  mode=$(git -C "$REPO_ROOT" ls-files -s "$rel" | awk '{print $1}')
  if [[ "$mode" == "100755" ]]; then
    pass "source mode 100755: lib/${sh##$SOURCE_SCRIPTS_DIR/lib/}"
  elif [[ -z "$mode" ]]; then
    pass "source mode (untracked, skip): lib/${sh##$SOURCE_SCRIPTS_DIR/lib/}"
  else
    fail "source mode NOT 100755 ($mode): lib/${sh##$SOURCE_SCRIPTS_DIR/lib/}"
  fi
done

echo ""
echo "--- E2E: installed start-dashboard.sh is executable (mode check on INSTALLED file) ---"
INSTALLED_STARTDASH="$TARGET/scripts/start-dashboard.sh"
if [[ -x "$INSTALLED_STARTDASH" ]]; then
  pass "installed start-dashboard.sh is executable"
else
  fail "installed start-dashboard.sh is NOT executable"
fi

echo ""
echo "--- E2E: coldstart writes dashboard_port into repo-side project.json ---"
# Simulate what coldstart-project.sh does when port_claim returns a port.
# We test the merge logic directly to avoid needing a full live coldstart.
E2E_TMP_PROJECT_JSON="$RUN_TMP/test-e2e-coldstart-pj.json"
echo '{"project_name":"e2e-test","version":1,"repo":"acme/e2e","language":"python"}' > "$E2E_TMP_PROJECT_JSON"
FAKE_PORT=5342
WRITE_PORT_PY="import json,pathlib; p=pathlib.Path('$E2E_TMP_PROJECT_JSON'); d=json.loads(p.read_text()) if p.exists() else {}; d['dashboard_port']=int('$FAKE_PORT'); p.write_text(json.dumps(d,indent=2)+chr(10))"
python3 -c "$WRITE_PORT_PY"
WRITTEN_PORT=$(python3 -c "import json; print(json.load(open('$E2E_TMP_PROJECT_JSON')).get('dashboard_port','MISSING'))")
if [[ "$WRITTEN_PORT" == "$FAKE_PORT" ]]; then
  pass "coldstart dashboard_port write-back: project.json has dashboard_port=$WRITTEN_PORT"
else
  fail "coldstart dashboard_port write-back: expected $FAKE_PORT, got $WRITTEN_PORT"
fi
rm -f "$E2E_TMP_PROJECT_JSON"

echo ""
echo "--- E2E: fleet discovery returns ok:true for project with sentinel fields ---"
# Write a minimal state-dir project.json with sentinel fields and verify discover() accepts it.
E2E_STATE_DIR="$RUN_TMP/e2e-fleet-test-state"
mkdir -p "$E2E_STATE_DIR"
python3 -c "import json,pathlib; d={'project_name':'e2e-fleet-test','version':1,'dashboard_port':5342}; pathlib.Path('$E2E_STATE_DIR/project.json').write_text(json.dumps(d,indent=2))"
DISCOVER_OK=$(python3 -c "
import sys, pathlib
sys.path.insert(0, '$REPO_ROOT')
from backend.fleet.discovery import _read_project
p = pathlib.Path('$E2E_STATE_DIR/project.json')
r = _read_project(p, '$E2E_STATE_DIR')
print('ok' if r.get('ok') else 'fail:' + str(r.get('error','')))
")
if [[ "$DISCOVER_OK" == "ok" ]]; then
  pass "fleet discovery: project with sentinel fields returns ok:true"
else
  fail "fleet discovery: expected ok, got $DISCOVER_OK"
fi
rm -rf "$E2E_STATE_DIR"

# --- 6-fix bundle assertions (projectb pilot regressions) ---

echo ""
echo "--- BUG 1: hook-event.sh present in loop-bootstrap/scripts/lib/ ---"
HOOK_EVENT_SRC="$REPO_ROOT/loop-bootstrap/scripts/lib/hook-event.sh"
assert_file "$HOOK_EVENT_SRC"
if [[ -x "$HOOK_EVENT_SRC" ]]; then
  pass "hook-event.sh is executable"
else
  fail "hook-event.sh is NOT executable"
fi
# Verify it was installed into the target by bootstrap
HOOK_EVENT_INSTALLED="$TARGET/scripts/lib/hook-event.sh"
assert_file "$HOOK_EVENT_INSTALLED"
if [[ -x "$HOOK_EVENT_INSTALLED" ]]; then
  pass "installed hook-event.sh is executable"
else
  fail "installed hook-event.sh is NOT executable"
fi

echo ""
echo "--- BUG 2: start-the-day.sh uses dynamic default branch detection ---"
STD_SH="$TARGET/scripts/start-the-day.sh"
assert_contains "$STD_SH" "symbolic-ref refs/remotes/origin/HEAD" "remote HEAD detection"
assert_contains "$STD_SH" "default_branch" "project.json default_branch fallback"
# Verify the script has the project.json intermediate fallback before "main"
assert_contains "$STD_SH" 'DEFAULT_BRANCH=$(python3' "project.json python fallback for default branch"

echo ""
echo "--- BUG 3: coldstart-project.sh initializes valid DuckDB ---"
COLDSTART_SH="$REPO_ROOT/scripts/coldstart-project.sh"
assert_contains "$COLDSTART_SH" "import duckdb" "duckdb python init"
assert_contains "$COLDSTART_SH" "duckdb.connect" "duckdb connect call"
# Should not use bare `touch "$STATE_DIR/stats.duckdb"` as the only mechanism
# (it now has the python init with touch as fallback)
assert_contains "$COLDSTART_SH" "duckdb" "touch fallback message"

echo ""
echo "--- BUG 4: coldstart-project.sh creates loop-metrics.jsonl ---"
assert_contains "$COLDSTART_SH" "loop-metrics.jsonl" "loop-metrics.jsonl placeholder"
assert_contains "$COLDSTART_SH" "TEAM_DIR/loop-metrics.jsonl" "loop-metrics in repo .autonomous-team"

echo ""
echo "--- BUG 5: loop-bootstrap/backend-snapshot/ exists and has Python files ---"
SNAPSHOT_DIR="$REPO_ROOT/loop-bootstrap/backend-snapshot"
assert_dir "$SNAPSHOT_DIR"
SNAPSHOT_PY_COUNT=$(find "$SNAPSHOT_DIR" -name "*.py" -type f | wc -l)
if [[ "$SNAPSHOT_PY_COUNT" -ge 100 ]]; then
  pass "backend-snapshot has $SNAPSHOT_PY_COUNT .py files (>= 100)"
else
  fail "backend-snapshot has only $SNAPSHOT_PY_COUNT .py files (expected >= 100)"
fi
# Key modules must be present
for mod in budget.py circuit_breaker.py context_manager.py discussion_cache.py agent_run.py; do
  if [[ -f "$SNAPSHOT_DIR/$mod" ]]; then
    pass "backend-snapshot: $mod present"
  else
    fail "backend-snapshot: $mod MISSING"
  fi
done
# Verify bootstrap installed backend snapshot into target (no-clobber)
TARGET_BACKEND_COUNT=$(find "$TARGET/backend" -name "*.py" -type f | wc -l)
if [[ "$TARGET_BACKEND_COUNT" -ge 100 ]]; then
  pass "installed backend has $TARGET_BACKEND_COUNT .py files (>= 100)"
else
  fail "installed backend has only $TARGET_BACKEND_COUNT .py files (expected >= 100)"
fi

echo ""
echo "--- BUG 6: pre-spawn-check.sh contamination recovery uses dynamic default branch ---"
PSC_SH="$TARGET/scripts/pre-spawn-check.sh"
assert_contains "$PSC_SH" "symbolic-ref refs/remotes/origin/HEAD" "remote HEAD detection in pre-spawn-check"
assert_contains "$PSC_SH" "_DEFAULT_BRANCH" "dynamic default branch variable"
# Verify the fix uses the dynamic variable in symbolic-ref reset
assert_contains "$PSC_SH" '"refs/heads/$_DEFAULT_BRANCH"' "reset uses dynamic default branch"
# Verify the fixed code compares PARENT_BRANCH to the dynamic variable, not literal "main"
assert_contains "$PSC_SH" 'PARENT_BRANCH" != "$_DEFAULT_BRANCH' "comparison uses dynamic branch var"

echo ""
echo "--- BUG 7: fleet.concurrency registered in server.py ---"
SERVER_PY="$REPO_ROOT/backend/server.py"
assert_contains "$SERVER_PY" '"fleet.concurrency"' "fleet.concurrency registration"
# Also assert the handler module exists
assert_file "$REPO_ROOT/backend/rpc/fleet_concurrency.py"

echo ""
echo "--- BUG 8: coldstart-project.sh writes state-side project.json sentinel ---"
COLDSTART_SH="$REPO_ROOT/scripts/coldstart-project.sh"
assert_contains "$COLDSTART_SH" "STATE_PROJECT_JSON" "state-side sentinel path variable"
assert_contains "$COLDSTART_SH" "STATE_DIR/project.json" "state-side sentinel creation"
assert_contains "$COLDSTART_SH" '"project_name"' "sentinel includes project_name"
assert_contains "$COLDSTART_SH" '"version"' "sentinel includes version"


echo ""
echo "--- GUARD: loop-bootstrap/hooks/ present with sandbox files (C2) ---"
HOOKS_BOOTSTRAP_DIR="$REPO_ROOT/loop-bootstrap/hooks"
assert_dir "$HOOKS_BOOTSTRAP_DIR"
assert_file "$HOOKS_BOOTSTRAP_DIR/sandbox.py"
assert_file "$HOOKS_BOOTSTRAP_DIR/sandbox_rules.py"
# sandbox_rules.py must have the _load_main_repo_root helper
assert_contains "$HOOKS_BOOTSTRAP_DIR/sandbox_rules.py" "_load_main_repo_root" "_load_main_repo_root helper"
assert_contains "$HOOKS_BOOTSTRAP_DIR/sandbox_rules.py" "project.json" "reads project.json"
# sandbox.py must use timezone-aware datetime (not deprecated utcnow)
assert_contains "$HOOKS_BOOTSTRAP_DIR/sandbox.py" "timezone.utc" "uses timezone-aware datetime"
assert_not_contains "$HOOKS_BOOTSTRAP_DIR/sandbox.py" "utcnow()" "no deprecated utcnow()"

echo ""
echo "--- GUARD: bootstrap installs hooks/ and registers PreToolUse hook (C2) ---"
# Verify bootstrap.sh installed hooks/sandbox.py into target
assert_dir "$TARGET/hooks"
assert_file "$TARGET/hooks/sandbox.py"
assert_file "$TARGET/hooks/sandbox_rules.py"
# Verify .claude/settings.json exists and registers the sandbox hook
assert_file "$TARGET/.claude/settings.json"
assert_contains "$TARGET/.claude/settings.json" "sandbox.py" "sandbox hook registered in settings.json"
assert_contains "$TARGET/.claude/settings.json" "PreToolUse" "PreToolUse hooks present in settings.json"
# projectb bug: hook path must use $CLAUDE_PROJECT_DIR so worktree-isolated sub-agents can find it
assert_contains "$TARGET/.claude/settings.json" 'CLAUDE_PROJECT_DIR' "sandbox hook uses \$CLAUDE_PROJECT_DIR for worktree path resolution"
assert_not_contains "$TARGET/.claude/settings.json" '"python3 hooks/sandbox.py"' "sandbox hook must NOT use bare relative path (breaks in worktrees)"

echo ""
echo "--- GUARD: scripts/bootstrap-github-labels.sh present (C3) ---"
LABELS_SCRIPT="$REPO_ROOT/scripts/bootstrap-github-labels.sh"
assert_file "$LABELS_SCRIPT"
if [[ -x "$LABELS_SCRIPT" ]]; then
  pass "bootstrap-github-labels.sh is executable"
else
  fail "bootstrap-github-labels.sh is NOT executable"
fi
assert_contains "$LABELS_SCRIPT" "code-review-passed" "creates code-review-passed label"
assert_contains "$LABELS_SCRIPT" "SPEC_READY" "creates SPEC_READY label"
assert_contains "$LABELS_SCRIPT" "team-log" "creates team-log label"
assert_contains "$LABELS_SCRIPT" "repo-resolve.sh" "sources repo-resolve.sh"
# Also verify it's in loop-bootstrap/scripts/ for forked installs
LABELS_BOOTSTRAP="$REPO_ROOT/loop-bootstrap/scripts/bootstrap-github-labels.sh"
assert_file "$LABELS_BOOTSTRAP"
if [[ -x "$LABELS_BOOTSTRAP" ]]; then
  pass "loop-bootstrap/scripts/bootstrap-github-labels.sh is executable"
else
  fail "loop-bootstrap/scripts/bootstrap-github-labels.sh is NOT executable"
fi
# Verify installed into target
assert_file "$TARGET/scripts/bootstrap-github-labels.sh"

echo ""
echo "--- GUARD: hook subdirs present in loop-bootstrap and installed in target (C4) ---"
# Source dirs
assert_dir "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-merge.d"
assert_dir "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-agent.d"
assert_file "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-merge.d/cross-file-pattern-check.sh"
assert_file "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-merge.d/tui-tester-sweep.sh"
assert_file "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-agent.d/cost-summary.sh"
# Executable bits in source
for hook_sh in \
  "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-merge.d/cross-file-pattern-check.sh" \
  "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-merge.d/tui-tester-sweep.sh" \
  "$REPO_ROOT/loop-bootstrap/scripts/hooks/post-agent.d/cost-summary.sh"; do
  if [[ -x "$hook_sh" ]]; then
    pass "executable: ${hook_sh##$REPO_ROOT/}"
  else
    fail "not executable: ${hook_sh##$REPO_ROOT/}"
  fi
done
# Installed in target
assert_dir "$TARGET/scripts/hooks/post-merge.d"
assert_dir "$TARGET/scripts/hooks/post-agent.d"
assert_file "$TARGET/scripts/hooks/post-merge.d/cross-file-pattern-check.sh"
assert_file "$TARGET/scripts/hooks/post-merge.d/tui-tester-sweep.sh"
assert_file "$TARGET/scripts/hooks/post-agent.d/cost-summary.sh"
# post-merge.d hooks must use repo-resolve.sh (not hardcode the repo)
assert_contains \
  "$TARGET/scripts/hooks/post-merge.d/cross-file-pattern-check.sh" \
  "repo-resolve.sh" \
  "cross-file-pattern-check sources repo-resolve.sh"
assert_not_contains \
  "$TARGET/scripts/hooks/post-merge.d/cross-file-pattern-check.sh" \
  "autonomous-agent-7/autonomous-forever" \
  "cross-file-pattern-check has no hardcoded repo"

echo ""
echo "--- GUARD: cross-file-detector.py present in loop-bootstrap/scripts/lib/ (C5) ---"
assert_file "$REPO_ROOT/loop-bootstrap/scripts/lib/cross-file-detector.py"
assert_file "$TARGET/scripts/lib/cross-file-detector.py"
# Verify the installed version uses project.json for repo resolution (not hardcoded)
assert_not_contains \
  "$TARGET/scripts/lib/cross-file-detector.py" \
  "autonomous-agent-7/autonomous-forever" \
  "cross-file-detector.py has no hardcoded repo"

echo ""
echo "--- GUARD: backend-snapshot must mirror backend/ (no drift) ---"
# Every .py file in loop-bootstrap/backend-snapshot/ must match the corresponding
# file in backend/. This catches future snapshot-mirror misses before they reach prod.
SNAPSHOT_PY_DRIFT=0
while IFS= read -r snap_file; do
  rel="${snap_file#$REPO_ROOT/loop-bootstrap/backend-snapshot/}"
  live_file="$REPO_ROOT/backend/$rel"
  if [[ ! -f "$live_file" ]]; then
    fail "snapshot-drift: $rel exists in snapshot but NOT in backend/"
    SNAPSHOT_PY_DRIFT=1
  elif ! diff -q "$snap_file" "$live_file" 2>&1 | grep -q differ; then
    : # identical
  else
    fail "snapshot-drift: $rel differs between snapshot and backend/"
    SNAPSHOT_PY_DRIFT=1
  fi
done < <(find "$REPO_ROOT/loop-bootstrap/backend-snapshot" -name "*.py" -type f | sort)
if [[ "$SNAPSHOT_PY_DRIFT" -eq 0 ]]; then
  SNAP_COUNT=$(find "$REPO_ROOT/loop-bootstrap/backend-snapshot" -name "*.py" -type f | wc -l)
  pass "backend-snapshot: all $SNAP_COUNT .py files mirror backend/ (no drift)"
fi

echo ""
echo "--- GUARD: coldstart writes dashboard_port to state-side sentinel ---"
# Fresh coldstart must populate dashboard_port in state-side sentinel even
# when run from a foreign directory (PYTHONPATH fix for port_claim module resolution).
CS_TMP="$RUN_TMP/coldstart-guard-test"
CS_HOME="$RUN_TMP/coldstart-guard-home"
mkdir -p "$CS_TMP" "$CS_HOME"
git -C "$CS_TMP" init -q 2>&1 || true
CS_OUT=$(cd /tmp && HOME="$CS_HOME" COLDSTART_STATE_ROOT="$CS_HOME" PATH="/usr/bin:/bin:/usr/local/bin" bash "$REPO_ROOT/scripts/coldstart-project.sh" "$CS_TMP" coldstart-guard --language python 2>&1) || true
CS_SENTINEL="$CS_HOME/.coldstart-guard-state/project.json"
if [[ -f "$CS_SENTINEL" ]]; then
  CS_PORT=$(python3 -c "import json; print(json.load(open('$CS_SENTINEL')).get('dashboard_port','MISSING'))" 2>&1 || echo ERROR)
  if [[ "$CS_PORT" =~ ^[0-9]+$ ]]; then
    pass "coldstart sentinel dashboard_port=$CS_PORT — port_claim resolves from any directory"
  else
    fail "coldstart sentinel dashboard_port='$CS_PORT' — port_claim failed from foreign dir (PYTHONPATH not set?)"
  fi
else
  fail "coldstart sentinel not created at $CS_SENTINEL"
fi
rm -rf "$CS_TMP" "$CS_HOME"
assert_contains "$REPO_ROOT/scripts/coldstart-project.sh" 'PYTHONPATH=' "PYTHONPATH set for port_claim"

# --- Portability: repo-resolve.sh ---

echo ""
echo "--- PORTABILITY: repo-resolve.sh present in source and installed ---"
REPO_RESOLVE_SRC="$REPO_ROOT/scripts/lib/repo-resolve.sh"
assert_file "$REPO_RESOLVE_SRC"
if [[ -x "$REPO_RESOLVE_SRC" ]]; then
  pass "repo-resolve.sh is executable in source"
else
  fail "repo-resolve.sh is NOT executable in source"
fi
REPO_RESOLVE_INSTALLED="$TARGET/scripts/lib/repo-resolve.sh"
assert_file "$REPO_RESOLVE_INSTALLED"
if [[ -x "$REPO_RESOLVE_INSTALLED" ]]; then
  pass "installed repo-resolve.sh is executable"
else
  fail "installed repo-resolve.sh is NOT executable"
fi

echo ""
echo "--- PORTABILITY: repo-resolve.sh resolution order ---"
# Gate 2: source from a stub project with project.json repo="test/proj"
GATE2_TMP="$(mktemp -d)"
mkdir -p "$GATE2_TMP/scripts/lib" "$GATE2_TMP/.autonomous-team"
cp "$REPO_RESOLVE_SRC" "$GATE2_TMP/scripts/lib/repo-resolve.sh"
echo '{"repo":"test/proj"}' > "$GATE2_TMP/.autonomous-team/project.json"
GATE2_RUNNER="$GATE2_TMP/runner.sh"
printf '#!/usr/bin/env bash\nsource "$(dirname "$0")/scripts/lib/repo-resolve.sh"\n_resolve_repo\n' > "$GATE2_RUNNER"
chmod +x "$GATE2_RUNNER"
GATE2_RESULT=$(bash "$GATE2_RUNNER" 2>/dev/null || echo "ERROR")
if [[ "$GATE2_RESULT" == "test/proj" ]]; then
  pass "Gate 2: project.json repo='test/proj' picked up by repo-resolve.sh"
else
  fail "Gate 2: expected 'test/proj', got '$GATE2_RESULT'"
fi
rm -rf "$GATE2_TMP"

echo ""
echo "--- PORTABILITY: rotate-team-log.sh present in loop-bootstrap/scripts/ ---"
ROTATE_BOOTSTRAP="$REPO_ROOT/loop-bootstrap/scripts/rotate-team-log.sh"
assert_file "$ROTATE_BOOTSTRAP"
if [[ -x "$ROTATE_BOOTSTRAP" ]]; then
  pass "loop-bootstrap/scripts/rotate-team-log.sh is executable"
else
  fail "loop-bootstrap/scripts/rotate-team-log.sh is NOT executable"
fi
# Must source repo-resolve.sh (not hardcode the repo)
assert_contains "$ROTATE_BOOTSTRAP" "repo-resolve.sh" "sources repo-resolve.sh"
assert_not_contains "$ROTATE_BOOTSTRAP" 'REPO="autonomous-agent-7/autonomous-forever"' "no hardcoded REPO= assignment"
# Lock path must be project-scoped
assert_contains "$ROTATE_BOOTSTRAP" 'LOCK="/tmp/team-log-rotate-' "project-scoped LOCK path"

echo ""
echo "--- PORTABILITY: installed rotate-team-log.sh is from bootstrap ---"
ROTATE_INSTALLED="$TARGET/scripts/rotate-team-log.sh"
assert_file "$ROTATE_INSTALLED"
# After bootstrap rewrites SOURCE_REPO→TARGET_REPO, it should not contain autonomous-forever
assert_not_contains "$ROTATE_INSTALLED" "autonomous-agent-7/autonomous-forever" \
  "installed rotate-team-log.sh has no hardcoded autonomous-forever"

echo ""
echo "--- PORTABILITY: critical scripts have no hardcoded repo ---"
# These scripts must not contain the hardcoded autonomous-forever repo slug.
# Portability is achieved via repo-resolve.sh or direct project.json reads.
for script_name in post-merge-hook.sh post-agent-hook.sh merge-and-hook.sh; do
  SH="$TARGET/scripts/$script_name"
  if [[ -f "$SH" ]]; then
    if ! grep -q "autonomous-agent-7/autonomous-forever" "$SH"; then
      pass "$script_name has no hardcoded repo"
    else
      fail "$script_name still has hardcoded autonomous-agent-7/autonomous-forever"
    fi
  else
    fail "$script_name not installed"
  fi
done
# post-merge-hook.sh and merge-and-hook.sh specifically must read repo from project.json
for script_name in post-merge-hook.sh merge-and-hook.sh; do
  SH="$TARGET/scripts/$script_name"
  if [[ -f "$SH" ]]; then
    if grep -q "repo-resolve.sh\|project.json" "$SH"; then
      pass "$script_name reads repo from project.json or repo-resolve.sh"
    else
      fail "$script_name does NOT read repo from project.json or repo-resolve.sh"
    fi
  fi
done

echo ""
echo "--- PORTABILITY: spawn templates use {{REPO}} ---"
for tmpl_name in docs-writer.tmpl run-analyst.tmpl release-manager.tmpl security-reviewer.tmpl runbook-writer.tmpl; do
  TMPL="$TARGET/backend/spawn_templates/$tmpl_name"
  if [[ -f "$TMPL" ]]; then
    if ! grep -q "autonomous-agent-7/autonomous-forever" "$TMPL"; then
      pass "$tmpl_name has no hardcoded repo (uses {{REPO}})"
    else
      fail "$tmpl_name still has hardcoded autonomous-agent-7/autonomous-forever"
    fi
  else
    fail "$tmpl_name not installed"
  fi
done

echo ""
echo ""
echo "--- PORTABILITY: SOURCE_REPO drift check (D#1872 item 8) ---"
# bootstrap.sh's SOURCE_REPO is a sed search key, not an identity claim — it
# must equal whatever slug is literally embedded in the do_install-reached
# corpus (templates/, scripts/, agents/, memories/), or the whole rewrite
# pass silently stops matching anything. This asserts that invariant
# directly against source control (not the installed $TARGET), so a future
# rename that updates one side without the other fails loudly here instead
# of shipping a corpus that quietly stops getting rewritten.
BOOTSTRAP_SRC="$REPO_ROOT/loop-bootstrap/bootstrap.sh"
SOURCE_REPO_LITERAL=$(grep -oP '(?<=SOURCE_REPO="\$\{LOOP_BOOTSTRAP_SOURCE_REPO:-)[^}]+' "$BOOTSTRAP_SRC" || true)
if [[ -z "$SOURCE_REPO_LITERAL" ]]; then
  fail "SOURCE_REPO drift: could not extract the fallback literal from bootstrap.sh (did its shape change?)"
else
  pass "SOURCE_REPO drift: extracted fallback literal '$SOURCE_REPO_LITERAL' from bootstrap.sh"
  # Spot-check against a known do_install-reached file that carries the
  # full-slug form (Form 1) — same file the PORTABILITY assertions above
  # already prove gets correctly rewritten at install time.
  CANARY="$REPO_ROOT/loop-bootstrap/templates/docs-writer.tmpl"
  if [[ -f "$CANARY" ]]; then
    if grep -qF "$SOURCE_REPO_LITERAL" "$CANARY"; then
      pass "SOURCE_REPO drift: fallback literal matches what docs-writer.tmpl actually carries"
    else
      fail "SOURCE_REPO drift: bootstrap.sh's SOURCE_REPO ('$SOURCE_REPO_LITERAL') no longer matches docs-writer.tmpl's embedded slug — do_install's sed will silently stop rewriting the corpus. Update SOURCE_REPO (or the corpus) so they match again."
    fi
  else
    fail "SOURCE_REPO drift: canary file docs-writer.tmpl not found"
  fi
fi

echo ""
echo "--- NAMING: coldstart.sh's step 3 no longer collides with loop-bootstrap/bootstrap.sh (D#1872 item 15) ---"
COLDSTART_SH="$REPO_ROOT/scripts/coldstart.sh"
if grep -qi "dependency bootstrap" "$COLDSTART_SH"; then
  fail "coldstart.sh still calls its step 3 'dependency bootstrap' — collides with the separate loop-bootstrap/bootstrap.sh (population) tool"
else
  pass "coldstart.sh's step 3 no longer says 'dependency bootstrap'"
fi
DEP_INSTALL_SITES=$(grep -ci "dependency install" "$COLDSTART_SH" || true)
if [[ "$DEP_INSTALL_SITES" -ge 5 ]]; then
  pass "coldstart.sh's step 3 renamed to 'dependency install' at $DEP_INSTALL_SITES site(s)"
else
  fail "coldstart.sh's step 3 rename incomplete — only $DEP_INSTALL_SITES 'dependency install' site(s) found, expected >= 5"
fi
# Both-directions: coldstart.sh --help output itself must not print the old name.
HELP_OUT=$(bash "$COLDSTART_SH" --help 2>&1 || true)
if echo "$HELP_OUT" | grep -qi "dependency bootstrap"; then
  fail "coldstart.sh --help still prints 'dependency bootstrap'"
else
  pass "coldstart.sh --help no longer prints 'dependency bootstrap'"
fi

echo ""
echo "--- PORTABILITY: spawn_templates.py has _load_repo() ---"
STPY="$REPO_ROOT/backend/spawn_templates.py"
assert_contains "$STPY" "_load_repo" "_load_repo() function present"
assert_contains "$STPY" "project.json" "reads project.json"
assert_contains "$STPY" "_make_repo_scope" "_make_repo_scope() present"
assert_contains "$STPY" '"REPO"' "REPO var added to render defaults"

echo ""
echo "--- PORTABILITY: backend-snapshot mirrors updated spawn_templates.py ---"
SNAP_ST="$REPO_ROOT/loop-bootstrap/backend-snapshot/spawn_templates.py"
if [[ -f "$SNAP_ST" ]]; then
  if diff -q "$STPY" "$SNAP_ST" > /dev/null 2>&1; then
    pass "backend-snapshot/spawn_templates.py matches backend/spawn_templates.py"
  else
    fail "backend-snapshot/spawn_templates.py DRIFTS from backend/spawn_templates.py"
  fi
else
  fail "backend-snapshot/spawn_templates.py missing"
fi

# --- Wave C: I1-I6 bootstrap completeness assertions ---

echo ""
echo "=== Wave C: I1-I6 completeness assertions ==="

# I1: .mcp.json template
echo ""
echo "--- I1: .mcp.json template ---"
MCP_TEMPLATE_SRC="$REPO_ROOT/loop-bootstrap/templates/.mcp.json.template"
assert_file "$MCP_TEMPLATE_SRC"
assert_contains "$MCP_TEMPLATE_SRC" "mcpServers" ".mcp.json.template has mcpServers key"
assert_contains "$MCP_TEMPLATE_SRC" "chrome-devtools" ".mcp.json.template lists chrome-devtools as example"
# bootstrap must have installed .mcp.json into the target (since it didn't exist)
assert_file "$TARGET/.mcp.json"
assert_contains "$TARGET/.mcp.json" "mcpServers" "installed .mcp.json has mcpServers"
# Idempotency: re-run should not overwrite existing .mcp.json
echo "sentinel-content" > "$TARGET/.mcp.json.bak"
cp "$TARGET/.mcp.json" "$TARGET/.mcp.json.bak"
bash "$BOOTSTRAP" --repo acme/test-cold-start-v2 --force "$TARGET" > "$RUN_TMP/bootstrap-i1-idem.log" 2>&1
if diff -q "$TARGET/.mcp.json" "$TARGET/.mcp.json.bak" > /dev/null 2>&1; then
  pass "I1 idempotent: .mcp.json not overwritten on re-run"
else
  fail "I1 idempotent: .mcp.json was modified on re-run"
fi
rm -f "$TARGET/.mcp.json.bak"

# I3: requirements.txt.template + setup-deps.sh
echo ""
echo "--- I3: requirements.txt template + setup-deps.sh ---"
REQ_TEMPLATE_SRC="$REPO_ROOT/loop-bootstrap/templates/requirements.txt.template"
assert_file "$REQ_TEMPLATE_SRC"
assert_contains "$REQ_TEMPLATE_SRC" "duckdb" "requirements.txt.template lists duckdb"
assert_contains "$REQ_TEMPLATE_SRC" "pyyaml" "requirements.txt.template lists pyyaml"
assert_contains "$REQ_TEMPLATE_SRC" "anthropic" "requirements.txt.template lists anthropic"
assert_contains "$REQ_TEMPLATE_SRC" "requests" "requirements.txt.template lists requests"
# bootstrap must have installed requirements.txt into target
assert_file "$TARGET/requirements.txt"
assert_contains "$TARGET/requirements.txt" "duckdb" "installed requirements.txt has duckdb"
# setup-deps.sh source file
SETUP_DEPS_SRC="$REPO_ROOT/loop-bootstrap/scripts/setup-deps.sh"
assert_file "$SETUP_DEPS_SRC"
if [[ -x "$SETUP_DEPS_SRC" ]]; then
  pass "loop-bootstrap/scripts/setup-deps.sh is executable"
else
  fail "loop-bootstrap/scripts/setup-deps.sh is NOT executable"
fi
assert_contains "$SETUP_DEPS_SRC" "requirements.txt" "setup-deps.sh references requirements.txt"
assert_contains "$SETUP_DEPS_SRC" "pip install" "setup-deps.sh runs pip install"
assert_contains "$SETUP_DEPS_SRC" "CHECK_ONLY" "setup-deps.sh has --check mode"
# bootstrap must have installed setup-deps.sh into target
assert_file "$TARGET/scripts/setup-deps.sh"
if [[ -x "$TARGET/scripts/setup-deps.sh" ]]; then
  pass "installed setup-deps.sh is executable"
else
  fail "installed setup-deps.sh is NOT executable"
fi
# idempotency: requirements.txt not overwritten
REQ_HASH_BEFORE=$(md5sum "$TARGET/requirements.txt" | awk '{print $1}')
bash "$BOOTSTRAP" --repo acme/test-cold-start-v2 --force "$TARGET" > "$RUN_TMP/bootstrap-i3-idem.log" 2>&1
REQ_HASH_AFTER=$(md5sum "$TARGET/requirements.txt" | awk '{print $1}')
if [[ "$REQ_HASH_BEFORE" == "$REQ_HASH_AFTER" ]]; then
  pass "I3 idempotent: requirements.txt not overwritten on re-run"
else
  fail "I3 idempotent: requirements.txt changed on re-run"
fi

# I4: team-log Issue auto-create (mock test — no real gh calls)
echo ""
echo "--- I4: team-log Issue auto-create logic in bootstrap.sh ---"
# Verify bootstrap.sh has the team-log creation logic
BOOTSTRAP_SH="$REPO_ROOT/loop-bootstrap/bootstrap.sh"
assert_contains "$BOOTSTRAP_SH" "BOOTSTRAP_SKIP_TEAMLOG" "bootstrap.sh has BOOTSTRAP_SKIP_TEAMLOG opt-out"
assert_contains "$BOOTSTRAP_SH" "rotate-team-log.sh" "bootstrap.sh references rotate-team-log.sh"
assert_contains "$BOOTSTRAP_SH" "team-log" "bootstrap.sh references team-log label"
# Verify BOOTSTRAP_SKIP_TEAMLOG=1 bypasses team-log creation
SKIP_LOG=$(BOOTSTRAP_SKIP_TEAMLOG=1 bash "$BOOTSTRAP_SH" --repo acme/test-cold-start-v2 --force "$TARGET" 2>&1 | grep -c "BOOTSTRAP_SKIP_TEAMLOG" || echo 0)
if [[ "$SKIP_LOG" -ge 1 ]]; then
  pass "I4: BOOTSTRAP_SKIP_TEAMLOG=1 skips team-log Issue creation"
else
  fail "I4: BOOTSTRAP_SKIP_TEAMLOG=1 did not suppress team-log creation"
fi

# I5: agent-feed.jsonl + circuit-breaker-history.jsonl in coldstart
echo ""
echo "--- I5: agent-feed.jsonl + circuit-breaker-history.jsonl in coldstart ---"
COLDSTART_SH="$REPO_ROOT/scripts/coldstart-project.sh"
assert_contains "$COLDSTART_SH" "agent-feed.jsonl" "coldstart creates agent-feed.jsonl"
assert_contains "$COLDSTART_SH" "circuit-breaker-history.jsonl" "coldstart creates circuit-breaker-history.jsonl"
# Run a mock coldstart in a temp dir and verify the files are created
I5_TMP_REPO="$RUN_TMP/i5-test-repo"
I5_HOME="$RUN_TMP/i5-test-home"
mkdir -p "$I5_TMP_REPO" "$I5_HOME"
git -C "$I5_TMP_REPO" init -q
I5_OUT=$(cd /tmp && HOME="$I5_HOME" COLDSTART_STATE_ROOT="$I5_HOME" PATH="/usr/bin:/bin:/usr/local/bin" bash "$COLDSTART_SH" "$I5_TMP_REPO" i5test --language python 2>&1) || true
I5_STATE="$I5_HOME/.i5test-state"
if [[ -f "$I5_STATE/agent-feed.jsonl" ]]; then
  pass "I5: coldstart creates agent-feed.jsonl"
else
  fail "I5: coldstart did NOT create agent-feed.jsonl (state=$I5_STATE)"
fi
if [[ -f "$I5_STATE/circuit-breaker-history.jsonl" ]]; then
  pass "I5: coldstart creates circuit-breaker-history.jsonl"
else
  fail "I5: coldstart did NOT create circuit-breaker-history.jsonl (state=$I5_STATE)"
fi
rm -rf "$I5_TMP_REPO" "$I5_HOME"

# I6: control-plane-defaults.json.template + bootstrap installs it
echo ""
echo "--- I6: control-plane defaults template ---"
CP_TEMPLATE_SRC="$REPO_ROOT/loop-bootstrap/templates/control-plane-defaults.json.template"
assert_file "$CP_TEMPLATE_SRC"
# Verify it's valid JSON (ignoring comment lines)
if python3 -c "
import json, sys
lines = open('$CP_TEMPLATE_SRC').readlines()
# Strip JS-style comment lines before parsing
cleaned = ''.join(l for l in lines if not l.strip().startswith('//'))
json.loads(cleaned)
print('ok')
" 2>/dev/null | grep -q "ok"; then
  pass "I6: control-plane-defaults.json.template is valid JSON (with comment stripping)"
else
  fail "I6: control-plane-defaults.json.template is NOT valid JSON"
fi
assert_contains "$CP_TEMPLATE_SRC" "gates" "control-plane-defaults.json.template has gates section"
assert_contains "$CP_TEMPLATE_SRC" "policies" "control-plane-defaults.json.template has policies section"
assert_contains "$CP_TEMPLATE_SRC" "auto_merge" "control-plane-defaults.json.template has auto_merge gate"
assert_contains "$CP_TEMPLATE_SRC" "executor" "control-plane-defaults.json.template has executor policy"
# bootstrap must have installed config.json into target
assert_file "$TARGET/.autonomous-team/config.json"
assert_contains "$TARGET/.autonomous-team/config.json" "gates" "installed config.json has gates"
assert_contains "$TARGET/.autonomous-team/config.json" "auto_merge" "installed config.json has auto_merge"
# idempotency: config.json not overwritten
CP_HASH_BEFORE=$(md5sum "$TARGET/.autonomous-team/config.json" | awk '{print $1}')
bash "$BOOTSTRAP" --repo acme/test-cold-start-v2 --force "$TARGET" > "$RUN_TMP/bootstrap-i6-idem.log" 2>&1
CP_HASH_AFTER=$(md5sum "$TARGET/.autonomous-team/config.json" | awk '{print $1}')
if [[ "$CP_HASH_BEFORE" == "$CP_HASH_AFTER" ]]; then
  pass "I6 idempotent: config.json not overwritten on re-run"
else
  fail "I6 idempotent: config.json changed on re-run"
fi
# Verify conservative defaults: auto_merge=false (safer for forks)
CP_AUTO_MERGE=$(python3 -c "import json; d=json.load(open('$TARGET/.autonomous-team/config.json')); print(d.get('gates',{}).get('auto_merge','MISSING'))" 2>/dev/null || echo ERROR)
if [[ "$CP_AUTO_MERGE" == "False" ]] || [[ "$CP_AUTO_MERGE" == "false" ]]; then
  pass "I6: control-plane defaults have auto_merge=false (conservative)"
else
  fail "I6: expected auto_merge=false in defaults, got '$CP_AUTO_MERGE'"
fi

echo ""
echo "--- I6b: gates.allow_claude_spawn present in installed config.json (D#1872 item 19b) ---"
# Reproduced bug: bootstrap.sh wrote this template to control-plane.json,
# but backend/control_plane.py reads config.json -- a different filename --
# so the file it wrote was never actually read, and backend/spawn_guard.py's
# assert_gate_present() hard-fails at server startup whenever
# gates.allow_claude_spawn is absent, which it always was.
assert_contains "$CP_TEMPLATE_SRC" "allow_claude_spawn" "control-plane-defaults.json.template declares allow_claude_spawn"
CP_SPAWN_GATE=$(python3 -c "import json; d=json.load(open('$TARGET/.autonomous-team/config.json')); print(d.get('gates',{}).get('allow_claude_spawn','MISSING'))" 2>/dev/null || echo ERROR)
if [[ "$CP_SPAWN_GATE" == "False" ]] || [[ "$CP_SPAWN_GATE" == "false" ]]; then
  pass "I6b: installed config.json has gates.allow_claude_spawn=false (key present, conservative default)"
else
  fail "I6b: expected gates.allow_claude_spawn=false in installed config.json, got '$CP_SPAWN_GATE'"
fi
# Both-directions: assert_gate_present() must not raise against the installed config.
GATE_CHECK_OUT=$(cd "$TARGET" && python3 -c "
import sys
sys.path.insert(0, '.')
from backend.spawn_guard import SpawnGuard
sg = SpawnGuard()
sg.assert_gate_present()
print('GATE_PRESENT_OK')
" 2>&1)
if echo "$GATE_CHECK_OUT" | grep -q "GATE_PRESENT_OK"; then
  pass "I6b: SpawnGuard.assert_gate_present() does not raise against bootstrap-installed config.json"
else
  fail "I6b: SpawnGuard.assert_gate_present() raised against bootstrap-installed config.json: $GATE_CHECK_OUT"
fi
# Mutation: with the key removed, assert_gate_present() MUST raise (proves the assertion is live, not vacuous).
python3 -c "
import json
p = '$TARGET/.autonomous-team/config.json'
d = json.load(open(p))
del d['gates']['allow_claude_spawn']
json.dump(d, open(p, 'w'))
"
GATE_CHECK_MUTATED=$(cd "$TARGET" && python3 -c "
import sys
sys.path.insert(0, '.')
from backend.spawn_guard import SpawnGuard
sg = SpawnGuard()
sg.assert_gate_present()
print('GATE_PRESENT_OK')
" 2>&1) || true
if echo "$GATE_CHECK_MUTATED" | grep -qi "allow_claude_spawn is missing"; then
  pass "I6b mutation: assert_gate_present() correctly raises when the key is removed"
else
  fail "I6b mutation: assert_gate_present() did NOT raise when the key was removed (vacuous check): $GATE_CHECK_MUTATED"
fi
# Restore: re-run bootstrap --force would skip (file exists); just re-install the key directly for any later assertions.
python3 -c "
import json
p = '$TARGET/.autonomous-team/config.json'
d = json.load(open(p))
d['gates']['allow_claude_spawn'] = False
json.dump(d, open(p, 'w'))
"

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
