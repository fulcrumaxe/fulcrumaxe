#!/usr/bin/env python3
"""agent-run-attribution-guard.py — behavioral guard for Agent()-spawned run
attribution (D#2316 PR-b).

Background
----------
`agent_run_tracker.start_run()` has exactly one caller in the repo,
`scripts/spawn-agent.sh:763`. Runs spawned through the `Agent()` tool never
call it, so when `complete_run()` fires from the SubagentStop hook there is
no started row to match against — it falls into its INSERT branch, which
used to unconditionally stamp `role='orphan-unmatched'` and write
`start_ts = end_ts` (so `duration_s` came out `0`, not "unknown"). Measured
on the operator host: 77-92 of ~120 same-day rows landed this way.

The fix keeps the completion side (where the recoverable data actually is):
`complete_run()` now accepts `role` / `discussion` / `start_ts`, and uses
them ONLY on the INSERT branch — an existing start_run() row's role is never
touched (no-clobber, D#2282's stated principle), and a role outside
`_KNOWN_ROLES` still lands as `orphan-unmatched` rather than being invented
into something real (no-guessing, D#2282's other stated principle). A
completion with no recoverable start time still writes `duration_s = NULL`
— never `0` — because `0s` reads as a measurement and this repo has
thirteen catalogued instances of a surface doing exactly that.

This is a behavioral probe, not a lint over source text: it builds a
fixture `agent_run` DB in a tmpdir (via `STATS_DB_PATH`, which
`agent_run_tracker._db_path()` checks first and which bypasses the pytest
state-dir guard — see `backend/state_paths.py`'s "AC-8" docstring) and
drives the real `start_run()` / `complete_run()` against a set of synthetic
completions this file constructs itself. No hardcoded population counts:
every expectation below is derived from the fixture construction below it,
per D#2316's "no literal counts" constraint (the filing's own 77% reading
was 64% four hours later).

Run from the repo root:

    python3 scripts/ci/agent-run-attribution-guard.py

Exit 0: every check passes.
Exit 1: a check failed — prints one `FAIL <detail>` line per failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

FAILURES: list[str] = []


def _fail(detail: str) -> None:
    FAILURES.append(detail)
    print(f"FAIL {detail}")


@dataclass
class Completion:
    """One synthetic complete_run() call this guard drives."""

    agent_id: str
    role: str | None
    discussion: int | None
    start_ts: datetime | None      # None => no recoverable start time
    end_ts: datetime
    prior_start_run_role: str | None = None  # non-None => call start_run() first
    known_role: bool = field(init=False)

    def __post_init__(self) -> None:
        from backend.agent_run_tracker import _KNOWN_ROLES  # noqa: PLC0415
        self.known_role = self.role is not None and self.role in _KNOWN_ROLES


def _build_fixture() -> list[Completion]:
    """Construct the synthetic completions this guard's checks are derived from.

    Every check below counts/asserts against THIS list, not a hardcoded
    number — changing this list changes both the fixture and what the
    checks expect it to produce.
    """
    base = datetime(2026, 9, 4, 3, 0, 0, tzinfo=timezone.utc)
    return [
        # INSERT-branch, resolvable role, recoverable start time.
        Completion(
            agent_id="fixture-insert-executor-1",
            role="executor",
            discussion=4001,
            start_ts=base,
            end_ts=base + timedelta(seconds=137.5),
        ),
        # INSERT-branch, resolvable role, NO recoverable start time.
        Completion(
            agent_id="fixture-insert-code-reviewer-1",
            role="code-reviewer",
            discussion=4002,
            start_ts=None,
            end_ts=base + timedelta(seconds=600),
        ),
        # INSERT-branch, no role supplied at all — genuinely unattributable.
        Completion(
            agent_id="fixture-insert-norole-1",
            role=None,
            discussion=None,
            start_ts=None,
            end_ts=base + timedelta(seconds=900),
        ),
        # INSERT-branch, role supplied but NOT a known role — no-guessing
        # invariant: this must land orphaned, not under the invented role.
        Completion(
            agent_id="fixture-insert-badrole-1",
            role="freelance-vibes-engineer",
            discussion=4003,
            start_ts=base,
            end_ts=base + timedelta(seconds=42),
        ),
        # UPDATE-branch: start_run() already wrote a real role. complete_run()
        # is then told a DIFFERENT role — the no-clobber invariant says the
        # stored role must survive untouched.
        Completion(
            agent_id="fixture-update-noclobber-1",
            role="executor",  # what complete_run() is (wrongly) told
            discussion=4004,
            start_ts=None,
            end_ts=base + timedelta(seconds=300),
            prior_start_run_role="project-manager",  # what start_run() actually wrote
        ),
    ]


def _run_fixture(db_path: Path, fixture: list[Completion]) -> None:
    from backend.agent_run_tracker import complete_run, start_run  # noqa: PLC0415

    for c in fixture:
        if c.prior_start_run_role is not None:
            start_run(
                agent_id=c.agent_id,
                role=c.prior_start_run_role,
                discussion=c.discussion,
            )
        complete_run(
            agent_id=c.agent_id,
            end_ts=c.end_ts,
            verdict="done",
            role=c.role,
            discussion=c.discussion,
            start_ts=c.start_ts,
        )


def _fetch_row(conn, agent_id: str):
    return conn.execute(
        "SELECT role, discussion, duration_s FROM agent_run WHERE agent_id = ?",
        [agent_id],
    ).fetchone()


def check_resolved_role_used(conn, fixture: list[Completion]) -> None:
    """Item 9 (positive half): a fixture completion carrying a resolved role
    lands with exactly that role — including on brand-new (INSERT-branch)
    rows, not just updates."""
    from backend.agent_run_tracker import _ORPHAN_ROLE  # noqa: PLC0415

    for c in fixture:
        if c.prior_start_run_role is not None:
            continue  # covered by check_no_clobber_invariant instead
        if not c.known_role:
            continue  # covered by check_no_guessing_invariant instead
        row = _fetch_row(conn, c.agent_id)
        if row is None:
            _fail(f"resolved-role: no row written for {c.agent_id}")
            continue
        role, discussion, _ = row
        if role != c.role:
            _fail(f"resolved-role: {c.agent_id} expected role={c.role!r}, got {role!r}")
        if discussion != c.discussion:
            _fail(
                f"resolved-role: {c.agent_id} expected discussion={c.discussion!r}, "
                f"got {discussion!r}"
            )
        if role == _ORPHAN_ROLE:
            _fail(f"resolved-role: {c.agent_id} landed orphan-unmatched despite a resolvable role")


def check_orphan_count_matches_unresolved(conn, fixture: list[Completion]) -> None:
    """Item 9 (count half): the number of orphan-unmatched rows among this
    fixture's INSERT-branch completions equals the number constructed
    without a resolvable role — derived from the fixture, not hardcoded."""
    from backend.agent_run_tracker import _ORPHAN_ROLE  # noqa: PLC0415

    insert_branch = [c for c in fixture if c.prior_start_run_role is None]
    expected_orphans = {c.agent_id for c in insert_branch if not c.known_role}

    agent_ids = [c.agent_id for c in insert_branch]
    placeholders = ",".join("?" for _ in agent_ids)
    rows = conn.execute(
        f"SELECT agent_id, role FROM agent_run WHERE agent_id IN ({placeholders})",
        agent_ids,
    ).fetchall()
    actual_orphans = {agent_id for agent_id, role in rows if role == _ORPHAN_ROLE}

    if actual_orphans != expected_orphans:
        _fail(
            "orphan-count: expected orphan-unmatched rows for "
            f"{sorted(expected_orphans)}, got {sorted(actual_orphans)}"
        )


def check_no_guessing_invariant(conn, fixture: list[Completion]) -> None:
    """Item 10: a role outside _KNOWN_ROLES is never promoted into a real
    role — the row is orphaned instead of stamped with the invented name."""
    from backend.agent_run_tracker import _ORPHAN_ROLE  # noqa: PLC0415

    for c in fixture:
        if c.role is None or c.known_role or c.prior_start_run_role is not None:
            continue
        row = _fetch_row(conn, c.agent_id)
        if row is None:
            _fail(f"no-guessing: no row written for {c.agent_id}")
            continue
        role, _, _ = row
        if role != _ORPHAN_ROLE:
            _fail(
                f"no-guessing: {c.agent_id} (unknown role {c.role!r} supplied) "
                f"expected role={_ORPHAN_ROLE!r}, got {role!r} — an unrecognised "
                "role must never be promoted into a real one"
            )


def check_no_clobber_invariant(conn, fixture: list[Completion]) -> None:
    """Item 11: when start_run() already wrote a real role, a later
    complete_run() carrying a different role must not overwrite it."""
    for c in fixture:
        if c.prior_start_run_role is None:
            continue
        row = _fetch_row(conn, c.agent_id)
        if row is None:
            _fail(f"no-clobber: no row written for {c.agent_id}")
            continue
        role, discussion, _ = row
        if role != c.prior_start_run_role:
            _fail(
                f"no-clobber: {c.agent_id} started with role={c.prior_start_run_role!r}, "
                f"complete_run() supplied a different role={c.role!r}, but the stored "
                f"role became {role!r} — an existing row's role must be untouchable"
            )


def check_duration_honesty(conn, fixture: list[Completion]) -> None:
    """Item 12: a recoverable start time yields an honest duration_s; an
    unrecoverable one yields NULL, never 0."""
    for c in fixture:
        if c.prior_start_run_role is not None:
            continue  # this check is about the INSERT branch specifically
        row = _fetch_row(conn, c.agent_id)
        if row is None:
            _fail(f"duration-honesty: no row written for {c.agent_id}")
            continue
        _, _, duration_s = row
        if c.start_ts is not None:
            expected = (c.end_ts - c.start_ts).total_seconds()
            if duration_s is None or abs(duration_s - expected) > 0.01:
                _fail(
                    f"duration-honesty: {c.agent_id} expected duration_s≈{expected}, "
                    f"got {duration_s!r}"
                )
        else:
            if duration_s is not None:
                _fail(
                    f"duration-honesty: {c.agent_id} has no recoverable start time — "
                    f"expected duration_s IS NULL, got {duration_s!r} "
                    "(0 or any number reads as a measurement that was never taken)"
                )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-run-attribution-guard-") as td:
        db_path = Path(td) / "stats.duckdb"
        os.environ["STATS_DB_PATH"] = str(db_path)

        try:
            import duckdb  # noqa: F401, PLC0415
        except ImportError:
            print("agent-run-attribution-guard: duckdb not installed — cannot run, failing closed")
            return 1

        fixture = _build_fixture()
        _run_fixture(db_path, fixture)

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            check_resolved_role_used(conn, fixture)
            check_orphan_count_matches_unresolved(conn, fixture)
            check_no_guessing_invariant(conn, fixture)
            check_no_clobber_invariant(conn, fixture)
            check_duration_honesty(conn, fixture)
        finally:
            conn.close()

    if FAILURES:
        print(f"agent-run-attribution-guard: {len(FAILURES)} check(s) failed")
        return 1

    print("agent-run-attribution-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
