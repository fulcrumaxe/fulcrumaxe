#!/usr/bin/env bash
# tests/test_coldstart_repo_visibility.sh — tests for D#2227:
#
# During a real coldstart, `gh` reported an existing private repo as
# not-found because the active `gh` account (under that $HOME) had no
# access to it — the account was simply wrong, not the repo missing. The
# interview read that 404 as "this repo doesn't exist" and offered to
# create it, with "create it as public" sitting right next to "create it
# as private" in the menu. Publishing is irreversible; a wrong active
# account is not.
#
# The fix has two independent parts, tested separately here:
#   1. scripts/coldstart.sh now runs a repo-visibility check in its
#      preflight step (reusing scripts/lib/gh-precondition.sh's
#      assert_gh_can_see_repo, the same central assert D#1787 built for
#      start-the-day.sh's identical ambiguity) whenever the target already
#      has a resolvable github.com origin remote. A 404 there is now a
#      hard, loud stop BEFORE mechanical wiring, labels, or the
#      HALT/interview step ever run — not a guess the interview has to
#      make later from a wrong premise.
#   2. The agent-facing text an operator's Team Lead actually reads at
#      HANDOFF time (scripts/lib/coldstart-halt-flow.sh's
#      _coldstart_halt_interview_handoff) and the reference docs it points
#      at (wiki/Coldstart-Interview-Protocol.md, .claude/commands/coldstart.md)
#      now explicitly say: never re-diagnose a 404 as "missing repo", and
#      never offer "create it as public" as an interview option at all.
#
# Run: bash tests/test_coldstart_repo_visibility.sh
# Expects: all assertions pass, exit 0
#
# Methodology note: item 1 is tested by extracting the EXACT shipped byte
# range of coldstart.sh's preflight block (via sed, not a hand-copied
# re-transcription — same technique tests/test_start_the_day_auth_guard.sh
# uses) and running it standalone against a scratch git repo with a stubbed
# `gh` on PATH. This avoids ever running the real mechanical-wiring /
# dependency-install / labels / HALT steps (which would need node, network
# access, and a real interactive-or-agent-driven interview) just to prove
# the ONE new gate fires correctly and before any of that.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLDSTART_SH="$REPO_ROOT/scripts/coldstart.sh"

PASS=0
FAIL=0
FIXTURES=()

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; [[ $# -gt 1 ]] && echo "        $2"; FAIL=$((FAIL + 1)); }

assert_true()  { if [[ "$2" == "0" ]]; then ok "$1"; else bad "$1" "expected success, got rc=$2"; fi; }
assert_false() { if [[ "$2" != "0" ]]; then ok "$1"; else bad "$1" "expected failure, got rc=0"; fi; }
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else bad "$1" "expected to contain [$2]" "$3"; fi
}
assert_not_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then bad "$1" "expected NOT to contain [$2]"; else ok "$1"; fi
}

# ── fixture: a fake `gh` on PATH ─────────────────────────────────────────
# Modes (via GH_STUB_MODE):
#   broken  — every `gh api repos/<slug>` call fails with the exact
#             NOT_FOUND-shaped body a real account-can't-see-it 404 produces.
#   healthy — the repo-visibility check succeeds (echoes the slug back).
make_gh_stub() {
  local dir
  dir=$(mktemp -d)
  FIXTURES+=("$dir")
  cat > "$dir/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
ARGS="$*"
MODE="${GH_STUB_MODE:-broken}"
NOT_FOUND_BODY='{"message":"Not Found","documentation_url":"https://docs.github.com/rest"}'

is_repo_check() { [[ "$ARGS" == "api repos/"* ]]; }
is_auth_status() { [[ "$ARGS" == "auth status"* ]]; }
repo_check_reply() { echo "${ARGS#api repos/}" | awk '{print $1}'; }

case "$MODE" in
  broken)
    if is_auth_status; then exit 0; fi
    if is_repo_check; then echo "$NOT_FOUND_BODY"; exit 1; fi
    echo "$NOT_FOUND_BODY"; exit 1
    ;;
  healthy)
    if is_auth_status; then exit 0; fi
    if is_repo_check; then repo_check_reply; exit 0; fi
    echo "$NOT_FOUND_BODY"; exit 1
    ;;
  *)
    echo "gh-stub: unknown GH_STUB_MODE=$MODE" >&2
    exit 2
    ;;
esac
STUB
  chmod +x "$dir/gh"
  echo "$dir"
}

# Extracts the EXACT shipped preflight block (comment header through its
# closing `fi`) by content, not a hardcoded line number, so this test
# doesn't silently start testing the wrong lines if the file shifts.
extract_preflight_block() {
  local start end
  start=$(grep -n '^# 1\. Preflight$' "$COLDSTART_SH" | head -1 | cut -d: -f1)
  start=$((start - 1))  # include the "# ---" separator line above it
  # The block ends at the first top-level `fi` (column 1) at or after start
  # that closes the `if [[ "$RESUME" -eq 0 ]]` wrapper.
  end=$(awk -v s="$start" 'NR>s && /^fi$/{print NR; exit}' "$COLDSTART_SH")
  sed -n "${start},${end}p" "$COLDSTART_SH"
}

# Runs the extracted preflight block standalone: $1=repo_path $2=gh_mode
run_preflight_block() {
  local repo_path="$1" mode="$2" stubdir snippet out rc
  stubdir=$(make_gh_stub)
  snippet="$(mktemp -d)/snippet.sh"
  FIXTURES+=("$(dirname "$snippet")")
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    echo "SCRIPT_DIR='${REPO_ROOT}/scripts'"
    echo "REPO_PATH='${repo_path}'"
    echo 'RESUME=0'
    extract_preflight_block
  } > "$snippet"
  out=$(PATH="$stubdir:$PATH" GH_STUB_MODE="$mode" timeout 30 bash "$snippet" 2>&1)
  rc=$?
  printf '%s\x1e%s' "$rc" "$out"
}

echo "== Sanity: the preflight block was actually extracted =="
BLOCK="$(extract_preflight_block)"
assert_contains "extracted block references assert_gh_can_see_repo" "assert_gh_can_see_repo" "$BLOCK"
assert_contains "extracted block references coldstart_preflight" "coldstart_preflight" "$BLOCK"

echo ""
echo "== Scenario: existing repo, origin remote resolves, gh account can't see it =="

BROKEN_REPO=$(mktemp -d)
FIXTURES+=("$BROKEN_REPO")
git -C "$BROKEN_REPO" init -q
git -C "$BROKEN_REPO" remote add origin "https://github.com/fulcrumaxe/gatekeep.git"

RESULT=$(run_preflight_block "$BROKEN_REPO" broken)
RC="${RESULT%%$'\x1e'*}"
OUT="${RESULT#*$'\x1e'}"

assert_false "preflight block exits non-zero when gh can't see the repo" "$RC"
assert_contains "prints the repo-visibility section header" "repo visibility" "$OUT"
assert_contains "diagnosis explains this is NOT the same as not existing" "NOT the same as" "$OUT"
assert_contains "diagnosis names the account that failed, not the repo, as the problem" "cannot resolve repo" "$OUT"
assert_contains "explicit refusal to let this become a repo-creation prompt" "Do NOT work around this" "$OUT"
assert_not_contains "never claims the repo itself doesn't exist" "does not exist" "$OUT"
assert_not_contains "never claims the repo itself doesn't exist (alt phrasing)" "doesn't exist" "$OUT"

echo ""
echo "== Scenario: existing repo, origin remote resolves, gh account CAN see it =="

HEALTHY_REPO=$(mktemp -d)
FIXTURES+=("$HEALTHY_REPO")
git -C "$HEALTHY_REPO" init -q
git -C "$HEALTHY_REPO" remote add origin "https://github.com/fulcrumaxe/gatekeep.git"

RESULT=$(run_preflight_block "$HEALTHY_REPO" healthy)
RC="${RESULT%%$'\x1e'*}"
OUT="${RESULT#*$'\x1e'}"

assert_true "preflight block passes through when gh CAN see the repo (not a fires-unconditionally gate)" "$RC"
assert_contains "confirms visibility before letting the pipeline continue" "gh account can see" "$OUT"

echo ""
echo "== Scenario: no origin remote yet (genuinely new project) — nothing to check =="

NEW_REPO=$(mktemp -d)
FIXTURES+=("$NEW_REPO")
git -C "$NEW_REPO" init -q

RESULT=$(run_preflight_block "$NEW_REPO" broken)
RC="${RESULT%%$'\x1e'*}"
OUT="${RESULT#*$'\x1e'}"

assert_true "no origin remote — the gate is skipped, not fired" "$RC"
assert_not_contains "repo-visibility section never runs with no remote to check" "repo visibility" "$OUT"

echo ""
echo "== Item 2: agent-facing text never offers 'create it as public' =="

HANDOFF_TEXT="$(cat "$REPO_ROOT/scripts/lib/coldstart-halt-flow.sh")"
assert_contains "halt-flow HANDOFF text carries the D#2227 warning" "D#2227" "$HANDOFF_TEXT"
assert_contains "halt-flow HANDOFF text explicitly forbids offering public creation" "create it as public" "$HANDOFF_TEXT"

WIKI_TEXT="$(cat "$REPO_ROOT/wiki/Coldstart-Interview-Protocol.md")"
assert_contains "wiki protocol doc forbids offering public creation" "create it as public" "$WIKI_TEXT"
assert_contains "wiki protocol doc explains why (irreversibility)" "not reversible" "$WIKI_TEXT"

CMD_TEXT="$(cat "$REPO_ROOT/.claude/commands/coldstart.md")"
assert_contains "coldstart.md forbids offering public creation" "create it as public" "$CMD_TEXT"
assert_contains "coldstart.md references D#2227" "D#2227" "$CMD_TEXT"

echo ""
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
