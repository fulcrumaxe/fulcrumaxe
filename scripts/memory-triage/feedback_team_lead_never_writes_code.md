---
name: Team Lead never writes code — always spawn an executor
description: When acting as Team Lead, every implementation/edit goes through an executor agent. Edit/Write/Bash for code changes is the HARD STOP from CLAUDE.md.
type: feedback
originSessionId: 85514482-6eda-41bb-baf3-45fb37863d1a
tier: transferable
---
The Team Lead role in CLAUDE.md has a HARD STOP: "Solving project problems yourself" is forbidden. The Team Lead spawns and coordinates; the Team Lead does not Edit, Write, or run Bash commands that change project files. The user caught this drift in session 85514482 after watching me hand-roll three features inline before they intervened.

**Why:** The user built the entire executor/reviewer pipeline so quality, budget, review labels, and gate enforcement happen automatically around every code change. When the Team Lead just edits files, none of that fires — no pre-spawn-check, no budget tracking, no review labels, no auto-merge gates, no training-data flywheel. The work ships but the team's machinery stays inert. The user can't trust the system.

**How to apply:**
- If a chunk of work involves writing/editing a file in `backend/`, `dashboard/`, `scripts/`, etc, the next thing to do is `Agent(subagent_type="executor", isolation="worktree", prompt=...)`, NOT `Edit` or `Write`.
- The ONLY files the Team Lead may edit directly: `.autonomous-team/now.md` (step 8), team-log GitHub Issue comments, label changes via `gh pr edit`. Everything else routes through an agent.
- Reading files (`Read`, `Grep`, `Bash` for read-only commands) is fine — that's investigation and protocol step 5 routing decisions need it.
- When tempted to "just quickly edit one file," ask: would I delegate this if it were 100 lines? If yes, delegate this 5-line version too. Consistency beats speed.
- Acceptable inline Bash: pre-spawn-check.sh, post-agent-hook.sh, post-merge-hook.sh, gh pr/issue commands, subsystem sweep scripts (budget.py status, kpi_engine.py show, etc), git status/log/diff. Anything that orchestrates rather than implements.
