"""live_analyst_daemon.py — Live-analyst background daemon (Discussion #574 PR-b).

Watches agent transcript files, runs live-mode classifiers every 30 seconds,
and writes intervention messages to agent FIFOs when hard-rule violations are detected.

Usage:
    python3 backend/live_analyst_daemon.py [--poll-interval 30] [--dry-run]

Gate: gates.live_run_analyst must be true to start.
PID file: .autonomous-team/live-analyst.pid

HARD RULE: This daemon MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop. It reads transcripts and writes to FIFOs only.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Shared tail primitives — secret scrubbing and spawn discovery live in
# transcript_tailer.py so the CLI and daemon share one implementation.
# Import with try/except for graceful degradation during incremental rollout.
try:
    from transcript_tailer import scrub_secrets, discover_active_spawns  # noqa: F401
    _TAILER_AVAILABLE = True
except ImportError:
    _TAILER_AVAILABLE = False

# Repository root — resolved from this file's location
REPO_ROOT = Path(__file__).resolve().parent.parent

# Allow running as a script from repo root: `python3 backend/live_analyst_daemon.py`
sys.path.insert(0, str(REPO_ROOT))

from backend._repo import PROJECT_TRANSCRIPT_SLUG  # noqa: E402

AUTONOMOUS_TEAM_DIR = REPO_ROOT / ".autonomous-team"
PID_FILE = AUTONOMOUS_TEAM_DIR / "live-analyst.pid"
INTERVENTION_LOG = AUTONOMOUS_TEAM_DIR / "intervention-log.jsonl"
INTERVENTION_LIBRARY = AUTONOMOUS_TEAM_DIR / "intervention-library.json"
WORKTREES_JSON = AUTONOMOUS_TEAM_DIR / "worktrees.json"

# Transcript glob pattern — watch all Claude agent output files
TRANSCRIPT_GLOB = f"/tmp/claude-*/{PROJECT_TRANSCRIPT_SLUG}/*/tasks/*.output"
# Fallback glob for non-standard layouts
TRANSCRIPT_GLOB_ALT = "/tmp/claude-*/*/tasks/*.output"

# Default configuration
DEFAULT_POLL_INTERVAL = 30  # seconds
DEFAULT_INTERVENTION_CAP = 3  # per-agent, across entire run

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [live-analyst] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("live_analyst_daemon")


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()


def _handle_signal(signum: int, _frame) -> None:  # type: ignore[type-arg]
    logger.info("received signal %d — shutting down", signum)
    _shutdown_event.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------

def check_gate() -> bool:
    """Return True if gates.live_run_analyst is enabled."""
    try:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "backend" / "control_plane.py"),
             "get", "gates.live_run_analyst"],
            capture_output=True, text=True, timeout=5,
        )
        val = result.stdout.strip().strip('"').lower()
        return val == "true"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Intervention library
# ---------------------------------------------------------------------------

def load_intervention_library() -> dict:
    """Load .autonomous-team/intervention-library.json."""
    if not INTERVENTION_LIBRARY.exists():
        logger.warning("intervention-library.json not found — using empty library")
        return {}
    try:
        data = json.loads(INTERVENTION_LIBRARY.read_text())
        return data.get("classifiers", {})
    except Exception as exc:
        logger.error("failed to load intervention library: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Intervention log (per-agent cap)
# ---------------------------------------------------------------------------

def _load_intervention_log() -> list[dict]:
    if not INTERVENTION_LOG.exists():
        return []
    records = []
    try:
        for line in INTERVENTION_LOG.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return records


def count_agent_interventions(agent_id: str) -> int:
    """Count interventions already sent to a given agent_id."""
    records = _load_intervention_log()
    return sum(1 for r in records if r.get("agent_id") == agent_id)


def append_intervention_log(record: dict) -> None:
    """Append one JSON record to the intervention log."""
    try:
        INTERVENTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(INTERVENTION_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.error("failed to write intervention log: %s", exc)


# ---------------------------------------------------------------------------
# Agent ID extraction from transcript path
# ---------------------------------------------------------------------------

def agent_id_from_path(path: str) -> str:
    """Extract a stable agent identifier from the transcript file path.

    Claude Code stores transcripts under:
      /tmp/claude-<session>/.../<agent_id>/tasks/<task>.output

    We use the parent directory name of the 'tasks' dir as the agent ID.
    Falls back to the full path hash if the structure is unexpected.
    """
    p = Path(path)
    # Walk up: tasks/ → <agent_dir>/
    if p.parent.name == "tasks":
        return p.parent.parent.name
    # Fallback: use directory containing the file
    return p.parent.name


# ---------------------------------------------------------------------------
# FIFO routing: find the FIFO for a given agent
# ---------------------------------------------------------------------------

def find_agent_fifo(agent_id: str) -> Optional[str]:
    """Look up the FIFO path for an agent.

    First checks .autonomous-team/worktrees.json for a registered FIFO path.
    Falls back to scanning /tmp for a FIFO named after the agent_id.

    Returns None if no FIFO is found.
    """
    # Check worktrees registry
    if WORKTREES_JSON.exists():
        try:
            worktrees = json.loads(WORKTREES_JSON.read_text())
            if isinstance(worktrees, list):
                for wt in worktrees:
                    if isinstance(wt, dict) and wt.get("agent_id") == agent_id:
                        fifo = wt.get("fifo_path") or wt.get("trigger_fifo")
                        if fifo and os.path.exists(fifo):
                            return fifo
        except Exception:
            pass

    # Fallback: look for a FIFO named after the agent under /tmp
    candidates = [
        f"/tmp/claude-{agent_id}/af-agent.fifo",
        f"/tmp/af-agent-{agent_id}.fifo",
        f"/tmp/claude-{agent_id[:8]}/af-agent.fifo",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    # Scan /tmp for any FIFO associated with this agent's session directory
    try:
        for entry in Path("/tmp").iterdir():
            if agent_id[:8] in entry.name and entry.is_dir():
                fifo = entry / "af-agent.fifo"
                if fifo.exists():
                    return str(fifo)
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Write intervention to agent FIFO
# ---------------------------------------------------------------------------

def write_intervention(fifo_path: str, message: str) -> bool:
    """Write an intervention message to the agent's FIFO (non-blocking).

    Returns True on success, False on failure.
    The write is attempted with O_NONBLOCK to avoid blocking if the agent
    is not reading. A failed write is logged but does not crash the daemon.
    """
    try:
        fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            payload = json.dumps({
                "id": f"live-analyst-{int(time.time())}",
                "prompt": message,
            }) + "\n"
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        logger.debug("FIFO write failed for %s: %s", fifo_path, exc)
        return False


# ---------------------------------------------------------------------------
# Team-log helper
# ---------------------------------------------------------------------------

def post_team_log(message: str) -> None:
    """Post one line to the team-log via rotate-team-log.sh (non-fatal)."""
    script = REPO_ROOT / "scripts" / "rotate-team-log.sh"
    if not script.exists():
        return
    try:
        subprocess.run(
            ["bash", str(script), "comment", message],
            timeout=15, capture_output=True,
        )
    except Exception as exc:
        logger.debug("team-log post failed: %s", exc)


# ---------------------------------------------------------------------------
# Per-file state: byte offset tracking
# ---------------------------------------------------------------------------

class FileState:
    """Track per-transcript-file state across daemon poll cycles."""

    __slots__ = ("path", "byte_offset", "last_modified")

    def __init__(self, path: str) -> None:
        self.path = path
        self.byte_offset: int = 0
        self.last_modified: float = 0.0


# ---------------------------------------------------------------------------
# Core worker: process one transcript file
# ---------------------------------------------------------------------------

def process_transcript(
    state: FileState,
    library: dict,
    dry_run: bool = False,
) -> None:
    """Run live classifiers on the transcript since the last byte offset.

    Updates state.byte_offset in place. Writes interventions when classifiers
    fire and the per-agent cap has not been reached.
    """
    path = state.path

    try:
        stat = os.stat(path)
    except OSError:
        return  # file disappeared

    if stat.st_mtime == state.last_modified and stat.st_size <= state.byte_offset:
        return  # nothing new

    agent_id = agent_id_from_path(path)

    # Call run_analyst.py --live via subprocess to isolate import side effects
    cmd = [
        sys.executable,
        str(REPO_ROOT / "backend" / "run_analyst.py"),
        "--live",
        "--transcript", path,
        "--since-byte", str(state.byte_offset),
    ]
    # Build child environment with REPO_ROOT on PYTHONPATH so that
    # `from backend._repo import ...` in run_analyst.py resolves correctly
    # regardless of where the daemon itself was started.
    child_env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("run_analyst.py --live timed out for %s", path)
        return
    except Exception as exc:
        logger.error("run_analyst.py --live failed for %s: %s", path, exc)
        return

    if result.returncode != 0:
        logger.warning(
            "run_analyst.py --live exited %d for %s: %s",
            result.returncode, path, result.stderr[:200],
        )
        return

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("could not parse run_analyst.py --live output for %s", path)
        return

    # Update byte offset from this run
    new_offset = data.get("next_byte_offset", state.byte_offset)
    state.byte_offset = new_offset
    state.last_modified = stat.st_mtime

    findings = data.get("findings", [])
    if not findings:
        return

    # Count interventions already sent to this agent (load once for this batch)
    current_count = count_agent_interventions(agent_id)

    for finding in findings:
        category = finding.get("category", "")
        if category not in library:
            logger.debug("category %r not in allowlist — skipping", category)
            continue

        cfg = library[category]
        cap = cfg.get("max_per_agent", DEFAULT_INTERVENTION_CAP)

        # Cap semantics: max_per_agent is a GLOBAL cap on the total number of
        # interventions sent to this agent across ALL classifiers combined, not
        # a per-classifier limit. If an agent has already received `cap`
        # interventions for any mix of findings, no further interventions fire
        # for any category. This is intentional: it prevents intervention spam
        # when multiple classifiers trigger in quick succession on the same
        # agent run. The cap is reset between agent sessions (different
        # agent_id values), so a fresh agent starts with a clean slate.
        if current_count >= cap:
            logger.info(
                "agent %s hit intervention cap (%d/%d) for %s — skipping",
                agent_id[:12], current_count, cap, category,
            )
            continue

        message = cfg.get("message_template", "")
        if not message:
            continue

        fifo_path = find_agent_fifo(agent_id)
        written = False
        if fifo_path and not dry_run:
            written = write_intervention(fifo_path, message)
        elif dry_run:
            written = True  # dry-run counts as successful

        if written or dry_run:
            # Log to intervention-log.jsonl
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent_id": agent_id,
                "transcript": path,
                "classifier": category,
                "finding_title": finding.get("title", "")[:120],
                "fifo": fifo_path or "(not found)",
                "dry_run": dry_run,
            }
            if not dry_run:
                append_intervention_log(record)
            current_count += 1

            # One-line preview for team-log
            preview = message[:80].replace("\n", " ")
            ts_str = datetime.now(timezone.utc).strftime("%H:%M")
            log_line = (
                f"[{ts_str}] live-analyst: intervened agent={agent_id[:12]} "
                f"classifier={category} message={preview!r}"
            )
            logger.info(log_line)
            if not dry_run:
                post_team_log(log_line)


# ---------------------------------------------------------------------------
# Transcript file discovery
# ---------------------------------------------------------------------------

def discover_transcripts() -> list[str]:
    """Return all currently existing .output transcript files."""
    paths: list[str] = []
    for pattern in (TRANSCRIPT_GLOB, TRANSCRIPT_GLOB_ALT):
        paths.extend(glob.glob(pattern, recursive=False))
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


# ---------------------------------------------------------------------------
# inotifywait watcher (Linux) — fallback to poll
# ---------------------------------------------------------------------------

def _start_inotifywait(watch_dirs: list[str]) -> Optional[subprocess.Popen]:
    """Start inotifywait on the given directories. Returns Popen or None."""
    if not watch_dirs:
        return None
    try:
        # inotifywait -m: monitor continuously, -r: recursive, -e modify,close_write
        cmd = ["inotifywait", "-m", "-r", "-e", "modify,close_write,create",
               "--format", "%w%f"] + watch_dirs
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        return proc
    except FileNotFoundError:
        return None  # inotifywait not available — fall back to polling
    except Exception as exc:
        logger.debug("inotifywait start failed: %s", exc)
        return None


def _get_watch_dirs() -> list[str]:
    """Return directories to watch with inotifywait."""
    dirs: set[str] = set()
    for p in discover_transcripts():
        parent = str(Path(p).parent.parent)  # tasks/ → agent dir
        dirs.add(parent)
    # Also include /tmp so we detect new agents
    try:
        for entry in Path("/tmp").iterdir():
            if entry.name.startswith("claude-") and entry.is_dir():
                dirs.add(str(entry))
    except Exception:
        pass
    return list(dirs)


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

def run_daemon(poll_interval: int = DEFAULT_POLL_INTERVAL, dry_run: bool = False) -> None:
    """Main daemon loop.

    Uses inotifywait when available (Linux); falls back to polling every
    poll_interval seconds.
    """
    logger.info(
        "live-analyst daemon starting (poll_interval=%ds, dry_run=%s)",
        poll_interval, dry_run,
    )

    library = load_intervention_library()
    if not library:
        logger.warning("intervention library is empty — no interventions will fire")

    # Per-file state keyed by path
    file_states: dict[str, FileState] = {}

    # Try inotifywait for change detection
    watch_dirs = _get_watch_dirs()
    inotify_proc = _start_inotifywait(watch_dirs)
    if inotify_proc:
        logger.info("inotifywait active — event-driven mode")
    else:
        logger.info("inotifywait unavailable — poll-fallback mode (every %ds)", poll_interval)

    last_poll = 0.0

    while not _shutdown_event.is_set():
        now = time.monotonic()

        # Determine whether to run a scan pass
        should_scan = False
        changed_paths: set[str] = set()

        if inotify_proc:
            # Non-blocking read of inotify events since last iteration
            assert inotify_proc.stdout is not None
            try:
                inotify_proc.stdout.fileno()  # ensure still open
                import select
                ready, _, _ = select.select([inotify_proc.stdout], [], [], 0.1)
                if ready:
                    line = inotify_proc.stdout.readline().strip()
                    if line and line.endswith(".output"):
                        changed_paths.add(line)
                        should_scan = True
            except Exception:
                # inotifywait died — fall back to polling
                inotify_proc = None
                logger.info("inotifywait died — switching to poll mode")

        # Always do a full poll pass every poll_interval seconds
        if now - last_poll >= poll_interval:
            should_scan = True
            last_poll = now

        if not should_scan:
            _shutdown_event.wait(timeout=0.5)
            continue

        # Refresh transcript list
        all_paths = discover_transcripts()
        # Add any paths triggered by inotify
        for cp in changed_paths:
            if cp not in all_paths and os.path.exists(cp):
                all_paths.append(cp)

        # Ensure all known paths have state
        for path in all_paths:
            if path not in file_states:
                file_states[path] = FileState(path)

        # Process each known file
        for path, state in list(file_states.items()):
            if _shutdown_event.is_set():
                break
            try:
                process_transcript(state, library, dry_run=dry_run)
            except Exception as exc:
                logger.error("error processing %s: %s", path, exc)

        # Reload library periodically (picks up edits without restart)
        library = load_intervention_library()

    logger.info("live-analyst daemon stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live-analyst daemon — watches agent transcripts and injects interventions.",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between poll passes (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run classifiers but do not write to any FIFO or intervention log.",
    )
    parser.add_argument(
        "--skip-gate-check", action="store_true",
        help="Skip the gates.live_run_analyst gate check (for testing only).",
    )
    args = parser.parse_args()

    if not args.skip_gate_check:
        if not check_gate():
            print(
                "ERROR: gates.live_run_analyst is not enabled. "
                "Set it with: python3 backend/control_plane.py set gates.live_run_analyst true",
                file=sys.stderr,
            )
            return 1

    # Write PID file
    try:
        AUTONOMOUS_TEAM_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
    except Exception as exc:
        logger.warning("could not write PID file: %s", exc)

    try:
        run_daemon(poll_interval=args.poll_interval, dry_run=args.dry_run)
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
