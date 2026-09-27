## POSTING YOUR ROUND 1 OUTPUT

Write the perspective/concerns/questions above, as one Discussion comment, to
`.discussion-outbox/comment.md` in your own worktree. Do not call `gh` or any
GitHub API yourself, and do not send it only in your final message — the
panel completeness gate reads the Discussion comment body, not your final
response to the Team Lead.

The SubagentStop hook posts that file to Discussion #{{discussion_number}}
after you stop, using the role and Discussion the Team Lead registered for
this spawn when it started — never anything the file itself says. If the
hook refuses the file (a forged identity, an oversized file), it is left in
place at `.discussion-outbox/comment.md` and the reason goes to the audit
log, not back to you.

End **both** the outbox file and your final message with the same JSON
envelope below, inside `<!-- AGENT_OUTPUT -->` markers. A comment without the
envelope, or an envelope only in your final message, does not count toward
panel completeness — the gate will treat your role as absent even though you
did the work.
