#!/usr/bin/env bash
# tests/test_telemetry_level0_bash.sh — level-0 network guard for
# scripts/lib/telemetry.sh (D#2565, AC-3 and AC-7's bash half).
#
# A pytest socket guard proves nothing about scripts/lib/telemetry.sh — the
# send is wired into a bash ritual, and a subprocess is invisible to a
# Python-level socket.socket patch. This stubs a small set of
# network/resolver-shaped binaries first on PATH instead — curl (the actual
# send tool), plus wget, nc, and getent (plausible stand-ins a future edit
# might reach for, or a resolver call ahead of the gate check). Each stub
# touches its own marker file and exits 0, so if telemetry.sh ever shells
# out to any of them while it shouldn't, the marker's existence catches it.
# Stubbing curl alone only proves curl wasn't called — it says nothing about
# the header's broader claim that NOTHING resolves a hostname or opens a
# socket before the gate check; a `getaddrinfo` call routed through `getent`
# would sail through a curl-only stub with 7/7 green.
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

cleanup() {
  rm -rf "$SCRATCH_STATE_DIR" "$CONFIG_SCRATCH" "$STUB_DIR"
}
trap cleanup EXIT

# Stub every network/resolver-shaped binary telemetry.sh could plausibly
# reach for: each touches its own marker and exits 0.
STUB_BINS="curl wget nc getent"
for bin in $STUB_BINS; do
  cat > "$STUB_DIR/$bin" <<EOF
#!/usr/bin/env bash
touch "$STUB_DIR/${bin}-was-called"
exit 0
EOF
  chmod +x "$STUB_DIR/$bin"
done

assert_no_stub_called() {
  local label_suffix="$1" bin
  for bin in $STUB_BINS; do
    assert_file_absent "stub $bin marker does not exist ($label_suffix)" "$STUB_DIR/${bin}-was-called"
  done
}

clear_stub_markers() {
  local bin
  for bin in $STUB_BINS; do
    rm -f "$STUB_DIR/${bin}-was-called"
  done
}

write_config() {
  # $1 = true|false for gates.telemetry_report
  cat > "$CONFIG_SCRATCH/config.json" <<EOF
{"gates": {"telemetry_report": $1}}
EOF
}

echo "== AC-3: gate off -> no network/resolver binary invoked =="
clear_stub_markers
write_config false
OUT=$(AF_CONTROL_PLANE_CONFIG="$CONFIG_SCRATCH/config.json" \
  AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR" \
  PATH="$STUB_DIR:$PATH" \
  bash "$TELEMETRY_SH" 2>&1)
RC=$?
assert_exit_0 "telemetry.sh exits 0 with gate off" "$RC"
assert_no_stub_called "gate off"

echo ""
echo "== AC-7: gate on, install id missing -> skip silently, no network, no id minted =="
clear_stub_markers
rm -rf "$SCRATCH_STATE_DIR"
mkdir -p "$SCRATCH_STATE_DIR"
write_config true
OUT=$(AF_CONTROL_PLANE_CONFIG="$CONFIG_SCRATCH/config.json" \
  AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR" \
  PATH="$STUB_DIR:$PATH" \
  bash "$TELEMETRY_SH" 2>&1)
RC=$?
assert_exit_0 "telemetry.sh exits 0 with gate on, id missing" "$RC"
assert_no_stub_called "id missing"
assert_file_absent "no id file was minted by the send path" "$SCRATCH_STATE_DIR/telemetry-install-id"

echo ""
echo "== Sanity: gate on, install id present -> PR-a still makes no network call =="
# PR-a has no payload/send logic yet, and scripts/lib/telemetry.sh does not
# call backend/telemetry_report.py's maybe_send() seam at all (confirmed by
# tracing telemetry.sh's exit paths during D#2565 review) — this documents
# current behaviour without claiming it satisfies an AC that belongs to
# PR-b. Wiring the seam in, and distinguishing "network unreachable"
# (silent, by design) from "the send seam itself malfunctioned" (should not
# be silent), is a PR-b item.
clear_stub_markers
printf 'deadbeefdeadbeefdeadbeefdeadbeef\n' > "$SCRATCH_STATE_DIR/telemetry-install-id"
chmod 0600 "$SCRATCH_STATE_DIR/telemetry-install-id"
OUT=$(AF_CONTROL_PLANE_CONFIG="$CONFIG_SCRATCH/config.json" \
  AUTONOMOUS_TEAM_STATE_DIR="$SCRATCH_STATE_DIR" \
  PATH="$STUB_DIR:$PATH" \
  bash "$TELEMETRY_SH" 2>&1)
RC=$?
assert_exit_0 "telemetry.sh exits 0 with gate on, id present" "$RC"
assert_no_stub_called "PR-a ships no send path yet"

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
