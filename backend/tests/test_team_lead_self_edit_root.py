"""classify_team_lead_self_edit must filter against the MAIN checkout root.

The classifier ignores a Team Lead's writes under `.autonomous-team/` — that is
the Team Lead's own state directory and writing there is its job. It recognises
those writes two ways: as a repo-relative prefix, and as an absolute path under
a checkout root.

The absolute arm used to anchor on REPO_ROOT, which is whichever tree
run_analyst was launched from. The Team Lead runs in the main checkout, so when
run_analyst runs from a worktree the two roots differ, the arm stops matching,
and every Team Lead state write is filed as a hard-rule violation. D#1997.

Why this file monkeypatches instead of just asserting on the real roots: on a
plain clone REPO_ROOT and the main root are the same directory, so a test
written against the live values passes whether or not the bug is present, and
CI runs plain clones. Substituting two deliberately different roots makes the
distinction observable on any host — which is the whole point of a pin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.run_analyst as ra

# Two roots that are unmistakably different, in the relationship a linked
# worktree has to the checkout it was branched from.
_MAIN = Path("/synthetic/main-repo")
_WORKTREE = _MAIN / ".claude" / "worktrees" / "agent-testpin"


@pytest.fixture()
def two_roots(monkeypatch):
    """Pretend run_analyst is running inside a worktree of a different main."""
    monkeypatch.setattr(ra, "REPO_ROOT", _WORKTREE, raising=True)
    monkeypatch.setattr(ra, "_MAIN_REPO_ROOT_PATH", _MAIN, raising=True)
    return _MAIN, _WORKTREE


def _team_lead_state(*write_paths: str) -> list[dict]:
    return [{
        "agent_id": "team-lead-testpin",
        "is_team_lead": True,
        "has_edit_write": True,
        "write_paths": set(write_paths),
    }]


def test_absolute_state_write_under_main_root_is_ignored(two_roots):
    """The case the bug broke: Team Lead writing its own state, absolute path."""
    main, _ = two_roots
    states = _team_lead_state(str(main / ".autonomous-team" / "now.md"))
    assert ra.classify_team_lead_self_edit(states) == []


def test_relative_state_write_is_ignored(two_roots):
    """The repo-relative arm is unaffected and still works."""
    states = _team_lead_state(".autonomous-team/now.md")
    assert ra.classify_team_lead_self_edit(states) == []


def test_absolute_project_write_under_main_root_still_fires(two_roots):
    """The classifier must not go quiet — this is the behaviour it exists for."""
    main, _ = two_roots
    states = _team_lead_state(str(main / "backend" / "server.py"))
    findings = ra.classify_team_lead_self_edit(states)
    assert len(findings) == 1
    assert findings[0]["category"] == "team_lead_self_edit"
    assert findings[0]["severity"] == "high"


def test_state_write_under_the_worktree_root_is_not_ignored(two_roots):
    """Anchoring on REPO_ROOT would exempt this path; anchoring on main does not.

    This is the assertion that actually distinguishes the two implementations.
    A `.autonomous-team/` write under the *worktree* is not the Team Lead's
    state directory — it is a write into a sub-agent's tree, which is exactly
    the kind of thing worth flagging.
    """
    _, worktree = two_roots
    states = _team_lead_state(str(worktree / ".autonomous-team" / "now.md"))
    findings = ra.classify_team_lead_self_edit(states)
    assert len(findings) == 1, (
        "a .autonomous-team/ path under the worktree root was exempted — the "
        "filter is anchored on REPO_ROOT rather than the main checkout"
    )


def test_mixed_writes_report_only_the_project_file(two_roots):
    main, _ = two_roots
    states = _team_lead_state(
        str(main / ".autonomous-team" / "now.md"),
        str(main / "backend" / "server.py"),
    )
    findings = ra.classify_team_lead_self_edit(states)
    assert len(findings) == 1
    evidence = " ".join(findings[0]["evidence"])
    assert "server.py" in evidence
    assert "now.md" not in evidence
