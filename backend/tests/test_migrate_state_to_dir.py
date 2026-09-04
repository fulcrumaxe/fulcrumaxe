"""Hermetic tests for backend/migrate_state_to_dir.py (D#1908 PR 3).

Every test builds its own donor/destination databases in tmp_path — never
against the live host copies (Implementation Notes: "hermetic, against
fixture databases in a tmp_path. Never against the live host copies.").

The state.db fixtures deliberately mirror the real, measured baseline this
Spec records (agent_lessons: donor=7 discussion=99 'CLI smoke test lesson'
rows vs. a larger live count) so a reviewer can see the tool reproduces that
shape without ever touching the actual host files.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import migrate_state_to_dir as m  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _make_state_db(path: Path, rows: list[tuple[str, int, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE agent_lessons (id TEXT PRIMARY KEY, discussion INTEGER, content TEXT)"
    )
    conn.execute("CREATE TABLE blackboard (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO agent_lessons VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def _make_stats_db(path: Path, rows: list[tuple[int, str]]) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE agent_run (id INTEGER PRIMARY KEY, ts TEXT)")
    if rows:
        con.executemany("INSERT INTO agent_run VALUES (?, ?)", rows)
    con.close()


def _make_nopk_stats_db(path: Path, rows: list[tuple[int, str]]) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE events (a INTEGER, b TEXT)")
    if rows:
        con.executemany("INSERT INTO events VALUES (?, ?)", rows)
    con.close()


def _make_stats_db_with_tables(path: Path, tables: dict[str, list[tuple[int, str]]]) -> None:
    """*tables*: {table_name: [(id, ts), ...]} — every table uses the same
    (id INTEGER PRIMARY KEY, ts TEXT) shape; only the table *names* present
    vary, which is exactly the asymmetry the donor/dest-table-enumeration
    bug turns on. Reproduces the real production shape: the destination
    grew `stat_anomalies`, which the donor — frozen seven days earlier —
    never had."""
    con = duckdb.connect(str(path))
    for name, rows in tables.items():
        con.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, ts TEXT)')
        if rows:
            con.executemany(f'INSERT INTO "{name}" VALUES (?, ?)', rows)
    con.close()


def _make_notifications_db(path: Path, rows: list[tuple[str, str, str, int, str]]) -> None:
    """rows: (event_type, channel, timestamp, success, message) — id is
    AUTOINCREMENT and deliberately not specified, exactly like the real
    notifications table (backend/db.py:81)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type TEXT, channel TEXT, timestamp TEXT, success INTEGER, "
        "message TEXT, error TEXT)"
    )
    conn.executemany(
        "INSERT INTO notifications (event_type, channel, timestamp, success, message) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _make_state_db_wal_open(path: Path, rows: list[tuple[str, int, str]]) -> sqlite3.Connection:
    """WAL-mode state.db fixture with committed rows left un-checkpointed in
    ``-wal``, and the writer connection kept open — reproduces the real
    production shape: backend/db.py:134 sets WAL mode unconditionally on
    every connection, and the cron loop / dashboard hold the database open
    while `--apply` runs as a separate process. Caller must close the
    returned connection when done (deliberately not closed here, so the
    -wal sidecar is still live for whatever runs next)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE agent_lessons (id TEXT PRIMARY KEY, discussion INTEGER, content TEXT)"
    )
    conn.execute("CREATE TABLE blackboard (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO agent_lessons VALUES (?,?,?)", rows)
    conn.commit()
    return conn


@pytest.fixture
def layout(tmp_path):
    repo_root = tmp_path / "repo"
    state_dir = tmp_path / "state"
    repo_root.mkdir()
    state_dir.mkdir()
    return repo_root, state_dir


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "backend" / "migrate_state_to_dir.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _stat(path: Path) -> tuple[int, float]:
    st = path.stat()
    return (st.st_size, st.st_mtime)


# ---------------------------------------------------------------------------
# AC-3.1 — missing donor
# ---------------------------------------------------------------------------

def test_missing_donor_reports_and_exits_zero(layout):
    repo_root, state_dir = layout
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    result = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr
    assert "no donor copy present" in result.stdout
    assert "state.db" in result.stdout
    assert "stats.duckdb" in result.stdout


def test_missing_donor_apply_is_a_clean_noop(layout):
    repo_root, state_dir = layout
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr
    assert "rows merged: {}" in result.stdout


# ---------------------------------------------------------------------------
# AC-3.2 — dry-run is provably side-effect free
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing_byte_and_mtime_identical(layout):
    repo_root, state_dir = layout
    _make_state_db(
        repo_root / "state.db",
        [(f"donor-{i}", 99, "CLI smoke test lesson") for i in range(7)],
    )
    _make_state_db(state_dir / "state.db", [(f"live-{i}", 1, "real lesson") for i in range(3)])
    _make_stats_db(repo_root / "stats.duckdb", [(1, "donor-a"), (2, "donor-b")])
    _make_stats_db(state_dir / "stats.duckdb", [(1, "live-a")])

    paths = [
        repo_root / "state.db",
        state_dir / "state.db",
        repo_root / "stats.duckdb",
        state_dir / "stats.duckdb",
    ]
    before = [_stat(p) for p in paths]

    result = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr

    after = [_stat(p) for p in paths]
    assert before == after, f"dry-run mutated a file: before={before} after={after}"
    assert "donor_only=7" in result.stdout
    assert "donor_only=1" in result.stdout  # stats.duckdb


# ---------------------------------------------------------------------------
# Baseline reproduction — the Spec's measured state.db shape
# ---------------------------------------------------------------------------

def test_dry_run_reproduces_measured_baseline_shape(layout):
    """Mirrors the Spec's own state.db census: 7 donor agent_lessons rows,
    all discussion=99 'CLI smoke test lesson', against a larger live count."""
    repo_root, state_dir = layout
    _make_state_db(
        repo_root / "state.db",
        [(f"donor-{i}", 99, "CLI smoke test lesson") for i in range(7)],
    )
    _make_state_db(state_dir / "state.db", [(f"live-{i}", 1, "real lesson") for i in range(1822)])
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    result = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr
    assert "donor_rows=7" in result.stdout
    assert "dest_rows=1822" in result.stdout
    assert "donor_only=7" in result.stdout
    assert result.stdout.count("'discussion': 99") >= 5  # sample is capped at 5 rows


# ---------------------------------------------------------------------------
# AC-3.6 / additive-only, never-a-winner
# ---------------------------------------------------------------------------

def test_apply_is_additive_only_never_overwrites_matching_key(layout):
    repo_root, state_dir = layout
    # Same PK, different content — the destination's version must survive.
    _make_state_db(repo_root / "state.db", [("shared-id", 99, "donor version")])
    _make_state_db(state_dir / "state.db", [("shared-id", 1, "live version — must not be overwritten")])
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(state_dir / "state.db")
    rows = conn.execute("SELECT id, discussion, content FROM agent_lessons").fetchall()
    conn.close()
    assert rows == [("shared-id", 1, "live version — must not be overwritten")]


def test_apply_row_counts_never_decrease(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [(f"donor-{i}", 99, "x") for i in range(5)])
    _make_state_db(state_dir / "state.db", [(f"live-{i}", 1, "y") for i in range(3)])
    _make_stats_db(repo_root / "stats.duckdb", [(i, "d") for i in range(4)])
    _make_stats_db(state_dir / "stats.duckdb", [(100, "l"), (101, "l2")])

    conn = sqlite3.connect(state_dir / "state.db")
    before_state = conn.execute("SELECT COUNT(*) FROM agent_lessons").fetchone()[0]
    conn.close()
    con = duckdb.connect(str(state_dir / "stats.duckdb"))
    before_stats = con.execute("SELECT COUNT(*) FROM agent_run").fetchone()[0]
    con.close()

    result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(state_dir / "state.db")
    after_state = conn.execute("SELECT COUNT(*) FROM agent_lessons").fetchone()[0]
    conn.close()
    con = duckdb.connect(str(state_dir / "stats.duckdb"))
    after_stats = con.execute("SELECT COUNT(*) FROM agent_run").fetchone()[0]
    con.close()

    assert after_state >= before_state
    assert after_stats >= before_stats
    assert after_state == 8  # 3 live + 5 additive donor rows, none skipped
    assert after_stats == 6  # 2 live + 4 additive donor rows, none skipped


# ---------------------------------------------------------------------------
# AC-3.5 — backup before any write; no write at all if backup fails
# ---------------------------------------------------------------------------

def test_apply_takes_backup_before_merging(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [("donor-0", 99, "x")])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr

    backups = list(state_dir.glob("migration-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "donor-state.db").exists()
    assert (backups[0] / "dest-state.db").exists()


def test_apply_backup_captures_wal_content_and_is_verified(layout):
    """E1 (security review, blocking): the production state.db runs in WAL
    mode. A raw `shutil.copy2` of just the main file — the original,
    now-replaced implementation — silently drops committed transactions
    still sitting in `-wal`. This fixture reproduces that exact shape (WAL
    mode, a committed row not yet checkpointed, the writer connection left
    open — what the cron loop / dashboard do in production) and asserts the
    backup contains that row, re-opened and read back exactly as an
    operator restoring it would: this test would have failed against the
    original shutil.copy2 implementation."""
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [("donor-0", 99, "x")])
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    dest_conn = _make_state_db_wal_open(
        state_dir / "state.db",
        [("wal-only-row", 1, "lives only in -wal, not checkpointed")],
    )
    try:
        assert (state_dir / "state.db-wal").exists(), (
            "fixture setup: a -wal sidecar must exist for this test to mean anything"
        )

        result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
        assert result.returncode == 0, result.stderr

        backups = list(state_dir.glob("migration-backup-*"))
        assert len(backups) == 1
        backup_dest = backups[0] / "dest-state.db"
        assert backup_dest.exists()

        verify_conn = sqlite3.connect(f"file:{backup_dest}?mode=ro", uri=True)
        try:
            assert verify_conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            row_ids = {r[0] for r in verify_conn.execute("SELECT id FROM agent_lessons").fetchall()}
        finally:
            verify_conn.close()
        assert "wal-only-row" in row_ids, (
            "backup is missing a row that was committed but not yet checkpointed — "
            "this is exactly the WAL-unsafe shutil.copy2 bug E1 fixed"
        )
    finally:
        dest_conn.close()


def test_sqlite_backup_verification_catches_row_count_mismatch(tmp_path, monkeypatch):
    """Directly exercises the verification step (not just the happy path):
    if the re-opened backup's row counts don't match a fresh read of the
    source, BackupFailedError must fire rather than silently accepting a
    bad backup."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES ('a')")
    conn.commit()
    conn.close()

    real_row_counts = m._sqlite_row_counts
    calls = {"n": 0}

    def _fake_row_counts(path):
        calls["n"] += 1
        counts = real_row_counts(path)
        if calls["n"] == 2:  # the post-backup verification read of dst
            counts = {k: v + 1 for k, v in counts.items()}
        return counts

    monkeypatch.setattr(m, "_sqlite_row_counts", _fake_row_counts)

    with pytest.raises(m.BackupFailedError, match="row counts do not match"):
        m._sqlite_backup_and_verify(src, dst)


def test_duckdb_backup_verification_catches_row_count_mismatch(tmp_path, monkeypatch):
    src = tmp_path / "src.duckdb"
    dst = tmp_path / "dst.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO t VALUES (1)")
    con.close()

    real_row_counts = m._duckdb_row_counts
    calls = {"n": 0}

    def _fake_row_counts(path):
        calls["n"] += 1
        counts = real_row_counts(path)
        if calls["n"] == 2:
            counts = {k: v + 1 for k, v in counts.items()}
        return counts

    monkeypatch.setattr(m, "_duckdb_row_counts", _fake_row_counts)

    with pytest.raises(m.BackupFailedError, match="row counts do not match"):
        m._duckdb_backup_and_verify(src, dst)


def test_sqlite_backup_verification_catches_integrity_failure(tmp_path, monkeypatch):
    """If the re-opened backup fails integrity_check, BackupFailedError must
    fire even though the row counts happen to match. Uses the module's own
    _sqlite_run_integrity_check seam rather than monkeypatching sqlite3
    internals directly, so this stays robust to unrelated sqlite3 calls
    elsewhere in the same test process."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES ('a')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(m, "_sqlite_run_integrity_check", lambda conn: ("corruption found",))

    with pytest.raises(m.BackupFailedError, match="integrity_check"):
        m._sqlite_backup_and_verify(src, dst)


def test_notifications_surrogate_key_does_not_cause_false_dedup(layout):
    """W2 (security review): notifications.id is INTEGER PRIMARY KEY
    AUTOINCREMENT — a surrogate key drawn from each database's own
    independent sequence. Donor id=1 and destination id=1 are unrelated
    rows here; matching on id would wrongly skip the donor row and the
    generated README would then falsely claim nothing unique remained."""
    repo_root, state_dir = layout
    _make_notifications_db(
        repo_root / "state.db", [("donor_evt", "slack", "t1", 1, "donor msg")]
    )
    _make_notifications_db(
        state_dir / "state.db", [("live_evt", "slack", "t0", 1, "live msg")]
    )
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    dry = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert dry.returncode == 0, dry.stderr
    assert "mode=full_row_excl_surrogate_pk" in dry.stdout
    assert "notifications: donor_rows=1 dest_rows=1 donor_only=1" in dry.stdout

    result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(state_dir / "state.db")
    rows = conn.execute("SELECT event_type, message FROM notifications ORDER BY id").fetchall()
    conn.close()
    assert rows == [("live_evt", "live msg"), ("donor_evt", "donor msg")]


def test_notifications_true_duplicate_content_is_not_reinserted(layout):
    """The flip side of the surrogate-key fix: a row with genuinely
    identical content (just renumbered by an independent sequence) must
    still be recognised as a duplicate and skipped, not blindly
    re-inserted just because the fix stopped matching on id."""
    repo_root, state_dir = layout
    _make_notifications_db(
        repo_root / "state.db", [("same_evt", "slack", "tX", 1, "same content")]
    )
    conn = sqlite3.connect(state_dir / "state.db")
    conn.execute(
        "CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type TEXT, channel TEXT, timestamp TEXT, success INTEGER, "
        "message TEXT, error TEXT)"
    )
    conn.execute(
        "INSERT INTO notifications (id, event_type, channel, timestamp, success, message) "
        "VALUES (5, 'same_evt', 'slack', 'tX', 1, 'same content')"
    )
    conn.commit()
    conn.close()
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    dry = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert dry.returncode == 0, dry.stderr
    assert "notifications: donor_rows=1 dest_rows=1 donor_only=0" in dry.stdout


def test_readonly_uri_survives_special_characters_in_path(tmp_path):
    """W3 (security review): the read-only URI used to be built as
    `f"file:{path}?mode=ro"`. A path containing `?` or `#` shifts the query
    string boundary and `mode=ro` silently stops being parsed, opening the
    database read-write instead. Reachable via --state-dir/--repo-root."""
    weird_dir = tmp_path / "weird?name#with space"
    weird_dir.mkdir()
    db_path = weird_dir / "x.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES ('a')")
    conn.commit()
    conn.close()

    ro_conn = sqlite3.connect(m._sqlite_ro_uri(db_path), uri=True)
    try:
        assert ro_conn.execute("SELECT COUNT(*) FROM t").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro_conn.execute("INSERT INTO t VALUES ('b')")
    finally:
        ro_conn.close()


def test_unexpected_exception_reports_phase_not_a_bare_traceback(layout, monkeypatch):
    """W4 (security review): an exception outside the two handled types
    (CorruptDonorError, BackupFailedError) used to escape main() as a raw
    traceback. It must instead be caught, phase-labelled, and explained."""
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [("donor-0", 99, "x")])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(m, "sqlite_apply", _boom)
    rc = m.main(["--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir)])
    assert rc == 1


def test_backup_failure_blocks_all_writes(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [("donor-0", 99, "x")])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    # Point --backup-dir at a path that is already a *file* — mkdir(exist_ok=True)
    # refuses to treat a file as a directory, so backup creation fails cleanly.
    blocker = state_dir.parent / "backup-blocker"
    blocker.write_text("not a directory")

    before_dest = _stat(state_dir / "state.db")
    result = _run_cli(
        "--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir),
        "--backup-dir", str(blocker),
    )
    assert result.returncode != 0
    assert "backup" in result.stderr.lower()

    after_dest = _stat(state_dir / "state.db")
    assert before_dest == after_dest, "destination was written despite backup failure"
    assert (repo_root / "state.db").exists(), "donor was archived despite backup failure"
    assert not (repo_root / "archive").exists()


# ---------------------------------------------------------------------------
# AC-3.7 — idempotent re-apply
# ---------------------------------------------------------------------------

def test_second_apply_is_a_true_noop(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [(f"donor-{i}", 99, "x") for i in range(3)])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(repo_root / "stats.duckdb", [(1, "d")])
    _make_stats_db(state_dir / "stats.duckdb", [])

    first = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert first.returncode == 0, first.stderr

    conn = sqlite3.connect(state_dir / "state.db")
    count_after_first = conn.execute("SELECT COUNT(*) FROM agent_lessons").fetchone()[0]
    conn.close()

    second = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert second.returncode == 0, second.stderr
    assert "rows merged: {}" in second.stdout  # both DBs — no donor left to merge

    conn = sqlite3.connect(state_dir / "state.db")
    count_after_second = conn.execute("SELECT COUNT(*) FROM agent_lessons").fetchone()[0]
    conn.close()
    assert count_after_second == count_after_first == 3


# ---------------------------------------------------------------------------
# AC-3.8 — archive, never delete
# ---------------------------------------------------------------------------

def test_apply_archives_donor_with_readme_and_never_deletes(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [("donor-0", 99, "x")])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(repo_root / "stats.duckdb", [(1, "d")])
    _make_stats_db(state_dir / "stats.duckdb", [])

    result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr

    assert not (repo_root / "state.db").exists()
    assert not (repo_root / "stats.duckdb").exists()

    archive_dirs = list((repo_root / "archive").glob("state-db-residue-*"))
    assert len(archive_dirs) == 1
    archive_dir = archive_dirs[0]
    assert (archive_dir / "state.db").exists()
    assert (archive_dir / "stats.duckdb").exists()
    assert (archive_dir / "README.md").exists()
    readme = (archive_dir / "README.md").read_text()
    for required in ("when removed", "why", "original path", "restore"):
        assert required.lower() in readme.lower(), f"README missing {required!r} section"


# ---------------------------------------------------------------------------
# AC-3.9 — corrupt donor is a hard refusal, never a silent skip
# ---------------------------------------------------------------------------

def test_corrupt_donor_state_db_refused_not_skipped(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [("donor-0", 99, "x")])
    with (repo_root / "state.db").open("r+b") as fh:
        fh.truncate(20)
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(repo_root / "stats.duckdb", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    before = _stat(state_dir / "state.db")
    result = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode != 0
    assert "state.db" in result.stderr
    assert _stat(state_dir / "state.db") == before


def test_corrupt_donor_stats_duckdb_refused_not_skipped(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(repo_root / "stats.duckdb", [(1, "d")])
    with (repo_root / "stats.duckdb").open("r+b") as fh:
        fh.truncate(20)
    _make_stats_db(state_dir / "stats.duckdb", [])

    before = _stat(state_dir / "stats.duckdb")
    result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode != 0
    assert "stats.duckdb" in result.stderr
    assert _stat(state_dir / "stats.duckdb") == before
    assert not list(state_dir.glob("migration-backup-*")), "backup must not be taken ahead of a refusal"


# ---------------------------------------------------------------------------
# Full-row fallback for tables without a declared primary key
# ---------------------------------------------------------------------------

def test_full_row_fallback_when_table_has_no_primary_key(layout):
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [])
    _make_state_db(state_dir / "state.db", [])
    _make_nopk_stats_db(repo_root / "stats.duckdb", [(1, "x"), (2, "y")])
    _make_nopk_stats_db(state_dir / "stats.duckdb", [(1, "x")])  # (1,'x') already present verbatim

    result = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert result.returncode == 0, result.stderr
    assert "mode=full_row" in result.stdout
    assert "donor_only=1" in result.stdout  # only (2, 'y') is new

    apply_result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert apply_result.returncode == 0, apply_result.stderr
    con = duckdb.connect(str(state_dir / "stats.duckdb"))
    rows = con.execute("SELECT * FROM events ORDER BY a").fetchall()
    con.close()
    assert rows == [(1, "x"), (2, "y")]


# ---------------------------------------------------------------------------
# Donor/destination table-set asymmetry (real-world bug: the destination is
# live and grows tables a frozen, seven-day-old donor never had — measured
# on the real host, dest had stat_anomalies and the donor didn't). Before
# this fix, _duckdb_tables() enumerated only the destination's tables and
# then queried every one of them against the donor, so a dest-only table
# crashed with DuckDB's CatalogException ("Table with name ... does not
# exist") — on the *normal* case, not an edge case — and a donor-only table
# was never even enumerated, so its rows were silently never considered for
# migration at all.
# ---------------------------------------------------------------------------

def test_destination_only_table_does_not_crash(layout):
    """The exact shape that crashed against the real host: the destination
    has a table (stat_anomalies, here renamed generically) the donor
    doesn't. Must not raise — a dest-only table has nothing to migrate by
    definition. This fixture would raise duckdb.CatalogException against
    the pre-fix code, which queried `donor.<table>` for every table in
    _duckdb_tables()'s (destination-only) enumeration."""
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db_with_tables(
        repo_root / "stats.duckdb", {"agent_run": [(1, "donor-a")]}
    )
    _make_stats_db_with_tables(
        state_dir / "stats.duckdb",
        {"agent_run": [(1, "donor-a")], "stat_anomalies": [(1, "live-only")]},
    )

    dry = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert dry.returncode == 0, dry.stderr
    assert "CatalogException" not in dry.stdout + dry.stderr
    assert "stat_anomalies: donor_rows=0 dest_rows=1 donor_only=0" in dry.stdout
    assert "mode=dest_only_no_donor_table" in dry.stdout
    # agent_run (present on both sides) is still reconciled normally.
    assert "agent_run: donor_rows=1 dest_rows=1 donor_only=0" in dry.stdout

    apply_result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert apply_result.returncode == 0, apply_result.stderr
    con = duckdb.connect(str(state_dir / "stats.duckdb"))
    assert con.execute('SELECT COUNT(*) FROM "stat_anomalies"').fetchone() == (1,)
    con.close()


def test_donor_only_table_is_refused_not_silently_skipped(layout):
    """The other half of the asymmetry: the donor has a table the
    destination lacks. Before this fix, this table was never even
    enumerated (iteration only ever walked the destination's table list),
    so any donor-only rows in it were silently never considered for
    migration — the exact failure mode the tool exists to prevent, just
    for a whole table instead of a row. Must be a hard refusal, not a
    silent skip and not an auto-create."""
    repo_root, state_dir = layout
    _make_state_db(repo_root / "state.db", [])
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db_with_tables(
        repo_root / "stats.duckdb",
        {"agent_run": [(1, "donor-a")], "donor_only_table": [(1, "irreplaceable")]},
    )
    _make_stats_db_with_tables(
        state_dir / "stats.duckdb", {"agent_run": [(1, "donor-a")]}
    )

    before = _stat(state_dir / "stats.duckdb")
    dry = _run_cli("--dry-run", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert dry.returncode != 0
    assert "donor_only_table" in dry.stderr
    assert _stat(state_dir / "stats.duckdb") == before

    apply_result = _run_cli("--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert apply_result.returncode != 0
    assert "donor_only_table" in apply_result.stderr
    assert _stat(state_dir / "stats.duckdb") == before
    assert not list(state_dir.glob("migration-backup-*")), (
        "a donor-only table must be caught before backup/merge even starts"
    )
    assert (repo_root / "stats.duckdb").exists(), "donor must survive a refusal untouched"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def test_requires_exactly_one_of_dry_run_or_apply(layout):
    repo_root, state_dir = layout
    _make_state_db(state_dir / "state.db", [])
    _make_stats_db(state_dir / "stats.duckdb", [])

    neither = _run_cli("--repo-root", str(repo_root), "--state-dir", str(state_dir))
    assert neither.returncode != 0

    both = _run_cli(
        "--dry-run", "--apply", "--repo-root", str(repo_root), "--state-dir", str(state_dir)
    )
    assert both.returncode != 0


def test_archive_never_overwrites_an_existing_file(tmp_path):
    """S2 (security review): shutil.move onto an existing destination
    silently replaces it. A regenerated donor archived a second time on the
    same day must not clobber the first run's archived copy."""
    archive_dir = tmp_path / "archive" / "state-db-residue-2026-01-01"
    archive_dir.mkdir(parents=True)
    (archive_dir / "state.db").write_bytes(b"first run's archived bytes")

    dest = m._unique_archive_dest(archive_dir, "state.db")
    assert dest != archive_dir / "state.db"
    assert not dest.exists()
    dest.write_bytes(b"second run's archived bytes")

    assert (archive_dir / "state.db").read_bytes() == b"first run's archived bytes"
