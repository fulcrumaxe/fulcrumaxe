#!/usr/bin/env bash
# scripts/lib/coldstart-halt-flow.sh
#
# D#1539 Batch W — the HALT-seam flow that replaces coldstart.sh's old blind
# checklist-and-exit step. Owns TWO wirings in one place (per the Spec's
# explicit decision to avoid two batches race-editing the same HALT block):
#
#   1. D#1538's deferred interview wiring ("Slice C") — drives
#      scripts/coldstart-interview/harness.sh one topic at a time.
#   2. The new-vs-existing orient beat + deep-tutorial offer added by D#1539.
#
# Three-beat shape (committed UX per the Spec's Consensus Summary):
#   beat 1 — orient:   one-paragraph mental model + the project_kind answer
#                       (already known from --mode; recorded into the "mode"
#                       interview topic too, so PM/mission-analyst can read it).
#   beat 2 — interview: coldstart-interview/harness.sh drives the remaining
#                       core topics, surfacing each question's `why` field as
#                       1-2 sentence JIT micro-teaching.
#   beat 3 — tutorial offer: OFFERS (never auto-launches)
#                       coldstart-tutorial/tutor.sh, recommended to run after
#                       the operator's first merge.
#
# This module never calls the AskUserQuestion tool itself (that's a Claude
# Code tool, not a shell primitive) -- it drives real interactive prompts via
# `read` when run in a real terminal, and falls back to manifest defaults
# (no stdin reads) when COLDSTART_HALT_NONINTERACTIVE=1, which is how
# --self-test exercises the whole flow without a human at the keyboard.
#
# D#1603 Slice 2 -- when COLDSTART_AGENT_DRIVEN=1, beat 2 (interview) hands
# off to an agent instead of running the read loop: it prints an orient
# message plus a structured HANDOFF block pointing at
# wiki/Coldstart-Interview-Protocol.md and scripts/coldstart-interview/
# repo-signals.sh, then returns. This is the adaptive, agent-conducted path
# -- the fixed `read`-driven topic loop below is unchanged and remains the
# fallback for a bare human running coldstart with no agent present.
#
# No network calls, no GH calls -- this module only touches the state dir via
# harness.sh / tutor.sh and prints to stdout/stderr.

set -euo pipefail

_HALT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_HALT_REPO_ROOT="$(cd "$_HALT_LIB_DIR/../.." && pwd)"

_INTERVIEW_HARNESS="$_HALT_REPO_ROOT/scripts/coldstart-interview/harness.sh"
_INTERVIEW_QUESTIONS="$_HALT_REPO_ROOT/scripts/coldstart-interview/questions.json"
_TUTOR="$_HALT_REPO_ROOT/scripts/coldstart-tutorial/tutor.sh"
_SEED_BACKLOG="$_HALT_REPO_ROOT/scripts/coldstart-interview/seed-backlog.py"

# --- beat 1: orient ---------------------------------------------------------

_coldstart_halt_orient() {
  local project_name="$1" repo_path="$2" mode="$3"
  local mode_desc
  if [[ "$mode" == "new" ]]; then
    mode_desc="starting a brand-new project from scratch"
  else
    mode_desc="wrapping this existing repo"
  fi
  cat <<EOF

=== coldstart.sh: orient (beat 1 of 3) ===

Mechanical setup is done: state dir, symlinks, project.json, labels, and the
sandbox hook are wired up for "$project_name" at $repo_path.

One-paragraph mental model before the interview: a Team Lead spawns
specialist roles (executor, code-reviewer, security-reviewer,
project-manager, and more) per unit of work. Every proposed change starts as
a GitHub Discussion, gets a frozen Spec, is implemented in an isolated git
worktree by an executor, reviewed, and merged — the Discussion -> Spec -> PR
-> merge loop. Everything an agent touches runs sandboxed until it's
reviewed.

Mode: $mode ($mode_desc)
EOF
}

# --- beat 2: interview -------------------------------------------------------

# Ask one question interactively (or return the manifest default when
# non-interactive). $1=prompt $2=why $3=default
_coldstart_halt_ask() {
  local prompt="$1" why="$2" default="$3" ans
  if [[ -n "$why" && "$why" != "null" ]]; then
    echo "  (why this matters: $why)" >&2
  fi
  if [[ "${COLDSTART_HALT_NONINTERACTIVE:-0}" -eq 1 ]]; then
    echo "$default"
    return 0
  fi
  read -r -p "  $prompt [$default]: " ans || ans=""
  echo "${ans:-$default}"
}

# Agent-driven handoff: prints orient copy + a structured HANDOFF block and
# returns WITHOUT touching stdin or looping over questions.json topics. The
# agent (Team Lead) picks this up and conducts the interview itself per
# wiki/Coldstart-Interview-Protocol.md, persisting answers via
# harness.sh --record-topic and checking harness.sh --coverage-check before
# --finish-session.
_coldstart_halt_interview_handoff() {
  local project_name="$1" session="$2" repo_path="$3"
  # D#2216: this is the seam the agent actually reads at real-coldstart time.
  # coldstart.sh exports AUTONOMOUS_TEAM_STATE_DIR before it ever calls into
  # this file, so it's normally already set correctly here -- but every
  # command below runs as a NEW subprocess from the agent's OWN later shell
  # calls, which does not inherit that export. Print it explicitly and put
  # it inline on every command, so the agent can't miss it and can't
  # accidentally fall through to harness.sh's own default state dir (which
  # is what produced D#2216's stray duplicate session).
  local state_dir="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.${project_name}-state}"
  echo ""
  echo "=== coldstart.sh: interview (beat 2 of 3) -- agent-driven handoff ==="
  echo "COLDSTART_AGENT_DRIVEN=1 detected -- an agent is conducting this coldstart, so the"
  echo "fixed read-loop interview is skipped. Over to you, agent."
  cat <<EOF

HANDOFF:
  protocol:        wiki/Coldstart-Interview-Protocol.md
  repo_signals:     bash scripts/coldstart-interview/repo-signals.sh --repo-path "$repo_path"
  session:          $session
  state_dir:        $state_dir
  record_answer:    AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/coldstart-interview/harness.sh --record-topic <topic> --answers '<json>' --session $session
  coverage_check:   AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/coldstart-interview/harness.sh --coverage-check --session $session
  finish:           AUTONOMOUS_TEAM_STATE_DIR="$state_dir" bash scripts/coldstart-interview/harness.sh --finish-session --session $session

IMPORTANT: every harness.sh call above is a fresh subprocess. Export
AUTONOMOUS_TEAM_STATE_DIR="$state_dir" in your own shell first (or set it
inline as shown above) for EVERY one of them, including --record-topic and
--finish-session. Skipping this makes harness.sh fall back to its own
default state dir instead of this project's, and a stray duplicate of the
interview session accumulates there instead of under $state_dir/coldstart/.

Run repo-signals.sh first and skip/pre-fill anything "$project_name" already answers.
Open with the mission/vision question, not a stack checklist. Choose each follow-up
adaptively from prior answers, the repo signals, and the domain. Treat questions.json
as a coverage checklist, not a script -- every core topic needs an answer, but order,
phrasing, and depth are yours to choose. Persist every answer via --record-topic as you
go, confirm --coverage-check comes back empty, then --finish-session and run generate.py.

GITHUB REPO STATE (D#2227): by the time you're reading this, preflight already
checked -- if "$repo_path" has a github.com origin remote, gh confirmed the active
account can actually SEE that repo. So don't independently re-diagnose "this repo
doesn't exist" from your own gh call coming back 404/not-found here or later (e.g.
during labels) -- that shape means the active gh account cannot see it, which is an
auth problem, not a missing repo, and preflight would already have stopped the run
over it. Never offer to create the repo as a remedy for that. Separately, and
regardless of cause: never present "create it as public" as an interview option,
full stop -- not even alongside "create it as private" in a list someone is
arrowing through. Publishing is irreversible; getting the auth right is not. If a
public repo is genuinely wanted, that is a deliberate action the operator takes
themselves, outside this flow -- never something this interview offers or defaults to.
EOF
}

_coldstart_halt_interview() {
  local project_name="$1" session="$2" mode="$3" repo_path="${4:-}"

  # D#1872 fix round -- an agent driving coldstart through a tool call (no
  # tty on stdin) previously fell through to the plain read-loop below with
  # neither COLDSTART_AGENT_DRIVEN nor COLDSTART_HALT_NONINTERACTIVE set:
  # `read -r -p` hits immediate EOF, doesn't even print its prompt text, and
  # silently returns every default in well under a second -- core_complete
  # ends up true with zero real interaction, and nothing about that failure
  # is visible in the transcript. Detect that specific gap and treat it the
  # same as an explicit agent handoff instead of guessing at defaults with
  # no human present. This does not touch the two callers that legitimately
  # want silent defaults on non-interactive stdin: both self-test's own
  # entry point (coldstart_halt_self_test, above) and --self-test's CI path
  # export COLDSTART_HALT_NONINTERACTIVE=1 themselves before ever reaching
  # here, so the elif below never fires for them.
  if [[ "${COLDSTART_AGENT_DRIVEN:-0}" -ne 1 ]] && [[ "${COLDSTART_HALT_NONINTERACTIVE:-0}" -ne 1 ]] && [[ ! -t 0 ]]; then
    COLDSTART_AGENT_DRIVEN=1
  fi

  if [[ "${COLDSTART_AGENT_DRIVEN:-0}" -eq 1 ]]; then
    _coldstart_halt_interview_handoff "$project_name" "$session" "$repo_path"
    return 0
  fi

  echo ""
  echo "=== coldstart.sh: interview (beat 2 of 3) ==="
  echo "A few quick questions about \"$project_name\" — press Enter to accept the default shown in [brackets]."

  # The project_kind answer is already known from --mode. Record it into the
  # "mode" topic directly so downstream PM/mission-analyst reads it, and so
  # this counts as the interview's first answered topic.
  bash "$_INTERVIEW_HARNESS" --start-topic mode --session "$session" >/dev/null
  bash "$_INTERVIEW_HARNESS" --record-topic mode \
    --answers "{\"project_kind\": \"$mode\"}" --session "$session" >/dev/null

  local remaining topic
  remaining="$(bash "$_INTERVIEW_HARNESS" --list-remaining --session "$session")"
  for topic in $(python3 -c "import json,sys; print(' '.join(json.loads(sys.argv[1])))" "$remaining"); do
    bash "$_INTERVIEW_HARNESS" --start-topic "$topic" --session "$session" >/dev/null

    local qrows built
    qrows="$(python3 - "$_INTERVIEW_QUESTIONS" "$topic" <<'PYEOF'
import json, sys
manifest_path, topic = sys.argv[1], sys.argv[2]
d = json.load(open(manifest_path))
t = next(t for t in d["topics"] if t["id"] == topic)
for q in t["questions"]:
    print("\t".join([q["id"], q.get("prompt", q["id"]), q.get("why", ""), str(q.get("default", ""))]))
PYEOF
)"
    built="{}"
    while IFS=$'\t' read -r qid qprompt qwhy qdefault; do
      [[ -z "$qid" ]] && continue
      local ans
      ans="$(_coldstart_halt_ask "$qprompt" "$qwhy" "$qdefault")"
      built="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); d[sys.argv[2]]=sys.argv[3]; print(json.dumps(d))" "$built" "$qid" "$ans")"
    done <<< "$qrows"

    bash "$_INTERVIEW_HARNESS" --record-topic "$topic" --answers "$built" --session "$session" >/dev/null
  done

  bash "$_INTERVIEW_HARNESS" --finish-session --session "$session"
}

# --- beat 3: tutorial offer ---------------------------------------------------

_coldstart_halt_tutorial_offer() {
  local session="$1"
  echo ""
  echo "=== coldstart.sh: deep tutorial (beat 3 of 3, opt-in) ==="
  local default_act
  default_act="$(bash "$_TUTOR" --default-action --session "$session")"
  if [[ "$default_act" == "skip" ]]; then
    echo "[coldstart] looks like a repeat operator (a prior coldstart session was found) — defaulting to skip the deep tutorial."
    bash "$_TUTOR" --skip-tutorial --session "$session" >/dev/null
  else
    cat <<EOF
[coldstart] first time here. The deep tutorial walks through the role
roster, the Discussion -> Spec -> PR -> merge loop, sandbox/worktree
isolation, the dashboard, and autonomy dials, in ~5-min resumable chunks.

Recommended: run it AFTER you've watched your first Discussion become a
merged PR — it's a lot easier to follow anchored to something you just saw
happen, rather than up front. Not launched automatically; it's opt-in, on
your schedule:

    bash scripts/coldstart-tutorial/tutor.sh --start --session $session
EOF
  fi
}

# --- seed backlog offer (D#1622 Batch C2) -----------------------------------
#
# NEVER auto-launches a real GitHub call, same offer-not-execute pattern as
# the tutorial offer above -- this is the HALT seam, a human-judgment point,
# not a place to fire GH mutations unprompted. In non-interactive/self-test
# mode this previews the plan OFFLINE (--plan-only, zero network calls) so
# CI still gets coverage; in a real interactive run it only prints the
# command for the operator to run themselves, gated on whether the
# "backlog.seed_backlog" interview answer was "yes".

_coldstart_halt_seed_backlog_offer() {
  local session="$1"
  local ans_path="${AUTONOMOUS_TEAM_STATE_DIR:-.autonomous-team}/coldstart/$session/answers.json"

  echo ""
  echo "=== coldstart.sh: seed initial backlog (opt-in) ==="

  if [[ ! -f "$ans_path" ]]; then
    echo "[coldstart] no answers.json found for session $session -- skipping backlog seed preview (no network)."
    return 0
  fi

  if [[ "${COLDSTART_HALT_NONINTERACTIVE:-0}" -eq 1 ]]; then
    echo "[coldstart] non-interactive mode -- previewing backlog seeds offline (--plan-only), no GitHub calls will be made."
    python3 "$_SEED_BACKLOG" --plan-only --answers "$ans_path" >/dev/null 2>&1 || true
    echo "[coldstart] backlog seed preview complete (plan-only, zero network calls)."
    return 0
  fi

  local seed_backlog_answer
  seed_backlog_answer="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print('no')
    sys.exit(0)
print(d.get('topics', {}).get('backlog', {}).get('seed_backlog', 'no'))
" "$ans_path" 2>/dev/null || echo "no")"

  if [[ "$seed_backlog_answer" != "yes" ]]; then
    echo "[coldstart] backlog seeding not requested (answer: $seed_backlog_answer). Run it yourself later if you change your mind:"
    echo "    python3 $_SEED_BACKLOG --answers $ans_path"
    return 0
  fi

  cat <<EOF
[coldstart] backlog seeding requested. Once you've run generate.py to produce
this project's config.json (if you haven't already), seed its starter
Discussion backlog with:

    python3 $_SEED_BACKLOG --answers $ans_path

This is never auto-launched here -- it makes real GitHub API calls against
the project's own repo (resolved from the identity.repo_owner /
identity.project_name answers), never this framework's repo. On a persistent
GitHub failure it degrades to a local replay file and still exits 0.
EOF
}

# --- entry point: real run ---------------------------------------------------

coldstart_halt_flow() {
  local repo_path="$1" project_name="$2" mode="$3"
  local session
  session="$(bash "$_INTERVIEW_HARNESS" --start-session)"
  echo "[coldstart] interview session: $session"

  _coldstart_halt_orient "$project_name" "$repo_path" "$mode"
  _coldstart_halt_interview "$project_name" "$session" "$mode" "$repo_path"
  _coldstart_halt_tutorial_offer "$session"
  _coldstart_halt_seed_backlog_offer "$session"
}

# --- entry point: --self-test -------------------------------------------------
#
# Exercises the exact same three beats non-interactively (no stdin reads, no
# GitHub calls) against a temp/ephemeral interview session, honoring an
# externally-set AUTONOMOUS_TEAM_STATE_DIR when present (so CI callers can
# point it at a scratch dir), and falling back to a private mktemp -d
# otherwise so this never touches a real operator's state dir.

coldstart_halt_self_test() {
  local mode="${1:-existing}"
  local made_temp_state_dir=0
  if [[ -z "${AUTONOMOUS_TEAM_STATE_DIR:-}" ]]; then
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)"
    export AUTONOMOUS_TEAM_STATE_DIR
    made_temp_state_dir=1
  fi
  export COLDSTART_HALT_NONINTERACTIVE=1

  echo "[self-test] state dir: $AUTONOMOUS_TEAM_STATE_DIR"
  echo "[self-test] mode: $mode"

  local session out rc=0
  session="$(bash "$_INTERVIEW_HARNESS" --start-session)"
  echo "[self-test] interview session: $session"

  out="$(_coldstart_halt_orient "selftest-project" "/tmp/selftest-repo" "$mode")"
  if ! echo "$out" | grep -q "Mode: $mode ("; then
    echo "[self-test] FAIL: orient beat did not reflect --mode $mode" >&2
    rc=1
  else
    echo "[self-test] orient beat reflects mode ($mode) OK"
  fi

  local interview_log="/tmp/coldstart-halt-selftest-interview-$$.log"
  if ! _coldstart_halt_interview "selftest-project" "$session" "$mode" "/tmp/selftest-repo" > "$interview_log" 2>&1; then
    echo "[self-test] FAIL: interview beat exited non-zero — see $interview_log" >&2
    rc=1
  else
    echo "[self-test] interview beat completed OK"
  fi

  if [[ "${COLDSTART_AGENT_DRIVEN:-0}" -eq 1 ]]; then
    # Agent-driven mode: the interview beat hands off instead of recording
    # topics itself, so check for the HANDOFF block and the ABSENCE of the
    # per-question read-loop prompts, not for a populated answers.json.
    if grep -q "^HANDOFF:" "$interview_log" && grep -q "Coldstart-Interview-Protocol.md" "$interview_log"; then
      echo "[self-test] agent-driven handoff block present OK"
    else
      echo "[self-test] FAIL: agent-driven mode did not print the HANDOFF/protocol pointer" >&2
      rc=1
    fi
    if grep -q "why this matters" "$interview_log" || grep -q "press Enter to accept the default" "$interview_log"; then
      echo "[self-test] FAIL: agent-driven mode still emitted per-question read-loop prompts" >&2
      rc=1
    else
      echo "[self-test] agent-driven mode emitted no per-question read prompts OK"
    fi
    rm -f "$interview_log"
  else
  local ans_path
  ans_path="$AUTONOMOUS_TEAM_STATE_DIR/coldstart/$session/answers.json"
  if [[ ! -f "$ans_path" ]]; then
    echo "[self-test] FAIL: answers.json not written by interview beat: $ans_path" >&2
    rc=1
  elif ! python3 -c "
import json
d = json.load(open('$ans_path'))
topics = d.get('topics', {})
assert topics.get('mode', {}).get('project_kind') == '$mode', topics.get('mode')
assert 'identity' in topics and 'stack' in topics and 'deploy' in topics and 'autonomy' in topics, topics.keys()
assert 'mission' in topics, topics.keys()
print('ok')
" > /dev/null; then
    echo "[self-test] FAIL: answers.json missing expected topics or wrong project_kind" >&2
    rc=1
  else
    echo "[self-test] answers.json carries project_kind=$mode and all core topics (incl. mission) OK"
  fi
  fi

  if ! _coldstart_halt_tutorial_offer "$session" > /tmp/coldstart-halt-selftest-tutorial-$$.log 2>&1; then
    echo "[self-test] FAIL: tutorial-offer beat exited non-zero — see /tmp/coldstart-halt-selftest-tutorial-$$.log" >&2
    rc=1
  else
    echo "[self-test] tutorial-offer beat completed (tutor.sh --default-action invoked) OK"
  fi

  local seed_backlog_log="/tmp/coldstart-halt-selftest-seed-backlog-$$.log"
  if ! _coldstart_halt_seed_backlog_offer "$session" > "$seed_backlog_log" 2>&1; then
    echo "[self-test] FAIL: seed-backlog offer beat exited non-zero — see $seed_backlog_log" >&2
    rc=1
  elif grep -qi "createDiscussion" "$seed_backlog_log"; then
    echo "[self-test] FAIL: seed-backlog offer beat attempted Discussion creation in non-interactive mode" >&2
    rc=1
  else
    echo "[self-test] seed-backlog offer beat completed with zero network calls OK"
  fi
  rm -f "$seed_backlog_log"

  rm -f /tmp/coldstart-halt-selftest-interview-$$.log /tmp/coldstart-halt-selftest-tutorial-$$.log
  if [[ "$made_temp_state_dir" -eq 1 ]]; then
    rm -rf "$AUTONOMOUS_TEAM_STATE_DIR"
  fi

  if [[ "$rc" -eq 0 ]]; then
    echo "[self-test] PASS"
  else
    echo "[self-test] one or more checks FAILED" >&2
  fi
  return $rc
}
