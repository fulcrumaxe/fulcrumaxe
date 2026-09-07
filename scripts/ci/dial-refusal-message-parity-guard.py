#!/usr/bin/env python3
"""dial-refusal-message-parity-guard.py — the dial refusal text must say the
same thing in both languages (D#1945).

The defect
----------
The message a dial refusal hands an operator existed as a byte-identical string
literal in three files, in two languages:

    backend/dial_registry.py
    ts-backend/src/spawn/dial-registry.ts
    ts-backend/src/rpc/mutating-p6b.ts

PR #1943 reworded it and updated all three by hand. The reviewer confirmed they
matched — by reading them side by side. Nothing asserted it, and no test file
referenced the string at all. So "these three agree" was an invariant held by
whoever remembered.

It is a security-surface message: it tells an operator why a dial change was
refused and what to do instead. A drift means the same refusal hands out
different remediation depending on which backend the operator went through, and
the divergence is invisible until somebody opens both files.

Two of this repo's three known instances of this shape — D#1941's two
definitions of "first line", D#1910's merge-gate label names in four places —
had already silently drifted by the time anyone looked. This one had not yet,
which is the only moment a parity check is cheap to write.

What this guard is, and what it deliberately is not
---------------------------------------------------
The TypeScript duplication is gone: both TypeScript call sites now import
SOURCE_NOT_ALLOWLISTED_REMEDY from ts-backend/src/spawn/dial-messages.ts. One
language, one definition — that is a real deduplication and needs no check.

What cannot be deduplicated is the Python↔TypeScript pair, short of generating
one language from the other, which is more machinery than one string is worth.
So that pair is compared here, and this guard is *only* that comparison:

    python_message == ts_message

**This file contains no copy of the message.** That is the whole point. A guard
that asserted either side against a hardcoded expected string would be a fourth
copy, going stale on the next reword, and would fail exactly the way the three
copies it replaces were failing — silently agreeing with itself.

For the same reason it is not a grep. A substring search would pass while the
two halves said different things, as long as both still contained the fragment
being searched for.

How each side is extracted, and why that survives reformatting
--------------------------------------------------------------
Both extractions anchor on the constant's **name**, never on its text.

*Python* — the file is parsed with ``ast`` (never imported, never executed; the
guard runs inside CI's import-smoke job and must not drag the backend in). The
guard finds the module-level assignment to ``_SOURCE_NOT_ALLOWLISTED_REMEDY``
and takes its value with ``ast.literal_eval``. Python's parser folds implicit
adjacent-string concatenation into one constant before the guard ever sees it,
so reflowing the literal across a different number of lines, or switching
between ``'`` and ``"``, changes nothing about what is extracted.

*TypeScript* — comments are stripped by a scanner that tracks string, template
and comment state, so a commented-out declaration cannot be mistaken for the
real one. The guard then finds ``export const SOURCE_NOT_ALLOWLISTED_REMEDY``,
walks to the terminating ``;`` (respecting string literals, so a ``;`` inside
the message would not end it early), and requires the initializer to be a
``+``-joined run of string literals, which it decodes and concatenates. Quote
style is irrelevant — ``"``, ``'`` and a backtick template with no substitution
are all accepted — and so is how the concatenation is broken across lines.

No normalisation happens on either side. Whitespace is not collapsed, Unicode
is not normalised, and case is preserved: a real divergence that is "only"
whitespace or "only" an em dash swapped for a hyphen is a real divergence, and
comparing normalised forms would let precisely those through. The message
contains both an em dash (U+2014) and a backtick-quoted shell command, and both
survive the round trip untouched.

It cannot pass vacuously
------------------------
The natural failure mode of a name-anchored extractor is "found nothing,
compared nothing, passed" — the defect shape this repo found nine separate ways
on 2026-09-06, twice inside guards written to prevent it. Every way this guard
could end up scanning nothing is an explicit failure:

  1. A source file is missing or unreadable            -> FAIL
  2. The Python file does not parse                    -> FAIL
  3. No module-level ``_SOURCE_NOT_ALLOWLISTED_REMEDY`` -> FAIL (renamed/moved)
  4. More than one such assignment                     -> FAIL (ambiguous; it
     will not silently pick one)
  5. The Python value is not a plain string literal
     (an f-string, a ``.format()`` call, a name)       -> FAIL
  6. No ``export const SOURCE_NOT_ALLOWLISTED_REMEDY`` -> FAIL
  7. More than one such export                         -> FAIL
  8. The TypeScript initializer is not a ``+``-joined
     run of plain string literals (an interpolation,
     an identifier, a call)                            -> FAIL
  9. Either extracted message is empty                 -> FAIL (an empty
     string equals an empty string, which would be a match over no content)
 10. A call site does not import the constant          -> FAIL
 11. The Python constant is defined but referenced
     nowhere else in its module                       -> FAIL (a constant
     nothing uses is a museum piece, and comparing it would vouch for text no
     operator ever sees)

There is no code path in this file that reports success without having decoded
two non-empty strings and compared them.

Read item 10 narrowly, and item 11 too
--------------------------------------
Item 10 checks that the import is PRESENT. It does not check that the constant
is USED. A TypeScript call site that keeps the import and re-inlines a
different literal at its throw site still passes this guard — that was measured
against this file, not assumed.

What actually closes that gap is `noUnusedLocals` in ts-backend/tsconfig.json:
the now-unused import fails `bun run typecheck` with TS6133, in the required
ts-backend CI job. So the property does hold — by a different mechanism than
this guard, and anyone deciding whether this check alone is sufficient should
know it is not the one holding.

Item 11 is the stronger of the two only because Python has no equivalent
tooling here: it walks the module for a Load of the name, so it does catch a
raise site that stopped using the constant.

Known gap: TS_CALL_SITES is a hardcoded pair, not a discovery
-------------------------------------------------------------
The two call sites are named literally below. This guard therefore says nothing
about a THIRD TypeScript file that appears later and inlines its own copy of
the message — such a file is never looked at. That is exactly the "a third copy
grows back" case, closed for the two sites known today and open for any added
tomorrow. Recorded here rather than fixed, so the next reader does not assume a
discovery this guard does not do.

Wiring
------
scripts/ci/run-guards.sh discovers this directory by plain listing, so this file
runs by existing. It is deliberately NOT referenced by any workflow and NOT
listed in scripts/ci/guard-ledger.json: guard-registry-check.py fails by name if
a discovered guard is also invoked directly, or if it is ledgered while the
runner discovers it.

    python3 scripts/ci/dial-refusal-message-parity-guard.py

Exit 0: both languages carry the same message, from one definition each.
Exit 1: they differ, or a side could not be located.
Exit 2: usage error.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

PY_SOURCE = REPO_ROOT / "backend" / "dial_registry.py"
PY_CONST = "_SOURCE_NOT_ALLOWLISTED_REMEDY"

TS_SOURCE = REPO_ROOT / "ts-backend" / "src" / "spawn" / "dial-messages.ts"
TS_CONST = "SOURCE_NOT_ALLOWLISTED_REMEDY"

# The call sites that must consume the TypeScript constant rather than carry
# their own copy. This is the invariant the deduplication half of D#1945
# establishes.
#
# Hardcoded, not discovered — see "Known gap" in the module docstring. A new
# third TypeScript file that inlines its own copy is not covered, because it is
# not in this tuple and nothing goes looking for it.
TS_CALL_SITES = (
    REPO_ROOT / "ts-backend" / "src" / "spawn" / "dial-registry.ts",
    REPO_ROOT / "ts-backend" / "src" / "rpc" / "mutating-p6b.ts",
)

GUARD = "dial-refusal-message-parity"


class GuardError(Exception):
    """A side could not be located or decoded. Always fatal — never a pass."""


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _read(path: Path) -> str:
    if not path.is_file():
        raise GuardError(
            f"{_rel(path)} does not exist — the guard has nothing to compare. "
            f"If the file moved, update this guard's path constant; a missing "
            f"side is a failure, not a skip"
        )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardError(f"{_rel(path)} could not be read: {exc}") from exc


# ---------------------------------------------------------------------------
# Python side — ast, no import, no execution
# ---------------------------------------------------------------------------

def extract_python_message(path: Path) -> str:
    src = _read(path)
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        raise GuardError(f"{_rel(path)} does not parse as Python: {exc}") from exc

    found: list[ast.expr] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == PY_CONST:
                found.append(node.value)

    if not found:
        raise GuardError(
            f"{_rel(path)} has no module-level {PY_CONST} — the Python half of "
            f"the message could not be located. It was renamed, moved into a "
            f"function, or deleted; this guard cannot vouch for a comparison it "
            f"could not make"
        )
    if len(found) > 1:
        raise GuardError(
            f"{_rel(path)} assigns {PY_CONST} {len(found)} times at module level "
            f"— which one is the message is ambiguous, so the guard refuses to "
            f"pick rather than compare an arbitrary one"
        )

    try:
        value = ast.literal_eval(found[0])
    except (ValueError, SyntaxError) as exc:
        raise GuardError(
            f"{_rel(path)}: {PY_CONST} is not a plain string literal "
            f"({exc}) — the guard reads the source without importing it, so an "
            f"f-string, a .format() call or a computed value cannot be resolved "
            f"here. Keep it a literal, or teach this guard how to read it"
        ) from exc

    if not isinstance(value, str):
        raise GuardError(
            f"{_rel(path)}: {PY_CONST} is a {type(value).__name__}, not a string"
        )

    # A constant nothing reads is a museum piece: it could match the TypeScript
    # side perfectly while the raise site had re-inlined a different literal.
    if not _python_name_is_read(tree, PY_CONST):
        raise GuardError(
            f"{_rel(path)}: {PY_CONST} is defined but never read anywhere else in "
            f"the module — the raise site is no longer using it, so comparing it "
            f"would vouch for text no operator ever sees"
        )

    return value


def _python_name_is_read(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            return True
    return False


# ---------------------------------------------------------------------------
# TypeScript side — a small scanner, no node/bun dependency
# ---------------------------------------------------------------------------

_QUOTES = "\"'`"

_TS_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
    "0": "\0", "\\": "\\", "'": "'", '"': '"', "`": "`", "/": "/",
    "\n": "",  # line continuation
}


def strip_ts_comments(src: str) -> str:
    """Blank out // and /* */ comments, preserving offsets and newlines.

    String and template literals are copied through untouched, so a comment
    marker inside the message (the text carries a backtick-quoted shell command)
    is never mistaken for a comment. Template substitutions are entered as code,
    so a comment inside ``${...}`` is stripped too.
    """
    out: list[str] = []
    i, n = 0, len(src)
    # Stack of open template literals; a `${` pushes brace-depth tracking.
    template_stack: list[int] = []

    while i < n:
        ch = src[i]
        two = src[i:i + 2]

        if two == "//":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if two == "/*":
            end = src.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                out.append("\n" if src[j] == "\n" else " ")
            i = end
            continue

        if ch in _QUOTES:
            i = _copy_literal(src, i, out, template_stack)
            continue

        if template_stack:
            # Inside a ${ ... } substitution: track braces so we know when the
            # template body resumes.
            if ch == "{":
                template_stack[-1] += 1
            elif ch == "}":
                template_stack[-1] -= 1
                if template_stack[-1] == 0:
                    template_stack.pop()
                    out.append(ch)
                    i = _copy_template_body(src, i + 1, out, template_stack)
                    continue

        out.append(ch)
        i += 1

    return "".join(out)


def _copy_literal(src: str, i: int, out: list[str], template_stack: list[int]) -> int:
    """Copy the literal starting at src[i] verbatim. Returns the next index."""
    quote = src[i]
    out.append(quote)
    i += 1
    if quote == "`":
        return _copy_template_body(src, i, out, template_stack)
    n = len(src)
    while i < n:
        ch = src[i]
        out.append(ch)
        i += 1
        if ch == "\\" and i < n:
            out.append(src[i])
            i += 1
            continue
        if ch == quote:
            return i
    return i


def _copy_template_body(src: str, i: int, out: list[str], template_stack: list[int]) -> int:
    """Copy a template literal body until its closing backtick or a ``${``."""
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(src[i + 1])
            i += 2
            continue
        if src[i:i + 2] == "${":
            out.append("${")
            template_stack.append(1)
            return i + 2
        out.append(ch)
        i += 1
        if ch == "`":
            return i
    return i


def _end_of_literal(src: str, i: int) -> int:
    """Index just past the complete literal starting at src[i].

    Templates are walked including their ``${...}`` substitutions, and
    substitutions are walked including any literals nested inside them. Getting
    this wrong in the lenient direction would be the dangerous failure: a
    desynchronised scan can run past the real end of an expression and read
    text that is not part of it.
    """
    quote = src[i]
    i += 1
    n = len(src)
    if quote != "`":
        while i < n:
            ch = src[i]
            if ch == "\\":
                i += 2
                continue
            i += 1
            if ch == quote:
                return i
        return i
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if src[i:i + 2] == "${":
            i += 2
            depth = 1
            while i < n and depth:
                c = src[i]
                if c in _QUOTES:
                    i = _end_of_literal(src, i)
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                i += 1
            continue
        i += 1
        if ch == "`":
            return i
    return i


def decode_ts_string(raw: str, where: str) -> str:
    """Decode one TypeScript string literal, quotes included."""
    quote = raw[0]
    body = raw[1:-1]
    if quote == "`" and "${" in body:
        raise GuardError(
            f"{where}: the message is a template literal with a ${{...}} "
            f"substitution. It must be a plain literal — a computed message "
            f"cannot be compared against Python without executing it"
        )
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            raise GuardError(f"{where}: string literal ends in a trailing backslash")
        esc = body[i]
        if esc == "u":
            i += 1
            if i < n and body[i] == "{":
                end = body.find("}", i)
                if end == -1:
                    raise GuardError(f"{where}: unterminated \\u{{...}} escape")
                out.append(chr(int(body[i + 1:end], 16)))
                i = end + 1
            else:
                out.append(chr(int(body[i:i + 4], 16)))
                i += 4
            continue
        if esc == "x":
            out.append(chr(int(body[i + 1:i + 3], 16)))
            i += 3
            continue
        if esc in _TS_ESCAPES:
            out.append(_TS_ESCAPES[esc])
            i += 1
            continue
        raise GuardError(
            f"{where}: unrecognised escape \\{esc} in the message literal. The "
            f"guard refuses to guess what it decodes to rather than compare a "
            f"value it may have got wrong"
        )
    return "".join(out)


def parse_ts_concat(expr: str, where: str) -> str:
    """Decode a ``+``-joined run of string literals into one value."""
    pieces: list[str] = []
    i, n = 0, len(expr)
    expect_literal = True
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if expect_literal:
            if ch not in _QUOTES:
                raise GuardError(
                    f"{where}: expected a string literal at offset {i}, found "
                    f"{expr[i:i + 24]!r}. The message must be plain literals "
                    f"joined by '+' so it can be read without executing it"
                )
            end = _end_of_literal(expr, i)
            pieces.append(decode_ts_string(expr[i:end], where))
            i = end
            expect_literal = False
            continue
        if ch != "+":
            raise GuardError(
                f"{where}: expected '+' or end of expression at offset {i}, "
                f"found {expr[i:i + 24]!r}"
            )
        i += 1
        expect_literal = True

    if expect_literal:
        raise GuardError(f"{where}: the initializer ends with a dangling '+'")
    if not pieces:
        raise GuardError(f"{where}: the initializer held no string literals at all")
    return "".join(pieces)


def extract_ts_message(path: Path) -> str:
    src = strip_ts_comments(_read(path))
    where = _rel(path)

    anchor = f"export const {TS_CONST}"
    hits = []
    start = src.find(anchor)
    while start != -1:
        after = start + len(anchor)
        # Whole-identifier match: SOURCE_NOT_ALLOWLISTED_REMEDY_V2 is not it.
        if after >= len(src) or not (src[after].isalnum() or src[after] in "_$"):
            hits.append(start)
        start = src.find(anchor, start + 1)

    if not hits:
        raise GuardError(
            f"{where} has no `{anchor}` — the TypeScript half of the message "
            f"could not be located. It was renamed, un-exported, or the file "
            f"moved; a side the guard cannot find is a failure, not a match"
        )
    if len(hits) > 1:
        raise GuardError(
            f"{where} declares `{anchor}` {len(hits)} times — ambiguous, so the "
            f"guard refuses to pick one"
        )

    i = hits[0] + len(anchor)
    # Skip an optional type annotation, then require a single '='.
    eq = src.find("=", i)
    if eq == -1:
        raise GuardError(f"{where}: {TS_CONST} has no initializer")
    between = src[i:eq]
    if not all(c.isspace() or c in ":?" or c.isalnum() or c in "_$<>[],." for c in between):
        raise GuardError(
            f"{where}: unexpected text between {TS_CONST} and its '=': {between!r}"
        )
    if src[eq + 1:eq + 2] == "=":
        raise GuardError(f"{where}: {TS_CONST} is compared, not assigned")

    # Walk to the terminating ';', stepping over string literals so a ';'
    # inside the message could never end the initializer early.
    j, n = eq + 1, len(src)
    while j < n:
        ch = src[j]
        if ch in _QUOTES:
            j = _end_of_literal(src, j)
            continue
        if ch == ";":
            break
        j += 1
    else:
        raise GuardError(f"{where}: {TS_CONST}'s initializer is unterminated")

    return parse_ts_concat(src[eq + 1:j], f"{where} :: {TS_CONST}")


def assert_call_sites_import(paths: tuple[Path, ...]) -> list[str]:
    """Every known call site must import the constant.

    Presence of the import, not use of it: a call site that keeps the import
    and re-inlines a different literal passes here. `noUnusedLocals` in
    ts-backend/tsconfig.json is what fails that case (TS6133, in the required
    ts-backend job) — see the module docstring.
    """
    ok: list[str] = []
    for path in paths:
        src = strip_ts_comments(_read(path))
        imported = False
        idx = src.find("import")
        while idx != -1:
            end = src.find(";", idx)
            stmt = src[idx:end if end != -1 else len(src)]
            if " from " in stmt and TS_CONST in stmt:
                # Whole-identifier check inside the import clause.
                pos = stmt.find(TS_CONST)
                after = pos + len(TS_CONST)
                before_ok = pos == 0 or not (stmt[pos - 1].isalnum() or stmt[pos - 1] in "_$")
                after_ok = after >= len(stmt) or not (stmt[after].isalnum() or stmt[after] in "_$")
                if before_ok and after_ok:
                    imported = True
                    break
            idx = src.find("import", idx + 1)
        if not imported:
            raise GuardError(
                f"{_rel(path)} does not import {TS_CONST} — a call site that "
                f"stopped importing the shared constant is a call site carrying "
                f"its own copy again, which is the three-copy state D#1945 "
                f"removed. The comparison above would still pass"
            )
        ok.append(_rel(path))
    return ok


# ---------------------------------------------------------------------------

def describe_difference(a: str, b: str) -> list[str]:
    """Human-readable account of where two messages first diverge."""
    lines = []
    limit = min(len(a), len(b))
    at = next((k for k in range(limit) if a[k] != b[k]), limit)
    if at < limit:
        lines.append(
            f"  first differs at character {at}: "
            f"python {a[at]!r} (U+{ord(a[at]):04X}) vs typescript {b[at]!r} "
            f"(U+{ord(b[at]):04X})"
        )
        lines.append(f"  common prefix: ...{a[max(0, at - 40):at]!r}")
    else:
        longer = "python" if len(a) > len(b) else "typescript"
        tail = (a if len(a) > len(b) else b)[limit:]
        lines.append(
            f"  identical for the first {limit} character(s); {longer} then "
            f"continues with {tail!r}"
        )
    return lines


def main() -> int:
    if len(sys.argv) > 1:
        print(f"usage: {Path(sys.argv[0]).name}", file=sys.stderr)
        return 2

    try:
        py_message = extract_python_message(PY_SOURCE)
        ts_message = extract_ts_message(TS_SOURCE)
        call_sites = assert_call_sites_import(TS_CALL_SITES)
    except GuardError as exc:
        print(f"{GUARD}: FAIL — {exc}", file=sys.stderr)
        return 1

    # Item 9 of the spec: two empty strings are equal, and that equality would
    # vouch for nothing. Neither side is allowed to be empty.
    for label, path, value in (
        ("python", PY_SOURCE, py_message),
        ("typescript", TS_SOURCE, ts_message),
    ):
        if not value.strip():
            print(
                f"{GUARD}: FAIL — the {label} message extracted from "
                f"{_rel(path)} is empty. An empty string matches an empty "
                f"string, which is a comparison over no content",
                file=sys.stderr,
            )
            return 1

    if py_message != ts_message:
        print(f"{GUARD}: FAIL — the dial refusal message has drifted apart", file=sys.stderr)
        print(f"  {_rel(PY_SOURCE)} :: {PY_CONST}", file=sys.stderr)
        print(f"    {py_message!r}", file=sys.stderr)
        print(f"  {_rel(TS_SOURCE)} :: {TS_CONST}", file=sys.stderr)
        print(f"    {ts_message!r}", file=sys.stderr)
        for line in describe_difference(py_message, ts_message):
            print(line, file=sys.stderr)
        print(
            "  These are one operator-facing message in two lanes. Reword both "
            "or neither.",
            file=sys.stderr,
        )
        return 1

    print(
        f"{GUARD}: OK — {len(py_message)} characters, identical in "
        f"{_rel(PY_SOURCE)} :: {PY_CONST} and {_rel(TS_SOURCE)} :: {TS_CONST}; "
        f"imported by {', '.join(call_sites)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
