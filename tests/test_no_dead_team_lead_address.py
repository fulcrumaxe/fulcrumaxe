"""tests/test_no_dead_team_lead_address.py

Regression test for Discussion #2139 (amendment 1, items 13/14/16).

`team-lead` is not a resolvable SendMessage address in either direction
(confirmed empirically on D#2045 / D#2139). Two live instructions survived
the original D#2045 fix (#2157) because that fix's check was scoped to
`.claude/agents/*.md` only and matched line-by-line:

  - `.autonomous-team/intervention-library.json` (wrong_premise_retries) —
    written straight to a stuck agent's FIFO by live_analyst_daemon.py.
  - `backend/spawn_templates/project-manager.tmpl` — wrapped across a
    newline (`SendMessage →\nteam-lead`), which a line-scoped grep can't see.

This test is the widened, newline-spanning check from item 14, plus the
item 16 "protected text must survive" guard so a future sweep tuned to
drive the count to zero doesn't delete correct prose/config alongside it.

D#2202: the scan is restricted to git-tracked files (`testsupport/git_tracked.py`).
`.autonomous-team/registry.json` is a gitignored, locally-generated cache of live
GitHub Discussion titles and carries 2 live "SendMessage ... team-lead" matches
(quoting exactly this exposure), so the unrestricted scan passed on a fresh clone
/ CI (file absent) and failed on any operator checkout that had run the loop
(file populated) — same latent exposure D#2202 found in
test_no_dead_role_refs.py, sharing this file's SCAN_DIRS shape.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))
from testsupport.git_tracked import git_tracked_files

# Directories that compose text an agent will act on — role cards, spawn
# templates, and the (now in-scope) .autonomous-team/*.json config/library
# layer. Item 14: widened path list AND a matcher that spans newlines.
SCAN_DIRS = [
    REPO_ROOT / ".claude" / "agents",
    REPO_ROOT / "backend" / "spawn_templates",
    REPO_ROOT / ".autonomous-team",
]
SCAN_SUFFIXES = {".md", ".tmpl", ".json"}

# [^.]{0,100} in the Spec's grep == "no period" up to 100 chars, DOTALL so
# it can span a newline.
DEAD_ADDRESS_RE = re.compile(r"SendMessage[^.]{0,100}team-lead", re.DOTALL)


def _iter_scanned_files():
    # See testsupport/git_tracked.py: restricts the scan to files git tracks
    # so a locally-generated, untracked cache like .autonomous-team/registry.json
    # can't flip this guard's verdict depending on whether the machine running it
    # has ever populated that cache (D#2202).
    tracked = git_tracked_files(REPO_ROOT, *SCAN_DIRS)
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for path in d.rglob("*"):
            if path.is_file() and path.suffix in SCAN_SUFFIXES and path.resolve() in tracked:
                yield path


def test_no_dead_sendmessage_team_lead_instruction():
    """Item 14: 0 live 'SendMessage ... team-lead' instructions, newline-spanning."""
    hits = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in DEAD_ADDRESS_RE.finditer(text):
            hits.append(f"{path.relative_to(REPO_ROOT)}: {m.group(0)!r}")
    assert hits == [], "dead 'SendMessage ... team-lead' instruction(s) found:\n" + "\n".join(hits)


def test_intervention_library_wrong_premise_retries_has_no_dead_address():
    """Item 13 (Residual A): the retry-loop-breaker message must not point at team-lead."""
    import json

    lib = json.loads((REPO_ROOT / ".autonomous-team" / "intervention-library.json").read_text())
    template = lib["classifiers"]["wrong_premise_retries"]["message_template"]
    assert "team-lead" not in template, template
    # Item 13's required shape: point at the durable AGENT_OUTPUT / Blocked-State
    # Fast-Exit channel, not a substitute SendMessage target.
    assert "AGENT_OUTPUT" in template
    assert "verdict" in template


def test_project_manager_tmpl_needs_boss_branch_has_no_dead_address():
    """Item 13 (Residual B): the 'no named role fits' STOP branch must not point at team-lead."""
    text = (REPO_ROOT / "backend" / "spawn_templates" / "project-manager.tmpl").read_text()
    assert not DEAD_ADDRESS_RE.search(text), "project-manager.tmpl still instructs SendMessage to team-lead"


def test_protected_team_lead_references_survive():
    """Item 16: these 4 references are correct as written and must not be deleted."""
    code_reviewer = (REPO_ROOT / ".claude" / "agents" / "code-reviewer.md").read_text()
    assert code_reviewer.count("escalate to team-lead after this many needs-fix cycles") == 1

    project_manager = (REPO_ROOT / ".claude" / "agents" / "project-manager.md").read_text()
    assert project_manager.count("[team-lead-signed]") == 1

    config = (REPO_ROOT / ".autonomous-team" / "config.json").read_text()
    assert config.count('"team-lead": {') == 1
