"""Ratchet against the repo-plane cutover prose regressing to a half-flip
shape (D#2424).

WHY THIS EXISTS
----------------
CLAUDE.md and ten agent cards described the `code_repo` cutover in two ways
that together produce a half flip:

  * naming only `.autonomous-team/config.json` as "the" place `code_repo` is
    set (or cleared) — silently leaving Python's resolver, which reads
    `project.json`, on the old plane while bash and TypeScript move; and
  * tying a plane's slug to a dated clause ("today", "after the cutover",
    "until then") — which inverts the moment the cutover actually lands,
    since the document an operator is most likely to be holding is the one
    that gets the direction backwards at exactly that moment.

Both defects were verified against the unfixed tree before this ratchet
existed (D#2424) and are fixed in the eleven files this module scans. This
module is what keeps them fixed — a future card written with the same
single-config-file or dated-slug prose fails here, not silently.

WHAT THIS ASSERTS
------------------
1. `test_config_json_mention_names_project_json_too` — any paragraph that
   talks about `code_repo` being set (or cleared) in
   `.autonomous-team/config.json` must also name
   `.autonomous-team/project.json` in the same paragraph. A paragraph is a
   block of text between blank lines; fenced code blocks are stripped first
   so a code sample that happens to quote the config path doesn't count as
   prose.
2. `test_slug_line_has_no_dated_clause` — any line naming either plane's
   slug (`autonomous-agent-7/fulcrumaxe` or `fulcrumaxe/fulcrumaxe`) must not
   also say "today", "after the cutover", or "until then" on that same line.

Both checks are keyed on co-occurrence, not on a bare slug or a bare
`config.json` mention. A slug alone in prose (legitimate now that the cutover
has happened) or a `config.json` mention unrelated to `code_repo` (e.g.
`boss_github_username` in project-manager.md) is not the defect shape and
must not be flagged — asserting against a bare slug would just recreate the
CLAUDE.md exemption this ratchet is trying to narrow
(tests/test_card_code_plane_pins.py::test_no_literal_code_plane_slug).

Scope: `.claude/agents/*.md` (all cards, so a new card is covered by
construction, not by being added to a list here) plus `CLAUDE.md`. Failures
name `path:line` and quote the offending text — a bare pass/fail over eleven
files is not actionable (D#2424 acceptance item 1).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_DIR = _REPO_ROOT / ".claude" / "agents"
_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"

_CONFIG_JSON = ".autonomous-team/config.json"
_PROJECT_JSON = ".autonomous-team/project.json"
_CODE_REPO = re.compile(r"\bcode_repo\b")
_SET_OR_CLEARED = re.compile(r"\b(set|cleared)\b", re.IGNORECASE)

_SLUGS = ("autonomous-agent-7/fulcrumaxe", "fulcrumaxe/fulcrumaxe")
_DATED_CLAUSES = ("today", "after the cutover", "until then")

# Fenced code blocks are prose-adjacent, not prose — a python one-liner that
# quotes the config path is not a claim about where code_repo is "set".
_FENCE = re.compile(r"```.*?```", re.DOTALL)


def _subject_files() -> list[Path]:
    files = sorted(_AGENTS_DIR.glob("*.md"))
    if _CLAUDE_MD.is_file():
        files.append(_CLAUDE_MD)
    return files


def _rel(p: Path) -> str:
    return str(p.relative_to(_REPO_ROOT))


def _blank_fences(text: str) -> str:
    """Replace fenced code blocks with the same count of newlines so every
    line number outside a fence is unaffected."""
    return _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _paragraphs_with_lines(text: str) -> list[tuple[int, str]]:
    """Return (first_line_number, paragraph_text) for each blank-line
    delimited block, after stripping fenced code blocks."""
    lines = _blank_fences(text).split("\n")
    paragraphs: list[tuple[int, str]] = []
    buf: list[str] = []
    start: int | None = None
    for i, line in enumerate(lines, 1):
        if line.strip() == "":
            if buf:
                paragraphs.append((start, "\n".join(buf)))
                buf = []
                start = None
            continue
        if start is None:
            start = i
        buf.append(line)
    if buf:
        paragraphs.append((start, "\n".join(buf)))
    return paragraphs


def test_config_json_mention_names_project_json_too():
    """A paragraph describing where `code_repo` is set/cleared must name
    both config files, or it reproduces the half-flip shape."""
    offenders = []
    for path in _subject_files():
        text = path.read_text(encoding="utf-8")
        for line_no, para in _paragraphs_with_lines(text):
            if _CONFIG_JSON not in para:
                continue
            if not _CODE_REPO.search(para):
                continue
            if not _SET_OR_CLEARED.search(para):
                continue
            if _PROJECT_JSON in para:
                continue
            snippet = para.strip().splitlines()[0][:120]
            offenders.append(f"{_rel(path)}:{line_no}: {snippet!r}")
    assert not offenders, (
        "paragraph names .autonomous-team/config.json as where `code_repo` is "
        "set/cleared without also naming .autonomous-team/project.json in the "
        "same paragraph — this is the half-flip shape (bash and TypeScript "
        "read config.json, Python reads project.json; setting only one moves "
        "two thirds of the system and leaves the rest behind silently). "
        "Offenders:\n" + "\n".join(offenders)
    )


def test_slug_line_has_no_dated_clause():
    """A line naming a plane's slug must not tie it to a dated cutover
    clause — the cutover already happened, so that phrasing is now
    backwards, not merely stale."""
    offenders = []
    for path in _subject_files():
        lines = path.read_text(encoding="utf-8").split("\n")
        for n, line in enumerate(lines, 1):
            if not any(slug in line for slug in _SLUGS):
                continue
            hit = next((c for c in _DATED_CLAUSES if c in line.lower()), None)
            if hit is None:
                continue
            offenders.append(
                f"{_rel(path)}:{n}: {line.strip()[:160]!r} (contains {hit!r})"
            )
    assert not offenders, (
        "line names a plane's slug next to a dated cutover clause "
        "('today' / 'after the cutover' / 'until then') — the cutover has "
        "already happened, so tying a slug to that phrasing is now backwards, "
        "not merely stale. Offenders:\n" + "\n".join(offenders)
    )
