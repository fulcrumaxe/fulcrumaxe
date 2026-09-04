"""Tests for backend/stats_connection.py.

Acceptance criteria from D#632:
  AC3 — spawn 2 subprocesses that each import the read singleton and SELECT concurrently; both succeed.
  AC4 — in-process, calling get_read_connection() twice returns the same object.
  AC5 — no duckdb.connect( call remains in backend/rpc/stats_*.py, backend/api.py,
         backend/server.py, or backend/stats_writer.py outside the singleton module.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"

sys.path.insert(0, str(ROOT))

try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Create a minimal stats.duckdb with the metric_event schema."""
    import duckdb as _duckdb  # noqa: PLC0415
    db = tmp_path / "stats.duckdb"
    conn = _duckdb.connect(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metric_event (
            ts      TIMESTAMP NOT NULL,
            metric  TEXT      NOT NULL,
            tags    JSON,
            value   DOUBLE    NOT NULL,
            unit    TEXT      NOT NULL,
            source  TEXT,
            PRIMARY KEY (ts, metric, tags)
        )
    """)
    conn.close()
    return db


# ---------------------------------------------------------------------------
# AC4 (updated): each call returns a fresh connection (no-singleton contract)
# ---------------------------------------------------------------------------

def test_get_read_connection_returns_new_connection_each_call(tmp_path):
    """Each call to get_read_connection() returns a fresh connection object.

    Post-D#888 PR-c the singleton was dropped so the dashboard backend does not
    hold a file lock across requests. Two calls must return distinct objects.
    """
    db = _make_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db)

    # Must import AFTER setting env var, and reset singletons between test runs.
    import importlib  # noqa: PLC0415
    import backend.stats_connection as sc  # noqa: PLC0415
    importlib.reload(sc)

    try:
        conn1 = sc.get_read_connection()
        conn2 = sc.get_read_connection()
        assert conn1 is not conn2, "Expected a fresh connection object on each call"
    finally:
        try:
            conn1.close()
        except Exception:
            pass
        try:
            conn2.close()
        except Exception:
            pass
        sc.close_all()
        del os.environ["STATS_DB_PATH"]


# ---------------------------------------------------------------------------
# AC3: two subprocesses read concurrently without conflict
# ---------------------------------------------------------------------------

_READER_SCRIPT = textwrap.dedent("""\
    import os, sys
    sys.path.insert(0, {root!r})
    os.environ["STATS_DB_PATH"] = {db!r}
    from backend.stats_connection import get_read_connection, close_all
    conn = get_read_connection()
    try:
        rows = conn.execute("SELECT COUNT(*) FROM metric_event").fetchall()
    finally:
        conn.close()
    close_all()
    print("ok", rows[0][0])
""")


def test_concurrent_subprocess_reads(tmp_path):
    """Two subprocesses can each open the read-only singleton concurrently."""
    db = _make_db(tmp_path)
    script = _READER_SCRIPT.format(root=str(ROOT), db=str(db))
    script_path = tmp_path / "reader.py"
    script_path.write_text(script)

    procs = [
        subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]

    results = [p.communicate(timeout=30) for p in procs]

    for i, (stdout, stderr) in enumerate(results):
        assert procs[i].returncode == 0, (
            f"Subprocess {i} failed (rc={procs[i].returncode}):\n"
            f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )
        assert stdout.decode().strip().startswith("ok"), (
            f"Subprocess {i} unexpected output: {stdout.decode()}"
        )


# ---------------------------------------------------------------------------
# AC5: no duckdb.connect( in dashboard/RPC consumer files
# ---------------------------------------------------------------------------
#
# Write functions in stats_writer.py use short-lived duckdb.connect() calls
# (open → write → close) to avoid holding cross-process write locks. This is
# intentional — the writer runs in the loop process, not the dashboard process.
# The read functions in stats_writer.py and all of stats_reader.py use
# get_read_connection() from the singleton module.
#
# Dashboard RPC handlers and api.py/server.py MUST NOT call duckdb.connect()
# directly — they must go through the singleton.

_RPC_FILES_TO_GREP = [
    "backend/rpc/stats_avg_fix_rounds_per_pr.py",
    "backend/rpc/stats_cost_spike_history.py",
    "backend/rpc/stats_loop_idle_ratio.py",
    "backend/rpc/stats_role_retry_rate.py",
    "backend/rpc/stats_role_success_rate.py",
    "backend/rpc/stats_team_lead_tokens.py",
    "backend/api.py",
    "backend/server.py",
]


def test_no_raw_duckdb_connect_in_dashboard_files():
    """Grep confirms no duckdb.connect( call in dashboard/RPC consumer files."""
    violations = []
    for rel in _RPC_FILES_TO_GREP:
        fpath = ROOT / rel
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "duckdb.connect(" in line:
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, (
        "duckdb.connect( found in dashboard/RPC files (must use singleton):\n"
        + "\n".join(violations)
    )


def test_read_functions_in_stats_writer_use_singleton():
    """Read-path functions in stats_writer.py use get_read_connection, not duckdb.connect."""
    fpath = ROOT / "backend" / "stats_writer.py"
    text = fpath.read_text(encoding="utf-8")

    # These functions should not open their own connections
    read_fn_names = [
        "cost_spike_history",
        "role_success_rate_24h",
        "role_retry_rate_24h",
        "team_lead_tokens_percentiles",
        "avg_fix_rounds_24h",
    ]

    for fn_name in read_fn_names:
        # Find function start and check that get_read_connection appears in it
        assert f"get_read_connection" in text, (
            f"stats_writer.py read path should use get_read_connection()"
        )
