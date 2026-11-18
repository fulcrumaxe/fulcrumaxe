#!/usr/bin/env bash
# tests/test_coldstart_noninteractive.sh — `coldstart.sh --non-interactive`,
# its status stream, the public-repo intake-gate self-check, resume after a
# kill, and `bootstrap.sh --branch`.
#
# Every check is a function that takes an engine copy and returns 0 when the
# behaviour holds. Each runs once against the real tree (must pass) and, where
# it guards something, once against a copy with that one thing broken (must
# FAIL) — a check that cannot fail proves nothing.
#
# No network: `gh` is a stub that logs and fails, tokens are invalid, state
# lives in a fresh temp dir, and the engine scripts are copied so the
# dependency step finds no package manifests to install.
#
# HARD RULE: Do NOT call `claude`, `claude -p`, `_start_loop_run`, or trigger /loop.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pinned hash of scripts/coldstart-status-schema.json. Changing the status
# vocabulary changes this on purpose: the cloud pins the same value.
PINNED_SCHEMA_SHA="5d80fd0932de256dc131ea6526f243a4a5d169618070233420be81607f01482a"

PASS=0
FAIL=0
ok()   { echo "  [OK] $1";   PASS=$((PASS + 1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

T="$(mktemp -d "${TMPDIR:-/tmp}/coldstart-ni.XXXXXX")"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
cat > "$T/bin/gh" <<EOF
#!/usr/bin/env bash
echo "gh \$*" >> "$T/gh.log"
exit 1
EOF
chmod +x "$T/bin/gh"

# engine_copy NAME — an engine checkout with only scripts/ and loop-bootstrap/.
engine_copy() {
  local d="$T/eng-$1"
  mkdir -p "$d"
  cp -r "$REPO_ROOT/scripts" "$d/scripts"
  cp -r "$REPO_ROOT/loop-bootstrap" "$d/loop-bootstrap"
  cp -r "$REPO_ROOT/backend" "$d/backend"
  cp -r "$REPO_ROOT/hooks" "$d/hooks"
  echo "$d"
}

# new_sb — fresh target repo, state root and home under $T/sb.
new_sb() {
  SB="$T/sb"
  rm -rf "$SB"
  mkdir -p "$SB/repo" "$SB/root" "$SB/home"
  git -C "$SB/repo" init -q -b main
  git -C "$SB/repo" -c user.name=t -c user.email=t@example.invalid commit -q --allow-empty -m init
  : > "$T/gh.log"; : > "$T/shim.log"
}

write_answers() { # file [omit-topic]
  python3 -I - "$1" "${2:-}" <<'PYEOF'
import json, sys
a = {
  "identity": {"project_name": "demo", "boss_github_username": "demo-owner"},
  "stack": {"primary_language": "python"},
  "deploy": {"ci_provider": "github"},
  "autonomy": {"agent_spawn_level": "2"},
  "mission": {"product_vision": "a demo"},
}
a.pop(sys.argv[2], None)
json.dump(a, open(sys.argv[1], "w"))
PYEOF
}

# ni_run ENG ANSWERS VISIBILITY STATUSFILE [extra args] — rc left in $RC.
ni_run() {
  local eng="$1" ans="$2" vis="$3" st="$4"
  shift 4
  # NI_STDIN: closed | a fifo path | (default) /dev/null
  (
    if [[ "${NI_STDIN:-}" == closed ]]; then exec 0<&-; else exec 0<>"${NI_STDIN:-/dev/null}"; fi
    exec timeout 120 env HOME="$SB/home" COLDSTART_STATE_ROOT="$SB/root" PATH="$T/bin:$PATH" \
      GH_TOKEN=invalid GITHUB_TOKEN=invalid GH_HOST=invalid.localhost COLDSTART_NI_GH_LOG="$T/shim.log" \
      bash "$eng/scripts/coldstart.sh" --non-interactive --answers-file "$ans" \
      --status-fd 3 --path "$SB/repo" --name demo --repo-visibility "$vis" "$@" \
      3>>"$st" >"$SB/out.log" 2>"$SB/err.log"
  ) 2>/dev/null
  RC=$?
}

tree_dump() {
  (
    cd "$1" || exit 1
    find . -path ./.git -prune -o -name timing.json -prune -o \( -type f -o -type l \) -print | sort |
    while IFS= read -r p; do
      if [[ -L "$p" ]]; then echo "L $p $(readlink "$p")"; else echo "F $p $(sha256sum < "$p")"; fi
    done
  )
}

# ---- checks ----------------------------------------------------------------

check_stream() { # ENG — closed-stdin run, valid lines, labels skipped, no gh
  local eng="$1" st="$T/stream.txt" ans="$T/ans.json"
  new_sb; write_answers "$ans"; : > "$st"
  NI_STDIN=closed ni_run "$eng" "$ans" public "$st"
  [[ $RC -eq 0 ]] || { echo "rc=$RC: $(tail -3 "$SB/err.log")"; return 1; }
  python3 -I - "$st" "$eng/scripts/coldstart-status-schema.json" <<'PYEOF' || return 1
import json, re, sys
lines = open(sys.argv[1]).read().splitlines()
s = json.load(open(sys.argv[2]))
pat = re.compile(r"^step=(\w+) state=(\w+)( code=([a-z_]+)(:[a-z_]+)?)?$")
for l in lines:
    m = pat.match(l)
    assert m, l
    assert m[1] in s["steps"] and m[2] in s["states"], l
    assert m[4] is None or m[4] in s["codes"], l
assert lines[0] == "step=preflight state=started", lines[0]
assert lines[-1] == "step=done state=ok", lines[-1]
assert "step=labels state=skipped code=cloud_owned" in lines
assert lines.count("step=labels state=skipped code=cloud_owned") == 1
assert not any("state=failed" in l for l in lines)
PYEOF
  [[ ! -s "$T/gh.log" ]] || { echo "real gh was reached: $(cat "$T/gh.log")"; return 1; }
  # The refusing shim may be probed for the operator login, nothing else:
  # no label, repo, issue, pr or api call.
  if grep -vxE 'gh auth status' "$T/shim.log" | grep -q .; then
    echo "unexpected gh calls: $(cat "$T/shim.log")"; return 1
  fi
}

check_no_stdin_read() { # ENG — stdin is a pipe nobody writes to; must not block
  local eng="$1" st="$T/stdin.txt" ans="$T/ans.json" fifo="$T/fifo"
  new_sb; write_answers "$ans"; : > "$st"; rm -f "$fifo"; mkfifo "$fifo"
  NI_STDIN="$fifo" ni_run "$eng" "$ans" private "$st"
  [[ $RC -eq 0 ]]
}

check_missing_topic() { # ENG — exit 3, named topic, nothing written
  local eng="$1" st="$T/miss.txt" ans="$T/ans-miss.json"
  new_sb; write_answers "$ans" mission; : > "$st"
  ni_run "$eng" "$ans" private "$st"
  [[ $RC -eq 3 ]] || { echo "rc=$RC"; return 1; }
  grep -qx 'step=interview state=failed code=missing_topic:mission' "$st" || return 1
  grep -q 'missing_topic:mission' "$SB/err.log" || return 1
  [[ ! -e "$SB/repo/.autonomous-team" && -z "$(ls -A "$SB/root")" ]]
}

check_gate_selfcheck() { # ENG — public: self-check runs, and a gate that lets a stranger through fails coldstart
  local eng="$1" st="$T/gate.txt" ans="$T/ans.json" rc_public rc_private
  new_sb; write_answers "$ans"; : > "$st"
  ni_run "$eng" "$ans" public "$st"; rc_public=$RC
  # A gate broken to trust everyone: public must refuse, private never asks.
  local variant="$T/eng-variant"
  rm -rf "$variant"; cp -r "$eng" "$variant"
  python3 -I - "$variant/scripts/lib/external_intake_gate.py" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p).read()
needle = 'return True, "external_awaiting_intake_approval"'
assert needle in s
open(p, "w").write(s.replace(needle, 'return False, "external_approved"'))
PYEOF
  new_sb; : > "$st"
  ni_run "$variant" "$ans" public "$st"
  local rc_broken=$RC
  grep -qx 'step=preflight state=failed code=preflight_failed' "$st" || { rm -rf "$variant"; return 1; }
  new_sb; : > "$st"
  ni_run "$variant" "$ans" private "$st"; rc_private=$RC
  rm -rf "$variant"
  [[ $rc_public -eq 0 && $rc_broken -eq 1 && $rc_private -eq 0 ]]
}

check_resume() { # ENG — kill at each step, rerun, same tree as a clean run
  local eng="$1" ans="$T/ans.json" st step clean
  write_answers "$ans"
  new_sb; st="$T/clean-st.txt"; : > "$st"
  ni_run "$eng" "$ans" public "$st"
  [[ $RC -eq 0 ]] || { echo "clean run rc=$RC: $(tail -3 "$SB/err.log")"; return 1; }
  clean="$(tree_dump "$SB/repo"; tree_dump "$SB/root")"
  [[ -n "$clean" ]] || return 1
  for step in preflight interview wiring deps sandbox_hook bundle; do
    new_sb; st="$T/kill-$step.txt"; : > "$st"
    COLDSTART_TEST_KILL_STEP="$step" ni_run "$eng" "$ans" public "$st"
    [[ $RC -eq 137 ]] || { echo "kill at $step: rc=$RC (wanted 137)"; return 1; }
    ni_run "$eng" "$ans" public "$st"
    [[ $RC -eq 0 ]] || { echo "resume after $step: rc=$RC: $(tail -3 "$SB/err.log")"; return 1; }
    if [[ "$(tree_dump "$SB/repo"; tree_dump "$SB/root")" != "$clean" ]]; then
      echo "tree differs after kill at $step"; return 1
    fi
  done
}

check_branch_flag() { # ENG — bootstrap writes only when B is checked out
  local eng="$1" repo="$T/bsrepo" before
  new_sb; repo="$SB/repo"; before="$(tree_dump "$repo")"
  bs() { env HOME="$T" PATH="$T/bin:$PATH" GH_TOKEN=invalid GITHUB_TOKEN=invalid GH_HOST=invalid.localhost \
           bash "$eng/loop-bootstrap/bootstrap.sh" --repo acme/demo "$@" "$repo" >/dev/null 2>"$T/bs.err"; }
  ! bs --branch other || { echo "accepted the wrong branch"; return 1; }
  grep -q 'was not touched' "$T/bs.err" && [[ "$before" == "$(tree_dump "$repo")" ]] || return 1
  # matching branch and no flag: both proceed (a dry run proves the guard lets them through)
  bs --branch main --dry-run && bs --dry-run
}

# expect_pass NAME FUNCTION ENG / expect_caught NAME FUNCTION MUTANT_ENG
expect_pass()   { if "$2" "$3"; then ok "$1"; else bad "$1"; fi; }
expect_caught() { if "$2" "$3" >/dev/null 2>&1; then bad "$1 (mutation NOT caught)"; else ok "$1 (mutation caught)"; fi; }

ENG="$(engine_copy real)"

echo "== status schema"
SHA="$(bash "$ENG/scripts/coldstart.sh" --print-status-schema)"
[[ "$SHA" == "$(sha256sum < "$ENG/scripts/coldstart-status-schema.json" | cut -d' ' -f1)" ]] && ok "prints sha256 of the schema file" || bad "schema hash is not the file's sha256"
[[ "$SHA" == "$PINNED_SCHEMA_SHA" ]] && ok "hash matches the pinned value" || bad "hash differs from pinned $PINNED_SCHEMA_SHA"
M="$(engine_copy m-schema)"
sed -i 's/"skipped"/"skipped", "extra"/' "$M/scripts/coldstart-status-schema.json"
[[ "$(bash "$M/scripts/coldstart.sh" --print-status-schema)" != "$PINNED_SCHEMA_SHA" ]] && ok "editing the vocabulary changes the hash (mutation caught)" || bad "vocabulary edit not caught by hash"
source "$ENG/scripts/lib/coldstart-status.sh"
coldstart_status_emit bogus ok 2>/dev/null && bad "emit accepted an unknown step" || ok "emit refuses a value outside the vocabulary"
coldstart_status_emit preflight failed made_up 2>/dev/null && bad "emit accepted an unknown code" || ok "emit refuses an unknown code"

echo "== stream, stdin, labels"
expect_pass "closed-stdin run: valid lines, labels skipped/cloud_owned, gh never called" check_stream "$ENG"
expect_pass "stdin is never read (open pipe with no writer does not block)" check_no_stdin_read "$ENG"
M3="$(engine_copy m-labels)"
sed -i 's/coldstart_status_emit labels skipped cloud_owned/coldstart_status_emit labels ok/' "$M3/scripts/lib/coldstart-noninteractive.sh"
expect_caught "labels reported skipped/cloud_owned" check_stream "$M3"

echo "== missing core topic"
expect_pass "missing core topic exits 3 with missing_topic:<id>, nothing written" check_missing_topic "$ENG"
M4="$(engine_copy m-missing)"
sed -i 's/return 3/return 0/' "$M4/scripts/lib/coldstart-noninteractive.sh"
expect_caught "missing-topic exit" check_missing_topic "$M4"

echo "== public-repo intake gate"
expect_pass "public: gate refuses a stranger; broken gate fails coldstart; private skips" check_gate_selfcheck "$ENG"
M5="$(engine_copy m-gate)"
sed -i 's/if \[\[ "\$NI_REPO_VISIBILITY" != "private" \]\]; then/if false; then/' "$M5/scripts/lib/coldstart-noninteractive.sh"
expect_caught "gate self-check is run" check_gate_selfcheck "$M5"
python3 -I - "$ENG" <<'PYEOF' && ok "S-8: a non-collaborator's Discussion starts no work (gate refuses)" || bad "S-8: gate let a stranger through"
import os, sys
os.environ.setdefault("AUTONOMOUS_TEAM_REPO", "selfcheck/selfcheck")
sys.path.insert(0, sys.argv[1] + "/scripts/lib")
sys.path.insert(1, sys.argv[1])
import external_intake_gate as g
for labels in ([], ["bug"], ["intake-approved-by-body-text"]):
    blocked, _ = g.should_block_spawn("stranger", labels, {"collab"})
    assert blocked is True, labels
blocked, _ = g.should_block_spawn("collab", [], {"collab"})
assert blocked is False
PYEOF
# No Issue path: the scripts that call the gate never read a GitHub Issue as work.
hits="$(grep -nE 'gh +issue' "$ENG/scripts/pre-spawn-check.sh" "$ENG/scripts/team-lead-iteration.sh" "$ENG/scripts/loop-phased-step5.sh" "$ENG/scripts/lib/external_intake_gate.py" | grep -v -- '--label team-log' || true)"
[[ -z "$hits" ]] && ok "S-8: no 'gh issue' work trigger exists (only the team-log lookup)" || bad "S-8: gh issue trigger found: $hits"

echo "== resume"
expect_pass "kill at every step, rerun: same tree as a clean run" check_resume "$ENG"
M6="$(engine_copy m-resume)"
sed -i 's/  coldstart_status_emit "\$step" started$/  coldstart_status_emit "$step" started; _ni_mark_done "$step"/' "$M6/scripts/lib/coldstart-noninteractive.sh"
grep -q '_ni_mark_done "\$step"$' "$M6/scripts/lib/coldstart-noninteractive.sh" || bad "resume mutant not applied"
expect_caught "resume (step journalled before it finishes)" check_resume "$M6"

echo "== bootstrap --branch"
expect_pass "--branch refuses a different checked-out branch, passes the matching one, flag-less unchanged" check_branch_flag "$ENG"
M7="$(engine_copy m-branch)"
sed -i 's/if \[\[ "\$CURRENT_BRANCH" != "\$TARGET_BRANCH" \]\]; then/if false; then/' "$M7/loop-bootstrap/bootstrap.sh"
expect_caught "--branch guard" check_branch_flag "$M7"

echo ""
echo "passed: $PASS  failed: $FAIL"
[[ $FAIL -eq 0 ]]
