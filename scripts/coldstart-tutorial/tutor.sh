#!/usr/bin/env bash
# scripts/coldstart-tutorial/tutor.sh
#
# Bookkeeping-only driver for the coldstart guided tutorial (D#1539 Batch T).
# Mirrors scripts/coldstart-interview/harness.sh's checkpoint pattern exactly:
# this script never teaches anything itself (Team Lead drives the actual
# teaching via AskUserQuestion at real coldstart time) -- it only persists
# per-lesson progress + elapsed wall-clock to
# $AUTONOMOUS_TEAM_STATE_DIR/coldstart/<session>/tutorial-progress.json so a
# dropped/killed session resumes without re-teaching completed lessons.
#
# Content lives in lessons.json (static, data-driven, points to wiki pages
# for depth rather than duplicating detail -- see wiki_ref per lesson).
#
# Subcommands:
#   --self-test                              non-interactive end-to-end self-check (CI)
#   --start [--session ID]                    initialize a session, print its ID
#   --start-lesson LESSON_ID [--session ID]   mark the start time of a lesson (for elapsed timing)
#   --mark-lesson LESSON_ID [--session ID]    mark a lesson complete + persist elapsed
#   --status [--session ID]                   print remaining lessons + summary
#   --resume [--session ID]                   print the next incomplete lesson id (or "none")
#   --default-action [--session ID]           print "skip" or "offer" (repeat-operator detection)
#   --skip-tutorial [--session ID]            record tutorial_status=skipped, exit 0
#
# No network calls, no GH calls -- this script only touches the state dir and
# the lessons.json manifest next to it. It never writes to .claude/agents/*.md
# or any other engine-synced path (Spec item 9, asserted in --self-test).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LESSONS="${COLDSTART_TUTORIAL_LESSONS:-$SCRIPT_DIR/lessons.json}"
STATE_ROOT="${AUTONOMOUS_TEAM_STATE_DIR:-$HOME/.autonomous-forever-state}"

# shellcheck source=../lib/platform-compat.sh
source "$SCRIPT_DIR/../lib/platform-compat.sh"

# _agents_fingerprint DIR — "path mtime size" per *.md file under DIR, one
# per line, sorted by path. Used by --self-test's engine-boundary guard to
# prove nothing under .claude/agents/ moved. Replaces a
# `find -exec stat ... {} \;` one-liner using GNU-only stat flags, which has
# no BSD spelling to fall into via -exec (D#2263 Phase 2).
_agents_fingerprint() {
  local dir="$1" f mtime size
  [[ -d "$dir" ]] || return 0
  while IFS= read -r f; do
    mtime=$(pc_stat_mtime "$f" 2>/dev/null) || mtime="ERR"
    size=$(pc_stat_size "$f" 2>/dev/null) || size="ERR"
    printf '%s %s %s\n' "$f" "$mtime" "$size"
  done < <(find "$dir" -name '*.md' | sort)
}

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}"
}

session_dir() { echo "$STATE_ROOT/coldstart/$1"; }

ensure_session_dir() {
  local dir
  dir="$(session_dir "$1")"
  mkdir -p "$dir"
  echo "$dir"
}

progress_path() { echo "$(session_dir "$1")/tutorial-progress.json"; }
marker_path() { echo "$(session_dir "$1")/.lesson-start-$2"; }

new_session_id() {
  echo "cst-$(date +%Y%m%dT%H%M%S)-$$"
}

init_progress_if_missing() {
  local session="$1"
  local path
  path="$(progress_path "$session")"
  if [[ ! -f "$path" ]]; then
    ensure_session_dir "$session" > /dev/null
    printf '{"session": "%s", "lessons": {}, "tutorial_status": "in_progress", "total_seconds": 0.0}\n' "$session" > "$path"
  fi
}

start_lesson() {
  local session="$1" lesson="$2"
  ensure_session_dir "$session" > /dev/null
  date +%s.%N > "$(marker_path "$session" "$lesson")"
}

mark_lesson() {
  local session="$1" lesson="$2"
  init_progress_if_missing "$session"
  local path start_marker start_epoch now_epoch elapsed
  path="$(progress_path "$session")"
  start_marker="$(marker_path "$session" "$lesson")"
  if [[ -f "$start_marker" ]]; then
    start_epoch="$(cat "$start_marker")"
  else
    start_epoch="$(date +%s.%N)"
  fi
  now_epoch="$(date +%s.%N)"
  elapsed="$(python3 -c "print(round(float(\"$now_epoch\") - float(\"$start_epoch\"), 3))")"

  python3 - "$LESSONS" "$path" "$lesson" "$elapsed" <<'PYEOF'
import json, sys

lessons_path, progress_path, lesson_id, elapsed = sys.argv[1:5]

with open(lessons_path) as f:
    manifest = json.load(f)
known_ids = {l["id"] for l in manifest["lessons"]}
if lesson_id not in known_ids:
    print(f"unknown lesson: {lesson_id}", file=sys.stderr)
    sys.exit(1)

with open(progress_path) as f:
    progress = json.load(f)
progress.setdefault("lessons", {})[lesson_id] = {
    "status": "complete",
    "elapsed_seconds": float(elapsed),
}
progress["total_seconds"] = round(
    sum(v.get("elapsed_seconds", 0.0) for v in progress["lessons"].values()), 3
)
with open(progress_path, "w") as f:
    json.dump(progress, f, indent=2, sort_keys=True)
    f.write("\n")
print("ok")
PYEOF

  rm -f "$start_marker"
}

all_lesson_ids() {
  python3 -c "import json; d=json.load(open('$LESSONS')); print(' '.join(l['id'] for l in d['lessons']))"
}

remaining_lessons() {
  local session="$1"
  local path
  path="$(progress_path "$session")"
  python3 - "$LESSONS" "$path" <<'PYEOF'
import json, sys
lessons_path, progress_path = sys.argv[1], sys.argv[2]
with open(lessons_path) as f:
    manifest = json.load(f)
try:
    with open(progress_path) as f:
        progress = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    progress = {"lessons": {}}
done = {k for k, v in (progress.get("lessons") or {}).items() if v.get("status") == "complete"}
remaining = [l["id"] for l in manifest["lessons"] if l["id"] not in done]
print(json.dumps(remaining))
PYEOF
}

status() {
  local session="$1"
  local path
  path="$(progress_path "$session")"
  echo "session:   $session"
  echo "progress:  $path"
  echo "remaining: $(remaining_lessons "$session")"
}

resume_next() {
  local session="$1"
  local remaining
  remaining="$(remaining_lessons "$session")"
  python3 -c "
import json
r = json.loads('''$remaining''')
print(r[0] if r else 'none')
"
}

# Repeat-operator detection: any OTHER session dir under $STATE_ROOT/coldstart/
# that already has an answers.json (a completed/in-progress coldstart-interview
# session) means this is a returning operator -- default the tutorial offer to
# "skip". A brand-new operator (no prior sessions) gets "offer".
default_action() {
  local session="$1"
  local root="$STATE_ROOT/coldstart"
  if [[ -d "$root" ]]; then
    local d
    for d in "$root"/*/; do
      [[ -d "$d" ]] || continue
      local other
      other="$(basename "$d")"
      [[ "$other" == "$session" ]] && continue
      if [[ -f "$d/answers.json" || -f "$d/tutorial-progress.json" ]]; then
        echo "skip"
        return 0
      fi
    done
  fi
  echo "offer"
}

skip_tutorial() {
  local session="$1"
  init_progress_if_missing "$session"
  local path
  path="$(progress_path "$session")"
  python3 - "$path" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    progress = json.load(f)
progress["tutorial_status"] = "skipped"
with open(path, "w") as f:
    json.dump(progress, f, indent=2, sort_keys=True)
    f.write("\n")
print("ok")
PYEOF
}

# --- self-test -----------------------------------------------------------

self_test() {
  local session
  session="selftest-$$-$(date +%s)"
  local dir
  dir="$(ensure_session_dir "$session")"
  echo "[self-test] session dir: $dir"

  # Engine-boundary guard baseline: fingerprint every .claude/agents/*.md
  # mtime+size before touching anything, re-check at the end (Spec item 9).
  local repo_root agents_before agents_after
  repo_root="$(cd "$SCRIPT_DIR/../.." && pwd)"
  agents_before="$(_agents_fingerprint "$repo_root/.claude/agents")"

  # 1. Start session + mark one lesson complete.
  start_lesson "$session" "orient"
  mark_lesson "$session" "orient" > /dev/null

  local path
  path="$(progress_path "$session")"
  if ! python3 -c "import json; json.load(open('$path'))" > /dev/null 2>&1; then
    echo "[self-test] FAIL: tutorial-progress.json is not valid JSON after lesson 1" >&2
    exit 1
  fi
  echo "[self-test] tutorial-progress.json valid after first lesson"

  # 2. Resumability: a second invocation into the same session must NOT
  #    re-list the completed lesson as remaining, and --resume must skip it.
  local remaining_after_1
  remaining_after_1="$(remaining_lessons "$session")"
  if echo "$remaining_after_1" | grep -q '"orient"'; then
    echo "[self-test] FAIL: orient still listed as remaining after being marked complete: $remaining_after_1" >&2
    exit 1
  fi
  local next
  next="$(resume_next "$session")"
  if [[ "$next" == "orient" ]]; then
    echo "[self-test] FAIL: --resume would re-teach completed lesson orient" >&2
    exit 1
  fi
  echo "[self-test] resumability OK — next lesson after orient: $next"

  # 3. Leave exactly one lesson incomplete; --status must report it and exit 0.
  local ids last_id
  ids="$(all_lesson_ids)"
  # shellcheck disable=SC2206
  ids_arr=($ids)
  last_id="${ids_arr[-1]}"
  for lid in "${ids_arr[@]}"; do
    [[ "$lid" == "$last_id" ]] && continue
    [[ "$lid" == "orient" ]] && continue
    start_lesson "$session" "$lid"
    mark_lesson "$session" "$lid" > /dev/null
  done

  local status_out status_rc
  status_out="$(status "$session")"
  status_rc=$?
  if [[ $status_rc -ne 0 ]]; then
    echo "[self-test] FAIL: --status exited non-zero" >&2
    exit 1
  fi
  if ! echo "$status_out" | grep -q "$last_id"; then
    echo "[self-test] FAIL: --status did not report the remaining lesson ($last_id): $status_out" >&2
    exit 1
  fi
  echo "[self-test] --status OK, reports remaining lesson: $last_id"

  # 4. --resume continues from the checkpoint (points at the one remaining lesson).
  local resume_id
  resume_id="$(resume_next "$session")"
  if [[ "$resume_id" != "$last_id" ]]; then
    echo "[self-test] FAIL: --resume expected '$last_id', got '$resume_id'" >&2
    exit 1
  fi
  echo "[self-test] --resume OK, checkpoint points at: $resume_id"

  # Finish the last lesson so this session now looks like a completed
  # coldstart-tutorial session (used by the repeat-operator check below).
  start_lesson "$session" "$last_id"
  mark_lesson "$session" "$last_id" > /dev/null
  # Also stamp an answers.json so this session mimics a prior completed
  # coldstart-interview session for the repeat-operator check.
  printf '{"topics": {"identity": {}}}\n' > "$(session_dir "$session")/answers.json"

  # 5. Repeat-operator default: a NEW session should now see this prior
  #    session and default to "skip".
  local second_session default_act
  second_session="selftest2-$$-$(date +%s)"
  ensure_session_dir "$second_session" > /dev/null
  default_act="$(default_action "$second_session")"
  if [[ "$default_act" != "skip" ]]; then
    echo "[self-test] FAIL: expected default-action 'skip' for repeat operator, got '$default_act'" >&2
    exit 1
  fi
  echo "[self-test] repeat-operator default-action OK: $default_act"

  # 6. A genuinely first-time session (isolated state dir, no prior sessions)
  #    must default to "offer".
  local fresh_root fresh_session fresh_act
  fresh_root="$(mktemp -d)"
  fresh_session="fresh-$$-$(date +%s)"
  fresh_act="$(AUTONOMOUS_TEAM_STATE_DIR="$fresh_root" "${BASH_SOURCE[0]}" --default-action --session "$fresh_session")"
  rm -rf "$fresh_root"
  if [[ "$fresh_act" != "offer" ]]; then
    echo "[self-test] FAIL: expected default-action 'offer' for first-time operator, got '$fresh_act'" >&2
    exit 1
  fi
  echo "[self-test] first-time default-action OK: $fresh_act"

  # 7. --skip-tutorial path exits cleanly and records "skipped" status.
  local skip_session
  skip_session="selftest-skip-$$-$(date +%s)"
  if ! "${BASH_SOURCE[0]}" --skip-tutorial --session "$skip_session" > /dev/null; then
    echo "[self-test] FAIL: --skip-tutorial exited non-zero" >&2
    exit 1
  fi
  local skip_path
  skip_path="$(progress_path "$skip_session")"
  if ! python3 -c "
import json
d = json.load(open('$skip_path'))
assert d.get('tutorial_status') == 'skipped', d
print('ok')
" > /dev/null; then
    echo "[self-test] FAIL: --skip-tutorial did not record 'skipped' status" >&2
    exit 1
  fi
  echo "[self-test] --skip-tutorial OK"

  # 8. Engine-boundary guard: confirm no .claude/agents/*.md file changed.
  agents_after="$(_agents_fingerprint "$repo_root/.claude/agents")"
  if [[ "$agents_before" != "$agents_after" ]]; then
    echo "[self-test] FAIL: engine-boundary guard violated — .claude/agents/*.md changed during self-test" >&2
    exit 1
  fi
  echo "[self-test] engine-boundary guard OK — no .claude/agents/*.md touched"

  echo "[self-test] PASS"
}

# --- CLI dispatch ----------------------------------------------------------

SESSION=""
LESSON=""
CMD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --self-test) CMD="self-test"; shift ;;
    --start) CMD="start"; shift ;;
    --start-lesson) CMD="start-lesson"; LESSON="$2"; shift 2 ;;
    --mark-lesson) CMD="mark-lesson"; LESSON="$2"; shift 2 ;;
    --status) CMD="status"; shift ;;
    --resume) CMD="resume"; shift ;;
    --default-action) CMD="default-action"; shift ;;
    --skip-tutorial) CMD="skip-tutorial"; shift ;;
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
  start)
    SESSION="${SESSION:-$(new_session_id)}"
    init_progress_if_missing "$SESSION"
    echo "$SESSION"
    ;;
  start-lesson)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    start_lesson "$SESSION" "$LESSON"
    ;;
  mark-lesson)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    mark_lesson "$SESSION" "$LESSON"
    ;;
  status)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    status "$SESSION"
    ;;
  resume)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    resume_next "$SESSION"
    ;;
  default-action)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    default_action "$SESSION"
    ;;
  skip-tutorial)
    [[ -n "$SESSION" ]] || { echo "--session required" >&2; exit 1; }
    skip_tutorial "$SESSION"
    ;;
  *)
    usage
    exit 1
    ;;
esac
