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
        """Non-vacuity: prove the rule above has subjects to be true about.

        Without this, deleting every pinned call would make the suite pass.
        """
        lines = path.read_text(encoding="utf-8").split("\n")
        resolved = [n for n, line in enumerate(lines, 1) if _RESOLVE in line]
        if not resolved:
            pytest.skip(f"{_rel(path)} issues no code-plane gh call")
        for n in resolved:
            line = lines[n - 1]
            nxt = lines[n] if n < len(lines) else ""
            joined = line + ("\n" + nxt if line.rstrip().endswith("\\") else "")
            assert _GUARDED.search(joined), (
                f"{_rel(path)}:{n} resolves the code plane but does not use the "
                "${CODE_REPO:?...} guard — an unresolved plane would reach gh as "
                'an empty --repo, which exits 0 against the checkout remote.'
            )

    def test_no_literal_code_plane_slug(self, path: Path):
        """D#2348 Spec item 3 — the code plane is config, never a literal.

        CLAUDE.md is exempt: it documents where the plane points on each side of
        the cutover, which is prose about the value rather than a call target.
        """
        if path == _CLAUDE_MD:
            pytest.skip("CLAUDE.md documents the destination; it issues no pinned call")
        assert _CODE_PLANE_LITERAL not in path.read_text(encoding="utf-8"), (
            f"{_rel(path)} hardcodes {_CODE_PLANE_LITERAL}. The code plane resolves "
            "through _resolve_code_repo; a literal is wrong on one side of the cutover."
        )
