#!/usr/bin/env bash
# scripts/coldstart-interview/harness.sh
#
# Drives the coldstart interview one topic at a time, persisting answers
# incrementally to $AUTONOMOUS_TEAM_STATE_DIR/coldstart/<session>/answers.json
# so a dropped session resumes without re-answering (Spec item 9), and
# recording per-topic + total wall-clock to timing.json (Spec item 11).
#
# The actual interactive question-asking (AskUserQuestion tool) is driven by
# the Team Lead at real coldstart time, one topic at a time, using the
# subcommands below to persist each topic's answers as they come in. This
# script itself never calls AskUserQuestion (it is a Claude Code tool, not a
# shell primitive) -- it only manages manifest reading + answer/timing
# persistence + resumability bookkeeping. See --self-test for a fully
# non-interactive exercise of the same code paths (used in CI).
#
# Subcommands:
#   --self-test                                  non-interactive end-to-end self-check (CI)
#   --start-session [--session ID]                initialize a session, print its ID
#   --list-remaining [--session ID]                print JSON array of unanswered core topic ids
#   --coverage-check [--session ID]                same output as --list-remaining, named for the
#                                                   agent-conducted interview's pre-finish completeness
#                                                   gate (see wiki/Coldstart-Interview-Protocol.md) --
#                                                   an empty array means every core topic is answered
#   --start-topic TOPIC [--session ID]             mark the start time of a topic (for elapsed timing)
#   --record-topic TOPIC --answers JSON [--session ID]
#                                                   merge JSON (question_id -> value) into answers.json
#                                                   for TOPIC, filling any missing question with its
#                                                   manifest default; update timing.json
#   --status [--session ID]                        print a human-readable summary
#   --finish-session [--session ID]                write total wall-clock + core-complete flag
#   --list-questions TOPIC [--mode new|existing] [--session ID]
#                                                   print resolved id/prompt pairs for TOPIC; mode
#                                                   comes from --mode if given, else from the
#                                                   session's recorded project_kind, else "existing"
#
# No network calls, no GH calls -- this script only touches the state dir and
# the manifest file next to it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${COLDSTART_MANIFEST:-$SCRIPT_DIR/questions.json}"
STATE_ROOT="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.fulcrumaxe-state}"

CORE_TOPIC_IDS=(identity stack deploy autonomy mission)

usage() {
  sed -n '2,36p' "${BASH_SOURCE[0]}"
}

session_dir() {
  local session="$1"
  echo "$STATE_ROOT/coldstart/$session"
}

ensure_session_dir() {
  local session="$1"
  local dir
  dir="$(session_dir "$session")"
  mkdir -p "$dir"
  echo "$dir"
}

answers_path() { echo "$(session_dir "$1")/answers.json"; }
timing_path() { echo "$(session_dir "$1")/timing.json"; }
marker_path() { echo "$(session_dir "$1")/.topic-start-$2"; }

# --- JSON helpers (inline python3, no extra files per the module-per-feature
#     NEW-files-only constraint) --------------------------------------------

json_init_if_missing() {
  local path="$1"
  local empty="$2"
  if [[ ! -f "$path" ]]; then
    printf '%s' "$empty" > "$path"
  fi
}

read_remaining_core_topics() {
  local session="$1"
  local ans
  ans="$(answers_path "$session")"
  python3 - "$MANIFEST" "$ans" "${CORE_TOPIC_IDS[@]}" <<'PYEOF'
import json, sys
manifest_path, answers_path = sys.argv[1], sys.argv[2]
core_ids = sys.argv[3:]
try:
    with open(answers_path) as f:
        answers = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    answers = {"topics": {}}
answered = set((answers.get("topics") or {}).keys())
remaining = [t for t in core_ids if t not in answered]
print(json.dumps(remaining))
PYEOF
}

record_topic() {
  local session="$1" topic="$2" answers_json="$3"
  local ans timing dir elapsed start_marker
  dir="$(ensure_session_dir "$session")"
  ans="$(answers_path "$session")"
  timing="$(timing_path "$session")"
  json_init_if_missing "$ans" '{"topics": {}}'
  json_init_if_missing "$timing" '{"topics": {}, "total_seconds": 0.0}'

  start_marker="$(marker_path "$session" "$topic")"
  local start_epoch now_epoch
  if [[ -f "$start_marker" ]]; then
    start_epoch="$(cat "$start_marker")"
  else
    start_epoch="$(date +%s.%N)"
  fi
  now_epoch="$(date +%s.%N)"
  elapsed="$(python3 -c "print(round(float(\"$now_epoch\") - float(\"$start_epoch\"), 3))")"

  python3 - "$MANIFEST" "$ans" "$timing" "$topic" "$answers_json" "$elapsed" <<'PYEOF'
import json, sys

manifest_path, answers_path, timing_path, topic, answers_json, elapsed = sys.argv[1:7]

with open(manifest_path) as f:
    manifest = json.load(f)

topic_def = next((t for t in manifest["topics"] if t["id"] == topic), None)
if topic_def is None:
    print(f"unknown topic: {topic}", file=sys.stderr)
    sys.exit(1)

try:
    supplied = json.loads(answers_json) if answers_json else {}
except json.JSONDecodeError:
    print("invalid --answers JSON", file=sys.stderr)
    sys.exit(1)

merged = {}
for q in topic_def["questions"]:
    qid = q["id"]
    if qid in supplied and supplied[qid] not in (None, ""):
        merged[qid] = supplied[qid]
    else:
        merged[qid] = q.get("default")

with open(answers_path) as f:
    answers = json.load(f)
answers.setdefault("topics", {})[topic] = merged
with open(answers_path, "w") as f:
    json.dump(answers, f, indent=2, sort_keys=True)
    f.write("\n")

with open(timing_path) as f:
    timing = json.load(f)
timing.setdefault("topics", {})[topic] = float(elapsed)
timing["total_seconds"] = round(sum(timing["topics"].values()), 3)
with open(timing_path, "w") as f:
    json.dump(timing, f, indent=2, sort_keys=True)
    f.write("\n")

print("ok")
PYEOF

  rm -f "$start_marker"
}

start_topic() {
  local session="$1" topic="$2"
  ensure_session_dir "$session" > /dev/null
  date +%s.%N > "$(marker_path "$session" "$topic")"
}

finish_session() {
  local session="$1"
  local ans timing
  ans="$(answers_path "$session")"
  timing="$(timing_path "$session")"
  json_init_if_missing "$ans" '{"topics": {}}'
  json_init_if_missing "$timing" '{"topics": {}, "total_seconds": 0.0}'
  python3 - "$ans" "$timing" "${CORE_TOPIC_IDS[@]}" <<'PYEOF'
import json, sys
ans_path, timing_path = sys.argv[1], sys.argv[2]
core_ids = sys.argv[3:]

with open(ans_path) as f:
    answers = json.load(f)
answered = set((answers.get("topics") or {}).keys())
core_complete = all(t in answered for t in core_ids)

with open(timing_path) as f:
    timing = json.load(f)
timing["core_complete"] = core_complete
timing["total_seconds"] = round(sum(timing.get("topics", {}).values()), 3)
with open(timing_path, "w") as f:
    json.dump(timing, f, indent=2, sort_keys=True)
    f.write("\n")
print(json.dumps({"core_complete": core_complete, "total_seconds": timing["total_seconds"]}))
PYEOF
}

status() {
  local session="$1"
  local ans timing
  ans="$(answers_path "$session")"
  timing="$(timing_path "$session")"
  echo "session:  $session"
  echo "dir:      $(session_dir "$session")"
  echo "answers:  $ans"
  echo "timing:   $timing"
  echo "remaining core topics: $(read_remaining_core_topics "$session")"
}

new_session_id() {
  echo "cs-$(date +%Y%m%dT%H%M%S)-$$"
}

# --- --list-questions: mode-aware manifest resolution ---------------------
#
# project_kind is already persisted by coldstart-halt-flow.sh via
# --record-topic mode --answers '{"project_kind": "<mode>"}' -- read it from
# the session rather than adding a second source of truth. --mode is for
# testing and for the case where no session exists yet.

resolve_mode() {
  local explicit_mode="$1" session="$2"
  if [[ -n "$explicit_mode" ]]; then
    echo "$explicit_mode"
    return
  fi
  if [[ -n "$session" ]]; then
    local ans
    ans="$(answers_path "$session")"
    if [[ -f "$ans" ]]; then
      python3 -c "
import json
try:
    d = json.load(open('$ans'))
    print(d.get('topics', {}).get('mode', {}).get('project_kind') or 'existing')
except (FileNotFoundError, json.JSONDecodeError):
    print('existing')
"
      return
    fi
  fi
  echo "existing"
}

list_questions() {
  local topic="$1" mode="$2" session="$3"
  local resolved_mode
  resolved_mode="$(resolve_mode "$mode" "$session")"
  python3 - "$MANIFEST" "$topic" "$resolved_mode" <<'PYEOF'
import json, sys

manifest_path, topic, mode = sys.argv[1], sys.argv[2], sys.argv[3]

with open(manifest_path) as f:
    manifest = json.load(f)

topic_def = next((t for t in manifest["topics"] if t["id"] == topic), None)
if topic_def is None:
    print(f"unknown topic: {topic}", file=sys.stderr)
    sys.exit(1)

suffix = "_new" if mode == "new" else ""
for q in topic_def["questions"]:
    prompt = q.get(f"prompt{suffix}") or q["prompt"]
    print(f"{q['id']}: {prompt}")
PYEOF
}

# --- self-test ---------------------------------------------------------

self_test() {
  local session
  session="selftest-$$-$(date +%s)"
  local dir
  dir="$(ensure_session_dir "$session")"
  echo "[self-test] session dir: $dir"

  # 1. Answer the "identity" topic using manifest defaults.
  start_topic "$session" "identity"
  record_topic "$session" "identity" "{}" > /dev/null

  local ans
  ans="$(answers_path "$session")"
  if ! python3 -c "import json; json.load(open('$ans'))" > /dev/null 2>&1; then
    echo "[self-test] FAIL: answers.json is not valid JSON after topic 1" >&2
    exit 1
  fi

  # 2. Resumability check: a "second invocation" (fresh call into this same
  #    session) must see identity as already answered and NOT re-ask it.
  local remaining_after_topic1
  remaining_after_topic1="$(read_remaining_core_topics "$session")"
  if echo "$remaining_after_topic1" | grep -q '"identity"'; then
    echo "[self-test] FAIL: identity still listed as remaining after being answered: $remaining_after_topic1" >&2
    exit 1
  fi
  echo "[self-test] resumability OK — remaining after identity: $remaining_after_topic1"

  # 3. Answer the rest of the core topics.
  for topic in stack deploy autonomy mission; do
    start_topic "$session" "$topic"
    record_topic "$session" "$topic" "{}" > /dev/null
  done

  local remaining_final
  remaining_final="$(read_remaining_core_topics "$session")"
  if [[ "$remaining_final" != "[]" ]]; then
    echo "[self-test] FAIL: expected no remaining core topics, got: $remaining_final" >&2
    exit 1
  fi

  # 3b. --coverage-check CLI dispatch: same completeness signal, exercised
  #     through the real command-line path (the agent's pre-finish gate).
  local coverage_after_all
  coverage_after_all="$("${BASH_SOURCE[0]}" --coverage-check --session "$session")"
  if [[ "$coverage_after_all" != "[]" ]]; then
    echo "[self-test] FAIL: --coverage-check expected [] after all core topics answered, got: $coverage_after_all" >&2
    exit 1
  fi
  echo "[self-test] --coverage-check OK (empty after full coverage)"

  local coverage_session
  coverage_session="coverage-selftest-$$-$(date +%s)"
  start_topic "$coverage_session" "identity"
  record_topic "$coverage_session" "identity" "{}" > /dev/null
  local coverage_partial
  coverage_partial="$("${BASH_SOURCE[0]}" --coverage-check --session "$coverage_session")"
  if ! echo "$coverage_partial" | grep -q '"mission"'; then
    echo "[self-test] FAIL: --coverage-check should still list unanswered 'mission' topic, got: $coverage_partial" >&2
    exit 1
  fi
  echo "[self-test] --coverage-check OK (lists unanswered topics, including mission): $coverage_partial"

  # 4. Re-run record_topic on an already-answered topic and confirm it does
  #    not blow up and answers.json stays valid (idempotent re-entry).
  record_topic "$session" "identity" "{}" > /dev/null

  # 5. finish-session + timing.json checks.
  finish_session "$session" > /dev/null
  local timing
  timing="$(timing_path "$session")"
  if ! python3 -c "
import json
d = json.load(open('$timing'))
assert d.get('core_complete') is True, d
assert set(d.get('topics', {}).keys()) >= {'identity','stack','deploy','autonomy','mission'}, d
assert 'total_seconds' in d
print('ok')
" > /dev/null; then
    echo "[self-test] FAIL: timing.json missing expected instrumentation fields" >&2
    exit 1
  fi
  echo "[self-test] timing.json OK: $(cat "$timing")"

  # 6. CLI-level dispatch check: exercise the real --record-topic --answers
  #    <json> command-line path (not the record_topic() function directly)
  #    so bugs living only in the CLI dispatch block (e.g. brace-matching
  #    bugs in default-value expansion) are actually caught here.
  local cli_session
  cli_session="selftest-cli-$$-$(date +%s)"
  if ! "${BASH_SOURCE[0]}" --record-topic identity --answers '{}' --session "$cli_session" > /dev/null; then
    echo "[self-test] FAIL: CLI --record-topic --answers '{}' exited non-zero" >&2
    exit 1
  fi
  local cli_ans
  cli_ans="$(answers_path "$cli_session")"
  if ! python3 -c "import json; json.load(open('$cli_ans'))" > /dev/null 2>&1; then
    echo "[self-test] FAIL: CLI --record-topic produced invalid answers.json" >&2
    exit 1
  fi
  echo "[self-test] CLI --record-topic --answers path OK"

  # 7. --list-questions: explicit --mode branching for both stack and deploy.
  local stack_new stack_existing deploy_new deploy_existing
  stack_new="$("${BASH_SOURCE[0]}" --list-questions stack --mode new)"
  stack_existing="$("${BASH_SOURCE[0]}" --list-questions stack --mode existing)"
  if [[ "$stack_new" == "$stack_existing" ]]; then
    echo "[self-test] FAIL: --list-questions stack --mode new/--mode existing produced identical output" >&2
    exit 1
  fi
  deploy_new="$("${BASH_SOURCE[0]}" --list-questions deploy --mode new)"
  deploy_existing="$("${BASH_SOURCE[0]}" --list-questions deploy --mode existing)"
  if [[ "$deploy_new" == "$deploy_existing" ]]; then
    echo "[self-test] FAIL: --list-questions deploy --mode new/--mode existing produced identical output" >&2
    exit 1
  fi
  echo "[self-test] --list-questions --mode branching OK (stack and deploy both differ)"

  # 8. --list-questions with no --mode resolves from the session's recorded
  #    project_kind (the real coldstart-halt-flow.sh path), not a second flag.
  local mode_session session_derived
  mode_session="modetest-$$-$(date +%s)"
  record_topic "$mode_session" "mode" '{"project_kind": "new"}' > /dev/null
  session_derived="$("${BASH_SOURCE[0]}" --list-questions stack --session "$mode_session")"
  if [[ "$session_derived" != "$stack_new" ]]; then
    echo "[self-test] FAIL: --list-questions without --mode should resolve mode from the session's project_kind" >&2
    exit 1
  fi
  echo "[self-test] --list-questions session-derived mode OK"

  # 9. No session and no --mode falls back to "existing" (the manifest
  #    default for project_kind), never a hard failure.
  local no_session_result
  no_session_result="$("${BASH_SOURCE[0]}" --list-questions stack --session "nonexistent-$$-$(date +%s)")"
  if [[ "$no_session_result" != "$stack_existing" ]]; then
    echo "[self-test] FAIL: --list-questions with no recorded session should default to existing" >&2
    exit 1
  fi
  echo "[self-test] --list-questions default-to-existing OK"

  echo "[self-test] PASS"
}

# --- CLI dispatch --------------------------------------------------------

SESSION=""
TOPIC=""
ANSWERS_JSON=""
MODE=""
CMD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --self-test) CMD="self-test"; shift ;;
    --start-session) CMD="start-session"; shift ;;
    --list-remaining) CMD="list-remaining"; shift ;;
    --coverage-check) CMD="coverage-check"; shift ;;
    --start-topic) CMD="start-topic"; TOPIC="$2"; shift 2 ;;
    --record-topic) CMD="record-topic"; TOPIC="$2"; shift 2 ;;
    --answers) ANSWERS_JSON="$2"; shift 2 ;;
    --status) CMD="status"; shift ;;
    --finish-session) CMD="finish-session"; shift ;;
    --list-questions) CMD="list-questions"; TOPIC="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

case "$CMD" in
  self-test)
    self_test
    exit 0
    ;;
  start-session)
    SESSION="${SESSION:-$(new_session_id)}"
    ensure_session_dir "$SESSION" > /dev/null
    echo "$SESSION"
    ;;
  list-remaining)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    read_remaining_core_topics "$SESSION"
    ;;
  coverage-check)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    read_remaining_core_topics "$SESSION"
    ;;
  start-topic)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    start_topic "$SESSION" "$TOPIC"
    ;;
  record-topic)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    if [[ -z "$ANSWERS_JSON" ]]; then
      ANSWERS_JSON='{}'
    fi
    record_topic "$SESSION" "$TOPIC" "$ANSWERS_JSON"
    ;;
  status)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    status "$SESSION"
    ;;
  finish-session)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    finish_session "$SESSION"
    ;;
  list-questions)
    [[ -n "$TOPIC" ]] || { echo "--list-questions requires a topic" >&2; exit 1; }
    list_questions "$TOPIC" "$MODE" "$SESSION"
    ;;
  *)
    usage
    exit 1
    ;;
esac
