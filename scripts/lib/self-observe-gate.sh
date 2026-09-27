#!/usr/bin/env bash
# scripts/lib/self-observe-gate.sh — Self-Observe Gate block helper for spawn-prompt injection.
#
# Usage:
#   source scripts/lib/self-observe-gate.sh
#   self_observe_gate_block [--shadow] <repo_root>  # prints ## Self-Observe Gate block to stdout
#
# The block is injected by pre-spawn-check.sh into executor spawns
# (when the relevant gate is enabled). It tells the agent to run run_analyst.py
# --single-transcript on its own output file and write retros to agent-retros.jsonl.
#
# Gate-default-false shadow mode:
#   gates.self_observe_executor = false  → shadow mode: write retros, don't flip verdict
#   gates.self_observe_executor = true   → active mode: non-corrected findings → needs-fix
#
# Turn-shape exemption (anti-gaming):
#   A turn is auto-exempt from classify_retro_skipped ONLY when its tool_calls contains
#   a call to `agent_retros.py append`. Wrapping text in <!-- self-observe --> is NOT exempt.
#
# Dedupe key: (agent_id, classifier, turn_idx) — agent_retros.py append handles this.
#
# repo_root handling (D#1876):
#   The block runs in a spawned agent's shell, which may be a git worktree — a worktree's
#   own $HOME/.claude/projects/<slug> directory (if it even exists) is NOT where that
#   agent's transcript lives; Claude Code keys transcripts by the *main checkout's* path.
#   So the transcript-discovery slug must be resolved ONCE, at generation time, against
#   the caller-supplied repo root — never re-derived inside the spawned agent's own
#   shell. Pass the root explicitly (pre-spawn-check.sh already computes REPO_ROOT at
#   :23 and sources this file in the same process); this function also falls back to
#   $REPO_ROOT or `git rev-parse --show-toplevel` for direct/manual invocation, and
#   refuses to emit a block (loud failure) if no root can be resolved at all, rather
#   than silently interpolating an empty path.
#
# backend/*.py paths — resolved at RUN time, inside the agent's own shell (D#2532):
#   Unlike the transcript slug, the run_analyst.py / agent_retros.py invocations below
#   deliberately do NOT use this caller-supplied repo_root. It is always the CALLER's
#   (pre-spawn-check.sh's) own root — the main checkout — which is wrong for a
#   worktree-isolated executor and is exactly what made the retro write sandbox-blocked
#   before D#2532: `hooks/sandbox_rules.py`'s deny-by-default unenumerated-write scan
#   flags any absolute path outside the agent's own worktree, and a literal
#   `__REPO_ROOT__`-substituted path is exactly that from inside a worktree. The block
#   below instead computes `AGENT_REPO_ROOT` live, via `git rev-parse --show-toplevel`
#   (a read-only git verb, always allowed) run in the AGENT's own shell — which returns
#   the worktree's own root for a worktree-isolated executor, and the main checkout root
#   for anyone else (e.g. Team Lead). The write target itself (agent-retros.jsonl) is no
#   longer templated into this script at all — it comes from
#   backend/state_paths.py via backend/agent_retros.py's own default, so there is
#   nothing left to get wrong here.
#
# See Discussion #531 for full rationale, Discussion #1876 for the repo-root fix,
# Discussion #2532 for the AGENT_REPO_ROOT / write-target fix.

# self_observe_gate_block [--shadow] [repo_root]
# Prints the ## Self-Observe Gate markdown block to stdout.
# Pass --shadow to emit the shadow-mode variant (writes retros but never flips verdict).
# repo_root may be given as a positional arg (either order relative to --shadow);
# if omitted, falls back to $REPO_ROOT, then `git rev-parse --show-toplevel`.
self_observe_gate_block() {
  local shadow=0
  local repo_root=""
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "--shadow" ]]; then
      shadow=1
    else
      repo_root="$arg"
    fi
  done

  if [[ -z "$repo_root" ]]; then
    repo_root="${REPO_ROOT:-}"
  fi
  if [[ -z "$repo_root" ]]; then
    repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  fi
  if [[ -z "$repo_root" ]]; then
    echo "self-observe-gate: cannot resolve repo root (pass it explicitly, set \$REPO_ROOT, or run inside a git checkout) — refusing to emit a block with an empty path" >&2
    return 1
  fi
  # Strip a trailing slash so the slug and interpolated paths below are clean.
  repo_root="${repo_root%/}"

  # Claude Code encodes a project's absolute repo path as a directory slug under
  # ~/.claude/projects/ by replacing every "/" with "-" (see backend/_repo.py's
  # _project_transcript_slug(), which this mirrors for the shell context — same
  # rule, no new resolver).
  local slug="${repo_root//\//-}"

  if [[ "$shadow" == "1" ]]; then
    local block
    block="$(cat <<'SHADOW_GATE'
## Self-Observe Gate (SHADOW MODE — mandatory before `verdict: done`)

Before emitting your AGENT_OUTPUT envelope, run the analyst on your own transcript.
In shadow mode, findings are LOGGED but do NOT change your verdict.

Run this as one shell block. The /tmp/claude-* glob is sandbox-blocked in sub-agent
worktrees — this uses $HOME directly instead (D#856):

```bash
AGENT_ID=$(basename "$WORKTREE_PATH" | sed 's/^agent-//')
AGENT_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
TRANSCRIPT=$(ls -t $HOME/.claude/projects/__TRANSCRIPT_SLUG__/*/subagents/agent-${AGENT_ID}.jsonl 2>/dev/null | head -1)
[ -r "$TRANSCRIPT" ] || TRANSCRIPT=""
if [ -z "$TRANSCRIPT" ]; then
  # Cannot read transcript -- emit skip and proceed (shadow mode anyway).
  # Include "self_observed": true, "retro_count": 0, "skip_reason": "no_transcript" in your envelope.
  echo "self-observe: skipped (no transcript access)" >&2
  python3 "$AGENT_REPO_ROOT/backend/stats/retro_skip.py" --reason no_transcript --role "$YOUR_ROLE" 2>/dev/null || true
else
  if FINDINGS=$(python3 "$AGENT_REPO_ROOT/backend/run_analyst.py" --single-transcript "$TRANSCRIPT" 2>&1); then
    :
  else
    RC=$?
    echo "self-observe: run_analyst.py failed (rc=$RC): $FINDINGS" >&2
    FINDINGS="ERROR"
  fi
  if [ "$FINDINGS" != "ERROR" ]; then
    AGENT_ID=$(basename "$TRANSCRIPT" .jsonl | sed 's/^agent-//')
    CLASSIFIER=$(echo "$FINDINGS" | python3 -c "import json,sys; fs=json.load(sys.stdin); print(fs[0]['category'] if fs else '')" 2>/dev/null || echo "")
    # For each finding, call agent_retros.py append. Do NOT set work_corrected=true
    # unless you actually fixed the issue. agent_retros.py resolves its own write
    # target (backend/state_paths.py) and records a countable skip metric itself
    # if the write fails (D#2532) -- no separate call needed here for that case.
    python3 "$AGENT_REPO_ROOT/backend/agent_retros.py" append \
      --agent-id "$AGENT_ID" \
      --role "$YOUR_ROLE" \
      --classifier "$CLASSIFIER" \
      --trigger "Describe the specific tool call or state that triggered this" \
      --why "Describe the assumption/shortcut that caused it" \
      --future-fix "One concrete actionable rule for next time" \
      --turn-idx 0 \
      --shadow-mode
  fi
fi
```

Proceed with your original verdict regardless of findings (shadow mode).
Include in your AGENT_OUTPUT envelope:
  "self_observed": true,
  "retro_count": <number of findings>

SHADOW_GATE
)"
    block="${block//__TRANSCRIPT_SLUG__/$slug}"
    block="${block//__REPO_ROOT__/$repo_root}"
    printf '%s\n' "$block"
  else
    local block
    block="$(cat <<'ACTIVE_GATE'
## Self-Observe Gate (MANDATORY before `verdict: done`)

Before emitting your AGENT_OUTPUT envelope, run the analyst on your own transcript.
Non-corrected findings force `verdict: needs-fix`.

Run this as one shell block. The /tmp/claude-* glob is sandbox-blocked in sub-agent
worktrees — this uses $HOME directly instead (D#856):

```bash
AGENT_ID=$(basename "$WORKTREE_PATH" | sed 's/^agent-//')
AGENT_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
TRANSCRIPT=$(ls -t $HOME/.claude/projects/__TRANSCRIPT_SLUG__/*/subagents/agent-${AGENT_ID}.jsonl 2>/dev/null | head -1)
[ -r "$TRANSCRIPT" ] || TRANSCRIPT=""
if [ -z "$TRANSCRIPT" ]; then
  # Cannot read transcript -- emit skip and proceed.
  # Include "self_observed": true, "retro_count": 0, "skip_reason": "no_transcript" in your envelope.
  echo "self-observe: skipped (no transcript access)" >&2
  python3 "$AGENT_REPO_ROOT/backend/stats/retro_skip.py" --reason no_transcript --role "$YOUR_ROLE" 2>/dev/null || true
else
  if FINDINGS=$(python3 "$AGENT_REPO_ROOT/backend/run_analyst.py" --single-transcript "$TRANSCRIPT" 2>&1); then
    :
  else
    RC=$?
    echo "self-observe: run_analyst.py failed (rc=$RC): $FINDINGS" >&2
    FINDINGS="ERROR"
  fi
  if [ "$FINDINGS" != "ERROR" ]; then
    AGENT_ID=$(basename "$TRANSCRIPT" .jsonl | sed 's/^agent-//')
    CLASSIFIER=$(echo "$FINDINGS" | python3 -c "import json,sys; fs=json.load(sys.stdin); print(fs[0]['category'] if fs else '')" 2>/dev/null || echo "")
    # For each finding (the tool call ensures turn-shape exemption from
    # classify_retro_skipped): call agent_retros.py append. Add --work-corrected
    # only if you fixed the issue before this step; otherwise leave it off.
    # agent_retros.py resolves its own write target (backend/state_paths.py) and
    # records a countable skip metric itself if the write fails (D#2532).
    python3 "$AGENT_REPO_ROOT/backend/agent_retros.py" append \
      --agent-id "$AGENT_ID" \
      --role "$YOUR_ROLE" \
      --classifier "$CLASSIFIER" \
      --trigger "Specific tool call / prompt / state that led to the violation" \
      --why "The assumption / habit / shortcut that produced the error" \
      --future-fix "One concrete actionable rule (e.g. 'before retrying permission_denied, run ls -la first')" \
      --turn-idx "$TURN_IDX"
  fi
fi
BUDGET_PCT=$(python3 __REPO_ROOT__/backend/budget.py status --json 2>/dev/null \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(int(d.get('pct_used',0)))" 2>/dev/null || echo 0)
if [ "$BUDGET_PCT" -ge 80 ]; then
  echo "Self-observe gate skipped: budget at ${BUDGET_PCT}%"
fi
```

If any finding has work_corrected=false: set verdict=needs-fix with retros referenced.
If all findings have work_corrected=true OR zero findings: proceed with verdict=done.
Include in your AGENT_OUTPUT envelope:
  "self_observed": true,
  "retro_count": <number of findings>

ACTIVE_GATE
)"
    block="${block//__TRANSCRIPT_SLUG__/$slug}"
    block="${block//__REPO_ROOT__/$repo_root}"
    printf '%s\n' "$block"
  fi
}

# Allow direct invocation
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-}"
  shift || true
  case "$cmd" in
    self_observe_gate_block)
      self_observe_gate_block "$@"
      ;;
    *)
      echo "Usage: $0 self_observe_gate_block [--shadow] [repo_root]" >&2
      exit 1
      ;;
  esac
fi
