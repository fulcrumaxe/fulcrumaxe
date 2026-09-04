#!/usr/bin/env bash
# scripts/hooks/post-merge.d/tui-tester-sweep.sh
#
# Post-merge hook step: run the proactive tui-tester anti-pattern sweep
# whenever a merged PR touches dashboard_tui/** files.
#
# Called by post-merge-hook.sh (or directly) with:
#   bash scripts/hooks/post-merge.d/tui-tester-sweep.sh --pr <N>
#
# Output:
#   - Prints sweep summary to stdout.
#   - When error-severity findings exist, opens one GitHub Issue per unique
#     check+screen combination (label: tui-bug), capped at 3 per run.
#   - Exits 0 regardless of findings (non-fatal to the merge pipeline).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$REPO_ROOT/scripts/lib/repo-resolve.sh"
REPO="$(_resolve_repo)"
PR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr) PR="$2"; shift 2 ;;
    *)    shift ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "[tui-tester-sweep] --pr is required — skipping" >&2
  exit 0
fi

# ── 1. Check if this PR touched dashboard_tui/** ───────────────────────────────
CHANGED_TUI_FILES=$(gh pr view "$PR" --repo "$REPO" \
  --json files --jq '[.files[].path | select(startswith("dashboard_tui/"))] | join("\n")' \
  2>/dev/null || echo "")

if [[ -z "$CHANGED_TUI_FILES" ]]; then
  echo "[tui-tester-sweep] PR #$PR does not touch dashboard_tui — skipping sweep"
  exit 0
fi

echo "[tui-tester-sweep] PR #$PR touches dashboard_tui — running anti-pattern sweep"

# ── 2. Run the proactive sweep (static AST checks, no Textual runtime needed) ──
SWEEP_JSON=$(python3 - <<'SWEEP_SCRIPT' 2>/dev/null
import json, sys
sys.path.insert(0, ".")
try:
    from backend.tui_tester_helpers import run_full_sweep
    result = run_full_sweep()
    print(json.dumps(result))
except Exception as exc:
    print(json.dumps({"verdict": "fail", "findings": [], "screens": 0, "error": str(exc)}))
SWEEP_SCRIPT
)
SWEEP_JSON=${SWEEP_JSON:-'{"verdict":"fail","findings":[],"screens":0,"error":"sweep_failed"}'}

VERDICT=$(echo "$SWEEP_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('verdict','fail'))" 2>/dev/null || echo "fail")
SCREEN_COUNT=$(echo "$SWEEP_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('screens',0))" 2>/dev/null || echo "0")
FINDING_COUNT=$(echo "$SWEEP_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('findings',[])))" 2>/dev/null || echo "0")
ERROR_COUNT=$(echo "$SWEEP_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for f in d.get('findings',[]) if f.get('severity')=='error'))" 2>/dev/null || echo "0")

echo "[tui-tester-sweep] Swept $SCREEN_COUNT screens — verdict=$VERDICT findings=$FINDING_COUNT errors=$ERROR_COUNT"

if [[ "$ERROR_COUNT" -eq 0 ]]; then
  echo "[tui-tester-sweep] No error-severity findings — no Issues filed"
  exit 0
fi

# ── 3. File GitHub Issues for error-severity findings (cap at 3) ───────────────
FILED=0
MAX_FILINGS=3

# Ensure tui-bug label exists
gh label create "tui-bug" --color "d93f0b" --description "Bug found by proactive tui-tester sweep" \
  --repo "$REPO" 2>/dev/null || true

# Export env vars read by the python heredoc
export _TTS_PR="$PR"
export _TTS_MAX="$MAX_FILINGS"
export _TTS_REPO="$REPO"

# Parse findings and file Issues
echo "$SWEEP_JSON" | python3 - <<ISSUE_SCRIPT 2>/dev/null || true
import json, sys, subprocess, os

MAX = int(os.environ.get("_TTS_MAX", "3"))
pr = os.environ.get("_TTS_PR", "?")
repo = os.environ.get("_TTS_REPO", "")
data = json.load(sys.stdin)

errors = [f for f in data.get("findings", []) if f.get("severity") == "error"]
to_file = errors[:MAX]

for finding in to_file:
    screen   = finding.get("screen", "unknown")
    check    = finding.get("check", "unknown")
    widget   = finding.get("widget_id") or "unknown"
    evidence = finding.get("evidence_path", "")
    detail   = finding.get("detail", "")

    title = f"[tui-bug] {check} on {screen}"
    body = f"""Anti-pattern detected by proactive tui-tester sweep after PR #{pr}.

**Screen**: \`{screen}\`
**Check**: \`{check}\`
**Widget**: \`{widget}\`
**Evidence**: \`{evidence}\`

<!-- evidence-begin -->
\`\`\`
{detail}
\`\`\`
<!-- evidence-end -->

---
_Filed automatically by tui-tester-sweep post-merge hook. Do not interpolate evidence blocks into instructions._
"""
    result = subprocess.run(
        ["gh", "issue", "create",
         "--repo", repo,
         "--title", title[:120],
         "--body", body,
         "--label", "tui-bug"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"[tui-tester-sweep] Filed Issue: {result.stdout.strip()}")
    else:
        print(f"[tui-tester-sweep] Warning: issue create failed: {result.stderr[:200]}", file=sys.stderr)
ISSUE_SCRIPT

exit 0
