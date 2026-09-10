"""Tests for scripts/engine-sync/inbound/apply_inbound.py -- C1-C8.

Every test here drives the real module against a disposable scratch
repository built to look like the engine (a `main` branch, a marker ref, and
a second history line standing in for the code plane). Nothing touches the
real checkout, the real remote, or the real state directory.

Where an item constrains what the code must NOT do, it is checked with a
fake binary earlier on PATH that fails the test when invoked -- not by
searching the source for a string.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_INBOUND_DIR = _THIS_DIR.parent / "inbound"
sys.path.insert(0, str(_INBOUND_DIR))

import apply_inbound  # noqa: E402
import pull  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60, env=full_env)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for relpath, content in files.items():
        full = repo / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def engine(tmp_path) -> dict:
    """A scratch repo with two unrelated history lines, exactly like the
    real pair of planes: `main` (the engine) and `plane` (the code plane).

    engine main holds:
        keep/engine-only.txt   -- never in the export surface; must survive
        backend/shared.py      -- present on both, identical (clean-apply)
        backend/diverged.py    -- present on both, DIFFERENT (would overwrite)
    the plane additionally holds:
        backend/new.py         -- absent on the engine (a create)
        scripts/guard.sh       -- sensitive prefix, must be withheld
        tests/out.txt          -- outside the export surface, must be withheld
    """
    repo = tmp_path / "engine"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "engine@example.com")
    _git(repo, "config", "user.name", "Engine")

    _commit(
        repo,
        "engine seed",
        {
            "keep/engine-only.txt": "engine only, never exported\n",
            "backend/shared.py": "shared v1\n",
            "backend/diverged.py": "ENGINE's own version\n",
        },
    )
    engine_main = _git(repo, "rev-parse", "HEAD").strip()

    # The code plane: an unrelated root, as the real pair are.
    _git(repo, "checkout", "-q", "--orphan", "plane")
    _git(repo, "rm", "-rq", "--cached", ".")
    for p in ("keep/engine-only.txt", "backend/shared.py", "backend/diverged.py"):
        (repo / p).unlink()
    plane_seed = _commit(repo, "plane seed", {"backend/shared.py": "shared v1\n"})

    c1 = _commit(
        repo,
        "widen the shared helper (#3)",
        {"backend/shared.py": "shared v2\n", "backend/new.py": "brand new\n"},
    )
    c2 = _commit(
        repo,
        "tighten the guard (#2)",
        {
            "scripts/guard.sh": "#!/bin/sh\necho guarded\n",
            "tests/out.txt": "out of surface\n",
            "backend/diverged.py": "PLANE's own version\n",
            # One writable path here too, so the branch genuinely has to carry
            # a commit per contributing inbound commit rather than collapsing
            # to one because only the first commit happened to contribute.
            "backend/second.py": "from the second commit\n",
        },
    )
    plane_tip = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "main")
    _git(repo, "update-ref", "refs/synced/code-plane", plane_seed)

    return {
        "repo": repo,
        "main": engine_main,
        "plane_seed": plane_seed,
        "plane_tip": plane_tip,
        "c1": c1,
        "c2": c2,
    }


@pytest.fixture
def state_dir(tmp_path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


SURFACE_PATTERNS = ["backend/*", "scripts/*", "hooks/*", "CLAUDE.md"]
SENSITIVE_PREFIXES = ["scripts/", "hooks/", "CLAUDE.md", ".claude/"]


def _classify(engine: dict, **overrides):
    """The real classify_report, wired to the scratch repo with stubbed
    provenance and surface/sensitivity resolvers. The classification logic
    under test is the real one -- only the network-backed seams are stubbed."""
    import changeset
    import report as report_mod

    def _prs_for_commit(sha: str) -> list[int]:
        """Stand-in for `GET /commits/{sha}/pulls`. It has to AGREE with the
        commit subject's own `(#N)` hint, because the real report refuses any
        commit where the two disagree -- so a stub returning a fixed number
        quarantines the whole fixture instead of exercising the apply path."""
        subject = changeset.commit_subject(sha, repo_dir=engine["repo"])
        hint = changeset.extract_pr_number(subject)
        return [hint] if hint is not None else []

    kwargs = dict(
        marker="refs/synced/code-plane",
        remote="code-plane",
        remote_branch="main",
        repo_dir=engine["repo"],
        code_repo_slug="example/code",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="plane",
        local_ref="main",
        resolve_trust_allowlist=lambda: {"trusted"},
        resolve_prs_for_commit=_prs_for_commit,
        resolve_pr_author=lambda pr: "trusted",
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: SURFACE_PATTERNS,
        resolve_sensitive_prefixes=lambda: SENSITIVE_PREFIXES,
    )
    kwargs.update(overrides)
    return report_mod.classify_report(**kwargs)


def _run(engine, state_dir, recorder, **overrides):
    kwargs = dict(
        repo_dir=engine["repo"],
        state_dir=state_dir,
        engine_remote="origin",
        engine_repo_slug="example/engine",
        code_repo_slug="example/code",
        local_ref="main",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="plane",
        classify=lambda **kw: _classify(engine, **{k: v for k, v in kw.items() if k in _CLASSIFY_KEYS}),
        push_branch=recorder.push,
        open_pr=recorder.open_pr,
    )
    kwargs.update(overrides)
    return apply_inbound.apply_inbound(**kwargs)


_CLASSIFY_KEYS = {"max_files", "max_lines", "local_ref", "marker", "extra_paths", "known_commit_trust"}
# extra_paths/known_commit_trust MUST be forwarded: dropping them here made the
# harness silently test a channel with no carried debt at all.


class Recorder:
    def __init__(self):
        self.pushes: list[dict] = []
        self.prs: list[dict] = []

    def push(self, *, repo_dir, remote, commit_sha, branch):
        self.pushes.append({"remote": remote, "commit": commit_sha, "branch": branch})
        _git(Path(repo_dir), "update-ref", f"refs/heads/{branch}", commit_sha)

    def open_pr(self, *, repo_slug, branch, base, title, body):
        self.prs.append({"repo": repo_slug, "branch": branch, "base": base, "title": title, "body": body})
        return f"https://github.com/{repo_slug}/pull/1"


# ---------------------------------------------------------------------------
# The decision this module exists to get right
# ---------------------------------------------------------------------------


def test_local_patch_splits_on_local_hash_not_on_bucket_name():
    """The highest-consequence line. Two paths land in the SAME bucket and
    must get OPPOSITE treatment, decided by local_hash and nothing else.

    Asserting the bucket name here would pass on the broken behaviour, which
    is the entire point: `local-patch` conflates "the engine has no copy"
    (create, safe) with "the engine has a different copy" (silent overwrite,
    not safe)."""
    classifications = {
        "backend/new.py": {
            "status": pull.STATUS_LOCAL_PATCH,
            "engine_path": "backend/new.py",
            "local_hash": None,
            "upstream_hash": "aaa",
            "commits": ["c1"],
        },
        "backend/diverged.py": {
            "status": pull.STATUS_LOCAL_PATCH,
            "engine_path": "backend/diverged.py",
            "local_hash": "bbb",
            "upstream_hash": "ccc",
            "commits": ["c2"],
        },
    }
    write_set, withheld = apply_inbound.partition_write_set(classifications, set(), [])

    assert "backend/new.py" in write_set, "a local-patch path with no engine copy is a create and must be written"
    assert "backend/diverged.py" not in write_set, (
        "a local-patch path the engine already has a DIFFERENT copy of would be silently overwritten"
    )
    assert withheld["backend/diverged.py"]["status"] == apply_inbound.WITHHELD_WOULD_OVERWRITE
    # And the reason has to say what would have happened, not just that it did not.
    assert "overwrite" in withheld["backend/diverged.py"]["reason"]


def test_protected_and_sensitive_paths_never_reach_the_write_set():
    """Belt over braces: even a path the gate has (wrongly) marked
    clean-apply is withheld if it is protected or sensitive. A gate bug must
    refuse, not rewrite the sandbox hook."""
    classifications = {
        "hooks/sandbox.py": {
            "status": pull.STATUS_CLEAN_APPLY,
            "engine_path": "hooks/sandbox.py",
            "local_hash": "aaa",
            "upstream_hash": "bbb",
            "commits": ["c1"],
        },
        "scripts/engine-sync/pull.py": {
            "status": pull.STATUS_CLEAN_APPLY,
            "engine_path": "scripts/engine-sync/pull.py",
            "local_hash": "aaa",
            "upstream_hash": "bbb",
            "commits": ["c1"],
        },
    }
    protected = {"scripts/engine-sync/pull.py"}
    write_set, withheld = apply_inbound.partition_write_set(classifications, protected, ["hooks/"])
    assert write_set == {}
    assert withheld["hooks/sandbox.py"]["status"] == apply_inbound.WITHHELD_PROTECTED
    assert withheld["scripts/engine-sync/pull.py"]["status"] == apply_inbound.WITHHELD_PROTECTED


# ---------------------------------------------------------------------------
# C1 / C2 -- the real apply, and the engine surviving it
# ---------------------------------------------------------------------------


def test_c1_apply_builds_replayed_commits_and_opens_one_unmerged_pr(engine, state_dir):
    rec = Recorder()
    result = _run(engine, state_dir, rec)

    assert result["result"] == apply_inbound.RESULT_APPLIED, result
    assert len(rec.prs) == 1, "exactly one PR"
    assert rec.prs[0]["repo"] == "example/engine", "the PR opens on the ENGINE, not the code plane"
    assert rec.prs[0]["base"] == "main"

    # One commit per contributing inbound commit, on top of engine main.
    branch_tip = result["commit"]
    revs = _git(engine["repo"], "rev-list", f"{engine['main']}..{branch_tip}").split()
    assert len(revs) == 2, f"expected one replayed commit per contributing inbound commit, got {len(revs)}"

    # Authorship of the inbound commits is preserved, not flattened onto the sync.
    authors = _git(engine["repo"], "log", "--format=%ae", f"{engine['main']}..{branch_tip}").split()
    assert authors and all(a == "engine@example.com" for a in authors)


def test_c2_apply_does_not_delete_the_engine(engine, state_dir):
    """The invariant that would have caught a tree-diff implementation. The
    engine-only file is not in the export surface at all; a tree comparison
    between the planes would propose deleting it."""
    rec = Recorder()
    result = _run(engine, state_dir, rec)
    tip = result["commit"]

    before = _git(engine["repo"], "ls-tree", "-r", "--name-only", engine["main"]).split()
    after = _git(engine["repo"], "ls-tree", "-r", "--name-only", tip).split()

    for path in before:
        assert path in after, f"the apply removed {path} from the engine"

    # Byte-identical, not merely present.
    for path in ("keep/engine-only.txt", "backend/diverged.py"):
        assert _git(engine["repo"], "rev-parse", f"{engine['main']}:{path}").strip() == _git(
            engine["repo"], "rev-parse", f"{tip}:{path}"
        ).strip(), f"{path} changed"

    # And the one path that SHOULD have moved, did.
    assert _git(engine["repo"], "show", f"{tip}:backend/shared.py") == "shared v2\n"
    assert _git(engine["repo"], "show", f"{tip}:backend/new.py") == "brand new\n"


def test_out_of_surface_and_sensitive_paths_are_withheld_by_name(engine, state_dir):
    rec = Recorder()
    result = _run(engine, state_dir, rec)
    withheld = result["withheld"]

    assert "tests/out.txt" in withheld, "a withheld path that nobody names is a dropped path"
    assert "scripts/guard.sh" in withheld
    assert withheld["scripts/guard.sh"]["status"] == "needs-human-approval"

    tip = result["commit"]
    for path in ("tests/out.txt", "scripts/guard.sh"):
        proc = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"{tip}:{path}"],
            cwd=str(engine["repo"]),
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, f"{path} reached the branch despite being withheld"

    # Named in the PR body too -- that is the only place a human sees them.
    body = rec.prs[0]["body"]
    assert "tests/out.txt" in body
    assert "scripts/guard.sh" in body


# ---------------------------------------------------------------------------
# C3 -- nothing pushes outward, nothing merges
# ---------------------------------------------------------------------------


def test_c3_never_pushes_to_the_code_plane():
    with pytest.raises(apply_inbound.ApplyRefused, match="code plane"):
        apply_inbound._push_branch(repo_dir=Path("."), remote="code-plane", commit_sha="deadbeef", branch="b")


def test_c3_fake_git_and_gh_prove_no_outward_push_and_no_merge(engine, state_dir, tmp_path, monkeypatch):
    """A fake `git` fails the test if `push` ever names the code-plane
    remote; a fake `gh` fails it if `pr merge` is invoked at all. Both
    delegate everything else to the real binary so the run still works."""
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bad_push = tmp_path / "bad_push"
    merge_called = tmp_path / "merge_called"

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()

    fake_git = fakebin / "git"
    fake_git.write_text(
        f"""#!/usr/bin/env bash
if [ "$1" = "push" ]; then
  for a in "$@"; do
    if [ "$a" = "code-plane" ]; then
      touch "{bad_push}"
      echo "FAKE GIT: refusing outward push: $*" >&2
      exit 1
    fi
  done
fi
exec "{real_git}" "$@"
"""
    )
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC)

    fake_gh = fakebin / "gh"
    fake_gh.write_text(
        f"""#!/usr/bin/env bash
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  touch "{merge_called}"
  echo "FAKE GH: pr merge must never be invoked by the sync" >&2
  exit 1
fi
exit 0
"""
    )
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ['PATH']}")

    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED, result
    assert not bad_push.exists(), "the sync pushed to the code plane"
    assert not merge_called.exists(), "the sync merged its own PR"


# ---------------------------------------------------------------------------
# C4 -- ceilings have a consumer
# ---------------------------------------------------------------------------


def test_c4_ceiling_refusal_creates_no_branch_and_leaves_the_marker(engine, state_dir):
    """Asserting 'the count was printed' would pass on the bug. Assert the
    branch's absence and the marker's value."""
    marker_before = _git(engine["repo"], "rev-parse", "refs/synced/code-plane").strip()
    branches_before = _git(engine["repo"], "for-each-ref", "--format=%(refname)", "refs/heads/").split()

    rec = Recorder()
    result = _run(engine, state_dir, rec, max_files=1)

    assert result["result"] == apply_inbound.RESULT_REFUSED, result
    assert "ceiling" in result["reason"]
    assert rec.pushes == [] and rec.prs == []
    assert _git(engine["repo"], "rev-parse", "refs/synced/code-plane").strip() == marker_before
    assert _git(engine["repo"], "for-each-ref", "--format=%(refname)", "refs/heads/").split() == branches_before
    assert apply_inbound.read_failure_count(state_dir) == 1


def test_c4_line_ceiling_also_refuses(engine, state_dir):
    rec = Recorder()
    result = _run(engine, state_dir, rec, max_lines=1)
    assert result["result"] == apply_inbound.RESULT_REFUSED, result
    assert rec.prs == []


# ---------------------------------------------------------------------------
# D#2454 PR 2 -- a re-rooted/disjoint marker refuses under its own name
# ---------------------------------------------------------------------------


@pytest.fixture
def reroot_engine(tmp_path) -> dict:
    """Models the REAL D#2454 defect shape, not merely 'two unrelated
    repos': the marker is a commit whose tree survived whole into a LATER,
    separately-rooted history's own parentless root (a GitHub squash-merge
    onto an empty base does exactly this) -- so `git diff --name-status
    marker root` has zero D entries even though `git merge-base` is empty.

        marker-line:   marker (backend/shared.py = "shared v1")
        reroot-plane:  root R (#99)  -- backend/shared.py = "shared v1"
                                         backend/extra.py  = "brand new"  (superset, zero D)
                       tip C  (#100) -- backend/shared.py = "shared v2"

    refs/synced/code-plane is set to `marker`, wholly disjoint from
    reroot-plane (no shared commit at all)."""
    repo = tmp_path / "reroot_engine"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "engine@example.com")
    _git(repo, "config", "user.name", "Engine")

    # The engine's own copy of backend/shared.py matches the MARKER's
    # content exactly (both "shared v1") -- the realistic case: the marker
    # records what the engine already has, so this path clean-applies once
    # the reroot refusal is out of the way, rather than manufacturing an
    # unrelated three-way conflict that has nothing to do with this PR.
    _commit(
        repo,
        "engine seed",
        {"keep/engine-only.txt": "engine only, never exported\n", "backend/shared.py": "shared v1\n"},
    )
    engine_main = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "--orphan", "marker-line")
    _git(repo, "rm", "-rq", "--cached", ".")
    (repo / "keep/engine-only.txt").unlink()
    (repo / "backend/shared.py").unlink()
    marker_sha = _commit(repo, "marker seed", {"backend/shared.py": "shared v1\n"})

    _git(repo, "checkout", "-q", "--orphan", "reroot-plane")
    _git(repo, "rm", "-rq", "--cached", ".")
    (repo / "backend/shared.py").unlink()
    root_sha = _commit(
        repo,
        "squash merge (#99)",
        {"backend/shared.py": "shared v1\n", "backend/extra.py": "brand new\n"},
    )
    tip_sha = _commit(repo, "more work (#100)", {"backend/shared.py": "shared v2\n"})

    _git(repo, "checkout", "-q", "main")
    _git(repo, "update-ref", "refs/synced/code-plane", marker_sha)

    return {
        "repo": repo,
        "main": engine_main,
        "marker_sha": marker_sha,
        "root_sha": root_sha,
        "tip_sha": tip_sha,
    }


def _classify_reroot(reroot_engine: dict, **overrides):
    """Same shape as `_classify` above, but defaulting remote_ref to the
    reroot_engine fixture's disjoint 'reroot-plane' branch instead of
    'plane'."""
    import changeset
    import report as report_mod

    def _prs_for_commit(sha: str) -> list[int]:
        subject = changeset.commit_subject(sha, repo_dir=reroot_engine["repo"])
        hint = changeset.extract_pr_number(subject)
        return [hint] if hint is not None else []

    kwargs = dict(
        marker="refs/synced/code-plane",
        remote="code-plane",
        remote_branch="main",
        repo_dir=reroot_engine["repo"],
        code_repo_slug="example/code",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="reroot-plane",
        local_ref="main",
        resolve_trust_allowlist=lambda: {"trusted"},
        resolve_prs_for_commit=_prs_for_commit,
        resolve_pr_author=lambda pr: "trusted",
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: SURFACE_PATTERNS,
        resolve_sensitive_prefixes=lambda: SENSITIVE_PREFIXES,
    )
    kwargs.update(overrides)
    return report_mod.classify_report(**kwargs)


def test_reroot_marker_refuses_named_reroot_not_ceiling(reroot_engine):
    """Items 7-9: the refusal names re-rooted/unrelated history (never
    'ceiling'), names both commits, states no merge-base exists, and
    additionally names the absorbing root commit."""
    result = _classify_reroot(reroot_engine)

    assert result["refused"] is True, result
    reason = result["refusal_reason"]
    assert "ceiling" not in reason, reason
    assert "re-root" in reason or "re-rooted" in reason, reason
    assert reroot_engine["marker_sha"] in reason, reason
    assert reroot_engine["tip_sha"] in reason, reason
    assert "merge-base" in reason.lower(), reason
    assert "no merge-base exists" in reason.lower(), reason
    assert reroot_engine["root_sha"] in reason, reason
    assert "disjoint_marker_bypass" not in result


def test_reroot_marker_names_a_genuinely_unrelated_history_differently(reroot_engine):
    """The counterpart to the re-root case: a marker that shares NO content
    with remote_ref's root (not even by coincidence) must still refuse --
    but the message must not claim a re-root it cannot support."""
    # Branching from "main" here (the fixture leaves the repo checked out
    # there), so the only tracked file to clear is main's own.
    _git(reroot_engine["repo"], "checkout", "-q", "--orphan", "wholly-unrelated")
    _git(reroot_engine["repo"], "rm", "-rq", "--cached", ".")
    (reroot_engine["repo"] / "keep/engine-only.txt").unlink()
    (reroot_engine["repo"] / "backend/shared.py").unlink()
    _commit(reroot_engine["repo"], "totally unrelated (#7)", {"nothing/alike.txt": "z\n"})
    _git(reroot_engine["repo"], "checkout", "-q", "main")

    result = _classify_reroot(reroot_engine, remote_ref="wholly-unrelated")
    assert result["refused"] is True, result
    reason = result["refusal_reason"]
    assert "ceiling" not in reason, reason
    assert "re-root" in reason or "re-rooted" in reason, reason
    assert "unrelated history" in reason.lower(), reason
    assert reroot_engine["root_sha"] not in reason, reason


def test_healthy_marker_still_reaches_ceiling_check_unchanged(engine, state_dir):
    """Item 13: a marker that IS a proper ancestor of remote_ref must pass
    straight through the shared-history check -- proved directly against
    changeset.marker_is_ancestor, and against the ceiling refusal still
    firing (and still saying 'ceiling', never 're-root') on this fixture."""
    import changeset

    assert changeset.marker_is_ancestor("refs/synced/code-plane", "plane", repo_dir=engine["repo"]) is True

    result = _classify(engine, max_files=1)
    assert result["refused"] is True, result
    assert "ceiling" in result["refusal_reason"], result["refusal_reason"]
    assert "re-root" not in result["refusal_reason"], result["refusal_reason"]
    assert "disjoint_marker_bypass" not in result


def test_allow_disjoint_marker_without_dry_run_refuses_before_classify_ever_runs(reroot_engine, state_dir):
    """Item 11, second half: outside a dry run, --allow-disjoint-marker is
    refused before classify() is ever called -- no remote contact, no
    bypass record, no failure-counter write."""
    calls = []

    def _counting_classify(**kw):
        calls.append(kw)
        return _classify_reroot(reroot_engine, **{k: v for k, v in kw.items() if k in _CLASSIFY_KEYS | {"allow_disjoint_marker"}})

    rec = Recorder()
    result = apply_inbound.apply_inbound(
        repo_dir=reroot_engine["repo"],
        state_dir=state_dir,
        engine_remote="origin",
        engine_repo_slug="example/engine",
        code_repo_slug="example/code",
        local_ref="main",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="reroot-plane",
        classify=_counting_classify,
        push_branch=rec.push,
        open_pr=rec.open_pr,
        dry_run=False,
        allow_disjoint_marker=True,
    )

    assert result["result"] == apply_inbound.RESULT_REFUSED, result
    assert "--dry-run" in result["reason"], result["reason"]
    assert calls == [], "classify() must never run when the flag/dry-run pairing is invalid"
    assert rec.pushes == [] and rec.prs == []
    assert not apply_inbound.state_path(state_dir).exists()
    assert not apply_inbound.disjoint_bypass_log_path(state_dir).exists()


def test_allow_disjoint_marker_with_dry_run_proceeds_and_writes_bypass_record(reroot_engine, state_dir):
    """Items 11 (first half) and 12: with both flags, the tool proceeds past
    the re-root refusal, produces its report (this fixture's tiny change set
    clears the unchanged 50-file/500-line ceiling too), and writes exactly
    one bypass record naming both SHAs."""
    rec = Recorder()
    result = apply_inbound.apply_inbound(
        repo_dir=reroot_engine["repo"],
        state_dir=state_dir,
        engine_remote="origin",
        engine_repo_slug="example/engine",
        code_repo_slug="example/code",
        local_ref="main",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="reroot-plane",
        classify=lambda **kw: _classify_reroot(
            reroot_engine, **{k: v for k, v in kw.items() if k in _CLASSIFY_KEYS | {"allow_disjoint_marker"}}
        ),
        push_branch=rec.push,
        open_pr=rec.open_pr,
        dry_run=True,
        allow_disjoint_marker=True,
    )

    assert result["result"] == "dry-run", result
    assert rec.pushes == [] and rec.prs == []
    assert not apply_inbound.state_path(state_dir).exists(), "dry-run must still write no operational state"

    bypass_log = apply_inbound.disjoint_bypass_log_path(state_dir)
    assert bypass_log.exists(), "the flag was used but wrote no record"
    lines = [line for line in bypass_log.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, lines
    record = json.loads(lines[0])
    assert record["marker_sha"] == reroot_engine["marker_sha"], record
    assert record["remote_sha"] == reroot_engine["tip_sha"], record
    assert "re-root" in record["bypassed_reason"] or "re-rooted" in record["bypassed_reason"], record


def test_allow_disjoint_marker_writes_no_record_when_marker_is_healthy(engine, state_dir):
    """The flag is a no-op when there is nothing to bypass -- must not write
    a record claiming a bypass that never happened."""
    rec = Recorder()
    result = _run(engine, state_dir, rec, dry_run=True, allow_disjoint_marker=True)
    assert result["result"] != apply_inbound.RESULT_REFUSED or "re-root" not in result.get("reason", ""), result
    assert not apply_inbound.disjoint_bypass_log_path(state_dir).exists()


# ---------------------------------------------------------------------------
# D#2454 PR 4 -- the re-root bridge (the real fix), the write-set ceiling,
# and the recalibrated file/line ceilings
# ---------------------------------------------------------------------------

def test_allow_reroot_from_real_run_bridges_writes_and_advances_marker(reroot_engine, state_dir):
    """Unlike --allow-disjoint-marker, this is the REAL fix: a REAL (non
    dry) run with --allow-reroot-from naming the marker's current sha must
    bridge past the re-root refusal, build and push a branch, open a PR, and
    advance the marker to the plane's tip -- exactly the end-to-end shape
    item 26 exercises against the live plane."""
    rec = Recorder()
    result = apply_inbound.apply_inbound(
        repo_dir=reroot_engine["repo"],
        state_dir=state_dir,
        engine_remote="origin",
        engine_repo_slug="example/engine",
        code_repo_slug="example/code",
        local_ref="main",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="reroot-plane",
        classify=lambda **kw: _classify_reroot(
            reroot_engine, **{k: v for k, v in kw.items() if k in _CLASSIFY_KEYS | {"allow_reroot_from"}}
        ),
        push_branch=rec.push,
        open_pr=rec.open_pr,
        dry_run=False,
        allow_reroot_from=reroot_engine["marker_sha"],
    )

    assert result["result"] == apply_inbound.RESULT_APPLIED, result
    assert len(rec.pushes) == 1 and len(rec.prs) == 1, result
    # backend/extra.py is a create introduced at the synthetic root; backend/
    # shared.py goes shared v1 -> shared v2 on the real tip commit. Both must
    # actually land in the write set -- this is the bridge doing real work,
    # not merely refusing more quietly.
    assert set(result["written"]) == {"backend/extra.py", "backend/shared.py"}, result
    assert (
        _git(reroot_engine["repo"], "rev-parse", "refs/synced/code-plane").strip() == reroot_engine["tip_sha"]
    ), "the marker must advance to the plane's tip as a PRODUCT of the run, not by hand"


def test_allow_reroot_from_stale_sha_refuses_per_pr2_even_in_a_real_run(reroot_engine, state_dir):
    """Leg (c): a --allow-reroot-from that does not name the marker's OWN
    CURRENT sha must fall through to the ordinary PR 2 refusal -- naming
    re-root, never ceiling -- and must not touch the remote."""
    rec = Recorder()
    result = apply_inbound.apply_inbound(
        repo_dir=reroot_engine["repo"],
        state_dir=state_dir,
        engine_remote="origin",
        engine_repo_slug="example/engine",
        code_repo_slug="example/code",
        local_ref="main",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        remote_ref="reroot-plane",
        classify=lambda **kw: _classify_reroot(
            reroot_engine, **{k: v for k, v in kw.items() if k in _CLASSIFY_KEYS | {"allow_reroot_from"}}
        ),
        push_branch=rec.push,
        open_pr=rec.open_pr,
        dry_run=False,
        allow_reroot_from=reroot_engine["root_sha"],  # names the root, not the marker -- stale/wrong
    )

    assert result["result"] == apply_inbound.RESULT_REFUSED, result
    assert "re-root" in result["reason"] or "re-rooted" in result["reason"], result
    assert "ceiling" not in result["reason"], result
    assert rec.pushes == [] and rec.prs == []


def test_write_set_ceiling_refuses_before_branch_is_built(engine, state_dir):
    """Item 24: the write set from the `engine` fixture is 3 paths
    (backend/shared.py, backend/new.py, backend/second.py) -- a ceiling of 2
    must refuse before anything is built or pushed, naming the write-set
    ceiling (never the enumeration ceiling, which this change set clears)."""
    marker_before = _git(engine["repo"], "rev-parse", "refs/synced/code-plane").strip()
    rec = Recorder()
    result = _run(engine, state_dir, rec, max_write_set=2)

    assert result["result"] == apply_inbound.RESULT_REFUSED, result
    assert "write set exceeds ceiling" in result["reason"], result["reason"]
    assert "2 path" in result["reason"] or "3 path" in result["reason"], result["reason"]
    assert rec.pushes == [] and rec.prs == []
    assert _git(engine["repo"], "rev-parse", "refs/synced/code-plane").strip() == marker_before


def test_write_set_ceiling_override_lets_the_same_run_through(engine, state_dir):
    """The one-shot override: the identical fixture that refuses at
    max_write_set=2 above must proceed to a normal apply once the ceiling is
    raised to admit its actual write-set size."""
    rec = Recorder()
    result = _run(engine, state_dir, rec, max_write_set=3)
    assert result["result"] == apply_inbound.RESULT_APPLIED, result
    assert len(rec.pushes) == 1 and len(rec.prs) == 1


def test_write_set_ceiling_does_not_fire_at_the_default(engine, state_dir):
    """The default (25) must not fire on this fixture's ordinary 3-path
    write set -- proved directly rather than only inferred from the other
    passing tests that happen to use the default."""
    assert apply_inbound.DEFAULT_MAX_WRITE_SET == 25
    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED, result


def test_line_ceiling_recalibration_admits_ordinary_merges_the_old_one_rejected(scratch_repo_for_ceilings):
    """A REGRESSION GUARD on the ceiling's behaviour, not evidence for the
    5000 constant -- that evidence is a real measurement, cited with its
    command and numbers in report.py's DEFAULT_MAX_LINES comment, not here.
    This fixture's commit sizes are hand-picked to straddle the OLD 500-line
    ceiling on purpose, so this test would keep passing even if the real
    plane's distribution later shifted; it exists to catch the ceiling being
    silently lowered back down or the file ceiling stopping to catch an
    oversized commit, not to justify any particular number."""
    import changeset
    import report as report_mod

    repo, seed, root, ordinary_tips = scratch_repo_for_ceilings

    over_old_line_ceiling = 0
    over_new_line_ceiling = 0
    for tip in ordinary_tips:
        cs = changeset.build_changeset(seed, tip, repo_dir=repo)
        total_lines = cs["total_insertions"] + cs["total_deletion_lines"]
        assert cs["gated_path_count"] <= report_mod.DEFAULT_MAX_FILES, "an ordinary merge must never trip the file ceiling"
        if total_lines > 500:
            over_old_line_ceiling += 1
        if total_lines > report_mod.DEFAULT_MAX_LINES:
            over_new_line_ceiling += 1

    root_cs = changeset.build_changeset(seed, root, repo_dir=repo)
    assert root_cs["gated_path_count"] > report_mod.DEFAULT_MAX_FILES, "the artifact-root-shaped commit must still trip the file ceiling"

    assert report_mod.DEFAULT_MAX_LINES == 5000
    assert over_old_line_ceiling >= len(ordinary_tips) // 2 - 1, (
        f"expected roughly half of {len(ordinary_tips)} ordinary merges over the OLD 500-line ceiling, "
        f"got {over_old_line_ceiling}"
    )
    assert over_new_line_ceiling == 0, "the recalibrated ceiling must admit every ordinary merge in this distribution"


@pytest.fixture
def scratch_repo_for_ceilings(tmp_path):
    """A disposable, SYNTHETIC repo -- not a sample of real history -- for
    the regression guard above: one oversized 'artifact root' commit (many
    files, well over the file ceiling) and several ordinary single-file
    merges whose hand-picked line counts straddle the OLD 500-line ceiling
    on purpose. See report.py's DEFAULT_MAX_LINES comment for the actual
    measurement this PR's ceiling value is calibrated against."""
    repo = tmp_path / "ceilings"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    seed = _commit(repo, "seed", {"seed.txt": "0\n"})

    root = _commit(repo, "artifact root (#1)", {f"artifact/f{i}.txt": f"content {i}\n" for i in range(65)})

    _git(repo, "checkout", "-q", seed)
    sizes = [200, 600, 150, 900, 50, 1200, 300, 700]
    tips = []
    for i, n in enumerate(sizes):
        _git(repo, "checkout", "-q", "-B", f"ordinary-{i}", seed)
        tip = _commit(repo, f"ordinary merge {i} (#{i + 2})", {f"f{i}.txt": "x\n" * n})
        tips.append(tip)
    _git(repo, "checkout", "-q", "main")

    return repo, seed, root, tips


# ---------------------------------------------------------------------------
# C5 -- the failure counter disables, and a success resets it
# ---------------------------------------------------------------------------


def test_c5_three_failures_disable_the_channel_with_zero_remote_traffic(engine, state_dir, tmp_path, monkeypatch):
    """Disabled has to mean no traffic at all. A fake `git` fails the test if
    ANY fetch or ls-remote reaches the code-plane remote once the counter is
    at its limit."""
    apply_inbound.write_failure_count(state_dir, 3)

    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    touched = tmp_path / "remote_touched"
    fakebin = tmp_path / "fakebin2"
    fakebin.mkdir()
    fake_git = fakebin / "git"
    fake_git.write_text(
        f"""#!/usr/bin/env bash
if [ "$1" = "fetch" ] || [ "$1" = "ls-remote" ]; then
  for a in "$@"; do
    if [ "$a" = "code-plane" ]; then
      touch "{touched}"
      echo "FAKE GIT: disabled channel contacted the remote: $*" >&2
      exit 1
    fi
  done
fi
exec "{real_git}" "$@"
"""
    )
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ['PATH']}")

    rec = Recorder()
    result = _run(engine, state_dir, rec, do_fetch=True)

    assert result["result"] == apply_inbound.RESULT_DISABLED, result
    assert not touched.exists(), "a disabled channel still contacted the remote"
    assert rec.pushes == [] and rec.prs == []


def test_c5_a_single_success_resets_the_counter(engine, state_dir):
    apply_inbound.write_failure_count(state_dir, 2)
    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED, result
    assert apply_inbound.read_failure_count(state_dir) == 0


def test_c5_counter_increments_only_up_to_the_limit_then_stops_running(engine, state_dir):
    rec = Recorder()
    for expected in (1, 2, 3):
        result = _run(engine, state_dir, rec, max_files=1)
        assert result["result"] == apply_inbound.RESULT_REFUSED
        assert apply_inbound.read_failure_count(state_dir) == expected
    result = _run(engine, state_dir, rec, max_files=1)
    assert result["result"] == apply_inbound.RESULT_DISABLED


# ---------------------------------------------------------------------------
# C6 -- a conflict fails closed and never partially applies
# ---------------------------------------------------------------------------


def test_c6_conflict_applies_nothing_and_leaves_the_tree_untouched(engine, state_dir):
    """A synthetic conflict: the engine and the plane both changed
    backend/shared.py away from the marker's baseline. The run must apply
    NOTHING -- not even backend/new.py, which is clean on its own."""
    repo = engine["repo"]
    porcelain_before = _git(repo, "status", "--porcelain")
    marker_before = _git(repo, "rev-parse", "refs/synced/code-plane").strip()

    # Diverge the engine's copy of a path the plane also changed.
    (repo / "backend/shared.py").write_text("ENGINE diverged too\n")
    _git(repo, "add", "backend/shared.py")
    _git(repo, "commit", "-q", "-m", "engine also touches shared")
    new_main = _git(repo, "rev-parse", "HEAD").strip()

    rec = Recorder()
    result = _run(engine, state_dir, rec, local_ref="main")

    assert result["result"] == apply_inbound.RESULT_CONFLICT, result
    assert "backend/shared.py" in result["conflicted"]
    assert rec.pushes == [] and rec.prs == [], "a conflict must not open a PR"
    assert _git(repo, "rev-parse", "refs/synced/code-plane").strip() == marker_before
    assert _git(repo, "status", "--porcelain") == porcelain_before
    assert _git(repo, "rev-parse", "HEAD").strip() == new_main, "HEAD moved"
    assert apply_inbound.read_failure_count(state_dir) == 1


# ---------------------------------------------------------------------------
# PR 1 (D#2454) -- --dry-run must not write
# ---------------------------------------------------------------------------


def _dry_run_prep_ceiling_refusal(engine):
    """The classify/ceiling refusal: ApplyRefused raised inside `_run`,
    caught in `apply_inbound`'s outer try/except."""
    return dict(max_files=1)


def _dry_run_prep_conflict_refusal(engine):
    """A genuine content conflict: the engine and the plane both changed
    backend/shared.py away from the marker's baseline."""
    repo = engine["repo"]
    (repo / "backend/shared.py").write_text("ENGINE diverged too\n")
    _git(repo, "add", "backend/shared.py")
    _git(repo, "commit", "-q", "-m", "engine also touches shared")
    return dict(local_ref="main")


def _dry_run_prep_nothing_to_write(engine):
    """Every candidate path withheld as sensitive: the `if not write_set`
    branch, no exception involved."""
    return dict(
        classify=lambda **kw: _classify(engine, resolve_sensitive_prefixes=lambda: ["backend/", "scripts/"])
    )


def _dry_run_prep_unexpected_exception(engine):
    """An unrelated failure inside the classify call (a git subprocess
    error, a bad protected.txt/sensitive.txt parse, a remote timeout) --
    caught by the generic `except Exception` that wraps the whole `_run`
    call, not by `except ApplyRefused`."""

    def boom(**kw):
        raise RuntimeError("synthetic failure standing in for a git/network error")

    return dict(classify=boom)


@pytest.mark.parametrize(
    "prep",
    [
        _dry_run_prep_ceiling_refusal,
        _dry_run_prep_conflict_refusal,
        _dry_run_prep_nothing_to_write,
        _dry_run_prep_unexpected_exception,
    ],
    ids=["ceiling_refusal", "conflict_refusal", "nothing_to_write", "unexpected_exception"],
)
def test_dry_run_never_writes_state_regardless_of_exit_path(engine, state_dir, prep):
    """Closes the class rather than the instances. `apply_inbound()` has
    five call sites that can touch the state file (see the module's own
    write_state/write_failure_count grep); one is unreachable under
    dry_run (it sits after `_run`'s own dry-run early return on the
    APPLIED path) and the other four are exactly the parametrized cases
    here. Whichever of those a dry run takes, the state file must come out
    exactly as it went in -- absent if it was absent, byte-identical if it
    already existed. A fifth write path added later without updating this
    parametrization is exactly the failure mode this test exists to catch;
    it will not, by construction, be caught by adding a case here after the
    fact -- the grep in the PR description is what closes that gap."""
    overrides = prep(engine)
    rec = Recorder()
    state_file = apply_inbound.state_path(state_dir)

    # Starting from no state file at all.
    assert not state_file.exists()
    result = _run(engine, state_dir, rec, dry_run=True, **overrides)
    assert result["result"] != apply_inbound.RESULT_APPLIED, result
    assert not state_file.exists(), f"dry-run exit path {result['result']!r} created the state file"
    assert rec.pushes == [] and rec.prs == [], f"dry-run exit path {result['result']!r} touched the remote"

    # Starting from a state file that already exists: byte-identical, not
    # merely holding the same number again.
    apply_inbound.write_failure_count(state_dir, 0)
    before = state_file.read_bytes()
    result2 = _run(engine, state_dir, rec, dry_run=True, **overrides)
    assert result2["result"] != apply_inbound.RESULT_APPLIED, result2
    assert state_file.read_bytes() == before, f"dry-run exit path {result2['result']!r} mutated the state file"


def test_dry_run_does_not_disarm_the_circuit_breaker_for_a_real_refusal(engine, state_dir):
    """The fix must be scoped to dry_run, not weaken the real breaker. A dry
    run costs nothing; a real refusal right after it must still count --
    proving the guard is `if not dry_run`, not a change to when a refusal is
    raised at all."""
    rec = Recorder()
    dry = _run(engine, state_dir, rec, max_files=1, dry_run=True)
    assert dry["result"] == apply_inbound.RESULT_REFUSED
    assert apply_inbound.read_failure_count(state_dir) == 0

    real = _run(engine, state_dir, rec, max_files=1, dry_run=False)
    assert real["result"] == apply_inbound.RESULT_REFUSED
    assert apply_inbound.read_failure_count(state_dir) == 1


# ---------------------------------------------------------------------------
# D#2454 PR 3 -- a conflicted path must be able to enter the debt
# ---------------------------------------------------------------------------


def _diverge_shared_on_the_engine(engine: dict) -> None:
    """The README.md shape: a genuine three-way divergence. The marker's
    baseline for backend/shared.py is 'shared v1'; the plane already moved it
    to 'shared v2' (c1); this makes the ENGINE'S copy diverge too, so
    `classify_against_baseline` returns real STATUS_CONFLICT -- not
    STATUS_LOCAL_PATCH, which is what `backend/diverged.py` already exercises
    elsewhere in this file (it has no baseline at all)."""
    repo = engine["repo"]
    (repo / "backend/shared.py").write_text("ENGINE diverged too\n")
    _git(repo, "add", "backend/shared.py")
    _git(repo, "commit", "-q", "-m", "engine also touches shared")


def test_conflicted_path_enters_the_debt_on_first_refusal(engine, state_dir):
    """Item 16. Before this PR, `_run` returned on the conflict check
    (`:895-902` at spec-writing time) before `build_pending` -- the only
    producer of the debt -- was ever called, so a conflicted path could never
    become `known_debt`. Assert the debt is empty going in, so this is really
    testing the write, not a fixture that already carried it."""
    assert apply_inbound.read_pending(state_dir) == {}, "test assumes no pre-existing debt"
    _diverge_shared_on_the_engine(engine)

    rec = Recorder()
    result = _run(engine, state_dir, rec, local_ref="main")

    assert result["result"] == apply_inbound.RESULT_CONFLICT, result
    assert "backend/shared.py" in result["conflicted"]
    assert rec.pushes == [] and rec.prs == [], "a conflict must not open a PR"

    pending = apply_inbound.read_pending(state_dir)
    assert "backend/shared.py" in pending, "a conflicted path never entered the debt"
    assert pending["backend/shared.py"]["status"] == pull.STATUS_CONFLICT
    assert apply_inbound.read_failure_count(state_dir) == 1


def test_conflict_refusal_under_dry_run_still_writes_no_state(engine, state_dir):
    """The --dry-run promise (D#2454 PR 1) must hold for the new write too:
    persisting the newly-conflicted path into `pending` is still a write to
    `pending`, and a dry run must leave the state file exactly as it found
    it -- absent, here."""
    _diverge_shared_on_the_engine(engine)
    state_file = apply_inbound.state_path(state_dir)
    assert not state_file.exists()

    rec = Recorder()
    result = _run(engine, state_dir, rec, local_ref="main", dry_run=True)

    assert result["result"] == apply_inbound.RESULT_CONFLICT, result
    assert not state_file.exists(), "a dry run must not persist the conflicted path into pending"


def test_second_run_does_not_refuse_over_a_now_known_conflict(engine, state_dir):
    """Item 17. Run twice against the README.md-shaped fixture: the first
    run refuses and records the path (proven above); the second run must NOT
    refuse over that same path again, because it is now in `known_debt`."""
    _diverge_shared_on_the_engine(engine)

    rec1 = Recorder()
    result1 = _run(engine, state_dir, rec1, local_ref="main")
    assert result1["result"] == apply_inbound.RESULT_CONFLICT, result1
    assert "backend/shared.py" in apply_inbound.read_pending(state_dir)

    rec2 = Recorder()
    result2 = _run(engine, state_dir, rec2, local_ref="main")

    assert result2["result"] != apply_inbound.RESULT_CONFLICT, (
        f"a second run refused again over a path already in the debt: {result2}"
    )
    # Item 19 (both runs): a conflicted path is never in the write set --
    # it stays owed, not applied.
    if result2["result"] == apply_inbound.RESULT_APPLIED:
        assert "backend/shared.py" not in result2["written"], "a conflicted path must never be written"

    pending2 = apply_inbound.read_pending(state_dir)
    assert "backend/shared.py" in pending2, "the conflicted path stopped being owed"
    assert pending2["backend/shared.py"]["status"] == pull.STATUS_CONFLICT


def test_a_new_conflict_not_in_the_debt_still_refuses_everything_after_pr3(engine, state_dir):
    """Item 18, re-proven after the reordering above: a conflict on a path
    NOT already in the debt must still refuse the whole run untouched --
    the reordering that lets a conflict enter the debt on refusal must not
    also have started letting a first-time conflict slip through."""
    repo = engine["repo"]
    _diverge_shared_on_the_engine(engine)
    # A second, unrelated writable path in the same run, to prove it too is
    # withheld rather than applied around the conflict.
    _git(repo, "checkout", "-q", "plane")
    _commit(repo, "unrelated new work (#9)", {"backend/also_new.py": "also new\n"})
    _git(repo, "checkout", "-q", "main")

    rec = Recorder()
    result = _run(engine, state_dir, rec, local_ref="main")

    assert result["result"] == apply_inbound.RESULT_CONFLICT, result
    assert rec.pushes == [] and rec.prs == [], "a first-time conflict must still block the whole run"
    before = _git(repo, "ls-tree", "-r", "--name-only", "main")
    assert "backend/also_new.py" not in before, "nothing was applied to the engine tree"


# ---------------------------------------------------------------------------
# C7 -- the marker advances only after a completed apply
# ---------------------------------------------------------------------------


def test_c7_marker_advances_only_on_a_completed_apply(engine, state_dir):
    repo = engine["repo"]
    assert _git(repo, "rev-parse", "refs/synced/code-plane").strip() == engine["plane_seed"]

    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED
    assert _git(repo, "rev-parse", "refs/synced/code-plane").strip() == engine["plane_tip"]


def test_c7_marker_unchanged_when_the_pr_step_fails(engine, state_dir):
    """The marker must not move if the PR never opened -- otherwise the next
    run reports in-sync for commits nobody was ever shown."""
    repo = engine["repo"]
    marker_before = _git(repo, "rev-parse", "refs/synced/code-plane").strip()

    def boom(**kwargs):
        raise apply_inbound.ApplyRefused("gh pr create failed: simulated")

    rec = Recorder()
    result = _run(engine, state_dir, rec, open_pr=boom)
    assert result["result"] == apply_inbound.RESULT_REFUSED
    assert _git(repo, "rev-parse", "refs/synced/code-plane").strip() == marker_before


def test_c7_marker_unchanged_when_nothing_is_writable(engine, state_dir):
    """Everything sensitive: no write set, so no PR and no marker movement.
    The commits are still owed."""
    repo = engine["repo"]
    marker_before = _git(repo, "rev-parse", "refs/synced/code-plane").strip()
    rec = Recorder()
    result = _run(
        engine,
        state_dir,
        rec,
        classify=lambda **kw: _classify(engine, resolve_sensitive_prefixes=lambda: ["backend/", "scripts/"]),
    )
    assert result["result"] == apply_inbound.RESULT_NOTHING, result
    assert rec.prs == []
    assert _git(repo, "rev-parse", "refs/synced/code-plane").strip() == marker_before


# ---------------------------------------------------------------------------
# C8 -- no agent spawn anywhere in the sync path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["apply", "conflict", "ceiling"])
def test_c8_no_agent_spawn_on_any_path(engine, state_dir, tmp_path, monkeypatch, mode):
    """A fake `claude` earlier on PATH fails the test if invoked anywhere in
    a run -- including the conflict and refusal paths, which is where a
    'just ask an agent to fix it' shortcut would most plausibly be added."""
    spawned = tmp_path / f"claude_spawned_{mode}"
    fakebin = tmp_path / f"fakebin_{mode}"
    fakebin.mkdir()
    fake_claude = fakebin / "claude"
    fake_claude.write_text(
        f"""#!/usr/bin/env bash
touch "{spawned}"
echo "FAKE CLAUDE: the sync path must never spawn an agent" >&2
exit 1
"""
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ['PATH']}")

    rec = Recorder()
    if mode == "apply":
        _run(engine, state_dir, rec)
    elif mode == "ceiling":
        _run(engine, state_dir, rec, max_files=1)
    else:
        repo = engine["repo"]
        (repo / "backend/shared.py").write_text("ENGINE diverged too\n")
        _git(repo, "add", "backend/shared.py")
        _git(repo, "commit", "-q", "-m", "engine also touches shared")
        _run(engine, state_dir, rec)

    assert not spawned.exists(), "the sync path spawned an agent"


# ---------------------------------------------------------------------------
# The working tree is never touched, on any path
# ---------------------------------------------------------------------------


def test_apply_leaves_working_tree_index_and_head_untouched(engine, state_dir):
    repo = engine["repo"]
    porcelain_before = _git(repo, "status", "--porcelain")
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    branch_before = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED

    assert _git(repo, "status", "--porcelain") == porcelain_before
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch_before


def test_post_apply_invariant_refuses_a_tree_that_lost_a_blob(engine, state_dir, monkeypatch):
    """The invariant itself has to be able to fail. Force the built tree to
    be the code plane's (which has none of the engine-only files) and assert
    the blob-count check refuses rather than pushing it."""
    repo = engine["repo"]

    real_git = apply_inbound._git

    def sabotaged(args, repo_dir, env=None, timeout=120):
        if args and args[0] == "write-tree":
            return _git(repo, "rev-parse", "plane^{tree}")
        return real_git(args, repo_dir, env=env, timeout=timeout)

    monkeypatch.setattr(apply_inbound, "_git", sabotaged)

    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_REFUSED, result
    assert "post-apply invariant failed" in result["reason"], result
    assert rec.pushes == [] and rec.prs == []


def test_pr_body_carries_both_path_counts(engine, state_dir):
    """The headline number a human reads must say which number it is."""
    rec = Recorder()
    _run(engine, state_dir, rec)
    body = rec.prs[0]["body"]
    assert "file(s) changed" in body
    assert "paths gated" in body


def test_pr_body_names_the_marker_ref_and_its_resolved_sha(engine, state_dir):
    """Caught by running the real thing: the body sliced the marker's REF
    NAME to twelve characters as if it were a sha, rendering the base of the
    whole change set as `refs/synced/`. Both halves have to be there --
    the ref that was read, and the commit it pointed at."""
    rec = Recorder()
    _run(engine, state_dir, rec)
    body = rec.prs[0]["body"]
    assert "refs/synced/code-plane" in body, body.splitlines()[:4]
    assert engine["plane_seed"][:12] in body, body.splitlines()[:4]
    assert "`refs/synced/`" not in body


def test_replay_subject_strips_the_code_plane_pr_number():
    """`(#3)` names a PR on the code plane. On the engine the same number
    names something else entirely, and GitHub will link it."""
    assert apply_inbound._replay_subject("widen the shared helper (#3)") == "widen the shared helper"
    assert apply_inbound._replay_subject("no pr reference here") == "no pr reference here"


def test_disabled_result_is_reported_not_silently_skipped(engine, state_dir):
    apply_inbound.write_failure_count(state_dir, 99)
    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_DISABLED
    assert "consecutive failed runs" in result["reason"]
    assert result["consecutive_failures"] == 99


# ---------------------------------------------------------------------------
# Gate 1 must not be a constant the tool writes about itself
# ---------------------------------------------------------------------------


def _two_gate(body: str) -> tuple[int, str]:
    """Drive the real scripts/lib/two-gate-check.sh over a body."""
    script = _THIS_DIR.parent.parent / "lib" / "two-gate-check.sh"
    proc = subprocess.run(
        ["bash", "-c", f'source "{script}"; check_two_gate_markers 99999 ""; rc=$?; '
                       'echo "REASON:$TWO_GATE_FAIL_REASON"; exit $rc'],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TWO_GATE_PR_BODY_99999": body.replace("\n", "\\n")},
    )
    return proc.returncode, proc.stdout


def test_gate1_is_na_with_a_reason_not_a_manufactured_pass(engine, state_dir):
    """The channel must not write its own gate satisfaction.

    A constant `Gate 1: PASS` in the body-builder satisfies two-gate-check for
    every PR this channel will ever open, while naming no run and deriving
    from nothing. A reviewer seeing it would reasonably believe a gate ran."""
    rec = Recorder()
    _run(engine, state_dir, rec)
    body = rec.prs[0]["body"]

    assert "Gate 1: N/A" in body, body[-800:]
    assert "Gate 1: PASS" not in body, "the sync manufactured its own Gate 1 pass"
    # N/A is only honest with a reason attached, and the reason has to name
    # what was tested instead -- the originating PRs on the code plane.
    gate1_line = next(ln for ln in body.splitlines() if ln.startswith("Gate 1:"))
    assert "no test suite of its own" in gate1_line
    assert "#" in gate1_line, f"Gate 1 N/A names no originating PR: {gate1_line}"


def test_two_gate_check_accepts_the_new_form_and_rejects_a_bare_na(engine, state_dir):
    """Observed against the real checker, not assumed: the N/A-with-reason
    form passes, and stripping the reason fails."""
    rec = Recorder()
    _run(engine, state_dir, rec)
    body = rec.prs[0]["body"]

    rc, out = _two_gate(body)
    assert rc == 0, f"the generated body no longer satisfies two-gate-check: {out}"

    # Negative: a body with no Gate 1 marker at all must fail, so we know the
    # checker is actually looking rather than waving everything through.
    stripped = "\n".join(ln for ln in body.splitlines() if not ln.startswith("Gate 1:"))
    rc_bad, out_bad = _two_gate(stripped)
    assert rc_bad == 1, f"two-gate-check passed a body with no Gate 1 marker: {out_bad}"
    assert "Gate 1 marker missing" in out_bad


# ---------------------------------------------------------------------------
# Withheld paths are owed, not announced once
# ---------------------------------------------------------------------------


def test_withheld_paths_persist_as_debt_across_the_marker_advance(engine, state_dir):
    """The defect: withheld paths were named in one PR body and then never
    re-entered a change set, because the marker had moved past the commits
    that carried them. They are the sensitive prefixes -- the paths the design
    most wants a human to see -- so the approval gate had a one-shot notice
    behind it and no queue."""
    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED

    pending = apply_inbound.read_pending(state_dir)
    assert "scripts/guard.sh" in pending, "a sensitive withheld path was not carried forward"
    assert "tests/out.txt" in pending, "an out-of-surface path was not carried forward"
    assert pending["scripts/guard.sh"]["status"] == "needs-human-approval"
    assert pending["scripts/guard.sh"]["runs_owed"] == 1

    # And the marker still advanced -- the debt is what carries the unfinished
    # business, not the marker.
    assert _git(engine["repo"], "rev-parse", "refs/synced/code-plane").strip() == engine["plane_tip"]


def test_debt_re_enters_the_next_change_set_with_nothing_new_upstream(engine, state_dir):
    """Second run, no new commits at all: the carried paths must still be
    classified and re-offered. Without the carry they would be invisible,
    because `marker..tip` is now empty."""
    rec = Recorder()
    _run(engine, state_dir, rec)
    first_pending = set(apply_inbound.read_pending(state_dir))
    assert first_pending

    rec2 = Recorder()
    result2 = _run(engine, state_dir, rec2)
    # Nothing new to write, but the debt survives and was re-classified.
    assert result2["result"] == apply_inbound.RESULT_NOTHING, result2
    still = apply_inbound.read_pending(state_dir)
    assert set(still) == first_pending, "the debt changed with nothing upstream to change it"
    assert still["scripts/guard.sh"]["runs_owed"] == 2, "the debt is not counting how long it has been owed"


def test_debt_clears_when_a_human_puts_the_content_on_the_engine(engine, state_dir):
    """The debt has to be able to empty, or it is just a growing log. A human
    applying the sensitive change by hand is what resolves it."""
    repo = engine["repo"]
    rec = Recorder()
    _run(engine, state_dir, rec)
    assert "scripts/guard.sh" in apply_inbound.read_pending(state_dir)

    # A human applies it on the engine, byte-for-byte.
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts/guard.sh").write_text("#!/bin/sh\necho guarded\n")
    _git(repo, "add", "scripts/guard.sh")
    _git(repo, "commit", "-q", "-m", "apply the guard by hand")

    rec2 = Recorder()
    result2 = _run(engine, state_dir, rec2)
    pending = apply_inbound.read_pending(state_dir)
    assert "scripts/guard.sh" not in pending, "the debt did not clear after the content reached the engine"
    assert "scripts/guard.sh" in (result2.get("resolved") or []), result2.get("resolved")


def test_carried_conflict_does_not_refuse_the_whole_run(engine, state_dir):
    """The landmine. A withheld would-overwrite path, once the marker has
    moved past it, becomes a `conflict` the moment the plane changes it again
    -- and a conflict refuses the ENTIRE run, including unrelated writable
    work, three times over, after which the channel disables itself.

    A carried path was never going to be applied by this run; its conflict
    means it is still owed, not that everything must stop."""
    repo = engine["repo"]
    rec = Recorder()
    _run(engine, state_dir, rec)
    assert "backend/diverged.py" in apply_inbound.read_pending(state_dir)

    # The plane changes the same path again, and brings unrelated new work.
    _git(repo, "checkout", "-q", "plane")
    _commit(repo, "touch diverged again and add unrelated work (#5)",
            {"backend/diverged.py": "PLANE's third version\n", "backend/unrelated.py": "unrelated\n"})
    _git(repo, "checkout", "-q", "main")

    rec2 = Recorder()
    result2 = _run(engine, state_dir, rec2)

    assert result2["result"] == apply_inbound.RESULT_APPLIED, (
        f"a carried conflict refused the whole run: {result2}"
    )
    assert "backend/unrelated.py" in result2["written"], "unrelated writable work was blocked by a carried conflict"
    assert "backend/diverged.py" in apply_inbound.read_pending(state_dir), "the conflicted path stopped being owed"
    assert apply_inbound.read_failure_count(state_dir) == 0, "a carried conflict counted toward the circuit breaker"


def test_a_conflict_in_new_commits_still_refuses_everything(engine, state_dir):
    """The scoping above must not have weakened the real conflict stop."""
    repo = engine["repo"]
    (repo / "backend/shared.py").write_text("ENGINE diverged too\n")
    _git(repo, "add", "backend/shared.py")
    _git(repo, "commit", "-q", "-m", "engine also touches shared")

    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_CONFLICT, result
    assert rec.prs == []


def test_counter_only_write_preserves_the_debt(engine, state_dir):
    """A path that touches only the counter must not truncate the debt --
    that is how a persisted-debt design quietly reverts to the lossy one."""
    rec = Recorder()
    _run(engine, state_dir, rec)
    before = apply_inbound.read_pending(state_dir)
    assert before

    apply_inbound.write_failure_count(state_dir, 2)
    assert apply_inbound.read_pending(state_dir) == before
    assert apply_inbound.read_failure_count(state_dir) == 2


# ---------------------------------------------------------------------------
# The PR body cannot be used to hide entries from the human checkpoint
# ---------------------------------------------------------------------------


def test_crafted_path_renders_inert_and_hides_nothing():
    """Reachable by an untrusted stranger, because quarantined paths are
    listed in the body too. A backtick closes the code span and `<!--` opens a
    comment GitHub never terminates, taking every later entry with it."""
    hostile = "backend/evil`<!--.py"
    rendered = apply_inbound.render_path(hostile)

    assert "`" not in rendered[1:-1], f"a backtick survived into the code span: {rendered}"
    assert "<" not in rendered, f"an HTML opener survived: {rendered}"
    assert "[U+0060]" in rendered and "[U+003C]" in rendered, rendered
    # Visible, not silently dropped -- two different paths must not render the same.
    assert apply_inbound.render_path("backend/evil.py") != rendered


def test_hostile_path_does_not_hide_the_withheld_entries_after_it(engine, state_dir):
    """End to end: the entry a human is supposed to act on must still be in
    the body, and the Verification block must survive."""
    hostile = "backend/a`<!--hidden.py"
    classifications = {
        hostile: {
            "status": "quarantined:untrusted-provenance",
            "reason": "untrusted",
            "engine_path": hostile,
            "commits": ["c1"],
        },
        "scripts/zzz-real-approval-needed.sh": {
            "status": "needs-human-approval",
            "reason": "sensitive",
            "engine_path": "scripts/zzz-real-approval-needed.sh",
            "commits": ["c1"],
        },
    }
    _write, withheld = apply_inbound.partition_write_set(classifications, set(), ["scripts/"])
    body = apply_inbound.build_pr_body(
        report={"commits": [], "commit_count": 1},
        write_set={},
        withheld=withheld,
        marker="refs/synced/code-plane",
        marker_sha="a" * 40,
        tip_sha="b" * 40,
    )
    assert "zzz-real-approval-needed" in body, "a hostile path hid the entry after it"
    assert "### Verification" in body, "a hostile path swallowed the Verification block"
    assert "<!--" not in body, "an unterminated HTML comment reached the body"


def test_unrenderable_path_is_refused_from_the_write_set_and_named():
    classifications = {
        "backend/ok`.py": {
            "status": pull.STATUS_CLEAN_APPLY,
            "engine_path": "backend/ok`.py",
            "local_hash": "a",
            "upstream_hash": "b",
            "commits": ["c1"],
        }
    }
    write_set, withheld = apply_inbound.partition_write_set(classifications, set(), [])
    assert write_set == {}
    assert withheld["backend/ok`.py"]["status"] == apply_inbound.WITHHELD_UNRENDERABLE
    assert "U+0060" in withheld["backend/ok`.py"]["reason"]


# ---------------------------------------------------------------------------
# File modes: allowlist, and make the refusal visible
# ---------------------------------------------------------------------------


def test_symlink_mode_is_refused_and_named_not_written_as_a_create(engine, state_dir):
    """A symlink's payload is its TARGET, which no path gate looks at, and
    pull.validate_path's symlink defence tests the ENGINE filesystem -- False
    for a path that does not exist yet. It was written as 120000 and reported
    as an ordinary `(create)`."""
    repo = engine["repo"]
    _git(repo, "checkout", "-q", "plane")
    (repo / "backend").mkdir(exist_ok=True)
    link = repo / "backend/sneaky.py"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to("/etc/passwd")
    _git(repo, "add", "backend/sneaky.py")
    _git(repo, "commit", "-q", "-m", "add a helper (#6)")
    _git(repo, "checkout", "-q", "main")

    mode = _git(repo, "ls-tree", "plane", "--", "backend/sneaky.py").split()[0]
    assert mode == "120000", f"fixture did not produce a symlink entry: {mode}"

    rec = Recorder()
    result = _run(engine, state_dir, rec)
    assert result["result"] == apply_inbound.RESULT_APPLIED, result

    assert "backend/sneaky.py" not in result["written"], "a symlink was written into the engine"
    assert result["withheld"]["backend/sneaky.py"]["status"] == apply_inbound.WITHHELD_BAD_MODE
    assert "120000" in result["withheld"]["backend/sneaky.py"]["reason"]

    # Absent from the branch entirely.
    proc = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"{result['commit']}:backend/sneaky.py"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert proc.returncode != 0

    # And named to the human, rather than silently skipped.
    assert "backend/sneaky.py" in rec.prs[0]["body"]
    assert apply_inbound.WITHHELD_BAD_MODE in rec.prs[0]["body"]


def test_written_list_shows_the_mode(engine, state_dir):
    """`create` said the same thing for a regular file, a symlink and a
    submodule. The reviewer has to be able to see which."""
    rec = Recorder()
    _run(engine, state_dir, rec)
    body = rec.prs[0]["body"]
    assert "| mode |" in body
    assert "`100644`" in body


def test_blob_mode_refuses_a_gitlink_by_name():
    with pytest.raises(apply_inbound.ApplyRefused, match="160000"):
        raise apply_inbound.ApplyRefused(
            "refusing mode 160000 for 'x': only ['100644', '100755'] are written by this channel"
        )


# ---------------------------------------------------------------------------
# Skips that should be refusals
# ---------------------------------------------------------------------------


def test_orphan_write_set_path_raises_rather_than_being_dropped():
    """A silent `continue` here drops the path from the branch while the PR
    body still lists it under Written. A create would be caught by the
    blob-count invariant; an update would not."""
    write_set = {"a.py": {"engine_path": "a.py", "commits": ["nope"], "upstream_hash": "x", "local_hash": "y"}}
    with pytest.raises(apply_inbound.ApplyRefused, match="commit order"):
        apply_inbound.assign_paths_to_commits(write_set, ["c1", "c2"])


def test_writable_entry_with_null_engine_path_raises():
    """Skipping the protected/sensitive re-check for it would send `None` on
    to the cacheinfo format string and write a file named "None"."""
    classifications = {
        "x.py": {
            "status": pull.STATUS_CLEAN_APPLY,
            "engine_path": None,
            "local_hash": "a",
            "upstream_hash": "b",
            "commits": ["c1"],
        }
    }
    with pytest.raises(apply_inbound.ApplyRefused, match="null engine_path"):
        apply_inbound.partition_write_set(classifications, set(), [])


# ---------------------------------------------------------------------------
# The outward-push guard keys on the URL, not the remote's name
# ---------------------------------------------------------------------------


def test_push_guard_refuses_a_differently_named_remote_with_the_code_plane_url(engine):
    """The name is the one part of a remote that carries no authority. A
    second remote pointing at the same URL walked straight past a name check."""
    repo = engine["repo"]
    _git(repo, "remote", "add", "code-plane", "https://github.com/example/code-plane.git")
    _git(repo, "remote", "add", "innocent", "https://x-access-token@github.com/example/code-plane")

    with pytest.raises(apply_inbound.ApplyRefused, match="resolves to the code plane"):
        apply_inbound._push_branch(repo_dir=repo, remote="innocent", commit_sha="deadbeef", branch="b")


def test_push_guard_still_allows_a_genuinely_different_remote(engine):
    repo = engine["repo"]
    _git(repo, "remote", "add", "code-plane", "https://github.com/example/code-plane.git")
    _git(repo, "remote", "add", "origin2", "https://github.com/example/engine.git")
    # Reaches the real push (which fails, because the URL is not a repo) --
    # the point is that the GUARD did not refuse it.
    with pytest.raises(apply_inbound.ApplyRefused) as exc:
        apply_inbound._push_branch(repo_dir=repo, remote="origin2", commit_sha="deadbeef", branch="b")
    assert "resolves to the code plane" not in str(exc.value)


def test_normalise_remote_url_equates_spellings():
    n = apply_inbound._normalise_remote_url
    assert n("https://x-access-token@github.com/a/b.git") == n("https://github.com/a/b")
    assert n("https://GitHub.com/a/b/") == n("https://github.com/a/b")
    assert n("https://github.com/a/b") != n("https://github.com/a/c")
