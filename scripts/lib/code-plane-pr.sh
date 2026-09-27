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
#   DIR=$(code_plane_pr extract --ref code-plane/main)   # resolved sha printed on stderr
#   SHA=$(code_plane_pr build --target-ref code-plane/main --base-ref <sha extract printed> \
#           --branch my-fix --message "fix the thing" \
#           path/to/file.txt=/private/scratch/file.txt)
#
# Or invoke directly:
#   bash scripts/lib/code-plane-pr.sh build --target-ref <ref> --base-ref <ref> \
#     --branch <name> --message <msg> [--skip-guards <reason>] \
#     <repo-path>=<local-file> [...]
#   bash scripts/lib/code-plane-pr.sh extract --ref <ref>
#   bash scripts/lib/code-plane-pr.sh push --remote <name> --branch <name> \
#     --commit <sha> [--skip-guards <reason>]
#
# Three disciplines this file enforces so an agent never has to remember them:
#
#   1. Byte-identity by hash, not by eye. `build` takes a --target-ref (the
#      commit the new commit is parented on) and a required --base-ref (what
#      the caller believed the current state was when it started editing —
#      pass the sha `extract` printed on stderr, or --target-ref's own value
#      again for the genuine no-gap case). Omitting --base-ref is a usage
#      error (exit 2): a plane-sensitive input that can be silently defaulted
#      is exactly the defect this discipline exists to prevent — a caller who
#      forgets the flag must be refused, not silently blended. For every
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
#      accordingly. A derived file this step regenerates itself (a mirror or
#      the manifest) is never executable, so it falls back to the target's
#      recorded mode, or 100644 if the path is brand new.
#
#   3. The scratch/index path is private and per-invocation. Every call uses
#      its own `mktemp -d` — never a fixed location — so two concurrent
#      invocations never share state, and a run that gets its scratch
#      directory wiped out from under it (a deleted session scratchpad, a
#      killed sibling process) never corrupts a different invocation's tree.
#
# `build` also self-verifies its own scope before returning a commit sha:
# it diffs the built commit against its parent and refuses (exit 4) unless
# the changed-path set is exactly the set of paths it was asked to write,
# UNION any path this step itself regenerated (see below).
#
# Derived files (D#2578)
# -----------------------
# Every code-plane commit goes through this helper, so this is the one place
# that can regenerate the derived files it can regenerate safely — before the
# commit exists, in a tree CI would actually see — instead of a human
# remembering to run a command after the fact and finding out from a red CI
# check. After the requested paths are staged (tree T1), and before
# commit-tree, `build`:
#
#   1. Extracts T1 into a private, per-invocation scratch tree (never the
#      caller's worktree — `detect_wrong_plane()` in manifest.py would refuse
#      there anyway, since it never has a populated archive/).
#   2. Regenerates the `agents/` and `commands/` mirrors for any written
#      `.claude/agents/*.md` / `.claude/commands/*.md` path, skipping any
#      pair listed in scripts/ci/twin-divergence-allowlist.json.
#   3. Regenerates engine/manifest.json by running
#      scripts/engine-sync/manifest.py generate (by absolute path) against
#      that same scratch tree. The regeneration is refused (exit 5) if it
#      would change a manifest entry for a path outside the union of what
#      the caller wrote and what this step itself regenerated — the "bounded
#      regeneration" guarantee: a full regenerate's diff is otherwise
#      unreviewably large, which is why this only ever widens the diff to
#      cover paths that are already part of this change.
#   4. Runs the three CI guards that check derived files
#      (scripts/ci/engine-manifest-guard.py, scripts/ci/ruff-ratchet.py,
#      scripts/ci/commands-twin-divergence-guard.sh) against the regenerated
#      scratch tree, and refuses (exit 5) if any of them would redden CI —
#      except `ruff-ratchet.py` exiting something other than 0 or 1 (for
#      example, no `ruff` on PATH), which is a loud WARN, not a refusal: a
#      guard that blocks real work over an environment gap gets disabled.
#      `engine-manifest-guard.py` and `commands-twin-divergence-guard.sh`
#      each refuse on ANY non-zero exit. A guard script absent from the
#      materialized tree (an old --target-ref, or a fixture that doesn't
#      carry it) is a stderr NOTE and is skipped — CI cannot run a guard the
#      tree doesn't have either.
#   5. Runs the full behavioural guard suite, scripts/ci/run-guards.sh,
#      against that same scratch tree (D#2622) — the tree `build` is about
#      to commit, not the caller's own checkout and not code-plane main.
#      `git archive | tar -x` alone leaves no `.git`, which is fine for
#      steps 1-4 but not for a guard that reads `git ls-files` to find its
#      subject set, so this step first turns the scratch tree into a
#      minimal, disposable git repo: `git init`, an
#      objects/info/alternates file pointing at this process's own object
#      store (read-only — nothing is ever written back through it), then
#      `git read-tree` of the exact tree already on disk. None of that is
#      one of the seven verbs (checkout/switch/branch/reset/clean/worktree/
#      restore) this script is never allowed to touch. AUTONOMOUS_TEAM_REPO
#      and AUTONOMOUS_TEAM_STATE_DIR are set explicitly for that one
#      subprocess call — resolved from the materialized tree's OWN
#      .autonomous-team/config.json, never inherited from the caller's
#      environment or state dir — and the run is bounded (`timeout 300`); a
#      timeout is a refusal, not a pass. Refuses (exit 5) naming the failing
#      guard(s) on any non-zero exit. scripts/ci/run-guards.sh absent from
#      the materialized tree (an old --target-ref, or a fixture that
#      doesn't carry it) is a stderr NOTE and is skipped, same discipline as
#      step 4.
#   6. Never regenerates scripts/ruff-known-findings.txt. Lowering it is a
#      judgement call ("this finding is fixed, not moved"), not a mechanical
#      derivation — see ruff-ratchet.py's own header. A finding that stops
#      reproducing refuses (exit 5) and names the exact baseline line to
#      change.
#
# `--skip-guards "<reason>"` on `build` or `push` skips steps 1-5 above (a
# loud WARN on stderr quoting the reason) and, on `push`, skips the
# re-verification before the network call. This exists so a guard red on
# main itself can never block the PR that fixes it. An empty or missing
# reason is a usage error (exit 2).
#
# Exit codes from `build`:
#   0  success — commit sha printed on stdout
#   2  usage error (bad args, unresolvable ref, missing local file, a
#      derived-files step that could not even run — for example
#      manifest.py itself failing)
#   3  byte-identity divergence between --base-ref and --target-ref
#   4  scope check failed — built commit touches more/fewer paths than asked
#      (asked = the requested paths union whatever this step regenerated)
#   5  a derived-file guard refused the commit (bounded-regeneration
#      overflow, a failing CI guard, or a ruff-baseline over-allowance), or
#      the full run-guards.sh suite failed (or timed out) against the tree
#      about to be committed; stderr names the offending path or guard(s).
#      --skip-guards bypasses this.
#
# Exit codes from `push`:
#   0  pushed (or, with --skip-guards, attempted the push regardless of
#      guard state — a real transport failure still exits non-zero)
#   2  usage error
#   5  the commit being pushed fails a derived-file guard; refused before
#      any network call ("push: REFUSED" on stderr). --skip-guards bypasses
#      this and proceeds straight to the transport.
#
# This file never runs `gh` and never pushes except via the `push` command
# above. It also never resolves a repo slug for its OWN routing purposes —
# which repo a PR opens against always stays in the caller's hands (see
# scripts/lib/repo-resolve.sh and `_resolve_code_repo`), matching the
# repo-scope card's boundary between "build the commit" and "open the PR".
# One exception (D#2622, step 5 above): the full guard run resolves the code
# repo from the materialized tree's OWN config purely to set
# AUTONOMOUS_TEAM_REPO for that one subprocess, because the guards need it
# for their own checks. That resolution never feeds `push`, never picks a
# remote, and never influences where the caller opens the PR.
#
# This file stays a single, self-contained script — executors copy it alone
# into a scratch directory — so it never `source`s a sibling file. It may
# still *run* scripts out of the materialized tree as subprocesses
# (`bash "$tree/scripts/lib/agents-plugin-mirror.sh" ...`,
# `python3 "$tree/scripts/engine-sync/manifest.py" generate`), which is not
# the same thing: those run in a private scratch tree that this process
# happens to have built, not as part of this file's own definition.

set -uo pipefail

code_plane_pr_usage() {
  cat <<'EOF'
scripts/lib/code-plane-pr.sh — build a code-plane commit without touching a
local ref, branch, index, or working tree.

  build   --target-ref <ref> --base-ref <ref> --branch <name>
          --message <msg> [--skip-guards <reason>]
          <repo-path>=<local-file> [<repo-path>=<local-file> ...]
          --base-ref is required (pass the sha `extract` printed on stderr,
          or --target-ref's own value again for the genuine no-gap case).
          Regenerates engine/manifest.json and the agents//commands/
          mirrors from the tree it just built, then runs the full
          scripts/ci/run-guards.sh suite against that same tree, and
          refuses (exit 5) if the result would still redden CI for a
          derived-file reason or if any behavioural guard fails.
          --skip-guards "<reason>" skips that regeneration and both guard
          checks entirely (loud WARN on stderr) — an empty/missing reason
          is a usage error (exit 2).
          Prints the built commit sha on stdout on success.

  extract --ref <ref>
          Extracts <ref>'s tree into a fresh, private scratch directory
          (mktemp -d) and prints that directory's path.

  push    --remote <name> --branch <name> --commit <sha>
          [--skip-guards <reason>]
          Re-verifies <sha>'s tree against the three derived-file guards
          before pushing, and refuses (exit 5, "push: REFUSED") if one
          fails — before any network call. --skip-guards "<reason>" skips
          the re-verification (loud WARN) and proceeds to the transport.
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

_cpp_array_contains() {
  # _cpp_array_contains <needle> <haystack...>
  local needle="$1"; shift
  local e
  for e in "$@"; do
    [[ "$e" == "$needle" ]] && return 0
  done
  return 1
}

_cpp_resolve_mode() {
  # _cpp_resolve_mode <target-sha> <repo-path>
  # Reads the mode for <repo-path> from <target-sha> if it exists there;
  # otherwise 100644 — every derived file this script writes (a mirror, the
  # manifest) is plain text, never executable, so there is no local-file
  # executable bit to fall back on the way the main pairs loop does for a
  # genuinely new caller-supplied file.
  local target_sha="$1" repo_path="$2" line mode
  line="$(git ls-tree "$target_sha" -- "$repo_path" 2>/dev/null)"
  if [[ -n "$line" ]]; then
    read -r mode _type _blob _rest <<<"$line"
    printf '%s\n' "$mode"
  else
    printf '100644\n'
  fi
}

_cpp_twin_allowlisted() {
  # _cpp_twin_allowlisted <dtree> <family> <name>
  local dtree="$1" family="$2" name="$3"
  local allowlist="$dtree/scripts/ci/twin-divergence-allowlist.json"
  [[ -f "$allowlist" ]] || return 1
  python3 -c '
import json, sys

allowlist_path, family, name = sys.argv[1:4]
try:
    data = json.load(open(allowlist_path))
except Exception:
    sys.exit(1)
key = family + ":" + name
for e in data.get("entries", []):
    if isinstance(e, dict) and e.get("pair") == key:
        sys.exit(0)
sys.exit(1)
' "$allowlist" "$family" "$name"
}

_cpp_apply_derived() {
  # _cpp_apply_derived <idx> <target-sha> <dtree> <repo-path> <content-file> <already-written 0|1>
  # Stages <content-file>'s bytes at <repo-path> in both the git index <idx>
  # and the scratch tree <dtree> (so a later guard run sees the regenerated
  # content), unless <dtree>/<repo-path> already matches byte-for-byte.
  # Prints one of: "" (no-op), "regenerated", "replaced" — or "ERROR".
  local idx="$1" target_sha="$2" dtree="$3" repo_path="$4" content_file="$5" already="$6"
  local dest="$dtree/$repo_path"
  if [[ -f "$dest" ]] && cmp -s "$dest" "$content_file"; then
    printf '\n'
    return 0
  fi
  mkdir -p "$(dirname "$dest")" || { printf 'ERROR\n'; return 1; }
  cp "$content_file" "$dest" || { printf 'ERROR\n'; return 1; }
  local mode blob
  mode="$(_cpp_resolve_mode "$target_sha" "$repo_path")"
  blob="$(git hash-object -w "$content_file" 2>/dev/null)" || { printf 'ERROR\n'; return 1; }
  if ! GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo "$mode,$blob,$repo_path" 2>/dev/null; then
    printf 'ERROR\n'
    return 1
  fi
  if [[ "$already" == "1" ]]; then
    printf 'replaced\n'
  else
    printf 'regenerated\n'
  fi
}

_cpp_run_guards() {
  # _cpp_run_guards <dtree>
  # Runs the three CI guards that check derived files against <dtree>.
  # Returns 0 if nothing refuses (a guard absent from <dtree> is a NOTE and
  # is skipped; ruff-ratchet.py exiting anything other than 0 or 1 is a
  # WARN, not a refusal). Returns 5 if any guard refuses.
  local dtree="$1" refused=0 out rc l

  local mg="$dtree/scripts/ci/engine-manifest-guard.py"
  if [[ -f "$mg" ]]; then
    out="$(python3 "$mg" 2>&1)"; rc=$?
    if [[ "$rc" -ne 0 ]]; then
      _cpp_err "build: guard refused: engine-manifest-guard.py (exit $rc)"
      while IFS= read -r l; do _cpp_err "build:   $l"; done <<<"$out"
      refused=1
    fi
  else
    _cpp_err "build: NOTE: scripts/ci/engine-manifest-guard.py absent from the materialized tree — skipped"
  fi

  local tg="$dtree/scripts/ci/commands-twin-divergence-guard.sh"
  if [[ -f "$tg" ]]; then
    out="$(bash "$tg" 2>&1)"; rc=$?
    if [[ "$rc" -ne 0 ]]; then
      _cpp_err "build: guard refused: commands-twin-divergence-guard.sh (exit $rc)"
      while IFS= read -r l; do _cpp_err "build:   $l"; done <<<"$out"
      refused=1
    fi
  else
    _cpp_err "build: NOTE: scripts/ci/commands-twin-divergence-guard.sh absent from the materialized tree — skipped"
  fi

  local rr="$dtree/scripts/ci/ruff-ratchet.py"
  if [[ -f "$rr" ]]; then
    out="$(python3 "$rr" 2>&1)"; rc=$?
    if [[ "$rc" -eq 1 ]]; then
      _cpp_err "build: guard refused: ruff-ratchet.py (exit $rc)"
      while IFS= read -r l; do _cpp_err "build:   $l"; done <<<"$out"
      refused=1
    elif [[ "$rc" -ne 0 ]]; then
      _cpp_err "build: WARN: ruff-ratchet.py did not run cleanly (exit $rc) — treated as advisory, not a refusal"
      while IFS= read -r l; do _cpp_err "build:   $l"; done <<<"$out"
    fi
  else
    _cpp_err "build: NOTE: scripts/ci/ruff-ratchet.py absent from the materialized tree — skipped"
  fi

  [[ "$refused" -eq 0 ]]
}

_cpp_materialize_git_index() {
  # _cpp_materialize_git_index <dtree> <tree-sha>
  # Turns the already-populated <dtree> (files already on disk, matching
  # <tree-sha> byte for byte — see _cpp_regenerate_derived_files) into a
  # minimal, disposable git repo whose INDEX also matches <tree-sha>, so a
  # `git ls-files`-based guard run from <dtree> sees a real subject set
  # instead of "not a git repository" (D#2622). Built from `git init` + an
  # objects/info/alternates file pointing at this process's own object
  # store + `git read-tree` — none of the seven verbs (checkout/switch/
  # branch/reset/clean/worktree/restore) this script is never allowed to
  # touch, and it only ever READS through the alternates file, never writes
  # back into the source object store through it. <dtree>'s working-tree
  # files are left exactly as they are; read-tree touches only the new
  # .git/index. Returns 2 on any failure.
  local dtree="$1" tree_sha="$2" common_dir
  common_dir="$(git rev-parse --git-common-dir 2>/dev/null)" || {
    _cpp_err "build: derived-files: could not resolve this process's own git dir for the guard index"
    return 2
  }
  common_dir="$(cd "$common_dir" 2>/dev/null && pwd)" || {
    _cpp_err "build: derived-files: could not resolve an absolute path for '$common_dir'"
    return 2
  }
  if ! git init -q "$dtree" 2>/dev/null; then
    _cpp_err "build: derived-files: git init failed while materializing the guard index at $dtree"
    return 2
  fi
  if ! mkdir -p "$dtree/.git/objects/info" 2>/dev/null; then
    _cpp_err "build: derived-files: could not create $dtree/.git/objects/info"
    return 2
  fi
  printf '%s\n' "$common_dir/objects" >"$dtree/.git/objects/info/alternates"
  if ! git -C "$dtree" read-tree "$tree_sha" 2>/dev/null; then
    _cpp_err "build: derived-files: git read-tree failed while materializing the guard index"
    return 2
  fi
  return 0
}

_cpp_run_full_guards() {
  # _cpp_run_full_guards <dtree>
  # Runs scripts/ci/run-guards.sh from <dtree> — which must already carry a
  # git index matching the tree, via _cpp_materialize_git_index — covering
  # behavioural guards (D#2622), not just the three derived-file guards
  # _cpp_run_guards checks above. Hermetic: AUTONOMOUS_TEAM_REPO is resolved
  # from <dtree>'s OWN config (never the caller's environment) and
  # AUTONOMOUS_TEAM_STATE_DIR points at a private scratch dir under the
  # build's own scratch, never the operator's real state dir. Bounded at
  # 300s; a timeout is a refusal, not a pass. Relays run-guards.sh's own
  # stdout/stderr onto this script's stderr, prefixed. Returns 0 on a clean
  # run (or a NOTE-skip when the runner is absent from <dtree>), 2 if the
  # code repo cannot be resolved, 5 if the runner fails or times out.
  local dtree="$1"
  local runner="$dtree/scripts/ci/run-guards.sh"
  if [[ ! -f "$runner" ]]; then
    _cpp_err "build: NOTE: scripts/ci/run-guards.sh absent from the materialized tree — full guard run skipped"
    return 0
  fi

  local resolver="$dtree/scripts/lib/repo-resolve.sh"
  if [[ ! -f "$resolver" ]]; then
    _cpp_err "build: derived-files: scripts/lib/repo-resolve.sh absent from the materialized tree — cannot resolve the code repo for the guard run"
    return 2
  fi
  local code_repo
  code_repo="$(cd "$dtree" && source scripts/lib/repo-resolve.sh && _resolve_code_repo 2>/dev/null)"
  if [[ -z "$code_repo" ]]; then
    _cpp_err "build: derived-files: could not resolve the code repo from the materialized tree's own config for the guard run"
    return 2
  fi

  local guard_state_dir
  guard_state_dir="$(dirname "$dtree")/guard-state"
  if ! mkdir -p "$guard_state_dir" 2>/dev/null; then
    _cpp_err "build: derived-files: could not create $guard_state_dir"
    return 2
  fi

  local out rc l
  out="$(cd "$dtree" && AUTONOMOUS_TEAM_REPO="$code_repo" AUTONOMOUS_TEAM_STATE_DIR="$guard_state_dir" \
    timeout --kill-after=5s 300 bash scripts/ci/run-guards.sh 2>&1)"
  rc=$?

  while IFS= read -r l; do _cpp_err "build:   $l"; done <<<"$out"

  if [[ "$rc" -ne 0 ]]; then
    _cpp_err "build: REFUSED — scripts/ci/run-guards.sh failed against the tree about to be committed (exit $rc); see the guard: lines above"
    return 5
  fi
  return 0
}

_cpp_regenerate_derived_files() {
  # _cpp_regenerate_derived_files <idx> <target-sha> <tree-sha> <dtree> <written-paths-file>
  # Extracts <tree-sha> into <dtree>, regenerates the agents/ and commands/
  # mirrors and engine/manifest.json, staging changes into <idx> and onto
  # disk under <dtree>. Prints regenerated-or-replaced repo paths (one per
  # line, "regenerated" ones only) to stdout on success. Returns 2 on a
  # hard failure (nothing usable to build from), 5 on a guard/bounds
  # refusal, 0 on success (including "nothing to do").
  local idx="$1" target_sha="$2" tree_sha="$3" dtree="$4" written_file="$5"
  local -a written_set=()
  local wline
  while IFS= read -r wline; do
    [[ -n "$wline" ]] && written_set+=("$wline")
  done <"$written_file"

  local scratch
  scratch="$(dirname "$dtree")"

  mkdir -p "$dtree" || { _cpp_err "build: derived-files: mkdir $dtree failed"; return 2; }
  if ! git archive "$tree_sha" | tar -x -C "$dtree"; then
    _cpp_err "build: derived-files: git archive | tar -x failed for tree $tree_sha"
    return 2
  fi

  local -a regenerated_paths=()

  # ── mirrors: agents/ and commands/ ──────────────────────────────────────
  # Each family's regeneration is gated independently: the "agents" family
  # needs scripts/lib/agents-plugin-mirror.sh to run the generator; the
  # "commands" family is a plain byte copy and needs nothing beyond the
  # source file itself. A fixture (or an old --target-ref) missing the
  # agents generator must not also silently skip the unrelated commands
  # family, and vice versa.
  local wp family mname mirror_path already result content_tmp e
  for wp in "${written_set[@]}"; do
    family=""
    case "$wp" in
      .claude/agents/*.md)
        family="agents"; mname="$(basename "$wp")"; mirror_path="agents/$mname" ;;
      .claude/commands/*.md)
        family="commands"; mname="$(basename "$wp")"; mirror_path="commands/$mname" ;;
      *) continue ;;
    esac
    if [[ "$family" == "agents" && ! -f "$dtree/scripts/lib/agents-plugin-mirror.sh" ]]; then
      _cpp_err "build: NOTE: scripts/lib/agents-plugin-mirror.sh absent from the materialized tree — $mirror_path not regenerated"
      continue
    fi
    if _cpp_twin_allowlisted "$dtree" "$family" "$mname"; then
      _cpp_err "build: NOTE: $mirror_path is an allowlisted deliberate variant — not regenerated"
      continue
    fi
    content_tmp="$scratch/derived-$family-$mname"
    if [[ "$family" == "agents" ]]; then
      if ! bash "$dtree/scripts/lib/agents-plugin-mirror.sh" "$dtree/.claude/agents/$mname" >"$content_tmp" 2>"$scratch/derived-err"; then
        _cpp_err "build: derived-files: agents-plugin-mirror.sh failed for $mname: $(cat "$scratch/derived-err")"
        return 2
      fi
    else
      if ! cp "$dtree/.claude/commands/$mname" "$content_tmp" 2>/dev/null; then
        _cpp_err "build: derived-files: could not read $dtree/.claude/commands/$mname"
        return 2
      fi
    fi
    already=0
    _cpp_array_contains "$mirror_path" "${written_set[@]}" && already=1
    result="$(_cpp_apply_derived "$idx" "$target_sha" "$dtree" "$mirror_path" "$content_tmp" "$already")"
    case "$result" in
      regenerated)
        regenerated_paths+=("$mirror_path")
        _cpp_err "build: regenerated $mirror_path (from .claude/$family/$mname)"
        ;;
      replaced)
        _cpp_err "build: replaced $mirror_path with the regenerated content (from .claude/$family/$mname) — the caller-supplied copy was stale"
        ;;
      ERROR)
        _cpp_err "build: derived-files: failed to stage $mirror_path"
        return 2
        ;;
      *) : ;;
    esac
  done

  # ── engine/manifest.json ─────────────────────────────────────────────────
  if [[ -f "$dtree/scripts/engine-sync/manifest.py" ]]; then
    local old_manifest="$scratch/manifest-old.json"
    if [[ -f "$dtree/engine/manifest.json" ]]; then
      cp "$dtree/engine/manifest.json" "$old_manifest"
    else
      printf '{}' >"$old_manifest"
    fi

    local gen_out gen_rc
    gen_out="$(python3 "$dtree/scripts/engine-sync/manifest.py" generate 2>&1)"
    gen_rc=$?
    if [[ "$gen_rc" -ne 0 ]]; then
      _cpp_err "build: derived-files: manifest.py generate failed (exit $gen_rc): $gen_out"
      return 2
    fi

    local allowed_file="$scratch/manifest-allowed.txt"
    { printf '%s\n' "${written_set[@]}"; printf '%s\n' "${regenerated_paths[@]}"; } >"$allowed_file"
    local written_file2="$scratch/manifest-written.txt"
    printf '%s\n' "${written_set[@]}" >"$written_file2"

    local check_out
    check_out="$(python3 - "$old_manifest" "$dtree/engine/manifest.json" "$allowed_file" "$written_file2" <<'PYEOF'
import json
import sys

old_path, new_path, allowed_path, written_path = sys.argv[1:5]
old = json.load(open(old_path)).get("files", {})
new = json.load(open(new_path)).get("files", {})
allowed = {l.strip() for l in open(allowed_path) if l.strip()}
written = {l.strip() for l in open(written_path) if l.strip()}

changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
for p in sorted(changed - allowed):
    print(f"OUTSIDE:{p}")
for p in sorted(changed & written):
    print(f"TRIGGER:{p}")
PYEOF
)"

    local outside triggers
    outside="$(printf '%s\n' "$check_out" | grep '^OUTSIDE:' | sed 's/^OUTSIDE://')"
    triggers="$(printf '%s\n' "$check_out" | grep '^TRIGGER:' | sed 's/^TRIGGER://')"

    if [[ -n "$outside" ]]; then
      _cpp_err "build: derived-files: regenerating engine/manifest.json would change an entry outside the requested write set:"
      while IFS= read -r p; do
        [[ -n "$p" ]] && _cpp_err "build:   $p"
      done <<<"$outside"
      return 5
    fi

    if cmp -s "$old_manifest" "$dtree/engine/manifest.json"; then
      : # unchanged — quiet, no-op
    else
      local already=0
      _cpp_array_contains "engine/manifest.json" "${written_set[@]}" && already=1
      local mode blob
      mode="$(_cpp_resolve_mode "$target_sha" "engine/manifest.json")"
      blob="$(git hash-object -w "$dtree/engine/manifest.json" 2>/dev/null)" || {
        _cpp_err "build: derived-files: hash-object failed for engine/manifest.json"
        return 2
      }
      if ! GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo "$mode,$blob,engine/manifest.json" 2>/dev/null; then
        _cpp_err "build: derived-files: failed to stage engine/manifest.json"
        return 2
      fi
      if [[ "$already" == "1" ]]; then
        _cpp_err "build: replaced engine/manifest.json with the regenerated content — the caller-supplied copy was stale"
      else
        regenerated_paths+=("engine/manifest.json")
        _cpp_err "build: regenerated engine/manifest.json ($(printf '%s ' $triggers))"
      fi
    fi
  fi

  printf '%s\n' "${regenerated_paths[@]}"
  return 0
}

# ── build ─────────────────────────────────────────────────────────────────────

code_plane_pr_build() {
  local base_ref="" target_ref="" branch="" message=""
  local skip_guards_given=false skip_guards_reason=""
  local -a pairs=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --base-ref)     base_ref="$2";     shift 2 ;;
      --target-ref)   target_ref="$2";   shift 2 ;;
      --branch)       branch="$2";       shift 2 ;;
      --message)      message="$2";      shift 2 ;;
      --skip-guards)  skip_guards_given=true; skip_guards_reason="$2"; shift 2 ;;
      --) shift; pairs+=("$@"); break ;;
      -*) _cpp_err "build: unknown flag '$1'"; return 2 ;;
      *) pairs+=("$1"); shift ;;
    esac
  done

  if [[ -z "$target_ref" || -z "$branch" || -z "$message" || ${#pairs[@]} -eq 0 ]]; then
    _cpp_err "build: --target-ref, --branch, --message and at least one PATH=LOCALFILE are required"
    return 2
  fi
  if [[ -z "$base_ref" ]]; then
    _cpp_err "build: --base-ref is required — pass the sha 'extract' printed on stderr for the tree you started from, or --target-ref's own value again if you genuinely have no prior base"
    return 2
  fi
  if [[ "$skip_guards_given" == true && -z "$skip_guards_reason" ]]; then
    _cpp_err "build: --skip-guards requires a non-empty reason"
    return 2
  fi

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

  local tree
  tree="$(GIT_INDEX_FILE="$idx" git write-tree 2>/dev/null)" || {
    _cpp_err "build: write-tree failed"
    rm -rf "$scratch"
    return 2
  }

  # ── derived-files step (D#2578) ─────────────────────────────────────────
  local -a regenerated_paths=()
  if [[ "$skip_guards_given" == true ]]; then
    _cpp_err "build: WARN --skip-guards: $skip_guards_reason (derived-files regeneration and guard checks skipped)"
  else
    local written_file="$scratch/written-paths.txt"
    printf '%s\n' "${written_paths[@]}" >"$written_file"
    local dtree="$scratch/tree"
    local regen_out regen_rc
    regen_out="$(_cpp_regenerate_derived_files "$idx" "$target_sha" "$tree" "$dtree" "$written_file")"
    regen_rc=$?
    if [[ "$regen_rc" -ne 0 ]]; then
      rm -rf "$scratch"
      return "$regen_rc"
    fi
    while IFS= read -r line; do
      [[ -n "$line" ]] && regenerated_paths+=("$line")
    done <<<"$regen_out"

    if [[ "${#regenerated_paths[@]}" -gt 0 ]]; then
      tree="$(GIT_INDEX_FILE="$idx" git write-tree 2>/dev/null)" || {
        _cpp_err "build: write-tree failed after derived-files regeneration"
        rm -rf "$scratch"
        return 2
      }
    fi

    if ! _cpp_run_guards "$dtree"; then
      _cpp_err "build: REFUSED — a derived-file guard failed against the regenerated tree; see the guard: lines above"
      rm -rf "$scratch"
      return 5
    fi

    # ── full behavioural guard suite (D#2622) — run against the exact tree
    # this build is about to commit, not the caller's checkout, not main.
    if ! _cpp_materialize_git_index "$dtree" "$tree"; then
      rm -rf "$scratch"
      return 2
    fi
    local full_guards_rc
    _cpp_run_full_guards "$dtree"
    full_guards_rc=$?
    if [[ "$full_guards_rc" -ne 0 ]]; then
      rm -rf "$scratch"
      return "$full_guards_rc"
    fi
  fi

  local commit_sha
  commit_sha="$(git commit-tree "$tree" -p "$target_sha" -m "$message" 2>/dev/null)" || {
    _cpp_err "build: commit-tree failed"
    rm -rf "$scratch"
    return 2
  }

  # Self-verify scope: refuse to hand back a commit that touches anything
  # other than exactly the paths it was asked to write, union whatever the
  # derived-files step itself regenerated. Guards against a --target-ref
  # that moved further than the caller realized.
  local actual expected
  actual="$(git diff --name-only "$target_sha" "$commit_sha" | sort -u)"
  expected="$(printf '%s\n' "${written_paths[@]}" "${regenerated_paths[@]}" | sort -u)"
  if [[ "$actual" != "$expected" ]]; then
    _cpp_err "build: scope check failed — commit touches a different path set than requested"
    _cpp_err "build:   requested: $(printf '%s ' "${written_paths[@]}")"
    _cpp_err "build:   regenerated: $(printf '%s ' "${regenerated_paths[@]}")"
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
  # Carry the resolved identity out on stderr (the discipline-1 fix in
  # `build` needs a caller-supplied --base-ref; this is what a caller pins it
  # to). Stdout stays exactly the directory path — never blend the two.
  _cpp_err "extract: ref=$ref sha=$sha"

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
  local skip_guards_given=false skip_guards_reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --remote)      remote="$2"; shift 2 ;;
      --branch)      branch="$2"; shift 2 ;;
      --commit)      commit="$2"; shift 2 ;;
      --force)       force=true;  shift ;;
      --skip-guards) skip_guards_given=true; skip_guards_reason="$2"; shift 2 ;;
      -*) _cpp_err "push: unknown flag '$1'"; return 2 ;;
      *) _cpp_err "push: unexpected argument '$1'"; return 2 ;;
    esac
  done
  if [[ -z "$remote" || -z "$branch" || -z "$commit" ]]; then
    _cpp_err "push: --remote, --branch and --commit are required"
    return 2
  fi
  if [[ "$skip_guards_given" == true && -z "$skip_guards_reason" ]]; then
    _cpp_err "push: --skip-guards requires a non-empty reason"
    return 2
  fi

  if [[ "$skip_guards_given" == true ]]; then
    _cpp_err "push: WARN --skip-guards: $skip_guards_reason (re-verification skipped)"
  else
    local commit_sha
    commit_sha="$(_cpp_resolve_commit commit "$commit")" || {
      _cpp_err "push: cannot resolve --commit '$commit'"
      return 2
    }
    local scratch
    scratch="$(mktemp -d)" || {
      _cpp_err "push: mktemp -d failed"
      return 2
    }
    local dtree="$scratch/tree"
    mkdir -p "$dtree"
    if ! git archive "$commit_sha" | tar -x -C "$dtree"; then
      _cpp_err "push: could not extract commit '$commit_sha' for re-verification"
      rm -rf "$scratch"
      return 2
    fi
    if ! _cpp_run_guards "$dtree"; then
      _cpp_err "push: REFUSED — the commit being pushed fails a derived-file guard; see the guard: lines above. Re-run 'build' to regenerate, or pass --skip-guards \"<reason>\" to push anyway."
      rm -rf "$scratch"
      return 5
    fi
    rm -rf "$scratch"
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
