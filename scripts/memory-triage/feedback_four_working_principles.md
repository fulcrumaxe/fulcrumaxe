---
name: Four working principles for every agent (Think / Simplicity / Surgical / Goal-driven)
description: Four rules agents must apply when coding. Surface assumptions, write minimum code, touch only what's required, verify against the real-world goal not just tests.
type: feedback
originSessionId: db7664de-2530-41cc-8214-e2c117f8188c
tier: transferable
---
Every agent doing project work (executor, code-reviewer, PM) must apply these four principles. They directly map to recurring bug categories observed in the codebase.

### 1. Think Before Coding
- State assumptions explicitly. If uncertain, ask rather than guess.
- Present multiple interpretations when ambiguity exists. Don't pick silently.
- Push back when warranted. If a simpler approach exists, say so before coding.
- Stop when confused. Name what's unclear and request clarification.

**Why:** Most bugs trace to a silent assumption. PR #484 shipped with field `ts` instead of `timestamp` because nobody questioned the field name. PR #509 shipped because `transcript_reader.py` assumed `msg` is always a dict. PR #515 had to fix an autofile that hardcoded the wrong repo ID for months.

**How to apply:** before writing any non-trivial code, state the assumptions in 1 sentence. If two valid interpretations exist, list both and pick (don't silently choose).

### 2. Simplicity First
- Write the minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked.
- No abstractions for single-use code.
- No flexibility or configurability that wasn't requested.
- No error handling for impossible scenarios.
- If 200 lines could be 50, rewrite it. If a senior engineer would call it overcomplicated, simplify.

**Why:** `append-loop-metrics.sh` shipped embedding the entire budget.status JSON inline — 30KB rows when ~500 bytes would do. `MissingVisualVerificationError` was declared but never raised. Settings page renders toggles, sliders, audit trail UI before any backend save existed.

**How to apply:** before adding a parameter, abstraction, or "for future flexibility" hook, ask: would the user accept this PR without it? If yes, cut it.

### 3. Surgical Changes
- Touch only what you must.
- Clean up only your own mess.
- Don't improve adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- Remove imports/variables/functions that YOUR changes made unused; leave pre-existing dead code alone.

**Why:** Worktree contamination (executor `git checkout` leaks into parent) happens because agents reach beyond their lane to "set up the environment" instead of minimum-touch. D#494 fixed data freshness with 559 additions across 14 files when a smaller core change would have done.

**How to apply:** every line in the diff should trace directly to the user's request. If a line is there for any other reason, justify it or remove it.

### 4. Goal-Driven Execution
- Define success criteria. Loop until verified.
- Transform imperative tasks into verifiable goals.
- For multi-step tasks, state a brief plan with explicit verification per step.

Examples:

| Instead of... | Transform to... |
|---|---|
| "Add validation" | "Write tests for invalid inputs, then make them pass" |
| "Fix the bug" | "Write a test that reproduces it, then make it pass" |
| "Refactor X" | "Ensure tests pass before AND after" |

**Why:** "Tests pass" is not "feature works." This is the principle behind D#497 (visual verification gate for dashboard) and D#508 (CLI real-world verification gate). Every fix in this session that regressed (D#480, D#486, D#487, D#493 partial) regressed because the verification stopped at `pytest exit 0` instead of running the actual binary or loading the actual page.

**How to apply:** before claiming `verdict: done`, run the artifact the user cares about — the binary, the UI page, the API call, the workflow end-to-end. Capture the output. Include in `tests_run` array with `real_world_input: true`.

---

### Aggregate test (apply to your work before envelope)

Ask in this order:

1. **Did I state my assumptions?** If you reread your work and find unstated assumptions, surface them.
2. **Is this the smallest change that meets the spec?** If yes, ship. If no, simplify.
3. **Did I touch anything beyond the immediate ask?** If yes, either remove it or justify it in the PR body.
4. **Did I verify against the real-world goal?** Not "tests pass" — "the feature works on real input."

If any answer is "no" or "I'm not sure" — STOP. Do not emit `verdict: done`.

---

Source: 2026-05-11 session, four widely-circulated principles. Adopted because they directly map to recurring bugs observed in this repo.
