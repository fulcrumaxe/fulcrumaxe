#!/usr/bin/env bash
# tests/test_identifier_rewrite_project_name.sh — acceptance test for D#2187
# (the joint D#2187+D#2188 Spec): the export-side rename of this project's
# pre-rename name (`autonomous-forever`, in all five spellings it ships in)
# to `fulcrumaxe`.
#
# This is deliberately NOT a synthetic-harness test like
# test_identifier_gate_allowlist_anchor.sh. The Spec's whole point is that a
# green `verify-export.sh` on its own proves nothing — a rewrite rule that
# ate every occurrence of "forever" would also be green. So this test runs
# the REAL export.sh against the REAL source tree and checks the numbers the
# Spec froze, including two negative controls that must FAIL the gate. If
# both of those don't fail, this change hasn't shown what it claims to.
#
# Run: bash tests/test_identifier_rewrite_project_name.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPORT_SH="$REPO_ROOT/open-source/export.sh"
VERIFY_SH="$REPO_ROOT/open-source/verify-export.sh"
GATE_SH="$REPO_ROOT/open-source/checks/identifier-gate.sh"

PASS=0
FAIL=0

ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL + 1)); }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  [[ "$expected" == "$actual" ]] && ok "$label" || bad "$label" "expected '$expected', got '$actual'"
}

assert_true() {
  local label="$1" rc="$2"
  [[ "$rc" -eq 0 ]] && ok "$label" || bad "$label" "expected rc 0, got $rc"
}

assert_false() {
  local label="$1" rc="$2"
  [[ "$rc" -ne 0 ]] && ok "$label" || bad "$label" "expected non-zero rc, got 0"
}

assert_not_contains() {
  local label="$1" needle="$2" haystack="$3"
  [[ "$haystack" != *"$needle"* ]] && ok "$label" || bad "$label" "unexpectedly found '$needle'"
}

TARGET_DIR="$(mktemp -d)"
NEG_DIR_8="$(mktemp -d)"
NEG_DIR_9="$(mktemp -d)"
cleanup() { rm -rf "$TARGET_DIR" "$NEG_DIR_8" "$NEG_DIR_9"; }
trap cleanup EXIT

echo "== Building a fresh export =="
EXPORT_OUT="$(bash "$EXPORT_SH" "$TARGET_DIR" 2>&1)"
EXPORT_RC=$?
assert_true "export.sh exits 0" "$EXPORT_RC"

echo ""
echo "== Item 3: zero occurrences of the old project name, any spelling =="
LEFTOVER="$(grep -rIoiE 'autonomous[-_ ]forever|autoforever' "$TARGET_DIR" 2>/dev/null | wc -l | tr -d '[:space:]')"
assert_eq "case-insensitive scan for all five spellings returns 0" "0" "$LEFTOVER"

echo ""
echo "== Item 4: the state-dir references were rewritten, not skipped =="
STATE_COUNT="$(grep -rIo 'fulcrumaxe-state' "$TARGET_DIR" 2>/dev/null | wc -l | tr -d '[:space:]')"
assert_eq "fulcrumaxe-state count" "100" "$STATE_COUNT"

echo ""
echo "== Item 5: over-broad-rule canary — serve_forever untouched =="
SERVE_FOREVER_COUNT="$(grep -rIo 'serve_forever' "$TARGET_DIR" 2>/dev/null | wc -l | tr -d '[:space:]')"
assert_eq "serve_forever count" "7" "$SERVE_FOREVER_COUNT"

echo ""
echo "== Item 6: over-broad-rule canary — ordinary 'forever' prose untouched =="
FOREVER_PROSE_COUNT="$(grep -rIn '\bforever\b' "$TARGET_DIR" 2>/dev/null | grep -v serve_forever | wc -l | tr -d '[:space:]')"
assert_eq "forever prose count (excluding serve_forever)" "15" "$FOREVER_PROSE_COUNT"

echo ""
echo "== Item 7: verify-export.sh is clean on its own fresh export =="
VERIFY_OUT="$(bash "$VERIFY_SH" 2>&1)"
VERIFY_RC=$?
assert_true "verify-export.sh exits 0" "$VERIFY_RC"
assert_not_contains "verify-export.sh stdout has no FAIL" "FAIL" "$VERIFY_OUT"

echo ""
echo "== Item 8: negative control — a leaked owner identifier still fails the gate =="
cp -r "$TARGET_DIR"/. "$NEG_DIR_8"/
echo "autonomous-agent-7" >> "$NEG_DIR_8/README.md"
GATE8_OUT="$(bash "$GATE_SH" "$NEG_DIR_8" 2>&1)"
GATE8_RC=$?
assert_false "gate rejects a bare owner leak" "$GATE8_RC"
[[ "$GATE8_OUT" == *"README.md"* ]] && ok "gate names the offending file" || bad "gate names the offending file" "not found in: $GATE8_OUT"

echo ""
echo "== Item 9: negative control — a post-rewrite project-name leak still fails the gate =="
cp -r "$TARGET_DIR"/. "$NEG_DIR_9"/
echo "AUTONOMOUS-FOREVER" >> "$NEG_DIR_9/README.md"
GATE9_OUT="$(bash "$GATE_SH" "$NEG_DIR_9" 2>&1)"
GATE9_RC=$?
assert_false "gate rejects a post-rewrite case-variant project-name leak" "$GATE9_RC"
[[ "$GATE9_OUT" == *"README.md"* ]] && ok "gate names the offending file (item 9)" || bad "gate names the offending file (item 9)" "not found in: $GATE9_OUT"

echo ""
echo "== Item 10: allowlist staleness stays fail-closed — expected delta is ZERO entries =="
# Was 2 (the #2190 survivors, a worktree-claims.sh comment quoting an archived
# path that carried the codename). Both were fixed at source once the whole
# tree became the thing that gets published, so the block is empty. The
# assertion's job is unchanged: pin the count so an entry cannot be added
# quietly. Zero is the number a zero-allowlist policy is supposed to hold at.
ALLOWLIST_COUNT="$(sed -n '/=== ALLOWLIST_START ===/,/=== ALLOWLIST_END ===/p' "$REPO_ROOT/open-source/IDENTIFIER-RULES.txt" | grep -vE '^\s*#|^\s*$|ALLOWLIST_START|ALLOWLIST_END' | wc -l | tr -d '[:space:]')"
assert_eq "ALLOWLIST carries no entries at all" "0" "$ALLOWLIST_COUNT"

echo ""
echo "== Item 11: colon rule — every allowlist entry has exactly 2 colons =="
COLON_BAD="$(sed -n '/=== ALLOWLIST_START ===/,/=== ALLOWLIST_END ===/p' "$REPO_ROOT/open-source/IDENTIFIER-RULES.txt" | grep -vE '^\s*#|^\s*$|ALLOWLIST_START|ALLOWLIST_END' | awk -F: 'NF-1 != 2 {print; c++} END{print c+0}' | tail -1)"
assert_eq "no allowlist entry carries a third colon" "0" "$COLON_BAD"

echo ""
echo "== Item 12: rewrite didn't produce an invalid Python identifier =="
COMPILE_OUT="$(python3 -m compileall -q "$TARGET_DIR/backend" "$TARGET_DIR/scripts" "$TARGET_DIR/hooks" 2>&1)"
COMPILE_RC=$?
assert_true "compileall exits 0" "$COMPILE_RC"
assert_eq "compileall produces no output" "" "$COMPILE_OUT"

echo ""
echo "== Item 13: rewrite didn't break JSON =="
JSON_OUT="$(python3 -c "
import json, glob, sys
paths = ['$TARGET_DIR/dashboard/public/manifest.json'] + glob.glob('$TARGET_DIR/dashboard/scenarios/*/load-page.scenario.json')
for p in paths:
    json.load(open(p))
print(len(paths))
" 2>&1)"
JSON_RC=$?
assert_true "manifest.json + every scenario JSON file still parses" "$JSON_RC"

echo ""
echo "== Item 15: the most adopter-visible branding is rewritten =="
TITLE_LINE="$(grep '<title>' "$TARGET_DIR/dashboard/index.html")"
[[ "$TITLE_LINE" == *"Fulcrumaxe"* ]] && ok "dashboard <title> reads Fulcrumaxe" || bad "dashboard <title> reads Fulcrumaxe" "$TITLE_LINE"
assert_not_contains "dashboard <title> has no old-name spelling" "utonomous" "$TITLE_LINE"
MANIFEST_JSON="$(cat "$TARGET_DIR/dashboard/public/manifest.json")"
[[ "$MANIFEST_JSON" == *'"name": "Fulcrumaxe"'* ]] && ok "manifest.json name is Fulcrumaxe" || bad "manifest.json name is Fulcrumaxe" "$MANIFEST_JSON"
# Note: the manifest's "description" field legitimately contains the plain
# English word "autonomous" ("autonomous development team") — that is not
# the branded project name, so the check below is spelling-specific (same
# canary regex as item 3), not a bare "utonomous" substring match.
MANIFEST_OLD_NAME="$(printf '%s' "$MANIFEST_JSON" | grep -ciE 'autonomous[-_ ]forever|autoforever' || true)"
assert_eq "manifest.json has no old-PROJECT-name spelling" "0" "$MANIFEST_OLD_NAME"

echo ""
echo "== Item 16: the one genuine history site was fixed at source, not swept =="
SERVER_PY="$REPO_ROOT/backend/server.py"
LEGACY_LITERAL="$(grep -c '_LEGACY_DB_PATH = Path.home() / ".autonomous-forever" / "server.db"' "$SERVER_PY")"
assert_eq "_LEGACY_DB_PATH literal is unchanged in our repo" "1" "$LEGACY_LITERAL"
DOCSTRING_STALE="$(grep -c 'hardcoded to \`\`~/.autonomous-forever/server.db\`\`' "$SERVER_PY")"
assert_eq "docstring no longer names the specific historical directory" "0" "$DOCSTRING_STALE"

echo ""
echo "== Item 17: the stale 'Do not add one. Ever.' rule is retired =="
RULES_TXT="$REPO_ROOT/open-source/IDENTIFIER-RULES.txt"
STALE_RULE="$(grep -c 'Do not add one. Ever.' "$RULES_TXT")"
assert_eq "'Do not add one. Ever.' no longer appears" "0" "$STALE_RULE"
README_NOTE="$(grep -c 'sentence it protected is gone' "$RULES_TXT")"
[[ "$README_NOTE" -ge 1 ]] && ok "replacement text records that README.md's rename-history sentence is gone" || bad "replacement text records that README.md's rename-history sentence is gone" "not found"

echo ""
echo "== Item 18: the ordering invariant comment is corrected, bare rules last =="
OLD_INVARIANT="$(grep -c "Order doesn't matter — these patterns don't overlap" "$RULES_TXT")"
assert_eq "the false 'order doesn't matter' claim is gone" "0" "$OLD_INVARIANT"
LAST_REWRITE_LINE="$(sed -n '/=== REWRITE_START ===/,/=== REWRITE_END ===/p' "$RULES_TXT" | grep -vE '^\s*#|^\s*$|REWRITE_START|REWRITE_END' | tail -1)"
[[ "$LAST_REWRITE_LINE" == *"AutoForever"* ]] && ok "a bare project-name rule is the last REWRITE entry" || bad "a bare project-name rule is the last REWRITE entry" "$LAST_REWRITE_LINE"

echo ""
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
