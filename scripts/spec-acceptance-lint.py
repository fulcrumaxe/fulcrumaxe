#!/usr/bin/env python3
"""scripts/spec-acceptance-lint.py — PM-time linter for `## Spec (Acceptance)` items (D#2377).

D#2377 is a retrospective: five acceptance items across D#2348's PRs turned out to be
wrong, unmeetable, or self-contradictory, because each was written as a grep, a
filename, or a count — a proxy for a behaviour rather than the behaviour itself. Three
of the five principles it draws from that need human judgement (contradiction between
two items, stating which axis a substitution covers, "is this criterion checkable at
all"); this tool covers only the two a program can actually decide:

  1. An item names a file (a backtick-quoted path token) that does not exist in the
     tree at --root.
  2. An item's criterion states a bare count — a number, digit or spelled out,
     directly describing a tally of something in the codebase (occurrences, consumers,
     suites, and similar) rather than an enumeration or a behavioural check. This does
     not try to verify the number — it refuses the shape, same stance as
     scripts/ci/verify-no-unreproducible-counts.py.

Only the numbered items inside `## Spec (Acceptance)` are linted — never the
`acceptance_files:` frontmatter that precedes them, which routinely names files the PR
is expected to create and does not exist yet by design.

This is a PM-time tool, not a CI guard: acceptance items live in Discussion bodies,
which CI cannot see. It never blocks anything by exit code — it prints findings (or
nothing) for the PM to act on, mirroring scripts/spec-context-oracle.py's
silent-when-empty behaviour.

Usage:
    python3 scripts/spec-acceptance-lint.py <discussion_number> [--root PATH]

Exit codes: 0 on a normal run (with or without findings). 1 only if the Discussion
body could not be read at all.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.discussion_status import get_sections  # noqa: E402

# ---------------------------------------------------------------------------
# Parsing: pull the numbered items out of the `## Spec (Acceptance)` section,
# skipping the `---\n...\n---\n` frontmatter block that precedes them.
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A\s*---\n.*?\n---\n", re.DOTALL)
_ITEM_START_RE = re.compile(r"^\s*(\d+)\.\s", re.MULTILINE)


def parse_items(spec_text: str) -> list[tuple[int, str]]:
    """Split a `## Spec (Acceptance)` section (frontmatter already stripped) into
    numbered items. Each item's text runs from its own `N.` marker to the next one
    (or end of text), so multi-line items are kept whole."""
    text = _FRONTMATTER_RE.sub("", spec_text, count=1)
    matches = list(_ITEM_START_RE.finditer(text))
    items = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        items.append((num, text[start:end].strip()))
    return items


# ---------------------------------------------------------------------------
# Check 1: filenames that don't exist
# ---------------------------------------------------------------------------

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_KNOWN_EXTENSIONS = (
    "py", "sh", "md", "json", "yml", "yaml", "txt", "js", "ts", "tsx", "jsx",
    "ini", "cfg", "toml", "html", "css", "ipynb",
)
_BARE_NAME_RE = re.compile(r"^[\w.\-]+\.(?:" + "|".join(_KNOWN_EXTENSIONS) + r")$")
_ALPHA_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]*$")


def _looks_like_path(token: str) -> bool:
    """A conservative filter: would a human reading this backtick span read it as a
    bare file path? Excludes URLs, flags, and multi-word commands (those are split
    into words by the caller before this is checked, so a real command like
    `bash tests/foo.sh` still yields the path half as its own word)."""
    token = token.strip().strip(",:;()")
    if not token or " " in token:
        return False
    if token.startswith(("http://", "https://", "$", "-", "#")):
        return False
    if "::" in token:
        token = token.split("::", 1)[0]
    if "/" in token:
        return bool(_ALPHA_EXT_RE.search(token))
    return bool(_BARE_NAME_RE.match(token))


def _extract_path_tokens(item_text: str) -> list[str]:
    tokens: list[str] = []
    for span in _BACKTICK_RE.findall(item_text):
        for word in span.split():
            word = word.strip("`'\"(),;:")
            if _looks_like_path(word):
                tokens.append(word.split("::", 1)[0])
    return tokens


@dataclass
class Finding:
    item: int
    kind: str  # "missing_file" or "bare_count"
    detail: str


def _path_exists(token: str, root: Path) -> bool:
    """A token with a "/" is checked at that exact path. A bare filename (no "/")
    is checked by basename anywhere in the tree — Spec prose routinely refers to a
    file by name only ("`CLAUDE.md`", "`spec-context-oracle.py`") without its
    directory, and requiring the exact root-relative path for those would flag
    real, existing, nested files as missing (caught running against D#2377's own
    body, which does exactly this for `project-manager.md`)."""
    if "/" in token:
        return (root / token).exists()
    return next(root.rglob(token), None) is not None


def find_missing_files(items: list[tuple[int, str]], root: Path) -> list[Finding]:
    findings = []
    seen: set[tuple[int, str]] = set()
    for num, text in items:
        for path in _extract_path_tokens(text):
            key = (num, path)
            if key in seen:
                continue
            seen.add(key)
            if not _path_exists(path, root):
                findings.append(Finding(num, "missing_file", path))
    return findings


# ---------------------------------------------------------------------------
# Check 2: bare counts used as criteria
# ---------------------------------------------------------------------------

_NUMBER_WORD = (
    r"(?:\d[\d,]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)"
)
_COUNT_NOUN = (
    r"(?:occurrences?|consumers?|suites?|sites?|instances?|callers?|usages?|"
    r"references?|matches?|hits?|spots?)"
)
_BARE_COUNT_RE = re.compile(
    rf"\b{_NUMBER_WORD}\b(?:\s+[A-Za-z][A-Za-z-]*){{0,3}}\s+{_COUNT_NOUN}\b",
    re.IGNORECASE,
)


def find_bare_counts(items: list[tuple[int, str]]) -> list[Finding]:
    findings = []
    for num, text in items:
        m = _BARE_COUNT_RE.search(text)
        if m:
            findings.append(Finding(num, "bare_count", m.group(0).strip()))
    return findings


# ---------------------------------------------------------------------------
# Aggregate + CLI
# ---------------------------------------------------------------------------

def lint(body: str, root: Path) -> list[Finding]:
    sections = get_sections(body)
    spec_text = sections.get("spec", "")
    items = parse_items(spec_text)
    findings = find_missing_files(items, root) + find_bare_counts(items)
    findings.sort(key=lambda f: (f.item, f.kind))
    return findings


def _fetch_body(discussion_num: int) -> str:
    proc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "backend" / "discussion_cache.py"),
         "get-body", str(discussion_num)],
        capture_output=True, text=True, timeout=15,
        cwd=str(_REPO_ROOT),
    )
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PM-time linter for Spec (Acceptance) items — D#2377"
    )
    parser.add_argument("discussion", type=int, help="Discussion number")
    parser.add_argument(
        "--root", type=Path, default=_REPO_ROOT,
        help="Tree root to check filenames against (default: this checkout's root)",
    )
    args = parser.parse_args(argv)

    try:
        body = _fetch_body(args.discussion)
    except Exception as e:  # pragma: no cover - subprocess/env failure
        print(f"[spec-acceptance-lint] Failed to read Discussion #{args.discussion}: {e}",
              file=sys.stderr)
        return 1

    if not body.strip():
        print(f"[spec-acceptance-lint] Discussion #{args.discussion} has an empty "
              f"body — nothing to lint.", file=sys.stderr)
        return 0

    findings = lint(body, args.root)

    if not findings:
        print(f"[spec-acceptance-lint] No findings for D#{args.discussion}.",
              file=sys.stderr)
        return 0

    for f in findings:
        if f.kind == "missing_file":
            print(f"item {f.item}: names `{f.detail}`, which does not exist at {args.root}")
        else:
            print(f"item {f.item}: criterion states a bare count — \"{f.detail}\"")

    return 0


if __name__ == "__main__":
    sys.exit(main())
