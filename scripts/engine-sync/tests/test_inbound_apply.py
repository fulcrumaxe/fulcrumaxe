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


_CLASSIFY_KEYS = {"max_files", "max_lines", "local_ref", "marker"}


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
