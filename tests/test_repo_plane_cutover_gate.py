"""tests/test_repo_plane_cutover_gate.py — the cutover cannot land early.

Setting ``code_repo`` in ``.autonomous-team/config.json`` (or ``project.json``)
IS the repo-plane cutover. It is one line, and it turns every remaining
mis-planed call site into a live misroute at the same instant — a security gate
that stops triggering, a CI kill switch that cannot be read, executor PRs that
all report as failed.

The constraint "do not set that key while defects remain" was, until this file
existed, a sentence in a PR body. That is the same shape as the deferral this
whole workstream exists to correct: an earlier plan handed the
``post-agent-hook.sh`` PR-existence check to "PR-d", whose actual subject was a
different file that does not use the resolver at all, so nothing covered it —
and the deferral was indistinguishable from the work being done right up until
it wasn't.

A constraint a reader must find and remember is not a constraint. This turns
"must not" into "cannot".

Two properties, and the second is what makes the first trustworthy:

1. The gate fires when ``code_repo`` is set while the ledger still lists
   defects, and does not fire in either legal state.
2. The gate fails LOUDLY on its own inputs. A missing, unreadable or
   marker-less ledger raises rather than parsing as an empty defect list —
   because "no entries" and "no file" look identical to anything that counts
   lines, and only one of them should clear the cutover. An empty-by-accident
   ledger green-lighting the flip is the precise failure this guards.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECTOR = REPO_ROOT / "scripts" / "audit_repo_plane.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_repo_plane_gate", DETECTOR)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["audit_repo_plane_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load()

LEDGER_WITH_DEFECTS = (
    "# REPO-PLANE-LEDGER-V1\n"
    "# header prose\n"
    "backend/team_status.py   2\n"
    "scripts/auto-plan.sh     2\n"
)
LEDGER_ALL_CLEAR = (
    "# REPO-PLANE-LEDGER-V1\n"
    "# every site fixed; entries intentionally absent\n"
)


def _tree(tmp_path: Path, ledger: str | None, config: dict | None = None,
          project: dict | None = None) -> Path:
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    if ledger is not None:
        (root / "scripts" / "repo-plane-known-defects.txt").write_text(ledger)
    if config is not None or project is not None:
        (root / ".autonomous-team").mkdir()
        if config is not None:
            (root / ".autonomous-team" / "config.json").write_text(json.dumps(config))
        if project is not None:
            (root / ".autonomous-team" / "project.json").write_text(json.dumps(project))
    return root


# ---------------------------------------------------------------------------
# Property 1 — the gate fires, and only when it should
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key_file", ["config.json", "project.json"])
def test_cutover_blocked_when_defects_remain(tmp_path, key_file):
    """code_repo set + non-empty ledger = blocked. Both files are cutover sites.

    PR-m sets the key in both, because the bash/TS resolvers read config.json
    and the Python resolver reads project.json — setting only one is a silent
    partial retarget, so either alone must trip this.
    """
    kwargs = {"config": None, "project": None}
    kwargs["config" if key_file == "config.json" else "project"] = {
        "repo": "owner/private", "code_repo": "public-org/public-repo"
    }
    root = _tree(tmp_path, LEDGER_WITH_DEFECTS, **kwargs)

    violations = audit.cutover_violations(root)

    assert violations, f"cutover was NOT blocked via {key_file}"
    assert "CUTOVER BLOCKED" in violations[0]
    assert "4 known repo-plane defect(s)" in violations[0]


def test_cutover_allowed_once_the_ledger_is_empty(tmp_path):
    """The gate must open. A gate that never opens gets deleted, not obeyed."""
    root = _tree(
        tmp_path, LEDGER_ALL_CLEAR,
        config={"repo": "owner/private", "code_repo": "public-org/public-repo"},
    )
    assert audit.cutover_violations(root) == []


def test_no_violation_before_the_cutover(tmp_path):
    """Today's state: defects remain but code_repo is unset. Not a violation."""
    root = _tree(tmp_path, LEDGER_WITH_DEFECTS, config={"repo": "owner/private"})
    assert audit.cutover_violations(root) == []


def test_no_config_at_all_is_not_a_violation(tmp_path):
    """A fork ships no .autonomous-team/. That is not a cutover."""
    root = _tree(tmp_path, LEDGER_WITH_DEFECTS)
    assert audit.cutover_violations(root) == []


def test_empty_string_code_repo_is_not_a_cutover(tmp_path):
    root = _tree(tmp_path, LEDGER_WITH_DEFECTS,
                 config={"repo": "owner/private", "code_repo": ""})
    assert audit.cutover_violations(root) == []


# ---------------------------------------------------------------------------
# Property 2 — loud on its own inputs (the skip-on-empty shape)
# ---------------------------------------------------------------------------

def test_missing_ledger_raises_rather_than_reading_as_no_defects(tmp_path):
    """Deleting the ledger must not be a way to clear the cutover."""
    root = _tree(tmp_path, None,
                 config={"repo": "owner/private", "code_repo": "public/repo"})
    with pytest.raises(audit.LedgerError) as exc:
        audit.cutover_violations(root)
    assert "empty defect list" in str(exc.value)


def test_ledger_without_the_marker_raises(tmp_path):
    """Truncated-to-nothing and all-defects-fixed must not look the same."""
    root = _tree(tmp_path, "backend/team_status.py 2\n",
                 config={"repo": "owner/private", "code_repo": "public/repo"})
    with pytest.raises(audit.LedgerError) as exc:
        audit.cutover_violations(root)
    assert "marker" in str(exc.value)


def test_zero_byte_ledger_raises(tmp_path):
    root = _tree(tmp_path, "",
                 config={"repo": "owner/private", "code_repo": "public/repo"})
    with pytest.raises(audit.LedgerError):
        audit.cutover_violations(root)


def test_unparseable_config_raises_rather_than_assuming_no_cutover(tmp_path):
    """Must not conclude "not cut over" from a file it could not read."""
    root = _tree(tmp_path, LEDGER_WITH_DEFECTS)
    (root / ".autonomous-team").mkdir()
    (root / ".autonomous-team" / "config.json").write_text("{ not json")
    with pytest.raises(audit.LedgerError):
        audit.cutover_violations(root)


def test_load_baseline_never_returns_empty_for_a_missing_file(tmp_path):
    """The regression this replaced: load_baseline used to return {}."""
    with pytest.raises(audit.LedgerError):
        audit.load_baseline(tmp_path / "nope.txt")


# ---------------------------------------------------------------------------
# The live tree
# ---------------------------------------------------------------------------

def test_live_tree_is_in_a_legal_state():
    """Runs against the real repo: either pre-cutover, or cut over and clean."""
    assert audit.cutover_violations(REPO_ROOT) == []


def test_live_ledger_is_intact():
    """The real ledger parses and carries its marker."""
    ledger = audit.load_baseline()
    assert isinstance(ledger, dict)
    text = (REPO_ROOT / "scripts" / "repo-plane-known-defects.txt").read_text()
    assert audit.LEDGER_MARKER in text


def test_live_tree_has_not_cut_over_yet():
    """Documents today's state, and fails loudly the day it changes.

    If this ever fails, the cutover has landed — at which point the gate above
    is the thing that had to have passed first.
    """
    assert audit.configured_code_repo(REPO_ROOT) == []
