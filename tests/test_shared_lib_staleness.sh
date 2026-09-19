#!/usr/bin/env bash
# tests/test_shared_lib_staleness.sh
#
# Acceptance tests for scripts/check-shared-lib-staleness.sh (D#2534).
#
# Hermetic: never touches the network or the real `gh` CLI. A fake `gh`
# stub is placed first on PATH and answers exactly the two `gh api` shapes
# the script issues (tree listing, blob content) from static fixture files
# this test writes into its own scratch dir. Local-file lookups are pointed
# at a scratch checkout via CHECK_SHARED_LIB_STALENESS_ROOT, never at this
# repo's real scripts/lib/.
#
# The checker also resolves a code-repo slug (scripts/lib/repo-resolve.sh's
# _require_code_repo) before it ever calls `gh` — reading this checkout's
# own .autonomous-team/config.json, or AUTONOMOUS_TEAM_REPO, if config.json
# is absent. That file is untracked (a clean clone of this repo has none),
# so this suite must not depend on it being present: export a fixture value
# so resolution succeeds the same way in every checkout, matching
# tests/test_gate1_receipt.sh's convention for the identical problem.
export AUTONOMOUS_TEAM_REPO="fixture/repo"

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STALENESS="$REPO_DIR/scripts/check-shared-lib-staleness.sh"

PASS=0
FAIL=0
FAILED_NAMES=()

check() {
  local name="$1" ok="$2"
  if [ "$ok" = "0" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    FAILED_NAMES+=("$name")
    echo "FAIL: $name" >&2
  fi
}

TMP_ROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

# ── Fake `gh` — never a real network call ────────────────────────────────────
FAKE_BIN="$TMP_ROOT/bin"
mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/gh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
if [[ "${1:-}" == "api" ]]; then
  path="${2:-}"
  if [[ "$path" == *"/git/trees/main?recursive=true" ]]; then
    if [[ "${FAKE_GH_TREE_FAIL:-0}" == "1" ]]; then
      echo "fake gh: simulated network failure" >&2
      exit 1
    fi
    cat "$FAKE_GH_TREE_JSON"
    exit 0
  fi
  if [[ "$path" == *"/git/blobs/"* ]]; then
    sha="${path##*/}"
    blob_file="${FAKE_GH_BLOBS_DIR:-/nonexistent}/$sha.json"
    if [[ -f "$blob_file" ]]; then
      cat "$blob_file"
      exit 0
    fi
    echo "fake gh: no such blob $sha" >&2
    exit 1
  fi
  echo "fake gh: unhandled api path: $path" >&2
  exit 1
fi
echo "fake gh: unhandled command: $*" >&2
exit 1
EOF
chmod +x "$FAKE_BIN/gh"

run_staleness() {
  # $1 = subject-dir, $2 = CHECK_SHARED_LIB_STALENESS_ROOT
  PATH="$FAKE_BIN:$PATH" CHECK_SHARED_LIB_STALENESS_ROOT="$2" \
    FAKE_GH_TREE_JSON="${FAKE_GH_TREE_JSON:-}" FAKE_GH_BLOBS_DIR="${FAKE_GH_BLOBS_DIR:-}" \
    FAKE_GH_TREE_FAIL="${FAKE_GH_TREE_FAIL:-0}" \
    bash "$STALENESS" "$1"
}

b64() { python3 -c 'import sys,base64; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())'; }

write_blob_fixture() {
  # write_blob_fixture <dir> <sha> <content-file>
  local dir="$1" sha="$2" content_file="$3"
  mkdir -p "$dir"
  python3 -c '
import json, sys
content = open(sys.argv[1], "rb").read()
import base64
print(json.dumps({"content": base64.b64encode(content).decode(), "encoding": "base64"}))
' "$content_file" >"$dir/$sha.json"
}

# ---------------------------------------------------------------------------
# A1: all identical — every file's local hash matches the code-plane tree.
# ---------------------------------------------------------------------------
SCRATCH_A="$TMP_ROOT/a/scripts/lib"
mkdir -p "$SCRATCH_A"
printf 'foo_fn() {\n  echo hi\n}\n' >"$SCRATCH_A/foo.sh"
printf 'def bar_fn():\n    pass\n' >"$SCRATCH_A/bar.py"
SHA_FOO_A="$(git hash-object "$SCRATCH_A/foo.sh")"
SHA_BAR_A="$(git hash-object "$SCRATCH_A/bar.py")"
TREE_A="$TMP_ROOT/tree-a.json"
python3 -c '
import json, sys
tree = [
    {"path": "scripts/lib/foo.sh", "type": "blob", "sha": sys.argv[1]},
    {"path": "scripts/lib/bar.py", "type": "blob", "sha": sys.argv[2]},
]
print(json.dumps({"tree": tree}))
' "$SHA_FOO_A" "$SHA_BAR_A" >"$TREE_A"

OUT_A="$(FAKE_GH_TREE_JSON="$TREE_A" run_staleness "scripts/lib" "$TMP_ROOT/a")"
RC_A=$?
[ "$RC_A" = "0" ] \
  && echo "$OUT_A" | grep -q "IDENTICAL.*scripts/lib/foo.sh" \
  && echo "$OUT_A" | grep -q "IDENTICAL.*scripts/lib/bar.py" \
  && ! echo "$OUT_A" | grep -qE "DIFFERS|ABSENT|UNREADABLE|UNREACHABLE" \
  && echo "$OUT_A" | grep -q "identical=2 differs=0 absent=0"
check "A1 all-identical -> exit0, both reported IDENTICAL, no other states" $?

# ---------------------------------------------------------------------------
# A2: mutation — local foo.sh is an older revision missing a function the
# code-plane blob has. Must report DIFFERS and name the missing function.
# bar.py stays identical as a control.
# ---------------------------------------------------------------------------
SCRATCH_B="$TMP_ROOT/b/scripts/lib"
mkdir -p "$SCRATCH_B"
printf 'foo_fn() {\n  echo old\n}\n' >"$SCRATCH_B/foo.sh"   # old: no helper_fn
printf 'def bar_fn():\n    pass\n' >"$SCRATCH_B/bar.py"     # unchanged

NEW_FOO="$TMP_ROOT/new-foo.sh"
printf 'foo_fn() {\n  echo new\n}\n\nhelper_fn() {\n  echo helper\n}\n' >"$NEW_FOO"
SHA_FOO_NEW="$(git hash-object "$NEW_FOO")"
SHA_BAR_B="$(git hash-object "$SCRATCH_B/bar.py")"

TREE_B="$TMP_ROOT/tree-b.json"
python3 -c '
import json, sys
tree = [
    {"path": "scripts/lib/foo.sh", "type": "blob", "sha": sys.argv[1]},
    {"path": "scripts/lib/bar.py", "type": "blob", "sha": sys.argv[2]},
]
print(json.dumps({"tree": tree}))
' "$SHA_FOO_NEW" "$SHA_BAR_B" >"$TREE_B"

BLOBS_B="$TMP_ROOT/blobs-b"
write_blob_fixture "$BLOBS_B" "$SHA_FOO_NEW" "$NEW_FOO"

OUT_B="$(FAKE_GH_TREE_JSON="$TREE_B" FAKE_GH_BLOBS_DIR="$BLOBS_B" run_staleness "scripts/lib" "$TMP_ROOT/b")"
RC_B=$?
[ "$RC_B" = "0" ] \
  && echo "$OUT_B" | grep -q "DIFFERS.*scripts/lib/foo.sh.*missing locally.*helper_fn" \
  && echo "$OUT_B" | grep -q "IDENTICAL.*scripts/lib/bar.py" \
  && echo "$OUT_B" | grep -q "identical=1 differs=1 absent=0"
check "A2 mutation -> exit0, DIFFERS names missing helper_fn, control stays IDENTICAL" $?

# ---------------------------------------------------------------------------
# A3: absent here — code plane has a file this checkout does not.
# ---------------------------------------------------------------------------
SCRATCH_C="$TMP_ROOT/c/scripts/lib"
mkdir -p "$SCRATCH_C"
# deliberately empty — nothing local at all

TREE_C="$TMP_ROOT/tree-c.json"
python3 -c '
import json
tree = [{"path": "scripts/lib/only-on-cp.sh", "type": "blob", "sha": "deadbeef"}]
print(json.dumps({"tree": tree}))
' >"$TREE_C"

OUT_C="$(FAKE_GH_TREE_JSON="$TREE_C" run_staleness "scripts/lib" "$TMP_ROOT/c")"
RC_C=$?
[ "$RC_C" = "0" ] \
  && echo "$OUT_C" | grep -q "ABSENT.*scripts/lib/only-on-cp.sh" \
  && ! echo "$OUT_C" | grep -q "IDENTICAL" \
  && echo "$OUT_C" | grep -q "identical=0 differs=0 absent=1"
check "A3 absent-here -> exit0, reported ABSENT, never IDENTICAL" $?

# ---------------------------------------------------------------------------
# A4: negative — code plane unreachable (tree fetch fails). Must NEVER report
# any file as identical, must say so plainly, and must still exit 0
# (advisory-only never blocks).
# ---------------------------------------------------------------------------
OUT_D="$(FAKE_GH_TREE_FAIL=1 run_staleness "scripts/lib" "$TMP_ROOT/a")"
RC_D=$?
[ "$RC_D" = "0" ] \
  && echo "$OUT_D" | grep -q "UNREACHABLE" \
  && ! echo "$OUT_D" | grep -qE "IDENTICAL|DIFFERS|ABSENT"
check "A4 code-plane unreachable -> exit0, UNREACHABLE, never identical" $?

# ---------------------------------------------------------------------------
# A5: negative — tree reachable but one differing file's blob content fetch
# fails. Must still report DIFFERS (not crash, not identical), and exit 0.
# ---------------------------------------------------------------------------
SCRATCH_E="$TMP_ROOT/e/scripts/lib"
mkdir -p "$SCRATCH_E"
printf 'foo_fn() {\n  echo old\n}\n' >"$SCRATCH_E/foo.sh"
SHA_FOO_MISSING_BLOB="deadfeed0000000000000000000000000000feed"
TREE_E="$TMP_ROOT/tree-e.json"
python3 -c '
import json, sys
print(json.dumps({"tree": [{"path": "scripts/lib/foo.sh", "type": "blob", "sha": sys.argv[1]}]}))
' "$SHA_FOO_MISSING_BLOB" >"$TREE_E"
# No FAKE_GH_BLOBS_DIR entry for this sha -> blob fetch fails inside the script.
OUT_E="$(FAKE_GH_TREE_JSON="$TREE_E" FAKE_GH_BLOBS_DIR="$TMP_ROOT/empty-blobs" run_staleness "scripts/lib" "$TMP_ROOT/e")"
RC_E=$?
[ "$RC_E" = "0" ] \
  && echo "$OUT_E" | grep -q "DIFFERS.*scripts/lib/foo.sh.*could not read code-plane content"
check "A5 blob content unreadable -> still DIFFERS (not identical), exit0" $?

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "shared-lib-staleness tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then
  echo "Failed: ${FAILED_NAMES[*]}" >&2
  exit 1
fi
exit 0
