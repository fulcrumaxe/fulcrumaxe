#!/usr/bin/env bash
# scripts/lib/coldstart-noninteractive.sh — `coldstart.sh --non-interactive`.
#
# The provision half of coldstart with nobody at the keyboard: answers come
# from a JSON file, progress goes to a status descriptor, and a run killed at
# any point can be started again and finishes with the same tree.
#
# Sourced by scripts/coldstart.sh, which sets REPO_PATH, PROJECT_NAME,
# LANGUAGE, MODE, STATE_DIR, REPO_ROOT and SCRIPT_DIR first, plus
# NI_ANSWERS_FILE and NI_REPO_VISIBILITY (public|private|unknown).
#
# Contract
#   - stdin is never read (it is replaced by /dev/null on entry).
#   - Steps run in the order of the status schema. Each reports `started`,
#     then `ok` or `failed <code>`. The labels step always reports
#     `skipped cloud_owned`: labels and every other `gh` call belong to the
#     cloud, so no `gh` is needed here (preflight does not require it).
#   - Exit codes: 0 done, 1 a step failed, 3 a core interview topic is
#     missing from the answers file (`code=missing_topic:<id>`).
#   - Resume: a step that finished is journalled under
#     $STATE_DIR/coldstart-run/ and reported `ok` without running again; a
#     step killed part-way is not journalled, so it runs again from its start
#     (every delegated step is idempotent).
#
# COLDSTART_TEST_KILL_STEP=<step> SIGKILLs this process right after that step
# reports `started`. It exists only so the resume test can kill a run at each
# step deterministically.

source "$SCRIPT_DIR/lib/coldstart-status.sh"
source "$SCRIPT_DIR/lib/coldstart-preflight.sh"

_INTERVIEW_HARNESS="$SCRIPT_DIR/coldstart-interview/harness.sh"
_INTERVIEW_QUESTIONS="$SCRIPT_DIR/coldstart-interview/questions.json"
_NI_SESSION="noninteractive"
_NI_CURRENT_STEP="preflight"

_ni_journal_dir() { echo "$STATE_DIR/coldstart-run"; }
_ni_is_done() { [[ -f "$(_ni_journal_dir)/$1.done" ]]; }
_ni_mark_done() {
  mkdir -p "$(_ni_journal_dir)" || return 1
  : > "$(_ni_journal_dir)/$1.done"
}

_ni_on_signal() {
  coldstart_status_emit "$_NI_CURRENT_STEP" failed interrupted || true
  exit 143
}

# _ni_step STEP FAIL_CODE FUNCTION — run one journalled step.
_ni_step() {
  local step="$1" fail_code="$2" fn="$3"
  _NI_CURRENT_STEP="$step"
  if _ni_is_done "$step"; then
    coldstart_status_emit "$step" ok
    return 0
  fi
  coldstart_status_emit "$step" started
  if [[ "${COLDSTART_TEST_KILL_STEP:-}" == "$step" ]]; then
    kill -KILL $$
  fi
  if "$fn" && _ni_mark_done "$step"; then
    coldstart_status_emit "$step" ok
    return 0
  fi
  coldstart_status_emit "$step" failed "$fail_code"
  return 1
}

_ni_preflight() {
  COLDSTART_GH_OPTIONAL=1 coldstart_preflight || return 1
  if [[ "$NI_REPO_VISIBILITY" != "private" ]]; then
    python3 -I "$SCRIPT_DIR/lib/coldstart-gate-selfcheck.py" || return 1
  fi
}

# Print the first core topic the answers file does not cover, or nothing.
_ni_missing_topic() {
  local core
  core="$(bash "$_INTERVIEW_HARNESS" --list-core-topics)" || return 1
  python3 -I - "$NI_ANSWERS_FILE" "$core" <<'PYEOF'
import json, sys
try:
    answers = json.load(open(sys.argv[1]))
except Exception:
    answers = {}
if not isinstance(answers, dict):
    answers = {}
for topic in json.loads(sys.argv[2]):
    value = answers.get(topic)
    if not isinstance(value, dict) or not value:
        print(topic)
        break
PYEOF
}

_ni_record_answers() {
  local topics topic json
  bash "$_INTERVIEW_HARNESS" --start-session --session "$_NI_SESSION" >/dev/null || return 1
  bash "$_INTERVIEW_HARNESS" --record-topic mode \
    --answers "{\"project_kind\": \"$MODE\"}" --session "$_NI_SESSION" >/dev/null || return 1
  topics="$(python3 -I - "$NI_ANSWERS_FILE" "$_INTERVIEW_QUESTIONS" <<'PYEOF'
import json, sys
answers = json.load(open(sys.argv[1]))
known = {t["id"] for t in json.load(open(sys.argv[2]))["topics"]}
for topic, value in answers.items():
    if topic == "mode":
        continue
    if topic in known and isinstance(value, dict):
        print(topic)
    else:
        print(f"[coldstart] ignoring unknown answers topic: {topic}", file=sys.stderr)
PYEOF
  )" || return 1
  for topic in $topics; do
    json="$(python3 -I -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$NI_ANSWERS_FILE" "$topic")" || return 1
    bash "$_INTERVIEW_HARNESS" --record-topic "$topic" --answers "$json" --session "$_NI_SESSION" >/dev/null || return 1
  done
  bash "$_INTERVIEW_HARNESS" --finish-session --session "$_NI_SESSION" >/dev/null
}

_ni_wiring() {
  bash "$REPO_ROOT/scripts/coldstart-project.sh" "$REPO_PATH" "$PROJECT_NAME" --language "$LANGUAGE" --mode "$MODE"
}

_ni_deps() { bash "$REPO_ROOT/scripts/bootstrap-deps.sh"; }

_ni_sandbox_hook() { bash "$REPO_ROOT/scripts/install-sandbox-hook.sh"; }

_ni_bundle() {
  if [[ -f "$REPO_ROOT/engine/manifest.json" ]]; then
    echo "[coldstart] engine/manifest.json found — engine files are applied per its manifest"
  else
    echo "[coldstart] no engine/manifest.json on this checkout — nothing to apply"
  fi
}

# Every GitHub call belongs to the cloud in this mode. Delegated scripts probe
# `gh` opportunistically (an operator-login lookup, for one) and fall back when
# it fails, but on a sandbox with no network that probe would hang on a socket.
# Put a `gh` that refuses first on PATH; COLDSTART_NI_GH_LOG, when set, records
# what was asked of it.
_NI_GH_SHIM_DIR=""
_ni_install_gh_shim() {
  _NI_GH_SHIM_DIR="$(mktemp -d)" || return 1
  cat > "$_NI_GH_SHIM_DIR/gh" <<'SHIMEOF'
#!/usr/bin/env bash
[[ -n "${COLDSTART_NI_GH_LOG:-}" ]] && echo "gh $*" >> "$COLDSTART_NI_GH_LOG"
echo "gh is cloud-owned in a non-interactive coldstart" >&2
exit 1
SHIMEOF
  chmod +x "$_NI_GH_SHIM_DIR/gh"
  export PATH="$_NI_GH_SHIM_DIR:$PATH"
  trap 'rm -rf "$_NI_GH_SHIM_DIR"' EXIT
}

coldstart_noninteractive_run() {
  exec </dev/null
  trap _ni_on_signal TERM INT HUP
  _ni_install_gh_shim || return 1

  if [[ -z "$NI_ANSWERS_FILE" || ! -f "$NI_ANSWERS_FILE" ]]; then
    echo "ERROR: --non-interactive needs --answers-file <existing JSON file> (got: ${NI_ANSWERS_FILE:-<none>})" >&2
    coldstart_status_emit preflight failed preflight_failed || true
    return 1
  fi

  # Preflight is read-only and cheap, so it runs on every start, resumed or not.
  _NI_CURRENT_STEP="preflight"
  coldstart_status_emit preflight started
  if [[ "${COLDSTART_TEST_KILL_STEP:-}" == "preflight" ]]; then kill -KILL $$; fi
  if ! _ni_preflight; then
    coldstart_status_emit preflight failed preflight_failed
    return 1
  fi
  coldstart_status_emit preflight ok

  # Answers are checked before anything is written.
  _NI_CURRENT_STEP="interview"
  if ! _ni_is_done interview; then
    local missing
    missing="$(_ni_missing_topic)" || { coldstart_status_emit interview failed preflight_failed; return 1; }
    if [[ -n "$missing" ]]; then
      echo "missing_topic:$missing" >&2
      coldstart_status_emit interview failed "missing_topic:$missing"
      return 3
    fi
  fi
  _ni_step interview preflight_failed _ni_record_answers || return 1
  _ni_step wiring wiring_failed _ni_wiring || return 1
  _ni_step deps deps_failed _ni_deps || return 1
  _NI_CURRENT_STEP="labels"
  coldstart_status_emit labels skipped cloud_owned
  _ni_step sandbox_hook wiring_failed _ni_sandbox_hook || return 1
  _ni_step bundle wiring_failed _ni_bundle || return 1
  coldstart_status_emit done ok
  return 0
}
