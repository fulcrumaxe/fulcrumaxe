#!/usr/bin/env python3
"""
scripts/import-epic-tasks.py — import epic task files into GitHub Discussions.

Reads task files under ``<repo-path>/<epic_dir>/epic-<N>[-<slug>]/<TASK>.md``
(``epic_dir`` defaults to ``epics``, overridable via ``task_source.epic_dir``
in ``.autonomous-team/project.json``) and creates one Discussion per task.

Frontmatter is parsed exclusively through ``backend.task_file`` — this module
never parses YAML itself. A file with a ``schema_version`` field is a v1 file
(validated against ``backend.task_file.SCHEMA_V1``); one without is a legacy
v0 file (validated against the smaller v0 field set).

v1 files are imported only when ``status: ready``, never when they carry a
``discussion`` field, never when ``repo`` names a different repo than
``--repo``, and never when their ``<epic>.<task>`` id is in ``--exclude``.
``--milestone`` further restricts v1 files. v0 files are filtered by
``--status`` instead (the only filter that applies to them) and never carry
BLOCKED-BY — that mechanism is v1-only.

Idempotency and drift detection use the ``epic-<N>.<TASK> — `` title-prefix
substring — not an exact title match — so retitling a file's ``title`` field
never creates a duplicate Discussion. Every created body ends with a
``<!-- TASK-FILE-SHA:<sha256> -->`` marker; a later run compares that against
the file's current hash to detect drift.

All GraphQL calls go through an injected client (see ``GraphQLClient`` below)
so tests can supply a fake with no network access.

CLI:
    python3 scripts/import-epic-tasks.py <repo-path> --repo <owner/name>
            [--status draft,ready] [--dry-run] [--epic <N>]
            [--exclude <epic.task>[,<epic.task>...]]
            [--milestone <m>[,<m>...]]
            [--parent-index <N>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend import task_file  # noqa: E402
from backend.discussion_status import extract_status_anchored  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESERVED_TASK_STEMS = {"epic", "README"}
DEFAULT_EPIC_DIR = "epics"
DEFAULT_V0_STATUS_FILTER = "not-started,in_progress"

# Dependency-ref grammar (mirrors backend.task_file's private regexes — kept
# separate on purpose: those are that module's internals, not a published
# contract this script should reach into).
_D_HASH_RE = re.compile(r"^D#\d+$")
_HASH_RE = re.compile(r"^#\d+$")
_CROSS_EPIC_RE = re.compile(r"^\d+\.[A-Za-z0-9][A-Za-z0-9-]*$")

_SHA_MARKER_RE = re.compile(r"<!--\s*TASK-FILE-SHA:([0-9a-f]{64})\s*-->")
_TITLE_PREFIX_RE = re.compile(r"epic-(\d+)\.([A-Za-z0-9-]+) — ")


class RateLimitError(Exception):
    pass


# ---------------------------------------------------------------------------
# GraphQL client — the only place that shells out to `gh`
# ---------------------------------------------------------------------------


class GraphQLClient:
    """Thin wrapper over ``gh api graphql`` for the four operations this
    script needs. Injected so tests can supply a fake with no network I/O.
    """

    def __init__(self, repo: str):
        self.repo = repo
        self.calls = 0
        self._repo_id: Optional[str] = None
        self._category_id: Optional[str] = None

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)

    def _owner_name(self) -> tuple[str, str]:
        owner, _, name = self.repo.partition("/")
        return owner, name

    def _resolve_repo_id(self) -> str:
        if self._repo_id is not None:
            return self._repo_id
        owner, name = self._owner_name()
        query = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){id}}"
        result = self._run(
            "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"
        )
        data = json.loads(result.stdout or "{}")
        self._repo_id = data["data"]["repository"]["id"]
        return self._repo_id

    def _resolve_category_id(self) -> str:
        if self._category_id is not None:
            return self._category_id
        owner, name = self._owner_name()
        query = (
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
            "discussionCategories(first:20){nodes{id name}}}}"
        )
        result = self._run(
            "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"
        )
        data = json.loads(result.stdout or "{}")
        cats = data["data"]["repository"]["discussionCategories"]["nodes"]
        for cat in cats:
            if cat["name"].lower() == "general":
                self._category_id = cat["id"]
                return self._category_id
        self._category_id = cats[0]["id"] if cats else ""
        return self._category_id

    def list_discussion_titles(self) -> dict[str, int]:
        """Return {title: number} for every Discussion in the repo (paginated)."""
        owner, name = self._owner_name()
        titles: dict[str, int] = {}
        cursor: Optional[str] = None
        while True:
            query = (
                "query($owner:String!,$name:String!,$after:String){repository(owner:$owner,name:$name){"
                "discussions(first:100, after:$after){nodes{number title}"
                "pageInfo{hasNextPage endCursor}}}}"
            )
            args = ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"]
            if cursor:
                args += ["-f", f"after={cursor}"]
            result = self._run(*args)
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

    def get_discussion(self, number: int) -> Optional[dict[str, Any]]:
        owner, name = self._owner_name()
        query = (
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
            "discussion(number:$number){id body}}}"
        )
        result = self._run(
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)["data"]["repository"]["discussion"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if data is None:
            return None
        return {"id": data["id"], "body": data.get("body", "")}

    def create_discussion(self, title: str, body: str) -> tuple[int, str]:
        """Create a Discussion. Raises RateLimitError on a secondary rate limit."""
        repo_id = self._resolve_repo_id()
        category_id = self._resolve_category_id()
        mutation = (
            "mutation($repoId:ID!,$catId:ID!,$title:String!,$body:String!){"
            "createDiscussion(input:{repositoryId:$repoId,categoryId:$catId,title:$title,body:$body}){"
            "discussion{number id}}}"
        )
        result = self._run(
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"repoId={repo_id}",
            "-f",
            f"catId={category_id}",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
        )
        if result.returncode != 0:
            if "secondary rate limit" in result.stderr.lower() or "403" in result.stderr:
                raise RateLimitError(result.stderr)
            raise RuntimeError(f"createDiscussion failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)["data"]["createDiscussion"]["discussion"]
        return data["number"], data["id"]

    def update_discussion_body(self, number: int, new_body: str) -> None:
        disc = self.get_discussion(number)
        if disc is None:
            raise RuntimeError(f"update_discussion_body: #{number} not found")
        mutation = (
            "mutation($discussionId:ID!,$body:String!){updateDiscussion(input:"
            "{discussionId:$discussionId,body:$body}){discussion{number}}}"
        )
        result = self._run(
            "api", "graphql", "-f", f"query={mutation}", "-f", f"discussionId={disc['id']}", "-f", f"body={new_body}"
        )
        if result.returncode != 0:
            raise RuntimeError(f"updateDiscussion failed: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_epic_dir_name(repo_path: Path) -> str:
    """Return ``task_source.epic_dir`` from .autonomous-team/project.json, or the default."""
    project_json = repo_path / ".autonomous-team" / "project.json"
    try:
        data = json.loads(project_json.read_text(encoding="utf-8"))
        name = data.get("task_source", {}).get("epic_dir")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_EPIC_DIR


# ---------------------------------------------------------------------------
# Walk epics dir
# ---------------------------------------------------------------------------


def _epic_dir_pattern(epic_filter: Optional[int]) -> re.Pattern[str]:
    if epic_filter is not None:
        return re.compile(rf"^epic-{epic_filter}(-.*)?$")
    return re.compile(r"^epic-\d+(-.*)?$")


def find_task_files(repo_path: Path, epic_dir_name: str, epic_filter: Optional[int] = None) -> list[Path]:
    """Walk <epic_dir>/epic-*/<TASK>.md files, skipping epic.md, README.md, and symlinks."""
    epic_root = repo_path / epic_dir_name
    if not epic_root.exists():
        return []

    pattern = _epic_dir_pattern(epic_filter)
    files: list[Path] = []
    for epic_dir in sorted(epic_root.iterdir()):
        if epic_dir.is_symlink() or not epic_dir.is_dir():
            continue
        if not pattern.match(epic_dir.name):
            continue
        for task_file_path in sorted(epic_dir.glob("*.md")):
            if task_file_path.stem in RESERVED_TASK_STEMS:
                continue
            if task_file_path.is_symlink():
                print(f"  [warn] Skipping symlink: {task_file_path}", file=sys.stderr)
                continue
            files.append(task_file_path)
    return files


def find_epic_dir_by_parent_discussion(repo_path: Path, epic_dir_name: str, parent_number: int) -> Optional[Path]:
    epic_root = repo_path / epic_dir_name
    if not epic_root.exists():
        return None
    for epic_dir in sorted(epic_root.iterdir()):
        if not epic_dir.is_dir() or epic_dir.is_symlink():
            continue
        epic_md = epic_dir / "epic.md"
        if not epic_md.exists():
            continue
        fm, _ = task_file.parse_task_file(epic_md)
        if fm.get("parent_discussion") == parent_number:
            return epic_dir
    return None


_PARENT_DISCUSSION_CACHE: dict[Path, Optional[int]] = {}


def load_parent_discussion(epic_dir: Path) -> Optional[int]:
    """Return epic.md's ``parent_discussion`` field for *epic_dir*, or None. Cached per dir."""
    if epic_dir in _PARENT_DISCUSSION_CACHE:
        return _PARENT_DISCUSSION_CACHE[epic_dir]
    epic_md = epic_dir / "epic.md"
    value: Optional[int] = None
    if epic_md.exists():
        fm, _ = task_file.parse_task_file(epic_md)
        raw = fm.get("parent_discussion")
        if isinstance(raw, int) and not isinstance(raw, bool):
            value = raw
    _PARENT_DISCUSSION_CACHE[epic_dir] = value
    return value


# ---------------------------------------------------------------------------
# Title formatting / title-prefix idempotency key
# ---------------------------------------------------------------------------


def format_title(fm: dict[str, Any]) -> str:
    """Build the Discussion title: ``[<Type>] epic-<N>.<task> — <title>``."""
    type_raw = str(fm.get("type", "task"))
    type_cap = type_raw[0].upper() + type_raw[1:] if type_raw else "Task"
    epic = fm.get("epic", "?")
    task = fm.get("task", "?")
    title = fm.get("title", "untitled")
    return f"[{type_cap}] epic-{epic}.{task} — {title}"


def existing_by_key(existing_titles: dict[str, int]) -> dict[str, int]:
    """Map ``{"<epic>.<task>": number}`` from a title->number listing, via prefix match."""
    out: dict[str, int] = {}
    for title, number in existing_titles.items():
        m = _TITLE_PREFIX_RE.search(title)
        if m:
            out[f"{m.group(1)}.{m.group(2)}"] = number
    return out


# ---------------------------------------------------------------------------
# Frontmatter block rendering (flow-style lists, so extract_file_list's
# inline-array strategy always matches acceptance_files regardless of what
# style the source file used).
# ---------------------------------------------------------------------------

import yaml  # noqa: E402


class _FlowList(list):
    pass


def _flow_list_representer(dumper: Any, data: Any) -> Any:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


yaml.add_representer(_FlowList, _flow_list_representer, Dumper=yaml.SafeDumper)


def format_frontmatter_block(fm: dict[str, Any]) -> str:
    """Render *fm* as a ``---\\n...\\n---`` block, list values in flow style."""
    fm2 = {k: (_FlowList(v) if isinstance(v, list) else v) for k, v in fm.items()}
    dumped = yaml.safe_dump(fm2, default_flow_style=False, sort_keys=False).rstrip("\n")
    return f"---\n{dumped}\n---"


# ---------------------------------------------------------------------------
# Body construction
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_v1_body(
    fm: dict[str, Any],
    file_body: str,
    blocked_refs: list[str],
    parent_discussion: Optional[int],
    sha_hex: str,
    now_iso: str,
) -> str:
    line1 = f"<!-- STATUS:SPEC_READY SINCE:{now_iso}"
    if blocked_refs:
        line1 += f" BLOCKED-BY:{','.join(blocked_refs)}"
    line1 += " -->"
    return _assemble_body(line1, fm, file_body, parent_discussion, sha_hex)


def build_v0_body(
    fm: dict[str, Any],
    file_body: str,
    parent_discussion: Optional[int],
    sha_hex: str,
    now_iso: str,
) -> str:
    line1 = f"<!-- STATUS:DISCUSSING SINCE:{now_iso} -->"
    return _assemble_body(line1, fm, file_body, parent_discussion, sha_hex)


def _assemble_body(
    line1: str,
    fm: dict[str, Any],
    file_body: str,
    parent_discussion: Optional[int],
    sha_hex: str,
) -> str:
    fm_block = format_frontmatter_block(fm)
    segments = [line1 + "\n" + fm_block]
    if parent_discussion is not None:
        segments.append(f"Parent: D#{parent_discussion}")
    if file_body.strip():
        segments.append(file_body.strip())
    body = "\n\n".join(segments)
    body += f"\n\n<!-- TASK-FILE-SHA:{sha_hex} -->"
    return body


def _utc_now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Dependency classification
# ---------------------------------------------------------------------------


def classify_dependency(dep: str, epic: Any) -> tuple[str, str]:
    """Return ("absolute", ref) for a #<n>/D#<n> ref (passed through unchanged),
    or ("keyed", "<epic>.<task>") for a same-epic or cross-epic task reference.
    """
    dep_str = str(dep)
    if _D_HASH_RE.match(dep_str) or _HASH_RE.match(dep_str):
        return "absolute", dep_str
    if _CROSS_EPIC_RE.match(dep_str):
        return "keyed", dep_str
    return "keyed", f"{epic}.{dep_str}"


# ---------------------------------------------------------------------------
# Selection / filtering
# ---------------------------------------------------------------------------


def _normalize_status(raw: Any) -> str:
    return str(raw or "").strip().replace("_", "-").lower()


def parse_selection(
    parsed_files: list[dict[str, Any]],
    repo: str,
    status_filter: set[str],
    exclude_ids: set[str],
    milestone_filter: Optional[set[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split parsed files into (v0_selected, v1_selected) per the filter rules."""
    v0_selected, v1_selected = [], []
    norm_status_filter = {_normalize_status(s) for s in status_filter}

    for item in parsed_files:
        fm = item["fm"]
        if "schema_version" in fm:
            if fm.get("status") != "ready":
                continue
            if "discussion" in fm:
                continue
            item_repo = fm.get("repo")
            if item_repo and item_repo != repo:
                continue
            key = f'{fm.get("epic")}.{fm.get("task")}'
            if key in exclude_ids:
                continue
            if milestone_filter is not None and fm.get("milestone") not in milestone_filter:
                continue
            v1_selected.append(item)
        else:
            if _normalize_status(fm.get("status")) not in norm_status_filter:
                continue
            v0_selected.append(item)

    return v0_selected, v1_selected


# ---------------------------------------------------------------------------
# Main import
# ---------------------------------------------------------------------------


def run_import(
    repo_path: Path,
    repo: str,
    client: GraphQLClient,
    status_filter: set[str],
    dry_run: bool,
    epic_filter: Optional[int],
    exclude_ids: Optional[set[str]] = None,
    milestone_filter: Optional[set[str]] = None,
    epic_dir_name: Optional[str] = None,
) -> int:
    """Run one import pass. Returns the process exit code (0 or 1)."""
    exclude_ids = exclude_ids or set()
    epic_dir_name = epic_dir_name or load_epic_dir_name(repo_path)

    print(f"=== import-epic-tasks: {repo} ===")
    print(f"    repo_path: {repo_path}")
    print(f"    dry_run: {dry_run}")

    task_paths = find_task_files(repo_path, epic_dir_name, epic_filter)
    parsed: list[dict[str, Any]] = []
    for path in task_paths:
        fm, body = task_file.parse_task_file(path)
        parsed.append(
            {
                "path": path,
                "fm": fm,
                "file_body": body,
                "stem": path.stem,
                "epic_dir": path.parent,
            }
        )

    print(f"Found {len(parsed)} task file(s) under {epic_dir_name}/")

    v0_selected, v1_selected = parse_selection(parsed, repo, status_filter, exclude_ids, milestone_filter)
    selected = v0_selected + v1_selected
    print(f"Selected {len(v0_selected)} v0 and {len(v1_selected)} v1 task(s)")

    # --- Validate the whole selection before creating anything (AC5) -------
    invalid = False
    for item in selected:
        problems = [p for p in task_file.validate_task_file(item["fm"], item["stem"]) if not p.startswith("warning:")]
        if problems:
            invalid = True
            for p in problems:
                print(f"[invalid] {item['path']}: {p}", file=sys.stderr)
    if invalid:
        print("[!] Validation failed — nothing created.", file=sys.stderr)
        return 1

    if not selected:
        print("Nothing to import.")
        return 0

    existing_titles = client.list_discussion_titles()
    created_map = existing_by_key(existing_titles)

    # key -> file info, across ALL parsed files (not just selected), so a
    # dependency on a file that's e.g. status:completed but wasn't selected
    # this run can still be recognised and omitted.
    key_to_file = {f'{p["fm"].get("epic")}.{p["fm"].get("task")}': p for p in parsed}

    exit_code = 0
    now_iso = _utc_now_iso()

    # --- v0: no dependency graph, no BLOCKED-BY -----------------------------
    for item in v0_selected:
        exit_code |= _create_or_drift(item, client, created_map, dry_run, now_iso, blocked_refs=None)

    # --- v1: topological creation order + BLOCKED-BY ------------------------
    # Items whose Discussion already exists just need the idempotent
    # create-or-drift path, not dependency resolution.
    pending = []
    for item in v1_selected:
        key = f'{item["fm"].get("epic")}.{item["fm"].get("task")}'
        if key in created_map:
            exit_code |= _create_or_drift(item, client, created_map, dry_run, now_iso, blocked_refs=[])
        else:
            pending.append(item)

    made_progress = True
    while pending and made_progress:
        made_progress = False
        still_pending = []
        for item in pending:
            fm = item["fm"]
            epic = fm.get("epic")
            deps = fm.get("depends_on") or []
            resolved_refs: list[str] = []
            blocked_on: list[str] = []
            for dep in deps:
                kind, value = classify_dependency(dep, epic)
                if kind == "absolute":
                    resolved_refs.append(value)
                    continue
                dep_file = key_to_file.get(value)
                if dep_file is not None and _normalize_status(dep_file["fm"].get("status")) == "completed":
                    continue
                if value in created_map:
                    resolved_refs.append(f"D#{created_map[value]}")
                else:
                    blocked_on.append(str(dep))
            if blocked_on:
                still_pending.append((item, blocked_on))
                continue

            key = f'{epic}.{fm.get("task")}'
            new_number = _create_v1(item, client, dry_run, now_iso, resolved_refs)
            if new_number is not None:
                created_map[key] = new_number
            made_progress = True

        pending = [item for item, _ in still_pending]
        if not made_progress:
            for item, blocked_on in still_pending:
                fm = item["fm"]
                key_label = f'{fm.get("epic")}.{fm.get("task")}'
                for ref in blocked_on:
                    print(f"unresolved dependency {ref} for {key_label}")
            exit_code = 1

    return exit_code


def _create_v1(
    item: dict[str, Any],
    client: GraphQLClient,
    dry_run: bool,
    now_iso: str,
    resolved_refs: list[str],
) -> Optional[int]:
    fm = item["fm"]
    title = format_title(fm)
    parent = load_parent_discussion(item["epic_dir"])
    sha_hex = file_sha256(item["path"])
    body = build_v1_body(fm, item["file_body"], resolved_refs, parent, sha_hex, now_iso)

    if dry_run:
        print(f"[dry] Would create: {title}")
        return None

    try:
        number, _node_id = client.create_discussion(title, body)
    except RateLimitError:
        print(f"[!] Rate limited creating: {title}", file=sys.stderr)
        return None
    except RuntimeError as exc:
        print(f"[!] Failed to create {title}: {exc}", file=sys.stderr)
        return None

    print(f"[+] Created #{number}: {title}")
    return number


def _create_or_drift(
    item: dict[str, Any],
    client: GraphQLClient,
    created_map: dict[str, int],
    dry_run: bool,
    now_iso: str,
    blocked_refs: Optional[list[str]],
) -> int:
    """Create a fresh Discussion, or — if one already exists for this
    epic.task key — check for content drift and update in place if the
    existing Discussion is still SPEC_READY. Returns 0 or 1 (exit code
    contribution)."""
    fm = item["fm"]
    epic = fm.get("epic")
    task = fm.get("task")
    key = f"{epic}.{task}"
    is_v1 = "schema_version" in fm
    parent = load_parent_discussion(item["epic_dir"])
    sha_hex = file_sha256(item["path"])

    existing_number = created_map.get(key)
    if existing_number is None:
        title = format_title(fm)
        if is_v1:
            body = build_v1_body(fm, item["file_body"], blocked_refs or [], parent, sha_hex, now_iso)
        else:
            body = build_v0_body(fm, item["file_body"], parent, sha_hex, now_iso)

        if dry_run:
            print(f"[dry] Would create: {title}")
            return 0
        try:
            number, _node_id = client.create_discussion(title, body)
        except RateLimitError:
            print(f"[!] Rate limited creating: {title}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"[!] Failed to create {title}: {exc}", file=sys.stderr)
            return 1
        print(f"[+] Created #{number}: {title}")
        created_map[key] = number
        return 0

    # Already exists — drift check.
    existing = client.get_discussion(existing_number)
    if existing is None:
        print(f"[!] #{existing_number} for {key} could not be fetched — skipping drift check", file=sys.stderr)
        return 1
    existing_body = existing["body"]
    m = _SHA_MARKER_RE.search(existing_body)
    old_sha = m.group(1) if m else None

    if old_sha == sha_hex:
        print(f"[=] Up to date (#{existing_number}): {key}")
        return 0

    old_status = extract_status_anchored(existing_body)
    if old_status != "SPEC_READY":
        print(f"drift: {key} changed after spawn (D#{existing_number})")
        return 0

    line1 = existing_body.splitlines()[0] if existing_body else ""
    fm_block = format_frontmatter_block(fm)
    segments = [line1 + "\n" + fm_block]
    if parent is not None:
        segments.append(f"Parent: D#{parent}")
    if item["file_body"].strip():
        segments.append(item["file_body"].strip())
    new_body = "\n\n".join(segments) + f"\n\n<!-- TASK-FILE-SHA:{sha_hex} -->"

    if dry_run:
        print(f"[dry] Would update #{existing_number} for drift: {key}")
        return 0
    client.update_discussion_body(existing_number, new_body)
    print(f"[~] Updated #{existing_number} for drift: {key}")
    return 0


# ---------------------------------------------------------------------------
# --parent-index
# ---------------------------------------------------------------------------


def _sort_key(item: dict[str, Any]) -> tuple[int, Any]:
    task = item["fm"].get("task", "")
    try:
        return (0, int(re.match(r"^\d+", str(task)).group()))  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return (1, str(task))


def build_parent_index(
    epic_dir: Path,
    epic_number: int,
    parent_line1: str,
    created_map: dict[str, int],
    epic_dir_name: str = DEFAULT_EPIC_DIR,
) -> str:
    """Build the index body for the parent Discussion of *epic_dir*."""
    tasks_path = f"{epic_dir_name}/{epic_dir.name}"
    fm_block = format_frontmatter_block(
        {
            "planned_prs": 0,
            "planned_prs_reason": (
                "epic index: the Team Lead closes it when every task in "
                f"{tasks_path}/ is completed or superseded"
            ),
        }
    )

    tasks: list[dict[str, Any]] = []
    for path in sorted(epic_dir.glob("*.md")):
        if path.stem in RESERVED_TASK_STEMS or path.is_symlink():
            continue
        fm, _ = task_file.parse_task_file(path)
        tasks.append({"fm": fm})

    tasks.sort(key=_sort_key)

    lines = []
    for t in tasks:
        fm = t["fm"]
        task_id = fm.get("task", "?")
        title = fm.get("title", "untitled")
        epic = fm.get("epic", epic_number)
        key = f"{epic}.{task_id}"
        if _normalize_status(fm.get("status")) == "completed":
            status_text = "completed"
        elif key in created_map:
            status_text = f"D#{created_map[key]}"
        else:
            status_text = "not imported"
        lines.append(f"- {task_id} — {title} — {status_text}")

    parts = [
        parent_line1 + "\n" + fm_block,
        f"Tasks: {tasks_path}/",
        "\n".join(lines),
    ]
    return "\n\n".join(p for p in parts if p)


def run_parent_index(
    repo_path: Path,
    client: GraphQLClient,
    parent_number: int,
    dry_run: bool,
    epic_dir_name: Optional[str] = None,
) -> int:
    epic_dir_name = epic_dir_name or load_epic_dir_name(repo_path)
    epic_dir = find_epic_dir_by_parent_discussion(repo_path, epic_dir_name, parent_number)
    if epic_dir is None:
        print(f"[!] No epic dir declares parent_discussion: {parent_number}", file=sys.stderr)
        return 1

    m = re.match(r"epic-(\d+)", epic_dir.name)
    epic_number = int(m.group(1)) if m else 0

    existing = client.get_discussion(parent_number)
    if existing is None:
        print(f"[!] Parent Discussion #{parent_number} could not be fetched", file=sys.stderr)
        return 1
    parent_line1 = existing["body"].splitlines()[0] if existing["body"] else ""

    existing_titles = client.list_discussion_titles()
    created_map = existing_by_key(existing_titles)

    body = build_parent_index(epic_dir, epic_number, parent_line1, created_map, epic_dir_name)

    if dry_run:
        print(f"[dry] Would write parent index for D#{parent_number}:")
        print(body)
        return 0

    client.update_discussion_body(parent_number, body)
    print(f"[+] Wrote parent index for D#{parent_number}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Import epic task files into GitHub Discussions.")
    parser.add_argument("repo_path", help="Path to the repository")
    parser.add_argument("--repo", required=True, help="GitHub owner/name")
    parser.add_argument("--status", default=DEFAULT_V0_STATUS_FILTER, help="Comma-separated v0 status filter")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--epic", type=int, default=None, help="Restrict to a single epic number")
    parser.add_argument("--exclude", default="", help="Comma-separated epic.task ids to skip (v1 only)")
    parser.add_argument("--milestone", default="", help="Comma-separated milestones to include (v1 only)")
    parser.add_argument("--parent-index", type=int, default=None, help="Write the index body for parent D#<N>")
    args = parser.parse_args(argv)

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.exists():
        print(f"Error: repo-path does not exist: {repo_path}", file=sys.stderr)
        return 1

    client = GraphQLClient(args.repo)
    status_filter = {s.strip() for s in args.status.split(",") if s.strip()}
    exclude_ids = {s.strip() for s in args.exclude.split(",") if s.strip()}
    milestone_filter = {s.strip() for s in args.milestone.split(",") if s.strip()} or None

    exit_code = run_import(
        repo_path=repo_path,
        repo=args.repo,
        client=client,
        status_filter=status_filter,
        dry_run=args.dry_run,
        epic_filter=args.epic,
        exclude_ids=exclude_ids,
        milestone_filter=milestone_filter,
    )

    if args.parent_index is not None:
        idx_code = run_parent_index(repo_path, client, args.parent_index, args.dry_run)
        exit_code = exit_code or idx_code

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
