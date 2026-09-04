"""
backend/agent_run.py — Agent transcript browser CLI.

Groups events from .autonomous-team/agent-feed.jsonl into per-agent "runs"
keyed by (role, discussion, pr) and pretty-prints headers, tool timelines,
and verdict summaries.

Usage:
    python3 backend/agent_run.py [--pr N] [--discussion N] [--role X]
                                 [--show-prompt] [--show-tools] [--json]

Exit codes:
    0 — one or more runs printed
    1 — no matching runs (stderr: "no matching runs")
    2 — argument error (argparse default)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure backend/ is importable when run as a script
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.agent_feed as _feed  # noqa: E402 — after sys.path tweak

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(ts_str: str | None) -> datetime | None:
    """Parse an ISO8601 timestamp string; return None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _fmt_ts(dt: datetime | None) -> str:
    """Format datetime as HH:MM:SS, or '?' if None."""
    if dt is None:
        return "?"
    return dt.strftime("%H:%M:%S")


def _duration_s(start: datetime | None, end: datetime | None) -> str:
    """Return '3s' style duration string, or '?' if timestamps missing."""
    if start is None or end is None:
        return "?"
    delta = (end - start).total_seconds()
    return f"{delta:.0f}s"


# ---------------------------------------------------------------------------
# Run grouping
# ---------------------------------------------------------------------------


def _build_runs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group events into runs keyed by (role, discussion, pr).

    Boundaries: agent_start opens a run; agent_end closes it.
    If agent_start is missing, the first event opens the run.
    If agent_end is missing, the run is reported as 'open'.
    """
    # We may have multiple runs for the same (role, discussion, pr) — e.g.
    # retry rounds. Use an ordered list of open slots per key so we can close
    # the correct one on agent_end.
    open_runs: dict[tuple, list[dict[str, Any]]] = {}
    closed_runs: list[dict[str, Any]] = []

    for ev in events:
        role = ev.get("role")
        if not role:
            continue

        disc = ev.get("discussion") or 0
        if disc == 0:
            disc = None

        pr = ev.get("pr") or None
        key = (role, disc, pr)

        etype = ev.get("event_type", "")

        if etype == "agent_start":
            run: dict[str, Any] = {
                "role": role,
                "discussion": disc,
                "pr": pr,
                "started_at": _parse_ts(ev.get("ts")),
                "ended_at": None,
                "verdict": None,
                "tokens": {"input": 0, "output": 0},
                "model": ev.get("model"),
                "tool_events": [],
                "files_touched": [],
                "prompt": None,
                "status": "open",
                "raw_events": [ev],
            }
            open_runs.setdefault(key, []).append(run)

        elif etype == "agent_end":
            slots = open_runs.get(key)
            if slots:
                run = slots.pop(0)
                if not slots:
                    del open_runs[key]
            else:
                # No matching start — create a synthetic run
                run = {
                    "role": role,
                    "discussion": disc,
                    "pr": pr,
                    "started_at": None,
                    "ended_at": None,
                    "verdict": None,
                    "tokens": {"input": 0, "output": 0},
                    "model": None,
                    "tool_events": [],
                    "files_touched": [],
                    "prompt": None,
                    "status": "open",
                    "raw_events": [],
                }

            run["ended_at"] = _parse_ts(ev.get("ts"))
            run["status"] = "closed"
            if ev.get("verdict"):
                run["verdict"] = ev["verdict"]
            if ev.get("model") and not run["model"]:
                run["model"] = ev["model"]
            tok = ev.get("tokens") or {}
            run["tokens"]["input"] += tok.get("input", 0)
            run["tokens"]["output"] += tok.get("output", 0)
            if ev.get("files"):
                run["files_touched"].extend(ev["files"])
            run["raw_events"].append(ev)

            # Extract AGENT_OUTPUT envelope from message if present
            msg = ev.get("message", "")
            if "AGENT_OUTPUT" in msg or ev.get("verdict"):
                if not run["verdict"] and ev.get("verdict"):
                    run["verdict"] = ev["verdict"]

            closed_runs.append(run)

        else:
            # Route into the most recent open run for this key, or start one
            slots = open_runs.get(key)
            if not slots:
                # Implicit run open
                run = {
                    "role": role,
                    "discussion": disc,
                    "pr": pr,
                    "started_at": _parse_ts(ev.get("ts")),
                    "ended_at": None,
                    "verdict": None,
                    "tokens": {"input": 0, "output": 0},
                    "model": ev.get("model"),
                    "tool_events": [],
                    "files_touched": [],
                    "prompt": None,
                    "status": "open",
                    "raw_events": [],
                }
                open_runs.setdefault(key, []).append(run)
                slots = open_runs[key]

            run = slots[-1]
            run["raw_events"].append(ev)

            # Accumulate tokens
            tok = ev.get("tokens") or {}
            run["tokens"]["input"] += tok.get("input", 0)
            run["tokens"]["output"] += tok.get("output", 0)

            # Track model
            if ev.get("model") and not run["model"]:
                run["model"] = ev["model"]

            # Collect tool events
            extra = ev.get("extra") or {}
            if extra.get("tool") or ev.get("files"):
                tool_ev = {
                    "ts": _parse_ts(ev.get("ts")),
                    "tool": extra.get("tool", ""),
                    "target": extra.get("target", ""),
                    "ok": extra.get("ok", True),
                }
                run["tool_events"].append(tool_ev)

            # Files from log events
            if ev.get("files"):
                run["files_touched"].extend(ev["files"])

    # Flush open runs as "open" status
    for key_runs in open_runs.values():
        for run in key_runs:
            closed_runs.append(run)

    return closed_runs


# ---------------------------------------------------------------------------
# Prompt lookup
# ---------------------------------------------------------------------------


def _find_prompt(run: dict[str, Any]) -> str | None:
    """Attempt to find a transcript file matching this run's time window.

    Returns file contents or None on miss/error.
    """
    try:
        started = run.get("started_at")
        ended = run.get("ended_at")
        start_ts = started.timestamp() if started else None
        end_ts = (ended.timestamp() + 60) if ended else (
            (started.timestamp() + 3600) if started else None
        )

        patterns = [
            "/tmp/claude-*/projects/*/tasks/*.output",
            "/tmp/claude-*/**/tasks/*.output",
        ]
        candidates = []
        for pattern in patterns:
            try:
                candidates.extend(glob.glob(pattern, recursive=True))
            except OSError:
                pass

        best: str | None = None
        for path in candidates:
            try:
                mtime = os.path.getmtime(path)
                if start_ts is not None and end_ts is not None:
                    if start_ts <= mtime <= end_ts:
                        best = path
                        break
                elif start_ts is None:
                    best = path
                    break
            except OSError:
                continue

        if best is None:
            return None

        with open(best, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    except OSError:
        return None


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------


def _print_run(run: dict[str, Any], show_prompt: bool, show_tools: bool) -> None:
    """Pretty-print a single run to stdout."""
    disc_str = f"discussion={run['discussion']}" if run["discussion"] is not None else "discussion=none"
    pr_str = f"pr={run['pr']}" if run["pr"] is not None else "pr=none"
    verdict = run["verdict"] or ("open" if run["status"] == "open" else "unknown")
    tok = run["tokens"]
    tok_str = f"{tok['input']}/{tok['output']}"
    model = run["model"] or "unknown"
    dur = _duration_s(run["started_at"], run["ended_at"])

    print(
        f"{'─' * 72}\n"
        f"{run['role']} | {disc_str} | {pr_str} | verdict={verdict} | "
        f"tokens={tok_str} | model={model} | duration={dur}"
    )

    if show_tools:
        tool_events = run["tool_events"]
        if tool_events:
            for tev in tool_events:
                ts_s = _fmt_ts(tev["ts"])
                tool = tev["tool"] or "(unknown)"
                target = tev["target"] or ""
                ok = "true" if tev["ok"] else "false"
                line = f"  {ts_s}  tool={tool}"
                if target:
                    line += f"  target={target}"
                line += f"  ok={ok}"
                print(line)
        else:
            print("  (no tool events recorded)")

    files = list(dict.fromkeys(run["files_touched"]))  # deduplicate, preserve order
    if files:
        print(f"  files: {', '.join(files)}")

    if show_prompt:
        prompt_text = _find_prompt(run)
        if prompt_text:
            print("  --- prompt/transcript ---")
            print(prompt_text)
            print("  --- end transcript ---")
        else:
            print("  (transcript not retained)")


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def _run_to_json(run: dict[str, Any], show_prompt: bool) -> dict[str, Any]:
    """Serialize a run to a JSON-safe dict."""
    started = run["started_at"]
    ended = run["ended_at"]
    tool_events_out = []
    for tev in run["tool_events"]:
        tool_events_out.append(
            {
                "ts": tev["ts"].isoformat() if tev["ts"] else None,
                "tool": tev["tool"],
                "target": tev["target"],
                "ok": tev["ok"],
            }
        )
    prompt_text: str | None = None
    if show_prompt:
        prompt_text = _find_prompt(run)

    dur = None
    if started and ended:
        dur = round((ended - started).total_seconds(), 1)

    return {
        "role": run["role"],
        "discussion": run["discussion"],
        "pr": run["pr"],
        "verdict": run["verdict"],
        "tokens": run["tokens"],
        "model": run["model"],
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "duration_seconds": dur,
        "tool_events": tool_events_out,
        "files_touched": list(dict.fromkeys(run["files_touched"])),
        "prompt": prompt_text,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Browse agent runs from .autonomous-team/agent-feed.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pr", type=int, metavar="N", help="Filter by PR number")
    parser.add_argument(
        "--discussion", type=int, metavar="N", help="Filter by Discussion number"
    )
    parser.add_argument("--role", metavar="X", help="Filter by agent role")
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Attempt to load and print the spawn transcript for each run",
    )
    parser.add_argument(
        "--show-tools",
        action="store_true",
        default=True,
        help="Include tool-use timeline (default: on)",
    )
    parser.add_argument(
        "--json",
        dest="json_out",
        action="store_true",
        help="Emit structured JSON array; suppresses decorative output",
    )

    args = parser.parse_args(argv)

    # Build predicate from filters
    def predicate(ev: dict[str, Any]) -> bool:
        role = ev.get("role")
        if not role:
            return False
        if args.role and role != args.role:
            return False
        if args.pr is not None:
            ev_pr = ev.get("pr") or None
            if ev_pr != args.pr:
                return False
        if args.discussion is not None:
            ev_disc = ev.get("discussion") or 0
            if ev_disc == 0:
                ev_disc = None
            if ev_disc != args.discussion:
                return False
        return True

    events = list(_feed.filter(predicate))
    runs = _build_runs(events)

    if not runs:
        print("no matching runs", file=sys.stderr)
        return 1

    if args.json_out:
        out = [_run_to_json(r, args.show_prompt) for r in runs]
        print(json.dumps(out, indent=2, default=str))
    else:
        for run in runs:
            _print_run(run, args.show_prompt, args.show_tools)
        print("─" * 72)

    return 0


if __name__ == "__main__":
    sys.exit(main())
