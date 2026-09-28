"""backend/pr_execution_policy.py — decide whether a reviewer or acceptance-
tester spawned for a PR may run PR-head code (pytest, scripts/ci/run-guards.sh,
suite scripts) on the operator host, or must stick to static review plus CI.

D#2644: the code plane is public and takes outside contributions. Every
reviewer and acceptance-tester today runs PR-head code with the operator's
full environment and ambient `gh` login. This module is the one resolver both
the rendered spawn prompt (via ``HOST_EXECUTION: host|static-only``, see
``apply_host_execution`` below) and the role cards key off, so a reviewer and
its own prompt never disagree about which case they are in.

Fails closed by construction: ``resolve()`` returns ``"host"`` only when BOTH
of the two existing intake CLIs confirm — by their own already-fail-closed
exit-code contracts — that the PR is internal. Any other outcome, including a
non-1 exit from either CLI, a timeout, a missing script, or any other
exception, is ``"static-only"``. No environment variable, label, or PR text
can change that: this module reads only subprocess exit codes.

Both CLIs already exist and are already what the merge gates call, so this
adds no new provenance logic:

  - ``scripts/lib/pr_intake_gate.py security-required-pr <pr> --repo <slug>``
    exits 1 only when the PR's author is confirmed in the trust set (an
    internal PR). 0 means external/security-required, 3 means unreadable.
  - ``scripts/lib/external_intake_gate.py security-required <discussion>``
    exits 1 only when the Discussion's ``provenance:external`` label is
    confirmed absent. 0/3/4 all mean required or unknown.

Neither of those two files is touched by this change (they differ across the
two planes); this module calls their CLIs by subprocess so it stays
byte-identical on both.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

# The two modes this module ever returns. Anything that isn't exactly HOST is
# treated as STATIC_ONLY — see normalize_mode().
HOST = "host"
STATIC_ONLY = "static-only"

# Bounded so a hung `gh` call behind either CLI can't hang a reviewer spawn.
_TIMEOUT_SECONDS = 60

# Literal markers a template wraps around a step that runs PR-head code.
# apply_host_execution() leaves the wrapped content untouched under "host"
# (only the marker lines themselves are added to the render) and replaces the
# whole wrapped span, markers included, under "static-only".
HOST_EXEC_BEGIN = "<!-- HOST_EXEC_BEGIN -->"
HOST_EXEC_END = "<!-- HOST_EXEC_END -->"

# Literal sentinel a template places wherever the rendered HOST_EXECUTION
# line belongs. Not a `{{var}}` token — it is replaced by this module, after
# spawn_templates.render_body() has already finished its own substitution,
# so no change is needed to spawn_templates.py's variable contract.
_HOST_EXECUTION_LINE_SENTINEL = "HOST_EXECUTION: __PR_HOST_EXECUTION_MODE__"


def _repo_root() -> Path:
    """The operator checkout this module itself lives in — never the PR head."""
    return Path(__file__).resolve().parent.parent


def _run_cli(cmd: list[str], *, run) -> tuple[Optional[int], str]:
    """Run *cmd*, returning (exit_code, "") on any completed run, or
    (None, detail) for a timeout or any other exception. Never raises.
    """
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {_TIMEOUT_SECONDS}s: {' '.join(cmd)}"
    except Exception as exc:  # noqa: BLE001 — fail closed on anything, not just the two named cases
        return None, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode, ""


def resolve(
    pr: int,
    pr_repo: str,
    discussion: Optional[int] = None,
    *,
    run=subprocess.run,
) -> tuple[str, str]:
    """Decide ``"host"`` vs ``"static-only"`` for a reviewer spawned against *pr*.

    Returns ``(mode, reason)``. *reason* is a short human-readable string
    naming which check produced the result — always non-empty when the mode
    is ``static-only``.

    *run* is the subprocess runner, injectable for tests (defaults to
    ``subprocess.run``) so the table in
    ``backend/tests/test_pr_execution_policy.py`` can exercise every branch
    without a real ``gh`` login.
    """
    root = _repo_root()
    pr_intake_gate = root / "scripts" / "lib" / "pr_intake_gate.py"
    pr_code, pr_detail = _run_cli(
        [sys.executable, str(pr_intake_gate), "security-required-pr", str(pr), "--repo", pr_repo],
        run=run,
    )
    if pr_code is None:
        return STATIC_ONLY, f"pr_intake_gate.py did not complete ({pr_detail})"
    if pr_code != 1:
        return STATIC_ONLY, f"pr_intake_gate.py security-required-pr exited {pr_code} (not confirmed internal)"

    if discussion is None:
        return HOST, "pr_intake_gate.py confirmed an internal PR author; no Discussion to cross-check"

    external_intake_gate = root / "scripts" / "lib" / "external_intake_gate.py"
    disc_code, disc_detail = _run_cli(
        [sys.executable, str(external_intake_gate), "security-required", str(discussion)],
        run=run,
    )
    if disc_code is None:
        return STATIC_ONLY, f"external_intake_gate.py did not complete ({disc_detail})"
    if disc_code != 1:
        return STATIC_ONLY, f"external_intake_gate.py security-required exited {disc_code} (not confirmed internal)"

    return HOST, "pr_intake_gate.py and external_intake_gate.py both confirmed internal"


def normalize_mode(mode: str) -> str:
    """Fail-closed normalization: only the exact literal ``"host"`` is host.

    A missing, empty, or unrecognized mode (a stale caller, a typo, a value
    that leaked in from somewhere other than ``resolve()``) renders as
    ``static-only`` rather than raising or defaulting to the more permissive
    option.
    """
    return HOST if mode == HOST else STATIC_ONLY


def host_execution_line(mode: str) -> str:
    """Render the ``HOST_EXECUTION: <mode>`` line for a spawn prompt."""
    return f"HOST_EXECUTION: {normalize_mode(mode)}"


def _static_only_section(pr: int, pr_repo: str) -> str:
    return (
        "Static review only under HOST_EXECUTION: static-only. Do not run any code from "
        "the PR head on this host: no test runner, no suite script, no linter invocation "
        "against the PR's own files. Review the diff statically and read CI instead:\n"
        f"  gh pr checks {pr} --repo {pr_repo}\n"
        'Report `tests_run: []` with `skip_reason: "host_execution_static_only"`.'
    )


def apply_host_execution(body: str, mode: str, *, pr: Optional[int] = None, pr_repo: str = "") -> str:
    """Post-process a rendered template *body* for the resolved *mode*.

    Two independent, literal substitutions — neither is a ``{{var}}`` token,
    so this runs after ``spawn_templates.render_body()`` has already done its
    own substitution and needs no change to that module's variable contract:

    1. The ``HOST_EXECUTION: __PR_HOST_EXECUTION_MODE__`` sentinel line, if
       present, becomes ``HOST_EXECUTION: <normalized mode>``.
    2. Every ``<!-- HOST_EXEC_BEGIN -->...<!-- HOST_EXEC_END -->`` wrapped
       span. Under ``"host"`` the span is left exactly as written (including
       its marker lines). Under anything else — the fail-closed default —
       the whole span, markers included, is replaced with a short
       static-review instruction naming *pr* / *pr_repo*.

    A *body* with neither the sentinel nor any marker pair (most roles) is
    returned unchanged.
    """
    resolved = normalize_mode(mode)
    body = body.replace(_HOST_EXECUTION_LINE_SENTINEL, host_execution_line(resolved))

    if resolved == HOST:
        return body

    out: list[str] = []
    i = 0
    while True:
        start = body.find(HOST_EXEC_BEGIN, i)
        if start == -1:
            out.append(body[i:])
            break
        out.append(body[i:start])
        end = body.find(HOST_EXEC_END, start)
        if end == -1:
            # Unterminated marker — leave the rest untouched rather than
            # silently dropping content a template author didn't intend to lose.
            out.append(body[start:])
            break
        end += len(HOST_EXEC_END)
        out.append(_static_only_section(pr or 0, pr_repo))
        i = end
    return "".join(out)
