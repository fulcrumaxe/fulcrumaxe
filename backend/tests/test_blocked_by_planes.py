"""Acceptance suite for D#2554 — BLOCKED-BY resolves PR refs against the code
plane, not the Discussion plane.

Every test here in the file exercises ``_default_fetcher`` directly. Every
other test in ``test_discussion_status.py`` injects a mock ``fetcher``, so
that suite reaches none of the only code that ever names a repo — which is
why the original defect (PR refs resolved against ``backend._repo.REPO_OWNER``
/``REPO_NAME``, the Discussion plane, instead of ``CODE_REPO``) survived a
fully green suite. These tests are written to fail against the pre-fix
``backend/blocked_by.py``: it builds one ``repository(...)`` selection (not
two aliased ones), reads the response from ``payload["data"]["repository"]``
(a key this suite's payloads never populate), and returns a dict with no
``plane_error`` key at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import blocked_by  # noqa: E402
from backend._repo import CODE_REPO, REPO_NAME, REPO_OWNER  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeProc:
    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _capture_query(monkeypatch, payload: dict, returncode: int = 0, stderr: str = ""):
    """Patch ``blocked_by.subprocess.run`` to record calls and hand back
    *payload* as the parsed ``gh api graphql`` response. Returns the list of
    argv lists ``subprocess.run`` was called with.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True, timeout=60):  # noqa: ARG001
        calls.append(cmd)
        return _FakeProc(json.dumps(payload), stderr, returncode)

    monkeypatch.setattr(blocked_by.subprocess, "run", fake_run)
    return calls


def _query_from_calls(calls: list[list[str]]) -> str:
    assert len(calls) == 1, f"expected exactly one subprocess.run call, got {len(calls)}"
    cmd = calls[0]
    query_arg = cmd[-1]
    assert query_arg.startswith("query=")
    return query_arg[len("query=") :]


# ---------------------------------------------------------------------------
# AC3 — one round trip, two aliases
# ---------------------------------------------------------------------------


def test_default_fetcher_one_round_trip_two_aliases(monkeypatch):
    payload = {
        "data": {
            "code": {"p169": {"state": "MERGED", "title": "x"}},
            "disc": {"d2540": {"body": "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->"}},
        }
    }
    calls = _capture_query(monkeypatch, payload, returncode=0)

    result = blocked_by._default_fetcher([169], [2540])

    assert len(calls) == 1
    query = _query_from_calls(calls)
    assert query.count("repository(") == 2

    # every pullRequest( selection sits inside the alias whose owner/name is
    # CODE_REPO; every discussion( selection sits inside the alias whose
    # owner/name is REPO_OWNER/REPO_NAME.
    split_at = query.index("disc: repository(")
    code_part, disc_part = query[:split_at], query[split_at:]
    code_owner, code_name = CODE_REPO.split("/", 1)

    assert f'owner:"{code_owner}", name:"{code_name}"' in code_part
    assert "pullRequest(" in code_part
    assert "discussion(" not in code_part

    assert f'owner:"{REPO_OWNER}", name:"{REPO_NAME}"' in disc_part
    assert "discussion(" in disc_part
    assert "pullRequest(" not in disc_part

    assert result["pr"] == {169: "MERGED"}
    assert 2540 in result["discussion"]


# ---------------------------------------------------------------------------
# AC4 — an empty plane emits no alias
# ---------------------------------------------------------------------------


def test_default_fetcher_omits_alias_for_empty_pr_plane(monkeypatch):
    payload = {"data": {"disc": {"d2540": {"body": "x"}}}}
    calls = _capture_query(monkeypatch, payload, returncode=0)

    blocked_by._default_fetcher([], [2540])

    query = _query_from_calls(calls)
    assert query.count("repository(") == 1
    assert "code:" not in query
    assert "disc:" in query


def test_default_fetcher_omits_alias_for_empty_discussion_plane(monkeypatch):
    payload = {"data": {"code": {"p169": {"state": "MERGED"}}}}
    calls = _capture_query(monkeypatch, payload, returncode=0)

    blocked_by._default_fetcher([169], [])

    query = _query_from_calls(calls)
    assert query.count("repository(") == 1
    assert "disc:" not in query
    assert "code:" in query


def test_default_fetcher_no_call_when_nothing_requested(monkeypatch):
    calls = _capture_query(monkeypatch, {"data": {}})
    result = blocked_by._default_fetcher([], [])
    assert calls == []
    assert result == {"pr": {}, "discussion": {}, "plane_error": {}}


# ---------------------------------------------------------------------------
# AC5 — _default_fetcher is under test at all, against the measured shape
# ---------------------------------------------------------------------------


def test_default_fetcher_parses_measured_payload_shape(monkeypatch):
    """The exact partial-response shape the PM measured live against the API
    (Consensus Summary, D#2554): one alias resolves fully, a nested number
    inside the other alias is null, and ``errors`` rides alongside ``data``.
    """
    payload = {
        "data": {
            "code": {"p169": {"state": "MERGED", "title": "x"}, "p999999": None},
            "disc": {"d2540": {"body": "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->"}},
        },
        "errors": [{"type": "NOT_FOUND", "path": ["code", "p999999"]}],
    }
    _capture_query(monkeypatch, payload, returncode=1)

    result = blocked_by._default_fetcher([169, 999999], [2540])

    assert result["pr"] == {169: "MERGED"}
    assert 999999 not in result["pr"]
    assert 2540 in result["discussion"]
    assert result["plane_error"] == {}


# ---------------------------------------------------------------------------
# AC6 — CLOSED no longer clears a PR ref (covered end-to-end for the resolver
# in test_discussion_status.py::test_pr_ref_resolution; this pins the literal
# set _default_fetcher's caller reads).
# ---------------------------------------------------------------------------


def test_pr_cleared_states_is_merged_only():
    assert blocked_by._PR_CLEARED_STATES == {"MERGED"}


# ---------------------------------------------------------------------------
# AC7 — provably a no-op for an undiverged adopter
# ---------------------------------------------------------------------------


def test_default_fetcher_is_a_noop_when_planes_match(monkeypatch):
    """Force CODE_REPO == REPO the way every adopter with ``code_repo`` unset
    always has it, and assert every owner/name pair in the emitted query is
    the same slug — reaching one repo, as today, never two.
    """
    same_slug = f"{blocked_by._REPO_OWNER}/{blocked_by._REPO_NAME}"
    monkeypatch.setattr(blocked_by, "_CODE_REPO", same_slug)

    payload = {
        "data": {
            "code": {"p169": {"state": "MERGED"}},
            "disc": {"d2540": {"body": "x"}},
        }
    }
    calls = _capture_query(monkeypatch, payload, returncode=0)

    blocked_by._default_fetcher([169], [2540])
    query = _query_from_calls(calls)

    expected_pair = f'owner:"{blocked_by._REPO_OWNER}", name:"{blocked_by._REPO_NAME}"'
    assert query.count(expected_pair) == 2


# ---------------------------------------------------------------------------
# AC10 — forbidden shapes
# ---------------------------------------------------------------------------


def test_split_code_repo_raises_rather_than_emitting_empty_owner_or_name(monkeypatch):
    monkeypatch.setattr(blocked_by, "_CODE_REPO", "not-a-valid-slug")
    with pytest.raises(RuntimeError):
        blocked_by._split_code_repo()


def test_discussion_repo_is_not_imported():
    assert not hasattr(blocked_by, "DISCUSSION_REPO")


# ---------------------------------------------------------------------------
# AC11 — a plane-level failure fails closed, and is distinguishable
# ---------------------------------------------------------------------------


def test_plane_level_failure_blocks_only_that_planes_refs(monkeypatch):
    """Measured shape: a wrong/inaccessible code_repo returns ``data.code:
    null`` while the Discussion alias still resolves. The code-plane refs
    must carry a distinct plane_error entry; the surviving Discussion alias
    must parse normally.
    """
    payload = {
        "data": {
            "code": None,
            "disc": {"d2540": {"body": "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->"}},
        },
        "errors": [{"path": ["code"]}],
    }
    _capture_query(monkeypatch, payload, returncode=1)

    result = blocked_by._default_fetcher([169], [2540])

    assert result["pr"] == {}
    assert result["plane_error"].get("pr") == blocked_by._CODE_REPO
    assert "discussion" not in result["plane_error"]
    assert 2540 in result["discussion"]


def test_plane_level_failure_reason_via_resolver_is_distinct_from_not_found(monkeypatch):
    payload = {
        "data": {
            "code": None,
            "disc": {"d2540": {"body": "<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->"}},
        },
        "errors": [{"path": ["code"]}],
    }
    _capture_query(monkeypatch, payload, returncode=1)

    r = blocked_by.BlockerResolver()
    outstanding = dict(r.unresolved(["#169", "D#2540"]))

    assert "#169" in outstanding
    assert "not found" not in outstanding["#169"]
    assert blocked_by._CODE_REPO in outstanding["#169"]
    # The surviving plane's Discussion ref resolved normally (STATUS:DONE) —
    # not also blocked by the code plane's failure.
    assert "D#2540" not in outstanding


def test_total_fetch_failure_still_raises(monkeypatch):
    """When every queried plane comes back missing/null AND the process
    itself failed, this is a fetch failure, not a per-plane one — the
    existing carried-over guard still raises so the whole batch fails closed.
    """
    payload = {"data": {"code": None}}
    _capture_query(monkeypatch, payload, returncode=1, stderr="network unreachable")

    with pytest.raises(RuntimeError):
        blocked_by._default_fetcher([169], [])


# ---------------------------------------------------------------------------
# AC8/AC9 — audit row per cleared ref, and audit failure is inert
# ---------------------------------------------------------------------------


def test_emit_cleared_audit_calls_get_audit_trail(monkeypatch):
    """A cleared ref emits one audit call naming the repo slug, object kind,
    number, observed state and resolver source. Exercised against a fake
    trail here (``get_audit_trail``'s real singleton freezes its path to
    whichever process called it first, so the live write is verified against
    a fresh subprocess at Gate 2, not in-process)."""
    calls = []

    class _FakeTrail:
        def emit(self, **kwargs):
            calls.append(kwargs)

    import backend.audit_trail as audit_trail_module

    monkeypatch.setattr(audit_trail_module, "get_audit_trail", lambda *a, **k: _FakeTrail())

    blocked_by._emit_cleared_audit("pr", 169, "MERGED", blocked_by._CODE_REPO, "backend._repo.CODE_REPO")

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["source"] == "gate"
    assert kwargs["new_value"]["repo"] == blocked_by._CODE_REPO
    assert kwargs["new_value"]["kind"] == "pr"
    assert kwargs["new_value"]["number"] == 169
    assert kwargs["new_value"]["state"] == "MERGED"
    assert kwargs["new_value"]["resolver_source"] == "backend._repo.CODE_REPO"
    assert kwargs["event_id"] == f"blocked_by:{blocked_by._CODE_REPO}:pr:169:MERGED"


def test_audit_write_failure_is_inert(monkeypatch):
    """AC9(b): an audit write must never block, delay, or crash resolution."""
    import backend.audit_trail as audit_trail_module

    def boom(*a, **k):  # noqa: ARG001
        raise RuntimeError("state dir unwritable")

    monkeypatch.setattr(audit_trail_module, "get_audit_trail", boom)

    # Must not raise even though get_audit_trail() itself blows up.
    blocked_by._emit_cleared_audit("pr", 169, "MERGED", blocked_by._CODE_REPO, "backend._repo.CODE_REPO")

    payload = {"data": {"code": {"p169": {"state": "MERGED"}}}}
    _capture_query(monkeypatch, payload, returncode=0)
    r = blocked_by.BlockerResolver()
    assert r.unresolved(["#169"]) == []


def test_module_importable_without_state_dir():
    """AC9(c): the module import itself must never require a state dir, and
    must not pull in backend.state_paths just by being imported."""
    env = {k: v for k, v in os.environ.items() if k != "AUTONOMOUS_TEAM_STATE_DIR"}
    proc = subprocess.run(
        [sys.executable, "-c", "import backend.blocked_by, sys; assert 'backend.state_paths' not in sys.modules"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
