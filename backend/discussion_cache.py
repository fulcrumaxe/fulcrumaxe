"""backend/discussion_cache.py — SQLite cache for GitHub Discussion reads.

Cuts GraphQL traffic by serving repeated reads from a local cache with a 300s TTL.
Cache lives at $AUTONOMOUS_TEAM_STATE_DIR/discussion_cache.db (never inside the repo).

Public API:
    get_body(number, fresh=False) -> str   cached body, or a fresh fetch when fresh=True;
                                            "" on total failure
    get(number, fresh=False) -> dict       full record: body, title, labels, updated_at
    list_open() -> list[dict]              ONE batched GraphQL call for all open discussions
    invalidate(number)                     force next read to re-fetch

CLI (called by shell scripts):
    python3 backend/discussion_cache.py get-body <N> [--fresh]
    python3 backend/discussion_cache.py list-open
    python3 backend/discussion_cache.py invalidate <N>

`fresh=True` bypasses the TTL check and re-fetches unconditionally. It exists for the
handful of correctness-critical reads (the spawn-gate's SPEC_READY check) where a stale
body can misdirect the caller; the bulk/context-assembly readers keep the plain TTL-cached
path on purpose — that's what the cache is for.

On GraphQL failure the cache returns stale data with a stderr warning rather than
raising — the calling script sees a non-empty result and can continue. This holds for
fresh reads too: a fresh read that hard-fails on a transient GraphQL blip would trade an
intermittent staleness bug for an outage, so it falls back to the same stale-cache
behaviour. The CLI's `get-body --fresh` distinguishes this case via exit code 3 (see
below) so callers that must not silently trust a stale fallback can react to it.

CLI exit codes for `get-body`:
    0   body printed to stdout, and (for --fresh) it is a live/current read
    1   nothing available at all — stdout is empty
    3   --fresh was requested but the live fetch failed; stdout holds a *stale*
        cached fallback (also flagged via a stderr warning)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import state_paths as _state_paths  # noqa: E402
from backend.state_paths import ensure_state_dir  # noqa: E402
from backend._repo import REPO as _REPO, REPO_OWNER as _REPO_OWNER, REPO_NAME as _REPO_NAME  # noqa: E402

_TTL: int = 300  # seconds


def _db_path() -> Path:
    # Resolved at call time, not import time — see D#1810.
    return _state_paths.STATE_DIR / "discussion_cache.db"

# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS discussion_cache (
    number     INTEGER PRIMARY KEY,
    body       TEXT    NOT NULL DEFAULT '',
    title      TEXT    NOT NULL DEFAULT '',
    labels     TEXT    NOT NULL DEFAULT '[]',
    updated_at TEXT    NOT NULL DEFAULT '',
    cached_at  TEXT    NOT NULL DEFAULT ''
);
"""


def _conn() -> sqlite3.Connection:
    ensure_state_dir()
    con = sqlite3.connect(str(_db_path()))
    con.row_factory = sqlite3.Row
    con.execute(_DDL)
    con.commit()
    return con


# ---------------------------------------------------------------------------
# Stats counters (hit/miss) — stored in same db
# ---------------------------------------------------------------------------

_COUNTER_DDL = """
CREATE TABLE IF NOT EXISTS discussion_cache_stats (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


def _inc(key: str) -> None:
    """Increment a named counter (best-effort — never raises)."""
    try:
        with _conn() as con:
            con.execute(_COUNTER_DDL)
            con.execute(
                "INSERT INTO discussion_cache_stats(key, value) VALUES(?, 1) "
                "ON CONFLICT(key) DO UPDATE SET value = value + 1",
                (key,),
            )
    except Exception:  # noqa: BLE001
        pass


def get_stats() -> dict:
    """Return hit/miss/total counts and hit_ratio (float 0–1)."""
    try:
        with _conn() as con:
            con.execute(_COUNTER_DDL)
            rows = con.execute("SELECT key, value FROM discussion_cache_stats").fetchall()
        counts = {r["key"]: r["value"] for r in rows}
    except Exception:  # noqa: BLE001
        counts = {}
    hits = counts.get("hit", 0)
    misses = counts.get("miss", 0)
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "total": total,
        "hit_ratio": round(hits / total, 4) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# GraphQL helpers
# ---------------------------------------------------------------------------

def _gh_graphql(query: str) -> Optional[dict]:
    """Run a gh graphql call and return parsed JSON, or None on error."""
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            warnings.warn(
                f"discussion_cache: gh graphql failed: {result.stderr.strip()}",
                stacklevel=2,
            )
            return None
        return json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"discussion_cache: gh graphql error: {exc}", stacklevel=2)
        return None


def _fetch_one(number: int) -> Optional[dict]:
    """Fetch a single discussion from GitHub. Returns dict or None on failure."""
    query = (
        f'query {{ repository(owner:"{_REPO_OWNER}", name:"{_REPO_NAME}") {{'
        f' discussion(number: {number}) {{ title body updatedAt labels(first:10) {{ nodes {{ name }} }} }} }} }}'
    )
    data = _gh_graphql(query)
    if not data:
        return None
    try:
        disc = data["data"]["repository"]["discussion"]
        if disc is None:
            return None
        labels = [n["name"] for n in (disc.get("labels", {}).get("nodes") or [])]
        return {
            "number": number,
            "title": disc.get("title", ""),
            "body": disc.get("body", ""),
            "labels": labels,
            "updated_at": disc.get("updatedAt", ""),
        }
    except (KeyError, TypeError):
        return None


def _fetch_all_open() -> Optional[list[dict]]:
    """Fetch all open discussions in one batched GraphQL call."""
    query = (
        f'query {{ repository(owner:"{_REPO_OWNER}", name:"{_REPO_NAME}") {{'
        ' discussions(first:100, states:[OPEN]) { nodes { number title body updatedAt'
        ' labels(first:10) { nodes { name } } } } } }'
    )
    data = _gh_graphql(query)
    if not data:
        return None
    try:
        nodes = data["data"]["repository"]["discussions"]["nodes"]
        result = []
        for disc in nodes:
            labels = [n["name"] for n in (disc.get("labels", {}).get("nodes") or [])]
            result.append({
                "number": disc["number"],
                "title": disc.get("title", ""),
                "body": disc.get("body", ""),
                "labels": labels,
                "updated_at": disc.get("updatedAt", ""),
            })
        return result
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_fresh(cached_at: str) -> bool:
    if not cached_at:
        return False
    try:
        t = time.mktime(time.strptime(cached_at, "%Y-%m-%dT%H:%M:%SZ"))
        return (time.time() - t) < _TTL
    except ValueError:
        return False


def _cache_row(con: sqlite3.Connection, record: dict) -> None:
    labels_json = json.dumps(record.get("labels", []))
    con.execute(
        "INSERT INTO discussion_cache(number, body, title, labels, updated_at, cached_at) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(number) DO UPDATE SET "
        "  body=excluded.body, title=excluded.title, labels=excluded.labels, "
        "  updated_at=excluded.updated_at, cached_at=excluded.cached_at",
        (
            record["number"],
            record.get("body", ""),
            record.get("title", ""),
            labels_json,
            record.get("updated_at", ""),
            _now_iso(),
        ),
    )


def _read_row(con: sqlite3.Connection, number: int) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM discussion_cache WHERE number = ?", (number,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Status values returned by _get_record: what kind of read produced the result.
#   "cached"        — served from a TTL-fresh cache row, no fetch attempted
#   "fetched"       — live GraphQL fetch succeeded (fresh=True or cache miss/stale)
#   "stale_fallback"— fetch failed, served a stale cached row instead
#   "empty"         — fetch failed and nothing was cached either

def _get_record(number: int, fresh: bool) -> tuple[Optional[dict], str]:
    """Shared read path for get_body/get. Returns (record_dict_or_None, status).

    record_dict has keys: number, title, body, labels, updated_at, cached_at.
    """
    with _conn() as con:
        row = _read_row(con, number)
        if row and not fresh and _is_fresh(row["cached_at"]):
            _inc("hit")
            return {
                "number": number,
                "title": row["title"],
                "body": row["body"],
                "labels": json.loads(row["labels"] or "[]"),
                "updated_at": row["updated_at"],
                "cached_at": row["cached_at"],
            }, "cached"

    # Cache miss, stale, or fresh=True forcing a live read — fetch.
    _inc("miss")
    record = _fetch_one(number)

    if record is None:
        # Graceful degradation: return stale value if available rather than failing
        # hard. This applies to fresh=True reads too — a transient GraphQL blip must
        # not turn an intermittent staleness bug into a spawn-gate outage.
        if row:
            sys.stderr.write(
                f"[discussion_cache] WARNING: GraphQL failed, returning stale body for #{number}\n"
            )
            return {
                "number": number,
                "title": row["title"],
                "body": row["body"],
                "labels": json.loads(row["labels"] or "[]"),
                "updated_at": row["updated_at"],
                "cached_at": row["cached_at"],
            }, "stale_fallback"
        return None, "empty"

    with _conn() as con:
        _cache_row(con, record)

    return {**record, "cached_at": _now_iso()}, "fetched"


def get_body(number: int, fresh: bool = False) -> str:
    """Return the discussion body.

    fresh=False (default): serve from cache when the TTL is still fresh — this is
        the bulk/context-assembly path the cache exists to optimise.
    fresh=True: bypass the TTL and re-fetch unconditionally. On GraphQL failure this
        still falls back to a stale cached value (with a stderr warning) rather than
        failing hard — use `get_body_status` if the caller needs to distinguish a
        genuinely fresh read from a stale fallback.

    Returns "" when nothing is available at all.
    """
    record, _status = _get_record(number, fresh)
    return record["body"] if record else ""


def get_body_status(number: int, fresh: bool = False) -> tuple[str, str]:
    """Like get_body, but also returns the read status.

    Returns (body, status) where status is one of "cached", "fetched",
    "stale_fallback", "empty". Callers that must not silently trust a stale
    fallback (e.g. the spawn gate) should branch on status rather than just body.
    """
    record, status = _get_record(number, fresh)
    return (record["body"] if record else ""), status


def get(number: int, fresh: bool = False) -> dict:
    """Return full cached record. Fetches if stale/missing, or unconditionally if fresh=True."""
    record, _status = _get_record(number, fresh)
    return record or {}


def list_open() -> list[dict]:
    """Fetch ALL open discussions in one GraphQL call, update cache, return list."""
    records = _fetch_all_open()
    if records is None:
        # Fallback: return whatever we have cached
        sys.stderr.write(
            "[discussion_cache] WARNING: GraphQL failed in list_open, returning stale cache\n"
        )
        with _conn() as con:
            rows = con.execute("SELECT * FROM discussion_cache").fetchall()
        return [
            {
                "number": r["number"],
                "title": r["title"],
                "body": r["body"],
                "labels": json.loads(r["labels"] or "[]"),
                "updated_at": r["updated_at"],
                "cached_at": r["cached_at"],
            }
            for r in rows
        ]

    with _conn() as con:
        for record in records:
            _cache_row(con, record)

    return [{**r, "cached_at": _now_iso()} for r in records]


def invalidate(number: int) -> None:
    """Clear cache entry so next read forces a fresh GraphQL fetch."""
    with _conn() as con:
        con.execute(
            "UPDATE discussion_cache SET cached_at = '' WHERE number = ?", (number,)
        )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _usage() -> None:
    sys.stderr.write(
        "Usage:\n"
        "  python3 backend/discussion_cache.py get-body <N> [--fresh]\n"
        "  python3 backend/discussion_cache.py get <N> [--fresh]\n"
        "  python3 backend/discussion_cache.py list-open\n"
        "  python3 backend/discussion_cache.py invalidate <N>\n"
        "  python3 backend/discussion_cache.py stats\n"
        "\n"
        "get-body exit codes: 0=printed (live if --fresh), 1=nothing available,\n"
        "  3=--fresh requested but fetch failed; stdout holds a STALE fallback.\n"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _usage()
        sys.exit(1)

    cmd = args[0]

    if cmd == "get-body":
        if len(args) < 2:
            sys.stderr.write("get-body requires a discussion number\n")
            sys.exit(1)
        fresh = "--fresh" in args[2:]
        body, status = get_body_status(int(args[1]), fresh=fresh)
        sys.stdout.write(body)
        if not body:
            sys.exit(1)
        if fresh and status == "stale_fallback":
            sys.exit(3)
        sys.exit(0)

    elif cmd == "get":
        if len(args) < 2:
            sys.stderr.write("get requires a discussion number\n")
            sys.exit(1)
        fresh = "--fresh" in args[2:]
        record = get(int(args[1]), fresh=fresh)
        print(json.dumps(record, indent=2))

    elif cmd == "list-open":
        records = list_open()
        print(json.dumps(records, indent=2))

    elif cmd == "invalidate":
        if len(args) < 2:
            sys.stderr.write("invalidate requires a discussion number\n")
            sys.exit(1)
        invalidate(int(args[1]))
        print(f"invalidated #{args[1]}")

    elif cmd == "stats":
        print(json.dumps(get_stats(), indent=2))

    else:
        sys.stderr.write(f"Unknown command: {cmd}\n")
        _usage()
        sys.exit(1)
