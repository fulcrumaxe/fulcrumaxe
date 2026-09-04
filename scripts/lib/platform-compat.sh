#!/usr/bin/env bash
# scripts/lib/platform-compat.sh — GNU/BSD platform-compatibility helpers
# for the loop-bootstrap adopter path and the handful of other scripts that
# read file mtimes/sizes or do date arithmetic (D#2263).
#
# Phase 1 (pc_preflight / pc_sed_i): GNU sed (Linux) and BSD sed (macOS)
# both take an -i flag but disagree on whether it requires a separate
# argument: `sed -i 's|a|b|g' file` is correct on Linux and wrong on macOS,
# where BSD sed consumes the sed script itself as the -i backup-suffix
# argument and then tries to run the target FILE as a sed script.
# bootstrap.sh runs under `set -euo pipefail`, so that failure used to kill
# the script mid-install, after files had already been copied into the
# target but before their repo identifiers were rewritten.
#
# Phase 2 (pc_stat_mtime / pc_stat_size / pc_date_offset): GNU `stat -c`
# and GNU `date -d` have no BSD equivalent flag at all (BSD stat uses `-f`,
# BSD date uses `-j -f FMT -v`). Unlike the sed case, BSD's rejection of
# `-c`/`-d` is a clean non-zero exit with nothing on stdout — but several
# call sites wrapped that call in `|| echo 0`, so on macOS the failure
# vanishes into a fake epoch-0 mtime instead of surfacing. An age computed
# against epoch 0 reads as "infinitely stale," which is worse than a
# crash: it is silent, and it is invisible on Linux because GNU stat never
# takes that branch. pc_stat_mtime/pc_stat_size deliberately do NOT supply
# their own "|| echo 0" — a caller that cannot get a real mtime must decide
# for itself what a safe default looks like for what it's about to do with
# that number (see run-loop-iteration.sh and inject-context.sh for two
# different answers to that).
#
# This file is the single place any platform branch for these paths lives.
# It never probes `uname`: a Linux host with a stripped/incompatible tool
# should refuse/degrade exactly like an incompatible macOS host would, and
# a macOS host with GNU coreutils on PATH should pass — so every probe
# actually RUNS the candidate command against a disposable scratch file
# and checks whether it did what it claims. Never probe against $TARGET.
#
# Every probe here is written to survive a caller's `set -euo pipefail`:
# probes are *expected* to fail on one of the two branches, so each
# expected-to-sometimes-fail command is individually guarded (`if ! cmd;
# then` / part of an `&&` chain) rather than left to trip the caller's
# errexit before this file gets a chance to report why.

PC_SED_I_MODE=""

# pc_preflight — run every Phase-1 utility probe. On success, sets whatever
# PC_* mode variable(s) the pc_* helpers below need and returns 0. On the
# first failure, prints a plain-language refusal to stderr — naming the
# missing/incompatible utility, no stack trace — and returns 1. Writes
# nothing outside its own mktemp scratch directories. Callers MUST check
# the return value and stop before performing any write of their own.
pc_preflight() {
  _pc_detect_sed_i || return 1
  return 0
}

# _pc_detect_sed_i — try the GNU in-place form, then the BSD in-place form,
# against a real scratch file, and record whichever one actually rewrote
# it. Neither branch is assumed to work; both are executed for real.
_pc_detect_sed_i() {
  local scratch f
  scratch="$(mktemp -d 2>/dev/null)" || {
    echo "ERROR: platform-compat preflight could not create a scratch directory (mktemp -d failed)" >&2
    return 1
  }
  f="$scratch/probe.txt"

  # GNU form: sed -i SCRIPT FILE
  printf 'a\n' > "$f"
  if sed -i 's/a/b/' "$f" >/dev/null 2>&1 && [[ "$(cat "$f" 2>/dev/null)" == "b" ]]; then
    PC_SED_I_MODE="gnu"
    rm -rf "$scratch"
    return 0
  fi

  # BSD form: sed -i '' SCRIPT FILE
  printf 'a\n' > "$f"
  if sed -i '' 's/a/b/' "$f" >/dev/null 2>&1 && [[ "$(cat "$f" 2>/dev/null)" == "b" ]]; then
    PC_SED_I_MODE="bsd"
    rm -rf "$scratch"
    return 0
  fi

  rm -rf "$scratch"
  echo "ERROR: no usable in-place 'sed -i' found on this PATH. bootstrap needs either GNU sed (sed -i SCRIPT file) or BSD sed (sed -i '' SCRIPT file); this host's sed accepted neither. Install GNU sed (most Linux distros ship it by default; on macOS: 'brew install gnu-sed') or make sure the system sed is unmodified." >&2
  return 1
}

# pc_sed_i EXPR FILE — portable in-place sed substitution, using whichever
# invocation style _pc_detect_sed_i found working. pc_preflight must have
# already run and returned 0 before this is called.
pc_sed_i() {
  local expr="$1" file="$2"
  case "$PC_SED_I_MODE" in
    gnu) sed -i "$expr" "$file" ;;
    bsd) sed -i '' "$expr" "$file" ;;
    *)
      echo "ERROR: pc_sed_i called before pc_preflight succeeded" >&2
      return 1
      ;;
  esac
}

# --- Phase 2: stat mtime/size, date offset -----------------------------

PC_STAT_MODE=""

# _pc_detect_stat_mode — try GNU stat's flag style, then BSD's, against a
# throwaway scratch file (never the caller's real file — a probe must not
# depend on that file's content or even continuing to exist), and cache
# whichever one actually printed a number. Lazy + cached: the first
# pc_stat_mtime/pc_stat_size call in a process pays for this, every call
# after that doesn't.
_pc_detect_stat_mode() {
  [[ -n "$PC_STAT_MODE" ]] && return 0
  local scratch
  scratch="$(mktemp 2>/dev/null)" || {
    echo "ERROR: platform-compat stat probe could not create a scratch file (mktemp failed)" >&2
    return 1
  }
  if stat -c %Y "$scratch" >/dev/null 2>&1; then
    PC_STAT_MODE="gnu"
  elif stat -f %m "$scratch" >/dev/null 2>&1; then
    PC_STAT_MODE="bsd"
  fi
  rm -f "$scratch"
  if [[ -z "$PC_STAT_MODE" ]]; then
    echo "ERROR: no usable 'stat' found on this PATH — neither GNU (stat -c) nor BSD (stat -f) flag style worked." >&2
    return 1
  fi
  return 0
}

# pc_stat_mtime FILE — print FILE's mtime as a Unix epoch integer, return 0.
# On any failure (FILE missing, or stat unusable on this host) prints
# nothing and returns 1 — deliberately no built-in "|| echo 0". A silent
# fallback to epoch 0 is the exact D#2263 Phase 2 bug: it doesn't crash, it
# just makes every derived age wrong. Callers decide their own safe
# default for their own situation.
pc_stat_mtime() {
  local file="$1"
  [[ -e "$file" ]] || return 1
  _pc_detect_stat_mode || return 1
  case "$PC_STAT_MODE" in
    gnu) stat -c %Y "$file" 2>/dev/null ;;
    bsd) stat -f %m "$file" 2>/dev/null ;;
  esac
}

# pc_stat_size FILE — print FILE's size in bytes, return 0. Same
# no-silent-fallback contract as pc_stat_mtime.
pc_stat_size() {
  local file="$1"
  [[ -e "$file" ]] || return 1
  _pc_detect_stat_mode || return 1
  case "$PC_STAT_MODE" in
    gnu) stat -c %s "$file" 2>/dev/null ;;
    bsd) stat -f %z "$file" 2>/dev/null ;;
  esac
}

# pc_date_offset BASE_DATE DAYS — print the date DAYS days away from
# BASE_DATE (BASE_DATE and the output are both '%Y-%m-%d'), return 0. DAYS
# is a signed integer, e.g. -1 or 7. Tries GNU `date -d` first, then BSD
# `date -j -f ... -v`. Both branches are actually executed — this is not a
# uname check — so a Linux host missing GNU date's -d support would fall
# through to try the BSD form too, same as a real BSD host would.
pc_date_offset() {
  local base_date="$1" days="$2" out
  if out=$(date -d "${base_date} ${days} day" '+%Y-%m-%d' 2>/dev/null); then
    printf '%s\n' "$out"
    return 0
  fi
  if out=$(date -j -f '%Y-%m-%d' -v"${days}d" "$base_date" '+%Y-%m-%d' 2>/dev/null); then
    printf '%s\n' "$out"
    return 0
  fi
  echo "ERROR: pc_date_offset could not compute a date offset on this host — neither GNU 'date -d' nor BSD 'date -j -v' worked for '$base_date' $days day(s)." >&2
  return 1
}
