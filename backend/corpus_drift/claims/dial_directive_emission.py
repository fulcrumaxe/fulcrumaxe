"""global.dial_directive_emission — count of dial_change audit rows in the last N days.

Scans the audit.jsonl for rows with kind="dial_change" within the window.
Healthy when ≥4 changes are recorded in 30 days (at least one per active week),
indicating the dial system is being used rather than ignored.

Uses backend.dial_registry.list_directives() for availability check; reads
audit.jsonl directly for the historical count.

Scope: global.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.corpus_drift.types import ClaimResult

logger = logging.getLogger(__name__)

CLAIM_ID = "global.dial_directive_emission"
ROLE_SCOPE = "global"

# Minimum dial_change count for "healthy" in a 30-day window.
_HEALTHY_COUNT = 4


def _state_dir() -> Path:
    env = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    if env:
        return Path(env)
    try:
        from backend.state_paths import STATE_DIR  # noqa: PLC0415
        return STATE_DIR
    except ImportError:
        return Path.home() / ".fulcrumaxe-state"


def _count_dial_changes(audit_path: Path, cutoff_ts: float) -> int:
    """Return the number of dial_change rows after cutoff_ts in audit.jsonl."""
    if not audit_path.exists():
        return 0

    count = 0
    try:
        with audit_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("kind") != "dial_change":
                    continue
                ts_str = row.get("timestamp", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        ).timestamp()
                        if ts >= cutoff_ts:
                            count += 1
                    except ValueError:
                        # No parseable timestamp — count it anyway (conservative)
                        count += 1
                else:
                    count += 1
    except OSError as exc:
        logger.debug("dial_directive_emission: cannot read audit.jsonl: %s", exc)

    return count


def _registry_available() -> bool:
    """Quick check that list_directives() is importable and returns data."""
    try:
        from backend.dial_registry import list_directives  # noqa: PLC0415
        directives = list_directives()
        return isinstance(directives, list)
    except Exception as exc:  # noqa: BLE001
        logger.debug("dial_directive_emission: dial_registry unavailable: %s", exc)
        return False


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    audit_path: Path | None = None,
    **_kwargs: Any,
) -> ClaimResult:
    """Count dial_change audit rows in the window.

    Parameters
    ----------
    runs:
        Agent run rows (unused — global claim reads audit.jsonl directly).
    transcripts_dir:
        Unused.
    window_days:
        Audit window in days.
    sample_cap:
        Unused (count-based claim).
    audit_path:
        Override for audit.jsonl path; defaults to STATE_DIR/audit.jsonl.
    """
    if audit_path is None:
        audit_path = _state_dir() / "audit.jsonl"

    cutoff_ts = datetime.now(timezone.utc).timestamp() - window_days * 86400
    count = _count_dial_changes(audit_path, cutoff_ts)

    # Verify dial_registry is operational (informational only)
    registry_ok = _registry_available()

    # We use "count" score_type but threshold differs from the binary archive claim:
    # healthy = count >= _HEALTHY_COUNT, watch = count >= 1, drift = 0.
    if count == 0:
        status = "drift"
        evidence = f"0 dial_change rows in {window_days}d window — dial system unused"
    elif count < _HEALTHY_COUNT:
        status = "watch"
        evidence = f"{count} dial_change row(s) in {window_days}d — below healthy threshold ({_HEALTHY_COUNT})"
    else:
        status = "healthy"
        evidence = f"{count} dial_change rows in {window_days}d"

    notes = "registry_available=True" if registry_ok else "registry_available=False"

    # Use window_days as a proxy for sample_size so status classification
    # never short-circuits to n/a (the audit file itself is the sample).
    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=window_days,
        score=count,
        score_type="count",
        status=status,
        evidence=evidence,
        notes=notes,
    )
