#!/usr/bin/env bash
# scripts/lib/verify-tree.sh — build a verification tree, and notice when it
# changes underneath you.
#
# Verification results have been wrong five distinct ways in one session, twice
# returning a clean pass from a tree that had silently reverted to base content.
# Every catch came from an agent deciding to checksum by hand — a habit, not a
# mechanism. This is the mechanism. Root cause is not handled here (D#1759).
#
#   source scripts/lib/verify-tree.sh
#   verify_tree_build "$PR_HEAD_SHA" /outside/the/checkout/tree   # clone+protect+snapshot
#   ( cd /outside/the/checkout/tree && python3 -m pytest tests/ -q ) 2>&1 | tee run.log
#   verify_tree_assert /outside/the/checkout/tree "$PR_HEAD_SHA"  || echo DISCARD
#   verify_tree_assert_log run.log                                || echo DISCARD
#
# Clone the sha you want to verify — the PR head is in the parent's object store
# after a fetch — so there is no diff to apply and nothing to lose. Keep <dest>
# OUTSIDE the checkout: a tree parked inside gets swept up by whole-tree tests
# (test_state_paths.py flags the clone's own backend/ copies, which reads as a
# regression and is not one).
#
# Why `git clone --shared --revision=<sha>`
# -----------------------------------------
# A hygiene standard, not a sandbox workaround: reviewers run at team_lead tier
# where hooks/sandbox.py:385 short-circuits before classify_bash is consulted, so
# nobody was ever blocked from another spelling. We adopt it because it avoids a
# shared checkout and carries real history — archive-plus-init yields a synthetic
# root commit, which inflates baselines and already produced one phantom result.
# Do not substitute the two-step clone-then-checkout form: its second half is
# blocked at hooks/sandbox_rules.py:2590 before any cwd analysis, at every tier.
# The `--quiet` below does not change that verdict (re-checked against
# classify_bash at worktree tier, git 2.55.0). `--shared` ties the clone to the
# parent object store and the parent's `gc.auto` is on; if that proves flaky,
# `--reference <parent> --dissociate --revision=<sha>` is also allowed, at the
# cost of copying objects.
#
# What is hashed, and what is not
# -------------------------------
# The manifest derives from `git ls-tree -r <sha>`, never a filesystem sweep.
# Per D#1967 `state.db`, `stats.duckdb`, `audit.jsonl` and `blackboard/` land in
# the repo root even with AUTONOMOUS_TEAM_STATE_DIR set, so a sweep is noisy by
# construction and gets switched off within a day. All four are untracked in
# HEAD, hence absent from manifest and fresh clone alike — that is load-bearing.
#
# Protection and assertion cover THE SAME SET: tracked regular files minus the
# carve-outs below, files only. Directories stay writable so a suite can deposit
# untracked droppings. A blanket `chmod -R a-w` fires on legitimate runs, and a
# check that fires on legitimate runs gets deleted — worse than no check at all.
# Known hole rather than assumed cover: symlink (120000) and submodule (160000)
# entries sit outside both, so a swapped symlink goes unnoticed — sha256sum
# follows a link and would describe the target. Neither mode exists at HEAD.
#
# Manifests live outside every tree under STATE_DIR/tree-manifests/. STATE_DIR
# comes from backend/state_paths.py and honours AUTONOMOUS_TEAM_STATE_DIR, so
# build and assert must agree on it; if they do not, assert exits 3 naming the
# path it looked in rather than silently passing.
#
# Exit codes (assert): 0 clean · 1 content changed · 2 live process rooted in the
# tree · 3 usage error or missing manifest. Call assert from OUTSIDE the tree; it
# is correct from inside, but outside is the documented flow.

# Carve-outs — tracked files that legitimately mutate at runtime, excluded from
# BOTH protection and assertion. Each name is a hole in the detector, so the list
# stays short and every entry is measured to be rewritten by a normal suite run.
# VERIFY_TREE_CARVEOUTS (whitespace-separated) adds to it; nothing removes.
_VT_CARVEOUTS_DEFAULT=(
  ".autonomous-team/config.json"
  ".autonomous-team/agent-profiles.json"
)

_vt_log() { printf 'verify-tree: %s\n' "$*" >&2; }

_vt_repo_root() { (cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd); }
_vt_abs() { readlink -f "$1" 2>/dev/null || printf '%s\n' "$1"; }
_vt_ppid() { awk '/^PPid:/ { print $2 }' "/proc/$1/status" 2>/dev/null; }

# STATE_DIR/tree-manifests — outside every tree, by construction.
_vt_manifest_dir() {
  local sd
  sd="$(python3 "$(_vt_repo_root)/backend/state_paths.py" 2>/dev/null | head -1)"
  [ -n "$sd" ] || sd="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.fulcrumaxe-state}"
  printf '%s/tree-manifests\n' "$sd"
}

# Stable id for a (tree, sha) pair, so assert can locate the manifest unaided.
_vt_id() { printf '%s\0%s' "$(_vt_abs "$1")" "$2" | sha256sum | cut -c1-16; }

_vt_manifest_path() { printf '%s/%s.sha256\n' "$(_vt_manifest_dir)" "$(_vt_id "$1" "$2")"; }

# Print the manifest path for (dest, sha), or return 3 if there is not one.
_vt_resolve_manifest() {
  local dest="$1" sha="$2" full manifest
  full="$(git -C "$dest" rev-parse --verify "${sha}^{commit}" 2>/dev/null)" || full="$sha"
  manifest="$(_vt_manifest_path "$dest" "$full")"
  [ -f "$manifest" ] || {
    _vt_log "no manifest for ($dest, $full) at $manifest — build with verify_tree_build, and keep AUTONOMOUS_TEAM_STATE_DIR stable between build and assert"
    return 3
  }
  printf '%s\n' "$manifest"
}

# sha256sum lines are '<64 hex><2 spaces><path>'; paths needing escaping were
# rejected at build time, so column 67 onward is the path, verbatim.
_vt_paths_z() { cut -c67- "$1" | tr '\n' '\0'; }

# NUL-separated list of tracked regular files at <sha>, carve-outs removed.
_vt_pathlist() {
  local parent="$1" sha="$2"
  local -A carve=()
  local c
  for c in "${_VT_CARVEOUTS_DEFAULT[@]}" ${VERIFY_TREE_CARVEOUTS:-}; do carve["$c"]=1; done

  local entry mode path
  local nl=$'\n' tab=$'\t'
  while IFS= read -r -d '' entry; do
    mode="${entry%% *}"
    path="${entry#* }"
    # Regular blobs only — see the symlink/submodule note in the header.
    case "$mode" in 100644 | 100755) ;; *) continue ;; esac
    [ -n "${carve[$path]:-}" ] && continue || :
    case "$path" in *\\* | *"$nl"* | *"$tab"*)
      _vt_log "REFUSING: tracked path needs escaping, manifest would be ambiguous: $path"
      return 1
      ;;
    esac
    printf '%s\0' "$path"
  done < <(git -C "$parent" ls-tree -r -z --format='%(objectmode) %(path)' "$sha")
}

# verify_tree_build <sha> <dest> [parent_repo] — clone, protect, snapshot.
verify_tree_build() {
  local sha="${1:-}" dest="${2:-}" parent="${3:-${VERIFY_TREE_PARENT:-$(_vt_repo_root)}}"
  [ -n "$sha" ] && [ -n "$dest" ] || { _vt_log "usage: verify_tree_build <sha> <dest> [parent_repo]"; return 3; }
  [ -e "$dest" ] && { _vt_log "refusing to build over an existing path: $dest"; return 3; } || :

  local full_sha
  full_sha="$(git -C "$parent" rev-parse --verify "${sha}^{commit}" 2>/dev/null)" || {
    _vt_log "no such commit in $parent: $sha"
    return 3
  }

  git clone --quiet --shared --revision="$full_sha" "$parent" "$dest" || {
    _vt_log "clone failed: git clone --shared --revision=$full_sha $parent $dest"
    return 3
  }

  # Every failure below removes $dest again: we created it, and build refuses to
  # write over an existing path, so leaving it would block the caller's retry.
  local got
  got="$(git -C "$dest" rev-parse HEAD)"
  if [ "$got" != "$full_sha" ]; then
    _vt_log "clone landed on $got, expected $full_sha — removed $dest"
    rm -rf "$dest"
    return 3
  fi

  local tmp
  tmp="$(mktemp -d)" || { rm -rf "$dest"; return 3; }
  _vt_pathlist "$parent" "$full_sha" > "$tmp/paths.z" || { rm -rf "$tmp" "$dest"; return 3; }

  # Protect then hash, so the manifest describes an already read-only tree.
  (cd "$dest" && xargs -0 -r chmod a-w) < "$tmp/paths.z" \
    || _vt_log "WARNING — chmod a-w did not cover every tracked file in $dest; the manifest is still authoritative, but the tree is not fully write-protected"

  local mdir manifest
  mdir="$(_vt_manifest_dir)"
  mkdir -p "$mdir" || { rm -rf "$tmp" "$dest"; return 3; }
  manifest="$(_vt_manifest_path "$dest" "$full_sha")"
  (cd "$dest" && xargs -0 -r sha256sum) < "$tmp/paths.z" > "$tmp/manifest" || {
    rm -rf "$tmp" "$dest"
    return 3
  }
  mv "$tmp/manifest" "$manifest" || {
    _vt_log "could not place the manifest at $manifest — removed $dest"
    rm -rf "$tmp" "$dest"
    return 3
  }

  local n
  n="$(wc -l < "$manifest" | tr -d ' ')"
  rm -rf "$tmp"
  _vt_log "built $dest at $full_sha — $n tracked files protected and hashed"
  _vt_log "manifest: $manifest"
  return 0
}

# verify_tree_assert <dest> <sha> — is this still the tree you built?
verify_tree_assert() {
  local dest="${1:-}" sha="${2:-}" manifest
  [ -n "$dest" ] && [ -n "$sha" ] || { _vt_log "usage: verify_tree_assert <dest> <sha>"; return 3; }
  manifest="$(_vt_resolve_manifest "$dest" "$sha")" || return 3

  local started tmp rc=0
  started=$(date +%s%N)
  tmp="$(mktemp -d)" || return 3
  _vt_paths_z "$manifest" > "$tmp/paths.z"
  (cd "$dest" && xargs -0 -r sha256sum) < "$tmp/paths.z" > "$tmp/now" 2>/dev/null

  awk '{ print substr($0, 67) "\t" substr($0, 1, 64) }' "$manifest" > "$tmp/was.tsv"
  awk '{ print substr($0, 67) "\t" substr($0, 1, 64) }' "$tmp/now" > "$tmp/now.tsv"
  awk -F'\t' '
    NR == FNR { was[$1] = $2; next }
    { if ($1 in was) { if (was[$1] != $2) print "CHANGED  " $1; delete was[$1] } }
    END { for (p in was) print "MISSING  " p }
  ' "$tmp/was.tsv" "$tmp/now.tsv" | LC_ALL=C sort > "$tmp/drift"

  local total drift
  total="$(wc -l < "$manifest" | tr -d ' ')"
  drift="$(wc -l < "$tmp/drift" | tr -d ' ')"
  if [ "$drift" -gt 0 ]; then
    _vt_log "FAIL — $drift of $total tracked files changed since build, in $dest"
    head -25 "$tmp/drift" | while IFS= read -r line; do printf '  %s\n' "$line" >&2; done
    [ "$drift" -gt 25 ] && _vt_log "... and $((drift - 25)) more" || :
    _vt_log "any result measured in this tree is void — rebuild at a fresh path and re-run"
    rc=1
  fi

  # cd / BEFORE the fork: the command-substitution process itself would
  # otherwise hold the caller's cwd for the whole scan and match its own tree.
  local pids
  pids="$(cd / && _vt_live_pids "$dest")"
  if [ -n "$pids" ]; then
    _vt_log "FAIL — process(es) still rooted in $dest: $pids"
    _vt_log "a run that overlaps another writer cannot be attributed to this tree"
    [ "$rc" -eq 0 ] && rc=2 || :
  fi

  rm -rf "$tmp"
  local ms=$((($(date +%s%N) - started) / 1000000))
  if [ "$rc" -eq 0 ]; then
    _vt_log "OK — $total tracked files unchanged, no live process rooted in $dest (${ms}ms)"
  else
    _vt_log "checked $total tracked files in ${ms}ms"
  fi
  return "$rc"
}

# PIDs whose cwd sits under <dest>, minus two exclusions whose difference is
# load-bearing. By DESCENT from $$: our own forks — assert's command-substitution
# process and any pipeline sibling of the caller inherit the CALLER's cwd, so a
# reviewer running assert from inside the tree is momentarily rooted in it too,
# and reporting those returned rc 2 on a clean tree. By IDENTITY with $$ or an
# ancestor: the caller's own shell chain; a terminal sitting in the tree is not
# another writer. Ancestors are never excluded by descent — measured here, every
# agent on the host shares one common harness process as an ancestor, so
# excluding all of its descendants would also exclude a sibling agent's suite,
# which is most of what this exists to catch; a sibling's chain reaches that
# shared parent but never $$. Residual gap, stated not papered over: a job you
# backgrounded from this same shell is not flagged — overlaps are caught by
# verify_tree_assert_log, and content changes by the hash.
_vt_live_pids() (
  local dest
  dest="$(_vt_abs "$1")"
  [ -d /proc ] || return 0
  cd / || return 0

  # PPid comes from status, never from stat field 4: a comm containing a space
  # or a ')' shifts every positional field in stat, so `awk '{print $4}'` on a
  # process named e.g. `my proc` yields the state letter instead of the ppid.
  # The walk then reads a nonexistent path, gets nothing, and skips the
  # candidate as ours — a silent miss in the detector.
  local self="$$"
  local -A mine=()
  local p="$self"
  while [ -n "$p" ] && [ "$p" != "0" ] && [ "$p" != "1" ]; do
    mine["$p"]=1
    p="$(_vt_ppid "$p")"
  done
  mine["${BASHPID:-$self}"]=1

  local out="" d pid cwd anc hops
  for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    cwd="$(readlink "$d/cwd" 2>/dev/null)" || continue
    # Literal prefix test, not a case glob: $dest may contain * ? or [.
    [ "$cwd" = "$dest" ] || [ "${cwd#"$dest"/}" != "$cwd" ] || continue
    [ -n "${mine[$pid]:-}" ] && continue || :
    anc="$pid"
    hops=0
    while [ -n "$anc" ] && [ "$anc" != "0" ] && [ "$anc" != "1" ] && [ "$hops" -lt 64 ]; do
      [ "$anc" = "$self" ] && { anc=""; break; }
      anc="$(_vt_ppid "$anc")"
      hops=$((hops + 1))
    done
    # Empty $anc means one of three things: the chain reached $$ (ours), a
    # process in the chain exited mid-walk, or PPid could not be read. All three
    # skip, deliberately — a chain we cannot follow is a chain we cannot call
    # foreign, and over-blocking is the ranked-worse outcome here.
    [ -z "$anc" ] && continue || :
    out="$out $pid"
  done
  printf '%s' "${out# }"
)

# verify_tree_assert_log <logfile> — mechanism 3: two pytest runs sharing a
# directory or a state dir interleave and land two summary lines in one log, and
# the second reads as a real result. Exactly one passes; zero or two do not.
verify_tree_assert_log() {
  local log="${1:-}"
  [ -n "$log" ] && [ -f "$log" ] || { _vt_log "usage: verify_tree_assert_log <logfile>"; return 3; }
  # Matches the decorated form, the -q form, and 'no tests ran in'.
  local re='^=*[[:space:]]*(([0-9]+ [a-z]+,? )+|no tests ran )in [0-9]+(\.[0-9]+)?s'
  local n
  n="$(grep -cE "$re" "$log" || true)"
  [ "$n" -eq 1 ] && return 0 || :
  if [ "$n" -eq 0 ]; then
    _vt_log "FAIL — no pytest summary line in $log; the run did not reach the end"
    return 1
  fi
  _vt_log "FAIL — $n pytest summary lines in $log; two runs overlapped, so neither result is attributable"
  grep -nE "$re" "$log" | head -10 | while IFS= read -r line; do printf '  %s\n' "$line" >&2; done
  return 1
}

# verify_tree_unprotect <dest> <sha> — restore u+w, for teardown or for editing.
verify_tree_unprotect() {
  local dest="${1:-}" sha="${2:-}" manifest tmp
  [ -n "$dest" ] && [ -n "$sha" ] || { _vt_log "usage: verify_tree_unprotect <dest> <sha>"; return 3; }
  manifest="$(_vt_resolve_manifest "$dest" "$sha")" || return 3
  tmp="$(mktemp -d)" || return 3
  _vt_paths_z "$manifest" > "$tmp/paths.z"
  (cd "$dest" && xargs -0 -r chmod u+w) < "$tmp/paths.z"
  rm -rf "$tmp"
  return 0
}
