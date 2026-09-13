#!/usr/bin/env bash
# tests/test_telemetry_level0_bash.sh — level-0 network guard for
# scripts/lib/telemetry.sh (D#2565, AC-3 and AC-7's bash half).
#
# A pytest socket guard proves nothing about scripts/lib/telemetry.sh — the
# send is wired into a bash ritual, and a curl subprocess is invisible to a
# Python-level socket.socket patch. This stubs `curl` first on PATH instead:
# it touches a marker file and exits 0, so if telemetry.sh ever shells out
# to curl while it shouldn't, the marker's existence catches it.
#
# Run: bash tests/test_telemetry_level0_bash.sh
# Expects: all assertions pass, exit 0
#
# Follows this repo's plain-bash test-script convention (see
# tests/test_ci_status_check.sh) rather than bats.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TELEMETRY_SH="$REPO_ROOT/scripts/lib/telemetry.sh"

PASS=0
FAIL=0

assert_exit_0() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then echo "  PASS: $label (exit 0)"; PASS=$((PASS + 1));
  else echo "  FAIL: $label (expected exit 0, got $rc)"; FAIL=$((FAIL + 1)); fi
}

assert_file_absent() {
  local label="$1" path="$2"
  if [ ! -e "$path" ]; then echo "  PASS: $label"; PASS=$((PASS + 1));
  else echo "  FAIL: $label — $path exists"; FAIL=$((FAIL + 1)); fi
}

# A scratch state dir for every case below — never the production dir.
SCRATCH_STATE_DIR="$(mktemp -d)"
CONFIG_SCRATCH="$(mktemp -d)"
STUB_DIR="$(mktemp -d)"
CURL_MARKER="$STUB_DIR/curl-was-called"

cleanup() {
  rm -rf "$SCRATCH_STATE_DIR" "$CONFIG_SCRATCH" "$STUB_DIR"
}
trap cleanup EXIT

# Stub curl: touches the marker, exits 0. Placed first on PATH.
cat > "$STUB_DIR/curl" <<EOF
#!/usr/bin/env bash
touch "$CURL_MARKER"
exit 0
EOF
chmod +x "$STUB_DIR/curl"

write_config() {
  # $1 = true|false for gates.telemetry_report
  cat > "$CONFIG_SCRATCH/config.json" <<EOF
{"gates": {"telemetry_report": $1}}
EOF
}

echo "== AC-3: gate off -> no curl invocation =="
rm -f "$CURL_MARKER"
write_config false
AF_CONTROL_PLANE_CONFIG="$CONFIG_SCRATCH/config.json" \
  AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR" \
  PATH="$STUB_DIR:$PATH" \
  bash "$TELEMETRY_SH"
RC=$?
assert_exit_0 "telemetry.sh exits 0 with gate off" "$RC"
assert_file_absent "stub curl marker does not exist (gate off)" "$CURL_MARKER"

echo ""
echo "== AC-7: gate on, install id missing -> skip silently, no curl, no id minted =="
rm -f "$CURL_MARKER"
rm -rf "$SCRATCH_STATE_DIR"
mkdir -p "$SCRATCH_STATE_DIR"
write_config true
AF_CONTROL_PLANE_CONFIG="$CONFIG_SCRATCH/config.json" \
  AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR" \
  PATH="$STUB_DIR:$PATH" \
  bash "$TELEMETRY_SH"
RC=$?
assert_exit_0 "telemetry.sh exits 0 with gate on, id missing" "$RC"
assert_file_absent "stub curl marker does not exist (id missing)" "$CURL_MARKER"
assert_file_absent "no id file was minted by the send path" "$SCRATCH_STATE_DIR/telemetry-install-id"

echo ""
echo "== Sanity: gate on, install id present -> PR-a still makes no curl call =="
# PR-a has no payload/send logic yet; this documents current behaviour
# without claiming it satisfies an AC that belongs to PR-b.
rm -f "$CURL_MARKER"
printf 'deadbeefdeadbeefdeadbeefdeadbeef\n' > "$SCRATCH_STATE_DIR/telemetry-install-id"
chmod 0600 "$SCRATCH_STATE_DIR/telemetry-install-id"
AF_CONTROL_PLANE_CONFIG="$CONFIG_SCRATCH/config.json" \
  AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR" \
  PATH="$STUB_DIR:$PATH" \
  bash "$TELEMETRY_SH"
RC=$?
assert_exit_0 "telemetry.sh exits 0 with gate on, id present" "$RC"
assert_file_absent "stub curl marker does not exist (PR-a ships no send path yet)" "$CURL_MARKER"

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
