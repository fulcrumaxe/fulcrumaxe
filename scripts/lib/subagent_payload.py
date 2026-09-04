#!/usr/bin/env python3
"""scripts/lib/subagent_payload.py — resolve a Claude Code SubagentStop
payload into the fields scripts/subagent-stop-hook.sh needs to record an
agent_run row (D#2238).

Why this exists: the hook used to hunt an `<!-- AGENT_OUTPUT -->` envelope
in `transcript_path`, but that field is the PARENT session's transcript —
it contains zero subagent turns (isSidechain never true there). The real
per-subagent content — the agent's own final message, its declared role,
and its own token usage — arrives in three fields the hook never read:
`agent_id`, `agent_type`, and `last_assistant_message`.

Precedence:
  role/verdict/etc: last_assistant_message envelope -> agent_type -> unknown
  tokens:           subagent's own transcript (by agent_id) -> envelope
                     tokens_used -> zero

The PARENT transcript's usage is NEVER read into tokens once
last_assistant_message is present in the payload — that is the
misattribution D#2238 acceptance item 5 guards against (it would otherwise
book the whole parent session's cache reads against whichever subagent
happened to stop next). `transcript_path`'s usage is only ever consulted on
the legacy path below, for payloads that carry no last_assistant_message at
all (older Claude Code builds, and the existing test fixtures that predate
this field).

Token sourcing for "the subagent's own transcript" tries two locations, in
order:
  1. A `tasks/<agent_id>*` file next to (or one level above) transcript_path.
     This is the convention the D#2238 discussion thread observed on some
     host, but it does not exist anywhere on THIS host (checked empirically
     at spec-implementation time) — kept for forward/host compatibility,
     cheap to check, never the only source relied on.
  2. Claude Code's own per-subagent transcript, written unconditionally at
     `~/.claude/projects/<slug(repo_root)>/<session_id>/subagents/agent-<agent_id>.jsonl`.
     Verified present on this host for every subagent, `isSidechain: true`,
     `agentId` matching the file's own agent_id, and real non-zero
     `input_tokens`/`output_tokens`/`cache_*` on its assistant records —
     i.e. genuinely the subagent's own usage, never the parent's.

Both require a validated `agent_id`; location 2 also requires a validated
`session_id`. Neither ever raises — a missing directory, an unreadable
file, or a malformed line is treated as "not found" and the caller falls
through to the next source. `resolve()` never raises either: any input
shape it doesn't recognise degrades to the same all-defaults object the
hook already treated as "unknown", so the hook's `exit 0` contract holds.

CLI usage:
  python3 subagent_payload.py [repo_root] < payload.json
    Reads the raw SubagentStop JSON on stdin, prints one resolved JSON
    object on stdout, exits 0 always.
"""
import json
import os
import re
import sys

AGENT_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
SESSION_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
ROLE_RE = re.compile(r'^[a-z][a-z-]{0,24}$')
ENVELOPE_RE = re.compile(
    r'<!--\s*AGENT_OUTPUT\s*-->\s*```json\s*(.*?)\s*```\s*<!--\s*/AGENT_OUTPUT\s*-->',
    re.DOTALL,
)
WRITE_TOOLS = {"Edit", "Write"}
USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens")

DEFAULTS = {
    "session_id": "unknown",
    "transcript_path": "",
    "agent_id": "",
    "agent_type": "",
    "role": "unknown",
    "verdict": "unknown",
    "discussion": "",
    "pr": "",
    "files": "",
    "self_observed": False,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "cache_creation_tokens": 0,
    "first_write_turn": None,
    "parse_ok": False,
    "own_transcript_path": "",
}


def _str_or_empty(v):
    return str(v) if v not in (None, "") else ""


def _valid(value, pattern):
    return value if isinstance(value, str) and pattern.match(value) else ""


def extract_text(content):
    """Extract text from a message content field: a plain string, or a list
    of content blocks (Claude Code's real shape) — returns the last
    non-empty text block."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return texts[-1] if texts else ""
    return ""


def find_envelope(text):
    """Return the parsed dict of the LAST <!-- AGENT_OUTPUT --> envelope in
    text, or None if absent/malformed."""
    if not text:
        return None
    matches = ENVELOPE_RE.findall(text)
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1].strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _sum_usage(path):
    """Sum Claude API usage across every assistant record in a
    transcript-shaped JSONL file. Handles both the real Claude Code shape
    (`{"type": "message"/"user"/"assistant", "message": {...}}`) and the
    flat fixture shape (`{"role": ..., "usage": ...}` at top level). Returns
    None on any read error or if every value summed to zero."""
    totals = {k: 0 for k in USAGE_KEYS}
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in ("message", "user", "assistant") and isinstance(obj.get("message"), dict):
                    msg = obj["message"]
                    role = msg.get("role", "")
                    usage = msg.get("usage", {})
                else:
                    role = obj.get("role", "")
                    usage = obj.get("usage", {})
                if role != "assistant" or not isinstance(usage, dict):
                    continue
                for k in USAGE_KEYS:
                    v = usage.get(k)
                    if isinstance(v, (int, float)) and v > 0:
                        totals[k] += int(v)
    except (OSError, IOError):
        return None
    return totals if any(totals.values()) else None


def find_own_usage(transcript_path, agent_id, session_id, repo_root):
    """Locate and sum usage from the subagent's OWN transcript — never the
    parent's. Returns a usage dict (see _sum_usage) or None. `agent_id`
    empty is itself the "treat as absent" case from the path-traversal
    guard — returns None without touching disk."""
    if not agent_id:
        return None

    if transcript_path:
        for d in (os.path.dirname(transcript_path),
                  os.path.dirname(os.path.dirname(transcript_path))):
            if not d:
                continue
            tasks_dir = os.path.join(d, "tasks")
            if not os.path.isdir(tasks_dir):
                continue
            try:
                names = sorted(n for n in os.listdir(tasks_dir) if n.startswith(agent_id))
            except OSError:
                names = []
            for name in names:
                totals = _sum_usage(os.path.join(tasks_dir, name))
                if totals:
                    return totals

    if session_id:
        home = os.environ.get("HOME", "")
        if home and repo_root:
            slug = repo_root.replace("/", "-")
            candidate = os.path.join(
                home, ".claude", "projects", slug, session_id,
                "subagents", "agent-%s.jsonl" % agent_id,
            )
            if os.path.isfile(candidate):
                totals = _sum_usage(candidate)
                if totals:
                    return totals

    return None


def find_own_transcript(transcript_path, agent_id, session_id, repo_root):
    """Locate the subagent's OWN transcript file — same candidate order as
    find_own_usage, but this returns the first EXISTING candidate path
    rather than the first with non-zero usage (D#2247). Used as the source
    for hook_event_id extraction: the parent transcript (transcript_path)
    never carries the spawn tag, only the subagent's own transcript does.

    Deliberately NOT routed through find_own_usage — that function's
    selection criterion is different (non-zero usage, not existence) and is
    the confirmed-working token-extraction path; duplicating this path
    construction is cheaper than risking a behavior change there.

    Returns "" when agent_id is empty or nothing exists. Never raises."""
    if not agent_id:
        return ""

    if transcript_path:
        for d in (os.path.dirname(transcript_path),
                  os.path.dirname(os.path.dirname(transcript_path))):
            if not d:
                continue
            tasks_dir = os.path.join(d, "tasks")
            if not os.path.isdir(tasks_dir):
                continue
            try:
                names = sorted(n for n in os.listdir(tasks_dir) if n.startswith(agent_id))
            except OSError:
                names = []
            for name in names:
                candidate = os.path.join(tasks_dir, name)
                if os.path.isfile(candidate):
                    return candidate

    if session_id:
        home = os.environ.get("HOME", "")
        if home and repo_root:
            slug = repo_root.replace("/", "-")
            candidate = os.path.join(
                home, ".claude", "projects", slug, session_id,
                "subagents", "agent-%s.jsonl" % agent_id,
            )
            if os.path.isfile(candidate):
                return candidate

    return ""


def scan_transcript(transcript_path):
    """Legacy fallback: scan the given transcript for its last assistant
    message text and usage, plus the first Edit/Write turn. Used only when
    the payload carries no last_assistant_message at all (older Claude Code
    builds, pre-existing test fixtures)."""
    last_text = ""
    last_usage = {}
    first_write_turn = None
    turn = 0
    try:
        with open(transcript_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in ("message", "user", "assistant") and isinstance(obj.get("message"), dict):
                    msg = obj["message"]
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    usage = msg.get("usage", {})
                else:
                    role = obj.get("role", "")
                    content = obj.get("content", "")
                    usage = obj.get("usage", {})
                if role != "assistant":
                    continue
                turn += 1
                text = extract_text(content)
                if text:
                    last_text = text
                if isinstance(usage, dict) and usage:
                    last_usage = usage
                if first_write_turn is None and isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict) and block.get("type") == "tool_use"
                                and block.get("name") in WRITE_TOOLS):
                            first_write_turn = turn
                            break
    except (OSError, IOError):
        return "", {}, None
    return last_text, last_usage, first_write_turn


def resolve(payload, repo_root=""):
    """Resolve one SubagentStop payload into the fields the hook needs.
    Never raises; always returns a complete dict (defaults on any garbage
    input)."""
    out = dict(DEFAULTS)
    if not isinstance(payload, dict):
        return out

    out["session_id"] = _str_or_empty(payload.get("session_id")) or "unknown"
    transcript_path = payload.get("transcript_path") or ""
    out["transcript_path"] = transcript_path if isinstance(transcript_path, str) else ""
    transcript_path = out["transcript_path"]

    agent_id = _valid(payload.get("agent_id", ""), AGENT_ID_RE)
    out["agent_id"] = agent_id
    agent_type = _valid(payload.get("agent_type", ""), ROLE_RE)
    out["agent_type"] = agent_type
    session_id = _valid(out["session_id"], SESSION_ID_RE)

    out["own_transcript_path"] = find_own_transcript(transcript_path, agent_id, session_id, repo_root)

    # lam_present is presence of the KEY in the payload, not "did it parse".
    # This is the item-5 guard: a subagent whose last_assistant_message is
    # present but crashed/truncated/malformed must resolve to unknown/zero,
    # never fall through to the parent transcript for role/verdict/discussion/
    # pr/tokens. Only a payload that never carried the field at all (legacy
    # shape, pre-existing test fixtures) may use transcript_path as a source
    # of those fields.
    lam_present = "last_assistant_message" in payload
    last_assistant_message = payload.get("last_assistant_message", "")
    lam_text = last_assistant_message if isinstance(last_assistant_message, str) \
        else extract_text(last_assistant_message)

    envelope = find_envelope(lam_text) if lam_present else None
    fallback_usage = {}
    first_write_turn = None

    if not lam_present and transcript_path and os.path.isfile(transcript_path):
        # Legacy fallback only — no last_assistant_message key in the payload
        # at all. This is the ONLY branch that may ever read transcript_path's
        # usage/role/verdict/discussion/pr; item 5's guard is that this branch
        # never runs when last_assistant_message was present, parsed or not.
        text, fallback_usage, first_write_turn = scan_transcript(transcript_path)
        envelope = find_envelope(text)
    elif transcript_path and os.path.isfile(transcript_path):
        # last_assistant_message WAS present (lam_present=True), whether or
        # not it parsed: first_write_turn is still transcript-only data with
        # no misattribution risk (it isn't a token count, verdict, or role),
        # so it's fine to recover it here unconditionally.
        _, _, first_write_turn = scan_transcript(transcript_path)

    out["first_write_turn"] = first_write_turn

    if envelope is not None:
        out["parse_ok"] = True
        out["role"] = envelope.get("agent") or agent_type or "unknown"
        out["verdict"] = envelope.get("verdict") or "unknown"
        out["discussion"] = _str_or_empty(envelope.get("discussion"))
        out["pr"] = _str_or_empty(envelope.get("pr"))
        files_touched = envelope.get("files_touched")
        out["files"] = ",".join(str(f) for f in files_touched) if isinstance(files_touched, list) else ""
        out["self_observed"] = bool(envelope.get("self_observed"))
        env_tokens = envelope.get("tokens_used")
        env_tokens = env_tokens if isinstance(env_tokens, dict) else {}
    else:
        env_tokens = {}
        if agent_type:
            out["role"] = agent_type

    own_usage = find_own_usage(transcript_path, agent_id, session_id, repo_root)
    if own_usage:
        out["input_tokens"] = own_usage["input_tokens"]
        out["output_tokens"] = own_usage["output_tokens"]
        out["cache_read_tokens"] = own_usage["cache_read_input_tokens"]
        out["cache_write_tokens"] = own_usage["cache_creation_input_tokens"]
        out["cache_creation_tokens"] = own_usage["cache_creation_input_tokens"]
    elif env_tokens:
        out["input_tokens"] = int(env_tokens.get("input") or 0)
        out["output_tokens"] = int(env_tokens.get("output") or 0)
        out["cache_read_tokens"] = int(env_tokens.get("cache_read") or 0)
        out["cache_write_tokens"] = int(env_tokens.get("cache_write") or 0)
        out["cache_creation_tokens"] = int(env_tokens.get("cache_write") or 0)
    elif not lam_present and fallback_usage:
        # Legacy-only: no last_assistant_message KEY anywhere in the payload,
        # so transcript_path's own usage is the only thing on the table.
        # Never reached when last_assistant_message was present (parsed or
        # not) — see item 5.
        out["input_tokens"] = int(fallback_usage.get("input_tokens") or 0)
        out["output_tokens"] = int(fallback_usage.get("output_tokens") or 0)
        out["cache_read_tokens"] = int(fallback_usage.get("cache_read_input_tokens") or 0)
        out["cache_write_tokens"] = int(fallback_usage.get("cache_creation_input_tokens") or 0)
        out["cache_creation_tokens"] = int(fallback_usage.get("cache_creation_input_tokens") or 0)

    return out


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    repo_root = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(resolve(payload, repo_root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
