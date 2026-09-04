#!/usr/bin/env bash
# scripts/replay-debater.sh — D#841 debater replay harness.
#
# Pull a list of historically-merged PRs that later turned out buggy, feed each
# (diff + reviewer comment) into the debater, score how often it would have
# flagged the bug, and write results to .autonomous-team/debater-replay-<DATE>.json.
#
# Usage:
#   scripts/replay-debater.sh                        # use default PR list (--label debater-replay)
#   scripts/replay-debater.sh --pr-list 12,34,56     # explicit PR numbers
#   scripts/replay-debater.sh --pr-list-file pr.txt  # one PR number per line
#   scripts/replay-debater.sh --dry-run              # print plan but don't spawn debater
#
# Acceptance: ≥30% (≥6/20) substantive flags → gate eligible for flip.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"
TODAY=$(date +%Y-%m-%d)
OUT="$REPO_ROOT/.autonomous-team/debater-replay-${TODAY}.json"

PR_LIST=""
PR_LIST_FILE=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr-list)      PR_LIST="$2"; shift 2 ;;
    --pr-list-file) PR_LIST_FILE="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=true; shift ;;
    *)
      echo "[replay-debater] Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

# ── Resolve PR list ─────────────────────────────────────────────────────────────
PRS=()
if [[ -n "$PR_LIST" ]]; then
  IFS=',' read -ra PRS <<< "$PR_LIST"
elif [[ -n "$PR_LIST_FILE" ]]; then
  [ -r "$PR_LIST_FILE" ] || { echo "[replay-debater] Cannot read $PR_LIST_FILE" >&2; exit 1; }
  while IFS= read -r line; do
    line=$(echo "$line" | tr -d '[:space:]')
    [ -n "$line" ] || continue
    [[ "$line" =~ ^# ]] && continue
    PRS+=("$line")
  done < "$PR_LIST_FILE"
else
  # Default: PRs labeled debater-replay (curated set of historically-buggy merges).
  mapfile -t PRS < <(gh pr list --repo "$REPO" --state closed --label debater-replay \
    --limit 50 --json number --jq '.[].number' 2>/dev/null || true)
fi

if [[ ${#PRS[@]} -eq 0 ]]; then
  echo "[replay-debater] No PRs to replay. Either tag historical PRs with the 'debater-replay' label,"
  echo "                 or pass --pr-list / --pr-list-file."
  exit 1
fi

echo "[replay-debater] Replaying ${#PRS[@]} PRs into debater (dry_run=$DRY_RUN)"
echo "[replay-debater] Output: $OUT"

# ── Per-PR replay ──────────────────────────────────────────────────────────────
RESULTS_JSON='[]'
FLAGGED=0
TOTAL=0

for PR in "${PRS[@]}"; do
  TOTAL=$((TOTAL+1))

  # Get the code-reviewer pass comment for this PR (heuristic: latest review with state APPROVED
  # or comment that mentions verdict: pass).
  REVIEWER_COMMENT=$(gh pr view "$PR" --repo "$REPO" --json comments \
    --jq '[.comments[] | select(.body | test("verdict.*pass|code-review-passed";"i"))] | last | .body // ""' 2>/dev/null || echo "")

  # Sanitize diff via inline Python (same logic as loop-phased-step5.sh _sanitize_diff).
  RAW_DIFF=$(gh pr diff "$PR" --repo "$REPO" 2>/dev/null || echo "")
  CLEAN_DIFF=$(python3 - "$RAW_DIFF" <<'PYEOF'
import re, sys
raw = sys.argv[1]
for tok in ("AGENT_OUTPUT","SPAWN_REQUEST","TERMINATE_REQUEST"):
    raw = re.sub(re.escape(tok), "[REDACTED-TOKEN]", raw)
raw = re.sub(r"STATUS:[A-Z_-]+", "[REDACTED-STATUS]", raw)
raw = re.sub(r"\`\`\`json\s*\{[^}]*\"verdict\"[^}]*\}\s*\`\`\`","[REDACTED-FENCED-ENVELOPE]", raw, flags=re.DOTALL)
# Strip chat-template / tokenizer-control tokens (CWE-20).
raw = re.sub(r"</?system>", "[REDACTED]", raw, flags=re.IGNORECASE)
raw = re.sub(r"<\|[a-zA-Z0-9_]+\|>", "[REDACTED]", raw)
raw = re.sub(r"\[/?role\]", "[REDACTED]", raw, flags=re.IGNORECASE)
if len(raw) > 8000:
    raw = raw[:8000] + "\n...[diff truncated at 8000 chars]"
sys.stdout.write(raw)
PYEOF
)

  if [ "$DRY_RUN" = "true" ]; then
    echo "[replay-debater] DRY PR#$PR diff_chars=${#CLEAN_DIFF} reviewer_chars=${#REVIEWER_COMMENT}"
    continue
  fi

  # Spawn debater with the same prompt shape used in production.
  TASK="You are the debater for PR #${PR} (REPLAY MODE — historical merge).

Reviewer: code-reviewer (FIXED ENUM).

The reviewer emitted verdict:pass on this PR. Find ONE substantive reason this PR should NOT merge. Do not nitpick style.

Reviewer comment:
${REVIEWER_COMMENT}

Sanitized PR diff:
${CLEAN_DIFF}

End with AGENT_OUTPUT envelope (verdict: pass or needs-fix)."

  ENV_OUT=$(bash "$SCRIPT_DIR/spawn-agent.sh" --role debater --task-prompt "$TASK" 2>/dev/null || echo "")
  VERDICT=$(echo "$ENV_OUT" | python3 - <<'PYEOF' 2>/dev/null || echo "skip"
import re, sys
txt = sys.stdin.read()
m = re.search(r'AGENT_OUTPUT.*?"verdict"\s*:\s*"([a-z-]+)"', txt, re.DOTALL)
print(m.group(1) if m else "skip")
PYEOF
)

  echo "[replay-debater] PR#$PR verdict=$VERDICT"
  [ "$VERDICT" = "needs-fix" ] && FLAGGED=$((FLAGGED+1))

  RESULTS_JSON=$(python3 - "$RESULTS_JSON" "$PR" "$VERDICT" <<'PYEOF'
import json, sys
data = json.loads(sys.argv[1])
data.append({"pr": int(sys.argv[2]), "verdict": sys.argv[3]})
print(json.dumps(data))
PYEOF
)
done

# ── Score & write ──────────────────────────────────────────────────────────────
if [ "$TOTAL" -gt 0 ]; then
  RATE=$(python3 -c "print(round($FLAGGED/$TOTAL,3))")
else
  RATE=0
fi

mkdir -p "$(dirname "$OUT")"
python3 - "$TODAY" "$TOTAL" "$FLAGGED" "$RATE" "$RESULTS_JSON" <<'PYEOF' > "$OUT"
import json, sys
today, total, flagged, rate, results_json = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
print(json.dumps({
  "date": today,
  "total": total,
  "flagged": flagged,
  "rate": rate,
  "threshold": 0.30,
  "pass": (rate >= 0.30) if total else False,
  "results": json.loads(results_json)
}, indent=2))
PYEOF

echo "[replay-debater] flagged=$FLAGGED/$TOTAL rate=$RATE → $OUT"
PASS_OK=$(python3 -c "print('yes' if $RATE >= 0.30 else 'no')")
echo "[replay-debater] Threshold (≥30%): $PASS_OK"
