---
name: a2a-kill-and-message
description: "When agents hang, the right primitive is kill-and-send-message — not pkill or wait-it-out"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c6897484-23b6-474b-9ffb-37f6ac7089d6
tier: transferable
---

When in-flight agents are stuck (preflight contention, lock waits, runaway loops), don't pkill them en masse and don't passively wait either. Both are wrong.

**The right shape:** kill the stuck process AND send the dead agent a structured message about WHY it was killed so the next iteration / the spawning Team Lead has context.

**Why:**
- `pkill -f preflight.sh` leaves the spawned agent with no signal — its parent shell returned a non-zero exit and the agent flounders.
- Letting things "play out naturally" works in degenerate cases (load drops, locks clear) but leaves no audit trail of WHY things stalled, and the same root cause recurs next session.
- User explicitly said 2026-05-14: *"none of that sounds good i would say we let the system play out naturally this is why a2a message would benfit we could kill and send message"* — i.e. the **A2A (agent-to-agent) message channel** is the missing piece.

**How to apply:**
- Don't escalate to mass pkill without an A2A broker telling the killed agent's parent: "agent X killed because Y, please re-spawn with override Z or fail-up."
- Until A2A messaging exists, log kills explicitly to `.autonomous-team/audit.jsonl` with `event_type=kill_reason` and `reason` so post-hoc analysis sees the cause.
- File a Discussion any time you'd benefit from kill-and-message — that's a feature request for the missing channel.

**Discussion to file:** consider proposing an A2A message broker as a follow-up to D#835 (live agent tail) — once we can SEE running agents, we need to SIGNAL them too.

**Update 2026-05-14**: discovered the `TaskStop` deferred tool — this IS the kill primitive for harness Agent() background tasks. Use `TaskStop(task_id=<agent-id>)` to terminate any background Agent() the harness is tracking. Confirmed working: stopped orphaned D#836 executor `a0dd5b02d6ba53d54` after it ran 29min without completion notification. Always have TaskStop loaded — and ALWAYS check for stuck background agents when the user reports "X is still running" — they may be orphaned harness tasks you've lost track of.

The "missing A2A message" is still missing for *informing* the killed agent why. TaskStop is destructive only; we can't send "you're being killed because Y" yet. But the destructive primitive alone closes 80% of the gap.
