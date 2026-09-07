#!/usr/bin/env bash
# scripts/check-tests-leave-tree-clean.sh
#
# Runs a pytest target and fails when the run leaves NEW paths behind in the
# working tree. The defect it exists to catch is a test that writes into the
# tree it is measuring, so that a later test's verdict is decided by residue
# from an earlier one rather than by the code (D#2453).
#
# The concrete case that prompted it: three separate agents in one day hit
# `backend/tests/test_state_paths_absolute.py::test_fresh_scratch_dir_beats_
# legacy_audit_path` and each spent time establishing the failure was not
# theirs. That test asserted an in-repo `.autonomous-team/` existed. It does
# not exist on a fresh clone of this repo — nothing under it is tracked — so
# the test only passed because earlier tests in the same run had created the
# directory as a side effect of writing `circuit-breaker-history.jsonl`,
# `kpi.json` and `parity-history.jsonl` into it.
#
# WHERE THIS RUNS, AND WHERE IT DOES NOT
# ---------------------------------------
# Local only. No CI job runs pytest at all (D#2443), and `backend/tests/` is
# excluded from CI by design (D#1477), so there is no CI build for this to
# gate and none is added here. It is deliberately NOT in `scripts/ci/`: a
# guard placed there runs on every CI build of the `backend (import-smoke)`
# job, where it would have no suite to observe and would only add runtime.
# Run it by hand, or from a pre-push hook if you want it enforced locally.
#
# WHY NOT PLAIN `git status --porcelain`
# ---------------------------------------
# Because on this repo it reports nothing. `.gitignore` names individual
# files under `.autonomous-team/` rather than the directory, so every path
# the residue lands in is an ignored path. Measured on a fresh clone of the
# code plane at d29b6088, host `jp`, after a full `backend/tests` run that
# had just created six paths under `.autonomous-team/`: plain
# `git status --porcelain` printed zero lines. The snapshot below therefore
# uses `--ignored=matching --untracked-files=all`, which is what makes the
# residue visible at all.
#
# A VERDICT REQUIRES AN OBSERVATION
# ----------------------------------
# The comparison below can be satisfied by an empty subject set: a run that
# executed no tests adds no paths, so "nothing was added" comes out green.
# That is the same defect this check exists to catch, sitting in the check —
# a result that reports something other than what its reader assumes. Code
# review on the first version caught it, having reproduced it three ways:
# the DEFAULT invocation on a checkout with no `.autonomous-team/config.json`
# (collection errors, pytest exit 2, zero tests run), a typo'd target
# filename (exit 4), and a `-k` expression matching nothing (exit 5). All
# three reported a clean tree.
#
# So the run is validated before the difference is interpreted, on two
# independent grounds, because either alone can be satisfied vacuously:
#
#   1. pytest's exit status must be 0 or 1. Both are real runs — 1 is tests
#      failing, which is still an observation of what a run does to the tree.
#      2 (collection error), 3 (internal error), 4 (usage error) and 5 (no
#      tests collected) all mean no run happened, and neither a pass nor a
#      fail can be issued from one.
#   2. at least one test case must actually have executed, counted from a
#      JUnit XML written to a scratch path outside the repo. This is the
#      backstop for an exit status that says 0 while nothing ran; it is not
#      redundant with (1), it is the same rule stated positively about the
#      subject set rather than about the exit code.
#
# Neither gate subsumes the other, and the collection-error case shows why:
# pytest writes a JUnit `<testcase>` entry for each collection ERROR, so the
# default-invocation repro above counts 3 executed cases while having run
# nothing at all. The exit status is what catches that one; the count is what
# would catch its mirror image.
#
# Neither gate reports "clean". A run that did not happen gets "could not
# observe", which is a failure, because the alternative is a green result
# that means nothing.
#
# WHY A DIFFERENCE, NOT A CLEANLINESS ASSERTION
# ----------------------------------------------
# The working tree here is *already* dirty on every real checkout —
# `.autonomous-team/config.json` is modified and uncommitted on the operator
# box, and untracked runtime state is normal. A check that demanded a clean
# tree would be red on day one everywhere and would get disabled, which is
# worse than the gap it was guarding. So this compares the before-set against
# the after-set and fails only on what the run ADDED or CHANGED.
#
# WHAT IT TOLERATES, STATED RATHER THAN HIDDEN
# ---------------------------------------------
# Interpreter bytecode (`__pycache__/`, `*.pyc`) and pytest's own
# `.pytest_cache/` are reported but not fatal — CPython writes them for any
# import and no test authored them. They are always PRINTED when observed, so
# a reader can see what was excluded instead of having to read this comment
# to find out. Everything else the run added is a failure.
#
# That tolerance is deliberately NOT blanket, because a first draft of it
# hid a real finding. A `.pyc` under a directory the run itself CREATED is
# not interpreter noise — the directory is the residue, whatever happens to
# be inside it. Measured case: `backend/spawn_diff.py` writes its temporary
# module into `.autonomous-team/` *only if that directory already exists*,
# and removes the `.py` but not the `__pycache__/*.pyc`. Blanket tolerance
# would have reported `OK` on a run that had just created an untracked
# `.autonomous-team/` — the exact shape this check exists to catch. So
# bytecode is tolerated only under a top-level path git already tracks (plus
# a `__pycache__/` or `.pytest_cache/` at the repo root itself); anywhere
# else it is fatal.
#
# Related but different in mechanism: `scripts/check-tests-live-state-paths.sh`
# is a STATIC lint over `tests/*.sh` that reads source for live-tree path
# literals. This one is DYNAMIC and Python-side — it runs the suite and looks
# at what actually appeared on disk, which is the only way to catch a write
# that goes through a resolver rather than a literal.
#
# Usage:
#   scripts/check-tests-leave-tree-clean.sh                  # backend/tests
#   scripts/check-tests-leave-tree-clean.sh backend/tests/test_kpi_engine.py
#   scripts/check-tests-leave-tree-clean.sh -k some_expression backend/tests
#
# Every argument is passed straight through to pytest.
#
# Exit status: 0 when a real run added nothing (or only tolerated cache
# paths), 1 when it added something — and also 1 when no run happened at all,
# which is not a pass. See "A VERDICT REQUIRES AN OBSERVATION" above.

set -uo pipefail

# `comm` requires its two inputs to be sorted in ITS OWN collation order, so
# the sort below and the comm further down have to agree. They did not on the
# first real run of this script: sorting under LC_ALL=C while comm ran under
# the ambient locale produced "comm: file 1 is not in sorted order" and a
# silently wrong difference that dropped the `.autonomous-team/` entries this
# check exists to report. Pinning the locale for the whole script is what
# keeps the two in step.
export LC_ALL=C

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "FAIL: not a git repository — this check compares git status before and after a run" >&2
  exit 1
fi

PYTEST_TARGET=("$@")
if [ ${#PYTEST_TARGET[@]} -eq 0 ]; then
  PYTEST_TARGET=("backend/tests")
fi

# The suite must not write to the production state dir while we are measuring
# what it writes to the tree. backend/state_paths.py refuses to resolve at all
# under pytest with this unset, so leaving it unset would not even get us a
# run. Point it at a scratch dir unless the caller already chose one.
SCRATCH_STATE_DIR=""
if [ -z "${AUTONOMOUS_TEAM_STATE_DIR:-}" ]; then
  SCRATCH_STATE_DIR="$(mktemp -d)"
  export AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR"
  echo "note: AUTONOMOUS_TEAM_STATE_DIR was unset — using scratch dir $SCRATCH_STATE_DIR"
fi

snapshot() {
  # --ignored=matching lists ignored paths individually instead of collapsing
  # them away entirely; -uall does the same for untracked ones. Both are load
  # bearing here — see the header comment.
  git status --porcelain=v1 --ignored=matching --untracked-files=all | sort
}

BEFORE_FILE="$(mktemp)"
AFTER_FILE="$(mktemp)"
JUNIT_FILE="$(mktemp)"   # outside the repo, so it is never itself residue
cleanup() {
  rm -f "$BEFORE_FILE" "$AFTER_FILE" "$JUNIT_FILE"
  [ -n "$SCRATCH_STATE_DIR" ] && rm -rf "$SCRATCH_STATE_DIR"
  return 0
}
trap cleanup EXIT

snapshot > "$BEFORE_FILE"
echo "before: $(wc -l < "$BEFORE_FILE" | tr -d ' ') tracked-or-ignored status entries"

echo "running: pytest ${PYTEST_TARGET[*]}"
python3 -m pytest "${PYTEST_TARGET[@]}" "--junitxml=$JUNIT_FILE"
PYTEST_RC=$?

snapshot > "$AFTER_FILE"
echo "after:  $(wc -l < "$AFTER_FILE" | tr -d ' ') tracked-or-ignored status entries"

# Lines present after but not before: paths the run added, or whose status
# it changed. comm needs both inputs sorted, which snapshot() guarantees.
ADDED="$(comm -13 "$BEFORE_FILE" "$AFTER_FILE")"

# ---------------------------------------------------------------------------
# Validate the run before interpreting the difference. See "A VERDICT REQUIRES
# AN OBSERVATION" in the header: an empty run adds nothing, so without this
# the check is at its most confident exactly when it saw the least.
# ---------------------------------------------------------------------------

EXECUTED=0
if [ -s "$JUNIT_FILE" ]; then
  EXECUTED="$(grep -o '<testcase' "$JUNIT_FILE" | wc -l | tr -d ' ')"
fi

no_observation() {
  echo >&2
  echo "FAIL: no observation was made, so no verdict can be issued." >&2
  echo "  $1" >&2
  echo "  pytest exit status: $PYTEST_RC; test cases executed: $EXECUTED" >&2
  if [ -n "$ADDED" ]; then
    echo "  (the tree did change during this non-run — reported for information," >&2
    echo "   but it is not a verdict about what a real run does:)" >&2
    printf '%s' "$ADDED" | sed 's/^/    /' >&2
  fi
  exit 1
}

case "$PYTEST_RC" in
  0|1) ;;   # a real run: everything passed, or tests failed. Both are observations.
  2) no_observation "pytest exit 2 — collection error. Nothing ran." ;;
  3) no_observation "pytest exit 3 — internal error. Nothing ran." ;;
  4) no_observation "pytest exit 4 — usage error (a mistyped target reaches here). Nothing ran." ;;
  5) no_observation "pytest exit 5 — no tests were collected." ;;
  *) no_observation "pytest exit $PYTEST_RC — unrecognized; treating as no run." ;;
esac

if [ "$EXECUTED" -lt 1 ]; then
  no_observation "pytest reported success but no test case executed."
fi

echo "pytest exit status: $PYTEST_RC over $EXECUTED test cases (not this check's verdict — residue is)"

if [ -z "$ADDED" ]; then
  echo "OK: pytest ${PYTEST_TARGET[*]} ran $EXECUTED test cases and added nothing to the working tree"
  exit 0
fi

# Interpreter/pytest cache shapes. Matching this is necessary but not
# sufficient to be tolerated — see the containment test below and the header.
CACHE_RE='(^|/)(__pycache__/|\.pytest_cache/)|\.pyc$'

# Top-level names git already tracks something under. A cache path rooted at
# one of these predates the run; a cache path rooted anywhere else means the
# run created that directory tree, which is itself the residue.
TRACKED_TOP="$(git ls-files | cut -d/ -f1 | sort -u)"

FATAL=""
TOLERATED=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  path="${line:3}"
  tolerated=0
  if printf '%s' "$path" | grep -Eq "$CACHE_RE"; then
    top="${path%%/*}"
    if [ "$top" = "__pycache__" ] || [ "$top" = ".pytest_cache" ]; then
      tolerated=1                       # at the repo root itself
    elif printf '%s\n' "$TRACKED_TOP" | grep -Fxq "$top"; then
      tolerated=1                       # under a tree git already tracks
    fi
  fi
  if [ "$tolerated" -eq 1 ]; then
    TOLERATED="${TOLERATED}${line}"$'\n'
  else
    FATAL="${FATAL}${line}"$'\n'
  fi
done <<< "$ADDED"

if [ -n "$TOLERATED" ]; then
  echo
  echo "tolerated (interpreter bytecode / pytest cache — reported, not fatal):"
  printf '%s' "$TOLERATED" | sed 's/^/  /'
fi

if [ -n "$FATAL" ]; then
  echo
  echo "FAIL: pytest ${PYTEST_TARGET[*]} left new paths in the working tree." >&2
  printf '%s' "$FATAL" | sed 's/^/  /' >&2
  echo >&2
  echo "A test that writes into the tree it is measuring makes a later test's" >&2
  echo "verdict depend on the order tests ran in. Point the write at a scratch" >&2
  echo "directory the test owns (tmp_path, or AUTONOMOUS_TEAM_STATE_DIR) instead." >&2
  exit 1
fi

echo
echo "OK: pytest ${PYTEST_TARGET[*]} ran $EXECUTED test cases and added only interpreter/pytest cache paths"
exit 0
