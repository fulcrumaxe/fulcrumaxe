"""run_analyst.py -- deterministic agent-run telemetry analyzer.

Reads recent agent-run data in bounded chunks, classifies issues using
regex + threshold classifiers (no LLM), and emits a structured report.

Usage:
    python3 backend/run_analyst.py [--since=7d] [--file-discussions] [--dry-run]

HARD RULE: This script MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop. It reads data only. See Discussion #439.
"""

from __future__ import annotations

if __name__ == '__main__' and __package__ is None:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple

from backend._repo import REPO, REPO_OWNER, REPO_NAME
from backend.loop_metrics_ts import parse_loop_metrics_ts as _parse_ts
from backend.repo_root import main_repo_root, repo_root
from backend.snapshot_path import SNAPSHOT_PATH

REPO_ROOT = repo_root()
RUN_REPORTS_DIR = REPO_ROOT / ".autonomous-team" / "run-reports"
AGENT_FEED = REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"
LOOP_RUNS_DIR = REPO_ROOT / ".autonomous-team" / "loop-runs"
COST_TRACKER = REPO_ROOT / ".autonomous-team" / "cost-tracker.json"
ROLE_EFFICIENCY = REPO_ROOT / ".autonomous-team" / "role-efficiency.json"
LOOP_METRICS = REPO_ROOT / ".autonomous-team" / "loop-metrics.jsonl"
BLACKBOARD_BUDGET_DIR = REPO_ROOT / ".autonomous-team" / "blackboard" / "budget"
SPAWN_QUEUE = REPO_ROOT / ".autonomous-team" / "spawn-queue.json"
WORKTREES_JSON = REPO_ROOT / ".autonomous-team" / "worktrees.json"
HOOK_EVENTS_DIR = REPO_ROOT / ".autonomous-team" / "hook-events"

CHUNK_SIZE = 30  # max runs per classification pass
MAX_FEED_EVENTS = 1000

# TTL for gh pr view cache (seconds)
_PR_CACHE: dict[int, tuple[float, dict]] = {}
PR_CACHE_TTL = 60.0

# Regex patterns for tool-use anomaly detection
RUNAWAY_PATTERNS = re.compile(
    r"(claude\s+-p|_start_loop_run|trigger.*\/loop|subprocess.*claude)",
    re.IGNORECASE,
)

SCOPE_CREEP_PATTERNS = re.compile(
    r"(scope.?creep|out.?of.?scope|too.?broad|over.?engineered)",
    re.IGNORECASE,
)

# Regex for git branch contamination
WORKTREE_CONTAMINATION_PATTERNS = re.compile(
    r"(git\s+checkout\s+(?!-b)|gh\s+pr\s+checkout|switched\s+to\s+branch|HEAD\s+is\s+now\s+at)",
    re.IGNORECASE,
)

# Regex for hard-rule violations beyond claude -p (indirect invocations)
HARD_RULE_VIOLATION_PATTERNS = re.compile(
    r"(subprocess\.Popen.*claude|os\.system.*claude|exec\(.*claude|"
    r"git\s+rm\s+(?!--cached)|general.purpose.*subagent|subagent_type.*general.purpose|"
    r"git\s+rm\s+[^\s])",
    re.IGNORECASE,
)

# Patterns for AGENT_OUTPUT envelope checks
AGENT_OUTPUT_MISSING_PATTERNS = re.compile(
    r"(AGENT_OUTPUT.*missing|envelope.*missing|malformed.*envelope|falling\s+back.*prose)",
    re.IGNORECASE,
)

# post-agent-hook call markers
POST_AGENT_HOOK_PATTERNS = re.compile(
    r"(post.agent.hook|post_agent_hook|scripts/post-agent-hook)",
    re.IGNORECASE,
)


def parse_since(since_str: str) -> datetime:
    """Parse a duration string like '7d', '24h', '30m' into a UTC datetime."""
    now = datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([dhm])", since_str.strip())
    if not m:
        raise ValueError(f"Invalid since format: {since_str!r}. Use e.g. '7d', '24h'.")
    value, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=value), "h": timedelta(hours=value), "m": timedelta(minutes=value)}[unit]
    return now - delta


# ---------------------------------------------------------------------------
# Data ingestion
# ---------------------------------------------------------------------------

def load_feed_events(since: datetime, max_events: int = MAX_FEED_EVENTS) -> list[dict]:
    """Load up to max_events events from agent-feed.jsonl (+ gzip rotations) since cutoff."""
    events: list[dict] = []

    # Rotated gzip files first (oldest to newest)
    gzip_files = sorted(AGENT_FEED.parent.glob("agent-feed-*.jsonl.gz"))
    for gz_path in gzip_files:
        try:
            with gzip.open(gz_path, "rt") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        ts = _parse_ts(ev.get("timestamp") or ev.get("ts") or "")
                        if ts and ts >= since:
                            events.append(ev)
                    except (json.JSONDecodeError, KeyError):
                        pass
        except (OSError, gzip.BadGzipFile):
            pass

    # Live feed
    if AGENT_FEED.exists():
        try:
            with open(AGENT_FEED) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        ts = _parse_ts(ev.get("timestamp") or ev.get("ts") or "")
                        if ts and ts >= since:
                            events.append(ev)
                    except (json.JSONDecodeError, KeyError):
                        pass
        except OSError:
            pass

    # Return most recent N events
    events.sort(key=lambda e: e.get("timestamp") or e.get("ts") or "", reverse=True)
    return events[:max_events]


def load_loop_logs(since: datetime) -> list[dict]:
    """Load loop-run log entries from .autonomous-team/loop-runs/ since cutoff."""
    entries: list[dict] = []
    if not LOOP_RUNS_DIR.exists():
        return entries
    for log_file in LOOP_RUNS_DIR.rglob("*.log"):
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
            if mtime < since:
                continue
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entry.setdefault("_source", str(log_file))
                        entries.append(entry)
                    except json.JSONDecodeError:
                        entries.append({"message": line, "_source": str(log_file), "_raw": True})
        except OSError:
            pass
    return entries


def load_audit_trail(since: datetime) -> list[dict]:
    """Load audit trail entries via the CLI tool."""
    try:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "backend" / "audit_trail.py"),
             "search", "--since=7d", "--format=json"],
            capture_output=True, text=True, timeout=30,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, FileNotFoundError):
        pass
    return []


def load_role_efficiency() -> dict:
    """Load role-efficiency.json if present."""
    if ROLE_EFFICIENCY.exists():
        try:
            with open(ROLE_EFFICIENCY) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_cost_tracker() -> dict:
    """Load cost-tracker.json if present."""
    if COST_TRACKER.exists():
        try:
            with open(COST_TRACKER) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_needs_fix_prs() -> list[dict]:
    """Load open PRs with code-review-needs-fix label."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", REPO,
             "--label", "code-review-needs-fix", "--json",
             "number,title,labels,createdAt,url"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, FileNotFoundError):
        pass
    return []


def load_loop_metrics(since: datetime) -> list[dict]:
    """Load loop-metrics.jsonl entries since cutoff."""
    entries: list[dict] = []
    if not LOOP_METRICS.exists():
        return entries
    try:
        with open(LOOP_METRICS) as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    raw_ts = entry.get("timestamp") or entry.get("ts")
                    ts = _parse_ts(raw_ts) if raw_ts is not None else None
                    if ts is not None and ts >= since:
                        entries.append(entry)
                    elif raw_ts is not None and ts is None:
                        # Value was present but not a parseable timestamp
                        # (e.g. an int epoch instead of an ISO string) --
                        # skip the row but say so, don't swallow it.
                        print(
                            f"run_analyst: skipping malformed row at "
                            f"{LOOP_METRICS.name}:{lineno} (unparseable timestamp: {raw_ts!r})",
                            file=sys.stderr,
                        )
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    print(
                        f"run_analyst: skipping malformed row at {LOOP_METRICS.name}:{lineno} "
                        f"({type(exc).__name__})",
                        file=sys.stderr,
                    )
    except OSError:
        pass
    return entries


def load_budget_data() -> dict:
    """Load budget data from blackboard budget/ keys."""
    result: dict = {}
    if not BLACKBOARD_BUDGET_DIR.exists():
        return result
    for key_file in BLACKBOARD_BUDGET_DIR.rglob("*.json"):
        try:
            with open(key_file) as f:
                data = json.load(f)
            rel = key_file.relative_to(BLACKBOARD_BUDGET_DIR)
            result[str(rel)] = data
        except (json.JSONDecodeError, OSError):
            pass
    return result


def load_spawn_queue() -> list[dict]:
    """Load spawn-queue.json items."""
    if not SPAWN_QUEUE.exists():
        return []
    try:
        with open(SPAWN_QUEUE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items", [])
    except (json.JSONDecodeError, OSError):
        pass
    return []


def load_worktrees_registry() -> list[dict]:
    """Load worktrees.json registry."""
    if not WORKTREES_JSON.exists():
        return []
    try:
        with open(WORKTREES_JSON) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.values())
    except (json.JSONDecodeError, OSError):
        pass
    return []


def load_hook_events(since: datetime) -> list[dict]:
    """Load hook event files from .autonomous-team/hook-events/."""
    events: list[dict] = []
    if not HOOK_EVENTS_DIR.exists():
        return events
    for ev_file in HOOK_EVENTS_DIR.rglob("*.json"):
        try:
            mtime = datetime.fromtimestamp(ev_file.stat().st_mtime, tz=timezone.utc)
            if mtime < since:
                continue
            with open(ev_file) as f:
                data = json.load(f)
            data.setdefault("_source", str(ev_file))
            events.append(data)
        except (json.JSONDecodeError, OSError):
            pass
    return events


def get_pr_diff_size(pr_number: int) -> dict:
    """Get PR additions+deletions, cached with TTL=60s."""
    now = time.time()
    if pr_number in _PR_CACHE:
        cached_at, cached_data = _PR_CACHE[pr_number]
        if now - cached_at < PR_CACHE_TTL:
            return cached_data
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo",
             REPO,
             "--json", "additions,deletions,labels,files"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            _PR_CACHE[pr_number] = (now, data)
            return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, FileNotFoundError):
        pass
    return {}


def get_current_branch() -> str:
    """Get the current branch of the parent repo (point-in-time)."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def load_loop_snapshot() -> dict:
    """Load the most recent loop snapshot from the canonical state-dir path."""
    snapshot_path = Path(SNAPSHOT_PATH)
    if not snapshot_path.exists():
        return {}
    try:
        with open(snapshot_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Classifiers  (deterministic -- regex + thresholds only)
# ---------------------------------------------------------------------------

def classify_failure_clusters(
    feed_events: list[dict], loop_logs: list[dict], audit: list[dict]
) -> list[dict]:
    """Group repeated error strings across sources."""
    error_counts: dict[str, list[str]] = defaultdict(list)
    all_entries = feed_events + loop_logs + audit

    for entry in all_entries:
        text = _entry_text(entry)
        for pattern in [
            r"merge conflict",
            r"preflight failed",
            r"permission denied",
            r"timeout",
            r"rate limit",
            r"502|503|504",
            r"worktree.*already",
            r"branch.*not found",
            r"cannot read property",
            r"traceback",
        ]:
            if re.search(pattern, text, re.IGNORECASE):
                source = entry.get("_source") or entry.get("agent") or "unknown"
                error_counts[pattern].append(source)

    findings = []
    for pattern, sources in error_counts.items():
        if len(sources) >= 3:
            findings.append({
                "category": "failure_cluster",
                "severity": "high" if len(sources) >= 5 else "medium",
                "title": f"Pattern {pattern!r} hit {len(sources)} times in recent runs",
                "evidence": list(dict.fromkeys(sources))[:10],
                "suggested_discussion_title": f"[Bug] Recurring {pattern!r} errors in agent runs",
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_cost_outliers(role_efficiency: dict, cost_tracker: dict) -> list[dict]:
    """Agents whose token-per-pass exceeds 2x role median."""
    findings = []
    roles = role_efficiency.get("roles") or role_efficiency
    if not isinstance(roles, dict):
        return findings

    token_costs = {}
    for role, data in roles.items():
        if isinstance(data, dict):
            tokens = data.get("avg_tokens_per_pass") or data.get("input_tokens") or 0
            token_costs[role] = tokens

    if not token_costs:
        return findings

    median = _median(list(token_costs.values()))
    if median == 0:
        return findings

    for role, tokens in token_costs.items():
        if tokens > 2 * median:
            findings.append({
                "category": "cost_outlier",
                "severity": "medium",
                "title": f"Role '{role}' uses {tokens:,} tokens/pass (median={median:,.0f})",
                "evidence": [role],
                "suggested_discussion_title": f"[Small] Reduce token usage for {role} agent",
                "suggested_tag": "[Small]",
            })
    return findings


def classify_fix_cycle_loops(
    feed_events: list[dict], audit: list[dict], needs_fix_prs: list[dict]
) -> list[dict]:
    """Discussions where >=3 needs-fix rounds happened."""
    findings = []
    disc_fix_counts: dict[str, int] = defaultdict(int)
    for entry in feed_events + audit:
        text = _entry_text(entry)
        disc_match = re.search(r"discussion[:#\s]+#?(\d+)", text, re.IGNORECASE)
        if disc_match and re.search(r"needs.?fix|needs_fix|fix.round", text, re.IGNORECASE):
            disc_fix_counts[disc_match.group(1)] += 1

    for disc_id, count in disc_fix_counts.items():
        if count >= 3:
            findings.append({
                "category": "fix_cycle_loop",
                "severity": "high",
                "title": f"Discussion #{disc_id} had {count} needs-fix rounds",
                "evidence": [f"discussion#{disc_id}"],
                "suggested_discussion_title": f"[Bug] Fix-cycle loop on Discussion #{disc_id}",
                "suggested_tag": "[Bug]",
            })

    for pr in needs_fix_prs:
        pr_num = str(pr.get("number", "?"))
        label_names = [l.get("name", "") for l in pr.get("labels", [])]
        fix_count = sum(1 for l in label_names if "needs-fix" in l)
        if fix_count >= 2:
            findings.append({
                "category": "fix_cycle_loop",
                "severity": "high",
                "title": f"PR #{pr_num} stuck with {fix_count} needs-fix labels: {pr.get('title','')}",
                "evidence": [f"pr#{pr_num}", pr.get("url", "")],
                "suggested_discussion_title": f"[Bug] PR #{pr_num} stuck in fix-cycle loop",
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_stalled_patterns(feed_events: list[dict], since: datetime) -> list[dict]:
    """Discussions stuck at IMPLEMENTING with no PR for >24h."""
    findings = []

    implementing_seen: dict[str, datetime] = {}
    pr_created_for: set[str] = set()

    for entry in sorted(feed_events, key=lambda e: e.get("timestamp") or ""):
        text = _entry_text(entry)
        ts = _parse_ts(entry.get("timestamp") or entry.get("ts") or "")
        disc_match = re.search(r"discussion[:#\s]+#?(\d+)", text, re.IGNORECASE)
        if not disc_match:
            continue
        disc_id = disc_match.group(1)

        if re.search(r"STATUS.*IMPLEMENTING|started.*implementing|executor.*started", text, re.IGNORECASE):
            if ts:
                implementing_seen.setdefault(disc_id, ts)

        if re.search(r"PR.*created|pull.?request.*created|#\d+.*merged", text, re.IGNORECASE):
            pr_created_for.add(disc_id)

    now = datetime.now(timezone.utc)
    for disc_id, impl_ts in implementing_seen.items():
        if disc_id not in pr_created_for and (now - impl_ts) > timedelta(hours=24):
            hours_stalled = int((now - impl_ts).total_seconds() / 3600)
            findings.append({
                "category": "stalled_pattern",
                "severity": "high",
                "title": f"Discussion #{disc_id} stuck at IMPLEMENTING for {hours_stalled}h with no PR",
                "evidence": [f"discussion#{disc_id}"],
                "suggested_discussion_title": f"[Bug] Discussion #{disc_id} stalled -- no PR after {hours_stalled}h",
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_spec_quality_flags(
    feed_events: list[dict], audit: list[dict]
) -> list[dict]:
    """PRs where reviewer flagged scope creep or out-of-scope issues."""
    findings = []
    seen: set[str] = set()

    for entry in feed_events + audit:
        text = _entry_text(entry)
        if SCOPE_CREEP_PATTERNS.search(text):
            pr_match = re.search(r"pr[:#\s]+#?(\d+)", text, re.IGNORECASE)
            key = pr_match.group(1) if pr_match else "unknown"
            if key not in seen:
                seen.add(key)
                findings.append({
                    "category": "spec_quality_flag",
                    "severity": "medium",
                    "title": f"Scope creep or out-of-scope flag on PR #{key}",
                    "evidence": [f"pr#{key}"],
                    "suggested_discussion_title": "[Small] Tighten spec scope definition process",
                    "suggested_tag": "[Small]",
                })
    return findings


def classify_tool_use_anomalies(
    feed_events: list[dict], loop_logs: list[dict], audit: list[dict]
) -> list[dict]:
    """Agents calling claude -p or _start_loop_run from Bash (runaway pattern)."""
    findings = []
    sources_flagged: list[str] = []

    for entry in feed_events + loop_logs + audit:
        text = _entry_text(entry)
        if RUNAWAY_PATTERNS.search(text):
            source = entry.get("_source") or entry.get("agent") or entry.get("role") or "unknown"
            sources_flagged.append(source)

    if sources_flagged:
        findings.append({
            "category": "tool_use_anomaly",
            "severity": "high",
            "title": f"Runaway spawn pattern detected in {len(sources_flagged)} entries",
            "evidence": list(dict.fromkeys(sources_flagged))[:10],
            "suggested_discussion_title": "[Bug] Agent invoked claude/-p or _start_loop_run -- runaway pattern",
            "suggested_tag": "[Bug]",
        })
    return findings


def classify_time_anomalies(role_efficiency: dict, feed_events: list[dict]) -> list[dict]:
    """Runs >2x role-median duration."""
    findings = []
    roles = role_efficiency.get("roles") or role_efficiency
    if not isinstance(roles, dict):
        return findings

    role_medians: dict[str, float] = {}
    for role, data in roles.items():
        if isinstance(data, dict):
            dur = data.get("avg_duration_seconds") or data.get("duration_seconds") or 0
            if dur:
                role_medians[role] = float(dur)

    if not role_medians:
        return findings

    for entry in feed_events:
        role = entry.get("role") or entry.get("agent") or ""
        duration = entry.get("duration_seconds") or entry.get("duration") or 0
        if not role or not duration:
            continue
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            continue
        median = role_medians.get(role, 0)
        if median and duration > 2 * median:
            findings.append({
                "category": "time_anomaly",
                "severity": "medium",
                "title": f"Role '{role}' run took {duration:.0f}s (median={median:.0f}s)",
                "evidence": [entry.get("id", "unknown")],
                "suggested_discussion_title": f"[Small] Investigate slow {role} runs",
                "suggested_tag": "[Small]",
            })

    return findings


# ---------------------------------------------------------------------------
# New classifiers (Phase A -- Discussion #478)
# ---------------------------------------------------------------------------

def classify_worktree_contamination(
    feed_events: list[dict], loop_logs: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect executors that ran git checkout / gh pr checkout in the parent repo.

    Severity: high -- these commands can leave the parent repo on a wrong branch,
    corrupting subsequent operations (see PR #438, #443, #475).
    """
    findings = []
    contaminated: list[str] = []

    for entry in feed_events + loop_logs + audit:
        text = _entry_text(entry)
        if WORKTREE_CONTAMINATION_PATTERNS.search(text):
            role = entry.get("role") or entry.get("agent") or "unknown"
            source = entry.get("_source") or entry.get("id") or "unknown"
            # Only flag executor roles (not code-reviewer doing gh pr checkout legitimately)
            if role in ("executor", "unknown") or "executor" in role:
                contaminated.append(source)

    if contaminated:
        findings.append({
            "category": "worktree_contamination",
            "severity": "high",
            "title": f"Worktree contamination: git checkout/gh pr checkout detected in {len(contaminated)} entries",
            "evidence": list(dict.fromkeys(contaminated))[:10],
            "suggested_discussion_title": "[Bug] Executor running git checkout in parent repo — worktree contamination",
            "suggested_tag": "[Bug]",
        })
    return findings


def classify_hard_rule_violations(
    feed_events: list[dict], loop_logs: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect hard-rule violations beyond the basic claude -p check.

    Patterns: subprocess.Popen(claude), os.system(claude), git rm on project files,
    general-purpose subagent_type usage.
    Severity: high -- these violate explicit team rules.
    """
    findings = []
    violations: list[str] = []

    for entry in feed_events + loop_logs + audit:
        text = _entry_text(entry)
        if HARD_RULE_VIOLATION_PATTERNS.search(text):
            source = entry.get("_source") or entry.get("agent") or entry.get("role") or "unknown"
            violations.append(source)

    if violations:
        findings.append({
            "category": "hard_rule_violation",
            "severity": "high",
            "title": f"Hard-rule violation (git rm / general-purpose / indirect claude spawn) in {len(violations)} entries",
            "evidence": list(dict.fromkeys(violations))[:10],
            "suggested_discussion_title": "[Bug] Hard-rule violation detected in agent run",
            "suggested_tag": "[Bug]",
        })
    return findings


def classify_agent_output_missing(
    feed_events: list[dict], loop_logs: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect agents that reported success in prose but had no parseable AGENT_OUTPUT envelope.

    Severity: medium -- breaks structured routing, falls back to fragile prose parsing.
    """
    findings = []
    missing_sources: list[str] = []

    for entry in feed_events + loop_logs + audit:
        text = _entry_text(entry)
        if AGENT_OUTPUT_MISSING_PATTERNS.search(text):
            source = entry.get("_source") or entry.get("agent") or entry.get("role") or "unknown"
            missing_sources.append(source)

    if missing_sources:
        findings.append({
            "category": "agent_output_missing",
            "severity": "medium",
            "title": f"AGENT_OUTPUT envelope missing or malformed in {len(missing_sources)} entries",
            "evidence": list(dict.fromkeys(missing_sources))[:10],
            "suggested_discussion_title": "[Bug] Agents not emitting AGENT_OUTPUT envelopes — routing falls back to prose",
            "suggested_tag": "[Bug]",
        })
    return findings


def classify_test_coverage_gap(
    feed_events: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect code-reviewer pass verdicts with empty tests_run for backend/dashboard PRs.

    Severity: medium -- skipping test runs on code changes hides regressions.
    """
    findings = []
    gap_prs: list[str] = []

    for entry in feed_events + audit:
        text = _entry_text(entry)
        # Look for pass verdict events with empty tests_run
        if re.search(r"tests_run.*\[\]|tests_run.*empty|empty.*tests_run", text, re.IGNORECASE):
            if re.search(r"code.review.passed|verdict.*pass", text, re.IGNORECASE):
                pr_match = re.search(r"pr[:#\s]+#?(\d+)", text, re.IGNORECASE)
                key = pr_match.group(1) if pr_match else "unknown"
                gap_prs.append(key)

        # Also check for AGENT_OUTPUT envelope with empty tests_run and pass verdict
        data = entry.get("data") or {}
        if isinstance(data, dict):
            tests_run = data.get("tests_run", None)
            verdict = data.get("verdict", "")
            agent = data.get("agent", "")
            if (agent == "code-reviewer" and verdict == "pass"
                    and tests_run is not None and len(tests_run) == 0):
                pr = str(data.get("pr", "unknown"))
                gap_prs.append(pr)

    if gap_prs:
        findings.append({
            "category": "test_coverage_gap",
            "severity": "medium",
            "title": f"Code-reviewer passed {len(gap_prs)} PR(s) with empty tests_run",
            "evidence": [f"pr#{p}" for p in list(dict.fromkeys(gap_prs))[:10]],
            "suggested_discussion_title": "[Bug] Code-reviewer marking pass without running tests",
            "suggested_tag": "[Bug]",
        })
    return findings


def classify_missing_post_agent_hook(
    feed_events: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect agent spawn events with no matching post-agent-hook entry.

    Heuristic: audit entries with role=spawn and no corresponding hook event.
    Severity: medium -- missing hook = budget/cost tracking misses spend.
    """
    findings = []
    spawn_roles: list[str] = []
    hook_recorded_roles: set[str] = set()

    for entry in feed_events + audit:
        text = _entry_text(entry)
        event_type = entry.get("event_type") or entry.get("type") or ""

        if re.search(r"spawn|spawned|SPAWN_REQUEST", text, re.IGNORECASE) and event_type not in ("hook", "post_hook"):
            role = entry.get("role") or entry.get("agent") or "unknown"
            spawn_roles.append(role)

        if POST_AGENT_HOOK_PATTERNS.search(text) or event_type in ("post_hook", "agent_complete"):
            role = entry.get("role") or entry.get("agent") or ""
            if role:
                hook_recorded_roles.add(role)

    # Roles that were spawned but never had a post-agent-hook call
    unhooked = [r for r in spawn_roles if r not in hook_recorded_roles and r != "unknown"]
    if unhooked:
        findings.append({
            "category": "missing_post_agent_hook",
            "severity": "medium",
            "title": f"Post-agent-hook not recorded for {len(unhooked)} spawn(s): {', '.join(list(dict.fromkeys(unhooked))[:5])}",
            "evidence": list(dict.fromkeys(unhooked))[:10],
            "suggested_discussion_title": "[Bug] Agent spawns missing post-agent-hook — budget tracking incomplete",
            "suggested_tag": "[Bug]",
        })
    return findings


def classify_token_burn_no_output(
    feed_events: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect agents that consumed >100k tokens with no PR, comment, Discussion update, or envelope.

    Severity: medium -- wasted spend with no durable output.
    """
    findings = []
    TOKEN_THRESHOLD = 100_000

    for entry in feed_events + audit:
        tokens = 0
        input_t = entry.get("input_tokens") or entry.get("tokens_in") or 0
        output_t = entry.get("output_tokens") or entry.get("tokens_out") or 0
        try:
            tokens = int(input_t) + int(output_t)
        except (TypeError, ValueError):
            pass
        if tokens < TOKEN_THRESHOLD:
            continue

        text = _entry_text(entry)
        has_output = bool(re.search(
            r"PR\s+#\d+|pull.?request|discussion.*comment|AGENT_OUTPUT|verdict|merged",
            text, re.IGNORECASE,
        ))
        if not has_output:
            role = entry.get("role") or entry.get("agent") or "unknown"
            findings.append({
                "category": "token_burn_no_output",
                "severity": "medium",
                "title": f"Role '{role}' burned {tokens:,} tokens with no recorded output",
                "evidence": [entry.get("id", "unknown"), f"tokens={tokens}"],
                "suggested_discussion_title": f"[Small] Investigate {role} token burn without output",
                "suggested_tag": "[Small]",
            })
    return findings


def classify_discussion_respun_n_times(
    feed_events: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect Discussions that had >3 executor spawns — systemic issue.

    Severity: high when >5 spawns, medium when >3.
    """
    findings = []
    disc_spawn_counts: dict[str, int] = defaultdict(int)

    for entry in feed_events + audit:
        text = _entry_text(entry)
        role = entry.get("role") or entry.get("agent") or ""
        if role not in ("executor",) and not re.search(
            r"executor", text, re.IGNORECASE
        ):
            continue
        if not re.search(r"spawn|started|begin", text, re.IGNORECASE):
            continue
        disc_match = re.search(r"discussion[:#\s]+#?(\d+)", text, re.IGNORECASE)
        if disc_match:
            disc_spawn_counts[disc_match.group(1)] += 1

    for disc_id, count in disc_spawn_counts.items():
        if count > 3:
            sev = "high" if count > 5 else "medium"
            findings.append({
                "category": "discussion_respun_too_many",
                "severity": sev,
                "title": f"Discussion #{disc_id} was respun {count} times — systemic issue",
                "evidence": [f"discussion#{disc_id}", f"spawns={count}"],
                "suggested_discussion_title": f"[Bug] Discussion #{disc_id} respun {count} times — investigate root cause",
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_hook_event_spam(hook_events: list[dict]) -> list[dict]:
    """Detect the same hook_event_id appearing 10+ times — script creating duplicate events.

    Severity: medium -- spam floods the event bus and can mask real events.
    """
    findings = []
    id_counts: dict[str, int] = defaultdict(int)

    for event in hook_events:
        event_id = (
            event.get("hook_event_id")
            or event.get("event_id")
            or event.get("id")
            or ""
        )
        if event_id:
            id_counts[event_id] += 1

    for event_id, count in id_counts.items():
        if count >= 10:
            findings.append({
                "category": "hook_event_spam",
                "severity": "medium",
                "title": f"hook_event_id '{event_id[:20]}...' appears {count} times — duplicate event creation",
                "evidence": [f"event_id={event_id}", f"count={count}"],
                "suggested_discussion_title": "[Bug] Hook event script creating duplicate events — spam in event bus",
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_transcript_repetition(
    feed_events: list[dict], loop_logs: list[dict]
) -> list[dict]:
    """Detect the same tool call appearing 5+ times in a single run — stuck-loop pattern.

    Severity: low (unless >20 repeats = high).
    """
    findings = []
    # Group events by run id
    run_texts: dict[str, list[str]] = defaultdict(list)
    for entry in feed_events + loop_logs:
        run_id = entry.get("run_id") or entry.get("session_id") or entry.get("id") or "global"
        text = _entry_text(entry)
        if text:
            run_texts[run_id].append(text)

    for run_id, texts in run_texts.items():
        combined = " ".join(texts)
        # Look for repeated Read/Bash tool calls on the same file/command
        for pattern in [r"Read\s+(/[^\s]+)", r"Bash\s*\(([^\)]{5,40})\)"]:
            matches = re.findall(pattern, combined)
            counter: dict[str, int] = defaultdict(int)
            for m in matches:
                counter[m] += 1
            for target, count in counter.items():
                if count >= 5:
                    sev = "high" if count >= 20 else "low"
                    findings.append({
                        "category": "transcript_repetition",
                        "severity": sev,
                        "title": f"Tool call '{target[:40]}' repeated {count}x in run '{run_id[:20]}'",
                        "evidence": [f"run={run_id[:20]}", f"target={target[:40]}", f"count={count}"],
                        "suggested_discussion_title": "[Bug] Agent stuck in tool-call loop — same call repeated N times",
                        "suggested_tag": "[Bug]",
                    })
    return findings


def classify_spec_impl_semantic_gap(
    feed_events: list[dict], audit: list[dict], needs_fix_prs: list[dict]
) -> list[dict]:
    """Detect PRs where diff size exceeds Spec ceiling or Spec says 'do not modify X' and diff touches X.

    Uses: text matching on feed events for spec line limits; gh pr view for actual diff size.
    Severity: medium.
    """
    findings = []

    # Pattern: spec says ≤N lines but diff is larger
    for pr_data in needs_fix_prs:
        pr_num = pr_data.get("number")
        if not pr_num:
            continue
        diff_data = get_pr_diff_size(pr_num)
        additions = diff_data.get("additions", 0)
        deletions = diff_data.get("deletions", 0)
        total_lines = additions + deletions
        # Check feed events for a spec size ceiling for this PR
        for entry in feed_events + audit:
            text = _entry_text(entry)
            pr_match = re.search(r"pr[:#\s]+#?(\d+)", text, re.IGNORECASE)
            if not pr_match or pr_match.group(1) != str(pr_num):
                continue
            size_match = re.search(r"[≤<=]{1,2}\s*(\d{2,4})\s*(lines|LOC)", text, re.IGNORECASE)
            if size_match:
                spec_max = int(size_match.group(1))
                if total_lines > spec_max * 1.2:  # 20% tolerance
                    findings.append({
                        "category": "spec_impl_semantic_gap",
                        "severity": "medium",
                        "title": f"PR #{pr_num} diff is {total_lines} lines but spec says ≤{spec_max}",
                        "evidence": [f"pr#{pr_num}", f"diff={total_lines}", f"spec_max={spec_max}"],
                        "suggested_discussion_title": f"[Bug] PR #{pr_num} exceeds spec size ceiling — scope drift",
                        "suggested_tag": "[Bug]",
                    })
                    break

    # Also check feed events for explicit "do not modify X" violations
    for entry in feed_events + audit:
        text = _entry_text(entry)
        if re.search(r"do not modify|must not touch|out of scope.*modified|modified.*out of scope", text, re.IGNORECASE):
            pr_match = re.search(r"pr[:#\s]+#?(\d+)", text, re.IGNORECASE)
            key = pr_match.group(1) if pr_match else "unknown"
            findings.append({
                "category": "spec_impl_semantic_gap",
                "severity": "medium",
                "title": f"PR #{key} modified a file marked 'do not modify' in spec",
                "evidence": [f"pr#{key}"],
                "suggested_discussion_title": f"[Bug] PR #{key} violated spec constraint — modified out-of-scope file",
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_branch_drift(feed_events: list[dict]) -> list[dict]:
    """Detect that parent repo is not on main during a /loop iteration.

    Checks actual current branch at analyst run time.
    Severity: high -- parent-repo branch drift corrupts spawned agents.
    """
    findings = []
    current_branch = get_current_branch()
    if current_branch and current_branch not in ("main", "master", ""):
        findings.append({
            "category": "branch_drift",
            "severity": "high",
            "title": f"Parent repo is on branch '{current_branch}' (not main) during analyst run",
            "evidence": [f"branch={current_branch}"],
            "suggested_discussion_title": "[Bug] Parent repo drifted off main — worktree contamination suspected",
            "suggested_tag": "[Bug]",
        })

    # Also surface historical drift events from feed
    for entry in feed_events:
        text = _entry_text(entry)
        if re.search(r"parent.*repo.*branch|branch.*not.*main|drifted.*off.*main|git.*checkout.*main", text, re.IGNORECASE):
            source = entry.get("_source") or entry.get("agent") or "unknown"
            findings.append({
                "category": "branch_drift",
                "severity": "medium",
                "title": f"Historical branch-drift event in {source}",
                "evidence": [source],
                "suggested_discussion_title": "[Bug] Branch-drift events in agent feed — investigate worktree hygiene",
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_stale_snapshot_consumption(
    loop_metrics: list[dict], feed_events: list[dict]
) -> list[dict]:
    """Detect a loop snapshot older than 600s (max_age_seconds).

    Severity: medium -- stale snapshot data leads to wrong routing decisions.

    MAX_AGE stays at 600s deliberately. Widening it would convert a visible
    false alarm into an invisible one; the fix for a permanently-firing finding
    is to make the refresh actually run (loop-snapshot-refresh.timer, every
    5 min), never to raise the bar until the alarm stops.
    """
    findings = []
    MAX_AGE = 600  # seconds

    for entry in loop_metrics + feed_events:
        text = _entry_text(entry)
        if re.search(r"SnapshotStale|snapshot.*stale|stale.*snapshot", text, re.IGNORECASE):
            source = entry.get("_source") or entry.get("agent") or "unknown"
            findings.append({
                "category": "stale_snapshot_consumption",
                "severity": "medium",
                "title": f"Stale loop-snapshot consumed in {source}",
                "evidence": [source, f"max_age={MAX_AGE}s"],
                "suggested_discussion_title": "[Bug] Loop-snapshot stale when read — subsystem snapshot needs refresh",
                "suggested_tag": "[Bug]",
            })

    # Also check the actual snapshot age
    snapshot = load_loop_snapshot()
    if snapshot:
        generated_at = snapshot.get("generated_at") or snapshot.get("timestamp") or ""
        ts = _parse_ts(generated_at)
        if ts:
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > MAX_AGE:
                findings.append({
                    "category": "stale_snapshot_consumption",
                    "severity": "medium",
                    "title": f"Current loop-snapshot is {int(age)}s old (>{MAX_AGE}s threshold)",
                    "evidence": [
                        f"age={int(age)}s",
                        f"generated_at={generated_at}",
                        f"path={SNAPSHOT_PATH}",
                        "check: systemctl --user status loop-snapshot-refresh.timer",
                    ],
                    "suggested_discussion_title": (
                        "[Small] loop-snapshot-refresh.timer has not run within its "
                        "5-minute interval — snapshot on disk is past MAX_AGE"
                    ),
                    "suggested_tag": "[Small]",
                })
    return findings


def classify_budget_cap_proximity(budget_data: dict) -> list[dict]:
    """Detect agents at ≥80% of per_agent_ceiling (500k tokens).

    Reads blackboard budget/ keys for per-agent spend.
    Severity: medium.
    """
    findings = []
    PER_AGENT_CEILING = 500_000
    WARN_THRESHOLD = 0.80

    for key, data in budget_data.items():
        if not isinstance(data, dict):
            continue
        spent = data.get("spent") or data.get("tokens_used") or data.get("total_tokens") or 0
        ceiling = data.get("ceiling") or data.get("per_agent_ceiling") or PER_AGENT_CEILING
        try:
            spent = int(spent)
            ceiling = int(ceiling)
        except (TypeError, ValueError):
            continue
        if ceiling <= 0:
            continue
        ratio = spent / ceiling
        if ratio >= WARN_THRESHOLD:
            role = data.get("role") or key
            findings.append({
                "category": "budget_cap_proximity",
                "severity": "medium",
                "title": f"Agent '{role}' at {ratio*100:.0f}% of token ceiling ({spent:,}/{ceiling:,})",
                "evidence": [f"role={role}", f"spent={spent}", f"ceiling={ceiling}", f"pct={ratio*100:.0f}%"],
                "suggested_discussion_title": "[Small] Agent approaching token budget ceiling — review token efficiency",
                "suggested_tag": "[Small]",
            })
    return findings


def classify_pre_spawn_check_missing(
    feed_events: list[dict], audit: list[dict]
) -> list[dict]:
    """Detect spawn events without a matching pre-spawn-check invocation.

    See Discussion #430. Severity: medium.
    """
    findings = []
    spawn_events: list[str] = []
    pre_spawn_checked: set[str] = set()

    for entry in feed_events + audit:
        text = _entry_text(entry)
        event_type = entry.get("event_type") or entry.get("type") or ""

        if re.search(r"SPAWN_REQUEST|spawn.*executor|spawn.*coordinator|spawn.*reviewer", text, re.IGNORECASE):
            disc_match = re.search(r"discussion[:#\s]+#?(\d+)", text, re.IGNORECASE)
            key = disc_match.group(1) if disc_match else "unknown"
            spawn_events.append(key)

        if re.search(r"pre.spawn.check|pre_spawn_check|scripts/pre-spawn-check", text, re.IGNORECASE):
            disc_match = re.search(r"discussion[:#\s]+#?(\d+)", text, re.IGNORECASE)
            key = disc_match.group(1) if disc_match else "unknown"
            pre_spawn_checked.add(key)

    missing = [k for k in spawn_events if k not in pre_spawn_checked and k != "unknown"]
    if missing:
        findings.append({
            "category": "pre_spawn_check_missing",
            "severity": "medium",
            "title": f"pre-spawn-check not run for {len(missing)} spawn(s): discussions {', '.join(list(dict.fromkeys(missing))[:5])}",
            "evidence": list(dict.fromkeys(missing))[:10],
            "suggested_discussion_title": "[Bug] Spawns missing pre-spawn-check call — budget/circuit-breaker bypass",
            "suggested_tag": "[Bug]",
        })
    return findings


# ---------------------------------------------------------------------------
# Phase A.2 transcript classifiers (Discussion #486)
# ---------------------------------------------------------------------------

_RE_WRONG_PREMISE = re.compile(
    r"(?i)(no such file|file not found|command not found|does not exist|"
    r"cannot find|not a valid|unrecognized|unexpected error|traceback)",
)
_RE_GENERAL_PURPOSE = re.compile(
    r"(?:subagent_type|subagent-type)[\"'\s:=]*general.?purpose",
    re.IGNORECASE,
)
_RE_AUTH_LEAK = re.compile(
    r"(?:"
    r"\bcurl\s+(?:[^\n]*\s)?-[a-zA-Z]*v\b"
    r"|\bcurl\s+(?:[^\n]*\s)?--verbose\b"
    r"|\bset\s+-x\b"
    r"|\bgh\s+api\b[^\n]*--verbose"
    r"|\bvastai\b[^\n]*(?:--explain|--curl)"
    r")",
    re.IGNORECASE,
)
_RE_PERMISSION_SEEKING = re.compile(
    r"\b(?:should\s+i|do\s+you\s+want\s+me\s+to|let\s+me\s+know\s+if|"
    r"shall\s+i|would\s+you\s+like\s+me\s+to)\b",
    re.IGNORECASE,
)
_TEAM_LEAD_KEYWORDS = re.compile(
    r"(?:you are the team lead|team lead operating protocol|"
    r"identity.*team lead|team-lead.*coordinator|"
    r"single.spawner invariant)",
    re.IGNORECASE,
)
_EDIT_WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


def _scan_transcripts(since_seconds: int = 7 * 24 * 3600) -> list[dict]:
    """Single-pass scan of transcript JSONL files; returns per-agent state dicts."""
    try:
        from transcript_reader import iter_transcripts, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_transcripts, agent_id_from_path
        except ImportError:
            return []

    results = []
    for path, turns_iter in iter_transcripts(since_seconds=since_seconds):
        agent_id = agent_id_from_path(path)
        state: dict = {
            "path": str(path),
            "agent_id": agent_id,
            "turns": 0,
            "is_team_lead": False,
            "has_edit_write": False,
            "auth_leak_cmds": [],
            "permission_phrases": [],
            "repeated_reads": Counter(),
            "read_turns": defaultdict(list),
            "write_paths": set(),
            "general_purpose_hits": [],
            "wrong_premise_err_turns": [],
            "prev_user_text": "",
        }

        for turn in turns_iter:
            state["turns"] += 1

            if turn.turn_idx < 3 and turn.role in ("user", "system"):
                if _TEAM_LEAD_KEYWORDS.search(turn.text):
                    state["is_team_lead"] = True

            for tc in turn.tool_calls:
                name = tc.get("name", "")
                inp = tc.get("input", {})

                if name in _EDIT_WRITE_TOOLS:
                    state["has_edit_write"] = True
                    fp = inp.get("file_path", inp.get("path", ""))
                    if fp:
                        state["write_paths"].add(fp)

                if name == "Read":
                    fp = inp.get("file_path", inp.get("path", ""))
                    if fp:
                        state["repeated_reads"][fp] += 1
                        state["read_turns"][fp].append(turn.turn_idx)

                if name == "Bash":
                    cmd = inp.get("command", "")
                    if _RE_AUTH_LEAK.search(cmd):
                        state["auth_leak_cmds"].append(cmd[:120])

                if name in ("Bash", "Agent"):
                    target = inp.get("command", "") or inp.get("prompt", "") or json.dumps(inp)
                    if _RE_GENERAL_PURPOSE.search(target):
                        state["general_purpose_hits"].append(target[:100])

            for tr in turn.tool_results:
                content = tr.get("content", "")
                is_error = tr.get("is_error", False)
                if is_error or _RE_WRONG_PREMISE.search(str(content)):
                    tool_use_id = tr.get("tool_use_id", "")
                    matched_name, matched_key = "unknown", ""
                    for tc in turn.tool_calls:
                        if tc.get("id") == tool_use_id or not tool_use_id:
                            matched_name = tc.get("name", "")
                            inp2 = tc.get("input", {})
                            if matched_name == "Bash":
                                matched_key = inp2.get("command", "")[:60]
                            elif matched_name == "Read":
                                matched_key = inp2.get("file_path", "")[:60]
                            else:
                                matched_key = json.dumps(inp2)[:60]
                            break
                    state["wrong_premise_err_turns"].append(
                        (turn.turn_idx, matched_name, matched_key)
                    )

            if turn.role == "assistant" and _RE_PERMISSION_SEEKING.search(turn.text):
                if not state.get("prev_user_text", "").rstrip().endswith("?"):
                    state["permission_phrases"].append((turn.turn_idx, turn.text[:100]))

            if turn.role == "user":
                state["prev_user_text"] = turn.text

        results.append(state)
    return results


def classify_wrong_premise_retries(transcript_states: list[dict]) -> list[dict]:
    """Detect agents retrying the same failing tool call 5+ times. Severity: high."""
    findings = []
    for state in transcript_states:
        err_groups: dict[tuple, list[int]] = defaultdict(list)
        for turn_idx, tool_name, norm_key in state.get("wrong_premise_err_turns", []):
            err_groups[(tool_name, norm_key)].append(turn_idx)
        for (tool_name, norm_key), turn_indices in err_groups.items():
            if len(turn_indices) >= 5:
                span = turn_indices[-1] - turn_indices[0] if len(turn_indices) > 1 else 0
                if span >= 2:
                    findings.append({
                        "category": "wrong_premise_retries",
                        "severity": "high",
                        "title": (
                            f"Agent {state['agent_id'][:20]} retried failing tool "
                            f"'{tool_name}' {len(turn_indices)}x "
                            f"(turns {turn_indices[0]}-{turn_indices[-1]})"
                        ),
                        "evidence": [
                            f"agent={state['agent_id'][:30]}",
                            f"tool={tool_name}",
                            f"key={norm_key[:50]}",
                            f"error_count={len(turn_indices)}",
                        ],
                        "suggested_discussion_title": (
                            "[Bug] Agent stuck on wrong-premise retry loop"
                        ),
                        "suggested_tag": "[Bug]",
                    })
    return findings


def classify_forbidden_subagent_type(transcript_states: list[dict]) -> list[dict]:
    """Detect use of forbidden subagent_type=general-purpose. Severity: high."""
    findings = []
    for state in transcript_states:
        hits = state.get("general_purpose_hits", [])
        if hits:
            findings.append({
                "category": "forbidden_subagent_type",
                "severity": "high",
                "title": (
                    f"Agent {state['agent_id'][:20]} used forbidden "
                    f"subagent_type=general-purpose ({len(hits)} occurrence(s))"
                ),
                "evidence": [f"agent={state['agent_id'][:30]}"] + [h[:80] for h in hits[:3]],
                "suggested_discussion_title": (
                    "[Bug] Agent used general-purpose subagent_type — hard-rule violation"
                ),
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_team_lead_self_edit(transcript_states: list[dict]) -> list[dict]:
    """Detect Team Lead transcripts with Edit/Write on project files. Severity: high."""
    findings = []
    for state in transcript_states:
        if not state.get("is_team_lead") or not state.get("has_edit_write"):
            continue
        # The absolute-path arm of this filter is anchored to the MAIN checkout,
        # not to REPO_ROOT. The Team Lead runs in the main checkout, so its
        # .autonomous-team/ writes are absolute paths under that root — but
        # REPO_ROOT is whichever tree run_analyst itself was launched from, and
        # when that is a worktree the two differ and the arm silently stops
        # matching. Every Team Lead .autonomous-team/ write then reads as a
        # project write and gets filed as a hard-rule violation.
        #
        # Unlike the sibling defect in _is_main_repo_write, this one
        # over-flags rather than under-flags, so it fails loudly — which is
        # presumably why it survived: someone seeing the noise would blame the
        # Team Lead, not the filter. Nothing pinned it until D#1997; see
        # backend/tests/test_team_lead_self_edit_root.py.
        #
        # _MAIN_REPO_ROOT_PATH is defined lower in this module; that is fine
        # here because the name resolves when the function runs, not when it is
        # defined, and it keeps one resolved value shared with the
        # worktree-contamination classifier rather than a second derivation.
        _tl_state_prefix = str(_MAIN_REPO_ROOT_PATH / ".autonomous-team") + "/"
        project_writes = [
            p for p in state.get("write_paths", set())
            if not p.startswith(".autonomous-team/")
            and not p.startswith(_tl_state_prefix)
        ]
        if project_writes:
            findings.append({
                "category": "team_lead_self_edit",
                "severity": "high",
                "title": (
                    f"Team Lead transcript {state['agent_id'][:20]} "
                    "contains Edit/Write on project files"
                ),
                "evidence": (
                    [f"agent={state['agent_id'][:30]}"]
                    + [f"file={p[:60]}" for p in list(project_writes)[:5]]
                ),
                "suggested_discussion_title": (
                    "[Bug] Team Lead writing code directly — hard-rule violation"
                ),
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_auth_leak_risk(transcript_states: list[dict]) -> list[dict]:
    """Detect Bash commands risking auth header leaks. Severity: high."""
    findings = []
    for state in transcript_states:
        cmds = state.get("auth_leak_cmds", [])
        if cmds:
            findings.append({
                "category": "auth_leak_risk",
                "severity": "high",
                "title": (
                    f"Agent {state['agent_id'][:20]} ran {len(cmds)} "
                    "command(s) that may leak auth headers"
                ),
                "evidence": (
                    [f"agent={state['agent_id'][:30]}"]
                    + [f"cmd={c[:80]}" for c in cmds[:3]]
                ),
                "suggested_discussion_title": (
                    "[Bug] Agent used curl -v / set -x — auth header leak risk"
                ),
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_permission_seeking(transcript_states: list[dict]) -> list[dict]:
    """Detect assistant turns asking for permission instead of acting. Severity: medium."""
    findings = []
    for state in transcript_states:
        phrases = state.get("permission_phrases", [])
        if phrases:
            findings.append({
                "category": "permission_seeking",
                "severity": "medium",
                "title": (
                    f"Agent {state['agent_id'][:20]} asked for permission "
                    f"instead of acting ({len(phrases)} instance(s))"
                ),
                "evidence": (
                    [f"agent={state['agent_id'][:30]}"]
                    + [f"turn={t}: {s[:60]}" for t, s in phrases[:3]]
                ),
                "suggested_discussion_title": (
                    "[Bug] Agent asking permission instead of acting — "
                    "violates dont_ask_just_act rule"
                ),
                "suggested_tag": "[Bug]",
            })
    return findings


def classify_repeated_file_reads(transcript_states: list[dict]) -> list[dict]:
    """Detect agents reading the same file >3 times without editing it. Severity: medium."""
    findings = []
    for state in transcript_states:
        agent_id = state["agent_id"]
        for file_path, count in state.get("repeated_reads", {}).items():
            if count <= 3:
                continue
            if file_path in state.get("write_paths", set()):
                continue
            findings.append({
                "category": "repeated_file_reads",
                "severity": "medium",
                "title": (
                    f"Agent {agent_id[:20]} read '{file_path[:40]}' "
                    f"{count}x without editing it"
                ),
                "evidence": [
                    f"agent={agent_id[:30]}",
                    f"file={file_path[:60]}",
                    f"read_count={count}",
                ],
                "suggested_discussion_title": (
                    "[Bug] Agent re-reading same file 4+ times — "
                    "context-drop pattern detected"
                ),
                "suggested_tag": "[Bug]",
            })
    return findings


# ---------------------------------------------------------------------------
# Phase A.3 transcript classifiers (Discussion #511)
# ---------------------------------------------------------------------------
# These classifiers consume TranscriptTurn iterables directly (not state dicts).
# Each returns list[Finding]; the dispatcher converts to dict for the main report.
# ---------------------------------------------------------------------------

class Finding(NamedTuple):
    """Lightweight result from a Phase A.3 per-turn classifier."""
    classifier: str
    severity: str   # "high" | "medium" | "low"
    turn_index: int
    detail: str


# -- 1. git rm usage ----------------------------------------------------------

_GIT_RM_PAT = re.compile(r"\bgit\s+rm\b", re.IGNORECASE)
_ARCHIVE_SAFE = re.compile(r"\bargit\s+rm\s+(?:-r\s+)?archive/", re.IGNORECASE)


def classify_git_rm_usage(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Hard-rule violation: git rm is never allowed on project files (CLAUDE.md)."""
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "")
            if _GIT_RM_PAT.search(cmd) and not _ARCHIVE_SAFE.search(cmd):
                findings.append(Finding(
                    classifier="git_rm_usage",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=f"git rm in Bash command: {cmd[:120]}",
                ))
    return findings


# -- 2. preflight skipped -----------------------------------------------------

_PREFLIGHT_PAT = re.compile(r"scripts/preflight\.sh", re.IGNORECASE)
_PR_CREATE_PAT = re.compile(r"gh\s+pr\s+create", re.IGNORECASE)


def classify_preflight_skipped(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Executor reached gh pr create without a scripts/preflight.sh call. Severity: high."""
    findings: list[Finding] = []
    saw_preflight = False
    pr_create_turn: int | None = None
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "")
            if _PREFLIGHT_PAT.search(cmd):
                saw_preflight = True
            if _PR_CREATE_PAT.search(cmd) and pr_create_turn is None:
                pr_create_turn = t.turn_idx
    if pr_create_turn is not None and not saw_preflight:
        findings.append(Finding(
            classifier="preflight_skipped",
            severity="high",
            turn_index=pr_create_turn,
            detail="gh pr create reached without scripts/preflight.sh call",
        ))
    return findings


# -- 3. reviewer skipped by impl-coord ----------------------------------------

_CODE_REVIEWER_PAT = re.compile(
    r"code.reviewer|roles.*code.reviewer|subagent_type.*code.reviewer",
    re.IGNORECASE,
)
_AGENT_DONE_PAT = re.compile(
    r'"verdict"\s*:\s*"done"',
    re.IGNORECASE,
)
_IMPL_COORD_KEYWORDS = re.compile(
    r"impl.coord|implementation.coordinator|you are.*impl",
    re.IGNORECASE,
)


def classify_reviewer_skipped_by_impl_coord(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Retired classifier — role no longer active. Always returns empty."""
    return []


# -- 4. sensitive file unlabeled ----------------------------------------------

_SENSITIVE_FILE_PAT = re.compile(
    r"(?:\.env|manifest.*\.json|auth[/._]|secret[/._]|token[/._]|credential[/._])",
    re.IGNORECASE,
)
_SEC_LABEL_PAT = re.compile(
    r"security.review.triggered|apply_label.*security",
    re.IGNORECASE,
)
_EDIT_WRITE_NAMES = frozenset({"Edit", "Write", "NotebookEdit"})


def classify_sensitive_file_unlabeled(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Edit/Write on sensitive file without security-review-triggered label. Severity: medium."""
    findings: list[Finding] = []
    saw_security_label = False
    sensitive_hits: list[tuple[int, str]] = []
    for t in turns:
        for tc in t.tool_calls:
            name = tc.get("name", "")
            inp = tc.get("input", {})
            if name in _EDIT_WRITE_NAMES:
                fp = inp.get("file_path", inp.get("path", ""))
                if fp and _SENSITIVE_FILE_PAT.search(fp):
                    sensitive_hits.append((t.turn_idx, fp))
            if name == "Bash":
                cmd = inp.get("command", "")
                if _SEC_LABEL_PAT.search(cmd):
                    saw_security_label = True
    if sensitive_hits and not saw_security_label:
        for turn_idx, fp in sensitive_hits[:3]:
            findings.append(Finding(
                classifier="sensitive_file_unlabeled",
                severity="medium",
                turn_index=turn_idx,
                detail=f"Edit/Write of sensitive file without security label: {fp[:80]}",
            ))
    return findings


# -- 5. tool output ignored ---------------------------------------------------

# Must have is_error:true AND contain a real error keyword to qualify as a failure.
# This prevents false positives from structured JSON responses, expected non-zero exits,
# and tool results that contain incidental matches (e.g. "not found" in filenames).
_TOOL_REAL_ERROR_PAT = re.compile(
    r"\bError\b|\bTraceback\b|exit 1\b|exit code [1-9]|\bfailed\b|\bnot found\b",
    re.IGNORECASE,
)
_ACKNOWLEDGEMENT_PAT = re.compile(
    r"error|not found|failed|doesn't exist|does not exist|missing|no such",
    re.IGNORECASE,
)
# Tool names that indicate the assistant is pivoting to a new action rather than ignoring.
_PIVOT_TOOLS = frozenset({"Bash", "Edit", "Read", "Write", "MultiEdit", "Grep", "Glob"})


def classify_tool_output_ignored(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """tool_result is_error:true with real error keyword, followed by assistant ignoring it.

    Three conditions ALL required to fire:
    1. tool_result has is_error:true AND result text contains a real error keyword
       (Error, Traceback, exit 1, failed, not found) — excludes structured JSON responses
       and expected non-zero exits without meaningful error text.
    2. Subsequent assistant message does NOT reference the error text (no acknowledgement).
    3. Subsequent assistant message does NOT immediately call a new tool — any Bash/Edit/Read
       call is a course-correction pivot, not ignorance.

    Severity: high.
    """
    findings: list[Finding] = []
    prev_failed = False
    prev_fail_turn = -1
    for t in turns:
        if t.role == "user" and t.tool_results:
            for tr in t.tool_results:
                # Condition 1: must have is_error:true AND a real error keyword
                if tr.get("is_error") and _TOOL_REAL_ERROR_PAT.search(tr.get("content", "")):
                    prev_failed = True
                    prev_fail_turn = t.turn_idx
        elif t.role == "assistant" and prev_failed:
            acknowledged = _ACKNOWLEDGEMENT_PAT.search(t.text)
            # Condition 3: any immediate tool call is a pivot — not ignorance
            pivoted = bool(t.tool_calls and any(
                tc.get("name", "") in _PIVOT_TOOLS for tc in t.tool_calls
            ))
            if not acknowledged and not pivoted:
                findings.append(Finding(
                    classifier="tool_output_ignored",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=(
                        f"Assistant ignored tool failure at turn {prev_fail_turn}: "
                        f"{t.text[:80]}"
                    ),
                ))
            prev_failed = False
    return findings


# -- 6. lied exit code --------------------------------------------------------

_POSITIVE_COMPLETION = re.compile(
    r"all tests pass|tests pass(?:ing)?|tests are (?:passing|green)|"
    r"successful(?:ly)?|works correctly|completed successfully|everything (?:looks|is) (?:good|fine|working)",
    re.IGNORECASE,
)
_NONZERO_EXIT = re.compile(r"exit code [1-9]\d*|exited with [1-9]|returned [1-9]\d*\b", re.IGNORECASE)


def classify_lied_exit_code(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Bash returned non-zero but next assistant message claims success. Severity: high."""
    findings: list[Finding] = []
    prev_nonzero = False
    prev_exit_turn = -1
    for t in turns:
        if t.role == "user" and t.tool_results:
            for tr in t.tool_results:
                content = tr.get("content", "")
                if tr.get("is_error") or _NONZERO_EXIT.search(content):
                    prev_nonzero = True
                    prev_exit_turn = t.turn_idx
        elif t.role == "assistant" and prev_nonzero:
            if _POSITIVE_COMPLETION.search(t.text):
                findings.append(Finding(
                    classifier="lied_exit_code",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=(
                        f"Non-zero exit at turn {prev_exit_turn} but agent claims: "
                        f"{t.text[:80]}"
                    ),
                ))
            prev_nonzero = False
    return findings


# -- 7. claim / transcript mismatch -------------------------------------------

_CLAIM_PAT = re.compile(
    r"(?:I (?:added|created|wrote|edited|implemented|updated|fixed|modified))\s+[`'\"]?([^\s`'\",.]+[^\s`'\",.])",
    re.IGNORECASE,
)


def classify_claim_transcript_mismatch(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Agent summary claims file added/written but no Edit/Write for it. Severity: high."""
    findings: list[Finding] = []
    written: set[str] = set()
    claims: list[tuple[int, str]] = []
    for t in turns:
        if t.role != "assistant":
            continue
        for tc in t.tool_calls:
            if tc.get("name") in _EDIT_WRITE_NAMES:
                fp = tc.get("input", {}).get("file_path", tc.get("input", {}).get("path", ""))
                if fp:
                    written.update([fp, Path(fp).name, Path(fp).stem])
        for m in _CLAIM_PAT.finditer(t.text):
            c = m.group(1).strip("`'\"")
            if len(c) > 5 and ("/" in c or "_" in c):
                claims.append((t.turn_idx, c))
    reported: set[str] = set()
    for turn_idx, claimed in claims:
        stems = {claimed, Path(claimed).name, Path(claimed).stem}
        if not stems & written and claimed not in reported:
            reported.add(claimed)
            findings.append(Finding(
                classifier="claim_transcript_mismatch", severity="high",
                turn_index=turn_idx,
                detail=f"Claimed '{claimed}' but no Edit/Write for it in transcript",
            ))
    return findings


# -- 8. bash retry cosmetic variants ------------------------------------------

_CMD_BASE_PAT = re.compile(r"^(?:sudo\s+)?(\S+)", re.IGNORECASE)
_FLAG_STRIP_PAT = re.compile(r"\s+-{1,2}[a-zA-Z][a-zA-Z0-9-]*(?:\s+\S+)?", re.IGNORECASE)


def classify_bash_retry_cosmetic_variants(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Same base command 3+ times with only flag changes — stuck retry. Severity: medium."""
    findings: list[Finding] = []
    cmd_tracker: dict[str, list[int]] = defaultdict(list)
    edit_turns: set[int] = set()
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") in _EDIT_WRITE_NAMES:
                edit_turns.add(t.turn_idx)
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "").strip()
            m = _CMD_BASE_PAT.match(cmd)
            if m:
                cmd_tracker[m.group(1)].append(t.turn_idx)
    for base, tl in cmd_tracker.items():
        if len(tl) >= 3 and (tl[-1] - tl[0]) >= 2:
            if not any(tl[0] < e < tl[-1] for e in edit_turns):
                findings.append(Finding(
                    classifier="bash_retry_cosmetic_variants", severity="medium",
                    turn_index=tl[0],
                    detail=f"Command '{base}' called {len(tl)}x (turns {tl[0]}-{tl[-1]}) with no intervening edits",
                ))
    return findings


# -- Dispatcher: run all Phase A.3 classifiers per transcript -----------------

try:
    from backend.classifiers.spawn_injection_audit import classify_spawn_injection  # noqa: E402
except ImportError:
    from classifiers.spawn_injection_audit import classify_spawn_injection  # noqa: E402

_PHASE_A3_CLASSIFIERS = [
    classify_git_rm_usage,
    classify_preflight_skipped,
    classify_reviewer_skipped_by_impl_coord,
    classify_sensitive_file_unlabeled,
    classify_tool_output_ignored,
    classify_lied_exit_code,
    classify_claim_transcript_mismatch,
    classify_bash_retry_cosmetic_variants,
    classify_spawn_injection,
]


def _run_phase_a3_classifiers(since_seconds: int = 7 * 24 * 3600) -> list[dict]:
    """Run Phase A.3 classifiers over all recent transcripts; return list of finding dicts."""
    try:
        from transcript_reader import iter_transcripts, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_transcripts, agent_id_from_path
        except ImportError:
            return []

    all_findings: list[dict] = []
    for path, turns_iter in iter_transcripts(since_seconds=since_seconds):
        agent_id = agent_id_from_path(path)
        turns_list = list(turns_iter)  # buffer once; A.3 classifiers may need full list
        for classifier_fn in _PHASE_A3_CLASSIFIERS:
            for finding in classifier_fn(iter(turns_list)):
                all_findings.append({
                    "category": finding.classifier,
                    "severity": finding.severity,
                    "title": (
                        f"[A3] {finding.classifier}: agent {agent_id[:20]} "
                        f"(turn {finding.turn_index})"
                    ),
                    "evidence": [
                        f"agent={agent_id[:40]}",
                        f"turn={finding.turn_index}",
                        f"detail={finding.detail[:120]}",
                    ],
                    "suggested_discussion_title": (
                        f"[Bug] transcript classifier {finding.classifier} fired"
                    ),
                    "suggested_tag": "[Bug]",
                })
    return all_findings


# ---------------------------------------------------------------------------
# Phase A.4 transcript classifiers (Discussion #523)
# ---------------------------------------------------------------------------
# 4 security, 1 hallucination, 2 discipline, 1 operability.
# Same Finding namedtuple + dispatcher pattern as Phase A.3.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared helpers for Phase A.4 security classifiers (Discussion #542)
#
# All 4 security classifiers are scoped to "real edits" only:
#   1. Only fire on Edit/Write tool calls (new_string/content), not Read results
#      or Bash heredocs.
#   2. Skip paths that are classifier internals, fixtures, docs, or test files.
#   3. Do not fire when the matched text lives inside a fenced code block or
#      shell HEREDOC body (i.e. inside backtick fences or <<EOF … EOF).
# ---------------------------------------------------------------------------

# Paths excluded from all A.4 security classifiers — matches trailing segment
_SEC_EXCLUDED_PATH_PAT = re.compile(
    r"(?:^|/)(?:run_analyst\.py"
    r"|[^/]*\.md"
    r"|[^/]*\.tmpl"
    r"|test_[^/]*\.py"
    r"|[^/]*_test\.py"
    r")|(?:^|/)tests/fixtures/",
    re.IGNORECASE,
)

# Fenced code block (``` ... ```) or shell HEREDOC (<<WORD\n…\nWORD) markers.
# Used to strip those regions before pattern-matching.
_FENCED_BLOCK_PAT = re.compile(
    r"```.*?```"                      # triple-backtick fenced block
    r"|<<\s*'?(\w+)'?\n.*?\n\1",      # shell HEREDOC  <<EOF\n…\nEOF
    re.DOTALL,
)


def _strip_meta_regions(text: str) -> str:
    """Remove fenced code blocks and HEREDOC bodies from text before matching."""
    return _FENCED_BLOCK_PAT.sub("", text)


def _is_excluded_path(fp: str) -> bool:
    """Return True when the file path is in the classifier-meta / test exclusion list."""
    return bool(_SEC_EXCLUDED_PATH_PAT.search(fp))


# -- 1. token_in_team_log (HIGH SEC) -----------------------------------------
# Scoped (D#542): only fires when the agent writes a Bash command that (a) calls
# the team-log API directly AND (b) embeds a token literal in that same command.
# Self-referential matches (classifier code, Discussion bodies, commit messages)
# are excluded because those surfaces are not Bash tool calls with team-log commands.

_TEAM_LOG_CMD_PAT = re.compile(
    r"gh\s+issue\s+comment.*--body|rotate.team.log\.sh\s+comment",
    re.IGNORECASE,
)
_TOKEN_VALUE_PAT = re.compile(
    r"(?:rpcToken|bearer|api.?key|Authorization)[=:\s]['\"]?[A-Za-z0-9_\-\.]{8,}",
    re.IGNORECASE,
)


def classify_token_in_team_log(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Bearer/token value posted to team-log Issue body. Severity: high (SEC).

    Scoped (D#542): only the Bash command string is checked, not read results,
    assistant prose, or heredoc bodies.  The command must match the team-log
    invocation pattern AND contain a token literal outside any HEREDOC region.
    """
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "")
            if not _TEAM_LOG_CMD_PAT.search(cmd):
                continue
            # Strip HEREDOC / fenced regions before checking for the token value
            stripped = _strip_meta_regions(cmd)
            if _TOKEN_VALUE_PAT.search(stripped):
                findings.append(Finding(
                    classifier="token_in_team_log",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=f"Token/bearer value in team-log comment: {cmd[:120]}",
                ))
    return findings


# -- 2. curl_insecure_or_k (HIGH SEC) ----------------------------------------
# Scoped (D#542): only fires on Write/Edit new_string — NOT on bare Bash curl
# commands, since those are execution, not code being authored into a file.
# This prevents false positives from classifier descriptions, fixtures, and
# CI/test scripts that merely invoke curl for connectivity checks.

_CURL_INSECURE_PAT = re.compile(r"\bcurl\b.*(?:\s-[a-zA-Z]*k\b|\s--insecure\b)", re.IGNORECASE)


def classify_curl_insecure_or_k(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """curl -k or --insecure written into a production file via Edit/Write. Severity: high (SEC).

    Scoped (D#542): match must be in new_string/content of Edit or Write tool call,
    targeting a non-excluded file path, outside fenced code blocks.
    """
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") not in {"Edit", "Write"}:
                continue
            inp = tc.get("input", {})
            fp = inp.get("file_path", inp.get("path", ""))
            if _is_excluded_path(fp):
                continue
            content = inp.get("new_string", inp.get("content", ""))
            stripped = _strip_meta_regions(content)
            if _CURL_INSECURE_PAT.search(stripped):
                findings.append(Finding(
                    classifier="curl_insecure_or_k",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=f"Insecure curl in {fp[:60]}: {content[:120]}",
                ))
    return findings


# -- 3. python_verify_false (HIGH SEC) ----------------------------------------
# Scoped (D#542): only fires on Edit/Write new_string targeting a non-excluded
# production file.  Previously matched Bash content (command=) which caused 6/6
# false positives from classifier code, Discussion bodies, commit messages, and
# gh pr create output.

_VERIFY_FALSE_PAT = re.compile(
    r"verify\s*=\s*False|ssl\._create_unverified_context",
    re.IGNORECASE,
)


def classify_python_verify_false(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """requests verify=False or ssl._create_unverified_context written into a file. Severity: high (SEC).

    Scoped (D#542): match must be in new_string/content of Edit or Write tool call,
    targeting a non-excluded file path, outside fenced code blocks.
    """
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") not in {"Edit", "Write"}:
                continue
            inp = tc.get("input", {})
            fp = inp.get("file_path", inp.get("path", ""))
            if _is_excluded_path(fp):
                continue
            content = inp.get("new_string", inp.get("content", ""))
            stripped = _strip_meta_regions(content)
            if _VERIFY_FALSE_PAT.search(stripped):
                findings.append(Finding(
                    classifier="python_verify_false",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=f"SSL verify disabled in {fp[:60]}: {content[:120]}",
                ))
    return findings


# -- 4. localstorage_token (HIGH SEC) -----------------------------------------

_LS_TOKEN_PAT = re.compile(
    r"localStorage\.setItem\s*\(\s*['\"](?:token|rpcToken|api.?key|authToken)['\"]",
    re.IGNORECASE,
)
_CODE_FILE_PAT = re.compile(r"\.(py|ts|tsx|js|jsx|sh|md)$", re.IGNORECASE)
_PERSONA_PATH_PAT = re.compile(r"personas/", re.IGNORECASE)


def classify_localstorage_token(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """localStorage.setItem('token'|'rpcToken', ...) in Write/Edit of code file. Severity: high (SEC).

    Scoped (D#542): match must be in new_string/content of Edit or Write tool call,
    targeting a production code file (not excluded paths), outside fenced code blocks.
    """
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") not in {"Edit", "Write"}:
                continue
            inp = tc.get("input", {})
            fp = inp.get("file_path", inp.get("path", ""))
            # Original code-file filter: must be a recognised code extension, not personas/
            if fp and (not _CODE_FILE_PAT.search(fp) or _PERSONA_PATH_PAT.search(fp)):
                continue
            # Additional D#542 exclusion: run_analyst.py, fixtures, docs, test files
            if _is_excluded_path(fp):
                continue
            content = inp.get("new_string", inp.get("content", ""))
            stripped = _strip_meta_regions(content)
            if _LS_TOKEN_PAT.search(stripped):
                findings.append(Finding(
                    classifier="localstorage_token",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=f"localStorage.setItem with token key in {fp[:60]}: {content[:80]}",
                ))
    return findings


# -- 5. nonexistent_pr_or_disc (MEDIUM) ---------------------------------------

_HASH_REF_PAT = re.compile(r"#(\d{1,5})\b")
# 60-second TTL cache; keys are (kind, number) → bool (exists)
_EXISTENCE_CACHE: dict[tuple, tuple[float, bool]] = {}
_EXISTENCE_TTL = 60.0


def _gh_ref_exists(kind: str, number: int) -> bool:
    """Check GitHub PR or Discussion existence with 60s TTL cache. No graphql in hot loop."""
    key = (kind, number)
    now = time.monotonic()
    if key in _EXISTENCE_CACHE:
        ts, result = _EXISTENCE_CACHE[key]
        if now - ts < _EXISTENCE_TTL:
            return result
    exists = False
    try:
        if kind == "pr":
            r = subprocess.run(
                ["gh", "pr", "view", str(number), "--repo", REPO,
                 "--json", "number"],
                capture_output=True, text=True, timeout=10,
            )
            exists = r.returncode == 0
        else:  # disc
            r = subprocess.run(
                ["gh", "api", "graphql", "-f",
                 f'query=query{{repository(owner:"{REPO_OWNER}",name:"{REPO_NAME}")'
                 f'{{discussion(number:{number}){{number}}}}}}'],
                capture_output=True, text=True, timeout=10,
            )
            exists = r.returncode == 0 and '"number"' in r.stdout
    except (subprocess.TimeoutExpired, OSError):
        exists = True  # assume exists on error to avoid false positives
    _EXISTENCE_CACHE[key] = (now, exists)
    return exists


def classify_nonexistent_pr_or_disc(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Agent text mentions #NNN where neither PR nor Discussion NNN exists. Severity: medium."""
    findings: list[Finding] = []
    reported: set[int] = set()
    for t in turns:
        if t.role != "assistant":
            continue
        for m in _HASH_REF_PAT.finditer(t.text):
            n = int(m.group(1))
            if n in reported or n < 1 or n > 9999:
                continue
            if not _gh_ref_exists("pr", n) and not _gh_ref_exists("disc", n):
                reported.add(n)
                findings.append(Finding(
                    classifier="nonexistent_pr_or_disc",
                    severity="medium",
                    turn_index=t.turn_idx,
                    detail=f"Reference #{n} not found as PR or Discussion",
                ))
    return findings


# -- 6. emoji_in_code_or_commit (MEDIUM) --------------------------------------

_EMOJI_PAT = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    r"\U00002702-\U000027B0\U0000FE00-\U0000FE0F]",
)
_CODE_EXT_PAT = re.compile(r"\.(py|ts|tsx|sh|md)$", re.IGNORECASE)
_PERSONAS_PAT = re.compile(r"personas/", re.IGNORECASE)
_GIT_COMMIT_PAT = re.compile(r'git\s+commit\b.*-m\s+["\']', re.IGNORECASE)


def classify_emoji_in_code_or_commit(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Emoji codepoints in Write/Edit of code files or in git commit message. Severity: medium."""
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            name = tc.get("name", "")
            inp = tc.get("input", {})
            if name in {"Edit", "Write"}:
                fp = inp.get("file_path", inp.get("path", ""))
                if not fp or not _CODE_EXT_PAT.search(fp) or _PERSONAS_PAT.search(fp):
                    continue
                content = inp.get("new_string", inp.get("content", ""))
                if _EMOJI_PAT.search(content):
                    findings.append(Finding(
                        classifier="emoji_in_code_or_commit",
                        severity="medium",
                        turn_index=t.turn_idx,
                        detail=f"Emoji in code file {fp[:60]}",
                    ))
            elif name == "Bash":
                cmd = inp.get("command", "")
                if _GIT_COMMIT_PAT.search(cmd) and _EMOJI_PAT.search(cmd):
                    findings.append(Finding(
                        classifier="emoji_in_code_or_commit",
                        severity="medium",
                        turn_index=t.turn_idx,
                        detail=f"Emoji in git commit: {cmd[:120]}",
                    ))
    return findings


# -- 7. trailing_summary (LOW) -------------------------------------------------

_TRAILING_SUMMARY_PAT = re.compile(
    r"(?:^|\n)##\s+(?:Summary|What I [Dd]id|Changes [Mm]ade)|[Hh]ere'?s a summary",
    re.MULTILINE,
)
_USER_ASKED_SUMMARY_PAT = re.compile(
    r"(?:summarize|summary|what did you do|what changes)",
    re.IGNORECASE,
)


def classify_trailing_summary(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Final assistant message contains unsolicited ## Summary / Here's a summary. Severity: low."""
    findings: list[Finding] = []
    turns_list = list(turns)
    user_asked = any(
        t.role == "user" and _USER_ASKED_SUMMARY_PAT.search(t.text)
        for t in turns_list
    )
    if user_asked:
        return findings
    # Check last few assistant messages
    assistant_turns = [t for t in turns_list if t.role == "assistant"]
    if not assistant_turns:
        return findings
    last = assistant_turns[-1]
    if _TRAILING_SUMMARY_PAT.search(last.text):
        findings.append(Finding(
            classifier="trailing_summary",
            severity="low",
            turn_index=last.turn_idx,
            detail=f"Unsolicited summary section in final message: {last.text[:80]}",
        ))
    return findings


# -- 8. no_status_when_blocked (MEDIUM) ----------------------------------------

_TEAM_LOG_CALL_PAT = re.compile(r"rotate.team.log\.sh\s+comment", re.IGNORECASE)
_WALL_CLOCK_THRESHOLD = 120  # seconds


def classify_no_status_when_blocked(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Agent run >120s wall-clock with no rotate-team-log.sh call. Severity: medium."""
    findings: list[Finding] = []
    turns_list = list(turns)
    # Estimate wall-clock from raw timestamps if present
    first_ts: float | None = None
    last_ts: float | None = None
    saw_team_log = False
    for t in turns_list:
        raw_ts = t.raw.get("timestamp") or t.raw.get("created_at") or t.raw.get("ts")
        if raw_ts:
            try:
                parsed = _parse_ts(str(raw_ts))
                if parsed:
                    epoch = parsed.timestamp()
                    if first_ts is None:
                        first_ts = epoch
                    last_ts = epoch
            except Exception:
                pass
        for tc in t.tool_calls:
            if tc.get("name") == "Bash":
                cmd = tc.get("input", {}).get("command", "")
                if _TEAM_LOG_CALL_PAT.search(cmd):
                    saw_team_log = True
    if first_ts and last_ts:
        duration = last_ts - first_ts
        if duration > _WALL_CLOCK_THRESHOLD and not saw_team_log:
            findings.append(Finding(
                classifier="no_status_when_blocked",
                severity="medium",
                turn_index=len(turns_list) - 1,
                detail=f"Agent ran {duration:.0f}s without any rotate-team-log.sh call",
            ))
    return findings


# -- Dispatcher: run all Phase A.4 classifiers per transcript -----------------

_PHASE_A4_CLASSIFIERS = [
    classify_token_in_team_log,
    classify_curl_insecure_or_k,
    classify_python_verify_false,
    classify_localstorage_token,
    classify_nonexistent_pr_or_disc,
    classify_emoji_in_code_or_commit,
    classify_trailing_summary,
    classify_no_status_when_blocked,
]


# ---------------------------------------------------------------------------
# Phase A.5: self-observe gate classifiers (Discussion #531)
# ---------------------------------------------------------------------------

_SELF_OBSERVE_ROLES = frozenset({"executor"})

# Matches executor role markers in the first few turns
_EXECUTOR_ROLE_PAT = re.compile(
    r"(?:implement\s+discussion|run\s+preflight|open\s+a\s+pr\s+targeting|executor)",
    re.IGNORECASE,
)
# Matches the AGENT_OUTPUT envelope
_AGENT_OUTPUT_PAT = re.compile(r'"verdict"\s*:\s*"done"', re.IGNORECASE)
# Matches evidence that the self-observe gate ran — the agent called agent_retros.py append
_SELF_OBSERVE_RAN_PAT = re.compile(
    r"agent_retros\.py\s+append|backend[/\\]agent_retros\.py",
    re.IGNORECASE,
)
# Matches self_observed: true in envelope
_SELF_OBSERVED_FLAG_PAT = re.compile(r'"self_observed"\s*:\s*true', re.IGNORECASE)


def classify_retro_skipped(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Executor emitted verdict:done without running self-observe gate.

    This fires when:
    1. The transcript looks like an executor (role keywords in early turns).
    2. The final assistant turn contains verdict:done in an AGENT_OUTPUT block.
    3. No Bash tool call in the transcript called agent_retros.py append.
    4. The envelope does NOT contain self_observed:true.

    Severity: medium (regression catcher, shadow by default).
    """
    turns_list = list(turns)
    if not turns_list:
        return []

    # Quick role detection: check first 5 turns for executor role keywords
    is_target_role = False
    for t in turns_list[:5]:
        if t.role in ("user", "system") and _EXECUTOR_ROLE_PAT.search(t.text):
            is_target_role = True
            break
    if not is_target_role:
        return []

    # Check if transcript ended with verdict:done
    final_verdict_done = False
    for t in reversed(turns_list):
        if t.role == "assistant" and _AGENT_OUTPUT_PAT.search(t.text):
            final_verdict_done = True
            break
    if not final_verdict_done:
        return []

    # Check if self-observe gate ran (agent_retros.py append call present)
    self_observe_ran = False
    for t in turns_list:
        for tc in t.tool_calls:
            if tc.get("name") == "Bash":
                cmd = tc.get("input", {}).get("command", "")
                if _SELF_OBSERVE_RAN_PAT.search(cmd):
                    self_observe_ran = True
                    break
        # Also accept self_observed:true in any assistant text
        if t.role == "assistant" and _SELF_OBSERVED_FLAG_PAT.search(t.text):
            self_observe_ran = True
            break
        if self_observe_ran:
            break

    if self_observe_ran:
        return []

    last_turn = turns_list[-1]
    return [Finding(
        classifier="classify_retro_skipped",
        severity="medium",
        turn_index=last_turn.turn_idx,
        detail="Agent emitted verdict:done without calling agent_retros.py append (self-observe gate skipped)",
    )]


# ---------------------------------------------------------------------------
# Phase A.5 — Discussion #548: 8 additional transcript classifiers
# ---------------------------------------------------------------------------

# -- Shared self-reference suppression helper --------------------------------

_META_TOKENS = (
    "classifier", "classify_", "phase_a", "d#508", "d#511",
    "d#542", "d#548", "fixtures/transcripts/",
)


def _is_self_referential_context(text: str, span: tuple[int, int]) -> bool:
    """Return True when the 200-char window around *span* is meta (classifier discussion).

    Used by classifiers #1,#3,#4,#6,#7 to suppress FP matches that appear inside
    a turn that is *about* classifiers (e.g. spec text, Discussion body echoes).
    Classifier #8 emits a self_ref_fp finding whenever this returns True.
    """
    start = max(0, span[0] - 100)
    end = min(len(text), span[1] + 100)
    window = text[start:end].lower()
    return any(tok in window for tok in _META_TOKENS)


# -- 1. thinking_block_excessive (LOW) ---------------------------------------

_THINKING_OPEN_PAT = re.compile(r"<thinking>", re.IGNORECASE)
_THINKING_CLOSE_PAT = re.compile(r"</thinking>", re.IGNORECASE)


def classify_thinking_block_excessive(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """<thinking> content >2000 chars without a subsequent tool call. Severity: low."""
    findings: list[Finding] = []
    turns_list = list(turns)
    for i, t in enumerate(turns_list):
        if t.role != "assistant":
            continue
        text = t.text
        m_open = _THINKING_OPEN_PAT.search(text)
        if not m_open:
            continue
        m_close = _THINKING_CLOSE_PAT.search(text, m_open.end())
        if not m_close:
            continue
        block = text[m_open.end():m_close.start()]
        if len(block) <= 2000:
            continue
        if _is_self_referential_context(text, (m_open.start(), m_close.end())):
            continue
        # Check: does this turn or the next one have a tool call?
        has_tool = bool(t.tool_calls)
        if not has_tool and i + 1 < len(turns_list):
            has_tool = bool(turns_list[i + 1].tool_calls)
        if not has_tool:
            findings.append(Finding(
                classifier="thinking_block_excessive",
                severity="low",
                turn_index=t.turn_idx,
                detail=f"<thinking> block {len(block)} chars, no subsequent tool call",
            ))
    return findings


# -- 2. full_file_read_when_grep (MEDIUM) ------------------------------------

def _edit_max_line(edit_input: dict) -> int:
    """Return the highest line number touched by an Edit tool call (0 if unknown)."""
    old = edit_input.get("old_string", "") or ""
    new = edit_input.get("new_string", "") or ""
    return max(old.count("\n"), new.count("\n"), 1)


def classify_full_file_read_when_grep(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Full >1000-line Read followed by Edit touching ≤5% of lines. Severity: medium.

    Bounded-buffer: cap file_sizes dict at 200 entries per transcript.
    No self-ref suppression needed: pattern is structural (tool sequence), not textual.

    NOTE: In the transcript format, tool_results arrive on the *user* turn that follows
    the assistant turn carrying the tool_call. We track pending Read IDs to match them
    to their results on the next user turn.
    """
    file_sizes: dict[str, tuple[int, int]] = {}  # fp -> (turn_idx, line_count)
    # pending_reads: tool_use_id -> (fp, turn_idx, limit_hint)
    pending_reads: dict[str, tuple[str, int, int]] = {}
    findings: list[Finding] = []
    for t in turns:
        # First: resolve any pending Read results from this turn's tool_results
        for tr in t.tool_results:
            tid = tr.get("tool_use_id", "")
            if tid in pending_reads:
                fp, read_turn_idx, limit_hint = pending_reads.pop(tid)
                content = tr.get("content", "")
                lc = content.count("\n") + 1 if content else limit_hint
                if lc > 1000:
                    file_sizes[fp] = (read_turn_idx, lc)
                    if len(file_sizes) > 200:
                        items = list(file_sizes.items())
                        file_sizes = dict(items[-100:])

        # Then: process tool_calls on this turn
        for tc in t.tool_calls:
            name = tc.get("name", "")
            inp = tc.get("input", {})
            tid = tc.get("id", "")
            if name == "Read":
                fp = inp.get("file_path", "")
                if fp and not _is_excluded_path(fp):
                    limit_hint = int(inp.get("limit", 0)) or 0
                    # Try same-turn tool_results first (edge case)
                    resolved = False
                    for tr in t.tool_results:
                        if tr.get("tool_use_id") == tid:
                            content = tr.get("content", "")
                            lc = content.count("\n") + 1 if content else limit_hint
                            if lc > 1000:
                                file_sizes[fp] = (t.turn_idx, lc)
                            resolved = True
                            break
                    if not resolved and tid:
                        pending_reads[tid] = (fp, t.turn_idx, limit_hint)
                    elif not resolved and limit_hint > 1000:
                        file_sizes[fp] = (t.turn_idx, limit_hint)
            elif name == "Edit":
                fp = inp.get("file_path", "")
                if fp in file_sizes:
                    read_turn, size_lines = file_sizes[fp]
                    edit_lines = _edit_max_line(inp)
                    if size_lines > 1000 and edit_lines < size_lines * 0.05:
                        findings.append(Finding(
                            classifier="full_file_read_when_grep",
                            severity="medium",
                            turn_index=t.turn_idx,
                            detail=(
                                f"Read {size_lines} lines from {fp!r} (turn {read_turn}), "
                                f"then Edit touched ~{edit_lines} lines (≤5%)"
                            ),
                        ))
    return findings


# -- 3. edit_then_revert (MEDIUM) --------------------------------------------

def classify_edit_then_revert(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Same file: N lines added then same N lines removed in one agent run. Severity: medium."""
    from collections import defaultdict
    file_edits: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") != "Edit":
                continue
            inp = tc.get("input", {})
            fp = inp.get("file_path", "")
            old = inp.get("old_string", "") or ""
            new = inp.get("new_string", "") or ""
            if not fp or _is_excluded_path(fp):
                continue
            combined = old + new
            if _is_self_referential_context(combined, (0, len(old))):
                continue
            file_edits[fp].append((t.turn_idx, old, new))
    for fp, edits in file_edits.items():
        if len(edits) < 2:
            continue
        for i, (turn_a, old_a, new_a) in enumerate(edits):
            for turn_b, old_b, new_b in edits[i + 1:]:
                added = new_a.strip()
                removed = old_b.strip()
                if added and removed and added == removed and len(added) > 10:
                    findings.append(Finding(
                        classifier="edit_then_revert",
                        severity="medium",
                        turn_index=turn_b,
                        detail=f"Edit then revert in {fp!r}: {len(added)} chars added (turn {turn_a}) then removed (turn {turn_b})",
                    ))
    return findings


# -- 4. line_number_drift (MEDIUM) -------------------------------------------

_LINE_N_PAT = re.compile(r"\bline[s]?\s+(\d+)\b", re.IGNORECASE)


def classify_line_number_drift(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Agent quotes 'line X' where X > file's actual line count inferred from a prior Read.
    Severity: medium.

    NOTE: tool_results arrive on the *user* turn following the assistant's Read call.
    We track pending Read IDs to match results on the next user turn.
    """
    file_line_counts: dict[str, int] = {}
    pending_reads: dict[str, str] = {}  # tool_use_id -> fp
    findings: list[Finding] = []
    turns_list = list(turns)
    for t in turns_list:
        # Resolve pending Read results from this user turn
        for tr in t.tool_results:
            tid = tr.get("tool_use_id", "")
            if tid in pending_reads:
                fp = pending_reads.pop(tid)
                content = tr.get("content", "")
                if content:
                    lc = content.count("\n") + 1
                    if lc > file_line_counts.get(fp, 0):
                        file_line_counts[fp] = lc

        # Register pending Read calls
        for tc in t.tool_calls:
            if tc.get("name") == "Read":
                fp = tc.get("input", {}).get("file_path", "")
                tid = tc.get("id", "")
                if fp and not _is_excluded_path(fp):
                    resolved = False
                    for tr in t.tool_results:
                        if tr.get("tool_use_id") == tid:
                            content = tr.get("content", "")
                            if content:
                                lc = content.count("\n") + 1
                                if lc > file_line_counts.get(fp, 0):
                                    file_line_counts[fp] = lc
                            resolved = True
                            break
                    if not resolved and tid:
                        pending_reads[tid] = fp

        # Check assistant text for line-N references
        if t.role == "assistant" and file_line_counts:
            text = t.text
            for m in _LINE_N_PAT.finditer(text):
                if _is_self_referential_context(text, m.span()):
                    continue
                quoted_line = int(m.group(1))
                ctx_start = max(0, m.start() - 200)
                ctx_end = min(len(text), m.end() + 200)
                context_window = text[ctx_start:ctx_end]
                for fp, lc in file_line_counts.items():
                    fname = fp.split("/")[-1]
                    if fname in context_window and quoted_line > lc:
                        findings.append(Finding(
                            classifier="line_number_drift",
                            severity="medium",
                            turn_index=t.turn_idx,
                            detail=f"Quoted line {quoted_line} > actual {lc} lines in {fp!r}",
                        ))
                        break
    return findings


# -- 5. no_pull_before_branch (MEDIUM) ---------------------------------------

_BRANCH_CREATE_PAT = re.compile(
    r"git\s+(?:checkout\s+-b|switch\s+-c)\s+\S+", re.IGNORECASE
)
_GIT_PULL_FF_PAT = re.compile(r"git\s+pull\s+.*--ff-only", re.IGNORECASE)


def classify_no_pull_before_branch(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """git checkout -b / switch -c without prior git pull --ff-only. Severity: medium.

    No self-ref suppression: pattern is a Bash command, not prose text.
    """
    findings: list[Finding] = []
    saw_pull = False
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "")
            if _GIT_PULL_FF_PAT.search(cmd):
                saw_pull = True
            elif _BRANCH_CREATE_PAT.search(cmd) and not saw_pull:
                findings.append(Finding(
                    classifier="no_pull_before_branch",
                    severity="medium",
                    turn_index=t.turn_idx,
                    detail=f"Branch created without prior git pull --ff-only: {cmd[:100]}",
                ))
    return findings


# -- 6. question_pile_up (LOW) -----------------------------------------------

_CODE_BLOCK_PAT = re.compile(r"```.*?```|`[^`]+`", re.DOTALL)


def classify_question_pile_up(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Single assistant turn with ≥3 '?' outside code blocks. Severity: low."""
    findings: list[Finding] = []
    for t in turns:
        if t.role != "assistant":
            continue
        text = t.text
        stripped = _CODE_BLOCK_PAT.sub("", text)
        if _is_self_referential_context(stripped, (0, len(stripped))):
            continue
        q_count = stripped.count("?")
        if q_count >= 3:
            findings.append(Finding(
                classifier="question_pile_up",
                severity="low",
                turn_index=t.turn_idx,
                detail=f"{q_count} question marks in assistant turn outside code blocks",
            ))
    return findings


# -- 7. memory_md_ignored (MEDIUM) -------------------------------------------

_MEMORY_READ_PAT = re.compile(r"MEMORY\.md", re.IGNORECASE)
_MEMORY_RULE_SIGNALS = [
    (re.compile(r"git\s+rm\b", re.IGNORECASE), "git rm used after MEMORY.md read (should use git mv to archive/)"),
    (re.compile(r"gh\s+pr\s+edit\s+.*--add-label", re.IGNORECASE), "gh pr edit --add-label used (banned; use gh api POST)"),
    (re.compile(r"git\s+add\s+-A\b|git\s+add\s+\.\s*$", re.IGNORECASE), "git add -A/. used (may include secrets)"),
]


def classify_memory_md_ignored(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Agent Read MEMORY.md then violated a cited rule. Severity: medium."""
    findings: list[Finding] = []
    turns_list = list(turns)
    saw_memory_read = False
    memory_read_turn = -1
    for t in turns_list:
        for tc in t.tool_calls:
            name = tc.get("name", "")
            inp = tc.get("input", {})
            if name == "Read":
                fp = inp.get("file_path", "") or ""
                if _MEMORY_READ_PAT.search(fp):
                    saw_memory_read = True
                    memory_read_turn = t.turn_idx
            if not saw_memory_read:
                continue
            if name == "Bash":
                cmd = inp.get("command", "") or ""
                if _is_self_referential_context(cmd, (0, len(cmd))):
                    continue
                for rule_pat, rule_msg in _MEMORY_RULE_SIGNALS:
                    if rule_pat.search(cmd):
                        findings.append(Finding(
                            classifier="memory_md_ignored",
                            severity="medium",
                            turn_index=t.turn_idx,
                            detail=f"After MEMORY.md read (turn {memory_read_turn}): {rule_msg}",
                        ))
    return findings


# -- 8. self_referenced_classifier_match (MEDIUM) ----------------------------

_SELF_REF_TRIGGER_PATS = [
    _THINKING_OPEN_PAT,
    re.compile(r"\bedit\b.*\brevert\b|\brevert\b.*\bedit\b", re.IGNORECASE),
    _LINE_N_PAT,
    re.compile(r"\?"),
    _MEMORY_READ_PAT,
]


def classify_self_referenced_classifier_match(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """FP catcher: fires when a classifier pattern matched but was suppressed as meta-context.
    Severity: medium. Signals that classifier suppression is active (shadow audit only).
    """
    findings: list[Finding] = []
    for t in turns:
        text = t.text
        if not text:
            continue
        for pat in _SELF_REF_TRIGGER_PATS:
            m = pat.search(text)
            if m and _is_self_referential_context(text, m.span()):
                findings.append(Finding(
                    classifier="self_referenced_classifier_match",
                    severity="medium",
                    turn_index=t.turn_idx,
                    detail=f"Pattern {pat.pattern[:60]!r} matched in meta-context window (suppressed FP)",
                ))
                break
    return findings


_PHASE_A5_CLASSIFIERS = [
    classify_retro_skipped,
    classify_thinking_block_excessive,
    classify_full_file_read_when_grep,
    classify_edit_then_revert,
    classify_line_number_drift,
    classify_no_pull_before_branch,
    classify_question_pile_up,
    classify_memory_md_ignored,
    classify_self_referenced_classifier_match,
]


# ---------------------------------------------------------------------------
# Phase A.6: Worktree isolation classifier (Discussion #592)
# ---------------------------------------------------------------------------
#
# Detects agents in worktree-isolated runs that wrote files to the main repo
# instead of their worktree.  Root cause: spawn prompts say "edit backend/foo.py"
# without specifying the worktree prefix; the model resolves to the main-repo
# absolute path it was trained on.
#
# Detection logic — <main-repo-root> below is main_repo_root(), resolved at
# import time, never a literal: the paths this classifier compares against are
# whatever checkout it is actually running against, and writing one machine's
# path here is what silently disabled it before (D#1997).
#   1. Determine the agent's worktree prefix from the transcript path.
#      Worktree transcripts live at:
#        <main-repo-root>/.claude/projects/.../<agent-id>.jsonl
#        OR the agent's cwd starts with <main-repo-root>/.claude/worktrees/agent-<id>/
#   2. Scan Edit/Write tool calls for file_path arguments.
#   3. Flag any path that:
#        a. starts with <main-repo-root>/
#        b. does NOT start with <main-repo-root>/.claude/worktrees/agent-<id>/
#        c. is NOT a .autonomous-team/ metadata path (those are shared by design)
#   4. One HIGH finding per occurrence.
# ---------------------------------------------------------------------------

# Every constant below describes the MAIN checkout, not the checkout this
# process happens to be running in. That distinction is the whole classifier:
# it asks "did an agent write into the main repo instead of its own worktree",
# so a main-repo root derived from this file's location answers the wrong
# question — inside a worktree it names the worktree, and then every path the
# classifier is supposed to flag fails the startswith() and nothing ever
# fires. It had been doing exactly that (D#1997).
_MAIN_REPO_ROOT_PATH = main_repo_root()
_MAIN_REPO_ROOT = str(_MAIN_REPO_ROOT_PATH)
_WORKTREE_BASE = str(_MAIN_REPO_ROOT_PATH / ".claude" / "worktrees")

# Paths under the main repo that are legitimately shared across all agents
# (not worktree-isolated) — writes here are not contamination.
_SHARED_PATH_PREFIXES = (
    str(_MAIN_REPO_ROOT_PATH / ".autonomous-team") + "/",
    str(_MAIN_REPO_ROOT_PATH / ".claude") + "/",
)

# Regex to extract agent-id from a worktree transcript path or cwd
_AGENT_ID_IN_PATH_PAT = re.compile(
    r"\.claude/(?:worktrees|projects)/[^/]*/agent-([a-f0-9]+)"
    r"|worktrees/agent-([a-f0-9]+)",
    re.IGNORECASE,
)
# Regex to identify agent_id from agent-feed path hints
_AGENT_FEED_AGENT_ID_PAT = re.compile(r"agent-([a-f0-9]{8,})", re.IGNORECASE)


def _worktree_prefix_from_agent_id(agent_id: str) -> str | None:
    """Return the expected absolute worktree root for a given agent-id, if it exists."""
    if not agent_id:
        return None
    candidate = f"{_WORKTREE_BASE}/agent-{agent_id}"
    if Path(candidate).exists():
        return candidate
    # The transcript dir name may include more of the id; try prefix match
    try:
        wt_dir = Path(_WORKTREE_BASE)
        if wt_dir.exists():
            for d in wt_dir.iterdir():
                if d.name.startswith(f"agent-{agent_id}") or agent_id.startswith(
                    d.name.replace("agent-", "")[:8]
                ):
                    return str(d)
    except OSError:
        pass
    return None


def _is_worktree_transcript(path: Path) -> tuple[bool, str]:
    """Return (is_worktree_run, agent_id).

    A transcript is considered worktree-isolated when its path contains
    .claude/worktrees/ or .claude/projects/ with an agent-<id> segment.
    """
    path_str = str(path)
    m = _AGENT_ID_IN_PATH_PAT.search(path_str)
    if m:
        agent_id = m.group(1) or m.group(2) or ""
        return bool(agent_id), agent_id
    return False, ""


def _is_main_repo_write(file_path: str) -> bool:
    """Return True if file_path is rooted in the main repo (not a shared path)."""
    if not file_path.startswith(_MAIN_REPO_ROOT):
        return False
    for prefix in _SHARED_PATH_PREFIXES:
        if file_path.startswith(prefix):
            return False
    return True


def _is_inside_worktree(file_path: str, worktree_prefix: str) -> bool:
    """Return True if file_path lives inside the agent's own worktree."""
    return file_path.startswith(worktree_prefix + "/") or file_path == worktree_prefix


def classify_wrote_outside_worktree(
    turns: Iterable["TranscriptTurn"],
    agent_id: str = "",
) -> list[Finding]:
    """Edit/Write tool call whose file_path is in the main repo, not the agent worktree.

    Severity: HIGH — writes to main contaminate the shared repo and break other agents.

    Detection:
    - Only fires when we can resolve the agent's worktree prefix from agent_id.
    - Relative paths (no leading /) are skipped — they're ambiguous.
    - .autonomous-team/ and .claude/ paths are excluded (shared by design).
    - One finding per occurrence (not one per file) so the evidence is granular.
    """
    findings: list[Finding] = []
    worktree_prefix = _worktree_prefix_from_agent_id(agent_id) if agent_id else None

    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") not in _EDIT_WRITE_NAMES:
                continue
            inp = tc.get("input", {})
            fp = inp.get("file_path", inp.get("path", ""))
            if not fp or not fp.startswith("/"):
                continue  # relative path — ambiguous, skip
            if not _is_main_repo_write(fp):
                continue  # not in main repo at all (e.g. /tmp/…)
            if worktree_prefix and _is_inside_worktree(fp, worktree_prefix):
                continue  # correct: inside own worktree
            if worktree_prefix is None:
                # No worktree on disk — flag if path looks like it should be in one
                # (i.e. agent_id looks like a worktree hash but directory is gone)
                if not agent_id:
                    continue  # can't determine isolation context
                # Still flag — agent was spawned with isolation=worktree but
                # directory may have been cleaned up already
            findings.append(Finding(
                classifier="wrote_outside_worktree",
                severity="high",
                turn_index=t.turn_idx,
                detail=(
                    f"Edit/Write on main-repo path {fp!r} from worktree agent "
                    f"{agent_id[:20] or 'unknown'} — should write to worktree"
                ),
            ))
    return findings


def _run_phase_a5_classifiers(since_seconds: int = 7 * 24 * 3600) -> list[dict]:
    """Run Phase A.5 self-observe classifiers over all recent transcripts."""
    try:
        from transcript_reader import iter_transcripts, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_transcripts, agent_id_from_path
        except ImportError:
            return []

    all_findings: list[dict] = []
    for path, turns_iter in iter_transcripts(since_seconds=since_seconds):
        agent_id = agent_id_from_path(path)
        turns_list = list(turns_iter)
        for classifier_fn in _PHASE_A5_CLASSIFIERS:
            for finding in classifier_fn(iter(turns_list)):
                all_findings.append({
                    "category": finding.classifier,
                    "severity": finding.severity,
                    "title": (
                        f"[A5] {finding.classifier}: agent {agent_id[:20]} "
                        f"(turn {finding.turn_index})"
                    ),
                    "evidence": [
                        f"agent={agent_id[:40]}",
                        f"turn={finding.turn_index}",
                        f"detail={finding.detail[:120]}",
                    ],
                    "suggested_discussion_title": (
                        "[Bug] executor skipped self-observe gate"
                    ),
                    "suggested_tag": "[Bug]",
                })
    return all_findings


def _run_phase_a6_classifiers(since_seconds: int = 7 * 24 * 3600) -> list[dict]:
    """Run Phase A.6 worktree isolation classifiers over all recent transcripts.

    Detects Edit/Write tool calls whose file_path is in the main repo instead
    of the agent's worktree (Discussion #592 PR-a).
    """
    try:
        from transcript_reader import iter_transcripts, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_transcripts, agent_id_from_path
        except ImportError:
            return []

    all_findings: list[dict] = []
    for path, turns_iter in iter_transcripts(since_seconds=since_seconds):
        is_worktree, agent_id = _is_worktree_transcript(path)
        if not is_worktree:
            continue  # only check worktree-isolated agents
        turns_list = list(turns_iter)
        for finding in classify_wrote_outside_worktree(iter(turns_list), agent_id=agent_id):
            all_findings.append({
                "category": finding.classifier,
                "severity": finding.severity,
                "title": (
                    f"[A6] {finding.classifier}: agent {agent_id[:20]} "
                    f"(turn {finding.turn_index})"
                ),
                "evidence": [
                    f"agent={agent_id[:40]}",
                    f"turn={finding.turn_index}",
                    f"detail={finding.detail[:120]}",
                ],
                "suggested_discussion_title": (
                    "[Bug] Worktree-isolated agent wrote to main repo — contamination"
                ),
                "suggested_tag": "[Bug]",
            })
    return all_findings


# ---------------------------------------------------------------------------
# Phase A.7: Sleep-retry loop classifier (Discussion #592 PR-b)
# ---------------------------------------------------------------------------
#
# Detects executor/reviewer transcripts containing `sleep` combined with `gh`
# commands inside a loop construct — the "sleep && gh pr create" anti-pattern
# that burns agent turns on rate-limit retries.
#
# Pattern (case-insensitive):
#   `until ... sleep ... gh` OR `while ... sleep ... gh`
# Also catches single-line compound: `sleep \d+ && gh` inside a Bash command.
#
# Severity: high — wastes budget, produces no output, leaves pending PRs
# untracked.
# ---------------------------------------------------------------------------

_SLEEP_RETRY_LOOP_PAT = re.compile(
    # until/while block that contains both "sleep" AND "gh" anywhere within it
    # (order independent — gh can be in the condition or the body)
    r"(until\b(?:(?!done\b).)*\bsleep\b(?:(?!done\b).)*done\b"   # until...done with sleep
    r"|until\b(?:(?!done\b).)*\bgh\b(?:(?!done\b).)*sleep(?:(?!done\b).)*done\b"  # until gh..sleep..done
    r"|while\b(?:(?!done\b).)*\bsleep\b(?:(?!done\b).)*\bgh\b(?:(?!done\b).)*done\b"  # while..sleep..gh..done
    r"|while\b(?:(?!done\b).)*\bgh\b(?:(?!done\b).)*sleep(?:(?!done\b).)*done\b"  # while gh..sleep..done
    r"|sleep\s+\d+\s*&&\s*gh\b"             # sleep N && gh ...
    r"|sleep\s+\d+\s*;\s*gh\b"              # sleep N; gh ...
    r")",
    re.IGNORECASE | re.DOTALL,
)


def classify_sleep_retry_loop(turns: Iterable["TranscriptTurn"]) -> list[Finding]:
    """Detect executor sleep loops used to retry gh commands after rate-limit.

    Hard rule (Discussion #592 PR-b): if gh pr create / gh comment / gh label
    returns a 403 secondary rate limit, agents MUST return verdict=done with
    blocked_reason="rate_limit" — NOT loop and sleep.

    Severity: high — wastes turns, produces no durable output, bypasses the
    pending-prs.json drain mechanism.
    """
    findings: list[Finding] = []
    for t in turns:
        for tc in t.tool_calls:
            if tc.get("name") != "Bash":
                continue
            cmd = tc.get("input", {}).get("command", "")
            if _SLEEP_RETRY_LOOP_PAT.search(cmd):
                findings.append(Finding(
                    classifier="sleep_retry_loop",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=f"sleep+gh loop in Bash: {cmd[:160]}",
                ))
    return findings


def _run_phase_a7_classifiers(since_seconds: int = 7 * 24 * 3600) -> list[dict]:
    """Run Phase A.7 sleep-retry-loop classifiers over all recent transcripts.

    Detects agents using sleep loops to retry gh API calls on rate-limit
    (Discussion #592 PR-b).
    """
    try:
        from transcript_reader import iter_transcripts, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_transcripts, agent_id_from_path
        except ImportError:
            return []

    all_findings: list[dict] = []
    for path, turns_iter in iter_transcripts(since_seconds=since_seconds):
        agent_id = agent_id_from_path(path)
        turns_list = list(turns_iter)
        for finding in classify_sleep_retry_loop(iter(turns_list)):
            all_findings.append({
                "category": finding.classifier,
                "severity": finding.severity,
                "title": (
                    f"[A7] {finding.classifier}: agent {agent_id[:20]} "
                    f"(turn {finding.turn_index})"
                ),
                "evidence": [
                    f"agent={agent_id[:40]}",
                    f"turn={finding.turn_index}",
                    f"detail={finding.detail[:160]}",
                ],
                "suggested_discussion_title": (
                    "[Bug] Executor sleep-retried gh command instead of writing pending-prs.json"
                ),
                "suggested_tag": "[Bug]",
            })
    return all_findings


# ---------------------------------------------------------------------------
# Phase A.8: stale_rebase + gate_check_skipped classifiers (Discussion #655)
# ---------------------------------------------------------------------------

try:
    from backend.classifiers.stale_rebase_warning import classify_stale_rebase_warning  # noqa: E402
except ImportError:
    from classifiers.stale_rebase_warning import classify_stale_rebase_warning  # noqa: E402

try:
    from backend.classifiers.gate_check_skipped import classify_gate_check_skipped  # noqa: E402
except ImportError:
    from classifiers.gate_check_skipped import classify_gate_check_skipped  # noqa: E402

try:
    from backend.classifiers.fixture_only_test_pass import classify_fixture_only_test_pass  # noqa: E402
except ImportError:
    from classifiers.fixture_only_test_pass import classify_fixture_only_test_pass  # noqa: E402

try:
    from backend.classifiers.silent_subprocess_failure import classify_silent_subprocess_failure  # noqa: E402
except ImportError:
    from classifiers.silent_subprocess_failure import classify_silent_subprocess_failure  # noqa: E402

_PHASE_A8_CLASSIFIERS = [
    classify_stale_rebase_warning,
    classify_gate_check_skipped,
    classify_fixture_only_test_pass,
    classify_silent_subprocess_failure,
]


def _run_phase_a8_classifiers(since_seconds: int = 7 * 24 * 3600) -> list[dict]:
    """Run Phase A.8 classifiers over all recent transcripts.

    Detects stale-base pushes (stale_rebase_warning) and merges that bypassed
    the security-review-passed gate (gate_check_skipped) — Discussion #655 PR-a.
    """
    try:
        from transcript_reader import iter_transcripts, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_transcripts, agent_id_from_path
        except ImportError:
            return []

    all_findings: list[dict] = []
    for path, turns_iter in iter_transcripts(since_seconds=since_seconds):
        agent_id = agent_id_from_path(path)
        turns_list = list(turns_iter)
        for classifier_fn in _PHASE_A8_CLASSIFIERS:
            for finding in classifier_fn(iter(turns_list)):
                all_findings.append({
                    "category": finding.classifier,
                    "severity": finding.severity,
                    "title": (
                        f"[A8] {finding.classifier}: agent {agent_id[:20]} "
                        f"(turn {finding.turn_index})"
                    ),
                    "evidence": [
                        f"agent={agent_id[:40]}",
                        f"turn={finding.turn_index}",
                        f"detail={finding.detail[:120]}",
                    ],
                    "suggested_discussion_title": (
                        f"[Bug] transcript classifier {finding.classifier} fired"
                    ),
                    "suggested_tag": "[Bug]",
                })
    return all_findings


def scan_single_transcript(transcript_path: str) -> list[dict]:
    """Scan a single transcript file and return findings as JSON-serializable list.

    Used by the self-observe gate: agents call this on their own transcript before
    emitting verdict:done. Returns findings sorted by severity (high first).

    Wall-clock target: ≤10s p95 for typical transcripts.
    """
    try:
        from transcript_reader import iter_turns, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_turns, agent_id_from_path
        except ImportError:
            return []

    path = Path(transcript_path)
    if not path.exists():
        return []

    agent_id = agent_id_from_path(path)
    turns_list = list(iter_turns(path))

    all_findings: list[dict] = []

    # --- Phase A.3 classifiers ---
    for classifier_fn in _PHASE_A3_CLASSIFIERS:
        for finding in classifier_fn(iter(turns_list)):
            all_findings.append({
                "category": finding.classifier,
                "severity": finding.severity,
                "title": (
                    f"[A3] {finding.classifier}: agent {agent_id[:20]} "
                    f"(turn {finding.turn_index})"
                ),
                "evidence": [
                    f"agent={agent_id[:40]}",
                    f"turn={finding.turn_index}",
                    f"detail={finding.detail[:120]}",
                ],
            })

    # --- Phase A.4 classifiers ---
    for classifier_fn in _PHASE_A4_CLASSIFIERS:
        for finding in classifier_fn(iter(turns_list)):
            all_findings.append({
                "category": finding.classifier,
                "severity": finding.severity,
                "title": (
                    f"[A4] {finding.classifier}: agent {agent_id[:20]} "
                    f"(turn {finding.turn_index})"
                ),
                "evidence": [
                    f"agent={agent_id[:40]}",
                    f"turn={finding.turn_index}",
                    f"detail={finding.detail[:120]}",
                ],
            })

    # --- Phase A.2 state-based classifiers (single transcript) ---
    state: dict = {
        "path": str(path),
        "agent_id": agent_id,
        "turns": 0,
        "is_team_lead": False,
        "has_edit_write": False,
        "auth_leak_cmds": [],
        "permission_phrases": [],
        "repeated_reads": Counter(),
        "read_turns": defaultdict(list),
        "write_paths": set(),
        "general_purpose_hits": [],
        "wrong_premise_err_turns": [],
        "prev_user_text": "",
    }
    for turn in turns_list:
        state["turns"] += 1
        if turn.turn_idx < 3 and turn.role in ("user", "system"):
            if _TEAM_LEAD_KEYWORDS.search(turn.text):
                state["is_team_lead"] = True
        for tc in turn.tool_calls:
            name = tc.get("name", "")
            inp = tc.get("input", {})
            if name in _EDIT_WRITE_TOOLS:
                state["has_edit_write"] = True
                fp = inp.get("file_path", inp.get("path", ""))
                if fp:
                    state["write_paths"].add(fp)
            if name == "Read":
                fp = inp.get("file_path", inp.get("path", ""))
                if fp:
                    state["repeated_reads"][fp] += 1
                    state["read_turns"][fp].append(turn.turn_idx)
            if name == "Bash":
                cmd = inp.get("command", "")
                if _RE_AUTH_LEAK.search(cmd):
                    state["auth_leak_cmds"].append(cmd[:120])
            if name in ("Bash", "Agent"):
                target = inp.get("command", "") or inp.get("prompt", "") or json.dumps(inp)
                if _RE_GENERAL_PURPOSE.search(target):
                    state["general_purpose_hits"].append(target[:100])
        for tr in turn.tool_results:
            content = tr.get("content", "")
            is_error = tr.get("is_error", False)
            if is_error or _RE_WRONG_PREMISE.search(str(content)):
                tool_use_id = tr.get("tool_use_id", "")
                matched_name, matched_key = "unknown", ""
                for tc in turn.tool_calls:
                    if tc.get("id") == tool_use_id or not tool_use_id:
                        matched_name = tc.get("name", "")
                        inp2 = tc.get("input", {})
                        matched_key = inp2.get("command", inp2.get("file_path", json.dumps(inp2)))[:60]
                        break
                state["wrong_premise_err_turns"].append(
                    (turn.turn_idx, matched_name, matched_key)
                )
        if turn.role == "assistant" and _RE_PERMISSION_SEEKING.search(turn.text):
            if not state.get("prev_user_text", "").rstrip().endswith("?"):
                state["permission_phrases"].append((turn.turn_idx, turn.text[:100]))
        if turn.role == "user":
            state["prev_user_text"] = turn.text

    for finding in classify_wrong_premise_retries([state]):
        all_findings.append(finding)
    for finding in classify_forbidden_subagent_type([state]):
        all_findings.append(finding)
    for finding in classify_team_lead_self_edit([state]):
        all_findings.append(finding)
    for finding in classify_auth_leak_risk([state]):
        all_findings.append(finding)
    for finding in classify_permission_seeking([state]):
        all_findings.append(finding)
    for finding in classify_repeated_file_reads([state]):
        all_findings.append(finding)

    # --- Phase A.7: sleep-retry-loop (Discussion #592 PR-b) ---
    for finding in classify_sleep_retry_loop(iter(turns_list)):
        all_findings.append({
            "category": finding.classifier,
            "severity": finding.severity,
            "title": (
                f"[A7] {finding.classifier}: agent {agent_id[:20]} "
                f"(turn {finding.turn_index})"
            ),
            "evidence": [
                f"agent={agent_id[:40]}",
                f"turn={finding.turn_index}",
                f"detail={finding.detail[:160]}",
            ],
        })

    # --- Phase A.8: stale_rebase + gate_check_skipped (Discussion #655 PR-a) ---
    for classifier_fn in _PHASE_A8_CLASSIFIERS:
        for finding in classifier_fn(iter(turns_list)):
            all_findings.append({
                "category": finding.classifier,
                "severity": finding.severity,
                "title": (
                    f"[A8] {finding.classifier}: agent {agent_id[:20]} "
                    f"(turn {finding.turn_index})"
                ),
                "evidence": [
                    f"agent={agent_id[:40]}",
                    f"turn={finding.turn_index}",
                    f"detail={finding.detail[:120]}",
                ],
            })

    # Dedupe and sort by severity
    seen_keys: set[tuple] = set()
    deduped: list[dict] = []
    for f_ in all_findings:
        key = (f_.get("category", ""), f_.get("title", ""))
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(f_)

    sev_order = {"high": 0, "medium": 1, "low": 2}
    deduped.sort(key=lambda f_: sev_order.get(f_.get("severity", "low"), 2))

    # Cap at top-3 findings by severity
    if len(deduped) > 3:
        truncated = deduped[:3]
        for f_ in truncated:
            f_["truncated"] = False
        # Mark that results were capped
        truncated[-1]["truncated"] = True
        return truncated

    return deduped


def _run_phase_a4_classifiers(since_seconds: int = 7 * 24 * 3600) -> list[dict]:
    """Run Phase A.4 classifiers over all recent transcripts; return list of finding dicts."""
    try:
        from transcript_reader import iter_transcripts, agent_id_from_path
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from transcript_reader import iter_transcripts, agent_id_from_path
        except ImportError:
            return []

    all_findings: list[dict] = []
    for path, turns_iter in iter_transcripts(since_seconds=since_seconds):
        agent_id = agent_id_from_path(path)
        turns_list = list(turns_iter)
        for classifier_fn in _PHASE_A4_CLASSIFIERS:
            for finding in classifier_fn(iter(turns_list)):
                all_findings.append({
                    "category": finding.classifier,
                    "severity": finding.severity,
                    "title": (
                        f"[A4] {finding.classifier}: agent {agent_id[:20]} "
                        f"(turn {finding.turn_index})"
                    ),
                    "evidence": [
                        f"agent={agent_id[:40]}",
                        f"turn={finding.turn_index}",
                        f"detail={finding.detail[:120]}",
                    ],
                    "suggested_discussion_title": (
                        f"[Bug] transcript classifier {finding.classifier} fired"
                    ),
                    "suggested_tag": "[Bug]",
                })
    return all_findings


# ---------------------------------------------------------------------------
# Live-mode classifier extension (Discussion #574 PR-a)
# ---------------------------------------------------------------------------
#
# Live mode: read a partial transcript from a byte offset, run the 4
# allowlisted hard-rule classifiers, return JSON with findings +
# next_byte_offset so the daemon can resume from where it left off.
#
# Allowlist (only these classifiers run in --live mode):
#   1. git_rm_usage           — hard rule, fires immediately
#   2. forbidden_subagent_type — hard rule, fires immediately
#   3. wrong_premise_retries  — only when retry count >= 8 (live threshold)
#   4. tool_output_ignored    — only when is_error:true + no pivot for 3+
#                               consecutive ignored turns
#
# Classifiers that require a complete transcript (e.g. classify_retro_skipped,
# classify_preflight_skipped) are excluded.  Pass --allow-partial to explicitly
# acknowledge this and still run live mode; it is the default for --live.
# ---------------------------------------------------------------------------

LIVE_ALLOWLIST = frozenset({
    "git_rm_usage",
    "forbidden_subagent_type",
    "wrong_premise_retries",
    "tool_output_ignored",
})

# Live-mode threshold: wrong_premise_retries only fires at >= 8 retries
LIVE_WRONG_PREMISE_MIN_RETRIES = 8

# Live-mode threshold: tool_output_ignored fires after 3+ consecutive ignored errors
LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK = 3


def iter_turns_from_offset(
    transcript_path: str,
    since_byte: int = 0,
) -> tuple[list["TranscriptTurn"], int]:
    """Parse a transcript JSONL starting at *since_byte*.

    Returns (turns_list, next_byte_offset).  next_byte_offset points to the
    byte after the last complete line successfully parsed — the daemon stores
    this to resume on the next poll cycle.

    Partial last lines (transcript still growing) are silently skipped so the
    offset always lands on a complete-line boundary.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from transcript_reader import TranscriptTurn as _TT  # noqa: F401 (type check)
        from transcript_reader import iter_turns as _iter_turns
    except ImportError:
        return [], since_byte

    path = Path(transcript_path)
    if not path.exists():
        return [], since_byte

    turns: list = []
    next_offset = since_byte

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(since_byte)
            while True:
                line_start = fh.tell()
                raw_line = fh.readline()
                if not raw_line:
                    break  # EOF
                if not raw_line.endswith("\n"):
                    # Partial line at EOF — transcript still being written; skip
                    break
                stripped = raw_line.rstrip("\n")
                if not stripped:
                    next_offset = fh.tell()
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    next_offset = fh.tell()
                    continue
                if not isinstance(obj, dict):
                    next_offset = fh.tell()
                    continue

                # Reuse transcript_reader's normalization logic
                msg_raw = obj.get("message", obj)
                msg = msg_raw if isinstance(msg_raw, dict) else obj
                role = str(msg.get("role", obj.get("role", "")))
                content_raw = msg.get("content", obj.get("content", ""))

                from transcript_reader import _extract_content, TranscriptTurn
                text, tool_calls, tool_results = _extract_content(content_raw)
                turn = TranscriptTurn(
                    turn_idx=len(turns),
                    role=role,
                    text=text,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    raw=obj,
                )
                turns.append(turn)
                next_offset = fh.tell()
    except OSError:
        return [], since_byte

    return turns, next_offset


def _live_wrong_premise_retries(turns: list) -> list[dict]:
    """Live-mode variant: only fires when retry count >= LIVE_WRONG_PREMISE_MIN_RETRIES.

    Reuses the same state-builder as classify_wrong_premise_retries but applies
    a stricter threshold to keep FP rate low on partial transcripts.
    """
    # Build state dict from turns (same logic as _scan_transcripts per-turn section)
    wrong_premise_err_turns: list[tuple[int, str, str]] = []
    for t in turns:
        for tr in t.tool_results:
            content = tr.get("content", "")
            is_error = tr.get("is_error", False)
            if is_error or _RE_WRONG_PREMISE.search(str(content)):
                tool_use_id = tr.get("tool_use_id", "")
                matched_name, matched_key = "unknown", ""
                for tc in t.tool_calls:
                    if tc.get("id") == tool_use_id or not tool_use_id:
                        matched_name = tc.get("name", "")
                        inp2 = tc.get("input", {})
                        if matched_name == "Bash":
                            matched_key = inp2.get("command", "")[:60]
                        elif matched_name == "Read":
                            matched_key = inp2.get("file_path", "")[:60]
                        else:
                            matched_key = json.dumps(inp2)[:60]
                        break
                wrong_premise_err_turns.append((t.turn_idx, matched_name, matched_key))

    err_groups: dict[tuple, list[int]] = defaultdict(list)
    for turn_idx, tool_name, norm_key in wrong_premise_err_turns:
        err_groups[(tool_name, norm_key)].append(turn_idx)

    findings = []
    for (tool_name, norm_key), turn_indices in err_groups.items():
        if len(turn_indices) >= LIVE_WRONG_PREMISE_MIN_RETRIES:
            span = turn_indices[-1] - turn_indices[0] if len(turn_indices) > 1 else 0
            if span >= 2:
                findings.append({
                    "category": "wrong_premise_retries",
                    "severity": "high",
                    "title": (
                        f"[live] wrong_premise_retries: {len(turn_indices)} retries of "
                        f"'{tool_name}' key='{norm_key[:40]}'"
                    ),
                    "evidence": [
                        f"tool={tool_name}",
                        f"key={norm_key[:50]}",
                        f"retry_count={len(turn_indices)}",
                        f"live_threshold={LIVE_WRONG_PREMISE_MIN_RETRIES}",
                    ],
                    "live_mode": True,
                })
    return findings


def _live_tool_output_ignored(turns: list) -> list[dict]:
    """Live-mode variant: fires when is_error:true tool results are ignored for 3+ turns.

    Standard classify_tool_output_ignored fires per incident. The live variant
    requires a streak of LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK ignored errors
    before firing, to reduce FP on transient errors.
    """
    streak = 0
    first_fail_turn = -1
    findings = []

    for t in turns:
        if t.role == "user" and t.tool_results:
            for tr in t.tool_results:
                if tr.get("is_error") and _TOOL_REAL_ERROR_PAT.search(tr.get("content", "")):
                    if streak == 0:
                        first_fail_turn = t.turn_idx
                    streak += 1
                    break
        elif t.role == "assistant" and streak > 0:
            acknowledged = _ACKNOWLEDGEMENT_PAT.search(t.text)
            pivoted = bool(t.tool_calls and any(
                tc.get("name", "") in _PIVOT_TOOLS for tc in t.tool_calls
            ))
            if acknowledged or pivoted:
                streak = 0
                first_fail_turn = -1
            # else: streak continues

    if streak >= LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK:
        findings.append({
            "category": "tool_output_ignored",
            "severity": "high",
            "title": (
                f"[live] tool_output_ignored: {streak} consecutive ignored errors "
                f"starting at turn {first_fail_turn}"
            ),
            "evidence": [
                f"streak={streak}",
                f"first_fail_turn={first_fail_turn}",
                f"live_threshold={LIVE_TOOL_OUTPUT_IGNORED_MIN_STREAK}",
            ],
            "live_mode": True,
        })
    return findings


def _live_forbidden_subagent_type(turns: list) -> list[dict]:
    """Live-mode wrapper for forbidden_subagent_type.

    classify_forbidden_subagent_type() consumes state dicts produced by
    _scan_transcripts(). For live mode we build the minimal state in-line
    rather than calling the heavy scanner.
    """
    general_purpose_hits = []
    for t in turns:
        for tc in t.tool_calls:
            name = tc.get("name", "")
            if name in ("Bash", "Agent"):
                inp = tc.get("input", {})
                # For Agent() calls check subagent_type field directly
                if name == "Agent":
                    subagent_type = inp.get("subagent_type", "")
                    if _RE_GENERAL_PURPOSE.search(
                        subagent_type + " " + json.dumps(inp)
                    ):
                        general_purpose_hits.append(json.dumps(inp)[:100])
                        continue
                # For Bash() check the full command text
                target = inp.get("command", "") or json.dumps(inp)
                if _RE_GENERAL_PURPOSE.search(target):
                    general_purpose_hits.append(target[:100])

    if not general_purpose_hits:
        return []

    return [{
        "category": "forbidden_subagent_type",
        "severity": "high",
        "title": (
            f"[live] forbidden_subagent_type: general-purpose used "
            f"({len(general_purpose_hits)} occurrence(s))"
        ),
        "evidence": [h[:80] for h in general_purpose_hits[:3]],
        "live_mode": True,
    }]


def run_live_mode(transcript_path: str, since_byte: int = 0) -> dict:
    """Run the live-mode allowlisted classifiers on a partial transcript.

    Reads the transcript from *since_byte*, runs the 4 allowlisted classifiers,
    and returns JSON-serializable output::

        {
            "findings": [...],
            "next_byte_offset": N,
            "classifiers_run": ["git_rm_usage", ...],
            "turns_read": N,
        }

    Safe to call on a transcript that is still being written (partial last line
    is skipped; next_byte_offset resumes from the last complete line).

    Classifiers that require a complete transcript (retro_skipped, preflight_skipped, etc.)
    are excluded — pass --allow-partial to acknowledge this limitation.
    """
    turns, next_offset = iter_turns_from_offset(transcript_path, since_byte)

    findings: list[dict] = []

    # 1. git_rm_usage — hard rule, no threshold
    #    classify_git_rm_usage() accepts Iterable[TranscriptTurn] directly
    for f in classify_git_rm_usage(iter(turns)):
        findings.append({
            "category": f.classifier,
            "severity": f.severity,
            "title": f"[live] {f.classifier} at turn {f.turn_index}: {f.detail[:120]}",
            "evidence": [f"turn={f.turn_index}", f"detail={f.detail[:120]}"],
            "live_mode": True,
        })

    # 2. forbidden_subagent_type — hard rule, no threshold
    #    Uses in-line state builder instead of classify_forbidden_subagent_type()
    #    (that function takes state dicts from _scan_transcripts, not TranscriptTurn)
    findings.extend(_live_forbidden_subagent_type(turns))

    # 3. wrong_premise_retries — live threshold: >= 8 retries
    findings.extend(_live_wrong_premise_retries(turns))

    # 4. tool_output_ignored — live threshold: 3+ consecutive ignored errors
    findings.extend(_live_tool_output_ignored(turns))

    return {
        "findings": findings,
        "next_byte_offset": next_offset,
        "classifiers_run": sorted(LIVE_ALLOWLIST),
        "turns_read": len(turns),
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def build_report(since: datetime, findings: list[dict], runs_analyzed: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "report_at": now.isoformat(),
        "window": {
            "since": since.isoformat(),
            "until": now.isoformat(),
        },
        "runs_analyzed": runs_analyzed,
        "findings": findings,
    }


def write_report(report: dict) -> tuple[Path, Path]:
    RUN_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    json_path = RUN_REPORTS_DIR / f"{date_str}.json"
    md_path = RUN_REPORTS_DIR / f"{date_str}.md"

    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    lines = [
        f"# Run Analysis Report -- {date_str}",
        "",
        f"**Window:** {report['window']['since']} -> {report['window']['until']}",
        f"**Runs analyzed:** {report['runs_analyzed']}",
        f"**Findings:** {len(report['findings'])}",
        "",
        "## Findings",
        "",
    ]
    if not report["findings"]:
        lines.append("_No issues found in this window._")
    else:
        by_sev = {"high": [], "medium": [], "low": []}
        for f_ in report["findings"]:
            by_sev.setdefault(f_.get("severity", "low"), []).append(f_)
        for sev in ("high", "medium", "low"):
            items = by_sev.get(sev, [])
            if items:
                lines.append(f"### {sev.upper()} ({len(items)})")
                lines.append("")
                for item in items:
                    lines.append(f"- **{item['category']}**: {item['title']}")
                    if item.get("suggested_discussion_title"):
                        lines.append(f"  - Suggested: _{item['suggested_discussion_title']}_")
                lines.append("")

    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    return json_path, md_path


def file_discussions(findings: list[dict], dry_run: bool = False) -> list[str]:
    """File one Discussion per high-severity category on the project repo."""
    repo_owner = REPO_OWNER
    repo_name = REPO_NAME

    # Resolve real IDs at runtime — never hardcode
    try:
        ids_result = subprocess.run(
            ["gh", "api", "graphql", "-f",
             f'query=query{{repository(owner:"{repo_owner}",name:"{repo_name}")'
             f'{{id discussionCategories(first:10){{nodes{{id name}}}}}}}}'],
            capture_output=True, text=True, timeout=15,
        )
        if ids_result.returncode != 0:
            print(f"[run-analyst] Could not resolve repo IDs: {ids_result.stderr[:200]}", file=sys.stderr)
            return []
        repo_data = json.loads(ids_result.stdout)["data"]["repository"]
        repo_id = repo_data["id"]
        cat_nodes = repo_data["discussionCategories"]["nodes"]
        cat_id = next((c["id"] for c in cat_nodes if c["name"] == "Ideas"), None)
        if not cat_id:
            print("[run-analyst] 'Ideas' discussion category not found", file=sys.stderr)
            return []
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"[run-analyst] Failed to resolve repo/category IDs: {exc}", file=sys.stderr)
        return []

    # Idempotency: fetch existing Discussion titles+bodies to avoid re-filing
    # Dedup strategy (in priority order):
    #   1. Stable marker  <!-- run-analyst:category=X --> present in body → skip (any STATUS)
    #   2. Legacy title-prefix match for Discussions without the marker (back-compat for #520/#521/#522)
    existing_nodes: list[dict] = []
    try:
        existing_result = subprocess.run(
            ["gh", "api", "graphql", "-f",
             f'query=query{{repository(owner:"{repo_owner}",name:"{repo_name}")'
             f'{{discussions(first:100){{nodes{{title body}}}}}}}}'],
            capture_output=True, text=True, timeout=15,
        )
        if existing_result.returncode == 0:
            existing_nodes = json.loads(existing_result.stdout)["data"]["repository"]["discussions"]["nodes"]
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError):
        pass  # non-fatal — proceed without idempotency check

    def _category_already_filed(category: str, title: str) -> bool:
        """Return True if a Discussion already tracks this category (any status)."""
        marker = f"<!-- run-analyst:category={category} -->"
        title_prefix = f"[Bug] run-analyst: {category}"
        for node in existing_nodes:
            if marker in node.get("body", ""):
                return True  # stable marker match
            # Legacy: Discussions auto-filed before the marker was introduced
            if node.get("title", "").startswith(title_prefix):
                return True
        return False

    # Group high-severity findings by category — one Discussion per category
    by_category: dict[str, list[dict]] = {}
    for f in findings:
        if f.get("severity") == "high":
            by_category.setdefault(f.get("category", "misc"), []).append(f)

    filed = []
    for category, group in list(by_category.items())[:3]:
        title = f"[Bug] run-analyst: {category} ({len(group)} instance{'s' if len(group) != 1 else ''})"
        if _category_already_filed(category, title):
            print(f"[run-analyst] Skipping {category!r} — Discussion already exists (any status)")
            continue
        evidence_lines = []
        for item in group[:10]:
            evidence_lines.extend(f"- {e}" for e in item.get("evidence", [])[:2])
        body = (
            f"<!-- STATUS:DISCUSSING SINCE:{datetime.now(timezone.utc).isoformat()} -->\n\n"
            f"## Background\n\n"
            f"Surfaced by run-analyst on {datetime.now(timezone.utc).date()}.\n\n"
            f"**Category:** {category}\n"
            f"**Severity:** high\n"
            f"**Instances:** {len(group)}\n\n"
            f"**Evidence:**\n"
            + ("\n".join(evidence_lines) or "- (no evidence recorded)")
            + "\n\n_This Discussion was auto-created by the run-analyst agent._"
            + f"\n\n<!-- run-analyst:category={category} -->"
        )
        if dry_run:
            print(f"[dry-run] Would file Discussion: {title!r}")
            filed.append(f"(dry-run) {title}")
            continue
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f",
                 f"query=mutation{{createDiscussion(input:{{repositoryId:{json.dumps(repo_id)},"
                 f"categoryId:{json.dumps(cat_id)},title:{json.dumps(title)},"
                 f"body:{json.dumps(body)}}}){{discussion{{number url}}}}}}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                try:
                    out = json.loads(result.stdout)
                    num = out["data"]["createDiscussion"]["discussion"]["number"]
                    print(f"[run-analyst] Filed Discussion #{num}: {title}")
                    filed.append(title)
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    print(f"[run-analyst] Could not parse mutation response: {exc}", file=sys.stderr)
            else:
                print(f"[run-analyst] FAILED to file Discussion {title!r}: {result.stderr[:300]}", file=sys.stderr)
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[run-analyst] Error filing Discussion {title!r}: {exc}", file=sys.stderr)
    return filed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry_text(entry: dict) -> str:
    data = entry.get("data")
    parts = [
        entry.get("message", ""),
        entry.get("body", ""),
        entry.get("content", ""),
        entry.get("text", ""),
        json.dumps(data) if data else "",
    ]
    return " ".join(str(p) for p in parts if p)


# _parse_ts (D#1753's string-timestamp parser) now lives in
# backend/loop_metrics_ts.py as parse_loop_metrics_ts, imported above as
# _parse_ts -- so stats_writer.loop_idle_ratio_24h and this module's
# load_loop_metrics share one implementation instead of each rolling their
# own (D#2315). Kept as a module-level name (not inlined at the call site)
# because backend/tests/test_run_analyst.py imports and patches it directly.


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    return (s[n // 2] + s[(n - 1) // 2]) / 2


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run-analyst: surface agent-run patterns")
    parser.add_argument("--since", default="7d", help="Look-back window, e.g. 7d, 24h (default: 7d)")
    parser.add_argument("--file-discussions", action="store_true",
                        help="File GitHub Discussions for high-severity findings (max 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without writing files or filing Discussions")
    parser.add_argument("--single-transcript", metavar="PATH",
                        help="Scan a single transcript file and print findings as JSON (self-observe gate mode). "
                             "≤10s p95 wall-clock. Returns findings list only — no report written.")

    # Live-mode flags (Discussion #574 PR-a)
    parser.add_argument("--live", action="store_true",
                        help="Live-mode: run the 4 allowlisted hard-rule classifiers on a "
                             "partial/in-flight transcript. Output: JSON {findings, next_byte_offset}. "
                             "Requires --transcript.")
    parser.add_argument("--transcript", metavar="PATH",
                        help="Path to a single .output transcript file (used with --live).")
    parser.add_argument("--since-byte", type=int, default=0,
                        help="Start reading transcript from this byte offset (used with --live). "
                             "Pass the next_byte_offset from the previous run to resume incrementally.")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Acknowledge that --live mode skips classifiers requiring a complete "
                             "transcript (retro_skipped, preflight_skipped, etc.). "
                             "This flag is implied by --live and is accepted but has no additional effect.")
    args = parser.parse_args()

    # --live mode: incremental classifier scan on a single transcript, output JSON
    if args.live:
        if not args.transcript:
            print("ERROR: --live requires --transcript <path>", file=sys.stderr)
            return 1
        result = run_live_mode(args.transcript, since_byte=args.since_byte)
        print(json.dumps(result, indent=2))
        return 0

    # --single-transcript mode: scan one file, print JSON findings, exit
    if args.single_transcript:
        findings = scan_single_transcript(args.single_transcript)
        print(json.dumps(findings, indent=2))
        return 0

    since = parse_since(args.since)

    feed_events = load_feed_events(since)
    loop_logs = load_loop_logs(since)
    audit = load_audit_trail(since)
    role_efficiency = load_role_efficiency()
    cost_tracker = load_cost_tracker()
    needs_fix_prs = load_needs_fix_prs()
    loop_metrics = load_loop_metrics(since)
    budget_data = load_budget_data()
    hook_events = load_hook_events(since)

    runs_analyzed = len(feed_events) + len(loop_logs) + len(audit)

    all_findings: list[dict] = []

    combined = feed_events + loop_logs + audit
    for i in range(0, max(1, len(combined)), CHUNK_SIZE):
        chunk = combined[i:i + CHUNK_SIZE]
        all_findings.extend(classify_failure_clusters(chunk, [], []))
        all_findings.extend(classify_spec_quality_flags(chunk, []))
        all_findings.extend(classify_tool_use_anomalies(chunk, [], []))
        all_findings.extend(classify_worktree_contamination(chunk, [], []))
        all_findings.extend(classify_hard_rule_violations(chunk, [], []))
        all_findings.extend(classify_agent_output_missing(chunk, [], []))

    all_findings.extend(classify_stalled_patterns(feed_events, since))
    all_findings.extend(classify_fix_cycle_loops(feed_events, audit, needs_fix_prs))
    all_findings.extend(classify_cost_outliers(role_efficiency, cost_tracker))
    all_findings.extend(classify_time_anomalies(role_efficiency, feed_events))

    # New Phase A classifiers (Discussion #478)
    all_findings.extend(classify_test_coverage_gap(feed_events, audit))
    all_findings.extend(classify_missing_post_agent_hook(feed_events, audit))
    all_findings.extend(classify_token_burn_no_output(feed_events, audit))
    all_findings.extend(classify_discussion_respun_n_times(feed_events, audit))
    all_findings.extend(classify_hook_event_spam(hook_events))
    all_findings.extend(classify_transcript_repetition(feed_events, loop_logs))
    all_findings.extend(classify_spec_impl_semantic_gap(feed_events, audit, needs_fix_prs))
    all_findings.extend(classify_branch_drift(feed_events))
    all_findings.extend(classify_stale_snapshot_consumption(loop_metrics, feed_events))
    all_findings.extend(classify_budget_cap_proximity(budget_data))
    all_findings.extend(classify_pre_spawn_check_missing(feed_events, audit))

    # Phase A.2 transcript classifiers (Discussion #486)
    since_seconds = int((datetime.now(timezone.utc) - since).total_seconds())
    transcript_states = _scan_transcripts(since_seconds=since_seconds)
    all_findings.extend(classify_wrong_premise_retries(transcript_states))
    all_findings.extend(classify_forbidden_subagent_type(transcript_states))
    all_findings.extend(classify_team_lead_self_edit(transcript_states))
    all_findings.extend(classify_auth_leak_risk(transcript_states))
    all_findings.extend(classify_permission_seeking(transcript_states))
    all_findings.extend(classify_repeated_file_reads(transcript_states))

    # Phase A.3 transcript classifiers (Discussion #511)
    all_findings.extend(_run_phase_a3_classifiers(since_seconds=since_seconds))

    # Phase A.4 transcript classifiers (Discussion #523)
    all_findings.extend(_run_phase_a4_classifiers(since_seconds=since_seconds))

    # Phase A.5: self-observe gate classifiers (Discussion #531)
    all_findings.extend(_run_phase_a5_classifiers(since_seconds=since_seconds))

    # Phase A.6: worktree isolation classifiers (Discussion #592)
    all_findings.extend(_run_phase_a6_classifiers(since_seconds=since_seconds))

    # Phase A.7: sleep-retry-loop classifiers (Discussion #592 PR-b)
    all_findings.extend(_run_phase_a7_classifiers(since_seconds=since_seconds))

    # Phase A.8: stale_rebase + gate_check_skipped classifiers (Discussion #655)
    all_findings.extend(_run_phase_a8_classifiers(since_seconds=since_seconds))

    seen_keys: set[tuple] = set()
    deduped: list[dict] = []
    for f_ in all_findings:
        key = (f_["category"], f_["title"])
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(f_)
    all_findings = deduped

    sev_order = {"high": 0, "medium": 1, "low": 2}
    all_findings.sort(key=lambda f_: sev_order.get(f_.get("severity", "low"), 2))

    report = build_report(since, all_findings, runs_analyzed)

    if args.dry_run:
        print(json.dumps(report, indent=2))
        return 0

    json_path, md_path = write_report(report)
    print(f"Report written: {json_path}")
    print(f"Summary:        {md_path}")
    print(f"Runs analyzed:  {runs_analyzed}")
    print(f"Findings:       {len(all_findings)}")

    if args.file_discussions and all_findings:
        # Generic high-severity filing (existing behaviour)
        filed = file_discussions(all_findings, dry_run=args.dry_run)
        if filed:
            print(f"Discussions filed: {len(filed)}")

        # Dedicated pipeline for wrote_outside_worktree hits (Discussion #592 PR-c)
        wow_hits = [
            f for f in all_findings
            if f.get("category") == "wrote_outside_worktree"
            and f.get("severity", "low") in ("high", "medium")
        ]
        if wow_hits:
            try:
                # Build hit dicts for analyst_bug_filer from run_analyst finding dicts.
                # The A.6 findings use 'evidence' list; extract agent_id and detail from there.
                from analyst_bug_filer import file_wrote_outside_worktree_hits
                normalized: list[dict] = []
                for finding in wow_hits:
                    evidence = finding.get("evidence", [])
                    agent_id = ""
                    detail = ""
                    for ev in evidence:
                        if ev.startswith("agent="):
                            agent_id = ev[len("agent="):]
                        elif ev.startswith("detail="):
                            detail = ev[len("detail="):]
                    normalized.append({
                        "classifier": "wrote_outside_worktree",
                        "severity": finding.get("severity", "high"),
                        "agent_id": agent_id,
                        "detail": detail,
                    })
                wow_results = file_wrote_outside_worktree_hits(
                    normalized, dry_run=args.dry_run
                )
                filed_wow = [r for r in wow_results if r.get("filed")]
                if filed_wow:
                    print(f"wrote_outside_worktree Discussions filed: {len(filed_wow)}")
            except ImportError:
                print(
                    "[run-analyst] analyst_bug_filer not found — skipping worktree-isolation auto-filing",
                    file=sys.stderr,
                )

    # PRESUM: pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
