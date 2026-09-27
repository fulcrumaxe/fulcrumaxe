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

# --- D#2558 AC11-AC13: install-time identity guard (--resume path) ---------

@test "coldstart.sh --resume tolerates a not-yet-written config.json" {
  local tmp_repo tmp_state
  tmp_repo="$(mktemp -d)"
  tmp_state="$(mktemp -d)"
  run env COLDSTART_STATE_ROOT="$tmp_state" bash "$REPO_ROOT/scripts/coldstart.sh" \
    --resume --path "$tmp_repo" --name BatsIdentityGuard1
  # The interview hasn't run yet in this fixture -- the identity guard must
  # not be why this fails (there's no epics/ dir either, which is fine: run_seed
  # reports "nothing to seed" and exits 0).
  [[ "$output" != *"D#2558 guard"* ]]
  rm -rf "$tmp_repo" "$tmp_state"
}

@test "coldstart.sh --resume hard-fails when boss_github_username is missing from config.json" {
  local tmp_repo tmp_state
  tmp_repo="$(mktemp -d)"
  tmp_state="$(mktemp -d)"
  mkdir -p "$tmp_repo/.autonomous-team"
  echo '{"repo": "acme/widget"}' > "$tmp_repo/.autonomous-team/config.json"
  run env COLDSTART_STATE_ROOT="$tmp_state" bash "$REPO_ROOT/scripts/coldstart.sh" \
    --resume --path "$tmp_repo" --name BatsIdentityGuard2
  [ "$status" -ne 0 ]
  [[ "$output" == *"boss_github_username"* ]]
  [[ "$output" == *".autonomous-team/config.json"* ]]
  rm -rf "$tmp_repo" "$tmp_state"
}

@test "coldstart.sh --resume hard-fails when boss_github_username fails the login grammar" {
  local tmp_repo tmp_state
  tmp_repo="$(mktemp -d)"
  tmp_state="$(mktemp -d)"
  mkdir -p "$tmp_repo/.autonomous-team"
  echo '{"repo": "acme/widget", "boss_github_username": "github-actions[bot]"}' > "$tmp_repo/.autonomous-team/config.json"
  run env COLDSTART_STATE_ROOT="$tmp_state" bash "$REPO_ROOT/scripts/coldstart.sh" \
    --resume --path "$tmp_repo" --name BatsIdentityGuard3
  [ "$status" -ne 0 ]
  [[ "$output" == *"not a valid GitHub login"* ]]
  rm -rf "$tmp_repo" "$tmp_state"
}

@test "coldstart.sh --resume proceeds past the identity guard with a valid boss_github_username" {
  local tmp_repo tmp_state
  tmp_repo="$(mktemp -d)"
  tmp_state="$(mktemp -d)"
  mkdir -p "$tmp_repo/.autonomous-team"
  echo '{"repo": "acme/widget", "boss_github_username": "octocat"}' > "$tmp_repo/.autonomous-team/config.json"
  run env COLDSTART_STATE_ROOT="$tmp_state" bash "$REPO_ROOT/scripts/coldstart.sh" \
    --resume --path "$tmp_repo" --name BatsIdentityGuard4
  [[ "$output" == *"boss_github_username is configured and valid"* ]]
  rm -rf "$tmp_repo" "$tmp_state"
}
