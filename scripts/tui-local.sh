#!/usr/bin/env bash
# tui-local.sh — Launch the fulcrumaxe TUI against a local Ollama model.
#
# Prerequisites:
#   - Ollama installed (binary at ~/.local/bin/ollama or system)
#   - Ollama server running on http://localhost:11434
#   - Model pulled: `ollama pull gemma4:e4b`
#
# Usage:
#   bash scripts/tui-local.sh              # default: gemma4:e4b
#   AF_MODEL=llama3.2 bash scripts/tui-local.sh
#
# The TUI spawns backend/server.py which uses the OpenAI-compatible provider
# pointed at Ollama's /v1 endpoint — no provider code changes needed.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PATH="$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/lib:${LD_LIBRARY_PATH:-}"

# --- Ensure Ollama is running ---
if ! curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
  echo "[tui-local] Ollama not running — starting server..."
  nohup ollama serve > /tmp/ollama-server.log 2>&1 &
  disown
  # Wait up to 10s for server to be ready
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
      echo "[tui-local] Ollama ready"
      break
    fi
    sleep 1
  done
fi

# --- Ensure the model is available ---
MODEL="${AF_MODEL:-gemma4:e4b}"
if ! ollama list | awk 'NR>1 {print $1}' | grep -q "^${MODEL}$"; then
  echo "[tui-local] Model $MODEL not found — pulling..."
  ollama pull "$MODEL"
fi

# --- Configure the TUI backend to use Ollama ---
export AF_PROVIDER=openai
export AF_MODEL="$MODEL"
export AF_BASE_URL="http://localhost:11434/v1"
export AF_API_KEY="ollama"   # Ollama doesn't need auth, but the provider requires a non-empty key
export AF_MAX_TOKENS="16384"   # gemma4 uses lots of tokens for reasoning before tool calls

echo "[tui-local] WARNING: the prompt lane now runs on the Claude Agent SDK — Claude"
echo "[tui-local] models only. AF_PROVIDER / AF_BASE_URL / AF_API_KEY / AF_MAX_TOKENS"
echo "[tui-local] below are NOT honored anymore and this script does NOT route to Ollama."
echo "[tui-local] The backend will instead use your real CLAUDE_CODE_OAUTH_TOKEN /"
echo "[tui-local] ANTHROPIC_API_KEY / \`claude login\` credentials. See wiki/Local-Ollama-TUI.md."
echo ""
echo "[tui-local] Starting TUI with (legacy vars, currently inert):"
echo "  Provider: $AF_PROVIDER"
echo "  Model:    $AF_MODEL"
echo "  Base URL: $AF_BASE_URL"
echo ""

# --- Launch the TUI ---
cd tui
if [ ! -d node_modules ]; then
  echo "[tui-local] Installing TUI dependencies..."
  npm install
fi
if [ ! -d dist ]; then
  echo "[tui-local] Building TUI..."
  npm run build
fi

exec node dist/index.js "$@"
