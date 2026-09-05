"""tests/test_repo_plane_ledger.py — the ledger says what is actually left.

``scripts/repo-plane-known-defects.txt`` is what stands between this repo and
the public-repo cutover: ``tests/test_repo_plane_cutover_gate.py`` and
``scripts/ci/repo-plane-guard.py`` both refuse a ``code_repo`` setting while it
lists defects. A list with that much authority is only worth what its corpus is
worth, so this file asserts three things and the third is the one that matters:

1. **The ledger matches the tree.** Every defect the detector finds is listed,
   with the right count, and nothing listed has been quietly fixed.
2. **Per call site, not per file.** Five files legitimately spend BOTH planes.
   "Fixed" is not the same as "every ``$REPO`` replaced" — an over-broad
   substitution would move the team-log Issue, the discussions GraphQL and the
   tui-bug label onto the code plane and pass a count-the-defects check while
   breaking three things. The Discussion-plane half of each is asserted as
   explicitly as the code-plane half.
3. **The corpus covers every carrier `gh` is actually called through.** This is
   the assertion the previous version of this file did not have, and its
   absence is why "36 defects" was wrong. The detector could not see ``gh``
   invoked from a program embedded in a shell script, and could not see
   TypeScript at all — two whole surfaces reporting zero call sites, which is
   indistinguishable from zero defects to anything reading the total.

Every assertion reads the detector's own per-call-site binding analysis
(``scripts/audit_repo_plane.py``), not a grep, and the detector refuses to
report anything until its own known-positive fixtures still reproduce.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECTOR = REPO_ROOT / "scripts" / "audit_repo_plane.py"
LEDGER = REPO_ROOT / "scripts" / "repo-plane-known-defects.txt"
CI_GUARD = REPO_ROOT / "scripts" / "ci" / "repo-plane-cutover-guard.py"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load_detector():
    spec = importlib.util.spec_from_file_location("audit_repo_plane_ledger", DETECTOR)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["audit_repo_plane_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load_detector()

PRIVATE = "owner/private-twin"
SCRATCH = "scratch-org/scratch-code-repo"


# ---------------------------------------------------------------------------
# 1. The ledger matches the tree
# ---------------------------------------------------------------------------

def test_detector_self_test_passes():
    """Nothing below means anything if the detector is broken."""
    assert audit.run_self_test(verbose=False) == 0


def _defects():
    return [f for f in audit.scan_tree(REPO_ROOT) if f.is_defect]


# The ledger-agrees-with-the-tree relation is asserted once, in
# tests/test_audit_repo_plane.py::test_the_ledger_and_the_tree_agree_per_file.
# It lived here too until #2409's frozen-count version of the same check was
# rewritten into the same relational form, at which point keeping both would
# have been two copies of one fact — which is how a fact goes stale in exactly
# the copy you did not run. It belongs in the detector's own test file, where
# it is measuring the detector.


def test_ledger_still_carries_its_marker():
    """An entry-free file without the marker is a truncated file, not a clean
    tree, and the two must never read the same."""
    assert audit.LEDGER_MARKER in LEDGER.read_text()


def test_the_tree_has_no_repo_plane_defects_in_any_language():
    """What this change finished, stated as a property of the tree rather than
    as a count in a PR body, so it cannot quietly regress.

    All three languages, deliberately named: every previous total in this
    workstream was correct for the corpus it was computed over and wrong for
    the tree, and each correction came from a language or a carrier nobody had
    asked about.
    """
    remaining = _defects()
    assert remaining == [], "\n".join(
        f"{f.path}:{f.line} [{f.surface}/{f.rule}] binding={f.binding} "
        f"plane={f.plane}\n    {f.snippet}"
        for f in remaining
    )


def test_all_three_languages_are_actually_in_the_corpus():
    """A zero computed over two languages is not the same zero.

    ts-backend was absent from iter_sources() for the whole first half of this
    workstream and reported no call sites, which is indistinguishable from
    reporting no defects.
    """
    langs = {lang for _, lang in audit.iter_sources(REPO_ROOT)}
    assert {"bash", "python", "typescript"} <= langs, sorted(langs)
    scanned = {f.language for f in audit.scan_tree(REPO_ROOT)}
    assert {"bash", "python", "typescript"} <= scanned, sorted(scanned)


# ---------------------------------------------------------------------------
# The cutover gate: blocked now, and provably not blocked forever
# ---------------------------------------------------------------------------

def _tree_with(tmp_path: Path, ledger_text: str, name: str) -> Path:
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "repo-plane-known-defects.txt").write_text(ledger_text)
    (root / ".autonomous-team").mkdir()
    (root / ".autonomous-team" / "config.json").write_text(
        json.dumps({"repo": PRIVATE, "code_repo": SCRATCH})
    )
    return root


def test_the_real_ledger_now_allows_a_cutover(tmp_path):
    """The acceptance test for this entire workstream.

    Copies the *real* ledger into a scratch tree and sets code_repo there. A
    scratch tree rather than the live one because a test must never write
    .autonomous-team/config.json, and because the live tree has not cut over —
    asserted separately below.
    """
    assert audit.cutover_violations(
        _tree_with(tmp_path, LEDGER.read_text(), "now")
    ) == [], "the real ledger still blocks the cutover"


def test_a_non_empty_ledger_still_blocks_it(tmp_path):
    """The other direction, without which the test above proves only that a
    gate which never fires never fires.

    Uses the real ledger's own header — its format, its marker — with one entry
    added back, so what is exercised is this file rather than a hand-written
    stand-in that might parse differently.
    """
    header = "\n".join(
        line for line in LEDGER.read_text().splitlines()
        if line.startswith("#") or not line.strip()
    )
    stale = header + "\nts-backend/src/spawn/post-merge-hook.ts 12\n"
    assert audit.LEDGER_MARKER in stale
    violations = audit.cutover_violations(_tree_with(tmp_path, stale, "stale"))
    assert violations, "the gate stopped firing on a ledger with entries"
    assert "12" in violations[0], violations


def test_no_cutover_is_configured_in_this_tree():
    """Documents today's state and fails loudly the day it changes."""
    assert audit.configured_code_repo(REPO_ROOT) == []


# ---------------------------------------------------------------------------
# --check and --strict mean what they say
# ---------------------------------------------------------------------------
#
# --check was documented as "exit 1 if any defect remains" and exited 0 with 36
# live defects in the tree, because it only ever failed on a regression against
# the ledger. Nobody reads a return statement; they read the help text. These
# two assertions pin the shipped behaviour of both flags to their documented
# behaviour, in the state the tree is actually in.

def _run_detector(*flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DETECTOR), *flags],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )


def test_check_passes_because_there_is_no_regression():
    proc = _run_detector("--check")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]


def test_strict_passes_too_now_that_nothing_remains():
    proc = _run_detector("--strict")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]


def test_the_two_flags_are_still_different_questions(tmp_path):
    """They agree today only because the ledger is empty, and a reader should
    not conclude from that that one of them is redundant.

    Exercised where the difference is still observable: a tree with a recorded
    defect. --check asks "is this worse than the ledger" and passes; --strict
    asks "is anything left" and fails. Driven through the module rather than
    the CLI because the CLI reads the real ledger, and a test must not write
    that file.
    """
    ledger = tmp_path / "ledger.txt"
    ledger.write_text(
        f"# {audit.LEDGER_MARKER}\nts-backend/src/spawn/post-merge-hook.ts 12\n"
    )
    baseline = audit.load_baseline(ledger)
    assert baseline == {"ts-backend/src/spawn/post-merge-hook.ts": 12}

    class _Defect:
        path = "ts-backend/src/spawn/post-merge-hook.ts"

    recorded = [_Defect()] * 12

    # --check's question: no file above its entry, no entry above its file.
    assert audit.check_against_baseline(recorded, baseline) == []
    # --strict's question: are there defects at all.
    assert len(recorded) > 0


# ---------------------------------------------------------------------------
# The gate runs in CI
# ---------------------------------------------------------------------------

def test_the_guard_is_a_step_in_a_workflow():
    """A gate no job runs is a sentence in a file.

    The cutover gate lived only in a pytest file, and no CI job runs pytest;
    scripts/run-pr-tests.sh routes by changed path and a PR touching only
    .autonomous-team/config.json matches no route at all. The cutover IS that
    PR, so the gate would not have run on the one change it guards.
    """
    assert CI_GUARD.exists()
    wired = [
        wf for wf in WORKFLOWS.glob("*.yml")
        if CI_GUARD.name in wf.read_text()
    ]
    assert wired, (
        f"{CI_GUARD.name} is referenced by no workflow — the repo-plane audit "
        f"runs nowhere. Workflows checked: "
        f"{[w.name for w in WORKFLOWS.glob('*.yml')]}"
    )


def test_the_guard_passes_on_this_tree():
    proc = subprocess.run(
        [sys.executable, str(CI_GUARD)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]


# ---------------------------------------------------------------------------
# 3. Corpus coverage — every carrier `gh` is reached through
# ---------------------------------------------------------------------------
#
# Six shell scripts call `gh` from an embedded program and reported ZERO call
# sites; ts-backend was not in the corpus at all. A detector cannot report a
# defect in a surface it does not read, and a total computed over a partial
# corpus is indistinguishable from a total computed over all of it.
#
# The self-test proves the shapes are detectable in a fixture. These prove the
# corpus actually reaches them in this tree — which is the half a fixture
# cannot cover, because a fixture is supplied to the scanner rather than found
# by it.

# Files whose `gh` call is spelled as an argv list inside an embedded program,
# which no scan for a shell `gh ` token can see. Only the interpolated-string
# carrier is asserted: the heredoc carrier is still unread by the detector, and
# that gap is reported in the ledger header rather than papered over with a
# test that would fail.
EMBEDDED_CARRIERS = [
    "scripts/drain-pending-prs.sh",
]




@pytest.mark.parametrize("rel", EMBEDDED_CARRIERS)
def test_embedded_gh_calls_are_in_the_corpus(rel):
    path = REPO_ROOT / rel
    found = [
        f for f in audit.scan_bash(path, path.read_text())
        if "python-in-" in f.rule
    ]
    assert found, (
        f"{rel} calls gh from an embedded program and the scan found none. "
        f"Each of these files reported zero call sites for that program at "
        f"some point in this workstream, which reads as 'nothing to fix here'."
    )


def test_typescript_is_in_the_corpus():
    langs = {lang for _, lang in audit.iter_sources(REPO_ROOT)}
    assert "typescript" in langs, (
        "iter_sources() yields no TypeScript — ts-backend runs the same loop "
        "against the same GitHub, merge path included, and would be unmeasured"
    )
    ts = [f for f in audit.scan_tree(REPO_ROOT) if f.language == "typescript"]
    assert ts, "the TypeScript corpus is empty"


# ---------------------------------------------------------------------------
# 2. Per call site: the files that legitimately spend both planes
# ---------------------------------------------------------------------------

BASH_SPLITS = {
    "scripts/team-lead-iteration.sh": [
        # code plane — PR reads, PR comments, the gate-label writes that
        # loop-phased-step5.sh reads back off the code plane, and the merge.
        # The label writes are the ones that matter most: leaving them on the
        # Discussion plane while the merge gate reads them from the code plane
        # is the deadlock D#2348's PR-j took three review rounds to close.
        ("gh pr list --state open", audit.CODE),
        ("""--json body --jq '.body // ""'""", audit.CODE),
        ("issues/${pr_num}/labels/code-review-passed", audit.CODE),
        ('issues/${pr_num}/labels" -f labels[]', audit.CODE),
        ('gh pr comment "$pr_num" --body "$_qg_comment_body"', audit.CODE),
        ("gh pr comment $pr_num --body 'All reviews passed", audit.CODE),
        ("--json labels --jq '[.labels[].name]'", audit.CODE),
        ("gh pr merge $pr_num --squash", audit.CODE),
        ("--json body --jq '.body'", audit.CODE),
        # Discussion plane — must NOT have moved.
        ("gh issue list --label team-log", audit.UNDIFFERENTIATED),
    ],
    "scripts/auto-plan.sh": [
        ('gh pr list --repo "$CODE_REPO" --state merged', audit.CODE),
        ('gh pr list --repo "$CODE_REPO" --state open', audit.CODE),
        ("discussions(first:50, orderBy:", audit.UNDIFFERENTIATED),
        ("discussions(first:50, states:OPEN)", audit.UNDIFFERENTIATED),
    ],
    "scripts/start-the-day.sh": [
        ('gh pr list --repo "$CODE_REPO" --state open', audit.CODE),
        ('gh pr list --repo "$CODE_REPO" --state merged', audit.CODE),
        ("hasDiscussionsEnabled", audit.UNDIFFERENTIATED),
        ("discussion(number:$num) { closed }", audit.UNDIFFERENTIATED),
        ("discussions(first:100, states:OPEN)", audit.UNDIFFERENTIATED),
        ("discussion(number:$num) { title }", audit.UNDIFFERENTIATED),
    ],
    "scripts/hooks/post-merge.d/tui-tester-sweep.sh": [
        ('gh pr view "$PR"', audit.CODE),
        # The tui-bug label belongs wherever the tui-bug Issues live, which is
        # the Discussion plane. Moving it with the PR read would have created a
        # label on one repo for Issues filed on another.
        ('gh label create "tui-bug"', audit.UNDIFFERENTIATED),
        # The tui-bug Issues themselves are filed from inside a heredoc, which
        # the detector does not read — see the ledger header. Not asserted here
        # rather than asserted falsely.
    ],
}


def _bash_gh_findings(rel: str):
    path = REPO_ROOT / rel
    return [
        f for f in audit.scan_bash(path, path.read_text())
        if f.kind in ("gh_call", "embedded_gh_call")
    ]


@pytest.mark.parametrize("rel", sorted(BASH_SPLITS))
def test_bash_two_plane_files_keep_both_planes(rel):
    findings = _bash_gh_findings(rel)
    for selector, expected in BASH_SPLITS[rel]:
        matches = [f for f in findings if selector in f.snippet]
        assert matches, (
            f"{rel}: selector {selector!r} matched no call site — the selector "
            f"is stale, so this assertion is not checking what it claims to.\n"
            + "\n".join(f"  {f.line}: {f.snippet}" for f in findings)
        )
        for found in matches:
            assert found.plane == expected, (
                f"{rel}:{found.line} binds {found.binding} carrying plane "
                f"{found.plane!r}, expected {expected!r}.\n    {found.snippet}"
            )


PY_SPLITS = {
    "backend/spawn_queue.py": [
        ("gh pr view", audit.CODE),
        # The team-log Issue. An over-broad rename would have moved it.
        ("gh issue list --label team-log", audit.UNDIFFERENTIATED),
    ],
}


@pytest.mark.parametrize("rel", sorted(PY_SPLITS))
def test_python_two_plane_files_keep_both_planes(rel):
    path = REPO_ROOT / rel
    findings = audit.scan_python(path, path.read_text())
    for selector, expected in PY_SPLITS[rel]:
        matches = [f for f in findings if selector in f.snippet]
        assert matches, (
            f"{rel}: selector {selector!r} matched nothing.\n"
            + "\n".join(f"  {f.line}: {f.snippet}" for f in findings)
        )
        for found in matches:
            assert found.plane == expected, (
                f"{rel}:{found.line} binds {found.binding} carrying plane "
                f"{found.plane!r}, expected {expected!r}.\n    {found.snippet}"
            )


# Every backend module retargeted in this change, and the count of code-plane
# `gh` call sites each must now bind to CODE_REPO. Counts, not just "no
# defects": a call site that disappeared would also produce zero defects.
PY_CODE_PLANE_SITES = {
    "backend/changelog.py": 1,
    "backend/corpus_drift/claims/two_gate.py": 2,
    "backend/cost_per_outcome.py": 1,
    "backend/loop_metrics_counters.py": 1,
    "backend/quality_scorer.py": 2,
    "backend/red_main_check.py": 1,
    "backend/release_manager.py": 2,
    "backend/run_analyst.py": 3,
    "backend/spawn_queue.py": 1,
    "backend/team_status.py": 2,
}


@pytest.mark.parametrize("rel", sorted(PY_CODE_PLANE_SITES))
def test_python_code_plane_sites_bind_the_code_plane(rel):
    path = REPO_ROOT / rel
    findings = audit.scan_python(path, path.read_text())
    code = [f for f in findings if f.surface == audit.SURFACE_CODE]
    assert len(code) == PY_CODE_PLANE_SITES[rel], (
        f"{rel}: found {len(code)} code-plane call sites, expected "
        f"{PY_CODE_PLANE_SITES[rel]} — a call site was added or removed, so "
        f"this file's classification needs re-reading.\n"
        + "\n".join(f"  {f.line}: {f.snippet}" for f in findings)
    )
    for f in code:
        assert f.plane == audit.CODE, (
            f"{rel}:{f.line} spends plane {f.plane!r} ({f.binding}) on a "
            f"code-plane call.\n    {f.snippet}"
        )


# ---------------------------------------------------------------------------
# The Python half actually reaches `gh` with the code plane
# ---------------------------------------------------------------------------
#
# The checks above read the source. This one runs it. backend/changelog.py is
# the representative because it is the file that had to keep BOTH names — its
# merged-PR query and /pull/ URLs are code plane, its /discussions/ URL is not
# — so a single rename would have been visibly wrong here first.

def _python_env(tmp_path, config: dict):
    """A scratch state dir carrying project.json, plus a `gh` shim on PATH.

    ``AUTONOMOUS_TEAM_REPO`` is deliberately unset: it collapses Python's two
    planes onto one value by design (see backend/tests/test_repo_planes.py),
    so setting it here would make the two planes agree for the wrong reason.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "project.json").write_text(json.dumps(config))

    bindir = tmp_path / "shimbin"
    bindir.mkdir()
    log = tmp_path / "gh.log"
    shim = bindir / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        "echo '[]'\n"
        "exit 0\n"
    )
    shim.chmod(0o755)

    env = os.environ.copy()
    env.pop("AUTONOMOUS_TEAM_REPO", None)
    env.pop("GH_REPO", None)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = str(state)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return env, log


_CHANGELOG_DRIVER = (
    "import sys\n"
    "sys.path.insert(0, %r)\n"
    "from backend import changelog\n"
    "changelog.load_merged_prs(5)\n"
    "print('PR_URL', changelog.generate_changelog("
    "[{'number': 7, 'title': 't', 'mergedAt': '2026-01-01T00:00:00Z',"
    " 'author': {'login': 'a'}, 'body': ''}]))\n"
) % str(REPO_ROOT)


def _run_changelog(tmp_path, config):
    env, log = _python_env(tmp_path, config)
    proc = subprocess.run(
        [sys.executable, "-c", _CHANGELOG_DRIVER],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout, (log.read_text().splitlines() if log.exists() else [])


def test_python_code_plane_unset_is_a_no_op(tmp_path):
    """code_repo absent: exactly the slug it used before."""
    out, invocations = _run_changelog(tmp_path, {"repo": PRIVATE})
    assert invocations, "changelog made no gh call at all"
    assert any(f"--repo {PRIVATE}" in line for line in invocations), invocations
    assert f"https://github.com/{PRIVATE}/pull/7" in out, out


def test_python_code_plane_set_retargets(tmp_path):
    """code_repo present: the gh argv and the PR URL both follow it."""
    out, invocations = _run_changelog(
        tmp_path, {"repo": PRIVATE, "code_repo": SCRATCH}
    )
    assert any(f"--repo {SCRATCH}" in line for line in invocations), (
        f"changelog queried merged PRs on the wrong plane: {invocations}"
    )
    assert not any(f"--repo {PRIVATE}" in line for line in invocations), (
        f"changelog still queried the Discussion plane: {invocations}"
    )
    assert f"https://github.com/{SCRATCH}/pull/7" in out, (
        f"the PR permalink did not follow the code plane: {out!r}"
    )


def test_python_code_repo_cannot_be_empty():
    """Why the Python half needs no `_require_code_repo` equivalent.

    ``CODE_REPO`` falls back to ``REPO``, whose resolver raises rather than
    returning "". There is no configuration in which a backend module hands
    ``gh`` an empty ``--repo``, so the bash guard has no Python counterpart —
    stated as a test rather than as a claim in a PR body.
    """
    from backend._repo import CODE_REPO, REPO

    assert CODE_REPO, "CODE_REPO resolved empty — the bash guard's premise"
    assert REPO
