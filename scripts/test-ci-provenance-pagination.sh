#!/usr/bin/env bash
# scripts/test-ci-provenance-pagination.sh — regression test: the CI
# provenance gate must count >100-file PRs through the paginated files
# endpoint (parent umbrella D#366).
#
# The gate used to fetch `gh pr view --json files`, which never paginates
# past 100 entries and still exits 0 — so its completeness check failed
# closed on EVERY PR over 100 files and none of them could merge through
# scripts/merge-and-hook.sh. It now fetches
# `gh api repos/.../pulls/N/files --paginate` and counts the full set.
#
# These tests stub `gh` with fixture PRs (a 150-file PR split across two
# pages — the exact shape `gh api --paginate` emits, one JSON array per
# page, unmerged) and prove the gate counts all of them.
#
# Exit 0 on pass, exit 1 on any failure. No network, no real gh calls.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=true
FAILURES=()

pass() { echo "ok: $1"; }
fail() { echo "FAIL: $1"; PASS=false; FAILURES+=("$1"); }

# shellcheck source=scripts/lib/ci-status-check.sh
source "$REPO_ROOT/scripts/lib/ci-status-check.sh"

TMPD="$(mktemp -d /tmp/test-ci-prov-pagination-XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT
export GH_STUB_DIR="$TMPD/fixtures"
mkdir -p "$GH_STUB_DIR" "$TMPD/bin"

# ── Stub gh ─────────────────────────────────────────────────────────────────
# Serves fixture pages for `gh api repos/<repo>/pulls/<pr>/files --paginate`
# (one JSON array per page, like the real --paginate) and the changedFiles
# scalar for `gh pr view <pr> --json changedFiles`. Anything else is a loud
# failure — the gate must not call gh any other way on this path.
cat > "$TMPD/bin/gh" <<'STUBEOF'
#!/usr/bin/env bash
shopt -s nullglob
if [ "${1:-}" = "api" ]; then
  ep="${2:-}"
  pr="$(printf '%s' "$ep" | sed -n 's|.*/pulls/\([0-9]*\)/files|\1|p')"
  shift 2 || true
  paginate=false
  for a in "$@"; do [ "$a" = "--paginate" ] && paginate=true; done
  if [ -z "$pr" ] || ! $paginate; then echo "stub gh: unexpected api call: $ep $*" >&2; exit 1; fi
  if [ -f "$GH_STUB_DIR/fail-$pr" ]; then echo "stub gh: forced api failure" >&2; exit 1; fi
  pages=("$GH_STUB_DIR/pr-$pr"-page-*.json)
  if [ "${#pages[@]}" -eq 0 ]; then echo "stub gh: no fixture pages for PR $pr" >&2; exit 1; fi
  for page in "${pages[@]}"; do cat "$page"; echo; done
  exit 0
fi
if [ "${1:-}" = "pr" ] && [ "${2:-}" = "view" ]; then
  pr="${3:-}"
  if [ -f "$GH_STUB_DIR/pr-$pr-changedFiles" ]; then
    cat "$GH_STUB_DIR/pr-$pr-changedFiles"
    exit 0
  fi
  echo "stub gh: no changedFiles fixture for PR $pr" >&2
  exit 1
fi
echo "stub gh: unexpected call: $*" >&2
exit 1
STUBEOF
chmod +x "$TMPD/bin/gh"
export PATH="$TMPD/bin:$PATH"

# Test mode arms the kill-switch + provenance seams below; the audit redirect
# keeps every seam note off the real ledger.
export CI_STATUS_TEST_MODE=1
export CI_STATUS_TEST_AUDIT_FILE="$TMPD/audit.jsonl"
export CI_KILL_SWITCH_OVERRIDE=HTTP_404
# The CI_PR_FILES seam would bypass the gh fetch this suite exists to test —
# make sure no leaked env var engages it for any fixture PR.
unset CI_PR_FILES_150 CI_PR_FILES_151 CI_PR_FILES_152 CI_PR_FILES_153 CI_PR_FILES_154 CI_PR_FILES_155 2>/dev/null || true

# make_pages <pr> <total> [workflow_1based_position]
# Writes pr-<pr>-page-{1,2}.json (100 per page, REST shape: {"filename": ...})
# and pr-<pr>-changedFiles. python3 builds the JSON so no path quoting can drift.
make_pages() {
  python3 - "$GH_STUB_DIR" "$1" "$2" "${3:-0}" <<'PYEOF'
import json, sys
d, pr, total, wp = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
paths = [f"src/file-{i:03d}.ts" for i in range(1, total + 1)]
if wp:
    assert 1 <= wp <= total
    paths[wp - 1] = ".github/workflows/ci.yml"
open(f"{d}/pr-{pr}-changedFiles", "w").write(str(total))
for n, start in enumerate(range(0, total, 100), 1):
    page = [{"filename": p} for p in paths[start:start + 100]]
    open(f"{d}/pr-{pr}-page-{n}.json", "w").write(json.dumps(page))
PYEOF
}

DISC=999

# ── 1. 150-file PR, no workflow files → pass (the regression) ────────────────
# The old fetch saw 100 entries against changedFiles=150 and failed closed.
make_pages 150 150
export CI_PROVENANCE_BLOCKED_999=no
check_ci_provenance_gate 150 "owner/repo" "$DISC" 2>/dev/null
[ $? -eq 0 ] && pass "150-file PR with no workflow files passes" \
  || fail "150-file PR blocked: $CI_STATUS_FAIL_REASON"

# ── 2. Workflow file at position 120 is seen → provenance block fires ────────
# If the gate only counted the first 100, this file would be invisible and the
# gate would wrongly pass. The D#1588 reason proves the block came from the
# file being SEEN, past the old cutoff.
make_pages 151 150 120
export CI_PROVENANCE_BLOCKED_999=yes
check_ci_provenance_gate 151 "owner/repo" "$DISC" 2>/dev/null
rc=$?
if [ "$rc" -eq 1 ] && [[ "$CI_STATUS_FAIL_REASON" == *"D#1588"* ]]; then
  pass "workflow file at position 120 triggers the provenance block"
else
  fail "position-120 workflow file not honored (rc=$rc reason=$CI_STATUS_FAIL_REASON)"
fi

# ── 3. Same PR, provenance cleared → pass (size alone never blocks) ──────────
export CI_PROVENANCE_BLOCKED_999=no
check_ci_provenance_gate 151 "owner/repo" "$DISC" 2>/dev/null
[ $? -eq 0 ] && pass "150-file workflow PR passes once provenance clears" \
  || fail "150-file workflow PR still blocked: $CI_STATUS_FAIL_REASON"

# ── 4. Shortfall still fails closed (D#2630 preserved) ───────────────────────
make_pages 152 100
echo 150 > "$GH_STUB_DIR/pr-152-changedFiles"
export CI_PROVENANCE_BLOCKED_999=no
check_ci_provenance_gate 152 "owner/repo" "$DISC" 2>/dev/null
rc=$?
if [ "$rc" -eq 1 ] && [[ "$CI_STATUS_FAIL_REASON" == *"truncated or inflated"* ]]; then
  pass "100-of-150 shortfall still fails closed"
else
  fail "shortfall not caught (rc=$rc reason=$CI_STATUS_FAIL_REASON)"
fi

# ── 5. Overshoot names the true count: "returned 150" ────────────────────────
# Direct proof the gate counted the FULL set, not the first page: with
# changedFiles=149 and 150 files fetched, the reason must say 150.
make_pages 155 150
echo 149 > "$GH_STUB_DIR/pr-155-changedFiles"
check_ci_provenance_gate 155 "owner/repo" "$DISC" 2>/dev/null
rc=$?
if [ "$rc" -eq 1 ] && [[ "$CI_STATUS_FAIL_REASON" == *"returned 150"* ]]; then
  pass "mismatch reason reports the full 150-file count"
else
  fail "full count not reported (rc=$rc reason=$CI_STATUS_FAIL_REASON)"
fi

# ── 6. gh api failure fails closed ───────────────────────────────────────────
touch "$GH_STUB_DIR/fail-153"
check_ci_provenance_gate 153 "owner/repo" "$DISC" 2>/dev/null
rc=$?
if [ "$rc" -eq 1 ] && [[ "$CI_STATUS_FAIL_REASON" == *"could not read the file list"* ]]; then
  pass "files-endpoint failure fails closed"
else
  fail "api failure not caught (rc=$rc reason=$CI_STATUS_FAIL_REASON)"
fi

# ── 7. Small-PR sanity: single page still works ──────────────────────────────
make_pages 154 3
check_ci_provenance_gate 154 "owner/repo" "$DISC" 2>/dev/null
[ $? -eq 0 ] && pass "3-file PR passes" \
  || fail "3-file PR blocked: $CI_STATUS_FAIL_REASON"

# ── Verdict ──────────────────────────────────────────────────────────────────
if $PASS; then
  echo "PASS: all ci-provenance-pagination checks passed"
  exit 0
else
  echo "FAIL: ${#FAILURES[@]} check(s) failed: ${FAILURES[*]}"
  exit 1
fi
