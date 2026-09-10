#!/usr/bin/env bash
# tests/test_hook_registry_check.sh — exercises scripts/ci/hook-registry-check.py
# against fixture repo trees (D#2362 PR-a).
#
# The checker resolves its repo root from __file__, so each case builds a
# throwaway tree (hooks/ + .claude/settings.json, occasionally
# .claude/settings.local.json) and runs the real checker source with
# __file__ pointed into that tree — the same technique
# tests/test_guard_registry_check.sh uses for its checker, and for the same
# reason: it keeps the checker itself out of its own subject set, which is
# what lets the empty case present a genuinely empty hooks/.
#
# Library-verification cases use real, importable fixture .py files with a
# real `from hooks.lib import x` relationship — the check does an actual
# subprocess import to build its library set, so a fixture without a real
# import would not exercise that path.
#
# HOME is redirected per-case so a real ~/.claude/settings.json on the host
# running this suite can never leak a registration into a fixture.
#
# Usage: bash tests/test_hook_registry_check.sh — exits 0 iff all pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECKER="$REPO_ROOT/scripts/ci/hook-registry-check.py"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

if [ ! -f "$CHECKER" ]; then
  echo "FAIL: not found: $CHECKER" >&2
  exit 1
fi

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

SHIM="$TMPROOT/shim.py"
cat > "$SHIM" <<'PY'
"""Run hook-registry-check.py as though it lived under a fixture repo root."""
import sys

src, fake_path = sys.argv[1], sys.argv[2]
sys.argv = ["hook-registry-check.py"] + sys.argv[3:]
with open(src) as fh:
    code = compile(fh.read(), src, "exec")
exec(code, {"__name__": "__main__", "__file__": fake_path})
PY

# make_tree <name> — build $TMPROOT/<name> with hooks/ and .claude/.
make_tree() {
  local root="$TMPROOT/$1"
  mkdir -p "$root/hooks" "$root/.claude"
  printf '' > "$root/hooks/__init__.py"
  echo "$root"
}

# settings <root> <hook-names...> — a tracked .claude/settings.json
# registering each named hook under a Bash PreToolUse matcher.
settings() {
  local root="$1"; shift
  python3 - "$root/.claude/settings.json" "$@" <<'PY'
import json, sys
path, names = sys.argv[1], sys.argv[2:]
hooks_list = [{"type": "command", "command": f"python3 $CLAUDE_PROJECT_DIR/hooks/{n}"} for n in names]
doc = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks_list}]}}
with open(path, "w") as f:
    json.dump(doc, f)
PY
}

# local_settings <root> <hook-names...> — same, but .claude/settings.local.json
local_settings() {
  local root="$1"; shift
  python3 - "$root/.claude/settings.local.json" "$@" <<'PY'
import json, sys
path, names = sys.argv[1], sys.argv[2:]
hooks_list = [{"type": "command", "command": f"python3 $CLAUDE_PROJECT_DIR/hooks/{n}"} for n in names]
doc = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks_list}]}}
with open(path, "w") as f:
    json.dump(doc, f)
PY
}

# ledger <root> <json>
ledger() {
  printf '%s\n' "$2" > "$1/hooks/hook-ledger.json"
}

# run_checker <root> [args...] — echo "<exit>|<stdout+stderr on one line>"
# HOME is pointed at an empty scratch dir so a real ~/.claude/settings.json
# on the host cannot leak a registration into the fixture.
run_checker() {
  local root="$1"; shift
  local fakehome out rc
  fakehome="$TMPROOT/fakehome"
  mkdir -p "$fakehome"
  out="$(HOME="$fakehome" python3 "$SHIM" "$CHECKER" "$root/scripts/ci/hook-registry-check.py" "$@" 2>&1)"
  rc=$?
  printf '%s|%s' "$rc" "$(printf '%s' "$out" | tr '\n' ' ')"
}

expect() {
  local label="$1" want_rc="$2" want_sub="$3" got="$4"
  local rc="${got%%|*}" out="${got#*|}"
  if [ "$rc" != "$want_rc" ]; then
    fail "$label" "expected exit $want_rc, got $rc — output: $out"
    return
  fi
  if [ -n "$want_sub" ] && [[ "$out" != *"$want_sub"* ]]; then
    fail "$label" "exit $rc as expected but output never mentioned '$want_sub' — output: $out"
    return
  fi
  pass "$label"
}

echo "== hook-registry-check =="

# 1. Happy path: a hook settings.json registers directly, plus an exempt
#    file nothing references.
R="$(make_tree happy)"
touch "$R/hooks/alpha.py"
printf 'x = 1\n' > "$R/hooks/local_tool.py"
settings "$R" alpha.py
ledger "$R" '{"exempt": {"local_tool.py": "run by hand on a dev host, never as a hook", "__init__.py": "package init"}, "own_step": {}}'
expect "registered + exempt reconciles clean" 0 "hook-registry-check: OK" "$(run_checker "$R")"

# 2. An unregistered, unledgered file fails and is named — this is D#2362's
#    original filed shape: hooks/repo_scope_warn.py registered nowhere.
R="$(make_tree unregistered)"
touch "$R/hooks/repo_scope_warn.py"
settings "$R"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "an unregistered, unledgered file fails and is named" 1 "repo_scope_warn.py is present in hooks/ but is not referenced" "$(run_checker "$R")"

# 3. Binding mutation check (D#2362 item 6, direction 1): registering that
#    same file in settings.json makes the check pass.
R="$(make_tree mutation_register)"
touch "$R/hooks/repo_scope_warn.py"
settings "$R" repo_scope_warn.py
ledger "$R" '{"exempt": {"__init__.py": "package init"}, "own_step": {}}'
expect "registering the file in settings.json makes it pass" 0 "PASS  repo_scope_warn.py  registered in settings" "$(run_checker "$R")"

# 4. Binding mutation check, direction 2: removing that registration makes
#    the check fail again, naming the same file.
R="$(make_tree mutation_unregister)"
touch "$R/hooks/repo_scope_warn.py"
settings "$R"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "removing the registration fails again, same file named" 1 "repo_scope_warn.py is present in hooks/ but is not referenced" "$(run_checker "$R")"

# 5. An own_step entry no settings file actually references is a claim the
#    check catches.
R="$(make_tree own_step_lies)"
touch "$R/hooks/alpha.py"
settings "$R"
ledger "$R" '{"exempt": {}, "own_step": {"alpha.py": "claims a settings file registers this directly"}}'
expect "an own_step entry nothing references fails" 1 "it runs nowhere and gates nothing" "$(run_checker "$R")"

# 6. An exempt entry that IS referenced is stale in the other direction —
#    this is the shape an operator gets by running an installer without
#    updating the ledger.
R="$(make_tree exempt_lies)"
touch "$R/hooks/alpha.py"
settings "$R" alpha.py
ledger "$R" '{"exempt": {"alpha.py": "claims nothing registers this"}, "own_step": {}}'
expect "an exempt entry a settings file does reference fails" 1 "one of the two is stale" "$(run_checker "$R")"

# 7. own_step registered via settings.local.json (not the tracked
#    settings.json) still counts as referenced.
R="$(make_tree own_step_local)"
touch "$R/hooks/alpha.py"
settings "$R"
local_settings "$R" alpha.py
ledger "$R" '{"exempt": {"__init__.py": "package init"}, "own_step": {"alpha.py": "installed via settings.local.json by an operator"}}'
expect "a settings.local.json registration counts as referenced" 0 "PASS  alpha.py  own_step, registered" "$(run_checker "$R")"

# 8. A blank reason is not a decision.
R="$(make_tree blank_reason)"
touch "$R/hooks/local-tool.py"
settings "$R"
ledger "$R" '{"exempt": {"local-tool.py": "   "}, "own_step": {}}'
expect "blank ledger reason fails" 1 "empty or non-string reason" "$(run_checker "$R")"

# 9. A ledger entry naming a file that no longer exists is stale.
R="$(make_tree stale)"
touch "$R/hooks/alpha.py"
settings "$R"
ledger "$R" '{"exempt": {"deleted-hook.py": "a real-looking reason"}, "own_step": {}}'
expect "stale ledger entry fails and is named" 1 "deleted-hook.py" "$(run_checker "$R")"

# 10. One file cannot be both exempt and own_step.
R="$(make_tree both_sections)"
touch "$R/hooks/alpha.py"
settings "$R" alpha.py
ledger "$R" '{"exempt": {"alpha.py": "r1"}, "own_step": {"alpha.py": "r2"}}'
expect "a file in both ledger sections fails" 1 "it cannot be both" "$(run_checker "$R")"

# 11. Discovering nothing is a failure, not a pass.
R="$(make_tree empty)"
rm -f "$R/hooks/__init__.py"
settings "$R"
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "empty hooks/ fails rather than reporting all-clear" 1 "discovered zero files" "$(run_checker "$R")"

# 12. Missing ledger and malformed-ledger shapes.
R="$(make_tree no_ledger)"
touch "$R/hooks/alpha.py"
settings "$R"
expect "missing ledger fails" 1 "hook-ledger.json is missing" "$(run_checker "$R")"
ledger "$R" '{"exempt": {}, "own_step": {}, "typo_key": 1}'
expect "unknown top-level ledger key fails" 1 "unknown top-level key" "$(run_checker "$R")"
ledger "$R" '{"note": "no exempt object here", "own_step": {}}'
expect "ledger without an exempt object fails" 1 "missing its required 'exempt' object" "$(run_checker "$R")"
ledger "$R" '{"exempt": {}}'
expect "ledger without an own_step object fails" 1 "missing its required 'own_step' object" "$(run_checker "$R")"

# 13. Library verification, direction 1 (D#2362 item 6, "library module"
#     half): a file genuinely imported by another hooks/*.py file — verified
#     by a real import, not by its underscore-prefixed name — must be
#     ledgered exempt or the check fails, naming it.
R="$(make_tree library_missing_ledger)"
printf 'VALUE = 1\n' > "$R/hooks/lib.py"
printf 'from hooks.lib import VALUE\n' > "$R/hooks/user.py"
settings "$R" user.py
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "a real library import with no ledger entry fails, naming it" 1 \
  "lib.py is imported by another hooks/*.py module (verified by an actual import, not a filename heuristic) but has no entry" \
  "$(run_checker "$R")"

# 14. Library verification, direction 2: ledgering it as exempt fixes it,
#     and the pass line records that the import was actually verified.
R="$(make_tree library_ledgered)"
printf 'VALUE = 1\n' > "$R/hooks/lib.py"
printf 'from hooks.lib import VALUE\n' > "$R/hooks/user.py"
settings "$R" user.py
ledger "$R" '{"exempt": {"lib.py": "imported by hooks/user.py", "__init__.py": "package init"}, "own_step": {}}'
expect "ledgering the verified library import passes and says verified" 0 \
  "PASS  lib.py  ledgered (verified: imported by another hooks/*.py module)" \
  "$(run_checker "$R")"

# 15. Library verification, direction 3: a file that is NOT actually
#     imported by anything gets no special library treatment even if its
#     name looks library-shaped — an underscore prefix alone proves nothing.
R="$(make_tree not_actually_a_library)"
printf 'VALUE = 1\n' > "$R/hooks/_looks_like_a_lib.py"
touch "$R/hooks/standalone.py"
settings "$R" standalone.py
ledger "$R" '{"exempt": {}, "own_step": {}}'
expect "an underscore-named file nothing imports fails the ordinary way, not as a library" 1 \
  "_looks_like_a_lib.py is present in hooks/ but is not referenced" \
  "$(run_checker "$R")"

# 16. A missing ~/.claude/settings.json and a missing .claude/settings.local.json
#     both contribute zero registrations without erroring — a contributor
#     without either file gets a clean run.
R="$(make_tree lenient_missing_settings)"
touch "$R/hooks/alpha.py"
settings "$R" alpha.py
rm -f "$R/.claude/settings.local.json"
ledger "$R" '{"exempt": {"__init__.py": "package init"}, "own_step": {}}'
expect "a missing settings.local.json and missing ~/.claude/settings.json are not errors" 0 \
  "hook-registry-check: OK" "$(run_checker "$R")"

# 17. --list prints the subject set including the ledger's neighbours, and a
#     count line that matches.
R="$(make_tree list_mode)"
touch "$R/hooks/alpha.py" "$R/hooks/beta.py"
settings "$R"
expect "--list includes discovered files" 0 "alpha.py" "$(run_checker "$R" --list)"
expect "--list counts them, including __init__.py" 0 "count: 3" "$(run_checker "$R" --list)"

# 18. The real tree, run the way CI would run it — proves the shipped
#     hooks/hook-ledger.json reconciles against the real hooks/ and the real
#     tracked .claude/settings.json.
expect "the real repo reconciles clean" 0 "hook-registry-check: OK" "$(run_checker "$REPO_ROOT")"

echo ""
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
