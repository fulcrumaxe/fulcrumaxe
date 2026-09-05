"""backend/migrate_state_to_dir.py — D#1908 PR 3: reconcile the frozen
repo-root database residue into the live external state dir.

Background
----------
Two files — ``state.db`` and ``stats.duckdb`` — used to accumulate directly
at the repo root (not under ``.autonomous-team/``, which is a symlink into
the external state dir and resolves correctly today). Both repo-root copies
are frozen at the same instant: whatever wrote them stopped six days before
this tool was written, while the external state dir kept moving. That makes
the repo-root pair a stale **donor**, not a live second writer — the state
dir is authoritative.

git status cannot see either file (PR #1971 gitignored both), so a reader
who trusts `git status` will conclude there is no problem. There is: the
donor may hold rows — real agent history — that never made it into the live
copy.

Rules this module holds itself to (D#1908 PR 3 Spec, non-negotiable):

1. Additive only. Never DELETE/DROP/TRUNCATE/REPLACE, never overwrite the
   destination. A donor row is inserted only when no matching row already
   exists in the destination.
2. Never pick a winner. Both copies are preserved; the donor is merged into
   the destination, not substituted for it.
3. Backup before any write, and the backup is *verified* before anything
   else proceeds — taken via each engine's online-backup mechanism (not a
   raw byte copy, which is unsafe against a live WAL-mode SQLite database —
   see security review finding E1), then re-opened and checked for
   integrity and row-count parity against the source. A backup that hasn't
   been read back is not a backup.
4. Dry-run first, and the dry-run is provably side-effect free: both
   connections are opened in genuine read-only mode (SQLite ``mode=ro`` URI,
   DuckDB ``read_only=True``/``(READ_ONLY)``), so a write is not just
   "not attempted" but rejected by the engine if it were ever attempted.
5. Nothing is ever deleted. After a successful --apply, donor files are
   *moved* (not copied-then-deleted-by-us — `shutil.move` — the bytes are
   never dropped) to ``archive/state-db-residue-<date>/`` with a README, per
   the Archive Protocol. `rm`/`git rm` never appear in this module. A name
   collision in the archive directory never overwrites an earlier archived
   file — it gets a disambiguating suffix instead.
6. A donor that fails to open or fails an integrity check is a hard refusal
   — non-zero exit, the offending file named in the message, nothing
   written — never a silent skip. A surrogate (sequence-generated) primary
   key is never used to decide "already present" — two independently
   written databases assign the same surrogate value to unrelated rows.

Usage::

    python3 backend/migrate_state_to_dir.py --dry-run
    python3 backend/migrate_state_to_dir.py --apply
    python3 backend/migrate_state_to_dir.py --apply   # idempotent re-run

``--repo-root``, ``--state-dir`` and ``--backup-dir`` override the donor
root, destination state dir, and backup location respectively — they exist
so the hermetic test suite (backend/tests/test_migrate_state_to_dir.py) can
run this whole module against fixture databases in a tmp_path and never
against the live host copies. Left unset, the donor root is wherever this
file's own checkout lives (``Path(__file__).resolve().parent.parent`` — the
same convention backend/db.py used for its own retired legacy branch) and
the destination is ``backend.state_paths.STATE_DIR``.

Before running --apply against the real host: quiesce writers first
(``bash scripts/stop-dashboard.sh``, pause the loop cron) — the backup is a
point-in-time snapshot, not a lock, and this is a one-shot migration against
the authoritative store.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow running as a script from repo root: `python3 backend/migrate_state_to_dir.py`
# (same bootstrap backend/blackboard.py and backend/migrate_to_sqlite.py use —
# running a script directly puts its own directory on sys.path, not its parent).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import duckdb
except ImportError:  # pragma: no cover — duckdb is a required dependency in practice
    duckdb = None  # noqa: N816


class CorruptDonorError(RuntimeError):
    """Raised when a database cannot be opened or fails an integrity check.

    Despite the name (most callers are checking the donor), this is also
    raised by the backup-verification path in ``take_backup`` when a
    just-taken backup fails to re-open or fails ``integrity_check`` — the
    message always names the actual offending path, so nothing is
    misleading to the operator. Caught at the top level and turned into a
    non-zero exit that writes nothing further — AC-3.9's hard-refusal
    requirement.
    """


class BackupFailedError(RuntimeError):
    """Raised when the pre-apply backup could not be completed and verified.

    Caught at the top level and turned into a non-zero exit before any
    merge is attempted — AC-3.5's "no write at all if the backup fails".
    Per security review finding E1, this now covers three failure classes:
    the online-backup call itself failing, the resulting backup failing to
    re-open / failing integrity_check, and the backup's row counts not
    matching the source it was taken from.
    """


class SchemaMismatchError(RuntimeError):
    """Raised when the donor has a table the destination lacks.

    A dest-only table has nothing to migrate by definition and is not an
    error. A *donor*-only table is a genuine structural question this tool
    refuses to answer on its own: creating the table in the destination is
    a schema decision (unreviewed DDL), silently discarding it loses real
    data, and silently reporting `donor_only=0` for it (the original bug —
    the table was never even enumerated) is worse than either. Caught at
    the top level exactly like CorruptDonorError: non-zero exit, nothing
    written, the offending table(s) named so a human can decide.
    """


# ---------------------------------------------------------------------------
# Default path resolution
# ---------------------------------------------------------------------------

def _default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_state_dir() -> Path:
    from backend import state_paths  # noqa: PLC0415

    return state_paths.STATE_DIR


# ---------------------------------------------------------------------------
# SQL identifier / URI safety helpers
# ---------------------------------------------------------------------------

def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded quotes.

    Table/column names are read from the donor's or destination's own
    schema — reaching this code with a hostile identifier already requires
    filesystem write access to one of the two database files, which is
    already game over. Quoting is cheap insurance regardless (S1, security
    review) and both SQLite and DuckDB accept double-quoted identifiers
    everywhere a bare identifier is accepted, including PRAGMA arguments
    (verified empirically before relying on it here).
    """
    return '"' + name.replace('"', '""') + '"'


def _sqlite_ro_uri(path: Path) -> str:
    """A SQLite read-only URI safe for a path containing ``?``/``#``/spaces.

    The original construction was ``f"file:{path}?mode=ro"``. SQLite
    silently ignores query parameters it doesn't recognise, so a raw ``?``
    or ``#`` inside *path* (reachable via ``--state-dir``/``--repo-root``)
    shifts the query-string boundary and ``mode=ro`` silently stops being
    parsed — the database opens read-write instead of the dry-run's
    advertised read-only (W3, security review). ``Path.as_uri()``
    percent-encodes reserved characters before the real query string is
    appended, so the ``?mode=ro`` appended here is unambiguously ours —
    verified empirically (a path containing both ``?`` and ``#`` still
    opens read-only and still rejects a write).
    """
    return path.resolve().as_uri() + "?mode=ro"


# ---------------------------------------------------------------------------
# Report data shapes (shared by SQLite and DuckDB)
# ---------------------------------------------------------------------------

@dataclass
class TableReport:
    table: str
    donor_rows: int
    dest_rows: int
    donor_only: int
    key_columns: list[str]
    key_mode: str  # "primary_key" | "full_row" | "full_row_excl_surrogate_pk" | "n/a"
    sample: list[dict] = field(default_factory=list)


@dataclass
class DbReport:
    label: str  # "state.db" / "stats.duckdb"
    donor_path: Path
    dest_path: Path
    donor_present: bool
    tables: list[TableReport] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SQLite (state.db)
# ---------------------------------------------------------------------------

def _sqlite_run_integrity_check(conn: sqlite3.Connection) -> tuple:
    """Broken out to its own function so tests can monkeypatch a forced
    failure without reaching into sqlite3 internals (used by both the donor
    check and the post-backup verification in _sqlite_backup_and_verify)."""
    return conn.execute("PRAGMA integrity_check").fetchone()


def _sqlite_integrity_check(path: Path) -> None:
    try:
        conn = sqlite3.connect(_sqlite_ro_uri(path), uri=True)
        try:
            # integrity_check, not quick_check (S3, security review): this is
            # a one-shot production migration and the file is small (state.db
            # is ~720KB) — the stronger, index-consistency-checking gate is
            # cheap enough that skipping it buys nothing.
            row = _sqlite_run_integrity_check(conn)
            if not row or row[0] != "ok":
                raise CorruptDonorError(f"{path}: PRAGMA integrity_check reported: {row}")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise CorruptDonorError(f"{path}: cannot open as SQLite database ({exc})") from exc


def _sqlite_tables(conn: sqlite3.Connection, schema: str) -> list[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(r[0] for r in rows)


def _sqlite_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info({_quote_ident(table)})").fetchall()
    return [r[1] for r in rows]


def _sqlite_pk_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info({_quote_ident(table)})").fetchall()
    pk_rows = sorted((r for r in rows if r[5]), key=lambda r: r[5])
    return [r[1] for r in pk_rows]


def _sqlite_is_autoincrement(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    """True if *table*'s DDL declares ``INTEGER PRIMARY KEY AUTOINCREMENT``.

    sqlite_master.sql carries the original CREATE TABLE text verbatim, and
    AUTOINCREMENT is the one reliable textual marker of a surrogate key
    drawn from SQLite's own internal sequence (W2, security review) — as
    opposed to a natural or content-derived key (a TEXT PRIMARY KEY like
    ``agent_lessons.id``), which two independently-written copies of the
    same schema populate with the *same* value only when it's genuinely the
    same logical row.
    """
    row = conn.execute(
        f"SELECT sql FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row and row[0] and "AUTOINCREMENT" in row[0].upper())


def _sqlite_match_key(
    conn: sqlite3.Connection, schema: str, table: str
) -> tuple[list[str], str, Optional[str]]:
    """Return (columns to match rows on, a label for that mode, surrogate column to omit on insert).

    A single-column AUTOINCREMENT primary key is a surrogate: two
    independently-written databases both start numbering from their own
    sequence, so donor id=42 and destination id=42 are almost certainly
    unrelated rows. Matching on it would silently treat every id collision
    as "already present" and skip real donor-only rows — exactly the W2
    finding. Falling back to every *other* column for the uniqueness check,
    and omitting the surrogate column from the INSERT so the destination's
    own sequence assigns a fresh id, means a genuinely-duplicate row (same
    content, coincidentally re-numbered) is still recognised as a duplicate,
    while a distinct row is inserted with its own new id instead of being
    silently dropped.
    """
    pk_cols = _sqlite_pk_columns(conn, schema, table)
    if len(pk_cols) == 1 and _sqlite_is_autoincrement(conn, schema, table):
        surrogate = pk_cols[0]
        non_surrogate = [c for c in _sqlite_columns(conn, schema, table) if c != surrogate]
        return non_surrogate, "full_row_excl_surrogate_pk", surrogate
    if pk_cols:
        return pk_cols, "primary_key", None
    return _sqlite_columns(conn, schema, table), "full_row", None


def _sqlite_cond(columns: list[str]) -> str:
    return " AND ".join(f"d.{_quote_ident(c)} IS m.{_quote_ident(c)}" for c in columns)


def sqlite_report(donor_path: Optional[Path], dest_path: Path) -> DbReport:
    donor_present = donor_path is not None and donor_path.exists()
    if donor_present:
        _sqlite_integrity_check(donor_path)

    conn = sqlite3.connect(_sqlite_ro_uri(dest_path), uri=True)
    try:
        if donor_present:
            conn.execute("ATTACH DATABASE ? AS donor", (_sqlite_ro_uri(donor_path),))
        tables = _sqlite_tables(conn, "main")
        reports: list[TableReport] = []
        for table in tables:
            qt = _quote_ident(table)
            dest_rows = conn.execute(f"SELECT COUNT(*) FROM main.{qt}").fetchone()[0]
            if not donor_present:
                reports.append(TableReport(table, 0, dest_rows, 0, [], "n/a"))
                continue

            donor_rows = conn.execute(f"SELECT COUNT(*) FROM donor.{qt}").fetchone()[0]
            key_cols, key_mode, _surrogate = _sqlite_match_key(conn, "main", table)
            cond = _sqlite_cond(key_cols)

            donor_only = conn.execute(
                f"SELECT COUNT(*) FROM donor.{qt} d WHERE NOT EXISTS "
                f"(SELECT 1 FROM main.{qt} m WHERE {cond})"
            ).fetchone()[0]

            sample: list[dict] = []
            if donor_only:
                all_cols = _sqlite_columns(conn, "main", table)
                cols_csv = ", ".join(_quote_ident(c) for c in all_cols)
                rows = conn.execute(
                    f"SELECT {cols_csv} FROM donor.{qt} d WHERE NOT EXISTS "
                    f"(SELECT 1 FROM main.{qt} m WHERE {cond}) LIMIT 5"
                ).fetchall()
                sample = [dict(zip(all_cols, r)) for r in rows]

            reports.append(
                TableReport(table, donor_rows, dest_rows, donor_only, key_cols, key_mode, sample)
            )
        return DbReport("state.db", donor_path or dest_path, dest_path, donor_present, reports)
    finally:
        conn.close()


def sqlite_apply(donor_path: Optional[Path], dest_path: Path) -> dict[str, int]:
    """Additively merge donor rows into dest_path. Returns {table: rows_inserted}."""
    donor_present = donor_path is not None and donor_path.exists()
    if not donor_present:
        return {}
    _sqlite_integrity_check(donor_path)

    # uri=True on the *connect* call (not just the ATTACH argument) is what
    # makes SQLite honour the `file:...?mode=ro` URI passed to ATTACH below
    # — without it ATTACH treats that string as a literal (bogus) filename.
    conn = sqlite3.connect(str(dest_path), uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS donor", (_sqlite_ro_uri(donor_path),))
        tables = _sqlite_tables(conn, "main")
        merged: dict[str, int] = {}
        conn.execute("BEGIN")
        for table in tables:
            qt = _quote_ident(table)
            key_cols, _key_mode, surrogate_col = _sqlite_match_key(conn, "main", table)
            cond = _sqlite_cond(key_cols)
            all_cols = _sqlite_columns(conn, "main", table)
            insert_cols = [c for c in all_cols if c != surrogate_col] if surrogate_col else all_cols
            cols_csv = ", ".join(_quote_ident(c) for c in insert_cols)
            cur = conn.execute(
                f"INSERT INTO main.{qt} ({cols_csv}) "
                f"SELECT {cols_csv} FROM donor.{qt} d WHERE NOT EXISTS "
                f"(SELECT 1 FROM main.{qt} m WHERE {cond})"
            )
            merged[table] = max(cur.rowcount, 0)
        conn.commit()
        return merged
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sqlite_row_counts(path: Path) -> dict[str, int]:
    """table -> row count, via a plain read-only connection. Also doubles as
    a cheap readability probe: a database that can't be queried raises here."""
    conn = sqlite3.connect(_sqlite_ro_uri(path), uri=True)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM main.{_quote_ident(t)}").fetchone()[0]
            for t in _sqlite_tables(conn, "main")
        }
    finally:
        conn.close()


def _sqlite_backup_and_verify(src: Path, dst: Path) -> None:
    """Online backup of *src* to *dst*, then re-open and verify *dst*.

    E1 (security review, blocking): the production ``state.db`` runs in WAL
    mode (``backend/db.py`` sets ``PRAGMA journal_mode=WAL`` unconditionally
    on every connection). Committed transactions can live in ``-wal`` until
    the next checkpoint, and a raw ``shutil.copy2`` of only the main file —
    taken with no read lock, while the cron loop and dashboard hold the
    database open — can silently omit recent commits or produce a torn,
    unopenable file. SQLite's own online backup API
    (``Connection.backup()``) takes a consistent snapshot including WAL
    content and is safe to run against a live database.

    "Taken" isn't enough on its own for a one-shot migration against the
    authoritative store — it has to be "taken and verified": re-open the
    result read-only, assert ``integrity_check`` is ``ok``, and assert its
    per-table row counts match a fresh read of the source taken immediately
    before the backup. Any failure raises :class:`BackupFailedError`, which
    the caller treats identically to a backup that could not be created at
    all — no merge is attempted.
    """
    try:
        src_counts = _sqlite_row_counts(src)
    except sqlite3.DatabaseError as exc:
        raise BackupFailedError(f"could not read source {src} before backup: {exc}") from exc

    try:
        src_conn = sqlite3.connect(_sqlite_ro_uri(src), uri=True)
        try:
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupFailedError(f"online backup of {src} to {dst} failed: {exc}") from exc

    try:
        dst_counts = _sqlite_row_counts(dst)
        verify_conn = sqlite3.connect(_sqlite_ro_uri(dst), uri=True)
        try:
            row = _sqlite_run_integrity_check(verify_conn)
        finally:
            verify_conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupFailedError(f"backup {dst} could not be reopened for verification: {exc}") from exc

    if not row or row[0] != "ok":
        raise BackupFailedError(f"backup {dst} failed integrity_check: {row}")
    if dst_counts != src_counts:
        raise BackupFailedError(
            f"backup {dst} row counts do not match source {src} taken moments earlier: "
            f"source={src_counts} backup={dst_counts}"
        )


# ---------------------------------------------------------------------------
# DuckDB (stats.duckdb)
# ---------------------------------------------------------------------------

def _duckdb_require() -> None:
    if duckdb is None:  # pragma: no cover
        raise RuntimeError("duckdb package is not installed")


def _duckdb_tables_in_catalog(conn, catalog: str) -> list[str]:
    """List tables in a specific attached-or-default catalog by name.

    The bug this replaces enumerated only the *destination's* tables
    (``table_catalog=current_database()``, which is always the primary
    connection — the destination here) and then queried every one of them
    against the donor too. A dest-only table (the destination is live and
    can grow tables a seven-day-old donor never had — the normal case, not
    an edge case) crashed with a DuckDB CatalogException; a donor-only
    table was silently never even looked at. Enumerating each side's own
    catalog separately is what lets the caller reason about dest-only and
    donor-only tables as the genuinely different cases they are.
    """
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_catalog = ?",
        [catalog],
    ).fetchall()
    return sorted({r[0] for r in rows})


def _duckdb_dest_tables(conn) -> list[str]:
    """Tables in the destination — the primary/default connection, whatever
    its real catalog name happens to be (derived from the file, not
    literally "main"; "main" is a schema name, not this)."""
    return _duckdb_tables_in_catalog(conn, conn.execute("SELECT current_database()").fetchone()[0])


def _duckdb_donor_tables(conn) -> list[str]:
    """Tables in the donor, which is always attached under the fixed alias
    ``donor`` — see the ``ATTACH ... AS donor`` call sites."""
    return _duckdb_tables_in_catalog(conn, "donor")


# Backward-compatible alias: _duckdb_row_counts (used only by the backup
# verification path, E1 — intentionally not touched by this fix) calls a
# single, unattached connection's own tables, which is exactly what
# _duckdb_dest_tables computes regardless of whether that file happens to be
# a source or a destination in that context.
_duckdb_tables = _duckdb_dest_tables


def _duckdb_columns(conn, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [r[1] for r in rows]


def _duckdb_pk_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE database_name = current_database() AND table_name = ? "
        "AND constraint_type = 'PRIMARY KEY'",
        [table],
    ).fetchall()
    return list(rows[0][0]) if rows else []


def _duckdb_is_generated(conn, table: str, column: str) -> bool:
    """DuckDB's rough equivalent of SQLite's AUTOINCREMENT: a column whose
    default draws from a sequence (``DEFAULT nextval('seq')``). stats.duckdb's
    schema was deliberately never censused ahead of this PR, so this is a
    defensive extension of W2's fix to the engine the review didn't have
    visibility into — same surrogate-key risk, same failure mode, if it ever
    applies here."""
    row = conn.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = ? AND column_name = ? AND table_catalog = current_database()",
        [table, column],
    ).fetchone()
    default = (row[0] or "") if row else ""
    return "nextval" in default.lower()


def _duckdb_match_key(conn, table: str) -> tuple[list[str], str, Optional[str]]:
    """DuckDB counterpart of :func:`_sqlite_match_key` — see its docstring."""
    pk_cols = _duckdb_pk_columns(conn, table)
    if len(pk_cols) == 1 and _duckdb_is_generated(conn, table, pk_cols[0]):
        surrogate = pk_cols[0]
        non_surrogate = [c for c in _duckdb_columns(conn, table) if c != surrogate]
        return non_surrogate, "full_row_excl_surrogate_pk", surrogate
    if pk_cols:
        return pk_cols, "primary_key", None
    return _duckdb_columns(conn, table), "full_row", None


def _duckdb_cond(columns: list[str]) -> str:
    return " AND ".join(f"d.{_quote_ident(c)} IS NOT DISTINCT FROM m.{_quote_ident(c)}" for c in columns)


def _duckdb_integrity_check(path: Path) -> None:
    """DuckDB has no separate integrity-check pragma — a failed connect/ATTACH
    (bad magic, truncated file, storage-version mismatch) already raises,
    and that's the corruption signal for this engine."""
    _duckdb_require()
    try:
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"ATTACH '{path}' AS probe (READ_ONLY)")
            con.execute("DETACH probe")
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — duckdb raises its own exception hierarchy
        raise CorruptDonorError(f"{path}: cannot open as DuckDB database ({exc})") from exc


def _duckdb_reject_donor_only_tables(donor_tables: list[str], dest_tables: list[str]) -> None:
    """Raise SchemaMismatchError if the donor has a table the destination
    lacks — see SchemaMismatchError's docstring for why this is a refusal
    rather than an auto-create or a silent skip."""
    donor_only = sorted(set(donor_tables) - set(dest_tables))
    if donor_only:
        raise SchemaMismatchError(
            f"donor has table(s) the destination does not: {', '.join(donor_only)}. "
            "This tool only reconciles rows in tables both copies already share — "
            "creating a new destination table is a schema decision, not a data-merge "
            "one, and refusing beats either silently discarding donor-only data or "
            "silently creating unreviewed schema. Handle manually, then re-run."
        )


def duckdb_report(donor_path: Optional[Path], dest_path: Path) -> DbReport:
    _duckdb_require()
    donor_present = donor_path is not None and donor_path.exists()
    if donor_present:
        _duckdb_integrity_check(donor_path)

    conn = duckdb.connect(str(dest_path), read_only=True)
    try:
        dest_tables = _duckdb_dest_tables(conn)
        if not donor_present:
            reports = [
                TableReport(
                    t, 0, conn.execute(f"SELECT COUNT(*) FROM main.{_quote_ident(t)}").fetchone()[0],
                    0, [], "n/a",
                )
                for t in dest_tables
            ]
            return DbReport("stats.duckdb", donor_path or dest_path, dest_path, False, reports)

        conn.execute(f"ATTACH '{donor_path}' AS donor (READ_ONLY)")
        donor_tables = _duckdb_donor_tables(conn)
        _duckdb_reject_donor_only_tables(donor_tables, dest_tables)

        common_tables = sorted(set(dest_tables) & set(donor_tables))
        dest_only_tables = sorted(set(dest_tables) - set(donor_tables))

        reports: list[TableReport] = []
        for table in common_tables:
            qt = _quote_ident(table)
            dest_rows = conn.execute(f"SELECT COUNT(*) FROM main.{qt}").fetchone()[0]
            donor_rows = conn.execute(f"SELECT COUNT(*) FROM donor.{qt}").fetchone()[0]
            key_cols, key_mode, _surrogate = _duckdb_match_key(conn, table)
            cond = _duckdb_cond(key_cols)

            donor_only = conn.execute(
                f"SELECT COUNT(*) FROM donor.{qt} d WHERE NOT EXISTS "
                f"(SELECT 1 FROM main.{qt} m WHERE {cond})"
            ).fetchone()[0]

            sample: list[dict] = []
            if donor_only:
                all_cols = _duckdb_columns(conn, table)
                cols_csv = ", ".join(_quote_ident(c) for c in all_cols)
                rows = conn.execute(
                    f"SELECT {cols_csv} FROM donor.{qt} d WHERE NOT EXISTS "
                    f"(SELECT 1 FROM main.{qt} m WHERE {cond}) LIMIT 5"
                ).fetchall()
                sample = [dict(zip(all_cols, r)) for r in rows]

            reports.append(
                TableReport(table, donor_rows, dest_rows, donor_only, key_cols, key_mode, sample)
            )

        # Dest-only tables have nothing to migrate by definition (the donor
        # doesn't have them at all) — reported for visibility, not an error.
        for table in dest_only_tables:
            qt = _quote_ident(table)
            dest_rows = conn.execute(f"SELECT COUNT(*) FROM main.{qt}").fetchone()[0]
            reports.append(TableReport(table, 0, dest_rows, 0, [], "dest_only_no_donor_table"))

        return DbReport("stats.duckdb", donor_path, dest_path, True, reports)
    finally:
        conn.close()


def duckdb_apply(donor_path: Optional[Path], dest_path: Path) -> dict[str, int]:
    _duckdb_require()
    donor_present = donor_path is not None and donor_path.exists()
    if not donor_present:
        return {}
    _duckdb_integrity_check(donor_path)

    conn = duckdb.connect(str(dest_path), read_only=False)
    try:
        dest_tables = _duckdb_dest_tables(conn)
        conn.execute(f"ATTACH '{donor_path}' AS donor (READ_ONLY)")
        donor_tables = _duckdb_donor_tables(conn)
        _duckdb_reject_donor_only_tables(donor_tables, dest_tables)
        common_tables = sorted(set(dest_tables) & set(donor_tables))

        merged: dict[str, int] = {}
        conn.execute("BEGIN TRANSACTION")
        try:
            for table in common_tables:
                qt = _quote_ident(table)
                key_cols, _key_mode, surrogate_col = _duckdb_match_key(conn, table)
                cond = _duckdb_cond(key_cols)
                all_cols = _duckdb_columns(conn, table)
                insert_cols = [c for c in all_cols if c != surrogate_col] if surrogate_col else all_cols
                cols_csv = ", ".join(_quote_ident(c) for c in insert_cols)
                result = conn.execute(
                    f"INSERT INTO main.{qt} ({cols_csv}) "
                    f"SELECT {cols_csv} FROM donor.{qt} d WHERE NOT EXISTS "
                    f"(SELECT 1 FROM main.{qt} m WHERE {cond})"
                ).fetchall()
                merged[table] = int(result[0][0]) if result and result[0] else 0
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return merged
    finally:
        conn.close()


def _duckdb_row_counts(path: Path) -> dict[str, int]:
    """table -> row count, via a plain read-only connection. Also doubles as
    the integrity signal for this engine: a corrupt/truncated file fails to
    connect or fails one of these queries rather than returning wrong data
    silently — see _duckdb_integrity_check's docstring for why DuckDB has no
    separate integrity-check pragma to call instead."""
    _duckdb_require()
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {
            t: con.execute(f"SELECT COUNT(*) FROM main.{_quote_ident(t)}").fetchone()[0]
            for t in _duckdb_tables(con)
        }
    finally:
        con.close()


def _duckdb_backup_and_verify(src: Path, dst: Path) -> None:
    """Online backup of *src* to *dst* via ``COPY FROM DATABASE``, then
    re-open and verify *dst*. DuckDB counterpart of
    :func:`_sqlite_backup_and_verify` — see its docstring for the E1
    rationale. DuckDB has no WAL-sidecar file the way SQLite does, but
    ``shutil.copy2`` on a database another connection has open is still not
    a defined-safe operation, so the same online-backup-plus-verify
    treatment applies.
    """
    _duckdb_require()
    try:
        src_counts = _duckdb_row_counts(src)
    except Exception as exc:  # noqa: BLE001 — duckdb's own exception hierarchy
        raise BackupFailedError(f"could not read source {src} before backup: {exc}") from exc

    try:
        con = duckdb.connect(str(dst))
        try:
            con.execute(f"ATTACH '{src}' AS backup_src (READ_ONLY)")
            target_alias = con.execute("SELECT current_database()").fetchone()[0]
            con.execute(f"COPY FROM DATABASE backup_src TO {_quote_ident(target_alias)}")
            con.execute("DETACH backup_src")
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        raise BackupFailedError(f"online backup of {src} to {dst} failed: {exc}") from exc

    try:
        dst_counts = _duckdb_row_counts(dst)
    except Exception as exc:  # noqa: BLE001
        raise BackupFailedError(f"backup {dst} could not be reopened for verification: {exc}") from exc

    if dst_counts != src_counts:
        raise BackupFailedError(
            f"backup {dst} row counts do not match source {src} taken moments earlier: "
            f"source={src_counts} backup={dst_counts}"
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(report: DbReport) -> str:
    lines = [f"== {report.label} =="]
    if not report.donor_present:
        lines.append(f"  donor: {report.donor_path} — no donor copy present")
        for t in report.tables:
            lines.append(f"  {t.table}: dest_rows={t.dest_rows}")
        return "\n".join(lines)

    lines.append(f"  donor:  {report.donor_path}")
    lines.append(f"  dest:   {report.dest_path}")
    for t in report.tables:
        lines.append(
            f"  {t.table}: donor_rows={t.donor_rows} dest_rows={t.dest_rows} "
            f"donor_only={t.donor_only} (key={'+'.join(t.key_columns) or 'none'}, "
            f"mode={t.key_mode})"
        )
        for row in t.sample:
            lines.append(f"    donor-only sample: {row}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backup / archive
# ---------------------------------------------------------------------------

# name -> (source path or None, engine) for the four possible files. Engine
# dispatches to the right online-backup-and-verify implementation (E1).
_BackupItem = tuple[str, Optional[Path], str]


def take_backup(backup_dir: Path, items: list[_BackupItem]) -> list[Path]:
    """Back up and verify every existing source in *items* into *backup_dir*.

    *items* is a list of (backup_filename, source_path, engine) triples,
    where engine is ``"sqlite"`` or ``"duckdb"``; a source that is None or
    doesn't exist is skipped. Raises :class:`BackupFailedError` — and stops
    immediately — on any failure: directory creation, the online backup
    itself, or its post-backup verification (AC-3.5, "no write at all if
    the backup fails", and E1's "taken and verified").
    """
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupFailedError(f"could not create backup dir {backup_dir}: {exc}") from exc

    backed_up: list[Path] = []
    for name, src, engine in items:
        if src is None or not src.exists():
            continue
        dst = backup_dir / name
        if engine == "sqlite":
            _sqlite_backup_and_verify(src, dst)
        elif engine == "duckdb":
            _duckdb_backup_and_verify(src, dst)
        else:  # pragma: no cover — programmer error, not a runtime condition
            raise ValueError(f"unknown backup engine {engine!r} for {name}")
        backed_up.append(dst)
    return backed_up


_ARCHIVE_README_TEMPLATE = """\
# state-db-residue-{date}

## When removed

{date} — moved here by `backend/migrate_state_to_dir.py --apply`
(D#1908 PR 3) immediately after that run's additive merge committed.

## What this is

The repo-root `state.db` and/or `stats.duckdb` files that D#1908 PR 3's
`backend/migrate_state_to_dir.py --apply` merged into the live external
state dir (`$AUTONOMOUS_TEAM_STATE_DIR`, default
`~/.autonomous-forever-state/`) on {date}.

## Why removed

These files were a frozen, six-day-stale donor: something used to write
`state.db`/`stats.duckdb` directly at the repo root instead of the external
state dir, then stopped. Every row unique to them has already been
additively copied into the live database — nothing here is data that exists
only in this archive.

## Original path

Repo root: `state.db`, `stats.duckdb` (sibling to this repository's
top-level files, not under `.autonomous-team/`).

## How to restore

`git mv` (or plain `mv`, since these were never git-tracked — see
`.gitignore`) the file(s) here back to the repo root. This does not undo the
merge: the rows are already live in the external state dir either way.

## What would justify restoring

Only forensic interest — e.g. auditing exactly what the donor looked like
before the merge. The migration is additive-only, so nothing recoverable
here is missing from the live database. Files present: {files}.
"""


def _unique_archive_dest(archive_dir: Path, name: str) -> Path:
    """A path under *archive_dir* for *name* that never overwrites an
    existing file (S2, security review). ``shutil.move`` onto an existing
    destination silently replaces it — a second same-day ``--apply`` that
    found a regenerated donor would otherwise clobber the first run's
    archived copy, which is exactly the kind of silent data loss the
    Archive Protocol exists to prevent."""
    candidate = archive_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        candidate = archive_dir / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def archive_donor(repo_root: Path, donor_paths: list[Optional[Path]]) -> Optional[Path]:
    """Move existing donor files to archive/state-db-residue-<date>/ with a README.

    Never deletes: `shutil.move` relocates the bytes, it does not drop them.
    Never overwrites an existing archived file (see `_unique_archive_dest`).
    Returns the archive directory if anything was moved, else None.
    """
    existing = [p for p in donor_paths if p is not None and p.exists()]
    if not existing:
        return None

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_dir = repo_root / "archive" / f"state-db-residue-{date_str}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved_names: list[str] = []
    for p in existing:
        dest = _unique_archive_dest(archive_dir, p.name)
        shutil.move(str(p), str(dest))
        moved_names.append(dest.name)

    readme = archive_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            _ARCHIVE_README_TEMPLATE.format(date=date_str, files=", ".join(moved_names)),
            encoding="utf-8",
        )
    return archive_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _now_compact_ts() -> str:
    # Microsecond precision: two --apply runs inside the same wall-clock
    # second (a fast re-apply in a test loop, say) would otherwise collide
    # on the same backup dir name and silently overwrite one backup's
    # contents with the other's.
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the frozen repo-root state.db / stats.duckdb residue "
            "into the live external state dir (D#1908 PR 3). Additive only; "
            "see module docstring for the full rule set."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Report what would move. Writes nothing.")
    mode.add_argument("--apply", action="store_true", help="Back up, then additively merge and archive the donor.")
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Donor root override (default: this checkout's own root).",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="Destination state dir override (default: backend.state_paths.STATE_DIR).",
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=None,
        help="Backup directory override (default: <state-dir>/migration-backup-<ISO8601>).",
    )
    return parser


# What's actually true about destination state when an unexpected exception
# (W4, security review) interrupts main() during each phase. Printed instead
# of a bare traceback so the operator isn't left guessing what a half-run
# migration did or didn't do.
_PHASE_NOTES = {
    "reporting": "No writes were attempted; nothing to recover.",
    "backup": "No merge or archive was attempted. The destination is untouched.",
    "merge state.db": (
        "The verified backup exists. state.db was not modified — its merge "
        "transaction rolled back or never started. stats.duckdb was not "
        "attempted. Nothing was archived. Safe to re-run --apply."
    ),
    "merge stats.duckdb": (
        "The verified backup exists. state.db's merge already committed. "
        "stats.duckdb was not modified — its merge transaction rolled back "
        "or never started. Nothing was archived yet; the donor files are "
        "still at the repo root. Safe to re-run --apply: state.db's merge "
        "is idempotent and will report zero new rows."
    ),
    "archive": (
        "Both databases were merged successfully. The donor files may or "
        "may not have been archived — check archive/state-db-residue-<date>/ "
        "manually. Safe to re-run --apply either way: it will find no donor "
        "(already archived) or retry the archive step (not yet archived)."
    ),
}


def _run(args: argparse.Namespace) -> int:
    """The actual CLI body, wrapped by main() in a broad except that reports
    which phase (module-level `phase`, tracked via the enclosing closure) was
    in progress — see _PHASE_NOTES and W4 above."""
    repo_root = args.repo_root or _default_repo_root()
    state_dir = args.state_dir or _default_state_dir()

    donor_state_db = repo_root / "state.db"
    donor_stats_db = repo_root / "stats.duckdb"
    dest_state_db = state_dir / "state.db"
    dest_stats_db = state_dir / "stats.duckdb"

    _run.phase = "reporting"
    try:
        state_report = sqlite_report(donor_state_db, dest_state_db)
        stats_report = duckdb_report(donor_stats_db, dest_stats_db)
    except (CorruptDonorError, SchemaMismatchError) as exc:
        print(f"[REFUSED] {exc}", file=sys.stderr)
        return 1

    print(format_report(state_report))
    print()
    print(format_report(stats_report))

    if args.dry_run:
        print()
        print("[DRY RUN] no changes made.")
        return 0

    _run.phase = "backup"
    backup_dir = args.backup_dir or (state_dir / f"migration-backup-{_now_compact_ts()}")
    try:
        take_backup(
            backup_dir,
            [
                ("donor-state.db", donor_state_db if donor_state_db.exists() else None, "sqlite"),
                ("dest-state.db", dest_state_db if dest_state_db.exists() else None, "sqlite"),
                ("donor-stats.duckdb", donor_stats_db if donor_stats_db.exists() else None, "duckdb"),
                ("dest-stats.duckdb", dest_stats_db if dest_stats_db.exists() else None, "duckdb"),
            ],
        )
    except BackupFailedError as exc:
        print(f"[REFUSED] backup failed, no changes made: {exc}", file=sys.stderr)
        return 1
    print()
    print(f"[OK] backup taken and verified at {backup_dir}")

    _run.phase = "merge state.db"
    try:
        state_merged = sqlite_apply(donor_state_db, dest_state_db)
    except CorruptDonorError as exc:
        print(f"[REFUSED] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] state.db rows merged: {state_merged}")

    _run.phase = "merge stats.duckdb"
    try:
        stats_merged = duckdb_apply(donor_stats_db, dest_stats_db)
    except (CorruptDonorError, SchemaMismatchError) as exc:
        print(f"[REFUSED] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] stats.duckdb rows merged: {stats_merged}")

    _run.phase = "archive"
    archive_dir = archive_donor(repo_root, [donor_state_db, donor_stats_db])
    if archive_dir:
        print(f"[OK] donor file(s) archived to {archive_dir}")
        print("     Remember to `git add` the archive directory and commit it.")
    else:
        print("[OK] no donor files present — nothing to archive (already migrated, or none ever existed here).")

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _run.phase = "reporting"
    try:
        return _run(args)
    except (CorruptDonorError, BackupFailedError, SchemaMismatchError):
        raise  # pragma: no cover — _run already handles and returns for these
    except Exception as exc:  # noqa: BLE001 — W4: report state, don't just crash
        phase = getattr(_run, "phase", "reporting")
        print(file=sys.stderr)
        print(f"[UNEXPECTED ERROR during phase '{phase}'] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"State: {_PHASE_NOTES.get(phase, 'Unknown phase — inspect manually before retrying.')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
