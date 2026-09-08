"""tests/test_pr_creation_instruction_agreement.py

D#1955 — two standing instructions used to contradict each other: the
start-of-day protocol told executors to use the REST API for PR creation and
to never rely on `gh pr create`, while `hooks/sandbox_rules.py` hard-blocks
REST `gh api` mutations from any sub-agent worktree. An executor could only
ever satisfy one of the two, and the only route that actually worked was the
one the written instruction forbade.

This file pins both halves of the agreement so a later edit can't silently
reintroduce either half of the contradiction:

  1. No instruction surface forbids `gh pr create` (tests 1-2, text-level).
  2. The sandbox's own behavior matches what the instructions now say
     (tests 3-4, calling `hooks.sandbox_rules.classify_bash` directly —
     the same code the real host runs, not a description of it).

WT is derived from hooks.sandbox_rules's own worktree-prefix constant,
never hardcoded — same idiom as tests/test_sandbox_path_token_scope.py.

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" python3 -m pytest tests/test_pr_creation_instruction_agreement.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.sandbox_rules import _WORKTREE_PREFIXES, classify_bash  # noqa: E402

WT = _WORKTREE_PREFIXES[0] + "d1955testid"

# ---------------------------------------------------------------------------
# Instruction surfaces (per Spec §3): where a written instruction can live.
# archive/ is excluded on purpose — the archive protocol keeps retired copies
# on disk by design, and one of them still carries the old line.
# ---------------------------------------------------------------------------


def _iter_instruction_surface_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.md",):
        files.extend((_REPO / ".claude" / "commands").glob(pattern))
        files.extend((_REPO / ".claude" / "agents").glob(pattern))
    files.extend((_REPO / "backend" / "spawn_templates").glob("*.tmpl"))
    files.extend(_REPO.rglob("fragments/*.md"))
    claude_md = _REPO / "CLAUDE.md"
    if claude_md.is_file():
        files.append(claude_md)
    return [f for f in files if f.is_file() and "archive" not in f.parts]


# A forbid-pattern for `gh pr create`: a negation word immediately governing
# the backtick-quoted command, or the command followed shortly by a
# prohibition word. Mutation proof: restoring the retired start-of-day bullet
# that told executors to rely on REST endpoints and steer clear of
# `gh pr create` (GraphQL rate-limit risk) makes
# test_no_instruction_forbids_gh_pr_create fail.
_FORBID_GH_PR_CREATE_RE = re.compile(
    r"(?:\bnever\b|\bdo not\b|\bdon't\b|\bmust not\b)\s*(?:use\s+)?`gh pr create`"
    r"|`gh pr create`\s*.{0,40}?\b(?:never|forbidden|prohibited)\b",
    re.IGNORECASE,
)

# A directive that tells an agent to actually run a mutating `gh api` call
# against a /pulls endpoint (as opposed to a warning that says NOT to).
# Mutation proof: adding the line
# "Use `gh api -X POST repos/o/r/pulls` to open the PR" to
# .claude/commands/start-the-day.md makes
# test_no_instruction_directs_rest_pr_creation fail.
_REST_PULLS_MUTATION_RE = re.compile(
    r"gh\s+api\s+(?:-X|--method)\s+(?:POST|PATCH|PUT|DELETE)\s+\S*pulls\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"\b(?:do not|don't|never|avoid)\b", re.IGNORECASE)


def _line_directs_rest_pr_creation(line: str) -> bool:
    match = _REST_PULLS_MUTATION_RE.search(line)
    if not match:
        return False
    prefix = line[: match.start()]
    return not _NEGATION_RE.search(prefix)


def test_no_instruction_forbids_gh_pr_create() -> None:
    offenders = []
    for path in _iter_instruction_surface_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _FORBID_GH_PR_CREATE_RE.search(text):
            offenders.append(str(path))
    assert offenders == [], f"instruction(s) still forbid `gh pr create`: {offenders}"


def test_no_instruction_directs_rest_pr_creation() -> None:
    offenders = []
    for path in _iter_instruction_surface_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if _line_directs_rest_pr_creation(line):
                offenders.append(f"{path}: {line.strip()}")
    assert offenders == [], f"instruction(s) direct REST PR creation: {offenders}"


def test_sandbox_blocks_rest_pr_creation_from_worktree() -> None:
    decision = classify_bash(
        "gh api -X POST repos/fulcrumaxe/fulcrumaxe/pulls -f title=t -f head=b -f base=main",
        WT,
    )
    assert decision.allow is False
    assert "sandbox_block_gh_api_mutation" in decision.reason


def test_sandbox_allows_gh_pr_create_from_worktree() -> None:
    plain = classify_bash("gh pr create --base main --title t --body b", WT)
    assert plain.allow is True, plain.reason

    with_repo_and_label = classify_bash(
        "gh pr create --repo fulcrumaxe/fulcrumaxe --base main --title t --body b --label bugfix",
        WT,
    )
    assert with_repo_and_label.allow is True, with_repo_and_label.reason
