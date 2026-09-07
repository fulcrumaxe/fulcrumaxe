"""backend/status_migration.py — D#2436 PR-b: migrate the ~40 open Discussions
that declare the invented ``NEW`` status back onto the vocabulary the state
machine actually reads.

Background
----------
40 of 127 open Discussions declared ``STATUS:NEW`` in one of (at least) five
shapes, measured against the live backlog:

  D — well-formed ``<!-- STATUS:NEW -->`` on line 1 (24 live).
  A — bare ``STATUS: NEW`` on line 1, with a space (9 live).
  A'— bare ``STATUS:NEW`` on line 1, no space (1 live).
  B — prose on line 1, bare ``STATUS: NEW`` around line ~90 (4 live).
  C — prose on line 1, ``<!-- STATUS:NEW -->`` on line 2 (2 live).

``NEW`` is not, and does not become, a member of ``VALID_STATUSES`` — see
D#2436's Implementation Notes: three coded filers already write the
discussing marker, none write ``NEW``, so converging existing bodies onto the
vocabulary costs three template lines and a bounded migration, while adding
``NEW`` as a real state would mean defining its exit transition, its stall
semantics, and touching four dispatchers. This module only ever writes
``MIGRATION_TARGET_STATUS``.

Why ``discussion_status.set_status()`` alone is not safe here (D#2436 Spec
item 10, measured against the real bodies):

  - Shapes A / A' / B: it has no marker to find (the bare line isn't
    ``<!-- STATUS:...-->``), so it *prepends* a fresh one — leaving the
    stale bare line behind at its original position. The Discussion now
    reads correctly through ``extract_status_anchored`` but still carries
    dead ``STATUS:`` text the reader never looks at.
  - Shape C: it finds the line-2 marker (its whole-body first-match search
    isn't anchored) and replaces *that* in place. ``extract_status_anchored``
    still reads line 1 — prose — and still returns ``UNKNOWN``. The write
    reports success on a body that is not fixed.

``migrate_body()`` below is the one writer this migration uses. It strips
every stray ``STATUS:`` occurrence (bare or misplaced HTML) outside fenced
code blocks before handing a now-markerless body to ``set_status()`` for
shapes A/A'/B/C, and uses ``set_status_anchored()`` directly for shape D
(single line replaced in place, nothing else in the body touched).

Every function here is pure and offline — no network, no ``GH_TOKEN``. The
live GitHub read/write/verify loop lives in
``scripts/migrate-status-new-discussions.py``, which imports this module.
"""
from __future__ import annotations

import re

from backend.discussion_status import (
    VALID_STATUSES,
    extract_status_anchored,
    set_status,
    set_status_anchored,
)

# The value every declared-NEW body converges onto. Not hardcoded as a bare
# string at call sites — asserted a member of VALID_STATUSES at import time
# so this module cannot itself drift from the vocabulary it targets.
MIGRATION_TARGET_STATUS = "DISCUSSING"
assert MIGRATION_TARGET_STATUS in VALID_STATUSES, (
    "MIGRATION_TARGET_STATUS must stay inside the existing vocabulary — "
    "D#2436 converges onto VALID_STATUSES, it does not extend it"
)

# Bounds a single migration run (D#2436 Spec item 11). Defined in exactly one
# place. `scripts/sweep-stalled-discussions.sh` has no batch cap of its own —
# its threshold is 24h since the marker's SINCE: — so migrating N bodies in
# one pass makes all N sweep-eligible simultaneously 24h later, each
# enqueuing a project-manager spawn. Not negotiable down per the Spec.
MIGRATION_BATCH_CAP = 10

# ---------------------------------------------------------------------------
# Fence-aware line classification
# ---------------------------------------------------------------------------

_FENCE_DELIM_RE = re.compile(r"^\s*```")


def _fenced_mask(body: str) -> list[bool]:
    """One bool per line of *body*: True if that line is a ``` delimiter or
    sits inside a fenced code block.

    D#2436's own body is the positive control for why this matters: it
    carries the literal ``STATUS:[A-Za-z_-]+`` regex example inside a
    fenced code block. A fence-blind scan over the raw body text flags that
    example as a stray STATUS line; this mask is what keeps it excluded.
    """
    lines = (body or "").split("\n")
    mask = [False] * len(lines)
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_DELIM_RE.match(line):
            mask[i] = True
            in_fence = not in_fence
            continue
        mask[i] = in_fence
    return mask


# A bare (non HTML-comment) STATUS line — shapes A, A', B.
_BARE_STATUS_LINE_RE = re.compile(r"^\s*STATUS\s*:")

# A full HTML STATUS marker, matched wherever it sits in the body.
_FULL_MARKER_RE = re.compile(r"<!--\s*STATUS:[^>]*-->")

# Extracts the value from a bare STATUS: line (shapes A, A', B).
_BARE_STATUS_VALUE_RE = re.compile(r"^\s*STATUS\s*:\s*(\w+)")

# Extracts the value from a well-formed HTML marker (shapes C, D).
_MARKER_VALUE_RE = re.compile(r"<!--\s*STATUS:(\w+)")


def _line_declares_new(line: str) -> bool:
    """True iff *line*, taken as a whole, IS a STATUS marker occurrence
    (bare or HTML-comment, filling the line) whose value is NEW.

    Structural, not a substring search — deliberately narrower than "the
    text STATUS:NEW appears somewhere in this line". D#2436's own body is
    the reason: it discusses the shape D marker inline —
    `` well-formed `<!-- STATUS:NEW -->` on line 1 `` — inside a single-
    backtick code span, not a line of its own and not a fenced code block
    either, so fence-awareness alone doesn't exclude it. Requiring the
    marker to BE the line (module the bare form's own trailing content, and
    fullmatch for the HTML form) is what keeps prose that merely quotes the
    string from being read as a real declaration.
    """
    bare = _BARE_STATUS_VALUE_RE.match(line)
    if bare:
        return bare.group(1) == "NEW"
    marker = _MARKER_VALUE_RE.match(line.strip())
    if marker and _FULL_MARKER_RE.fullmatch(line.strip()):
        return marker.group(1) == "NEW"
    return False


def find_stray_status_lines(body: str) -> list[int]:
    """0-based line indices, outside fenced code blocks, of any BARE
    ``STATUS:`` line (not a well-formed ``<!-- STATUS:...-->`` marker).

    This is the fence-aware residue check from D#2436 Spec item 13: after a
    migration write, none of these should remain. Fence-awareness is what
    keeps this from flagging D#2436's own body (see ``_fenced_mask``).
    """
    lines = (body or "").split("\n")
    mask = _fenced_mask(body)
    return [i for i, line in enumerate(lines) if not mask[i] and _BARE_STATUS_LINE_RE.match(line)]


# Public alias — "residue check" is what the Spec calls this same scan when
# applied to a post-migration body.
find_residue_lines = find_stray_status_lines


def needs_migration(body: str) -> bool:
    """True iff *body* declares the invented NEW value anywhere outside a
    fenced code block — bare or HTML-comment form, on the authoritative line
    or buried later in the body (shapes A, A', B, C, D from D#2436).

    A body with no STATUS marker at all returns False: it already fails open
    to DISCUSSING through the registry and is out of this migration's scope.
    A body with a well-formed marker for any *other* recognized status
    (IMPLEMENTING, DONE, ...) also returns False — this migration only ever
    touches bodies actually declaring NEW. A body that merely *mentions* the
    string (inline code documenting the shape, as D#2436's own body does)
    also returns False — see ``_line_declares_new``.
    """
    lines = (body or "").split("\n")
    mask = _fenced_mask(body)
    return any(not mask[i] and _line_declares_new(line) for i, line in enumerate(lines))


def migrate_body(body: str, now_iso: str | None = None) -> str:
    """Return *body* rewritten so its authoritative marker reads
    ``MIGRATION_TARGET_STATUS``, with every stray NEW declaration removed.

    Raises ``ValueError`` if *body* does not declare NEW anywhere
    (``needs_migration(body)`` is False) — this function is the migration's
    only writer and must refuse to touch a body outside its scope, the same
    fail-closed posture ``set_status_anchored`` takes for an unanchored body.

    Shape D (well-formed marker on line 1, any value including NEW) is
    replaced in place via ``set_status_anchored`` — one line changed, the
    rest of the body byte-identical.

    Shapes A / A' / B / C (unanchored — the authoritative line carries no
    readable marker) have every stray STATUS occurrence outside fenced code
    blocks deleted first (the bare line for A/A'/B, the misplaced HTML
    marker's own line for C), then the now-markerless body is handed to
    ``set_status()``, which prepends a fresh marker at the front.
    """
    if not needs_migration(body):
        raise ValueError(
            "migrate_body: body does not declare the invented NEW value "
            "anywhere outside a fenced code block — refusing to touch a "
            "body outside this migration's scope"
        )

    if extract_status_anchored(body) != "UNKNOWN":
        # Shape D: a well-formed marker sits on line 1 already (its value is
        # NEW, or needs_migration would not have matched). Replace in place.
        return set_status_anchored(body, MIGRATION_TARGET_STATUS, now_iso=now_iso)

    # Shapes A / A' / B / C: strip every stray STATUS occurrence outside
    # fenced code blocks, then hand the markerless body to set_status(),
    # which prepends cleanly (see module docstring for why set_status()
    # alone is unsafe on the unstripped body).
    lines = (body or "").split("\n")
    mask = _fenced_mask(body)
    kept: list[str] = []
    for i, line in enumerate(lines):
        if mask[i]:
            kept.append(line)
            continue
        if _BARE_STATUS_LINE_RE.match(line):
            continue  # shape A / A' / B — delete the stale bare line
        if _FULL_MARKER_RE.fullmatch(line.strip()):
            continue  # shape C — delete the misplaced HTML marker's own line
        kept.append(line)
    cleaned = "\n".join(kept)
    # When the deleted line sat at the very front (shapes A / A'), the blank
    # separator line that used to follow it is still there — but it was part
    # of the "marker + separator" unit, not body content. set_status() below
    # supplies its own "marker\n\n" separator, so a leading blank left behind
    # here would double it. Only leading newlines are stripped; a blank line
    # deleted from the *middle* of the body (shape B) is untouched, matching
    # "byte-identical otherwise" (Spec item 14).
    cleaned = cleaned.lstrip("\n")
    return set_status(cleaned, MIGRATION_TARGET_STATUS, now_iso=now_iso)


def verify_migrated(body: str) -> tuple[bool, list[str]]:
    """Post-write sanity check for a *fetched-from-GitHub* body.

    Returns ``(ok, problems)``. ``ok`` is True iff the body's authoritative
    marker reads MIGRATION_TARGET_STATUS AND no bare STATUS residue remains
    outside a fenced code block. This is the read-back verification D#2436
    Spec item 12 requires — it must run against the body GitHub actually
    served back, never the local pre-write string.
    """
    problems: list[str] = []
    status = extract_status_anchored(body)
    if status != MIGRATION_TARGET_STATUS:
        problems.append(f"extract_status_anchored returned {status!r}, expected {MIGRATION_TARGET_STATUS!r}")
    residue = find_residue_lines(body)
    if residue:
        problems.append(f"bare STATUS: residue on line(s) {residue}")
    return (not problems, problems)


def strip_status_lines(body: str) -> str:
    """Return *body* with every STATUS occurrence removed — bare lines and
    whole-line HTML markers, outside fenced code blocks. Used to compare a
    pre-image against a migrated body for content preservation (Spec item
    14): once STATUS lines are stripped from both sides, everything else
    must be identical.
    """
    lines = (body or "").split("\n")
    mask = _fenced_mask(body)
    kept: list[str] = []
    for i, line in enumerate(lines):
        if mask[i]:
            kept.append(line)
            continue
        if _BARE_STATUS_LINE_RE.match(line):
            continue
        if _FULL_MARKER_RE.fullmatch(line.strip()):
            continue
        kept.append(line)
    # A marker line at position 0 (real in a pre-image, synthetic in every
    # migrated body via set_status()'s "marker\n\n" prepend) leaves a leading
    # blank behind once stripped; normalize it away on both sides so the
    # comparison isn't thrown off by which side happened to carry the
    # leading marker. A blank removed from the *middle* of the body is left
    # exactly as found.
    return "\n".join(kept).lstrip("\n")


def content_preserved(pre_body: str, post_body: str) -> bool:
    """True iff *post_body* differs from *pre_body* only in its STATUS
    line(s) — i.e. every other line is byte-identical (Spec item 14).
    """
    return strip_status_lines(pre_body) == strip_status_lines(post_body)
