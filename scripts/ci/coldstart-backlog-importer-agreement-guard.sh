#!/usr/bin/env bash
# scripts/ci/coldstart-backlog-importer-agreement-guard.sh — CI gate for D#2451.
#
# scripts/lib/coldstart-backlog.sh used to restate what
# scripts/import-epic-tasks.py would do with a backlog directory: its own
# frontmatter parse, its own required-field set, its own status filter. Five
# separate defects were found in that shape in one afternoon, every one a
# divergence between the restatement and the thing restated, and every one
# with a green test suite (D#2451) — because tests/test_coldstart_backlog_
# template.sh is a `tests/` path, and CI runs no pytest and no general bash
# suite (D#2443): the only `tests/` path ci.yml invokes is
# tests/test_merge_and_hook.sh. So that suite has been gating nothing.
#
# This guard is the fix for THAT half of the problem: it is not a better test
# file, it is the same comparison wired into scripts/ci/, which run-guards.sh
# actually runs on every PR (see scripts/ci/run-guards.sh and D#2339). Discovery
# is a plain directory listing — adding this file is the whole of "wiring the
# guard in"; see D#2451 item 10 for why nothing in .github/workflows/ changes.
#
# Two independent things this module answers, checked two different ways
# ------------------------------------------------------------------------
# _coldstart_backlog_importable_count answers "how many task files would the
# importer act on" by asking the importer directly. agree() checks that by
# diffing the module's count against a direct `import-epic-tasks.py --dry-run`
# invocation on the same tree — a genuine two-path comparison, not a
# tautology: the module's function builds its own throwaway proxy directory
# and shells out on its own, so a bug in that plumbing (wrong proxy path,
# wrong repo-path argument, a symlink that does not get created) would show
# up as a disagreement even though both paths ultimately call the same
# importer script.
#
# coldstart_backlog_classify answers a DIFFERENT question this function does
# not touch at all: is this backlog well-formed enough to call "conforming",
# per _COLDSTART_BACKLOG_REQUIRED_FIELDS and the -L-following finds that back
# it. classify_is() checks that by asserting the classify() verdict directly.
#
# This split exists because an earlier version of this file conflated the
# two: it had fixtures labelled "D#2451 item 1" (required-field shrink) and
# "D#2451 item 8" (symlinked epic directory) that only ever called agree(),
# i.e. only ever exercised _coldstart_backlog_importable_count. That function
# never reads _COLDSTART_BACKLOG_REQUIRED_FIELDS and never runs a bash `find`
# over the fixture tree at all (it hands the whole tree to the importer via a
# proxy symlink and lets Python's own glob/is_dir follow it) — so it has zero
# power to detect a regression in either the required-field list or the `-L`
# fix, no matter how the fixture is built. Confirmed by reverting each in
# isolation (only the field list; only the `-L` additions) on a copy of this
# tree: agree() stayed green both times, 7 checked/0 failed, while calling
# coldstart_backlog_classify() directly on the same fixture flipped from
# "conforming" to "incomplete" (field-list revert) or "empty" (find revert).
# classify_is() is what actually exercises the code path that flipped.
#
# It also pins the importer's output *format* on one fixture with a
# hand-verified expected count (D#2451 item 4): if `After status filter: N`
# ever stops appearing in that shape, this fixture's assertion goes red
# specifically, distinguishing "the format changed" from "a real count
# regressed".
#
# Discovering nothing to compare is a FAILURE (D#2451 item 12), not a silent
# pass — see the CHECKED -eq 0 check near the bottom.
#
# A note on a bug this file used to have, for whoever edits it next: an
# earlier draft used a `mkfixture() { d="$(mktemp -d)"; FIXTURES+=("$d"); }`
# helper called as `D="$(mkfixture)"` at each call site, copied from
# tests/test_coldstart_backlog_template.sh's own helper. Wrapping a function
# that appends to a global array in `$(...)` runs it in a forked subshell, so
# every FIXTURES append was silently lost the moment that subshell exited —
# cleanup's `for d in "${FIXTURES[@]:-}"` then iterated an EMPTY array, whose
# `:-` fallback supplies one empty-string element, so it deleted nothing.
# Harmless here (only means leftover /tmp dirs), but it is the same
# restatement-drift shape this Discussion is about — a helper's own behaviour
# silently diverging from what the code calling it assumed — so fixtures are
# created inline below instead, in the same shell as the array append.
#
# Run: bash scripts/ci/coldstart-backlog-importer-agreement-guard.sh
# Exit 0: every assertion agrees with its independently-checked ground truth.
# Exit 1: a disagreement, or zero fixtures were compared.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKLOG_LIB="$REPO_ROOT/scripts/lib/coldstart-backlog.sh"
IMPORTER="$REPO_ROOT/scripts/import-epic-tasks.py"

FIXTURES=()
CHECKED=0
FAILED=0

cleanup() {
  local d
  for d in ${FIXTURES[@]+"${FIXTURES[@]}"}; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

if [[ ! -f "$BACKLOG_LIB" ]]; then
  echo "coldstart-backlog-importer-agreement-guard: FAIL — $BACKLOG_LIB not found" >&2
  exit 1
fi
if [[ ! -f "$IMPORTER" ]]; then
  echo "coldstart-backlog-importer-agreement-guard: FAIL — $IMPORTER not found" >&2
  exit 1
fi
# shellcheck source=scripts/lib/coldstart-backlog.sh
source "$BACKLOG_LIB"

# $1 label, $2 root (holds root/epics), $3 host note
#
# Calls _coldstart_backlog_importable_count_into_globals directly (no
# `x="$(...)"`) for the same reason coldstart_backlog_step's conforming
# branch does: command substitution forks a subshell, and this function's
# _COLDSTART_BACKLOG_REFUSAL_REASON on a refusal would never reach this
# scope if it were set inside one.
agree() {
  local label="$1" root="$2" host="$3" mine theirs
  CHECKED=$((CHECKED + 1))
  if _coldstart_backlog_importable_count_into_globals "$root/epics"; then
    mine="$_COLDSTART_BACKLOG_LAST_COUNT"
  else
    echo "FAIL: $label — module refused: $_COLDSTART_BACKLOG_REFUSAL_REASON (fixture: $root, host: $host)"
    FAILED=$((FAILED + 1))
    return
  fi
  theirs="$(python3 "$IMPORTER" "$root" --repo example-org/example-project --dry-run 2>/dev/null \
            | sed -n 's/^After status filter: \([0-9]*\) task.*/\1/p' | head -n 1)"
  [[ -n "$theirs" ]] || theirs=0
  if [[ "$mine" == "$theirs" ]]; then
    echo "PASS: $label (module=$mine importer=$theirs, fixture: $root, host: $host)"
  else
    echo "FAIL: $label — module says $mine, importer says $theirs (fixture: $root, host: $host)"
    FAILED=$((FAILED + 1))
  fi
}

# $1 label, $2 root (holds root/epics), $3 expected classify() verdict,
# $4 whether the importer should say it would create something from this
# tree ("yes"/"no" — the ground truth that justifies $3, checked
# independently rather than asserted from nowhere), $5 host note.
#
# Exercises coldstart_backlog_classify() directly — the function
# _COLDSTART_BACKLOG_REQUIRED_FIELDS and the `-L` fixes actually live in.
# agree() above cannot stand in for this: _coldstart_backlog_importable_count
# never reads the required-field list and never runs a bash `find` at all.
classify_is() {
  local label="$1" root="$2" expected="$3" should_act="$4" host="$5" raw acted got
  CHECKED=$((CHECKED + 1))
  raw="$(python3 "$IMPORTER" "$root" --repo example-org/example-project --dry-run 2>/dev/null)"
  if grep -qF "would create Discussion" <<<"$raw"; then acted="yes"; else acted="no"; fi
  if [[ "$acted" != "$should_act" ]]; then
    echo "FAIL: $label — fixture does not corroborate what it claims: importer would-create is '$acted', fixture was built to be '$should_act' (fixture: $root, host: $host)"
    FAILED=$((FAILED + 1))
    return
  fi
  got="$(coldstart_backlog_classify "$root/epics")"
  if [[ "$got" == "$expected" ]]; then
    echo "PASS: $label classifies '$got', agreeing with the importer's own would-create signal ($acted) (fixture: $root, host: $host)"
  else
    echo "FAIL: $label classifies '$got', expected '$expected' (importer would-create: $acted) (fixture: $root, host: $host)"
    FAILED=$((FAILED + 1))
  fi
}

mk_task() {
  # $1 dest path, $2 status text verbatim
  local dest="$1" status="$2"
  printf -- '---\nepic: 3\ntask: 1\ntitle: "Task 1"\ntype: feature\nstatus: %s\n---\n\n# Task 1\n' "$status" >"$dest"
}

HOST="$(uname -a 2>/dev/null || echo unknown)"

echo "=== fixture: ordinary conforming backlog, one open task ==="
D="$(mktemp -d)"; FIXTURES+=("$D")
mkdir -p "$D/epics/epic-3-billing"
mk_task "$D/epics/epic-3-billing/01.md" "not-started"
agree "ordinary conforming backlog" "$D" "$HOST"

echo ""
echo "=== fixture: all-completed backlog (agreed zero, not asserted zero) ==="
D="$(mktemp -d)"; FIXTURES+=("$D")
mkdir -p "$D/epics/epic-3-billing"
mk_task "$D/epics/epic-3-billing/01.md" "completed"
agree "all-completed backlog" "$D" "$HOST"

echo ""
echo "=== fixture: minimal frontmatter — status only, D#2451 item 1 ==="
# The required-field-set divergence: the old module required epic/task/
# title/type/status to call a file conforming; the importer only ever
# needed status (everything else falls back to '?'/'untitled'). Checked via
# classify_is() (which is what actually reads _COLDSTART_BACKLOG_REQUIRED_
# FIELDS), not agree() -- see the header comment for why agree() alone would
# not have caught this. agree() also runs here, because a minimal file is
# still a fine sanity check on the importable-count plumbing; it is just not
# the assertion that covers item 1.
D="$(mktemp -d)"; FIXTURES+=("$D")
mkdir -p "$D/epics/epic-3-billing"
printf -- '---\nstatus: not-started\n---\n\n# minimal\n' >"$D/epics/epic-3-billing/01.md"
classify_is "minimal frontmatter (status only)" "$D" "conforming" "yes" "$HOST"
agree "minimal frontmatter (status only)" "$D" "$HOST"

echo ""
echo "=== fixture: symlinked epic directory, D#2451 item 8 ==="
# The destructive one: a backlog reachable only through a symlinked epic-*
# directory used to read as having no task files, then get an example
# scaffolded next to it. Real content lives OUTSIDE the fixture root on
# purpose, reached only via the symlink -- the case that would actually have
# been destroyed. Checked via classify_is(): _coldstart_backlog_importable_
# count hands the whole tree to the importer through its own proxy symlink
# and never runs a bash `find` over it, so it cannot see whether THIS
# module's `-L` fixes are present or not -- confirmed by reverting only the
# `-L` additions and watching agree() on this exact fixture stay green while
# coldstart_backlog_classify() flips from "conforming" to "empty". agree()
# still runs here too, as a sanity check on the proxy-symlink plumbing
# _coldstart_backlog_importable_count itself does.
REAL="$(mktemp -d)"; FIXTURES+=("$REAL")
mkdir -p "$REAL/epic-3-billing"
mk_task "$REAL/epic-3-billing/01.md" "not-started"
D="$(mktemp -d)"; FIXTURES+=("$D")
mkdir -p "$D/epics"
ln -s "$REAL/epic-3-billing" "$D/epics/epic-3-billing"
classify_is "symlinked epic directory" "$D" "conforming" "yes" "$HOST"
agree "symlinked epic directory" "$D" "$HOST"

echo ""
echo "=== fixture: tab before a trailing comment, D#2451 item 7c ==="
# 'status: not-started<TAB># comment' is invalid in a position PyYAML
# rejects, so the importer's parse_frontmatter fails closed on the WHOLE
# file (fm={}) and the status filter drops it -- 0. The old bash sed/grep
# parser did not care about YAML validity and still extracted "not-started",
# reporting 1. Routing through the importer (this module's current
# implementation) makes them agree by construction. Confirmed load-bearing:
# reverting only _coldstart_backlog_importable_count's body back to the old
# sed parser (keeping the required-field and -L fixes) makes agree() fail
# exactly here (module says 1, importer says 0) while every other agree()
# fixture in this file still passes.
D="$(mktemp -d)"; FIXTURES+=("$D")
mkdir -p "$D/epics/epic-3-billing"
printf -- '---\nepic: 3\ntask: 1\ntitle: "Task 1"\ntype: feature\nstatus: not-started\t# flip when done\n---\n\n# Task 1\n' \
  >"$D/epics/epic-3-billing/01.md"
agree "tab before a trailing comment" "$D" "$HOST"

echo ""
echo "=== fixture: mixed backlog, pinned output-format canary, D#2451 item 4 ==="
# Parsing 'After status filter: N' out of the importer's stdout is itself a
# restatement of its OUTPUT FORMAT. This fixture pins that format: two tasks
# are hand-verified importable (not-started, in_progress) and two are not
# (completed, superseded), so the importer's own line is asserted to read
# EXACTLY "After status filter: 2 task(s) to process" -- not just "the
# regex matched something". If the importer's wording ever changes, this
# assertion goes red specifically, which is the signal item 4 asks for.
D="$(mktemp -d)"; FIXTURES+=("$D")
mkdir -p "$D/epics/epic-3-billing"
mk_task "$D/epics/epic-3-billing/01.md" "not-started"
mk_task "$D/epics/epic-3-billing/02.md" "in_progress"
mk_task "$D/epics/epic-3-billing/03.md" "completed"
mk_task "$D/epics/epic-3-billing/04.md" "superseded"
RAW="$(python3 "$IMPORTER" "$D" --repo example-org/example-project --dry-run 2>/dev/null)"
CHECKED=$((CHECKED + 1))
if grep -qF "After status filter: 2 task(s) to process" <<<"$RAW"; then
  echo "PASS: importer output format is pinned (fixture: $D, host: $HOST)"
else
  echo "FAIL: importer output format changed -- expected the literal line" \
       "'After status filter: 2 task(s) to process', got:"
  grep -E '^After status filter' <<<"$RAW" || echo "  (no 'After status filter' line at all)"
  FAILED=$((FAILED + 1))
fi
agree "mixed backlog" "$D" "$HOST"

echo ""
echo "=============================================="
if [[ "$CHECKED" -eq 0 ]]; then
  # D#2451 item 12: comparing nothing is a failure, not a silent pass. A
  # guard that goes green when its subject disappears (an empty fixture set,
  # a helper that stopped being sourced) is the exact shape this repo hit
  # nine times on 2026-09-06.
  echo "coldstart-backlog-importer-agreement-guard: FAIL — zero fixtures were compared; a guard that compares nothing cannot vouch for agreement" >&2
  exit 1
fi

echo "coldstart-backlog-importer-agreement-guard: $CHECKED checked, $FAILED failed"
if [[ "$FAILED" -gt 0 ]]; then
  echo "coldstart-backlog-importer-agreement-guard: FAIL" >&2
  exit 1
fi
echo "coldstart-backlog-importer-agreement-guard: OK"
exit 0
