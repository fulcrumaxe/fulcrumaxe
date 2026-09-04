"""
discussion_status.py — Shared helper for parsing STATUS from Discussion bodies.

STATUS lines look like:
  <!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->
  <!-- STATUS:IMPLEMENTING SINCE:2026-05-09T01:00:00Z -->
  <!-- STATUS:REVIEWING PR:#321 SINCE:2026-05-09T02:00:00Z -->
  <!-- STATUS:DONE PR:#321 SINCE:2026-05-09T03:00:00Z -->
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Optional

_STATUS_PATTERN = re.compile(r"<!--\s*STATUS:(\w+)")
_PR_PATTERN = re.compile(r"<!--\s*STATUS:[^>]*PR:#(\d+)")
_SINCE_PATTERN = re.compile(r"<!--\s*STATUS:[^>]*SINCE:([^\s>]+)")
# BLOCKED-BY is read only from inside the STATUS comment, and only from the
# authoritative (first non-empty) line — see extract_blocked_by. The lazy
# [^>]*? makes token order irrelevant: BLOCKED-BY may sit before or after
# SINCE:/PR: and parse identically.
#
# Presence and value are matched SEPARATELY, by two regexes. One combined
# regex — the first shape of this code — made "no BLOCKED-BY field" and "a
# BLOCKED-BY field whose value did not match" the same answer, the empty list,
# so every one of these silently read as NOT blocked:
#
#     BLOCKED-BY: #1750          (one space after the colon)
#     BLOCKED-BY:#1691, D#1746   (one space after the comma — D#1746 dropped)
#     BLOCKED-BY:                (empty value)
#     blocked-by:#1750           (lowercase)
#
# The second is the dangerous one: the old [^\s>]+ stopped at the space, so
# only #1691 was ever resolved and the gate opened the moment it merged. A
# constraint that a stray space turns off is the exact defect D#1755 is about,
# so the field must be able to fail, but never to disappear.
_BLOCKED_BY_FIELD = re.compile(r"<!--\s*STATUS:[^>]*?\bBLOCKED-BY:", re.IGNORECASE)

# The value runs to the next known STATUS-line token, or to the end of the
# comment. Only the known tokens terminate it: an unrecognised one lands inside
# the captured value and surfaces as a malformed ref, which blocks. That is
# deliberate — a token we do not understand means we do not understand the
# line, and not understanding it must not read as "clear to spawn".
_BLOCKED_BY_PATTERN = re.compile(
    r"<!--\s*STATUS:[^>]*?\bBLOCKED-BY:(.*?)(?=\s+(?:PR|SINCE|BLOCKED-BY):|\s*-->)",
    re.IGNORECASE,
)

# Stands in for the ref list when the field is present but nothing usable came
# out of it. It never parses as a ref, so backend/blocked_by.py reports it and
# blocks — the constraint fails loudly instead of vanishing.
BLOCKED_BY_UNPARSEABLE = "<empty or unreadable BLOCKED-BY value>"

VALID_STATUSES = {
    "DISCUSSING",
    "SPEC_READY",
    "IMPLEMENTING",
    "REVIEWING",
    "DONE",
    "CLOSED",
}

# The three required section headers (in the three-section spec template).
REQUIRED_SECTIONS = ["Intent", "Spec (Acceptance)", "Implementation Notes"]

# Map from header display name to the dict key returned by get_sections().
_SECTION_KEYS = {
    "Intent": "intent",
    "Spec (Acceptance)": "spec",
    "Implementation Notes": "implementation_notes",
}

# Regex that matches any of the three required ## headers at line start.
_SECTION_HEADER_RE = re.compile(
    r"^##\s+(Intent|Spec \(Acceptance\)|Implementation Notes)",
    re.MULTILINE,
)

# Strip the leading STATUS comment from legacy bodies.
_STATUS_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def extract_status(body: str) -> str:
    """Return the STATUS value from a Discussion body, or 'UNKNOWN'."""
    m = _STATUS_PATTERN.search(body or "")
    return m.group(1) if m else "UNKNOWN"


def _authoritative_line(body: str) -> str:
    """Return the first non-empty line of *body* — the authoritative STATUS line.

    By convention ``set_status()`` always writes the marker there, so anything
    after it is prose and must never be read as status.
    """
    for line in (body or "").splitlines():
        if line.strip():
            return line
    return ""


def extract_status_anchored(body: str) -> str:
    """Return the STATUS value read from the first non-empty line only.

    ``extract_status`` searches the whole body, so any later occurrence of a
    ``<!-- STATUS:... -->`` marker (inside a fenced code block, a quoted
    rejection note, etc.) is indistinguishable from the authoritative one on
    line 1. This anchors the read to the first non-empty line before handing
    off to the unchanged ``extract_status`` parser — see D#1798.
    """
    line = _authoritative_line(body)
    return extract_status(line) if line else "UNKNOWN"


def _blocked_by_raw(line: str) -> Optional[str]:
    """Return the raw ``BLOCKED-BY:`` value from *line*, or None when absent.

    The two answers are distinct on purpose: ``None`` means the field is not
    there, ``""`` means it is there and empty. Callers that collapse those two
    into one answer are the fail-open this exists to prevent.

    The value is returned verbatim (stripped only of surrounding whitespace) so
    ``set_status`` can carry it across a marker rewrite without reinterpreting
    a value it may not be able to parse.
    """
    if not _BLOCKED_BY_FIELD.search(line):
        return None
    m = _BLOCKED_BY_PATTERN.search(line)
    return m.group(1).strip() if m else ""


def extract_blocked_by(body: str) -> list[str]:
    """Return the ordered ``BLOCKED-BY:`` ref list from the STATUS comment.

        <!-- STATUS:SPEC_READY BLOCKED-BY:#1691,D#1746 SINCE:X -->  ->  ["#1691", "D#1746"]

    A ref is ``#<pr>`` or ``D#<discussion>``. Refs are returned verbatim and in
    source order; validating and resolving them is ``backend/blocked_by.py``'s
    job, because an unparseable ref must block rather than vanish here.

    Anchored to the authoritative line for the same reason ``extract_status``
    is (D#1798, D#1755): a ``BLOCKED-BY:`` quoted in prose or inside a fenced
    code block is documentation, not a constraint, and returns ``[]``.

    ``[]`` means "no field, nothing to check" and nothing else. A field that is
    present but yields no usable ref returns ``[BLOCKED_BY_UNPARSEABLE]``, which
    blocks — see the note on the two regexes above.
    """
    raw = _blocked_by_raw(_authoritative_line(body))
    if raw is None:
        return []
    refs = [ref for ref in (r.strip() for r in raw.split(",")) if ref]
    return refs or [BLOCKED_BY_UNPARSEABLE]


def is_spec_ready(body: str) -> bool:
    """True iff the authoritative (first-line) STATUS marker is SPEC_READY.

    This answers "is the Spec written", and only that. It is deliberately NOT
    the spawn predicate: whether an executor may start also depends on the
    ``BLOCKED-BY:`` refs, and resolving those needs network I/O that every
    caller of this function — including reporting-only ones — would otherwise
    pay for. The combined question lives in
    ``backend.blocked_by.partition_spec_ready``, and every gating reader goes
    through it: ``scripts/lib/spec-ready-gate.sh``, both selector paths in
    ``scripts/loop-phased-step5.sh``, ``scripts/auto-plan.sh``,
    ``scripts/start-the-day.sh``.

    Since D#1755 this has exactly one production caller —
    ``partition_spec_ready`` itself. Adding a second one that treats it as the
    spawn gate is how this repo grows another "two implementations of one
    predicate, disagreeing" bug; if you want the spawn answer, call
    ``partition_spec_ready``.
    """
    return extract_status_anchored(body) == "SPEC_READY"


def extract_linked_pr(body: str) -> Optional[int]:
    """Return the linked PR number from the STATUS line, or None."""
    m = _PR_PATTERN.search(body or "")
    return int(m.group(1)) if m else None


def extract_since(body: str) -> Optional[str]:
    """Return the SINCE timestamp from the STATUS line, or None."""
    m = _SINCE_PATTERN.search(body or "")
    return m.group(1) if m else None


def set_status(body: str, new_status: str, now_iso: str | None = None) -> str:
    """Return *body* with the STATUS marker set to *new_status*.

    - If a ``<!-- STATUS:... -->`` marker already exists it is replaced in-place.
    - If no marker exists the new marker is prepended to the body (two newlines
      after it), so the rest of the text is unchanged.

    ``BLOCKED-BY:`` is carried across the rewrite; ``PR:`` is not — see below.

    *now_iso* defaults to the current UTC instant formatted as ISO 8601
    (``YYYY-MM-DDTHH:MM:SSZ``).  Pass a fixed string in tests.
    """
    if now_iso is None:
        import datetime
        now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Match the full <!-- STATUS:... --> comment block (any content between <!-- and -->
    # that starts with STATUS:).  This covers:
    #   <!-- STATUS:X SINCE:ts -->
    #   <!-- STATUS:X PR:#N SINCE:ts -->
    #   <!-- STATUS:X --> (bare, no SINCE)
    _FULL_MARKER_RE = re.compile(r"<!--\s*STATUS:[^>]*-->")

    existing = _FULL_MARKER_RE.search(body or "")

    # Carry BLOCKED-BY across the rewrite. This function is the repo's only
    # programmatic status writer, and .claude/agents/project-manager.md now
    # tells PMs to hand-write BLOCKED-BY — so rebuilding the marker from
    # status+SINCE alone made a single automated status flip silently erase a
    # sequencing constraint. That is a marker that looks load-bearing and is
    # not, which is the defect D#1755 exists to remove.
    #
    # The value is re-emitted verbatim, not re-serialised from parsed refs: a
    # value this module cannot parse must survive so it keeps failing closed,
    # rather than being quietly normalised away. Presence alone is preserved
    # too (an empty BLOCKED-BY: round-trips), for the same reason.
    #
    # PR: is deliberately NOT carried. That loss predates BLOCKED-BY and is a
    # separate question — a PR ref surviving onto a new status is arguably its
    # own bug — so it is pinned by a test rather than changed here.
    raw_blocked = _blocked_by_raw(existing.group(0)) if existing else None
    blocked_token = "" if raw_blocked is None else f" BLOCKED-BY:{raw_blocked}"

    new_marker = f"<!-- STATUS:{new_status}{blocked_token} SINCE:{now_iso} -->"

    if existing:
        # Replace the existing marker.
        return _FULL_MARKER_RE.sub(new_marker, body, count=1)
    else:
        # No marker present — prepend it.
        return new_marker + "\n\n" + (body or "")


def set_status_anchored(body: str, new_status: str, now_iso: str | None = None) -> str:
    """Like ``set_status()``, but refuses to write unless the marker is
    anchored to the first non-empty line.

    ``set_status()`` finds and replaces the first ``<!-- STATUS:... -->``
    occurrence *anywhere* in the body — safe only because, by convention, that
    first occurrence is always the one on line 1. This function checks the
    convention holds before delegating, instead of trusting it silently: if
    the first non-empty line carries no marker (the marker is missing, or it
    sits further down the body), it raises rather than guessing which
    occurrence — if any — is authoritative.

    This is the precondition the new ``set-status --stdin`` CLI subcommand
    needs and the raw ``sed`` it replaces never had (D#2021): the CLI is the
    only unattended writer of Discussion bodies, so it must fail closed
    instead of rewriting a marker it can't confirm is the real one.

    Raises ``ValueError`` when unanchored. Never called by any other
    production caller — ``set_status()``'s existing whole-body first-match
    search is unchanged for its own callers.
    """
    if extract_status_anchored(body) == "UNKNOWN":
        raise ValueError(
            "first non-empty line carries no STATUS marker — refusing to "
            "write (an unanchored write would have to guess which "
            "occurrence, if any, is authoritative)"
        )
    return set_status(body, new_status, now_iso=now_iso)


def get_sections(body: str) -> dict[str, str]:
    """Parse the three-section spec template from a Discussion body.

    Returns a dict with keys ``intent``, ``spec``, ``implementation_notes``.

    If the body uses the new three-section format (all headers present), each
    value contains the text between that header and the next ``##`` header (or
    EOF).

    Back-compat: if none of the three section headers are found (legacy body),
    returns ``{"intent": "", "spec": <full body without status comment>,
    "implementation_notes": ""}``.  Callers that only use ``spec`` continue to
    work without modification.

    Partial format (some but not all headers present): headers that exist are
    parsed normally; missing ones return empty strings.
    """
    body = body or ""
    matches = list(_SECTION_HEADER_RE.finditer(body))

    # Legacy body — no section headers at all.
    if not matches:
        stripped = _STATUS_COMMENT_RE.sub("", body).strip()
        return {"intent": "", "spec": stripped, "implementation_notes": ""}

    result: dict[str, str] = {"intent": "", "spec": "", "implementation_notes": ""}

    for i, m in enumerate(matches):
        header_name = m.group(1)
        key = _SECTION_KEYS.get(header_name)
        if key is None:
            continue
        content_start = m.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        result[key] = body[content_start:content_end].strip()

    return result


def missing_sections(body: str) -> list[str]:
    """Return the names of required section headers absent from *body*.

    Uses the display names from REQUIRED_SECTIONS:
    ``["Intent", "Spec (Acceptance)", "Implementation Notes"]``.

    Returns an empty list when all three headers are present.
    """
    body = body or ""
    found = {m.group(1) for m in _SECTION_HEADER_RE.finditer(body)}
    return [s for s in REQUIRED_SECTIONS if s not in found]


def _fetch_body(discussion_num: int) -> str:
    """Fetch a Discussion body via discussion_cache.py."""
    import os
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cache_script = os.path.join(script_dir, "discussion_cache.py")
    result = subprocess.run(
        [sys.executable, cache_script, "get-body", str(discussion_num)],
        capture_output=True,
        text=True,
    )
    return result.stdout


if __name__ == "__main__":
    import json

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <get-sections|missing-sections|extract-status|set-status> ...", file=sys.stderr)
        sys.exit(1)

    subcommand = sys.argv[1]

    # extract-status reads the body from stdin (not a discussion number) so callers
    # that already fetched a live/--fresh body can feed it straight in, instead of
    # going through _fetch_body()'s cache path (which does not use --fresh, see
    # discussion_cache.py get-body — D#1778).
    #
    # --anchored switches to extract_status_anchored(): the whole body is read
    # and this module does the "first non-empty line" split itself, rather than
    # a caller pre-selecting a line before handing it off. scripts/lib/
    # spec-ready-gate.sh used to do that split with `grep -m1 -E '\S'`, which
    # breaks on '\n' only — Python's str.splitlines() (used inside
    # extract_status_anchored) also breaks on \x0b, \x0c, \x1c-\x1e, \x85,
    # U+2028 and U+2029, so the two disagreed on any body whose real first line
    # was preceded only by one of those eight characters (D#1941). Giving the
    # shell gate this mode instead of its own grep collapses the two
    # definitions of "first line" back to one.
    if subcommand == "extract-status":
        if len(sys.argv) < 3 or sys.argv[2] != "--stdin":
            print(f"Usage: {sys.argv[0]} extract-status --stdin [--anchored]", file=sys.stderr)
            sys.exit(1)
        anchored = len(sys.argv) > 3 and sys.argv[3] == "--anchored"
        stdin_body = sys.stdin.read()
        print(extract_status_anchored(stdin_body) if anchored else extract_status(stdin_body))
        sys.exit(0)

    # set-status --stdin <VALUE> — the CLI entry point set_status() never had
    # (D#2021). Reads a body on stdin, writes the rewritten body to stdout,
    # touching exactly the one anchored marker on line 1. This is what
    # scripts/post-merge-hook.sh now calls instead of its old unanchored,
    # global `sed` substitution.
    if subcommand == "set-status":
        if len(sys.argv) < 4 or sys.argv[2] != "--stdin":
            print(f"Usage: {sys.argv[0]} set-status --stdin <VALUE>", file=sys.stderr)
            sys.exit(1)
        new_value = sys.argv[3]
        stdin_body = sys.stdin.read()
        try:
            updated_body = set_status_anchored(stdin_body, new_value)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.stdout.write(updated_body)
        sys.exit(0)

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <get-sections|missing-sections> <discussion_number>", file=sys.stderr)
        sys.exit(1)

    discussion_num = int(sys.argv[2])
    body = _fetch_body(discussion_num)

    if subcommand == "get-sections":
        print(json.dumps(get_sections(body)))
    elif subcommand == "missing-sections":
        missing = missing_sections(body)
        if missing:
            names = ", ".join(missing)
            print(f"WARN: discussion #{discussion_num} body missing section: {names}", file=sys.stderr)
        # stdout: JSON list
        print(json.dumps(missing))
    else:
        print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
        sys.exit(1)
