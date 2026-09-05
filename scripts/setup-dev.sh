#!/usr/bin/env bash
# One-command dev environment setup for autonomous-forever.
# Checks prerequisites, creates virtualenv, installs deps, initializes DB,
# copies .env.example → .env, and runs the test suite.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo "=== autonomous-forever dev setup ==="
echo ""

# ── Prerequisites ──────────────────────────────────────────────────────────

echo "Checking prerequisites..."

# Python 3.10+
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.10+ and re-run."
fi
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]; }; then
    fail "Python 3.10+ required (found $PYTHON_VERSION)"
fi
ok "Python $PYTHON_VERSION"

# Node.js 18+
if ! command -v node &>/dev/null; then
    warn "Node.js not found — TUI build will be skipped. Install Node.js 18+ to enable."
    HAS_NODE=false
else
    NODE_MAJOR=$(node --version | sed 's/v//' | cut -d. -f1)
    if [ "$NODE_MAJOR" -lt 18 ]; then
        warn "Node.js 18+ recommended (found $(node --version)) — TUI build may fail."
    fi
    ok "Node.js $(node --version)"
    HAS_NODE=true
fi

echo ""

# ── Python virtualenv ──────────────────────────────────────────────────────

if [ ! -d ".venv" ]; then
    echo "Creating Python virtualenv..."
    python3 -m venv .venv
    ok "Created .venv"
else
    ok "Virtualenv .venv already exists"
fi

# Activate
# shellcheck source=/dev/null
source .venv/bin/activate

echo "Installing Python dependencies..."
if [ -f "requirements.txt" ]; then
    pip install -q -r requirements.txt
    ok "requirements.txt installed"
elif [ -f "backend/requirements.txt" ]; then
    pip install -q -r backend/requirements.txt
    ok "backend/requirements.txt installed"
else
    warn "No requirements.txt found — installing pytest only"
    pip install -q pytest pytest-cov
fi

# Always ensure pytest available
pip install -q pytest pytest-cov 2>/dev/null || true
echo ""

# ── Node.js / TUI ──────────────────────────────────────────────────────────

if [ "$HAS_NODE" = "true" ] && [ -d "tui" ]; then
    echo "Installing TUI dependencies..."
    (cd tui && npm install --silent)
    ok "tui/node_modules installed"
    echo ""
fi

# ── Database init ─────────────────────────────────────────────────────────

echo "Initializing SQLite database..."
mkdir -p .autonomous-team
python3 - <<'PYEOF'
import sqlite3, pathlib
db_path = pathlib.Path(".autonomous-team/state.db")
conn = sqlite3.connect(str(db_path))
conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
conn.commit()
conn.close()
print(f"  DB ready at {db_path}")
PYEOF
ok "Database initialized"
echo ""

# ── .env setup ────────────────────────────────────────────────────────────

if [ -f ".env.example" ] && [ ! -f ".env" ]; then
    cp .env.example .env
    ok "Created .env from .env.example"
elif [ -f ".env" ]; then
    ok ".env already exists"
else
    warn "No .env.example found — skipping .env creation"
fi
echo ""

# ── Run tests ─────────────────────────────────────────────────────────────

echo "Running test suite..."
if python3 -m pytest tests/ -x --tb=short -q 2>&1; then
    ok "Test suite passed"
else
    warn "Some tests failed — check output above. This may be expected on a fresh checkout."
fi
echo ""

# ── Done ──────────────────────────────────────────────────────────────────

echo -e "${GREEN}=== Setup complete ===${NC}"
echo ""
echo "Next steps:"
echo "  source .venv/bin/activate  # activate virtualenv in this shell"
echo "  make test                  # run all tests"
echo "  make lint                  # run linters"
echo "  make coverage              # generate HTML coverage report"
echo ""
