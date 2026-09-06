#!/usr/bin/env bash
# tests/test_pre_push_guard.sh — behavioural suite for the local pre-push
# guard (scripts/install-pre-push-guard.sh).
#
# Every push below is a REAL push to a scratch bare remote on local disk.
# None of them is `--dry-run`: `git push --dry-run` does not drive `pre-push`
# the way a real push does, so a dry-run proves nothing about the guarded
# path. No network is touched — the "remote" is a directory.
#
# Test 0 is a control with the hook NOT installed. It force-rewinds `main` on
# a scratch remote and asserts that it SUCCEEDS. Without it, Test 3's refusal
# is just an assertion nobody has watched fail, and could be coming from
# anywhere — a missing branch, a bad refspec, an unwritable remote. Test 0 is
# what makes Test 3 evidence.
#
# Run from anywhere:
#   bash tests/test_pre_push_guard.sh
#
# Requires: bash 4+, git 2.32+ (for GIT_CONFIG_GLOBAL).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-pre-push-guard.sh"

if [[ ! -f "$INSTALLER" ]]; then
  echo "FAIL: installer not found at $INSTALLER" >&2
  exit 1
fi

# Per-run scratch root under mktemp -d, never a fixed /tmp path: two
# concurrent runs of this suite must not share a directory name.
SCRATCH="$(mktemp -d)" || { echo "FAIL: could not create scratch dir" >&2; exit 1; }
trap 'rm -rf "$SCRATCH"' EXIT

# Hermetic git: no operator global/system config reaches these fixtures, so a
# host with core.hooksPath set (or a different init.defaultBranch) cannot
# change what this suite measures. HOME is redirected for the same reason.
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export HOME="$SCRATCH/home"
mkdir -p "$HOME"

PASS=0
FAIL=0
TEST_NAME=""

pass() { echo "  PASS: $TEST_NAME"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $TEST_NAME — $*"; FAIL=$((FAIL + 1)); }

# ── Fixture ───────────────────────────────────────────────────────────────
#
# make_fixture <name> -> echoes the work-tree path; the bare remote sits
# beside it. The work tree carries commits A and B on `main`, both already
# pushed, so `origin/main` is real and a rewind has something to rewind.
# Commit subjects are recorded in files so a later assertion can read remote
# state without trusting the push's own exit code.

make_fixture() {
  local name="$1"
  local base="$SCRATCH/$name"
  local remote="$base/remote.git"
  local work="$base/work"

  mkdir -p "$base"
  git init --bare -b main "$remote" >/dev/null 2>&1
  git init -b main "$work" >/dev/null 2>&1
  git -C "$work" config user.email "test@example.invalid"
  git -C "$work" config user.name "pre-push guard suite"
  git -C "$work" remote add origin "$remote"

  echo A > "$work/file.txt"
  git -C "$work" add file.txt
  git -C "$work" commit -q -m "A"
  echo B > "$work/file.txt"
  git -C "$work" add file.txt
  git -C "$work" commit -q -m "B"
  git -C "$work" push -q -u origin main >/dev/null 2>&1

  echo "$work"
}

# remote_main_subject <work> — the subject line of whatever the scratch
# remote's `main` actually points at right now. Read from the remote, not
# from a push's exit status.
remote_main_subject() {
  local work="$1" sha
  sha="$(git -C "$work" ls-remote origin refs/heads/main 2>/dev/null | awk '{print $1}')"
  if [[ -z "$sha" ]]; then
    echo "(absent)"
    return 0
  fi
  git -C "$work" log -1 --format=%s "$sha" 2>/dev/null || echo "(unknown $sha)"
}

# diverge <work> <subject> — rewind one commit and commit something else, so
# local `main` is no longer a descendant of what the remote holds.
diverge() {
  local work="$1" subject="$2"
  git -C "$work" reset -q --hard HEAD~1
  echo "$subject" > "$work/file.txt"
  git -C "$work" add file.txt
  git -C "$work" commit -q -m "$subject"
}

echo "pre-push guard — real pushes against scratch bare remotes"
echo "scratch root: $SCRATCH"
echo

# ── Test 0: control — the same force-rewind, with NO hook installed ────────
TEST_NAME="control: force-rewind of main succeeds when the guard is absent"
CTRL="$(make_fixture control)"
if [[ -e "$(git -C "$CTRL" rev-parse --absolute-git-dir)/hooks/pre-push" ]]; then
  fail "fixture already has a pre-push hook; control is meaningless"
else
  diverge "$CTRL" "control-rewind"
  if git -C "$CTRL" push -q --force origin main >/dev/null 2>&1; then
    if [[ "$(remote_main_subject "$CTRL")" == "control-rewind" ]]; then
      pass
    else
      fail "push reported success but remote main is at $(remote_main_subject "$CTRL")"
    fi
  else
    fail "force-rewind failed even with no hook installed — the fixture, not the guard, is refusing"
  fi
fi

# ── The guarded fixture ───────────────────────────────────────────────────
WORK="$(make_fixture guarded)"
GITDIR="$(git -C "$WORK" rev-parse --absolute-git-dir)"
HOOK="$GITDIR/hooks/pre-push"

# The installer resolves its own repo root from its location, exactly as an
# operator runs it, so copy it into the fixture and run it there.
mkdir -p "$WORK/scripts"
cp "$INSTALLER" "$WORK/scripts/install-pre-push-guard.sh"

# ── Test 1: install writes one executable hook, exit 0 ────────────────────
TEST_NAME="first install: exit 0, one executable .git/hooks/pre-push"
OUT1="$(bash "$WORK/scripts/install-pre-push-guard.sh" 2>&1)"
RC1=$?
if [[ $RC1 -ne 0 ]]; then
  fail "installer exited $RC1: $OUT1"
elif [[ ! -f "$HOOK" ]]; then
  fail "no hook at $HOOK (installer said: $OUT1)"
elif [[ ! -x "$HOOK" ]]; then
  fail "$HOOK exists but is not executable"
else
  pass
fi

FIRST_SUM="$(cksum < "$HOOK")"

# ── Test 2: second install is a no-op, exit 0, still exactly one hook ─────
TEST_NAME="second install: exit 0, hook unchanged, still exactly one hook file"
OUT2="$(bash "$WORK/scripts/install-pre-push-guard.sh" 2>&1)"
RC2=$?
HOOK_COUNT="$(find "$GITDIR/hooks" -maxdepth 1 -name 'pre-push*' -not -name '*.sample' | wc -l | tr -d ' ')"
if [[ $RC2 -ne 0 ]]; then
  fail "second run exited $RC2: $OUT2"
elif [[ "$(cksum < "$HOOK")" != "$FIRST_SUM" ]]; then
  fail "hook content changed between identical runs"
elif [[ "$HOOK_COUNT" -ne 1 ]]; then
  fail "expected exactly 1 pre-push hook file, found $HOOK_COUNT"
elif [[ ! -x "$HOOK" ]]; then
  fail "hook is no longer executable after the second run"
else
  pass
fi

# ── Test 3: a real fast-forward push to main still succeeds ──────────────
TEST_NAME="fast-forward push to main succeeds with the guard installed"
echo C > "$WORK/file.txt"
git -C "$WORK" add file.txt
git -C "$WORK" commit -q -m "C"
if git -C "$WORK" push origin main >/dev/null 2>&1 && [[ "$(remote_main_subject "$WORK")" == "C" ]]; then
  pass
else
  fail "guard blocked a legitimate fast-forward; remote main is at $(remote_main_subject "$WORK")"
fi

# ── Test 4: a real force push that rewinds main is refused ───────────────
TEST_NAME="force push rewinding main is refused, non-zero exit, remote unmoved"
diverge "$WORK" "D"
ERRFILE="$SCRATCH/refusal.err"
git -C "$WORK" push --force origin main >/dev/null 2>"$ERRFILE"
RC4=$?
if [[ $RC4 -eq 0 ]]; then
  fail "push succeeded; remote main is now at $(remote_main_subject "$WORK")"
elif [[ "$(remote_main_subject "$WORK")" != "C" ]]; then
  fail "remote main moved to $(remote_main_subject "$WORK") despite the non-zero exit"
elif ! grep -q "pre-push guard: refusing non-fast-forward push" "$ERRFILE"; then
  fail "non-zero exit but no guard message on stderr: $(head -c 300 "$ERRFILE")"
else
  pass
  # Echo what the operator actually sees. The whole point of this hook is
  # that the message is short and says what to do next; printing it here
  # means a reviewer reads the real thing rather than the source of it.
  sed -n '1,3p' "$ERRFILE" | sed 's/^/         | /'
fi

# ── Test 5: a real force push rewinding a NON-main branch is unaffected ──
TEST_NAME="force push rewinding a non-main branch is unaffected"
git -C "$WORK" checkout -q -B feature HEAD
git -C "$WORK" push -q origin feature >/dev/null 2>&1
diverge "$WORK" "feature-rewind"
if git -C "$WORK" push --force origin feature >/dev/null 2>&1; then
  pass
else
  fail "guard blocked a force push to 'feature' — it must only look at main"
fi
git -C "$WORK" checkout -q main

# ── Test 6: --no-verify still gets the same rewind through ───────────────
TEST_NAME="--no-verify --force rewinding main still succeeds (documented bypass)"
if git -C "$WORK" push --no-verify --force origin main >/dev/null 2>&1 \
   && [[ "$(remote_main_subject "$WORK")" == "D" ]]; then
  pass
else
  fail "bypass did not work; remote main is at $(remote_main_subject "$WORK")"
fi

# ── Test 7: deleting main is refused, BY THE HOOK ────────────────────────
#
# A bare remote refuses to delete its own current branch anyway
# (receive.denyDeleteCurrent), so a non-zero exit alone would not tell us
# which side said no. The assertion is on the hook's own stderr line, which
# only the client-side hook can produce.
TEST_NAME="deleting main is refused by the hook, not just by the remote"
DELERR="$SCRATCH/delete.err"
git -C "$WORK" push origin :main >/dev/null 2>"$DELERR"
RC7=$?
if [[ $RC7 -eq 0 ]]; then
  fail "deletion of remote main succeeded"
elif ! grep -q "pre-push guard: refusing to DELETE" "$DELERR"; then
  fail "refused, but not by the guard: $(head -c 300 "$DELERR")"
elif [[ "$(remote_main_subject "$WORK")" != "D" ]]; then
  fail "remote main is gone or moved: $(remote_main_subject "$WORK")"
else
  pass
fi

# ── Test 8: the README documents the bypass ──────────────────────────────
TEST_NAME="README documents the --no-verify bypass"
README="$REPO_ROOT/scripts/pre-push-guard.README.md"
if [[ ! -f "$README" ]]; then
  fail "missing $README"
elif grep -q -- "--no-verify" "$README"; then
  pass
else
  fail "$README does not mention --no-verify"
fi

# ── Test 9: a foreign pre-push hook is never clobbered ───────────────────
TEST_NAME="a pre-existing foreign pre-push hook is left byte-identical, exit 1"
FOREIGN="$(make_fixture foreign)"
FOREIGN_GITDIR="$(git -C "$FOREIGN" rev-parse --absolute-git-dir)"
mkdir -p "$FOREIGN/scripts" "$FOREIGN_GITDIR/hooks"
cp "$INSTALLER" "$FOREIGN/scripts/install-pre-push-guard.sh"
printf '#!/bin/sh\n# somebody else got here first\nexit 0\n' > "$FOREIGN_GITDIR/hooks/pre-push"
chmod 755 "$FOREIGN_GITDIR/hooks/pre-push"
FOREIGN_SUM="$(cksum < "$FOREIGN_GITDIR/hooks/pre-push")"
OUT9="$(bash "$FOREIGN/scripts/install-pre-push-guard.sh" 2>&1)"
RC9=$?
if [[ $RC9 -eq 0 ]]; then
  fail "installer reported success over a foreign hook: $OUT9"
elif [[ "$(cksum < "$FOREIGN_GITDIR/hooks/pre-push")" != "$FOREIGN_SUM" ]]; then
  fail "foreign hook was modified"
else
  pass
fi

# ── Test 10: core.hooksPath is refused rather than silently ignored ──────
TEST_NAME="core.hooksPath set: installer exits 1 and writes no dead hook"
HP="$(make_fixture hookspath)"
HP_GITDIR="$(git -C "$HP" rev-parse --absolute-git-dir)"
mkdir -p "$HP/scripts" "$HP/elsewhere-hooks"
cp "$INSTALLER" "$HP/scripts/install-pre-push-guard.sh"
git -C "$HP" config core.hooksPath "$HP/elsewhere-hooks"
OUT10="$(bash "$HP/scripts/install-pre-push-guard.sh" 2>&1)"
RC10=$?
if [[ $RC10 -eq 0 ]]; then
  fail "installer reported success while core.hooksPath was set: $OUT10"
elif [[ -e "$HP_GITDIR/hooks/pre-push" ]]; then
  fail "installer wrote a hook git would never run"
else
  pass
fi

echo
echo "passed: $PASS   failed: $FAIL"
[[ $FAIL -eq 0 ]] || exit 1
