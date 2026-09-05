#!/usr/bin/env bash
# tests/test_start_the_day_sync_block.sh — D#1759 (verb) + D#2075 (gate):
# scripts/start-the-day.sh's "## 1. Sync to fresh main" section.
#
# Run: bash tests/test_start_the_day_sync_block.sh
# Expects: all assertions pass, exit 0
#
# Two independent defects lived seven lines apart in this section:
#   - `git reset --mixed origin/main` moves HEAD/index only and leaves the
#     working directory untouched, so the section reported success while
#     tracked files sat stale on disk (D#1759).
#   - the `BRANCH != main` restore is correct in the primary checkout but
#     inverted in a linked worktree, where sitting on worktree-agent-<id> IS
#     the healthy state — it fired precisely when nothing was wrong and
#     repointed a live executor's HEAD onto the shared main ref (D#2075).
#
# METHODOLOGY (same approach as test_start_the_day_auth_guard.sh): this file
# never runs the real script against a real checkout — doing that is what
# caused both incidents, and CLAUDE.md forbids it. Instead it `sed`-extracts
# the exact shipped bytes of the section by locating its start/end markers
# (not hardcoded line numbers, which drift), wraps them in a standalone
# script, and runs that against synthetic git repos built fresh under
# `mktemp -d` — real git history, real content, entirely disposable.
#
# The dominant defect shape here is a command that reports success while
# leaving content stale. Every content assertion below compares sha256 of
# the actual file bytes, not just HEAD or exit code — an assertion that only
# checked those two would have passed against the broken code for three
# weeks, which is the whole reason this bug went unnoticed.

set -uo pipefail

REAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_SCRIPT="$REAL_REPO_ROOT/scripts/start-the-day.sh"
REAL_RESOLVER="$REAL_REPO_ROOT/scripts/lib/repo-root-resolve.sh"

PASS=0
FAIL=0
FIXTURES=()

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; [[ $# -gt 1 ]] && echo "        $2"; FAIL=$((FAIL + 1)); }

new_fixture_dir() {
  local d
  d=$(mktemp -d)
  FIXTURES+=("$d")
  printf '%s\n' "$d"
}

# Captured stdout/stderr from each sync-new.sh/sync-old.sh invocation below
# live here rather than at fixed /tmp/sync_*_out.$$ names — $$ is stable for
# the life of one run of this script, but the bare grep-matched prefix before
# it (e.g. "/tmp/sync_new_out.") is still a literal a lint can't distinguish
# from a real collision risk, so route it through the same fixture-dir
# mechanism as everything else here (D#2254).
LOG_TMP="$(new_fixture_dir)"

echo "== Static checks against the real shipped file =="

if ! grep -q 'git reset --mixed' "$REAL_SCRIPT"; then
  ok "no 'git reset --mixed' left in scripts/start-the-day.sh"
else
  bad "no 'git reset --mixed' left in scripts/start-the-day.sh" "found a match"
fi

if ! grep -q 'reset --hard' "$REAL_SCRIPT"; then
  ok "no 'reset --hard' anywhere in scripts/start-the-day.sh"
else
  bad "no 'reset --hard' anywhere in scripts/start-the-day.sh" "found a match — --hard is explicitly out of scope"
fi

# ── Extract the exact shipped "## 1. Sync to fresh main" section, by marker
# text rather than a hardcoded line range (D#1759's own Spec warned line
# numbers drift between when they're written down and when they're used).
START_LINE=$(grep -n '^echo "## 1\. Sync to fresh main"$' "$REAL_SCRIPT" | head -1 | cut -d: -f1)
END_LINE=$(grep -n '^echo "## 1b\. Working-tree divergence check"$' "$REAL_SCRIPT" | head -1 | cut -d: -f1)

if [[ -z "$START_LINE" || -z "$END_LINE" || "$END_LINE" -le "$START_LINE" ]]; then
  bad "located section 1's start/end markers in scripts/start-the-day.sh" "start=$START_LINE end=$END_LINE"
  echo ""
  echo "=== Results: $PASS passed, $FAIL failed ==="
  exit 1
fi
ok "located section 1 by marker text (lines $START_LINE-$END_LINE)"

# Back up one line from END_LINE and drop the trailing blank-line + comment
# block that precedes the "## 1b" echo, so we only take section 1's body.
# (The extraction is intentionally generous — trailing comments before the
# marker are harmless as standalone shell.)
SECTION_END=$((END_LINE - 1))

extract_section() {
  sed -n "${START_LINE},${SECTION_END}p" "$REAL_SCRIPT"
}

build_new_sync_script() {
  local dest="$1"
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    echo 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    echo 'REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"'
    echo 'cd "$REPO_ROOT"'
    echo 'source "$SCRIPT_DIR/lib/repo-root-resolve.sh"'
    extract_section
    echo 'echo "SYNC_EXIT_OK"'
  } > "$dest"
  chmod +x "$dest"
}

# Hardcoded historical snippet (the pre-fix bytes) for the negative-direction
# proof in item 6 — the whole point is showing today's content-change
# assertion FAILS against the code this Spec replaced. The current file no
# longer contains this, by design (see the static checks above), so it can't
# be sed-extracted; it's transcribed here as a regression fixture only.
build_old_sync_script() {
  local dest="$1"
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    echo 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    echo 'REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"'
    echo 'cd "$REPO_ROOT"'
    echo 'git fetch origin main -q 2>&1 | tail -2 || true'
    echo 'BRANCH=$(git branch --show-current 2>/dev/null)'
    echo 'if [[ "$BRANCH" != "main" ]]; then'
    echo '  echo "  HEAD was on the wrong branch — restoring to main"'
    echo '  git symbolic-ref HEAD refs/heads/main'
    echo 'fi'
    echo 'git reset --mixed origin/main >/dev/null 2>&1'
    echo 'echo "  HEAD: $(git rev-parse --short HEAD)"'
    echo 'echo "SYNC_EXIT_OK"'
  } > "$dest"
  chmod +x "$dest"
}

# ── Build a throwaway origin + seed layout shared by every scenario below.
# origin's HEAD is preset to refs/heads/main before any push (git init --bare
# defaults to refs/heads/master on this host, and a push of a differently
# named branch never fixes that up — every downstream clone would otherwise
# fail to check out a working tree at all).
FX=$(new_fixture_dir)
git init -q --bare "$FX/origin.git"
git --git-dir="$FX/origin.git" symbolic-ref HEAD refs/heads/main

git clone -q "$FX/origin.git" "$FX/seed"
(
  cd "$FX/seed"
  git symbolic-ref HEAD refs/heads/main
  git config user.email t@t.com
  git config user.name Tester
  echo "v1" > FILE.txt
  echo "other" > OTHER.txt
  mkdir -p scripts/lib
  build_new_sync_script scripts/sync-new.sh
  build_old_sync_script scripts/sync-old.sh
  cp "$REAL_RESOLVER" scripts/lib/repo-root-resolve.sh
  # scripts/ is a repo_divergence.py "critical path" — the upstream bump
  # below touches this too so the stale-tier assertion has something real
  # to key on (root-level FILE.txt/OTHER.txt intentionally are not critical).
  echo "critical-v1" > scripts/CRITICAL.txt
  git add FILE.txt OTHER.txt scripts/sync-new.sh scripts/sync-old.sh scripts/lib/repo-root-resolve.sh scripts/CRITICAL.txt
  git commit -q -m seed
  git push -q origin main
)

git clone -q "$FX/origin.git" "$FX/primary_new"
git clone -q "$FX/origin.git" "$FX/primary_old"
git clone -q "$FX/origin.git" "$FX/primary_dirty"
git clone -q "$FX/origin.git" "$FX/primary_dirty2"
git clone -q "$FX/origin.git" "$FX/primary_wt"
git clone -q "$FX/origin.git" "$FX/upstream_writer"

(
  cd "$FX/upstream_writer"
  git config user.email t@t.com
  git config user.name Tester
  echo "v2" > FILE.txt
  echo "critical-v2" > scripts/CRITICAL.txt
  git add FILE.txt scripts/CRITICAL.txt
  git commit -q -m "upstream advances"
  git push -q origin main
)

ORIGIN_V2_SHA=$(git -C "$FX/upstream_writer" show origin/main:FILE.txt | sha256sum | awk '{print $1}')

echo ""
echo "== Criterion 5 (positive): NEW verb updates working-tree content =="
SHA_BEFORE=$(sha256sum "$FX/primary_new/FILE.txt" | awk '{print $1}')
( cd "$FX/primary_new" && bash scripts/sync-new.sh >"$LOG_TMP/sync_new_out" 2>&1 )
NEW_RC=$?
SHA_AFTER=$(sha256sum "$FX/primary_new/FILE.txt" | awk '{print $1}')
rm -f "$LOG_TMP/sync_new_out"
if [[ $NEW_RC -eq 0 && "$SHA_AFTER" == "$ORIGIN_V2_SHA" && "$SHA_AFTER" != "$SHA_BEFORE" ]]; then
  ok "pull --ff-only updates FILE.txt content to match origin/main (sha256 changed and matches)"
else
  bad "pull --ff-only updates FILE.txt content" "rc=$NEW_RC before=$SHA_BEFORE after=$SHA_AFTER origin=$ORIGIN_V2_SHA"
fi

echo ""
echo "== Criterion 6 (negative direction): the SAME assertion fails against the old code =="
build_old_sync_script "$FX/primary_old/scripts/sync-old.sh" 2>/dev/null || true
SHA_OLD_BEFORE=$(sha256sum "$FX/primary_old/FILE.txt" | awk '{print $1}')
( cd "$FX/primary_old" && bash scripts/sync-old.sh >"$LOG_TMP/sync_old_out" 2>&1 )
OLD_RC=$?
SHA_OLD_AFTER=$(sha256sum "$FX/primary_old/FILE.txt" | awk '{print $1}')
HEAD_OLD_AFTER=$(git -C "$FX/primary_old" rev-parse HEAD)
ORIGIN_HEAD=$(git -C "$FX/primary_old" rev-parse origin/main)
rm -f "$LOG_TMP/sync_old_out"
if [[ "$HEAD_OLD_AFTER" == "$ORIGIN_HEAD" && "$SHA_OLD_AFTER" == "$SHA_OLD_BEFORE" && "$SHA_OLD_AFTER" != "$ORIGIN_V2_SHA" ]]; then
  ok "old 'reset --mixed' code reproduces D#1759: HEAD advances, content stays stale — today's content assertion correctly FAILS against it"
else
  bad "old-code repro" "expected HEAD-correct/content-wrong; rc=$OLD_RC head_after=$HEAD_OLD_AFTER origin_head=$ORIGIN_HEAD sha_after=$SHA_OLD_AFTER"
fi

echo ""
echo "== Criterion 7a: colliding local edit aborts non-zero, is not discarded =="
git -C "$FX/primary_dirty" fetch -q origin main
echo "local-edit-do-not-lose" > "$FX/primary_dirty/FILE.txt"
LOCAL_SHA=$(sha256sum "$FX/primary_dirty/FILE.txt" | awk '{print $1}')
( cd "$FX/primary_dirty" && bash scripts/sync-new.sh >"$LOG_TMP/sync_dirty_out" 2>&1 )
DIRTY_RC=$?
SHA_AFTER_DIRTY=$(sha256sum "$FX/primary_dirty/FILE.txt" | awk '{print $1}')
rm -f "$LOG_TMP/sync_dirty_out"
if [[ $DIRTY_RC -ne 0 && "$SHA_AFTER_DIRTY" == "$LOCAL_SHA" ]]; then
  ok "colliding local edit aborts non-zero and survives untouched (no --hard, no silent desync)"
else
  bad "colliding local edit handling" "rc=$DIRTY_RC local=$LOCAL_SHA after=$SHA_AFTER_DIRTY"
fi

echo ""
echo "== Criterion 7b: non-colliding local edit survives AND the sync still completes =="
git -C "$FX/primary_dirty2" fetch -q origin main
echo "unrelated-local-note" > "$FX/primary_dirty2/OTHER.txt"
OTHER_SHA_BEFORE=$(sha256sum "$FX/primary_dirty2/OTHER.txt" | awk '{print $1}')
( cd "$FX/primary_dirty2" && bash scripts/sync-new.sh >"$LOG_TMP/sync_dirty2_out" 2>&1 )
NONCOLLIDE_RC=$?
OTHER_SHA_AFTER=$(sha256sum "$FX/primary_dirty2/OTHER.txt" | awk '{print $1}')
FILE_SHA_AFTER=$(sha256sum "$FX/primary_dirty2/FILE.txt" | awk '{print $1}')
rm -f "$LOG_TMP/sync_dirty2_out"
if [[ $NONCOLLIDE_RC -eq 0 && "$OTHER_SHA_AFTER" == "$OTHER_SHA_BEFORE" && "$FILE_SHA_AFTER" == "$ORIGIN_V2_SHA" ]]; then
  ok "non-colliding local edit is preserved and the sync completes/updates FILE.txt"
else
  bad "non-colliding local edit handling" "rc=$NONCOLLIDE_RC other_before=$OTHER_SHA_BEFORE other_after=$OTHER_SHA_AFTER file_after=$FILE_SHA_AFTER"
fi

echo ""
echo "== Criterion 12/13: refuses from a linked worktree, names the remedy, HEAD unchanged =="
git -C "$FX/primary_wt" worktree add -q -b wt-branch "$FX/wt1"
WT_HEAD_BEFORE=$(git -C "$FX/wt1" symbolic-ref HEAD)
( cd "$FX/wt1" && bash scripts/sync-new.sh >"$LOG_TMP/sync_wt_out" 2>&1 )
WT_RC=$?
WT_OUT=$(cat "$LOG_TMP/sync_wt_out" 2>/dev/null || true)
WT_HEAD_AFTER=$(git -C "$FX/wt1" symbolic-ref HEAD)
rm -f "$LOG_TMP/sync_wt_out"
if [[ $WT_RC -ne 0 && "$WT_HEAD_AFTER" == "$WT_HEAD_BEFORE" ]]; then
  ok "refuses from a linked worktree; that worktree's HEAD is unchanged"
else
  bad "linked-worktree refusal" "rc=$WT_RC head_before=$WT_HEAD_BEFORE head_after=$WT_HEAD_AFTER"
fi
if printf '%s' "$WT_OUT" | grep -qF "main checkout"; then
  ok "refusal message names the remedy (points at the main checkout)"
else
  bad "refusal message names the remedy" "output was: $WT_OUT"
fi

echo ""
echo "== Criterion 10: refusal is immune to a spoofed AUTONOMOUS_TEAM_REPO_ROOT =="
WT_HEAD_BEFORE_ENV=$(git -C "$FX/wt1" symbolic-ref HEAD)
( cd "$FX/wt1" && AUTONOMOUS_TEAM_REPO_ROOT=/tmp/totally-bogus-does-not-exist bash scripts/sync-new.sh >"$LOG_TMP/sync_env_out" 2>&1 )
ENV_RC=$?
WT_HEAD_AFTER_ENV=$(git -C "$FX/wt1" symbolic-ref HEAD)
rm -f "$LOG_TMP/sync_env_out"
if [[ $ENV_RC -ne 0 && "$WT_HEAD_AFTER_ENV" == "$WT_HEAD_BEFORE_ENV" ]]; then
  ok "refusal holds even with AUTONOMOUS_TEAM_REPO_ROOT spoofed to a nonexistent path"
else
  bad "env-immunity" "rc=$ENV_RC before=$WT_HEAD_BEFORE_ENV after=$WT_HEAD_AFTER_ENV"
fi

echo ""
echo "== Criterion 12 (other direction): sync still PROCEEDS from the primary checkout =="
SHA_PWT_BEFORE=$(sha256sum "$FX/primary_wt/FILE.txt" | awk '{print $1}')
( cd "$FX/primary_wt" && bash scripts/sync-new.sh >"$LOG_TMP/sync_pwt_out" 2>&1 )
PWT_RC=$?
SHA_PWT_AFTER=$(sha256sum "$FX/primary_wt/FILE.txt" | awk '{print $1}')
rm -f "$LOG_TMP/sync_pwt_out"
if [[ $PWT_RC -eq 0 && "$SHA_PWT_AFTER" == "$ORIGIN_V2_SHA" ]]; then
  ok "sync proceeds and updates content from the primary checkout, even though it has a linked worktree"
else
  bad "primary-checkout proceed path" "rc=$PWT_RC before=$SHA_PWT_BEFORE after=$SHA_PWT_AFTER"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
