#!/usr/bin/env bash
# tests/test_audit_registered_hooks.sh
#
# Tests scripts/audit-registered-hooks.sh (D#2344, extended D#2533): a
# read-only report of every hook registered against this repo's tool calls,
# from both the user-global and the project-local settings files.
#
# Covers the acceptance criteria:
#   1. Every hook entry is reported with event, matcher, command as written,
#      the deciding token, its resolved path, and a content hash.
#   2. The liveness assertion is on the RESOLVED path, not on presence of
#      the string "PreToolUse" -- a foreign-only fixture is reported as
#      foreign and names the path; an in-repo-only fixture reports none.
#   3. "registered but not ours" (foreign-only) renders differently from
#      "not registered at all" (no PreToolUse entries) and from
#      "registered and ours" (in-repo).
#   4. Exit status is 0 in every one of those cases.
#   5. Degenerate inputs (missing file, invalid JSON, no "hooks" key) each
#      produce a stated result and exit 0.
#   6. (D#2533) Every absolute-path token in a command is classified, not
#      just the last one, and FOREIGN wins over IN-REPO -- a wrapper
#      command must not report the wrapped file's hash under a FOREIGN or
#      IN-REPO verdict that belongs to a different token.
#   7. (D#2533) A trailing value-less flag fails fast instead of hanging.
#   8. (D#2533) A FOREIGN/UNRESOLVED entry raises a [WARN] marker line,
#      distinct from the entry dump line; a clean run raises [OK].
#   9. (D#2533 fix round 1) Every degenerate branch raises exactly one
#      marker too, not just the entry-loop path: [OK] for a settings file
#      this script fully read and found nothing registered in (missing,
#      present with no "hooks" key), [WARN] for one it could not read at
#      all (invalid JSON). Checked on the project section (where the
#      degenerate fixtures already lived) and, for the no-"hooks"-key case
#      specifically, on the global section too -- that is the exact shape
#      the round-1 review reproduced against a real operator settings file.
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

mkdir -p "$T/repoA/hooks" "$T/foreign_repo/hooks" "$T/foreign_repo/other"
echo "# in-repo hook" > "$T/repoA/hooks/sandbox.py"
echo "# foreign hook" > "$T/foreign_repo/hooks/sandbox.py"
echo "# foreign wrapper" > "$T/foreign_repo/other/wrapper.py"
MISSING_SETTINGS="$T/does_not_exist.json"

run_audit() {
  # $1=project settings path
  bash "$SCRIPT" --repo-root "$T/repoA" --project-settings "$1" --global-settings "$MISSING_SETTINGS"
}

run_audit_global() {
  # $1=global settings path, project settings absent
  bash "$SCRIPT" --repo-root "$T/repoA" --project-settings "$MISSING_SETTINGS" --global-settings "$1"
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
# Fixture (d): D#2533 finding 1's exact shape -- a foreign wrapper listed
# BEFORE the genuine in-repo hook. This is the detector-evasion case: the
# old last-token rule picked the in-repo path here and printed ITS hash
# under an IN-REPO verdict. The fix must report FOREIGN, name the wrapper
# token, and print the WRAPPER's hash -- never the in-repo hook's.
# -----------------------------------------------------------------------
FIXTURE_D="$T/fixture_d.json"
python3 - "$FIXTURE_D" "$T/foreign_repo/other/wrapper.py" <<'PYEOF'
import json, sys
path, wrapper = sys.argv[1], sys.argv[2]
settings = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command",
        "command": f"python3 {wrapper} $CLAUDE_PROJECT_DIR/hooks/sandbox.py"}]}
]}}
json.dump(settings, open(path, "w"))
PYEOF

OUT_D=$(run_audit "$FIXTURE_D")
RC_D=$?
WRAPPER_HASH=$(sha256sum "$T/foreign_repo/other/wrapper.py" | awk '{print $1}')
INREPO_HASH=$(sha256sum "$T/repoA/hooks/sandbox.py" | awk '{print $1}')

if echo "$OUT_D" | grep -q "project FOREIGN event=PreToolUse.*sha256=$WRAPPER_HASH"; then
  ok "case (d): foreign-then-in-repo wrapper is classified FOREIGN with the wrapper's own hash"
else
  fail_test "case (d): foreign-then-in-repo wrapper is classified FOREIGN with the wrapper's own hash" "$OUT_D"
fi

if echo "$OUT_D" | grep -q "sha256=$INREPO_HASH"; then
  fail_test "case (d): must not print the in-repo hook's hash for a FOREIGN verdict (the D#2533 bug)" "$OUT_D"
else
  ok "case (d): does not print the in-repo hook's hash for the FOREIGN verdict"
fi

[ "$RC_D" -eq 0 ] && ok "case (d): exits 0" || fail_test "case (d): exits 0" "got $RC_D"

# -----------------------------------------------------------------------
# Fixture (e): in-repo token FIRST, foreign token second. FOREIGN must
# still win even though the foreign token is not the last one, and the
# printed hash must be the foreign token's.
# -----------------------------------------------------------------------
FIXTURE_E="$T/fixture_e.json"
python3 - "$FIXTURE_E" "$T/foreign_repo/other/wrapper.py" <<'PYEOF'
import json, sys
path, wrapper = sys.argv[1], sys.argv[2]
settings = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command",
        "command": f"python3 $CLAUDE_PROJECT_DIR/hooks/sandbox.py {wrapper}"}]}
]}}
json.dump(settings, open(path, "w"))
PYEOF

OUT_E=$(run_audit "$FIXTURE_E")
RC_E=$?

if echo "$OUT_E" | grep -q "project FOREIGN event=PreToolUse.*sha256=$WRAPPER_HASH"; then
  ok "case (e): in-repo-then-foreign is still classified FOREIGN with the foreign token's hash"
else
  fail_test "case (e): in-repo-then-foreign is still classified FOREIGN with the foreign token's hash" "$OUT_E"
fi

[ "$RC_E" -eq 0 ] && ok "case (e): exits 0" || fail_test "case (e): exits 0" "got $RC_E"

# -----------------------------------------------------------------------
# Fixture (f): no absolute-path token at all -> UNRESOLVED
# -----------------------------------------------------------------------
FIXTURE_F="$T/fixture_f.json"
cat > "$FIXTURE_F" <<'JSON'
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]}}
JSON

OUT_F=$(run_audit "$FIXTURE_F")
RC_F=$?

if echo "$OUT_F" | grep -q "project UNRESOLVED event=PreToolUse"; then
  ok "case (f): a command with no absolute-path token is UNRESOLVED"
else
  fail_test "case (f): a command with no absolute-path token is UNRESOLVED" "$OUT_F"
fi

[ "$RC_F" -eq 0 ] && ok "case (f): exits 0" || fail_test "case (f): exits 0" "got $RC_F"

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

# fix round 1 (D#2533 blocker): a missing file is a verified-clean state --
# this script read the absence and confirmed nothing is registered -- so it
# gets [OK], on its own line, for BOTH the project and global sections.
if echo "$OUT_MISSING" | grep -qE '^  project \[OK\]'; then
  ok "fix round 1: missing project settings file raises an [OK] marker"
else
  fail_test "fix round 1: missing project settings file raises an [OK] marker" "$OUT_MISSING"
fi
if echo "$OUT_MISSING" | grep -qE '^  global \[OK\]'; then
  ok "fix round 1: missing global settings file raises an [OK] marker"
else
  fail_test "fix round 1: missing global settings file raises an [OK] marker" "$OUT_MISSING"
fi

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

# fix round 1: invalid JSON means this script cannot see what the file
# contains, so it cannot claim [OK] -- it gets [WARN] instead, distinct from
# the FOREIGN/UNRESOLVED [WARN] above but the same marker an operator scans
# for.
if echo "$OUT_BAD" | grep -qE '^  project \[WARN\]'; then
  ok "fix round 1: invalid JSON raises a [WARN] marker (unverifiable, not silently OK)"
else
  fail_test "fix round 1: invalid JSON raises a [WARN] marker" "$OUT_BAD"
fi

# No "hooks" key -- this is the exact blocker from fix round 1: the built
# PR's "no hooks key" branch printed no marker at all, reproduced against
# the operator's real ~/.claude/settings.json (no "hooks" key there).
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

if echo "$OUT_NOHOOKS" | grep -qE '^  project \[OK\]'; then
  ok "fix round 1: no-hooks-key raises an [OK] marker (the D#2533 blocker)"
else
  fail_test "fix round 1: no-hooks-key raises an [OK] marker (the D#2533 blocker)" "$OUT_NOHOOKS"
fi

# Same check on the GLOBAL settings file specifically -- this is the exact
# shape the reviewer reproduced against the operator's real
# ~/.claude/settings.json (project settings absent so only global renders).
OUT_NOHOOKS_GLOBAL=$(run_audit_global "$FIXTURE_NOHOOKS")
RC_NOHOOKS_GLOBAL=$?
if echo "$OUT_NOHOOKS_GLOBAL" | grep -qE '^  global \[OK\]'; then
  ok "fix round 1: no-hooks-key on the global settings file raises an [OK] marker"
else
  fail_test "fix round 1: no-hooks-key on the global settings file raises an [OK] marker" "$OUT_NOHOOKS_GLOBAL"
fi
[ "$RC_NOHOOKS_GLOBAL" -eq 0 ] && ok "fix round 1: no-hooks-key on global exits 0" || fail_test "fix round 1: no-hooks-key on global exits 0" "got $RC_NOHOOKS_GLOBAL"

# -----------------------------------------------------------------------
# D#2533 finding 2: a trailing value-less flag must fail fast, not hang.
# Bounded by `timeout` so a regression cannot hang this suite itself.
# -----------------------------------------------------------------------
HANG_OUT=$(timeout --kill-after=5s 10 bash "$SCRIPT" --repo-root 2>&1)
RC_HANG=$?

if [ "$RC_HANG" -ne 124 ]; then
  ok "finding 2: trailing --repo-root with no value does not hang (rc=$RC_HANG, not 124)"
else
  fail_test "finding 2: trailing --repo-root with no value does not hang" "got rc=124 (timeout fired)"
fi

if [ "$RC_HANG" -ne 0 ] && [ "$RC_HANG" -ne 124 ]; then
  ok "finding 2: trailing --repo-root with no value exits non-zero (argument-parse error, not a report outcome)"
else
  fail_test "finding 2: trailing --repo-root with no value exits non-zero" "got rc=$RC_HANG"
fi

if echo "$HANG_OUT" | grep -qF -- '--repo-root'; then
  ok "finding 2: the error message names the offending flag"
else
  fail_test "finding 2: the error message names the offending flag" "$HANG_OUT"
fi

# -----------------------------------------------------------------------
# D#2533 finding 3: [WARN]/[OK] markers, checked on the global settings
# file specifically -- that is the scenario this script exists for
# (D#2344: a foreign hook registered globally, invisible to a project-only
# check).
# -----------------------------------------------------------------------
GLOBAL_FOREIGN="$T/global_foreign.json"
python3 - "$GLOBAL_FOREIGN" "$T/foreign_repo/hooks/sandbox.py" <<'PYEOF'
import json, sys
path, foreign_hook = sys.argv[1], sys.argv[2]
settings = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {foreign_hook}"}]}
]}}
json.dump(settings, open(path, "w"))
PYEOF

OUT_GWARN=$(run_audit_global "$GLOBAL_FOREIGN")
RC_GWARN=$?

if echo "$OUT_GWARN" | grep -qE '^  global \[WARN\]'; then
  ok "finding 3: a FOREIGN entry in the global settings file raises a [WARN] marker, its own line"
else
  fail_test "finding 3: a FOREIGN entry in the global settings file raises a [WARN] marker, its own line" "$OUT_GWARN"
fi

[ "$RC_GWARN" -eq 0 ] && ok "finding 3: exits 0 even when [WARN] fires (report, never a gate)" || fail_test "finding 3: exits 0 even when [WARN] fires" "got $RC_GWARN"

GLOBAL_CLEAN="$T/global_clean.json"
cat > "$GLOBAL_CLEAN" <<'JSON'
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 $CLAUDE_PROJECT_DIR/hooks/sandbox.py"}]}]}}
JSON

OUT_GOK=$(run_audit_global "$GLOBAL_CLEAN")
RC_GOK=$?

if echo "$OUT_GOK" | grep -qE '^  global \[OK\]'; then
  ok "finding 3: a clean global settings file raises an [OK] marker"
else
  fail_test "finding 3: a clean global settings file raises an [OK] marker" "$OUT_GOK"
fi

[ "$RC_GOK" -eq 0 ] && ok "finding 3: exits 0 on a clean global run" || fail_test "finding 3: exits 0 on a clean global run" "got $RC_GOK"

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
