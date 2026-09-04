#!/usr/bin/env bash
# scripts/check-tui-anti-patterns.sh
#
# Pre-merge gate: run the proactive tui-tester anti-pattern sweep and exit
# non-zero if any finding has severity=error.
#
# Intent: block PR merge on genuine bugs (severity=error). Warnings (severity=warn)
# are printed but do not cause a non-zero exit — same logic as run_full_sweep but
# only the error tier blocks.
#
# Usage:
#   bash scripts/check-tui-anti-patterns.sh
#   bash scripts/check-tui-anti-patterns.sh --repo-root /path/to/repo
#
# Exit codes:
#   0   — sweep passed (no error-severity findings)
#   1   — one or more error-severity findings found (block the PR)
#   2   — sweep failed to run (Python error, missing module, etc.)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Parse optional --repo-root override (for testing)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) REPO_ROOT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

echo "[check-tui-anti-patterns] running sweep from $REPO_ROOT"

# Run the static sweep — no Textual runtime required
SWEEP_JSON=$(cd "$REPO_ROOT" && python3 - <<'SWEEP_SCRIPT' 2>&1
import json, sys
sys.path.insert(0, ".")
try:
    from backend.tui_tester_helpers import run_full_sweep
    result = run_full_sweep()
    print(json.dumps(result))
except Exception as exc:
    # Emit a structured failure so the shell can detect it
    print(json.dumps({"verdict": "fail", "findings": [], "screens": 0, "error": str(exc)}))
    sys.exit(2)
SWEEP_SCRIPT
)

EXIT_CODE=$?

# If Python itself exited non-zero something went badly wrong
if [[ $EXIT_CODE -ne 0 ]]; then
  echo "[check-tui-anti-patterns] ERROR: sweep script exited $EXIT_CODE" >&2
  echo "$SWEEP_JSON" >&2
  exit 2
fi

# Extract counts from JSON
VERDICT=$(echo "$SWEEP_JSON" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d.get('verdict','fail'))" 2>/dev/null || echo "fail")
SCREEN_COUNT=$(echo "$SWEEP_JSON" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d.get('screens',0))" 2>/dev/null || echo "0")
FINDING_COUNT=$(echo "$SWEEP_JSON" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(len(d.get('findings',[])))" 2>/dev/null || echo "0")
ERROR_COUNT=$(echo "$SWEEP_JSON" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(sum(1 for f in d.get('findings',[]) if f.get('severity')=='error'))" \
  2>/dev/null || echo "0")
WARN_COUNT=$(echo "$SWEEP_JSON" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(sum(1 for f in d.get('findings',[]) if f.get('severity')=='warn'))" \
  2>/dev/null || echo "0")

echo "[check-tui-anti-patterns] swept $SCREEN_COUNT screens — verdict=$VERDICT findings=$FINDING_COUNT errors=$ERROR_COUNT warnings=$WARN_COUNT"

# Print details for any error-severity findings (helps PR author fix them)
if [[ "$ERROR_COUNT" -gt 0 ]]; then
  echo ""
  echo "Error-severity findings (must be fixed before merge):"
  echo "$SWEEP_JSON" | python3 - <<'DETAIL_SCRIPT'
import json, sys
data = json.load(sys.stdin)
for f in data.get("findings", []):
    if f.get("severity") == "error":
        print(f"  [{f.get('screen','?')}] {f.get('check','?')} — {f.get('detail','')[:120]}")
DETAIL_SCRIPT
  echo ""
  echo "[check-tui-anti-patterns] FAIL — $ERROR_COUNT error(s) block merge"
  exit 1
fi

echo "[check-tui-anti-patterns] PASS — no error-severity findings"
exit 0
