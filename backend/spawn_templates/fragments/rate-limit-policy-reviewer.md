## RATE_LIMIT_POLICY

RATE LIMIT POLICY: If `gh api`, `gh pr comment`, or any `gh` label call
fails with a 403 secondary rate limit or "API rate limit exceeded" error, do NOT retry in
a loop and do NOT use `sleep`. Instead:
  1. Return your current verdict immediately (pass, needs-fix, or skip) in the
     AGENT_OUTPUT envelope with `blocked_reason: "rate_limit"`.
  2. The Team Lead will re-run the label application after the limit clears.
  Do NOT leave background tasks running.
