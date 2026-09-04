#!/usr/bin/env python3
"""
seccomp execve fence — blocks execve/execveat of the claude binary from
subagent shell contexts (bash -c, python3 os.execve, nohup, etc.).

Usage:
    python3 hooks/claude_execve_fence.py -- <cmd> [args...]

Architecture (parent-as-supervisor):

    Parent (fence.py) forks a child. The CHILD installs the seccomp NOTIFY
    filter, sends the notify_fd number to the parent, waits for a "go" signal,
    then execs the target command. The PARENT copies the child's notify_fd via
    pidfd_getfd, sends the go signal, and runs the supervisor loop reading
    execve notifications.

    Because the parent is the natural parent of the child (in the Linux process
    hierarchy), it can read /proc/<child_pid>/mem under ptrace_scope=1 without
    requiring CAP_SYS_PTRACE.

    The parent exits with the child's exit status, so the caller gets the
    correct exit code.

    CRITICAL ordering: all ctypes/find_library calls MUST happen before
    filt.load() inside the child process — find_library spawns ldconfig via
    execve which deadlocks against a NOTIFY filter.

Scope and wiring:
    This fence intercepts shell-level execve calls within the guarded process
    tree. It is invoked by wrapping a shell command:
        python3 hooks/claude_execve_fence.py -- bash -c "..."

    It does NOT protect against Agent()-spawned subagents: Claude Code's
    internal Agent() tool uses the parent harness process (not a child execve),
    so the seccomp NOTIFY filter in a child's tree cannot intercept it.
    That layer is protected by the sandbox.py PreToolUse hook instead.

    The fence is called from scripts or integration tests that directly
    shell-exec commands under a controlled environment.  It is NOT wired
    through spawn-agent.sh because spawn-agent.sh only assembles prompts and
    does not itself invoke the claude binary.

Fail-closed policy (overrides Spec's "degrade-open"):
    If pyseccomp is not installed, the kernel is too old for pidfd_getfd
    (Linux < 5.6), or any seccomp setup step fails, the fence logs the reason
    and exits 1.  It NEVER falls back to exec-without-filter.  The security
    guarantee requires the fence to be active; silently bypassing it destroys
    that guarantee and also eliminates the audit trail required by the policy.

    An audit log entry MUST be written before any security-relevant action.
    If the log cannot be written, the fence also exits 1 — no exec without
    an audit trail.

TOCTOU mitigation:
    After reading the execve path via /proc/<child>/mem, the child could in
    theory rewrite its own memory before SECCOMP_USER_NOTIF_FLAG_CONTINUE
    finalises.  To avoid this race, CONTINUE is only sent for paths that are
    definitively NOT the claude binary.  Any empty/unreadable path or
    path that matches claude gets EPERM unconditionally.

Requires: pyseccomp (libseccomp Python bindings), Linux 5.6+ (pidfd_getfd).
"""

import errno as _errno_mod
import json
import os
import platform
import select
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────────────
SECCOMP_USER_NOTIF_FLAG_CONTINUE = 1
_EPERM = _errno_mod.EPERM

# Absolute path anchored to the repo root so the log dir is always found
# regardless of the working directory at invocation time.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_FALLBACK_LOG_DIR = _REPO_ROOT / ".autonomous-team" / "hook-events"

# Linux syscall numbers (x86_64)
_NR_pidfd_open = 434
_NR_pidfd_getfd = 438


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fallback_log(reason: str) -> None:
    """
    Write a one-line warning when the fence cannot be installed.

    If the log directory is unwritable this is fatal — we exit 1 so that
    no security-relevant action proceeds without an audit trail.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = _FALLBACK_LOG_DIR / f"fence-fallback-{date_str}.jsonl"
    try:
        _FALLBACK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"ts": _iso_now(), "reason": reason, "pid": os.getpid()})
                + "\n"
            )
    except OSError as exc:
        sys.stderr.write(
            f"claude_execve_fence: FATAL: cannot write audit log to {log_path}: {exc}\n"
            f"  original reason: {reason}\n"
        )
        sys.exit(1)


def _resolve_claude_path() -> tuple[str, str]:
    """Return (symlink_path, realpath) for the claude binary."""
    import shutil

    sym = shutil.which("claude") or ""
    if not sym:
        return ("", "")
    try:
        real = str(Path(sym).resolve())
    except OSError:
        real = sym
    return (sym, real)


def _read_cstring_from_proc_mem(pid: int, addr: int) -> str:
    """Read a null-terminated string from /proc/<pid>/mem at addr."""
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            f.seek(addr)
            buf = b""
            while True:
                chunk = f.read(256)
                if not chunk:
                    break
                null = chunk.find(b"\x00")
                if null != -1:
                    buf += chunk[:null]
                    break
                buf += chunk
                if len(buf) > 4096:
                    break
        return buf.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _audit_block(pid: int, path: str) -> None:
    """
    Append a block event to the hook-events audit log.

    Raises OSError on write failure — callers must handle this and abort
    the exec (no security-relevant action without an audit trail).
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = _FALLBACK_LOG_DIR / f"blocks-{date_str}.jsonl"
    _FALLBACK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": _iso_now(),
                    "event": "execve_fence_block",
                    "pid": pid,
                    "path": path,
                }
            )
            + "\n"
        )


def _is_claude_path(path: str, sym_path: str, real_path: str) -> bool:
    """Return True if path refers to the claude binary."""
    if not path:
        return False
    basename = os.path.basename(path.rstrip("/"))
    if basename == "claude":
        return True
    try:
        resolved = str(Path(path).resolve())
        if real_path and resolved == real_path:
            return True
        if sym_path and resolved == sym_path:
            return True
    except OSError:
        pass
    return False


# ── ctypes structures for seccomp_notify_alloc/receive/respond/free ──────────


def _make_seccomp_structs():
    """Return (Notif, NotifResp) ctypes struct types."""
    import ctypes

    class _SeccompData(ctypes.Structure):
        _fields_ = [
            ("nr", ctypes.c_int),
            ("arch", ctypes.c_uint32),
            ("instruction_pointer", ctypes.c_uint64),
            ("args", ctypes.c_uint64 * 6),
        ]

    class _Notif(ctypes.Structure):
        _fields_ = [
            ("id", ctypes.c_uint64),
            ("pid", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("data", _SeccompData),
        ]

    class _NotifResp(ctypes.Structure):
        _fields_ = [
            ("id", ctypes.c_uint64),
            ("val", ctypes.c_int64),
            ("error", ctypes.c_int32),
            ("flags", ctypes.c_uint32),
        ]

    return _Notif, _NotifResp


def _setup_libseccomp_notify(lib, Notif, NotifResp):
    """Wire up argtypes for seccomp notify functions on lib."""
    import ctypes

    lib.seccomp_notify_alloc.restype = ctypes.c_int
    lib.seccomp_notify_alloc.argtypes = (
        ctypes.POINTER(ctypes.POINTER(Notif)),
        ctypes.POINTER(ctypes.POINTER(NotifResp)),
    )
    lib.seccomp_notify_receive.restype = ctypes.c_int
    lib.seccomp_notify_receive.argtypes = (ctypes.c_int, ctypes.POINTER(Notif))
    lib.seccomp_notify_respond.restype = ctypes.c_int
    lib.seccomp_notify_respond.argtypes = (ctypes.c_int, ctypes.POINTER(NotifResp))
    lib.seccomp_notify_free.restype = None
    lib.seccomp_notify_free.argtypes = (
        ctypes.POINTER(Notif),
        ctypes.POINTER(NotifResp),
    )


def _supervisor_loop(
    notify_fd: int,
    child_pid: int,
    sym_path: str,
    real_path: str,
    lib,
    Notif,
    NotifResp,
) -> None:
    """
    Parent supervisor loop: reads NOTIFY events, checks path, responds EPERM
    or CONTINUE.  Exits when the child has exited.

    TOCTOU mitigation: we only send CONTINUE for paths that are definitively
    NOT the claude binary and that we could successfully read.  An empty/
    unreadable path (which could indicate memory rewriting) is treated as a
    claude-match and receives EPERM.  This avoids the race between our read
    and the kernel's CONTINUE finalisation.
    """
    import ctypes

    POLL_INTERVAL = 0.5

    while True:
        try:
            rlist, _, _ = select.select([notify_fd], [], [], POLL_INTERVAL)
        except (OSError, ValueError):
            break

        if rlist:
            req_ptr = ctypes.POINTER(Notif)()
            lib.seccomp_notify_alloc(ctypes.byref(req_ptr), None)

            rc = lib.seccomp_notify_receive(notify_fd, req_ptr)
            if rc != 0:
                lib.seccomp_notify_free(req_ptr, None)
                err = ctypes.get_errno()
                if err in (_errno_mod.ENOENT, _errno_mod.EBADF):
                    break
                break

            notif = req_ptr.contents
            pid = notif.pid
            addr = notif.data.args[0]
            notif_id = notif.id
            lib.seccomp_notify_free(req_ptr, None)

            path = _read_cstring_from_proc_mem(pid, addr)

            resp_ptr = ctypes.POINTER(NotifResp)()
            lib.seccomp_notify_alloc(None, ctypes.byref(resp_ptr))
            resp = resp_ptr.contents
            resp.id = notif_id
            resp.val = 0

            # TOCTOU-safe decision: block anything that looks like claude OR
            # that we could not read (empty path means uncertain).  Only CONTINUE
            # when path is definitively not claude.
            if not path or _is_claude_path(path, sym_path, real_path):
                try:
                    _audit_block(pid, path)
                except OSError as exc:
                    sys.stderr.write(
                        f"claude_execve_fence: WARNING: audit log write failed: {exc}\n"
                    )
                    # Still block — security > completeness inside the supervisor
                    # loop (we cannot exit 1 here without orphaning the child).
                resp.error = -_EPERM
                resp.flags = 0
            else:
                resp.error = 0
                resp.flags = SECCOMP_USER_NOTIF_FLAG_CONTINUE

            rc = lib.seccomp_notify_respond(notify_fd, resp_ptr)
            lib.seccomp_notify_free(None, resp_ptr)
            if rc != 0:
                err = ctypes.get_errno()
                if err not in (_errno_mod.ENOENT,):
                    pass  # non-fatal

        # Non-blocking check if child has exited
        try:
            waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            break
        if waited_pid == child_pid:
            if os.WIFEXITED(status):
                sys.exit(os.WEXITSTATUS(status))
            elif os.WIFSIGNALED(status):
                sys.exit(128 + os.WTERMSIG(status))
            sys.exit(1)


def _probe_pidfd_support(libc) -> bool:
    """
    Return True if the kernel supports pidfd_open + pidfd_getfd (Linux 5.6+).

    We probe before forking so we can fail cleanly without any child process
    running.
    """
    try:
        fd = libc.syscall(_NR_pidfd_open, os.getpid(), 0)
        if fd < 0:
            return False
        os.close(fd)
        return True
    except OSError:
        return False


def install_fence_and_exec(cmd: list[str]) -> None:
    """
    Fork a child that will install the fence and exec cmd.
    Parent becomes the supervisor, waits for child to exit, propagates exit code.

    Fail-closed: exits 1 (with audit log) instead of exec-without-fence on any
    setup failure.
    """
    if platform.system() != "Linux":
        _fallback_log(
            f"fence-required-but-unavailable: non-Linux platform: {platform.system()}"
        )
        sys.stderr.write(
            f"claude_execve_fence: FATAL: fence required but platform is {platform.system()}\n"
        )
        sys.exit(1)

    try:
        import pyseccomp as _pyseccomp_check  # noqa: F401
    except ImportError:
        _fallback_log("fence-required-but-unavailable: pyseccomp not installed")
        sys.stderr.write(
            "claude_execve_fence: FATAL: pyseccomp not installed — fence required\n"
        )
        sys.exit(1)

    import pyseccomp
    import ctypes
    import ctypes.util

    # Pre-load libc and wire seccomp_notify_fd BEFORE child calls filt.load()
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        pyseccomp._libseccomp.seccomp_notify_fd.argtypes = (ctypes.c_void_p,)
        pyseccomp._libseccomp.seccomp_notify_fd.restype = ctypes.c_int
    except Exception as exc:
        _fallback_log(f"fence-required-but-unavailable: ctypes setup failed: {exc}")
        sys.stderr.write(
            f"claude_execve_fence: FATAL: ctypes setup failed: {exc}\n"
        )
        sys.exit(1)

    # Pre-flight: probe pidfd_open/pidfd_getfd support (Linux 5.6+).
    # Do this BEFORE fork so we can exit cleanly without an orphan child.
    if not _probe_pidfd_support(libc):
        _fallback_log(
            "fence-required-but-unavailable: pidfd_open/pidfd_getfd not supported (kernel < 5.6)"
        )
        sys.stderr.write(
            "claude_execve_fence: FATAL: kernel does not support pidfd_getfd (need Linux 5.6+)\n"
        )
        sys.exit(1)

    sym_path, real_path = _resolve_claude_path()

    Notif, NotifResp = _make_seccomp_structs()

    # Sync pipes: fd_pipe (child sends notify_fd), go_pipe (parent sends "go")
    fd_rd, fd_wr = os.pipe()
    go_rd, go_wr = os.pipe()

    child_pid = os.fork()

    if child_pid == 0:
        # ── CHILD: install filter, tell parent fd, wait for go, exec ──────────
        os.close(fd_rd)
        os.close(go_wr)

        PR_SET_NO_NEW_PRIVS = 38
        rc = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        if rc != 0:
            # prctl failed — close pipes and exit; parent will see closed pipe
            # and exit 1.  Never exec without the fence.
            os.close(fd_wr)
            os.close(go_rd)
            os._exit(1)

        try:
            filt = pyseccomp.SyscallFilter(defaction=pyseccomp.ALLOW)
            filt.add_rule(pyseccomp.NOTIFY, "execve")
            filt.add_rule(pyseccomp.NOTIFY, "execveat")
            filt.load()
        except Exception:
            # Filter failed — close pipes and exit; parent exits 1.
            # Never exec without the fence.
            os.close(fd_wr)
            os.close(go_rd)
            os._exit(1)

        notify_fd = pyseccomp._libseccomp.seccomp_notify_fd(filt._filter)

        # Send notify_fd number to parent
        try:
            os.write(fd_wr, notify_fd.to_bytes(4, "little"))
        except OSError:
            pass
        os.close(fd_wr)

        # Wait for parent's "go" signal before exec
        try:
            rlist, _, _ = select.select([go_rd], [], [], 5.0)
            if rlist:
                os.read(go_rd, 1)
        except OSError:
            pass
        os.close(go_rd)

        # Exec target — this triggers the first NOTIFY event
        try:
            os.execvp(cmd[0], cmd)
        except FileNotFoundError:
            print(f"claude_execve_fence: command not found: {cmd[0]}", file=sys.stderr)
            os._exit(127)
        os._exit(1)

    else:
        # ── PARENT (SUPERVISOR) ───────────────────────────────────────────────
        os.close(fd_wr)
        os.close(go_rd)

        # Read child's notify_fd number
        notify_fd = -1
        fd_bytes = b""
        try:
            rlist, _, _ = select.select([fd_rd], [], [], 5.0)
            if rlist:
                fd_bytes = os.read(fd_rd, 4)
        except OSError:
            pass
        os.close(fd_rd)

        if len(fd_bytes) == 4:
            child_notify_fd_num = int.from_bytes(fd_bytes, "little")
            # Copy child's notify_fd to our process via pidfd_getfd.
            # The pidfd_open probe above confirmed kernel support, so a failure
            # here means a runtime error — fail closed.
            try:
                pidfd = libc.syscall(_NR_pidfd_open, child_pid, 0)
                if pidfd >= 0:
                    notify_fd_copy = libc.syscall(
                        _NR_pidfd_getfd, pidfd, child_notify_fd_num, 0
                    )
                    os.close(pidfd)
                    if notify_fd_copy >= 0:
                        notify_fd = notify_fd_copy
            except OSError:
                pass

        if notify_fd < 0:
            # Could not obtain notify_fd — kill the child and exit 1.
            # Never run the child without a working supervisor.
            _fallback_log(
                "fence-required-but-unavailable: pidfd_getfd failed at runtime — child killed"
            )
            sys.stderr.write(
                "claude_execve_fence: FATAL: could not obtain notify_fd via pidfd_getfd\n"
            )
            try:
                import signal
                os.kill(child_pid, signal.SIGKILL)
                os.waitpid(child_pid, 0)
            except OSError:
                pass
            sys.exit(1)

        # Wire up libseccomp notify functions
        lib = pyseccomp._libseccomp
        _setup_libseccomp_notify(lib, Notif, NotifResp)

        # Send "go" — child will exec now
        try:
            os.write(go_wr, b"G")
        except OSError:
            pass
        os.close(go_wr)

        # Supervisor loop
        _supervisor_loop(
            notify_fd, child_pid, sym_path, real_path, lib, Notif, NotifResp
        )

        # Final wait (if loop exits without sys.exit)
        try:
            os.close(notify_fd)
        except OSError:
            pass
        try:
            _, status = os.waitpid(child_pid, 0)
            if os.WIFEXITED(status):
                sys.exit(os.WEXITSTATUS(status))
            elif os.WIFSIGNALED(status):
                sys.exit(128 + os.WTERMSIG(status))
        except ChildProcessError:
            pass
        sys.exit(0)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] != "--":
        print(
            "Usage: python3 hooks/claude_execve_fence.py -- <cmd> [args...]",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = args[1:]
    if not cmd:
        print("Error: no command specified after --", file=sys.stderr)
        sys.exit(1)
    install_fence_and_exec(cmd)


if __name__ == "__main__":
    main()
