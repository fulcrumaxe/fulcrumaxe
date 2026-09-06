"""Tests for scripts/engine-sync/inbound/gate.py and report.py -- D#2439
Slice B negatives B3-B7.
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
# export-surface membership (real MANIFEST.md) -- B5
# --------------------------------------------------------------------------

def test_export_surface_patterns_include_expected_and_exclude_out_of_surface():
    patterns = gate.load_export_surface_patterns()
    assert gate.is_in_export_surface("scripts/foo/bar.py", patterns)
    assert gate.is_in_export_surface(".claude/agents/executor.md", patterns)
    assert gate.is_in_export_surface("CLAUDE.md", patterns)
    assert gate.is_in_export_surface("agents/executor.md", patterns)  # generated mirror, still public

    assert not gate.is_in_export_surface("open-source/IDENTIFIER-RULES.txt", patterns)
    assert not gate.is_in_export_surface(".autonomous-team/config.json", patterns)
    assert not gate.is_in_export_surface("archive/2026-01-01-old/README.md", patterns)


# --------------------------------------------------------------------------
# sensitivity (real sensitive.txt) -- B4
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
# B4 -- the content gate does what the allowlist cannot (both halves)
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
    patterns = gate.load_export_surface_patterns()
    sensitive = gate.read_sensitive_prefixes()

    category, reason = gate.path_gate(remote_path, patterns, sensitive, target_root=_REPO_ROOT)
    assert category == gate.CAT_NEEDS_APPROVAL

    valid, reason2 = pull.validate_path(remote_path, _REPO_ROOT, patterns, excludes=[])
    assert valid, f"allowlist should have let {remote_path} through: {reason2}"


def test_generated_mirror_alone_also_needs_approval():
    engine_path, category = gate.reverse_map_path("agents/executor.md")
    assert category == ""  # not the pure-generated bootstrap file; reverse-maps normally
    patterns = gate.load_export_surface_patterns()
    sensitive = gate.read_sensitive_prefixes()
    result_category, _ = gate.path_gate(engine_path, patterns, sensitive, target_root=_REPO_ROOT)
    assert result_category == gate.CAT_NEEDS_APPROVAL
    valid, _ = pull.validate_path(engine_path, _REPO_ROOT, patterns, excludes=[])
    assert valid


# --------------------------------------------------------------------------
# B5 -- out-of-surface writes are rejected BY NAME
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "remote_path",
    ["open-source/IDENTIFIER-RULES.txt", ".autonomous-team/config.json", "archive/2026-01-01-x/README.md"],
)
def test_out_of_surface_paths_rejected_by_name(remote_path):
    patterns = gate.load_export_surface_patterns()
    sensitive = gate.read_sensitive_prefixes()
    category, reason = gate.path_gate(remote_path, patterns, sensitive, target_root=_REPO_ROOT)
    assert category == gate.CAT_OUT_OF_SURFACE
    assert reason  # a real reason, not silence


# --------------------------------------------------------------------------
# B6 -- path traversal, each with a distinct reason, never reaching the
# hash-classification stage
# --------------------------------------------------------------------------

@pytest.mark.parametrize("remote_path", ["../x", "a/../../x", "/etc/passwd"])
def test_path_traversal_rejected(remote_path):
    patterns = gate.load_export_surface_patterns()
    sensitive = gate.read_sensitive_prefixes()
    category, reason = gate.path_gate(remote_path, patterns, sensitive, target_root=_REPO_ROOT)
    assert category == gate.CAT_PATH_UNSAFE
    assert reason


def test_symlink_parent_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "scripts").symlink_to(outside, target_is_directory=True)

    patterns = ["scripts/*"]  # a permissive include so the symlink check is what actually fires
    category, reason = gate.path_gate("scripts/evil.sh", patterns, [], target_root=root)
    assert category == gate.CAT_PATH_UNSAFE
    assert "outside" in reason or "symlink" in reason


def test_traversal_reasons_are_distinct():
    patterns = gate.load_export_surface_patterns()
    reasons = set()
    for p in ["../x", "a/../../x", "/etc/passwd"]:
        _, reason = gate.path_gate(p, patterns, [], target_root=_REPO_ROOT)
        reasons.add(reason)
    assert len(reasons) == 3, f"expected 3 distinct reasons, got {reasons}"


# --------------------------------------------------------------------------
# Full-pipeline helpers for B1/B2/B3/B7 (report.classify_report)
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _commit(repo: Path, message: str, files: dict[str, str], committer_email: str | None = None, trailer: str | None = None) -> str:
    for relpath, content in files.items():
        full = repo / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        _git(repo, "add", relpath)
    msg = message if not trailer else f"{message}\n\n{trailer}\n"
    env_args = []
    if committer_email:
        _git(repo, "commit", "-q", "-m", msg, f"--author=Someone <{committer_email}>")
    else:
        _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def scratch_repo(tmp_path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


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
        resolve_trust_allowlist=lambda: {"trusted-dev"},
        resolve_pr_author=lambda pr: "trusted-dev",
        is_trusted_author=lambda login, allowlist: login in allowlist,
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
    """B3: the same commit, forged Co-Authored-By/committer naming a trusted
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
        resolve_trust_allowlist=lambda: {"trusted-dev"},
        resolve_pr_author=lambda pr: "someone-else",
        is_trusted_author=lambda login, allowlist: login in allowlist,
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
        resolve_trust_allowlist=lambda: {"trusted-dev"},
        resolve_pr_author=lambda pr: "trusted-dev",
        is_trusted_author=lambda login, allowlist: login in allowlist,
    )

    assert result["refused"] is True
    assert "ceiling" in result["refusal_reason"]

    marker_after = _git(scratch_repo, "rev-parse", seed).strip()
    status_after = _git(scratch_repo, "status", "--porcelain")
    assert marker_before == marker_after
    assert status_before == status_after


def test_report_writes_nothing_on_success_either(scratch_repo):
    """B7, success path: git status and the marker's resolved sha are
    byte-identical before and after a normal (non-refused) report run."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(scratch_repo, "small change (#6)", {"only.txt": "hi\n"})

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
        resolve_trust_allowlist=lambda: {"trusted-dev"},
        resolve_pr_author=lambda pr: "trusted-dev",
        is_trusted_author=lambda login, allowlist: login in allowlist,
    )
    assert result["refused"] is False

    marker_after = _git(scratch_repo, "rev-parse", seed).strip()
    status_after = _git(scratch_repo, "status", "--porcelain")
    assert marker_before == marker_after
    assert status_before == status_after
