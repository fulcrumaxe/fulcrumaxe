"""Fleet cost: the right project set, in windows that match their labels
(D#2317 PR-b).

Three things were wrong at once on the Fleet Token Spend panel, and they
have to be tested together because fixing one alone leaves the panel
confidently wrong in a different way:

  1. ``fleet.cost`` iterated ``discover_projects()`` (``~/.*-state/project.json``),
     so it read ``cost_summary.json`` for seven dead fixtures and never for
     ``~/.autonomous-forever-state`` -- which is exactly where
     ``scripts/hooks/post-agent.d/cost-summary.sh`` writes it. Writer and
     reader never met.
  2. The writer pruned with ``entries[-7:]``, keeping the last seven
     *written* entries however old, and the reader summed the whole file
     under the label "Last 7d".
  3. A project with no observation reported ``0``, indistinguishable from a
     project measured at zero spend.

Every fixture below builds its own state dirs and asserts against its own
construction -- no literal project count, name or port from the operator's
host appears here.

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" pytest -x -q backend/tests/test_fleet_cost_window.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.fleet.cost_summary import read_cost_summary, update_cost_summary  # noqa: E402
from backend.fleet.cost_window import WINDOW_DAYS, summarize, today_utc, window_dates  # noqa: E402


def _date(offset_days: int) -> str:
    """A UTC date string *offset_days* before today."""
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _write_summary(state_dir: Path, entries: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "cost_summary.json").write_text(
        json.dumps({"updated_at": "", "last_7d": entries}), encoding="utf-8"
    )


def _entry(offset_days: int, tokens: int) -> dict:
    return {"date": _date(offset_days), "input_tokens": tokens, "output_tokens": 0}


# ---------------------------------------------------------------------------
# Item 2 — read filters to a real calendar window
# ---------------------------------------------------------------------------


def test_window_is_seven_calendar_days_ending_today():
    dates = window_dates(today_utc())
    assert len(dates) == WINDOW_DAYS
    assert dates[-1] == today_utc()
    assert dates == sorted(dates)


def test_entry_older_than_the_window_never_contributes(tmp_path):
    state_dir = tmp_path / ".alpha-state"
    _write_summary(state_dir, [_entry(30, 1_000_000), _entry(0, 10)])

    summary = read_cost_summary(state_dir)

    assert summary["tokens_7d"] == 10
    assert summary["tokens_today_utc"] == 10


def test_entry_on_the_window_edge_still_contributes(tmp_path):
    """today - (WINDOW_DAYS - 1) is inside the window; one day older is not."""
    state_dir = tmp_path / ".edge-state"
    _write_summary(state_dir, [_entry(WINDOW_DAYS - 1, 7), _entry(WINDOW_DAYS, 999)])

    summary = read_cost_summary(state_dir)

    assert summary["tokens_7d"] == 7


# ---------------------------------------------------------------------------
# Item 3 — the writer prunes by date, not by entry count
# ---------------------------------------------------------------------------


def test_writer_prunes_by_date_not_entry_count(tmp_path):
    state_dir = tmp_path / ".sparse-state"
    # 20 sparse entries spanning 60 days. entries[-7:] would have kept the
    # seven most recently *written* of these regardless of age.
    _write_summary(state_dir, [_entry(offset, 100) for offset in range(60, 0, -3)])

    update_cost_summary(state_dir, input_tokens=1, output_tokens=0)

    kept = json.loads((state_dir / "cost_summary.json").read_text())["last_7d"]
    oldest_allowed = window_dates(today_utc())[0]
    assert kept, "today's own entry must survive the prune"
    assert all(e["date"] >= oldest_allowed for e in kept), kept


# ---------------------------------------------------------------------------
# Items 4 and 5 — the invariant, and no-signal vs measured zero
# ---------------------------------------------------------------------------


def test_no_entry_in_window_reports_no_signal_not_zero(tmp_path):
    state_dir = tmp_path / ".stale-state"
    _write_summary(state_dir, [_entry(30, 1_000_000)])

    summary = read_cost_summary(state_dir)

    assert summary["tokens_today_utc"] is None
    assert summary["tokens_7d"] is None
    assert summary["projected_eod_tokens"] is None


def test_missing_cost_summary_reports_no_signal(tmp_path):
    summary = read_cost_summary(tmp_path / ".never-written-state")
    assert summary["tokens_7d"] is None


def test_entries_in_window_but_none_today_is_a_measured_zero(tmp_path):
    """The writer appends on every completed run, so an in-window file with
    no entry dated today is evidence of zero spend today -- not a gap."""
    state_dir = tmp_path / ".quiet-state"
    _write_summary(state_dir, [_entry(2, 5_000)])

    summary = read_cost_summary(state_dir)

    assert summary["tokens_today_utc"] == 0
    assert summary["tokens_7d"] == 5_000


def test_today_never_exceeds_seven_days(tmp_path):
    state_dir = tmp_path / ".invariant-state"
    _write_summary(state_dir, [_entry(offset, 1_000) for offset in range(0, WINDOW_DAYS)])

    summary = read_cost_summary(state_dir)

    assert summary["tokens_today_utc"] <= summary["tokens_7d"]


# ---------------------------------------------------------------------------
# Item 6 — the 24h label is gone from the payload too
# ---------------------------------------------------------------------------


def test_payload_carries_no_field_claiming_24h(tmp_path):
    state_dir = tmp_path / ".naming-state"
    update_cost_summary(state_dir, input_tokens=10, output_tokens=1)

    summary = read_cost_summary(state_dir)

    assert "tokens_today_utc" in summary
    assert not [k for k in summary if "24h" in k], summary


def test_summarize_uses_the_supplied_clock():
    """summarize() is pure with respect to `now`, so the calendar-day
    boundary is testable without waiting for midnight UTC."""
    stamp = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    entries = [
        {"date": "2026-09-03", "input_tokens": 100, "output_tokens": 0},
        {"date": "2026-09-02", "input_tokens": 50, "output_tokens": 0},
        {"date": "2026-08-01", "input_tokens": 9_999, "output_tokens": 0},
    ]

    totals = summarize(entries, now=stamp)

    assert totals["tokens_today_utc"] == 100
    assert totals["tokens_7d"] == 150
    assert totals["projected_eod_tokens"] == 200  # 100 over 12h, extrapolated


# ---------------------------------------------------------------------------
# Item 1 — fleet.cost iterates the resolved fleet set
# ---------------------------------------------------------------------------


def test_fleet_cost_includes_a_runtime_only_project(tmp_path):
    """A project with a dashboard-runtime.json and no project.json used to
    be invisible to this handler -- which is the shape of the one project
    whose cost_summary.json is actually written."""
    runtime_only = tmp_path / ".runtime-only-state"
    _write_summary(runtime_only, [_entry(0, 4_242)])

    resolved = [{"name": "runtime-only", "state_dir": str(runtime_only), "status": "unknown"}]

    from backend.rpc import fleet_cost

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    assert result["total_7d"] == 4_242
    assert result["per_project"][0]["tokens_today_utc"] == 4_242


def test_fleet_totals_omit_projects_with_no_signal(tmp_path):
    measured = tmp_path / ".measured-state"
    stale = tmp_path / ".stale-state"
    _write_summary(measured, [_entry(0, 900)])
    _write_summary(stale, [_entry(45, 1_000_000)])

    resolved = [
        {"name": "measured", "state_dir": str(measured), "status": "ok"},
        {"name": "stale", "state_dir": str(stale), "status": "unknown"},
    ]

    from backend.rpc import fleet_cost

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    assert result["total_today_utc"] == 900
    assert result["total_7d"] == 900
    by_name = {p["name"]: p for p in result["per_project"]}
    assert "tokens_7d" not in by_name["stale"]
    assert by_name["stale"]["ok"] is True
    assert result["total_today_utc"] <= result["total_7d"]


def test_fleet_totals_absent_when_nothing_measured(tmp_path):
    stale = tmp_path / ".all-stale-state"
    _write_summary(stale, [_entry(45, 1_000_000)])

    resolved = [{"name": "stale", "state_dir": str(stale), "status": "unknown"}]

    from backend.rpc import fleet_cost

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    assert "total_today_utc" not in result
    assert "total_7d" not in result
    assert "projected_eod" not in result


def test_fleet_cost_error_record_carries_no_zero(tmp_path):
    resolved = [
        {"name": "broken", "state_dir": str(tmp_path / ".broken-state"),
         "status": "error", "error": "JSON parse error"},
    ]

    from backend.rpc import fleet_cost

    with patch("backend.fleet.fleet_set.resolve_fleet_set", return_value=resolved):
        result = fleet_cost.handle({})

    record = result["per_project"][0]
    assert record["ok"] is False
    assert not [k for k in record if k.startswith("tokens_")], record
