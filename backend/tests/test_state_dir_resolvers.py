"""Every live AUTONOMOUS_TEAM_STATE_DIR resolver must reach the SAME decision
backend/state_paths.py reaches, for every input class — including raising
where state_paths raises (D#2183).

Before this Discussion, backend/worktree_state_watcher.py and backend/
corpus_drift/claims/dial_directive_emission.py each read the variable
themselves, disagreeing with state_paths on relative input, an empty
string, and (worktree_state_watcher only) an unset value under pytest. A
test that only asserts resolver-A == resolver-B would pass when all three
are wrong together — the exact defect this Discussion measured — so every
case below asserts against the LITERAL expected value/exception, not
against another resolver.

Each case runs in a separate subprocess, matching the PM's measurement
methodology: state resolution is call-time (not cached), but a subprocess
also lets the "no pytest" cases run genuinely outside pytest, which cannot
be faked by deleting PYTEST_CURRENT_TEST from inside a pytest worker.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import state_paths  # noqa: E402
from backend.worktree_state_watcher import _state_dir as watcher_state_dir  # noqa: E402
from backend.corpus_drift.claims.dial_directive_emission import _state_dir as dial_state_dir  # noqa: E402

# Reference the exception classes through the module at assert time, never
# bound to a name here at import time: backend/tests/test_dial_registry.py
# reloads backend.state_paths elsewhere in this same pytest process, which
# rebinds RelativeStateDirError/UnsandboxedStatePathError to new class
# objects — a name imported here at collection time would silently stop
# matching what the resolvers raise after that reload runs.

_RESOLVER_SNIPPETS = {
    "state_paths": "from backend.state_paths import STATE_DIR as v",
    "worktree_state_watcher": "from backend.worktree_state_watcher import _state_dir; v = _state_dir()",
    "dial_directive_emission": (
        "from backend.corpus_drift.claims.dial_directive_emission import _state_dir; v = _state_dir()"
    ),
}

_OUTCOME_MARKER = "RESULT:"


def _run_resolver(resolver: str, env_overrides: dict[str, str | None]) -> tuple[str, str]:
    """Run *resolver* in a fresh subprocess with *env_overrides* applied.

    Returns (kind, detail): kind is "ok" (detail = resolved path string) or
    an exception class name (detail = str(exception)).
    """
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env["PYTHONPATH"] = str(_REPO_ROOT)

    snippet = (
        f"{_RESOLVER_SNIPPETS[resolver]}\n"
        f"print({_OUTCOME_MARKER!r} + 'ok:' + str(v))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_REPO_ROOT),
    )
    for line in proc.stdout.splitlines():
        if line.startswith(_OUTCOME_MARKER):
            _, _, rest = line.partition(_OUTCOME_MARKER)
            kind, _, detail = rest.partition(":")
            return kind, detail
    # No RESULT line: the process raised uncaught. Identify the exception
    # class from the traceback's last "ExceptionType: message" line.
    tb_lines = [l for l in proc.stderr.splitlines() if l and not l.startswith(" ")]
    last = tb_lines[-1] if tb_lines else proc.stderr
    exc_name, _, detail = last.partition(":")
    # Python prints the fully-qualified class name when it's not a builtin
    # (e.g. "backend.state_paths.RelativeStateDirError") — take the last
    # dotted component so this compares equal across the three resolvers
    # regardless of which module's copy of the class raised it.
    return exc_name.strip().rsplit(".", 1)[-1], detail.strip()


_RESOLVERS = ["state_paths", "worktree_state_watcher", "dial_directive_emission"]

# (label, env_overrides, expected_kind, expected_detail_check)
# expected_detail_check is None (don't check), or a callable(str) -> bool.
_HOME = str(Path.home() / ".autonomous-forever-state")
_TILDE_EXPANDED = str(Path.home() / "some-state")

_CASES = [
    (
        "unset, no pytest",
        {"AUTONOMOUS_TEAM_STATE_DIR": None, "PYTEST_CURRENT_TEST": None},
        "ok",
        lambda d: d == _HOME,
    ),
    (
        "unset, under pytest",
        {"AUTONOMOUS_TEAM_STATE_DIR": None, "PYTEST_CURRENT_TEST": "fake::test"},
        "UnsandboxedStatePathError",
        None,
    ),
    (
        "empty string",
        {"AUTONOMOUS_TEAM_STATE_DIR": "", "PYTEST_CURRENT_TEST": None},
        "RelativeStateDirError",
        None,
    ),
    (
        "relative path",
        {"AUTONOMOUS_TEAM_STATE_DIR": "relative/state", "PYTEST_CURRENT_TEST": None},
        "RelativeStateDirError",
        None,
    ),
    (
        "dot",
        {"AUTONOMOUS_TEAM_STATE_DIR": ".", "PYTEST_CURRENT_TEST": None},
        "RelativeStateDirError",
        None,
    ),
    (
        "tilde-relative",
        {"AUTONOMOUS_TEAM_STATE_DIR": "~/some-state", "PYTEST_CURRENT_TEST": None},
        "ok",
        lambda d: d == _TILDE_EXPANDED,
    ),
]


@pytest.mark.parametrize("resolver", _RESOLVERS)
@pytest.mark.parametrize("label,env_overrides,expected_kind,expected_check", _CASES, ids=[c[0] for c in _CASES])
def test_resolver_matches_state_paths_literal_expectation(
    resolver, label, env_overrides, expected_kind, expected_check
):
    kind, detail = _run_resolver(resolver, env_overrides)
    assert kind == expected_kind, (
        f"{resolver} / {label}: expected {expected_kind!r}, got {kind!r} ({detail!r})"
    )
    if expected_check is not None:
        assert expected_check(detail), f"{resolver} / {label}: unexpected value {detail!r}"


def test_worktree_state_watcher_bypass_regression(monkeypatch):
    """D#2183 item 3 — the sharpest defect, pinned directly (no subprocess).

    Before this fix, worktree_state_watcher._state_dir() returned the
    PRODUCTION state dir when AUTONOMOUS_TEAM_STATE_DIR was unset under
    pytest, instead of raising like state_paths does. pytest always sets
    PYTEST_CURRENT_TEST, so this test IS the "under pytest" condition —
    no subprocess needed to fake it.
    """
    monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
    assert os.environ.get("PYTEST_CURRENT_TEST"), "expected pytest to have set PYTEST_CURRENT_TEST"
    with pytest.raises(state_paths.UnsandboxedStatePathError):
        watcher_state_dir()


def test_dial_directive_emission_bypass_not_reintroduced(monkeypatch):
    """dial_directive_emission never had the pytest-bypass hole, but it
    also must not gain one now that it delegates to state_paths.
    """
    monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
    with pytest.raises(state_paths.UnsandboxedStatePathError):
        dial_state_dir()


def test_worktree_scan_stops_before_the_walk(tmp_path, monkeypatch):
    """D#2183 item 4 — a relative state dir must exit non-zero with a
    single-line diagnostic and must never reach file_bug()/file_discussion(),
    i.e. it must never file a spurious [Bug] Discussion.
    """
    from backend import worktree_state_watcher as watcher

    manifest = {"entries": [{"in_repo": "audit.jsonl", "external": "audit.jsonl", "type": "file"}]}
    registry = [{"worktree_id": "fixture-1", "path": str(tmp_path), "status": "active"}]

    monkeypatch.setattr(watcher, "load_manifest", lambda: manifest["entries"])
    monkeypatch.setattr(watcher, "load_worktrees", lambda: registry)

    called = {"file_bug": False}

    def _spy_file_bug(*args, **kwargs):
        called["file_bug"] = True

    monkeypatch.setattr(watcher, "file_bug", _spy_file_bug)
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", "relative/state")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        watcher.scan(dry_run=True)

    assert exc_info.value.code != 0
    assert called["file_bug"] is False


def test_corpus_drift_audit_degrades_instead_of_crashing(tmp_path):
    """D#2183 item 5 — a relative state dir must not crash the audit; the
    dial_directive_emission claim must come back n/a with an
    'evaluator error:' evidence prefix, and other claims must still run.
    """
    env = dict(os.environ)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = "relative/state"
    env["PYTHONPATH"] = str(_REPO_ROOT)

    proc = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "corpus-drift-audit.py"),
            "--since", "30d",
            "--output-dir", str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "global.dial_directive_emission" in proc.stdout
    dial_lines = [l for l in proc.stdout.splitlines() if "global.dial_directive_emission" in l]
    assert dial_lines, proc.stdout
    assert "[n/a" in dial_lines[0], dial_lines[0]
    # The snapshot write must be skipped, not attempted against the scattered
    # relative path.
    assert "Snapshot skipped" in proc.stderr
