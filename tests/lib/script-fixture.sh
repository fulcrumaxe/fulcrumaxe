#!/usr/bin/env bash
# tests/lib/script-fixture.sh — stage a scripts/*.sh file under test together
# with the scripts/lib/*.sh files it actually `source`s, into a scratch dir
# (D#2163).
#
# The problem this solves
# ------------------------
# Four bash suites each built their own scratch copy of a scripts/*.sh file
# under test but only copied the top-level script, never the scripts/lib/*
# files it sources. The copy sources under `set -uo pipefail` (no `-e`), so
# a missing lib doesn't abort the script — it just quietly removes the
# functions that lib defined, and the suite fails downstream with a symptom
# that often doesn't even mention the missing file.
#
# One suite's fixture (test_pre_spawn_token_cap.sh's make_ws) got close —
# `cp -r "$SCRIPTS_DIR/lib/" "$ws/scripts/lib/"` — but tripped a separate
# GNU cp behavior: when the destination directory already exists (it does,
# from a prior `mkdir -p`), `cp -r src/ dst/` nests the source inside it
# instead of merging into it, landing every lib at `dst/lib/lib/*.sh`
# instead of `dst/lib/*.sh`.
#
# Neither "forget the libs" nor "cp -r everything and hope" is the fix.
# Copying the whole of scripts/lib/ into every fixture defeats the reason
# these are scratch fixtures in the first place: a test could start passing
# because of a lib it never meant to exercise, and a lib deleted from
# scripts/lib/ would stop failing the suite that's supposed to catch that.
#
# What this does instead
# ------------------------
# stage_script_with_libs copies the named script, then resolves the
# `source "<path>.sh"` lines it actually contains — literal statements only,
# grepped off the file, never a hardcoded per-script list, so the fixture
# can't drift out of sync with the script the way the four originals did —
# and copies only those libs. Libs can source other libs (e.g.
# scripts/lib/auto-pull-step.sh sources scripts/lib/repo-resolve.sh), so the
# resolution is transitive: each staged lib is scanned the same way, and
# newly discovered libs are queued until nothing new turns up.
#
# Usage:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/script-fixture.sh"
#   stage_script_with_libs "$REPO_ROOT" "spawn-agent.sh" "$ws/scripts"
#
# After the call:
#   $ws/scripts/spawn-agent.sh   <- the real script, byte-for-byte
#   $ws/scripts/lib/<name>.sh    <- exactly the libs it sources
#                                    (transitively), nothing else
#
# A caller that wants to override a staged lib with its own stub (a mocked
# gh-token.sh, fake hook-event.sh functions, etc.) should write that stub
# *after* calling this — the helper doesn't know about a caller's mocks and
# won't skip a lib on the caller's behalf. Staging first and stubbing after
# is also what lets a caller keep an existing manual stub unchanged: the
# stub file simply gets overwritten back to the caller's content once the
# helper is done.

# _script_fixture_sourced_libs <script_file>
# Prints, one per line, the basename of every *.sh file the given script
# `source`s via a literal `source "<path>.sh"` statement. Only lines that
# start with the bare `source` keyword (after leading whitespace) match, so
# comment lines — including `# shellcheck source=scripts/lib/x.sh` and prose
# like "# This is the single source of truth" — are excluded. A dynamic
# source (`source "$SOME_VAR"`, no literal .sh in the quoted text) is not
# matched; none of D#2163's four suites need one resolved.
_script_fixture_sourced_libs() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  { grep -E '^[[:space:]]*source[[:space:]]+"[^"]+\.sh"' "$file" || true; } \
    | sed -E 's/^[[:space:]]*source[[:space:]]+"([^"]+)".*/\1/' \
    | while IFS= read -r path; do
        basename "$path"
      done
  return 0
}

# stage_script_with_libs <repo_root> <script_name> <dest_scripts_dir>
#
# Copies <repo_root>/scripts/<script_name> to
# <dest_scripts_dir>/<script_name>, then copies every scripts/lib/*.sh file
# it sources — transitively — into <dest_scripts_dir>/lib/. Nothing else in
# <dest_scripts_dir> is touched, so this is safe to call either before or
# after a caller's own stubs, as long as the caller applies its own stubs
# last if it wants them to stick (see header comment).
stage_script_with_libs() {
  local repo_root="$1" script_name="$2" dest_scripts_dir="$3"
  local src_script="$repo_root/scripts/$script_name"
  local lib_src_dir="$repo_root/scripts/lib"
  local lib_dst_dir="$dest_scripts_dir/lib"

  if [[ ! -f "$src_script" ]]; then
    echo "stage_script_with_libs: no such script: $src_script" >&2
    return 1
  fi

  if ! mkdir -p "$dest_scripts_dir" "$lib_dst_dir"; then
    echo "stage_script_with_libs: could not create $lib_dst_dir" >&2
    return 1
  fi

  if ! cp "$src_script" "$dest_scripts_dir/$script_name"; then
    echo "stage_script_with_libs: could not copy $src_script" >&2
    return 1
  fi
  chmod +x "$dest_scripts_dir/$script_name" 2>/dev/null || true

  # Worklist over the transitive source graph. Each lib is staged at most
  # once — the associative array is the "already staged" set.
  local -A staged=()
  local -a queue=()
  local name child src_lib

  while IFS= read -r name; do
    if [[ -n "$name" ]]; then
      queue+=("$name")
    fi
  done < <(_script_fixture_sourced_libs "$src_script")

  while [[ "${#queue[@]}" -gt 0 ]]; do
    name="${queue[0]}"
    queue=("${queue[@]:1}")

    if [[ -n "${staged[$name]:-}" ]]; then
      continue
    fi
    staged["$name"]=1

    src_lib="$lib_src_dir/$name"
    if [[ ! -f "$src_lib" ]]; then
      echo "stage_script_with_libs: $script_name sources missing lib: $name" >&2
      continue
    fi

    if ! cp "$src_lib" "$lib_dst_dir/$name"; then
      echo "stage_script_with_libs: could not copy $src_lib" >&2
      return 1
    fi
    chmod +x "$lib_dst_dir/$name" 2>/dev/null || true

    while IFS= read -r child; do
      if [[ -n "$child" ]] && [[ -z "${staged[$child]:-}" ]]; then
        queue+=("$child")
      fi
    done < <(_script_fixture_sourced_libs "$src_lib")
  done

  return 0
}
