#!/usr/bin/env python3
"""
parse_jobs.py — YAML manifest parser/validator for the cron-bridge dispatcher.

Exit codes:
  0  valid — prints JSON array of enabled+due jobs to stdout
  1  schema invalid — prints error to stderr, no output to stdout

Usage:
  python3 scripts/schedule/parse_jobs.py --manifest scripts/schedule/jobs.yaml \
      --registry scripts/schedule/jobs --minute <YYYY-MM-DDTHH:MM>
  python3 scripts/schedule/parse_jobs.py --validate-only --manifest ...
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

SLUG_RE = re.compile(r'^[a-z0-9-]+$')
CRON_FIELDS = 5
EX_SCHEMA_INVALID = 1

REQUIRED_FIELDS = {"name", "job", "schedule"}
OPTIONAL_DEFAULTS = {
    "timeout_seconds": 300,
    "token_ceiling": 0,
    "backoff_minutes_on_failure": 0,
    "enabled": True,
}

# ── Sentinel exit codes (must match dispatcher.sh) ────────────────────────────

EX_ALREADY_RUNNING = 125
EX_BUDGET = 126
EX_BREAKER_OPEN = 127


def _die(msg: str, code: int = EX_SCHEMA_INVALID) -> None:
    print(f"[parse_jobs] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ── YAML loader (stdlib only — no PyYAML required) ────────────────────────────

def _load_yaml(path: Path) -> list[dict]:
    """
    Minimal YAML list parser. Handles the subset used in jobs.yaml:
    - list entries starting with '- '
    - key: value pairs (string, int, bool)
    - quoted strings
    Raises ValueError on parse errors.
    """
    try:
        import yaml  # type: ignore[import]
        with path.open() as f:
            data = yaml.safe_load(f)
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError("top-level must be a YAML list")
        return data
    except ImportError:
        pass
    # Fallback: hand-rolled parser for the small subset we use
    return _parse_yaml_list(path.read_text())


def _parse_yaml_list(text: str) -> list[dict]:
    """Parse a YAML list of mappings — minimal subset only."""
    entries: list[dict] = []
    current: dict | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue
        if line.startswith('- '):
            if current is not None:
                entries.append(current)
            current = {}
            line = line[2:]
        elif line.startswith('  ') and current is not None:
            line = line.lstrip()
        else:
            if line.startswith('- '):
                # already handled above
                pass
            elif current is None and line:
                raise ValueError(f"line {lineno}: unexpected content outside list: {line!r}")
            else:
                continue

        if ':' not in line:
            continue
        k, _, v = line.partition(':')
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # Strip inline comments
        v = re.sub(r'\s+#.*$', '', v)
        # Parse value types
        if v == '' or v is None:
            current[k] = None  # type: ignore[index]
        elif v.startswith('"') and v.endswith('"'):
            current[k] = v[1:-1]  # type: ignore[index]
        elif v.startswith("'") and v.endswith("'"):
            current[k] = v[1:-1]  # type: ignore[index]
        elif v.lower() == 'true':
            current[k] = True  # type: ignore[index]
        elif v.lower() == 'false':
            current[k] = False  # type: ignore[index]
        elif re.match(r'^-?\d+$', v):
            current[k] = int(v)  # type: ignore[index]
        else:
            current[k] = v  # type: ignore[index]

    if current is not None:
        entries.append(current)
    return entries


# ── Cron expression matcher ───────────────────────────────────────────────────

def _cron_matches(expr: str, dt: datetime) -> bool:
    """Return True if the cron expression matches dt (minute-level granularity)."""
    parts = expr.strip().split()
    if len(parts) != CRON_FIELDS:
        raise ValueError(f"cron expression must have {CRON_FIELDS} fields: {expr!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = parts

    def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
        if field == '*':
            return True
        for part in field.split(','):
            if '/' in part:
                rng, _, step = part.partition('/')
                step_n = int(step)
                lo_n = lo if rng == '*' else int(rng.split('-')[0])
                hi_n = hi if rng == '*' or '-' not in rng else int(rng.split('-')[1])
                if lo_n <= value <= hi_n and (value - lo_n) % step_n == 0:
                    return True
            elif '-' in part:
                a, b = part.split('-', 1)
                if int(a) <= value <= int(b):
                    return True
            elif int(part) == value:
                return True
        return False

    return (
        _field_match(minute_f, dt.minute, 0, 59)
        and _field_match(hour_f, dt.hour, 0, 23)
        and _field_match(dom_f, dt.day, 1, 31)
        and _field_match(month_f, dt.month, 1, 12)
        and _field_match(dow_f, dt.weekday(), 0, 6)  # 0=Mon in Python; cron 0=Sun
    )


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_entry(entry: dict, registry: Path) -> dict:
    """Validate a single job entry. Returns normalised entry or raises ValueError."""
    if not isinstance(entry, dict):
        raise ValueError(f"each job entry must be a mapping, got {type(entry).__name__}")

    missing = REQUIRED_FIELDS - set(entry.keys())
    if missing:
        raise ValueError(f"job entry missing required fields: {missing} — entry: {entry}")

    name = entry["name"]
    if not isinstance(name, str) or not SLUG_RE.match(name):
        raise ValueError(f"'name' must match [a-z0-9-]+, got: {name!r}")

    # Reject shell metacharacters defensively (belt and suspenders)
    if re.search(r'[;&|`$<>\\()\[\]{}\s]', str(name)):
        raise ValueError(f"'name' contains shell metacharacters: {name!r}")

    job_key = entry["job"]
    if not isinstance(job_key, str):
        raise ValueError(f"'job' must be a string, got: {type(job_key).__name__}")
    # Reject path traversal
    if '..' in job_key or '/' in job_key:
        raise ValueError(f"'job' must be a registry key (no path separators or '..'): {job_key!r}")
    # Reject shell metacharacters in job key
    if re.search(r'[;&|`$<>\\()\[\]{}\s\'\"]', job_key):
        raise ValueError(f"'job' contains invalid characters: {job_key!r}")

    registry_file = registry / f"{job_key}.sh"
    if not registry_file.exists():
        raise ValueError(f"'job' key {job_key!r} not found in registry: {registry_file}")
    if not os.access(registry_file, os.X_OK):
        raise ValueError(f"registry file not executable: {registry_file}")

    schedule = entry["schedule"]
    if not isinstance(schedule, str):
        raise ValueError(f"'schedule' must be a string, got: {type(schedule).__name__}")
    # Validate cron expression by attempting to parse it
    try:
        _cron_matches(schedule, datetime.now(timezone.utc))
    except ValueError as exc:
        raise ValueError(f"invalid 'schedule': {exc}") from exc

    # Apply defaults
    normalised = {**OPTIONAL_DEFAULTS, **entry}
    # Type check optional fields
    for field, expected_type in [
        ("timeout_seconds", int),
        ("token_ceiling", int),
        ("backoff_minutes_on_failure", int),
        ("enabled", bool),
    ]:
        val = normalised[field]
        if not isinstance(val, expected_type):
            raise ValueError(f"'{field}' must be {expected_type.__name__}, got {type(val).__name__}: {val!r}")
    if normalised["timeout_seconds"] <= 0:
        raise ValueError(f"'timeout_seconds' must be > 0, got: {normalised['timeout_seconds']}")

    return normalised


def validate_manifest(manifest_path: Path, registry: Path) -> list[dict]:
    """Load and validate manifest. Returns list of normalised job dicts."""
    if not manifest_path.exists():
        _die(f"manifest not found: {manifest_path}")

    try:
        raw = _load_yaml(manifest_path)
    except Exception as exc:
        _die(f"YAML parse error: {exc}")

    if not isinstance(raw, list):
        _die("manifest must be a YAML list of job entries")

    jobs = []
    for i, entry in enumerate(raw):
        try:
            jobs.append(_validate_entry(entry, registry))
        except ValueError as exc:
            _die(f"job entry [{i}] invalid: {exc}")

    return jobs


# ── Due-job filter ────────────────────────────────────────────────────────────

def filter_due(jobs: list[dict], now: datetime) -> list[dict]:
    """Return jobs that are enabled and whose schedule matches now."""
    due = []
    for job in jobs:
        if not job.get("enabled", True):
            continue
        try:
            if _cron_matches(job["schedule"], now):
                due.append(job)
        except ValueError:
            # Already validated — shouldn't happen
            pass
    return due


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Parse and validate jobs.yaml manifest")
    ap.add_argument("--manifest", required=True, help="Path to jobs.yaml")
    ap.add_argument("--registry", required=True, help="Path to jobs/ registry directory")
    ap.add_argument("--minute", default=None,
                    help="ISO8601 minute to check due jobs (default: now). Format: YYYY-MM-DDTHH:MM")
    ap.add_argument("--validate-only", action="store_true",
                    help="Validate only — do not filter by schedule; exit 0 if valid")
    ap.add_argument("--all-jobs", action="store_true",
                    help="Output all enabled jobs regardless of schedule")
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    registry_path = Path(args.registry)

    if not registry_path.is_dir():
        _die(f"registry directory not found: {registry_path}")

    jobs = validate_manifest(manifest_path, registry_path)

    if args.validate_only:
        print(f"[parse_jobs] {len(jobs)} job(s) validated OK", file=sys.stderr)
        sys.exit(0)

    if args.all_jobs:
        print(json.dumps(jobs))
        return

    if args.minute:
        try:
            now = datetime.fromisoformat(args.minute)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except ValueError:
            _die(f"--minute must be ISO8601 YYYY-MM-DDTHH:MM, got: {args.minute!r}")
    else:
        now = datetime.now(timezone.utc)

    due = filter_due(jobs, now)
    print(json.dumps(due))


if __name__ == "__main__":
    main()
