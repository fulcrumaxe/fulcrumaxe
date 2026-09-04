#!/usr/bin/env bash
# scripts/lib/team-lead-prompts.sh — Canonical spawn shape for Team Lead.
#
# Sourced by Team Lead to emit the correct spawn-agent.sh invocation shape
# for each role.  Copy the relevant snippet and fill in the placeholders.
#
# Why this file exists:
#   The corpus drift audit found that 84% of Team Lead Agent() calls bypass
#   spawn-agent.sh and use inline prompts instead.  The wrapper bundles
#   pre-spawn-check, persona injection, working principles, self-observe gate,
#   and concurrency-cap enforcement — all of which are skipped without it.
#
# Usage (Team Lead interactive session):
#   source scripts/lib/team-lead-prompts.sh
#   tl_spawn_executor 543 "Implement Discussion #543 per the spec"
#   tl_spawn_code_reviewer 88 543
#
# Or copy the snippet literals below into a loop step directly.

# ── Canonical spawn snippets ──────────────────────────────────────────────────
#
# EXECUTOR
# --------
#   PROMPT=$(bash scripts/spawn-agent.sh \
#     --role executor \
#     --discussion <N> \
#     --task-prompt "<task description>" \
#     --isolation worktree)
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   Agent(subagent_type="executor", isolation="worktree", prompt="$PROMPT")
#
#   Do NOT also pass --worktree-path here: nothing pre-provisions a tree for
#   this shape (no --pr), and Agent(isolation="worktree") always provisions
#   its own regardless — spawn-agent.sh rejects --worktree-path without --pr
#   for exactly this reason (D#2222). Once the Agent() call returns, its
#   result carries the real worktree path/branch — register (and, if a row
#   was already written from an earlier guess, reconcile) against that
#   actual path, not an assumed one:
#     bash scripts/lib/worktree-registry.sh reconcile-path \
#       --id <agent_id> --actual-path <path from Agent() result>
#
# CODE-REVIEWER
# -------------
#   PROMPT=$(bash scripts/spawn-agent.sh \
#     --role code-reviewer \
#     --discussion <N> \
#     --pr <PR_NUMBER> \
#     --task-prompt "Review PR #<PR_NUMBER> for Discussion #<N>.")
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   Agent(subagent_type="code-reviewer", prompt="$PROMPT")
#
# PROJECT-MANAGER
# ---------------
#   PROMPT=$(bash scripts/spawn-agent.sh \
#     --role project-manager \
#     --discussion <N> \
#     --task-prompt "Write a Spec for Discussion #<N>.")
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   Agent(subagent_type="project-manager", prompt="$PROMPT")
#
# SECURITY-REVIEWER
# -----------------
#   PROMPT=$(bash scripts/spawn-agent.sh \
#     --role security-reviewer \
#     --discussion <N> \
#     --pr <PR_NUMBER> \
#     --security-trigger \
#     --task-prompt "Security review PR #<PR_NUMBER>.")
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   Agent(subagent_type="security-reviewer", prompt="$PROMPT")
#
# ACCEPTANCE-TESTER
# -----------------
#   PROMPT=$(bash scripts/spawn-agent.sh \
#     --role acceptance-tester \
#     --discussion <N> \
#     --pr <PR_NUMBER> \
#     --task-prompt "Acceptance-test PR #<PR_NUMBER> against Discussion #<N> acceptance criteria.")
#   [ $? -ne 0 ] && { echo "Spawn blocked"; continue; }
#   Agent(subagent_type="acceptance-tester", prompt="$PROMPT")

# ── Shell helper functions (optional convenience wrappers) ─────────────────────

tl_spawn_executor() {
  # Usage: tl_spawn_executor <discussion_number> "<task_description>"
  local disc="$1" task="$2"
  bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/spawn-agent.sh" \
    --role executor \
    --discussion "$disc" \
    --task-prompt "$task" \
    --isolation worktree
}

tl_spawn_code_reviewer() {
  # Usage: tl_spawn_code_reviewer <pr_number> <discussion_number>
  local pr="$1" disc="$2"
  bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/spawn-agent.sh" \
    --role code-reviewer \
    --discussion "$disc" \
    --pr "$pr" \
    --task-prompt "Review PR #${pr} for Discussion #${disc}."
}

tl_spawn_project_manager() {
  # Usage: tl_spawn_project_manager <discussion_number> "<task_description>"
  local disc="$1" task="$2"
  bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/spawn-agent.sh" \
    --role project-manager \
    --discussion "$disc" \
    --task-prompt "$task"
}
