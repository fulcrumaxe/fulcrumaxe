#!/usr/bin/env bash
# tests/test_hook_staleness_check.sh
#
# Acceptance tests for scripts/hook-staleness-check.sh (D#2362 PR-b).
#
# Hermetic: never touches the network or the real `gh` CLI. A fake `gh` stub
# is placed first on PATH and answers the four `gh api` shapes the checker
# (and the check-shared-lib-staleness.sh it reuses) issue: a recursive tree
# listing, a blob read, a path-filtered commit list, and a single commit —
# from static fixture files this test writes into its own scratch dir.
#
# All local-file and settings-file lookups are pointed at a synthetic
# scratch checkout via HOOK_STALENESS_CHECK_ROOT (and HOOK_STALENESS_HOME
# for the user-level settings file), never at this repo's real hooks/ or
# .claude/settings.json.
#
# The checker resolves a code-repo slug (scripts/lib/repo-resolve.sh's
# _require_code_repo) before it calls `gh` at all — reading this checkout's
# own .autonomous-team/config.json, or AUTONOMOUS_TEAM_REPO if that file is
# absent. Export a fixture value so resolution succeeds the same way in
# every checkout, matching tests/test_shared_lib_staleness.sh's convention
# for the identical problem.
export AUTONOMOUS_TEAM_REPO="fixture/repo"

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STALENESS="$REPO_DIR/scripts/hook-staleness-check.sh"
SHARED_LIB="$REPO_DIR/scripts/check-shared-lib-staleness.sh"

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
    [[ "${FAKE_GH_TREE_FAIL:-0}" == "1" ]] && { echo "fake gh: simulated failure" >&2; exit 1; }
    cat "$FAKE_GH_TREE_JSON"
    exit 0
  fi
  if [[ "$path" == *"/git/blobs/"* ]]; then
    sha="${path##*/}"
    blob_file="${FAKE_GH_BLOBS_DIR:-/nonexistent}/$sha.json"
    if [[ -f "$blob_file" ]]; then cat "$blob_file"; exit 0; fi
    echo "fake gh: no such blob $sha" >&2
    exit 1
  fi
  if [[ "$path" == *"/commits?path="* ]]; then
    encoded="${path#*commits?path=}"
    encoded="${encoded%%&*}"
    commits_file="${FAKE_GH_COMMITS_DIR:-/nonexistent}/${encoded//\//_}.json"
    if [[ -f "$commits_file" ]]; then cat "$commits_file"; exit 0; fi
    echo "[]"
    exit 0
  fi
  if [[ "$path" == *"/commits/"* ]]; then
    sha="${path##*/}"
    commit_file="${FAKE_GH_COMMIT_DIR:-/nonexistent}/$sha.json"
    if [[ -f "$commit_file" ]]; then cat "$commit_file"; exit 0; fi
    echo "fake gh: no such commit $sha" >&2
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
  # $1 = CHECK_ROOT, $2 = HOME override
  PATH="$FAKE_BIN:$PATH" \
    HOOK_STALENESS_CHECK_ROOT="$1" \
    HOOK_STALENESS_HOME="$2" \
    HOOK_STALENESS_SHARED_LIB_SCRIPT="$SHARED_LIB" \
    FAKE_GH_TREE_JSON="${FAKE_GH_TREE_JSON:-}" \
    FAKE_GH_BLOBS_DIR="${FAKE_GH_BLOBS_DIR:-}" \
    FAKE_GH_COMMITS_DIR="${FAKE_GH_COMMITS_DIR:-}" \
    FAKE_GH_COMMIT_DIR="${FAKE_GH_COMMIT_DIR:-}" \
    FAKE_GH_TREE_FAIL="${FAKE_GH_TREE_FAIL:-0}" \
    bash "$STALENESS"
}

write_settings() {
  # write_settings <root> <hook-relpath-under-hooks>
  local root="$1" hook_name="$2"
  mkdir -p "$root/.claude"
  cat >"$root/.claude/settings.json" <<SETTINGSEOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python3 \$CLAUDE_PROJECT_DIR/hooks/${hook_name}" }
        ]
      }
    ]
  }
}
SETTINGSEOF
}

write_tree_json() {
  # write_tree_json <out-file> <path1>=<sha1> [<path2>=<sha2> ...]
  local out="$1"; shift
  python3 -c '
import json, sys
tree = []
for pair in sys.argv[1:]:
    path, sha = pair.split("=", 1)
    tree.append({"path": path, "type": "blob", "sha": sha})
print(json.dumps({"tree": tree}))
' "$@" >"$out"
}

write_commits_list_json() {
  # write_commits_list_json <out-file> <sha1> [<sha2> ...] -- newest first
  local out="$1"; shift
  python3 -c '
import json, sys
print(json.dumps([{"sha": s} for s in sys.argv[1:]]))
' "$@" >"$out"
}

write_commit_json() {
  # write_commit_json <out-file> <filename> <blob-sha> <subject>
  local out="$1" filename="$2" blob_sha="$3" subject="$4"
  python3 -c '
import json, sys
filename, blob_sha, subject = sys.argv[1:4]
print(json.dumps({
    "commit": {"message": subject + "\n\nbody text"},
    "files": [{"filename": filename, "sha": blob_sha}],
}))
' "$filename" "$blob_sha" "$subject" >"$out"
}

# ---------------------------------------------------------------------------
# Shared fixture hooks/ package for every scenario below:
#   hooks/__init__.py     — trivial package init
#   hooks/foo.py          — the ONLY settings-registered hook; imports bar_lib
#   hooks/bar_lib.py      — library, imported only by foo.py, not registered
#   hooks/unrelated.py    — neither registered nor imported by anything
#                           registered; must never appear in output at all
# ---------------------------------------------------------------------------
write_hooks_package() {
  # write_hooks_package <hooks-dir> <foo-body-tag>
  local dir="$1" tag="$2"
  mkdir -p "$dir"
  printf '' >"$dir/__init__.py"
  printf 'import hooks.bar_lib  # noqa: F401\n\ndef adjudicate():\n    return "%s"\n' "$tag" >"$dir/foo.py"
  printf 'def helper():\n    return "lib"\n' >"$dir/bar_lib.py"
  printf 'def unused():\n    return "unrelated"\n' >"$dir/unrelated.py"
}

# ---------------------------------------------------------------------------
# T1: everything current — every in-scope file's local content matches the
# code plane. Item 6 (clean synthetic checkout).
# ---------------------------------------------------------------------------
ROOT_A="$TMP_ROOT/a"
write_hooks_package "$ROOT_A/hooks" "current"
write_settings "$ROOT_A" "foo.py"

SHA_INIT_A="$(git hash-object "$ROOT_A/hooks/__init__.py")"
SHA_FOO_A="$(git hash-object "$ROOT_A/hooks/foo.py")"
SHA_BAR_A="$(git hash-object "$ROOT_A/hooks/bar_lib.py")"
SHA_UNRELATED_A="$(git hash-object "$ROOT_A/hooks/unrelated.py")"
TREE_A="$TMP_ROOT/tree-a.json"
write_tree_json "$TREE_A" \
  "hooks/__init__.py=$SHA_INIT_A" \
  "hooks/foo.py=$SHA_FOO_A" \
  "hooks/bar_lib.py=$SHA_BAR_A" \
  "hooks/unrelated.py=$SHA_UNRELATED_A"

FAKE_GH_TREE_JSON="$TREE_A" OUT_A="$(run_staleness "$ROOT_A" "$TMP_ROOT/empty-home")"
RC_A=$?
[ "$RC_A" = "0" ] \
  && echo "$OUT_A" | grep -q "CURRENT.*hooks/foo.py" \
  && echo "$OUT_A" | grep -q "CURRENT.*hooks/bar_lib.py" \
  && echo "$OUT_A" | grep -q "CURRENT.*hooks/__init__.py" \
  && ! echo "$OUT_A" | grep -q "unrelated.py" \
  && ! echo "$OUT_A" | grep -q "^STALE " \
  && echo "$OUT_A" | grep -q "registered=3 current=3 stale=0"
check "T1 clean checkout -> exit0, registered+imported CURRENT, unrelated file never mentioned" $?

# ---------------------------------------------------------------------------
# T2: revert the registered hook by one commit — item 6 (mutation). foo.py's
# local content is the OLD revision; the code plane's blob is NEW. Exactly
# one commit touched the path since the local content matched. bar_lib.py
# and __init__.py stay identical as controls.
# ---------------------------------------------------------------------------
ROOT_B="$TMP_ROOT/b"
write_hooks_package "$ROOT_B/hooks" "old"
write_settings "$ROOT_B" "foo.py"

NEW_FOO="$TMP_ROOT/new-foo.py"
printf 'import hooks.bar_lib  # noqa: F401\n\ndef adjudicate():\n    return "new"\n' >"$NEW_FOO"
SHA_FOO_NEW="$(git hash-object "$NEW_FOO")"
SHA_INIT_B="$(git hash-object "$ROOT_B/hooks/__init__.py")"
SHA_BAR_B="$(git hash-object "$ROOT_B/hooks/bar_lib.py")"

TREE_B="$TMP_ROOT/tree-b.json"
write_tree_json "$TREE_B" \
  "hooks/__init__.py=$SHA_INIT_B" \
  "hooks/foo.py=$SHA_FOO_NEW" \
  "hooks/bar_lib.py=$SHA_BAR_B"

BLOBS_B="$TMP_ROOT/blobs-b"
mkdir -p "$BLOBS_B"
python3 -c '
import json, base64, sys
content = open(sys.argv[1], "rb").read()
print(json.dumps({"content": base64.b64encode(content).decode(), "encoding": "base64"}))
' "$NEW_FOO" >"$BLOBS_B/$SHA_FOO_NEW.json"

COMMITS_DIR_B="$TMP_ROOT/commits-list-b"
mkdir -p "$COMMITS_DIR_B"
ONE_COMMIT_SHA="1111111111111111111111111111111111abcd"
write_commits_list_json "$COMMITS_DIR_B/hooks_foo.py.json" "$ONE_COMMIT_SHA"

COMMIT_DIR_B="$TMP_ROOT/commit-b"
mkdir -p "$COMMIT_DIR_B"
write_commit_json "$COMMIT_DIR_B/$ONE_COMMIT_SHA.json" "hooks/foo.py" "$SHA_FOO_NEW" \
  "bounds the tokeniser cost — a 1.2MB command stalled it for two minutes"

OUT_B="$(FAKE_GH_TREE_JSON="$TREE_B" FAKE_GH_BLOBS_DIR="$BLOBS_B" \
  FAKE_GH_COMMITS_DIR="$COMMITS_DIR_B" FAKE_GH_COMMIT_DIR="$COMMIT_DIR_B" \
  run_staleness "$ROOT_B" "$TMP_ROOT/empty-home")"
RC_B=$?
[ "$RC_B" = "0" ] \
  && echo "$OUT_B" | grep -q "STALE.*hooks/foo.py  behind=1" \
  && echo "$OUT_B" | grep -q "bounds the tokeniser cost" \
  && echo "$OUT_B" | grep -q "CURRENT.*hooks/bar_lib.py" \
  && echo "$OUT_B" | grep -q "CURRENT.*hooks/__init__.py" \
  && ! echo "$OUT_B" | grep -q "behind=0" \
  && echo "$OUT_B" | grep -q "registered=3 current=2 stale=1"
check "T2 one commit behind -> exit0, names exactly foo.py as behind=1 with the commit subject, controls stay CURRENT" $?

# ---------------------------------------------------------------------------
# T3: no settings-registered hooks at all -> reports plainly, exit 0, never
# silent.
# ---------------------------------------------------------------------------
ROOT_C="$TMP_ROOT/c"
mkdir -p "$ROOT_C/hooks" "$ROOT_C/.claude"
printf '' >"$ROOT_C/hooks/__init__.py"
echo '{"hooks": {}}' >"$ROOT_C/.claude/settings.json"

OUT_C="$(run_staleness "$ROOT_C" "$TMP_ROOT/empty-home")"
RC_C=$?
[ "$RC_C" = "0" ] \
  && echo "$OUT_C" | grep -q "no settings-registered hooks" \
  && echo "$OUT_C" | grep -q "registered=0 current=0 stale=0"
check "T3 nothing registered -> exit0, says so plainly, not a silent pass" $?

# ---------------------------------------------------------------------------
# T4: code plane unreachable -> UNREACHABLE, never a false CURRENT, exit 0
# (advisory-only never blocks).
# ---------------------------------------------------------------------------
ROOT_D="$ROOT_A"
OUT_D="$(FAKE_GH_TREE_FAIL=1 run_staleness "$ROOT_D" "$TMP_ROOT/empty-home")"
RC_D=$?
[ "$RC_D" = "0" ] \
  && echo "$OUT_D" | grep -q "UNREACHABLE" \
  && ! echo "$OUT_D" | grep -q "CURRENT"
check "T4 code-plane unreachable -> exit0, UNREACHABLE, never a false CURRENT" $?

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "hook-staleness-check tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then
  echo "Failed: ${FAILED_NAMES[*]}" >&2
  exit 1
fi
exit 0
