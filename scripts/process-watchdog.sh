#!/usr/bin/env bash
# scripts/process-watchdog.sh — kill stale autonomous-team processes
# Finds processes older than 30 minutes matching known patterns,
# skips protected PIDs (active dashboard/loop services and own ancestors).
#
# Dry-run by default: reports what it would signal and sends nothing.
# Pass --kill to actually signal (SIGTERM, then SIGKILL if still alive).
# Safe to run concurrently. Creates no state files.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 30 minutes. Overridable via env for testability (there is no other way to
# exercise the "older than MAX_AGE_SEC" branch without actually waiting);
# production runs always get the 1800s default.
MAX_AGE_SEC="${PROCESS_WATCHDOG_MAX_AGE_SEC:-1800}"

KILL_MODE=false
for arg in "$@"; do
  case "$arg" in
    --kill) KILL_MODE=true ;;
  esac
done

echo "process-watchdog: REPO_DIR=$REPO_DIR"
if [ "$KILL_MODE" = true ]; then
  echo "process-watchdog: mode=KILL (signals will be sent)"
else
  echo "process-watchdog: mode=DRY-RUN (pass --kill to actually signal)"
fi

# --------------------------------------------------------------------------
# Build protected PID set: live pidfiles under .autonomous-team/, plus own
# ancestors.
#
# The pidfile set is discovered by glob, not enumerated by name. The three
# names this used to check for (tui.pid, server.pid, loop.lock) included two
# that nothing in the tree ever wrote — they were phantom filenames, so the
# protected set was silently empty on every machine regardless of REPO_DIR.
# The real pidfiles (dashboard-*.pid, live-analyst.pid, etc.) are not 1:1
# with process names either (e.g. dashboard-sse.pid holds the PID that
# `python.*server\.py` matches), so a fixed enumeration would rot again.
# Globbing picks up whatever pidfiles actually exist, present or future.
#
# Each candidate PID is validated before being trusted: non-empty, numeric,
# and kill -0 succeeds — a stale pidfile must not protect a recycled PID.
# --------------------------------------------------------------------------
declare -a PROTECTED_PIDS=()

declare -a PIDFILES=()
shopt -s nullglob
PIDFILES+=("$REPO_DIR/.autonomous-team"/*.pid)
shopt -u nullglob
if [ -f "$REPO_DIR/.autonomous-team/loop.lock" ]; then
  PIDFILES+=("$REPO_DIR/.autonomous-team/loop.lock")
fi

for pidfile in "${PIDFILES[@]}"; do
  pid_val=$(cat "$pidfile" 2>/dev/null | tr -d '[:space:]')
  if [[ "$pid_val" =~ ^[0-9]+$ ]] && kill -0 "$pid_val" 2>/dev/null; then
    PROTECTED_PIDS+=("$pid_val")
  fi
done

# Walk own ancestor chain via /proc/$PID/stat ppid field
WALK_PID=$$
while [ "$WALK_PID" -gt 1 ] 2>/dev/null; do
  PROTECTED_PIDS+=("$WALK_PID")
  WALK_PID=$(awk '{print $4}' /proc/"$WALK_PID"/stat 2>/dev/null || echo 0)
done

echo "process-watchdog: protected PIDs: ${PROTECTED_PIDS[*]:-<none>}"

# Helper: check if a PID is in the protected set
is_protected() {
  local check_pid=$1
  local p
  for p in "${PROTECTED_PIDS[@]}"; do
    [ "$p" = "$check_pid" ] && return 0
  done
  return 1
}

# Helper: escape ERE/BRE metacharacters so a literal path is matched
# literally by pgrep -f, rather than partly interpreted as a regex.
regex_escape() {
  printf '%s' "$1" | sed -e 's/[][\.^$*+?(){}|]/\\&/g'
}

# Helper: confirm `pattern` appears as an exact argv element (or argv[0])
# of `pid`'s command line, not merely as text inside a longer argument.
# pgrep -f matches the whole command line as a string, so a process whose
# command line happens to *mention* an anchored path — another agent's
# shell, a grep, an editor — would otherwise be selected too.
argv_has_exact_element() {
  local pid="$1" pattern="$2"
  local cmdline_file="/proc/$pid/cmdline"
  [ -r "$cmdline_file" ] || return 1
  local arg
  while IFS= read -r -d '' arg; do
    [ "$arg" = "$pattern" ] && return 0
  done < "$cmdline_file"
  return 1
}

# Helper: send SIGTERM, wait up to 5s, escalate to SIGKILL if still alive.
# Shared by every detection pass so there is exactly one place that
# implements "TERM then KILL" (D#2006: the whole point of this script is
# that bare `timeout` never escalates — this must actually escalate).
# Echoes the signal that ultimately reaped the process ("SIGTERM" or
# "SIGKILL") on stdout; callers capture it via command substitution.
escalate_kill() {
  local pid="$1"
  local signal="SIGTERM"

  kill -TERM "$pid" 2>/dev/null || true

  for _i in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done

  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    signal="SIGKILL"
  fi

  printf '%s\n' "$signal"
}

# --------------------------------------------------------------------------
# Resolve team-log issue number (only needed when actually killing)
# --------------------------------------------------------------------------
LOG_ISSUE=""
resolve_log_issue() {
  [ -n "$LOG_ISSUE" ] && return
  if command -v gh &>/dev/null; then
    LOG_ISSUE=$(gh issue list --label team-log --state open --json number \
      --jq '.[0].number' 2>/dev/null || true)
  fi
}

# --------------------------------------------------------------------------
# Patterns: exactly two, each an absolute path anchored under the resolved
# REPO_DIR. This is deliberately narrow — a broad pattern like
# "python.*server\.py" or "node dist/index.js" matches any unrelated
# process that happens to share that command shape.
#
# A pattern naming the retired agent CLI was removed from this list
# 2026-08-17: pgrep -f matches the full command line, and the worktree-agent
# path prefix used by live worktree agents shares that retired name as a
# substring, so the pattern was capable of matching (and SIGTERM/SIGKILLing)
# live agents, not just stale processes.
#
# `opencode` was removed for the same reason as above, plus one more: it is
# a third-party CLI (opencode.ai) this repo never launches, so there is no
# repo path to anchor it to. Keeping it unanchored means SIGKILLing a
# user's own unrelated coding-agent session.
# --------------------------------------------------------------------------
PATTERNS=(
  "$REPO_DIR/tui/dist/index.js"
  "$REPO_DIR/dashboard/server.py"
)

# --------------------------------------------------------------------------
# Scan, filter, kill (or report, in dry-run)
# --------------------------------------------------------------------------
for pattern in "${PATTERNS[@]}"; do
  escaped_pattern=$(regex_escape "$pattern")

  while IFS= read -r pid; do
    [ -z "$pid" ] && continue

    cmd=$(ps -o args= -p "$pid" 2>/dev/null | head -c 80 | tr -d '\n' || true)

    # Skip protected PIDs
    if is_protected "$pid"; then
      echo "process-watchdog: PID $pid ($cmd) — SKIP: protected"
      continue
    fi

    # Argv-exact validation: reject text-only matches (D5)
    if ! argv_has_exact_element "$pid" "$pattern"; then
      echo "process-watchdog: PID $pid ($cmd) — SKIP: pattern is text in the command line, not an argv element"
      continue
    fi

    # Check elapsed time
    elapsed=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -z "$elapsed" ]; then
      continue  # process already gone
    fi
    if [ "$elapsed" -lt "$MAX_AGE_SEC" ]; then
      echo "process-watchdog: PID $pid ($cmd) — SKIP: only ${elapsed}s old (< ${MAX_AGE_SEC}s)"
      continue
    fi

    rss_kb=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ' || echo 0)
    rss_mb=$(( ${rss_kb:-0} / 1024 ))
    elapsed_min=$(( elapsed / 60 ))

    if [ "$KILL_MODE" != true ]; then
      echo "process-watchdog: PID $pid ($cmd, running ${elapsed_min}m, ${rss_mb}MB RSS) — DRY-RUN: would signal (pass --kill to act)"
      continue
    fi

    signal=$(escalate_kill "$pid")

    echo "process-watchdog: PID $pid ($cmd, running ${elapsed_min}m, ${rss_mb}MB RSS) — KILLED: $signal"

    # Log the kill to team-log
    resolve_log_issue
    if [ -n "$LOG_ISSUE" ]; then
      bash "$REPO_DIR/scripts/rotate-team-log.sh" comment \
        "[$(date +%H:%M)] watchdog: killed stale process PID $pid ($cmd, running ${elapsed_min}m, ${rss_mb}MB RSS) — $signal" \
        2>/dev/null || true
    fi

  done < <(pgrep -f "$escaped_pattern" 2>/dev/null || true)
done

# --------------------------------------------------------------------------
# Second pass: orphaned pytest processes (D#2006).
#
# Three orphaned full-suite `timeout N python3 -m pytest ...` runs were
# found reparented to systemd, running 4.6-5.6 hours past their own
# `timeout` bound, because bare `timeout N` (no --kill-after) sends SIGTERM
# once and then waits forever for a child that does not take the hint.
# Unlike PATTERNS above, this is not restricted to $REPO_DIR — a pytest run
# can be launched from any worktree or scratch checkout, and the signal
# that identifies it isn't a path, it's that its own parent has already
# exited while it is still running, old, and unambiguously a pytest
# invocation.
#
# All three conditions below are required, and each is independently
# load-bearing:
#   1. ppid == 1        — genuinely orphaned (reparented to init), not a
#                          live child of a still-running shell or `timeout`.
#   2. age >= MAX_AGE_SEC — a young process may still be legitimately
#                          mid-run; only age proves it is stuck.
#   3. "pytest" is an exact argv element (argv_has_exact_element, the same
#      helper the PATTERNS pass uses) — not a substring match, so a
#      process that merely *mentions* pytest in some longer argument (a
#      log path, a grep) is never swept up. This also correctly matches
#      the real-world shape from the Discussion: a `timeout N python3 -m
#      pytest ...` wrapper's own argv contains "pytest" as an exact token
#      (the -m module name), even though the wrapper's argv[0] is
#      "timeout", not "pytest".
# --------------------------------------------------------------------------
while IFS= read -r line; do
  pid=$(printf '%s' "$line" | awk '{print $1}')
  ppid_val=$(printf '%s' "$line" | awk '{print $2}')
  elapsed=$(printf '%s' "$line" | awk '{print $3}')
  [ -z "$pid" ] && continue
  [ "$ppid_val" = "1" ] || continue

  cmd=$(ps -o args= -p "$pid" 2>/dev/null | head -c 80 | tr -d '\n' || true)
  [ -z "$cmd" ] && continue  # already gone

  if ! argv_has_exact_element "$pid" "pytest"; then
    continue  # ppid==1 but not a pytest invocation — not this pass's concern
  fi

  if is_protected "$pid"; then
    echo "process-watchdog: PID $pid ($cmd) — SKIP: protected"
    continue
  fi

  if [ -z "$elapsed" ]; then
    continue  # process already gone
  fi
  if [ "$elapsed" -lt "$MAX_AGE_SEC" ]; then
    echo "process-watchdog: PID $pid ($cmd) — SKIP: only ${elapsed}s old (< ${MAX_AGE_SEC}s), orphaned pytest"
    continue
  fi

  rss_kb=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ' || echo 0)
  rss_mb=$(( ${rss_kb:-0} / 1024 ))
  elapsed_min=$(( elapsed / 60 ))

  if [ "$KILL_MODE" != true ]; then
    echo "process-watchdog: PID $pid ($cmd, running ${elapsed_min}m, ${rss_mb}MB RSS, orphaned pytest) — DRY-RUN: would signal (pass --kill to act)"
    continue
  fi

  signal=$(escalate_kill "$pid")

  echo "process-watchdog: PID $pid ($cmd, running ${elapsed_min}m, ${rss_mb}MB RSS, orphaned pytest) — KILLED: $signal"

  resolve_log_issue
  if [ -n "$LOG_ISSUE" ]; then
    bash "$REPO_DIR/scripts/rotate-team-log.sh" comment \
      "[$(date +%H:%M)] watchdog: killed orphaned pytest PID $pid ($cmd, running ${elapsed_min}m, ${rss_mb}MB RSS) — $signal" \
      2>/dev/null || true
  fi
done < <(ps -eo pid=,ppid=,etimes= 2>/dev/null || true)

exit 0
