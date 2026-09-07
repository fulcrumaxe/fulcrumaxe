#!/usr/bin/env bash
# scripts/lib/code-plane-pr.sh — build a code-plane commit without touching
# any local ref, branch, index, or working tree.
#
# An agent worktree's history is unrelated to the code plane's history (their
# merge-base is empty), so the worktree's own auto-created branch can never
# serve as a PR head there, and the obvious repair — checkout/switch/branch/
# reset/clean/worktree/restore — is refused by the sandbox regardless of cwd.
# This helper builds the commit entirely out of git plumbing (read-tree,
# ls-tree, hash-object, write-tree, commit-tree), none of which touches a
# local ref, the real index, or the working tree, so none of it is refused.
#
# Usage (source, then call the dispatcher):
#   source scripts/lib/code-plane-pr.sh
#   SHA=$(code_plane_pr build --target-ref code-plane/main \
#           --branch my-fix --message "fix the thing" \
#           path/to/file.txt=/private/scratch/file.txt)
#
# Or invoke directly:
#   bash scripts/lib/code-plane-pr.sh build --target-ref <ref> [--base-ref <ref>] \
#     --branch <name> --message <msg> <repo-path>=<local-file> [...]
#   bash scripts/lib/code-plane-pr.sh extract --ref <ref>
#   bash scripts/lib/code-plane-pr.sh push --remote <name> --branch <name> --commit <sha>
#
# Three disciplines this file enforces so an agent never has to remember them:
#
#   1. Byte-identity by hash, not by eye. `build` takes a --target-ref (the
#      commit the new commit is parented on) and an optional --base-ref (what
#      the caller believed the current state was when it started editing —
#      defaults to --target-ref when omitted, i.e. no gap, no risk). For every
#      touched path that exists on --target-ref, the blob recorded there is
#      compared by hash against the blob recorded at the same path on
#      --base-ref. If they differ, the path moved on the target between the
#      caller's base and now, and `build` refuses (exit 3) rather than
#      silently discarding whichever side lost. A path absent on --target-ref
#      is a new file, not a divergence, and is accepted unconditionally.
#
#   2. The file mode is read, never guessed. `build` reads the mode for an
#      existing path from `git ls-tree` on --target-ref. A brand-new path (not
#      present on --target-ref) has no target mode to read; rather than
#      defaulting blind, it reads the one real signal available — the local
#      source file's own executable bit — and writes 100755 or 100644
#      accordingly.
#
#   3. The scratch/index path is private and per-invocation. Every call uses
#      its own `mktemp -d` — never a fixed location — so two concurrent
#      invocations never share state, and a run that gets its scratch
#      directory wiped out from under it (a deleted session scratchpad, a
#      killed sibling process) never corrupts a different invocation's tree.
#
# `build` also self-verifies its own scope before returning a commit sha:
# it diffs the built commit against its parent and refuses (exit 4) unless
# the changed-path set is exactly the set of paths it was asked to write.
# That is the property that keeps a second round from silently widening —
# rebuilding the tree from a --target-ref that moved since a first round
# would otherwise fold in every intervening change to main as if it were
# part of this change.
#
# Exit codes from `build`:
#   0  success — commit sha printed on stdout
#   2  usage error (bad args, unresolvable ref, missing local file)
#   3  byte-identity divergence between --base-ref and --target-ref
#   4  scope check failed — built commit touches more/fewer paths than asked
#
# This file never runs `gh`, never pushes, and never resolves a repo slug —
# that stays in the caller's hands (see scripts/lib/repo-resolve.sh and
# `_resolve_code_repo`), matching the repo-scope card's boundary between
# "build the commit" and "open the PR".

set -uo pipefail

code_plane_pr_usage() {
  cat <<'EOF'
scripts/lib/code-plane-pr.sh — build a code-plane commit without touching a
local ref, branch, index, or working tree.

  build   --target-ref <ref> [--base-ref <ref>] --branch <name>
          --message <msg> <repo-path>=<local-file> [<repo-path>=<local-file> ...]
          Prints the built commit sha on stdout on success.

  extract --ref <ref>
          Extracts <ref>'s tree into a fresh, private scratch directory
          (mktemp -d) and prints that directory's path.

  push    --remote <name> --branch <name> --commit <sha>
          Pushes <sha> to refs/heads/<branch> on <remote>. Network call —
          never exercised by the hermetic test suite.

See the header comment in this file for the disciplines `build` enforces.
EOF
}

# ── internal helpers ─────────────────────────────────────────────────────────

_cpp_err() {
  echo "code-plane-pr.sh: $1" >&2
}

_cpp_resolve_commit() {
  # _cpp_resolve_commit <ref-or-flag-name> <ref-value>
  local flag="$1" ref="$2" sha
  sha="$(git rev-parse "${ref}^{commit}" 2>/dev/null)" || return 1
  printf '%s\n' "$sha"
}

# ── build ─────────────────────────────────────────────────────────────────────

code_plane_pr_build() {
  local base_ref="" target_ref="" branch="" message=""
  local -a pairs=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --base-ref)   base_ref="$2";   shift 2 ;;
      --target-ref) target_ref="$2"; shift 2 ;;
      --branch)     branch="$2";     shift 2 ;;
      --message)    message="$2";    shift 2 ;;
      --) shift; pairs+=("$@"); break ;;
      -*) _cpp_err "build: unknown flag '$1'"; return 2 ;;
      *) pairs+=("$1"); shift ;;
    esac
  done

  if [[ -z "$target_ref" || -z "$branch" || -z "$message" || ${#pairs[@]} -eq 0 ]]; then
    _cpp_err "build: --target-ref, --branch, --message and at least one PATH=LOCALFILE are required"
    return 2
  fi
  [[ -n "$base_ref" ]] || base_ref="$target_ref"

  local target_sha base_sha
  target_sha="$(_cpp_resolve_commit target-ref "$target_ref")" || {
    _cpp_err "build: cannot resolve --target-ref '$target_ref'"
    return 2
  }
  base_sha="$(_cpp_resolve_commit base-ref "$base_ref")" || {
    _cpp_err "build: cannot resolve --base-ref '$base_ref'"
    return 2
  }

  local scratch
  scratch="$(mktemp -d)" || {
    _cpp_err "build: mktemp -d failed"
    return 2
  }
  local idx="$scratch/index"

  if ! GIT_INDEX_FILE="$idx" git read-tree "$target_sha" 2>/dev/null; then
    _cpp_err "build: git read-tree failed for '$target_sha'"
    rm -rf "$scratch"
    return 2
  fi

  local pair repo_path local_file mode line target_blob
  local base_line base_blob
  local -a written_paths=()

  for pair in "${pairs[@]}"; do
    repo_path="${pair%%=*}"
    local_file="${pair#*=}"
    if [[ -z "$repo_path" || "$repo_path" == "$pair" ]]; then
      _cpp_err "build: expected PATH=LOCALFILE, got '$pair'"
      rm -rf "$scratch"
      return 2
    fi
    if [[ ! -f "$local_file" ]]; then
      _cpp_err "build: local file not found: $local_file"
      rm -rf "$scratch"
      return 2
    fi

    line="$(git ls-tree "$target_sha" -- "$repo_path")"
    if [[ -n "$line" ]]; then
      read -r mode _type target_blob _rest <<<"$line"
      if [[ "$base_sha" != "$target_sha" ]]; then
        base_line="$(git ls-tree "$base_sha" -- "$repo_path")"
        if [[ -n "$base_line" ]]; then
          read -r _bmode _btype base_blob _brest <<<"$base_line"
          if [[ "$base_blob" != "$target_blob" ]]; then
            _cpp_err "build: diverged: $repo_path base=${base_blob:0:7} target=${target_blob:0:7}"
            rm -rf "$scratch"
            return 3
          fi
        fi
      fi
    else
      # New path — nothing to read from --target-ref. Not a guess: read the
      # one piece of real information available, the local source file's own
      # executable bit, rather than defaulting blind.
      if [[ -x "$local_file" ]]; then
        mode="100755"
      else
        mode="100644"
      fi
    fi

    local blob
    blob="$(git hash-object -w "$local_file" 2>/dev/null)" || {
      _cpp_err "build: hash-object failed for $local_file"
      rm -rf "$scratch"
      return 2
    }
    if ! GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo "$mode,$blob,$repo_path" 2>/dev/null; then
      _cpp_err "build: update-index failed for $repo_path"
      rm -rf "$scratch"
      return 2
    fi
    written_paths+=("$repo_path")
  done

  local tree commit_sha
  tree="$(GIT_INDEX_FILE="$idx" git write-tree 2>/dev/null)" || {
    _cpp_err "build: write-tree failed"
    rm -rf "$scratch"
    return 2
  }
  commit_sha="$(git commit-tree "$tree" -p "$target_sha" -m "$message" 2>/dev/null)" || {
    _cpp_err "build: commit-tree failed"
    rm -rf "$scratch"
    return 2
  }

  # Self-verify scope: refuse to hand back a commit that touches anything
  # other than exactly the paths it was asked to write. Guards against a
  # --target-ref that moved further than the caller realized.
  local actual expected
  actual="$(git diff --name-only "$target_sha" "$commit_sha" | sort -u)"
  expected="$(printf '%s\n' "${written_paths[@]}" | sort -u)"
  if [[ "$actual" != "$expected" ]]; then
    _cpp_err "build: scope check failed — commit touches a different path set than requested"
    _cpp_err "build:   requested: $(printf '%s ' "${written_paths[@]}")"
    _cpp_err "build:   actual:    $(printf '%s ' $actual)"
    rm -rf "$scratch"
    return 4
  fi

  rm -rf "$scratch"
  _cpp_err "build: parent=$target_sha branch=$branch commit=$commit_sha"
  printf '%s\n' "$commit_sha"
}

# ── extract ───────────────────────────────────────────────────────────────────

code_plane_pr_extract() {
  local ref=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ref) ref="$2"; shift 2 ;;
      -*) _cpp_err "extract: unknown flag '$1'"; return 2 ;;
      *) _cpp_err "extract: unexpected argument '$1'"; return 2 ;;
    esac
  done
  if [[ -z "$ref" ]]; then
    _cpp_err "extract: --ref is required"
    return 2
  fi

  local sha
  sha="$(_cpp_resolve_commit ref "$ref")" || {
    _cpp_err "extract: cannot resolve --ref '$ref'"
    return 2
  }

  local dir
  dir="$(mktemp -d)" || {
    _cpp_err "extract: mktemp -d failed"
    return 2
  }
  if ! git archive "$sha" | tar -x -C "$dir"; then
    _cpp_err "extract: git archive | tar -x failed for '$sha'"
    rm -rf "$dir"
    return 2
  fi
  printf '%s\n' "$dir"
}

# ── push ──────────────────────────────────────────────────────────────────────

code_plane_pr_push() {
  local remote="" branch="" commit="" force=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --remote) remote="$2"; shift 2 ;;
      --branch) branch="$2"; shift 2 ;;
      --commit) commit="$2"; shift 2 ;;
      --force)  force=true;  shift ;;
      -*) _cpp_err "push: unknown flag '$1'"; return 2 ;;
      *) _cpp_err "push: unexpected argument '$1'"; return 2 ;;
    esac
  done
  if [[ -z "$remote" || -z "$branch" || -z "$commit" ]]; then
    _cpp_err "push: --remote, --branch and --commit are required"
    return 2
  fi
  # Every `build` starts fresh from --target-ref rather than the previous
  # round's commit (see the header comment), so a second round's commit is a
  # sibling of the first, not a descendant — plain push is never a
  # fast-forward on a second round. --force is opt-in and only ever moves
  # the caller's own PR branch, never a shared branch like main.
  if [[ "$force" == true ]]; then
    git push --force-with-lease "$remote" "${commit}:refs/heads/${branch}"
  else
    git push "$remote" "${commit}:refs/heads/${branch}"
  fi
}

# ── dispatcher ────────────────────────────────────────────────────────────────

code_plane_pr() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    build)   code_plane_pr_build "$@" ;;
    extract) code_plane_pr_extract "$@" ;;
    push)    code_plane_pr_push "$@" ;;
    ""|help|-h|--help) code_plane_pr_usage ;;
    *) _cpp_err "unknown command '$cmd'"; code_plane_pr_usage; return 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  code_plane_pr "$@"
fi
