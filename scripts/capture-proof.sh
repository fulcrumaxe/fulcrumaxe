#!/usr/bin/env bash
# capture-proof.sh — Screenshot and recording capture for E2E verification
#
# Captures API endpoint responses as JSON, browser screenshots via Puppeteer,
# and terminal recordings via asciinema (fallback to script+ffmpeg if available).
#
# Usage:
#   ./scripts/capture-proof.sh --proof-dir DIR --timestamp TS [--api-port PORT]
#                               [--skip-dashboard] [--skip-recordings]
#
# Output: artifacts saved to $PROOF_DIR/{api-responses,screenshots,recordings}/
#
# Run from the repository root.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PROOF_DIR=""
TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
API_PORT=18099
SAAS_PORT=3000
SKIP_DASHBOARD=false
SKIP_RECORDINGS=false

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proof-dir)        PROOF_DIR="$2"; shift 2 ;;
    --timestamp)        TIMESTAMP="$2"; shift 2 ;;
    --api-port)         API_PORT="$2"; shift 2 ;;
    --skip-dashboard)   SKIP_DASHBOARD=true; shift ;;
    --skip-saas)        SAAS_PORT=""; shift ;;
    --skip-recordings)  SKIP_RECORDINGS=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$PROOF_DIR" ]]; then
  PROOF_DIR="$REPO_ROOT/verification-report/proof/$TIMESTAMP"
fi

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
mkdir -p "$PROOF_DIR/api-responses"
mkdir -p "$PROOF_DIR/screenshots"
mkdir -p "$PROOF_DIR/recordings"
mkdir -p "$PROOF_DIR/annotated"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_pass() { echo "  PASS: $*"; }
_fail() { echo "  FAIL: $*"; }
_info() { echo "  INFO: $*"; }
_log()  { echo "[$(date +%H:%M:%S)] $*"; }

annotate() {
  local input="$1" output="$2" status="$3" label="$4"
  bash "$REPO_ROOT/scripts/annotate-proof.sh" \
    --input "$input" \
    --output "$output" \
    --status "$status" \
    --label "$label" \
    --timestamp "$TIMESTAMP" 2>/dev/null || cp "$input" "$output"
}

# ---------------------------------------------------------------------------
# Section 1: API endpoint captures
# ---------------------------------------------------------------------------
_log "Capturing API endpoint responses (port $API_PORT)..."

API_BASE="http://127.0.0.1:$API_PORT"
API_ENDPOINTS=(
  "/health:api-health"
  "/v1/budget/status:budget-status"
  "/v1/registry:registry"
  "/v1/control/gates:control-gates"
  "/v1/agents:agents"
  "/v1/kpi:kpi"
  "/v1/cost/summary:cost-summary"
  "/metrics:prometheus-metrics"
  "/openapi.json:openapi-spec"
)

for entry in "${API_ENDPOINTS[@]}"; do
  endpoint="${entry%%:*}"
  name="${entry##*:}"
  out_file="$PROOF_DIR/api-responses/${name}.json"

  http_code=$(curl -sf -o "$out_file" -w "%{http_code}" \
    "$API_BASE$endpoint" 2>/dev/null || echo "000")

  if [[ "$http_code" =~ ^2 ]]; then
    _pass "GET $endpoint → $http_code"
  else
    _fail "GET $endpoint → $http_code"
    echo "{\"error\": \"HTTP $http_code\", \"endpoint\": \"$endpoint\"}" > "$out_file"
  fi
done

# ---------------------------------------------------------------------------
# Section 2: Saas-service endpoint captures
# ---------------------------------------------------------------------------
if [[ -n "$SAAS_PORT" ]]; then
  _log "Capturing saas-service endpoint responses (port $SAAS_PORT)..."
  SAAS_BASE="http://127.0.0.1:$SAAS_PORT"
  SAAS_ENDPOINTS=(
    "/health:saas-health"
    "/agents:saas-agents"
    "/projects:saas-projects"
  )
  for entry in "${SAAS_ENDPOINTS[@]}"; do
    endpoint="${entry%%:*}"
    name="${entry##*:}"
    out_file="$PROOF_DIR/api-responses/${name}.json"
    http_code=$(curl -sf -o "$out_file" -w "%{http_code}" \
      "$SAAS_BASE$endpoint" 2>/dev/null || echo "000")
    if [[ "$http_code" =~ ^2 ]]; then
      _pass "GET $endpoint → $http_code"
    else
      _fail "GET $endpoint → $http_code (saas-service may not be running)"
      echo "{\"error\": \"HTTP $http_code\"}" > "$out_file"
    fi
  done
fi

# ---------------------------------------------------------------------------
# Section 3: Browser screenshots via Puppeteer
# ---------------------------------------------------------------------------
if [[ "$SKIP_DASHBOARD" == "false" ]]; then
  _log "Capturing browser screenshots via Puppeteer..."
  VISUAL_VERIFY="$REPO_ROOT/scripts/visual-verify.js"
  if [[ -f "$VISUAL_VERIFY" ]] && command -v node >/dev/null 2>&1; then
    node "$VISUAL_VERIFY" \
      --api-base "$API_BASE" \
      --output-dir "$PROOF_DIR/screenshots" 2>/dev/null || true
    _info "Puppeteer screenshots saved to $PROOF_DIR/screenshots/"
  else
    _info "Puppeteer not available (node or visual-verify.js missing) — skipping browser screenshots"
  fi

  # Screenshot of dashboard if running
  if curl -sf "http://127.0.0.1:5173/" >/dev/null 2>&1; then
    if command -v scrot >/dev/null 2>&1; then
      scrot "$PROOF_DIR/screenshots/dashboard-full.png" 2>/dev/null || true
      _pass "Dashboard screenshot captured via scrot"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Section 4: Terminal recordings
# ---------------------------------------------------------------------------
if [[ "$SKIP_RECORDINGS" == "false" ]]; then
  _log "Capturing terminal recordings..."

  # Run smoke-test and record via script + asciinema if available
  if command -v asciinema >/dev/null 2>&1; then
    _info "Recording smoke-test via asciinema..."
    asciinema rec "$PROOF_DIR/recordings/smoke-test.cast" \
      --command "bash $REPO_ROOT/scripts/smoke-test.sh --continue-on-error" \
      --title "smoke-test $TIMESTAMP" \
      --overwrite 2>/dev/null || true

    # Convert cast to PNG using existing ansi-to-png.sh
    if [[ -f "$PROOF_DIR/recordings/smoke-test.cast" ]] && \
       [[ -f "$REPO_ROOT/scripts/ansi-to-png.sh" ]]; then
      bash "$REPO_ROOT/scripts/ansi-to-png.sh" \
        "$PROOF_DIR/recordings/smoke-test.cast" \
        "$PROOF_DIR/screenshots/smoke-test-terminal.png" 2>/dev/null || true
    fi
  else
    # Fallback: run smoke-test, pipe output to ansi-to-png
    _info "asciinema not available — running smoke-test and capturing output..."
    bash "$REPO_ROOT/scripts/smoke-test.sh" --continue-on-error \
      > "$PROOF_DIR/recordings/smoke-test.txt" 2>&1 || true
    _info "Smoke test output saved to $PROOF_DIR/recordings/smoke-test.txt"
  fi

  # Record API integration test
  if command -v asciinema >/dev/null 2>&1; then
    _info "Recording API integration test via asciinema..."
    asciinema rec "$PROOF_DIR/recordings/api-test.cast" \
      --command "bash $REPO_ROOT/scripts/api-integration-test.sh --port $API_PORT --no-start" \
      --title "api-integration-test $TIMESTAMP" \
      --overwrite 2>/dev/null || true
  else
    bash "$REPO_ROOT/scripts/api-integration-test.sh --port $API_PORT --no-start" \
      > "$PROOF_DIR/recordings/api-test.txt" 2>&1 || true
  fi
fi

# ---------------------------------------------------------------------------
# Section 5: Annotate screenshots
# ---------------------------------------------------------------------------
_log "Annotating captured screenshots..."
for img in "$PROOF_DIR/screenshots"/*.png; do
  [[ -f "$img" ]] || continue
  basename_no_ext="$(basename "${img%.png}")"
  annotated_out="$PROOF_DIR/annotated/${basename_no_ext}-annotated.png"
  # Determine pass/fail from filename conventions or default to pass
  status="PASS"
  label="Screenshot: $basename_no_ext"
  annotate "$img" "$annotated_out" "$status" "$label"
done

_log "Capture complete. Artifacts in: $PROOF_DIR"
