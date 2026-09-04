#!/usr/bin/env python3
"""Boundary-guard tests for D#1622 Batch C3 (engine-sync provenance coordination).

Proves -- using the REAL engine-sync matcher (imported from manifest.py, not a
hand-copied regex) -- that every path the coldstart interview writes
customization to is provably outside engine-sync's clean-apply write set:

  - scripts/coldstart-interview/generate.py's config.json (dials + C1's
    active_roles) and CLAUDE.project.md
  - scripts/coldstart-interview/seed-backlog.py's replay file
  - any .autonomous-team/ coldstart artifact

The rule proven for each path is: matched by allowlist.txt's [exclude] block,
OR not matched by the [include] block. Either way engine-sync's manifest
generator (manifest.py::collect_files) never places it in the candidate set,
so pull.py::classify_against_baseline() never gets a chance to clean-apply
over it.

If a future PR moves a coldstart output into a new [include] path, this test
fails loudly (it imports the live matcher, so it tracks the real rules).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent  # scripts/engine-sync/tests
ENGINE_SYNC_DIR = HERE.parent  # scripts/engine-sync
REPO_ROOT = ENGINE_SYNC_DIR.parent.parent
COLDSTART_DIR = REPO_ROOT / "scripts" / "coldstart-interview"

# Import the REAL manifest.py module (not a hand-copied regex) -- same
# importlib pattern test_manifest.py already uses in this directory.
MANIFEST_MODULE_PATH = ENGINE_SYNC_DIR / "manifest.py"
_spec = importlib.util.spec_from_file_location("engine_sync_manifest_boundary", MANIFEST_MODULE_PATH)
manifest_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(manifest_mod)

# Import generate.py + seed-backlog.py to exercise their real output paths
# rather than assuming them.
sys.path.insert(0, str(COLDSTART_DIR))
sys.path.insert(0, str(REPO_ROOT))
import generate  # noqa: E402

_seed_spec = importlib.util.spec_from_file_location(
    "coldstart_seed_backlog_boundary", COLDSTART_DIR / "seed-backlog.py"
)
seed_backlog = importlib.util.module_from_spec(_seed_spec)
assert _seed_spec.loader is not None
# Register before exec: seed-backlog.py uses @dataclass, which looks its
# defining module up via sys.modules[cls.__module__] during class creation.
sys.modules[_seed_spec.name] = seed_backlog
_seed_spec.loader.exec_module(seed_backlog)

FIXTURE_HEADLESS_NODEPLOY = COLDSTART_DIR / "tests" / "fixtures" / "answers-headless-nodeploy.json"

# Every path a coldstarted project's customization can land on, expressed as
# repo-root-relative strings the way the real allowlist matcher expects them
# (fnmatch against the POSIX relative path). These are the two plausible
# on-disk placements config.json/CLAUDE.project.md are documented to use
# (repo root, or nested under a project subdirectory) -- both must be safe,
# not just one.
COLDSTART_CUSTOMIZATION_PATHS = [
    "config.json",  # generate.py's config.json written at a project's repo root
    ".autonomous-team/config.json",  # this repo's own convention (dials + active_roles)
    "CLAUDE.project.md",  # generate.py's project overlay
]


@pytest.fixture(scope="module")
def allowlist_patterns():
    return manifest_mod.read_allowlist()


def test_config_json_paths_are_boundary_safe(allowlist_patterns):
    """AC1/AC3: every plausible on-disk location for the generated config.json
    (which carries both `dials` and C1's `active_roles`) is either excluded
    or simply never included -- proven via the real is_included/is_excluded."""
    includes, excludes = allowlist_patterns
    for relpath in ("config.json", ".autonomous-team/config.json"):
        excluded = manifest_mod.is_excluded(relpath, excludes)
        included = manifest_mod.is_included(relpath, includes)
        assert excluded or not included, (
            f"{relpath!r} is boundary-unsafe: included={included} excluded={excluded} "
            "-- a coldstart customization must never be reachable by clean-apply"
        )


def test_claude_project_md_is_boundary_safe(allowlist_patterns):
    """AC1: CLAUDE.project.md (the generated overlay) is not in [include],
    so engine-sync never manages it."""
    includes, _excludes = allowlist_patterns
    assert not manifest_mod.is_included("CLAUDE.project.md", includes), (
        "CLAUDE.project.md must not be an engine-sync [include] path"
    )


def test_canonical_claude_md_is_not_included(allowlist_patterns):
    """AC4: canonical CLAUDE.md is confirmed NOT in [include]. generate.py's
    footer only *suggests* a manual pointer-line edit -- it does not write to
    canonical CLAUDE.md itself, so no [include] path is touched by the
    deferred pointer-line wiring (verified not in scope for this batch, see
    generate.py::build_claude_overlay's own comment)."""
    includes, _excludes = allowlist_patterns
    assert not manifest_mod.is_included("CLAUDE.md", includes)

    # Cross-check generate.py never writes a file named CLAUDE.md.
    src = (COLDSTART_DIR / "generate.py").read_text()
    assert "does not do so automatically" in src, (
        "expected generate.py to still document the pointer-line wiring as deferred; "
        "if this line changed, re-check whether canonical CLAUDE.md is now written"
    )


def test_seed_backlog_replay_path_is_boundary_safe(allowlist_patterns):
    """AC1: the C2 seed-backlog.py replay file (degraded-failure path) lives
    under .autonomous-team/<state-dir-default>, which is excluded."""
    includes, excludes = allowlist_patterns
    replay_path = seed_backlog.replay_file_path(session="boundary-guard-test", base_dir=None)
    relpath = replay_path.as_posix()
    if relpath.startswith("/"):
        # AUTONOMOUS_TEAM_STATE_DIR (if set in this environment) may resolve
        # to an absolute path entirely outside the repo tree -- structurally
        # safe by construction, since engine-sync only ever walks REPO_ROOT.
        assert not relpath.startswith(str(REPO_ROOT)) or ".autonomous-team" in relpath
        return
    excluded = manifest_mod.is_excluded(relpath, excludes)
    included = manifest_mod.is_included(relpath, includes)
    assert excluded or not included, (
        f"seed-backlog replay path {relpath!r} is boundary-unsafe: "
        f"included={included} excluded={excluded}"
    )
    assert relpath.startswith(".autonomous-team/"), (
        "expected the default replay path to live under .autonomous-team/ "
        f"(deny-wins exclude pattern); got {relpath!r}"
    )


def test_active_roles_lives_in_excluded_config_json(allowlist_patterns, tmp_path):
    """AC3: prove -- against a REAL generator run, not an assumption -- that
    C1's active_roles field is written into config.json (the same excluded
    file dials already live in), not a separate/new file."""
    includes, excludes = allowlist_patterns
    out_dir = tmp_path / "out"
    generate.generate(FIXTURE_HEADLESS_NODEPLOY, out_dir)

    written = sorted(p.name for p in out_dir.iterdir())
    assert written == ["CLAUDE.project.md", "config.json"], (
        "generator must only ever write these two files"
    )

    config = json.loads((out_dir / "config.json").read_text())
    assert "active_roles" in config and config["active_roles"], (
        "expected active_roles to be populated in config.json by this fixture"
    )

    # The file active_roles lives in is named exactly "config.json" -- prove
    # that name is boundary-safe via the real matcher regardless of which of
    # the two documented placements (repo root or .autonomous-team/) a given
    # project uses.
    assert manifest_mod.is_excluded("config.json", excludes)
    assert manifest_mod.is_excluded(".autonomous-team/config.json", excludes)


def test_live_manifest_never_contains_this_repos_own_config(allowlist_patterns):
    """AC2: live manifest check against the REAL repo tree. This repo's own
    .autonomous-team/config.json genuinely exists on disk -- confirm
    manifest.py::collect_files() (the actual candidate-set builder engine-sync
    uses) never places it in the candidate set."""
    includes, excludes = allowlist_patterns
    own_config = REPO_ROOT / ".autonomous-team" / "config.json"
    if not own_config.is_file():
        pytest.skip(".autonomous-team/config.json not present in this checkout")

    candidates = manifest_mod.collect_files(REPO_ROOT, includes, excludes)
    assert ".autonomous-team/config.json" not in candidates, (
        "engine-sync's live candidate set must never contain this repo's own "
        "config.json -- deny-wins exclude failed to hold"
    )
    # Nothing under .autonomous-team/ at all should ever surface.
    leaked = [p for p in candidates if p.startswith(".autonomous-team/")]
    assert not leaked, f".autonomous-team/* paths leaked into the manifest candidate set: {leaked}"


def test_no_protected_txt_extension_needed(allowlist_patterns):
    """AC6: protected.txt should NOT need to grow for any coldstart
    customization path, because every one of them is already boundary-safe
    (excluded, or simply never included) rather than relying on
    classify_against_baseline()'s local-patch/conflict fallback to skip it.
    If this assertion ever fails for some path, that path has moved into a
    reachable [include] pattern and protected.txt genuinely needs the new
    entry (see wiki/Coldstart-Engine-Sync-Boundary.md)."""
    includes, excludes = allowlist_patterns
    for relpath in COLDSTART_CUSTOMIZATION_PATHS:
        excluded = manifest_mod.is_excluded(relpath, excludes)
        included = manifest_mod.is_included(relpath, includes)
        assert excluded or not included, (
            f"{relpath!r} is boundary-unsafe (included={included}, excluded={excluded}) -- "
            "add it to scripts/engine-sync/protected.txt and document why in "
            "wiki/Coldstart-Engine-Sync-Boundary.md"
        )
