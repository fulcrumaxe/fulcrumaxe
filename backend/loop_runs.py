#!/usr/bin/env python3
"""
backend/loop_runs.py — per-iteration loop run recorder + tail CLI.

Write side (called from run-loop-iteration.sh):
  python3 backend/loop_runs.py start              → writes started_at stub, prints filename
  python3 backend/loop_runs.py finish --file F     \
    --exit N [--stderr PATH]                       → finalises the file

Read side:
  python3 backend/loop_runs.py tail [--n 10] [--failures-only]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _runs_dir(repo_root: Path | None = None) -> Path:
    """Return .autonomous-team/loop-runs/ dir, creating it if needed."""
    if repo_root is None:
        # Derive from this file's location: backend/ → repo root
        repo_root = Path(__file__).resolve().parent.parent
    d = repo_root / ".autonomous-team" / "loop-runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts_to_filename(ts: str) -> str:
    """Convert ISO8601 timestamp to a filename-safe string.

    2026-05-15T02:46:00Z  →  2026-05-15T02-46-00Z.json
    Colons are replaced with hyphens so the name is safe on all platforms.
    """
    safe = ts.replace(":", "-")
    if not safe.endswith(".json"):
        safe += ".json"
    return safe


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    """Write a stub JSON file and print its path to stdout."""
    ts = _now_iso()
    runs_dir = _runs_dir()
    filename = _ts_to_filename(ts)
    path = runs_dir / filename

    stub = {
        "started_at": ts,
        "finished_at": None,
        "exit_code": None,
        "duration_s": None,
        "last_stderr_lines": [],
    }
    path.write_text(json.dumps(stub, indent=2) + "\n")
    print(str(path))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    """Finalise a loop-run file with exit code, duration, and stderr tail."""
    run_file = Path(args.file)
    if not run_file.exists():
        print(f"loop_runs finish: file not found: {run_file}", file=sys.stderr)
        return 1

    try:
        data = json.loads(run_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"loop_runs finish: cannot read {run_file}: {exc}", file=sys.stderr)
        return 1

    finished_at = _now_iso()
    started_at = data.get("started_at", finished_at)

    # Compute duration from ISO strings (seconds, integer)
    try:
        from datetime import datetime as _dt
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start_epoch = _dt.strptime(started_at, fmt).replace(
            tzinfo=timezone.utc
        ).timestamp()
        end_epoch = _dt.strptime(finished_at, fmt).replace(
            tzinfo=timezone.utc
        ).timestamp()
        duration_s = int(end_epoch - start_epoch)
    except Exception:
        duration_s = 0

    # Read last_stderr_lines (last 20 lines, truncated to 4KB total)
    last_stderr_lines: list[str] = []
    if args.stderr:
        stderr_path = Path(args.stderr)
        if stderr_path.exists():
            try:
                raw = stderr_path.read_text(errors="replace")
                # Truncate to 4KB before splitting
                if len(raw) > 4096:
                    raw = raw[-4096:]
                last_stderr_lines = [l for l in raw.splitlines() if l][-20:]
            except OSError:
                pass

    data["finished_at"] = finished_at
    data["exit_code"] = args.exit
    data["duration_s"] = duration_s
    data["last_stderr_lines"] = last_stderr_lines

    # Atomic write
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(run_file.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, str(run_file))
    except OSError as exc:
        os.unlink(tmp_path)
        print(f"loop_runs finish: write failed: {exc}", file=sys.stderr)
        return 1

    # Prune oldest files, keeping last 1000
    _prune(run_file.parent, keep=1000)
    return 0


def _prune(runs_dir: Path, keep: int = 1000) -> None:
    """Delete oldest JSON files beyond the keep limit. Best-effort."""
    try:
        files = sorted(runs_dir.glob("*.json"))
        excess = len(files) - keep
        if excess > 0:
            for old in files[:excess]:
                try:
                    old.unlink()
                except OSError:
                    pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------

def cmd_tail(args: argparse.Namespace) -> int:
    """Print recent loop runs as a one-line-per-run table."""
    runs_dir = _runs_dir()
    files = sorted(runs_dir.glob("*.json"))

    if not files:
        print("no loop runs recorded yet")
        return 0

    # Parse files — newest last in sorted order, so take tail
    rows: list[dict] = []
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # Skip stubs (not yet finished)
        if data.get("exit_code") is None:
            continue
        rows.append(data)

    if args.failures_only:
        rows = [r for r in rows if r.get("exit_code", 0) != 0]

    # Take last n rows
    rows = rows[-args.n:]

    if not rows:
        if args.failures_only:
            print("no failed loop runs recorded yet")
        else:
            print("no loop runs recorded yet")
        return 0

    # Print table: timestamp  exit  duration_s  brief_reason
    header = f"{'timestamp':<25} {'exit':>4} {'duration_s':>10}  last_stderr"
    print(header)
    print("-" * 70)
    for r in rows:
        ts = r.get("started_at", "?")[:19]  # trim trailing Z, keep 19 chars
        if ts and ts.endswith("T"):
            ts = ts[:-1]
        exit_code = r.get("exit_code", "?")
        dur = r.get("duration_s", "?")
        # Brief reason: first non-empty stderr line, truncated
        stderr_lines = r.get("last_stderr_lines", [])
        reason = ""
        for line in stderr_lines:
            stripped = line.strip()
            if stripped:
                reason = stripped[:60]
                break
        print(f"{ts:<25} {str(exit_code):>4} {str(dur):>10}  {reason}")

    return 0


# ---------------------------------------------------------------------------
# Latest-failure path helper (used by watchdog)
# ---------------------------------------------------------------------------

def latest_failing_run_path(repo_root: Path | None = None) -> str | None:
    """Return path of the most recent loop-run JSON with non-zero exit_code, or None."""
    runs_dir = _runs_dir(repo_root)
    files = sorted(runs_dir.glob("*.json"), reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ec = data.get("exit_code")
        if ec is not None and ec != 0:
            return str(path)
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loop_runs.py",
        description="Loop iteration exit-code recorder and tail CLI",
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("start", help="Write a new loop-run stub, print its path")

    fin = sub.add_parser("finish", help="Finalise a loop-run file")
    fin.add_argument("--file", required=True, help="Path returned by 'start'")
    fin.add_argument("--exit", type=int, required=True,
                     dest="exit", help="Loop iteration exit code")
    fin.add_argument("--stderr", default="",
                     help="Path to stderr file (optional)")

    t = sub.add_parser("tail", help="Print recent loop runs")
    t.add_argument("--n", type=int, default=10, help="Number of rows to show")
    t.add_argument("--failures-only", action="store_true",
                   help="Show only non-zero exit iterations")

    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.cmd == "start":
        return cmd_start(args)
    elif args.cmd == "finish":
        return cmd_finish(args)
    elif args.cmd == "tail":
        return cmd_tail(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
