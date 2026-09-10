"""Tests for backend/stats_reader.py — summary() lock-conflict tolerance.

D#2524 PR-a item 5: backend/stats_reader.summary() invoked while a second
process holds read_only=False on the same scratch DB must return the metric
rows rather than propagating duckdb.IOException. This is the reader-side half
of the atomicity fix — see test_stats_writer.py::TestRecordManyAtomicity for
the write-side half.

Isolation: STATS_DB_PATH env var is monkeypatched to a tmp_path file, same
convention as test_stats_writer.py. The real ~/.autonomous-forever-state/
stats.duckdb is NEVER touched.

Run with:
    python3 -m pytest backend/tests/test_stats_reader.py -v
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Skip entire module if duckdb is not installed
try:
    import duckdb as _duckdb_mod
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect stats_writer/stats_reader to a fresh temp DuckDB.

    Mirrors test_stats_writer.py's fixture: clears AUTONOMOUS_TEAM_STATE_DIR
    so path #2 in _db_path() does not fall back to the production state dir.
    """
    db_file = tmp_path / "test_stats.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(db_file))
    monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
    yield db_file


# Import under test after the fixture module is loaded so monkeypatching applies.
import backend.stats_reader as sr  # noqa: E402
import backend.stats_writer as sw  # noqa: E402


# ---------------------------------------------------------------------------
# Real cross-process lock-conflict helper — same pattern as
# test_stats_writer.py::_start_holder. A mock or same-process second
# connection does not exercise the real mechanism (D#2149).
# ---------------------------------------------------------------------------

_HOLDER_SCRIPT_TMPL = textwrap.dedent("""\
    import sys, time
    sys.path.insert(0, {root!r})
    import duckdb
    conn = duckdb.connect({db!r}, read_only={read_only!r})
    with open({ready!r}, "w") as fh:
        fh.write("acquired")
    time.sleep({hold_s!r})
    conn.close()
""")


def _start_holder(tmp_path: Path, db_file: Path, *, read_only: bool, hold_s: float):
    """Spawn a subprocess that holds a real DuckDB lock on db_file for hold_s.

    Blocks until the holder confirms acquisition via a ready-file. Returns
    the Popen handle — caller must terminate()/wait() it.
    """
    if not db_file.exists():
        _duckdb_mod.connect(str(db_file)).close()

    ready_file = tmp_path / f"reader_holder_ready_{_time.monotonic_ns()}.flag"
    script = _HOLDER_SCRIPT_TMPL.format(
        root=str(_REPO_ROOT), db=str(db_file), read_only=read_only,
        ready=str(ready_file), hold_s=hold_s,
    )
    script_path = tmp_path / f"reader_holder_{_time.monotonic_ns()}.py"
    script_path.write_text(script)

    proc = subprocess.Popen([sys.executable, str(script_path)])
    deadline = _time.monotonic() + 10.0
    while not ready_file.exists():
        if proc.poll() is not None:
            raise RuntimeError(f"holder process exited early (rc={proc.returncode}) before acquiring the lock")
        if _time.monotonic() > deadline:
            proc.kill()
            raise RuntimeError("holder process never signaled that it acquired the lock")
        _time.sleep(0.01)
    return proc


# ===========================================================================
# summary() — lock-conflict tolerance (D#2524 PR-a item 5)
# ===========================================================================


class TestSummaryLockTolerance:

    def test_summary_returns_rows_despite_concurrent_writer_holder(self, tmp_path, isolated_db):
        """Binding item 5: a real second process holds read_only=False on the
        scratch DB. summary() must return the actual metric rows -- not an
        empty result standing in for a swallowed exception, and not a raised
        duckdb.IOException -- once the holder releases within the retry
        budget."""
        fixed_ts = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        sw.record("probe_metric", 42.0, "count", ts=fixed_ts)

        holder = _start_holder(tmp_path, isolated_db, read_only=False, hold_s=0.4)
        try:
            result = sr.summary()
        finally:
            holder.wait(timeout=10)

        names = [row["name"] for row in result]
        assert "probe_metric" in names, (
            f"summary() must return the real rows once the writer releases, got: {result}"
        )
        probe = next(r for r in result if r["name"] == "probe_metric")
        assert probe["value"] == 42.0

    def test_summary_degrades_to_empty_list_when_holder_outlives_retry_budget(self, tmp_path, isolated_db):
        """Covers the Implementation Notes' 'widen _open_conn to the lock case,
        deliberately and narrowly' instruction: even when the writer never
        releases and the reader's bounded retry is exhausted, summary() must
        degrade to [] rather than letting a raw duckdb.IOException propagate
        out through the RPC layer (which is exactly the F3 mechanism: an
        exception surfacing as a dashboard read failure that looks identical
        to 'no data')."""
        sw.record("probe_metric", 1.0, "count")
        holder = _start_holder(tmp_path, isolated_db, read_only=False, hold_s=10.0)
        try:
            result = sr.summary()  # must not raise
            assert result == []
        finally:
            holder.terminate()
            holder.wait(timeout=10)
