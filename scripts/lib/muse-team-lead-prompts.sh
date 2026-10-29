#!/usr/bin/env bash
# scripts/lib/muse-team-lead-prompts.sh — Canonical spawn shape for Team Lead on Muse.
#
# Muse twin of scripts/lib/team-lead-prompts.sh (same role snippets, Muse
# spawn verbs). Sourced by Team Lead to emit the correct muse-spawn-prompt.sh
# invocation shape for each role. Copy the relevant snippet and fill in the
# placeholders. Source this file OR team-lead-prompts.sh, never both.
#
# The Muse spawn discipline (1:1 with the Claude lane's Agent() call):
#   1. Assemble the prompt with scripts/lib/muse-spawn-prompt.sh (it bundles
#      role card + template + brief + working principles + the AGENT_OUTPUT
#      envelope contract — never hand-write a spawn prompt).
#   2. Call subagent_spawn with the assembled prompt, passing subagent_type
#      ("executor", "code-reviewer", ...) — the run's --agents overlay
#      defines every role card as a type — and the --event-id as the
#      spawn's task name. The stop-time join reads the event id back out
#      of the spawn-accept agent_path — a wrong or missing task name
#      orphans the run row (same orphan class as a lost hook_event_id tag).
#   3. Call subagent_wait for its result; the wait summary is the child's
#      report. Fix rounds go through subagent_send_message to the same
#      subagent_id, reusing the SAME --event-id on every resume round.
#
# Test/smoke seam: MUSE_SPAWN_NO_REGISTER=1 passes --no-register through to
# the assembler (no registry/start/feed writes).
#
# Usage (Team Lead interactive session):
#   source scripts/lib/muse-team-lead-prompts.sh
#   tl_muse_spawn_executor 543 "Implement Discussion #543 per the spec"
#   tl_muse_spawn_code_reviewer 88 543
#
# Or copy the snippet literals below into a loop step directly.

# ── Canonical spawn snippets ──────────────────────────────────────────────────
#
# EXECUTOR
# --------
#   PROMPT=$(bash scripts/lib/muse-spawn-prompt.sh \
#     --role executor \
#     --brief "<task description>" \
#     --event-id "executor-<N>-<unix-ts>" \
#     --discussion <N>)
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   subagent_spawn with the prompt above, subagent_type="executor",
#   requesting worktree isolation, then subagent_wait for its result.
#   The wait result carries the real worktree path/branch — register (and, if
#   a row was already written from an earlier guess, reconcile) against that
#   actual path, not an assumed one:
#     bash scripts/lib/worktree-registry.sh reconcile-path \
#       --id <subagent_id> --actual-path <path from wait result>
#
# CODE-REVIEWER
# -------------
#   PROMPT=$(bash scripts/lib/muse-spawn-prompt.sh \
#     --role code-reviewer \
#     --brief "Review PR #<PR_NUMBER> for Discussion #<N>." \
#     --event-id "code-reviewer-<N>-<unix-ts>" \
#     --discussion <N>)
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   subagent_spawn with the prompt above, subagent_type="code-reviewer", then subagent_wait for its result.
#
# PROJECT-MANAGER
# ---------------
#   PROMPT=$(bash scripts/lib/muse-spawn-prompt.sh \
#     --role project-manager \
#     --brief "Write a Spec for Discussion #<N>." \
#     --event-id "project-manager-<N>-<unix-ts>" \
#     --discussion <N>)
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   subagent_spawn with the prompt above, subagent_type="project-manager", then subagent_wait for its result.
#
# SECURITY-REVIEWER
# -----------------
#   PROMPT=$(bash scripts/lib/muse-spawn-prompt.sh \
#     --role security-reviewer \
#     --brief "Security review PR #<PR_NUMBER>." \
#     --event-id "security-reviewer-<N>-<unix-ts>" \
#     --discussion <N>)
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   subagent_spawn with the prompt above, subagent_type="security-reviewer", then subagent_wait for its result.
#
# ACCEPTANCE-TESTER
# -----------------
#   PROMPT=$(bash scripts/lib/muse-spawn-prompt.sh \
#     --role acceptance-tester \
#     --brief "Acceptance-test PR #<PR_NUMBER> against Discussion #<N> acceptance criteria." \
#     --event-id "acceptance-tester-<N>-<unix-ts>" \
#     --discussion <N>)
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   subagent_spawn with the prompt above, subagent_type="acceptance-tester", then subagent_wait for its result.

# ── Shell helper functions (optional convenience wrappers) ─────────────────────

_muse_tl_lib_dir() {
  cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

# _muse_tl_event_id <role> <discussion> [event-id] — reuse the given id for
# fix rounds, else mint a stable-shape <role>-<discussion>-<unix-ts> id.
_muse_tl_event_id() {
  if [[ -n "${3:-}" ]]; then
    printf '%s' "$3"
  else
    printf '%s-%s-%s' "$1" "$2" "$(date +%s)"
  fi
}

tl_muse_spawn_executor() {
  # Usage: tl_muse_spawn_executor <discussion_number> "<task_description>" [event-id]
  local disc="$1" task="$2" event_id
  event_id="$(_muse_tl_event_id executor "$disc" "${3:-}")"
  echo "event-id: $event_id (pass as the subagent_spawn task name with subagent_type=<role>; reuse on fix rounds)" >&2
  bash "$(_muse_tl_lib_dir)/muse-spawn-prompt.sh" \
    --role executor \
    --brief "$task" \
    --event-id "$event_id" \
    --discussion "$disc" \
    ${MUSE_SPAWN_NO_REGISTER:+--no-register}
}

tl_muse_spawn_code_reviewer() {
  # Usage: tl_muse_spawn_code_reviewer <pr_number> <discussion_number> [event-id]
  local pr="$1" disc="$2" event_id
  event_id="$(_muse_tl_event_id code-reviewer "$disc" "${3:-}")"
  echo "event-id: $event_id (pass as the subagent_spawn task name with subagent_type=<role>; reuse on fix rounds)" >&2
  bash "$(_muse_tl_lib_dir)/muse-spawn-prompt.sh" \
    --role code-reviewer \
    --brief "Review PR #${pr} for Discussion #${disc}." \
    --event-id "$event_id" \
    --discussion "$disc" \
    ${MUSE_SPAWN_NO_REGISTER:+--no-register}
}

tl_muse_spawn_project_manager() {
  # Usage: tl_muse_spawn_project_manager <discussion_number> "<task_description>" [event-id]
  local disc="$1" task="$2" event_id
  event_id="$(_muse_tl_event_id project-manager "$disc" "${3:-}")"
  echo "event-id: $event_id (pass as the subagent_spawn task name with subagent_type=<role>; reuse on fix rounds)" >&2
  bash "$(_muse_tl_lib_dir)/muse-spawn-prompt.sh" \
    --role project-manager \
    --brief "$task" \
    --event-id "$event_id" \
    --discussion "$disc" \
    ${MUSE_SPAWN_NO_REGISTER:+--no-register}
}
