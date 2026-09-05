"""tests/test_repo_plane_fail_loud.py

Behavioural tests for the code-plane call sites retargeted under D#2397.

Two properties, and neither is a grep:

1. **An unresolvable code plane aborts before `gh` runs.** This is the whole
   reason the sites needed touching rather than renaming. ``gh --repo ""`` is
   not an error — gh exits 0 after silently resolving the slug from the
   checkout's git remote — so a call site that passes an empty string does not
   fail, it succeeds against whatever repo the process happens to be standing
   in. A test that only asserted a non-zero exit status would pass for a script
   that ran `gh` against the wrong repo and then failed for some later reason.

   So these tests install a **`gh` shim that logs its argv and exits 0**, and
   assert the log is **empty**. Zero invocations is the property; the exit
   status is secondary.

2. **The change is inert today and effective tomorrow.** Every retargeted site
   is proved in *both* plane states: with ``code_repo`` absent it must resolve
   exactly what it resolved before, and with ``code_repo`` set to a scratch
   value it must resolve that value. One direction alone would not distinguish
   a correct change from a variable that is simply ignored.

Everything runs against a throwaway tree under tmp_path. Nothing here reads or
writes the live .autonomous-team tree, and the `gh` shim means nothing here can
reach GitHub even if a guard regressed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# The sites this PR retargeted to the code plane. Each entry is the script, the
# shell snippet that drives it far enough to reach a `gh` call, and whether the
# guard is expected to make the process exit non-zero.
#
# security-trigger.sh is the odd one out and deliberately so: its contract is
# "0 = triggered, non-zero = not triggered", and its only caller passes that
# straight through. Any non-zero return would therefore be read as "no security
# review needed", so its guard fails CLOSED — exit 0, meaning triggered.


def _tree(tmp_path: Path, config: dict | None) -> Path:
    """A fake repo containing the real scripts/ tree at its real relative path.

    The resolvers compute repo_root from ``BASH_SOURCE``, so copying
    ``scripts/`` under a fresh root is what makes ``.autonomous-team/`` absent
    and the plane genuinely unresolvable.
    """
    fake = tmp_path / "fake-repo"
    fake.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SCRIPTS, fake / "scripts", dirs_exist_ok=True)
    if config is not None:
        (fake / ".autonomous-team").mkdir(exist_ok=True)
        (fake / ".autonomous-team" / "config.json").write_text(json.dumps(config))
    return fake


def _gh_shim(tmp_path: Path) -> tuple[Path, Path]:
    """A `gh` on PATH that records argv and exits 0. Returns (bindir, logfile).

    Exiting 0 is the important part: a shim that failed would let a broken
    guard pass this test for the wrong reason.
    """
    bindir = tmp_path / "shimbin"
    bindir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "gh-invocations.log"
    shim = bindir / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        "exit 0\n"
    )
    shim.chmod(0o755)
    return bindir, log


def _run(script: str, fake: Path, bindir: Path, extra_env: dict | None = None):
    env = os.environ.copy()
    env.pop("AUTONOMOUS_TEAM_REPO", None)
    env.pop("GH_REPO", None)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(fake / "home")
    (fake / "home").mkdir(exist_ok=True)
    if extra_env:
        env.update(extra_env)
    runner = fake / "runner.sh"
    runner.write_text(script)
    return subprocess.run(
        ["bash", str(runner)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(fake),
    )


# ---------------------------------------------------------------------------
# Property 1 — an unresolvable plane produces ZERO gh invocations
# ---------------------------------------------------------------------------

UNRESOLVABLE_SITES = {
    "env-bootstrap": 'source "$(dirname "$0")/scripts/env-bootstrap.sh"\n',
    # Bare `true`, not `--set true`: this script takes a positional value, and
    # an unrecognised flag makes it exit during argument parsing — before it
    # ever reaches a `gh` call. That spelling made the zero-invocations
    # assertion below pass vacuously, and it passed just as happily with the
    # guard deleted. Caught by mutating the guard and watching the test not
    # fail, which is the only way a "nothing happened" assertion can be trusted.
    "set-ci-kill-switch": (
        'bash "$(dirname "$0")/scripts/set-ci-kill-switch.sh" '
        'true --reason "test"\n'
    ),
    "post-agent-hook": (
        'bash "$(dirname "$0")/scripts/post-agent-hook.sh" '
        "--role executor --verdict done --pr 1\n"
    ),
    "sweep-stuck-prs": 'bash "$(dirname "$0")/scripts/sweep-stuck-prs.sh"\n',
    "gh-label": (
        'source "$(dirname "$0")/scripts/lib/gh-label.sh"\n'
        "apply_label 1 code-review-passed\n"
    ),
    "security-trigger": (
        'source "$(dirname "$0")/scripts/lib/security-trigger.sh"\n'
        "detect_security_trigger 1\n"
    ),
    # --- the second half of the ledger ---------------------------------
    # Each of these is driven far enough to reach its `gh` call, and each is
    # in REACHES_GH below, so the zero-invocation assertion has a mirror that
    # would catch a driver that exits before it ever gets there.
    "a11y-ui-files": 'bash "$(dirname "$0")/scripts/a11y-ui-files.sh" 1\n',
    "check-pr-cli-touched": (
        'bash "$(dirname "$0")/scripts/check-pr-cli-touched.sh" 1\n'
    ),
    "check-pr-dashboard-touched": (
        'bash "$(dirname "$0")/scripts/check-pr-dashboard-touched.sh" 1\n'
    ),
    "tui-tester-pre-merge": (
        'bash "$(dirname "$0")/scripts/hooks/pre-merge.d/tui-tester-sweep.sh" '
        "--pr 1\n"
    ),
    "tui-tester-post-merge": (
        'bash "$(dirname "$0")/scripts/hooks/post-merge.d/tui-tester-sweep.sh" '
        "--pr 1\n"
    ),
    # No --pr-list, so it falls through to the `gh pr list --label
    # debater-replay` lookup. --dry-run keeps it from spawning anything.
    "replay-debater": (
        'bash "$(dirname "$0")/scripts/replay-debater.sh" --dry-run\n'
    ),
    "run-pr-tests": 'bash "$(dirname "$0")/scripts/run-pr-tests.sh" 1\n',
    "start-the-day": (
        'bash "$(dirname "$0")/scripts/start-the-day.sh" --no-sweeps\n'
    ),
    # Opening a PR against the wrong repo is the worst single outcome in the
    # audit, and it was invisible to every sweep that ran before this one: the
    # call is spelled `subprocess.run(['gh', 'api', ...])` inside a
    # double-quoted `python3 -c`, so no scan looking for a literal `gh ` token
    # could see it. The queue file has to exist or the script exits before it
    # would reach either the guard or `gh`.
    "drain-pending-prs": (
        'mkdir -p "$(dirname "$0")/.autonomous-team"\n'
        "printf '%s' '[{\"branch\":\"b\",\"title\":\"t\",\"body\":\"x\"}]' "
        '> "$(dirname "$0")/.autonomous-team/pending-prs.json"\n'
        'bash "$(dirname "$0")/scripts/drain-pending-prs.sh"\n'
    ),
}

# auto-plan.sh is held out of the set above, and the reason is worth stating
# rather than hiding behind an omission.
#
# It resolves BOTH planes at the top and degrades per data source rather than
# aborting, by design — every one of its section functions is documented to
# print "(data source unavailable: ...)" instead of killing the day's plan. So
# with nothing configured it still issues its Discussion GraphQL queries (with
# an empty owner, which is a pre-existing condition this workstream did not
# introduce and does not touch). A blanket "zero gh invocations" assertion
# would therefore be false for it while the property that actually matters --
# no CODE-plane call escapes with an unresolved code plane -- holds exactly.
#
# Asserting the blanket version and then relaxing it to make it pass would be
# the per-file-instead-of-per-call-site error this whole audit exists to
# correct, one level up.
AUTO_PLAN_DRIVER = 'bash "$(dirname "$0")/scripts/auto-plan.sh"\n'


def test_auto_plan_makes_no_code_plane_call_with_an_unresolved_plane(tmp_path):
    """Its Discussion queries still run; not one PR call does."""
    fake = _tree(tmp_path, config=None)
    bindir, log = _gh_shim(tmp_path)

    proc = _run(AUTO_PLAN_DRIVER, fake, bindir)

    invocations = log.read_text().splitlines() if log.exists() else []
    code_plane = [line for line in invocations if _is_code_plane_invocation(line)]
    assert code_plane == [], (
        "auto-plan.sh made a code-plane call with an unresolved code plane; "
        "`gh pr list --repo \"\"` would answer from whatever repo the checkout "
        f"points at.\ncode-plane invocations: {code_plane}\n"
        f"stderr: {proc.stderr[-1200:]}"
    )
    assert "code repo" in proc.stderr, (
        f"auto-plan.sh skipped its PR sections silently: {proc.stderr[-1200:]}"
    )


def test_auto_plan_reaches_gh_with_the_code_plane_when_it_resolves(tmp_path):
    """The mirror. Without it the assertion above proves only that a script
    which never calls gh never calls gh."""
    fake = _tree(tmp_path, {"repo": PRIVATE, "code_repo": SCRATCH})
    bindir, log = _gh_shim(tmp_path)

    _run(AUTO_PLAN_DRIVER, fake, bindir)

    invocations = log.read_text().splitlines() if log.exists() else []
    code_plane = [line for line in invocations if _is_code_plane_invocation(line)]
    assert code_plane, (
        "auto-plan.sh made no code-plane call even with a resolvable plane — "
        f"the assertion above is vacuous. invocations: {invocations}"
    )
    assert all(SCRATCH in line for line in code_plane), (
        f"auto-plan.sh's PR calls did not carry the code plane: {code_plane}"
    )
    # And the half that must NOT move: its discussions GraphQL still names the
    # Discussion-plane owner. Proving the split needs both halves; proving only
    # that something moved cannot tell a fix from an over-broad substitution.
    #
    # Matched against the whole log rather than line by line: the shim records
    # `$*`, and a GraphQL query argument spans several physical lines, so the
    # `discussions(` selector and the `owner:` binding land on different lines
    # of the log for what is one single invocation.
    log_text = log.read_text()
    owner, name = PRIVATE.split("/")
    assert "discussions(" in log_text, (
        "auto-plan.sh stopped querying discussions entirely"
    )
    assert f'owner:"{owner}", name:"{name}"' in log_text, (
        "auto-plan.sh's discussions query followed the code plane; the two "
        f"planes must diverge, not travel together. log:\n{log_text[:2000]}"
    )


# Which of these assertions is mutation-sensitive, measured rather than assumed.
#
# Deleting each guard and re-running its own case caught 7 of the 8 sites added
# in the second half. The survivor is run-pr-tests.sh, and the reason is worth
# writing down instead of quietly dropping the case: it runs under `set -e`, so
# the plain `REPO="$(_resolve_repo)"` it used to have already aborted the script
# before any `gh` call. Its zero-invocation property was true before this change
# and is true after it.
#
# scripts/team-lead-iteration.sh is `set -e` for the same reason and is
# deliberately NOT in this dict at all — adding it would contribute a case that
# passes identically with and without the work, which reads as coverage and is
# not. What that file's change actually did is a per-call-site retarget of its
# PR reads, PR comments, gate-label writes and merge, and that is asserted
# directly in tests/test_repo_plane_ledger.py against the detector's own
# binding analysis, where it can be checked call site by call site.
#
# run-pr-tests.sh is kept here anyway because the property is worth locking in
# — but it is a regression fence, not evidence that its guard is load-bearing.
@pytest.mark.parametrize("name", sorted(UNRESOLVABLE_SITES))
def test_unresolvable_code_plane_makes_zero_gh_calls(tmp_path, name):
    """No .autonomous-team/config.json, no env var — nothing may reach `gh`."""
    fake = _tree(tmp_path, config=None)
    bindir, log = _gh_shim(tmp_path)

    proc = _run(UNRESOLVABLE_SITES[name], fake, bindir)

    invocations = log.read_text().splitlines() if log.exists() else []
    assert invocations == [], (
        f"{name} invoked gh {len(invocations)} time(s) with an unresolved code "
        f"plane; `gh --repo \"\"` silently falls back to the checkout's remote, "
        f"so these calls would hit an arbitrary repo.\n"
        f"invocations: {invocations}\nstderr: {proc.stderr[-1500:]}"
    )


@pytest.mark.parametrize("name", sorted(UNRESOLVABLE_SITES))
def test_unresolvable_code_plane_writes_reason_to_its_own_stderr(tmp_path, name):
    """Each site names the knob that fixes it on ITS OWN stderr.

    Named for what it actually asserts. It is a statement about each script's
    contract, NOT about what an operator sees: security-trigger.sh's only
    caller is loop-phased-step5.sh:181, which invokes it as
    `detect_security_trigger "$pr" 2>/dev/null` — so this message is
    discarded before it reaches a log. The redirect is pre-existing and out of
    scope here; the fail-closed *behaviour* (tested separately below) is what
    protects the gate, and that survives the redirect because it travels by
    exit status rather than by text.

    Left as a contract test rather than deleted: the message is right, and
    whoever removes that redirect should not also have to rediscover that the
    scripts already explain themselves.
    """
    fake = _tree(tmp_path, config=None)
    bindir, _ = _gh_shim(tmp_path)

    proc = _run(UNRESOLVABLE_SITES[name], fake, bindir)

    assert "code_repo" in proc.stderr or "code repo" in proc.stderr, (
        f"{name} gave no actionable stderr for an unresolved plane: "
        f"{proc.stderr[-1500:]}"
    )


# --- Positive control -------------------------------------------------------
# "Zero gh invocations" is only evidence if the same driver DOES invoke gh when
# the plane resolves. Without this, a typo in the invocation (or a script that
# exits early for an unrelated reason) makes every assertion above pass while
# testing nothing — which is exactly what happened to the set-ci-kill-switch
# case on the first draft of this file.
#
# env-bootstrap.sh is excluded because it makes no gh call at all; it sets the
# default other calls inherit, and is checked separately below.
#
# start-the-day.sh is excluded from the *slug* half for a different reason. It
# does reach `gh` when the plane resolves — which is the mirror this control
# exists to provide, and it is asserted separately below — but the first call it
# makes is the Discussion-plane repo-access precondition
# (assert_gh_can_see_repo), which aborts against a shimmed `gh` before the run
# ever gets to `gh pr list`. Requiring the code-plane slug in its argv would
# assert something the script legitimately does not do at that point.
_NO_GH_CALL = {"env-bootstrap"}
_NO_CODE_PLANE_CALL_IN_A_SCRIPTS_ONLY_TREE = {"start-the-day"}
REACHES_GH = {
    k: v for k, v in UNRESOLVABLE_SITES.items()
    if k not in _NO_GH_CALL | _NO_CODE_PLANE_CALL_IN_A_SCRIPTS_ONLY_TREE
}

# Surfaces that must be on the code plane, matched against the shim's logged
# argv. `gh issue ...` is deliberately absent: it addresses both surfaces (the
# team log is an Issue on the Discussion plane, while a PR comment posted via
# `gh issue comment` is code plane), so it is classified by a human in
# scripts/audit_repo_plane.py rather than asserted here.
_CODE_PLANE_ARGV = ("pr ", "/pulls", "variable ", "/actions", "run ", "workflow ")


def _is_code_plane_invocation(argv_line: str) -> bool:
    return any(tok in argv_line for tok in _CODE_PLANE_ARGV)


@pytest.mark.parametrize("name", sorted(REACHES_GH))
def test_resolvable_code_plane_reaches_gh_with_that_slug(tmp_path, name):
    """The mirror of the zero-invocation test, and what makes it meaningful.

    Also the end-to-end half of the both-plane-states proof: it is not enough
    that the resolver returns the scratch slug, the slug has to arrive in the
    argv `gh` is actually called with.
    """
    fake = _tree(tmp_path, {"repo": PRIVATE, "code_repo": SCRATCH})
    bindir, log = _gh_shim(tmp_path)

    proc = _run(REACHES_GH[name], fake, bindir)

    invocations = log.read_text().splitlines() if log.exists() else []
    assert invocations, (
        f"{name} made no gh call even with a resolvable plane — the "
        f"zero-invocation assertion for this site is therefore vacuous.\n"
        f"stdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-800:]}"
    )
    assert any(SCRATCH in line for line in invocations), (
        f"{name} called gh but not against the configured code plane "
        f"{SCRATCH!r}; it would hit the wrong repo after the cutover.\n"
        f"invocations: {invocations}"
    )
    # Only CODE-plane invocations are required to carry the code-plane slug.
    #
    # Several of these scripts legitimately touch both planes in one process:
    # post-agent-hook.sh REST-checks a PR on the code plane and then appends to
    # the team log — an Issue — through rotate-team-log.sh on the Discussion
    # plane. A blanket "no invocation may mention the Discussion slug" would
    # have failed on the one call that is behaving correctly, which is the
    # same per-file-instead-of-per-call-site error this whole audit exists to
    # correct. So classify each logged invocation and check only the code ones.
    misrouted = [
        line for line in invocations
        if _is_code_plane_invocation(line) and PRIVATE in line
    ]
    assert not misrouted, (
        f"{name} passed the Discussion-plane slug {PRIVATE!r} to a code-plane "
        f"call.\nmisrouted: {misrouted}\nall invocations: {invocations}"
    )


def test_start_the_day_reaches_gh_once_the_plane_resolves(tmp_path):
    """The mirror for start-the-day's zero-invocation assertion.

    It aborts at ``CODE_REPO="$(_require_code_repo ...)" || exit 1``, which is
    line 29 — before gh-token.sh, before the repo-access precondition, before
    anything. That makes "zero gh invocations" a real measurement rather than a
    coincidence only if the same driver *does* reach `gh` when the plane
    resolves. It does: the precondition check is the first call out.
    """
    fake = _tree(tmp_path, {"repo": PRIVATE, "code_repo": SCRATCH})
    bindir, log = _gh_shim(tmp_path)

    _run(UNRESOLVABLE_SITES["start-the-day"], fake, bindir)

    invocations = log.read_text().splitlines() if log.exists() else []
    assert invocations, (
        "start-the-day made no gh call even with a resolvable plane — its "
        "zero-invocation assertion proves nothing."
    )


def test_env_bootstrap_exports_the_code_plane(tmp_path):
    """GH_REPO is gh's own default repo, inherited by every unpinned call.

    This is not one call site: scripts/env-bootstrap.sh is sourced by
    start-tui.sh and the loop runner, so whatever plane it exports becomes the
    default for every `gh` in the process that does not pin its own --repo.
    """
    fake = _tree(tmp_path, {"repo": PRIVATE, "code_repo": SCRATCH})
    bindir, _ = _gh_shim(tmp_path)

    proc = _run(
        'source "$(dirname "$0")/scripts/env-bootstrap.sh"\n'
        'echo "GH_REPO=$GH_REPO"\n',
        fake,
        bindir,
    )

    assert f"GH_REPO={SCRATCH}" in proc.stdout, (
        f"env-bootstrap exported the wrong plane as gh's process-wide default: "
        f"{proc.stdout!r} / {proc.stderr[-500:]!r}"
    )


def test_security_trigger_fails_closed_not_open(tmp_path):
    """The security gate must report *triggered* when it cannot check.

    detect_security_trigger's contract is 0 = triggered, non-zero = not
    triggered, and loop-phased-step5.sh's _check_security_trigger returns it
    verbatim. So every non-zero value — including a bespoke error code — reads
    to the only caller as "no security review needed". An unresolvable plane
    must therefore return 0, demanding a review it cannot prove unnecessary.
    """
    fake = _tree(tmp_path, config=None)
    bindir, log = _gh_shim(tmp_path)

    proc = _run(
        'source "$(dirname "$0")/scripts/lib/security-trigger.sh"\n'
        "detect_security_trigger 1\n"
        'echo "rc=$?"\n',
        fake,
        bindir,
    )

    assert "rc=0" in proc.stdout, (
        "security-trigger failed OPEN: a non-zero return is read by its only "
        f"caller as 'no security review needed'. stdout={proc.stdout!r} "
        f"stderr={proc.stderr[-1000:]!r}"
    )
    assert (log.read_text().splitlines() if log.exists() else []) == []


# ---------------------------------------------------------------------------
# Property 2 — both plane states, for every retargeted resolver
# ---------------------------------------------------------------------------

PRIVATE = "owner/private-twin"
SCRATCH = "scratch-org/scratch-code-repo"


def _resolve(tmp_path: Path, func: str, config: dict) -> tuple[str, int]:
    fake = _tree(tmp_path, config)
    bindir, _ = _gh_shim(tmp_path)
    proc = _run(
        'source "$(dirname "$0")/scripts/lib/repo-resolve.sh"\n' f"{func}\n",
        fake,
        bindir,
    )
    return proc.stdout.strip(), proc.returncode


def test_code_plane_unset_is_a_no_op(tmp_path):
    """code_repo absent: the code plane is exactly what it was before."""
    out, rc = _resolve(tmp_path, "_require_code_repo test", {"repo": PRIVATE})
    assert rc == 0
    assert out == PRIVATE


def test_code_plane_set_retargets(tmp_path):
    """code_repo present: the code plane follows it, the Discussion plane does not."""
    config = {"repo": PRIVATE, "code_repo": SCRATCH}
    code, code_rc = _resolve(tmp_path, "_require_code_repo test", config)
    disc, disc_rc = _resolve(tmp_path, "_resolve_discussion_repo", config)

    assert code_rc == 0 and code == SCRATCH, (
        "setting code_repo did not move the code plane — the retarget is a no-op "
        "in both directions, which is worse than not doing it"
    )
    assert disc_rc == 0 and disc == PRIVATE, (
        "setting code_repo moved the Discussion plane too; the two planes must "
        "diverge, not travel together"
    )


def test_require_code_repo_prints_nothing_on_failure(tmp_path):
    """A guard that printed a partial value would defeat `x=$(...) || exit`.

    The caller substitutes stdout into a variable. If the failure path emitted
    anything on stdout, that string would become the repo slug.
    """
    out, rc = _resolve(tmp_path, "_require_code_repo test", {"language": "en"})
    assert rc != 0
    assert out == ""


def test_gh_label_repo_is_namespaced(tmp_path):
    """Sourcing gh-label.sh must not clobber a caller's own REPO.

    scripts/sweep-stuck-prs.sh set REPO, then sourced this library three lines
    later and had it silently replaced. Both values happen to be the code plane
    today, so the bug is invisible until the planes diverge.
    """
    fake = _tree(tmp_path, {"repo": PRIVATE, "code_repo": SCRATCH})
    bindir, _ = _gh_shim(tmp_path)

    proc = _run(
        'REPO="caller/owns-this"\n'
        'source "$(dirname "$0")/scripts/lib/gh-label.sh"\n'
        'echo "REPO=$REPO"\n'
        'echo "LABEL=$_GH_LABEL_REPO"\n',
        fake,
        bindir,
    )

    assert "REPO=caller/owns-this" in proc.stdout, (
        f"gh-label.sh overwrote the caller's REPO: {proc.stdout!r}"
    )
    assert f"LABEL={SCRATCH}" in proc.stdout, (
        f"gh-label.sh did not resolve the code plane: {proc.stdout!r}"
    )
