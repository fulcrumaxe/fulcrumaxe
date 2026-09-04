"""
Auto-generates wiki/Project-Status.md from existing data sources.

Reads registry.json, loop-metrics.jsonl, config.json, and git log to produce
a Markdown status page. Works gracefully when any data source is missing.

Usage:
    python backend/status_page.py generate         # writes wiki/Project-Status.md
    python backend/status_page.py preview          # prints to stdout, no file write
    python backend/status_page.py --output PATH    # write to custom path
    python backend/status_page.py                  # defaults to generate
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Bootstrap sys.path so `backend.repo_root` is importable when this script is
# invoked directly (python backend/status_page.py) rather than as a module.
_SELF_ROOT = Path(__file__).resolve().parent.parent
if str(_SELF_ROOT) not in sys.path:
    sys.path.insert(0, str(_SELF_ROOT))

from backend.repo_root import main_repo_root

_REPO_ROOT = main_repo_root()
_STATE_DIR = _REPO_ROOT / ".autonomous-team"
_REGISTRY_PATH = _STATE_DIR / "registry.json"
_METRICS_PATH = _STATE_DIR / "loop-metrics.jsonl"
_CONFIG_PATH = _STATE_DIR / "config.json"
_DEFAULT_OUTPUT = _REPO_ROOT / "wiki" / "Project-Status.md"


# ---------------------------------------------------------------------------
# Data loaders — all return safe defaults on failure
# ---------------------------------------------------------------------------


def load_registry(path: Path = _REGISTRY_PATH) -> dict:
    """Read registry.json. Returns empty skeleton on missing or corrupt file."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def load_metrics(path: Path = _METRICS_PATH, n: int = 10) -> list[dict]:
    """Read last N lines of loop-metrics.jsonl. Returns empty list on failure."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(rows) >= n:
                break
        return list(reversed(rows))
    except OSError:
        return []


def get_recent_commits(n: int = 10) -> list[str]:
    """Run git log --oneline -N. Returns empty list on failure."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"-{n}"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def load_config(path: Path = _CONFIG_PATH) -> dict:
    """Read config.json. Returns empty dict on missing or corrupt file."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_status_page(
    registry: dict,
    metrics: list[dict],
    commits: list[str],
    config: dict,
) -> str:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []

    lines.append(f"<!-- generated: {now_iso} -->")
    lines.append("# Project Status")
    lines.append("")
    lines.append(f"_Auto-generated {now_iso}_")
    lines.append("")

    # --- Project Health --------------------------------------------------
    lines.append("## Project Health")
    lines.append("")
    discussions = registry.get("discussions", [])
    if not discussions and not registry:
        lines.append("No registry data available.")
    else:
        # Only count open discussions (closed_at is None) for active-status metrics.
        # DONE discussions are always closed, so counting them over all discussions
        # is intentional — matches registry.py's approach for lifetime totals.
        open_discussions = [d for d in discussions if d.get("closed_at") is None]

        done = sum(1 for d in discussions if d.get("status") == "DONE")
        total = len(discussions)
        in_progress = sum(
            1 for d in open_discussions
            if d.get("status") in ("IMPLEMENTING", "REVIEWING")
        )
        discussing = sum(1 for d in open_discussions if d.get("status") == "DISCUSSING")
        spec_ready = sum(1 for d in open_discussions if d.get("status") == "SPEC_READY")
        completion_rate = round(done / total * 100) if total else 0

        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Total discussions | {total} |")
        lines.append(f"| Completed (DONE) | {done} |")
        lines.append(f"| In progress | {in_progress} |")
        lines.append(f"| Discussing / open | {discussing + spec_ready} |")
        lines.append(f"| Completion rate | {completion_rate}% |")

        velocity = registry.get("velocity", {})
        if velocity.get("tasks_per_day"):
            lines.append(f"| Velocity | {velocity['tasks_per_day']} tasks/day |")
        if velocity.get("avg_days_to_complete") is not None:
            lines.append(f"| Avg days to complete | {velocity['avg_days_to_complete']} days |")

    lines.append("")

    # --- Active Work -----------------------------------------------------
    lines.append("## Active Work")
    lines.append("")
    active = [
        d for d in discussions
        if d.get("status") in ("IMPLEMENTING", "REVIEWING", "DISCUSSING", "SPEC_READY")
    ]
    if active:
        lines.append("| # | Title | Status |")
        lines.append("|---|-------|--------|")
        for d in sorted(active, key=lambda x: x.get("number", 0), reverse=True)[:10]:
            num = d.get("number", "?")
            title = d.get("title", "").replace("|", "\\|")
            status = d.get("status", "")
            lines.append(f"| #{num} | {title} | {status} |")
    else:
        lines.append("No active discussions.")
    lines.append("")

    # --- Recent Activity -------------------------------------------------
    lines.append("## Recent Activity")
    lines.append("")

    recent_done = [d for d in discussions if d.get("status") == "DONE"]
    recent_done.sort(key=lambda x: x.get("closed_at") or x.get("created_at") or "", reverse=True)
    if recent_done[:5]:
        lines.append("**Recently completed:**")
        lines.append("")
        for d in recent_done[:5]:
            num = d.get("number", "?")
            title = d.get("title", "")
            pr = d.get("pr")
            pr_part = f" (PR #{pr})" if pr else ""
            lines.append(f"- #{num}: {title}{pr_part}")
        lines.append("")

    if commits:
        lines.append("**Last 10 commits:**")
        lines.append("")
        lines.append("```")
        for c in commits:
            lines.append(c)
        lines.append("```")
    else:
        lines.append("No commit history available.")
    lines.append("")

    # --- Loop Health -----------------------------------------------------
    lines.append("## Loop Health")
    lines.append("")
    if not metrics:
        lines.append("No loop metrics available.")
    else:
        last = metrics[-1]
        last_ts = last.get("timestamp", "unknown")
        last_duration = last.get("duration_seconds", "?")
        total_iters = len(metrics)
        durations = [m["duration_seconds"] for m in metrics if isinstance(m.get("duration_seconds"), (int, float))]
        avg_duration = round(sum(durations) / len(durations), 1) if durations else None
        total_agents = sum(m.get("agents_spawned", 0) for m in metrics)
        total_merged = sum(m.get("prs_merged", 0) for m in metrics)
        idle_count = sum(1 for m in metrics if m.get("idle"))
        idle_ratio = round(idle_count / len(metrics) * 100) if metrics else 0

        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Last run | {last_ts} |")
        lines.append(f"| Last duration | {last_duration}s |")
        if avg_duration is not None:
            lines.append(f"| Avg duration (last {total_iters}) | {avg_duration}s |")
        lines.append(f"| Agents spawned (last {total_iters}) | {total_agents} |")
        lines.append(f"| PRs merged (last {total_iters}) | {total_merged} |")
        lines.append(f"| Idle ratio | {idle_ratio}% |")

        last_actions = last.get("actions", [])
        if last_actions:
            lines.append("")
            if isinstance(last_actions, list):
                lines.append("**Last loop actions:**")
                for action in last_actions:
                    lines.append(f"- {action}")
            else:
                # Producer writes actions as an int count; display it without iterating.
                lines.append(f"**Last loop actions:** {last_actions}")
    lines.append("")

    # --- Config Gates ----------------------------------------------------
    gates = config.get("gates", {})
    if gates:
        lines.append("## Active Gates")
        lines.append("")
        lines.append("| Gate | Status |")
        lines.append("|------|--------|")
        for gate, enabled in gates.items():
            status = "enabled" if enabled else "disabled"
            lines.append(f"| {gate} | {status} |")
        lines.append("")

    # --- Footer ----------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("_Auto-generated by [status_page.py](../blob/main/backend/status_page.py). Do not edit manually._")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="status_page",
        description="Generate wiki/Project-Status.md from live project data.",
    )
    p.add_argument(
        "command",
        nargs="?",
        choices=["generate", "preview"],
        default="generate",
        help="generate (write file) or preview (stdout only). Default: generate.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Output path (default: {_DEFAULT_OUTPUT})",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write Project-Status.md into. Takes precedence over "
        "--output when both are set — derived artifacts should not land in the "
        "source tree.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    registry = load_registry()
    metrics = load_metrics()
    commits = get_recent_commits()
    config = load_config()

    content = render_status_page(registry, metrics, commits, config)

    if args.command == "preview":
        print(content)
        return 0

    # generate — write to file. --output-dir takes precedence over --output.
    output_path: Path = (args.output_dir / "Project-Status.md") if args.output_dir is not None else args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    print(f"Written: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
