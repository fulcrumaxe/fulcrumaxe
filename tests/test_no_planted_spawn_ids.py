"""tests/test_no_planted_spawn_ids.py — negative controls for the planted
spawn-id guard (D#1807, D#1960).

The sweep itself moved to scripts/ci/no-planted-spawn-ids-guard.py (D#1957),
where scripts/ci/run-guards.sh discovers it and the `backend (import-smoke)` job
runs it on every CI run. Read that module for what the guard is for, which
surfaces it covers, and — in the decision table in its docstring — which surfaces
it deliberately does not.

What is left here is the part CI cannot express as an exit code: the guard's own
behaviour, checked against scratch repositories where a planted id is put there
on purpose. A guard that always passes — because the pattern is wrong, because
the walk never reaches real files, because the extension filter is too narrow —
looks exactly like a working one until the day it matters.

Nothing in this file restates a constant from the guard. Every pattern, the
extension set and the exclusion set are imported and compared against the
module's own values. A test that copies another module's behaviour as a literal
passes forever while the two drift apart; that shape produced five defects in one
afternoon on this repo (D#2451), and the assertions below are written as
comparisons against `guard.<NAME>` for that reason.

NOTE ON WHAT THIS GATES: CI runs no pytest today (D#2443), so this file gates
nothing on its own. The guard it exercises does gate — via run-guards.sh — and
these tests are what make its failure path something someone has actually
watched. Run them with `python3 -m pytest tests/test_no_planted_spawn_ids.py`.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GUARD_PATH = _REPO_ROOT / "scripts" / "ci" / "no-planted-spawn-ids-guard.py"


def _load_guard():
    """Import the guard by path — its filename is hyphenated, matching every
    other file in scripts/ci/, so it is not a legal module name."""
    spec = importlib.util.spec_from_file_location(
        "no_planted_spawn_ids_guard", _GUARD_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()

# Local alias only — the value comes from the guard, never from a literal here.
TAG = guard.TAG


def _init_repo(root: Path) -> None:
    """Make *root* a real git checkout.

    The tests below need a genuine index, not a stand-in: scan_tree reads
    `git ls-files`, so handing it a plain directory would exercise nothing.
    No commit is made and no user identity is configured — `git add` alone
    populates the index, which is exactly the boundary the sweep reads.
    """
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True,
                   capture_output=True)


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def _write_tracked(root: Path, rel: str, body: str) -> Path:
    p = _write(root, rel, body)
    subprocess.run(["git", "-C", str(root), "add", "--", rel], check=True,
                   capture_output=True)
    return p


def test_repo_is_clean_of_planted_ids():
    """The acceptance criterion (D#1807 #3-4): the sweep reports zero hits over
    this checkout. "These files were edited" is not the criterion — the sweep
    reporting zero is.

    This duplicates what CI runs, on purpose: someone running the test file
    locally should get the same answer the build gets, without having to know
    the runner exists."""
    hits = guard.scan_tree(_REPO_ROOT)
    assert hits == [], (
        "planted canonical-shaped spawn id found in a tracked file — any agent "
        "transcript that reads these files adopts the id as its own, "
        "contaminating agent_run telemetry:\n"
        + "\n".join(f"  {f}:{ln}: {m}" for f, ln, m in hits)
    )


def test_guard_fails_when_a_planted_id_is_present(tmp_path):
    """Negative control (D#1807 criterion 5): the guard must actually guard.

    Plant a canonical-shaped id in a scratch file inside the sweep's own scope
    and confirm scan_tree finds it and names the exact file and line. tmp_path
    is a fresh directory pytest tears down after the test, so nothing needs
    manual cleanup.
    """
    _init_repo(tmp_path)
    _write_tracked(tmp_path, "scratch.py", f"# example: {TAG}executor-1807-1785301265\n")

    hits = guard.scan_tree(tmp_path)

    assert len(hits) == 1, f"expected exactly one planted-id hit, got: {hits}"
    fname, lineno, matched = hits[0]
    assert fname == "scratch.py"
    assert lineno == 1
    assert matched == f"{TAG}executor-1807-1785301265"


def test_guard_catches_a_plant_in_a_transcript_fixture(tmp_path):
    """.jsonl is the transcript format itself, and every transcript fixture in
    the repo is a plausible place to write a literal spawn tag — so it is the
    one extension the sweep cannot afford to skip. Pinned separately from the
    generic negative control above because dropping ".jsonl" back out of
    SCAN_EXTENSIONS leaves that one green."""
    _init_repo(tmp_path)
    _write_tracked(
        tmp_path,
        "fixtures/transcripts/some_run.jsonl",
        '{"type": "user", "message": {"content": '
        f'"{TAG}executor-1807-1785301265"}}}}\n',
    )

    assert guard.scan_tree(tmp_path) == [
        (
            "fixtures/transcripts/some_run.jsonl",
            1,
            f"{TAG}executor-1807-1785301265",
        )
    ]


def test_guard_catches_a_plant_under_loop_bootstrap(tmp_path):
    """D#1960: loop-bootstrap/ is in scope, and this is the assertion that says
    so from the guard's side rather than from the exclusion set's.

    It is the highest-consequence directory in the tree for this particular
    defect — it is the seed payload copied into every newly provisioned repo, so
    a plant there is not confined to this repo the way a plant in backend/ is.
    It was excluded until D#1957 on the grounds that it was partly-derived
    working-tree content, which stopped being true when the sweep moved to
    `git ls-files`.

    Deliberately not written as "loop-bootstrap is absent from the exclusion
    set": that assertion would still pass if the extension filter or the walk
    stopped reaching the directory for some unrelated reason.
    """
    _init_repo(tmp_path)
    _write_tracked(
        tmp_path,
        "loop-bootstrap/prompts/seed.md",
        f"example spawn line: {TAG}executor-1960-1785301265\n",
    )

    assert guard.scan_tree(tmp_path) == [
        ("loop-bootstrap/prompts/seed.md", 1, f"{TAG}executor-1960-1785301265")
    ]


def test_guard_ignores_excluded_directories(tmp_path):
    """archive/, .claude/worktrees/ and node_modules/ are excluded by design
    (D#1807 criterion 7) — a planted id living only inside one of those must not
    fail the sweep.

    Every path here is deliberately git-added, so the exclusion list is what
    keeps them out, not the index. That is the stronger assertion: it still
    holds if someone vendors a dependency or commits an archive fixture.

    The nested entries are insurance, not a reproduction of anything in this
    repo: on this repo's main at d274c8b7 the count of tracked paths carrying an
    excluded component at any depth is zero. These cases are pinned so the
    any-depth behaviour cannot regress unnoticed if that changes. `.git/` is not
    listed because git cannot track anything inside it — it is out of scope
    structurally.
    """
    _init_repo(tmp_path)
    for rel in (
        "archive/some-old-thing-2026-01-01/notes.py",
        ".claude/worktrees/some-agent/scratch.py",
        "node_modules/some-pkg/index.js",
        "dashboard/node_modules/some-pkg/index.js",
        "tui/node_modules/some-pkg/nested/deep/index.js",
        "backend/archive/old-fixtures-2026-01-01/run.jsonl",
    ):
        _write_tracked(tmp_path, rel, f"{TAG}executor-1-1785301265\n")

    assert guard.scan_tree(tmp_path) == []


def test_untracked_runtime_state_is_not_reported(tmp_path):
    """The reason this sweep reads the index instead of walking the tree.

    `.autonomous-team/hook-events/blocks-<date>.jsonl` is the sandbox hook's
    block log. It records agent prompts verbatim, so it records spawn tags
    verbatim; it is untracked, is NOT gitignored, rotates daily, and
    regenerates — no source edit can clear a hit on it. A working-tree walk
    reported it and stayed red forever on any operator checkout.

    That log is a real leak and it is fixed at the writer, not here — see
    hooks/spawn_tag_redaction.py and tests/test_hook_block_log_redaction.py.

    Both directions are asserted together on purpose: the tracked plant must
    still be caught, so this cannot be satisfied by scanning nothing.
    """
    _init_repo(tmp_path)
    _write_tracked(
        tmp_path,
        "backend/tests/fixtures/transcripts/committed.jsonl",
        f"{TAG}executor-1807-1785301265\n",
    )
    _write(
        tmp_path,
        ".autonomous-team/hook-events/blocks-2026-08-18.jsonl",
        f'{{"kind": "block", "prompt": "{TAG}executor-1807-1785301265"}}\n',
    )

    hits = guard.scan_tree(tmp_path)

    assert [f for f, _, _ in hits] == [
        "backend/tests/fixtures/transcripts/committed.jsonl"
    ], f"expected only the tracked plant, got: {hits}"


def test_empty_index_raises_instead_of_passing_vacuously(tmp_path):
    """A guard that scans zero files and reports zero hits is not a guard.

    `git ls-files` returning nothing exits 0, so the non-zero-exit check in
    _tracked_files does not catch it, and the sweep would report zero hits from
    zero files scanned.

    Reproduced before the check existed by running scan_tree against a freshly
    `git init`-ed repository with nothing staged: it returned [] and passed,
    guarding nothing. That is the condition asserted here, and it is reached
    without touching the environment.
    """
    _init_repo(tmp_path)

    with pytest.raises(RuntimeError, match="empty index"):
        guard.scan_tree(tmp_path)


def test_loop_bootstrap_is_not_excluded():
    """D#1960 from the other side: the name must be gone from the set itself.

    Paired with test_guard_catches_a_plant_under_loop_bootstrap above, which
    checks the behaviour. Keeping both is what makes a silent re-exclusion fail
    loudly rather than turning one green test into one red one somewhere
    unrelated.
    """
    assert "loop-bootstrap" not in guard.EXCLUDED_DIR_NAMES
    assert guard.EXCLUDED_DIR_NAMES == frozenset(
        {"archive", "node_modules", ".git"}
    )


def test_scan_extensions_cover_the_formats_agents_read():
    """Pinned against the guard's own set, not a copy of it.

    The assertion is a subset check plus an equality check on the full value:
    the subset names the extensions that have a recorded reason to be there
    (D#1807 criterion 2 for the source formats, and .jsonl because it is the
    transcript format the extractor actually walks), and the equality is what
    makes a silent addition or removal show up here rather than nowhere.
    """
    must_cover = {".py", ".sh", ".md", ".ts", ".jsonl"}
    assert must_cover <= guard.SCAN_EXTENSIONS
    assert guard.SCAN_EXTENSIONS == frozenset(
        {
            ".py", ".sh", ".md", ".ts", ".tsx", ".js", ".json", ".jsonl",
            ".yml", ".yaml", ".tmpl", ".txt",
        }
    )


def test_guard_source_carries_no_contiguous_tag():
    """The guard and this file both discuss the tag at length, so both are prime
    candidates for planting the thing they exist to catch — the D#1957 filing
    found exactly that in PR #1947's own body. Both assemble the prefix from
    fragments; this asserts the result, over the real file contents."""
    for path in (_GUARD_PATH, Path(__file__)):
        assert guard.CANON.search(path.read_text()) is None, (
            f"{path.name} contains a contiguous canonical spawn id — the file "
            "would contaminate every agent that reads it"
        )
