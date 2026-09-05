#!/usr/bin/env bash
# tui-tester-pilot-sweep.sh — scheduled job: run Pilot sweep and file Discussions on failures.
#
# Runs every 15 min via the cron-bridge dispatcher (see scripts/schedule/jobs.yaml).
# token_ceiling: 0 — pure Python, no agent spawn.
#
# Gate: gates.tui_tester_pilot_sweep (default false). Exits 0 immediately when off.
#
# Rate limit: max 1 Discussion per (screen, check_name) per hour.
# Dismissed-pair cache: $STATE_DIR/tui-tester/dismissed-pairs.json
#   { "<screen>:<check_name>": "<ISO8601 last-filed timestamp>", ... }
#
# Discussion cap: 3 per run.
#
# Exit codes: 0 = success or gate-off, 1 = sweep failure

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$REPO_ROOT/scripts/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"
REPO_OWNER="${REPO%%/*}"
REPO_NAME="${REPO##*/}"

STATE_DIR="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"
DISMISSED_PAIRS_FILE="$STATE_DIR/tui-tester/dismissed-pairs.json"
MAX_DISCUSSIONS_PER_RUN=3

# ── 1. Control plane gate ─────────────────────────────────────────────────────
GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get gates.tui_tester_pilot_sweep 2>/dev/null || echo "false")
if [[ "$GATE" != "true" ]]; then
  echo "[tui-tester-pilot-sweep] gate=off — skipping (set gates.tui_tester_pilot_sweep=true to enable)"
  exit 0
fi

echo "[tui-tester-pilot-sweep] gate=on — running Pilot sweep"

# ── 2. Run pilot sweep ────────────────────────────────────────────────────────
SWEEP_OUTPUT=$(python3 "$REPO_ROOT/scripts/tui-tester/pilot-sweep.py" \
  --state-dir "$STATE_DIR" 2>&1)
SWEEP_RC=$?

if [[ $SWEEP_RC -eq 2 ]]; then
  echo "[tui-tester-pilot-sweep] sweep returned exit 2 (fail) — skipping Discussion filing" >&2
  echo "$SWEEP_OUTPUT" >&2
  exit 1
fi

# Extract the JSON result from the last line of stdout (pilot-sweep.py emits compact JSON last)
FINDINGS_JSON=$(echo "$SWEEP_OUTPUT" | tail -1)

VERDICT=$(echo "$FINDINGS_JSON" | python3 -c \
  "import json,sys; print(json.load(sys.stdin).get('verdict','fail'))" 2>/dev/null || echo "fail")

echo "[tui-tester-pilot-sweep] sweep verdict=$VERDICT"

if [[ "$VERDICT" == "pass" ]]; then
  echo "[tui-tester-pilot-sweep] no findings — done"
  exit 0
fi

# ── 3. Extract failed findings ────────────────────────────────────────────────
FAILED_FINDINGS=$(echo "$FINDINGS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
findings = data.get('findings', [])
failed = [f for f in findings if f.get('status') == 'fail']
print(json.dumps(failed))
" 2>/dev/null || echo "[]")

FAILED_COUNT=$(echo "$FAILED_FINDINGS" | python3 -c \
  "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

echo "[tui-tester-pilot-sweep] failed findings=$FAILED_COUNT"

if [[ "$FAILED_COUNT" -eq 0 ]]; then
  exit 0
fi

# ── 4. Load dismissed-pair cache ──────────────────────────────────────────────
mkdir -p "$(dirname "$DISMISSED_PAIRS_FILE")"
if [[ ! -f "$DISMISSED_PAIRS_FILE" ]]; then
  echo '{}' > "$DISMISSED_PAIRS_FILE"
fi

NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ── 5. Resolve GitHub repo + Discussion category ──────────────────────────────
REPO_ID=$(gh api graphql \
  -f query='query { repository(owner:"'"$REPO_OWNER"'", name:"'"$REPO_NAME"'") { id } }' \
  2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['repository']['id'])" \
  2>/dev/null || echo "")

CATEGORY_ID=$(gh api graphql \
  -f query='query { repository(owner:"'"$REPO_OWNER"'", name:"'"$REPO_NAME"'") {
    discussionCategories(first:10) { nodes { id name } }
  } }' \
  2>/dev/null \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
cats = data.get('data', {}).get('repository', {}).get('discussionCategories', {}).get('nodes', [])
for c in cats:
    if c['name'].lower() in ('general', 'ideas', 'announcements', 'team'):
        print(c['id'])
        break
if cats and not any(c['name'].lower() in ('general', 'ideas', 'announcements', 'team') for c in cats):
    print(cats[0]['id'])
" 2>/dev/null || echo "")

if [[ -z "$REPO_ID" || -z "$CATEGORY_ID" ]]; then
  echo "[tui-tester-pilot-sweep] could not resolve repo/category IDs — skipping Discussion filing" >&2
  exit 0
fi

# ── 6. File Discussions for new findings (rate-limited) ───────────────────────
DISCUSSIONS_FILED=0

while IFS= read -r FINDING_JSON; do
  [[ "$DISCUSSIONS_FILED" -ge "$MAX_DISCUSSIONS_PER_RUN" ]] && break

  SCREEN=$(echo "$FINDING_JSON" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('tab','unknown'))" 2>/dev/null || echo "unknown")
  CHECK_NAME=$(echo "$FINDING_JSON" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('check_name','unknown'))" 2>/dev/null || echo "unknown")
  DETAIL=$(echo "$FINDING_JSON" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('detail',''))" 2>/dev/null || echo "")

  PAIR_KEY="${SCREEN}:${CHECK_NAME}"

  # Check rate limit: skip if filed within the last hour
  SKIP=$(DISMISSED_PAIRS_FILE="$DISMISSED_PAIRS_FILE" PAIR_KEY="$PAIR_KEY" python3 - <<'RATE_CHECK'
import json, os, sys
from datetime import datetime, timezone, timedelta

dismissed_path = os.environ["DISMISSED_PAIRS_FILE"]
pair_key = os.environ["PAIR_KEY"]

try:
    cache = json.loads(open(dismissed_path).read())
except Exception:
    cache = {}

last_filed = cache.get(pair_key, "")
if last_filed:
    try:
        last_dt = datetime.fromisoformat(last_filed.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        if now_dt - last_dt < timedelta(hours=1):
            print("skip")
            sys.exit(0)
    except Exception:
        pass

print("file")
RATE_CHECK
  )

  if [[ "$SKIP" == "skip" ]]; then
    echo "[tui-tester-pilot-sweep] rate-limited: $PAIR_KEY (last filed within 1h) — skipping"
    continue
  fi

  # Build Discussion body
  BODY=$(SCREEN="$SCREEN" CHECK_NAME="$CHECK_NAME" DETAIL="$DETAIL" python3 - <<'BUILD_BODY'
import json, os

screen = os.environ.get("SCREEN", "")
check = os.environ.get("CHECK_NAME", "")
detail = os.environ.get("DETAIL", "")

lines = [
    "<!-- tui-tester-pilot-sweep -->",
    f"## TUI tester: `{screen}` / `{check}` failed",
    "",
    "The cron-driven Pilot sweep detected a data check failure.",
    "",
    f"**Screen**: `{screen}`",
    f"**Check**: `{check}`",
]
if detail:
    lines += [
        "",
        "**Detail**:",
        "```",
        detail[:500],
        "```",
    ]
lines += [
    "",
    "---",
    "_Filed automatically by tui-tester-pilot-sweep scheduled job._",
    "_To dismiss permanently: update the dismissed-pairs cache in_",
    "_\`\$STATE_DIR/tui-tester/dismissed-pairs.json\`._",
]
print("\n".join(lines))
BUILD_BODY
  )

  TITLE="[tui-tester] ${SCREEN}/${CHECK_NAME} check failed"

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
    --repo "$REPO" \
    2>/dev/null)
  DISC_RC=$?

  if [[ $DISC_RC -ne 0 ]]; then
    echo "[tui-tester-pilot-sweep] Discussion creation failed (exit $DISC_RC) — skipping" >&2
    continue
  fi

  NEW_DISC=$(echo "$DISC_RESULT" \
    | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['data']['createDiscussion']['discussion']['number'])" \
    2>/dev/null || echo "")

  if [[ -z "$NEW_DISC" ]]; then
    echo "[tui-tester-pilot-sweep] could not parse Discussion number from response — skipping" >&2
    continue
  fi

  echo "[tui-tester-pilot-sweep] filed Discussion #$NEW_DISC for $PAIR_KEY"
  DISCUSSIONS_FILED=$((DISCUSSIONS_FILED + 1))

  # Update dismissed-pair cache with current timestamp
  DISMISSED_PAIRS_FILE="$DISMISSED_PAIRS_FILE" PAIR_KEY="$PAIR_KEY" NOW_ISO="$NOW_ISO" python3 - <<'UPDATE_CACHE'
import json, os
from pathlib import Path

cache_path = Path(os.environ["DISMISSED_PAIRS_FILE"])
pair_key = os.environ["PAIR_KEY"]
now_iso = os.environ["NOW_ISO"]

try:
    cache = json.loads(cache_path.read_text())
except Exception:
    cache = {}

cache[pair_key] = now_iso
cache_path.write_text(json.dumps(cache, indent=2))
UPDATE_CACHE

done < <(echo "$FAILED_FINDINGS" | python3 -c "
import json, sys
findings = json.load(sys.stdin)
for f in findings:
    print(json.dumps(f))
")

echo "[tui-tester-pilot-sweep] done — filed=$DISCUSSIONS_FILED"
exit 0
