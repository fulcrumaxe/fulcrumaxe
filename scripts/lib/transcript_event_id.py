#!/usr/bin/env python3
"""scripts/lib/transcript_event_id.py — recover the spawn hook_event_id from
a Claude Code subagent transcript.

Why this exists (D#1784): `hook_event_id=` is appended to the assembled spawn
prompt, but the prompt reaches the agent as a file reference — so the tag
never appears in the agent's first user message. It shows up later, inside
the `tool_result` block produced when the agent reads its own prompt file.
That payload lives under the block's `content` key, not `text`. An extractor
that only reads `text` blocks recovers 0 of 21 real transcripts; this one
also walks `tool_result` payloads and recovers >=19 of 21.

Pure function, no side effects. Two call sites share this module rather than
each carrying their own copy of the same regex-over-text-blocks logic:
  - scripts/subagent-stop-hook.sh   (Phase 2, D#1784)
  - scripts/cron/backfill-agent-runs.sh (Phase 3, D#1784, landed by D#1953 / PR #1969)

Why the match must be shape-validated: agents write about this tag in prose,
and that prose lands in their own transcripts — briefs, review comments, and
this very docstring all get read into a `tool_result` at some point. A bare
mention inside backticks matches the tag prefix on line 1, long before the
real tag appears, so "first match wins" hands back a single backtick. Those
ids are not merely useless: `complete_run` upserts on `agent_id`, so every
garbage id collides onto one row and overwrites other agents' telemetry.
The extractor therefore only accepts a match with the canonical
`<role>-<disc-or-nod>-<unix_ts>` shape that spawn-agent.sh:437 emits, and
keeps scanning past anything else — a non-canonical hit must not stop the
walk, because the contaminating mention always precedes the genuine tag.

`hook_event_init` (scripts/lib/hook-event.sh:143) echoes the same prefix on
stdout with a sha/uuid id, which is a second way a transcript picks up a tag
that was never its own. Shape validation rejects those too.

CLI usage:
  python3 transcript_event_id.py <transcript.jsonl>
    Prints the canonical event id (e.g. "executor-1784-1785301265") and
    exits 0 if found. Prints nothing and exits 0 if no canonical tag is
    present in the transcript. Does not raise on bad input — malformed
    lines, unexpected block shapes, and unreadable files are all treated as
    "no tag found", and a bad line never aborts the scan of the rest of the
    transcript.
"""
from __future__ import annotations

import json
import re
import sys

# The literal is split so that this module's own source never contains the
# tag prefix immediately followed by a canonical-looking id — otherwise every
# agent that reads this file would adopt the example id as its own.
_TAG = "hook_event_" "id="

# Canonical id shape, exactly as scripts/spawn-agent.sh:437 builds it:
#   "${ROLE}-${DISCUSSION:-nod}-$(date +%s)"
# Role is lowercase alpha segments (matching the sanity check downstream at
# subagent-stop-hook.sh:383), the discussion is digits or the literal "nod",
# and the timestamp is unix seconds. Keeping the role alpha-only makes the
# three-part split unambiguous, the same way the downstream role parser
# breaks at the first numeric-or-"nod" segment.
_ROLE = r"[a-z]+(?:-[a-z]+)*"
_DISCUSSION = r"(?:[0-9]+|nod)"
_UNIX_TS = r"[0-9]{9,12}"

# The trailing lookahead stops a longer token from being silently truncated
# into a valid-looking id, while still allowing the tag to be wrapped in
# backticks, quotes, or punctuation.
_PATTERN = re.compile(
    _TAG + "(" + _ROLE + "-" + _DISCUSSION + "-" + _UNIX_TS + r")(?![\w-])"
)

# Belt-and-braces bound: the role portion is unbounded in the grammar above,
# so cap the accepted id length rather than trusting agent-controlled text.
_MAX_EVENT_ID_LEN = 64


def _message_role(obj: dict) -> str:
    """Return the role of a transcript line, handling both transcript shapes.

    Shape A (real Claude Code): {"type": "user", "message": {"role": "user", ...}}
    Shape B (flat/legacy):      {"role": "user", "content": ...}
    """
    role = obj.get("role", "")
    if not role and isinstance(obj.get("message"), dict):
        role = obj["message"].get("role", "")
    return role


def _message_content(obj: dict):
    content = obj.get("content", "")
    if not content and isinstance(obj.get("message"), dict):
        content = obj["message"].get("content", "")
    return content


def _candidate_text(content) -> str:
    """Concatenate every text-bearing chunk out of one message's content.

    Covers three block shapes seen in real transcripts:
      - a block with a "text" key (today's behaviour)
      - a block whose "content" is a plain str (tool_result, str payload)
      - a block whose "content" is a list of {"type": "text", "text": ...}
        blocks (tool_result, list-of-blocks payload)
    Falls back to str(content) for a bare string/other scalar content field.

    Every chunk is isinstance-checked before it goes into the join: a block
    whose "text" is a dict or a list (not seen in the corpus today, but
    nothing guarantees it) would otherwise raise TypeError out of str.join
    and abort the scan of the whole transcript, silently suppressing a
    genuine tag further down the file.
    """
    if isinstance(content, list):
        chunks = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
            inner = block.get("content")  # tool_result payload lives here
            if isinstance(inner, str):
                chunks.append(inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if not isinstance(sub, dict):
                        continue
                    sub_text = sub.get("text")
                    if isinstance(sub_text, str) and sub_text:
                        chunks.append(sub_text)
        return "\n".join(chunks)
    return str(content)


def _scan_line(line: str) -> str:
    """Return the first canonical id in one transcript line, or "".

    Every match in the line is considered, not just the first: a prose
    mention and the genuine tag routinely share a message.
    """
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    if _message_role(obj) not in ("user", "system"):
        return ""
    text = _candidate_text(_message_content(obj))
    for match in _PATTERN.finditer(text):
        candidate = match.group(1)
        if len(candidate) <= _MAX_EVENT_ID_LEN:
            return candidate
    return ""


def extract_event_id(transcript_path: str) -> str:
    """Return the first canonical hook event id in transcript order, or "".

    Only user/system messages are searched — the spawn prompt and the
    tool_result of the agent reading it are both user-role turns.

    "First" means the first match that has the canonical
    `<role>-<disc-or-nod>-<unix_ts>` shape. Non-canonical matches are
    skipped and the walk continues rather than returning; a prose mention of
    the tag reliably precedes the real one, so bailing out on the first
    non-canonical hit would keep exactly the bug this validation exists to
    fix. Once a canonical id is found the walk stops, so a later re-read of
    the prompt file cannot substitute a different id.
    """
    try:
        with open(transcript_path, "r", errors="replace") as fh:
            for line in fh:
                # A single unexpected block shape must not end the scan —
                # the genuine tag may still be further down the file.
                try:
                    event_id = _scan_line(line)
                except Exception:  # noqa: BLE001 - documented never-raises contract
                    continue
                if event_id:
                    return event_id
    except OSError:
        return ""
    return ""


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: transcript_event_id.py <transcript.jsonl>", file=sys.stderr)
        return 2
    event_id = extract_event_id(argv[1])
    if event_id:
        print(event_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
