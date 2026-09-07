#!/usr/bin/env python3
"""hooks/payload_shape.py

Record what the PreToolUse hook is actually handed (D#2324).

Two reviews of the sandbox left the same question open and unanswerable from
the tree: does the payload Claude Code hands hooks/sandbox.py carry anything
that distinguishes a sub-agent's tool call from the top-level session's? Every
answer available so far was a guess about a schema nobody had written down.

So this records the schema instead of guessing at it. One row, the first time
a given key set is seen:

    {"ts": ..., "kind": "payload_shape", "decision": "observe",
     "signature": "<digest of the key set>",
     "payload_keys": ["cwd", "hook_event_name", "session_id", ...],
     "key_count": 5,
     "session_id": "<value, or null>"}

This OBSERVES. It does not prevent, and it never changes a decision: every
entry point below is wrapped so that no failure of any kind can propagate to
the caller, and nothing here calls sys.exit. hooks/sandbox.py runs on every
single tool call, so an exception raised here would degrade the guardrail for
everything.

It also does not INFER. If a payload has no `session_id`, the row says
`"session_id": null` rather than filling something in from the environment or
the cwd. The point of the exercise is to find out what is there, and a row
that quietly reconstructs a missing field answers a different question than
the one asked. Deciding whether a role-aware tier is buildable is downstream
of reading a few days of these rows, not of this file.

What gets recorded, and what deliberately does not
--------------------------------------------------
Recorded: the payload's key NAMES, a digest of them, how many there were, and
the value of `session_id`.

Not recorded: every other value. Not the command text, not the file path, not
the cwd, not the tool input. Those are the fields that carry user content and
(per PR-b's security review, CWE-532) can carry an in-URL credential; the
sibling writers in hooks/sandbox.py record command text because their rows are
about a specific command, and these rows are not about any command at all.

`session_id` is the one value that is recorded, because it is the only field
that could answer the question this exists to answer — a digest of it would
not join against anything, so hashing would preserve the privacy of a value
while destroying the entire reason for collecting it. Three things bound the
exposure: it is a Claude Code-generated session identifier rather than
user-authored content; it is written at most once per distinct key set, so a
handful of rows over the life of the log rather than one per tool call; and it
is coerced to a bounded-length string, so a payload carrying something large
or structured under that key cannot dump it into an append-only file.

The key names come from the payload too, so they are bounded the same way: a
capped count, a capped length each, and JSON-encoded on the way out (which
escapes NUL, control characters and lone surrogates rather than emitting
them).

Dedup, and why the marker is written first
-------------------------------------------
Each hook invocation is its own process, so the "have I seen this shape?"
memory has to outlive it. It is a directory of empty marker files named by the
key-set digest, created with O_CREAT|O_EXCL — atomic, so two hook processes
racing on a brand-new shape still produce exactly one row, with no lock file
and no read-modify-write.

The marker is created BEFORE the row is appended, which trades a vanishingly
rare lost row for never double-logging. The two failure modes are not
symmetric: the marker directory and the daily hook-events file live in the
same directory, so if the marker was created at all, that directory is
writable and the append that follows it will almost certainly succeed. The
audit.jsonl copy lives elsewhere and can fail on its own; that is guarded
separately so losing it does not cost the hook-events copy.

The markers live beside the rows in .autonomous-team/hook-events/ rather than
in the state dir, deliberately. The row is written to hook-events
unconditionally but to <state_dir>/audit.jsonl only when the state dir exists,
so a marker in the state dir would mean no dedup at all on a host that has not
run setup-state-dir.sh — which is precisely the case where per-invocation
logging would turn this into the noise it is designed not to be.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from datetime import date
from pathlib import Path

# Bounds on what a single row may contain. A real Claude Code payload has a
# handful of short keys; these exist so that an unexpected or malformed one
# cannot write an unbounded row into an append-only file.
_MAX_KEYS = 64
_MAX_KEY_CHARS = 200
_MAX_SESSION_ID_CHARS = 200

# Subdirectory of the hook-events dir holding one empty file per key set seen.
_MARKER_DIRNAME = "payload-shapes"


def payload_key_names(payload: object) -> list[str]:
    """`sorted(payload.keys())`, stringified and bounded.

    Returns [] for anything that is not a dict — json.loads happily returns a
    list, a string or a number, and none of those has a shape worth recording.
    """
    if not isinstance(payload, dict):
        return []
    names = sorted(str(key)[:_MAX_KEY_CHARS] for key in payload.keys())
    return names[:_MAX_KEYS]


def payload_shape_signature(key_names: list[str]) -> str:
    """A stable, filename-safe digest of a key set.

    Digested over the JSON encoding of the list rather than over a joined
    string. Any single separator character can appear inside a key name, and
    then two different key sets share a digest: NUL-joining ["a", "b"] and
    ["a\\x00b"] produces the identical byte string, so the shape that arrives
    second is silently never recorded. JSON escapes the separator inside a
    value, which removes the whole class.

    It also solves the encoding problem for free: json.loads can produce lone
    surrogates from a "\\ud800" escape, which plain utf-8 encoding refuses,
    and json.dumps escapes them back to ASCII on the way out.
    """
    encoded = json.dumps(key_names).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:32]


def build_payload_shape_row(payload: object) -> dict:
    """The telemetry row for one payload. Pure — touches no filesystem."""
    key_names = payload_key_names(payload)

    session_id = None
    true_key_count = 0
    if isinstance(payload, dict):
        true_key_count = len(payload)
        raw = payload.get("session_id")
        if raw is not None:
            session_id = str(raw)[:_MAX_SESSION_ID_CHARS]

    return {
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "kind": "payload_shape",
        # Neither "allow" nor "block" nor even "warn": this row is a
        # by-product of a decision made entirely elsewhere.
        "decision": "observe",
        "signature": payload_shape_signature(key_names),
        "payload_keys": key_names,
        # The count BEFORE the _MAX_KEYS cap, so a truncated list is visible
        # as truncated rather than reading as the whole payload.
        "key_count": true_key_count,
        "session_id": session_id,
    }


def _resolve_state_dir() -> Path | None:
    """Where <state_dir>/audit.jsonl lives, or None to write no audit copy.

    Same resolution as the sibling writers in hooks/sandbox.py, with two
    differences, both deliberate.

    It is lazy: those writers used to spell the fallback as the default
    argument of `os.environ.get(...)`, which Python evaluates eagerly, so
    `Path.home()` ran even when the variable IS set — on a path where the
    answer was never going to be used. This function never needed that
    change, since it already branched on `env is not None` first.

    And a set-but-empty variable writes nowhere rather than falling back.
    `Path("")` is the process cwd, which for anything launched from the repo
    root is the checkout itself — the same mechanism that once made a 4.4 MB
    DuckDB file stageable (see .gitignore). Falling back to the production
    state dir instead would be worse still: a test that empties the variable
    to isolate itself would land rows in the one append-only file that has no
    cleanup path.

    D#2447: `Path.home()` falls through to a passwd-database lookup when HOME
    is unset — it does not raise, contrary to what this docstring used to
    claim — silently rerouting a hand-driven run (HOME deliberately unset to
    test the unset path) to the operator's real production state dir. Read
    HOME explicitly and return None when it is unset too, matching the
    AUTONOMOUS_TEAM_STATE_DIR-unset branch's "write nowhere" behavior instead
    of falling back further.
    """
    try:
        env = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
        if env is not None:
            return Path(env) if env.strip() else None
        home = os.environ.get("HOME")
        if not home:
            return None
        return Path(home) / ".autonomous-forever-state"
    except Exception:
        return None


def _append(path: Path, line: str) -> None:
    """Append one line, swallowing every failure independently."""
    try:
        with open(path, "a") as fh:
            fh.write(line)
    except Exception:
        pass


def record_payload_shape(payload: object, telemetry_dir: Path) -> bool:
    """Append one `payload_shape` row the first time a key set is seen.

    Returns True when a row was written, False otherwise (already seen, not a
    dict, or anything at all went wrong). The return value is for tests — the
    caller in hooks/sandbox.py ignores it, because there is no outcome here
    that should change what the hook does next.

    Never raises.
    """
    try:
        key_names = payload_key_names(payload)
        if not key_names:
            return False

        signature = payload_shape_signature(key_names)
        marker_dir = Path(telemetry_dir) / _MARKER_DIRNAME
        marker = marker_dir / f"{signature}.seen"

        if marker.exists():
            return False  # cheap path: one stat on every call after the first

        marker_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False  # lost the race; the winner is writing the row
        os.close(fd)

        line = json.dumps(build_payload_shape_row(payload)) + "\n"

        _append(
            Path(telemetry_dir) / f"blocks-{date.today().isoformat()}.jsonl", line
        )

        state_dir = _resolve_state_dir()
        if state_dir is not None and state_dir.exists():
            _append(state_dir / "audit.jsonl", line)

        return True
    except Exception:
        return False
