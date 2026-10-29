#!/usr/bin/env python3
"""
generate-initial-plan.py — emit an initial PLAN-<DATE>.md for a coldstarted project.

Usage:
    python3 scripts/generate-initial-plan.py <project_path> [--date YYYY-MM-DD] \
        [--repo OWNER/NAME] [--force] [--p1-count N]

Reads open Discussions from GitHub (via gh GraphQL), groups them by epic label
or Discussion type, assigns P1/P2/P3 priority, and writes a PLAN-<DATE>.md using
templates/PLAN-template.md as the base.

Idempotent: if PLAN-<DATE>.md already exists, prints a message and exits unless
--force is passed.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_project_json(project_path: Path) -> dict:
    pj = project_path / ".autonomous-team" / "project.json"
    if not pj.exists():
        return {}
    with open(pj) as f:
        return json.load(f)


def gh_list_discussions(repo: str, limit: int = 100) -> list:
    """Fetch open Discussions from the repo via gh GraphQL."""
    owner, name = repo.split("/", 1)
    query = """
query($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    discussions(first: $first, states: OPEN) {
      nodes {
        number
        title
        body
        labels(first: 10) {
          nodes { name }
        }
        category { name }
      }
    }
  }
}
""".strip()

    try:
        result = subprocess.run(
            [
                "gh", "api", "graphql",
                "-F", f"owner={owner}",
                "-F", f"name={name}",
                "-F", f"first={limit}",
                "-f", f"query={query}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"WARNING: gh GraphQL failed: {result.stderr[:200]}", file=sys.stderr)
            return []
        data = json.loads(result.stdout)
        nodes = data["data"]["repository"]["discussions"]["nodes"]
        return nodes
    except Exception as e:
        print(f"WARNING: Failed to fetch discussions: {e}", file=sys.stderr)
        return []


def get_labels(node: dict) -> list:
    return [l["name"] for l in node.get("labels", {}).get("nodes", [])]


def get_epic_label(labels: list) -> str:
    """Return the first epic-N label, or empty string."""
    for label in labels:
        if label.startswith("epic-"):
            return label
    return ""


_LABEL_STATUSES = frozenset(["SPEC_READY", "IMPLEMENTING", "REVIEWING", "SPEC_WRITING"])


def get_spec_status(body: str, labels: list | None = None) -> str:
    """Extract STATUS from Discussion body or labels.

    Body marker takes precedence over label — if both are present and conflict,
    the body marker wins.  Falls back to a recognised GitHub label when the body
    has no STATUS: marker.
    """
    # Body marker first — existing logic unchanged
    for line in (body or "").splitlines():
        if "STATUS:" in line:
            # Extract STATUS:WORD
            for part in line.split():
                if part.startswith("STATUS:"):
                    return part.split(":", 1)[1].strip()
    # Fall back to label
    if labels:
        for label in labels:
            if label in _LABEL_STATUSES:
                return label
    return ""


def group_discussions(discussions: list) -> dict:
    """Group by epic label or category, return dict of group -> list of discussions."""
    groups: dict[str, list] = {}
    for disc in discussions:
        labels = get_labels(disc)
        epic = get_epic_label(labels)
        if epic:
            group = epic
        else:
            cat = disc.get("category", {}) or {}
            group = cat.get("name", "General") or "General"
        groups.setdefault(group, []).append(disc)
    return groups


def load_template(scripts_dir: Path) -> str:
    """Load PLAN-template.md from loop-bootstrap/templates/, scripts/../templates/,
    or the installed location at backend/spawn_templates/.

    Hard-fails (raises FileNotFoundError) if none of the three candidates
    resolve — this used to fall back to a minimal inline template instead,
    but that fallback's placeholder comments didn't match the ones
    render_plan() replaces with actual Discussions (`<!-- Format: D#NNN
    ... -->`). Substitution against it "succeeded" while silently discarding
    every Discussion this script fetched and prioritised: the run reported
    success and wrote a plan file, just one with none of the content it was
    supposed to have (D#2218). A generator that cannot render its content
    must say so loudly, not ship an empty-looking plan that looks fine.
    """
    # Try all known locations in order of preference.
    # bootstrap.sh installs PLAN-template.md to backend/spawn_templates/ in the target.
    candidates = [
        scripts_dir.parent / "templates" / "PLAN-template.md",          # loop-bootstrap source
        scripts_dir / "templates" / "PLAN-template.md",                  # scripts/templates/
        scripts_dir.parent / "backend" / "spawn_templates" / "PLAN-template.md",  # installed target
    ]
    for c in candidates:
        if c.exists():
            return c.read_text()

    raise FileNotFoundError(
        "PLAN-template.md not found in any of:\n  "
        + "\n  ".join(str(c) for c in candidates)
        + "\nbootstrap.sh should have installed it to backend/spawn_templates/ — "
        "re-run loop-bootstrap/bootstrap.sh, or copy "
        "loop-bootstrap/templates/PLAN-template.md to one of the paths above by hand."
    )


def render_plan(
    template: str,
    date: str,
    project_name: str,
    repo: str,
    p1: list,
    p2: list,
    p3: list,
) -> str:
    content = template
    content = content.replace("{{date}}", date)
    content = content.replace("{{project_name}}", project_name)

    def disc_line(d: dict) -> str:
        labels = get_labels(d)
        label_str = " ".join(f"[{l}]" for l in labels[:3] if l) if labels else ""
        title = d["title"][:90]
        return f"- D#{d['number']} — {title} {label_str}".rstrip()

    p1_block = "\n".join(disc_line(d) for d in p1) if p1 else "<!-- none ready -->"
    p2_block = "\n".join(disc_line(d) for d in p2) if p2 else "<!-- none yet -->"
    p3_block = "\n".join(disc_line(d) for d in p3) if p3 else "<!-- none yet -->"

    # Replace the placeholder comment blocks with real content
    content = content.replace(
        "<!-- Format: D#NNN — short title [SPEC_READY] -->",
        p1_block,
    )
    content = content.replace(
        "<!-- Format: D#NNN — short title [DISCUSSING/SPEC_WRITING] -->",
        p2_block,
    )
    # The P3 block is the last one
    content = content.replace(
        "<!-- Format: D#NNN — short title -->",
        p3_block,
    )

    return content


def main():
    parser = argparse.ArgumentParser(description="Generate initial PLAN-<DATE>.md")
    parser.add_argument("project_path", help="Absolute path to the target project repo")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        help="Date for the plan file (YYYY-MM-DD, default: today UTC)")
    parser.add_argument("--repo", default="",
                        help="OWNER/NAME — overrides project.json repo field")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing plan file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print rendered plan to stdout without writing to disk")
    parser.add_argument("--p1-count", type=int, default=5,
                        help="Max SPEC_READY discussions to list as P1 (default: 5)")
    args = parser.parse_args()

    project_path = Path(args.project_path).resolve()
    if not project_path.is_dir():
        print(f"ERROR: project path not found: {project_path}", file=sys.stderr)
        sys.exit(1)

    project_json = load_project_json(project_path)
    project_name = project_json.get("project_name", project_path.name)
    repo = args.repo or project_json.get("repo", "")

    if not repo:
        print("ERROR: --repo OWNER/NAME is required (or set 'repo' in project.json)", file=sys.stderr)
        sys.exit(1)

    autonomous_team_dir = project_path / ".autonomous-team"
    autonomous_team_dir.mkdir(parents=True, exist_ok=True)

    plan_path = autonomous_team_dir / f"PLAN-{args.date}.md"

    if plan_path.exists() and not args.force:
        print(f"PLAN-{args.date}.md already exists at {plan_path}")
        print("Pass --force to overwrite.")
        sys.exit(0)

    print(f"Fetching open Discussions from {repo}...")
    discussions = gh_list_discussions(repo)

    if not discussions:
        print("WARNING: No discussions found (or gh fetch failed). Generating empty plan.")

    # Separate by SPEC_READY vs other statuses
    spec_ready = []
    in_progress = []
    backlog = []

    for disc in discussions:
        status = get_spec_status(disc.get("body", "") or "", get_labels(disc))
        if status == "SPEC_READY":
            spec_ready.append(disc)
        elif status in ("IMPLEMENTING", "REVIEWING", "SPEC_WRITING"):
            in_progress.append(disc)
        else:
            backlog.append(disc)

    # Assign priorities
    p1 = spec_ready[: args.p1_count]
    p2_candidates = spec_ready[args.p1_count :] + in_progress
    p2 = p2_candidates[:10]
    p3 = backlog + p2_candidates[10:]

    # Load template from scripts parent dir
    scripts_dir = Path(__file__).parent
    try:
        template = load_template(scripts_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    content = render_plan(
        template=template,
        date=args.date,
        project_name=project_name,
        repo=repo,
        p1=p1,
        p2=p2,
        p3=p3,
    )

    if args.dry_run:
        print(content)
        print(f"[dry-run] would write to {plan_path}")
        return

    plan_path.write_text(content)
    print(f"Written: {plan_path}")
    print(f"  P1: {len(p1)} SPEC_READY discussions")
    print(f"  P2: {len(p2)} in-progress/queued discussions")
    print(f"  P3: {len(p3)} backlog discussions")


if __name__ == "__main__":
    main()
