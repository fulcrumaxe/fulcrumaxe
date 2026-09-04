#!/usr/bin/env bash
# scripts/lib/working-principles.sh — Working Principles block helper for spawn-prompt injection.
#
# Usage (source or call directly):
#   source scripts/lib/working-principles.sh
#   working_principles_block       # prints ## Working Principles block to stdout
#
# The ## Working Principles block is injected into spawn prompts by pre-spawn-check.sh so each
# agent receives behavior guidance on every spawn (mitigates behavior drift — parallel to
# how ## Voice blocks mitigate persona drift per arXiv 2511.00222).
#
# Mirrors the wiring pattern of scripts/lib/persona.sh / ## Voice block (D#450 / PR #485).
#
# The four principles address recurring bug categories observed in session history:
#   1. Think Before Coding — catches silent assumption bugs (wrong field names, dict type errors)
#   2. Simplicity First    — catches speculative inclusion and dead flexibility
#   3. Surgical Changes    — catches scope creep and worktree contamination
#   4. Goal-Driven Execution — catches "tests pass but feature broken" failures
#
# See Discussion #519 for full rationale and evidence table.

# working_principles_block
# Prints the ## Working Principles markdown block to stdout (always — no config file needed).
working_principles_block() {
  cat <<'PRINCIPLES'
## Working Principles

Apply ALL four before emitting `verdict: done`.

### 1. Think Before Coding
- State assumptions explicitly. Don't pick silently when ambiguity exists.
- Push back if a simpler approach exists. Stop and ask if confused.

### 2. Simplicity First
- Minimum code that meets the spec. No speculative features, flexibility, or abstractions.
- If 200 lines could be 50, rewrite it.

### 3. Surgical Changes
- Touch only what the spec requires. Don't improve adjacent code.
- Match existing style. Remove only orphans YOUR change created.

### 4. Goal-Driven Execution
- "Tests pass" is not "feature works."
- Before claiming done: run the artifact the user cares about (binary, UI page, API call) against real input. Capture output.
- For multi-step work, state a brief plan with verification per step.

**Aggregate test before `verdict: done`:**
1. Did I state my assumptions?
2. Is this the smallest change that meets the spec?
3. Did I touch anything beyond the immediate ask?
4. Did I verify against the real-world goal, not just pytest?

If any answer is "no" or "I'm not sure" — STOP.

### 5. Blocked-State Fast-Exit

When you hit a hard blocker, STOP immediately and emit `verdict: fail`. Do NOT attempt cosmetic workarounds, alternative tools, or retries with different flags.

**Trigger conditions (any one is sufficient):**
- Sandbox denial (hook blocks a tool call)
- Missing env variable or dependency that cannot be installed within the sandbox
- Unresolvable merge conflict (rebase fails with real file conflicts)
- 3 or more consecutive identical tool failures (same error, same root cause)

**Required AGENT_OUTPUT envelope on block:**
```json
{
  "verdict": "fail",
  "block_reason": "<one-line cause>",
  "evidence": "<tool name + last error excerpt>"
}
```

Emitting this envelope and stopping is the correct response. Burning 30–200 extra turns on workarounds is not.

### 6. Defensive-Re-Read VETO

**No re-reading the same file within 3 turns of a prior read.** Re-reading the same file
inside a 3-turn window is a defensive-probing anti-pattern that costs ~100 tokens each
round-trip. Exception: actively diagnosing a specific bug located in that file. If you
find yourself reaching for `Read` on a file you read in the last 3 turns, stop and ask:
do I actually need it, or am I distrusting the brief? If the brief is unclear, reply with
`verdict: blocked, block_reason: "spec_incomplete"` instead.

### 7. No Cosmetic Retries

Do NOT repeat the same command with superficial variations after a failure. Each cosmetic retry
burns ~$1500 tokens and produces no diagnostic value.

**Forbidden patterns** — running any of the following with only cosmetic changes after a failure:
`python3`, `ls`, `grep`, `cat`, `gh`, `head`, `tail`, `find`

Cosmetic variations include: different quoting, adding/removing `2>&1`, reordering flags, changing
cwd via `cd &&`, swapping single/double quotes, adding `|| true` to silence the error.

**Escape hatch (one of these must be true to retry):**
- Change a meaningful flag that alters the command's behavior
- Change the target (different file, different endpoint, different branch)
- Switch to a semantically different tool (Read instead of cat, Grep instead of grep)
- You have diagnosed the root cause and the retry addresses it directly
- Trigger §5 Blocked-State Fast-Exit: emit `verdict: fail` and stop

If none of the above apply — STOP. Diagnose first.

### 8. Never Wait on a Background Run

You are a sub-agent. **Nothing re-invokes you when a command you backgrounded finishes.**
Nothing you start yourself wakes you back up — not `run_in_background`, not `cmd &`, not
`nohup`, and not a Monitor you arm yourself. A Monitor's events are delivered to the top-level
session, not to a spawned agent, so arming one and ending your turn to wait on it stalls you
exactly like a self-started background shell. If you end a turn saying you will wait, the turn
just ends — no PR, no verdict, no report. This has cost nine agent-cycles, one of them 457K
tokens.

Three legal moves. Pick one:
1. **Foreground it, bounded**: `timeout --kill-after=5s 600 pytest backend/tests/test_foo.py 2>&1 | tail -40`.
   Target the narrowest thing that answers your question, not the whole suite.
2. **Skip it**: report what you verified directly and name the gap in your envelope.
3. **Can't bound it**: emit `verdict: fail`, `block_reason: "unbounded_background_run"`, stop.

You are in this failure mode if you are about to write "I'll wait for", "waiting for the
background", "once the monitor reports", or any variant. Stop and pick 1, 2, or 3.

The no-sleep rule under **Bash discipline** targets rate-limit retry loops. It does not
license parking — parking is worse than either alternative above.
PRINCIPLES
}

# Allow direct invocation: bash scripts/lib/working-principles.sh working_principles_block
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-}"
  shift || true
  case "$cmd" in
    working_principles_block)
      working_principles_block
      ;;
    *)
      echo "Usage: $0 working_principles_block" >&2
      exit 1
      ;;
  esac
fi
