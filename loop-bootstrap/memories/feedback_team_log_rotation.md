---
name: Rotate team-log at GitHub comment ceiling
description: When the team-log Issue hits GitHub's 2500-comment cap, archive it and open a fresh one — don't just stop logging
type: feedback
originSessionId: a267c7bf-7678-4f93-a4d3-5490a697ebbc
tier: transferable
---
When the team-log GitHub Issue (label: `team-log`) hits GitHub's 2500-comment cap, `addComment` returns "Commenting is disabled on issues with more than 2500 comments". The protocol is:

1. Close the full team-log Issue with a final comment linking to the new one (if room) or just close it.
2. Open a NEW Issue with the `team-log` label and a title like `Team Activity Log (continued from #N)`.
3. The hook scripts that post to team-log must auto-detect the cap (HTTP error or comment count ≥ 2500) and trigger rotation, not silently drop the log line.
4. Old logs stay accessible via the closed Issue — never delete or `git rm` them.

**Why:** Observability is critical. Hitting the cap silently breaks the entire log-to-team-log pattern in CLAUDE.md and every hook script. Discovered 2026-05-09 when `gh issue comment` failed with the disabled-commenting error during a refocus iteration.

**How to apply:** Any hook or script that posts to team-log (post-agent-hook.sh, post-merge-hook.sh, manual `gh issue comment $LOG`) must wrap the call so a 2500-cap failure triggers archive-and-rotate. The "current" team-log Issue is whichever open Issue carries the `team-log` label most recently — `gh issue list --label team-log --state open` returns it.
