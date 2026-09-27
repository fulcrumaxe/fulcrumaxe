#!/usr/bin/env bash
# scripts/lib/pre-code-checklist.sh — Pre-Code Checklist and VETO rule fragment.
#
# Injected into executor and code-reviewer prompts by spawn-agent.sh.
# Achieves identical behavioral effect to embedding in .claude/agents/*.md
# without requiring user-side permission grants.
#
# Functions:
#   pre_code_checklist_block     — prints executor Pre-Code Checklist section
#   code_reviewer_checklist_block — prints code-reviewer pre_code_checklist check
#
# The VETO rule ("no re-reading the same file within 3 turns") is embedded in
# working-principles.sh (principle 7) so it reaches ALL agent roles.

# pre_code_checklist_block
# Prints the Pre-Code Checklist markdown section for executor prompts.
pre_code_checklist_block() {
  cat <<'CHECKLIST'
## Pre-Code Checklist

Before your first `Write` or `Edit`, post a single comment in your output containing:
1. **BRIEF understood?** (one sentence summary of the task in your own words)
2. **Files I will touch**: (bulleted list of absolute paths)
3. **External docs reviewed**: (paste the URLs from the spec's `external_docs:` block — if none, write `none required`)
4. **Architectural assumptions**: (one or two sentences — e.g. "I will add a new file rather than modify the hub")

If any answer is unknowable from the brief alone, reply with `verdict: blocked, block_reason: "spec_incomplete"` and stop. Do NOT probe.
CHECKLIST
}

# code_reviewer_checklist_block
# Prints the pre_code_checklist check for code-reviewer prompts.
code_reviewer_checklist_block() {
  cat <<'CRCHECK'
## Pre-Code Checklist Enforcement

When reviewing executor output, check whether the executor posted the Pre-Code Checklist
before their first `Write` or `Edit` call. The checklist must appear as a comment
containing all four items: "BRIEF understood?", "Files I will touch",
"External docs reviewed", and "Architectural assumptions".

If the executor's transcript shows a `Write` or `Edit` to a project file **before** the
checklist was completed, emit `verdict: needs-fix` with:
  reason: "pre_code_checklist_missing"
  issues: ["Executor made file edits without completing the Pre-Code Checklist first.
            The checklist (BRIEF understood / Files I will touch / External docs reviewed /
            Architectural assumptions) must appear before the first Write or Edit call."]
CRCHECK
}

# Allow direct invocation
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-}"
  shift || true
  case "$cmd" in
    pre_code_checklist_block)
      pre_code_checklist_block
      ;;
    code_reviewer_checklist_block)
      code_reviewer_checklist_block
      ;;
    *)
      echo "Usage: $0 pre_code_checklist_block | code_reviewer_checklist_block" >&2
      exit 1
      ;;
  esac
fi
