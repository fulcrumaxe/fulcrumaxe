#!/usr/bin/env bash
# tests/test_auto_pull_locale_guard.sh — the auto-pull path routes on git's
# *English* output. This suite checks that it still routes correctly when git
# is not speaking English.
#
# Run: bash tests/test_auto_pull_locale_guard.sh
# Expects: all assertions pass, exit 0 (or a loud SKIP — see below).
#
# Why this is a separate file from tests/test_post_merge_hook_pull.sh:
# that suite does a global `export LC_ALL=C` near the top. The pin is correct
# there — it is what makes its Test 7 deterministic — but it also means that
# suite *structurally cannot observe* a missing LC_ALL=C anywhere in the code it
# drives. Every command it runs inherits C from the environment, so a guarded
# call and an unguarded call behave identically under it. Adding a locale case
# to that file would produce an assertion that passes against unfixed code.
# Measured while writing this: stripping LC_ALL=C from the two pulls in
# auto-pull-step.sh leaves that suite at 29 passed / 0 failed.
#
# So this file deliberately does NOT pin LC_ALL globally. It sets a translated
# environment per-call, and every check that matters is paired with a negative
# control that fails if the translation shim ever stops translating. That is the
# point: a locale test that quietly stops translating becomes a test that
# asserts nothing, and it would still be green.
#
# Hermetic, on the same terms as test_post_merge_hook_pull.sh: throwaway repo
# pairs under `mktemp -d`, the team-log write redefined to a temp file, `gh`
# stubbed on PATH, AUTONOMOUS_TEAM_STATE_DIR repointed at a temp dir. Nothing
# here touches the operator's checkout, the network, or the GitHub API. This
# matters more than usual — the branch under test runs a destructive
# `git checkout -B main origin/main`.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/auto-pull-step.sh
source "${REAL_REPO_ROOT}/scripts/lib/auto-pull-step.sh"

PASS=0
FAIL=0
ERRORS=()
FIXTURES=()

TMP_STATE="$(mktemp -d)"
FIXTURES+=("$TMP_STATE")
export AUTONOMOUS_TEAM_STATE_DIR="$TMP_STATE/state"
mkdir -p "$AUTONOMOUS_TEAM_STATE_DIR"

# See the header of test_post_merge_hook_pull.sh: a real `gh` with no --repo
# override resolves the invoking directory's git remote, which is this actual
# repo. Do not remove this stub even for a test that "shouldn't" reach it.
_REPO="fixture-org/fixture-repo"
GH_STUB_DIR="$TMP_STATE/bin"
mkdir -p "$GH_STUB_DIR"
GH_CALL_LOG="$TMP_STATE/gh-calls.txt"
: > "$GH_CALL_LOG"
cat > "$GH_STUB_DIR/gh" <<'GHSTUB'
#!/usr/bin/env bash
echo "$*" >> "${GH_CALL_LOG:?GH_CALL_LOG must be set for the gh stub}"
if [[ "$*" == *"issue create"* ]]; then
  echo "https://github.com/fixture-org/fixture-repo/issues/1"
  exit 0
fi
if [[ "$*" == *"issue list"* ]]; then
  echo "null"
  exit 0
fi
exit 0
GHSTUB
chmod +x "$GH_STUB_DIR/gh"
export GH_CALL_LOG
export PATH="$GH_STUB_DIR:$PATH"

TEAMLOG=""
auto_pull_step_teamlog() { printf 'TEAMLOG: %s\n' "$1" >> "$TEAMLOG"; }

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label — expected to find: $needle"
    echo "    Output was: $haystack" >&2
  fi
}

assert_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    fail "$label — did not expect to find: $needle"
    echo "    Output was: $haystack" >&2
  else
    pass "$label"
  fi
}

assert_rc() {
  local label="$1" rc="$2" want="$3"
  if [[ "$rc" == "$want" ]]; then
    pass "$label"
  else
    fail "$label — return code was $rc (expected $want)"
  fi
}

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    pass "$label"
  else
    fail "$label — got [$got], expected [$want]"
  fi
}

head_of() { git -C "$1" rev-parse HEAD 2>/dev/null || echo ""; }
branch_of() { git -C "$1" branch --show-current 2>/dev/null || echo ""; }

# ── Locale shim ──────────────────────────────────────────────────────────────
#
# Setting LC_ALL to a translated locale name is NOT enough, and assuming it is
# is the easiest way to write a green test that proves nothing. Measured on the
# host this was written on (nixos, git 2.55.0): `locale -a` lists exactly four
# entries — C, C.utf8, en_US.utf8, POSIX. Running `LC_ALL=de_DE.UTF-8 git ...`
# there emits a setlocale warning and then perfectly English output, because
# glibc falls back when the locale is not installed. A test built that way would
# have "run under a translated locale" in its name and be asserting on English.
#
# git's .mo catalogues are installed independently of the locale definitions
# (share/locale/*/LC_MESSAGES/git.mo), and glibc's gettext honours LANGUAGE
# for message lookup as long as the active locale is not C/POSIX. So the shim
# is: an installed non-C UTF-8 locale for setlocale to accept, plus LANGUAGE to
# pick the catalogue. That combination does translate here.
#
# Everything below is probed at run time rather than assumed, and if no
# combination works the suite SKIPS loudly instead of reporting a pass.

XLOC=""
XLANG=""

# Known-English needle that git emits when a fetched ref does not exist. This is
# one of the two strings auto-pull-step.sh greps for.
FETCH_NEEDLE="couldn't find remote ref"

probe_translation() {
  local probe_dir loc lang out
  probe_dir="$(mktemp -d)"
  FIXTURES+=("$probe_dir")
  git init -q --bare "$probe_dir/remote.git"
  git init -q "$probe_dir/work"
  git -C "$probe_dir/work" config user.email "test@test.com"
  git -C "$probe_dir/work" config user.name "Test"
  git -C "$probe_dir/work" commit -q --allow-empty -m init
  git -C "$probe_dir/work" remote add origin "$probe_dir/remote.git"

  for loc in $(locale -a 2>/dev/null | grep -iE 'utf-?8' | grep -viE '^(C|POSIX)'); do
    for lang in de fr es it pt sv ru vi bg el ko zh_CN ja; do
      out="$(LC_ALL="$loc" LANGUAGE="$lang" git -C "$probe_dir/work" fetch origin main 2>&1)"
      # Translated iff git stopped emitting the English needle. A locale that
      # merely reformats (or that glibc silently declined) still prints it.
      if ! printf '%s' "$out" | grep -qF -- "$FETCH_NEEDLE"; then
        XLOC="$loc"
        XLANG="$lang"
        return 0
      fi
    done
  done
  return 1
}

echo "Probing for a working translated-locale shim..."
if ! probe_translation; then
  echo
  echo "SKIP: no locale/LANGUAGE combination on this host makes git emit"
  echo "      non-English output, so the locale guard cannot be exercised here."
  echo "      Installed locales: $(locale -a 2>/dev/null | tr '\n' ' ')"
  echo "      git: $(git --version)"
  echo
  echo "      This is a SKIP, not a pass. Nothing about LC_ALL=C was verified."
  echo "      Re-run on a host with git's message catalogues installed"
  echo "      (share/locale/*/LC_MESSAGES/git.mo) and at least one non-C UTF-8"
  echo "      locale in \`locale -a\`."
  exit 0
fi
echo "  Using LC_ALL=$XLOC LANGUAGE=$XLANG"
echo

# ── Fixtures ─────────────────────────────────────────────────────────────────

setup_fake_origin() {
  local origin_dir="$1"
  git -C "$origin_dir" init --initial-branch=main -q
  git -C "$origin_dir" config user.email "test@test.com"
  git -C "$origin_dir" config user.name "Test"
  echo "file1" > "$origin_dir/file1.txt"
  git -C "$origin_dir" add .
  git -C "$origin_dir" commit -m "init" -q
}

setup_fake_local() {
  local local_dir="$1" origin_dir="$2"
  git clone "$origin_dir" "$local_dir" -q --local
  git -C "$local_dir" config user.email "test@test.com"
  git -C "$local_dir" config user.name "Test"
}

new_fixture() {
  T="$(mktemp -d)"
  FIXTURES+=("$T")
  T_ORIGIN="$T/origin"
  T_LOCAL="$T/local"
  TEAMLOG="$T/teamlog.txt"
  mkdir -p "$T_ORIGIN"
  : > "$TEAMLOG"
  setup_fake_origin "$T_ORIGIN"
  setup_fake_local "$T_LOCAL" "$T_ORIGIN"
}

# Runs the shipping function with git translated. The fix under test pins
# LC_ALL=C on the individual commands whose output is grepped, so it must win
# over this environment — that is exactly the property being asserted.
run_step_translated() {
  OUT="$(LC_ALL="$XLOC" LANGUAGE="$XLANG" auto_pull_step "$T_LOCAL" 2>&1)" && RC=0 || RC=$?
  COMBINED="$OUT
$(cat "$TEAMLOG" 2>/dev/null || true)"
}

# ── Test 1: the shim genuinely translates the fetch gate (negative control) ───
#
# Without this control the rest of the file is unfalsifiable. If a future git,
# locale change, or packaging change stops translating, Tests 2 and 4 would pass
# against unfixed code and this suite would go green while asserting nothing.
echo "Test 1: control — the unguarded fetch really does stop matching"
new_fixture
git -C "$T_LOCAL" fetch origin main -q
git -C "$T_ORIGIN" branch -m main trunk    # origin no longer has a main ref

RAW_TRANSLATED="$(LC_ALL="$XLOC" LANGUAGE="$XLANG" git -C "$T_LOCAL" fetch origin main 2>&1)"
RAW_PINNED="$(LC_ALL="$XLOC" LANGUAGE="$XLANG" LC_ALL=C git -C "$T_LOCAL" fetch origin main 2>&1)"

assert_not_contains "test1: translated fetch loses the English gate string" \
  "$RAW_TRANSLATED" "$FETCH_NEEDLE"
assert_contains "test1: the same fetch under LC_ALL=C keeps it" \
  "$RAW_PINNED" "$FETCH_NEEDLE"

# ── Test 2: the fetch-failure recovery still fires under a translated locale ──
#
# Same fixture shape as Test 7 of test_post_merge_hook_pull.sh: origin advances,
# local learns origin/main and then stays a commit behind, and the remote main
# ref is renamed away. With the ref gone, nothing in the step except the
# force-reset recovery can move HEAD — so HEAD landing on the origin tip is
# proof the recovery branch ran, not a side effect of an ordinary pull.
echo "Test 2: fetch-recovery fires under a translated locale"
new_fixture
echo "second" > "$T_ORIGIN/second.txt"
git -C "$T_ORIGIN" add .
git -C "$T_ORIGIN" commit -m "advance origin" -q
git -C "$T_LOCAL" fetch origin main -q
git -C "$T_ORIGIN" branch -m main trunk
TARGET="$(head_of "$T_ORIGIN")"

run_step_translated
assert_rc "test2: returns 0 after the force-reset recovery" "$RC" "0"
assert_contains "test2: reports the forced reset" "$COMBINED" "forcing reset to origin/main"
assert_eq "test2: still on main afterwards" "$(branch_of "$T_LOCAL")" "main"
assert_eq "test2: HEAD was reset onto the last known origin/main" "$(head_of "$T_LOCAL")" "$TARGET"

# ── Test 3: the shim translates the stash gate too (negative control) ─────────
echo "Test 3: control — the unguarded stash push really does stop matching"
new_fixture
printf 'alpha\nbeta\ngamma\n' > "$T_LOCAL/probe.txt"
git -C "$T_LOCAL" add -- probe.txt
git -C "$T_LOCAL" commit -q -m "add probe"
printf 'alpha\nbeta\ngamma-local\n' > "$T_LOCAL/probe.txt"

STASH_TRANSLATED="$(LC_ALL="$XLOC" LANGUAGE="$XLANG" git -C "$T_LOCAL" stash push -m probe -- probe.txt 2>&1)"
assert_not_contains "test3: translated stash push loses the English gate string" \
  "$STASH_TRANSLATED" "Saved working directory"

printf 'alpha\nbeta\ngamma-local2\n' > "$T_LOCAL/probe.txt"
STASH_PINNED="$(LC_ALL="$XLOC" LANGUAGE="$XLANG" LC_ALL=C git -C "$T_LOCAL" stash push -m probe -- probe.txt 2>&1)"
assert_contains "test3: the same stash push under LC_ALL=C keeps it" \
  "$STASH_PINNED" "Saved working directory"

# ── Test 4: the modified-file stash recovery still fires under translation ────
#
# This gate is the one that does NOT fail safe. Under a translated locale the
# stash push succeeds, the "Saved working directory" check misses, and
# auto_pull_recover_modified returns 1 reporting it declined to act — without
# setting AUTO_PULL_STASH_REF. The operator's edits are sitting in a stash that
# nothing names, and the caller has been told nothing happened.
echo "Test 4: modified-file stash recovery fires under a translated locale"
new_fixture
printf 'alpha\nbeta\ngamma\n' > "$T_ORIGIN/shared.txt"
git -C "$T_ORIGIN" add -- shared.txt
git -C "$T_ORIGIN" commit -m "add shared.txt" -q
git -C "$T_LOCAL" pull -q --ff-only origin main
printf 'alpha-upstream\nbeta\ngamma\n' > "$T_ORIGIN/shared.txt"
git -C "$T_ORIGIN" commit -am "upstream edits hunk B" -q
printf 'alpha\nbeta\ngamma-local\n' > "$T_LOCAL/shared.txt"   # hunk A, uncommitted

MOD_RC=0
LC_ALL="$XLOC" LANGUAGE="$XLANG" auto_pull_recover_modified "$T_LOCAL" 2>/dev/null || MOD_RC=$?

assert_rc "test4: recovery returns 0" "$MOD_RC" "0"
assert_contains "test4: reports stashing and restoring, not declining" \
  "${AUTO_PULL_STASH_SUMMARY:-}" "stashed and restored"
assert_not_contains "test4: does not report a declined stash push" \
  "${AUTO_PULL_STASH_SUMMARY:-}" "did not report success"

CONTENT="$(cat "$T_LOCAL/shared.txt")"
assert_contains "test4: keeps the local hunk" "$CONTENT" "gamma-local"
assert_contains "test4: picks up the upstream hunk" "$CONTENT" "alpha-upstream"
assert_eq "test4: no stash left behind" \
  "$(git -C "$T_LOCAL" stash list | wc -l | tr -d ' ')" "0"

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "─────────────────────────────────────────"
echo "Passed: $PASS   Failed: $FAIL"
echo "Shim:   LC_ALL=$XLOC LANGUAGE=$XLANG"
if [[ $FAIL -gt 0 ]]; then
  echo
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
exit 0
