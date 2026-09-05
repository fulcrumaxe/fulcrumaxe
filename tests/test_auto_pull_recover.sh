#!/usr/bin/env bash
# tests/test_auto_pull_recover.sh — tests for scripts/lib/auto-pull-recover.sh (D#1911)
#
# Run: bash tests/test_auto_pull_recover.sh
# Expects: all assertions pass, exit 0
#
# These tests source and call the *shipping* function. They do not copy it into
# a heredoc — a copy can pass while the code that runs after every merge is
# still broken, which is exactly the hole D#1948 tracks in the older
# post-merge-hook test. Nothing here touches that file.
#
# Every fixture is a throwaway pair of git repos under `mktemp -d`, built at run
# time. That is deliberate: two of the cases need a filename containing a
# newline, and committing such a name to *this* repo would push it into every
# future clone and every open-source export. It never leaves the temp dir.
#
# Layout of one fixture:
#
#   $TMP/upstream        the "origin" repo
#   $TMP/box/work        a clone of it — the fixture repo root
#   $TMP/box/canary      one level above the repo root: the $HOME stand-in
#
# Follows the repo's plain-bash test convention (tests/test_ci_status_check.sh,
# tests/test_two_gate_check.sh).

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The shipping lib, sourced as-is — no copy, no heredoc.
# shellcheck source=scripts/lib/auto-pull-recover.sh
source "${REAL_REPO_ROOT}/scripts/lib/auto-pull-recover.sh"

PASS=0
FAIL=0
FIXTURES=()

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && chmod -R u+w "$d" 2>/dev/null
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL: $1"; [[ $# -gt 1 ]] && echo "        $2"; FAIL=$((FAIL + 1)); }

assert_true()  { if [[ "$2" == "0" ]]; then ok "$1"; else bad "$1" "expected success, got rc=$2"; fi; }
assert_false() { if [[ "$2" != "0" ]]; then ok "$1"; else bad "$1" "expected failure, got rc=0"; fi; }

assert_file()     { if [[ -f "$2" ]]; then ok "$1"; else bad "$1" "missing file: $(printf '%q' "$2")"; fi; }
assert_no_file()  { if [[ ! -e "$2" ]]; then ok "$1"; else bad "$1" "file should be gone: $(printf '%q' "$2")"; fi; }
assert_eq()       { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected [$3], got [$2]"; fi; }
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain [$2], got: $3"; fi
}

# ── fixture helpers ───────────────────────────────────────────────────────────

# new_fixture — sets TMP, UP, WORK. Upstream has one commit; WORK is a clone of it.
new_fixture() {
  TMP="$(mktemp -d)"
  FIXTURES+=("$TMP")
  UP="$TMP/upstream"
  mkdir -p "$UP" "$TMP/box"
  git init -q -b main "$UP"
  git -C "$UP" config user.email "test@example.invalid"
  git -C "$UP" config user.name "fixture"
  printf 'base\n' > "$UP/README.md"
  git -C "$UP" add -- README.md
  git -C "$UP" commit -qm "base"
  git clone -q "$UP" "$TMP/box/work"
  WORK="$TMP/box/work"
  git -C "$WORK" config user.email "test@example.invalid"
  git -C "$WORK" config user.name "fixture"
}

# upstream_add <relpath> <content> — commit a new file upstream (not yet pulled).
upstream_add() {
  local rel="$1" content="$2" dir
  dir="${rel%/*}"
  [[ "$dir" != "$rel" ]] && mkdir -p -- "${UP}/${dir}"
  printf '%s\n' "$content" > "${UP}/${rel}"
  git -C "$UP" add -- "$rel"
  git -C "$UP" commit -qm "add file"
}

# work_untracked <relpath> <content> — create an untracked file in the clone.
work_untracked() {
  local rel="$1" content="$2" dir
  dir="${rel%/*}"
  [[ "$dir" != "$rel" ]] && mkdir -p -- "${WORK}/${dir}"
  printf '%s\n' "$content" > "${WORK}/${rel}"
}

# try_pull — run the pull the hook runs; sets PULL_OUT / PULL_RC.
try_pull() {
  PULL_OUT="$(LC_ALL=C git -C "$WORK" pull --ff-only origin main 2>&1)" && PULL_RC=0 || PULL_RC=$?
}

archive_dir() { echo "$WORK"/archive/auto-pull-displaced-*; }

EVIL_SETTINGS=$'d/evil\n.claude/settings.json'
EVIL_CANARY=$'evil\n../canary'

# ── 6a — injection: a newline in an incoming filename must not reach a real file ─
echo "6a: newline-injected line is not treated as a path of its own"
new_fixture
upstream_add "$EVIL_SETTINGS" "hostile"
work_untracked "$EVIL_SETTINGS" "hostile-local"
work_untracked ".claude/settings.json" "REAL-OPERATOR-SETTINGS"

try_pull
assert_false "pull refuses to run (collision present)" "$PULL_RC"
assert_contains "git names both fragments on separate lines" ".claude/settings.json" "$PULL_OUT"

auto_pull_recover_untracked "$WORK" && REC_RC=0 || REC_RC=$?
assert_true "recovery acted" "${REC_RC}"
assert_file "the real .claude/settings.json survives" "$WORK/.claude/settings.json"
assert_eq "…with its content untouched" "$(cat "$WORK/.claude/settings.json")" "REAL-OPERATOR-SETTINGS"
assert_no_file "the genuinely-colliding file was moved aside" "${WORK}/${EVIL_SETTINGS}"
try_pull
assert_true "retried pull succeeds" "$PULL_RC"

# ── 6b — traversal: nothing above the repo root may be touched ────────────────
echo "6b: a '..'-bearing incoming filename cannot escape the repo root"
new_fixture
printf 'CANARY\n' > "$TMP/box/canary"
upstream_add "$EVIL_CANARY" "hostile"
work_untracked "$EVIL_CANARY" "hostile-local"

try_pull
assert_false "pull refuses to run (collision present)" "$PULL_RC"
assert_contains "git's prose contains a bare ../canary line" "../canary" "$PULL_OUT"

auto_pull_recover_untracked "$WORK" && REC_RC=0 || REC_RC=$?
assert_true "recovery acted" "$REC_RC"
assert_file "the canary one level above the repo root survives" "$TMP/box/canary"
assert_eq "…with its content untouched" "$(cat "$TMP/box/canary")" "CANARY"
try_pull
assert_true "retried pull succeeds" "$PULL_RC"

# the containment gate itself, driven directly — it is the only thing between
# this code and $HOME, so it gets an assertion that does not depend on the
# derivation happening to be correct.
auto_pull_candidate_allowed "$WORK" "../canary" && G_RC=0 || G_RC=$?
assert_false "containment gate rejects a literal ../canary candidate" "$G_RC"
assert_contains "…and says why" "resolves outside the repo root" "$AUTO_PULL_SKIP_REASON"

auto_pull_candidate_allowed "$WORK" ".git/config" && G_RC=0 || G_RC=$?
assert_false "containment gate rejects a path inside .git/" "$G_RC"
assert_contains "…and says why" "inside .git/" "$AUTO_PULL_SKIP_REASON"

# ── 6c — recovery still works, and the file is recoverable ───────────────────
echo "6c: a genuine collision is displaced, not deleted, and the pull proceeds"
new_fixture
upstream_add "docs/new.md" "from-upstream"
work_untracked "docs/new.md" "LOCAL-CONTENT"

try_pull
assert_false "pull refuses to run" "$PULL_RC"

auto_pull_recover_untracked "$WORK" && REC_RC=0 || REC_RC=$?
assert_true "recovery acted" "$REC_RC"
try_pull
assert_true "retried pull exits 0" "$PULL_RC"
assert_eq "docs/new.md is now the upstream copy" "$(cat "$WORK/docs/new.md")" "from-upstream"

ARC="$(archive_dir)"
assert_file "the local copy is retrievable from archive/" "$ARC/docs/new.md"
assert_eq "…byte-for-byte" "$(cat "$ARC/docs/new.md" 2>/dev/null)" "LOCAL-CONTENT"
assert_file "the archive directory carries an Archive Protocol README" "$ARC/README.md"
assert_contains "…which names the original path" "docs/new.md" "$(cat "$ARC/README.md" 2>/dev/null)"

# ── 6d — tracked files are never acted on ────────────────────────────────────
echo "6d: a tracked, locally-modified path in the candidate set is skipped"
new_fixture
printf 'locally modified\n' > "$WORK/README.md"

auto_pull_candidate_allowed "$WORK" "README.md" && G_RC=0 || G_RC=$?
assert_false "tracked-ness gate rejects a tracked path" "$G_RC"
assert_contains "…and says why" "tracked by git" "$AUTO_PULL_SKIP_REASON"
assert_file "the tracked file is still there" "$WORK/README.md"
assert_contains "git still reports the local modification" "README.md" \
  "$(git -C "$WORK" diff --name-only)"
assert_eq "its content is untouched" "$(cat "$WORK/README.md")" "locally modified"

# A path is not a pathspec. Without ":(literal)" this tracked file reads back as
# untracked, because git parses the leading ":(glob)" as pathspec magic.
GLOBBY=':(glob)weird'
printf 'tracked\n' > "${WORK}/${GLOBBY}"
git -C "$WORK" add -- ":(literal)${GLOBBY}"
git -C "$WORK" commit -qm "a filename that looks like pathspec magic"
auto_pull_candidate_allowed "$WORK" "$GLOBBY" && G_RC=0 || G_RC=$?
assert_false "tracked-ness gate rejects a tracked ':(glob)weird'" "$G_RC"
assert_contains "…as tracked, not as some other reason" "tracked by git" "$AUTO_PULL_SKIP_REASON"

# ── 6e — git's trailing prose is not a filename ──────────────────────────────
echo "6e: 'Aborting' from git's own output never becomes a candidate"
new_fixture
upstream_add "docs/new.md" "from-upstream"
work_untracked "docs/new.md" "LOCAL-CONTENT"
printf 'A REAL FILE NAMED ABORTING\n' > "$WORK/Aborting"

try_pull
assert_false "pull refuses to run" "$PULL_RC"
assert_contains "git's output really does contain the word Aborting" "Aborting" "$PULL_OUT"

auto_pull_recover_untracked "$WORK" && REC_RC=0 || REC_RC=$?
assert_true "recovery acted" "$REC_RC"
assert_file "the repo-root file named Aborting survives" "$WORK/Aborting"
assert_eq "…with its content untouched" "$(cat "$WORK/Aborting")" "A REAL FILE NAMED ABORTING"
assert_contains "the report names only the real collision" "docs/new.md" "$AUTO_PULL_RECOVER_MOVED"

# ── 6f — blast-radius bound ──────────────────────────────────────────────────
echo "6f: a derived set of 21 entries is refused outright, not acted on partially"
new_fixture
for i in $(seq -w 1 21); do
  printf 'up\n' > "$UP/bulk-$i.txt"
  work_untracked "bulk-$i.txt" "local-$i"
done
git -C "$UP" add -A
git -C "$UP" commit -qm "twenty-one files"

try_pull
assert_false "pull refuses to run" "$PULL_RC"

auto_pull_recover_untracked "$WORK" && REC_RC=0 || REC_RC=$?
assert_false "recovery declines when the derived set is over the bound" "$REC_RC"
assert_contains "…and reports the size and the bound" "over the bound of 20" "$AUTO_PULL_RECOVER_SUMMARY"
MOVED_ANY=0
for i in $(seq -w 1 21); do
  [[ -f "$WORK/bulk-$i.txt" ]] || MOVED_ANY=1
done
assert_eq "no file was moved" "$MOVED_ANY" "0"
assert_no_file "no archive directory was created" "$WORK/archive"

# ── a filesystem failure is reported as a filesystem failure ─────────────────
echo "a failure to archive is not reported as a gate rejection"
new_fixture
upstream_add "docs/new.md" "from-upstream"
work_untracked "docs/new.md" "LOCAL-CONTENT"
printf 'not a directory\n' > "$WORK/archive"   # mkdir -p under it must fail

auto_pull_recover_untracked "$WORK" && REC_RC=0 || REC_RC=$?
assert_false "recovery declines" "$REC_RC"
assert_contains "the summary counts the filesystem failure" "0 rejected by the safety gates and 1 failed on the filesystem" \
  "$AUTO_PULL_RECOVER_SUMMARY"
assert_contains "…and the per-path line says which step failed" "could not create its archive directory" \
  "$AUTO_PULL_RECOVER_SKIPPED"
assert_file "the file it could not archive is still in place" "$WORK/docs/new.md"

# ── declines cleanly when there is nothing to do ─────────────────────────────
echo "declines cleanly when no incoming addition collides with anything"
new_fixture
upstream_add "docs/new.md" "from-upstream"
auto_pull_recover_untracked "$WORK" && REC_RC=0 || REC_RC=$?
assert_false "recovery declines" "$REC_RC"
assert_contains "…and says so" "no incoming addition collides" "$AUTO_PULL_RECOVER_SUMMARY"
assert_no_file "nothing was archived" "$WORK/archive"

# ── item 10 — this repo itself carries no newline-bearing filename ───────────
echo "this repository commits no filename containing a newline"
A="$(git -C "$REAL_REPO_ROOT" ls-files -z | tr -d '\n' | wc -c)"
B="$(git -C "$REAL_REPO_ROOT" ls-files -z | wc -c)"
assert_eq "git ls-files -z byte count is unchanged by stripping newlines" "$A" "$B"

echo
echo "─────────────────────────────────────────"
echo "  passed: $PASS   failed: $FAIL"
if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
