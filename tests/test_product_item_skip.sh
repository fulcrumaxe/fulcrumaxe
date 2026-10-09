#!/usr/bin/env bash
# tests/test_product_item_skip.sh — the loop's issue scan skips product items
# (D#6 S2-OWN). Fixture test for scripts/lib/product-item-skip.sh.
#
# Run: bash tests/test_product_item_skip.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKIP="$REPO_ROOT/scripts/lib/product-item-skip.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/fxs2own-skip-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"

# Stub gh, proven below. It records every call.
cat > "$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--fxs2own-stub-identity" ]]; then echo "fxs2own-gh-stub"; exit 0; fi
echo "$*" >> "${STUB_CALL_LOG:?}"
exit "${STUB_GH_RC:-0}"
EOF
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"
export STUB_CALL_LOG="$WORK/gh-calls.log"
[[ "$(command -v gh)" == "$WORK/bin/gh" && "$(gh --fxs2own-stub-identity)" == "fxs2own-gh-stub" ]] \
  || { echo "ABORT: gh is not the stub" >&2; exit 2; }
pass "gh on PATH is the stub"

# Issues as `gh issue list --json number,title,labels` prints them.
FIXTURE='[
 {"number":11,"title":"plain bug","labels":[{"name":"bug"}]},
 {"number":12,"title":"owned by the product","labels":[{"name":"enhancement"},{"name":"fulcrumaxe:product"}]},
 {"number":13,"title":"no labels at all","labels":[]},
 {"number":14,"title":"label spelled in another case","labels":[{"name":"Fulcrumaxe:Product"}]},
 {"number":15,"title":"similar but different label","labels":[{"name":"fulcrumaxe:product-adjacent"}]}
]'

numbers() { python3 -c 'import json,sys; print(",".join(str(i["number"]) for i in json.load(sys.stdin)))'; }

# 1. labelled issues are not routed; one log line each; others pass through.
: > "$STUB_CALL_LOG"
RC=0
OUT="$(printf '%s' "$FIXTURE" | bash "$SKIP" 2>"$WORK/err.txt")" || RC=$?
[[ "$RC" -eq 0 ]] && pass "exits 0" || fail "exit $RC"
[[ "$(printf '%s' "$OUT" | numbers)" == "11,13,15" ]] && pass "only the unlabelled issues are routed (11,13,15)" || fail "routed: $(printf '%s' "$OUT" | numbers)"
[[ "$(grep -c '^skipped: product item #' "$WORK/err.txt")" -eq 2 ]] && pass "two skip lines, one per skipped issue" || fail "skip lines: $(cat "$WORK/err.txt")"
grep -qx 'skipped: product item #12' "$WORK/err.txt" && pass "line for #12" || fail "no line for #12"
grep -qx 'skipped: product item #14' "$WORK/err.txt" && pass "line for #14 (label match is case-insensitive)" || fail "no line for #14"
grep -q 'skipped: product item #1[135]' "$WORK/err.txt" && fail "a routed issue was logged as skipped" || pass "no skip line for a routed issue"
[[ ! -s "$STUB_CALL_LOG" ]] && pass "without --log-issue no gh call is made" || fail "unexpected gh call"
printf '%s' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[0]["title"]=="plain bug"' \
  && pass "other fields are kept" || fail "fields lost"

# 2. without the label an issue is routed as today, with no log line.
: > "$STUB_CALL_LOG"
OUT="$(printf '%s' '[{"number":21,"title":"x","labels":[{"name":"bug"}]}]' | bash "$SKIP" 2>"$WORK/err.txt")"
[[ "$(printf '%s' "$OUT" | numbers)" == "21" ]] && pass "unlabelled issue routed" || fail "unlabelled issue dropped"
[[ ! -s "$WORK/err.txt" ]] && pass "no log line for a routed issue" || fail "log line for routed issue"

# 3. plain-string label lists also work.
OUT="$(printf '%s' '[{"number":31,"labels":["fulcrumaxe:product"]},{"number":32,"labels":["bug"]}]' | bash "$SKIP" 2>/dev/null)"
[[ "$(printf '%s' "$OUT" | numbers)" == "32" ]] && pass "string label lists handled" || fail "string labels: $(printf '%s' "$OUT" | numbers)"

# 4. --log-issue posts one team-log comment per skipped issue, to the named repo.
: > "$STUB_CALL_LOG"
printf '%s' "$FIXTURE" | bash "$SKIP" --log-issue 99 --repo fxs2own-owner/disc >/dev/null 2>&1
[[ "$(grep -c '^issue comment 99 --repo fxs2own-owner/disc ' "$STUB_CALL_LOG")" -eq 2 ]] && pass "one team-log comment per skipped issue, repo pinned" || fail "gh calls: $(cat "$STUB_CALL_LOG")"
grep -q 'loop: skipped: product item #12' "$STUB_CALL_LOG" && pass "comment text carries the skip line" || fail "comment text wrong"

# 5. a failing log post does not change what is routed.
RC=0
OUT="$(printf '%s' "$FIXTURE" | STUB_GH_RC=1 bash "$SKIP" --log-issue 99 --repo fxs2own-owner/disc 2>/dev/null)" || RC=$?
[[ "$RC" -eq 0 && "$(printf '%s' "$OUT" | numbers)" == "11,13,15" ]] && pass "log failure does not affect routing" || fail "log failure changed result (rc=$RC)"

# 6. unreadable input is an error, never "nothing to skip".
RC=0
printf 'not json' | bash "$SKIP" >/dev/null 2>&1 || RC=$?
[[ "$RC" -eq 2 ]] && pass "non-JSON input exits 2" || fail "non-JSON exit $RC"
RC=0
printf '{"a":1}' | bash "$SKIP" >/dev/null 2>&1 || RC=$?
[[ "$RC" -eq 2 ]] && pass "non-array JSON exits 2" || fail "non-array exit $RC"

# 7. --log-issue without --repo is refused (a bare gh resolves from the checkout).
RC=0
printf '[]' | bash "$SKIP" --log-issue 99 >/dev/null 2>&1 || RC=$?
[[ "$RC" -eq 2 ]] && pass "--log-issue without --repo refused" || fail "no-repo exit $RC"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then exit 1; fi
echo "PRESUM: pass"
