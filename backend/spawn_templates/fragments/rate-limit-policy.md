## RATE_LIMIT_POLICY

RATE LIMIT POLICY: If `gh pr create`, `gh api`, `gh pr comment`, or any `gh` label call
fails with a 403 secondary rate limit or "API rate limit exceeded" error, do NOT retry in
a loop and do NOT use `sleep`. Instead:
  1. Write a row to `.autonomous-team/pending-prs.json` (append to array, create if missing):
       {"branch": "<branch-name>", "title": "<pr-title>", "body": "<pr-body>", "discussion": {{discussion_number}}}
  2. Return verdict=done with `pr: null` and `blocked_reason: "rate_limit"` in the
     AGENT_OUTPUT envelope.
  Team Lead will call `bash scripts/drain-pending-prs.sh` after the next merge to open
  the PR once the limit clears. Do NOT leave background tasks running.
