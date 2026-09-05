"""tests/test_no_dead_role_refs.py

Regression test for Discussion #2195.

`impl-coordinator` is not a real role: there is no `.claude/agents/impl-coordinator.md`
card, and Team Lead orchestrates executor + code-reviewer directly per CLAUDE.md's
single-spawner invariant. Commit `415c8c0d` (D#899, 2026-05-15) retired the role and drove
the hyphenated spelling (`impl-coordinator`) to zero in `.claude/agents/`, but left 22 live
occurrences of the spaced form (`Impl Coordinator`) untouched — because the cleanup was
verified with a single-literal grep, that fix read as complete for three months.

This test is the case-insensitive, all-spellings widened check D#2195 asks for: one pattern
(`impl[-_ ]?coordinator`, case-insensitive) covering hyphenated, spaced, snake_case, and
title-case forms in one scan, across the three directories agents actually read from.

Modeled directly on `tests/test_no_dead_team_lead_address.py` (D#2139).

D#2202: the scan is restricted to git-tracked files (`testsupport/git_tracked.py`).
`.autonomous-team/registry.json` is a gitignored, locally-generated cache of live
GitHub Discussion titles — six of which legitimately quote "impl-coordinator" as a
historical record, including D#2195's own title — so the unrestricted scan passed
on a fresh clone / CI (file absent) and failed on any operator checkout that had
run the loop (file populated). Scoping to tracked files fixes the class: the next
generated `.json` dropped into `.autonomous-team/` can't reintroduce this.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))
from testsupport.git_tracked import git_tracked_files

# The three directories that compose text an agent will act on — role cards, spawn
# templates, and the .autonomous-team/*.json config/library layer. Same set as
# test_no_dead_team_lead_address.py's SCAN_DIRS (D#2139 prior art).
SCAN_DIRS = [
    REPO_ROOT / ".claude" / "agents",
    REPO_ROOT / "backend" / "spawn_templates",
    REPO_ROOT / ".autonomous-team",
]
SCAN_SUFFIXES = {".md", ".tmpl", ".json"}

# Dated historical plan records are excluded by design (D#2195 Out of scope): they
# document what was planned in the past and must not be rewritten to match present-day
# role names.
#
# This exclusion no longer changes the verdict. It used to be load-bearing:
# .autonomous-team/PLAN-2026-05-12.md was tracked and carried 2 intentional
# historical references, so without the exclusion the guard could never pass. The
# repo split moved the four dated PLAN-*.md to the private companion repo and
# gitignored the pattern, so no PLAN file is tracked here now and the scan (which
# is tracked-files-only) would skip them regardless.
#
# Kept anyway, because start-the-day still GENERATES .autonomous-team/PLAN-<DATE>.md
# on this operator's disk every day. Those are untracked, but the exclusion is what
# stops the guard from going red if one is ever force-added.
EXCLUDED_NAME_RE = re.compile(r"^PLAN-.*\.md$")

# One pattern covering hyphen, underscore, space, and title-case in a single
# case-insensitive scan. re.IGNORECASE is the whole fix for the 415c8c0d failure mode:
# that cleanup's grep matched only the literal hyphenated spelling.
#
# NOTE: this deliberately does NOT match the abbreviated "impl_coord" spelling used by
# the live control-plane gate key `self_observe_impl_coord` in
# .autonomous-team/config.json. That key is out of scope for this Spec (renaming it is a
# behavior change with migration risk) — do not loosen this pattern to `impl[-_ ]?coord`.
DEAD_ROLE_RE = re.compile(r"impl[-_ ]?coordinator", re.IGNORECASE)


def _iter_scanned_files():
    # .autonomous-team/ holds a lot of locally-generated runtime state
    # (registry.json — a gitignored cache of live GitHub Discussion titles —
    # is the concrete case D#2202 fixed) alongside the tracked role cards and
    # spawn templates this guard exists to check. Scoping to what git tracks
    # means the verdict depends on repo content, not on which caches happen
    # to be populated on the machine running the test. See
    # testsupport/git_tracked.py for why a failed git call raises rather than
    # degrading to "no tracked files" (that would make this scan vacuous).
    tracked = git_tracked_files(REPO_ROOT, *SCAN_DIRS)
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for path in d.rglob("*"):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            if EXCLUDED_NAME_RE.match(path.name):
                continue
            if path.resolve() not in tracked:
                continue
            yield path


def test_no_dead_impl_coordinator_references():
    """Zero occurrences of impl-coordinator, in any spelling, case-insensitively,
    across .claude/agents/, .autonomous-team/, and backend/spawn_templates/
    (excluding dated PLAN-*.md historical records)."""
    hits = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in DEAD_ROLE_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            hits.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {m.group(0)!r}")
    assert hits == [], "dead 'impl-coordinator' role reference(s) found:\n" + "\n".join(hits)


def test_protected_role_rules_survive():
    """No line was deleted to zero the count — the rule/table each line carried still
    holds, now naming Team Lead instead of the nonexistent role (D#2195 item 8)."""
    executor = (REPO_ROOT / ".claude" / "agents" / "executor.md").read_text()
    assert executor.count("Notify only Team Lead (never reviewers directly)") == 1

    project_manager = (REPO_ROOT / ".claude" / "agents" / "project-manager.md").read_text()
    # Every row of the STATUS table still has a non-empty Owner column.
    for status in ("SPEC_READY", "IMPLEMENTING", "REVIEWING", "DONE"):
        line = next(
            line for line in project_manager.splitlines() if line.startswith(f"| `{status}`")
        )
        cells = [c.strip() for c in line.strip("|").split("|")]
        owner = cells[-1]
        assert owner, f"{status} row lost its Owner column: {line!r}"
