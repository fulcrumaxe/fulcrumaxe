#!/usr/bin/env python3
"""scripts/audit-dashboard-tiles.py

Audit every dashboard tile in dashboard/src/pages/{stats,runs,kpi,pr,fleet}/
for three quality checks:

  A) Read-only  — the RPC handler does NOT write to state files, does NOT
                  increment counters, does NOT trigger spawns.
  B) Live writer — the data source the handler reads from has a registered
                   writer in backend.stats_writer.registered_metrics() or
                   reads from a live system source (fleet discovery, agent_run
                   table, git log, GitHub API, etc.).
  C) Honest empty-state — the tile's TSX renders a proper empty-state component
                          when data is empty/null, NOT a hardcoded "0%" or
                          "unknown" silent fake.

Outputs a Markdown table to wiki/Dashboard-Tile-Audit.md.
Prints suggested follow-up Discussions for any failing tile.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional  # noqa: F401

# Repo root — script is in scripts/, go up one level.
REPO_ROOT = Path(__file__).parent.parent

TILE_DIRS = [
    REPO_ROOT / "dashboard" / "src" / "pages" / "stats",
    REPO_ROOT / "dashboard" / "src" / "pages" / "runs",
    REPO_ROOT / "dashboard" / "src" / "pages" / "kpi",
    REPO_ROOT / "dashboard" / "src" / "pages" / "pr",
    REPO_ROOT / "dashboard" / "src" / "pages" / "fleet",
]

RPC_HANDLER_DIRS = [
    REPO_ROOT / "backend" / "rpc",
    REPO_ROOT / "backend",  # server.py inline handlers
]

# --------------------------------------------------------------------------- #
# Write-op patterns — any of these in an RPC handler = not read-only
# NOTE: list.append() building results in memory is NOT a write.
#       subprocess.run() for read-only queries is NOT a write.
#       We only flag writes to persistent state: files, DB mutations, spawns.
# --------------------------------------------------------------------------- #
_WRITE_PATTERNS = [
    re.compile(r"\bos\.system\b"),
    re.compile(r"\bos\.popen\b"),
    re.compile(r"open\([^)]+['\"]w['\"]"),      # open(..., "w")
    re.compile(r"open\([^)]+['\"]a['\"]"),      # open(..., "a")
    re.compile(r"[a-zA-Z_]+\.write\("),          # file_handle.write() — not list.write
    re.compile(r"stats_writer\.record\("),
    re.compile(r"record_loop_iter\("),
    re.compile(r"_agent_spawned\("),
    re.compile(r"spawn_agent\("),
    re.compile(r"claude\s*-p\b"),
    re.compile(r"\.execute\([^)]*INSERT"),
    re.compile(r"\.execute\([^)]*UPDATE"),
    re.compile(r"\.execute\([^)]*DELETE"),
    re.compile(r"\.execute\([^)]*CREATE(?!\s+TABLE IF NOT EXISTS)"),
    # subprocess that mutates state — spawns agents, writes files
    re.compile(r'subprocess.*\["python3".*"spawn'),
    re.compile(r'subprocess.*spawn.agent'),
]

# We allow reap_stale() in fleet_concurrency — it's a housekeeping write but not
# a metric write, and is well-understood. Flag it as a note, not a fail.
_WRITE_EXEMPTIONS: set[str] = {"reap_stale()", "fleet.concurrency"}

# --------------------------------------------------------------------------- #
# Live-source categories — if the handler reads from one of these, check B = pass
# --------------------------------------------------------------------------- #
# Metrics that have registered writers in stats_writer.registered_metrics()
# We load these dynamically below.
_REGISTERED_METRICS: frozenset[str] = frozenset()

# Patterns in handler source that indicate a live (non-fixture) data source:
_LIVE_SOURCE_PATTERNS = [
    re.compile(r"duckdb\.connect"),
    re.compile(r"stats_writer\.\w+"),          # reads from stats writer functions
    re.compile(r"agent_run_reader"),
    re.compile(r"kpi_engine"),
    re.compile(r"fleet\.discovery"),
    re.compile(r"fleet\.concurrency"),
    re.compile(r"fleet\.cost_summary"),
    re.compile(r"cost_summary"),
    re.compile(r"read_cost_summary"),
    re.compile(r"cost_tracker"),               # cost_tracker.py subprocess reader
    re.compile(r"github\b"),
    re.compile(r"gh api\b"),
    re.compile(r"dial_usage"),
    re.compile(r"get_duckdb_writers"),
    re.compile(r"hook.events"),
    re.compile(r"audit\.jsonl"),
    re.compile(r"agent.feed"),
    re.compile(r"loop.metrics"),
    re.compile(r"conn\.execute"),              # DuckDB/SQLite read
    re.compile(r"\.fetchall\(\)"),
    re.compile(r"\.fetchone\(\)"),
    re.compile(r"from backend\."),
    re.compile(r"import backend\."),
    re.compile(r"discover_projects"),
    re.compile(r"fleet_cap"),
    re.compile(r"count_fleet"),
    re.compile(r"subprocess\.run"),            # subprocess read queries (e.g. cost_tracker)
]

# Patterns in handler source that suggest FIXTURE / seed data (bad):
_FIXTURE_PATTERNS = [
    re.compile(r"e2e.fixtures\.json"),
    re.compile(r"fixture"),
    re.compile(r"HARDCODED"),
    re.compile(r"seed"),
]

# --------------------------------------------------------------------------- #
# Empty-state patterns in TSX — presence of any of these = honest empty state
# --------------------------------------------------------------------------- #
_EMPTY_STATE_GOOD_PATTERNS = [
    re.compile(r"data-testid=['\"].*empty"),
    re.compile(r"No\s+\w+.*data"),
    re.compile(r"No\s+\w+.*yet"),
    re.compile(r"No\s+\w+.*found"),
    re.compile(r"No\s+\w+.*discovered"),
    re.compile(r"No\s+\w+.*runs"),
    re.compile(r"No\s+\w+.*agent"),
    re.compile(r"No\s+\w+.*PRs"),
    re.compile(r"No\s+\w+.*projects"),
    re.compile(r"No\s+\w+.*metrics"),
    re.compile(r"No\s+\w+.*in last"),
    re.compile(r"empty.state"),
    re.compile(r"EmptyState"),
    re.compile(r"sharedStyles\.state"),
    re.compile(r"styles\.state"),
    re.compile(r"empty-state"),
    re.compile(r"\.length === 0"),
    re.compile(r"\.length == 0"),
    re.compile(r"applicable.*false"),
    re.compile(r"!hasActivity"),
    re.compile(r"Not enough data"),
    re.compile(r"Insufficient data"),
    re.compile(r"null.*\?.*<div"),                # null ? <div>... : null
    re.compile(r"data\s*&&\s*data\."),            # data && data.foo (safe guard)
    re.compile(r"sample_size\s*[<>=]"),           # sample_size < 5 → N/A
]

# Patterns that flag a silent fake (bad) — these must be in a JSX render context,
# not in a utility function. We look for the pattern being RETURNED from JSX.
# We only flag "0%" or "unknown" if they appear as the ONLY content of a data cell
# (e.g. <td>0%</td> or <span>unknown</span>) — not in format helper functions.
_SILENT_FAKE_PATTERNS = [
    re.compile(r'[>]\s*0%\s*[<]'),         # >0%< in JSX
    re.compile(r"[>]\s*unknown\s*[<]"),     # >unknown< in JSX
    re.compile(r'defaultValue.*=.*"0%"'),   # defaultValue="0%"
    re.compile(r"HARDCODED", re.IGNORECASE),
    re.compile(r'return\s+["\']0%["\']'),   # return "0%"  (not just a display fn returning "unknown")
]

# --------------------------------------------------------------------------- #
# RPC method → file-name mapping (auto-discovered from @_rpc_method decorators)
# --------------------------------------------------------------------------- #

# Pattern: @_rpc_method("method.name") or @_rpc_method('method.name')
_RPC_DECORATOR_RE = re.compile(r"""@_rpc_method\(\s*["'](\S+?)["']\s*\)""")
# Pattern: from backend.rpc import <module_name>
_RPC_IMPORT_RE = re.compile(r"""from backend\.rpc import (\w+)""")

# Cached result — built once on first call to _resolve_handler_file
_RPC_METHOD_MAP_CACHE: dict[str, str] | None = None


def _build_rpc_method_map() -> dict[str, str]:
    """Walk backend/server.py and backend/rpc/*.py to discover @_rpc_method decorators.

    For each decorated function in server.py, inspects the next ~10 lines of the
    function body. If the body imports from ``backend.rpc import <module>``, the
    handler is resolved to ``backend/rpc/<module>.py``. Otherwise it lives inline
    in ``backend/server.py``.

    Files in backend/rpc/*.py are also scanned in case any declare their own
    @_rpc_method decorator directly (rare, but future-proof).

    Duplicate registrations are reported to stderr rather than silently overwritten.
    """
    mapping: dict[str, str] = {}

    # ---- 1. Walk backend/rpc/*.py for any direct @_rpc_method decorators ----
    rpc_dir = REPO_ROOT / "backend" / "rpc"
    if rpc_dir.exists():
        for py_file in sorted(rpc_dir.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            try:
                src = py_file.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = str(py_file.relative_to(REPO_ROOT))
            for m in _RPC_DECORATOR_RE.finditer(src):
                method = m.group(1)
                if method in mapping:
                    print(
                        f"[warn] duplicate @_rpc_method({method!r}): "
                        f"already mapped to {mapping[method]!r}, "
                        f"also found in {rel!r}",
                        file=sys.stderr,
                    )
                else:
                    mapping[method] = rel

    # ---- 2. Walk backend/server.py — parse method + resolve delegate module --
    server_py = REPO_ROOT / "backend" / "server.py"
    if server_py.exists():
        try:
            lines = server_py.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

        i = 0
        while i < len(lines):
            dec_match = _RPC_DECORATOR_RE.match(lines[i].strip())
            if dec_match:
                method = dec_match.group(1)
                # Look ahead up to 12 lines for the function body to find a
                # "from backend.rpc import <module>" delegation.
                delegate_file: str = "backend/server.py"
                for j in range(i + 1, min(i + 13, len(lines))):
                    imp_match = _RPC_IMPORT_RE.search(lines[j])
                    if imp_match:
                        mod_name = imp_match.group(1)
                        candidate = REPO_ROOT / "backend" / "rpc" / f"{mod_name}.py"
                        if candidate.exists():
                            delegate_file = str(candidate.relative_to(REPO_ROOT))
                        break
                    # Stop at next decorator (new handler starts)
                    stripped = lines[j].strip()
                    if stripped.startswith("@_rpc_method") and j != i:
                        break

                if method in mapping:
                    print(
                        f"[warn] duplicate @_rpc_method({method!r}): "
                        f"already mapped to {mapping[method]!r}, "
                        f"also found in backend/server.py",
                        file=sys.stderr,
                    )
                else:
                    mapping[method] = delegate_file
            i += 1

    return mapping


def _get_rpc_method_map() -> dict[str, str]:
    """Return the cached RPC method map, building it on first call."""
    global _RPC_METHOD_MAP_CACHE
    if _RPC_METHOD_MAP_CACHE is None:
        _RPC_METHOD_MAP_CACHE = _build_rpc_method_map()
    return _RPC_METHOD_MAP_CACHE

# Tiles that use a non-jsonRpc API module (kpi.ts, not jsonRpc directly)
_KPI_API_MAP: dict[str, str] = {
    "getVelocity": "kpi.history",
    "getCycleTime": "kpi.cycle_time",
    "getCostByDiscussion": "cost.by_discussion",
}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class TileAudit:
    tile_path: str
    rpc_method: str
    handler_file: str
    check_a_pass: bool
    check_b_pass: bool
    check_c_pass: bool
    check_a_evidence: str = ""
    check_b_evidence: str = ""
    check_c_evidence: str = ""
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_registered_metrics() -> frozenset[str]:
    """Load registered_metrics() from backend.stats_writer, silently skip on failure."""
    global _REGISTERED_METRICS
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from backend.stats_writer import registered_metrics  # type: ignore[import]
        _REGISTERED_METRICS = registered_metrics()
    except Exception as exc:
        print(f"[warn] could not load registered_metrics: {exc}", file=sys.stderr)
        _REGISTERED_METRICS = frozenset()
    return _REGISTERED_METRICS


def _extract_rpc_methods(tsx_source: str) -> list[str]:
    """Extract ALL RPC method names from a TSX tile file.

    Handles both:
      jsonRpc<T>('stats.foo', ...)  — may appear multiple times in one tile
      getVelocity / getCycleTime / getCostByDiscussion (kpi.ts functions)

    Returns a list with at least one element.  Multi-RPC tiles (e.g.
    RoleSuccessRateTile) return all methods so each handler can be checked.
    """
    # Direct jsonRpc calls — find ALL occurrences
    methods: list[str] = re.findall(
        r"jsonRpc\s*(?:<[^>]+>)?\s*\(\s*['\"]([^'\"]+)['\"]", tsx_source
    )
    if methods:
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for m in methods:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique

    # kpi.ts function calls
    for fn_name, method in _KPI_API_MAP.items():
        if fn_name in tsx_source:
            return [method]

    # Tiles that receive data as props (pr/ subdirectory tiles like PrMetaCard)
    if "Props" in tsx_source and "jsonRpc" not in tsx_source:
        return ["(props-only)"]

    return ["(unknown)"]


def _extract_rpc_method(tsx_source: str) -> str:
    """Compat shim — return first RPC method name (kept for test compatibility)."""
    return _extract_rpc_methods(tsx_source)[0]


def _resolve_handler_file(rpc_method: str) -> str:
    """Return the relative path to the handler file for this RPC method."""
    return _get_rpc_method_map().get(rpc_method, "(unknown)")


def _handler_source(handler_rel: str, rpc_method: str | None = None) -> str:
    """Return the source of the handler file (or handler section for server.py).

    For server.py, we extract only the handler function for the given rpc_method
    to avoid false positives from unrelated methods in the same file.
    """
    if handler_rel in ("(unknown)", "(props-only)"):
        return ""
    path = REPO_ROOT / handler_rel
    if not path.exists():
        return ""
    full_src = path.read_text(encoding="utf-8")

    # For server.py, extract only the relevant handler section
    if handler_rel == "backend/server.py" and rpc_method:
        # Find the @_rpc_method("method") decorator and extract until the next one
        method_escaped = re.escape(rpc_method)
        pattern = re.compile(
            r'@_rpc_method\(["\']' + method_escaped + r'["\'][^\n]*\n'
            r'(?:.*?\n)*?'
            r'(?=@_rpc_method\b|\Z)',
            re.MULTILINE,
        )
        m = pattern.search(full_src)
        if m:
            return m.group(0)
        # Fallback: search for the method name context (±50 lines)
        for i, line in enumerate(full_src.splitlines()):
            if rpc_method in line and "_rpc_method" in line:
                lines = full_src.splitlines()
                start = max(0, i)
                end = min(len(lines), i + 60)
                return "\n".join(lines[start:end])

    return full_src


def _check_a_read_only(rpc_method: str, handler_src: str, handler_file: str) -> tuple[bool, str]:
    """Check A: handler is read-only (no persistent state writes, no spawns)."""
    if not handler_src:
        return True, "handler file not found — assumed read-only"

    # auth_retry.record is explicitly a write op (records auth retries)
    if rpc_method == "auth_retry.record":
        return False, "auth_retry.record writes counter — intentional write"

    found_writes: list[str] = []
    for pat in _WRITE_PATTERNS:
        if pat.search(handler_src):
            found_writes.append(pat.pattern)

    if found_writes:
        return False, f"write patterns found: {', '.join(found_writes[:3])}"
    return True, "no persistent state writes or spawn calls"


_STALE_THRESHOLD_SECONDS = 24 * 3600  # 24 hours


def _db_freshness_check(handler_src: str, rpc_method: str) -> tuple[bool, str] | None:
    """Query actual DB/file freshness when handler reads from a known live source.

    Returns (pass, evidence) if a freshness check was performed, or None if
    no recognized live source could be probed (caller falls back to static check).

    Sources checked:
      - stats.duckdb metric_event table  (MAX(ts) per inferred metric name)
      - agent_runs table                  (MAX(end_ts))
      - audit.jsonl                       (last line ts)
    """
    try:
        import duckdb  # type: ignore[import]
    except ImportError:
        return None  # duckdb not available — skip live check

    now_ts = datetime.now(timezone.utc).timestamp()

    # ---- agent_runs table (runs.* handlers) --------------------------------
    if re.search(r"agent_run_reader|agent_runs\b", handler_src):
        # Try to find the agent_runs DB (same duckdb or state.db SQLite)
        try:
            from backend.state_paths import STATS_DB  # noqa: PLC0415
            if STATS_DB.exists():
                conn = duckdb.connect(str(STATS_DB), read_only=True)
                try:
                    row = conn.execute(
                        "SELECT MAX(end_ts) FROM agent_runs"
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[0] is not None:
                    max_ts_raw = row[0]
                    # DuckDB returns a datetime object or string
                    if isinstance(max_ts_raw, datetime):
                        max_ts = max_ts_raw.replace(tzinfo=timezone.utc).timestamp() \
                            if max_ts_raw.tzinfo is None else max_ts_raw.timestamp()
                    else:
                        max_ts = datetime.fromisoformat(str(max_ts_raw).replace("Z", "+00:00")).timestamp()
                    age_h = (now_ts - max_ts) / 3600
                    if now_ts - max_ts > _STALE_THRESHOLD_SECONDS:
                        return False, f"stale writer (last write {age_h:.1f}h ago — agent_runs.end_ts)"
                    return True, f"live writer: agent_runs last write {age_h:.1f}h ago"
        except Exception as exc:
            return None  # can't probe — fall back to static

    # ---- audit.jsonl -------------------------------------------------------
    if re.search(r"audit\.jsonl", handler_src):
        try:
            from backend.state_paths import AUDIT_LOG  # noqa: PLC0415
            if AUDIT_LOG.exists():
                # Read last non-empty line
                last_line: str | None = None
                with AUDIT_LOG.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            last_line = line
                if last_line:
                    data = json.loads(last_line)
                    ts_str = data.get("ts") or data.get("timestamp") or data.get("time")
                    if ts_str:
                        ts_str = str(ts_str).replace("Z", "+00:00")
                        max_ts = datetime.fromisoformat(ts_str).timestamp()
                        age_h = (now_ts - max_ts) / 3600
                        if now_ts - max_ts > _STALE_THRESHOLD_SECONDS:
                            return False, f"stale writer (last write {age_h:.1f}h ago — audit.jsonl)"
                        return True, f"live writer: audit.jsonl last write {age_h:.1f}h ago"
        except Exception:
            return None  # can't probe — fall back to static

    # ---- stats.duckdb metric_event -----------------------------------------
    # Trigger when handler reads duckdb directly OR delegates to stats_writer
    if re.search(
        r"duckdb\.connect|conn\.execute|\.fetchall\(\)|\.fetchone\(\)"
        r"|from backend\.stats_writer|import backend\.stats_writer"
        r"|stats_writer\.\w+",
        handler_src,
    ):
        try:
            # Infer metric name from rpc_method (e.g. stats.role_success_rate → role_success_rate)
            metric_guess: str | None = None
            if "." in rpc_method and not rpc_method.startswith("("):
                suffix = rpc_method.split(".", 1)[1]
                metric_guess = suffix  # e.g. "role_success_rate"

            from backend.state_paths import STATS_DB  # noqa: PLC0415
            if STATS_DB.exists():
                conn = duckdb.connect(str(STATS_DB), read_only=True)
                try:
                    if metric_guess:
                        row = conn.execute(
                            "SELECT MAX(ts) FROM metric_event WHERE metric = ?",
                            [metric_guess],
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT MAX(ts) FROM metric_event"
                        ).fetchone()
                finally:
                    conn.close()
                if row and row[0] is not None:
                    max_ts_raw = row[0]
                    if isinstance(max_ts_raw, datetime):
                        max_ts = max_ts_raw.replace(tzinfo=timezone.utc).timestamp() \
                            if max_ts_raw.tzinfo is None else max_ts_raw.timestamp()
                    else:
                        max_ts = datetime.fromisoformat(str(max_ts_raw).replace("Z", "+00:00")).timestamp()
                    age_h = (now_ts - max_ts) / 3600
                    if now_ts - max_ts > _STALE_THRESHOLD_SECONDS:
                        return False, f"stale writer (last write {age_h:.1f}h ago — metric_event)"
                    return True, f"live writer: metric_event last write {age_h:.1f}h ago"
        except Exception:
            return None  # can't probe — fall back to static

    return None  # no recognized live DB source to probe


def _check_b_live_writer(rpc_method: str, handler_src: str) -> tuple[bool, str]:
    """Check B: data source has a live writer (not fixture/seed).

    First attempts a runtime DB freshness probe (24h window).  Falls back to
    static pattern matching when the DB is not reachable or not applicable.
    """
    if not handler_src:
        # props-only tiles (pr/ tiles) receive data from a parent page
        if rpc_method == "(props-only)":
            return True, "props-only tile — data source is parent page's RPC"
        return False, "handler not found"

    # Check for fixture fallback (present but gated behind AF_E2E_FIXTURES env)
    has_fixture = any(pat.search(handler_src) for pat in _FIXTURE_PATTERNS)
    fixture_gated = "AF_E2E_FIXTURES" in handler_src

    # Check for live data source patterns (static)
    live_hits = [pat.pattern for pat in _LIVE_SOURCE_PATTERNS if pat.search(handler_src)]

    if not live_hits and has_fixture and not fixture_gated:
        return False, "only fixture/seed data source found, no live reader"

    if has_fixture and not fixture_gated:
        return False, f"ungated fixture fallback found (live hits: {live_hits[:2]})"

    if live_hits:
        # Static check passed — now do runtime freshness probe if possible
        freshness = _db_freshness_check(handler_src, rpc_method)
        if freshness is not None:
            return freshness

        note = live_hits[0] if live_hits else "unknown"
        if has_fixture:
            return True, f"live source present ({note}); fixture is dev-mode only (AF_E2E_FIXTURES)"
        return True, f"live source: {note}"

    # auth_retry.record — it's a write handler, B doesn't really apply
    if rpc_method == "auth_retry.record":
        return True, "write handler — no live reader needed"

    return False, "no recognized live data source pattern"


def _check_c_honest_empty(tsx_source: str, rpc_method: str) -> tuple[bool, str]:
    """Check C: tile renders an honest empty-state, not a silent fake."""
    if rpc_method == "(props-only)":
        # pr/ tiles get null-guarded data from parent; check for null prop guard
        has_null_guard = bool(re.search(r"quality\s*&&|quality\s*\?|pr\s*&&|pr\s*\?", tsx_source))
        if has_null_guard:
            return True, "null-guarded props"
        # PrMetaCard and others always receive a pr prop — no empty state needed
        return True, "props always provided by parent page — no empty state needed"

    # Check for silent fakes first
    for pat in _SILENT_FAKE_PATTERNS:
        if pat.search(tsx_source):
            return False, f"silent fake pattern found: {pat.pattern}"

    # Check for honest empty state
    for pat in _EMPTY_STATE_GOOD_PATTERNS:
        if pat.search(tsx_source):
            return True, f"empty-state pattern present: {pat.pattern}"

    # If tile has no data state handling at all but shows static content only
    # (e.g. DiscussionLinkCard), that's still honest
    has_data_state = bool(re.search(r"useState\b|setData\b|useEffect\b|data\s*===?\s*null", tsx_source))
    if not has_data_state:
        return True, "static/prop-driven component — no async state needed"

    return False, "no honest empty-state pattern found"


def _tile_relative_path(tile_path: Path) -> str:
    """Return path relative to repo root."""
    try:
        return str(tile_path.relative_to(REPO_ROOT))
    except ValueError:
        return str(tile_path)


# --------------------------------------------------------------------------- #
# Main audit
# --------------------------------------------------------------------------- #
def audit_tiles() -> list[TileAudit]:
    _load_registered_metrics()
    results: list[TileAudit] = []

    for tile_dir in TILE_DIRS:
        if not tile_dir.exists():
            print(f"[warn] tile dir not found: {tile_dir}", file=sys.stderr)
            continue

        tsx_files = sorted(tile_dir.glob("*.tsx"))
        for tsx_path in tsx_files:
            # Skip index files and test files
            if tsx_path.name in ("index.ts", "index.tsx", "styles.ts") or "__tests__" in str(tsx_path):
                continue
            # Skip non-tile component files in pr/ (cards are tiles too)
            tsx_source = tsx_path.read_text(encoding="utf-8")

            rpc_methods = _extract_rpc_methods(tsx_source)
            tile_rel = _tile_relative_path(tsx_path)

            # Run A/B checks against EACH RPC handler; tile passes only if ALL pass.
            # Check C is per-tile (not per-RPC handler).
            all_a_pass = True
            all_b_pass = True
            ev_a_parts: list[str] = []
            ev_b_parts: list[str] = []
            handler_files: list[str] = []

            for rpc_method in rpc_methods:
                handler_rel = _resolve_handler_file(rpc_method)
                handler_src = _handler_source(handler_rel, rpc_method)
                check_a, ev_a = _check_a_read_only(rpc_method, handler_src, handler_rel)
                check_b, ev_b = _check_b_live_writer(rpc_method, handler_src)
                if not check_a:
                    all_a_pass = False
                if not check_b:
                    all_b_pass = False
                ev_a_parts.append(f"{rpc_method}: {ev_a}")
                ev_b_parts.append(f"{rpc_method}: {ev_b}")
                handler_files.append(handler_rel)

            # Use primary (first) RPC method for display; report all in evidence
            primary_rpc = rpc_methods[0]
            rpc_display = primary_rpc if len(rpc_methods) == 1 else (
                primary_rpc + f" (+{len(rpc_methods)-1} more)"
            )
            primary_handler = handler_files[0] if handler_files else "(unknown)"

            check_c, ev_c = _check_c_honest_empty(tsx_source, primary_rpc)

            results.append(TileAudit(
                tile_path=tile_rel,
                rpc_method=rpc_display,
                handler_file=primary_handler,
                check_a_pass=all_a_pass,
                check_b_pass=all_b_pass,
                check_c_pass=check_c,
                check_a_evidence="; ".join(ev_a_parts),
                check_b_evidence="; ".join(ev_b_parts),
                check_c_evidence=ev_c,
            ))

    return results


# --------------------------------------------------------------------------- #
# Report generation
# --------------------------------------------------------------------------- #
def _status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def generate_report(results: list[TileAudit]) -> str:
    total = len(results)
    a_pass = sum(1 for r in results if r.check_a_pass)
    b_pass = sum(1 for r in results if r.check_b_pass)
    c_pass = sum(1 for r in results if r.check_c_pass)

    lines = [
        "# Dashboard Tile Audit",
        "",
        "Auto-generated by `scripts/audit-dashboard-tiles.py`.",
        "",
        "## Summary",
        "",
        f"| Check | Pass | Fail | Total |",
        f"|-------|------|------|-------|",
        f"| A: Read-only handler | {a_pass} | {total - a_pass} | {total} |",
        f"| B: Live writer | {b_pass} | {total - b_pass} | {total} |",
        f"| C: Honest empty-state | {c_pass} | {total - c_pass} | {total} |",
        "",
        "## Check Definitions",
        "",
        "- **A) Read-only** — handler does NOT write to state files, increment counters, or trigger spawns.",
        "- **B) Live writer** — the data source has an active writer (DuckDB, agent_run table, git log, fleet discovery, etc.).",
        "- **C) Honest empty-state** — tile renders an empty-state component when data is empty/null, not a hardcoded `0%` or `unknown`.",
        "",
        "## Tile Results",
        "",
        "| Tile | RPC Method | Handler | A | B | C | Notes |",
        "|------|-----------|---------|---|---|---|-------|",
    ]

    for r in sorted(results, key=lambda x: x.tile_path):
        tile_name = Path(r.tile_path).name
        a = _status(r.check_a_pass)
        b = _status(r.check_b_pass)
        c = _status(r.check_c_pass)
        notes = "; ".join(r.notes) if r.notes else ""
        handler_short = r.handler_file.replace("backend/rpc/", "rpc/")
        lines.append(
            f"| `{tile_name}` | `{r.rpc_method}` | `{handler_short}` "
            f"| {a} | {b} | {c} | {notes} |"
        )

    lines.extend(["", "## Evidence Detail", ""])

    for r in sorted(results, key=lambda x: x.tile_path):
        tile_name = Path(r.tile_path).name
        all_pass = r.check_a_pass and r.check_b_pass and r.check_c_pass
        lines.append(f"### `{tile_name}`")
        lines.append(f"- **Tile**: `{r.tile_path}`")
        lines.append(f"- **RPC**: `{r.rpc_method}`")
        lines.append(f"- **Handler**: `{r.handler_file}`")
        a_icon = "✓" if r.check_a_pass else "✗"
        b_icon = "✓" if r.check_b_pass else "✗"
        c_icon = "✓" if r.check_c_pass else "✗"
        lines.append(f"- **A** {a_icon}: {r.check_a_evidence}")
        lines.append(f"- **B** {b_icon}: {r.check_b_evidence}")
        lines.append(f"- **C** {c_icon}: {r.check_c_evidence}")
        lines.append("")

    # Failing tiles — suggest follow-up
    failing = [r for r in results if not (r.check_a_pass and r.check_b_pass and r.check_c_pass)]
    if failing:
        lines.extend(["## Follow-up Suggestions", ""])
        for r in failing:
            tile_name = Path(r.tile_path).name
            issues = []
            if not r.check_a_pass:
                issues.append("A: read-only violation")
            if not r.check_b_pass:
                issues.append("B: no live writer")
            if not r.check_c_pass:
                issues.append("C: missing empty-state")
            lines.append(
                f"- **{tile_name}** — {', '.join(issues)}"
            )
        lines.append("")

    lines.append(f"*{total} tiles scored. Generated by scripts/audit-dashboard-tiles.py.*")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Follow-up suggestions (printed to stdout)
# --------------------------------------------------------------------------- #
def print_followups(results: list[TileAudit]) -> None:
    failing = [r for r in results if not (r.check_a_pass and r.check_b_pass and r.check_c_pass)]
    if not failing:
        print("All tiles pass all checks — no follow-up Discussions needed.")
        return

    print("\n--- Suggested follow-up Discussions ---")
    for r in failing:
        tile_name = Path(r.tile_path).name
        issues = []
        if not r.check_a_pass:
            issues.append(f"read-only violation ({r.check_a_evidence})")
        if not r.check_b_pass:
            issues.append(f"no live writer ({r.check_b_evidence})")
        if not r.check_c_pass:
            issues.append(f"missing honest empty-state ({r.check_c_evidence})")
        issue_str = "; ".join(issues)
        print(f"Suggested follow-up Discussion: Fix {tile_name} — {issue_str}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    print("Auditing dashboard tiles…", file=sys.stderr)
    results = audit_tiles()

    print(f"Scored {len(results)} tiles.", file=sys.stderr)

    report = generate_report(results)

    wiki_path = REPO_ROOT / "wiki" / "Dashboard-Tile-Audit.md"
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.write_text(report, encoding="utf-8")
    print(f"Report written to {wiki_path}", file=sys.stderr)

    # Also print summary to stdout
    total = len(results)
    a_pass = sum(1 for r in results if r.check_a_pass)
    b_pass = sum(1 for r in results if r.check_b_pass)
    c_pass = sum(1 for r in results if r.check_c_pass)
    print(f"\n=== Dashboard Tile Audit Results ===")
    print(f"Tiles scored: {total}")
    print(f"A (read-only):     {a_pass}/{total} pass")
    print(f"B (live writer):   {b_pass}/{total} pass")
    print(f"C (honest empty):  {c_pass}/{total} pass")

    print_followups(results)


if __name__ == "__main__":
    main()
