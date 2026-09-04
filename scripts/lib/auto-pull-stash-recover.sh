#!/usr/bin/env bash
# scripts/lib/auto-pull-stash-recover.sh — safe recovery from an auto-pull
# modified-file collision (D#2301).
#
# Background: the modified-file branch of scripts/lib/auto-pull-step.sh used
# to do nothing but print "auto-pull SKIPPED — working tree has modifications"
# and give up. That message reaches the team log (via auto_pull_step_teamlog)
# every time, indistinguishable in severity from a routine success, so it
# accretes: nine merges across three different tracked files hit this branch
# and silently left an operator checkout behind main. One of those merges
# (PR #2297) left cost pricing unpriced on the machine that mattered while
# `main`, the PR, and the review were all correct.
#
# The untracked-collision branch next to this one already solves the same
# shape of problem for a different git error — gated, bounded, non-destructive
# self-remediation, deriving its file set from plumbing rather than git's
# unparseable English stderr. This file gives the modified-file branch the
# same discipline, plus a clean "no" when remediation is not safe:
#
#   * the colliding set is the intersection of two plumbing outputs (locally
#     modified tracked files, and paths the incoming commits touch) — no
#     filename is ever named in this file, so every tracked file this problem
#     was discovered on is covered the same way, without a line of code that
#     has to know any of their names (D#2301 AC-4 / AC-10);
#   * the local edit is moved aside with a *path-limited* `git stash push`,
#     never a whole-tree stash, so unrelated dirty files are left alone;
#   * before ever reapplying that stash, `git merge-tree --write-tree` proves
#     — without touching the working tree, the index, or anything but one
#     throwaway tree object — whether the reapply would conflict. Only a
#     merge `git merge-tree` itself certifies clean gets a real
#     `git stash pop`. A conflicted `git stash pop` leaves "UU" entries the
#     operator did not have before the call, and this code has no cleanup
#     move that isn't `git checkout --` or `git reset --hard` — both
#     forbidden (Archive Protocol / D#2301 constraints) — so the fix is to
#     never attempt the pop that would need cleaning up;
#   * on that path the stash is never dropped and never reset away — it is
#     the operator's original content, named in AUTO_PULL_STASH_REF, and
#     recoverable with `git stash show -p <ref>` or `git stash pop` by hand.
#
# Public contract:
#
#   auto_pull_recover_modified <repo_root>
#     0 = collision fully remediated: the colliding paths were stashed, the
#         pull succeeded, and the stash was reapplied cleanly. The caller
#         should treat this exactly like an ordinary successful pull.
#     1 = declined before touching anything — over the AUTO_PULL_STASH_MAX
#         bound, a merge/rebase/cherry-pick already in progress, or staged-
#         but-uncommitted index entries present. Nothing was stashed, nothing
#         was pulled, and the tree is byte-identical to before the call.
#     2 = the pull already happened (HEAD advanced) but the stash could not
#         be reapplied without risking a conflict, or the stash itself could
#         not be pushed/popped cleanly. The stash is preserved — never
#         dropped, never reset away — and named in AUTO_PULL_STASH_REF.
#     Sets on return:
#       AUTO_PULL_STASH_SUMMARY   one-line human summary for the team log
#       AUTO_PULL_STASH_REF       stash ref (e.g. stash@{0}) when local
#                                 content remains stashed; empty otherwise
#
# Dependency-free on purpose, same as auto-pull-recover.sh: takes repo_root as
# an argument rather than reading an ambient global, so tests can source it
# standalone and drive it against a fixture. Bash 4+, git, coreutils.

# Blast-radius bound, same idea as AUTO_PULL_RECOVER_MAX next door: a derived
# set larger than this is a sign the derivation is wrong, not a sign there is
# a lot of work to do. Overridable for tests.
AUTO_PULL_STASH_MAX="${AUTO_PULL_STASH_MAX:-20}"

_apsr_log() { printf '[auto-pull-stash-recover] %s\n' "$1" >&2; }

auto_pull_recover_modified() {
  local repo_root="${1:-}"
  AUTO_PULL_STASH_SUMMARY=""
  # shellcheck disable=SC2034  # public return value — read by the caller
  # after this function returns (auto-pull-step.sh: ${AUTO_PULL_STASH_REF:-}),
  # per the contract documented above. Never read within this file itself.
  AUTO_PULL_STASH_REF=""

  if [[ -z "$repo_root" ]] || ! git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
    AUTO_PULL_STASH_SUMMARY="declined: '${repo_root}' is not a git repository"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  local git_dir
  git_dir="$(git -C "$repo_root" rev-parse --git-dir 2>/dev/null || echo "")"
  if [[ -z "$git_dir" ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: could not resolve the .git directory"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi
  case "$git_dir" in
    /*) : ;;
    *) git_dir="${repo_root}/${git_dir}" ;;
  esac

  # Pre-flight gate (a) — mid-merge/rebase/cherry-pick. Stashing on top of an
  # operation the operator is already mid-way through is exactly the kind of
  # partial action this function exists to refuse.
  if [[ -f "${git_dir}/MERGE_HEAD" || -d "${git_dir}/rebase-merge" \
        || -f "${git_dir}/rebase-apply" || -d "${git_dir}/rebase-apply" \
        || -f "${git_dir}/CHERRY_PICK_HEAD" ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: repo is mid-merge/rebase/cherry-pick — stashing now would land on top of an operation already in progress"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  # Pre-flight gate (b) — staged-but-uncommitted index entries. A path-limited
  # stash over the colliding files is safe; a repo with unrelated staged work
  # in progress is not a state this code should be touching at all.
  if [[ -n "$(git -C "$repo_root" diff --cached --name-only 2>/dev/null)" ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: index has staged-but-uncommitted entries — stashing here could sweep up unrelated staged work"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  # FETCH_HEAD has to be current — it is the incoming half of the derivation.
  if ! git -C "$repo_root" fetch origin main >/dev/null 2>&1; then
    AUTO_PULL_STASH_SUMMARY="declined: 'git fetch origin main' failed, so FETCH_HEAD cannot be trusted"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  # The colliding set (AC-10): tracked files with an unstaged local
  # modification, intersected with paths the incoming commits touch. Both
  # halves come from plumbing, NUL-delimited — nothing here comes from a
  # name's text or from git's prose.
  local -a local_mod=()
  local p
  while IFS= read -r -d '' p; do
    [[ -n "$p" ]] && local_mod+=("$p")
  done < <(git -C "$repo_root" diff --name-only -z 2>/dev/null)

  if [[ ${#local_mod[@]} -eq 0 ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: no tracked local modification found to stash"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  local -A incoming=()
  local ip
  while IFS= read -r -d '' ip; do
    [[ -n "$ip" ]] && incoming["$ip"]=1
  done < <(git -C "$repo_root" diff --name-only -z HEAD..FETCH_HEAD 2>/dev/null)

  local -a candidates=()
  for p in "${local_mod[@]}"; do
    [[ -n "${incoming[$p]+set}" ]] && candidates+=("$p")
  done

  local n=${#candidates[@]}
  if [[ $n -eq 0 ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: no locally modified file collides with an incoming change"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi
  if [[ $n -gt $AUTO_PULL_STASH_MAX ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: colliding set has ${n} entries, over the bound of ${AUTO_PULL_STASH_MAX} — refusing to act partially"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  local stamp stash_msg
  stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  stash_msg="auto-pull-stash-recover ${stamp}"

  local base_commit
  base_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || echo "")"
  if [[ -z "$base_commit" ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: could not resolve HEAD before stashing"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  local stash_out stash_rc
  stash_out=$(git -C "$repo_root" stash push -m "$stash_msg" -- "${candidates[@]}" 2>&1) && stash_rc=0 || stash_rc=$?
  if [[ $stash_rc -ne 0 ]] || ! printf '%s' "$stash_out" | grep -q "^Saved working directory"; then
    AUTO_PULL_STASH_SUMMARY="declined: 'git stash push' over the colliding path(s) did not report success (${stash_out})"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  local stash_ref
  stash_ref="$(git -C "$repo_root" stash list --format='%gd' 2>/dev/null | head -1)"
  if [[ -z "$stash_ref" ]]; then
    # Should be unreachable given the "Saved working directory" check above,
    # but if it happens, do not guess — report and leave things alone.
    AUTO_PULL_STASH_SUMMARY="declined: stash push reported success but no stash entry can be found"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 1
  fi

  local pull_out pull_rc
  pull_out=$(LC_ALL=C git -C "$repo_root" pull --ff-only origin main 2>&1) && pull_rc=0 || pull_rc=$?
  if [[ $pull_rc -ne 0 ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: pull still failed after stashing the colliding path(s) (${pull_out}) — local edits are safe in ${stash_ref}"
    # shellcheck disable=SC2034  # public return value, see the contract above
    AUTO_PULL_STASH_REF="$stash_ref"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 2
  fi

  # Would reapplying the stash conflict? `git merge-tree --write-tree` answers
  # that by doing the real three-way merge (base=the pre-stash HEAD,
  # ours=the post-pull HEAD, theirs=the stash commit) entirely against the
  # object database — nothing on disk changes, win or lose.
  local mt_rc
  git -C "$repo_root" merge-tree --write-tree --merge-base="$base_commit" HEAD "$stash_ref" >/dev/null 2>&1 && mt_rc=0 || mt_rc=$?
  if [[ $mt_rc -ne 0 ]]; then
    AUTO_PULL_STASH_SUMMARY="declined: local edit to ${n} colliding file(s) overlaps the incoming change and cannot be reapplied without a conflict — the pull completed, local edits are preserved in ${stash_ref}"
    # shellcheck disable=SC2034  # public return value, see the contract above
    AUTO_PULL_STASH_REF="$stash_ref"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 2
  fi

  local pop_out pop_rc
  pop_out=$(git -C "$repo_root" stash pop 2>&1) && pop_rc=0 || pop_rc=$?
  if [[ $pop_rc -ne 0 ]]; then
    # merge-tree said this would be clean; do not guess at cleanup if reality
    # disagrees (a race with something else touching the tree, for example).
    # Leave the stash exactly where it is.
    stash_ref="$(git -C "$repo_root" stash list --format='%gd' 2>/dev/null | head -1)"
    AUTO_PULL_STASH_SUMMARY="declined: pull completed but 'git stash pop' failed unexpectedly after its pre-check passed (${pop_out}) — local edits are preserved in ${stash_ref}"
    # shellcheck disable=SC2034  # public return value, see the contract above
    AUTO_PULL_STASH_REF="$stash_ref"
    _apsr_log "$AUTO_PULL_STASH_SUMMARY"
    return 2
  fi

  AUTO_PULL_STASH_SUMMARY="stashed and restored ${n} colliding file(s) (${stash_msg}), then pulled cleanly"
  _apsr_log "$AUTO_PULL_STASH_SUMMARY"
  return 0
}
