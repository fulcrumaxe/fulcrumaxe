"""Regression test for the prose grep that feeds coldstart's repo-identity guard.

WHY THIS EXISTS (D#2348 PR-j)
-----------------------------
`scripts/coldstart.sh` reads a *runtime identity* out of an English sentence in
`.claude/agents/executor.md`:

    CARD_REPO="$(grep -m1 -oE 'ONLY interact with .[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+.' \\
                   "$AGENT_CARD" | grep -oE '[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')"

and the D#2226 repo-identity guard then hard-exits 1 when that value disagrees
with `.autonomous-team/project.json`'s `repo`.

Two properties make this fragile enough to be worth a test:

  1. The regex needs the slug immediately after "ONLY interact with " plus one
     character (the opening backtick). Any rewording that leads with prose —
     "ONLY interact with this project's own two repos — `<slug>`" — makes the
     grep return the empty string, and the guard then fails with a
     repo-identity error that names nothing about the real cause. This was hit
     for real while writing PR-j and was caught by running the grep, not by any
     check.

  2. Since PR-j the Repo Scope block names two planes, so more than one slug can
     appear near that sentence and *word order in prose* decides which one the
     guard validates against.

`scripts/ci/repo-target-gate.sh` cannot cover this: it skips `*.md` entirely,
and its own header calls that "the largest gap". So this test is the check.

Deliberately narrow. It asserts the extraction still yields the Discussion-plane
slug that `project.json` carries — nothing about the code plane. Teaching the
coldstart guard about two planes is the cutover's job, not this test's; if that
lands, this test is what tells you the prose has to move with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXECUTOR_CARD = _REPO_ROOT / ".claude" / "agents" / "executor.md"
_COLDSTART = _REPO_ROOT / "scripts" / "coldstart.sh"
_PROJECT_JSON = _REPO_ROOT / ".autonomous-team" / "project.json"

# Byte-for-byte the two patterns coldstart.sh:367 pipes together.
_SENTENCE_RE = re.compile(r"ONLY interact with .[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+.")
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _extract_card_repo(card: Path) -> str:
    """Reproduce coldstart.sh's CARD_REPO extraction. Empty string == no match,
    which is exactly what the shell substitution yields."""
    m = _SENTENCE_RE.search(card.read_text(encoding="utf-8"))
    if not m:
        return ""
    slug = _SLUG_RE.search(m.group(0))
    return slug.group(0) if slug else ""


class TestCardRepoIdentityExtraction:
    def test_extraction_is_non_empty(self):
        """An empty extraction makes the D#2226 guard fail with a misleading
        message, so assert this separately from the value."""
        assert _extract_card_repo(_EXECUTOR_CARD), (
            "coldstart.sh's CARD_REPO grep found no slug in "
            f"{_EXECUTOR_CARD.relative_to(_REPO_ROOT)}. The Repo Scope block must "
            "keep the slug immediately after 'ONLY interact with ' plus one "
            "character, or scripts/coldstart.sh's repo-identity guard breaks."
        )

    def test_extraction_matches_project_json_repo(self):
        """The guard compares CARD_REPO to project.json's `repo`; keep them equal."""
        if not _PROJECT_JSON.is_file():
            pytest.skip("no .autonomous-team/project.json in this checkout")
        expected = (json.loads(_PROJECT_JSON.read_text(encoding="utf-8")).get("repo") or "")
        if not expected:
            pytest.skip("project.json carries no `repo` key")
        assert _extract_card_repo(_EXECUTOR_CARD) == expected, (
            "executor.md's 'ONLY interact with' sentence must name the same repo "
            "project.json does, and name it FIRST — scripts/coldstart.sh takes the "
            "first slug in that sentence and the D#2226 guard exits 1 on a mismatch."
        )

    def test_coldstart_still_greps_this_shape(self):
        """If coldstart.sh stops extracting identity from prose, delete this file
        rather than leaving a test that guards nothing."""
        if not _COLDSTART.is_file():
            pytest.skip("scripts/coldstart.sh not present in this checkout")
        body = _COLDSTART.read_text(encoding="utf-8")
        assert "ONLY interact with" in body, (
            "scripts/coldstart.sh no longer greps 'ONLY interact with' out of an "
            "agent card. If the repo-identity guard was reworked, this test is "
            "stale — remove it in the same change."
        )
