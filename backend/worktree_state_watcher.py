"""
worktree_state_watcher.py — detective classifier for state files that bypass the external state dir.

Reads .autonomous-team/state-symlinks.json (created by D#630) and walks every active worktree
from .autonomous-team/worktrees.json. For each (worktree, manifest entry) pair, checks whether
the path is a correct symlink to the external state dir. Any real file or wrong-target symlink
is flagged as a finding and filed as a [Bug] Discussion (idempotent via dedup marker).

This is the detective complement to D#630's preventive auto-symlink-on-spawn fix.

CLI:
    python3 backend/worktree_state_watcher.py scan
    python3 backend/worktree_state_watcher.py scan --dry-run   # no API calls
    python3 backend/worktree_state_watcher.py scan --json      # machine-readable output
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

from backend._repo import REPO as _GH_REPO, REPO_OWNER, REPO_NAME

# ── Path resolution ───────────────────────────────────────────────────────────

def _repo_root() -> Path:
    """Return the repository root (parent of .autonomous-team/)."""
    here = Path(__file__).resolve().parent.parent
    if (here / ".autonomous-team").is_dir():
        return here
    # Fallback: walk up from cwd
    cwd = Path.cwd()
    while cwd != cwd.parent:
        if (cwd / ".autonomous-team").is_dir():
            return cwd
        cwd = cwd.parent
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _repo_root()
MANIFEST_PATH = REPO_ROOT / ".autonomous-team" / "state-symlinks.json"
REGISTRY_PATH = REPO_ROOT / ".autonomous-team" / "worktrees.json"

# Active statuses in the worktree registry
ACTIVE_STATUSES = {"active", "committed", "pushed"}


# ── Manifest / registry loading ───────────────────────────────────────────────

def load_manifest() -> list[dict[str, str]]:
    """Read state-symlinks.json. Returns [] if absent (D#630 not yet deployed)."""
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text())
        return data.get("entries", [])
    except Exception as exc:
        print(f"[worktree_state_watcher] WARNING: could not read manifest: {exc}", file=sys.stderr)
        return []


def load_worktrees() -> list[dict[str, Any]]:
    """Read worktrees.json registry; return only active entries."""
    if not REGISTRY_PATH.exists():
        return []
    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except Exception as exc:
        print(f"[worktree_state_watcher] WARNING: could not read worktrees registry: {exc}", file=sys.stderr)
        return []
    return [e for e in data if e.get("status") in ACTIVE_STATUSES]


def _state_dir() -> Path:
    """Return the external state dir (AUTONOMOUS_TEAM_STATE_DIR or ~/.autonomous-forever-state/)."""
    env = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".autonomous-forever-state"


# ── Path checker ──────────────────────────────────────────────────────────────

def check_path(wt_path: str, entry: dict[str, str], state_dir: Path) -> dict[str, Any] | None:
    """
    Check one (worktree, manifest entry) pair.

    Returns a finding dict if the path is problematic, None if correct.

    Finding kinds:
      - real_file   — the path exists but is a regular file or directory (not a symlink)
      - wrong_symlink — the path is a symlink but resolves to a different target than expected
      - missing_ok  — path does not exist at all (no finding; absence is not a problem)
    """
    in_repo = entry.get("in_repo", "")
    external = entry.get("external", "")
    if not in_repo or not external:
        return None

    check = Path(wt_path) / ".autonomous-team" / in_repo
    expected_target = state_dir / external

    try:
        stat = check.lstat()
    except FileNotFoundError:
        return None  # Absent — fine, nothing to report
    except OSError as exc:
        return {
            "worktree_id": "unknown",
            "worktree_path": wt_path,
            "in_repo_path": str(check.relative_to(wt_path)),
            "kind": "stat_error",
            "error": str(exc),
            "size_bytes": 0,
            "mtime": "",
        }

    import stat as stat_mod

    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    size = stat.st_size

    if stat_mod.S_ISLNK(stat.st_mode):
        # It's a symlink — check target
        actual_target = Path(os.readlink(check))
        # Resolve both to absolute for comparison
        if not actual_target.is_absolute():
            actual_target = (check.parent / actual_target).resolve()
        else:
            actual_target = actual_target.resolve()
        expected_resolved = expected_target.resolve()
        if actual_target == expected_resolved:
            return None  # Correct symlink
        return {
            "kind": "wrong_symlink",
            "in_repo_path": str(check.relative_to(wt_path)),
            "actual_target": str(actual_target),
            "expected_target": str(expected_resolved),
            "size_bytes": size,
            "mtime": mtime,
        }
    else:
        # Real file or directory
        return {
            "kind": "real_file",
            "in_repo_path": str(check.relative_to(wt_path)),
            "size_bytes": size,
            "mtime": mtime,
        }


# ── Dedup check ───────────────────────────────────────────────────────────────

def _dedup_marker(worktree_id: str, in_repo_path: str) -> str:
    """Return the idempotent dedup marker string for a finding."""
    # Normalise path separators
    safe_path = in_repo_path.replace("\\", "/").replace(" ", "_")
    return f"<!-- worktree-state-watcher:{worktree_id}:{safe_path} -->"


def _discussion_already_filed(marker: str) -> bool:
    """
    Search open Discussions for the dedup marker via GraphQL.
    Returns True if already filed.
    """
    query = """query SearchMarker($q: String!) {
  search(query:$q, type:DISCUSSION, first:5) {
    nodes {
      ... on Discussion { body number }
    }
  }
}"""
    search_term = f"repo:{_GH_REPO} {marker}"
    try:
        result = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query={query}",
             "-f", f"q={search_term}"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        nodes = data.get("data", {}).get("search", {}).get("nodes", [])
        for node in nodes:
            if marker in (node.get("body") or ""):
                return True
    except Exception:
        pass
    return False


# ── Bug Discussion filer ──────────────────────────────────────────────────────

def _get_repo_id() -> str:
    query = f'query {{ repository(owner:"{REPO_OWNER}", name:"{REPO_NAME}") {{ id }} }}'
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return json.loads(result.stdout)["data"]["repository"]["id"]
    except Exception:
        pass
    return ""


def _get_category_id(category_name: str = "General") -> str:
    query = f"""query {{
  repository(owner:"{REPO_OWNER}", name:"{REPO_NAME}") {{
    discussionCategories(first:20) {{ nodes {{ id name }} }}
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
                if node["name"] == category_name:
                    return node["id"]
            # Fallback to "General"
            for node in nodes:
                if node["name"] == "General":
                    return node["id"]
    except Exception:
        pass
    return ""


def file_bug(finding: dict[str, Any], dry_run: bool = False) -> str | None:
    """
    File a [Bug] Discussion for the given finding (idempotent via dedup marker).

    Returns the Discussion URL if filed (or a placeholder for dry-run), None on skip/failure.
    """
    worktree_id = finding.get("worktree_id", "unknown")
    worktree_path = finding.get("worktree_path", "")
    in_repo_path = finding.get("in_repo_path", "")
    kind = finding.get("kind", "unknown")
    size_bytes = finding.get("size_bytes", 0)
    mtime = finding.get("mtime", "")

    marker = _dedup_marker(worktree_id, in_repo_path)

    if not dry_run and _discussion_already_filed(marker):
        print(f"[worktree_state_watcher] already filed for worktree={worktree_id} path={in_repo_path} — skipping")
        return None

    title = f"[Bug] Worktree state divergence: {in_repo_path} is a {kind.replace('_', ' ')} in worktree {worktree_id}"

    kind_description = {
        "real_file": (
            f"The path `{in_repo_path}` inside worktree `{worktree_id}` is a **real file** "
            f"({size_bytes} bytes) instead of a symlink to the external state dir. "
            "This means state writes went to the worktree-local copy and were not shared with "
            "the canonical external state dir — they will be lost when the worktree is reaped."
        ),
        "wrong_symlink": (
            f"The path `{in_repo_path}` inside worktree `{worktree_id}` is a symlink, but points "
            f"to `{finding.get('actual_target', '?')}` instead of the expected target "
            f"`{finding.get('expected_target', '?')}`. State writes likely went to the wrong location."
        ),
    }.get(kind, f"The path `{in_repo_path}` has an unexpected kind: `{kind}`.")

    body = f"""## Worktree state divergence detected

**Worktree ID:** `{worktree_id}`
**Worktree path:** `{worktree_path}`
**Divergent path:** `.autonomous-team/{in_repo_path}`
**Kind:** `{kind}`
**Size:** {size_bytes} bytes
**Last modified:** {mtime}

## Description

{kind_description}

This was detected by `backend/worktree_state_watcher.py`, the detective classifier for D#630's
preventive auto-symlink-on-spawn fix. It means the worktree was either created before D#630 was
deployed, created out-of-band (not via `spawn-agent.sh`), or the symlink setup regressed.

## Suggested repair

Run `setup-state-dir.sh` inside the worktree to migrate the real file to the external state dir
and replace it with the correct symlink:

```bash
cd {worktree_path}
bash scripts/setup-state-dir.sh
```

**Caution:** `setup-state-dir.sh` may overwrite the external canonical file with the worktree-local
copy. Review the contents of `.autonomous-team/{in_repo_path}` before running, or accept the data loss.

{marker}
"""

    if dry_run:
        print(f"[worktree_state_watcher] DRY-RUN would file Discussion:")
        print(f"  Title: {title}")
        print(f"  Marker: {marker}")
        return f"https://github.com/{_GH_REPO}/discussions/dry-run"

    repo_id = _get_repo_id()
    category_id = _get_category_id("General")
    if not repo_id or not category_id:
        print("[worktree_state_watcher] ERROR: could not resolve repo/category IDs for Discussion filing", file=sys.stderr)
        return None

    mutation = """mutation CreateDiscussion($repoId:ID!, $catId:ID!, $title:String!, $body:String!) {
  createDiscussion(input:{repositoryId:$repoId, categoryId:$catId, title:$title, body:$body}) {
    discussion { url number }
  }
}"""
    try:
        result = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query={mutation}",
             "-f", f"repoId={repo_id}",
             "-f", f"catId={category_id}",
             "-f", f"title={title}",
             "-f", f"body={body}"],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            url = data["data"]["createDiscussion"]["discussion"]["url"]
            print(f"[worktree_state_watcher] Filed Discussion: {url}")
            return url
        else:
            print(f"[worktree_state_watcher] ERROR filing Discussion: {result.stderr[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"[worktree_state_watcher] ERROR filing Discussion: {exc}", file=sys.stderr)
    return None


# ── Counter increment ─────────────────────────────────────────────────────────

def _increment_divergence_counter(count: int) -> None:
    """Record worktree_state_divergence_total via stats_writer (best-effort)."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from backend.stats_writer import record  # type: ignore[import]
        record(
            "worktree_state_divergence_total",
            float(count),
            "count",
            tags={"source": "worktree_state_watcher"},
            source="worktree_state_watcher",
        )
    except Exception:
        pass  # Non-fatal — stats_writer may not be available in test environments


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan(dry_run: bool = False) -> list[dict[str, Any]]:
    """
    Walk every active worktree and check each manifest entry.

    Returns the list of findings (each has worktree_id, worktree_path, kind, etc.).
    Bugs are filed (or printed in dry-run) for each finding.
    """
    manifest = load_manifest()
    if not manifest:
        print("[worktree_state_watcher] manifest absent (state-symlinks.json not found) — skipping", file=sys.stderr)
        return []

    worktrees = load_worktrees()
    if not worktrees:
        print("[worktree_state_watcher] no active worktrees in registry — nothing to check")
        return []

    state_dir = _state_dir()
    findings: list[dict[str, Any]] = []

    for wt in worktrees:
        wt_id = wt.get("worktree_id", "unknown")
        wt_path = wt.get("path", "")
        if not wt_path:
            continue
        for entry in manifest:
            result = check_path(wt_path, entry, state_dir)
            if result is not None:
                result["worktree_id"] = wt_id
                result["worktree_path"] = wt_path
                findings.append(result)

    if findings:
        _increment_divergence_counter(len(findings))
        for finding in findings:
            file_bug(finding, dry_run=dry_run)

    return findings


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detective classifier for worktree state files bypassing the external state dir."
    )
    subparsers = parser.add_subparsers(dest="command")
    scan_cmd = subparsers.add_parser("scan", help="Scan active worktrees for state divergence.")
    scan_cmd.add_argument("--dry-run", action="store_true", help="Print findings; do not file Discussions.")
    scan_cmd.add_argument("--json", dest="as_json", action="store_true", help="Output findings as JSON.")
    args = parser.parse_args()

    if args.command != "scan":
        parser.print_help()
        sys.exit(0)

    findings = scan(dry_run=args.dry_run)

    if args.as_json:
        print(json.dumps({"findings": findings, "count": len(findings)}, indent=2))
    elif findings:
        print(f"[worktree_state_watcher] {len(findings)} finding(s):")
        for f in findings:
            print(f"  worktree={f.get('worktree_id')} path={f.get('in_repo_path')} kind={f.get('kind')}")
    else:
        print("[worktree_state_watcher] scan complete — 0 findings")


if __name__ == "__main__":
    main()
