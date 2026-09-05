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
