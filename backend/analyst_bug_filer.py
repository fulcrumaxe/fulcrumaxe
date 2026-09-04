"""analyst_bug_filer.py — auto-file [Bug] Discussions from run_analyst classifier hits.

Wires the wrote_outside_worktree classifier (and future classifiers) to GitHub
Discussion auto-filing. Modeled after backend/browser_tour_filer.py.

Usage:
    # Dry-run: print body and placeholder URL, exit 0
    python3 backend/analyst_bug_filer.py --hit '{"classifier":"wrote_outside_worktree",...}' --dry-run

    # Real run: file Discussion on GitHub
    python3 backend/analyst_bug_filer.py --hit '{"classifier":"wrote_outside_worktree",...}' --apply

The --hit JSON dict must contain at minimum:
    classifier  : str  — "wrote_outside_worktree" (others silently skipped)
    severity    : str  — "high" | "medium" | "low"
    agent_id    : str  — unique agent identifier from transcript path
    detail      : str  — human-readable description of the violation
    file_path   : str  — (optional) file that was written outside worktree
    branch      : str  — (optional) branch HEAD context

Idempotency: the filed Discussion body contains a marker:
    <!-- analyst-bug:wrote_outside_worktree:{agent_id} -->
A second run with the same agent_id is a no-op (returns None / prints "skipped").
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a script from repo root: `python3 backend/analyst_bug_filer.py`
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend._repo import REPO, REPO_OWNER, REPO_NAME

# Severity levels that trigger auto-filing
FILING_SEVERITIES = {"high", "medium"}

# Classifiers handled by this module
HANDLED_CLASSIFIERS = frozenset({"wrote_outside_worktree"})

# Placeholder URL returned in dry-run mode
DRY_RUN_URL = f"https://github.com/{REPO}/discussions/DRY-RUN"

_CATEGORY_CACHE: dict[str, str] = {}
_REPO_ID_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _get_repo_id() -> str:
    """Return the repository node ID (cached per process)."""
    if "id" in _REPO_ID_CACHE:
        return _REPO_ID_CACHE["id"]
    query = (
        f'query{{repository(owner:"{REPO_OWNER}",name:"{REPO_NAME}"){{id}}}}'
    )
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            rid = data["data"]["repository"]["id"]
            _REPO_ID_CACHE["id"] = rid
            return rid
    except Exception:
        pass
    return ""


def _get_category_id(category_name: str = "General") -> str:
    """Return the Discussion category node ID (cached per process)."""
    if category_name in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[category_name]
    query = (
        f'query{{repository(owner:"{REPO_OWNER}",name:"{REPO_NAME}")'
        f'{{discussionCategories(first:20){{nodes{{id name}}}}}}}}'
    )
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            nodes = data["data"]["repository"]["discussionCategories"]["nodes"]
            for node in nodes:
                _CATEGORY_CACHE[node["name"]] = node["id"]
            if category_name in _CATEGORY_CACHE:
                return _CATEGORY_CACHE[category_name]
            if "General" in _CATEGORY_CACHE:
                return _CATEGORY_CACHE["General"]
    except Exception:
        pass
    return ""


def _search_existing_discussions(marker: str) -> bool:
    """Return True if any Discussion body already contains the given marker string."""
    query = (
        f'query{{repository(owner:"{REPO_OWNER}",name:"{REPO_NAME}")'
        f'{{discussions(first:100){{nodes{{body}}}}}}}}'
    )
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            nodes = data["data"]["repository"]["discussions"]["nodes"]
            for node in nodes:
                if marker in node.get("body", ""):
                    return True
    except Exception:
        pass
    return False


def _create_discussion(title: str, body: str, repo_id: str, category_id: str) -> str | None:
    """Create a Discussion and return its URL, or None on failure."""
    mutation = (
        "mutation CreateDiscussion($repoId:ID!,$catId:ID!,$title:String!,$body:String!){"
        "createDiscussion(input:{repositoryId:$repoId,categoryId:$catId,"
        "title:$title,body:$body}){discussion{url number}}}"
    )
    try:
        result = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", f"query={mutation}",
                "-f", f"repoId={repo_id}",
                "-f", f"catId={category_id}",
                "-f", f"title={title}",
                "-f", f"body={body}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            disc = data["data"]["createDiscussion"]["discussion"]
            return disc["url"]
        else:
            print(
                f"[analyst_bug_filer] GraphQL error: {result.stderr[:200]}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[analyst_bug_filer] Exception filing Discussion: {exc}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Body builder
# ---------------------------------------------------------------------------

def _build_body(hit: dict[str, Any], marker: str) -> str:
    """Build the Discussion body for a wrote_outside_worktree hit."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent_id = hit.get("agent_id", "unknown")
    file_path = hit.get("file_path", hit.get("detail", ""))
    branch = hit.get("branch", "")
    detail = hit.get("detail", "")
    severity = hit.get("severity", "high")

    # Extract file path from detail string if not explicitly provided
    if not file_path:
        m = re.search(r"'(/[^']+)'|\"(/[^\"]+)\"", detail)
        if m:
            file_path = m.group(1) or m.group(2)

    lines = [
        f"<!-- STATUS:DISCUSSING SINCE:{now} -->",
        "",
        "## Summary",
        "",
        f"A worktree-isolated agent wrote to the main repository path instead of its worktree.",
        "",
        f"**Severity:** {severity}",
        f"**Agent ID:** `{agent_id}`",
    ]
    if file_path:
        lines.append(f"**File written outside worktree:** `{file_path}`")
    if branch:
        lines.append(f"**Branch HEAD context:** `{branch}`")
    lines += [
        "",
        "## Finding",
        "",
        detail or "Edit/Write tool call targeted main-repo path instead of worktree.",
        "",
        "## How to reproduce",
        "",
        "1. Run `python3 backend/run_analyst.py --since=24h` to surface recent violations.",
        f"2. Filter findings for `classifier=wrote_outside_worktree` and `agent_id={agent_id}`.",
        "3. Inspect the transcript file for Edit/Write tool calls with absolute paths",
        f"   under `{_REPO_ROOT}/` instead of the agent's worktree prefix",
        "   `.claude/worktrees/agent-<id>/`.",
        "",
        "## Expected behaviour",
        "",
        "All Edit/Write calls from a worktree-isolated agent must target paths under",
        "`.claude/worktrees/agent-<id>/`. The spawn prompt now injects `YOUR WORKTREE: <path>`",
        "as the first instruction block (Discussion #592 PR-a).",
        "",
        "*Filed automatically by `backend/analyst_bug_filer.py`.*",
        "",
        marker,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class AnalystBugFiler:
    """File [Bug] Discussions from run_analyst classifier findings.

    Usage:
        filer = AnalystBugFiler()
        url = filer.file_bug(hit, dry_run=False)
    """

    def file_bug(
        self,
        hit: dict[str, Any],
        dry_run: bool = False,
        category_name: str = "General",
    ) -> str | None:
        """File a [Bug] Discussion for a single classifier hit.

        Args:
            hit: Classifier finding dict. Must include 'classifier', 'severity',
                 'agent_id'. Optional: 'file_path', 'branch', 'detail'.
            dry_run: When True, print the Discussion body and return a placeholder
                     URL without making any API calls.
            category_name: GitHub Discussion category to use.

        Returns:
            Discussion URL on success, DRY_RUN_URL in dry-run mode, None if
            skipped (idempotent / severity below threshold / unsupported classifier).
        """
        classifier = hit.get("classifier", "")
        severity = hit.get("severity", "")
        agent_id = hit.get("agent_id", "")

        # Only handle known classifiers
        if classifier not in HANDLED_CLASSIFIERS:
            print(
                f"[analyst_bug_filer] Skipping unsupported classifier: {classifier!r}",
                file=sys.stderr,
            )
            return None

        # Severity gate
        if severity.lower() not in FILING_SEVERITIES:
            print(
                f"[analyst_bug_filer] Skipping {classifier!r} — severity {severity!r} below threshold",
                file=sys.stderr,
            )
            return None

        if not agent_id:
            agent_id = "unknown"

        marker = f"<!-- analyst-bug:{classifier}:{agent_id} -->"
        title = f"[Bug] Worktree isolation violation: {agent_id[:40]} wrote to main repo"

        # Idempotency check
        if not dry_run and _search_existing_discussions(marker):
            print(
                f"[analyst_bug_filer] Skipped — Discussion with marker {marker!r} already exists",
            )
            return None

        body = _build_body(hit, marker)

        if dry_run:
            print(f"[dry-run] Would file Discussion: {title!r}")
            print(f"[dry-run] Body:\n{body}")
            return DRY_RUN_URL

        repo_id = _get_repo_id()
        category_id = _get_category_id(category_name)
        if not repo_id or not category_id:
            print(
                "[analyst_bug_filer] Could not resolve repo_id or category_id — aborting",
                file=sys.stderr,
            )
            return None

        url = _create_discussion(title, body, repo_id, category_id)
        if url:
            print(f"[analyst_bug_filer] Filed: {url}")
        return url


# ---------------------------------------------------------------------------
# Convenience function (matches browser_tour_filer.py style)
# ---------------------------------------------------------------------------

def file_wrote_outside_worktree_hits(
    findings: list[dict[str, Any]],
    dry_run: bool = False,
    category_name: str = "General",
) -> list[dict[str, Any]]:
    """Filter findings for wrote_outside_worktree and file one Discussion per unique agent_id.

    Deduplicates by agent_id before filing — multiple hits from the same agent
    produce at most one Discussion.

    Args:
        findings: List of finding dicts from run_analyst classifiers.
        dry_run: Pass through to AnalystBugFiler.file_bug().
        category_name: GitHub Discussion category to use.

    Returns:
        List of {agent_id, url, filed} dicts.
    """
    filer = AnalystBugFiler()
    # Collect per-agent_id worst hit (prefer high > medium)
    sev_order = {"high": 0, "medium": 1, "low": 2}
    per_agent: dict[str, dict[str, Any]] = {}
    for f in findings:
        if f.get("classifier") != "wrote_outside_worktree":
            continue
        if f.get("severity", "low").lower() not in FILING_SEVERITIES:
            continue
        aid = f.get("agent_id", "unknown")
        if aid not in per_agent:
            per_agent[aid] = f
        else:
            existing_sev = sev_order.get(per_agent[aid].get("severity", "low"), 2)
            new_sev = sev_order.get(f.get("severity", "low"), 2)
            if new_sev < existing_sev:
                per_agent[aid] = f

    results = []
    for agent_id, hit in per_agent.items():
        url = filer.file_bug(hit, dry_run=dry_run, category_name=category_name)
        results.append({
            "agent_id": agent_id,
            "url": url,
            "filed": url is not None,
        })
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="File [Bug] Discussions from run_analyst classifier hits."
    )
    parser.add_argument(
        "--hit",
        required=True,
        help="JSON string representing a single classifier finding. "
             "Required fields: classifier, severity, agent_id. "
             "Optional: file_path, branch, detail.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Discussion body and return a placeholder URL. No API calls.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="File the Discussion on GitHub. Idempotent — skips if already filed.",
    )
    parser.add_argument(
        "--category",
        default="General",
        help="GitHub Discussion category name (default: General).",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("ERROR: Specify --dry-run or --apply.", file=sys.stderr)
        return 1

    try:
        hit = json.loads(args.hit)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --hit is not valid JSON: {exc}", file=sys.stderr)
        return 1

    filer = AnalystBugFiler()
    url = filer.file_bug(hit, dry_run=args.dry_run, category_name=args.category)

    result = {
        "classifier": hit.get("classifier"),
        "agent_id": hit.get("agent_id"),
        "url": url,
        "filed": url is not None,
        "dry_run": args.dry_run,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
