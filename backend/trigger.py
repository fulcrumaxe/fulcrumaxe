"""
Cron-side trigger — writes a loop iteration request to the TUI's FIFO.
If the TUI is not running, falls back to shelling out directly to the
`claude` CLI. Resolution order: $CLAUDE_BIN env override, then PATH.
(There used to be a third, vendored interpreter fallback here, but nothing
in this repo ever installed a binary there, so that tier could never fire —
dropped rather than carried as dead weight; see the same fix in
run-loop-iteration.sh.) If `claude` can't be resolved, this fails loudly
(see the guard below) rather than crash with a raw traceback.
Usage: python backend/trigger.py "run /loop iteration"
"""
import logging
import shutil
import subprocess
import sys, json, os, time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script: python backend/trigger.py
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

FIFO = "/tmp/af-trigger.fifo"
REPO_DIR = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)
LOCK_PATH = REPO_DIR / ".autonomous-team" / "loop.lock"
SESSION_PATH = REPO_DIR / ".autonomous-team" / "session.json"
NOW_MD_PATH = REPO_DIR / ".autonomous-team" / "now.md"
LOOP_LOG_PATH = REPO_DIR / ".autonomous-team" / "loop.log"


def _check_lockfile() -> bool:
    """
    Return True if we should proceed (no live iteration running).
    Return False if a live iteration is running and we should skip.
    Removes stale lockfiles automatically.
    """
    if not LOCK_PATH.exists():
        return True

    try:
        pid = int(LOCK_PATH.read_text().strip())
    except (ValueError, OSError):
        # Unreadable or corrupt lockfile — remove and proceed.
        LOCK_PATH.unlink(missing_ok=True)
        return True

    try:
        os.kill(pid, 0)
        # PID is alive — skip.
        logger.info("skipping — iteration %s still running", pid)
        return False
    except ProcessLookupError:
        # PID is dead — stale lockfile.
        logger.info("removing stale lockfile (pid %s is dead)", pid)
        LOCK_PATH.unlink(missing_ok=True)
        return True
    except PermissionError:
        # PID exists but we can't signal it — treat as alive, skip.
        logger.info("skipping — iteration %s still running (no permission to signal)", pid)
        return False


def _read_session() -> dict | None:
    """Read and validate session.json. Returns None on missing, corrupt, or expired."""
    if not SESSION_PATH.exists():
        return None
    try:
        data = json.loads(SESSION_PATH.read_text())
        # Validate required fields.
        if not isinstance(data.get("session_id"), str):
            return None
        if not isinstance(data.get("created_at"), str):
            return None
        if not isinstance(data.get("iteration_count"), int):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _write_session(session_id: str, iteration_count: int) -> None:
    """Write session.json atomically."""
    data = {
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "iteration_count": iteration_count,
    }
    tmp = SESSION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(SESSION_PATH)


def _should_rotate(session: dict) -> bool:
    """Return True if the session has exceeded iteration count or age thresholds."""
    max_iterations = int(os.environ.get("AF_SESSION_MAX_ITERATIONS", "20"))
    max_age_minutes = int(os.environ.get("AF_SESSION_MAX_AGE_MINUTES", "120"))

    if session.get("iteration_count", 0) >= max_iterations:
        logger.info("session rotation — reached %s iterations (max %s)", session['iteration_count'], max_iterations)
        return True

    try:
        created_at = datetime.fromisoformat(session["created_at"])
        age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60
        if age_minutes >= max_age_minutes:
            logger.info("session rotation — session is %.1f minutes old (max %s)", age_minutes, max_age_minutes)
            return True
    except (KeyError, ValueError):
        # Bad timestamp — rotate to be safe.
        return True

    return False


def _rotate_session(session: dict) -> None:
    """
    Rotate the session: append a summary section to now.md, delete session.json.
    Summary is built from the last 5 SUMMARY lines in loop.log.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Collect last 5 SUMMARY lines from loop.log.
    summary_lines: list[str] = []
    if LOOP_LOG_PATH.exists():
        try:
            lines = LOOP_LOG_PATH.read_text().splitlines()
            summary_lines = [l for l in lines if "SUMMARY" in l][-5:]
        except OSError:
            pass

    summary_text = "\n".join(summary_lines) if summary_lines else "(no summary entries found)"
    rotation_notice = (
        f"\n## Session rotated at {timestamp}\n"
        f"Session {session.get('session_id', 'unknown')} ran {session.get('iteration_count', 0)} iterations.\n"
        f"Last activity from loop.log:\n```\n{summary_text}\n```\n"
    )

    try:
        existing = NOW_MD_PATH.read_text() if NOW_MD_PATH.exists() else ""
        NOW_MD_PATH.write_text(existing + rotation_notice)
    except OSError as e:
        logger.warning("could not append rotation notice to now.md: %s", e)

    SESSION_PATH.unlink(missing_ok=True)
    logger.info("session rotated — starting fresh")


def _run_preflight() -> tuple[bool, str]:
    """
    Run scripts/loop-preflight.sh. Returns (should_proceed, preflight_summary_json).
    If the script exits non-zero, returns (False, "{}").
    If the script fails to run entirely, returns (True, "{}") and logs a warning
    (module unavailability should not block the loop).
    """
    preflight_script = REPO_DIR / "scripts" / "loop-preflight.sh"
    if not preflight_script.exists():
        logger.warning("loop-preflight.sh not found — skipping pre-flight")
        return True, "{}"

    try:
        result = subprocess.run(
            ["bash", str(preflight_script)],
            capture_output=True,
            text=True,
            cwd=str(REPO_DIR),
        )
    except OSError as e:
        logger.warning("could not run loop-preflight.sh: %s", e)
        return True, "{}"

    if result.returncode != 0:
        logger.warning("pre-flight exited %s — skipping iteration", result.returncode)
        if result.stderr:
            logger.warning("%s", result.stderr)
        return False, "{}"

    summary = result.stdout.strip()
    return True, summary


def main():
    from backend.log import setup_logging
    setup_logging(json_format=False)  # trigger runs in cron context — plain text is more legible

    prompt = " ".join(sys.argv[1:]) or "run /loop iteration"

    if not _check_lockfile():
        sys.exit(0)

    # Automatic backup at the start of every loop iteration — never blocks the loop on failure.
    try:
        from backend.backup import create_backup, prune_backups  # noqa: PLC0415
        create_backup()
        prune_backups(keep=20)
    except Exception as _bk_err:  # noqa: BLE001
        logger.warning("backup failed (non-fatal): %s", _bk_err)

    # Run loop pre-flight: initialize budget, sync registry, check gates.
    should_proceed, preflight_summary = _run_preflight()
    if not should_proceed:
        sys.exit(0)

    # Inject pre-flight summary into the prompt so the Team Lead has it at step 0.
    if preflight_summary and preflight_summary != "{}":
        prompt = f"[Loop pre-flight: {preflight_summary}]\n\n{prompt}"

    # Read session, check rotation.
    session = _read_session()
    if session is not None and _should_rotate(session):
        _rotate_session(session)
        session = None

    session_id = session["session_id"] if session is not None else None

    # Record this iteration in the persistent session history.
    try:
        from backend.session_manager import SessionManager as _SM  # noqa: PLC0415
        _sm = _SM()
        if _sm.current_session() is None:
            _sm.start_session()
        _sm.record_iteration()
    except Exception as _e:  # noqa: BLE001
        logger.warning("session_manager: could not record iteration: %s", _e)

    if os.path.exists(FIFO):
        req = {"id": f"cron-{int(time.time())}", "prompt": prompt, "session_id": session_id}
        with open(FIFO, "w") as f:
            f.write(json.dumps(req) + "\n")
        # On the very first iteration (no session yet), we can't know the session_id
        # until the server responds. The TUI's backend.ts done-event handler writes
        # session.json when it receives the done event. For subsequent iterations
        # (session already exists) we just increment the counter here.
        if session is not None:
            _write_session(session_id, session["iteration_count"] + 1)
        # If session is None (first iteration), backend.ts writes session.json on done.
    else:
        # TUI not running — fall back to a direct `claude` CLI invocation.
        home = Path.home()
        os.environ["GH_CONFIG_DIR"] = str(home / ".config" / "gh")
        os.environ["PATH"] = f"{home / '.local' / 'bin'}:{os.environ.get('PATH', '')}"

        # Resolve claude: $CLAUDE_BIN env override, then PATH. No vendored
        # fallback — nothing in this repo installs a binary under a legacy
        # venv path, so that tier never fired and has been dropped.
        binary_path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
        if not binary_path:
            logger.error(
                "TUI not running and could not resolve claude — checked "
                "$CLAUDE_BIN and PATH. Install the 'claude' CLI, or start "
                "the TUI so cron iterations go through the FIFO instead. "
                "Skipping this iteration."
            )
            sys.exit(1)

        cmd = [binary_path, "-p", prompt]
        if session_id is not None:
            cmd = [binary_path, "--session", session_id, "-p", prompt]
            # Increment iteration count before exec (no chance to do it after).
            _write_session(session_id, session["iteration_count"] + 1)

        os.execv(binary_path, cmd)


if __name__ == "__main__":
    main()
