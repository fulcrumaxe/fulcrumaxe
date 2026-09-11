#!/usr/bin/env bash
# tests/test_audit_registered_hooks.sh
#
# Tests scripts/audit-registered-hooks.sh (D#2344): a read-only report of
# every hook registered against this repo's tool calls, from both the
# user-global and the project-local settings files.
#
# Covers the acceptance criteria:
#   1. Every hook entry is reported with event, matcher, command as written,
#      resolved path, and a content hash.
#   2. The liveness assertion is on the RESOLVED path, not on presence of
#      the string "PreToolUse" -- a foreign-only fixture is reported as
#      foreign and names the path; an in-repo-only fixture reports none.
#   3. "registered but not ours" (foreign-only) renders differently from
#      "not registered at all" (no PreToolUse entries) and from
#      "registered and ours" (in-repo).
#   4. Exit status is 0 in every one of those cases.
#   5. Degenerate inputs (missing file, invalid JSON, no "hooks" key) each
#      produce a stated result and exit 0.
#
# Self-contained: every fixture lives under mktemp -d. This test never
# reads or writes the operator's real $HOME or this repo's own committed
# .claude/settings.json -- every invocation below passes explicit
# --repo-root/--project-settings/--global-settings overrides.
#
# Usage: bash tests/test_audit_registered_hooks.sh
# Exit 0 = all tests passed; non-zero = at least one failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/audit-registered-hooks.sh"

PASS=0
FAIL=0

ok() {
  echo "  PASS: $1"
  PASS=$((PASS + 1))
}

fail_test() {
  echo "  FAIL: $1"
  [ -n "${2:-}" ] && echo "        $2"
  FAIL=$((FAIL + 1))
}

if [ ! -f "$SCRIPT" ]; then
  echo "FATAL: $SCRIPT not found" >&2
  exit 1
fi

T=$(mktemp -d)
cleanup() { rm -rf "$T"; }
trap cleanup EXIT

mkdir -p "$T/repoA/hooks" "$T/foreign_repo/hooks"
echo "# in-repo hook" > "$T/repoA/hooks/sandbox.py"
echo "# foreign hook" > "$T/foreign_repo/hooks/sandbox.py"
MISSING_SETTINGS="$T/does_not_exist.json"

run_audit() {
  # $1=project settings path
  bash "$SCRIPT" --repo-root "$T/repoA" --project-settings "$1" --global-settings "$MISSING_SETTINGS"
}

# -----------------------------------------------------------------------
# Fixture (a): no PreToolUse entries at all (only an unrelated event)
# -----------------------------------------------------------------------
FIXTURE_A="$T/fixture_a.json"
cat > "$FIXTURE_A" <<'JSON'
{"hooks": {"SubagentStop": [{"matcher": "", "hooks": [{"type": "command", "command": "bash $CLAUDE_PROJECT_DIR/scripts/x.sh"}]}]}}
JSON

OUT_A=$(run_audit "$FIXTURE_A")
RC_A=$?

if echo "$OUT_A" | grep -qF 'project: no PreToolUse entries registered'; then
  ok "case (a): no-PreToolUse-at-all is reported as such"
else
  fail_test "case (a): no-PreToolUse-at-all is reported as such" "$OUT_A"
fi

if echo "$OUT_A" | grep -q 'IN-REPO event=PreToolUse\|FOREIGN event=PreToolUse'; then
  fail_test "case (a): must not render any PreToolUse entry line" "$OUT_A"
else
  ok "case (a): renders no PreToolUse entry line"
fi

[ "$RC_A" -eq 0 ] && ok "case (a): exits 0" || fail_test "case (a): exits 0" "got $RC_A"

# -----------------------------------------------------------------------
# Fixture (b): PreToolUse entries pointing only OUTSIDE this repo
# (the D#2344 scenario: two hooks enforcing, the maintained one not among them)
# -----------------------------------------------------------------------
FIXTURE_B="$T/fixture_b.json"
python3 - "$FIXTURE_B" "$T/foreign_repo/hooks/sandbox.py" <<'PYEOF'
import json, sys
path, foreign_hook = sys.argv[1], sys.argv[2]
settings = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {foreign_hook}"}]}
]}}
json.dump(settings, open(path, "w"))
PYEOF

OUT_B=$(run_audit "$FIXTURE_B")
RC_B=$?

if echo "$OUT_B" | grep -q "project FOREIGN event=PreToolUse.*$T/foreign_repo/hooks/sandbox.py"; then
  ok "case (b): foreign PreToolUse entry reported as FOREIGN and names the path"
else
  fail_test "case (b): foreign PreToolUse entry reported as FOREIGN and names the path" "$OUT_B"
fi

if echo "$OUT_B" | grep -qF 'project: no PreToolUse entries registered'; then
  fail_test "case (b): must NOT render like case (a) (no entries at all)" "$OUT_B"
else
  ok "case (b): does not render like case (a)"
fi

if echo "$OUT_B" | grep -q 'IN-REPO event=PreToolUse'; then
  fail_test "case (b): must NOT render like case (c) (in-repo)" "$OUT_B"
else
  ok "case (b): does not render like case (c)"
fi

[ "$RC_B" -eq 0 ] && ok "case (b): exits 0" || fail_test "case (b): exits 0" "got $RC_B"

# -----------------------------------------------------------------------
# Fixture (c): PreToolUse entries pointing INSIDE this repo
# -----------------------------------------------------------------------
FIXTURE_C="$T/fixture_c.json"
cat > "$FIXTURE_C" <<'JSON'
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/hooks/sandbox.py"}]}]}}
JSON

OUT_C=$(run_audit "$FIXTURE_C")
RC_C=$?

if echo "$OUT_C" | grep -q "project IN-REPO event=PreToolUse.*resolved=$T/repoA/hooks/sandbox.py"; then
  ok "case (c): in-repo PreToolUse entry reported as IN-REPO with resolved path"
else
  fail_test "case (c): in-repo PreToolUse entry reported as IN-REPO with resolved path" "$OUT_C"
fi

if echo "$OUT_C" | grep -qE 'sha256=[0-9a-f]{64}'; then
  ok "case (c): reports a sha256 content hash of the resolved file"
else
  fail_test "case (c): reports a sha256 content hash of the resolved file" "$OUT_C"
fi

if echo "$OUT_C" | grep -q 'FOREIGN'; then
  fail_test "case (c): must not report anything as FOREIGN" "$OUT_C"
else
  ok "case (c): reports nothing as FOREIGN"
fi

[ "$RC_C" -eq 0 ] && ok "case (c): exits 0" || fail_test "case (c): exits 0" "got $RC_C"

# -----------------------------------------------------------------------
# Degenerate inputs -- each a stated result, exit 0
# -----------------------------------------------------------------------

# Missing file
OUT_MISSING=$(bash "$SCRIPT" --repo-root "$T/repoA" --project-settings "$MISSING_SETTINGS" --global-settings "$MISSING_SETTINGS")
RC_MISSING=$?
if echo "$OUT_MISSING" | grep -qF 'project: not present'; then
  ok "degenerate: missing settings file reports 'not present'"
else
  fail_test "degenerate: missing settings file reports 'not present'" "$OUT_MISSING"
fi
[ "$RC_MISSING" -eq 0 ] && ok "degenerate: missing file exits 0" || fail_test "degenerate: missing file exits 0" "got $RC_MISSING"

# Invalid JSON
FIXTURE_BAD="$T/bad.json"
echo '{not valid json' > "$FIXTURE_BAD"
OUT_BAD=$(run_audit "$FIXTURE_BAD")
RC_BAD=$?
if echo "$OUT_BAD" | grep -qF 'project: invalid JSON'; then
  ok "degenerate: invalid JSON reports 'invalid JSON'"
else
  fail_test "degenerate: invalid JSON reports 'invalid JSON'" "$OUT_BAD"
fi
[ "$RC_BAD" -eq 0 ] && ok "degenerate: invalid JSON exits 0" || fail_test "degenerate: invalid JSON exits 0" "got $RC_BAD"

# No "hooks" key
FIXTURE_NOHOOKS="$T/nohooks.json"
echo '{}' > "$FIXTURE_NOHOOKS"
OUT_NOHOOKS=$(run_audit "$FIXTURE_NOHOOKS")
RC_NOHOOKS=$?
if echo "$OUT_NOHOOKS" | grep -qF 'project: no hooks key'; then
  ok "degenerate: no hooks key reports 'no hooks key'"
else
  fail_test "degenerate: no hooks key reports 'no hooks key'" "$OUT_NOHOOKS"
fi
[ "$RC_NOHOOKS" -eq 0 ] && ok "degenerate: no hooks key exits 0" || fail_test "degenerate: no hooks key exits 0" "got $RC_NOHOOKS"

# -----------------------------------------------------------------------
# Never mutates either settings file
# -----------------------------------------------------------------------
HASH_BEFORE=$(sha256sum "$FIXTURE_C" | awk '{print $1}')
run_audit "$FIXTURE_C" >/dev/null
HASH_AFTER=$(sha256sum "$FIXTURE_C" | awk '{print $1}')
if [ "$HASH_BEFORE" = "$HASH_AFTER" ]; then
  ok "read-only: project settings file is byte-identical after running the audit"
else
  fail_test "read-only: project settings file is byte-identical after running the audit" "before=$HASH_BEFORE after=$HASH_AFTER"
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed (host: $(hostname 2>/dev/null || echo unknown))"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
