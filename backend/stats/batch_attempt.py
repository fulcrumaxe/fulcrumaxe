"""backend/stats/batch_attempt.py — make a lost stats batch observable (D#2524 PR-b).

PR-a (D#2524) made ``stats_writer.record_many()`` atomic: a merge's metric
batch now commits in full or not at all, never a prefix. But "not at all"
still happens under sustained lock contention, and a merge with **zero**
``metric_event`` rows is indistinguishable from a merge whose post-merge hook
never ran the stats step at all — both leave the same empty state.

This module adds the missing state: a lightweight ``stats_batch_attempt``
marker, written just before ``record_many()`` is called for a merge's batch,
naming the PR and the ordered list of metric names about to be attempted.
The marker is written via its own short, separately-retried connection so it
survives ``record_many()`` failing later in the same invocation.

    mark_batch_attempt(pr, expected_metrics)   — call before record_many()
    find_incomplete_batches(prs=None)          — the detector

The detector cross-references every attempt marker against what actually
landed in ``metric_event`` for that PR:
  - no attempt marker for a PR  -> not reportable (can't tell "lost" from
    "hook never ran"; that ambiguity is exactly what PR-a's F4 caveat named,
    and reporting it either way would be a guess).
  - attempt marker + full expected metric set written -> complete, no report.
  - attempt marker + a strict subset (including the empty set) written ->
    incomplete, reported by PR name.

Reachable without reading a merge log: run this module directly --

    python3 -m backend.stats.batch_attempt [--pr N ...]

-- which prints a JSON report and exits 1 if any incomplete batch is found,
0 otherwise (D#2524 item 10).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── DuckDB path helper (mirrors stats_writer._db_path / anomaly_detector._db_path) ──


def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


#: Bound on how long mark_batch_attempt() retries acquiring its connection.
#: Deliberately short — this is a single small insert, not the data batch
#: itself, and it must not itself become the thing that loses the marker.
_MARK_RETRY_BUDGET_S = 2.0

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stats_batch_attempt (
    ts                TIMESTAMP NOT NULL,
    pr                TEXT      NOT NULL,
    expected_metrics  JSON      NOT NULL,
    source            TEXT,
    PRIMARY KEY (ts, pr)
)
"""


def ensure_table(conn: Any) -> None:
    """Create stats_batch_attempt table if it doesn't exist."""
    conn.execute(_CREATE_TABLE_SQL)


def mark_batch_attempt(
    pr: str,
    expected_metrics: list[str],
    source: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Record that a stats batch for `pr` is about to be attempted.

    Call this BEFORE record_many(rows) with the ordered list of metric names
    in `rows`. Opens its own connection (bounded retry, same lock-acquisition
    primitive record_many uses) and commits immediately, so the marker
    persists even if the caller's later record_many() call fails.

    Raises on failure (duckdb not installed, or the connection could not be
    acquired within the retry budget) — callers that must not let a marker
    failure abort the stats step should catch around this call themselves.
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "duckdb not installed — run: pip install duckdb"
        ) from exc

    from backend.stats_connection import _connect_with_retry  # noqa: PLC0415

    db = db_path or _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = _connect_with_retry(str(db), read_only=False, retry_budget_s=_MARK_RETRY_BUDGET_S)
    except duckdb.IOException as exc:
        raise IOError(f"batch_attempt: lock conflict on {db}: {exc}") from exc

    try:
        ensure_table(conn)
        ts = datetime.now(timezone.utc)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        conn.execute(
            "INSERT OR IGNORE INTO stats_batch_attempt (ts, pr, expected_metrics, source) "
            "VALUES (CAST(? AS TIMESTAMP), ?, CAST(? AS JSON), ?)",
            [ts_str, str(pr), json.dumps(list(expected_metrics)), source],
        )
    finally:
        conn.close()


# ── Detector ─────────────────────────────────────────────────────────────────


@dataclass
class IncompleteBatch:
    """One attempted-but-incomplete stats batch."""

    pr: str
    attempted_at: str
    expected: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "lost_zero" if not self.written else "lost_partial"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "attempted_at": self.attempted_at,
            "status": self.status,
            "expected": self.expected,
            "written": self.written,
            "missing": self.missing,
        }


def find_incomplete_batches(
    prs: list[str] | None = None,
    db_path: Path | None = None,
) -> list[IncompleteBatch]:
    """Cross-reference stats_batch_attempt markers against metric_event.

    Returns one IncompleteBatch per attempt whose written metric set is a
    strict subset of what it expected to write (the empty set included).
    A PR with no attempt marker at all is never returned here — that PR's
    zero rows cannot be attributed (see module docstring).
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return []

    from backend.stats_connection import _connect_with_retry  # noqa: PLC0415

    db = db_path or _db_path()
    if not db.exists():
        return []

    try:
        conn = _connect_with_retry(str(db), read_only=True)
    except duckdb.IOException:
        # A permanently-held conflicting lock: report nothing rather than
        # raising — the detector is a best-effort observability tool, not
        # part of the write path this Discussion is protecting.
        return []

    try:
        query = "SELECT ts::TEXT, pr, expected_metrics FROM stats_batch_attempt"
        params: list[str] = []
        if prs:
            placeholders = ",".join(["?"] * len(prs))
            query += f" WHERE pr IN ({placeholders})"
            params = [str(p) for p in prs]
        query += " ORDER BY ts"

        try:
            attempts = conn.execute(query, params).fetchall()
        except Exception:
            # stats_batch_attempt doesn't exist yet (pre-PR-b DB) — nothing
            # is attributable, matching the "no marker" case above.
            return []

        results: list[IncompleteBatch] = []
        for ts_str, pr, expected_json in attempts:
            expected = json.loads(expected_json)
            expected_set = set(expected)

            try:
                written_rows = conn.execute(
                    "SELECT DISTINCT metric FROM metric_event "
                    "WHERE JSON_EXTRACT_STRING(tags, '$.pr') = ?",
                    [pr],
                ).fetchall()
            except Exception:
                # metric_event doesn't exist yet — an attempt was marked but
                # record_many() never got far enough to create the table.
                # That is exactly the "attempted and lost" shape (zero rows).
                written_rows = []
            written_set = {r[0] for r in written_rows}

            if expected_set.issubset(written_set):
                continue  # complete (or superset, e.g. extra unrelated metrics) — nothing to report

            results.append(
                IncompleteBatch(
                    pr=pr,
                    attempted_at=ts_str,
                    expected=expected,
                    written=sorted(written_set & expected_set),
                    missing=sorted(expected_set - written_set),
                )
            )
        return results
    finally:
        conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    _REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)

    ap = argparse.ArgumentParser(
        description="Report merges whose stats batch was attempted but not fully written"
    )
    ap.add_argument("--pr", action="append", default=None, help="limit to this PR (repeatable)")
    args = ap.parse_args()

    incomplete = find_incomplete_batches(prs=args.pr)
    print(json.dumps([b.as_dict() for b in incomplete], indent=2))
    sys.exit(1 if incomplete else 0)
