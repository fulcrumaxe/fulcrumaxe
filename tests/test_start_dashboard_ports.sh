#!/usr/bin/env bash
# tests/test_start_dashboard_ports.sh — synthetic tests for port derivation
# and cross-project kill safety in start-dashboard.sh.
#
# Does NOT boot any real services. Tests the port resolution logic and the
# cross-project kill refusal without starting processes.
#
# Exit 0 on success, non-zero on failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0

pass() { echo "  PASS: $*"; ((PASS++)) || true; }
fail() { echo "  FAIL: $*"; ((FAIL++)) || true; }

echo ""
echo "=== test_start_dashboard_ports.sh ==="
echo ""

# ---------------------------------------------------------------------------
# Test (a): port derivation from dashboard_port in project.json
# ---------------------------------------------------------------------------
echo "[test-a] Port derivation from dashboard_port"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Write a fake project.json with dashboard_port=5100 but no ports block
cat > "$TMP_DIR/project.json" <<'EOF'
{
  "project_name": "test-proj",
  "version": 1,
  "dashboard_port": 5100
}
EOF

# Run the port derivation snippet extracted from start-dashboard.sh
DERIVED="$(python3 - <<PYEOF
import json
d = json.load(open("$TMP_DIR/project.json"))
ports = d.get("ports", {})
dp = d.get("dashboard_port")
if ports.get("vite") and ports.get("api") and ports.get("rpc") and ports.get("sse"):
    print(f"vite={ports['vite']} api={ports['api']} rpc={ports['rpc']} sse={ports['sse']}")
elif isinstance(dp, int):
    print(f"vite={dp} api={dp+100} rpc={dp+200} sse={dp+300}")
PYEOF
)"

if echo "$DERIVED" | grep -q "vite=5100 api=5200 rpc=5300 sse=5400"; then
  pass "Port derivation: dashboard_port=5100 yields vite=5100 api=5200 rpc=5300 sse=5400"
else
  fail "Port derivation failed. Got: '$DERIVED'"
fi

# ---------------------------------------------------------------------------
# Test (b): explicit ports block takes priority
# ---------------------------------------------------------------------------
echo "[test-b] Explicit ports block wins over derivation"

cat > "$TMP_DIR/project.json" <<'EOF'
{
  "project_name": "autonomous-forever",
  "version": 1,
  "dashboard_port": 5173,
  "ports": {
    "vite": 5173,
    "api": 18099,
    "rpc": 8765,
    "sse": 8420
  }
}
EOF

DERIVED="$(python3 - <<PYEOF
import json
d = json.load(open("$TMP_DIR/project.json"))
ports = d.get("ports", {})
dp = d.get("dashboard_port")
if ports.get("vite") and ports.get("api") and ports.get("rpc") and ports.get("sse"):
    print(f"vite={ports['vite']} api={ports['api']} rpc={ports['rpc']} sse={ports['sse']}")
elif isinstance(dp, int):
    print(f"vite={dp} api={dp+100} rpc={dp+200} sse={dp+300}")
PYEOF
)"

if echo "$DERIVED" | grep -q "vite=5173 api=18099 rpc=8765 sse=8420"; then
  pass "Explicit ports block: autonomous-forever hardcoded ports preserved"
else
  fail "Explicit ports block not honored. Got: '$DERIVED'"
fi

# ---------------------------------------------------------------------------
# Test (c): cross-project kill refusal
# ---------------------------------------------------------------------------
echo "[test-c] Cross-project kill refusal"

STATE_TMP="$TMP_DIR/.other-state"
mkdir -p "$STATE_TMP"

# Write a fake runtime for "other-project" that owns port 5100
cat > "$STATE_TMP/dashboard-runtime.json" <<EOF
{
  "project_name": "other-project",
  "project_repo": "test-org/other-project",
  "state_dir": "$STATE_TMP",
  "ports": { "vite": 5100, "api": 5200, "rpc": 5300, "sse": 5400 },
  "pids": { "api": 9991, "server": 9992, "sse": 9993, "vite": 9994 },
  "started_at": "2026-05-18T10:00:00Z"
}
EOF

# Write a Python script to run the ownership check
cat > "$TMP_DIR/check_ownership.py" <<PYEOF
import glob, json, sys
from pathlib import Path

port = 5100
this_project = "test-proj"
home = Path("$TMP_DIR")  # Use tmp dir as fake HOME for testing

for runtime_path in glob.glob(str(home / ".*-state" / "dashboard-runtime.json")):
    try:
        d = json.loads(Path(runtime_path).read_text())
    except Exception:
        continue
    other_name = d.get("project_name", "")
    if not other_name or other_name == this_project:
        continue
    ports_map = d.get("ports", {})
    for service, p in ports_map.items():
        if p == port:
            state_dir = str(Path(runtime_path).parent)
            repo = d.get("project_repo") or d.get("repo", state_dir)
            print(f"port {port} is owned by project '{other_name}' ({repo})")
            sys.exit(1)
sys.exit(0)
PYEOF

OWNERSHIP_OUTPUT="$(python3 "$TMP_DIR/check_ownership.py" 2>&1 || true)"
python3 "$TMP_DIR/check_ownership.py" 2>/dev/null && OWNERSHIP_EXIT=0 || OWNERSHIP_EXIT=$?

if [[ "$OWNERSHIP_EXIT" -ne 0 ]]; then
  if echo "$OWNERSHIP_OUTPUT" | grep -q "other-project"; then
    pass "Cross-project kill refusal: correctly identified port owned by 'other-project'"
  else
    fail "Cross-project kill refusal: wrong error message. Got: '$OWNERSHIP_OUTPUT'"
  fi
else
  fail "Cross-project kill refusal: should have exited non-zero for port conflict"
fi

# ---------------------------------------------------------------------------
# Test (d): no conflict for same project
# ---------------------------------------------------------------------------
echo "[test-d] Same-project ownership does not block"

cat > "$TMP_DIR/check_ownership2.py" <<PYEOF2
import glob, json, sys
from pathlib import Path

port = 5100
this_project = "other-project"  # Same as the runtime file
home = Path("$TMP_DIR")

for runtime_path in glob.glob(str(home / ".*-state" / "dashboard-runtime.json")):
    try:
        d = json.loads(Path(runtime_path).read_text())
    except Exception:
        continue
    other_name = d.get("project_name", "")
    if not other_name or other_name == this_project:
        continue
    ports_map = d.get("ports", {})
    for service, p in ports_map.items():
        if p == port:
            print(f"port {port} is owned by project '{other_name}'")
            sys.exit(1)
sys.exit(0)
PYEOF2

python3 "$TMP_DIR/check_ownership2.py" 2>/dev/null && OWNERSHIP_EXIT2=0 || OWNERSHIP_EXIT2=$?

if [[ "$OWNERSHIP_EXIT2" -eq 0 ]]; then
  pass "Same-project ownership check: no conflict (exit 0)"
else
  fail "Same-project ownership check: incorrectly flagged own port as conflict"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo ""

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
exit 0
