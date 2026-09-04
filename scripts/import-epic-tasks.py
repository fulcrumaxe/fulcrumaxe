#!/usr/bin/env python3
"""
scripts/import-epic-tasks.py — import epic task files into GitHub Discussions.

Usage:
    python3 scripts/import-epic-tasks.py <repo-path> --repo <owner/name>
            [--status not-started,in_progress]
            [--dry-run]
            [--epic <N>]
            [--include-empty-epics]
            [--exclude-epic epic-22-vcs-agentblame,epic-99-foo]

Idempotent — checks for an existing Discussion with the same title before
creating.  Rate-limited: on 403 secondary-rate-limit, remaining tasks are
written to .autonomous-team/pending-imports.json and the script exits cleanly.

Frontmatter fields parsed:
    epic, task, title, type, status, estimated_hours, depends_on, tags,
    parent_task, supersedes

Discussion title format:
    [<Type>] epic-<N>.<task> — <title>

Labels created (if missing):
    epic-<N>, <type>, est-<estimated_hours>h

Empty-epic overview Discussion title format (--include-empty-epics):
    [Epic] epic-<N> — <title>
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# GraphQL call instrumentation (D#1526 AC#12 — timing/call summary)
# ---------------------------------------------------------------------------

_GRAPHQL_CALLS = 0

# ---------------------------------------------------------------------------
# Rate-limit retry tuning (D#1526 AC#11 — capped exponential backoff + jitter)
# ---------------------------------------------------------------------------

MAX_RETRY_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0


def _capped_backoff_sleep(attempt: int) -> None:
    """Sleep with capped exponential backoff + jitter before retry `attempt` (0-indexed)."""
    backoff = min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
    jitter = random.uniform(0, backoff * 0.25)
    time.sleep(backoff + jitter)

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from body.

    Returns
    -------
    (frontmatter_dict, body_text)
    """
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    rest = text[3:]
    match = re.search(r"^---\s*$", rest, re.MULTILINE)
    if not match:
        return {}, text

    fm_text = rest[: match.start()].strip()
    body = rest[match.end():].lstrip("\n")

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}

    return fm, body


# ---------------------------------------------------------------------------
# Title formatter
# ---------------------------------------------------------------------------

def format_title(fm: dict[str, Any]) -> str:
    """Build the Discussion title from frontmatter fields.

    Format: [<Type>] epic-<N>.<task> — <title>
    """
    type_raw = str(fm.get("type", "task"))
    # Capitalise first letter only
    type_cap = type_raw[0].upper() + type_raw[1:] if type_raw else "Task"

    epic = fm.get("epic", "?")
    task = fm.get("task", "?")
    title = fm.get("title", "untitled")

    return f"[{type_cap}] epic-{epic}.{task} — {title}"


# ---------------------------------------------------------------------------
# gh CLI helpers
# ---------------------------------------------------------------------------

def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command."""
    global _GRAPHQL_CALLS
    if "graphql" in args:
        _GRAPHQL_CALLS += 1
    cmd = ["gh"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def gh_json(*args: str) -> Any:
    """Run gh and parse JSON output."""
    result = gh(*args, check=False)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def ensure_label(repo: str, label: str, dry_run: bool = False) -> None:
    """Create label if it doesn't exist."""
    if dry_run:
        print(f"  [dry-run] would ensure label: {label}")
        return

    result = gh("label", "list", "--repo", repo, "--json", "name", check=False)
    if result.returncode == 0:
        existing = {item["name"] for item in json.loads(result.stdout or "[]")}
        if label in existing:
            return

    # Create with a neutral colour
    gh("label", "create", label, "--repo", repo, "--color", "ededed", "--force", check=False)


def get_discussion_category_id(repo: str) -> str | None:
    """Return the node ID for the 'General' discussion category (or first available)."""
    owner, name = repo.split("/", 1)
    query = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        discussionCategories(first: 20) {
          nodes { id name }
        }
      }
    }
    """
    result = gh(
        "api", "graphql",
        "-f", f"query={query}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        cats = data["data"]["repository"]["discussionCategories"]["nodes"]
        # Prefer "General", else take first
        for cat in cats:
            if cat["name"].lower() == "general":
                return cat["id"]
        if cats:
            return cats[0]["id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        pass
    return None


def get_repo_node_id(repo: str) -> str | None:
    """Return the repository node ID needed for createDiscussion."""
    owner, name = repo.split("/", 1)
    query = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) { id }
    }
    """
    result = gh(
        "api", "graphql",
        "-f", f"query={query}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)["data"]["repository"]["id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None


def list_existing_discussion_titles(repo: str) -> dict[str, int]:
    """Return {title: number} for all open discussions in the repo (paginates)."""
    owner, name = repo.split("/", 1)
    titles: dict[str, int] = {}
    cursor = None

    while True:
        # Use $after variable (nullable String) to support pagination without
        # injecting the cursor value into the query string.
        query = """
        query($owner: String!, $name: String!, $after: String) {
          repository(owner: $owner, name: $name) {
            discussions(first: 100, after: $after) {
              nodes { number title }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        gh_args = [
            "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
        ]
        if cursor:
            gh_args += ["-f", f"after={cursor}"]
        result = gh(*gh_args, check=False)
        if result.returncode != 0:
            break
        try:
            data = json.loads(result.stdout)
            disc = data["data"]["repository"]["discussions"]
            for node in disc["nodes"]:
                titles[node["title"]] = node["number"]
            page_info = disc["pageInfo"]
            if page_info["hasNextPage"]:
                cursor = page_info["endCursor"]
            else:
                break
        except (KeyError, TypeError, json.JSONDecodeError):
            break

    return titles


def create_discussion(
    repo_id: str,
    category_id: str,
    title: str,
    body: str,
    repo: str,
    dry_run: bool = False,
) -> tuple[int, str] | None:
    """Create a GitHub Discussion via GraphQL. Returns (number, node_id) or None.

    The node id is selected here (instead of a second per-Discussion query
    later) so callers — e.g. add_labels_to_discussion — can reuse it directly.
    """
    if dry_run:
        print(f"  [dry-run] would create Discussion: {title!r}")
        return None

    mutation = """
    mutation($repoId: ID!, $catId: ID!, $title: String!, $body: String!) {
      createDiscussion(input: {repositoryId: $repoId, categoryId: $catId, title: $title, body: $body}) {
        discussion { number id }
      }
    }
    """
    global _GRAPHQL_CALLS
    _GRAPHQL_CALLS += 1
    result = subprocess.run(
        [
            "gh", "api", "graphql",
            "-f", f"query={mutation}",
            "-f", f"repoId={repo_id}",
            "-f", f"catId={category_id}",
            "-f", f"title={title}",
            "-f", f"body={body}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        # Check for rate limit
        if "secondary rate limit" in result.stderr.lower() or "403" in result.stderr:
            raise RateLimitError(result.stderr)
        print(f"  [warn] Failed to create discussion {title!r}: {result.stderr.strip()}", file=sys.stderr)
        return None

    try:
        data = json.loads(result.stdout)
        disc = data["data"]["createDiscussion"]["discussion"]
        return disc["number"], disc["id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"  [warn] Unexpected createDiscussion response: {exc}", file=sys.stderr)
        return None


def create_discussion_with_backoff(
    repo_id: str,
    category_id: str,
    title: str,
    body: str,
    repo: str,
    dry_run: bool = False,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> tuple[int, str] | None:
    """create_discussion with capped exponential backoff + jitter on rate-limit.

    Retries up to `max_attempts` times total (bounded — never an unbounded
    loop). Re-raises RateLimitError only after the final attempt also hits
    a rate limit; the caller then falls through to save_pending.
    """
    last_exc: RateLimitError | None = None
    for attempt in range(max_attempts):
        try:
            return create_discussion(
                repo_id=repo_id,
                category_id=category_id,
                title=title,
                body=body,
                repo=repo,
                dry_run=dry_run,
            )
        except RateLimitError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                _capped_backoff_sleep(attempt)
    assert last_exc is not None
    raise last_exc


def add_labels_to_discussion(
    repo: str,
    discussion_number: int,
    labels: list[str],
    disc_id: str | None = None,
    dry_run: bool = False,
) -> None:
    """Add labels to a discussion using its already-known node id.

    `disc_id` is threaded in from create_discussion's response (D#1526 AC#10)
    — this used to run its own per-Discussion node-id lookup query; that
    O(n) round-trip is gone now that the id is already in hand from the
    createDiscussion mutation.
    """
    if dry_run or not labels:
        return
    if not disc_id:
        print(f"  [warn] add_labels_to_discussion: no disc_id for #{discussion_number}, skipping", file=sys.stderr)
        return

    owner, name = repo.split("/", 1)

    # Get label node IDs
    label_ids = []
    label_query = """
    query($owner: String!, $name: String!, $label: String!) {
      repository(owner: $owner, name: $name) {
        label(name: $label) { id }
      }
    }
    """
    for label_name in labels:
        r = gh(
            "api", "graphql",
            "-f", f"query={label_query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-f", f"label={label_name}",
            check=False,
        )
        if r.returncode == 0:
            try:
                lid = json.loads(r.stdout)["data"]["repository"]["label"]["id"]
                if lid:
                    label_ids.append(lid)
            except (KeyError, TypeError, json.JSONDecodeError):
                pass

    if not label_ids:
        return

    ids_fragment = " ".join(f'"{lid}"' for lid in label_ids)
    mutation = f"""
    mutation {{
      addLabelsToLabelable(input: {{
        labelableId: "{disc_id}",
        labelIds: [{ids_fragment}]
      }}) {{
        labelable {{ ... on Discussion {{ number }} }}
      }}
    }}
    """
    gh("api", "graphql", "-f", f"query={mutation}", check=False)


def update_discussion_body(
    repo: str,
    discussion_number: int,
    new_body: str,
    dry_run: bool = False,
) -> None:
    """Update the body of an existing Discussion (for depends_on backfill)."""
    if dry_run:
        return

    owner, name = repo.split("/", 1)
    # Get node ID
    query = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        discussion(number: $number) { id body }
      }
    }
    """
    result = gh(
        "api", "graphql",
        "-f", f"query={query}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"number={discussion_number}",
        check=False,
    )
    if result.returncode != 0:
        return

    try:
        data = json.loads(result.stdout)["data"]["repository"]["discussion"]
        disc_id = data["id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return

    mutation = """
    mutation($discussionId: ID!, $body: String!) {
      updateDiscussion(input: {discussionId: $discussionId, body: $body}) {
        discussion { number }
      }
    }
    """
    subprocess.run(
        [
            "gh", "api", "graphql",
            "-f", f"query={mutation}",
            "-f", f"discussionId={disc_id}",
            "-f", f"body={new_body}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Rate-limit sentinel
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    pass


# ---------------------------------------------------------------------------
# Walk epics dir
# ---------------------------------------------------------------------------

def find_task_files(repo_path: Path, epic_filter: int | None = None) -> list[Path]:
    """Walk epics/epic-*/<N>.md files, skipping overview files."""
    epic_root = repo_path / "epics"
    if not epic_root.exists():
        return []

    files: list[Path] = []
    pattern = f"epic-{epic_filter}" if epic_filter is not None else "epic-*"

    for epic_dir in sorted(epic_root.glob(pattern)):
        if not epic_dir.is_dir():
            continue
        for task_file in sorted(epic_dir.glob("*.md")):
            # Skip overview files
            if task_file.stem == "epic":
                continue
            # Skip symlinks — they could point to sensitive files outside the repo
            if task_file.is_symlink():
                print(f"  [warn] Skipping symlink: {task_file}", file=sys.stderr)
                continue
            # Only numeric stems (e.g. 1.md, 25b.md) or alphanumeric task IDs
            files.append(task_file)

    return files


def epic_title_from_md(epic_md_path: Path) -> str:
    """Extract the first # heading from epic.md; fall back to dirname."""
    try:
        text = epic_md_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    except OSError:
        pass
    # Fallback: use the parent directory name
    return epic_md_path.parent.name


def find_empty_epic_dirs(
    repo_path: Path,
    exclude_epics: set[str],
    epic_filter: int | None = None,
) -> list[Path]:
    """Return epic dirs that have an epic.md but NO numeric task files.

    A dir is considered empty if, after skipping:
      - epic.md (the overview file)
      - symlinks
    there are zero remaining *.md files.

    Args:
        repo_path: root of the project repo
        exclude_epics: set of dirname strings to skip (e.g. {"epic-22-vcs-agentblame"})
        epic_filter: if set, only check epic-<epic_filter>-* dirs
    """
    epic_root = repo_path / "epics"
    if not epic_root.exists():
        return []

    pattern = f"epic-{epic_filter}-*" if epic_filter is not None else "epic-*"
    empty_dirs: list[Path] = []

    for epic_dir in sorted(epic_root.glob(pattern)):
        if not epic_dir.is_dir():
            continue
        if epic_dir.name in exclude_epics:
            continue
        # Skip symlinked dirs
        if epic_dir.is_symlink():
            continue

        epic_md = epic_dir / "epic.md"
        if not epic_md.exists():
            continue

        # Check for task files (non-epic.md, non-symlink .md files)
        has_tasks = any(
            f for f in epic_dir.glob("*.md")
            if f.stem != "epic" and not f.is_symlink()
        )
        if not has_tasks:
            empty_dirs.append(epic_dir)

    return empty_dirs


# ---------------------------------------------------------------------------
# Save pending imports on rate-limit
# ---------------------------------------------------------------------------

def save_pending(repo_path: Path, remaining: list[dict[str, Any]]) -> None:
    pending_file = repo_path / ".autonomous-team" / "pending-imports.json"
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    pending_file.write_text(json.dumps(remaining, indent=2))
    print(f"\n[!] Rate limited — saved {len(remaining)} pending tasks to {pending_file}")


# ---------------------------------------------------------------------------
# Main importer
# ---------------------------------------------------------------------------

def run_import(
    repo_path: Path,
    repo: str,
    status_filter: set[str],
    dry_run: bool,
    epic_filter: int | None,
    include_empty_epics: bool = False,
    exclude_epics: set[str] | None = None,
) -> None:
    if exclude_epics is None:
        exclude_epics = set()

    global _GRAPHQL_CALLS
    _GRAPHQL_CALLS = 0
    _start_time = time.time()

    print(f"=== import-epic-tasks: {repo} ===")
    print(f"    repo_path: {repo_path}")
    print(f"    status filter: {sorted(status_filter)}")
    print(f"    dry_run: {dry_run}")
    if epic_filter is not None:
        print(f"    epic filter: {epic_filter}")
    if include_empty_epics:
        print(f"    include_empty_epics: True")
    if exclude_epics:
        print(f"    exclude_epics: {sorted(exclude_epics)}")
    print("")

    task_files = find_task_files(repo_path, epic_filter)
    if not task_files and not include_empty_epics:
        print("[!] No task files found. Check that <repo-path>/epics/epic-*/<N>.md files exist.")
        return

    print(f"Found {len(task_files)} task file(s) in epics/")

    # Parse all task files
    tasks: list[dict[str, Any]] = []
    for fpath in task_files:
        text = fpath.read_text(encoding="utf-8", errors="replace")
        fm, body = parse_frontmatter(text)
        status = str(fm.get("status", "")).replace("-", "_").replace(" ", "_").lower()
        # Normalise: "not-started" and "not_started" both accepted
        status_norm = status.replace("_", "-")

        # Apply status filter
        filter_normalised = {s.replace("_", "-") for s in status_filter}
        if status_norm not in filter_normalised:
            continue

        tasks.append({
            "path": str(fpath),
            "fm": fm,
            "body": text,  # full file content is the body per spec
            "title": format_title(fm),
        })

    print(f"After status filter: {len(tasks)} task(s) to process")
    if not tasks and not include_empty_epics:
        return

    # Fetch existing discussions to skip duplicates
    if not dry_run:
        print("Fetching existing discussions …")
        existing = list_existing_discussion_titles(repo)
        print(f"  {len(existing)} existing discussions found")
    else:
        existing = {}

    # Fetch repo & category IDs (needed for createDiscussion)
    repo_id = category_id = None
    if not dry_run:
        repo_id = get_repo_node_id(repo)
        category_id = get_discussion_category_id(repo)
        if not repo_id or not category_id:
            print("[error] Could not resolve repo ID or category ID from GitHub API.", file=sys.stderr)
            sys.exit(1)

    # First pass: create discussions
    # title -> discussion_number (for newly created + already-existing)
    title_to_number: dict[str, int] = dict(existing)
    created: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for task in tasks:
        title = task["title"]
        fm = task["fm"]

        if title in existing:
            print(f"[=] Skip (exists #{existing[title]}): {title}")
            continue

        # Ensure labels exist
        labels = []
        epic_num = fm.get("epic")
        if epic_num is not None:
            labels.append(f"epic-{epic_num}")
        task_type = str(fm.get("type", "")).lower()
        if task_type:
            labels.append(task_type)
        est = fm.get("estimated_hours")
        if est is not None:
            labels.append(f"est-{est}h")

        for label in labels:
            try:
                ensure_label(repo, label, dry_run=dry_run)
            except Exception as exc:
                print(f"  [warn] label {label!r}: {exc}", file=sys.stderr)

        # Create discussion — capped exponential backoff + jitter on 403,
        # then fall through to save_pending once attempts are exhausted.
        try:
            result = create_discussion_with_backoff(
                repo_id=repo_id or "",
                category_id=category_id or "",
                title=title,
                body=task["body"],
                repo=repo,
                dry_run=dry_run,
            )
        except RateLimitError:
            # Save remaining and exit cleanly
            remaining_indices = tasks.index(task)
            remaining = [
                {"title": t["title"], "path": t["path"]} for t in tasks[remaining_indices:]
            ]
            save_pending(repo_path, remaining)
            return

        disc_number, disc_node_id = result if result is not None else (None, None)

        if disc_number is not None:
            print(f"[+] Created #{disc_number}: {title}")
            title_to_number[title] = disc_number
            created.append({"title": title, "number": disc_number, "fm": fm})
            # Add labels to discussion — reuses the node id from create_discussion,
            # no extra per-Discussion id query.
            if labels:
                add_labels_to_discussion(repo, disc_number, labels, disc_id=disc_node_id, dry_run=dry_run)
            # Polite delay to stay under secondary rate limit
            time.sleep(1)
        else:
            if dry_run:
                print(f"[dry] Would create: {title}")
            else:
                print(f"[!] Failed to create: {title}")
                pending.append({"title": title, "path": task["path"]})

    # Second pass: depends_on backfill
    # Build a task-number → discussion-number map (using epic+task as key)
    print("\nRunning depends_on backfill …")
    # Map: (epic, task_id) -> disc_number
    task_key_map: dict[str, int] = {}
    for task in tasks:
        fm = task["fm"]
        epic = fm.get("epic")
        task_id = fm.get("task")
        title = task["title"]
        if epic is not None and task_id is not None and title in title_to_number:
            task_key_map[f"{epic}.{task_id}"] = title_to_number[title]

    for item in created:
        fm = item["fm"]
        depends_on = fm.get("depends_on", [])
        if not depends_on:
            continue

        # Resolve depends_on values to discussion numbers
        resolved_refs = []
        epic = fm.get("epic")
        for dep in (depends_on if isinstance(depends_on, list) else [depends_on]):
            dep_str = str(dep)
            # Try same-epic relative reference first: "25" → epic-N.25
            same_epic_key = f"{epic}.{dep_str}"
            if same_epic_key in task_key_map:
                resolved_refs.append(f"#{task_key_map[same_epic_key]}")
            else:
                # Try as absolute epic.task key
                if dep_str in task_key_map:
                    resolved_refs.append(f"#{task_key_map[dep_str]}")

        if not resolved_refs:
            continue

        deps_line = f"\nDepends on: {', '.join(resolved_refs)}"
        # Prepend to body
        new_body = item.get("body", "") + deps_line if "body" in item else deps_line

        if dry_run:
            print(f"  [dry-run] Would update #{item['number']} with: Depends on: {', '.join(resolved_refs)}")
        else:
            update_discussion_body(repo, item["number"], new_body, dry_run=dry_run)
            print(f"  [backfill] #{item['number']}: Depends on {', '.join(resolved_refs)}")

    if pending:
        save_pending(repo_path, pending)

    # ---------------------------------------------------------------------------
    # Empty-epic overview Discussions
    # ---------------------------------------------------------------------------
    created_overviews: list[dict[str, Any]] = []

    if include_empty_epics:
        print("\nScanning for empty epics (no task files) …")
        empty_epic_dirs = find_empty_epic_dirs(repo_path, exclude_epics, epic_filter)
        print(f"  Found {len(empty_epic_dirs)} empty epic dir(s)")

        # Re-fetch existing titles (may have grown during task import above)
        if not dry_run:
            existing_after = list_existing_discussion_titles(repo)
        else:
            existing_after = {}

        for epic_dir in empty_epic_dirs:
            epic_md = epic_dir / "epic.md"
            # Extract N from dirname like "epic-27-typescript-conversion"
            dir_name = epic_dir.name  # e.g. "epic-27-typescript-conversion"
            m = re.match(r"epic-(\d+)", dir_name)
            epic_n = m.group(1) if m else dir_name

            title_text = epic_title_from_md(epic_md)
            overview_title = f"[Epic] epic-{epic_n} — {title_text}"

            if overview_title in existing_after:
                print(f"[=] Skip overview (exists #{existing_after[overview_title]}): {overview_title}")
                continue

            epic_body_raw = epic_md.read_text(encoding="utf-8", errors="replace")
            context_header = (
                "<!-- STATUS:SCOPING --> "
                "This epic has no individual task files yet. "
                "Operators: file sub-task Discussions under this epic as scope solidifies.\n\n"
            )
            overview_body = context_header + epic_body_raw

            # Ensure labels
            overview_labels = [f"epic-{epic_n}", "epic-overview"]
            for label in overview_labels:
                try:
                    ensure_label(repo, label, dry_run=dry_run)
                except Exception as exc:
                    print(f"  [warn] label {label!r}: {exc}", file=sys.stderr)

            try:
                ov_result = create_discussion_with_backoff(
                    repo_id=repo_id or "",
                    category_id=category_id or "",
                    title=overview_title,
                    body=overview_body,
                    repo=repo,
                    dry_run=dry_run,
                )
            except RateLimitError:
                print(f"[!] Rate limited during empty-epic overview import at {overview_title!r}")
                break

            ov_number, ov_node_id = ov_result if ov_result is not None else (None, None)

            if ov_number is not None:
                print(f"[+] Created epic overview #{ov_number}: {overview_title}")
                existing_after[overview_title] = ov_number
                created_overviews.append({"title": overview_title, "number": ov_number, "epic_dir": str(epic_dir)})
                add_labels_to_discussion(repo, ov_number, overview_labels, disc_id=ov_node_id, dry_run=dry_run)
                time.sleep(1)
            else:
                if dry_run:
                    print(f"[dry] Would create overview: {overview_title}")
                else:
                    print(f"[!] Failed to create overview: {overview_title}")

    print(f"\nDone. Created {len(created)} task discussion(s), {len(created_overviews)} epic overview(s).")

    elapsed = time.time() - _start_time
    total_created = len(created) + len(created_overviews)
    print(f"seeded {total_created} discussions in {elapsed:.1f}s ({_GRAPHQL_CALLS} graphql calls)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import epic task files into GitHub Discussions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("repo_path", help="Path to the repository")
    parser.add_argument("--repo", required=True, help="GitHub owner/name (e.g. example-org/example-project)")
    parser.add_argument(
        "--status",
        default="not-started,in_progress",
        help="Comma-separated list of statuses to import (default: not-started,in_progress)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be created, no API calls")
    parser.add_argument("--epic", type=int, default=None, help="Restrict to a single epic number")
    parser.add_argument(
        "--include-empty-epics",
        action="store_true",
        default=False,
        help=(
            "After task-file import, also create one overview Discussion per epic dir "
            "that has an epic.md but no numeric task files. Labels: epic-<N>, epic-overview."
        ),
    )
    parser.add_argument(
        "--exclude-epic",
        default="epic-22-vcs-agentblame",
        help=(
            "Comma-separated epic dirnames to skip when --include-empty-epics is set. "
            "Default: epic-22-vcs-agentblame (merged stub)."
        ),
    )
    args = parser.parse_args()

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.exists():
        print(f"Error: repo-path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    status_filter = {s.strip() for s in args.status.split(",") if s.strip()}
    exclude_epics = {s.strip() for s in args.exclude_epic.split(",") if s.strip()}

    run_import(
        repo_path=repo_path,
        repo=args.repo,
        status_filter=status_filter,
        dry_run=args.dry_run,
        epic_filter=args.epic,
        include_empty_epics=args.include_empty_epics,
        exclude_epics=exclude_epics,
    )


if __name__ == "__main__":
    main()
