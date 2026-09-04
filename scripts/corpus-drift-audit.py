#!/usr/bin/env python3
"""scripts/corpus-drift-audit.py — periodic corpus drift audit.

Measures observed vs stated agent behavior for hand-curated claims.
Writes a Markdown report to wiki/Corpus-Drift-Report.md and a dated JSON
snapshot to $STATE_DIR/corpus-drift/<YYYY-MM-DD>.json.

Usage:
    python3 scripts/corpus-drift-audit.py [--since 30d] [--role <role>] [--sample-cap N]

Examples:
    python3 scripts/corpus-drift-audit.py --since 30d
    python3 scripts/corpus-drift-audit.py --since 7d --role code-reviewer
    python3 scripts/corpus-drift-audit.py --since 30d --sample-cap 50

Performance note:
    Default sample cap is 100 runs per role.  If the audit takes over 2 minutes,
    reduce the cap: --sample-cap 50.  The cap is documented in the report header.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root.
# _SCRIPT_ROOT is __file__-derived so sys.path works from worktrees too.
_SCRIPT_ROOT = Path(__file__).parent.parent
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))

from backend.corpus_drift.types import ClaimResult
from backend.corpus_drift.report import render_markdown, write_json_snapshot
from backend.repo_root import main_repo_root

logger = logging.getLogger(__name__)


def _parse_since(since_str: str) -> tuple[int, datetime]:
    """Parse a 'Nd' or 'Nw' since string.  Returns (window_days, cutoff_datetime)."""
    m = re.fullmatch(r'(\d+)([dw])', since_str.strip().lower())
    if not m:
        raise ValueError(f"Invalid --since value '{since_str}'. Use e.g. '30d' or '4w'.")
    n = int(m.group(1))
    unit = m.group(2)
    days = n if unit == 'd' else n * 7
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return days, cutoff


def _state_dir() -> Path:
    env = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    if env:
        return Path(env)
    try:
        from backend.state_paths import STATE_DIR  # noqa: PLC0415
        return STATE_DIR
    except ImportError:
        return Path.home() / ".fulcrumaxe-state"


def _load_runs(role: str, since_iso: str) -> list[dict]:
    """Load agent_run rows for *role* since *since_iso*.  Returns [] on failure."""
    try:
        from backend.agent_run_reader import by_role  # noqa: PLC0415
        return by_role(role, since_iso)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load runs for role=%s: %s", role, exc)
        return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Corpus drift audit — measure observed vs stated agent behavior.",
    )
    parser.add_argument(
        "--since",
        default="30d",
        metavar="N[d|w]",
        help="Audit window, e.g. '30d' or '4w' (default: 30d)",
    )
    parser.add_argument(
        "--role",
        default=None,
        metavar="ROLE",
        help="Restrict audit to one role (default: all)",
    )
    parser.add_argument(
        "--sample-cap",
        type=int,
        default=100,
        dest="sample_cap",
        metavar="N",
        help="Max runs/transcripts per claim (default: 100). Lower to reduce runtime.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="Override output directory for wiki report (default: <repo_root>/wiki/)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    t_start = time.monotonic()

    window_days, cutoff = _parse_since(args.since)
    since_iso = cutoff.isoformat()

    state_dir = _state_dir()

    # ── Report paths ────────────────────────────────────────────────────────
    wiki_dir = Path(args.output_dir) if args.output_dir else main_repo_root() / "wiki"
    report_path = wiki_dir / "Corpus-Drift-Report.md"

    snapshot_dir = state_dir / "corpus-drift"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_path = snapshot_dir / f"{today}.json"

    # ── Claim registry ───────────────────────────────────────────────────────
    # Import claim modules; each exposes evaluate() + CLAIM_ID + ROLE_SCOPE
    from backend.corpus_drift.claims import pytest_invoked, archive_protocol, self_observe, spawn_wrapper, two_gate  # noqa: PLC0415
    from backend.corpus_drift.claims import dial_directive_emission, three_section_spec_used, scrubber_bypass_absent  # noqa: PLC0415

    # List of (claim_module, role_filter_for_runs)
    # role_filter=None means load all runs, "global" gets a combined set
    claim_configs = [
        (pytest_invoked, "code-reviewer"),
        (archive_protocol, None),        # global — pass empty runs, uses transcripts only
        (self_observe, None),            # global
        (spawn_wrapper, "team-lead"),
        (two_gate, "executor"),
        (dial_directive_emission, None),          # global — reads audit.jsonl directly
        (three_section_spec_used, "project-manager"),
        (scrubber_bypass_absent, "executor"),     # executor + code-reviewer scope
    ]

    # ── Evaluate claims ──────────────────────────────────────────────────────
    results: list[ClaimResult] = []
    generated_at = datetime.now(timezone.utc)

    print(f"Corpus drift audit — window: {window_days}d, sample cap: {args.sample_cap}")
    print(f"Cutoff: {since_iso[:19]} UTC")
    print()

    for module, role_filter in claim_configs:
        # Skip if --role flag restricts to a different role
        if args.role and module.ROLE_SCOPE not in (args.role, "global"):
            continue

        # Load runs for this claim's role scope
        if role_filter:
            runs = _load_runs(role_filter, since_iso)
        else:
            runs = []

        claim_start = time.monotonic()
        try:
            result = module.evaluate(
                runs=runs,
                transcripts_dir=None,
                window_days=window_days,
                sample_cap=args.sample_cap,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Claim %s failed: %s", module.CLAIM_ID, exc)
            result = ClaimResult(
                claim_id=module.CLAIM_ID,
                role_scope=module.ROLE_SCOPE,
                sample_size=0,
                score=0.0,
                score_type="fraction",
                status="n/a",
                evidence=f"evaluator error: {exc}",
            )

        elapsed_ms = int((time.monotonic() - claim_start) * 1000)
        results.append(result)
        print(f"  [{result.status:7s}] {result.claim_id}  {result.score_display()}  N={result.sample_size}  ({elapsed_ms}ms)")

    total_elapsed = time.monotonic() - t_start
    print()
    print(f"Evaluated {len(results)} claim(s) in {total_elapsed:.1f}s")

    # ── Write outputs ────────────────────────────────────────────────────────
    rendered = render_markdown(
        results=results,
        window_days=window_days,
        generated_at=generated_at,
        sample_cap=args.sample_cap,
        report_path=report_path,
    )
    print(f"Report written: {report_path}")

    write_json_snapshot(
        results=results,
        window_days=window_days,
        generated_at=generated_at,
        snapshot_path=snapshot_path,
    )
    print(f"Snapshot written: {snapshot_path}")

    # ── Summary table ────────────────────────────────────────────────────────
    print()
    print("Summary:")
    print(f"  healthy : {sum(1 for r in results if r.status == 'healthy')}")
    print(f"  watch   : {sum(1 for r in results if r.status == 'watch')}")
    print(f"  drift   : {sum(1 for r in results if r.status == 'drift')}")
    print(f"  n/a     : {sum(1 for r in results if r.status == 'n/a')}")

    # Exit non-zero when any claim is in drift (for use in CI / alerting)
    # but still write all outputs first
    has_drift = any(r.status == "drift" for r in results)
    if has_drift:
        print("\nWARNING: one or more claims in drift state.")
    else:
        print("\nNo drift detected in scored claims.")

    return 0  # always exit 0 per Spec (report-only, no gate enforcement yet)


if __name__ == "__main__":
    sys.exit(main())
