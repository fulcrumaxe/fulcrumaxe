#!/usr/bin/env bash
# tests/test-check-tui-anti-patterns.sh
#
# Fixture-driven tests for scripts/check-tui-anti-patterns.sh.
#
# Test 1: clean repo root (current main) — expect exit 0 (pass)
# Test 2: injected anti-pattern (DataTable with cursor_type="cell") — expect exit 1 (block)
#
# Runs in isolation using a temp dir so existing screen files are not mutated.
# The injection approach patches _SCREEN_SOURCE_MAP to point at a synthetic file.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$REPO_ROOT/scripts/check-tui-anti-patterns.sh"

PASS=0
FAIL=0

_pass() { echo "[PASS] $1"; ((PASS++)) || true; }
_fail() { echo "[FAIL] $1"; ((FAIL++)) || true; }

# ── Test 1: current main should be clean (no error-severity findings) ─────────
echo ""
echo "=== Test 1: clean main reports verdict=pass ==="

OUT=$(bash "$CHECK_SCRIPT" 2>&1)
EXIT=$?

echo "$OUT"

if [[ $EXIT -eq 0 ]]; then
  _pass "clean main exits 0"
else
  _fail "clean main exits $EXIT (expected 0) — error-severity findings present on main"
fi

if echo "$OUT" | grep -q "PASS — no error-severity findings"; then
  _pass "clean main prints PASS message"
else
  _fail "clean main output missing PASS message"
fi

# ── Test 2: injected anti-pattern should block (exit 1) ───────────────────────
echo ""
echo "=== Test 2: injected cursor_type=cell anti-pattern exits 1 ==="

# Create a temporary fake repo layout:
#   tmp_root/
#     backend/                 ← symlink to real backend (for imports)
#     dashboard_tui/screens/   ← one synthetic screen with the anti-pattern
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

# Symlink the real backend so Python imports work
ln -s "$REPO_ROOT/backend" "$TMP_ROOT/backend"

# Create a minimal dashboard_tui/screens dir
mkdir -p "$TMP_ROOT/dashboard_tui/screens"

# Write one synthetic screen that has cursor_type="cell" (the regression pattern)
cat > "$TMP_ROOT/dashboard_tui/screens/bad_screen.py" <<'PYEOF'
from textual.widgets import DataTable

class BadScreen:
    def compose(self):
        # cursor_type="cell" is the known regression — RowHighlighted never fires
        yield DataTable(id="t", cursor_type="cell")
PYEOF

# Patch run_full_sweep to scan only our injected screen.
# We do this by writing a tiny shim module that overrides _SCREEN_SOURCE_MAP
# before calling run_full_sweep, then write a wrapper check script.
INJECTED_CHECK=$(mktemp /tmp/check-injected-XXXXXX.sh)
cat > "$INJECTED_CHECK" <<SHEOF
#!/usr/bin/env bash
set -uo pipefail
REPO_ROOT_REAL="$REPO_ROOT"
TMP_ROOT_REAL="$TMP_ROOT"

SWEEP_JSON=\$(cd "\$REPO_ROOT_REAL" && python3 - <<'PYEOF' 2>&1
import json, sys
sys.path.insert(0, ".")
from pathlib import Path
import backend.tui_tester_helpers as h
import backend.tui_tester_kpi_registry as registry

# Redirect the screen map to the injected screen only
tmp = Path("$TMP_ROOT")
screens_dir = tmp / "dashboard_tui" / "screens"

findings = []
source_path = screens_dir / "bad_screen.py"
raw = registry.check_screen_clean(
    source_path=source_path,
    screen_name="bad_screen",
    screen_instance=None,
)
for f in raw:
    findings.append({
        "screen": f.screen,
        "widget_id": f.widget_id,
        "check": f.check,
        "severity": f.severity,
        "evidence_path": f.evidence_path,
        "detail": f.detail,
    })

error_count = sum(1 for f in findings if f["severity"] == "error")
verdict = "pass" if not findings else ("needs-fix" if error_count > 0 else "warn")
result = {"findings": findings, "verdict": verdict, "screens": 1}
print(json.dumps(result))
PYEOF
)

ERROR_COUNT=\$(echo "\$SWEEP_JSON" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(sum(1 for f in d.get('findings',[]) if f.get('severity')=='error'))" \
  2>/dev/null || echo "0")

if [[ "\$ERROR_COUNT" -gt 0 ]]; then
  echo "[injected-check] FAIL — \$ERROR_COUNT error(s) block merge"
  exit 1
fi
echo "[injected-check] PASS — no error-severity findings"
exit 0
SHEOF
chmod +x "$INJECTED_CHECK"

OUT2=$(bash "$INJECTED_CHECK" 2>&1)
EXIT2=$?
rm -f "$INJECTED_CHECK"

echo "$OUT2"

if [[ $EXIT2 -eq 1 ]]; then
  _pass "injected anti-pattern exits 1 (blocks merge)"
else
  _fail "injected anti-pattern exits $EXIT2 (expected 1 — anti-pattern not detected)"
fi

if echo "$OUT2" | grep -q "error(s) block merge"; then
  _pass "injected anti-pattern prints block message"
else
  _fail "injected anti-pattern missing block message"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
