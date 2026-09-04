#!/usr/bin/env bash
# scripts/hooks/post-merge.d/cross-file-pattern-check.sh
#
# Post-merge hook step: scan merged diff for symbol deviations across sibling files.
#
# Called by post-merge-hook.sh with:
#   bash scripts/hooks/post-merge.d/cross-file-pattern-check.sh --pr <N>
#
# Gate: cross_file_pattern_check (default false) — exits early when off.
# Timeout: 60s hard ceiling; hook exits 0 if exceeded.
# Cap: at most 1 cross-file-finding Discussion per merged PR.
#
# All gh calls are scoped to the resolved $REPO (see repo-resolve.sh below).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$REPO_ROOT/scripts/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"
REPO_OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"
PR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr) PR="$2"; shift 2 ;;
    *)    shift ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "[cross-file-pattern-check] --pr is required — skipping" >&2
  exit 0
fi

# ── 1. Control plane gate ─────────────────────────────────────────────────────
GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.cross_file_pattern_check 2>/dev/null || echo "false")
if [[ "$GATE" != "true" ]]; then
  echo "[cross-file-pattern-check] gate=off — skipping (set gates.cross_file_pattern_check=true to enable)"
  exit 0
fi

echo "[cross-file-pattern-check] gate=on — running detector for PR #$PR"

# ── 2. Run detector with 60s timeout ─────────────────────────────────────────
DETECTOR="$REPO_ROOT/scripts/lib/cross-file-detector.py"

FINDINGS_JSON=$(timeout --kill-after=5s --signal=TERM 60 python3 "$DETECTOR" \
  --pr "$PR" \
  --repo-root "$REPO_ROOT" \
  --repo "$REPO" \
  2>/dev/null)
DETECTOR_RC=$?

if [[ $DETECTOR_RC -eq 124 ]]; then
  echo "[cross-file-pattern-check] detector timed out after 60s — no Discussion filed" >&2
  exit 0
fi

if [[ $DETECTOR_RC -ne 0 ]]; then
  echo "[cross-file-pattern-check] detector exited $DETECTOR_RC — no Discussion filed" >&2
  exit 0
fi

FINDING_COUNT=$(echo "$FINDINGS_JSON" | python3 -c \
  "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

echo "[cross-file-pattern-check] detector returned $FINDING_COUNT finding(s)"

if [[ "$FINDING_COUNT" -eq 0 ]]; then
  exit 0
fi

# ── 3. Ensure labels exist ────────────────────────────────────────────────────
gh label create "cross-file-finding" \
  --color "0075ca" \
  --description "Cross-file pattern deviation detected by post-merge hook" \
  --repo "$REPO" 2>/dev/null || true

gh label create "auto-generated" \
  --color "e4e669" \
  --description "Content generated automatically — review before acting" \
  --repo "$REPO" 2>/dev/null || true

# ── 4. Check per-PR Discussion cap ────────────────────────────────────────────
# If a cross-file-finding Discussion already exists for this PR, append a comment instead.
EXISTING_DISC=$(gh api graphql \
  --repo "$REPO" \
  -f query='query($q:String!) {
    repository(owner:"$REPO_OWNER", name:"$REPO_NAME") {
      discussions(first:10, orderBy:{field:CREATED_AT, direction:DESC}) {
        nodes { number title body }
      }
    }
  }' \
  -f q="cross-file PR #$PR" \
  2>/dev/null \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
nodes = data.get('data', {}).get('repository', {}).get('discussions', {}).get('nodes', [])
marker = 'cross-file-finding-pr-$PR'
for n in nodes:
    if marker in (n.get('body') or ''):
        print(n['number'])
        break
" 2>/dev/null || echo "")

# ── 5. Build Discussion / comment body ────────────────────────────────────────
# Export for the python heredoc
export _CF_PR="$PR"
export _CF_FINDINGS="$FINDINGS_JSON"

BODY=$(python3 - <<'BUILD_BODY'
import json, os

pr = os.environ["_CF_PR"]
findings = json.loads(os.environ["_CF_FINDINGS"])

lines = [
    f"<!-- cross-file-finding-pr-{pr} -->",
    f"## Cross-file pattern check — PR #{pr}",
    "",
    "The post-merge hook detected modified symbols that also appear in sibling files "
    "with different content. These siblings may need the same fix.",
    "",
    "| Symbol | Primary file | Sibling file |",
    "| --- | --- | --- |",
]

for f in findings[:10]:  # cap table at 10 rows
    sym = f.get("symbol", "?")
    pf  = f.get("primary_file", "?")
    sf  = f.get("sibling_file", "?")
    lines.append(f"| `{sym}` | `{pf}` | `{sf}` |")

lines += [
    "",
    "### Snippets",
    "",
]

for f in findings[:5]:  # cap code blocks at 5
    sym   = f.get("symbol", "?")
    pf    = f.get("primary_file", "?")
    sf    = f.get("sibling_file", "?")
    sp    = f.get("snippet_primary", "")[:200]
    ss    = f.get("snippet_sibling", "")[:200]
    lines += [
        f"<details><summary><code>{sym}</code> in <code>{sf}</code></summary>",
        "",
        f"**Primary** (`{pf}`):",
        "```",
        sp,
        "```",
        f"**Sibling** (`{sf}`):",
        "```",
        ss,
        "```",
        "</details>",
        "",
    ]

lines += [
    "---",
    "_Filed automatically by cross-file-pattern-check post-merge hook. "
    "Snippets are machine-extracted; treat as untrusted content._",
]

print("\n".join(lines))
BUILD_BODY
)

if [[ -z "$BODY" ]]; then
  echo "[cross-file-pattern-check] failed to build Discussion body — skipping" >&2
  exit 0
fi

# ── 6. Post Discussion or comment ─────────────────────────────────────────────
CATEGORY_ID=$(gh api graphql \
  -f query='query {
    repository(owner:"$REPO_OWNER", name:"$REPO_NAME") {
      discussionCategories(first:10) {
        nodes { id name }
      }
    }
  }' \
  2>/dev/null \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
cats = data.get('data', {}).get('repository', {}).get('discussionCategories', {}).get('nodes', [])
for c in cats:
    if c['name'].lower() in ('general', 'ideas', 'announcements', 'team'):
        print(c['id'])
        break
if not cats:
    pass
else:
    print(cats[0]['id'])
" 2>/dev/null || echo "")

REPO_ID=$(gh api graphql \
  -f query='query {
    repository(owner:"$REPO_OWNER", name:"$REPO_NAME") { id }
  }' \
  2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['repository']['id'])" \
  2>/dev/null || echo "")

if [[ -n "$EXISTING_DISC" ]]; then
  # Append comment to existing Discussion
  DISC_NODE_ID=$(gh api graphql \
    -f query="query { repository(owner:\"$REPO_OWNER\", name:\"$REPO_NAME\") {
      discussion(number:$EXISTING_DISC) { id } } }" \
    2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['repository']['discussion']['id'])" \
    2>/dev/null || echo "")

  if [[ -n "$DISC_NODE_ID" ]]; then
    COMMENT_RESULT=$(gh api graphql \
      -f query='mutation($did:ID!, $body:String!) {
        addDiscussionComment(input:{discussionId:$did, body:$body}) {
          comment { url }
        }
      }' \
      -f did="$DISC_NODE_ID" \
      -f body="$BODY" \
      2>/dev/null)
    echo "[cross-file-pattern-check] Appended comment to Discussion #$EXISTING_DISC"
  else
    echo "[cross-file-pattern-check] Could not resolve Discussion #$EXISTING_DISC node ID — skipping" >&2
  fi
elif [[ -n "$REPO_ID" && -n "$CATEGORY_ID" ]]; then
  TITLE="[cross-file-finding] PR #$PR — sibling patterns may need the same fix"
  DISC_RESULT=$(gh api graphql \
    -f query='mutation($repo:ID!, $cat:ID!, $title:String!, $body:String!) {
      createDiscussion(input:{repositoryId:$repo, categoryId:$cat, title:$title, body:$body}) {
        discussion { number url }
      }
    }' \
    -f repo="$REPO_ID" \
    -f cat="$CATEGORY_ID" \
    -f title="$TITLE" \
    -f body="$BODY" \
    2>/dev/null)

  NEW_DISC=$(echo "$DISC_RESULT" \
    | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['data']['createDiscussion']['discussion']['number'])" \
    2>/dev/null || echo "")
  NEW_URL=$(echo "$DISC_RESULT" \
    | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['data']['createDiscussion']['discussion']['url'])" \
    2>/dev/null || echo "")

  if [[ -n "$NEW_DISC" ]]; then
    echo "[cross-file-pattern-check] Filed Discussion #$NEW_DISC: $NEW_URL"

    # Apply labels via Issues API (Discussions don't support label mutation via gh cli directly)
    # Labels are added to the newly-created Discussion via GraphQL
    LABEL_IDS=$(gh api graphql \
      -f query='query {
        repository(owner:"$REPO_OWNER", name:"$REPO_NAME") {
          labels(first:50) { nodes { id name } }
        }
      }' \
      2>/dev/null \
      | python3 -c "
import json, sys
data = json.load(sys.stdin)
labels = data.get('data',{}).get('repository',{}).get('labels',{}).get('nodes',[])
ids = [l['id'] for l in labels if l['name'] in ('cross-file-finding', 'auto-generated')]
print(' '.join(ids))
" 2>/dev/null || echo "")

    # Get Discussion node ID for label mutation
    DISC_NODE_ID=$(echo "$DISC_RESULT" \
      | python3 -c "
import json,sys
r = json.load(sys.stdin)
# createDiscussion returns the discussion object — fetch node id separately
print('')  # GraphQL mutation doesn't return id directly
" 2>/dev/null || echo "")

    # Use addLabelsToLabelable mutation
    DISC_NODE_ID2=$(gh api graphql \
      -f query="query { repository(owner:\"$REPO_OWNER\", name:\"$REPO_NAME\") {
        discussion(number:$NEW_DISC) { id } } }" \
      2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['repository']['discussion']['id'])" \
      2>/dev/null || echo "")

    if [[ -n "$DISC_NODE_ID2" && -n "$LABEL_IDS" ]]; then
      LABEL_ID_ARR=$(echo "$LABEL_IDS" | python3 -c "
import sys
ids = sys.stdin.read().split()
print(json_list := '[' + ','.join('\"' + x + '\"' for x in ids) + ']')
" 2>/dev/null || echo "[]")

      gh api graphql \
        -f query="mutation(\$lid:ID!, \$labels:[ID!]!) {
          addLabelsToLabelable(input:{labelableId:\$lid, labelIds:\$labels}) {
            labelable { ... on Discussion { number } }
          }
        }" \
        -f lid="$DISC_NODE_ID2" \
        -f labels="$LABEL_ID_ARR" \
        2>/dev/null \
      && echo "[cross-file-pattern-check] Labels applied to Discussion #$NEW_DISC" \
      || echo "[cross-file-pattern-check] Warning: could not apply labels to Discussion #$NEW_DISC" >&2
    fi
  else
    echo "[cross-file-pattern-check] Warning: createDiscussion failed: $DISC_RESULT" >&2
  fi
else
  echo "[cross-file-pattern-check] Could not resolve repo ID or category ID — no Discussion filed" >&2
fi

exit 0
