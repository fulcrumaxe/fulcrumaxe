"""
browser_tour_filer.py — parse browser-tester AGENT_OUTPUT and auto-file [Bug] Discussions.

Takes a tour result JSON envelope (from .autonomous-team/browser-tours/*.json),
walks the issues[] array, filters severity in {error, high, medium}, and
creates one GitHub Discussion per finding via GraphQL.

Usage:
    python3 backend/browser_tour_filer.py --tour-file .autonomous-team/browser-tours/nightly-2026-05-10T04-00.json
    python3 backend/browser_tour_filer.py --tour-json '{"agent":"browser-tester",...}'

The GraphQL call can be mocked by passing --dry-run (prints mutations, no API calls).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a script from repo root: `python3 backend/browser_tour_filer.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend._repo import REPO, REPO_OWNER, REPO_NAME

# Severity levels that trigger auto-filing
FILING_SEVERITIES = {"error", "high", "medium"}

# Discussion category node ID for "Bug" category — resolved at runtime via GraphQL
_CATEGORY_CACHE: dict[str, str] = {}


def _get_discussion_category_id(category_name: str = "General") -> str:
    """Return the Discussion category node ID for the repo, cached per session."""
    if category_name in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[category_name]

    query = f"""query {{
  repository(owner:"{REPO_OWNER}", name:"{REPO_NAME}") {{
    discussionCategories(first:20) {{
      nodes {{ id name }}
    }}
  }}
}}"""
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            nodes = data["data"]["repository"]["discussionCategories"]["nodes"]
            for node in nodes:
                _CATEGORY_CACHE[node["name"]] = node["id"]
            # Try exact match first, then General
            if category_name in _CATEGORY_CACHE:
                return _CATEGORY_CACHE[category_name]
            if "General" in _CATEGORY_CACHE:
                return _CATEGORY_CACHE["General"]
    except Exception:
        pass
    # Fallback: empty string — caller must handle
    return ""


def _get_repo_id() -> str:
    """Return the repository node ID."""
    query = f"""query {{
  repository(owner:"{REPO_OWNER}", name:"{REPO_NAME}") {{ id }}
}}"""
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data["data"]["repository"]["id"]
    except Exception:
        pass
    return ""


def _file_discussion(title: str, body: str, category_id: str, repo_id: str,
                     dry_run: bool = False) -> str | None:
    """Create a GitHub Discussion and return its URL, or None on failure."""
    mutation = """mutation CreateDiscussion($repoId:ID!, $catId:ID!, $title:String!, $body:String!) {
  createDiscussion(input:{repositoryId:$repoId, categoryId:$catId, title:$title, body:$body}) {
    discussion { url number }
  }
}"""
    if dry_run:
        print(f"[dry-run] Would file Discussion: {title!r}")
        print(f"[dry-run] Body preview: {body[:200]}")
        return f"https://github.com/{REPO}/discussions/DRY-RUN"

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
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            disc = data["data"]["createDiscussion"]["discussion"]
            return disc["url"]
        else:
            print(f"[browser_tour_filer] GraphQL error: {result.stderr}", file=sys.stderr)
    except Exception as e:
        print(f"[browser_tour_filer] Exception filing Discussion: {e}", file=sys.stderr)
    return None


def _build_discussion_body(issue: dict[str, Any], tour_meta: dict[str, Any]) -> str:
    """Build the Discussion body for a single finding."""
    pr = tour_meta.get("pr")
    trigger = tour_meta.get("trigger", "unknown")
    tour_file = tour_meta.get("tour_file", "")
    affected_pages = tour_meta.get("affected_pages", [])
    queued_at = tour_meta.get("queued_at", "")

    file_ref = issue.get("file", "")
    line_ref = issue.get("line")
    severity = issue.get("severity", "unknown")
    message = issue.get("message", "")

    lines = ["<!-- STATUS:DISCUSSING SINCE:" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + " -->", ""]
    lines.append("## Automated browser-tester finding")
    lines.append("")
    lines.append(f"**Severity:** {severity}")
    lines.append(f"**Trigger:** {trigger}")
    if pr:
        lines.append(f"**Originating PR:** #{pr}")
    if affected_pages:
        lines.append(f"**Affected pages:** {', '.join(affected_pages)}")
    if queued_at:
        lines.append(f"**Queued at:** {queued_at}")
    lines.append("")
    lines.append("## Finding")
    lines.append("")
    lines.append(message)
    if file_ref:
        loc = f"`{file_ref}`"
        if line_ref:
            loc += f" line {line_ref}"
        lines.append(f"\n**Location:** {loc}")
    lines.append("")
    lines.append("## Context")
    lines.append("")
    if tour_file:
        lines.append(f"Tour output file: `{tour_file}`")
    if pr:
        lines.append(
            f"This was detected during the automated post-merge browser tour triggered by PR #{pr}."
        )
    else:
        lines.append("This was detected during the automated nightly browser regression sweep.")
    lines.append("")
    lines.append("*Filed automatically by `backend/browser_tour_filer.py`. Verify before closing.*")

    return "\n".join(lines)


def parse_and_file(
    envelope: dict[str, Any],
    tour_meta: dict[str, Any] | None = None,
    dry_run: bool = False,
    category_name: str = "General",
) -> list[dict[str, Any]]:
    """
    Parse an AGENT_OUTPUT envelope and file [Bug] Discussions for severity >= medium.

    Args:
        envelope: The parsed AGENT_OUTPUT JSON dict.
        tour_meta: Extra context: {pr, trigger, affected_pages, queued_at, tour_file}.
                   If None, reads from envelope top-level fields.
        dry_run: Print mutations without making API calls.
        category_name: GitHub Discussion category name to use.

    Returns:
        List of filed Discussion records: [{title, url, severity, message}, ...]
    """
    if tour_meta is None:
        tour_meta = {
            "pr": envelope.get("pr"),
            "trigger": envelope.get("trigger", "unknown"),
            "affected_pages": envelope.get("affected_pages", []),
            "queued_at": envelope.get("queued_at", ""),
            "tour_file": "",
        }

    issues = envelope.get("issues", [])
    if not issues:
        print("[browser_tour_filer] No issues in envelope — nothing to file.")
        return []

    # Filter to severity >= medium
    filing_issues = [
        iss for iss in issues
        if iss.get("severity", "").lower() in FILING_SEVERITIES
    ]

    if not filing_issues:
        print(f"[browser_tour_filer] {len(issues)} issue(s) found, none above severity threshold.")
        return []

    print(f"[browser_tour_filer] Filing {len(filing_issues)} of {len(issues)} issues.")

    repo_id = "" if dry_run else _get_repo_id()
    category_id = "" if dry_run else _get_discussion_category_id(category_name)

    filed = []
    pr = tour_meta.get("pr")

    for iss in filing_issues:
        severity = iss.get("severity", "unknown")
        # Build a short title from the message
        msg = iss.get("message", "Unknown finding")
        short_msg = msg[:80].rstrip()
        if pr:
            title = f"[Bug] PR #{pr} regression: {short_msg}"
        else:
            title = f"[Bug] Browser-tester: {short_msg}"

        body = _build_discussion_body(iss, tour_meta)
        url = _file_discussion(title, body, category_id, repo_id, dry_run=dry_run)

        record: dict[str, Any] = {
            "title": title,
            "url": url,
            "severity": severity,
            "message": msg,
            "filed": url is not None,
        }
        filed.append(record)
        if url:
            print(f"[browser_tour_filer] Filed: {url} ({severity})")
        else:
            print(f"[browser_tour_filer] Failed to file: {title!r}", file=sys.stderr)

    return filed


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse browser-tester findings and auto-file Discussions.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tour-file", help="Path to a tour result JSON file.")
    group.add_argument("--tour-json", help="Inline JSON string of the tour envelope.")
    parser.add_argument("--dry-run", action="store_true", help="Print mutations, don't call API.")
    parser.add_argument("--pr", type=int, help="PR number that triggered this tour (overrides envelope).")
    parser.add_argument("--trigger", default="", help="Trigger type: post-merge or nightly.")
    parser.add_argument("--affected-pages", default="", help="Comma-separated list of affected pages.")
    parser.add_argument("--category", default="General", help="GitHub Discussion category name.")
    args = parser.parse_args()

    # Load envelope
    if args.tour_file:
        envelope = json.loads(Path(args.tour_file).read_text())
        tour_file_path = args.tour_file
    else:
        envelope = json.loads(args.tour_json)
        tour_file_path = ""

    # Build tour_meta
    tour_meta: dict[str, Any] = {
        "pr": args.pr or envelope.get("pr"),
        "trigger": args.trigger or envelope.get("trigger", "unknown"),
        "affected_pages": [p for p in args.affected_pages.split(",") if p] or envelope.get("affected_pages", []),
        "queued_at": envelope.get("queued_at", ""),
        "tour_file": tour_file_path,
    }

    filed = parse_and_file(envelope, tour_meta, dry_run=args.dry_run, category_name=args.category)
    print(json.dumps({"filed": len(filed), "results": filed}, indent=2))


if __name__ == "__main__":
    main()
