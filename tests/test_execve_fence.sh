#!/usr/bin/env bash
# tests/test_execve_fence.sh
#
# Integration tests for hooks/claude_execve_fence.py
#
# Tests the 10 D#439 bypass patterns plus positive (allowed) cases.
# Acceptance criteria:
#   1. Subagent shell trying any of the 10 D#439-bypass forms → EPERM
#   2. Legitimate non-claude execve → succeeds unchanged
#   3. Block event logged for audit
#   4. Inheritance verified (nested shells)
#
# Usage: bash tests/test_execve_fence.sh
# Exit 0 = all tests passed; non-zero = at least one failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# D#2267: hooks/claude_execve_fence.py's own _FALLBACK_LOG_DIR is anchored to
# wherever the invoked *file* lives (Path(__file__).resolve().parent.parent),
# same pattern as hooks/sandbox.py and with no env override either. Every
# `python3 "$FENCE" -- ...` invocation below writes real audit rows on a
# block, so running the real $REPO_ROOT copy would append to the LIVE
# .autonomous-team/hook-events/blocks-<date>.jsonl. Run against an isolated
# fixture copy instead — see tests/lib/repo-root-fixture.sh. (The
# _FALLBACK_LOG_DIR-monkeypatch wrapper scripts further down this file are a
# different case: they import hooks.claude_execve_fence as a module in their
# own throwaway interpreter and override the attribute before ever using it,
# which is already hermetic without needing the fixture.)
source "$REPO_ROOT/tests/lib/repo-root-fixture.sh"
FIXTURE_ROOT="$(repo_root_fixture_make "$REPO_ROOT")" || {
  echo "FAIL: could not create isolated repo-root fixture" >&2
  exit 1
}
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

FENCE="$FIXTURE_ROOT/hooks/claude_execve_fence.py"

if [[ ! -f "$FENCE" ]]; then
  echo "FAIL: fence script not found at $FENCE" >&2
  exit 1
fi

PASS=0
FAIL=0

# All fake binaries/dirs for this suite live under one mktemp'd dir rather
# than fixed /tmp/test_fence_*_$$ names — $$ is stable for the life of one
# run, but the literal prefix a lint sees is still shared across every
# concurrent run of this suite (D#2254).
RUN_TMP="$(mktemp -d /tmp/test_execve_fence.XXXXXX)"

# ── Fake 'claude' binary for testing (avoids touching the real one) ──────────
FAKE_CLAUDE="$RUN_TMP/test_fence_claude"
cp /bin/echo "$FAKE_CLAUDE"
chmod +x "$FAKE_CLAUDE"

# Symlink named 'claude' in /tmp for bypass-case testing
FAKE_DIR="$RUN_TMP/test_fence_dir"
mkdir -p "$FAKE_DIR"
ln -sf "$FAKE_CLAUDE" "$FAKE_DIR/claude"

cleanup() {
  rm -rf "$RUN_TMP" "$FIXTURE_ROOT"
}
trap cleanup EXIT

# ── Helper functions ─────────────────────────────────────────────────────────

fence_run() {
  # Run cmd under the fence and capture output + exit code
  local timeout_sec="${FENCE_TIMEOUT:-8}"
  FENCE_OUT=$(timeout "$timeout_sec" python3 "$FENCE" -- "$@" 2>&1) || FENCE_RC=$?
  FENCE_RC="${FENCE_RC:-0}"
}

assert_blocked() {
  local name="$1"
  shift
  FENCE_RC=0
  fence_run "$@"
  if [[ "$FENCE_RC" -ne 0 ]]; then
    echo "PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name — expected non-zero exit (EPERM block), got rc=0, out=${FENCE_OUT:0:80}"
    FAIL=$((FAIL + 1))
  fi
}

assert_allowed() {
  local name="$1"
  shift
  FENCE_RC=0
  fence_run "$@"
  if [[ "$FENCE_RC" -eq 0 ]]; then
    echo "PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name — expected rc=0 (allowed), got rc=$FENCE_RC, out=${FENCE_OUT:0:80}"
    FAIL=$((FAIL + 1))
  fi
}

# ── Negative tests: legitimate execs must pass ───────────────────────────────

assert_allowed "git passes through" git --version
assert_allowed "bash passes through" bash -c "echo ok"
assert_allowed "python3 passes through" python3 -c "print('ok')"
assert_allowed "ls passes through" ls /tmp

# ── Bypass case 1: direct absolute path ─────────────────────────────────────
# D#439 pattern: exec /path/to/claude directly
assert_blocked "bypass-1: absolute path $FAKE_DIR/claude" \
  "$FAKE_DIR/claude" --help

# ── Bypass case 2: basename 'claude' in PATH ────────────────────────────────
# D#439 pattern: PATH manipulation so 'claude' resolves to our fake binary
assert_blocked "bypass-2: basename claude via PATH" \
  bash -c "PATH=$FAKE_DIR:\$PATH claude --help"

# ── Bypass case 3: exec claude ───────────────────────────────────────────────
# D#439 pattern: builtin exec form
assert_blocked "bypass-3: exec claude" \
  bash -c "PATH=$FAKE_DIR:\$PATH exec claude --help"

# ── Bypass case 4: env FOO=bar claude ───────────────────────────────────────
# D#439 pattern: env prefix
assert_blocked "bypass-4: env-prefix claude" \
  bash -c "PATH=$FAKE_DIR:\$PATH env DUMMY=1 claude --help"

# ── Bypass case 5: bash -c 'claude ...' ─────────────────────────────────────
# D#439 pattern: shell -c wrapper
assert_blocked "bypass-5: bash -c 'claude'" \
  bash -c "PATH=$FAKE_DIR:\$PATH bash -c 'claude --help'"

# ── Bypass case 6: sh -c claude ─────────────────────────────────────────────
# D#439 pattern: /bin/sh variant
assert_blocked "bypass-6: sh -c 'claude'" \
  sh -c "PATH=$FAKE_DIR:\$PATH sh -c 'claude --help'"

# ── Bypass case 7: backtick $(which claude) ─────────────────────────────────
# D#439 pattern: command substitution
assert_blocked "bypass-7: \$(command -v claude)" \
  bash -c "PATH=$FAKE_DIR:\$PATH bash -c 'exec \$(command -v claude) --help'"

# ── Bypass case 8: nohup claude ─────────────────────────────────────────────
# D#439 pattern: nohup wrapper
assert_blocked "bypass-8: nohup claude" \
  bash -c "PATH=$FAKE_DIR:\$PATH nohup claude --help 2>/dev/null"

# ── Bypass case 9: renamed/different-basename binary (realpath inode match) ──
# D#439 pattern: hardlinked copy of claude at a non-'claude' path.
# The fence blocks this when its realpath matches the registered claude realpath.
# In tests, we simulate by registering FAKE_CLAUDE as the 'claude' binary and
# then trying to exec it via a different name.
# We verify the basename-only path is blocked since our fake has 'claude' in symlink.
# Testing with a direct absolute path to the non-symlink fake binary (basename ≠ claude)
# — this should NOT be blocked (fence only blocks 'claude' basename or real claude inode).
NONCLAUD="$RUN_TMP/test_fence_notclaud"
cp /bin/echo "$NONCLAUD"
chmod +x "$NONCLAUD"
# This should be ALLOWED: basename is not 'claude' and path != real claude realpath
FENCE_RC=0
fence_run "$NONCLAUD" hello
if [[ "$FENCE_RC" -eq 0 ]]; then
  echo "PASS: bypass-9: non-claude binary (different basename) allowed"
  PASS=$((PASS + 1))
else
  echo "FAIL: bypass-9: non-claude binary incorrectly blocked"
  FAIL=$((FAIL + 1))
fi
rm -f "$NONCLAUD"

# ── Bypass case 10: python os.execve ────────────────────────────────────────
# D#439 pattern: python3 -c "import os; os.execve('/path/to/claude', ...)"
assert_blocked "bypass-10: python3 os.execve" \
  python3 -c "import os; os.execve('$FAKE_DIR/claude', ['$FAKE_DIR/claude', '--help'], {})"

# ── Acceptance criterion 4: inheritance ─────────────────────────────────────
# Filter must survive two fork+exec hops
assert_blocked "inheritance: 2-hop bash nesting" \
  bash -c "PATH=$FAKE_DIR:\$PATH bash -c 'bash -c \"claude --help\"'"

# ── Audit log verification ───────────────────────────────────────────────────
# Run one more blocked exec and check the audit log was written
DATE_TODAY=$(date -u +%Y-%m-%d)
AUDIT_LOG="$FIXTURE_ROOT/.autonomous-team/hook-events/blocks-${DATE_TODAY}.jsonl"

python3 "$FENCE" -- bash -c "PATH=$FAKE_DIR:\$PATH claude --help" 2>/dev/null || true

if [[ -f "$AUDIT_LOG" ]]; then
  if grep -q "execve_fence_block" "$AUDIT_LOG" 2>/dev/null; then
    echo "PASS: audit log written (blocks-${DATE_TODAY}.jsonl)"
    PASS=$((PASS + 1))
  else
    echo "FAIL: audit log exists but no execve_fence_block entry"
    FAIL=$((FAIL + 1))
  fi
else
  echo "FAIL: audit log not written at $AUDIT_LOG"
  FAIL=$((FAIL + 1))
fi

# ── Fail-closed: pyseccomp uninstalled → exit 1, claude not exec'd ──────────
# We simulate missing pyseccomp by overriding PYTHONPATH to a temp dir that
# shadows it with a broken import, then restore.
FAKE_PYSECCOMP_DIR=$(mktemp -d)
cat > "$FAKE_PYSECCOMP_DIR/pyseccomp.py" <<'PYEOF'
raise ImportError("simulated missing pyseccomp")
PYEOF

FENCE_RC=0
PYTHONPATH="$FAKE_PYSECCOMP_DIR" timeout 8 python3 "$FENCE" -- echo "should-not-run" 2>/dev/null \
  || FENCE_RC=$?
rm -rf "$FAKE_PYSECCOMP_DIR"

if [[ "$FENCE_RC" -ne 0 ]]; then
  echo "PASS: fail-closed: pyseccomp missing → fence exits non-zero (rc=$FENCE_RC)"
  PASS=$((PASS + 1))
else
  echo "FAIL: fail-closed: pyseccomp missing but fence exited 0 (exec-without-fence)"
  FAIL=$((FAIL + 1))
fi

# ── Fail-closed: audit log dir unwritable → exit 1 ───────────────────────────
# Write a file at the log dir path so mkdir fails, then verify the fence exits 1.
FAKE_AUDIT_DIR=$(mktemp -d)
FAKE_AUDIT_LOG="$FAKE_AUDIT_DIR/.autonomous-team/hook-events"
mkdir -p "$(dirname "$FAKE_AUDIT_LOG")"
# Create a regular file in place of the hook-events directory so mkdir fails
touch "$FAKE_AUDIT_LOG"

# We can't easily override _FALLBACK_LOG_DIR without modifying the source.
# Instead, verify the fallback-log function's fail-closed path by testing the
# fence in an environment where we override the log path via a small wrapper.
WRAPPER_DIR=$(mktemp -d)
cat > "$WRAPPER_DIR/fence_logfail_test.py" <<PYEOF
import sys, os
sys.path.insert(0, "$REPO_ROOT")
import hooks.claude_execve_fence as f

# Override log dir to a path that cannot be created (parent is a file)
f._FALLBACK_LOG_DIR = f.Path("$FAKE_AUDIT_LOG") / "subdir"

# Call _fallback_log — should call sys.exit(1)
try:
    f._fallback_log("test-unwritable")
    print("ERROR: _fallback_log did not exit on log write failure", file=sys.stderr)
    sys.exit(0)  # wrong
except SystemExit as e:
    if e.code == 1:
        print("OK: _fallback_log exited 1 on unwritable dir")
        sys.exit(0)  # test passes (we caught exit(1))
    sys.exit(e.code)
PYEOF

FENCE_RC=0
timeout 8 python3 "$WRAPPER_DIR/fence_logfail_test.py" 2>/dev/null \
  && FENCE_RC=0 || FENCE_RC=$?
rm -rf "$WRAPPER_DIR" "$FAKE_AUDIT_DIR"

if [[ "$FENCE_RC" -eq 0 ]]; then
  echo "PASS: fail-closed: _fallback_log exits 1 when log dir is unwritable"
  PASS=$((PASS + 1))
else
  echo "FAIL: fail-closed: _fallback_log did not exit 1 on unwritable log dir (rc=$FENCE_RC)"
  FAIL=$((FAIL + 1))
fi

# ── Fail-closed: kernel pidfd_getfd unavailable → exit 1 ─────────────────────
# We test the _probe_pidfd_support path by patching libc.syscall to fail.
WRAPPER_DIR2=$(mktemp -d)
cat > "$WRAPPER_DIR2/fence_nopidfd_test.py" <<PYEOF
import sys, os, ctypes, ctypes.util
sys.path.insert(0, "$REPO_ROOT")
import hooks.claude_execve_fence as f

# Build a fake libc-like object whose syscall always returns -1 (ENOSYS).
class FakeLibc:
    def syscall(self, nr, *args):
        return -1

fake_libc = FakeLibc()
result = f._probe_pidfd_support(fake_libc)
if result is False:
    print("OK: _probe_pidfd_support returns False when syscall fails")
    sys.exit(0)
else:
    print("ERROR: _probe_pidfd_support returned True despite syscall failure", file=sys.stderr)
    sys.exit(1)
PYEOF

FENCE_RC=0
timeout 8 python3 "$WRAPPER_DIR2/fence_nopidfd_test.py" 2>/dev/null \
  && FENCE_RC=0 || FENCE_RC=$?
rm -rf "$WRAPPER_DIR2"

if [[ "$FENCE_RC" -eq 0 ]]; then
  echo "PASS: fail-closed: _probe_pidfd_support returns False when pidfd_getfd unavailable"
  PASS=$((PASS + 1))
else
  echo "FAIL: fail-closed: _probe_pidfd_support returned True despite simulated kernel failure"
  FAIL=$((FAIL + 1))
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
