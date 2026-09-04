#!/usr/bin/env bash
# ollama-smoke-test.sh — Verify the Ollama OpenAI-compatible endpoint works
# for TUI tool use against gemma4:e4b.
#
# Tests:
#   1. Ollama server is reachable
#   2. Model is loaded
#   3. /v1/chat/completions returns a sensible response
#   4. Function calling works (required for TUI tool use)
#
# Usage: bash scripts/ollama-smoke-test.sh

set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/lib:${LD_LIBRARY_PATH:-}"

MODEL="${AF_MODEL:-gemma4:e4b}"
FAILURES=0

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES+1)); }

echo "=== Ollama smoke test — $MODEL ==="

# 1. Server reachable
echo "[1/4] Server reachable"
if curl -sf http://localhost:11434/api/version >/dev/null; then
  VER=$(curl -sf http://localhost:11434/api/version | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
  pass "ollama $VER"
else
  fail "ollama server not responding on :11434"
  exit 1
fi

# 2. Model available
echo "[2/4] Model $MODEL available"
if ollama list | awk 'NR>1 {print $1}' | grep -q "^${MODEL}$"; then
  pass "$MODEL in ollama list"
else
  fail "$MODEL not found — run: ollama pull $MODEL"
  exit 1
fi

# 3. Basic chat completion (OpenAI-compatible endpoint)
echo "[3/4] /v1/chat/completions"
RESP=$(curl -sf http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ollama" \
  -d "$(python3 -c "import json; print(json.dumps({
    'model': '$MODEL',
    'messages': [{'role': 'user', 'content': 'Reply with exactly the word: OK'}],
    'max_tokens': 16,
    'temperature': 0
  }))")" 2>&1)

if [ -z "$RESP" ]; then
  fail "empty response from /v1/chat/completions"
else
  CONTENT=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])" 2>/dev/null || echo "")
  if [ -n "$CONTENT" ]; then
    pass "got content: ${CONTENT:0:50}"
  else
    fail "malformed response: ${RESP:0:200}"
  fi
fi

# 4. Tool calling (required for TUI Agent)
echo "[4/4] Tool calling (function calling)"
TOOL_RESP=$(curl -sf http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ollama" \
  -d "$(python3 -c "import json; print(json.dumps({
    'model': '$MODEL',
    'messages': [{'role': 'user', 'content': 'What is the weather in Tokyo? Use the get_weather tool.'}],
    'tools': [{
      'type': 'function',
      'function': {
        'name': 'get_weather',
        'description': 'Get current weather for a city',
        'parameters': {
          'type': 'object',
          'properties': {
            'city': {'type': 'string'}
          },
          'required': ['city']
        }
      }
    }],
    'tool_choice': 'required',
    'max_tokens': 1024,
    'temperature': 0
  }))")" 2>&1)

TOOL_CALL=$(echo "$TOOL_RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    tc = d['choices'][0]['message'].get('tool_calls')
    if tc:
        print(tc[0]['function']['name'])
    else:
        print('NONE')
except Exception as e:
    print(f'ERR: {e}')
" 2>/dev/null)

if [ "$TOOL_CALL" = "get_weather" ]; then
  pass "model returned get_weather tool call"
elif [ "$TOOL_CALL" = "NONE" ]; then
  fail "model did not emit a tool call — TUI tools will not work"
else
  fail "tool call test error: $TOOL_CALL"
fi

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo "All smoke tests PASSED — TUI is ready to use $MODEL"
  exit 0
else
  echo "$FAILURES smoke test(s) FAILED"
  exit 1
fi
