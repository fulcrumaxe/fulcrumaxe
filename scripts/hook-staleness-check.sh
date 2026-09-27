#!/usr/bin/env bash
# scripts/hook-staleness-check.sh — advisory: is the hooks/ code this
# checkout actually RUNS current with the code plane's main? (D#2362 PR-b)
#
# "Actually runs" is narrower than all of hooks/: it is the settings-
# registered entry points (what a tracked or operator-local settings file
# names in a "command" string) plus their real import closure — the library
# modules a registered hook loads at run time, verified the same way D#2362
# PR-a's scripts/ci/hook-registry-check.py verifies library status: a fresh
# `import hooks.<x>` in a subprocess, never a filename heuristic. That
# closure matters because a registered hook's own file can be current while
# a module it imports is not — hooks/sandbox.py and hooks/sandbox_rules.py,
# measured 7 commits and a 487-line divergence apart on 2026-09-10, is
# exactly that shape: the registered entry point was current-looking, the
# code it actually runs was not.
#
# This deliberately does read one thing PR-a's design note said PR-b would
# not: the local settings files, to learn what "registered" means on this
# checkout (item 2 of the frozen Spec's PR-b requires exactly that scope).
# It does NOT read hooks/hook-ledger.json — that file is a code-plane
# artifact from a PR ahead of where an engine checkout may have synced (as
# measured true for this very checkout on 2026-09-27), so depending on it
# would make this check fail exactly where it is most needed. The registered
# set and its import closure are both derived independently, straight from
# this checkout's own settings files and its own hooks/*.py files.
#
# Reuses scripts/check-shared-lib-staleness.sh (D#2534) for the actual
# code-plane read — the recursive tree listing plus per-file blob-SHA
# compare, via `gh api`, never git ancestry (the code plane and this
# checkout share no history since the cutover, D#2437) — rather than
# re-implementing that walk. This script only adds what D#2534's checker
# does not do: narrow the file set to what's actually registered, and, for
# anything that differs, name the code-plane commits responsible instead of
# a bare content-differs flag.
#
# Per-file "commits behind" is derived by walking the code plane's own commit
# history for that path (gh api .../commits?path=...) and stopping at the
# first commit whose resulting blob SHA matches the local file's blob SHA
# (git hash-object) — content addressing, so no shared ancestry is required
# either. If the local content is not found within the commit window this
# script queries, that is disclosed as a lower bound rather than presented
# as an exact count.
#
# Advisory only: this script ALWAYS exits 0 and reports on stdout. Per the
# hooks/ scoring rule (CLAUDE.md: over-blocking is worse than under-blocking
# here), a check that can block real work gets disabled, and a disabled
# check reports nothing. It is not a CI gate and is not wired into
# scripts/ci/run-guards.sh — CI has no operator checkout and no
# ~/.claude/settings.json — it is wired into scripts/start-the-day.sh
# instead. It never installs or registers anything; it only reads.
#
# `gh` has no request timeout of its own and hangs indefinitely against a
# black-holed network, so every network-touching call — each `gh api` call
# in the per-stale-file commit walk, and the check-shared-lib-staleness.sh
# invocation this script reuses for the code-plane read — is wrapped in
# `timeout --kill-after=5s`, matching the import-closure subprocess's own
# pattern above. On top of the per-call timeouts, the whole per-stale-file
# commit walk is bounded by an overall wall-clock budget
# (HOOK_STALENESS_BUDGET_SECONDS), so a run with many stale files cannot sum
# per-call timeouts into a long stall. A timeout or an exhausted budget
# degrades that one file (or the whole run, for the shared-lib call) to a
# `behind=unknown` / `UNREACHABLE` line — never a false CURRENT — and this
# script still always exits 0.
#
# Usage:
#   bash scripts/hook-staleness-check.sh
#
# Env overrides (mainly for tests):
#   HOOK_STALENESS_CHECK_ROOT    local base dir for hooks/*.py and
#                                 .claude/settings*.json lookups. Defaults to
#                                 this repo's root. Lets a caller point every
#                                 local-file check at a different checkout
#                                 (including a synthetic one, or a real
#                                 operator checkout other than this repo)
#                                 without touching this tree.
#   HOOK_STALENESS_HOME           base dir for the user-level
#                                 ~/.claude/settings.json lookup. Defaults to
#                                 $HOME.
#   HOOK_STALENESS_SHARED_LIB_SCRIPT  path to check-shared-lib-staleness.sh.
#                                 Defaults to the sibling script in this repo.
#   HOOK_STALENESS_MAX_COMMITS    how many commits of code-plane history per
#                                 stale file to walk before giving up and
#                                 reporting a lower bound. Defaults to 50.
#   HOOK_STALENESS_GH_API_TIMEOUT  soft timeout, in seconds, for a single
#                                 `gh api` call in the per-stale-file commit
#                                 walk, paired with a 5s --kill-after.
#                                 Defaults to 20.
#   HOOK_STALENESS_SHARED_LIB_TIMEOUT  soft timeout, in seconds, for the
#                                 whole check-shared-lib-staleness.sh
#                                 invocation, paired with a 5s --kill-after.
#                                 Defaults to 30.
#   HOOK_STALENESS_BUDGET_SECONDS  overall wall-clock budget, in seconds,
#                                 measured from script start, spent on the
#                                 per-stale-file commit-history walk. Once
#                                 exhausted, any file still to be walked is
#                                 reported behind=unknown instead of spending
#                                 more time on it. Never affects the
#                                 CURRENT/STALE classification itself, which
#                                 is decided earlier by the shared-lib
#                                 content comparison — a slow or unreachable
#                                 network can only cost detail, never produce
#                                 a false CURRENT. Defaults to 60.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/repo-resolve.sh
source "$LIB_ROOT/scripts/lib/repo-resolve.sh"

CHECK_ROOT="${HOOK_STALENESS_CHECK_ROOT:-$LIB_ROOT}"
SETTINGS_HOME="${HOOK_STALENESS_HOME:-$HOME}"
SHARED_LIB_SCRIPT="${HOOK_STALENESS_SHARED_LIB_SCRIPT:-$SCRIPT_DIR/check-shared-lib-staleness.sh}"
MAX_COMMITS="${HOOK_STALENESS_MAX_COMMITS:-50}"
GH_API_TIMEOUT="${HOOK_STALENESS_GH_API_TIMEOUT:-20}"
SHARED_LIB_TIMEOUT="${HOOK_STALENESS_SHARED_LIB_TIMEOUT:-30}"
BUDGET_SECONDS="${HOOK_STALENESS_BUDGET_SECONDS:-60}"
START_EPOCH="$(date +%s)"

# _budget_exceeded — true once HOOK_STALENESS_BUDGET_SECONDS have elapsed
# since script start. Checked before each network-touching step of the
# per-stale-file commit walk so a black-holed network degrades to a bounded
# report instead of many stale files each burning their own full per-call
# timeout in turn.
_budget_exceeded() {
  local elapsed
  elapsed=$(( $(date +%s) - START_EPOCH ))
  [[ $elapsed -ge $BUDGET_SECONDS ]]
}

# _registered_names — hooks/*.py basenames referenced by a "command" string
# in any of the three settings files, one per line, sorted+unique. Mirrors
# hook-registry-check.py's collect_commands()+is_referenced(): parse the
# actual JSON "hooks" tree rather than grep raw text, and match the trailing
# "hooks/<name>" suffix so a $CLAUDE_PROJECT_DIR-prefixed or absolute-path
# command both resolve the same way. A missing settings file (operator-local
# ~/.claude/settings.json, or a gitignored .claude/settings.local.json)
# contributes zero registrations, not an error.
_registered_names() {
  python3 - "$CHECK_ROOT" "$SETTINGS_HOME" <<'PYEOF'
import json, re, sys
from pathlib import Path

repo_root = Path(sys.argv[1])
home = Path(sys.argv[2])
settings_files = [
    repo_root / ".claude" / "settings.json",
    repo_root / ".claude" / "settings.local.json",
    home / ".claude" / "settings.json",
]
pattern = re.compile(r"hooks/([A-Za-z0-9_.-]+\.py)")
names = set()

def walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                names.update(pattern.findall(value))
            else:
                walk(value)
    elif isinstance(node, list):
        for item in node:
            walk(item)

for p in settings_files:
    if not p.is_file():
        continue
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    if isinstance(raw, dict):
        walk(raw.get("hooks", {}))

for n in sorted(names):
    print(n)
PYEOF
}

# _import_closure_names <name> — hooks/*.py basenames loaded (including
# <name> itself and hooks/__init__.py) by a fresh `import hooks.<stem>` run
# in a clean subprocess, one per line. A real import, not a filename
# heuristic — same technique hook-registry-check.py uses to verify library
# status. An import failure (syntax error, missing dependency) is reported
# as "just itself", never a script failure: staleness reporting on the
# entry point alone is strictly better than refusing to report anything.
_import_closure_names() {
  local name="$1" stem mod
  case "$name" in
    *.py) ;;
    *) return 0 ;;
  esac
  stem="${name%.py}"
  if [[ "$stem" == "__init__" ]]; then
    mod="hooks"
  else
    mod="hooks.$stem"
  fi
  timeout --kill-after=5s 30 python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
mod = sys.argv[2]
try:
    __import__(mod)
except Exception:
    print(mod.replace('hooks.', '', 1) + '.py' if mod != 'hooks' else '__init__.py')
    sys.exit(0)
for k in sorted(sys.modules):
    if k == 'hooks':
        print('__init__.py')
    elif k.startswith('hooks.'):
        print(k[len('hooks.'):] + '.py')
" "$CHECK_ROOT" "$mod" 2>/dev/null
}

# _report_stale <code_repo> <rel_path> — for a hook the shared-lib-staleness
# read already found DIFFERS, name how many code-plane commits touched the
# path since the local blob last matched, and their subject lines.
_report_stale() {
  local code_repo="$1" rel_path="$2" local_path local_sha

  if _budget_exceeded; then
    echo "STALE      ${rel_path}  behind=unknown (skipped: wall-clock budget of ${BUDGET_SECONDS}s exceeded)"
    return
  fi

  local_path="$CHECK_ROOT/$rel_path"

  if [[ ! -f "$local_path" ]]; then
    echo "STALE      ${rel_path}  behind=unknown (local file missing)"
    return
  fi
  local_sha="$(git hash-object "$local_path" 2>/dev/null)"
  if [[ -z "$local_sha" ]]; then
    echo "STALE      ${rel_path}  behind=unknown (could not hash local file)"
    return
  fi

  local commits_json
  if ! commits_json="$(timeout --kill-after=5s "$GH_API_TIMEOUT" gh api "repos/${code_repo}/commits?path=${rel_path}&per_page=${MAX_COMMITS}" 2>&1)"; then
    echo "STALE      ${rel_path}  behind=unknown (could not list code-plane commits for this path)"
    return
  fi

  local shas
  shas="$(printf '%s' "$commits_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(data, list):
    sys.exit(1)
for c in data:
    print(c.get("sha", ""))
' 2>/dev/null)"
  if [[ -z "$shas" ]]; then
    echo "STALE      ${rel_path}  behind=unknown (no code-plane commit history found for this path)"
    return
  fi

  local behind=0 matched=0 sha commit_json blob_sha subject budget_hit=0
  local -a subjects=()
  while IFS= read -r sha; do
    [[ -z "$sha" ]] && continue
    if _budget_exceeded; then
      budget_hit=1
      break
    fi
    if ! commit_json="$(timeout --kill-after=5s "$GH_API_TIMEOUT" gh api "repos/${code_repo}/commits/${sha}" 2>&1)"; then
      break
    fi
    blob_sha="$(printf '%s' "$commit_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
target = sys.argv[1]
for f in data.get("files", []):
    if f.get("filename") == target:
        print(f.get("sha", ""))
        break
' "$rel_path" 2>/dev/null)"
    if [[ "$blob_sha" == "$local_sha" ]]; then
      matched=1
      break
    fi
    subject="$(printf '%s' "$commit_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
msg = (data.get("commit", {}) or {}).get("message", "") or ""
print(msg.splitlines()[0] if msg.splitlines() else "")
' 2>/dev/null)"
    behind=$((behind + 1))
    subjects+=("${sha:0:8} ${subject}")
  done <<<"$shas"

  echo "STALE      ${rel_path}  behind=${behind}"
  local s
  for s in "${subjects[@]}"; do
    echo "             - ${s}"
  done
  if [[ $budget_hit -eq 1 ]]; then
    echo "             (stopped: wall-clock budget of ${BUDGET_SECONDS}s exceeded after ${behind} commit(s) checked — behind count is a lower bound)"
  elif [[ $matched -eq 0 && $behind -gt 0 ]]; then
    echo "             (local content not found within the last ${MAX_COMMITS} commits checked for this path — behind count is a lower bound)"
  fi
}

main() {
  local code_repo rc
  code_repo="$(_require_code_repo "hook-staleness-check" 2>&1)"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "HOOK-STALENESS: UNREACHABLE reason=\"code plane unresolved: ${code_repo}\""
    exit 0
  fi

  local direct name
  direct="$(_registered_names)"
  if [[ -z "$direct" ]]; then
    echo "HOOK-STALENESS: no settings-registered hooks/*.py found under ${CHECK_ROOT} or ${SETTINGS_HOME}"
    echo ""
    echo "HOOK-STALENESS SUMMARY host=$(hostname 2>/dev/null || echo unknown) registered=0 current=0 stale=0"
    exit 0
  fi

  declare -A scope=()
  local n
  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    scope["$name"]=1
    while IFS= read -r n; do
      [[ -z "$n" ]] && continue
      scope["$n"]=1
    done < <(_import_closure_names "$name")
  done <<<"$direct"

  local shared_out shared_rc
  shared_out="$(CHECK_SHARED_LIB_STALENESS_ROOT="$CHECK_ROOT" timeout --kill-after=5s "$SHARED_LIB_TIMEOUT" bash "$SHARED_LIB_SCRIPT" hooks 2>&1)"
  shared_rc=$?

  if [[ $shared_rc -ne 0 ]]; then
    echo "HOOK-STALENESS: UNREACHABLE reason=\"shared-lib staleness check did not complete within ${SHARED_LIB_TIMEOUT}s (exit ${shared_rc})\""
    echo ""
    echo "HOOK-STALENESS SUMMARY host=$(hostname 2>/dev/null || echo unknown) registered=${#scope[@]} current=0 stale=0"
    exit 0
  fi

  if printf '%s\n' "$shared_out" | grep -q "^STALENESS: UNREACHABLE"; then
    echo "HOOK-STALENESS: UNREACHABLE reason=\"code-plane read failed: $(printf '%s\n' "$shared_out" | grep "^STALENESS: UNREACHABLE" | head -1)\""
    echo ""
    echo "HOOK-STALENESS SUMMARY host=$(hostname 2>/dev/null || echo unknown) registered=${#scope[@]} current=0 stale=0"
    exit 0
  fi

  local current=0 stale=0 line state rel_path base
  while IFS= read -r line; do
    case "$line" in
      IDENTICAL\ *|DIFFERS\ *|ABSENT\ *|UNREADABLE\ *)
        state="${line%% *}"
        rel_path="$(printf '%s' "$line" | awk '{print $2}')"
        ;;
      *)
        continue
        ;;
    esac
    base="${rel_path##*/}"
    [[ -n "${scope[$base]:-}" ]] || continue

    case "$state" in
      IDENTICAL)
        current=$((current + 1))
        echo "CURRENT    ${rel_path}"
        ;;
      ABSENT)
        stale=$((stale + 1))
        echo "STALE      ${rel_path}  behind=unknown (registered/imported, but absent from this checkout — present on code plane)"
        ;;
      UNREADABLE)
        stale=$((stale + 1))
        echo "STALE      ${rel_path}  behind=unknown (could not hash local file)"
        ;;
      DIFFERS)
        stale=$((stale + 1))
        _report_stale "$code_repo" "$rel_path"
        ;;
    esac
  done <<<"$shared_out"

  echo ""
  echo "HOOK-STALENESS SUMMARY host=$(hostname 2>/dev/null || echo unknown) registered=${#scope[@]} current=${current} stale=${stale}"
  exit 0
}

main
