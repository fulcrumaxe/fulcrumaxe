"""Tests for scripts/engine-sync/inbound/gate.py and report.py.

Most of these exercise gate.py's matching LOGIC against a small synthetic
surface-pattern list defined below, not the real open-source/MANIFEST.md --
that file is engine-only (open-source/ is deliberately never exported), so
a checkout of the code plane this PR merges onto has no such file on disk.
A test suite that can only pass in one of the two checkouts it ships to is
exactly the failure mode this module's own tree-diff-trap guard exists to
catch one level up: a check that is green for a reason the reader doesn't
expect. Exactly one test below (test_load_export_surface_patterns_parses_
real_manifest) reads the real file, and it is skipped -- with a stated,
presence-checked reason, not a blanket one -- when that file is absent.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_INBOUND_DIR = _THIS_DIR.parent / "inbound"
_ENGINE_SYNC_DIR = _THIS_DIR.parent
_REPO_ROOT = _ENGINE_SYNC_DIR.parent.parent
sys.path.insert(0, str(_INBOUND_DIR))
sys.path.insert(0, str(_ENGINE_SYNC_DIR))

import gate  # noqa: E402
import pull  # noqa: E402
import report  # noqa: E402

# A small stand-in for open-source/MANIFEST.md's real PATHS_START +
# GENERATED_PATHS_START patterns, covering exactly the shapes these tests
# need: a plain directory prefix, a nested one, a glob-suffixed one, a bare
# filename, and the two generated mirrors. Keeping this local means these
# tests assert gate.py's matching behaviour, not MANIFEST.md's current
# contents -- the one test that needs the real file is separate, below.
_SAMPLE_SURFACE_PATTERNS = [
    "scripts/*",
    "backend/*",
    "hooks/*",
    ".claude/agents/*.md",
    ".claude/commands/*.md",
    "CLAUDE.md",
    "agents/*",
    "commands/*",
]


# --------------------------------------------------------------------------
# reverse_map_path
# --------------------------------------------------------------------------

def test_reverse_map_mirror_paths():
    assert gate.reverse_map_path("agents/executor.md") == (".claude/agents/executor.md", "")
    assert gate.reverse_map_path("commands/coldstart.md") == (".claude/commands/coldstart.md", "")
    assert gate.reverse_map_path("scripts/foo.py") == ("scripts/foo.py", "")


def test_reverse_map_pure_generated_file_is_reported_not_dropped():
    engine_path, category = gate.reverse_map_path("loop-bootstrap/bootstrap-paths.generated")
    assert engine_path is None
    assert category == gate.CAT_GENERATED


# --------------------------------------------------------------------------
# export-surface membership, against the synthetic pattern list
# --------------------------------------------------------------------------

def test_surface_patterns_include_expected_and_exclude_out_of_surface():
    assert gate.is_in_export_surface("scripts/foo/bar.py", _SAMPLE_SURFACE_PATTERNS)
    assert gate.is_in_export_surface(".claude/agents/executor.md", _SAMPLE_SURFACE_PATTERNS)
    assert gate.is_in_export_surface("CLAUDE.md", _SAMPLE_SURFACE_PATTERNS)
    assert gate.is_in_export_surface("agents/executor.md", _SAMPLE_SURFACE_PATTERNS)  # generated mirror, still public

    assert not gate.is_in_export_surface("open-source/IDENTIFIER-RULES.txt", _SAMPLE_SURFACE_PATTERNS)
    assert not gate.is_in_export_surface(".autonomous-team/config.json", _SAMPLE_SURFACE_PATTERNS)
    assert not gate.is_in_export_surface("archive/2026-01-01-old/README.md", _SAMPLE_SURFACE_PATTERNS)


def _manifest_available(path: Path) -> bool:
    return Path(path).is_file()


_REAL_MANIFEST_SKIP_REASON = (
    "open-source/MANIFEST.md absent on this checkout -- open-source/ is engine-only "
    "and deliberately never exported. This test reads the real manifest to prove "
    "load_export_surface_patterns parses it correctly; every other test in this "
    "module exercises the same matching logic against the synthetic pattern list "
    "above and does not need this file."
)


@pytest.mark.skipif(not _manifest_available(gate.MANIFEST_MD_PATH), reason=_REAL_MANIFEST_SKIP_REASON)
def test_load_export_surface_patterns_parses_real_manifest():
    patterns = gate.load_export_surface_patterns()
    assert gate.is_in_export_surface("scripts/foo/bar.py", patterns)
    assert gate.is_in_export_surface(".claude/agents/executor.md", patterns)
    assert gate.is_in_export_surface("CLAUDE.md", patterns)
    assert not gate.is_in_export_surface("open-source/IDENTIFIER-RULES.txt", patterns)
    assert not gate.is_in_export_surface(".autonomous-team/config.json", patterns)


def test_manifest_skip_predicate_tracks_file_presence_not_a_constant(tmp_path):
    """The skip above must be gated on whether the file actually exists,
    not on a hardcoded True/False -- proven by calling the same predicate
    against two real, different filesystem states, not by reading its
    source."""
    missing = tmp_path / "does-not-exist" / "MANIFEST.md"
    assert _manifest_available(missing) is False

    present = tmp_path / "MANIFEST.md"
    present.write_text("<!-- PATHS_START -->\nscripts/\n<!-- PATHS_END -->\n")
    assert _manifest_available(present) is True


# --------------------------------------------------------------------------
# sensitivity (real sensitive.txt -- this file ships on every plane, so no
# skip is needed here)
# --------------------------------------------------------------------------

def test_sensitive_prefixes_cover_named_examples():
    prefixes = gate.read_sensitive_prefixes()
    assert gate.is_sensitive(".claude/agents/executor.md", prefixes)
    assert gate.is_sensitive("hooks/sandbox.py", prefixes)
    assert gate.is_sensitive("scripts/spawn-agent.sh", prefixes)
    assert gate.is_sensitive("CLAUDE.md", prefixes)
    assert not gate.is_sensitive("backend/api.py", prefixes)
    assert not gate.is_sensitive("dashboard/src/App.tsx", prefixes)


# --------------------------------------------------------------------------
# the content gate does what the allowlist cannot (both halves in one test)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "remote_path",
    [
        ".claude/agents/executor.md",
        "hooks/sandbox.py",
        "scripts/spawn-agent.sh",
        "CLAUDE.md",
    ],
)
def test_sensitive_paths_need_approval_but_are_valid_by_allowlist(remote_path):
    sensitive = gate.read_sensitive_prefixes()

    category, reason = gate.path_gate(remote_path, remote_path, _SAMPLE_SURFACE_PATTERNS, sensitive, target_root=_REPO_ROOT)
    assert category == gate.CAT_NEEDS_APPROVAL

    valid, reason2 = pull.validate_path(remote_path, _REPO_ROOT, _SAMPLE_SURFACE_PATTERNS, excludes=[])
    assert valid, f"allowlist should have let {remote_path} through: {reason2}"


def test_generated_mirror_alone_also_needs_approval():
    engine_path, category = gate.reverse_map_path("agents/executor.md")
    assert category == ""  # not the pure-generated bootstrap file; reverse-maps normally
    sensitive = gate.read_sensitive_prefixes()
    result_category, _ = gate.path_gate(
        "agents/executor.md", engine_path, _SAMPLE_SURFACE_PATTERNS, sensitive, target_root=_REPO_ROOT
    )
    assert result_category == gate.CAT_NEEDS_APPROVAL
    valid, _ = pull.validate_path(engine_path, _REPO_ROOT, _SAMPLE_SURFACE_PATTERNS, excludes=[])
    assert valid


def test_sensitivity_checks_the_raw_remote_path_too_not_only_the_mapped_one():
    """Belt-and-suspenders: even if the reverse-mapped engine path does NOT
    itself look sensitive, a sensitive RAW remote path must still trigger
    needs-human-approval. Constructed adversarially (an engine_path that
    doesn't overlap any real mirror target) so the two checks are provably
    independent rather than happening to agree because .claude/ already
    covers every real mirror destination.

    Verified against the pre-fix version of path_gate (single-argument,
    engine-path-only) before adding the second check here: with only the
    mapped-path check, this exact case returns "" (cleared, no approval
    needed) because "backend/foo.py" matches no sensitive prefix at all --
    the raw "agents/..." form was never consulted. That was the gap; the
    assertion below is what closes it.
    """
    sensitive = gate.read_sensitive_prefixes()
    category, reason = gate.path_gate(
        "agents/should-not-matter.md",  # raw remote path: sensitive via the "agents/" prefix
        "backend/foo.py",  # mapped engine path: in-surface (backend/*) but NOT sensitive on its own
        _SAMPLE_SURFACE_PATTERNS,
        sensitive,
        target_root=_REPO_ROOT,
    )
    assert category == gate.CAT_NEEDS_APPROVAL
    assert "raw remote path" in reason


# --------------------------------------------------------------------------
# out-of-surface writes are rejected BY NAME
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "remote_path",
    ["open-source/IDENTIFIER-RULES.txt", ".autonomous-team/config.json", "archive/2026-01-01-x/README.md"],
)
def test_out_of_surface_paths_rejected_by_name(remote_path):
    sensitive = gate.read_sensitive_prefixes()
    category, reason = gate.path_gate(remote_path, remote_path, _SAMPLE_SURFACE_PATTERNS, sensitive, target_root=_REPO_ROOT)
    assert category == gate.CAT_OUT_OF_SURFACE
    assert reason  # a real reason, not silence


# --------------------------------------------------------------------------
# path traversal, each with a distinct reason, never reaching the
# hash-classification stage
# --------------------------------------------------------------------------

@pytest.mark.parametrize("remote_path", ["../x", "a/../../x", "/etc/passwd"])
def test_path_traversal_rejected(remote_path):
    category, reason = gate.path_gate(remote_path, remote_path, _SAMPLE_SURFACE_PATTERNS, [], target_root=_REPO_ROOT)
    assert category == gate.CAT_PATH_UNSAFE
    assert reason


def test_symlink_parent_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "scripts").symlink_to(outside, target_is_directory=True)

    patterns = ["scripts/*"]  # a permissive include so the symlink check is what actually fires
    category, reason = gate.path_gate("scripts/evil.sh", "scripts/evil.sh", patterns, [], target_root=root)
    assert category == gate.CAT_PATH_UNSAFE
    assert "outside" in reason or "symlink" in reason


def test_traversal_reasons_are_distinct():
    reasons = set()
    for p in ["../x", "a/../../x", "/etc/passwd"]:
        _, reason = gate.path_gate(p, p, _SAMPLE_SURFACE_PATTERNS, [], target_root=_REPO_ROOT)
        reasons.add(reason)
    assert len(reasons) == 3, f"expected 3 distinct reasons, got {reasons}"


# --------------------------------------------------------------------------
# Full-pipeline tests (report.classify_report), with every real-file
# dependency (surface patterns, sensitive prefixes) injected as a stub --
# these never touch open-source/MANIFEST.md or scripts/engine-sync/inbound/
# sensitive.txt, only the synthetic lists defined in this file.
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _commit(repo: Path, message: str, files: dict[str, str | None], committer_email: str | None = None, trailer: str | None = None) -> str:
    """*files* maps relpath -> content; a value of None deletes that path
    instead of writing it."""
    for relpath, content in files.items():
        full = repo / relpath
        if content is None:
            full.unlink()
            _git(repo, "rm", "-q", relpath)
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            _git(repo, "add", relpath)
    msg = message if not trailer else f"{message}\n\n{trailer}\n"
    if committer_email:
        _git(repo, "commit", "-q", "-m", msg, f"--author=Someone <{committer_email}>")
    else:
        _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def scratch_repo(tmp_path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    # Pin the initial branch name explicitly -- git's own default
    # (init.defaultBranch, unset here) is "master" on this host, and several
    # tests below deliberately check out a second branch and need "main" to
    # mean something specific regardless of any git config.
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


_STUB_KWARGS = dict(
    resolve_trust_allowlist=lambda: {"trusted-dev"},
    resolve_pr_author=lambda pr: "trusted-dev",
    is_trusted_author=lambda login, allowlist: login in allowlist,
    resolve_surface_patterns=lambda: _SAMPLE_SURFACE_PATTERNS,
    resolve_sensitive_prefixes=lambda: ["hooks/", "scripts/", ".claude/", "CLAUDE.md", "agents/", "commands/"],
)


def test_report_classifies_clean_apply_and_already_applied(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(scratch_repo, "trusted change (#10)", {"backend/newfile.txt": "hello\n"})

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        **_STUB_KWARGS,
    )

    assert result["refused"] is False
    assert result["commit_count"] == 1
    assert "backend/newfile.txt" in result["classifications"]
    # No baseline entry existed for backend/newfile.txt and local == None !=
    # upstream -> local-patch ("no recorded baseline; adopt-in-place"), which
    # is the correct conservative classification for a brand-new path, per
    # pull.classify_against_baseline's own contract.
    assert result["classifications"]["backend/newfile.txt"]["status"] in (
        pull.STATUS_LOCAL_PATCH,
        pull.STATUS_ALREADY_APPLIED,
    )


def test_report_quarantines_untrusted_provenance_regardless_of_commit_metadata(scratch_repo):
    """The same commit, forged Co-Authored-By/committer naming a trusted
    login, must STILL quarantine -- because provenance here is resolved by
    an injected PR-author lookup that never reads the commit at all."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(
        scratch_repo,
        "sneaky change (#77)",
        {"payload.txt": "danger\n"},
        committer_email="trusted-dev@example.com",
        trailer="Co-Authored-By: Trusted Dev <trusted-dev@example.com>",
    )

    # resolve_pr_author is a stub that always resolves PR #77's GitHub-
    # authenticated author as "someone-else" -- deliberately NOT reading
    # anything from the commit above, which is the point.
    stub_kwargs = {**_STUB_KWARGS, "resolve_pr_author": lambda pr: "someone-else"}
    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        **stub_kwargs,
    )

    assert result["classifications"]["payload.txt"]["status"] == gate.CAT_QUARANTINED
    assert "payload.txt" in result["buckets"][gate.CAT_QUARANTINED]


def test_report_refuses_above_ceiling_and_writes_nothing(scratch_repo):
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(scratch_repo, "big change (#5)", {f"f{i}.txt": "x\n" for i in range(11)})

    marker_before = _git(scratch_repo, "rev-parse", seed).strip()
    status_before = _git(scratch_repo, "status", "--porcelain")

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=1,
        max_lines=500,
        do_fetch=False,
        **_STUB_KWARGS,
    )

    assert result["refused"] is True
    assert "ceiling" in result["refusal_reason"]

    marker_after = _git(scratch_repo, "rev-parse", seed).strip()
    status_after = _git(scratch_repo, "status", "--porcelain")
    assert marker_before == marker_after
    assert status_before == status_after


def test_report_writes_nothing_on_success_either(scratch_repo):
    """Success path: git status and the marker's resolved sha are
    byte-identical before and after a normal (non-refused) report run."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(scratch_repo, "small change (#6)", {"backend/only.txt": "hi\n"})

    marker_before = _git(scratch_repo, "rev-parse", seed).strip()
    status_before = _git(scratch_repo, "status", "--porcelain")

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        **_STUB_KWARGS,
    )
    assert result["refused"] is False

    marker_after = _git(scratch_repo, "rev-parse", seed).strip()
    status_after = _git(scratch_repo, "status", "--porcelain")
    assert marker_before == marker_after
    assert status_before == status_after


def test_report_refuses_deletion_of_out_of_surface_engine_path(scratch_repo):
    """The deletion-refusal check: a code-plane commit that deletes a path
    the engine actually has, which resolves outside the synthetic export
    surface, must refuse the whole report rather than classify around it --
    this is the real tree-diff-trap shape (a path that should never have
    been deletable in the first place), made reachable through a single
    real per-commit delete instead of a two-tree comparison.

    The deletion commit lives on a SEPARATE branch (simulating "the code
    plane's tip"), never on `main` itself -- `main` is what `local_ref`
    reads as "the engine's own copy", and it must still have the file for
    this to be a meaningful refusal (deleting a file neither side has would
    not exercise the check at all)."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n", "archive/old/keepsake.txt": "precious\n"})
    _git(scratch_repo, "checkout", "-q", "-b", "code-plane-main")
    tip = _commit(scratch_repo, "deletes an out-of-surface engine file (#20)", {"archive/old/keepsake.txt": None})

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        local_ref="main",
        **_STUB_KWARGS,
    )

    assert result["refused"] is True
    assert "outside the" in result["refusal_reason"]
    assert "archive/old/keepsake.txt" in result["refusal_reason"]


def test_report_allows_deletion_of_in_surface_engine_path(scratch_repo):
    """The mirror of the test above: deleting a path that IS in the export
    surface is a real, reportable event, not a refusal -- the ceiling exists
    to catch the tree-diff trap, not to forbid every delete a real commit
    might legitimately make. Same branch layout as above: the delete lands
    on a separate branch, `main` still has the file."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n", "backend/old.py": "x\n"})
    _git(scratch_repo, "checkout", "-q", "-b", "code-plane-main")
    tip = _commit(scratch_repo, "deletes an in-surface file (#21)", {"backend/old.py": None})

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        local_ref="main",
        **_STUB_KWARGS,
    )

    assert result["refused"] is False
    assert result["classifications"]["backend/old.py"]["status"] == pull.STATUS_REJECTED
    assert "backend/old.py" in result["buckets"][pull.STATUS_REJECTED]


def test_report_reads_local_copy_from_local_ref_not_head(scratch_repo):
    """local_ref defaults to "main" precisely so running this tool from a
    feature branch does not silently change what "the engine's own copy"
    means. Constructed so HEAD and main disagree: the code plane's tip
    lives on its own branch (main is untouched by it, exactly as it would
    be against a real remote), and a THIRD branch checked out as HEAD
    diverges locally from main. The report's classification must follow
    main, not whatever HEAD happens to be at call time."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n", "backend/shared.py": "same\n"})

    _git(scratch_repo, "checkout", "-q", "-b", "code-plane-main")
    tip = _commit(scratch_repo, "upstream changes shared.py (#30)", {"backend/shared.py": "upstream-version\n"})

    # Back to main's own tip, then diverge a feature branch from there --
    # main itself is never touched again, so it stays at the seed content.
    _git(scratch_repo, "checkout", "-q", "main")
    _git(scratch_repo, "checkout", "-q", "-b", "feature-branch")
    (scratch_repo / "backend" / "shared.py").write_text("feature-branch-local-edit\n")
    _git(scratch_repo, "add", "backend/shared.py")
    _git(scratch_repo, "commit", "-q", "-m", "local edit on the feature branch, not on main")

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        local_ref="main",
        **_STUB_KWARGS,
    )

    # main still has the seed content -> local == base != upstream -> clean-apply.
    # If this read HEAD (the feature branch) instead, it would see the
    # feature-branch edit and report local-patch/conflict instead.
    assert result["classifications"]["backend/shared.py"]["status"] == pull.STATUS_CLEAN_APPLY


def test_report_refuses_when_local_ref_does_not_resolve(scratch_repo):
    """An unresolvable --local-ref must refuse cleanly, not silently
    misclassify. Before this check existed, blob_hash_at(local_ref, ...)
    returned None for every path against a nonexistent ref -- indistinguishable
    from every engine path genuinely being absent -- so every genuine
    clean-apply path came back conflict instead, with exit 0 and a
    confident-looking report. Confirmed that failure mode directly against
    report.py on the command line before adding the check in classify_report."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n", "backend/shared.py": "same\n"})
    _git(scratch_repo, "checkout", "-q", "-b", "code-plane-main")
    tip = _commit(scratch_repo, "upstream change (#40)", {"backend/shared.py": "upstream-version\n"})

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        local_ref="refs/heads/definitely-not-a-real-branch",
        **_STUB_KWARGS,
    )

    assert result["refused"] is True
    assert "definitely-not-a-real-branch" in result["refusal_reason"]
    # It must refuse BEFORE producing any classification -- not a report that
    # happens to also set refused=True alongside a wrong buckets dict.
    assert "classifications" not in result
    assert "buckets" not in result


def test_report_with_a_real_local_ref_still_classifies_normally(scratch_repo):
    """The mirror check: a --local-ref that DOES resolve must behave exactly
    as before -- this guards against the resolution check itself becoming
    the thing that breaks the normal path."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n", "backend/shared.py": "same\n"})
    _git(scratch_repo, "checkout", "-q", "-b", "code-plane-main")
    tip = _commit(scratch_repo, "upstream change (#41)", {"backend/shared.py": "upstream-version\n"})

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        local_ref="main",
        **_STUB_KWARGS,
    )

    assert result["refused"] is False
    assert result["classifications"]["backend/shared.py"]["status"] == pull.STATUS_CLEAN_APPLY
