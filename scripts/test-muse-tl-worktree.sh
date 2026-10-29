#!/usr/bin/env bash
# scripts/test-muse-tl-worktree.sh — verify the Team-Lead worktree provisioner.
#
# Covers the Helicon/serve fallback: native subagent isolation is unavailable
# on `muse serve` hosts, so Team Lead provisions + registers the worktree
# itself and confines the executor to it via the brief.
#
# Tests (all inside a throwaway local clone — the real repo is never touched):
#   1. Helper exists and parses (bash -n clean)
#   2. Missing required args fail loudly (exit != 0)
#   3. provision succeeds: path exists, worktree listed, HEAD == base,
#      registry entry present
#   4. Double-provision (same path / same branch) is refused
#   5. release succeeds: path gone, worktree list clean, registry marked done
#   6. Releasing an unknown id fails loudly
#
# Exit 0 on pass, exit 1 on any failure. No network, no gh, no muse calls.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=true
FAILURES=()

pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1"; PASS=false; FAILURES+=("$1"); }

HELPER="$REPO_ROOT/scripts/lib/muse-tl-worktree.sh"

# Isolated state FIRST — registry + feed writes land here or in the throwaway
# clone, never in the real state dir or tracked feed.
TMPSTATE="$(mktemp -d /tmp/test-muse-tl-wt-XXXXXX)"
export AUTONOMOUS_TEAM_STATE_DIR="$TMPSTATE"
export STATS_DB_PATH="$TMPSTATE/stats.duckdb"
CLONE="$TMPSTATE/clone"
export _WTR_REPO_ROOT="$CLONE"

_cleanup() { rm -rf "$TMPSTATE"; }
trap _cleanup EXIT

# ── 1. Helper exists and parses ─────────────────────────────────────────
if [[ -f "$HELPER" ]] && bash -n "$HELPER"; then
  pass "helper exists and parses"
else
  fail "helper exists and parses"
fi

# ── 2. Missing args fail loudly ──────────────────────────────────────────
if bash "$HELPER" provision --id only-an-id 2>/dev/null; then
  fail "provision with missing args fails"
else
  pass "provision with missing args fails"
fi

# Throwaway clone for lifecycle tests (local-path clone: no network).
if ! git clone -q "$REPO_ROOT" "$CLONE" 2>/dev/null; then
  fail "throwaway clone setup"
fi

# ── 3. provision succeeds ────────────────────────────────────────────────
BASE_SHA="$(git -C "$CLONE" rev-parse HEAD)"
GOT_PATH="$(cd "$CLONE" && bash "$HELPER" provision --id test-exec-1 \
  --role executor --discussion 1 --branch d1-test-branch --base HEAD 2>/dev/null)"
if [[ -z "$GOT_PATH" || ! -d "$GOT_PATH" ]]; then
  fail "provision creates the worktree path"
else
  pass "provision creates the worktree path"
fi
if git -C "$CLONE" worktree list 2>/dev/null | grep -q "d1-test-branch"; then
  pass "provision registers with git"
else
  fail "provision registers with git"
fi
if [[ "$(git -C "$GOT_PATH" rev-parse HEAD 2>/dev/null)" == "$BASE_SHA" ]]; then
  pass "provision checks out the base ref"
else
  fail "provision checks out the base ref"
fi
if bash "$REPO_ROOT/scripts/lib/worktree-registry.sh" list 2>/dev/null | grep -q "test-exec-1"; then
  pass "provision registers the worktree"
else
  fail "provision registers the worktree"
fi

# ── 4. Double-provision refused ──────────────────────────────────────────
if (cd "$CLONE" && bash "$HELPER" provision --id test-exec-1 \
  --role executor --discussion 1 --branch d1-test-branch --base HEAD 2>/dev/null); then
  fail "re-provision of same id is refused"
else
  pass "re-provision of same id is refused"
fi
if (cd "$CLONE" && bash "$HELPER" provision --id test-exec-2 \
  --role executor --discussion 1 --branch d1-test-branch --base HEAD 2>/dev/null); then
  fail "re-provision of same branch is refused"
else
  pass "re-provision of same branch is refused"
fi

# ── 5. release succeeds ──────────────────────────────────────────────────
if (cd "$CLONE" && bash "$HELPER" release --id test-exec-1 2>/dev/null); then
  pass "release exits 0"
else
  fail "release exits 0"
fi
if [[ ! -e "$GOT_PATH" ]] && ! git -C "$CLONE" worktree list 2>/dev/null | grep -q "d1-test-branch"; then
  pass "release removes the worktree"
else
  fail "release removes the worktree"
fi
if bash "$REPO_ROOT/scripts/lib/worktree-registry.sh" list 2>/dev/null | grep "test-exec-1" | grep -q "discarded"; then
  pass "release marks the registry entry discarded"
else
  fail "release marks the registry entry discarded"
fi

# ── 6. Unknown id release fails ──────────────────────────────────────────
if (cd "$CLONE" && bash "$HELPER" release --id no-such-id 2>/dev/null); then
  fail "release of unknown id fails"
else
  pass "release of unknown id fails"
fi

# ── verdict ──────────────────────────────────────────────────────────────
if [[ "$PASS" == "true" ]]; then
  echo "PASS: muse-tl-worktree"
  exit 0
else
  echo "FAILURES (${#FAILURES[@]}):"
  printf '  - %s\n' "${FAILURES[@]}"
  exit 1
fi
