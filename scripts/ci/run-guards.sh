#!/usr/bin/env bash
# scripts/ci/run-guards.sh — run every behavioral guard in scripts/ci/ (D#2339 PR-b).
#
# Why this exists
# ---------------
# Every behavioral guard this team writes used to land as one more step in the
# same `backend (import-smoke)` job in .github/workflows/ci.yml. Five
# guard-adding PRs in roughly one hour, three of them conflicting on those
# exact lines. The mechanical conflict is annoying; the dangerous part is the
# resolution. The conflict is two steps competing for one insertion point, so
# taking one side drops the other — and nothing fails. The job still runs, the
# PR still merges, and that guard is simply gone with no signal.
#
# With this runner the workflow carries one guard step. Adding a guard is
# adding a file to scripts/ci/ and touching no YAML, so there is no shared
# insertion point left to conflict on and no step for a conflict resolution
# to drop.
#
# What it does NOT do
# -------------------
# It contains no guard logic. It discovers, dispatches, times, and reports.
# Every judgement about the codebase lives in the guard files themselves.
#
# Two properties are load-bearing, and each is asserted by
# scripts/ci/guard-registry-check.py rather than left to good intentions:
#
#   1. Per-guard attribution survives. One opaque "guards failed" would be
#      worse than the nineteen separate steps it replaces, so every guard gets
#      its own PASS/FAIL line and the final line names every failure by
#      filename. Every guard runs even after an earlier one fails, so a PR
#      that breaks two guards learns about both in one CI round.
#
#   2. Discovering nothing is a FAILURE, not a pass. A runner that finds no
#      guards and exits 0 is the silent skip this whole Discussion exists to
#      remove — it would report every guard fine while running none of them.
#      Same reasoning for a file this runner cannot dispatch: it fails and
#      names the file rather than skipping past it.
#
# Discovery is a plain directory listing, deliberately NOT a mode-bit filter.
# Only some files here carry the executable bit while all of them are invoked
# as `python3 <path>` or `bash <path>`, so a mode-based subject set would find
# a handful of guards and silently miss the rest. Dispatch is on the extension
# for the same reason.
#
# Two kinds of file in scripts/ci/ are not run here, both declared in
# scripts/ci/guard-ledger.json with a reason a reviewer can check:
#
#   exempt    — not run anywhere at all (and guard-registry-check.py fails if
#               such a file turns out to be wired after all).
#   own_step  — invoked directly by a workflow because it needs something this
#               runner cannot give it (a PR event payload, an unshallow
#               checkout, or a step-level `if:`). guard-registry-check.py
#               fails if such a file is NOT actually referenced by a workflow,
#               so the ledger cannot lie in that direction either.
#
# Both kinds are PRINTED at the top of every run as `SKIP <file> — ledgered
# '<section>': <reason>`. What the checker cannot catch is a live guard being
# moved into the ledger with a plausible-sounding reason: that is a reviewable
# one-line edit either way, and it was a reviewable one-line edit before this
# runner existed too. Printing it is what keeps it from being an absence
# nobody can see in the log.
#
# Usage
# -----
#   bash scripts/ci/run-guards.sh            # run every discovered guard
#   bash scripts/ci/run-guards.sh --list     # print the discovered set, run nothing
#   bash scripts/ci/run-guards.sh --dir DIR  # discover in DIR instead of scripts/ci/
#
# --dir changes exactly one thing: which directory is listed. Discovery,
# ledger loading, dispatch, reporting and every exit code below are the same
# code on the same path, so a --dir run is evidence about the real run (D#2149).
#
# Exit 0: every discovered guard passed.
# Exit 1: a guard failed, a file could not be dispatched, or discovery was empty.
# Exit 2: usage error.

# NOT `set -e`: a failing guard must not abort the loop — running the rest is
# the whole point of item 2 above. `pipefail` so a guard's exit status is not
# swallowed by a pipeline.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD_DIR="$REPO_ROOT/scripts/ci"
SELF_NAME="$(basename "${BASH_SOURCE[0]}")"
LEDGER_NAME="guard-ledger.json"
MODE="run"

while [ $# -gt 0 ]; do
  case "$1" in
    --list) MODE="list"; shift ;;
    --dir)
      if [ $# -lt 2 ]; then
        echo "run-guards: --dir needs a directory" >&2
        exit 2
      fi
      GUARD_DIR="$2"; shift 2 ;;
    *)
      echo "usage: $SELF_NAME [--list] [--dir DIR]" >&2
      exit 2 ;;
  esac
done

if [ ! -d "$GUARD_DIR" ]; then
  echo "run-guards: FAIL — $GUARD_DIR is not a directory" >&2
  exit 1
fi
# Resolve before the cd below, so a relative --dir still means what the caller
# meant by it.
GUARD_DIR="$(cd "$GUARD_DIR" && pwd)"

# Every guard used to be its own workflow step, and a workflow step runs at the
# repo root. Working directory is part of what the guards were getting from the
# hub, so the runner hands them the same one rather than passing on whatever
# directory it happened to be invoked from.
cd "$REPO_ROOT" || exit 1

# The ledger lives beside the guards, so a --dir fixture gets its own (usually
# absent, which means "exclude nothing" — correct for a fixture directory).
# Emits one "<name><TAB><section><TAB><reason>" line per excluded file: the
# reason is carried out of the ledger so the run can PRINT it. A guard that
# stops running has to be visible in the log as a decision somebody wrote
# down, not as an absence — the whole defect this Discussion is about is a
# guard disappearing with no signal anywhere.
ledger_excluded() {
  local ledger="$GUARD_DIR/$LEDGER_NAME"
  [ -f "$ledger" ] || return 0
  python3 - "$ledger" <<'PY'
import json, sys
try:
    raw = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as exc:
    # Unreadable ledger: exclude nothing and say so. Excluding nothing makes
    # the runner try to run everything, which is loud; silently excluding
    # everything would be the failure mode this file exists to prevent.
    print(f"run-guards: WARN — could not read the ledger: {exc}", file=sys.stderr)
    raise SystemExit(0)
if not isinstance(raw, dict):
    raise SystemExit(0)
rows = {}
for key in ("exempt", "own_step"):
    section = raw.get(key)
    if isinstance(section, dict):
        for name, reason in section.items():
            rows[name] = (key, str(reason).strip().replace("\t", " "))
for name in sorted(rows):
    key, reason = rows[name]
    print(f"{name}\t{key}\t{reason}")
PY
}

LEDGER_ROWS="$(ledger_excluded)"
EXCLUDED=" $LEDGER_NAME $SELF_NAME $(printf '%s\n' "$LEDGER_ROWS" | cut -f1 | tr '\n' ' ') "

GUARDS=()
while IFS= read -r path; do
  name="$(basename "$path")"
  case "$EXCLUDED" in *" $name "*) continue ;; esac
  GUARDS+=("$name")
done < <(find "$GUARD_DIR" -maxdepth 1 -type f -print | sort)

if [ "$MODE" = "list" ]; then
  for name in ${GUARDS[@]+"${GUARDS[@]}"}; do
    echo "$name"
  done
  echo "count: ${#GUARDS[@]}"
  exit 0
fi

# Say out loud which files were NOT run and why, before running anything.
# guard-registry-check.py enforces that each of these reasons is non-empty and
# points the right way, but only a human reading the log can notice that a
# guard which used to run is now sitting in this list.
if [ -n "$LEDGER_ROWS" ]; then
  while IFS=$'\t' read -r name section reason; do
    [ -n "$name" ] || continue
    echo "SKIP $name — ledgered '$section': $reason"
  done <<< "$LEDGER_ROWS"
  echo
fi

# Empty discovery is a failure. See property 2 in the header.
if [ "${#GUARDS[@]}" -eq 0 ]; then
  echo "run-guards: FAIL — discovered zero guards in $GUARD_DIR; a guard runner that runs nothing gates nothing" >&2
  exit 1
fi

FAILED=()
PASSED=0

for name in "${GUARDS[@]}"; do
  path="$GUARD_DIR/$name"
  case "$name" in
    *.py) cmd=(python3 "$path") ;;
    *.sh) cmd=(bash "$path") ;;
    *)
      # Not a skip. A file nobody can dispatch is a guard nobody runs.
      echo "--- $name"
      echo "run-guards: FAIL — $name has no runnable extension (.py or .sh); add one, or ledger it in scripts/ci/$LEDGER_NAME with a reason" >&2
      FAILED+=("$name")
      continue ;;
  esac

  echo "--- $name"
  start=$(date +%s%N)
  "${cmd[@]}"
  rc=$?
  elapsed=$(( ($(date +%s%N) - start) / 100000000 ))
  secs="$(( elapsed / 10 )).$(( elapsed % 10 ))"

  if [ "$rc" -eq 0 ]; then
    echo "PASS $name (${secs}s)"
    PASSED=$(( PASSED + 1 ))
  else
    echo "FAIL $name (${secs}s) exit=$rc"
    FAILED+=("$name")
  fi
done

echo
SKIPPED=0
[ -n "$LEDGER_ROWS" ] && SKIPPED=$(printf '%s\n' "$LEDGER_ROWS" | grep -c .)
echo "run-guards: ${#GUARDS[@]} guard(s) in ${GUARD_DIR#"$REPO_ROOT"/}, $PASSED passed, ${#FAILED[@]} failed, $SKIPPED ledgered as not-run"

if [ "${#FAILED[@]}" -gt 0 ]; then
  # Last line names every failure. An aggregate with no filename would make
  # this runner worse than the per-step form it replaces.
  echo "run-guards: FAIL — $(printf '%s, ' "${FAILED[@]}" | sed 's/, $//')" >&2
  exit 1
fi

echo "run-guards: OK"
