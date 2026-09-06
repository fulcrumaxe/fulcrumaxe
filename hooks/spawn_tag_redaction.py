#!/usr/bin/env python3
"""hooks/spawn_tag_redaction.py — make spawn tags non-extractable before the
sandbox hook writes them to its block log (D#1959).

The problem
-----------
.autonomous-team/hook-events/blocks-<date>.jsonl records what an agent tried to
do, verbatim: the command, the tool input, an attempted Agent() prompt. Agent
prompts carry the "hook_event_id=<role>-<disc>-<unix_ts>" spawn tag that
scripts/lib/transcript_event_id.py exists to recover, so the block log
accumulates real, contiguous, canonical spawn ids — and any agent that later
reads that log adopts one as its own. ``complete_run()`` upserts on agent_id, so
the borrowed id lands on an EXISTING agent_run row with someone else's end_ts and
token counts.

The tracked-source guard (scripts/ci/no-planted-spawn-ids-guard.py) cannot help
here and deliberately does not try: the log is untracked, rotates daily, and
regenerates, so no source edit clears it and no index-reading sweep sees it. The
only place the problem can be fixed is where the log is written.

Truncation is not redaction. The writers already cap these fields at 300-500
characters; that shortens a prompt without touching an id sitting in the first
line of it.

What "redacted" means here
--------------------------
The tag prefix survives, the id does not. A line that read `<tag>` immediately
followed by `executor-1-1785301265` comes back as `<tag>` immediately followed by
`[redacted-spawn-id]`, where `<tag>` is the prefix spelled out in TAG below.
(Spelling it that way, rather than showing the before-and-after literally, is the
same discipline this module enforces: a contiguous example here would contaminate
every agent that reads it.) A reader still sees that a spawn tag
was present, which is diagnostically useful, while every extractor keyed on the
tag walks past it — both scripts/lib/transcript_event_id.py's canonical pattern
and the tracked-source guard's require an identifier character immediately after
the tag, and "[" is not one.

Nothing else in the text is touched. The blocked command, the cwd and the agent
identity are exactly as they were; a redaction that destroyed the audit value
would be a worse outcome than the leak it fixed.

Why this pattern is wider than the extractor's
----------------------------------------------
scripts/lib/transcript_event_id.py accepts a lowercase role, a numeric-or-"nod"
discussion, and a 9-12 digit timestamp. The source guard accepts a slightly
different set (mixed case, also "None", 9-11 digits). This module accepts a
superset of both and does not import either.

That is deliberate, and it is not the "same constant in two places" failure. A
detector and a scrubber fail in opposite directions: a detector that over-matches
raises false alarms on legitimate text, so it is tuned narrow; a scrubber that
under-matches leaks the thing it was written to remove, so it is tuned wide.
Sharing one pattern would force one of them to take the other's failure mode.
What is pinned instead is the property that matters — tests/test_hook_block_log_
redaction.py runs the real extractor over a real redacted log line and asserts it
recovers nothing, so this module is checked against the consumer's answer rather
than against a restatement of its own regex.

This module is also why it stays dependency-free: it runs inside a PreToolUse
hook on every tool call, and hooks/sandbox.py already swallows telemetry errors
to keep a logging failure from changing a sandbox decision. A cross-tree import
into scripts/lib/ would be one more thing that can fail on that path for no gain.
"""
from __future__ import annotations

import re

# Assembled from two adjacent fragments so this module's own source never
# carries the tag prefix immediately followed by a canonical-looking id.
TAG = "hook_event_" + "id="

# Superset of every canonical grammar in play (see the module docstring):
#   role  — any identifier-ish run, either case
#   disc  — digits, "nod", or "None"
#   ts    — nine or more digits, unbounded above
_ID = r"[A-Za-z][A-Za-z0-9_-]*-(?:[0-9]+|nod|None)-[0-9]{9,}"

_TAGGED_ID = re.compile(re.escape(TAG) + _ID)

REDACTED = TAG + "[redacted-spawn-id]"


def redact_spawn_tags(text: str) -> str:
    """Return *text* with every canonical spawn tag rendered non-extractable.

    Pure and total: a non-str argument is coerced, and text with no tag comes
    back byte-identical. Never raises — it sits on the hook's logging path,
    where an exception would be a sandbox decision changed by a telemetry
    detail.
    """
    if not isinstance(text, str):
        text = str(text)
    return _TAGGED_ID.sub(REDACTED, text)
