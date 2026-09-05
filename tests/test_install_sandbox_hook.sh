#!/usr/bin/env bash
# tests/test_install_sandbox_hook.sh
#
# Regression test for D#1814: install-sandbox-hook.sh must register its
# hooks in the PROJECT's own .claude/settings.json using the literal
# $CLAUDE_PROJECT_DIR token, never in the user-global ~/.claude/settings.json
# with an absolute path.
#
# The bug this guards against: two projects on one machine both running the
# (old) installer clobbered each other's registration, because the write
# target was $HOME/.claude/settings.json and the written command hardcoded
# whichever repo the installer happened to run from — last writer wins.
# This test builds two throwaway projects, installs into both, and proves
# neither's registration is touched by the other's install.
#
# Self-contained: every installer invocation lives under mktemp -d and runs
# with HOME pointed at a scratch directory under $T -- this test never
# writes the operator's real $HOME or this repo's own tracked
# .claude/settings.json. It does READ this repo's own committed
# .claude/settings.json (via `git show`, a read-only verb) for the
# committed-registration check at the end -- that check is the point: it
# proves the fix is a checked-in fact, not just something the installer
# produces when run.
#
# Usage: bash tests/test_install_sandbox_hook.sh
# Exit 0 = all tests passed; non-zero = at least one failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER_SRC="$REPO_ROOT/scripts/install-sandbox-hook.sh"

PASS=0
FAIL=0

ok() {
  echo "ok: $1"
  PASS=$((PASS + 1))
}

fail_test() {
  echo "FAIL: $1"
  echo "      $2"
  FAIL=$((FAIL + 1))
}

if [[ ! -f "$INSTALLER_SRC" ]]; then
  echo "FAIL: installer not found at $INSTALLER_SRC" >&2
  exit 1
fi

T=$(mktemp -d)
cleanup() { rm -rf "$T"; }
trap cleanup EXIT

# Fake, empty $HOME for every installer invocation below — the real
# operator's $HOME is never read or written by this test.
FAKE_HOME="$T/fakehome"
mkdir -p "$FAKE_HOME"

for P in projA projB; do
  mkdir -p "$T/$P/scripts" "$T/$P/hooks"
  cp "$INSTALLER_SRC" "$T/$P/scripts/install-sandbox-hook.sh"
  : > "$T/$P/hooks/sandbox.py"                    # installer only checks existence
  : > "$T/$P/hooks/subagent_stop_dial_audit.py"
done

# ---------------------------------------------------------------------------
# Helpers (all read-only JSON inspection via Python heredocs — no bash
# interpolation of $CLAUDE_PROJECT_DIR, so the literal token in the JSON
# is never accidentally expanded before comparison).
# ---------------------------------------------------------------------------

# Prints: <PreToolUse count>\n<sorted matcher list>\n<SubagentStop count>
inspect_settings() {
  python3 - "$1" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
pre = s.get("hooks", {}).get("PreToolUse", [])
stop = s.get("hooks", {}).get("SubagentStop", [])
print(len(pre))
print(" ".join(sorted(e.get("matcher", "") for e in pre)))
print(len(stop))
PYEOF
}

# Prints the count of commands (across PreToolUse + SubagentStop) that do
# NOT contain the literal $CLAUDE_PROJECT_DIR token.
count_missing_project_dir_token() {
  python3 - "$1" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
cmds = []
for e in s.get("hooks", {}).get("PreToolUse", []):
    for h in e.get("hooks", []):
        cmds.append(h.get("command", ""))
for e in s.get("hooks", {}).get("SubagentStop", []):
    for h in e.get("hooks", []):
        cmds.append(h.get("command", ""))
print(sum(1 for c in cmds if "$CLAUDE_PROJECT_DIR" not in c))
PYEOF
}

# Prints: <PreToolUse entry count>\n<count of distinct commands>
inspect_pretooluse_commands() {
  python3 - "$1" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
pre = s.get("hooks", {}).get("PreToolUse", [])
cmds = set()
for e in pre:
    for h in e.get("hooks", []):
        cmds.add(h.get("command", ""))
print(len(pre))
print(len(cmds))
PYEOF
}

# Seeds a settings.json with the legacy absolute-path form for all four
# matchers plus one SubagentStop entry.
seed_legacy_settings() {
  local settings_path="$1" hook_abs="$2" stop_abs="$3"
  python3 - "$settings_path" "$hook_abs" "$stop_abs" <<'PYEOF'
import json, sys
settings_path, hook_abs, stop_abs = sys.argv[1:4]
hook_cmd = "python3 " + hook_abs
stop_cmd = "python3 " + stop_abs
settings = {
    "hooks": {
        "PreToolUse": [
            {"matcher": m, "hooks": [{"type": "command", "command": hook_cmd}]}
            for m in ["Agent", "Bash", "Edit", "Write"]
        ],
        "SubagentStop": [
            {"matcher": "", "hooks": [{"type": "command", "command": stop_cmd}]}
        ],
    }
}
with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
PYEOF
}

# ---------------------------------------------------------------------------
# Install into projA, then projB.
# ---------------------------------------------------------------------------

PROJA_OUT=$(HOME="$FAKE_HOME" bash "$T/projA/scripts/install-sandbox-hook.sh" 2>"$T/stderr_a.log")
PROJA_SHA_BEFORE_B=$(sha256sum "$T/projA/.claude/settings.json" | awk '{print $1}')

PROJB_OUT=$(HOME="$FAKE_HOME" bash "$T/projB/scripts/install-sandbox-hook.sh" 2>"$T/stderr_b.log")

# 1. Both settings files exist.
if [[ -f "$T/projA/.claude/settings.json" && -f "$T/projB/.claude/settings.json" ]]; then
  ok "both projA and projB .claude/settings.json exist"
else
  fail_test "settings files exist" "projA=$T/projA/.claude/settings.json projB=$T/projB/.claude/settings.json"
fi

# 2. Each has exactly four PreToolUse entries with matchers Agent Bash Edit
#    Write, and one SubagentStop entry.
for proj in projA projB; do
  RESULT=$(inspect_settings "$T/$proj/.claude/settings.json")
  COUNT=$(echo "$RESULT" | sed -n '1p')
  MATCHERS=$(echo "$RESULT" | sed -n '2p')
  STOP_COUNT=$(echo "$RESULT" | sed -n '3p')

  if [[ "$COUNT" == "4" && "$MATCHERS" == "Agent Bash Edit Write" ]]; then
    ok "$proj: exactly four PreToolUse entries, matchers Agent Bash Edit Write"
  else
    fail_test "$proj: PreToolUse matcher set" "count=$COUNT matchers='$MATCHERS'"
  fi

  if [[ "$STOP_COUNT" == "1" ]]; then
    ok "$proj: exactly one SubagentStop entry"
  else
    fail_test "$proj: SubagentStop entry count" "count=$STOP_COUNT"
  fi
done

# 3. Neither file contains the string $T/proj — i.e. no absolute path
#    leaked into the JSON.
if ! grep -qF "$T/proj" "$T/projA/.claude/settings.json" && ! grep -qF "$T/proj" "$T/projB/.claude/settings.json"; then
  ok "no absolute throwaway-project path leaked into either settings.json"
else
  fail_test "absolute path leak" "grep -F '$T/proj' matched one of the settings files"
fi

# 4. Every written command contains $CLAUDE_PROJECT_DIR (as a literal).
for proj in projA projB; do
  MISSING=$(count_missing_project_dir_token "$T/$proj/.claude/settings.json")
  if [[ "$MISSING" == "0" ]]; then
    ok "$proj: every written command contains the literal \$CLAUDE_PROJECT_DIR token"
  else
    fail_test "$proj: literal \$CLAUDE_PROJECT_DIR check" "$MISSING command(s) missing it"
  fi
done

# 5. sha256sum of projA/.claude/settings.json is unchanged across projB's
#    install.
PROJA_SHA_AFTER_B=$(sha256sum "$T/projA/.claude/settings.json" | awk '{print $1}')
if [[ "$PROJA_SHA_BEFORE_B" == "$PROJA_SHA_AFTER_B" ]]; then
  ok "projA settings.json byte-identical before and after projB's install (two-project isolation)"
else
  fail_test "two-project isolation" "projA sha changed: before=$PROJA_SHA_BEFORE_B after=$PROJA_SHA_AFTER_B"
fi

# 6. Re-running projA's installer prints "already installed" and leaves the
#    file byte-identical.
PROJA_RERUN_OUT=$(HOME="$FAKE_HOME" bash "$T/projA/scripts/install-sandbox-hook.sh" 2>"$T/stderr_a_rerun.log")
PROJA_SHA_AFTER_RERUN=$(sha256sum "$T/projA/.claude/settings.json" | awk '{print $1}')

if [[ "$PROJA_RERUN_OUT" == "already installed" ]]; then
  ok "re-running projA's installer prints 'already installed'"
else
  fail_test "idempotent re-run output" "got: $PROJA_RERUN_OUT"
fi

if [[ "$PROJA_SHA_AFTER_B" == "$PROJA_SHA_AFTER_RERUN" ]]; then
  ok "re-running projA's installer leaves the file byte-identical"
else
  fail_test "idempotent re-run byte-identical" "sha changed on re-run"
fi

# 7. A run where a project's settings.json is pre-seeded with the legacy
#    absolute-path command upgrades it in place to the $CLAUDE_PROJECT_DIR
#    form and still ends with exactly four PreToolUse entries — not eight.
LEGACY_PROJ="$T/projC"
mkdir -p "$LEGACY_PROJ/scripts" "$LEGACY_PROJ/hooks" "$LEGACY_PROJ/.claude"
cp "$INSTALLER_SRC" "$LEGACY_PROJ/scripts/install-sandbox-hook.sh"
: > "$LEGACY_PROJ/hooks/sandbox.py"
: > "$LEGACY_PROJ/hooks/subagent_stop_dial_audit.py"

seed_legacy_settings \
  "$LEGACY_PROJ/.claude/settings.json" \
  "$LEGACY_PROJ/hooks/sandbox.py" \
  "$LEGACY_PROJ/hooks/subagent_stop_dial_audit.py"

HOME="$FAKE_HOME" bash "$LEGACY_PROJ/scripts/install-sandbox-hook.sh" >"$T/legacy_out.log" 2>"$T/legacy_stderr.log"

LEGACY_RESULT=$(inspect_pretooluse_commands "$LEGACY_PROJ/.claude/settings.json")
LEGACY_COUNT=$(echo "$LEGACY_RESULT" | sed -n '1p')
LEGACY_UNIQ_CMDS=$(echo "$LEGACY_RESULT" | sed -n '2p')

if [[ "$LEGACY_COUNT" == "4" ]]; then
  ok "legacy pre-seeded settings.json ends with exactly four PreToolUse entries (not eight)"
else
  fail_test "legacy upgrade entry count" "expected 4, got $LEGACY_COUNT"
fi

if [[ "$LEGACY_UNIQ_CMDS" == "1" ]]; then
  ok "legacy pre-seeded entries collapse to a single upgraded command (no duplicates)"
else
  fail_test "legacy upgrade uniqueness" "expected 1 unique command, got $LEGACY_UNIQ_CMDS"
fi

if grep -qF '$CLAUDE_PROJECT_DIR' "$LEGACY_PROJ/.claude/settings.json"; then
  ok "legacy absolute-path command upgraded in place to \$CLAUDE_PROJECT_DIR form"
else
  fail_test "legacy upgrade to literal form" "\$CLAUDE_PROJECT_DIR not found in upgraded file"
fi

if ! grep -qF "$LEGACY_PROJ/hooks" "$LEGACY_PROJ/.claude/settings.json"; then
  ok "legacy absolute path no longer present after upgrade"
else
  fail_test "legacy absolute path removed" "old absolute path still present"
fi

# 8. Over-broad legacy matching: an entry for a matcher this installer does
#    NOT manage (e.g. "Read"), and an entry whose command happens to end in
#    /hooks/sandbox.py but resolves OUTSIDE this project's root, must never
#    be rewritten. Only Bash/Edit/Write/Agent entries pointing at this
#    project's own hooks/sandbox.py are ours to touch.
FOREIGN_PROJ="$T/projD"
mkdir -p "$FOREIGN_PROJ/scripts" "$FOREIGN_PROJ/hooks" "$FOREIGN_PROJ/.claude"
mkdir -p "$T/unrelated-tool/hooks"
cp "$INSTALLER_SRC" "$FOREIGN_PROJ/scripts/install-sandbox-hook.sh"
: > "$FOREIGN_PROJ/hooks/sandbox.py"
: > "$FOREIGN_PROJ/hooks/subagent_stop_dial_audit.py"
: > "$T/unrelated-tool/hooks/sandbox.py"

FOREIGN_CMD="python3 $T/unrelated-tool/hooks/sandbox.py"
python3 - "$FOREIGN_PROJ/.claude/settings.json" "$FOREIGN_CMD" <<'PYEOF'
import json, sys
settings_path, foreign_cmd = sys.argv[1], sys.argv[2]
settings = {
    "hooks": {
        "PreToolUse": [
            # A matcher we don't manage -- must survive untouched.
            {"matcher": "Read", "hooks": [{"type": "command", "command": foreign_cmd}]},
            # A matcher we DO manage, but pointed at an unrelated tool's
            # hooks/sandbox.py outside this project's root -- must also
            # survive untouched (not silently repointed at us).
            {"matcher": "Bash", "hooks": [{"type": "command", "command": foreign_cmd}]},
        ]
    }
}
with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
PYEOF

HOME="$FAKE_HOME" bash "$FOREIGN_PROJ/scripts/install-sandbox-hook.sh" >"$T/foreign_out.log" 2>"$T/foreign_stderr.log"

FOREIGN_READ_CMD=$(python3 - "$FOREIGN_PROJ/.claude/settings.json" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
for e in s["hooks"]["PreToolUse"]:
    if e.get("matcher") == "Read":
        print(e["hooks"][0]["command"])
        break
PYEOF
)
if [[ "$FOREIGN_READ_CMD" == "$FOREIGN_CMD" ]]; then
  ok "an entry on an unmanaged matcher (Read) is left untouched"
else
  fail_test "unmanaged matcher untouched" "Read entry command changed to: $FOREIGN_READ_CMD"
fi

FOREIGN_BASH_CMD=$(python3 - "$FOREIGN_PROJ/.claude/settings.json" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
for e in s["hooks"]["PreToolUse"]:
    if e.get("matcher") == "Bash":
        print(e["hooks"][0]["command"])
        break
PYEOF
)
if [[ "$FOREIGN_BASH_CMD" == "$FOREIGN_CMD" ]]; then
  ok "a managed matcher (Bash) pointed at an unrelated tool's hooks/sandbox.py outside this project's root is left untouched"
else
  fail_test "out-of-root command untouched" "Bash entry command changed to: $FOREIGN_BASH_CMD"
fi

# 9. Committed-registration regression test (the security-review blocking
#    finding): this repo's own COMMITTED .claude/settings.json must already
#    have the four PreToolUse matchers -- a fresh clone or `git worktree
#    add` from this branch is sandboxed WITHOUT anyone running the
#    installer. Checks the git-tracked blob via `git show` (a read-only
#    verb) rather than the working tree, because the bug this guards
#    against is exactly a gap between the two: an uncommitted local
#    modification that `git checkout --`, `git restore`, or a fresh
#    worktree checkout would silently drop.
COMMITTED_SETTINGS=$(git show HEAD:.claude/settings.json 2>/dev/null)
if [[ -z "$COMMITTED_SETTINGS" ]]; then
  fail_test "committed .claude/settings.json readable at HEAD" "git show HEAD:.claude/settings.json returned nothing"
else
  COMMITTED_RESULT=$(echo "$COMMITTED_SETTINGS" | python3 -c "
import json, sys
s = json.load(sys.stdin)
pre = s.get('hooks', {}).get('PreToolUse', [])
matchers = sorted(e.get('matcher', '') for e in pre if isinstance(e, dict))
print(len(pre))
print(' '.join(matchers))
")
  COMMITTED_COUNT=$(echo "$COMMITTED_RESULT" | sed -n '1p')
  COMMITTED_MATCHERS=$(echo "$COMMITTED_RESULT" | sed -n '2p')

  if [[ "$COMMITTED_COUNT" == "4" && "$COMMITTED_MATCHERS" == "Agent Bash Edit Write" ]]; then
    ok "committed .claude/settings.json at HEAD already has all four PreToolUse matchers -- a fresh clone/worktree is sandboxed without running the installer"
  else
    fail_test "committed registration present at HEAD" "count=$COMMITTED_COUNT matchers='$COMMITTED_MATCHERS' -- this change must be committed, not left as a working-tree-only edit"
  fi

  if echo "$COMMITTED_SETTINGS" | grep -qF '$CLAUDE_PROJECT_DIR'; then
    ok "committed PreToolUse commands use the literal \$CLAUDE_PROJECT_DIR token"
  else
    fail_test "committed command form" "\$CLAUDE_PROJECT_DIR not found in committed .claude/settings.json"
  fi
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
