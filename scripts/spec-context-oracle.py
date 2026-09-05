#!/usr/bin/env python3
"""spec-context-oracle.py — empirical-memory context oracle for SPEC_READY transition.

Reads a Discussion body, parses file paths / function names / D#N / PR#N references,
queries existing data sources, and posts a single "Empirical context (auto-generated)"
comment on the Discussion.

Usage:
    python3 scripts/spec-context-oracle.py <discussion_num> [--max-duration 10] [--dry-run]

Budget: 10s wall-clock maximum. Sections with zero findings are suppressed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Ensure repo root is in sys.path for backend imports
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend import state_paths as _state_paths  # noqa: E402
from backend._repo import REPO  # noqa: E402

# GraphQL wants the owner and name halves separately. Two queries below used
# to spell them out as literals — and, being the pre-rename slug, they kept
# working only through GitHub's rename redirect, so a wrong target would have
# resolved silently rather than erroring. Derived from REPO instead so both
# queries follow whatever backend._repo resolves for this checkout.
_REPO_OWNER, _REPO_NAME = REPO.split("/", 1)

# ---------------------------------------------------------------------------
# STATE_DIR / AUDIT_LOG / STATS_DB — resolved at call time (D#1810)
# ---------------------------------------------------------------------------
# These used to be `from backend.state_paths import STATE_DIR, AUDIT_LOG,
# STATS_DB` at module scope, which froze each value at import time and
# defeated a later AUTONOMOUS_TEAM_STATE_DIR override. Module __getattr__
# (PEP 562) makes external access (`oracle.STATE_DIR`) resolve fresh on every
# read, UNLESS a caller — a test, typically — assigns the name directly
# (`oracle.STATE_DIR = tmp_path`), which shadows __getattr__ exactly like any
# other module attribute. `_attr()` routes this script's own internal
# references through the same globals-first-else-resolve-fresh logic so both
# call sites see one consistent value. It checks `globals()` rather than
# going through `getattr(sys.modules[__name__], name)` because tests load
# this script via `importlib.util.spec_from_file_location(...)` without
# registering it in sys.modules — `globals()` always works regardless of how
# the module was loaded, since it's the same dict as the module's __dict__.

_ORACLE_RESOLVERS = {
    "STATE_DIR": lambda: _state_paths.STATE_DIR,
    "AUDIT_LOG": lambda: _state_paths.AUDIT_LOG,
    "STATS_DB": lambda: _state_paths.STATS_DB,
}


def _resolve_oracle_attr(name: str):
    resolver = _ORACLE_RESOLVERS.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()


def __getattr__(name: str):
    return _resolve_oracle_attr(name)


def _attr(name: str):
    if name in globals():
        return globals()[name]
    return _resolve_oracle_attr(name)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RETROS_FILE = _REPO_ROOT / ".autonomous-team" / "agent-retros.jsonl"
_SUB_TIMEOUT = 2  # seconds per subquery
_MAX_DISCUSSION_REFS = 5
_MAX_FILE_REFS = 10
_MAX_SYMBOL_REFS = 5
_MAX_GIT_LOG_ENTRIES = 10


# ---------------------------------------------------------------------------
# Budget context manager
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self._incomplete = False

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            self._incomplete = True
            raise BudgetExceeded("oracle budget exceeded")

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def incomplete(self) -> bool:
        return self._incomplete

    def set_incomplete(self) -> None:
        self._incomplete = True


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------

def parse_references(body: str) -> dict[str, list]:
    """Extract file paths, D#N refs, PR#N refs, and backticked symbols."""
    # File paths: relative paths like backend/foo.py, scripts/bar.sh, tui/src/X.tsx
    file_pattern = re.compile(
        r"(?<!\w)(?:backend|scripts|tests|tui|dashboard|hooks|templates|wiki|archive)"
        r"(?:/[A-Za-z0-9_./-]+)+(?:\.[A-Za-z]{1,6})?"
    )
    files = list(dict.fromkeys(file_pattern.findall(body)))[:_MAX_FILE_REFS]

    # D#N references
    dnums = list(dict.fromkeys(int(m) for m in re.findall(r"D#(\d+)", body)))[:_MAX_DISCUSSION_REFS]

    # PR#N references
    prnums = list(dict.fromkeys(int(m) for m in re.findall(r"PR\s*#(\d+)", body)))[:_MAX_DISCUSSION_REFS]

    # Backticked identifiers that look like function names: `foo()`, `_bar`, `camelCase`
    symbol_pattern = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\(\))?)`")
    # Exclude common prose words
    EXCLUDE = {
        "true", "false", "null", "None", "True", "False", "int", "str", "dict",
        "list", "set", "tuple", "bool", "float", "bytes", "git", "python3",
        "grep", "rg", "jq", "cat", "bash", "json", "yaml", "toml",
    }
    symbols = [
        s.rstrip("()") for s in symbol_pattern.findall(body)
        if s.rstrip("()") not in EXCLUDE and len(s) >= 3
    ]
    symbols = list(dict.fromkeys(symbols))[:_MAX_SYMBOL_REFS]

    return {"files": files, "dnums": dnums, "prnums": prnums, "symbols": symbols}


# ---------------------------------------------------------------------------
# Run subprocess with timeout
# ---------------------------------------------------------------------------

def _run_with_timeout(cmd: list[str], timeout: float, cwd: str | None = None) -> tuple[str, bool]:
    """Run command with timeout. Returns (stdout, timed_out)."""
    # Honour SPEC_ORACLE_FAULT for testing
    fault = os.environ.get("SPEC_ORACLE_FAULT", "")
    if fault == "slow_git" and cmd[0] == "git":
        time.sleep(5)  # injected fault

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or str(_REPO_ROOT),
        )
        return result.stdout, False
    except subprocess.TimeoutExpired:
        return "", True


# ---------------------------------------------------------------------------
# File context: git log + also_called_by
# ---------------------------------------------------------------------------

def gather_file_context(files: list[str], budget: Budget) -> list[dict]:
    results = []
    for path in files:
        budget.check()
        entry: dict[str, Any] = {"path": path, "partial": False}

        # git log
        timeout = min(_SUB_TIMEOUT, budget.remaining())
        stdout, timed_out = _run_with_timeout(
            ["git", "log", f"-n{_MAX_GIT_LOG_ENTRIES}", "--format=%H %as %s", "--", path],
            timeout=timeout,
        )
        if timed_out:
            entry["partial"] = True
            entry["reason"] = "timeout"
            budget.set_incomplete()
            results.append(entry)
            continue

        commits = []
        for line in stdout.strip().splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3:
                sha, date, subject = parts
                commits.append({"sha": sha[:8], "date": date, "subject": subject})
        entry["recent_commits"] = commits

        # also_called_by: shell scripts that invoke this path
        timeout = min(_SUB_TIMEOUT, budget.remaining())
        stdout2, timed_out2 = _run_with_timeout(
            ["grep", "-rIl", "--include=*.sh", path, "scripts/"],
            timeout=timeout,
        )
        if timed_out2:
            entry["also_called_by"] = []
        else:
            callers = [line.strip() for line in stdout2.strip().splitlines() if line.strip()]
            entry["also_called_by"] = callers

        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Fix-cycle context: DuckDB agent_run query
# ---------------------------------------------------------------------------

def _extract_pr_numbers_from_log(log_output: str) -> list[int]:
    """Extract PR numbers from git log subjects (pattern: (#NNNN))."""
    return [int(m) for m in re.findall(r"\(#(\d+)\)", log_output)]


def gather_fix_cycles(files: list[dict], budget: Budget) -> dict[str, dict]:
    """Query stats.duckdb agent_run for fix_cycles and last_pr per file path.

    For each file, uses git log to find PR numbers that touched the file, then
    queries agent_run WHERE pr IN (pr_list) to count needs-fix verdicts (fix_cycles)
    and the most recent PR number (last_pr).

    Returns a mapping of file_path -> {"fix_cycles": int, "last_pr": int|None}.
    Wraps DuckDB access inside the budget; returns empty dict on any error.
    """
    budget.check()
    if not files:
        return {}

    try:
        import duckdb  # noqa: PLC0415 — optional dependency
    except ImportError:
        return {}

    stats_db = _attr("STATS_DB")
    if not stats_db.exists():
        return {}

    result: dict[str, dict] = {}

    try:
        con = duckdb.connect(str(stats_db), read_only=True)
    except Exception:
        return {}

    try:
        for entry in files:
            budget.check()
            path = entry.get("path", "")
            if not path:
                continue

            # Use git log already fetched in entry["recent_commits"] when available,
            # otherwise re-query with a short timeout.
            pr_numbers: list[int] = []
            commits = entry.get("recent_commits", [])
            if commits:
                # Extract PR numbers from commit subjects already gathered
                for c in commits:
                    pr_numbers.extend(_extract_pr_numbers_from_log(c.get("subject", "")))
            else:
                # Re-run git log with a short timeout to get PR numbers
                timeout = min(_SUB_TIMEOUT, budget.remaining())
                stdout, timed_out = _run_with_timeout(
                    ["git", "log", f"-n{_MAX_GIT_LOG_ENTRIES}", "--format=%s", "--", path],
                    timeout=timeout,
                )
                if not timed_out:
                    pr_numbers.extend(_extract_pr_numbers_from_log(stdout))

            pr_numbers = list(dict.fromkeys(pr_numbers))  # deduplicate

            fix_cycles = 0
            last_pr: int | None = None

            if pr_numbers:
                try:
                    placeholders = ", ".join(str(p) for p in pr_numbers)
                    # Count needs-fix verdicts (code-reviewer) for these PRs
                    row = con.execute(
                        f"SELECT COUNT(*) FROM agent_run "
                        f"WHERE pr IN ({placeholders}) AND role = 'code-reviewer' AND verdict = 'needs-fix'"
                    ).fetchone()
                    if row:
                        fix_cycles = int(row[0])

                    # Find the last (max) PR number that has any agent_run entry
                    pr_row = con.execute(
                        f"SELECT MAX(pr) FROM agent_run WHERE pr IN ({placeholders})"
                    ).fetchone()
                    if pr_row and pr_row[0] is not None:
                        last_pr = int(pr_row[0])
                    else:
                        # Fall back to max from git log
                        last_pr = max(pr_numbers) if pr_numbers else None
                except Exception:
                    pass
            elif pr_numbers == [] and commits:
                # No PR numbers found in git log but file has commits — last_pr stays None
                pass

            result[path] = {"fix_cycles": fix_cycles, "last_pr": last_pr}
    except BudgetExceeded:
        budget.set_incomplete()
    finally:
        try:
            con.close()
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Discussion context
# ---------------------------------------------------------------------------

def gather_discussion_context(dnums: list[int], budget: Budget) -> list[dict]:
    results = []
    for dnum in dnums:
        budget.check()
        timeout = min(_SUB_TIMEOUT, budget.remaining())
        try:
            proc = subprocess.run(
                [sys.executable, str(_REPO_ROOT / "backend" / "discussion_cache.py"), "get-body", str(dnum)],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(_REPO_ROOT),
            )
            body = proc.stdout
        except subprocess.TimeoutExpired:
            budget.set_incomplete()
            results.append({"number": dnum, "partial": True, "reason": "timeout"})
            continue

        # Parse STATUS and title from body
        status_match = re.search(r"<!--\s*STATUS:(\w+)", body)
        status = status_match.group(1) if status_match else "UNKNOWN"

        # Extract PR number from STATUS line
        pr_match = re.search(r"PR:#(\d+)", body)
        related_prs = [int(pr_match.group(1))] if pr_match else []

        # Try to get title from GraphQL (cached)
        title = _get_discussion_title(dnum, budget)

        results.append({
            "number": dnum,
            "title": title,
            "status": status,
            "related_prs": related_prs,
            "partial": False,
        })
    return results


def _get_discussion_title(dnum: int, budget: Budget) -> str:
    timeout = min(_SUB_TIMEOUT, budget.remaining())
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query=query {{ repository(owner:\"{_REPO_OWNER}\", name:\"{_REPO_NAME}\") {{ discussion(number:{dnum}) {{ title }} }} }}"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(_REPO_ROOT),
        )
        data = json.loads(proc.stdout)
        return data["data"]["repository"]["discussion"]["title"]
    except Exception:
        return f"Discussion #{dnum}"


# ---------------------------------------------------------------------------
# Symbol context: grep search
# ---------------------------------------------------------------------------

def gather_symbol_context(symbols: list[str], budget: Budget) -> list[dict]:
    results = []
    for sym in symbols:
        budget.check()
        timeout = min(_SUB_TIMEOUT, budget.remaining())

        # Search for definition pattern
        def_pattern = rf"def {re.escape(sym)}|function {re.escape(sym)}|const {re.escape(sym)}\s*="
        stdout, timed_out = _run_with_timeout(
            ["grep", "-rIn", "--include=*.py", "--include=*.ts",
             "-E", def_pattern, "backend/", "scripts/"],
            timeout=timeout,
        )
        if timed_out:
            budget.set_incomplete()
            results.append({"name": sym, "partial": True, "reason": "timeout"})
            continue

        defined_in = None
        lines = stdout.strip().splitlines()
        if lines:
            first = lines[0]
            parts = first.split(":", 2)
            if len(parts) >= 2:
                defined_in = f"{parts[0]}:{parts[1]}"

        # Count callers
        timeout2 = min(_SUB_TIMEOUT, budget.remaining())
        stdout2, timed_out2 = _run_with_timeout(
            ["grep", "-rIl", "--include=*.py", "--include=*.ts", "--include=*.sh",
             sym, "backend/", "scripts/"],
            timeout=timeout2,
        )
        callers_count = len(stdout2.strip().splitlines()) if not timed_out2 else 0

        results.append({
            "name": sym,
            "defined_in": defined_in,
            "callers_count": callers_count,
            "partial": False,
        })
    return results


# ---------------------------------------------------------------------------
# Classifier signals from agent-retros.jsonl
# ---------------------------------------------------------------------------

def gather_classifier_signals(file_results: list[dict], budget: Budget) -> dict[str, int]:
    """Count classifier occurrences in retros over last 30 days."""
    budget.check()
    if not RETROS_FILE.exists():
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    counts: dict[str, int] = {}

    try:
        with RETROS_FILE.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = entry.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    continue

                if ts < cutoff:
                    continue

                classifier = entry.get("classifier", "")
                if classifier:
                    counts[classifier] = counts.get(classifier, 0) + 1

    except OSError:
        pass

    return counts


# ---------------------------------------------------------------------------
# Audit trail search for file touches
# ---------------------------------------------------------------------------

def gather_audit_signals(files: list[dict], budget: Budget) -> list[dict]:
    """Scan audit.jsonl for entries referencing the given files."""
    budget.check()
    audit_log = _attr("AUDIT_LOG")
    if not audit_log.exists():
        return []

    file_paths = [f["path"] for f in files]
    if not file_paths:
        return []

    # Also check rotated log
    audit_files = [audit_log]
    rotated = audit_log.parent / "audit.jsonl.1"
    if rotated.exists():
        audit_files.insert(0, rotated)

    hits: list[dict] = []
    seen_keys: set[str] = set()

    for audit_path in audit_files:
        try:
            with audit_path.open() as f:
                for line in f:
                    try:
                        budget.check()
                    except BudgetExceeded:
                        return hits[:10]
                    if len(hits) >= 20:
                        break
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    for fp in file_paths:
                        if fp in line:
                            key = f"{entry.get('ts','')}-{entry.get('seq','0')}"
                            if key not in seen_keys:
                                seen_keys.add(key)
                                hits.append({
                                    "ts": entry.get("ts"),
                                    "source": entry.get("source"),
                                    "action": entry.get("action"),
                                    "file": fp,
                                    "actor": entry.get("actor"),
                                })
                            break
        except OSError:
            pass

    return hits[:10]


# ---------------------------------------------------------------------------
# Artifact building
# ---------------------------------------------------------------------------

def build_artifact(
    discussion: int,
    files: list[dict],
    discussions_referenced: list[dict],
    symbols: list[dict],
    classifier_signals: dict[str, int],
    audit_signals: list[dict],
    incomplete: bool,
    duration_ms: int,
    sources_consulted: list[str],
) -> dict:
    return {
        "discussion": discussion,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "discussions_referenced": discussions_referenced,
        "symbols": symbols,
        "classifier_signals": classifier_signals,
        "audit_signals": audit_signals,
        "incomplete": incomplete,
        "duration_ms": duration_ms,
        "sources_consulted": sources_consulted,
        "suppressed": False,
    }


def has_findings(artifact: dict) -> bool:
    return bool(
        artifact["files"]
        or artifact["discussions_referenced"]
        or artifact["symbols"]
        or artifact["classifier_signals"]
        or artifact["audit_signals"]
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(artifact: dict, discussion: int, incomplete: bool = False) -> str:
    header = "**Empirical context (incomplete)**" if incomplete else "**Empirical context (auto-generated)**"
    lines = [header, ""]

    # Files section
    if artifact["files"]:
        lines.append("**Files referenced — prior touches:**")
        for f in artifact["files"]:
            path = f["path"]
            commits = f.get("recent_commits", [])
            callers = f.get("also_called_by", [])
            if f.get("partial"):
                lines.append(f"- `{path}` — _(query timed out)_")
                continue
            if commits:
                last = commits[0]
                lines.append(
                    f"- `{path}` — last touched {last['date']}: {last['subject'][:80]}"
                )
            else:
                lines.append(f"- `{path}` — no recent commits found")
            if callers:
                caller_list = ", ".join(f"`{c}`" for c in callers[:3])
                lines.append(f"  - Also invoked by: {caller_list}")
        lines.append("")

    # Discussions referenced
    if artifact["discussions_referenced"]:
        lines.append("**Discussions referenced:**")
        for d in artifact["discussions_referenced"]:
            num = d["number"]
            title = d.get("title", f"Discussion #{num}")
            status = d.get("status", "UNKNOWN")
            related = d.get("related_prs", [])
            pr_str = f" (PR #{related[0]})" if related else ""
            if d.get("partial"):
                lines.append(f"- D#{num} — _(query timed out)_")
            else:
                lines.append(f"- D#{num} — {status}{pr_str}: {title[:80]}")
        lines.append("")

    # Symbols
    if artifact["symbols"]:
        lines.append("**Symbols referenced:**")
        for s in artifact["symbols"]:
            name = s["name"]
            if s.get("partial"):
                lines.append(f"- `{name}` — _(query timed out)_")
            elif s.get("defined_in"):
                lines.append(
                    f"- `{name}` — defined at `{s['defined_in']}`; "
                    f"referenced in {s.get('callers_count', 0)} file(s)"
                )
            else:
                lines.append(f"- `{name}` — no definition found in backend/scripts")
        lines.append("")

    # Classifier signals
    if artifact["classifier_signals"]:
        lines.append("**Classifier signals (last 30 days):**")
        for classifier, count in sorted(artifact["classifier_signals"].items(), key=lambda x: -x[1]):
            lines.append(f"- `{classifier}`: {count} occurrence(s)")
        lines.append("")

    # Audit signals
    if artifact["audit_signals"]:
        lines.append("**Recent audit activity touching these files:**")
        for sig in artifact["audit_signals"][:5]:
            ts = (sig.get("ts") or "")[:10]
            actor = sig.get("actor", "unknown")
            file_ = sig.get("file", "")
            action = sig.get("action", "")
            lines.append(f"- {ts} {actor} {action} `{file_}`")
        lines.append("")

    # Footer
    duration_s = artifact["duration_ms"] / 1000
    state_dir = str(_attr("STATE_DIR"))
    incomplete_note = " (incomplete — budget exceeded)" if incomplete else ""
    lines.append(
        f"_Source: `scripts/spec-context-oracle.py {discussion}`{incomplete_note}. "
        f"Ran in {duration_s:.1f}s. "
        f"Raw data: `{state_dir}/spec-context/{discussion}.json`._"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Archive artifact
# ---------------------------------------------------------------------------

def write_artifact(artifact: dict, discussion: int) -> Path:
    spec_context_dir = _attr("STATE_DIR") / "spec-context"
    spec_context_dir.mkdir(parents=True, exist_ok=True)
    out_path = spec_context_dir / f"{discussion}.json"
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    return out_path


# ---------------------------------------------------------------------------
# Post Discussion comment
# ---------------------------------------------------------------------------

def get_discussion_node_id(discussion: int) -> str | None:
    """Get the GraphQL node ID for a Discussion number."""
    try:
        result = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query=query {{ repository(owner:\"{_REPO_OWNER}\", name:\"{_REPO_NAME}\") {{ discussion(number:{discussion}) {{ id }} }} }}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(_REPO_ROOT),
        )
        data = json.loads(result.stdout)
        return data["data"]["repository"]["discussion"]["id"]
    except Exception as e:
        print(f"[oracle] Failed to get discussion node ID: {e}", file=sys.stderr)
        return None


def post_comment(discussion: int, body: str) -> bool:
    """Post a comment on the Discussion. Returns True on success."""
    node_id = get_discussion_node_id(discussion)
    if not node_id:
        return False

    try:
        result = subprocess.run(
            ["gh", "api", "graphql",
             "-f", "query=mutation($id:ID!, $body:String!) { addDiscussionComment(input:{discussionId:$id, body:$body}) { comment { id } } }",
             "-f", f"id={node_id}",
             "-f", f"body={body}"],
            capture_output=True, text=True, timeout=15,
            cwd=str(_REPO_ROOT),
        )
        if result.returncode != 0:
            print(f"[oracle] Failed to post comment: {result.stderr}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[oracle] Failed to post comment: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Audit trail append
# ---------------------------------------------------------------------------

def append_audit_event(
    discussion: int,
    duration_ms: int,
    found_files: int,
    found_discussions: int,
    found_symbols: int,
    incomplete: bool,
    suppressed: bool,
) -> None:
    """Append one line to audit.jsonl."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "spec_oracle_run",
        "discussion": discussion,
        "duration_ms": duration_ms,
        "found_files": found_files,
        "found_discussions": found_discussions,
        "found_symbols": found_symbols,
        "incomplete": incomplete,
        "suppressed": suppressed,
    }

    try:
        with _attr("AUDIT_LOG").open("a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        print(f"[oracle] Failed to write audit event: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Empirical context oracle for SPEC_READY transition")
    parser.add_argument("discussion", type=int, help="Discussion number")
    parser.add_argument("--max-duration", type=float, default=10.0, help="Wall-clock budget in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Do not post comment; print to stdout")
    args = parser.parse_args()

    start = time.monotonic()
    budget = Budget(args.max_duration)

    # Read discussion body
    try:
        proc = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "discussion_cache.py"),
             "get-body", str(args.discussion)],
            capture_output=True, text=True, timeout=10,
            cwd=str(_REPO_ROOT),
        )
        body = proc.stdout
    except Exception as e:
        print(f"[oracle] Failed to read Discussion #{args.discussion}: {e}", file=sys.stderr)
        return 1

    if not body.strip():
        print(f"[oracle] Discussion #{args.discussion} has empty body — suppressing.", file=sys.stderr)
        return 0

    # Parse references
    parsed = parse_references(body)
    sources_consulted = ["discussion_body"]

    # Gather contexts with budget
    files: list[dict] = []
    discussions_referenced: list[dict] = []
    symbols: list[dict] = []
    classifier_signals: dict[str, int] = {}
    audit_signals: list[dict] = []

    try:
        if parsed["files"]:
            sources_consulted.append("git_log")
            files = gather_file_context(parsed["files"], budget)

        if parsed["dnums"]:
            sources_consulted.append("discussion_cache")
            discussions_referenced = gather_discussion_context(parsed["dnums"], budget)

        if parsed["symbols"]:
            sources_consulted.append("grep_symbols")
            symbols = gather_symbol_context(parsed["symbols"], budget)

        if parsed["files"]:
            sources_consulted.append("stats_duckdb")
            fix_cycle_data = gather_fix_cycles(files, budget)
            # Merge fix_cycles and last_pr into each file entry
            for f in files:
                fc = fix_cycle_data.get(f["path"], {})
                f["fix_cycles"] = fc.get("fix_cycles", 0)
                f["last_pr"] = fc.get("last_pr", None)

        sources_consulted.append("agent_retros")
        classifier_signals = gather_classifier_signals(files, budget)

        if parsed["files"]:
            sources_consulted.append("audit_jsonl")
            audit_signals = gather_audit_signals(files, budget)

    except BudgetExceeded:
        budget.set_incomplete()
        print(f"[oracle] Budget exceeded — returning partial results", file=sys.stderr)

    duration_ms = int((time.monotonic() - start) * 1000)
    incomplete = budget.incomplete

    artifact = build_artifact(
        discussion=args.discussion,
        files=files,
        discussions_referenced=discussions_referenced,
        symbols=symbols,
        classifier_signals=classifier_signals,
        audit_signals=audit_signals,
        incomplete=incomplete,
        duration_ms=duration_ms,
        sources_consulted=sources_consulted,
    )

    # Check if there are any findings
    if not has_findings(artifact):
        artifact["suppressed"] = True
        print(f"[oracle] No findings for D#{args.discussion} — suppressing comment.", file=sys.stderr)

    # Write artifact to disk regardless
    out_path = write_artifact(artifact, args.discussion)
    print(f"[oracle] Artifact written: {out_path}", file=sys.stderr)

    # Append audit event
    append_audit_event(
        discussion=args.discussion,
        duration_ms=duration_ms,
        found_files=len(files),
        found_discussions=len(discussions_referenced),
        found_symbols=len(symbols),
        incomplete=incomplete,
        suppressed=artifact["suppressed"],
    )

    # Render and post comment (if findings exist)
    if not artifact["suppressed"]:
        md = render_markdown(artifact, args.discussion, incomplete=incomplete)

        if args.dry_run:
            print(md)
        else:
            success = post_comment(args.discussion, md)
            if success:
                print(f"[oracle] Comment posted on D#{args.discussion}", file=sys.stderr)
            else:
                print(f"[oracle] Failed to post comment on D#{args.discussion}", file=sys.stderr)
                return 1

    total_duration = time.monotonic() - start
    print(f"[oracle] Done in {total_duration:.2f}s (incomplete={incomplete})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
