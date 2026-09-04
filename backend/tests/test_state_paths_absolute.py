"""AUTONOMOUS_TEAM_STATE_DIR must be absolute, and every stats/audit consumer
must go through the one resolver in backend/state_paths.py (D#1967).

The bug this pins: `_state_dir()` returned `Path(env)` unmodified, so a
relative — or set-but-empty — state dir resolved against the process cwd.
Run from the repo root, that put state.db, stats.duckdb, audit.jsonl and
blackboard/ straight into the checkout, untracked and unignored.

Both directions are tested on purpose. A check that is too loose changes
nothing; one that is too strict breaks every caller that passes a legitimate
absolute or `~`-relative path.
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

# Reference the exception class through the module at assert time, never bound
# here at import time. The suite imports backend/ modules under more than one
# name and reloads several of them, so a name bound now can end up being a
# different class object than the one the resolver actually raises.


# ---------------------------------------------------------------------------
# Rejected: relative values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["relstate", ".", "..", "./relstate", "a/b/c", ""])
def test_relative_state_dir_is_rejected(value, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", value)
    monkeypatch.delenv("STATS_DB_PATH", raising=False)
    for name in ("STATE_DIR", "STATE_DB", "STATS_DB", "AUDIT_LOG", "BLACKBOARD_DIR"):
        with pytest.raises(state_paths.RelativeStateDirError) as exc:
            getattr(state_paths, name)
        assert "AUTONOMOUS_TEAM_STATE_DIR" in str(exc.value)


def test_relative_state_dir_writes_nothing_to_cwd(tmp_path):
    """The whole point: a relative value must not quietly create files here.

    Runs in a subprocess with cwd set to a scratch dir, so a regression that
    reinstates the old behaviour shows up as real files on disk.
    """
    env = dict(os.environ)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = "relstate"
    env.pop("STATS_DB_PATH", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["PYTHONPATH"] = str(_REPO_ROOT)

    proc = subprocess.run(
        [sys.executable, "-c",
         "from backend import state_paths as sp; sp.ensure_state_dir()"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    assert "AUTONOMOUS_TEAM_STATE_DIR" in proc.stderr
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Accepted: absolute values, and the spellings that become absolute
# ---------------------------------------------------------------------------

def test_absolute_state_dir_passes_through(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STATS_DB_PATH", raising=False)
    assert state_paths.STATE_DIR == tmp_path
    assert state_paths.STATE_DB == tmp_path / "state.db"
    assert state_paths.STATS_DB == tmp_path / "stats.duckdb"
    assert state_paths.AUDIT_LOG == tmp_path / "audit.jsonl"
    assert state_paths.BLACKBOARD_DIR == tmp_path / "blackboard"


def test_tilde_state_dir_is_expanded(monkeypatch):
    """`~/x` is not absolute until expanded — expand, don't reject."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", "~/some-state-dir")
    monkeypatch.delenv("STATS_DB_PATH", raising=False)
    assert state_paths.STATE_DIR == Path.home() / "some-state-dir"
    assert state_paths.STATE_DIR.is_absolute()


def test_stats_db_path_override_still_wins(tmp_path, monkeypatch):
    """STATS_DB_PATH is an explicit per-value override and is not STATE_DIR-derived."""
    monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
    monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "x.duckdb"))
    assert state_paths.STATS_DB == tmp_path / "x.duckdb"


# ---------------------------------------------------------------------------
# One resolver per file
# ---------------------------------------------------------------------------

def test_reader_and_writer_agree_on_stats_db(tmp_path, monkeypatch):
    """agent_run_reader and agent_run_tracker used to hold byte-identical copies
    of the same resolver with nothing checking they stayed in step."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STATS_DB_PATH", raising=False)
    from backend.agent_run_reader import _db_path as read_path
    from backend.agent_run_tracker import _db_path as write_path
    assert read_path() == write_path() == tmp_path / "stats.duckdb"


def test_fresh_scratch_dir_beats_legacy_audit_path(tmp_path, monkeypatch):
    """A still-empty scratch state dir must win.

    The old `_audit_log_path()` guarded its scratch branch with
    `if p.exists()`, so pointing the env var at a clean directory — the exact
    thing reviewers are told to do for isolation — silently lost to the
    in-repo legacy log.
    """
    legacy_dir = _REPO_ROOT / ".autonomous-team"
    assert legacy_dir.exists(), "expected an in-repo .autonomous-team/ to shadow"

    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    from backend.agent_run_tracker import _audit_log_path
    resolved = _audit_log_path()
    assert resolved == tmp_path / "audit.jsonl"
    assert not resolved.exists(), "scratch dir is empty — the point of the test"


@pytest.mark.parametrize("module_name,attr", [
    ("backend.agent_run_reader", "_db_path"),
    ("backend.agent_run_tracker", "_db_path"),
    ("backend.stats_reader", "_db_path"),
    ("backend.stats_writer", "_db_path"),
    ("backend.stats_freshness_watchdog", "_db_path"),
    ("backend.stats.anomaly_detector", "_db_path"),
    ("backend.stats.sdk_vs_cc", "_db_path"),
    ("backend.stats.scheduled_jobs", "_db_path"),
    ("backend.rpc.stats_pre_write_burn", "_db_path"),
])
def test_every_stats_db_path_resolves_under_state_dir(module_name, attr, tmp_path, monkeypatch):
    """Every surviving `_db_path()` is a delegation, not a second implementation."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STATS_DB_PATH", raising=False)
    import importlib
    mod = importlib.import_module(module_name)
    assert Path(getattr(mod, attr)()) == tmp_path / "stats.duckdb"


def test_no_second_spelling_of_stats_or_audit_paths():
    """AC-8: the only place these two filenames are joined to a state dir is
    state_paths.py itself."""
    import re
    pattern = re.compile(
        r"(\.autonomous-team|\.fulcrumaxe-state)[^\n]*(stats\.duckdb|audit\.jsonl)"
    )
    offenders = []
    for path in sorted((_REPO_ROOT / "backend").rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT)
        if rel.parts[1] == "tests" or path.name == "state_paths.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert offenders == [], "second spelling of a state path:\n" + "\n".join(offenders)
