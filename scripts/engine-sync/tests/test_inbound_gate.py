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

import changeset  # noqa: E402
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


def test_find_reverse_map_collisions_detects_many_to_one():
    """agents/executor.md and .claude/agents/executor.md both reverse-map to
    engine .claude/agents/executor.md -- a real collision this repo's
    current two mirrors can produce today (unlike the raw-remote-path
    sensitivity arm, which needs a FUTURE mirror to be reachable)."""
    collisions = gate.find_reverse_map_collisions(
        ["agents/executor.md", ".claude/agents/executor.md", "scripts/unrelated.py"]
    )
    assert collisions == {".claude/agents/executor.md": [".claude/agents/executor.md", "agents/executor.md"]}


def test_find_reverse_map_collisions_excludes_generated_only_paths():
    """The pure-generated bootstrap file has no engine_path at all, so it
    can never collide with anything and must never appear as a phantom
    collision key (None)."""
    collisions = gate.find_reverse_map_collisions(
        ["loop-bootstrap/bootstrap-paths.generated", "scripts/a.py", "scripts/b.py"]
    )
    assert collisions == {}


def test_find_reverse_map_collisions_empty_when_no_overlap():
    assert gate.find_reverse_map_collisions(["scripts/a.py", "backend/b.py"]) == {}


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

    # Executable/config surfaces inside the export surface that were
    # missing until a review found them reproducing as clean-apply on a
    # trusted-author edit.
    assert gate.is_sensitive(".github/workflows/ci.yml", prefixes)
    assert gate.is_sensitive("requirements.txt", prefixes)
    assert gate.is_sensitive(".claude-plugin/plugin.json", prefixes)
    # The trap: a "scripts/" prefix does NOT cover a nested directory of the
    # same name under a different top-level path -- loop-bootstrap/ needs
    # its own entry, which is exactly what a prefix-only fix would miss.
    assert gate.is_sensitive("loop-bootstrap/scripts/generate.sh", prefixes)
    assert gate.is_sensitive("loop-bootstrap/bootstrap.sh", prefixes)
    # backend/spawn_templates/ -- the 24 role .tmpl files plus the 9 shared
    # fragments/ they include are role definitions exactly like
    # .claude/agents/*.md, just rendered under backend/ instead. "backend/"
    # itself stays reviewed-non-sensitive (see _REVIEWED_NON_SENSITIVE_PATTERNS
    # below) -- only this one subtree needed its own entry.
    assert gate.is_sensitive("backend/spawn_templates/executor.tmpl", prefixes)
    assert gate.is_sensitive("backend/spawn_templates/fragments/two-gate-protocol.md", prefixes)
    assert not gate.is_sensitive("backend/spawn_templates.md", prefixes)  # prefix, not substring


# The full real export surface, and which of it is a reviewed decision.
# Kept here (not production code) because it is test-only completeness
# bookkeeping, not a runtime gate -- production only ever needs
# sensitive.txt's prefixes, checked via is_sensitive.
_REVIEWED_NON_SENSITIVE_PATTERNS = frozenset(
    {
        "dashboard/",
        "ts-backend/",
        "tui/",
        "backend/",
        "LICENSE",
        "NOTICE",
        "README.md",
        "CONTRIBUTING.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
    }
)


def _representative_path(raw_pattern: str) -> str:
    """A concrete relpath standing in for a raw (pre-glob-conversion)
    MANIFEST.md pattern, so it can be tested against is_sensitive/
    is_in_export_surface the same way a real touched path would be."""
    if raw_pattern.endswith("/"):
        return raw_pattern + "example.txt"
    if "*" in raw_pattern:
        return raw_pattern.replace("*", "example")
    return raw_pattern  # an exact filename, used as-is


@pytest.mark.skipif(not _manifest_available(gate.MANIFEST_MD_PATH), reason=_REAL_MANIFEST_SKIP_REASON)
def test_every_export_surface_entry_has_a_recorded_sensitivity_decision():
    """Structural completeness check, not four hand-picked examples: every
    raw PATHS_START/GENERATED_PATHS_START entry in the REAL manifest must
    be covered by EITHER sensitive.txt (a reviewed sensitive surface) OR
    _REVIEWED_NON_SENSITIVE_PATTERNS above (a surface someone has actually
    looked at and decided doesn't need human approval). An entry in neither
    is a silent gap -- exactly how .github/workflows/ci.yml,
    requirements.txt, loop-bootstrap/ and .claude-plugin/ were missed the
    first time: nobody's check ever looked at the FULL list, only at
    whatever examples someone happened to think of. The next time the
    export surface grows, this is what stops the new entry from going
    unreviewed instead of another manual audit."""
    text = gate.MANIFEST_MD_PATH.read_text()
    raw_patterns = gate._parse_marker_block(text, "PATHS") + gate._parse_marker_block(text, "GENERATED_PATHS")
    sensitive_prefixes = gate.read_sensitive_prefixes()

    unrecorded = []
    for raw in raw_patterns:
        if raw in _REVIEWED_NON_SENSITIVE_PATTERNS:
            continue
        sample = _representative_path(raw)
        if gate.is_sensitive(sample, sensitive_prefixes):
            continue
        unrecorded.append(raw)

    assert not unrecorded, (
        f"export-surface entries with no recorded sensitivity decision: {unrecorded} -- "
        "add each to sensitive.txt (if it's an executable/instruction-bearing surface) or to "
        "_REVIEWED_NON_SENSITIVE_PATTERNS above (if someone has looked and decided it's fine)"
    )


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
        "backend/spawn_templates/executor.tmpl",
        "backend/spawn_templates/fragments/two-gate-protocol.md",
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


def _resolve_prs_matching_subject_hint(sha: str, repo_dir: Path) -> list[int]:
    """Test-only stand-in for the real commits/pulls API: returns [N] where
    N is the commit's own subject `(#N)` hint (or [999] if there is none).
    This deliberately makes the stub AGREE with whatever
    changeset.extract_pr_number parses from the subject, so tests that
    are not specifically exercising the provenance mechanism itself don't
    each need a hand-wired resolver matching whatever PR number they
    happened to put in a commit message. Tests that DO exercise the
    mechanism (the subject-forgery case, the two-distinct-authors case)
    override resolve_prs_for_commit explicitly instead of using this."""
    subject = changeset.commit_subject(sha, repo_dir=repo_dir)
    hint = changeset.extract_pr_number(subject)
    return [hint] if hint is not None else [999]


def _stub_kwargs(repo_dir: Path) -> dict:
    return dict(
        resolve_trust_allowlist=lambda: {"trusted-dev"},
        resolve_pr_author=lambda pr: "trusted-dev",
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: _SAMPLE_SURFACE_PATTERNS,
        resolve_sensitive_prefixes=lambda: ["hooks/", "scripts/", ".claude/", "CLAUDE.md", "agents/", "commands/"],
        resolve_prs_for_commit=lambda sha: _resolve_prs_matching_subject_hint(sha, repo_dir),
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
        **_stub_kwargs(scratch_repo),
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


def test_report_refuses_reverse_map_collision(scratch_repo):
    """agents/executor.md (the generated mirror) and .claude/agents/executor.md
    (its real source) both reverse-map to the same engine path. If a single
    commit touches both with DIFFERENT content, classifying them
    independently would let whichever one a caller happens to write last
    silently win -- this must instead refuse both as a named collision."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(
        scratch_repo,
        "touches both the mirror and its source (#23)",
        {
            "agents/executor.md": "mirror version\n",
            ".claude/agents/executor.md": "source version, different content\n",
        },
    )

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
        **_stub_kwargs(scratch_repo),
    )

    assert result["refused"] is False  # a per-path finding, not a whole-report refusal
    assert result["classifications"]["agents/executor.md"]["status"] == gate.CAT_COLLISION
    assert result["classifications"][".claude/agents/executor.md"]["status"] == gate.CAT_COLLISION
    assert "agents/executor.md" in result["buckets"][gate.CAT_COLLISION]
    assert ".claude/agents/executor.md" in result["buckets"][gate.CAT_COLLISION]
    # Neither ever reaches hash classification once flagged as a collision.
    assert "agents/executor.md" not in result["buckets"][pull.STATUS_CLEAN_APPLY]
    assert ".claude/agents/executor.md" not in result["buckets"][pull.STATUS_CLEAN_APPLY]


def test_report_quarantines_untrusted_provenance_regardless_of_commit_metadata(scratch_repo):
    """The same commit, forged Co-Authored-By/committer naming a trusted
    login, must STILL quarantine -- because provenance is resolved entirely
    through resolve_prs_for_commit + resolve_pr_author, neither of which
    ever reads the commit's own content.

    Modelled with TWO DISTINCT PRs and TWO DISTINCT real authors (#4 ->
    trusted-dev, #77 -> attacker), not a constant resolve_pr_author -- a
    stub that returns the same author regardless of which PR is asked about
    would pass this test even if the code resolved the WRONG PR, which is
    exactly the false-green shape a subject-varying-only version of this
    test had before: it varied Co-Authored-By/committer email (fields
    nothing reads) while holding a constant-author stub, so it could never
    have caught a wrong PR selection either."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(
        scratch_repo,
        "sneaky change",  # no (#N) at all -- nothing to cross-check against
        {"payload.txt": "danger\n"},
        committer_email="trusted-dev@example.com",
        trailer="Co-Authored-By: Trusted Dev <trusted-dev@example.com>",
    )

    def resolve_prs_for_commit(_sha):
        # The real (stubbed) commits/pulls resolution for this commit,
        # deliberately independent of the commit's own content: it really
        # belongs to PR #77, an untrusted contributor's own PR.
        return [77]

    def resolve_pr_author(pr):
        return {4: "trusted-dev", 77: "attacker"}[pr]

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
        resolve_pr_author=resolve_pr_author,
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: _SAMPLE_SURFACE_PATTERNS,
        resolve_sensitive_prefixes=lambda: ["hooks/", "scripts/", ".claude/", "CLAUDE.md", "agents/", "commands/"],
        resolve_prs_for_commit=resolve_prs_for_commit,
    )

    assert result["classifications"]["payload.txt"]["status"] == gate.CAT_QUARANTINED
    assert "payload.txt" in result["buckets"][gate.CAT_QUARANTINED]


def test_report_refuses_subject_pr_number_that_disagrees_with_the_real_api(scratch_repo):
    """The exact live-repro shape: an untrusted contributor writes a commit
    subject that NAMES a real, trusted PR -- `Tidy up imports (#4)`, say,
    where PR #4 genuinely belongs to a trusted contributor -- but this
    commit itself actually belongs to a different PR entirely. Before the
    commits/pulls API became the sole commit->PR link, the subject's own
    number was trusted directly: this exact shape resolved `trusted=True`
    and reached clean-apply, with the whole provenance boundary defeated by
    typing a number into a commit message.

    Modelled with two distinct real PRs/authors so the mismatch is the
    thing being tested, not an author stub that would agree regardless."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip = _commit(scratch_repo, "Tidy up imports (#4)", {"payload.txt": "danger\n"})

    def resolve_prs_for_commit(_sha):
        # The real API resolution: this commit actually belongs to PR
        # #999 (the attacker's own), never the #4 its subject claims.
        return [999]

    def resolve_pr_author(pr):
        return {4: "trusted-dev", 999: "attacker"}[pr]

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
        resolve_pr_author=resolve_pr_author,
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: _SAMPLE_SURFACE_PATTERNS,
        resolve_sensitive_prefixes=lambda: ["hooks/", "scripts/", ".claude/", "CLAUDE.md", "agents/", "commands/"],
        resolve_prs_for_commit=resolve_prs_for_commit,
    )

    assert result["classifications"]["payload.txt"]["status"] == gate.CAT_QUARANTINED
    # The reason must name the disagreement itself, not merely "untrusted" --
    # proving the mismatch was what triggered the refusal, not a coincidence.
    assert "disagreement" in result["classifications"]["payload.txt"]["reason"]
    assert "payload.txt" not in result["buckets"][pull.STATUS_CLEAN_APPLY]


def test_report_refuses_zero_and_ambiguous_pr_resolutions(scratch_repo):
    """commits/pulls resolving to zero PRs, or to more than one, must both
    refuse rather than guess -- fail closed on an unreadable or ambiguous
    link exactly as on a mismatched one."""
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n"})
    tip_zero = _commit(scratch_repo, "orphan commit", {"zero.txt": "x\n"})

    result = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip_zero,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        resolve_trust_allowlist=lambda: {"trusted-dev"},
        resolve_pr_author=lambda pr: "trusted-dev",
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: _SAMPLE_SURFACE_PATTERNS,
        resolve_sensitive_prefixes=lambda: ["hooks/", "scripts/", ".claude/", "CLAUDE.md", "agents/", "commands/"],
        resolve_prs_for_commit=lambda _sha: [],
    )
    assert result["classifications"]["zero.txt"]["status"] == gate.CAT_QUARANTINED
    assert "no PR" in result["classifications"]["zero.txt"]["reason"]

    _git(scratch_repo, "checkout", "-q", "-b", "another-branch")
    tip_ambiguous = _commit(scratch_repo, "ambiguous commit", {"ambiguous.txt": "x\n"})

    result2 = report.classify_report(
        marker=seed,
        remote="unused",
        remote_branch="unused",
        remote_ref=tip_ambiguous,
        repo_dir=scratch_repo,
        code_repo_slug="irrelevant/irrelevant",
        max_files=50,
        max_lines=500,
        do_fetch=False,
        resolve_trust_allowlist=lambda: {"trusted-dev"},
        resolve_pr_author=lambda pr: "trusted-dev",
        is_trusted_author=lambda login, allowlist: login in allowlist,
        resolve_surface_patterns=lambda: _SAMPLE_SURFACE_PATTERNS,
        resolve_sensitive_prefixes=lambda: ["hooks/", "scripts/", ".claude/", "CLAUDE.md", "agents/", "commands/"],
        resolve_prs_for_commit=lambda _sha: [5, 6],
    )
    assert result2["classifications"]["ambiguous.txt"]["status"] == gate.CAT_QUARANTINED
    assert "ambiguous" in result2["classifications"]["ambiguous.txt"]["reason"]


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
        **_stub_kwargs(scratch_repo),
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
        **_stub_kwargs(scratch_repo),
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
        **_stub_kwargs(scratch_repo),
    )

    assert result["refused"] is True
    assert "outside the" in result["refusal_reason"]
    assert "archive/old/keepsake.txt" in result["refusal_reason"]


def test_report_refuses_delete_spelled_as_rename(scratch_repo):
    """A delete spelled as a rename must be caught by the same deletion
    refusal as a plain delete -- git's --name-status would otherwise report
    only an R### against the new path, with no "D" anywhere, and the
    refusal (which keys off "D" in a path's statuses) would never see it.
    changeset.commit_name_status's synthetic "D" for the renamed-away path
    is what closes this; this test exercises it through the real refusal,
    not just at the changeset level."""
    content = "line one\nline two\nline three\nline four\nline five\n"
    seed = _commit(scratch_repo, "seed", {"engine.txt": "v1\n", "archive/old/keepsake.txt": content})
    _git(scratch_repo, "checkout", "-q", "-b", "code-plane-main")
    _git(scratch_repo, "mv", "archive/old/keepsake.txt", "archive/old/renamed-keepsake.txt")
    _git(scratch_repo, "commit", "-q", "-m", "rename the keepsake away (#22)")
    tip = _git(scratch_repo, "rev-parse", "HEAD").strip()

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
        **_stub_kwargs(scratch_repo),
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
        **_stub_kwargs(scratch_repo),
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
        **_stub_kwargs(scratch_repo),
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
        **_stub_kwargs(scratch_repo),
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
        **_stub_kwargs(scratch_repo),
    )

    assert result["refused"] is False
    assert result["classifications"]["backend/shared.py"]["status"] == pull.STATUS_CLEAN_APPLY
