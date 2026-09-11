#!/usr/bin/env bash
# scripts/ci/pr-mutation-evidence.sh — reproduce a PR body's mutation claim,
# don't just assert one was made (D#1984).
#
# WHY THIS EXISTS
#
# Five times in one session, on five different PRs, a test was cited as
# coverage that could not fail for the reason it existed — each was green,
# each was cited as evidence, none tested what its name said. The common
# shape: a proxy for the behaviour (a string's presence, a file's existence,
# an operation that can't report failure) stood in for actually trying to
# break the thing and watching it go red.
#
# The cheaper fix — a PR-body field asserting a mutation claim was WRITTEN
# DOWN — was rejected on exactly that reasoning: it is itself an assertion
# that a string is present, which is the defect class this file exists to
# close. D#1942 is the concrete counter-example: that Spec's mutation table
# was written out in full, in good faith, and was simply wrong; nobody ran it.
#
# So this script does not read a claim. It REPRODUCES one: it applies the
# declared unified diff, runs the declared command on both the clean tree and
# the patched tree, and requires green-then-red. One-sided would be exactly
# as blind as the defect it replaces — a silently-missed `sed` reports a pass
# on a still-green run either way.
#
# WHAT THIS CANNOT CATCH — read before citing this as coverage
#
# This check reproduces ONE of the four failure shapes on record, fully; a
# second, partially; and misses two entirely. It cannot tell a mutation that
# tests the right function from one that mocks the wrong one, and it cannot
# tell a test that asserts correct behaviour from one that asserts a known
# defect as correct. It verifies reproducibility, not relevance, and it says
# nothing about a host shape it did not run on. A declared gap and an
# undeclared one read identically from outside and have opposite
# consequences — so: this script's PASS means "the claimed mutation, on this
# host, in this run, was reproduced." Nothing more.
#
# INPUT — $PR_BODY_FILE IN CI, $PR_BODY ONLY FOR LOCAL USE
#
# Same convention as scripts/ci/pr-link-policy.sh, for the same reason: in CI
# the body is read from $GITHUB_EVENT_PATH into a file and named by
# PR_BODY_FILE, never passed through a step's `env:` block (the runner prints
# that block into the log before the step runs). $PR_BODY is for local runs
# and this file's own test suite only.
#
# THE PR BODY IS UNTRUSTED INPUT
#
# On a public repo the body is written by anyone who can open a PR. Both
# declared fields are handled as data, never as code to interpret blindly:
#   - The diff is written to a private temp file and applied with `git apply`
#     (never `--unsafe-paths`, so git's own refusal to write outside the
#     working tree stays in force) — never concatenated into a shell command.
#   - The command is validated against a fixed allowlist of PREFIXES before
#     it is ever run, and is executed via `bash -c "$COMMAND"` — a single
#     subprocess argument, never `eval`, so a malicious command cannot mutate
#     this script's own variables or exit it early out from under the trap
#     that guarantees the revert.
# The allowlist is deliberately not a sandbox — CLAUDE.md's guardrail framing
# says over-blocking costs more than under-blocking here, and the marginal
# risk is small regardless: CI already executes the PR's own code the moment
# it runs the suite the command names. The allowlist exists to catch an
# ACCIDENT (a copy-pasted destructive command, an empty field silently
# passing), not to contain an attacker.
#
# WHY absence is not failure, and why that is not cowardice: see the Spec.
# In short — a check that goes red on day one for every PR that never knew
# about it is a check somebody disables, and fail-on-false-claim with
# pass-on-silence makes lying strictly worse than saying nothing, which is
# the right ordering while adoption of the block is voluntary.
#
# TOOL PRESENCE — CHECKED BEFORE A DECLARED COMMAND IS EVER RUN (D#2537)
#
# This gate is required, but for weeks the job it ran in had no dependency
# install step: `pytest` was declared in requirements.txt, not ambient on the
# runner image, so a real, honest `pytest ...` claim failed with a tooling
# error that shared its exit code AND its message shape with "ran it, and
# the claim was false" — the more rigorous a PR's evidence, the less likely
# it could merge. The workflow now installs this repo's Python dependencies
# before this script runs (see .github/workflows/pr-gates.yml), which closes
# the gap for the tools this repo already depends on. This script no longer
# assumes that stays true forever: before invoking a declared command it
# checks the tool it needs is actually present (`command -v`, and for a
# `python3 -m X` form, that the module actually imports — `command -v
# python3` alone would not have caught today's exact defect, since
# `ubuntu-latest` always ships a `python3`). "Cannot evaluate this claim" is
# reported loudly and distinctly from a real FAIL, and does not block — see
# the tool-presence check below for why.
#
# FORMAT
#
#   ## Mutation evidence
#
#   Host shape: fresh clone (CI runner) | linked worktree | both
#   Command: pytest tests/test_foo.py::test_bar -q
#
#   ```diff
#   --- a/backend/foo.py
#   +++ b/backend/foo.py
#   @@ -12,7 +12,7 @@
#   -    return all(c.ok for c in candidates)
#   +    return any(c.ok for c in candidates)
#   ```
#
# Host shape is free text, recorded and never validated — it exists so a
# mutant killed on one checkout shape but not another (a real, documented gap
# in the register this Discussion is about) is at least labelled, since this
# script can only ever speak for the shape it actually ran on.
#
# Command must start with one of: "pytest ", "python3 -m pytest ",
# "python3 -m unittest ", "bash tests/". Exactly one ```diff fence is
# required; more than one is rejected rather than silently taking the first.
#
# Usage:
#   PR_BODY_FILE=/path/to/body.txt bash scripts/ci/pr-mutation-evidence.sh
#   PR_BODY="$(gh pr view 123 --json body -q .body)" bash scripts/ci/pr-mutation-evidence.sh
#
# Resolved against the git repository in the CURRENT WORKING DIRECTORY, like
# scripts/ci/publish-denylist.sh — this is what makes the script testable
# against a throwaway fixture repo (cd into it) rather than only against the
# live checkout.
#
# Exit 0 = no block present (nothing claimed, nothing to reproduce), or a
#          claim was made and reproduced (green clean, red patched), or a
#          claim was made but this environment cannot run the tool it needs
#          (an environment gap, logged loudly on stderr as a WARN — not a
#          verdict on the claim, and not a block: see "TOOL PRESENCE" above
#          and Spec D#2537 item 7, "never worse off than prose").
# Exit 1 = a claim was made, the declared tool WAS available, and it did not
#          reproduce; or the command was rejected, the patch did not apply,
#          or the checkout could not be measured. There is deliberately no
#          SKIP branch on a real, measurable claim: a check that COULD
#          measure a claim it was handed must not report a pass.
# Exit 2 = usage error (neither input set, or PR_BODY_FILE unreadable).

set -uo pipefail

if [[ $# -gt 0 ]]; then
  echo "usage: PR_BODY=<text> $(basename "$0")" >&2
  exit 2
fi

# Each command run is bounded — a hung suite must not hang the check forever.
TIMEOUT_SECONDS="${PR_MUTATION_EVIDENCE_TIMEOUT:-300}"

# ---------------------------------------------------------------------------
# Read the body.
# ---------------------------------------------------------------------------
BODY_SOURCE=""
if [[ -n "${PR_BODY_FILE:-}" ]]; then
  if [[ ! -r "$PR_BODY_FILE" ]]; then
    echo "FAIL: PR_BODY_FILE is set to '$PR_BODY_FILE' but that file is not readable." >&2
    exit 2
  fi
  PR_BODY="$(cat "$PR_BODY_FILE")"
  BODY_SOURCE="\$PR_BODY_FILE"
elif [[ -n "${PR_BODY+set}" ]]; then
  BODY_SOURCE="\$PR_BODY"
else
  echo "FAIL: neither PR_BODY_FILE nor PR_BODY is set." >&2
  echo "      In CI, write the body to a file from \$GITHUB_EVENT_PATH and name it" >&2
  echo "      in PR_BODY_FILE. Do NOT put the body in the step's \`env:\` block." >&2
  exit 2
fi

echo "pr-mutation-evidence: body from $BODY_SOURCE, ${#PR_BODY} chars"

# ---------------------------------------------------------------------------
# Find the block. Absence is success — see header.
# ---------------------------------------------------------------------------
if ! printf '%s\n' "$PR_BODY" | grep -qE '^## Mutation evidence[[:space:]]*$'; then
  echo "PASS: no mutation evidence block in the PR body — nothing claimed, nothing to reproduce."
  exit 0
fi

# Section content: everything after the heading line up to (not including)
# the next level-2 heading, or end of body. Parsed with awk rather than a
# single regex over the whole body, because the fence content is arbitrary
# diff text and will itself contain lines starting with "---" and "+++".
extract_block() {
  awk '
    BEGIN { found = 0 }
    /^## Mutation evidence[[:space:]]*$/ { found = 1; next }
    found && /^## / { found = 0; exit }
    found { print }
  '
}
BLOCK="$(printf '%s\n' "$PR_BODY" | extract_block)"

# ---------------------------------------------------------------------------
# Host shape — recorded, never validated.
# ---------------------------------------------------------------------------
HOST_SHAPE="$(printf '%s\n' "$BLOCK" | sed -n -E 's/^Host shape:[[:space:]]*//p' | head -1)"
echo "pr-mutation-evidence: host shape (as declared, not verified): '${HOST_SHAPE:-<not stated>}'"

# ---------------------------------------------------------------------------
# Command — validated against an allowlist of PREFIXES before anything runs.
# An absent, empty, or non-matching command is rejected outright: an empty
# command must never be treated as a vacuous pass (that is the shape of
# register entry 4 — grep -F "" matching everything).
# ---------------------------------------------------------------------------
COMMAND_RAW="$(printf '%s\n' "$BLOCK" | sed -n -E 's/^Command:[[:space:]]*//p' | head -1)"
COMMAND="$(printf '%s' "$COMMAND_RAW" | sed -E 's/[[:space:]]+$//')"

command_allowed() {
  case "$1" in
    "pytest "*) return 0 ;;
    "python3 -m pytest "*) return 0 ;;
    "python3 -m unittest "*) return 0 ;;
    "bash tests/"*) return 0 ;;
    *) return 1 ;;
  esac
}

if ! command_allowed "$COMMAND"; then
  echo "FAIL: rejected command: '${COMMAND:-<absent or empty>}'" >&2
  echo "      Command: must start with one of: 'pytest ', 'python3 -m pytest ', 'python3 -m unittest ', 'bash tests/'." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Tool presence — checked before the command is invoked, or even before the
# diff is parsed (D#2537). "I could not run this" and "I ran it and the
# claim was false" must never share an exit code or a message: this branch
# owns the first, everything below owns the second.
#
# `command -v` is the literal instrument the Spec asks for, plus a module
# import check for the `python3 -m X` forms — `command -v python3` alone
# would pass on `ubuntu-latest` even when the module the PR actually needs
# (pytest) is not installed, which is precisely today's defect restated.
#
# A missing tool is NOT a false claim: the command never ran, so it neither
# reproduced nor failed to. Blocking here would make a declared,
# machine-checkable block strictly worse than prose for the identical
# underlying truth (the environment cannot evaluate either one) — the one
# outcome this Discussion exists to rule out (Spec item 7). So this exits 0,
# like the "no block" case above, but with a WARN loud enough on stderr that
# an operator reading the log does not mistake silence for verification.
# ---------------------------------------------------------------------------
MISSING_TOOL=""
case "$COMMAND" in
  "pytest "*)
    command -v pytest >/dev/null 2>&1 || MISSING_TOOL="pytest"
    ;;
  "python3 -m pytest "*)
    if ! command -v python3 >/dev/null 2>&1; then
      MISSING_TOOL="python3"
    elif ! python3 -c "import pytest" >/dev/null 2>&1; then
      MISSING_TOOL="pytest (python3 -c \"import pytest\" failed — module not installed)"
    fi
    ;;
  "python3 -m unittest "*)
    command -v python3 >/dev/null 2>&1 || MISSING_TOOL="python3"
    ;;
  "bash tests/"*)
    command -v bash >/dev/null 2>&1 || MISSING_TOOL="bash"
    ;;
esac

if [[ -n "$MISSING_TOOL" ]]; then
  echo "WARN: cannot evaluate this claim — required tool not available in this environment: $MISSING_TOOL" >&2
  echo "      Command declared: '$COMMAND'" >&2
  echo "      This is an environment gap, not a false claim: the command was never run, so it neither reproduced nor failed to reproduce." >&2
  echo "      Distinct from every FAIL below (those mean the command ran) and from 'PASS: no mutation evidence block' above (this claim WAS declared, just unmeasurable here)." >&2
  exit 0
fi

# ---------------------------------------------------------------------------
# Exactly one ```diff fence is required.
# ---------------------------------------------------------------------------
FENCE_COUNT="$(printf '%s\n' "$BLOCK" | grep -c '^```diff[[:space:]]*$' || true)"
if [[ "$FENCE_COUNT" -eq 0 ]]; then
  echo "FAIL: patch did not apply — no \`\`\`diff fence found in the Mutation evidence block." >&2
  exit 1
fi
if [[ "$FENCE_COUNT" -gt 1 ]]; then
  echo "FAIL: patch did not apply — ${FENCE_COUNT} \`\`\`diff fences found in the Mutation evidence block; exactly one is required, not the first of several." >&2
  exit 1
fi

DIFF_CONTENT="$(printf '%s\n' "$BLOCK" | awk '
  /^```diff[[:space:]]*$/ { infence = 1; next }
  infence && /^```[[:space:]]*$/ { exit }
  infence { print }
')"

if [[ -z "$DIFF_CONTENT" ]]; then
  echo "FAIL: patch did not apply — the \`\`\`diff fence in the Mutation evidence block is empty." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# From here on we touch the working tree, so it has to be a real checkout.
# ---------------------------------------------------------------------------
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "FAIL: $(pwd) is not a git checkout — this gate applies and reverts a patch and cannot report a pass without one." >&2
  exit 1
fi

PATCH_FILE="$(mktemp)"
PATCH_APPLIED=0
# Revert-on-exit survives every exit path below, including an early one from
# a failure branch — acceptance item 9 requires the checkout to come back
# clean regardless of which check failed. Only reverts if the real (not
# --check) apply actually ran.
trap '
  if [[ "${PATCH_APPLIED:-0}" -eq 1 ]]; then
    git apply -R "$PATCH_FILE" 2>/dev/null || echo "WARN: revert of the applied patch failed — the checkout may be dirty; run git apply -R or git checkout -- . manually" >&2
  fi
  rm -f "$PATCH_FILE"
' EXIT

printf '%s\n' "$DIFF_CONTENT" >"$PATCH_FILE"

APPLY_CHECK_ERR="$(git apply --check "$PATCH_FILE" 2>&1)"
APPLY_CHECK_RC=$?
if [[ $APPLY_CHECK_RC -ne 0 ]]; then
  echo "FAIL: patch did not apply — $APPLY_CHECK_ERR" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Leg 1 — the clean tree. Must be green before we touch anything: a mutation
# claim attached to an already-broken suite proves nothing about the
# mutation, and reporting a pass there is exactly the false-certification
# shape this file exists to prevent.
# ---------------------------------------------------------------------------
BASELINE_OUT="$(timeout "$TIMEOUT_SECONDS" bash -c "$COMMAND" 2>&1)"
BASELINE_RC=$?
if [[ $BASELINE_RC -ne 0 ]]; then
  echo "FAIL: baseline is not green — command '$COMMAND' exited $BASELINE_RC on the clean tree, before the patch was applied." >&2
  echo "--- baseline output ---" >&2
  printf '%s\n' "$BASELINE_OUT" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Leg 2 — apply for real, then run the same command again.
# ---------------------------------------------------------------------------
APPLY_ERR="$(git apply "$PATCH_FILE" 2>&1)"
APPLY_RC=$?
if [[ $APPLY_RC -ne 0 ]]; then
  echo "FAIL: patch did not apply — $APPLY_ERR" >&2
  exit 1
fi
PATCH_APPLIED=1

MUTANT_OUT="$(timeout "$TIMEOUT_SECONDS" bash -c "$COMMAND" 2>&1)"
MUTANT_RC=$?

if [[ $MUTANT_RC -eq 0 ]]; then
  echo "FAIL: mutant survived — command '$COMMAND' still exited 0 with the patch applied." >&2
  echo "      Green on the clean tree AND green on the mutated tree means this claim did not reproduce:" >&2
  echo "      the named command does not distinguish the two, so it is not evidence for the coverage claimed." >&2
  exit 1
fi

echo "PASS: mutation evidence reproduced — '$COMMAND' exited 0 on the clean tree and $MUTANT_RC with the patch applied."
exit 0
