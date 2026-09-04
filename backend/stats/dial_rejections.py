"""backend/stats/dial_rejections.py — Dial-rejection telemetry reader.

Reads the last 24h of:
  - audit.jsonl rows with kind="dial_directive_rejected"
  - .autonomous-team/hook-events/blocks-YYYY-MM-DD.jsonl rows for today and
    yesterday, classified into three sandbox-block categories.

Pure function — no side effects, no writes, no spawns.

Sandbox-block category mapping (from blocks JSONL `reason` field):
  sandbox_block_agent_spawn      — reasons starting with "agent_spawn_" or
                                    "claude_spawn_forbidden:"
  sandbox_block_gh_api_mutation  — reasons starting with "sandbox_block_gh_api_mutation"
  sandbox_block_untrusted_cwd    — reason == "agent_spawn_in_untrusted_cwd"

Note: blocks-*.jsonl lives in <repo_root>/.autonomous-team/hook-events/, not
inside the state dir.  The absolute path is derived from HOOK_EVENTS_DIR below.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Hook-events directory — always at a fixed location relative to the repo root.
# We resolve it relative to THIS file: backend/stats/dial_rejections.py lives
# two levels below the repo root (backend/stats/), so we go up three levels.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_EVENTS_DIR: Path = _REPO_ROOT / ".autonomous-team" / "hook-events"

# ---------------------------------------------------------------------------
# Category classifier for sandbox blocks
# ---------------------------------------------------------------------------

_SANDBOX_BLOCK_KINDS = (
    "sandbox_block_agent_spawn",
    "sandbox_block_gh_api_mutation",
    "sandbox_block_untrusted_cwd",
)


def _classify_sandbox_block(reason: str) -> str | None:
    """Map a blocks-file `reason` string to one of the three spec kinds.

    Returns None when the reason does not match any known category (i.e., the
    row should be ignored for sandbox-block counting purposes).
    """
    if not reason:
        return None
    if reason.startswith("sandbox_block_gh_api_mutation"):
        return "sandbox_block_gh_api_mutation"
    if reason == "agent_spawn_in_untrusted_cwd":
        return "sandbox_block_untrusted_cwd"
    if reason.startswith("agent_spawn_in") or reason.startswith("claude_spawn_forbidden:"):
        return "sandbox_block_agent_spawn"
    return None


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def read_dial_rejections(state_dir: "Path | str | None" = None) -> dict:
    """Return 24h rejection counters across directive rejections + sandbox blocks.

    Parameters
    ----------
    state_dir:
        Path to the state directory (contains audit.jsonl).
        When None, falls back to backend.state_paths.STATE_DIR.

    Returns
    -------
    {
        "rejected_directives_24h": {
            "total": int,
            "by_reason": {<reason>: int},   # top-5 + "other"
            "last_at": str | None,           # ISO8601 of most recent
        },
        "sandbox_blocks_24h": {
            "total": int,
            "by_kind": {
                "sandbox_block_agent_spawn":     int,
                "sandbox_block_gh_api_mutation": int,
                "sandbox_block_untrusted_cwd":   int,
            },
            "last_at": str | None,
        },
        "last_rejection": {
            "kind": str,
            "reason_or_class": str,
            "timestamp": str,
            "cwd": str | None,
        } | None,
    }
    """
    # ------------------------------------------------------------------
    # Resolve state dir
    # ------------------------------------------------------------------
    if state_dir is None:
        from backend.state_paths import STATE_DIR  # noqa: PLC0415
        _state_dir = STATE_DIR
    else:
        _state_dir = Path(state_dir)

    audit_log = _state_dir / "audit.jsonl"

    # ------------------------------------------------------------------
    # Time window
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # ------------------------------------------------------------------
    # Phase 1: scan audit.jsonl for dial_directive_rejected rows
    # ------------------------------------------------------------------
    dir_total = 0
    dir_reasons: dict[str, int] = {}
    dir_last_at: str | None = None
    dir_last_ts: datetime | None = None

    if audit_log.exists():
        try:
            with audit_log.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if row.get("kind") != "dial_directive_rejected":
                        continue

                    ts_str = row.get("ts", "") or row.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    if ts < cutoff:
                        continue

                    dir_total += 1
                    reason = row.get("reason", "") or "unknown"
                    dir_reasons[reason] = dir_reasons.get(reason, 0) + 1

                    if dir_last_ts is None or ts > dir_last_ts:
                        dir_last_ts = ts
                        dir_last_at = ts_str
        except OSError:
            pass

    # Apply top-5 + "other" bucketing
    dir_by_reason = _top5_with_other(dir_reasons)

    # ------------------------------------------------------------------
    # Phase 2: scan blocks-*.jsonl for sandbox_block_* rows
    # ------------------------------------------------------------------
    blk_total = 0
    blk_by_kind: dict[str, int] = {k: 0 for k in _SANDBOX_BLOCK_KINDS}
    blk_last_at: str | None = None
    blk_last_ts: datetime | None = None

    today = now.date()
    yesterday = today - timedelta(days=1)

    for date_str in (yesterday.isoformat(), today.isoformat()):
        blocks_file = HOOK_EVENTS_DIR / f"blocks-{date_str}.jsonl"
        if not blocks_file.exists():
            continue
        try:
            with blocks_file.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if row.get("decision") != "block":
                        continue

                    ts_str = row.get("ts", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    if ts < cutoff:
                        continue

                    reason = row.get("reason", "")
                    kind = _classify_sandbox_block(reason)
                    if kind is None:
                        continue

                    blk_total += 1
                    blk_by_kind[kind] = blk_by_kind.get(kind, 0) + 1

                    if blk_last_ts is None or ts > blk_last_ts:
                        blk_last_ts = ts
                        blk_last_at = ts_str
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Compute last_rejection across both sources
    # ------------------------------------------------------------------
    last_rejection = None

    # Collect candidates: (ts, kind, reason_or_class, ts_str, cwd)
    candidates: list[tuple[datetime, str, str, str, str | None]] = []

    # From directive rejections — we need the actual row data; re-scan briefly
    # to get the most recent row's full data. For efficiency, we already captured
    # the last timestamp; re-scan only if we found at least one row.
    if dir_last_ts is not None and audit_log.exists():
        try:
            with audit_log.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("kind") != "dial_directive_rejected":
                        continue
                    ts_str = row.get("ts", "") or row.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    reason_or_class = (
                        row.get("reason", "")
                        or row.get("class", "")
                        or "unknown"
                    )
                    candidates.append((ts, "dial_directive_rejected", reason_or_class, ts_str, None))
        except OSError:
            pass

    # From sandbox blocks
    if blk_last_ts is not None:
        for date_str in (yesterday.isoformat(), today.isoformat()):
            blocks_file = HOOK_EVENTS_DIR / f"blocks-{date_str}.jsonl"
            if not blocks_file.exists():
                continue
            try:
                with blocks_file.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if row.get("decision") != "block":
                            continue
                        ts_str = row.get("ts", "")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if ts < cutoff:
                            continue
                        reason = row.get("reason", "")
                        kind = _classify_sandbox_block(reason)
                        if kind is None:
                            continue
                        candidates.append((ts, kind, reason, ts_str, row.get("cwd")))
            except OSError:
                pass

    if candidates:
        best = max(candidates, key=lambda x: x[0])
        last_rejection = {
            "kind": best[1],
            "reason_or_class": best[2],
            "timestamp": best[3],
            "cwd": best[4],
        }

    return {
        "rejected_directives_24h": {
            "total": dir_total,
            "by_reason": dir_by_reason,
            "last_at": dir_last_at,
        },
        "sandbox_blocks_24h": {
            "total": blk_total,
            "by_kind": blk_by_kind,
            "last_at": blk_last_at,
        },
        "last_rejection": last_rejection,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _top5_with_other(counts: dict[str, int]) -> dict[str, int]:
    """Return top-5 entries by count; remainder bucketed as 'other'.

    When there are 5 or fewer distinct keys, returns them as-is (no 'other').
    When there are more than 5, the top-5 are kept and the rest are summed
    under the key 'other'.
    """
    if not counts:
        return {}
    sorted_items = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    if len(sorted_items) <= 5:
        return dict(sorted_items)
    top5 = dict(sorted_items[:5])
    other_total = sum(v for _, v in sorted_items[5:])
    if other_total > 0:
        top5["other"] = other_total
    return top5
