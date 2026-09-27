"""Tests for the audit row apply_inbound.py writes when it advances
`refs/synced/code-plane` (D#2472).

`refs/synced/*` gets no reflog from git -- `core.logAllRefUpdates=true`
only extends reflogs to `refs/heads/`, `refs/remotes/`, `refs/notes/` and
`HEAD` -- so an audit row written at the marker-advance is the only record
of a move. These tests drive the real `apply_inbound.apply_inbound()`
against a disposable scratch repo (same harness shape as
test_inbound_apply.py, kept self-contained here rather than imported, since
`--import-mode=importlib` gives no guarantee that a sibling test module is
importable by name) and assert on the audit row itself, not on the ref's
new value -- the ref moving is not evidence the row was written.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_INBOUND_DIR = _THIS_DIR.parent / "inbound"
_REPO_ROOT = _THIS_DIR.parent.parent.parent
for _p in (str(_INBOUND_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apply_inbound  # noqa: E402

import backend.audit_trail as audit_trail_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures -- a minimal engine/plane pair, just enough to drive one real
# RESULT_APPLIED run (the classify/write-set decision logic itself is
# covered by test_inbound_apply.py; this file only needs one writable path).
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for relpath, content in files.items():
        full = repo / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def engine(tmp_path) -> dict:
    repo = tmp_path / "engine"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "engine@example.com")
    _git(repo, "config", "user.name", "Engine")
    _commit(repo, "engine seed", {"keep/engine-only.txt": "kept\n"})
    engine_main = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "--orphan", "plane")
    _git(repo, "rm", "-rq", "--cached", ".")
    (repo / "keep" / "engine-only.txt").unlink()
    plane_seed = _commit(repo, "plane seed", {"backend/shared.py": "v1\n"})
    plane_tip = _commit(repo, "widen (#1)", {"backend/new.py": "brand new\n"})

    _git(repo, "checkout", "-q", "main")
    _git(repo, "update-ref", "refs/synced/code-plane", plane_seed)

    return {"repo": repo, "main": engine_main, "plane_seed": plane_seed, "plane_tip": plane_tip}


@pytest.fixture
def state_dir(tmp_path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


class _Recorder:
    def __init__(self):
        self.prs: list[dict] = []

    def push(self, *, repo_dir, remote, commit_sha, branch):
        _git(Path(repo_dir), "update-ref", f"refs/heads/{branch}", commit_sha)

    def open_pr(self, *, repo_slug, branch, base, title, body):
        self.prs.append({"repo": repo_slug, "branch": branch})
        return f"https://github.com/{repo_slug}/pull/1"


def _classify(engine: dict, **overrides):
    import changeset
    import report as report_mod

    def _prs_for_commit(sha: str) -> list[int]:
        subject = changeset.commit_subject(sha, repo_dir=engine["repo"])
        hint = changeset.extract_pr_number(subject)
        return [hint] if hint is not None else []

    kwargs = dict(
        marker="refs/synced/code-plane",
        remote="code-plane",
        remote_branch="main",
        repo_dir=engine["repo"],
        code_repo_slug="example/code",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="plane",
        local_ref="main",
        resolve_trust_allowlist=lambda: {"trusted"},
        resolve_prs_for_commit=_prs_for_commit,
        resolve_pr_author=lambda pr: "trusted",
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: ["backend/*"],
        resolve_sensitive_prefixes=lambda: [],
    )
    kwargs.update(overrides)
    return report_mod.classify_report(**kwargs)


_CLASSIFY_KEYS = {"max_files", "max_lines", "local_ref", "marker", "extra_paths", "known_commit_trust"}


def _run_apply(engine, state_dir, rec, **overrides):
    kwargs = dict(
        repo_dir=engine["repo"],
        state_dir=state_dir,
        engine_remote="origin",
        engine_repo_slug="example/engine",
        code_repo_slug="example/code",
        local_ref="main",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="plane",
        classify=lambda **kw: _classify(engine, **{k: v for k, v in kw.items() if k in _CLASSIFY_KEYS}),
        push_branch=rec.push,
        open_pr=rec.open_pr,
    )
    kwargs.update(overrides)
    return apply_inbound.apply_inbound(**kwargs)


def _audit_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def scratch_audit(tmp_path):
    """Bind the module-level AuditTrail singleton to a private scratch file
    for the duration of one test, then restore whatever it was -- same
    pattern backend/tests/test_audit_trail.py::TestSingleton uses. Passing
    an explicit path bypasses backend.state_paths entirely (AuditTrail only
    reads AUTONOMOUS_TEAM_STATE_DIR when constructed with audit_path=None),
    so this never touches the production trail even if
    AUTONOMOUS_TEAM_STATE_DIR happens to be unset."""
    original = audit_trail_mod._singleton
    audit_trail_mod._singleton = None
    audit_path = tmp_path / "audit.jsonl"
    audit_trail_mod.get_audit_trail(audit_path)
    try:
        yield audit_path
    finally:
        audit_trail_mod._singleton = original


# ---------------------------------------------------------------------------
# Item 1 / item 2 -- the row exists, and carries old value, new value, and
# the run identity. There is exactly one call site that can advance the
# marker in apply_inbound.py (the update-ref inside `_run`, reached only
# after the branch is pushed and the PR is open -- see the comment above
# that call site); no --force/repair mode exists in this file, so this one
# test covers the full enumeration.
# ---------------------------------------------------------------------------


def test_marker_advance_writes_audit_row_with_old_new_and_run_identity(engine, state_dir, scratch_audit):
    rec = _Recorder()
    result = _run_apply(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED, result
    assert result["marker_advanced_to"] == engine["plane_tip"]

    rows = _audit_rows(scratch_audit)
    marker_rows = [r for r in rows if r["source"] == "engine_sync" and r["key"] == "refs/synced/code-plane"]
    assert len(marker_rows) == 1, rows

    row = marker_rows[0]
    assert row["old"] == engine["plane_seed"], "old value must be what the ref pointed to BEFORE the move"
    assert row["new"]["sha"] == engine["plane_tip"], "new value must be what the ref was moved to"
    assert row["new"]["pr_url"].endswith("/pull/1"), "run identity: the PR this move belongs to"
    assert row["new"]["branch"], "run identity: the branch this move belongs to"
    assert row["actor"] == "apply_inbound"


def test_read_ref_returns_none_for_a_marker_that_has_never_been_set(engine):
    """Exercised directly rather than through a full apply run: deleting the
    marker before a run makes classify_report refuse the run outright (an
    absent marker reads as unrelated/re-rooted history, not "first run"), so
    the "no prior value" case is never reached by a real apply. `_read_ref`
    itself must still not raise on a ref that does not resolve -- it is the
    function `_advance_marker` relies on to read the OLD value before every
    move, including a hypothetical future one where the marker legitimately
    has none."""
    _git(engine["repo"], "update-ref", "-d", "refs/synced/code-plane")
    assert apply_inbound._read_ref("refs/synced/code-plane", engine["repo"]) is None


# ---------------------------------------------------------------------------
# Item 3 -- mutation check, both directions. Not runnable as an assertion in
# CI (it requires editing the source under test), so it is exercised here as
# a recorded manual step: see the PR body for the red-then-green transcript
# this test produced with the audit call removed and then restored.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Item 4 -- a failed audit write must not silently become an unrecorded
# move. Decision made for this ref: the move proceeds (the branch is already
# pushed and the PR already open by the time the marker advances, so there
# is nothing left to refuse), and the failure is surfaced loudly instead of
# swallowed -- it propagates out of `_advance_marker`, is caught by
# `apply_inbound`'s own outer exception handler, and comes back as a failed
# run rather than a clean RESULT_APPLIED.
# ---------------------------------------------------------------------------


def test_failed_audit_write_surfaces_loudly_instead_of_a_silent_unrecorded_move(engine, state_dir, scratch_audit):
    class _BoomTrail:
        def emit(self, *a, **kw):
            raise RuntimeError("disk full (injected for D#2472 item 4)")

    audit_trail_mod._singleton = _BoomTrail()

    rec = _Recorder()
    result = _run_apply(engine, state_dir, rec)

    assert result["result"] != apply_inbound.RESULT_APPLIED, result
    assert "disk full (injected for D#2472 item 4)" in result.get("reason", ""), result
    # The failure was not swallowed: it costs the channel a strike, exactly
    # like any other unexpected error caught by apply_inbound's outer
    # handler would.
    assert result.get("consecutive_failures") == 1, result

    # The ref itself still moved -- the branch/PR side effects already
    # happened, and this is the "proceeds" half of the decision above. The
    # failure is surfaced through the run result, not through refusing the
    # ref move.
    assert _git(engine["repo"], "rev-parse", "refs/synced/code-plane").strip() == engine["plane_tip"]
