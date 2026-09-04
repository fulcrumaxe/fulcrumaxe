#!/usr/bin/env python3
"""subscription_usage.py — rolling-window token usage against Claude subscription quota.

Reads Claude Code's local JSONL transcripts under ~/.claude/projects/ and sums
input+output tokens within a rolling window to estimate subscription quota usage.
Cache read/write tokens are excluded to match Anthropic's quota counting.

Subscription-cost ceilings
--------------------------
current_plan_limits() returns monthly/daily USD caps representing the *subscription
billing cost*, not the Anthropic API spend cap.  The lookup table (plan → monthly USD):

    pro       → $20 /mo   daily = $20 / 30 ≈ $0.67
    max-5x    → $100/mo   daily = $100 / 30 ≈ $3.33
    max-20x   → $200/mo   daily = $200 / 30 ≈ $6.67
    team      → $25 /user/mo (1-user assumption)  daily = $25 / 30 ≈ $0.83

Resolution order for current_plan_limits():
  1. "config" — explicit subscription.daily_usd_cap / monthly_usd_cap in config.json
  2. "estimated" — plan-name looked up in the table above
  3. "hardcoded-fallback" — $15/day, $450/month (keeps dashboard stable on failure)

Weekly cap tracking
-------------------
Anthropic Max plans have a separate weekly Sonnet/Opus hour cap that resets on a
per-user fixed day/time. Use --weekly to see this alongside the 5h rolling window.

Token-to-hour conversion (community-derived, not officially published):
    Sonnet: 1 hour ≈ 4,000,000 tokens
    Opus:   1 hour ≈ 800,000 tokens

These are rough estimates based on observed throughput; actual limits may differ.
Model detection uses the "model" field in JSONL entries when present.

Usage:
    python3 backend/subscription_usage.py
    python3 backend/subscription_usage.py --json
    python3 backend/subscription_usage.py --plan max-5x
    python3 backend/subscription_usage.py --plan pro --json
    python3 backend/subscription_usage.py --weekly
    python3 backend/subscription_usage.py --weekly --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from backend.blackboard import get_blackboard as _get_blackboard
except Exception:  # noqa: BLE001
    _get_blackboard = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_FILE = REPO_ROOT / ".autonomous-team" / "subscription-plans.json"
CONFIG_FILE = REPO_ROOT / ".autonomous-team" / "config.json"

try:
    from backend._repo import PROJECT_TRANSCRIPT_SLUG
except Exception:  # noqa: BLE001 - allow direct-script invocation (`python3 backend/subscription_usage.py`)
    PROJECT_TRANSCRIPT_SLUG = str(REPO_ROOT).replace("/", "-")

# ---------------------------------------------------------------------------
# Built-in plan defaults (fallback when subscription-plans.json is absent)
# ---------------------------------------------------------------------------

_BUILTIN_PLANS: dict[str, dict[str, Any]] = {
    "pro":     {"window_hours": 5, "tokens_quota": 220_000},
    "max-5x":  {"window_hours": 5, "tokens_quota": 1_100_000},
    "max-20x": {"window_hours": 5, "tokens_quota": 4_400_000},
    "team":    {"window_hours": 5, "tokens_quota": 220_000},
}

_BUILTIN_NOTE = (
    "Community-derived estimates; not authoritative. "
    "Override locally if your plan differs."
)

# ---------------------------------------------------------------------------
# Weekly cap constants
# ---------------------------------------------------------------------------

# Token-to-hour conversion rates (community-derived rough estimates).
# Anthropic does not publish official throughput numbers for subscription caps.
# Sonnet: ~4M tokens/hour observed on max-20x under heavy load
# Opus:   ~800K tokens/hour (much slower, fewer per-hour)
_SONNET_TOKENS_PER_HOUR = 4_000_000
_OPUS_TOKENS_PER_HOUR = 800_000

# Day-of-week name → Python weekday integer (Monday=0, Sunday=6)
_WEEKDAY_MAP: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


# ---------------------------------------------------------------------------
# Plan table loading
# ---------------------------------------------------------------------------

def _load_plans() -> dict[str, dict[str, Any]]:
    """Load plan table from subscription-plans.json, falling back to built-ins."""
    try:
        if PLANS_FILE.exists():
            data = json.loads(PLANS_FILE.read_text())
            plans = data.get("plans", {})
            if plans:
                return plans
    except Exception:  # noqa: BLE001
        pass
    return _BUILTIN_PLANS.copy()


def _plan_info(plan_name: str, plans: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return plan info dict, falling back to max-20x if unknown."""
    return plans.get(plan_name) or plans.get("max-20x") or _BUILTIN_PLANS["max-20x"]


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    return {}


def _config_plan(config: dict[str, Any]) -> str | None:
    sub = config.get("subscription") or {}
    return sub.get("plan")


def _config_window_hours(config: dict[str, Any]) -> float | None:
    sub = config.get("subscription") or {}
    wh = sub.get("window_hours")
    try:
        return float(wh) if wh is not None else None
    except (TypeError, ValueError):
        return None


def _config_weekly_reset(config: dict[str, Any]) -> tuple[str, str, str]:
    """Return (reset_day, reset_time, reset_timezone) from config, with defaults."""
    sub = config.get("subscription") or {}
    reset_day = sub.get("weekly_reset_day") or "thursday"
    reset_time = sub.get("weekly_reset_time") or "06:00"
    reset_tz = sub.get("weekly_reset_timezone") or "America/New_York"
    return str(reset_day).lower(), str(reset_time), str(reset_tz)


# ---------------------------------------------------------------------------
# Weekly reset datetime helpers
# ---------------------------------------------------------------------------

def _tz_offset_hours(tz_name: str) -> float:
    """Return UTC offset in hours for a timezone name.

    Uses a small lookup table for common US timezones. Falls back to 0 (UTC)
    for unrecognised names rather than requiring pytz/zoneinfo.

    NOTE: Does not account for DST transitions. For the purpose of approximate
    weekly-reset math this is acceptable — being off by 1h matters less than
    not knowing the window at all.
    """
    # Standard (non-DST) offsets for common zones
    _OFFSETS: dict[str, float] = {
        "America/New_York": -5.0,
        "America/Chicago": -6.0,
        "America/Denver": -7.0,
        "America/Los_Angeles": -8.0,
        "America/Phoenix": -7.0,  # no DST
        "America/Anchorage": -9.0,
        "Pacific/Honolulu": -10.0,
        "UTC": 0.0,
        "GMT": 0.0,
        "Europe/London": 0.0,
        "Europe/Paris": 1.0,
        "Europe/Berlin": 1.0,
        "Asia/Tokyo": 9.0,
        "Asia/Shanghai": 8.0,
        "Australia/Sydney": 10.0,
    }
    return _OFFSETS.get(tz_name, 0.0)


def _parse_reset_time(reset_time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' string into (hour, minute) ints. Falls back to (6, 0)."""
    try:
        parts = reset_time_str.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return max(0, min(23, hour)), max(0, min(59, minute))
    except (ValueError, IndexError, AttributeError):
        return 6, 0


def last_weekly_reset(
    reset_day: str,
    reset_time: str,
    reset_timezone: str,
    now: Optional[datetime] = None,
) -> datetime:
    """Compute the most recent weekly reset datetime (UTC) prior to `now`.

    Args:
        reset_day:       Weekday name, e.g. "thursday"
        reset_time:      "HH:MM" local time of reset, e.g. "06:00"
        reset_timezone:  IANA timezone name (best-effort — see _tz_offset_hours)
        now:             Override for current UTC time (for testing)

    Returns:
        UTC datetime of the last reset occurrence.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    target_weekday = _WEEKDAY_MAP.get(reset_day.lower(), 3)  # default Thursday=3
    reset_hour, reset_minute = _parse_reset_time(reset_time)
    tz_offset = _tz_offset_hours(reset_timezone)

    # Express 'now' in local time (approximate — ignores DST)
    local_now = now + timedelta(hours=tz_offset)

    # Find the most recent occurrence of target_weekday at reset_hour:reset_minute
    # in local time. Go back up to 7 days.
    days_since = (local_now.weekday() - target_weekday) % 7
    candidate = local_now - timedelta(days=days_since)
    candidate = candidate.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)

    # If the candidate is in the future (same day but before reset time), go back 7 days
    if candidate > local_now:
        candidate -= timedelta(days=7)

    # Convert back to UTC
    reset_utc = candidate - timedelta(hours=tz_offset)
    # Ensure timezone-aware
    if reset_utc.tzinfo is None:
        reset_utc = reset_utc.replace(tzinfo=timezone.utc)

    return reset_utc


def next_weekly_reset(
    reset_day: str,
    reset_time: str,
    reset_timezone: str,
    now: Optional[datetime] = None,
) -> datetime:
    """Return the next weekly reset datetime (UTC) after `now`."""
    last = last_weekly_reset(reset_day, reset_time, reset_timezone, now)
    return last + timedelta(weeks=1)


def time_to_reset(
    reset_day: str,
    reset_time: str,
    reset_timezone: str,
    now: Optional[datetime] = None,
) -> str:
    """Return human-readable 'Xh Ym' string until next weekly reset."""
    if now is None:
        now = datetime.now(timezone.utc)
    nxt = next_weekly_reset(reset_day, reset_time, reset_timezone, now)
    delta = nxt - now
    total_seconds = max(0, int(delta.total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}h {minutes}m"


# ---------------------------------------------------------------------------
# JSONL discovery and parsing
# ---------------------------------------------------------------------------

def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _find_jsonl_files(projects_dir: Path) -> list[Path]:
    """Glob all *.jsonl files under the projects directory."""
    if not projects_dir.exists():
        return []
    try:
        return list(projects_dir.glob("**/*.jsonl"))
    except Exception:  # noqa: BLE001
        return []


def _parse_timestamp(raw: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime."""
    if not isinstance(raw, str):
        return None
    try:
        # Handle 'Z' suffix and various formats
        ts = raw.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _extract_tokens(entry: dict[str, Any]) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) from a JSONL entry.

    Handles three shapes (in priority order):
      1. Claude Code session transcripts — `entry["message"]["usage"]`
      2. API response style — top-level `entry["usage"]`
      3. Some assistant-turn formats — direct `entry["input_tokens"]` / `["output_tokens"]`

    Cache tokens (cache_read_input_tokens, cache_creation_input_tokens) are
    excluded to match Anthropic's quota accounting.
    """
    input_tok = 0
    output_tok = 0

    # Shape 1: Claude Code session transcripts nest usage under `message`
    message = entry.get("message")
    usage = None
    if isinstance(message, dict):
        nested = message.get("usage")
        if isinstance(nested, dict):
            usage = nested

    # Shape 2: top-level usage dict
    if usage is None:
        top = entry.get("usage")
        if isinstance(top, dict):
            usage = top

    if usage is not None:
        input_tok = int(usage.get("input_tokens") or 0)
        output_tok = int(usage.get("output_tokens") or 0)
        # Explicitly exclude cache tokens — do not add them
    else:
        # Shape 3: direct keys
        input_tok = int(entry.get("input_tokens") or 0)
        output_tok = int(entry.get("output_tokens") or 0)

    return max(0, input_tok), max(0, output_tok)


def _earliest_token_entry_after(
    files: list[Path],
    window_start: datetime,
) -> datetime | None:
    """Return the timestamp of the first JSONL entry with tokens after window_start.

    Scans all provided JSONL files and returns the earliest timestamp >= window_start
    that has at least one token. Returns None if no such entry exists.
    """
    earliest: datetime | None = None

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(entry, dict):
                continue

            ts = _parse_timestamp(entry.get("timestamp"))
            if ts is None or ts < window_start:
                continue

            inp, out = _extract_tokens(entry)
            if inp + out == 0:
                continue

            if earliest is None or ts < earliest:
                earliest = ts

    return earliest


def _sum_tokens_in_window(
    files: list[Path],
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Sum input+output tokens for entries within [window_start, window_end]."""
    total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(entry, dict):
                continue

            ts = _parse_timestamp(entry.get("timestamp"))
            if ts is None:
                continue

            if window_start <= ts <= window_end:
                inp, out = _extract_tokens(entry)
                total += inp + out

    return total


def _is_opus_model(model_str: str) -> bool:
    """Return True if the model string identifies an Opus-class model."""
    return "opus" in model_str.lower()


def _is_sonnet_model(model_str: str) -> bool:
    """Return True if the model string identifies a Sonnet-class model."""
    return "sonnet" in model_str.lower()


def _sum_tokens_by_model(
    files: list[Path],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, int]:
    """Sum tokens separately for Sonnet and Opus within [window_start, window_end].

    Returns:
        dict with keys:
          sonnet_tokens  — total input+output for Sonnet-class models
          opus_tokens    — total input+output for Opus-class models
          other_tokens   — total for unrecognised/unknown model entries
    """
    sonnet = 0
    opus = 0
    other = 0

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(entry, dict):
                continue

            ts = _parse_timestamp(entry.get("timestamp"))
            if ts is None:
                continue

            if not (window_start <= ts <= window_end):
                continue

            inp, out = _extract_tokens(entry)
            tok = inp + out
            if tok == 0:
                continue

            # Check model field in entry or nested message
            model_str = ""
            if isinstance(entry.get("message"), dict):
                model_str = str(entry["message"].get("model") or "")
            if not model_str:
                model_str = str(entry.get("model") or "")

            if model_str and _is_opus_model(model_str):
                opus += tok
            elif model_str and _is_sonnet_model(model_str):
                sonnet += tok
            else:
                # No model label — attribute to Sonnet as the dominant default
                sonnet += tok

    return {"sonnet_tokens": sonnet, "opus_tokens": opus, "other_tokens": other}


# ---------------------------------------------------------------------------
# 5h reset boundary helpers
# ---------------------------------------------------------------------------

_BB_RESET_KEY = "subscription/last_5h_reset"


def last_5h_reset(
    now: datetime | None = None,
    _projects_dir_override: Optional[Path] = None,
) -> datetime:
    """Return the start of the current 5-hour subscription window.

    Resolution order:
      1. Blackboard key ``subscription/last_5h_reset`` — if stored AND within
         (now-5h, now], use it directly (persisted from a prior call).
      2. Earliest token entry in the last 5h of transcripts — pin it to the
         blackboard so future calls avoid a full transcript scan.
      3. Idle (no tokens in last 5h) — return (now-5h) without pinning.

    Args:
        now: Current time override (for testing). Defaults to UTC now.
        _projects_dir_override: Override for the projects directory (for testing).

    Returns:
        UTC datetime representing the start of the current 5h window.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    window_start_floor = now - timedelta(hours=5)

    # --- 1. Check blackboard for a persisted reset timestamp ---
    bb = None
    try:
        if _get_blackboard is not None:
            bb = _get_blackboard()
            stored = bb.read(_BB_RESET_KEY)
            if stored is not None:
                stored_dt = _parse_timestamp(str(stored))
                if stored_dt is not None and window_start_floor < stored_dt <= now:
                    return stored_dt
    except Exception:  # noqa: BLE001
        pass

    # --- 2. Scan transcripts for earliest token entry in last 5h ---
    projects_dir = _projects_dir_override if _projects_dir_override is not None else _projects_dir()
    files = _find_jsonl_files(projects_dir)
    earliest = None
    if files:
        earliest = _earliest_token_entry_after(files, window_start_floor)

    if earliest is not None:
        # Pin to blackboard so next call skips the scan
        if bb is not None:
            try:
                bb.write(_BB_RESET_KEY, earliest.isoformat(), updated_by="subscription_usage")
            except Exception:  # noqa: BLE001
                pass
        return earliest

    # --- 3. Idle: no tokens in last 5h — return floor without pinning ---
    return window_start_floor


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def current_usage(
    plan: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return rolling-window subscription usage.

    Args:
        plan: Plan name override. Resolution order: explicit arg → config →
              fallback "max-20x".
        now:  Current time override (for testing). Defaults to UTC now.

    Returns:
        dict with keys:
          percent        — usage as a float percentage (may exceed 100)
          tokens_used    — int, sum of input+output tokens in window
          tokens_quota   — int, plan quota for the window
          window_start   — ISO timestamp string
          window_end     — ISO timestamp string
          plan           — effective plan name
          window_hours   — window duration in hours
    """
    if now is None:
        now = datetime.now(timezone.utc)

    config = _load_config()
    plans = _load_plans()

    # Plan resolution
    effective_plan = plan or _config_plan(config) or "max-20x"
    plan_info = _plan_info(effective_plan, plans)

    window_hours_cfg = _config_window_hours(config)
    window_hours = float(
        window_hours_cfg
        if window_hours_cfg is not None
        else plan_info.get("window_hours", 5)
    )
    tokens_quota = int(plan_info.get("tokens_quota", _BUILTIN_PLANS["max-20x"]["tokens_quota"]))

    window_start = last_5h_reset(now)
    window_end = now

    # Token counting
    projects_dir = _projects_dir()
    files = _find_jsonl_files(projects_dir)
    tokens_used = 0
    if files:
        tokens_used = _sum_tokens_in_window(files, window_start, window_end)

    percent = (tokens_used / tokens_quota * 100.0) if tokens_quota > 0 else 0.0

    return {
        "percent": percent,
        "tokens_used": tokens_used,
        "tokens_quota": tokens_quota,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "plan": effective_plan,
        "window_hours": window_hours,
    }


# ---------------------------------------------------------------------------
# Weekly cap usage
# ---------------------------------------------------------------------------

def weekly_usage(
    plan: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return weekly Sonnet/Opus hour usage since the last scheduled reset.

    Uses community-derived token-to-hour conversion rates:
        Sonnet: 1 hour ≈ 4,000,000 tokens
        Opus:   1 hour ≈ 800,000 tokens

    These are rough estimates only. Anthropic does not publish authoritative
    throughput numbers for subscription cap accounting.

    Args:
        plan: Plan name override. Resolution order: explicit arg → config →
              fallback "max-20x".
        now:  Current time override (for testing). Defaults to UTC now.

    Returns:
        dict with keys:
          weekly_pct_sonnet   — float, Sonnet hour-equivalents used as a % of quota
          weekly_pct_opus     — float, Opus hour-equivalents used as a % of quota (0 if plan has no opus quota)
          sonnet_hours_used   — float, estimated Sonnet hours consumed since reset
          opus_hours_used     — float, estimated Opus hours consumed since reset
          sonnet_hours_quota  — int, weekly Sonnet quota from plan
          opus_hours_quota    — int, weekly Opus quota from plan
          sonnet_tokens       — int, raw Sonnet tokens since reset
          opus_tokens         — int, raw Opus tokens since reset
          time_to_reset       — str, e.g. "47h 23m"
          window_start        — ISO timestamp of last reset
          window_end          — ISO timestamp of now
          plan                — str, effective plan name
          _note               — conversion rate caveat
    """
    if now is None:
        now = datetime.now(timezone.utc)

    config = _load_config()
    plans = _load_plans()

    effective_plan = plan or _config_plan(config) or "max-20x"
    plan_info = _plan_info(effective_plan, plans)

    weekly_block = plan_info.get("weekly") or {}
    sonnet_hours_quota = int(weekly_block.get("sonnet_hours_quota") or 240)
    opus_hours_quota = int(weekly_block.get("opus_hours_quota") or 24)

    reset_day, reset_time_str, reset_tz = _config_weekly_reset(config)

    window_start = last_weekly_reset(reset_day, reset_time_str, reset_tz, now)
    window_end = now

    # Token counting
    projects_dir = _projects_dir()
    files = _find_jsonl_files(projects_dir)
    by_model = _sum_tokens_by_model(files, window_start, window_end) if files else {
        "sonnet_tokens": 0, "opus_tokens": 0, "other_tokens": 0
    }

    sonnet_tokens = by_model["sonnet_tokens"]
    opus_tokens = by_model["opus_tokens"]

    sonnet_hours = sonnet_tokens / _SONNET_TOKENS_PER_HOUR
    opus_hours = opus_tokens / _OPUS_TOKENS_PER_HOUR

    weekly_pct_sonnet = (sonnet_hours / sonnet_hours_quota * 100.0) if sonnet_hours_quota > 0 else 0.0
    weekly_pct_opus = (opus_hours / opus_hours_quota * 100.0) if opus_hours_quota > 0 else 0.0

    return {
        "weekly_pct_sonnet": weekly_pct_sonnet,
        "weekly_pct_opus": weekly_pct_opus,
        "sonnet_hours_used": round(sonnet_hours, 4),
        "opus_hours_used": round(opus_hours, 4),
        "sonnet_hours_quota": sonnet_hours_quota,
        "opus_hours_quota": opus_hours_quota,
        "sonnet_tokens": sonnet_tokens,
        "opus_tokens": opus_tokens,
        "time_to_reset": time_to_reset(reset_day, reset_time_str, reset_tz, now),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "plan": effective_plan,
        "_note": (
            "Conversion: Sonnet 1h≈4M tokens, Opus 1h≈800K tokens. "
            "Community-derived estimates only — not officially published."
        ),
    }


# ---------------------------------------------------------------------------
# Team Lead JSONL helpers
# ---------------------------------------------------------------------------

# Project dir name for Team Lead sessions (without worktree suffix).
# Team Lead transcripts live directly in this dir; sub-agent transcripts live
# in sibling dirs with longer names that include the worktree path suffix.
_TEAM_LEAD_PROJECT_DIR_NAME = PROJECT_TRANSCRIPT_SLUG


def _team_lead_project_dir() -> Path:
    """Return the project dir that contains Team Lead JSONL transcripts."""
    return _projects_dir() / _TEAM_LEAD_PROJECT_DIR_NAME


def _is_team_lead_dir(dir_path: Path) -> bool:
    """Return True if this project directory belongs to the Team Lead session.

    Team Lead transcripts: ~/.claude/projects/-home-agent-fulcrumaxe/*.jsonl
    Sub-agent transcripts: .../-home-agent-fulcrumaxe--claude-worktrees-agent-*/*.jsonl

    We match the exact dir name to exclude all worktree variants.
    """
    return dir_path.name == _TEAM_LEAD_PROJECT_DIR_NAME


def _find_team_lead_jsonl_files(projects_dir: Path) -> list[Path]:
    """Glob *.jsonl files from the Team Lead project dir only (no subdirs).

    Excludes worktree variant dirs (sub-agents) by requiring an exact
    dir-name match against _TEAM_LEAD_PROJECT_DIR_NAME.
    """
    tl_dir = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
    if not tl_dir.exists() or not tl_dir.is_dir():
        return []
    try:
        return list(tl_dir.glob("*.jsonl"))
    except Exception:  # noqa: BLE001
        return []


def _extract_all_tokens(entry: dict) -> tuple[int, int, int, int]:
    """Return (input, output, cache_read, cache_write) from a JSONL entry.

    Handles Claude Code transcript shape: entry["message"]["usage"].
    Also handles top-level entry["usage"].
    """
    input_tok = 0
    output_tok = 0
    cache_read = 0
    cache_write = 0

    usage: dict | None = None

    # Shape 1: Claude Code session transcripts nest usage under `message`
    message = entry.get("message")
    if isinstance(message, dict):
        nested = message.get("usage")
        if isinstance(nested, dict):
            usage = nested

    # Shape 2: top-level usage dict
    if usage is None:
        top = entry.get("usage")
        if isinstance(top, dict):
            usage = top

    if usage is not None:
        input_tok = int(usage.get("input_tokens") or 0)
        output_tok = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    else:
        # Shape 3: direct keys
        input_tok = int(entry.get("input_tokens") or 0)
        output_tok = int(entry.get("output_tokens") or 0)
        cache_read = int(entry.get("cache_read_input_tokens") or 0)
        cache_write = int(entry.get("cache_creation_input_tokens") or 0)

    return max(0, input_tok), max(0, output_tok), max(0, cache_read), max(0, cache_write)


def team_lead_usage(
    since_ts: float | None = None,
    until_ts: float | None = None,
    window_seconds: int = 18000,
    _projects_dir_override: Path | None = None,
) -> dict:
    """Return Team Lead session token usage within a time window.

    Reads JSONL files from ~/.claude/projects/-home-agent-fulcrumaxe/
    (the exact project root dir — excludes worktree sub-agent dirs).

    Args:
        since_ts:          Unix timestamp for window start. Defaults to
                           ``now - window_seconds`` when None.
        until_ts:          Unix timestamp for window end. Defaults to now when None.
        window_seconds:    Fallback window size when since_ts is None (default 5h).
        _projects_dir_override: Override projects dir path (for testing).

    Returns:
        {
            "input":          int,   # regular input tokens
            "output":         int,   # output tokens
            "cache_read":     int,   # cache-read input tokens (separately tracked)
            "cache_write":    int,   # cache-creation input tokens (separately tracked)
            "session_files":  list[str],  # JSONL filenames found (names only)
            "window_seconds": int,   # effective window used
            "since_ts":       float,
            "until_ts":       float,
        }
    """
    now_dt = datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()

    effective_until = until_ts if until_ts is not None else now_ts
    effective_since = since_ts if since_ts is not None else (effective_until - window_seconds)

    window_start = datetime.fromtimestamp(effective_since, tz=timezone.utc)
    window_end = datetime.fromtimestamp(effective_until, tz=timezone.utc)

    projects_dir = _projects_dir_override if _projects_dir_override is not None else _projects_dir()
    files = _find_team_lead_jsonl_files(projects_dir)

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    per_turn_totals: list[int] = []

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(entry, dict):
                continue

            ts = _parse_timestamp(entry.get("timestamp"))
            if ts is None:
                continue

            if not (window_start <= ts <= window_end):
                continue

            inp, out, cr, cw = _extract_all_tokens(entry)
            turn_total = inp + out
            if turn_total > 0:
                total_input += inp
                total_output += out
                total_cache_read += cr
                total_cache_write += cw
                per_turn_totals.append(turn_total)

    # Compute p50 / p95 of per-turn token totals
    p50 = 0
    p95 = 0
    if per_turn_totals:
        sorted_turns = sorted(per_turn_totals)
        n = len(sorted_turns)
        p50_idx = max(0, int(n * 0.50) - 1) if n > 1 else 0
        p95_idx = max(0, int(n * 0.95) - 1) if n > 1 else 0
        p50 = sorted_turns[p50_idx]
        p95 = sorted_turns[p95_idx]

    return {
        "input": total_input,
        "output": total_output,
        "cache_read": total_cache_read,
        "cache_write": total_cache_write,
        "session_files": [f.name for f in files],
        "window_seconds": int(effective_until - effective_since),
        "since_ts": effective_since,
        "until_ts": effective_until,
        "p50_tokens_per_turn": p50,
        "p95_tokens_per_turn": p95,
        "sessions_count": len(files),
    }


# ---------------------------------------------------------------------------
# Subscription billing cost caps (USD)
# ---------------------------------------------------------------------------

# Monthly subscription billing cost per plan in USD.
# Used by current_plan_limits() to derive daily/monthly caps when no explicit
# config values are present.
_PLAN_MONTHLY_USD: dict[str, float] = {
    "pro": 20.0,
    "max-5x": 100.0,
    "max-20x": 200.0,
    "team": 25.0,  # per-user assumption: 1 user
}

_FALLBACK_DAILY_USD = 15.0
_FALLBACK_MONTHLY_USD = 450.0


def current_plan_limits(plan: str | None = None) -> dict[str, Any]:
    """Return USD billing caps for the current (or given) Claude subscription plan.

    Resolution order:
      1. ``config`` — if config.json has ``subscription.daily_usd_cap`` and
         ``subscription.monthly_usd_cap``, those values win.
      2. ``estimated`` — derive from the plan name using the table in the module
         docstring.  ``daily_usd_cap = monthly_usd_cap / 30``.
      3. ``hardcoded-fallback`` — $15/day, $450/month when neither config nor
         plan resolution yields a result.  Matches old dashboard constants so
         there is zero visible regression on failure.

    Args:
        plan: Plan name override.  When None, the plan is read from config.json
              (subscription.plan); defaults to "max-20x" if absent.

    Returns:
        {
            "daily_usd_cap":   float,
            "monthly_usd_cap": float,
            "source":          "config" | "estimated" | "hardcoded-fallback",
        }
    """
    config = _load_config()

    # --- 1. Explicit config values ---
    sub_cfg = config.get("subscription") or {}
    explicit_daily = sub_cfg.get("daily_usd_cap")
    explicit_monthly = sub_cfg.get("monthly_usd_cap")
    if explicit_daily is not None and explicit_monthly is not None:
        try:
            return {
                "daily_usd_cap": float(explicit_daily),
                "monthly_usd_cap": float(explicit_monthly),
                "source": "config",
            }
        except (TypeError, ValueError):
            pass

    # --- 2. Estimate from plan name ---
    effective_plan = plan or _config_plan(config) or "max-20x"
    monthly_usd = _PLAN_MONTHLY_USD.get(effective_plan)
    if monthly_usd is not None:
        return {
            "daily_usd_cap": round(monthly_usd / 30, 4),
            "monthly_usd_cap": monthly_usd,
            "source": "estimated",
        }

    # --- 3. Hardcoded fallback ---
    return {
        "daily_usd_cap": _FALLBACK_DAILY_USD,
        "monthly_usd_cap": _FALLBACK_MONTHLY_USD,
        "source": "hardcoded-fallback",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show rolling-window Claude subscription quota usage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print usage dict as JSON instead of human-readable line.",
    )
    parser.add_argument(
        "--plan",
        metavar="PLAN",
        help="Override plan (pro, max-5x, max-20x, team).",
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        dest="weekly",
        help="Report weekly Sonnet/Opus hour cap usage instead of the 5h rolling window.",
    )
    parser.add_argument(
        "--team-lead",
        action="store_true",
        dest="team_lead",
        help="Report Team Lead (parent session) token usage from JSONL transcripts.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=18000,
        metavar="SECONDS",
        help="Window in seconds for --team-lead (default 18000 = 5h).",
    )
    args = parser.parse_args()

    if args.team_lead:
        result = team_lead_usage(window_seconds=args.window)
        if args.json_output:
            print(json.dumps(result, indent=2))
        else:
            inp = result["input"]
            out = result["output"]
            cr = result["cache_read"]
            cw = result["cache_write"]
            sessions = result["sessions_count"]
            p50 = result["p50_tokens_per_turn"]
            p95 = result["p95_tokens_per_turn"]
            winsec = result["window_seconds"]
            winh = winsec / 3600
            print(f"Team Lead usage  window={winh:.1f}h  sessions={sessions}")
            print(f"  input:       {inp:>12,}")
            print(f"  output:      {out:>12,}")
            print(f"  cache_read:  {cr:>12,}")
            print(f"  cache_write: {cw:>12,}")
            print(f"  p50/turn:    {p50:>12,}")
            print(f"  p95/turn:    {p95:>12,}")
        return

    if args.weekly:
        result = weekly_usage(plan=args.plan or None)
        if args.json_output:
            print(json.dumps(result, indent=2))
        else:
            spct = result["weekly_pct_sonnet"]
            opct = result["weekly_pct_opus"]
            sh = result["sonnet_hours_used"]
            sq = result["sonnet_hours_quota"]
            oh = result["opus_hours_used"]
            oq = result["opus_hours_quota"]
            ttr = result["time_to_reset"]
            plan_name = result["plan"]
            print(
                f"weekly usage  plan={plan_name}  reset in {ttr}"
            )
            print(
                f"  sonnet: {spct:.1f}%  ({sh:.2f} / {sq}h)"
            )
            print(
                f"  opus:   {opct:.1f}%  ({oh:.2f} / {oq}h)"
            )
            print(f"  note: {result['_note']}")
        return

    result = current_usage(plan=args.plan or None)

    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        pct = result["percent"]
        used = result["tokens_used"]
        quota = result["tokens_quota"]
        wh = result["window_hours"]
        plan_name = result["plan"]
        print(
            f"usage: {pct:.1f}%  ({used:,} / {quota:,} tokens,"
            f" window {wh:.0f}h, plan={plan_name})"
        )


if __name__ == "__main__":
    main()
