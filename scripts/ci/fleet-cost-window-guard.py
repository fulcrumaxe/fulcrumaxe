#!/usr/bin/env python3
"""fleet-cost-window-guard.py — behavioral guard for the Fleet Token Spend
panel's project set and calendar windows (D#2317 PR-b).

Background
----------
The panel read ``LAST 24H = 0`` against ``LAST 7D = 1.2M`` on the busiest
day the team had ever had — 118 runs with token counts, 43 PRs merged.
Three separate defects produced that:

  - ``fleet.cost`` iterated ``discover_projects()``
    (``~/.*-state/project.json``), so it opened ``cost_summary.json`` only
    for seven dead fixtures and never for ``~/.autonomous-forever-state``,
    which is where ``scripts/hooks/post-agent.d/cost-summary.sh`` actually
    writes it. The 1.2M belonged entirely to stale fixtures; the serving
    project's own 1.9M day sat in a file the panel never opened.
  - the writer pruned with ``entries[-7:]`` — the last seven *written*
    entries, whatever their age. The live file spanned 19 days and all of
    it was summed under a label reading "Last 7d".
  - a project with no observation reported ``0``, which is
    indistinguishable from a project measured at zero spend.

This guard is behavioral, not a lint over source text: it seeds real
fixture state dirs with a 60-day-sparse ``cost_summary.json``, calls the
real ``read_cost_summary`` / ``update_cost_summary`` / ``fleet_cost.handle``,
and asserts D#2317 PR-b acceptance items 2-5. Every assertion is against
the fixture's own construction — no project count, name or port from any
particular host appears here.

``resolve_fleet_set()`` is patched to the fixture's own records rather than
left to scan a real ``$HOME``: the set-resolution behaviour it provides is
already covered behaviourally by ``fleet-status-honesty-guard.py``, and
what needs proving here is that ``fleet_cost.handle()`` consumes whatever
that resolver returns — including a runtime-only project that the old
``discover_projects()`` path could never have seen.

stdlib only, no network, no GH_TOKEN, and nothing written outside a
tempdir.

Run from the repo root:

    python3 scripts/ci/fleet-cost-window-guard.py

Exit 0: every check passes.
Exit 1: a check failed — prints one `FAIL <detail>` line per failure.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

FAILURES: list[str] = []


def _fail(detail: str) -> None:
    FAILURES.append(detail)
    print(f"FAIL {detail}")


def _date(offset_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _seed(state_dir: Path, entries: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "cost_summary.json").write_text(
        json.dumps({"updated_at": "", "last_7d": entries}), encoding="utf-8"
    )


def _entry(offset_days: int, tokens: int) -> dict:
    return {"date": _date(offset_days), "input_tokens": tokens, "output_tokens": 0}


# ---------------------------------------------------------------------------
# Item 2 — read_cost_summary filters to a real calendar window
# ---------------------------------------------------------------------------


def check_window_excludes_stale_entries(tmp: Path) -> None:
    from backend.fleet.cost_summary import read_cost_summary
    from backend.fleet.cost_window import WINDOW_DAYS, today_utc, window_dates

    state_dir = tmp / ".window-state"
    old_tokens = 1_000_000
    new_tokens = 10
    _seed(state_dir, [_entry(30, old_tokens), _entry(0, new_tokens)])

    summary = read_cost_summary(state_dir)
    if summary.get("tokens_7d") != new_tokens:
        _fail(
            f"item 2: a {old_tokens}-token entry dated 30 days ago still contributes — "
            f"tokens_7d={summary.get('tokens_7d')!r}, expected {new_tokens}"
        )

    dates = window_dates(today_utc())
    if len(dates) != WINDOW_DAYS or dates[-1] != today_utc():
        _fail(f"item 2: window is not {WINDOW_DAYS} days ending today — {dates!r}")

    # The oldest in-window date must still count; one day older must not.
    edge_dir = tmp / ".edge-state"
    _seed(edge_dir, [_entry(WINDOW_DAYS - 1, 7), _entry(WINDOW_DAYS, 999)])
    edge = read_cost_summary(edge_dir)
    if edge.get("tokens_7d") != 7:
        _fail(
            f"item 2: window edge is wrong — tokens_7d={edge.get('tokens_7d')!r}, "
            "expected only the entry dated today-(WINDOW_DAYS-1) to count"
        )


# ---------------------------------------------------------------------------
# Item 3 — the writer prunes by date, not by entry count
# ---------------------------------------------------------------------------


def check_writer_prunes_by_date(tmp: Path) -> None:
    from backend.fleet.cost_summary import update_cost_summary
    from backend.fleet.cost_window import today_utc, window_dates

    state_dir = tmp / ".sparse-state"
    # 20 sparse entries spanning 60 days. `entries[-7:]` would have kept the
    # seven most recently written of these regardless of age.
    _seed(state_dir, [_entry(offset, 100) for offset in range(60, 0, -3)])

    update_cost_summary(state_dir, input_tokens=1, output_tokens=0)

    kept = json.loads((state_dir / "cost_summary.json").read_text())["last_7d"]
    oldest_allowed = window_dates(today_utc())[0]
    stale = [e for e in kept if e.get("date", "") < oldest_allowed]
    if stale:
        _fail(f"item 3: writer kept entries older than {oldest_allowed} — {stale!r}")
    if not kept:
        _fail("item 3: writer pruned away today's own entry")


# ---------------------------------------------------------------------------
# Items 1, 4, 5 — resolved set, invariant, no-signal vs measured zero
# ---------------------------------------------------------------------------


def check_handler_over_resolved_set(tmp: Path) -> None:
    from backend.rpc import fleet_cost

    # A runtime-only project: no project.json, so the old discover_projects()
    # path could never have seen it — which is the shape of the one project
    # whose cost_summary.json is actually being written.
    runtime_only = tmp / ".runtime-only-state"
    runtime_tokens = 4_242
    _seed(runtime_only, [_entry(0, runtime_tokens)])

    # A stale fixture: a big number, all of it outside the window.
    stale = tmp / ".stale-fixture-state"
    _seed(stale, [_entry(45, 1_000_000)])

    resolved = [
        {"name": "runtime-only", "state_dir": str(runtime_only), "status": "unknown"},
        {"name": "stale-fixture", "state_dir": str(stale), "status": "unknown"},
        {"name": "broken", "state_dir": str(tmp / ".broken-state"),
         "status": "error", "error": "JSON parse error"},
    ]

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    by_name = {p["name"]: p for p in result.get("per_project", [])}

    if len(by_name) != len(resolved):
        _fail(f"item 1: handler returned {len(by_name)} records for {len(resolved)} resolved projects")

    if by_name.get("runtime-only", {}).get("tokens_7d") != runtime_tokens:
        _fail(
            "item 1: a runtime-only project's cost_summary.json was not read — "
            f"record={by_name.get('runtime-only')!r}"
        )

    if result.get("total_7d") != runtime_tokens:
        _fail(
            f"item 1/2: fleet total_7d={result.get('total_7d')!r}, expected {runtime_tokens} "
            "(the stale fixture's out-of-window million must not contribute)"
        )

    # Item 5 — no-signal is not a zero.
    for name in ("stale-fixture", "broken"):
        record = by_name.get(name, {})
        zeroed = [k for k, v in record.items() if k.startswith("tokens_") or k.startswith("projected_")]
        if zeroed:
            _fail(f"item 5: {name} carries token fields with no observation behind them — {record!r}")

    # Item 4 — the invariant, per project and fleet-wide.
    for name, record in by_name.items():
        today, seven = record.get("tokens_today_utc"), record.get("tokens_7d")
        if today is not None and seven is not None and today > seven:
            _fail(f"item 4: {name} reports tokens_today_utc={today} > tokens_7d={seven}")
    total_today, total_7d = result.get("total_today_utc"), result.get("total_7d")
    if total_today is not None and total_7d is not None and total_today > total_7d:
        _fail(f"item 4: fleet total_today_utc={total_today} > total_7d={total_7d}")


def check_all_stale_reports_no_totals(tmp: Path) -> None:
    """A fleet where nothing landed in the window reports no totals at all —
    the case that used to print `0` on the busiest day on record."""
    from backend.rpc import fleet_cost

    stale = tmp / ".everything-stale-state"
    _seed(stale, [_entry(45, 1_000_000)])
    resolved = [{"name": "stale", "state_dir": str(stale), "status": "unknown"}]

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    for key in ("total_today_utc", "total_7d", "projected_eod"):
        if key in result:
            _fail(f"item 5: {key} present ({result[key]!r}) with no in-window observation anywhere")


def check_measured_zero_is_still_reported(tmp: Path) -> None:
    """A project with in-window entries but none dated today HAS been
    measured at zero for today — that number must survive, or the fix
    swaps one lie for another."""
    from backend.rpc import fleet_cost

    quiet = tmp / ".quiet-state"
    _seed(quiet, [_entry(2, 5_000)])
    resolved = [{"name": "quiet", "state_dir": str(quiet), "status": "ok"}]

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    if result.get("total_today_utc") != 0:
        _fail(
            "item 5: a measured zero was dropped — total_today_utc="
            f"{result.get('total_today_utc')!r}, expected 0"
        )
    if result.get("total_7d") != 5_000:
        _fail(f"item 5: total_7d={result.get('total_7d')!r}, expected 5000")


# ---------------------------------------------------------------------------
# Item 6 — no field claims 24h
# ---------------------------------------------------------------------------


def check_no_field_claims_24h(tmp: Path) -> None:
    from backend.fleet.cost_summary import read_cost_summary, update_cost_summary
    from backend.rpc import fleet_cost

    state_dir = tmp / ".naming-state"
    update_cost_summary(state_dir, input_tokens=10, output_tokens=1)

    summary = read_cost_summary(state_dir)
    lying = [k for k in summary if "24h" in k]
    if lying:
        _fail(f"item 6: read_cost_summary still carries {lying!r} for a calendar-day-to-date value")

    resolved = [{"name": "naming", "state_dir": str(state_dir), "status": "ok"}]
    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    lying = [k for k in result if "24h" in k]
    lying += [k for record in result["per_project"] for k in record if "24h" in k]
    if lying:
        _fail(f"item 6: fleet.cost still carries {lying!r} for a calendar-day-to-date value")


def main() -> int:
    checks = (
        check_window_excludes_stale_entries,
        check_writer_prunes_by_date,
        check_handler_over_resolved_set,
        check_all_stale_reports_no_totals,
        check_measured_zero_is_still_reported,
        check_no_field_claims_24h,
    )
    for check in checks:
        with tempfile.TemporaryDirectory(prefix="fleet-cost-guard-") as tmpdir:
            check(Path(tmpdir))

    if FAILURES:
        print(f"\nfleet-cost-window-guard: {len(FAILURES)} check(s) failed")
        return 1
    print(f"fleet-cost-window-guard: {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
