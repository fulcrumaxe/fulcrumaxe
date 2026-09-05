#!/usr/bin/env bash
# scripts/lib/auto-pull-recover.sh — safe recovery from an auto-pull untracked
# collision (D#1911).
#
# Background: post-merge-hook.sh's auto_pull step used to recover from
#   "error: The following untracked working tree files would be overwritten by merge"
# by parsing filenames out of git's *stderr prose* and unlinking each one at
# the repo root, unvalidated. Two things went wrong with that:
#
#   * The fifty-line `grep -A` window had no terminator, so git's trailing prose
#     was parsed as a filename. Observed captures include `Aborting` and
#     `Updating <sha>..<sha>`.
#     A repo-root file named `Aborting` died with no adversary involved at all.
#   * Git does not quote-escape paths in that message, and a filename may
#     contain a newline. `evil<LF>../canary` arrives as two lines, the second of
#     which concatenated to a path one level *above* the repo root — in
#     range of ~/.autonomous-forever-state/, ~/.config/gh/ and ~/.ssh/.
#
# The fix is not a better parser. The set of paths this code acts on is
# *derived* from git plumbing (NUL-delimited) and never from a name's text, so
# no spelling of a filename can add a member to it. Every derived member then
# passes two independent gates before anything happens to it, and the action is
# a move into archive/ rather than a delete (CLAUDE.md Archive Protocol).
#
# Public contract:
#
#   auto_pull_recover_untracked <repo_root>
#     0 = at least one colliding untracked file was moved aside; the caller
#         should retry `git pull --ff-only`.
#     1 = declined. Nothing was moved and nothing was deleted; the caller should
#         log and skip, exactly as the modified-file branch already does.
#     Sets on return:
#       AUTO_PULL_RECOVER_SUMMARY  one-line human summary for the team log
#       AUTO_PULL_RECOVER_MOVED    indented lines, "displaced <src> -> <dest>"
#       AUTO_PULL_RECOVER_SKIPPED  indented lines, "skipped <path> — <reason>"
#     Paths in those strings are %q-escaped, so a newline-bearing name stays on
#     one line and stays readable to someone who was not present.
#
#   auto_pull_candidate_allowed <repo_root> <path>
#     0 = both gates pass.
#     1 = rejected; AUTO_PULL_SKIP_REASON says which gate and why.
#     Exported separately so the gates can be driven directly by tests — they
#     are the only thing standing between this code and $HOME, and a gate that
#     is only exercised through the happy path is a gate nobody is watching.
#
# Dependency-free on purpose: takes repo_root as an argument rather than reading
# an ambient global, so tests can source it standalone. Bash 4+, git, coreutils.

# Blast-radius bound. A derived set larger than this is a sign the derivation is
# wrong, not a sign there is a lot of work to do — skip and report rather than
# act partially. Overridable for tests.
AUTO_PULL_RECOVER_MAX="${AUTO_PULL_RECOVER_MAX:-20}"

# Quote a path for logs. Newlines become $'\n' instead of wrapping the line.
_apr_q() { printf '%q' "$1"; }

_apr_log() { printf '[auto-pull-recover] %s\n' "$1" >&2; }

# realpath -m into _APR_REALPATH_OUT. Returns realpath's own exit status, so a
# missing binary or a failed resolve is a signal the caller can actually act on.
# Assigns rather than prints because the caller would otherwise wrap the call in
# $( ), which eats a trailing newline in the resolved path.
_APR_REALPATH_OUT=""
_apr_realpath() {
  local out rc
  out="$(realpath -m -- "$1" 2>/dev/null; printf 'X%s' "$?")"
  rc="${out##*X}"   # greedy: everything after the marker, which we appended last
  out="${out%X*}"   # shortest suffix starting at that marker
  _APR_REALPATH_OUT="${out%$'\n'}"
  return "${rc:-1}"
}

auto_pull_candidate_allowed() {
  local repo_root="$1" path="$2"
  AUTO_PULL_SKIP_REASON=""

  if [[ -z "$path" ]]; then
    AUTO_PULL_SKIP_REASON="empty path"
    return 1
  fi

  local root_real full_real
  if ! _apr_realpath "$repo_root"; then
    AUTO_PULL_SKIP_REASON="the repo root could not be resolved"
    return 1
  fi
  root_real="$_APR_REALPATH_OUT"
  if ! _apr_realpath "${repo_root}/${path}"; then
    AUTO_PULL_SKIP_REASON="path could not be resolved"
    return 1
  fi
  full_real="$_APR_REALPATH_OUT"
  if [[ -z "$root_real" || -z "$full_real" ]]; then
    AUTO_PULL_SKIP_REASON="path resolved to nothing"
    return 1
  fi

  # Gate (a) — containment. Trailing slashes on both sides so a sibling named
  # "${REPO_ROOT}evil" is not mistaken for a child of ${REPO_ROOT}. realpath
  # resolves symlinks too, so a symlink pointing out of the tree fails here as
  # well as a literal "..".
  if [[ "${full_real}/" != "${root_real}/"* ]]; then
    AUTO_PULL_SKIP_REASON="resolves outside the repo root (${full_real})"
    return 1
  fi
  if [[ "${full_real}/" == "${root_real}/.git/"* ]]; then
    AUTO_PULL_SKIP_REASON="resolves inside .git/"
    return 1
  fi

  # Gate (b) — tracked-ness. Independent of (a) on purpose: containment catches
  # traversal, tracked-ness catches destruction of project files. Both are
  # cheap, they fail for different reasons, and a fix whose safety rests on one
  # check is one refactor away from being unsafe again. This is also why there
  # is no denylist of sensitive paths — .claude/, hooks/ and
  # scripts/lib/two-gate-check.sh are all tracked, so they are covered here
  # with no list to maintain as new sensitive files appear.
  # ":(literal)" because a pathspec is not a path: without it a tracked file
  # named ":(glob)weird" is read as pathspec magic, matches nothing, and reads
  # back as untracked. Unreachable through the derivation above — a tracked file
  # never appears as "??" in status — but this gate is also callable directly,
  # and the no-denylist claim above only holds if it is exact.
  if git -C "$repo_root" ls-files --error-unmatch -- ":(literal)$path" >/dev/null 2>&1; then
    AUTO_PULL_SKIP_REASON="tracked by git — a local edit here is a human's problem, not ours"
    return 1
  fi

  return 0
}

_apr_write_archive_readme() {
  local dir="$1" stamp="$2" moved_report="$3"
  cat > "${dir}/README.md" <<READMEEOF
# Untracked files displaced by auto-pull — ${stamp}

## When removed

${stamp}, automatically, by \`scripts/post-merge-hook.sh\`'s \`auto_pull\` step
(via \`scripts/lib/auto-pull-recover.sh\`).

## Why

\`git pull --ff-only origin main\` refused to run because incoming commits add
files that already existed here, untracked. Rather than delete them — which is
what this code used to do — auto-pull moves them aside so the pull can proceed,
and leaves them here for you.

Each file below was in **both** of these sets, and nowhere else:

- added by the incoming commits (\`git diff --name-only -z --diff-filter=A HEAD..FETCH_HEAD\`)
- untracked in the working tree (\`git status --porcelain -z --untracked-files=all\`)

and passed both safety gates: it resolves inside the repository root, and git
does not track it.

## Original path

Relative to the repository root, mirrored under this directory. A file archived
from \`docs/new.md\` is at \`docs/new.md\` inside this folder.

Displaced in this run:

\`\`\`
${moved_report}\`\`\`

## How to restore

\`\`\`bash
# from the repository root, for one file:
mv "archive/auto-pull-displaced-${stamp}/<relative/path>" "<relative/path>"
\`\`\`

Diff it against the version the pull brought in first — the tracked copy is now
the project's, and yours may be a stale local artifact that is genuinely done.

## What would justify restoring

A file here that turns out to be real local work rather than a stale build
artifact or a leftover from an earlier run. If nothing here is worth keeping
after you have looked, this whole directory can be moved to a dated archive
folder of its own or left in place — it is untracked and costs nothing.
READMEEOF
}

auto_pull_recover_untracked() {
  local repo_root="${1:-}"
  AUTO_PULL_RECOVER_SUMMARY=""
  AUTO_PULL_RECOVER_MOVED=""
  AUTO_PULL_RECOVER_SKIPPED=""

  if [[ -z "$repo_root" ]] || ! git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
    AUTO_PULL_RECOVER_SUMMARY="declined: '${repo_root}' is not a git repository"
    _apr_log "$AUTO_PULL_RECOVER_SUMMARY"
    return 1
  fi

  # FETCH_HEAD has to be current — it is the incoming half of the derivation.
  if ! git -C "$repo_root" fetch origin main >/dev/null 2>&1; then
    AUTO_PULL_RECOVER_SUMMARY="declined: 'git fetch origin main' failed, so FETCH_HEAD cannot be trusted"
    _apr_log "$AUTO_PULL_RECOVER_SUMMARY"
    return 1
  fi

  # Incoming additions, NUL-delimited. Nothing here comes from prose.
  local -a incoming=()
  local p
  while IFS= read -r -d '' p; do
    [[ -n "$p" ]] && incoming+=("$p")
  done < <(git -C "$repo_root" diff --name-only -z --diff-filter=A HEAD..FETCH_HEAD 2>/dev/null)

  if [[ ${#incoming[@]} -eq 0 ]]; then
    AUTO_PULL_RECOVER_SUMMARY="declined: the incoming commits add no files, so no untracked file can be colliding"
    _apr_log "$AUTO_PULL_RECOVER_SUMMARY"
    return 1
  fi

  # Untracked working-tree files, NUL-delimited. --untracked-files=all so files
  # inside a wholly-untracked directory are named individually; the default
  # would report the directory and the intersection below would miss them.
  local -A untracked=()
  local st
  while IFS= read -r -d '' st; do
    [[ "${st:0:3}" == "?? " ]] || continue
    untracked["${st:3}"]=1
  done < <(git -C "$repo_root" status --porcelain -z --untracked-files=all 2>/dev/null)

  # The intersection is the whole point: a member has to be named by git twice,
  # in two different plumbing outputs, to be acted on.
  local -a candidates=()
  for p in "${incoming[@]}"; do
    [[ -n "${untracked[$p]+set}" ]] && candidates+=("$p")
  done

  local n=${#candidates[@]}
  if [[ $n -eq 0 ]]; then
    AUTO_PULL_RECOVER_SUMMARY="declined: no incoming addition collides with an untracked working-tree file"
    _apr_log "$AUTO_PULL_RECOVER_SUMMARY"
    return 1
  fi
  if [[ $n -gt $AUTO_PULL_RECOVER_MAX ]]; then
    AUTO_PULL_RECOVER_SUMMARY="declined: derived set has ${n} entries, over the bound of ${AUTO_PULL_RECOVER_MAX} — refusing to act partially"
    _apr_log "$AUTO_PULL_RECOVER_SUMMARY"
    return 1
  fi

  local stamp archive_rel archive_abs
  stamp="$(date +%Y-%m-%d)"
  archive_rel="archive/auto-pull-displaced-${stamp}"
  archive_abs="${repo_root}/${archive_rel}"

  local moved=0 gate_rejected=0 fs_failed=0 dest dest_dir dest_rel src
  for p in "${candidates[@]}"; do
    if ! auto_pull_candidate_allowed "$repo_root" "$p"; then
      gate_rejected=$((gate_rejected + 1))
      AUTO_PULL_RECOVER_SKIPPED+="  skipped $(_apr_q "$p") — ${AUTO_PULL_SKIP_REASON}"$'\n'
      _apr_log "skipped $(_apr_q "$p") — ${AUTO_PULL_SKIP_REASON}"
      continue
    fi

    src="${repo_root}/${p}"
    dest="${archive_abs}/${p}"
    # Pure parameter expansion, not $(dirname) — command substitution would eat
    # a trailing newline in a directory component.
    dest_dir="${dest%/*}"
    if [[ -e "$dest" || -L "$dest" ]]; then
      dest="${dest}.$(date +%s)"
    fi

    if ! mkdir -p -- "$dest_dir" 2>/dev/null; then
      fs_failed=$((fs_failed + 1))
      AUTO_PULL_RECOVER_SKIPPED+="  skipped $(_apr_q "$p") — could not create its archive directory"$'\n'
      _apr_log "skipped $(_apr_q "$p") — could not create its archive directory"
      continue
    fi
    if ! mv -- "$src" "$dest" 2>/dev/null; then
      fs_failed=$((fs_failed + 1))
      AUTO_PULL_RECOVER_SKIPPED+="  skipped $(_apr_q "$p") — mv into the archive failed"$'\n'
      _apr_log "skipped $(_apr_q "$p") — mv into the archive failed"
      continue
    fi

    dest_rel="${dest#"${repo_root}/"}"
    moved=$((moved + 1))
    AUTO_PULL_RECOVER_MOVED+="  displaced $(_apr_q "$p") -> $(_apr_q "$dest_rel")"$'\n'
    _apr_log "displaced $(_apr_q "$p") -> $(_apr_q "$dest_rel")"
  done

  if [[ $moved -eq 0 ]]; then
    # Name both subsystems and their counts. A single "rejected by the safety
    # gates" line sends whoever is debugging this into the gate logic even when
    # the real cause was an unwritable archive/ directory.
    AUTO_PULL_RECOVER_SUMMARY="declined: nothing moved — of ${n} derived candidate(s), ${gate_rejected} rejected by the safety gates and ${fs_failed} failed on the filesystem (per-path reasons follow)"
    _apr_log "$AUTO_PULL_RECOVER_SUMMARY"
    return 1
  fi

  _apr_write_archive_readme "$archive_abs" "$stamp" "$AUTO_PULL_RECOVER_MOVED"

  AUTO_PULL_RECOVER_SUMMARY="displaced ${moved} of ${n} colliding untracked file(s) into ${archive_rel}/ — nothing was deleted"
  _apr_log "$AUTO_PULL_RECOVER_SUMMARY"
  return 0
}
