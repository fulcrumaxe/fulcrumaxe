#!/usr/bin/env bash
# scripts/lib/worktree-registry.sh — worktree lifecycle registry helper.
#
# Tracks every Agent(isolation:"worktree") from spawn through cleanup.
# Registry file: .autonomous-team/worktrees.json (JSON array of entries).
# All mutating operations are flock-guarded via worktrees.json.lock.
#
# Usage:
#   source scripts/lib/worktree-registry.sh
#   worktree_registry <command> [args...]
#
# Or invoke directly:
#   bash scripts/lib/worktree-registry.sh register --id <id> --role <r> --path <p> --pid <pid> [--discussion N] [--branch B] [--base B]
#   bash scripts/lib/worktree-registry.sh heartbeat <worktree_id>
#   bash scripts/lib/worktree-registry.sh mark-status <worktree_id> <status>
#   bash scripts/lib/worktree-registry.sh set-pr <worktree_id> <pr_number>
#   bash scripts/lib/worktree-registry.sh reconcile-path --id <id> --actual-path <path>
#   bash scripts/lib/worktree-registry.sh list [--status S] [--json]
#   bash scripts/lib/worktree-registry.sh reap [--ttl-min N] [--dry-run] [--clean-generated-wiki] [--enable-git-tracked-removal]
#   bash scripts/lib/worktree-registry.sh count-active
#   bash scripts/lib/worktree-registry.sh count-disk
#
# --enable-git-tracked-removal (D#2001 PR2): opt-in for Step 6 to actually
# remove a git-tracked worktree once it clears every safety condition
# (old enough, clean, pushed, not self, no open PR on its branch). Without
# --dry-run this is OFF by default -- reap-worktrees.sh is invoked live
# after every agent completion, and this default must never flip. Pass it
# only for a deliberate, human-run pass.
#
# D#2149: a dry-run previews the invocation you actually typed, not some
# other invocation. Without the opt-in, --dry-run classifies a git-tracked
# candidate the same way a real run does (skipped-git-tracked, with an
# informational candidate-git-tracked line for visibility); with the
# opt-in, --dry-run's "would-remove" preview is capped by the same
# WORKTREE_REAP_MAX_PER_PASS a real run enforces. See
# scripts/reap-worktrees.sh's header for the full classification table.
# Real removals are capped per pass (default 25, WORKTREE_REAP_MAX_PER_PASS).
#
# A dry-run is not sufficient Gate 2 evidence for a change to this file's
# rm -rf path (the :1273-class path guards, reachable only in the real,
# dry_run==false arm) -- see D#2149 acceptance item 3 for the real-run
# differential that substitutes for it.

set -uo pipefail

# ── Resolve paths ────────────────────────────────────────────────────────────

_wtr_resolve_root() {
  local dir
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/.autonomous-team" ]]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  # Fallback: use current working directory
  echo "$(pwd)"
}

_WTR_REPO_ROOT="${_WTR_REPO_ROOT:-$(_wtr_resolve_root)}"
_WTR_REGISTRY="${_WTR_REPO_ROOT}/.autonomous-team/worktrees.json"
_WTR_LOCK="${_WTR_REPO_ROOT}/.autonomous-team/worktrees.json.lock"
_WTR_ARCHIVE_DIR="${_WTR_REPO_ROOT}/archive/orphan-diffs"
_WTR_WORKTREES_DIR="${_WTR_REPO_ROOT}/.claude/worktrees"
# Same audit.jsonl convention as scripts/merge-and-hook.sh and
# scripts/sweep-stale-worktrees.sh -- one row per real removal (D#2001 PR2 AC-15).
_WTR_AUDIT_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.fulcrumaxe-state}"
_WTR_AUDIT_FILE="${_WTR_AUDIT_DIR}/audit.jsonl"

# repo-resolve.sh gives us _resolve_repo() for the open-PR guard below
# (D#2001 PR2) — same helper scripts/lib/worktree-claims.sh already uses for
# its own gh calls. Best-effort: a missing helper only degrades the open-PR
# guard to fail-closed (see _wtr_load_open_pr_branches), it never breaks
# sourcing of this file.
# shellcheck source=scripts/lib/repo-resolve.sh
source "${_WTR_REPO_ROOT}/scripts/lib/repo-resolve.sh" 2>/dev/null || true

# Hard cap: configurable via WORKTREE_CAP env var
WORKTREE_CAP="${WORKTREE_CAP:-8}"
# TTL default: configurable via WORKTREE_TTL_MIN env var
WORKTREE_TTL_MIN="${WORKTREE_TTL_MIN:-60}"

# ── Low-level JSON helpers ────────────────────────────────────────────────────

_wtr_read_registry() {
  if [[ -f "$_WTR_REGISTRY" ]]; then
    cat "$_WTR_REGISTRY"
  else
    echo "[]"
  fi
}

_wtr_write_registry() {
  local content="$1"
  local tmp="${_WTR_REGISTRY}.tmp"
  printf '%s\n' "$content" > "$tmp"
  mv "$tmp" "$_WTR_REGISTRY"
}

_wtr_now_iso() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

_wtr_now_epoch() {
  date +%s
}

_wtr_date_ymd() {
  date +%Y-%m-%d
}

# ── Locking ───────────────────────────────────────────────────────────────────

# Acquire exclusive flock on fd 9 (opens the lockfile itself).
_wtr_lock() {
  touch "$_WTR_LOCK"
  exec 9>"$_WTR_LOCK"
  flock -x 9
}

_wtr_unlock() {
  flock -u 9 2>/dev/null || true
  exec 9>&- 2>/dev/null || true
}

# ── Self-exclusion guard (D#1864) ────────────────────────────────────────────
#
# A reap pass must never remove the worktree the calling process is standing
# in — that is exactly what deleted reviewer/executor worktrees mid-run.
# _WTR_SELF_ROOT is resolved once per _cmd_reap invocation (not per entry).
# Fail-closed: if the toplevel cannot be resolved, _WTR_SELF_ROOT becomes the
# sentinel __UNRESOLVED__ and _wtr_is_self refuses every target.

_WTR_SELF_ROOT="${_WTR_SELF_ROOT:-}"

# _WTR_REPO_ROOT is set exactly once -- either by the caller (env var, before
# sourcing this file) or by _wtr_resolve_root() at source time (line ~49
# above) -- and nothing in this file ever reassigns it afterward. That makes
# its resolved (realpath'd) form pass-invariant: every _cmd_reap invocation
# runs in its own process (reap-worktrees.sh sources this file fresh and
# calls `worktree_registry reap` exactly once), so "cached for the life of
# the process" and "cached for the life of one pass" are the same guarantee.
# D#2120: 209 of 418 python3 spawns in one reap pass were this exact value
# (_WTR_REPO_ROOT) being realpath'd again for every registry entry inside
# _wtr_is_self. Resolving it once into this cache and reusing the cache is
# the fix -- it is NOT the same kind of caching as _WTR_SELF_ROOT (which is
# deliberately re-resolved every _cmd_reap call because $PWD's toplevel can
# differ call to call); _WTR_REPO_ROOT itself never changes, so there is no
# staleness window to reason about here.
_WTR_REPO_ROOT_RESOLVED_CACHE=""
_WTR_REPO_ROOT_RESOLVED_CACHE_SET=0

# _wtr_resolved_repo_root
# Populates (once) and reuses _WTR_REPO_ROOT_RESOLVED_CACHE with the realpath
# of $_WTR_REPO_ROOT. Shared by _wtr_is_self and by Step 6's own
# resolved_repo_root setup (same value, same spawn -- no reason to pay for it
# twice). Sets the cache to "" (falls through to string-compare against the
# unresolved value) when _WTR_REPO_ROOT is unset, matching prior behaviour.
_wtr_resolved_repo_root() {
  if [[ "$_WTR_REPO_ROOT_RESOLVED_CACHE_SET" -ne 1 ]]; then
    _WTR_REPO_ROOT_RESOLVED_CACHE_SET=1
    if [[ -n "${_WTR_REPO_ROOT:-}" ]]; then
      _WTR_REPO_ROOT_RESOLVED_CACHE="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$_WTR_REPO_ROOT" 2>/dev/null || echo "$_WTR_REPO_ROOT")"
    fi
  fi
}

_wtr_resolve_self_root() {
  local toplevel
  toplevel="$(git rev-parse --show-toplevel 2>/dev/null)"
  if [[ -z "$toplevel" ]]; then
    _WTR_SELF_ROOT="__UNRESOLVED__"
    echo "self-exclusion: cannot resolve current toplevel — refusing all removals this pass" >&2
    return
  fi
  local resolved
  resolved="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$toplevel" 2>/dev/null)"
  if [[ -z "$resolved" ]]; then
    _WTR_SELF_ROOT="__UNRESOLVED__"
    echo "self-exclusion: cannot resolve current toplevel — refusing all removals this pass" >&2
    return
  fi
  _WTR_SELF_ROOT="$resolved"
}

# _wtr_is_self <target_path> [already_resolved]
# Returns 0 (true — refuse removal) when the resolved target equals
# _WTR_SELF_ROOT, is an ancestor of _WTR_SELF_ROOT, or equals _WTR_REPO_ROOT.
# Fails closed: when _WTR_SELF_ROOT is unresolved, always returns 0.
#
# <already_resolved>: pass "1" when the caller has already run the target
# through realpath (Step 5, Step 6, and sweep-stale-worktrees.sh's caller all
# do this today, then threw the result away and made this function redo the
# exact same resolve -- D#2120). Omit it (or pass anything else) and this
# function resolves the target itself, exactly as before -- an unresolved
# caller's behaviour is unchanged.
_wtr_is_self() {
  local target="${1:-}"
  local already_resolved="${2:-0}"
  # Fail closed on an empty target too — an unreachable-today branch is still
  # a reachable one waiting for a fourth call site, and this guard's whole
  # purpose is fail-closed.
  [[ -z "$target" ]] && return 0

  if [[ "${_WTR_SELF_ROOT:-}" == "__UNRESOLVED__" ]]; then
    return 0
  fi
  # An unset self root (e.g. a call site outside _cmd_reap that never ran
  # _wtr_resolve_self_root) is exactly as unresolved as the sentinel — fail
  # closed here too, not open.
  [[ -z "${_WTR_SELF_ROOT:-}" ]] && return 0

  local resolved_target
  if [[ "$already_resolved" == "1" ]]; then
    resolved_target="$target"
  else
    resolved_target="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$target" 2>/dev/null || echo "$target")"
  fi

  if [[ "$resolved_target" == "$_WTR_SELF_ROOT" ]]; then
    return 0
  fi
  # Ancestor check: target is an ancestor of the self root.
  if [[ "$_WTR_SELF_ROOT" == "$resolved_target"/* ]]; then
    return 0
  fi
  if [[ -n "${_WTR_REPO_ROOT:-}" ]]; then
    _wtr_resolved_repo_root
    if [[ "$resolved_target" == "$_WTR_REPO_ROOT_RESOLVED_CACHE" ]]; then
      return 0
    fi
  fi
  return 1
}

# ── Open-PR branch/HEAD guard (D#2001 PR2, D#2129) ───────────────────────────
#
# A git-tracked worktree is never removed while its branch backs an open PR,
# as either head or base ref, OR while its HEAD commit is an open PR's head
# commit (D#2129 -- covers a *detached* worktree, which has no branch to
# match: `pr_tree_provision` and every harness `agent-*` tree are detached).
# One batched `gh pr list --state open` call per _cmd_reap pass — never
# per-worktree, mirroring scripts/lib/worktree-claims.sh's
# wtc_load_merged_heads (53 worktrees x 1 gh call each is not acceptable in a
# tool that runs after every agent spawn). The SHA arm reuses this same call
# (adds `headRefOid` to the existing --json field list) -- no additional gh
# round-trip.
#
# Fail-closed: if the open-PR list cannot be obtained (no `gh`, no repo slug,
# API error), every branch AND every HEAD commit is treated as protected --
# the guard degrades toward "never remove", never toward "assume no PRs
# exist". A SHA arm that failed open here would be worse than the gap it
# closes, so it shares the same _WTR_OPEN_PR_BRANCHES_AVAILABLE flag rather
# than getting an independent (and independently fallible) one.
#
# Test overrides (mirrors worktree-claims.sh's mock-var convention so fixtures
# never make a real `gh` call):
#   WTR_TEST_MODE=1                — required alongside the override below.
#                                    D#2001 PR2 fix-cycle 1 (security review):
#                                    WTR_OPEN_PR_BRANCHES_OVERRIDE alone used
#                                    `${VAR+set}`, so an operator accidentally
#                                    exporting it empty in a real shell would
#                                    silently disable this guard (every branch
#                                    reads as unprotected). Requiring the
#                                    explicit test-mode flag means the guard
#                                    can only be turned off by a caller that
#                                    is deliberately in a test harness.
#   WTR_OPEN_PR_BRANCHES_OVERRIDE — newline-separated branch names, used
#                                    instead of calling `gh pr list --state open`
#                                    ONLY when WTR_TEST_MODE=1 is also set.
#                                    Set (even to "") to mean "available, this
#                                    is the list".
#   WTR_OPEN_PR_HEAD_SHAS_OVERRIDE — newline-separated HEAD commit SHAs,
#                                    companion to the override above for the
#                                    SHA arm. Same double-gate: only read when
#                                    WTR_TEST_MODE=1 AND the branches override
#                                    is also set, so it can never be tripped
#                                    on its own by an ambient env var. Unset
#                                    (the common case -- existing fixtures
#                                    never set it) means "no protected SHAs",
#                                    identical to pre-D#2129 behaviour.
#   WTR_SKIP_GH=1                  — force the gh-unavailable degrade path.
#                                    No test-mode gate needed: this already
#                                    fails closed (every branch protected).

_WTR_OPEN_PR_BRANCHES_CACHE=""
_WTR_OPEN_PR_HEAD_SHAS_CACHE=""
_WTR_OPEN_PR_BRANCHES_LOADED=0
_WTR_OPEN_PR_BRANCHES_AVAILABLE=1

_wtr_load_open_pr_branches() {
  [[ "$_WTR_OPEN_PR_BRANCHES_LOADED" -eq 1 ]] && return 0
  _WTR_OPEN_PR_BRANCHES_LOADED=1

  if [[ "${WTR_TEST_MODE:-0}" == "1" && -n "${WTR_OPEN_PR_BRANCHES_OVERRIDE+set}" ]]; then
    _WTR_OPEN_PR_BRANCHES_CACHE="$WTR_OPEN_PR_BRANCHES_OVERRIDE"
    if [[ -n "${WTR_OPEN_PR_HEAD_SHAS_OVERRIDE+set}" ]]; then
      _WTR_OPEN_PR_HEAD_SHAS_CACHE="$WTR_OPEN_PR_HEAD_SHAS_OVERRIDE"
    else
      _WTR_OPEN_PR_HEAD_SHAS_CACHE=""
    fi
    _WTR_OPEN_PR_BRANCHES_AVAILABLE=1
    return 0
  fi
  if [[ "${WTR_SKIP_GH:-0}" == "1" ]]; then
    _WTR_OPEN_PR_BRANCHES_CACHE=""
    _WTR_OPEN_PR_HEAD_SHAS_CACHE=""
    _WTR_OPEN_PR_BRANCHES_AVAILABLE=0
    return 1
  fi
  if ! command -v gh &>/dev/null; then
    _WTR_OPEN_PR_BRANCHES_CACHE=""
    _WTR_OPEN_PR_HEAD_SHAS_CACHE=""
    _WTR_OPEN_PR_BRANCHES_AVAILABLE=0
    return 1
  fi

  local repo rc=0
  repo="$(_resolve_repo 2>/dev/null || true)"
  if [[ -z "$repo" ]]; then
    _WTR_OPEN_PR_BRANCHES_CACHE=""
    _WTR_OPEN_PR_HEAD_SHAS_CACHE=""
    _WTR_OPEN_PR_BRANCHES_AVAILABLE=0
    return 1
  fi

  # D#2001 PR2 fix-cycle 1 (security review, hardening item): the same
  # silent-truncation defect that blocked PR #2125 -- a bare --limit with no
  # check on whether the result actually hit that limit. Fetch as a JSON
  # array (not pre-flattened by --jq) so the PR *count* can be checked
  # before trusting the list; a count landing on the limit means the list
  # may be incomplete, and this fails closed rather than silently miss PRs.
  # D#2129: headRefOid added to the same --json field list -- one gh call
  # still covers both the branch arm and the new SHA arm.
  local limit=1000
  local raw
  raw=$(gh pr list --repo "$repo" --state open --limit "$limit" --json headRefName,baseRefName,headRefOid 2>/dev/null) || rc=$?
  if [[ $rc -ne 0 ]]; then
    _WTR_OPEN_PR_BRANCHES_CACHE=""
    _WTR_OPEN_PR_HEAD_SHAS_CACHE=""
    _WTR_OPEN_PR_BRANCHES_AVAILABLE=0
    return 1
  fi

  local pr_count
  pr_count=$(echo "$raw" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "-1")
  if [[ ! "$pr_count" =~ ^[0-9]+$ ]]; then
    _WTR_OPEN_PR_BRANCHES_CACHE=""
    _WTR_OPEN_PR_HEAD_SHAS_CACHE=""
    _WTR_OPEN_PR_BRANCHES_AVAILABLE=0
    return 1
  fi
  if [[ "$pr_count" -ge "$limit" ]]; then
    echo "open-PR guard: gh pr list returned ${pr_count} PRs (>= limit ${limit}) -- possible truncation, treating the list as unavailable rather than risk missing one" >&2
    _WTR_OPEN_PR_BRANCHES_CACHE=""
    _WTR_OPEN_PR_HEAD_SHAS_CACHE=""
    _WTR_OPEN_PR_BRANCHES_AVAILABLE=0
    return 1
  fi

  _WTR_OPEN_PR_BRANCHES_CACHE=$(echo "$raw" | python3 -c "
import json, sys
for pr in json.load(sys.stdin):
    h = pr.get('headRefName')
    b = pr.get('baseRefName')
    if h:
        print(h)
    if b:
        print(b)
" 2>/dev/null)
  _WTR_OPEN_PR_HEAD_SHAS_CACHE=$(echo "$raw" | python3 -c "
import json, sys
for pr in json.load(sys.stdin):
    s = pr.get('headRefOid')
    if s:
        print(s)
" 2>/dev/null)
  _WTR_OPEN_PR_BRANCHES_AVAILABLE=1
  return 0
}

# _wtr_worktree_has_open_pr <branch> <head_sha>
# Returns 0 (true — protected, never remove) when the branch is the head or
# base ref of an open PR, OR the given HEAD commit sha is an open PR's head
# commit (D#2129 -- this is what covers a detached worktree, which has no
# branch to match on), OR when the open-PR list could not be obtained
# (fail-closed). Returns 1 (false — no open PR found) only when the list was
# obtained and neither the branch nor the sha appears in it. Either argument
# may be empty (a detached worktree passes "" for branch; a branch-backed
# worktree always has a sha too, so both arms are checked whenever both are
# available -- protection is the union of the two, not the intersection).
_wtr_worktree_has_open_pr() {
  local branch="${1:-}" sha="${2:-}"

  _wtr_load_open_pr_branches
  if [[ "$_WTR_OPEN_PR_BRANCHES_AVAILABLE" -eq 0 ]]; then
    return 0
  fi

  if [[ -n "$branch" && -n "$_WTR_OPEN_PR_BRANCHES_CACHE" ]]; then
    local line
    while IFS= read -r line; do
      [[ "$line" == "$branch" ]] && return 0
    done <<< "$_WTR_OPEN_PR_BRANCHES_CACHE"
  fi

  if [[ -n "$sha" && -n "$_WTR_OPEN_PR_HEAD_SHAS_CACHE" ]]; then
    local sline
    while IFS= read -r sline; do
      [[ "$sline" == "$sha" ]] && return 0
    done <<< "$_WTR_OPEN_PR_HEAD_SHAS_CACHE"
  fi

  return 1
}

# ── Commands ──────────────────────────────────────────────────────────────────

_cmd_register() {
  local id="" role="" path="" pid="" discussion="" branch="" base="main"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id)         id="$2";         shift 2 ;;
      --role)       role="$2";       shift 2 ;;
      --path)       path="$2";       shift 2 ;;
      --pid)        pid="$2";        shift 2 ;;
      --discussion) discussion="$2"; shift 2 ;;
      --branch)     branch="$2";     shift 2 ;;
      --base)       base="$2";       shift 2 ;;
      *) echo "register: unknown arg $1" >&2; return 1 ;;
    esac
  done

  if [[ -z "$id" || -z "$role" || -z "$path" || -z "$pid" ]]; then
    echo "register: --id, --role, --path, --pid are required" >&2
    return 1
  fi

  local now
  now="$(_wtr_now_iso)"

  _wtr_lock

  local current
  current="$(_wtr_read_registry)"

  # Check cap BEFORE adding
  local active_count
  active_count=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(sum(1 for e in data if e.get('status') == 'active'))
" 2>/dev/null || echo "0")

  if [[ "$active_count" -ge "$WORKTREE_CAP" ]]; then
    _wtr_unlock
    echo "ERROR: worktree cap ($WORKTREE_CAP) reached — cannot register $id" >&2
    return 2
  fi

  # Idempotent: if already registered, just update heartbeat
  local new_registry
  new_registry=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
worktree_id=sys.argv[1]
# Check if already exists
existing=[e for e in data if e.get('worktree_id')==worktree_id]
if existing:
    # Already registered — idempotent
    print(json.dumps(data, indent=2))
    sys.exit(0)

entry={
    'worktree_id': sys.argv[1],
    'path': sys.argv[2],
    'agent_id': sys.argv[3],
    'role': sys.argv[4],
    'discussion': int(sys.argv[5]) if sys.argv[5] else None,
    'pr': None,
    'base_branch': sys.argv[6],
    'branch': sys.argv[7] if sys.argv[7] else None,
    'parent_pid': int(sys.argv[8]),
    'created_at': sys.argv[9],
    'last_heartbeat': sys.argv[9],
    'status': 'active',
}
data.append(entry)
print(json.dumps(data, indent=2))
" "$id" "$path" "$id" "$role" "$discussion" "$base" "$branch" "$pid" "$now")

  _wtr_write_registry "$new_registry"
  _wtr_unlock
  echo "registered: $id (role=$role pid=$pid)"
}

_cmd_heartbeat() {
  local id="${1:-}"
  if [[ -z "$id" ]]; then
    echo "heartbeat: worktree_id required" >&2
    return 1
  fi

  local now
  now="$(_wtr_now_iso)"

  _wtr_lock

  local current
  current="$(_wtr_read_registry)"

  local new_registry
  new_registry=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
wid,now=sys.argv[1],sys.argv[2]
found=False
for e in data:
    if e.get('worktree_id')==wid:
        e['last_heartbeat']=now
        found=True
        break
if not found:
    import sys; print(f'heartbeat: {wid} not found in registry', file=sys.stderr)
print(json.dumps(data, indent=2))
" "$id" "$now")

  _wtr_write_registry "$new_registry"
  _wtr_unlock
}

_cmd_mark_status() {
  local id="${1:-}"
  local status="${2:-}"
  if [[ -z "$id" || -z "$status" ]]; then
    echo "mark-status: worktree_id and status required" >&2
    return 1
  fi

  local valid_statuses="active committed pushed merged orphaned discarded"
  if ! echo "$valid_statuses" | grep -qw "$status"; then
    echo "mark-status: invalid status '$status'. Valid: $valid_statuses" >&2
    return 1
  fi

  _wtr_lock

  local current
  current="$(_wtr_read_registry)"

  local new_registry
  new_registry=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
wid,status=sys.argv[1],sys.argv[2]
found=False
for e in data:
    if e.get('worktree_id')==wid:
        e['status']=status
        found=True
        break
if not found:
    print(f'mark-status: {wid} not found in registry', file=sys.stderr)
print(json.dumps(data, indent=2))
" "$id" "$status")

  _wtr_write_registry "$new_registry"
  _wtr_unlock
  echo "mark-status: $id -> $status"
}

_cmd_set_pr() {
  local id="${1:-}"
  local pr="${2:-}"
  if [[ -z "$id" || -z "$pr" ]]; then
    echo "set-pr: worktree_id and pr_number required" >&2
    return 1
  fi

  _wtr_lock

  local current
  current="$(_wtr_read_registry)"

  local new_registry
  new_registry=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
wid,pr=sys.argv[1],int(sys.argv[2])
found=False
for e in data:
    if e.get('worktree_id')==wid:
        e['pr']=pr
        found=True
        break
if not found:
    print(f'set-pr: {wid} not found in registry', file=sys.stderr)
print(json.dumps(data, indent=2))
" "$id" "$pr")

  _wtr_write_registry "$new_registry"
  _wtr_unlock
  echo "set-pr: $id -> pr=$pr"
}

# D#2222: reconcile a registered entry's path against the tree an agent
# actually ran in. The Agent tool provisions its own worktree whenever
# isolation="worktree" is passed on the Agent() call, independent of any
# path spawn-agent.sh assembled into the prompt — so a registry entry
# written at spawn time can describe a tree nothing ever used. Call this
# once the Agent() call returns its real worktree path (or once the agent's
# own AGENT_OUTPUT/pwd report is known), so a mismatch is caught by
# construction instead of by an agent happening to notice and say so.
_cmd_reconcile_path() {
  local id="" actual_path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id)           id="$2";           shift 2 ;;
      --actual-path)  actual_path="$2";  shift 2 ;;
      *) echo "reconcile-path: unknown arg $1" >&2; return 1 ;;
    esac
  done

  if [[ -z "$id" || -z "$actual_path" ]]; then
    echo "reconcile-path: --id and --actual-path are required" >&2
    return 1
  fi

  # Reviewer finding (non-blocking, PR #2231): this subcommand's whole
  # purpose is correcting a registry entry to reality, so a --actual-path
  # that doesn't exist on disk would write a "corrected" entry that is
  # itself fiction. Guard against that cheaply before touching the registry.
  if [[ ! -e "$actual_path" ]]; then
    echo "reconcile-path: --actual-path '$actual_path' does not exist — refusing to reconcile against it" >&2
    return 1
  fi

  _wtr_lock

  local current
  current="$(_wtr_read_registry)"

  local combined
  combined=$(echo "$current" | python3 -c "
import json, os, sys
data = json.load(sys.stdin)
wid, actual = sys.argv[1], sys.argv[2]

def norm(p):
    try:
        return os.path.realpath(p)
    except Exception:
        return p

status = 'not_found'
for e in data:
    if e.get('worktree_id') == wid:
        registered = e.get('path', '')
        if norm(registered) == norm(actual):
            status = 'match'
        else:
            e['original_path'] = registered
            e['path'] = actual
            e['path_reconciled'] = True
            status = 'corrected:' + registered
        break

print('STATUS:' + status)
print(json.dumps(data, indent=2))
" "$id" "$actual_path")

  local status_line new_registry
  status_line="$(echo "$combined" | head -1)"
  new_registry="$(echo "$combined" | tail -n +2)"

  _wtr_write_registry "$new_registry"
  _wtr_unlock

  case "$status_line" in
    STATUS:not_found)
      echo "reconcile-path: $id not found in registry — nothing to reconcile" >&2
      return 1
      ;;
    STATUS:match)
      echo "reconcile-path: $id path matches registry ($actual_path) — no correction needed"
      ;;
    STATUS:corrected:*)
      local orig="${status_line#STATUS:corrected:}"
      echo "WARN: reconcile-path: $id registry path mismatch — registered='$orig' actual='$actual_path'. Registry entry corrected." >&2
      ;;
    *)
      echo "reconcile-path: unexpected result for $id: $status_line" >&2
      return 1
      ;;
  esac
}

_cmd_list() {
  local filter_status="" json_mode=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --status) filter_status="$2"; shift 2 ;;
      --json)   json_mode=true; shift ;;
      *) echo "list: unknown arg $1" >&2; return 1 ;;
    esac
  done

  # Read-only: shared lock
  touch "$_WTR_LOCK"
  exec 9>"$_WTR_LOCK"
  flock -s 9

  local current
  current="$(_wtr_read_registry)"

  flock -u 9 2>/dev/null || true
  exec 9>&- 2>/dev/null || true

  if [[ "$json_mode" == "true" ]]; then
    if [[ -n "$filter_status" ]]; then
      echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(json.dumps([e for e in data if e.get('status')==sys.argv[1]], indent=2))
" "$filter_status"
    else
      echo "$current" | python3 -m json.tool
    fi
    return 0
  fi

  # Human-readable table
  echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
status_filter=sys.argv[1] if len(sys.argv)>1 else ''
if status_filter:
    data=[e for e in data if e.get('status')==status_filter]
if not data:
    print('(no entries)')
    sys.exit(0)
print(f\"{'ID':<20} {'ROLE':<18} {'STATUS':<12} {'PID':<8} {'DISC':<6} {'PR':<6} {'HEARTBEAT'}\")
print('-'*90)
for e in data:
    hb=e.get('last_heartbeat','?')[:19] if e.get('last_heartbeat') else '?'
    print(f\"{e.get('worktree_id','?'):<20} {e.get('role','?'):<18} {e.get('status','?'):<12} {str(e.get('parent_pid','?')):<8} {str(e.get('discussion') or ''):<6} {str(e.get('pr') or ''):<6} {hb}\")
" "$filter_status"
}

_cmd_count_active() {
  touch "$_WTR_LOCK"
  exec 9>"$_WTR_LOCK"
  flock -s 9

  local current
  current="$(_wtr_read_registry)"

  flock -u 9 2>/dev/null || true
  exec 9>&- 2>/dev/null || true

  echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
# Count 'live' worktrees: active, committed, or pushed -- matches reaper's live classification
print(sum(1 for e in data if e.get('status') in ('active','committed','pushed')))
" 2>/dev/null || echo "0"
}

# _cmd_count_disk — count worktree directories actually on disk.
#
# The registry (_cmd_count_active above) is never populated: nothing in this
# repo calls `worktree_registry register`, and the .claude/worktrees/agent-*
# directories are created by the Claude Code harness, not by anything here.
# Disk is therefore the only authoritative source for "how many worktrees
# exist right now" -- this reuses _cmd_reap's enumeration (find ... -maxdepth 1
# -mindepth 1 -type d) rather than adding a second enumerator with different
# semantics.
_cmd_count_disk() {
  local count=0
  if [[ -d "$_WTR_WORKTREES_DIR" ]]; then
    while IFS= read -r _; do
      count=$((count + 1))
    done < <(find "$_WTR_WORKTREES_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null)
  fi
  echo "$count"
}

_cmd_reap() {
  local ttl_min="${WORKTREE_TTL_MIN:-60}"
  local dry_run=false
  local clean_generated_wiki=false
  # D#2001 PR2: the git-tracked-worktree removal path (Step 6 below) is OFF
  # by default in a real (non-dry-run) pass. reap-worktrees.sh is invoked
  # live (no --dry-run) from post-agent-hook.sh after every agent completion
  # -- shipping this capability enabled-by-default there would silently start
  # removing real worktrees on the very next agent completion after merge.
  # D#2149: --dry-run now respects this flag the same way a real run does --
  # a dry-run without the opt-in classifies a git-tracked candidate as
  # skipped-git-tracked (with an informational line), not would-remove.
  local enable_git_tracked_removal=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ttl-min)                    ttl_min="$2"; shift 2 ;;
      --dry-run)                    dry_run=true; shift ;;
      --clean-generated-wiki)       clean_generated_wiki=true; shift ;;
      --enable-git-tracked-removal) enable_git_tracked_removal=true; shift ;;
      *) echo "reap: unknown arg $1" >&2; return 1 ;;
    esac
  done

  # D#2001 PR2 AC-13: real removals of git-tracked worktrees are capped per
  # pass so a first real run cannot delete the whole eligible population in
  # one action. Reporting (--dry-run) is uncapped -- it is not destructive.
  local gt_removal_cap="${WORKTREE_REAP_MAX_PER_PASS:-25}"
  local git_tracked_removed_this_pass=0

  local ttl_sec=$(( ttl_min * 60 ))
  local now_epoch
  now_epoch="$(_wtr_now_epoch)"
  local today
  today="$(_wtr_date_ymd)"

  # Resolve self-exclusion root once per pass — never per entry.
  _wtr_resolve_self_root

  # Ensure archive dir exists
  mkdir -p "$_WTR_ARCHIVE_DIR"

  _wtr_lock

  local current
  current="$(_wtr_read_registry)"

  local merged_cleanup=0
  local newly_orphaned=0
  local patches_archived=0
  local reaped=0

  # Build set of on-disk worktrees (names under .claude/worktrees/)
  local on_disk_ids=()
  if [[ -d "$_WTR_WORKTREES_DIR" ]]; then
    while IFS= read -r wt_path; do
      on_disk_ids+=("$(basename "$wt_path")")
    done < <(find "$_WTR_WORKTREES_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null)
  fi

  # D#2001 PR1, AC-3: a registry that is absent, empty, or malformed must not
  # look identical to "nothing to clean up" when directories actually exist on
  # disk. Classify it loudly instead of letting a downstream `json.load`
  # failure disappear behind `2>/dev/null || true`.
  local registry_status="ok"
  if [[ ! -f "$_WTR_REGISTRY" ]]; then
    registry_status="missing"
  else
    registry_status=$(echo "$current" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print('malformed')
    sys.exit(0)
if not isinstance(data, list):
    print('malformed')
elif len(data) == 0:
    print('empty')
else:
    print('ok')
" 2>/dev/null || echo "malformed")
  fi

  if [[ "$registry_status" != "ok" && "${#on_disk_ids[@]}" -gt 0 ]]; then
    echo "registry-empty: .autonomous-team/worktrees.json is ${registry_status} but ${#on_disk_ids[@]} worktree directories exist on disk under .claude/worktrees/ — the registry cannot be used to decide what exists" >&2
  fi

  # Step 1: Auto-cleanup merged worktrees still on disk
  local registry_ids_merged
  registry_ids_merged=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print('\n'.join(e['worktree_id'] for e in data if e.get('status')=='merged'))
" 2>/dev/null || true)

  while IFS= read -r wid; do
    [[ -z "$wid" ]] && continue
    local entry_path
    entry_path=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
for e in data:
    if e.get('worktree_id')==sys.argv[1]:
        print(e.get('path',''))
        break
" "$wid" 2>/dev/null || true)

    if [[ -n "$entry_path" && -d "${_WTR_REPO_ROOT}/${entry_path#./}" ]]; then
      local abs_path="${_WTR_REPO_ROOT}/${entry_path#./}"

      if _wtr_is_self "$abs_path"; then
        echo "self-exclusion-refused: $wid ($abs_path)" >&2
        continue
      fi

      if [[ "$dry_run" == "false" ]]; then
        git -C "$_WTR_REPO_ROOT" worktree unlock "$abs_path" 2>/dev/null || true
        git -C "$_WTR_REPO_ROOT" worktree remove -f -f "$abs_path" 2>/dev/null || true
        git -C "$_WTR_REPO_ROOT" worktree prune 2>/dev/null || true
        current=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
for e in data:
    if e.get('worktree_id')==sys.argv[1]:
        e['status']='discarded'
        break
print(json.dumps(data, indent=2))
" "$wid")
      fi
      merged_cleanup=$((merged_cleanup + 1))
      echo "  merged-cleanup: $wid (path=$entry_path)"
    fi
  done <<< "$registry_ids_merged"

  # Step 2 & 3 (parent_pid/last_heartbeat orphan detection) archived 2026-08-24
  # (D#2159): it only ever fired for registry entries created by
  # `worktree_registry register`, and nothing in production code calls
  # `register` — only test fixtures do (see the count-disk comment above).
  # With the registry always `[]` in production, this block's loop over
  # registry_active_ids ran zero iterations regardless of on-disk worktree
  # count; verified with a before/after dry-run parity diff at removal time.
  # Full code + why/how-to-restore: archive/worktree-registry-heartbeat-half-2026-08-24/

  # After registry orphans are reaped: detect if parent repo is on a non-main [gone] branch
  # (leaked feature branch from executor checkout). Auto-checkout main when safe.
  if [[ "$dry_run" == "false" ]]; then
    local parent_branch
    parent_branch=$(git -C "$_WTR_REPO_ROOT" branch --show-current 2>/dev/null || echo "")
    if [[ -n "$parent_branch" && "$parent_branch" != "main" ]]; then
      local dirty
      dirty=$(git -C "$_WTR_REPO_ROOT" status --porcelain 2>/dev/null | head -1 || echo "")
      if [[ -z "$dirty" ]]; then
        echo "  parent-branch-recovery: parent repo on '${parent_branch}' (not main) — auto-checkout main"
        git -C "$_WTR_REPO_ROOT" checkout main 2>/dev/null && \
          echo "  parent-branch-recovery: switched to main" || \
          echo "  parent-branch-recovery: WARNING — checkout main failed (needs manual fix)"
      else
        echo "  parent-branch-recovery: WARNING — parent on '${parent_branch}' with uncommitted changes, cannot auto-checkout main"
      fi
    fi
  fi

  # Step 5: Back-compat — on-disk worktrees with NO registry entry
  #
  # Safety model: a dir is physically removed ONLY when ALL FOUR hold:
  #   1. absent from the registry (worktrees.json), AND
  #   2. absent from `git worktree list --porcelain` (resolved absolute path), AND
  #   3. `git -C <dir> status --porcelain` is EMPTY (no uncommitted tracked changes), AND
  #   4. `git -C <dir> rev-list HEAD --not --remotes` is EMPTY (fully pushed).
  # Any dir failing condition 3 or 4 is archived (diff saved) and SKIPPED — never removed.
  # The rm -rf fallback is path-guarded: refuses to remove anything not strictly under
  # ${_WTR_WORKTREES_DIR}/ and never removes ${_WTR_WORKTREES_DIR} itself.

  # Compute set of git-tracked worktree paths (mirror of health_report._git_worktree_paths())
  # One `git worktree list --porcelain` call, fetched once here and reused by
  # Step 6 below for its branch lookup (D#2001 PR2) -- do not call this a
  # second time.
  local git_worktree_porcelain
  git_worktree_porcelain=$(git -C "$_WTR_REPO_ROOT" worktree list --porcelain 2>/dev/null || true)

  local git_tracked_paths
  git_tracked_paths=$(echo "$git_worktree_porcelain" | awk '/^worktree / { print substr($0, 10) }')

  for on_disk_id in "${on_disk_ids[@]:-}"; do
    [[ -z "$on_disk_id" ]] && continue

    # Condition 1: absent from registry
    local in_registry
    in_registry=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
found=any(e.get('worktree_id')==sys.argv[1] for e in data)
print('yes' if found else 'no')
" "$on_disk_id" 2>/dev/null || echo "no")
    [[ "$in_registry" == "yes" ]] && continue

    local abs_path="${_WTR_WORKTREES_DIR}/${on_disk_id}"
    [[ -d "$abs_path" ]] || continue

    # Resolve abs_path to handle symlinks / redundant components
    local resolved_abs_path
    resolved_abs_path=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$abs_path" 2>/dev/null || echo "$abs_path")

    # Self-exclusion guard — covers both removal branches below (dirty/unpushed
    # archive-then-prune, and clean+pushed remove), which share this abs_path.
    # D#2120: already resolved above -- tell _wtr_is_self so it doesn't spawn
    # a second, redundant realpath for the same string.
    if _wtr_is_self "$resolved_abs_path" 1; then
      echo "self-exclusion-refused: $on_disk_id ($resolved_abs_path)" >&2
      continue
    fi

    # Condition 2: absent from git worktree list --porcelain
    # Compare resolved paths — dirs git still tracks are handled by existing registry paths.
    local in_git_tracked
    in_git_tracked=$(echo "$git_tracked_paths" | python3 -c "
import sys, os
target = os.path.realpath(sys.argv[1])
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        if os.path.realpath(line) == target:
            print('yes')
            sys.exit(0)
    except Exception:
        pass
print('no')
" "$abs_path" 2>/dev/null || echo "no")

    if [[ "$in_git_tracked" == "yes" ]]; then
      # Git still tracks this dir -- this back-compat (on-disk, no-registry)
      # path never removes a git-tracked worktree. D#2001 PR2: the deferral
      # below used to be false (it pointed at a registry with zero
      # production callers, so nothing ever picked this up). Step 6 below
      # is now the real handler -- it already runs the per-worktree
      # dirty/unpushed/self/young classification this predicate needs, with
      # bounded parallelism, so the six-part safe-removal predicate and the
      # open-PR guard live there instead of duplicating that computation
      # (and the two `git status`/`rev-list` subprocess calls it costs) a
      # second time in this sequential loop.
      continue
    fi

    # Candidate: absent from registry AND absent from git worktree list.
    # Now evaluate safety predicates before any removal.

    # Condition 3: clean working tree (no uncommitted tracked changes)
    local status_output
    status_output=$(git -C "$abs_path" status --porcelain 2>/dev/null || true)

    # Condition 4: fully pushed (no commit unreachable from any remote ref)
    # Use rev-list HEAD --not --remotes — canonical, immune to stale local refs.
    local unpushed_output
    unpushed_output=$(git -C "$abs_path" rev-list HEAD --not --remotes 2>/dev/null || true)

    local is_clean=true
    local is_pushed=true
    [[ -n "$status_output" ]] && is_clean=false
    [[ -n "$unpushed_output" ]] && is_pushed=false

    # --clean-generated-wiki rescue: when the flag is set and the dir is an orphan
    # (already confirmed above: absent from registry AND absent from git worktree list),
    # attempt to discard ONLY the two named generated wiki reports if — and only if —
    # ALL of the following hold (AC-1):
    #   (b) git status --porcelain lists ONLY wiki/Corpus-Drift-Report.md and/or
    #       wiki/Project-Status.md as modified (M / ' M'), zero other tracked changes,
    #       zero untracked (??) entries.
    #   (c) git rev-list HEAD --not --remotes is empty (no unpushed commits).
    # Any dir failing any condition is left unchanged and falls through to the
    # existing dirty/unpushed archive+skip branch below.
    if [[ "$clean_generated_wiki" == "true" && "$is_pushed" == "true" && "$is_clean" == "false" ]]; then
      # Evaluate the strict predicate using Python (avoids bash portability pitfalls with
      # multi-line status parsing). Returns "ok" only when ALL conditions in AC-1b hold.
      local rescue_ok
      rescue_ok=$(python3 - "$status_output" <<'PYEOF'
import sys

# AC-1 permitted paths (relative to repo root, no leading slash)
PERMITTED = {"wiki/Corpus-Drift-Report.md", "wiki/Project-Status.md"}

status_text = sys.argv[1] if len(sys.argv) > 1 else ""
lines = [l for l in status_text.splitlines() if l]  # drop blank lines

if not lines:
    # status is empty — should not reach here (is_clean==true), but be safe
    print("no")
    sys.exit(0)

changed_paths = set()
for line in lines:
    # porcelain v1 format: XY <path>  (2 status chars + space + path)
    if len(line) < 4:
        print("no")
        sys.exit(0)
    xy = line[:2]
    path = line[3:]
    # Untracked files (??) — AC-1b forbids any untracked entries
    if xy == "??":
        print("no")
        sys.exit(0)
    # Only allow modification status codes: " M" (worktree modified) or "M " (index modified)
    # In practice these dirs only have " M" (unstaged wiki file changes), but accept "M " too.
    if xy.strip() not in ("M",):
        # Only pure 'M' codes (XY where one side is M and the other is space)
        # Accept: " M" (xy[0]==' ', xy[1]=='M') or "M " (xy[0]=='M', xy[1]==' ')
        if not ((xy[0] == ' ' and xy[1] == 'M') or (xy[0] == 'M' and xy[1] == ' ')):
            print("no")
            sys.exit(0)
    if path not in PERMITTED:
        print("no")
        sys.exit(0)
    changed_paths.add(path)

# Must be a non-empty subset of the two permitted paths
if not changed_paths:
    print("no")
    sys.exit(0)

print("ok")
PYEOF
)

      if [[ "$rescue_ok" == "ok" ]]; then
        # Predicate holds: discard ONLY the two named generated reports.
        if [[ "$dry_run" == "true" ]]; then
          echo "  would-clean-generated-wiki: $on_disk_id (would check out 2 named reports, then remove)"
          reaped=$((reaped + 1))
          continue
        fi

        # Surgical checkout — ONLY these two explicit paths, never a wildcard.
        git -C "$abs_path" checkout -- wiki/Corpus-Drift-Report.md wiki/Project-Status.md 2>/dev/null || true

        # Re-evaluate safety predicates after the checkout.
        status_output=$(git -C "$abs_path" status --porcelain 2>/dev/null || true)
        unpushed_output=$(git -C "$abs_path" rev-list HEAD --not --remotes 2>/dev/null || true)
        is_clean=true
        is_pushed=true
        [[ -n "$status_output" ]] && is_clean=false
        [[ -n "$unpushed_output" ]] && is_pushed=false

        if [[ "$is_clean" == "true" && "$is_pushed" == "true" ]]; then
          local log_msg="[$(date +%H:%M)] reap-worktrees: cleaned-generated-wiki: $on_disk_id (checked out 2 named reports)"
          echo "  cleaned-generated-wiki: $on_disk_id (checked out 2 named reports)"
          bash "${_WTR_REPO_ROOT}/scripts/rotate-team-log.sh" comment "$log_msg" 2>/dev/null || true
          # Fall through — is_clean and is_pushed are now true, so the safe-remove
          # path below will handle physical removal.
        else
          # Checkout did not fully clean the dir — preserve it unchanged.
          echo "  skipped-unsafe (post-rescue still dirty/unpushed): $on_disk_id"
          continue
        fi
      fi
      # If rescue_ok != "ok": fall through to existing skip branch below.
    fi

    if [[ "$is_clean" == "false" || "$is_pushed" == "false" ]]; then
      # Has uncommitted or unpushed work — archive the patch first, then prune the dir.
      # The dir is safe to remove because:
      #   (1) absent from registry → no running agent owns it
      #   (2) absent from git worktree list → not a live git-tracked worktree
      # We only prune AFTER confirming the patch landed on disk (non-empty file).

      # Safety gate: if the worktree has untracked files (git status --porcelain shows
      # '??' entries), do NOT prune it. git diff HEAD captures only tracked changes, so
      # untracked files would be silently lost even if the patch file appears non-empty
      # (the header comments alone make -s return true). Preserve the dir so no
      # untracked work is ever deleted without being archived first.
      if echo "$status_output" | grep -qE '^\?\?'; then
        echo "  skipped-unsafe (has-untracked-files): $on_disk_id" >&2
        continue
      fi

      local skip_reason=""
      if [[ "$is_clean" == "false" && "$is_pushed" == "false" ]]; then
        skip_reason="dirty+unpushed"
      elif [[ "$is_clean" == "false" ]]; then
        skip_reason="dirty"
      else
        skip_reason="unpushed"
      fi

      local uncommitted
      uncommitted=$(git -C "$abs_path" diff HEAD 2>/dev/null || true)
      local unpushed_log
      unpushed_log=$(git -C "$abs_path" log -p HEAD --not --remotes 2>/dev/null || true)

      if [[ "$dry_run" == "false" ]]; then
        local patch_file="${_WTR_ARCHIVE_DIR}/${on_disk_id}-${today}.patch"
        {
          echo "# Orphan worktree (no-registry, ${skip_reason}): $on_disk_id"
          echo "# Reason: absent from registry + git worktree list; patch archived before removal"
          echo "# ===== uncommitted (git diff HEAD) ====="
          echo "$uncommitted"
          echo "# ===== unpushed commits (git log -p HEAD --not --remotes) ====="
          echo "$unpushed_log"
        } > "$patch_file"

        # Safety gate: only prune the dir when the patch file was written successfully.
        if [[ -s "$patch_file" ]]; then
          patches_archived=$((patches_archived + 1))
          echo "  patch-archived (no-registry+${skip_reason}): $on_disk_id -> archive/orphan-diffs/${on_disk_id}-${today}.patch"

          # Path guard: refuse to rm -rf anything not strictly under ${_WTR_WORKTREES_DIR}/
          local resolved_worktrees_dir_dirty
          resolved_worktrees_dir_dirty=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$_WTR_WORKTREES_DIR" 2>/dev/null || echo "$_WTR_WORKTREES_DIR")

          local path_guard_ok_dirty=false
          if [[ "$resolved_abs_path" == "${resolved_worktrees_dir_dirty}/"* && \
                "$resolved_abs_path" != "$resolved_worktrees_dir_dirty" ]]; then
            path_guard_ok_dirty=true
          fi

          if [[ "$path_guard_ok_dirty" == "false" ]]; then
            echo "  path-guard-refused (rm -rf blocked — path not under worktrees dir): $on_disk_id ($resolved_abs_path)" >&2
            continue
          fi

          # Prune: attempt git worktree remove, then rm -rf fallback
          git -C "$_WTR_REPO_ROOT" worktree unlock "$abs_path" 2>/dev/null || true
          git -C "$_WTR_REPO_ROOT" worktree remove -f "$abs_path" 2>/dev/null || true
          git -C "$_WTR_REPO_ROOT" worktree prune 2>/dev/null || true
          if [[ -d "$abs_path" ]]; then
            rm -rf "$abs_path"
          fi

          reaped=$((reaped + 1))
          echo "  pruned-after-archive (no-registry+${skip_reason}): $on_disk_id"
        else
          # Patch write failed (empty file) — do NOT remove the dir; preserve for safety.
          echo "  skipped-unsafe (archive-write-failed): $on_disk_id" >&2
        fi
      else
        # Dry-run: show what would happen
        echo "  would-archive (no-registry+${skip_reason}): $on_disk_id"
        echo "  would-prune-after-archive (no-registry+${skip_reason}): $on_disk_id"
        reaped=$((reaped + 1))
      fi
      continue
    fi

    # All four conditions satisfied — safe to remove.
    if [[ "$dry_run" == "true" ]]; then
      echo "  would-remove (no-registry+untracked+clean+pushed): $on_disk_id"
      reaped=$((reaped + 1))
      continue
    fi

    # Path guard: refuse to rm -rf anything not strictly under ${_WTR_WORKTREES_DIR}/
    local resolved_worktrees_dir
    resolved_worktrees_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$_WTR_WORKTREES_DIR" 2>/dev/null || echo "$_WTR_WORKTREES_DIR")

    local path_guard_ok=false
    # Must start with worktrees_dir + "/" AND not equal worktrees_dir itself
    if [[ "$resolved_abs_path" == "${resolved_worktrees_dir}/"* && \
          "$resolved_abs_path" != "$resolved_worktrees_dir" ]]; then
      path_guard_ok=true
    fi

    if [[ "$path_guard_ok" == "false" ]]; then
      echo "  path-guard-refused (rm -rf blocked — path not under worktrees dir): $on_disk_id ($resolved_abs_path)" >&2
      continue
    fi

    # Attempt git worktree remove first (best-effort; may no-op for git-untracked dirs)
    git -C "$_WTR_REPO_ROOT" worktree unlock "$abs_path" 2>/dev/null || true
    git -C "$_WTR_REPO_ROOT" worktree remove -f "$abs_path" 2>/dev/null || true
    git -C "$_WTR_REPO_ROOT" worktree prune 2>/dev/null || true

    # Fallback: if dir still exists on disk (git no longer tracks it, so remove no-ops),
    # perform guarded rm -rf.
    if [[ -d "$abs_path" ]]; then
      rm -rf "$abs_path"
      echo "  physically-removed (rm -rf fallback): $on_disk_id"
    fi

    reaped=$((reaped + 1))
    echo "  discarded (no-registry+untracked): $on_disk_id"
  done

  # ── Step 6: Enumeration + skip-reason report, now the git-tracked-removal
  #    handler (D#2001 PR1 added the report; PR2 adds the removal) ──────────
  #
  # Counts every worktree `git worktree list --porcelain` still knows about
  # (excluding the main checkout) and classifies why the reaper does or does
  # not act on it. This is the single place a "clean, pushed, old enough,
  # not self" git-tracked worktree gets evaluated -- it already runs the
  # per-worktree dirty/unpushed classification (with bounded parallelism)
  # that the six-part safe-removal predicate needs, so the predicate is
  # applied here rather than duplicating that computation (and its two git
  # subprocess calls per worktree) a second time back in Step 5's on-disk
  # loop, which Step 5 still defers to this step for every git-tracked path.
  #
  # D#2149: `--dry-run` previews the invocation you actually typed. Without
  # --enable-git-tracked-removal, a dry-run classifies a git-tracked
  # candidate the same way a real run does -- skipped-git-tracked, nothing
  # more evaluated for it (just an informational candidate-git-tracked line
  # to stderr for visibility). This step's cost and behaviour for that
  # bucket are unchanged from PR1 either way. With the opt-in, `--dry-run`
  # evaluates the full predicate (open-PR guard, removal cap) for an
  # accurate "would-remove" preview -- read-only, no mutation risk. A REAL
  # removal additionally requires --enable-git-tracked-removal: this hot
  # path runs after every agent completion (post-agent-hook.sh:533, live,
  # no --dry-run), and shipping this capability enabled-by-default there
  # would silently start removing real worktrees on the very next agent
  # completion after merge.
  #
  # Every git-tracked path lands in exactly one skip bucket OR is
  # reaped/would-be-reaped -- the skip-breakdown sums to `enumerated` minus
  # that reaped count.
  # D#2120: same value _wtr_is_self needs below -- share its cache instead of
  # spawning a second python3 for the identical realpath.
  _wtr_resolved_repo_root
  local resolved_repo_root="$_WTR_REPO_ROOT_RESOLVED_CACHE"

  # Path -> branch map, parsed once (pure bash, no subprocess) from the same
  # `git worktree list --porcelain` text Step 5 already fetched into
  # $git_worktree_porcelain -- never call that a second time.
  # D#2129: same porcelain text also carries a `HEAD <sha>` line for every
  # entry (branch-backed or detached) -- parsed here alongside the branch
  # map, no new subprocess, so a detached worktree's commit is available to
  # the open-PR guard below without a per-entry `git rev-parse`.
  local -A _gt_branch_map=()
  local -A _gt_head_map=()
  local _gwp_path=""
  while IFS= read -r _gwp_line; do
    if [[ "$_gwp_line" == worktree\ * ]]; then
      _gwp_path="${_gwp_line#worktree }"
    elif [[ "$_gwp_line" == branch\ refs/heads/* ]]; then
      _gt_branch_map["$_gwp_path"]="${_gwp_line#branch refs/heads/}"
    elif [[ "$_gwp_line" == HEAD\ * ]]; then
      _gt_head_map["$_gwp_path"]="${_gwp_line#HEAD }"
    elif [[ -z "$_gwp_line" ]]; then
      _gwp_path=""
    fi
  done <<< "$git_worktree_porcelain"

  # Batch-resolve every git-tracked path's realpath AND mtime in ONE python3
  # call instead of one `python3` (realpath) plus one `stat` spawn per entry.
  # On a 200-worktree host this pair of per-entry forks was the single
  # biggest cost in this pass (D#2001 PR1 fix-cycle 1: +9.4s/+79% measured
  # against origin/main). D#2120: `_wtr_is_self` below is now handed this
  # same batched-resolved value (already_resolved=1) instead of the raw
  # path, so it no longer spawns a second per-entry python3 to redo the
  # realpath this loop already paid for.
  local -A _gt_resolved_map=() _gt_mtime_map=()
  while IFS=$'\t' read -r _gt_orig _gt_res _gt_mt; do
    [[ -z "$_gt_orig" ]] && continue
    _gt_resolved_map["$_gt_orig"]="$_gt_res"
    _gt_mtime_map["$_gt_orig"]="$_gt_mt"
  done < <(python3 -c "
import os, sys
for line in sys.stdin:
    line = line.rstrip('\n')
    if not line:
        continue
    resolved = os.path.realpath(line)
    try:
        # D#2001 PR2 fix-cycle 1 (security review): the root directory's own
        # mtime does not move when a file deep in the tree is written, so a
        # worktree an agent has been actively editing for hours can still
        # read as 'young' by root-mtime alone -- it will not read as
        # ancient/idle, but it does not protect against the opposite
        # misreading either. Also checking .git/index catches any git
        # operation (add, commit, checkout, status refreshing the stat
        # cache) at the cost of one extra, already-batched stat call --
        # cheap, bounded, and does not require walking the tree (which
        # would add real per-entry cost to a hook that runs after every
        # agent completion). It does not catch a pure filesystem write with
        # no git interaction at all; closing that gap fully needs either a
        # full tree walk (real hot-path cost) or a liveness signal this
        # tool does not have (the registry this predicate deliberately does
        # not depend on is empty by design -- see the Spec).
        newest = os.stat(line).st_mtime
        try:
            idx_mtime = os.stat(os.path.join(line, '.git', 'index')).st_mtime
            if idx_mtime > newest:
                newest = idx_mtime
        except OSError:
            pass
        mtime = str(int(newest))
    except OSError:
        mtime = ''
    print(line + '\t' + resolved + '\t' + mtime)
" <<< "$git_tracked_paths")

  local enumerated=0
  local skip_self=0 skip_young=0 skip_dirty=0 skip_unpushed=0 skip_git_tracked=0
  local skip_open_pr=0 skip_unknown=0
  # Reused by the git-tracked candidate branch below for the path-scope
  # guard (D#2001 PR2 fix-cycle 1: this guard existed only for Step 5's
  # rm -rf fallback, never applied here). Computed once for the whole pass,
  # not per entry -- Pass 1 below already resolves every candidate's own
  # path in the batched call above, so no new per-entry subprocess is added.
  local resolved_worktrees_dir
  resolved_worktrees_dir="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$_WTR_WORKTREES_DIR" 2>/dev/null || echo "$_WTR_WORKTREES_DIR")"

  # Pass 1: cheap, in-process filtering (main checkout / self / missing dir /
  # young). Whatever survives this pass is what actually needs the two git
  # subprocesses (status + rev-list) — the part batching realpath/mtime
  # cannot remove, since each worktree's dirty/unpushed state is genuinely
  # per-worktree. Collected into an array so Pass 2 can run them concurrently
  # instead of one-at-a-time.
  local -a _gt_candidates=()

  while IFS= read -r _gt_path; do
    [[ -z "$_gt_path" ]] && continue

    local _gt_resolved="${_gt_resolved_map[$_gt_path]:-$_gt_path}"
    [[ "$_gt_resolved" == "$resolved_repo_root" ]] && continue  # main checkout — excluded by definition

    enumerated=$((enumerated + 1))

    # D#2120: $_gt_resolved (above) is the same batched realpath Step 6
    # already paid for -- pass it in resolved so _wtr_is_self doesn't spawn
    # its own per-entry python3 to redo that exact work.
    if _wtr_is_self "$_gt_resolved" 1; then
      skip_self=$((skip_self + 1))
      continue
    fi

    if [[ ! -d "$_gt_path" ]]; then
      # Git still has admin metadata but the directory is gone. `git worktree
      # prune` (called elsewhere in this file) reconciles this; report it
      # under the same bucket as any other entry this tool cannot yet act on.
      skip_git_tracked=$((skip_git_tracked + 1))
      continue
    fi

    local _gt_mtime="${_gt_mtime_map[$_gt_path]:-}"
    [[ -z "$_gt_mtime" ]] && _gt_mtime="$now_epoch"  # stat failed in the batch pass — treat as fresh, not stale
    local _gt_age=$(( now_epoch - _gt_mtime ))
    if [[ "$_gt_age" -lt "$ttl_sec" ]]; then
      skip_young=$((skip_young + 1))
      continue
    fi

    _gt_candidates+=("$_gt_path")
  done <<< "$git_tracked_paths"

  # Pass 2: classify survivors (dirty / unpushed / still-git-tracked) with
  # bounded parallelism. Each worktree's status+rev-list pair is independent
  # of every other's, so running up to 8 at a time cuts wall-clock without
  # changing what gets checked — this is the part of the D#2001 PR1
  # fix-cycle-1 finding (+9.4s/+79% vs origin/main) that realpath/mtime
  # batching alone could not remove. Each job writes its one-word verdict to
  # its own file rather than touching a shared variable, since a backgrounded
  # subshell's variables never propagate back to this function.
  if [[ "${#_gt_candidates[@]}" -gt 0 ]]; then
    local _gt_tmpdir
    _gt_tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/wtr-reap-step6.XXXXXX")"

    # D#2001 PR2 fix-cycle 1 (security review, blocking finding 1): this used
    # to discard git's exit code and stderr and read empty stdout as "clean"
    # -- so a `git status` failure (corrupt index, unreadable subdirectory,
    # anything) resolved toward *remove* instead of toward *skip*. Every
    # other guard in this file fails closed on an error (`_wtr_is_self` on an
    # unresolved root, `_wtr_worktree_has_open_pr` on a gh error, Pass 1 above
    # treating a failed stat as fresh) -- this one now matches them: any
    # non-zero exit code, any stderr at all, or a read failure routes to the
    # new "unknown" bucket, which is reported and never removed.
    _wtr_step6_classify() {
      local path="$1" outfile="$2"
      local status_out status_rc

      # D#2120: no mktemp. $outfile is already "${_gt_tmpdir}/${_gt_idx}" --
      # unique per candidate inside a tmpdir that is unique per pass -- so
      # deriving the stderr path from it can't collide with any other
      # candidate's file, concurrently or across passes. If the redirect
      # itself couldn't create the file (permissions, disk full, tmpdir
      # gone), the `-r` check below still routes to "unknown", the same
      # fail-closed contract the old mktemp-failure branch gave us.
      local status_err="${outfile}.status.err"
      status_out=$(git -C "$path" status --porcelain 2>"$status_err")
      status_rc=$?

      if [[ ! -r "$status_err" ]]; then
        echo "unknown" > "$outfile"
        return
      fi
      local status_stderr
      status_stderr="$(<"$status_err")"

      if [[ "$status_rc" -ne 0 || -n "$status_stderr" ]]; then
        echo "unknown" > "$outfile"
        return
      fi

      # [MADRCTU]: the Spec's condition 4 is "no tracked changes", not
      # specifically the [MADRC] porcelain codes -- those miss a tracked
      # path replaced by a symlink (reports "T") and a merge conflict
      # ("UU"/"AA"/etc, second column U). Widening only ever excludes more
      # candidates, never removes a guard.
      local status
      status=$(echo "$status_out" | awk '{ code=substr($0,1,2); if (code ~ /[MADRCTU]/) print }')
      if [[ -n "$status" ]]; then
        echo "dirty" > "$outfile"
        return
      fi

      local unpushed_out unpushed_rc
      local unpushed_err="${outfile}.unpushed.err"
      unpushed_out=$(git -C "$path" rev-list HEAD --not --remotes 2>"$unpushed_err")
      unpushed_rc=$?

      if [[ ! -r "$unpushed_err" ]]; then
        echo "unknown" > "$outfile"
        return
      fi
      local unpushed_stderr
      unpushed_stderr="$(<"$unpushed_err")"

      if [[ "$unpushed_rc" -ne 0 || -n "$unpushed_stderr" ]]; then
        echo "unknown" > "$outfile"
        return
      fi

      if [[ -n "$unpushed_out" ]]; then
        echo "unpushed" > "$outfile"
      else
        echo "git-tracked" > "$outfile"
      fi
    }

    local _gt_idx=0 _gt_inflight=0
    local _gt_cand
    for _gt_cand in "${_gt_candidates[@]}"; do
      _wtr_step6_classify "$_gt_cand" "${_gt_tmpdir}/${_gt_idx}" &
      _gt_idx=$((_gt_idx + 1))
      _gt_inflight=$((_gt_inflight + 1))
      if [[ "$_gt_inflight" -ge 8 ]]; then
        wait
        _gt_inflight=0
      fi
    done
    wait

    # Consume by index (not a glob over the tmpdir) so each verdict stays
    # paired with the path that produced it -- the "git-tracked" bucket
    # (D#2001 PR2) needs that path to look up its branch and, if eligible,
    # remove it or report it as a candidate.
    local _gt_i _gt_bucket _gt_path_i _gt_branch _gt_head_sha _gt_resolved_path
    for _gt_i in "${!_gt_candidates[@]}"; do
      _gt_path_i="${_gt_candidates[$_gt_i]}"
      _gt_bucket="$(cat "${_gt_tmpdir}/${_gt_i}" 2>/dev/null || true)"
      case "$_gt_bucket" in
        dirty)    skip_dirty=$((skip_dirty + 1)) ;;
        unpushed) skip_unpushed=$((skip_unpushed + 1)) ;;
        unknown)
          # D#2001 PR2 fix-cycle 1: classify() could not get a trustworthy
          # answer (git error, stderr, or the classify subshell itself
          # failed to write a result) -- reported and skipped, never
          # removed. An empty $_gt_bucket (missing result file) also lands
          # here via the case default below.
          skip_unknown=$((skip_unknown + 1))
          echo "  skipped-unknown (classification failed, git-tracked): $(basename "$_gt_path_i")" >&2
          ;;
        git-tracked)
          _gt_branch="${_gt_branch_map[$_gt_path_i]:-}"
          _gt_head_sha="${_gt_head_map[$_gt_path_i]:-}"

          if [[ "$enable_git_tracked_removal" == "false" ]]; then
            # Hot-path default (post-agent-hook.sh:533 invokes exactly this:
            # real mode, no opt-in). Zero additional git/gh calls beyond
            # what Pass 2 above already paid -- identical cost to PR1.
            # D#2149: gates on the opt-in alone now (dropped the
            # dry_run == false conjunct) -- a dry-run without the opt-in
            # classifies the same as a real run, so it can no longer
            # promise a removal the real run would never make.
            skip_git_tracked=$((skip_git_tracked + 1))
            if [[ "$dry_run" == "true" ]]; then
              # Informational only -- does not touch `reaped`. Keeps
              # candidate visibility alive under --dry-run without the
              # opt-in (D#2149).
              echo "  candidate-git-tracked (requires --enable-git-tracked-removal): $(basename "$_gt_path_i")" >&2
            fi
            continue
          fi

          # D#2001 PR2 fix-cycle 1 (security review, blocking finding 2):
          # the worktrees-dir path guard (predicate condition 3) was never
          # applied here -- only Step 5's rm -rf fallback had it. Reuses
          # the realpath Pass 1 already resolved for every candidate
          # (_gt_resolved_map) and $resolved_worktrees_dir computed once
          # above -- no new per-entry subprocess.
          _gt_resolved_path="${_gt_resolved_map[$_gt_path_i]:-$_gt_path_i}"
          if [[ "$_gt_resolved_path" != "${resolved_worktrees_dir}/"* || "$_gt_resolved_path" == "$resolved_worktrees_dir" ]]; then
            echo "  path-guard-refused (outside worktrees dir, git-tracked): $(basename "$_gt_path_i") ($_gt_resolved_path)" >&2
            skip_git_tracked=$((skip_git_tracked + 1))
            continue
          fi

          if _wtr_worktree_has_open_pr "$_gt_branch" "$_gt_head_sha"; then
            skip_open_pr=$((skip_open_pr + 1))
            echo "  skipped-open-pr: $(basename "$_gt_path_i") (branch=${_gt_branch:-<detached>})" >&2
            continue
          fi

          # D#2149: cap check hoisted above the dry-run "would-remove"
          # branch (was below it, in the real-only arm) so a
          # --dry-run --enable-git-tracked-removal preview respects the
          # same per-pass cap a real run enforces, instead of promising
          # unbounded removals. Applies to both arms --
          # $git_tracked_removed_this_pass counts previewed and actually
          # removed candidates alike, so a capped dry-run and a capped
          # real run stop at the same candidate.
          if [[ "$git_tracked_removed_this_pass" -ge "$gt_removal_cap" ]]; then
            echo "  skipped-cap-reached (git-tracked): $(basename "$_gt_path_i")" >&2
            skip_git_tracked=$((skip_git_tracked + 1))
            continue
          fi

          if [[ "$dry_run" == "true" ]]; then
            echo "  would-remove (git-tracked): $(basename "$_gt_path_i") (branch=${_gt_branch:-<detached>})"
            reaped=$((reaped + 1))
            git_tracked_removed_this_pass=$((git_tracked_removed_this_pass + 1))
            continue
          fi

          # Real removal -- only reachable here with --enable-git-tracked-removal.
          if git -C "$_WTR_REPO_ROOT" worktree remove --force "$_gt_path_i" 2>&1 | sed 's/^/  [remove-git-tracked] /'; then
            local _gt_audit_ts
            _gt_audit_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ)"
            mkdir -p "$_WTR_AUDIT_DIR" 2>/dev/null || true
            # D#2001 PR2 fix-cycle 1: hand-built JSON with unescaped
            # interpolation broke on a branch/path containing a quote.
            # json.dumps escapes whatever is actually in the values.
            python3 -c "
import json, sys
row = {
    'kind': 'worktree_reap_git_tracked_removed',
    'path': sys.argv[1],
    'branch': sys.argv[2],
    'reason': 'clean+pushed+ttl-expired+no-open-pr',
    'timestamp': sys.argv[3],
}
print(json.dumps(row))
" "$_gt_path_i" "$_gt_branch" "$_gt_audit_ts" >> "$_WTR_AUDIT_FILE"
            reaped=$((reaped + 1))
            git_tracked_removed_this_pass=$((git_tracked_removed_this_pass + 1))
            echo "  removed (git-tracked): $(basename "$_gt_path_i")"
          else
            echo "  WARN: git worktree remove --force failed for $(basename "$_gt_path_i")" >&2
            skip_git_tracked=$((skip_git_tracked + 1))
          fi
          ;;
        *)
          # Empty or unrecognized bucket (e.g. the classify subshell for
          # this candidate never wrote a result file) -- same fail-closed
          # treatment as an explicit "unknown" verdict.
          skip_unknown=$((skip_unknown + 1))
          echo "  skipped-unknown (no classification result, git-tracked): $(basename "$_gt_path_i")" >&2
          ;;
      esac
    done
    rm -rf "$_gt_tmpdir"
  fi

  # D#2001 PR2: skip_total no longer always equals enumerated -- a
  # git-tracked worktree that cleared every safety condition is reaped (or,
  # in --dry-run, would-be-reaped) instead of landing in a skip bucket, so
  # the sum is `enumerated` minus however many of those this pass counted
  # into `reaped` (AC-2's "sum to enumerated minus removals").
  local skip_total=$(( skip_self + skip_young + skip_dirty + skip_unpushed + skip_git_tracked + skip_open_pr + skip_unknown ))
  # Diagnostic output (what the reaper SAW), not the action log (what it
  # DID) — goes to stderr alongside the registry-empty warning above, not
  # stdout with the per-entry action lines. reap-worktrees.sh:110 merges
  # both streams with `2>&1` before filtering, so this changes nothing for
  # the one caller today; it keeps the two report lines and the warning on
  # the same stream for whatever the next caller turns out to be.
  echo "enumerated=${enumerated} (git worktree list --porcelain minus main checkout)" >&2
  echo "skip-breakdown: skipped-git-tracked=${skip_git_tracked} skipped-young=${skip_young} skipped-dirty=${skip_dirty} skipped-unpushed=${skip_unpushed} skipped-self=${skip_self} skipped-open-pr=${skip_open_pr} skipped-unknown=${skip_unknown} (sum=${skip_total})" >&2

  # Write final registry
  if [[ "$dry_run" == "false" ]]; then
    _wtr_write_registry "$current"
  fi
  _wtr_unlock

  # Compute active count from final registry
  local active_count
  active_count=$(echo "$current" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(sum(1 for e in data if e.get('status')=='active'))
" 2>/dev/null || echo "0")

  local reaped_summary="${reaped} reaped"
  if [[ "${_WTR_SELF_ROOT:-}" == "__UNRESOLVED__" ]]; then
    reaped_summary="0 reaped (self-exclusion unresolved)"
  fi

  echo "worktrees: ${active_count} active, ${reaped_summary}, ${patches_archived} patch archived, ${merged_cleanup} merged-cleanup"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

worktree_registry() {
  local cmd="${1:-}"
  shift || true

  case "$cmd" in
    register)        _cmd_register "$@" ;;
    heartbeat)       _cmd_heartbeat "$@" ;;
    mark-status)     _cmd_mark_status "$@" ;;
    set-pr)          _cmd_set_pr "$@" ;;
    reconcile-path)  _cmd_reconcile_path "$@" ;;
    list)            _cmd_list "$@" ;;
    count-active)    _cmd_count_active "$@" ;;
    count-disk)      _cmd_count_disk "$@" ;;
    reap)            _cmd_reap "$@" ;;
    *)
      echo "worktree-registry: unknown command '$cmd'" >&2
      echo "Usage: worktree-registry <register|heartbeat|mark-status|set-pr|reconcile-path|list|count-active|count-disk|reap> [args...]" >&2
      return 1
      ;;
  esac
}

# Allow direct invocation: bash scripts/lib/worktree-registry.sh <cmd> [args...]
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  worktree_registry "$@"
fi
