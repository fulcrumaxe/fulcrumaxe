"""tests/test_process_watchdog_patterns.py

Regression guard for scripts/process-watchdog.sh.

process-watchdog.sh finds processes older than 30 minutes whose full
command line matches one of PATTERNS (via `pgrep -f`), and signals them
unless they are in a protected-PID set. Because pgrep -f matches the
*entire* command line, a pattern that is a substring of an unrelated, live
path is a live-fire risk, not a cosmetic issue — and a protected-PID set
that is empty is not a fallback, it's a loaded gun with the safety off.

D#1863 fixed two independent defects at once:
  1. REPO_DIR was an absolute checkout path matching /home/(agent|jp) that
     exists nowhere real, so every pattern was effectively unanchored.
  2. Even with REPO_DIR corrected, the protected-PID pidfile names
     (tui.pid, server.pid) were never written by anything in the tree —
     they were phantom filenames, so the protected set was empty on every
     machine including ours. Fixing REPO_DIR alone does not fix this; the
     pidfile discovery had to move to a glob over .autonomous-team/*.pid
     (see D2 in the Discussion).

This file extends (rather than parallels) the original two-test file,
because a prior version of that file shipped a test
(test_retired_cli_pattern_not_present) that mutation testing showed was
fully subsumed by its sibling and carried zero independent signal, while
neither test asserted anything about a process outside the repo — which is
the entire subject of this Discussion. Every test below is designed to
fail under a targeted mutation of the specific behavior it claims to
guard, not just "some test in this file went red."
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = REPO_ROOT / "scripts" / "process-watchdog.sh"

# The same pattern scripts/check-no-hardcoded-checkout-paths.sh scans with,
# assembled from two pieces so this line does not match itself when that guard
# scans this file. The guard's own header explains the same dodge.
_CHECKOUT_PATH_RE = re.compile("/home/" + "(agent|jp)")
PIDDIR = REPO_ROOT / ".autonomous-team"

sys.path.insert(0, str(REPO_ROOT))
from hooks.sandbox_rules import _WORKTREE_PREFIXES, MAIN_REPO_ROOT

# The retired worktree-agent path prefix — read from the live source of
# truth rather than retyped here. _WORKTREE_PREFIXES has three entries: the
# current .claude/worktrees/ prefix, the retired one, and a /tmp/wt- prefix.
_KNOWN_OTHER_PREFIXES = {
    str(MAIN_REPO_ROOT / ".claude" / "worktrees") + "/",
    "/tmp/wt-",
}
_RETIRED_PREFIX = next(p for p in _WORKTREE_PREFIXES if p not in _KNOWN_OTHER_PREFIXES)

# A representative command line for a live worktree agent using that prefix.
LIVE_WORKTREE_AGENT_CMDLINE = f"python3 {_RETIRED_PREFIX}abc123def456/backend/trigger.py run"


def _extract_patterns() -> list[str]:
    """Extract the PATTERNS bash array from process-watchdog.sh without running the watchdog."""
    script = WATCHDOG.read_text()
    m = re.search(r"^PATTERNS=\((.*?)^\)", script, re.MULTILINE | re.DOTALL)
    assert m, "PATTERNS array not found in scripts/process-watchdog.sh"
    return re.findall(r'"([^"]*)"', m.group(1))


def _bash_extended_regex_matches(pattern: str, cmdline: str) -> bool:
    """Mirror pgrep -f's matching: bash [[ cmdline =~ pattern ]] (POSIX ERE)."""
    result = subprocess.run(
        ["bash", "-c", '[[ "$1" =~ $2 ]]', "_", cmdline, pattern],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_patterns_do_not_match_live_worktree_agent_cmdline():
    """No PATTERNS entry may match a live worktree-agent command line.

    Fails if the retired-CLI pattern (a substring of the retired worktree
    path prefix) is reintroduced to PATTERNS; passes as long as PATTERNS
    doesn't collide with that live path prefix.
    """
    patterns = _extract_patterns()
    assert patterns, "expected at least one pattern in PATTERNS"

    matches = [p for p in patterns if _bash_extended_regex_matches(p, LIVE_WORKTREE_AGENT_CMDLINE)]
    assert matches == [], (
        f"PATTERNS entries {matches} match a live worktree-agent command line "
        f"({LIVE_WORKTREE_AGENT_CMDLINE!r}) — the watchdog would kill live agents."
    )


def test_patterns_are_exactly_two_and_repo_dir_anchored():
    """PATTERNS has exactly two entries, both anchored under the script's own REPO_DIR.

    Asserted programmatically against the resolved REPO_DIR, not by string
    equality against a hardcoded list — so this fails under either
    mutation: re-adding a third entry (e.g. "opencode"), or replacing an
    anchored entry with an unanchored one (e.g. "python.*server\\.py").
    This single check subsumes the old direct "opencode is absent" guard,
    without the subsumption problem that guard had: dropping either
    assertion below independently breaks this test for a distinct reason
    (wrong count vs. wrong anchor).
    """
    patterns = _extract_patterns()
    assert len(patterns) == 2, f"expected exactly 2 patterns, got {len(patterns)}: {patterns}"
    for p in patterns:
        assert p.startswith("$REPO_DIR/"), f"pattern {p!r} is not anchored under $REPO_DIR"


def _run_watchdog(args: list[str] | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PATH", "/usr/bin:/bin")
    if env_extra:
        env.update(env_extra)
    cmd = ["bash", str(WATCHDOG)] + (args or [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)


def _spawn_sleeper(argv0: str, extra_argv: list[str] | None = None) -> subprocess.Popen:
    """Spawn a background process whose argv0 is exactly `argv0`.

    Uses `exec -a` so the running process's /proc/<pid>/cmdline shows the
    exact path we want to test against, without actually executing that
    path (which could be a real service with side effects, e.g. binding a
    port). A trailing `true` after `sleep` prevents bash's tail-call exec
    optimization from replacing argv0 with the real "sleep" binary's own
    name once sleep is the last simple command.
    """
    inner = "sleep 300; true"
    if extra_argv:
        inner = " ".join(extra_argv) + "; " + inner
    proc = subprocess.Popen(
        ["bash", "-c", 'exec -a "$1" bash -c "$2"', "_", argv0, inner],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.3)  # let exec -a land before the caller inspects /proc
    return proc


def _kill_quietly(proc: subprocess.Popen) -> None:
    """Kill `proc` and anything it forked.

    Several helpers here spawn `bash -c '...; sleep N; true'`: the trailing
    `true` forces bash to fork a child for `sleep` rather than exec-replacing
    itself, so SIGKILLing just the parent leaves that child running as an
    orphan for the rest of its sleep. Every spawner in this file therefore
    starts its own session (start_new_session=True), which makes the
    spawned process its own process group leader — pgid == its own pid, by
    construction, for as long as that group exists. That means the group
    can be targeted by `proc.pid` directly even after the leader itself
    has already been reaped (e.g. by the watchdog's own --kill in the test
    that exercises it) and its PID is no longer a valid os.getpgid() lookup
    — killing by the numeric pgid still reaches any surviving child.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    except Exception:
        pass


def _candidate_line(stdout: str, pid: int) -> str | None:
    """The per-candidate verdict line for `pid`, distinct from the summary "protected PIDs: ..." line."""
    prefix = f"process-watchdog: PID {pid} "
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line
    return None


def test_decoy_outside_repo_not_selected():
    """A process outside the repo whose cmdline resembles the old broad patterns is not selected.

    Baseline (recorded, not asserted here — it's a property of pgrep -f
    itself, not of this repo's script): pgrep -af 'python.*server\\.py' on
    this host returned a decoy python process started from a scratch
    directory outside the repo (measured 2026-08-17, PID 3132676 /
    3173022 in independent runs). The old PATTERNS entry
    "python.*server\\.py" is an unanchored regex that matches any command
    line containing "server.py" preceded by "python" and anything — including
    a decoy that has nothing to do with this project.

    This test proves the new, anchored pattern does not have that problem:
    a decoy server.py run from a scratch directory is never even a pgrep
    candidate, because the anchored pattern requires the literal
    $REPO_DIR/dashboard/server.py path to appear in the command line.
    """
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        assert not str(scratch_path).startswith(str(REPO_ROOT)), "scratch dir must be outside the repo"

        decoy_py = scratch_path / "server.py"
        decoy_py.write_text("import time\ntime.sleep(300)\n")
        decoy_dist = scratch_path / "dist"
        decoy_dist.mkdir()
        (decoy_dist / "index.js").write_text("setTimeout(() => {}, 300000);\n")

        # Relative invocation (cwd=scratch), matching the Discussion's own
        # live reproduction: `pgrep -af 'python.*server\.py'` returned
        # `python3 ./server.py`. This also means the decoy's argv element is
        # the literal text "server.py" — the same text an unanchored
        # PATTERNS entry would be — so this test is sensitive to anchoring
        # specifically, not just to the separate argv-exact-element gate.
        py_proc = subprocess.Popen(
            ["python3", "server.py"], cwd=str(scratch_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        js_proc = subprocess.Popen(
            ["node", "dist/index.js"], cwd=str(scratch_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            time.sleep(0.5)

            # Baseline, reproduced live: the OLD unanchored patterns DO match
            # these decoys' actual command lines. This is what makes the new
            # anchored pattern a real fix and not a no-op.
            py_cmdline = Path(f"/proc/{py_proc.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            assert _bash_extended_regex_matches(r"python.*server\.py", py_cmdline), (
                "expected the OLD broad pattern to match the python decoy "
                f"(cmdline={py_cmdline!r}) — baseline assumption violated"
            )

            result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
            assert result.returncode == 0
            assert str(py_proc.pid) not in result.stdout, (
                f"decoy python PID {py_proc.pid} outside the repo was selected:\n{result.stdout}"
            )
            assert str(js_proc.pid) not in result.stdout, (
                f"decoy node PID {js_proc.pid} outside the repo was selected:\n{result.stdout}"
            )
        finally:
            _kill_quietly(py_proc)
            _kill_quietly(js_proc)


def test_live_in_repo_target_matches_pattern_and_is_protected_independently():
    """The anchored pattern still matches a genuine in-repo target — and protection is a separate mechanism.

    D#1863 correction 3 warns that a fix which narrows the pattern into
    matching *nothing* would pass a naive "decoy not selected" check while
    leaving the watchdog unable to recognize its own protected services at
    all. This test asserts both facts against the same process so that
    outcome is not possible:
      1. the pattern DOES match a process whose cmdline is the real
         anchored path ($REPO_DIR/dashboard/server.py) — proven by running
         the watchdog with nothing protecting it, and observing it as a
         dry-run kill candidate;
      2. when a pidfile lists that same PID, it flips to protected — the
         watchdog now names it explicitly as skipped for that reason.
    A process is used rather than the real dashboard/server.py service to
    avoid side effects (binding the real service's port); exec -a gives it
    the real target's exact argv0 without executing that file.
    """
    target = str(REPO_ROOT / "dashboard" / "server.py")
    proc = _spawn_sleeper(target)
    pidfile = PIDDIR / f"test-watchdog-{proc.pid}.pid"
    try:
        # Unprotected: matches the pattern, and (with age forced to 0) is a
        # dry-run kill candidate rather than being silently ignored.
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, proc.pid)
        assert line is not None, (
            f"expected in-repo target PID {proc.pid} to match the anchored pattern:\n{result.stdout}"
        )
        assert "DRY-RUN: would signal" in line, line

        # Now protect it via a pidfile and confirm the verdict flips.
        pidfile.write_text(str(proc.pid))
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, proc.pid)
        assert line is not None, f"expected protected PID {proc.pid} to still be logged:\n{result.stdout}"
        assert "SKIP: protected" in line, f"expected protected PID {proc.pid} to be skipped:\n{line}"
    finally:
        pidfile.unlink(missing_ok=True)
        _kill_quietly(proc)


def test_sibling_text_match_is_not_selected():
    """A process whose cmdline merely *mentions* the anchored path as text is not selected.

    pgrep -f matches the whole command line, so a sibling process — another
    agent's shell, a grep, an editor — whose command line happens to
    contain the anchored path as a substring of some longer argument would
    otherwise be selected. Baseline, reproduced live: raw `pgrep -f`
    against the escaped anchored pattern DOES return such a process; the
    watchdog's argv-exact-element validation (D5) must reject it.
    """
    target = str(REPO_ROOT / "dashboard" / "server.py")
    escaped = re.sub(r"([][\\.^$*+?(){}|])", r"\\\1", target)

    # `; true` prevents bash's tail-call exec optimization from discarding
    # the original -c text (which contains `target` as a substring, not as
    # its own argv element) once sleep would otherwise become the last
    # simple command.
    proc = subprocess.Popen(
        ["bash", "-c", f'echo "{target}" >/dev/null; sleep 300; true'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(0.5)
        cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()

        # Baseline: raw pgrep -f (no argv validation) DOES select this PID.
        pgrep = subprocess.run(
            ["pgrep", "-f", escaped], capture_output=True, text=True, check=False
        )
        assert str(proc.pid) in pgrep.stdout.split(), (
            f"expected baseline pgrep -f to match the sibling text-match process "
            f"(cmdline={cmdline!r}); if this fails the test setup itself is wrong"
        )

        # Fixed behavior: the watchdog's argv-exact validation rejects it —
        # it may still be *logged* as a skipped candidate (that's the point
        # of the readable dry-run output), but never as a kill candidate.
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, proc.pid)
        assert line is not None, f"expected a logged verdict for PID {proc.pid}:\n{result.stdout}"
        assert "DRY-RUN: would signal" not in line and "KILLED" not in line, (
            f"sibling text-match PID {proc.pid} was selected despite not being "
            f"an exact argv element:\n{line}"
        )
        assert "SKIP" in line, line
    finally:
        _kill_quietly(proc)


def test_no_signal_without_kill_flag_then_kill_flag_signals():
    """Both directions of D4: dry-run by default, --kill required to act.

    A dry-run flag that is also dry under --kill would be a silent no-op —
    exactly this Discussion's failure mode repeated — so both directions
    are asserted against the same sacrificial process.
    """
    target = str(REPO_ROOT / "tui" / "dist" / "index.js")
    proc = _spawn_sleeper(target)
    try:
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        assert result.returncode == 0
        assert proc.poll() is None, "process was signaled despite no --kill flag"
        assert "KILLED" not in result.stdout

        result = _run_watchdog(["--kill"], env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        assert result.returncode == 0
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        assert proc.poll() is not None, "process was not reaped by --kill"
    finally:
        _kill_quietly(proc)


def test_protected_pid_glob_includes_live_pidfile_and_rejects_stale_one():
    """D2: protected set is built by globbing .autonomous-team/*.pid, with validation.

    Covers three cases: a pidfile holding a live, numeric PID is protected;
    a pidfile holding a numeric-but-dead PID is not (the kill -0 check); a
    pidfile holding garbage text is not (the ^[0-9]+$ check), and does not
    crash the script (set -u is on).

    The live PID is an independently spawned process, not this test's own
    PID or one of pytest's — the watchdog's separate ancestor-chain walk
    would otherwise protect the test/pytest process regardless of whether
    the pidfile glob logic works at all, masking exactly the bug this test
    exists to catch.
    """
    live_proc = subprocess.Popen(
        ["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    time.sleep(0.3)
    dead_proc = subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dead_pid = dead_proc.pid
    dead_proc.wait(timeout=5)  # now guaranteed not running

    live_pidfile = PIDDIR / "test-watchdog-live.pid"
    dead_pidfile = PIDDIR / "test-watchdog-dead.pid"
    garbage_pidfile = PIDDIR / "test-watchdog-garbage.pid"
    live_pidfile.write_text(str(live_proc.pid))
    dead_pidfile.write_text(str(dead_pid))
    garbage_pidfile.write_text("not-a-pid")
    try:
        result = _run_watchdog()
        assert result.returncode == 0, "a non-numeric pidfile value must not crash the script"
        protected_line = result.stdout.split("protected PIDs:", 1)[1].splitlines()[0]
        assert str(live_proc.pid) in protected_line, (
            f"expected live pidfile's PID to appear in the protected set:\n{protected_line}"
        )
        assert str(dead_pid) not in protected_line.split(), (
            f"a numeric but dead PID from a stale pidfile must not be trusted:\n{protected_line}"
        )
    finally:
        live_pidfile.unlink(missing_ok=True)
        dead_pidfile.unlink(missing_ok=True)
        garbage_pidfile.unlink(missing_ok=True)
        _kill_quietly(live_proc)


def test_no_hardcoded_checkout_path_literal():
    """The hardcoded checkout path that made the protected set silently empty is gone.

    Asserts against the *pattern* the checkout-path guard scans for rather than
    the one spelling D#1863 happened to remove. The old assertion would have
    passed on a re-hardcode under a different user's home, which is the same
    defect wearing a different name — and spelling the literal out here made
    this file flag itself in the guard's own scan.
    """
    script = WATCHDOG.read_text()
    hit = _CHECKOUT_PATH_RE.search(script)
    assert hit is None, (
        f"REPO_DIR must be resolved, not hardcoded — found {hit.group()!r}"
        if hit else ""
    )


def test_repo_dir_uses_bash_source_idiom_not_git():
    """D1: REPO_DIR is resolved via BASH_SOURCE, not `git rev-parse` (unavailable in the export, wrong under a worktree)."""
    script = WATCHDOG.read_text()
    assert "git rev-parse" not in script
    assert "BASH_SOURCE" in script


def test_shellcheck_clean():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not on PATH")
    result = subprocess.run([shellcheck, str(WATCHDOG)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# D#2006: orphaned pytest detection.
#
# Three orphaned full-suite `timeout N python3 -m pytest ...` runs were
# found reparented to systemd, running 4.6-5.6 hours past their own bound,
# because bare `timeout N` (no --kill-after) sends SIGTERM once and then
# waits forever for a child that never takes the hint. The watchdog's
# second pass detects any process where all three of these hold:
#   1. ppid == 1 (genuinely orphaned)
#   2. age >= MAX_AGE_SEC
#   3. "pytest" is an exact argv element
#
# Every helper here spawns a *fake* process — a bash instance that never
# actually runs pytest — and tags it with a unique marker, so these tests
# can find and clean up exactly the process they created even while other
# agents' real pytest suites are running concurrently on the same host
# (this is precisely the contention D#2006 documents; these tests must
# never select or touch anyone else's process).
# ---------------------------------------------------------------------------


def _read_ppid(pid: int) -> int | None:
    """Read a process's PPid from /proc/<pid>/status. None if the process is gone."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    for line in text.splitlines():
        if line.startswith("PPid:"):
            return int(line.split(":", 1)[1].strip())
    return None


def _spawn_orphaned(argv_tail: list[str], inner: str = "sleep 300; true") -> tuple[int, str]:
    """Spawn a process whose own argv (after 'bash -c inner') is exactly
    argv_tail plus a unique marker, and whose immediate parent exits right
    away so it is reparented to init (ppid == 1) — the same shape as the
    Discussion's own evidence: a `timeout N python3 -m pytest ...` wrapper
    left running when its own parent shell exits. Returns (pid, marker).
    """
    marker = f"WD-TEST-{os.getpid()}-{time.time_ns()}"
    full_tail = [*argv_tail, marker]
    quoted = " ".join(shlex.quote(a) for a in full_tail)
    subprocess.run(
        ["bash", "-c", f'(setsid bash -c {shlex.quote(inner)} {quoted} &)'],
        check=True,
    )
    deadline = time.time() + 3
    while time.time() < deadline:
        result = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        for token in result.stdout.split():
            cand = int(token)
            if _read_ppid(cand) == 1:
                return cand, marker
        time.sleep(0.1)
    raise AssertionError(f"failed to spawn+orphan a process tagged {marker!r}")


def _spawn_orphaned_wrapper_and_child(
    argv_tail: list[str], inner: str = 'trap "" TERM; sleep 300; true'
) -> tuple[int, int, str]:
    """Spawn a genuine two-level orphaned tree: a real `timeout`-wrapped
    bash process (the 'wrapper', matching the D#2006 incident's own
    evidence) with a live child underneath it that traps and discards
    SIGTERM the same way the real incident processes did. Both processes
    get reparented to init (ppid == 1 for the wrapper) once the spawning
    shell backgrounds and exits.

    Neither process ever execs python or pytest -- `python3 -m pytest ...`
    appears only as inert positional arguments to `bash -c` (assigned to
    $0/$1/... inside the script, never invoked), which is enough to tag
    both the wrapper's and the child's argv for detection without ever
    running a real test suite.

    Returns (wrapper_pid, child_pid, marker).
    """
    marker = f"WD-TEST-{os.getpid()}-{time.time_ns()}"
    full_tail = [*argv_tail, marker]
    quoted = " ".join(shlex.quote(a) for a in full_tail)
    subprocess.run(
        ["bash", "-c", f'(setsid timeout 300 bash -c {shlex.quote(inner)} {quoted} &)'],
        check=True,
    )

    wrapper_pid = None
    deadline = time.time() + 3
    while time.time() < deadline and wrapper_pid is None:
        result = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        for token in result.stdout.split():
            cand = int(token)
            if _read_ppid(cand) != 1:
                continue
            try:
                comm = Path(f"/proc/{cand}/comm").read_text().strip()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if comm == "timeout":
                wrapper_pid = cand
                break
        if wrapper_pid is None:
            time.sleep(0.1)
    if wrapper_pid is None:
        raise AssertionError(f"failed to spawn+orphan a wrapper tagged {marker!r}")

    child_pid = None
    deadline = time.time() + 3
    while time.time() < deadline and child_pid is None:
        result = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        for token in result.stdout.split():
            cand = int(token)
            if cand != wrapper_pid and _read_ppid(cand) == wrapper_pid:
                child_pid = cand
                break
        if child_pid is None:
            time.sleep(0.1)
    if child_pid is None:
        raise AssertionError(f"wrapper {wrapper_pid} has no live child tagged {marker!r}")

    return wrapper_pid, child_pid, marker


def _spawn_orphaned_argv0_pytest(inner_body: str = "sleep 300; true") -> tuple[int, str]:
    """Spawn an orphaned process whose own argv[0] is literally 'pytest'
    (via bash's `exec -a`), without ever executing the real pytest binary
    -- it's bash itself running `inner_body`, with its displayed argv[0]
    overridden. Exercises the bare-executable invocation shape
    (argv[0] basename == "pytest"), distinct from the "-m pytest" module
    shape the other synthetic helpers cover.
    """
    marker = f"WD-TEST-{os.getpid()}-{time.time_ns()}"
    tagged = f": {marker}; {inner_body}"
    launcher = f"exec -a pytest bash -c {shlex.quote(tagged)}"
    subprocess.run(
        ["bash", "-c", f'(setsid bash -c {shlex.quote(launcher)} &)'],
        check=True,
    )
    deadline = time.time() + 3
    while time.time() < deadline:
        result = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        for token in result.stdout.split():
            cand = int(token)
            if _read_ppid(cand) == 1:
                return cand, marker
        time.sleep(0.1)
    raise AssertionError(f"failed to spawn+orphan an argv0=pytest process tagged {marker!r}")


def _spawn_direct_child(argv_tail: list[str], inner: str = "sleep 300; true") -> subprocess.Popen:
    """Spawn a process with argv (after 'bash -c inner') exactly argv_tail,
    whose parent is this test process itself — i.e. NOT reparented, so
    ppid != 1. Used to prove the ppid==1 condition is load-bearing.
    """
    marker = f"WD-TEST-{os.getpid()}-{time.time_ns()}"
    proc = subprocess.Popen(
        ["bash", "-c", inner, *argv_tail, marker],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.3)
    return proc


def _kill_orphan_quietly(pid: int) -> None:
    """Best-effort cleanup for a pid returned by _spawn_orphaned (its own session/pgid leader)."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _kill_orphan_tree_quietly(*pids: int | None) -> None:
    """Best-effort cleanup for pids from _spawn_orphaned_wrapper_and_child.

    `timeout` (without --foreground, the default) puts its monitored child
    in a *separate* process group from its own, so the wrapper and child
    are not necessarily reachable via a single killpg -- kill each pid and
    its own process group explicitly.
    """
    for pid in pids:
        if pid is None:
            continue
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def test_orphaned_pytest_all_conditions_true_is_detected_and_named():
    """Positive case (D#2006 acceptance item 7, non-vacuity): a synthetic
    orphaned pytest process — ppid==1, old enough, "pytest" an exact argv
    element — is found and named in dry-run output, and is left alive
    (dry-run sends no signal; acceptance item 3)."""
    pid, marker = _spawn_orphaned(["python3", "-m", "pytest", "tests/", "backend/tests/", "-q"])
    try:
        assert _read_ppid(pid) == 1, "test setup broken: spawned process was not orphaned"
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        assert result.returncode == 0
        line = _candidate_line(result.stdout, pid)
        assert line is not None, f"expected orphaned pytest PID {pid} to be named:\n{result.stdout}"
        assert "orphaned pytest" in line, line
        assert "DRY-RUN: would signal" in line, line
        assert os.kill(pid, 0) is None, "dry-run must not have signalled the process"
    finally:
        _kill_orphan_quietly(pid)


def test_orphaned_pytest_condition_ppid_is_load_bearing():
    """D#2006 acceptance item 2: dropping ppid==1 (a live, non-orphaned
    child with the same argv and age override) must stop detection."""
    proc = _spawn_direct_child(["python3", "-m", "pytest", "tests/", "-q"])
    try:
        assert _read_ppid(proc.pid) != 1, "test setup broken: process is unexpectedly orphaned"
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, proc.pid)
        assert line is None or "orphaned pytest" not in line, (
            f"a non-orphaned pytest-argv process was selected:\n{result.stdout}"
        )
    finally:
        _kill_quietly(proc)


def test_orphaned_pytest_condition_age_is_load_bearing():
    """D#2006 acceptance item 2: dropping the age threshold (a fresh orphan,
    no MAX_AGE_SEC override — the production 1800s default applies) must
    stop detection, even though ppid==1 and the argv match both hold."""
    pid, marker = _spawn_orphaned(["python3", "-m", "pytest", "tests/", "-q"])
    try:
        assert _read_ppid(pid) == 1, "test setup broken: spawned process was not orphaned"
        result = _run_watchdog()  # no age override — process is seconds old
        line = _candidate_line(result.stdout, pid)
        assert line is None or (
            "DRY-RUN: would signal" not in line and "KILLED" not in line
        ), f"a too-young orphaned pytest process was selected as a candidate:\n{result.stdout}"
    finally:
        _kill_orphan_quietly(pid)


def test_orphaned_pytest_condition_argv_element_is_load_bearing():
    """D#2006 acceptance item 2: dropping the exact-argv-element match (an
    orphaned, old-enough process whose argv does not contain "pytest")
    must stop detection, even though ppid==1 and age both hold."""
    pid, marker = _spawn_orphaned(["python3", "-m", "unittest", "tests/", "-q"])
    try:
        assert _read_ppid(pid) == 1, "test setup broken: spawned process was not orphaned"
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, pid)
        assert line is None or "orphaned pytest" not in line, (
            f"an orphan without 'pytest' as an argv element was selected:\n{result.stdout}"
        )
    finally:
        _kill_orphan_quietly(pid)


def test_orphaned_pytest_kill_escalates_sigterm_ignoring_process_to_sigkill():
    """D#2006 acceptance item 4: the Discussion's own evidence is that these
    processes ignore SIGTERM — all three real orphans needed -KILL. Proves
    the escalation actually happens (SIGKILL), not merely attempted once,
    against a process that traps and discards SIGTERM."""
    pid, marker = _spawn_orphaned(
        ["python3", "-m", "pytest", "tests/", "-q"],
        # trailing `true` prevents bash's tail-call exec optimization from
        # replacing this process's argv with plain "sleep 300" (discarding
        # both the trap and the tagged argv) once sleep would otherwise be
        # the last simple command — see _spawn_sleeper's docstring above.
        inner='trap "" TERM; sleep 300; true',
    )
    try:
        assert _read_ppid(pid) == 1, "test setup broken: spawned process was not orphaned"
        result = _run_watchdog(["--kill"], env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        assert result.returncode == 0
        line = _candidate_line(result.stdout, pid)
        assert line is not None, f"expected a verdict line for PID {pid}:\n{result.stdout}"
        assert "KILLED: SIGKILL" in line, (
            f"expected escalation to SIGKILL against a SIGTERM-ignoring process, got:\n{line}"
        )
        deadline = time.time() + 5
        while _read_ppid(pid) is not None and time.time() < deadline:
            time.sleep(0.1)
        assert _read_ppid(pid) is None, f"PID {pid} survived --kill escalation"
    finally:
        _kill_orphan_quietly(pid)


def test_orphaned_pytest_protected_pid_not_signalled():
    """D#2006 acceptance item 5: protected PIDs are never signalled, using
    the SAME protected-set construction (pidfile glob under
    .autonomous-team/*.pid) the PATTERNS pass already relies on — not a
    second mechanism."""
    pid, marker = _spawn_orphaned(["python3", "-m", "pytest", "tests/", "-q"])
    pidfile = PIDDIR / f"test-watchdog-pytest-{pid}.pid"
    try:
        assert _read_ppid(pid) == 1, "test setup broken: spawned process was not orphaned"
        pidfile.write_text(str(pid))
        result = _run_watchdog(["--kill"], env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, pid)
        assert line is not None, f"expected protected PID {pid} to still be logged:\n{result.stdout}"
        assert "SKIP: protected" in line, f"expected protected orphan to be skipped:\n{line}"
        assert _read_ppid(pid) == 1, "a protected PID must not be signalled"
    finally:
        pidfile.unlink(missing_ok=True)
        _kill_orphan_quietly(pid)


def test_orphaned_pytest_kill_escalates_two_level_tree_reaps_child():
    """D#2006 code-review finding 1 (binding item): the documented incident
    shape is a two-level tree, not a single flat process -- a real
    `timeout`-wrapped process (the one this watchdog actually detects,
    since its own argv carries the "-m pytest" tokens) with the real work
    running underneath it as a separate, live child PID. `kill -KILL`
    against a single PID never cascades to children, so SIGKILL-ing only
    the detected wrapper leaves the child alive and re-orphaned a second
    time while the watchdog still reports a successful kill -- the exact
    false-success failure mode this Discussion is about. Proves
    escalate_kill reaps BOTH the wrapper and its child, not just the
    detected PID.
    """
    wrapper_pid, child_pid, marker = _spawn_orphaned_wrapper_and_child(
        ["python3", "-m", "pytest", "tests/", "-q"],
    )
    try:
        assert _read_ppid(wrapper_pid) == 1, "test setup broken: wrapper was not orphaned"
        assert _read_ppid(child_pid) == wrapper_pid, "test setup broken: child not parented to wrapper"

        result = _run_watchdog(["--kill"], env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        assert result.returncode == 0
        line = _candidate_line(result.stdout, wrapper_pid)
        assert line is not None, f"expected a verdict line for wrapper PID {wrapper_pid}:\n{result.stdout}"
        assert "KILLED: SIGKILL" in line, (
            f"expected escalation to SIGKILL against a SIGTERM-ignoring wrapper, got:\n{line}"
        )

        deadline = time.time() + 5
        while (
            (_read_ppid(wrapper_pid) is not None or _read_ppid(child_pid) is not None)
            and time.time() < deadline
        ):
            time.sleep(0.1)
        assert _read_ppid(wrapper_pid) is None, f"wrapper PID {wrapper_pid} survived --kill escalation"
        assert _read_ppid(child_pid) is None, (
            f"child PID {child_pid} survived --kill escalation -- the wrapper was reaped but the "
            "real work kept running, re-orphaned, while the watchdog reported KILLED: SIGKILL"
        )
    finally:
        _kill_orphan_tree_quietly(wrapper_pid, child_pid)


def test_orphaned_grep_pytest_argument_not_matched():
    """D#2006 code-review finding 2: "pytest" as a bare argument to an
    unrelated command must not be treated as a pytest invocation, even when
    it is an exact standalone argv element, orphaned, and old enough. A
    command like `grep pytest somefile.txt` (a plausible real command --
    an agent grepping a log for the word "pytest") has "pytest" as an
    exact argv element too, but it is not a pytest invocation.
    """
    pid, marker = _spawn_orphaned(["grep", "pytest", "somefile.txt"], inner="sleep 300; true")
    try:
        assert _read_ppid(pid) == 1, "test setup broken: spawned process was not orphaned"
        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, pid)
        assert line is None or "orphaned pytest" not in line, (
            f"a 'grep pytest ...' process was matched as a pytest invocation:\n{result.stdout}"
        )
    finally:
        _kill_orphan_quietly(pid)


def test_orphaned_pytest_argv0_bare_executable_is_detected():
    """D#2006 code-review finding 2, other direction: the fix must still
    catch the bare `pytest` executable invocation shape (argv[0]'s basename
    is exactly "pytest"), not just the "-m pytest" module shape covered by
    the other synthetic tests -- both are real invocation forms named in
    the acceptance criteria.
    """
    pid, marker = _spawn_orphaned_argv0_pytest()
    try:
        assert _read_ppid(pid) == 1, "test setup broken: spawned process was not orphaned"
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()
        assert argv0 == "pytest", f"test setup broken: argv[0] was {argv0!r}, not 'pytest'"

        result = _run_watchdog(env_extra={"PROCESS_WATCHDOG_MAX_AGE_SEC": "0"})
        line = _candidate_line(result.stdout, pid)
        assert line is not None and "orphaned pytest" in line, (
            f"a bare 'pytest' executable invocation was not detected:\n{result.stdout}"
        )
    finally:
        _kill_orphan_quietly(pid)
