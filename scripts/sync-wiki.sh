#!/usr/bin/env bash
# sync-wiki.sh — copies wiki/*.md into the GitHub Wiki repo and pushes.
# Run after merging changes that update wiki/ pages.
# Requires GitHub credentials with push access to the wiki repo.
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Guard: wiki/ is internal-only and doesn't ship in the open-source export
# (D#1858) — an adopter's tree legitimately has nothing to sync. Checked
# explicitly, ahead of the network round-trip below, rather than relying
# on the curl 404 guard to catch it: that guard only fires when the
# adopter hasn't enabled a GitHub Wiki, and this script runs under
# `set -euo pipefail`, so `cp -R` from a wiki/ that doesn't exist would
# otherwise be a hard crash for anyone who has.
if [ ! -d "$REPO_DIR/wiki" ]; then
  echo "no local wiki/ directory to sync — skipping"
  exit 0
fi

# Guard: this script's own copy path never reads $REPO_DIR's HEAD — it copies
# files from $REPO_DIR/wiki into a throwaway clone. But backend/status_page.py,
# invoked below to generate the wiki's "Recent commits" section, runs
# `git log --oneline -N` with no explicit ref, cwd'd at $REPO_DIR (via
# backend/repo_root.py's main_repo_root()) — so it silently reflects whatever
# $REPO_DIR's HEAD happens to be. Verified 2026-09-09: pointing status_page.py
# at a clone detached 5 commits back, and separately at one attached to a
# branch cut 8 commits back, both produced a "Recent commits" list from the
# wrong point in history with no error — the exact silent-failure shape this
# Discussion was filed over, just surfacing through status_page.py rather than
# through this script's own copy path. Warn only, don't abort: the hand-authored
# wiki/ pages and the changelog (which is gh-pr-list-based, not HEAD-based) are
# still correct, and a guard that blocks the whole sync over a reviewer's
# detached checkout would just get disabled.
if ! CURRENT_BRANCH=$(git -C "$REPO_DIR" symbolic-ref -q --short HEAD 2>/dev/null); then
  echo "WARNING: $REPO_DIR is in detached-HEAD state — the wiki's Recent Commits section will reflect this HEAD position, not main" >&2
elif [[ "$CURRENT_BRANCH" != "main" ]]; then
  echo "WARNING: $REPO_DIR is on branch '$CURRENT_BRANCH', not main — the wiki's Recent Commits section will reflect this branch, not main" >&2
fi

# Test seam: tests/test_sync_wiki_guards.sh needs to drive the real clone/
# generate/commit/push path (including the push-race retry below) against a
# local bare repo instead of a live GitHub wiki, since there's no way to make
# the HTTPS probe below or a real `github.com` clone/push succeed in a test
# sandbox. Production never sets this, so production always takes the normal
# resolve-and-probe path.
if [[ -n "${SYNC_WIKI_URL_OVERRIDE:-}" ]]; then
  WIKI_URL="$SYNC_WIKI_URL_OVERRIDE"
else
  source "$SCRIPT_DIR/lib/repo-resolve.sh"
  _REPO="$(_resolve_repo)"

  # Guard: probe whether the wiki repo exists before cloning.
  # GitHub returns 404 when wiki is not enabled or has never been initialised.
  WIKI_URL="https://github.com/${_REPO}.wiki.git"
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$WIKI_URL/info/refs?service=git-upload-pack" 2>/dev/null || echo "000")
  if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "wiki not enabled (HTTP $HTTP_STATUS) — skipping sync"
    exit 0
  fi
fi

WIKI_DIR=$(mktemp -d)
trap "rm -rf \"$WIKI_DIR\"" EXIT

echo "Cloning wiki repo..."
git clone "$WIKI_URL" "$WIKI_DIR"

echo "Copying wiki pages..."
# Recursive copy so subdir pages (runbooks/, postmortems/, analytics/, etc.)
# sync to the live wiki too — GitHub Wiki repos are flat internally but
# resolve nested paths fine as long as the files exist at those paths.
cp -R "$REPO_DIR"/wiki/. "$WIKI_DIR/"

# Generated pages write into the clone, never into the source tree — their
# consumer is the GitHub Wiki, not the checkout (D#1908). Each tolerates
# failure the same way post-merge-wiki.sh used to (`|| true`) so a `gh`
# hiccup doesn't abort the sync of the hand-authored pages just copied above.
echo "Generating status page into wiki clone..."
python3 "$REPO_DIR/backend/status_page.py" generate --output-dir "$WIKI_DIR" 2>&1 || true

echo "Generating changelog into wiki clone..."
python3 "$REPO_DIR/backend/changelog.py" generate --output-dir "$WIKI_DIR" 2>&1 || true

cd "$WIKI_DIR"
git add -A

if git diff --cached --quiet; then
  echo "Wiki is up to date — nothing to push."
  exit 0
fi

git commit -m "sync wiki from main repo (auto-generated)"

# Push-race handling: retry with pull --rebase if push is rejected.
#
# Concurrent merges can each clone, generate, and reach this point around the
# same time. Whichever clone pushes second loses to git's non-fast-forward
# rejection — and because the whole clone lives in the mktemp -d above, which
# the `trap ... EXIT` deletes on exit, an unhandled rejection silently throws
# away this run's generated content instead of just failing loudly.
# `--force-with-lease` alone would "fix" the rejection by overwriting whichever
# run pushed first, which loses content rather than saving it. Instead: on
# rejection, rebase this commit onto the competing update and retry, so both
# land.
_WIKI_PUSH_MAX_ATTEMPTS=5
_wiki_push_attempt=1
until git push; do
  if [ "$_wiki_push_attempt" -ge "$_WIKI_PUSH_MAX_ATTEMPTS" ]; then
    echo "FATAL: wiki push rejected $_WIKI_PUSH_MAX_ATTEMPTS times in a row (push race) — giving up" >&2
    exit 1
  fi
  echo "wiki push rejected (attempt $_wiki_push_attempt/$_WIKI_PUSH_MAX_ATTEMPTS) — a competing update landed first, rebasing and retrying..." >&2
  if ! git pull --rebase; then
    echo "FATAL: git pull --rebase failed while retrying the wiki push — resolve manually in the wiki repo" >&2
    exit 1
  fi
  _wiki_push_attempt=$((_wiki_push_attempt + 1))
done
echo "Wiki synced successfully."
