"""Every code-plane `gh` call in an agent card must resolve its repo in-statement.

WHY THIS EXISTS (D#2348 PR-j, finding E5)
-----------------------------------------
The cards were first written with `--repo "$CODE_REPO"`. That reads as a pin and
greps as a pin, and it was neither:

  * nothing in `scripts/`, `backend/` or `hooks/` exports a bare `CODE_REPO` —
    the four assignments that exist are `_CODE_REPO`, script-local
  * an agent's shell state does not survive between tool calls, so a variable
    set by an earlier command is empty in the next one
  * `gh --repo ""` is NOT an error. It exits 0 after silently resolving from the
    checkout's git remote.

So every "pinned" call behaved exactly like the bare call it replaced, and was
harder to spot than the original, because the original at least grepped as bare.
An audit that checks write/read pairs name the same variable cannot catch this —
both halves were equally empty.

WHAT THIS ASSERTS
-----------------
1. No card mentions `$CODE_REPO` in the unguarded spelling. Uses must be
   `${CODE_REPO:?...}`, which aborts the command before `gh` runs.
2. Any line that USES the guard also RESOLVES it, in the same statement. Two
   lines would invite two tool calls, which is the original defect.
3. No card carries a literal code-plane slug (D#2348 Spec item 3). The code
   plane is config; a literal is wrong on one side of the cutover.

The same rules apply to `CLAUDE.md`, which is read at every spawn.

NOT ASSERTED, deliberately: that the Discussion plane is a literal. It is, and
that is correct — it is private permanently, and `.claude/agents/executor.md`'s
copy is load-bearing for the coldstart identity guard (see
tests/test_card_repo_identity_extraction.py). Pinning that here as well would
duplicate a rule without adding coverage.

NON-VACUITY FIX (D#2391)
------------------------
`test_every_code_plane_gh_call_is_guarded` used to look for lines containing
the resolve assignment (`_RESOLVE`) and check those were guarded. Deleting a
card's pinned calls outright, or reverting one to a fully bare `gh pr` call
with no `--repo` at all, removes the very text that check was searching for —
so the subject list went to empty and the check `pytest.skip`'d instead of
failing. A skip reads green in a summary the same way a pass does.

It now scans for real `gh pr` statements directly (`_GH_PR_VERB`, anchored to
a statement-start position) and requires each one to already carry the guard
or the Discussion-plane literal, so deleting the guard no longer deletes the
thing being checked. Scoped to `gh pr` specifically — PRs exist only on the
code plane, so every real `gh pr` statement needs pinning by construction;
`gh issue`/`gh api`/etc. are unaffected, per "NOT ASSERTED" above.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_DIR = _REPO_ROOT / ".claude" / "agents"
_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"

# The public code-plane slug. A card must never carry it as a literal.
_CODE_PLANE_LITERAL = "fulcrumaxe/fulcrumaxe"

# The in-statement resolution the cards prescribe.
_RESOLVE = 'CODE_REPO="$(source scripts/lib/repo-resolve.sh && _resolve_code_repo)"'

# `$CODE_REPO` or `${CODE_REPO}` — i.e. any use that is NOT the `:?` guarded form
# and NOT the assignment itself.
_UNGUARDED = re.compile(r"\$CODE_REPO\b|\$\{CODE_REPO\}")
_GUARDED = re.compile(r"\$\{CODE_REPO:\?[^}]*\}")

# An actual gh invocation, as opposed to prose that quotes the spelling. The
# rule-of-thumb sentences in each block legitimately show `--repo "${CODE_REPO:?…}"`
# while explaining it; only a line that really runs gh has a statement to be
# wrong about.
_GH_INVOCATION = re.compile(r"\bgh (pr|issue|api|run|search|repo|label) ")

# The Discussion plane's own literal (permanently private, never resolved —
# see the module docstring's "NOT ASSERTED" note). A `gh pr` call is never
# legitimately pointed here, since PRs don't exist on that plane, but the
# check accepts it for the same reason test_no_literal_code_plane_slug does
# not touch this spelling: it's the one hardcoded repo this project intends.
_DISCUSSION_PLANE_LITERAL = "autonomous-agent-7/fulcrumaxe"

# A real `gh pr <verb>` statement — anchored to a statement-start position
# (line start, after `; `, right inside a `(` / `$(`, or after `&&`/`||`) so
# it does not match an inline aside like "Do NOT use `gh pr review`" or a
# mid-sentence mention like "Most recent 5 merged PRs: gh pr list ...". Those
# are never executed verbatim, so they have no --repo to be wrong about.
_GH_PR_VERB = re.compile(
    r"(?:^\s*|; |\(|&&\s|\|\|\s)gh pr (view|edit|create|comment|diff|list|merge|review) "
)

# A human-typed placeholder standing in for a real value, e.g. CLAUDE.md's
# documented fallback `gh pr list --repo <the value the command above
# printed> --state open` for when `$(source ...)` is refused. That line is
# never run verbatim either — it is copied and edited by hand first — so it
# is exempt for the same reason a backtick-quoted mention is.
_PLACEHOLDER_VALUE = re.compile(r"<[^<>]+>")


def _scoped_files() -> list[Path]:
    files = sorted(_AGENTS_DIR.glob("*.md"))
    if _CLAUDE_MD.is_file():
        files.append(_CLAUDE_MD)
    return files


def _rel(p: Path) -> str:
    return str(p.relative_to(_REPO_ROOT))


@pytest.mark.parametrize("path", _scoped_files(), ids=_rel)
class TestCodePlanePins:
    def test_no_unguarded_code_repo_expansion(self, path: Path):
        """`--repo "$CODE_REPO"` expands to `--repo ""` and gh exits 0."""
        bad = [
            (n, line)
            for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
            if _UNGUARDED.search(line)
        ]
        assert not bad, (
            f"{_rel(path)} uses the unguarded CODE_REPO spelling on "
            f"{[n for n, _ in bad]}. Use ${{CODE_REPO:?code plane unresolved}} — an "
            'empty --repo is not an error, gh exits 0 and uses the checkout remote.'
        )

    def test_guarded_uses_resolve_in_the_same_statement(self, path: Path):
        """A guard on line N is useless if the resolve is a separate tool call.

        Continuation lines (the previous line ends with a backslash) inherit the
        statement, so the resolve is allowed to sit on the line above.
        """
        lines = path.read_text(encoding="utf-8").split("\n")
        offenders = []
        for n, line in enumerate(lines, 1):
            if not _GUARDED.search(line) or not _GH_INVOCATION.search(line):
                continue
            if _RESOLVE in line:
                continue
            prev = lines[n - 2] if n >= 2 else ""
            if prev.rstrip().endswith("\\") and _RESOLVE in prev:
                continue
            offenders.append(n)
        assert not offenders, (
            f"{_rel(path)} runs gh with ${{CODE_REPO:?...}} on {offenders} without "
            "resolving it in the same statement. Shell state does not survive "
            "between an agent's tool calls — join the resolve and the call with "
            "';' on one line, or continue the statement with a trailing backslash."
        )

    def test_every_code_plane_gh_call_is_guarded(self, path: Path):
        """Every real `gh pr` statement is guarded — checked by finding the
        statement itself, not by finding the guard and checking it's well-formed.

        The old version searched for `_RESOLVE` text and verified whatever it
        found was guarded; deleting that text (not just the guard) emptied the
        subject list and the check `pytest.skip`'d rather than failing — a
        skip that read green next to a pass. This scans for the `gh pr`
        statement directly (`_GH_PR_VERB`), so deleting the guard, or the
        whole pinned call, both leave a bare `gh pr` statement behind for this
        check to still find and fail on. A file with no `gh pr` statements at
        all (most cards) has nothing to check and passes with zero assertions
        — not a skip, so it does not hide behind a green summary either.

        Two shapes are exempt because neither is ever executed verbatim: an
        inline aside like `` `gh pr review` `` (excluded by requiring a
        statement-start position) and a line carrying a human-typed
        placeholder like `<the value ...>` (`_PLACEHOLDER_VALUE`) in place of
        a real argument.

        Not covered: a `gh pr` statement written with unconventional spacing
        or a verb outside the `_GH_PR_VERB` list would not be found by this
        check either — it relies on the same statement shapes every card in
        this repo already uses.
        """
        lines = path.read_text(encoding="utf-8").split("\n")
        offenders = []
        for n, line in enumerate(lines, 1):
            if not _GH_PR_VERB.search(line) or _PLACEHOLDER_VALUE.search(line):
                continue
            if _GUARDED.search(line) or _DISCUSSION_PLANE_LITERAL in line:
                continue
            nxt = lines[n] if n < len(lines) else ""
            if line.rstrip().endswith("\\") and (
                _GUARDED.search(nxt) or _DISCUSSION_PLANE_LITERAL in nxt
            ):
                continue
            offenders.append(n)
        assert not offenders, (
            f"{_rel(path)} runs gh pr on {offenders} without a guarded "
            "${CODE_REPO:?...} resolve (or the private Discussion-plane "
            "literal) — an unpinned gh pr call would silently resolve from "
            "the checkout remote."
        )

    def test_no_literal_code_plane_slug(self, path: Path):
        """D#2348 Spec item 3 — the code plane is config, never a literal call
        target.

        CLAUDE.md is exempt from this specific check, and only this check: it
        legitimately names the plane's resolved value in prose (D#2424) —
        that is exposition about the value, not a hardcoded call target, and
        the two are different things. This exemption is narrower than it
        looks: it does not exempt CLAUDE.md from every prose rule about the
        plane. It may not tie that value to a dated cutover clause ("today",
        "after the cutover", "until then") — that half is enforced
        separately, and for every card plus CLAUDE.md, by
        tests/test_plane_prose_ratchet.py.

        A plain return (not `pytest.skip`) keeps this exemption from counting
        as a skip — D#2391 wants zero skips from this suite, since a skip on
        an empty subject is exactly the failure mode under repair here.
        """
        if path == _CLAUDE_MD:
            return
        assert _CODE_PLANE_LITERAL not in path.read_text(encoding="utf-8"), (
            f"{_rel(path)} hardcodes {_CODE_PLANE_LITERAL}. The code plane resolves "
            "through _resolve_code_repo; a literal is wrong on one side of the cutover."
        )
