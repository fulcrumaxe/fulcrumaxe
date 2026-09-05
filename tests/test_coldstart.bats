#!/usr/bin/env bats
# tests/test_coldstart.bats — smoke tests for scripts/coldstart.sh (D#1526 AC#14).
#
# Run with: bats tests/test_coldstart.bats
# Falls back to tests/test_coldstart.sh (pure bash) if bats is unavailable.

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
}

@test "coldstart.sh --help exits 0 and lists required flags" {
  run bash "$REPO_ROOT/scripts/coldstart.sh" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"--path"* ]]
  [[ "$output" == *"--name"* ]]
  [[ "$output" == *"--language"* ]]
  [[ "$output" == *"--backlog"* ]]
  [[ "$output" == *"--dry-run"* ]]
  [[ "$output" == *"--phase"* ]]
  [[ "$output" == *"--resume"* ]]
}

@test "coldstart.sh --dry-run mutates nothing" {
  rm -rf "$HOME/.BatsDemo-state"
  run bash "$REPO_ROOT/scripts/coldstart.sh" --path /tmp/does-not-exist-bats-xyz --name BatsDemo --dry-run
  [ "$status" -eq 0 ]
  [ ! -d "$HOME/.BatsDemo-state" ]
  [ ! -e /tmp/does-not-exist-bats-xyz ]
  [[ "$output" == *"Nothing was written"* ]]
}

@test "coldstart-preflight.sh reports missing prerequisites with a friendly message, no traceback" {
  # Minimal PATH containing only bash — simulates gh/node/python3 absent.
  local stub_dir
  stub_dir="$(mktemp -d)"
  ln -sf "$(command -v bash)" "$stub_dir/bash"
  run env PATH="$stub_dir" bash "$REPO_ROOT/scripts/lib/coldstart-preflight.sh"
  [ "$status" -ne 0 ]
  [[ "$output" == *"missing prerequisite"* ]]
  [[ "$output" != *"Traceback"* ]]
  [[ "$output" != *"line "*", in "* ]]
  rm -rf "$stub_dir"
}

@test "coldstart.sh syntax is valid" {
  run bash -n "$REPO_ROOT/scripts/coldstart.sh"
  [ "$status" -eq 0 ]
}

@test "coldstart.sh unknown flag exits non-zero with usage" {
  run bash "$REPO_ROOT/scripts/coldstart.sh" --bogus-flag
  [ "$status" -ne 0 ]
  [[ "$output" == *"Usage:"* ]]
}

@test "coldstart.sh --help lists --mode and --self-test" {
  run bash "$REPO_ROOT/scripts/coldstart.sh" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"--mode"* ]]
  [[ "$output" == *"--self-test"* ]]
}

@test "coldstart.sh --self-test exercises the HALT flow with no live GitHub writes (default mode)" {
  local tmp_state
  tmp_state="$(mktemp -d)"
  run env AUTONOMOUS_TEAM_STATE_DIR="$tmp_state" bash "$REPO_ROOT/scripts/coldstart.sh" --self-test
  [ "$status" -eq 0 ]
  [[ "$output" == *"mode: existing"* ]]
  [[ "$output" == *"[self-test] PASS"* ]]
  rm -rf "$tmp_state"
}

@test "coldstart.sh --mode new --self-test reflects the new-vs-existing branch" {
  local tmp_state
  tmp_state="$(mktemp -d)"
  run env AUTONOMOUS_TEAM_STATE_DIR="$tmp_state" bash "$REPO_ROOT/scripts/coldstart.sh" --mode new --self-test
  [ "$status" -eq 0 ]
  [[ "$output" == *"mode: new"* ]]
  [[ "$output" == *"orient beat reflects mode (new)"* ]]
  [[ "$output" == *"[self-test] PASS"* ]]
  rm -rf "$tmp_state"
}

@test "coldstart.sh rejects an invalid --mode value" {
  run bash "$REPO_ROOT/scripts/coldstart.sh" --path /tmp/does-not-exist --name x --mode bogus --dry-run
  [ "$status" -ne 0 ]
  [[ "$output" == *"--mode must be"* ]]
}

@test "coldstart.sh --dry-run with no --mode still defaults to existing (back-compat)" {
  run bash "$REPO_ROOT/scripts/coldstart.sh" --path /tmp/does-not-exist-bats-xyz2 --name BatsDemo2 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"mode:               existing"* ]]
}
