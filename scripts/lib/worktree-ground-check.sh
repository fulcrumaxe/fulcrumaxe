#!/usr/bin/env bash
# scripts/lib/worktree-ground-check.sh — did the ground a command was
# standing on survive the command?
#
# D#1809 Lane B: a reviewer's test run was corrupted when its working
# directory vanished mid-run (the directory itself was removed by something
# else while a test suite was executing inside it), and the corrupted run
# reported as 489 ordinary test failures on a clean branch. A vanished
# directory is not the same failure mode as a real bug and must not be able
# to read as either an ordinary pass or an ordinary set of failures.
#
# wt_ground_intact <dir>
#   Returns 0 if <dir> exists AND `git -C <dir> rev-parse --git-dir`
#   succeeds from it (proving <dir> is still a resolvable git working tree,
#   not just a directory that happens to still be present as an empty
#   husk). Returns 1 otherwise.
#
# Takes an explicit <dir> argument on purpose — never resolves via ambient
# $PWD. Lane A's guard (_wtr_is_self) resolved via `git rev-parse
# --show-toplevel` with no `-C`, which made it cwd-dependent: called after a
# `cd` elsewhere, it would silently answer for the wrong directory and look
# like a working guard while being inert. This predicate is called once
# before a command runs and once after, from the caller's original cwd, so
# it must not depend on where the caller has wandered to in between.
#
# Self-contained. No repo-specific state, no sourcing of other libs.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/worktree-ground-check.sh"
#   if wt_ground_intact "$dir"; then ...; fi

wt_ground_intact() {
  local dir="$1"
  [ -n "$dir" ] || return 1
  [ -d "$dir" ] || return 1
  git -C "$dir" rev-parse --git-dir >/dev/null 2>&1
}
