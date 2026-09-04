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
git push
echo "Wiki synced successfully."
