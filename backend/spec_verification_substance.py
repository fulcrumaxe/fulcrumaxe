"""
spec_verification_substance.py — substance-based classifier for the backend
real-world-verification gate (D#2008).

D#2008: the Stage-2 code-reviewer gate hard-failed any CLI-touching PR whose
frozen Spec lacked a literal ``## Real-world verification`` heading — even
when the Spec plainly carried the same substance under a different heading
(D#1997 used ``## Spec (Acceptance)``) or in a linked comment instead of the
body (D#1944). A frozen Spec cannot gain a new heading, so that gate was
unsatisfiable by construction for any Spec written before the heading
requirement existed. This module replaces the heading match with a substance
match: it looks for runnable verification commands under a closed list of
recognised headings, wherever they are, and reports "absent" (which flags,
but never blocks — see backend/spawn_templates/code-reviewer.tmpl) only when
none exist.

Pure text in, dict out — no network, no ``gh``, stdlib only. Modelled on
backend/spec_external_docs.py, the existing precedent for a Spec-checking
module. Network I/O (fetching a Discussion body, or a linked frozen-Spec
comment) lives in scripts/lib/resolve-spec-text.sh and only there, so this
module stays offline-testable.

Classification only — this module never executes anything it finds, and
never did anything but classify until an execution path was added and then
removed again within this same PR (D#2008 code review, sixth round, owner
ruling). Rounds 2-5 additionally extracted commands from a Spec and ran
them (first widened to equivalent headings, then narrowed back to the
canonical heading after a security review found the widened version was a
real command-injection surface), on the theory that D#2008 needed both a
classification fix and a verification-execution feature. It didn't: PR
#1999 and PR #1995 unblock on classify_spec_text's/classify_discussion's
verdict alone, with nothing run. Five rounds tried, in order, to (1)-(3)
tell prose from commands by inspecting the string — unwinnable, English
cannot be enumerated — then (4) move the trust decision to the verdict
layer — which reopened the injection surface — then (5) parse Markdown
faithfully enough to keep a heading spoof from earning execution trust —
also unwinnable, for the same reason as (1)-(3): fence styles, HTML
comments, and rendering edge cases cannot be enumerated either. Removing
execution entirely, rather than making it safer, is what actually ends
this: extract_commands_for_run and the execution path in
scripts/run-backend-verification.sh are gone (that script is archived —
see archive/run-backend-verification-2026-08-20/README.md). The
code-reviewer runs verification commands itself, the way it already does
for every other PR, using its own judgment instead of a regex's.

Usage::

    from backend.spec_verification_substance import classify_spec_text

    result = classify_spec_text(spec_body)
    # {"status": "satisfied" | "satisfied_with_flag" | "absent",
    #  "matched_section": "<heading>" | None,
    #  "commands": [...], "negative_checks": [...], "flags": [...]}

CLI::

    python3 backend/spec_verification_substance.py check --discussion 1997 --json
    python3 backend/spec_verification_substance.py check --spec-file spec.md --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Section recognition — an explicit closed list, not a fuzzy match. A closed
# list is auditable and cannot drift the way a regex tuned to "look like a
# heading" can. The canonical heading is tried first; the others are
# equivalent shapes that carry the same substance under a different name and
# are flagged (`equivalent_section`) so a reviewer can see the difference.
# ---------------------------------------------------------------------------

_CANONICAL_HEADING = "## Real-world verification"
_EQUIVALENT_HEADINGS = [
    "## Spec (Acceptance)",
    "## Acceptance",
    "### Acceptance",
    "## Verification",
]

# Executable-leader allowlist for the anti-looseness predicate (see
# _is_command below). Deliberately narrow and explicit rather than "looks
# like a command" — a bare identifier in backticks (`feature_verified`,
# `main`) must never qualify as verification substance.
_EXECUTABLE_LEADERS = frozenset({
    "python3", "python", "bash", "sh", "pytest", "npm", "node", "git", "gh",
    "grep", "rg", "curl", "make", "cargo", "pip", "timeout", "jq", "awk",
    "sed", "diff", "wc", "test", "yarn", "tsc", "echo",
})

_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")
_FENCE_RE = re.compile(r"^\s*```")

# A token "looks like an argument" if it has a path separator, a leading
# flag dash, an env-style assignment, a quote character (a quoted string
# fragment), or a short file-extension-shaped suffix. Real commands almost
# always carry at least one of these among their non-leader tokens; plain
# English sentences almost never do.
#
# D#2008 code review, THIRD round: an earlier version of this rule used an
# English-stopword blacklist ("the", "is", "whether", ...) instead. That
# broke on real commands that legitimately contain common English/Python
# words as syntax -- `assert "hooks" in s`, `if [ -f x ]; then ...; fi`,
# `assert x and y` all use "in"/"if"/"and" as real keywords, not filler,
# and a stopword list can't tell those apart from prose without becoming
# either useless or actively wrong. Argument shape avoids the problem
# entirely: it doesn't care what the WORDS are, only whether something in
# the candidate looks like it was meant to be passed to a program (a path,
# a flag, a quoted fragment) -- and that signal survives "in"/"if"/"and"
# showing up as legitimate syntax while still rejecting prose (`diff
# confirms output matches expected`, `test confirms deployment succeeded`,
# `grep confirms matches exist`, none of which contain any such signal).
_ARG_SHAPE_RE = re.compile(r"[/\\]|^-|=|['\"]|\.[A-Za-z]{1,5}$")

# Short commands (<=2 remaining tokens: `echo hello`, `yarn test`,
# `git mv`) are accepted without needing an argument-shape token at all --
# genuine short invocations like this are common, and an English sentence
# essentially never happens to both open with a real leader word AND stop
# at exactly one or two words. Longer runs of remaining tokens are where
# prose and commands start looking similar in length, so those must show
# an argument-shape signal.
_MAX_ARGLESS_TOKENS = 2


# ---------------------------------------------------------------------------
# Anti-looseness predicate
# ---------------------------------------------------------------------------


def _is_command(candidate: str) -> bool:
    """D#2008's anti-looseness rule.

    A backtick span (or fenced-block line) counts as a command only if
    *all* hold:
      1. its first whitespace-separated token is an executable leader;
      2. it has at least 2 whitespace-separated tokens; and
      3. if there are more than _MAX_ARGLESS_TOKENS remaining tokens, at
         least one of them has argument shape (a path, a flag, an env
         assignment, a quoted fragment, or a short file extension) --
         see _ARG_SHAPE_RE.

    ``feature_verified`` fails (not a leader, 1 token). ``main`` fails
    (not a leader, 1 token). A bare ``pytest`` or ``jq`` also fails
    (1 token) -- deliberately: those same bare tool names show up
    constantly as *descriptive* mentions in Spec prose ("assert via `jq`
    that ..."), not as standalone invocations, and D#1944's own Spec text
    has exactly that shape. Requiring >=2 tokens costs a small, known,
    non-blocking false negative (a genuinely bare `pytest` command isn't
    recognised) in exchange for closing that false-positive door -- the
    frozen Spec's ruling is that ambiguous text must be rejected, not
    accepted, because an `absent` classification only flags while a
    wrongly-accepted candidate gets executed for real.

    ``python3 backend/kpi_engine.py show --json`` passes: leader, and
    "backend/kpi_engine.py"/"--json" both have argument shape. ``diff
    confirms output matches expected`` has 4 remaining tokens and none of
    them look like an argument -- rejected. ``echo hello`` and ``yarn
    test`` both pass despite having no argument-shape token, because
    they're short enough (<=2 remaining tokens) not to need one.

    (D#2008, third code review round -- filtering scripts/run-backend-
    verification.sh's execution through this same predicate is what
    actually closes the fabricated-exit-code deadlock; tightening
    classification alone, in round 2, did not, because
    extract_commands_for_run never consulted this function until now.)
    """
    tokens = candidate.strip().split()
    if len(tokens) < 2:
        return False
    leader, rest = tokens[0], tokens[1:]
    if leader not in _EXECUTABLE_LEADERS:
        return False
    if len(rest) > _MAX_ARGLESS_TOKENS and not any(
        _ARG_SHAPE_RE.search(tok) for tok in rest
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Section + command extraction
# ---------------------------------------------------------------------------


def _find_section(text: str, heading: str) -> str | None:
    """Return the text under *heading* (up to the next ``#``-level heading or
    EOF), or None if *heading* is not present as its own line.

    Matching is exact-heading-then-newline, so ``## Acceptance`` does not
    accidentally match a line like ``## Acceptance Criteria`` — the closed
    list stays closed.
    """
    pattern = re.compile(
        r"^" + re.escape(heading) + r"[ \t]*\n(.*?)(?=\n#{1,6}[ \t]|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1) if m else None


def _raw_candidates(section_text: str) -> list[str]:
    """Return every backtick-span and fenced-block-line candidate string in
    *section_text*, in document order, deduped by first occurrence.

    This one pass handles all four command shapes found in real Specs in
    this repo without needing four separate regexes: a dash-bulleted
    backtick span (`` - `cmd` ``), a numbered item with the command on an
    indented continuation line (D#1997's shape), a bare indented backtick
    line, and each line inside a fenced ```bash block. Filtering (loose vs
    strict) happens in the caller — this function only extracts candidates.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    in_fence = False
    for line in section_text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            candidate = line.strip()
            if candidate and candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
            continue
        for m in _BACKTICK_SPAN_RE.finditer(line):
            candidate = m.group(1).strip()
            if candidate and candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
    return candidates


def _extract_commands_loose(section_text: str) -> list[str]:
    """Canonical-section (``## Real-world verification``) extraction, for
    classify_spec_text's own ``commands`` field (the GATE-STATUS report,
    AC1-AC7). Classification only — nothing this returns is executed
    (D#2008, 6th round; see the module docstring).

    Unchanged from the pre-D#2008 behaviour (AC2 — no regression in the
    existing path): any non-empty backtick/fenced candidate counts, with no
    leader filtering. This heading is PM-authored specifically for this
    gate (D#508), and the anti-looseness predicate exists to keep
    *equivalent* sections (authored for a different purpose, e.g. an
    acceptance checklist) from being read as loosely as this one always has
    been for gate-status reporting purposes. (D#2008, 6th round: this
    module is classification-only now — nothing it returns is executed;
    see the module docstring.)
    """
    return _raw_candidates(section_text)


def _extract_commands_strict(section_text: str) -> list[str]:
    """Equivalent-section extraction — the anti-looseness rule applies.

    A candidate counts as a command only if it passes ``_is_command``. This
    is what keeps a ``## Verification`` section of pure prose
    (`feature_verified`, `main`) from satisfying the gate (AC5).
    """
    return [c for c in _raw_candidates(section_text) if _is_command(c)]


def _extract_negative_checks(section_text: str) -> list[str]:
    """Pull ``Negative checks:`` bullet lines out of *section_text* for
    reporting alongside ``commands`` in classify_spec_text's result —
    classification-only (D#2008, 6th round); nothing here is executed.
    """
    checks: list[str] = []
    in_block = False
    for line in section_text.splitlines():
        if re.match(r"^\s*Negative checks:\s*$", line, re.IGNORECASE):
            in_block = True
            continue
        if in_block:
            if re.match(r"^[A-Z]", line):
                in_block = False
                continue
            m = re.match(r"^\s*-\s*(.+)$", line)
            if m:
                checks.append(m.group(1).strip())
    return checks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_spec_text(text: str) -> dict:
    """Classify *text* (a Discussion body, optionally with a linked frozen
    comment appended by scripts/lib/resolve-spec-text.sh) for real-world
    verification substance.

    Returns::

        {"status": "satisfied" | "satisfied_with_flag" | "absent",
         "matched_section": "<heading>" | None,
         "commands": [...], "negative_checks": [...], "flags": [...]}

    The canonical heading is tried first. If it is present but yields zero
    commands, or is absent entirely, each equivalent heading is tried in
    list order. The first heading that yields >=1 command wins.

    ``matched_section`` reports the first *recognised* heading found even
    when it yields zero commands — that is deliberate: a reviewer reading
    `verification_substance` needs to distinguish "no verification content
    of any recognised shape" from "a recognised heading was there but
    nothing in it looked like a command", which is the D#1984 shape this
    whole change exists to stop reproducing. Only the ``status`` field
    collapses both to "absent" — collapsing which heading was found would
    destroy that distinction.
    """
    text = text or ""

    fallback_heading: str | None = None
    fallback_negs: list[str] = []

    section = _find_section(text, _CANONICAL_HEADING)
    if section is not None:
        commands = _extract_commands_loose(section)
        negs = _extract_negative_checks(section)
        if commands:
            return {
                "status": "satisfied",
                "matched_section": _CANONICAL_HEADING,
                "commands": commands,
                "negative_checks": negs,
                "flags": [],
            }
        fallback_heading = _CANONICAL_HEADING
        fallback_negs = negs

    for heading in _EQUIVALENT_HEADINGS:
        section = _find_section(text, heading)
        if section is None:
            continue
        commands = _extract_commands_strict(section)
        negs = _extract_negative_checks(section)
        if commands:
            return {
                "status": "satisfied_with_flag",
                "matched_section": heading,
                "commands": commands,
                "negative_checks": negs,
                "flags": ["equivalent_section"],
            }
        if fallback_heading is None:
            fallback_heading = heading
            fallback_negs = negs

    return {
        "status": "absent",
        "matched_section": fallback_heading,
        "commands": [],
        "negative_checks": fallback_negs,
        "flags": [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMENT_MARKER_RE = re.compile(r"^<!-- SPEC_TEXT_FROM_COMMENT:\d+ -->[ \t]*$", re.MULTILINE)


def _resolve_via_shell(discussion_num: int) -> str:
    """Shell out to scripts/lib/resolve-spec-text.sh — the one place this
    module touches the network (indirectly, via that script's `gh` call).
    """
    script = REPO_ROOT / "scripts" / "lib" / "resolve-spec-text.sh"
    result = subprocess.run(
        ["bash", str(script), str(discussion_num)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"resolve-spec-text.sh failed for discussion #{discussion_num}")
    return result.stdout


def classify_discussion(discussion_num: int) -> dict:
    """Classify a live Discussion by number, resolving body + linked frozen
    comment (D#1944's shape) via scripts/lib/resolve-spec-text.sh.

    When the body alone is absent but the linked comment supplies the
    substance, the result carries an added ``spec_in_comment`` flag — this
    is AC4's case: a body-only matcher would still fail PR #1995's Spec,
    because that Spec's ``## Spec (Acceptance)`` block lives in comment
    18074578, not the body.
    """
    full_text = _resolve_via_shell(discussion_num)

    marker = _COMMENT_MARKER_RE.search(full_text)
    if marker is None:
        return classify_spec_text(full_text)

    body_part = full_text[: marker.start()]
    body_result = classify_spec_text(body_part)
    if body_result["status"] != "absent":
        return body_result

    full_result = classify_spec_text(full_text)
    if full_result["status"] != "absent":
        full_result["flags"] = full_result["flags"] + ["spec_in_comment"]
    return full_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spec_verification_substance.py")
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser("check", help="classify a Spec's verification substance")
    check_p.add_argument("--discussion", type=int, help="Discussion number (live, via gh)")
    check_p.add_argument("--spec-file", help="path to a Spec text file (offline)")
    check_p.add_argument("--json", action="store_true", help="print the full JSON result")

    args = parser.parse_args(argv)

    if args.command != "check":
        parser.print_help()
        return 1

    if args.discussion is not None:
        result = classify_discussion(args.discussion)
    elif args.spec_file:
        text = Path(args.spec_file).read_text(encoding="utf-8")
        result = classify_spec_text(text)
    else:
        parser.error("check requires --discussion or --spec-file")
        return 2

    if args.json:
        print(json.dumps(result))
    else:
        print(result["status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
