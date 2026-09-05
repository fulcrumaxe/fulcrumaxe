"""tests/test_no_planted_spawn_ids.py — repo-wide guard against a planted
canonical-shaped ``hook_event_id`` id landing in agent transcripts (D#1807).

Why this exists: scripts/spawn-agent.sh appends a "hook_event_id=<role>-<disc-
or-nod>-<unix_ts>" line to every spawn prompt so scripts/subagent-stop-hook.sh
(via scripts/lib/transcript_event_id.py) can
recover which agent_run row a transcript belongs to. If a repo source file
ALSO happens to carry that same tag prefix immediately followed by a
canonical-shaped id — as a doc-comment example, a test fixture, or a
``.find()`` argument — then any agent whose transcript includes that file's
contents (a code review, a "read this file" tool_result, a docstring quoted
back in a reply) adopts the planted id as if it were its own genuine spawn
tag. ``complete_run()`` upserts on agent_id, so the planted id lands in
``agent_run`` as a real-looking row against the wrong Discussion with a
fabricated timestamp.

Before PR #1802, the extractor accepted the FIRST match after the tag
regardless of shape, so a bare mention in backticks yielded a visibly-wrong
id (a lone backtick). #1802 added shape validation, which is correct and
necessary — but it also means a planted CANONICAL-shaped id is now
indistinguishable from a genuine one. #1802 replaced visible garbage with
well-formed garbage; this sweep is what actually removes the plant, and this
test is what keeps it removed. See D#1807.

This test intentionally does NOT import scripts/lib/transcript_event_id.py's
regex (see Implementation Notes on D#1807): the guard's job is to be
trivially readable and hard to accidentally disable, not perfectly in sync
with the extractor's shape. The two patterns can drift; that's an accepted
cost, not an oversight.

Scope: the sweep reads ``git ls-files``, not the working tree. An earlier
revision of this file walked the tree and argued that was the right default,
on the grounds that a planted id contaminates any agent that reads it the
moment it is on disk, tracked or not. That argument is still true, but it
was answering a different question than this guard asks.

What flipped it: widening the sweep to ``.jsonl`` (below) immediately matched
``.autonomous-team/hook-events/blocks-<date>.jsonl`` — the sandbox hook's own
block log, which records agent prompts verbatim and therefore records spawn
tags verbatim. That file is untracked, is not gitignored, rotates daily, and
regenerates. No source edit can clear the hit. Walking the working tree
therefore made this test green on a fresh clone and permanently red on an
operator checkout, and with CI disabled repo-wide (D#1937) the operator
checkout is the only place it ever runs. A test that is always red where it
actually runs is a false positive that crowds out real findings — the precise
failure mode D#1796 exists to eliminate — so it would have shipped the same
disease it was written to cure.

``git ls-files`` resolves that structurally rather than by listing paths to
skip: runtime state is untracked, so it is out of scope by construction, and
the result no longer depends on which machine runs it. It is also the honest
reading of what this guard is for — stopping a plant from entering *repo
source*. The index (not HEAD) is the boundary, so a plant is caught as soon
as it is staged, which is the moment it becomes source. If the hook log is
ever committed it becomes tracked, and the guard fires on it — correctly.

Live contamination of an untracked runtime log is a real problem, and it is
NOT solved here: it needs the tag redacted where the log is written. That is
filed separately. Conflating the two is what makes an exclusion list grow
forever.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The tag is assembled from two adjacent fragments so THIS file's own source
# never contains the tag prefix immediately followed by a canonical-looking
# id — otherwise every agent that reads this guard would be contaminated by
# the very thing it exists to catch.
TAG = "hook_event_" + "id="

# Canonical id shape: "<role>-<discussion-or-nod-or-None>-<unix_ts>", matching
# what scripts/spawn-agent.sh:437 emits ("${ROLE}-${DISCUSSION:-nod}-$(date
# +%s)") and the "None" fallback seen in some legacy call sites.
CANON = re.compile(
    re.escape(TAG) + r"([a-zA-Z][a-zA-Z0-9_-]*-(?:\d+|nod|None)-\d{9,11})"
)

# Extensions an agent transcript realistically ingests: source, scripts,
# docs, config, prompt templates. Widened past .py per D#1807 criterion 2 —
# .sh, .md, and .ts are exactly as readable by an agent as .py is.
#
# .jsonl earns its place for a stronger reason than the rest: it IS the
# transcript format, so a fixture modelling a spawn prompt is the single most
# natural place for someone to write a literal tag followed by a canonical
# id — and scripts/lib/transcript_event_id.py reads exactly these files.
# Leaving it out left every transcript fixture in the repo unguarded against
# the one contamination path the extractor actually walks.
_SCAN_EXTENSIONS = frozenset(
    {
        ".py", ".sh", ".md", ".ts", ".tsx", ".js", ".json", ".jsonl", ".yml",
        ".yaml", ".tmpl", ".txt",
    }
)

# archive/ is excluded: content there is frozen by the Archive Protocol
# (CLAUDE.md) and must never be edited to appease a sweep — that defeats the
# point of an archive. As of D#1807 (2026-07-30), archive/orphan-diffs/*.patch
# alone held 22 such adjacencies, all inside historical patch text; that
# count is not re-verified here because archive/ is out of scan scope by
# design, not because the number is still exactly 22 today.
#
# Also excluded: loop-bootstrap/ (bootstrap payload, not steady-state repo
# content), plus node_modules/, .claude/worktrees/, and .git/.
#
# Of those, only archive/ and loop-bootstrap/ are load-bearing — they are the
# ones git actually tracks (1657 and 34 files respectively at the time of
# writing). Since the sweep reads the index, the other three are already out
# of scope by construction: nothing under a node_modules/ directory is
# tracked (the single `git ls-files | grep node_modules` hit is a test named
# test_worktree_node_modules_symlink.sh, not a vendored file), .claude/
# worktrees/ is never tracked, and git cannot track anything inside .git/.
# They stay in the set as cheap insurance in case someone ever vendors a
# dependency, not because they fire today.
#
# These names are matched at ANY depth, not just at the top level — but that
# is insurance too, for all four names, not a rule any of them currently
# exercises. Measured against this repo's index (3561 files): archive/ prunes
# 1657 files and loop-bootstrap/ 34, every one of them at depth 0, and the
# count of tracked files with an excluded component at depth > 0 is zero
# (`git ls-files | grep '/archive/'` returns nothing). An earlier revision of
# this comment justified the any-depth match with a nested `backend/archive/`
# — that path does not exist and never did. The any-depth code is kept
# because it is correct and costs nothing, not because it prunes something a
# first-component check would miss today.
_EXCLUDED_DIR_NAMES = frozenset({"archive", "node_modules", "loop-bootstrap", ".git"})


def _is_excluded_dir(rel_parts: tuple[str, ...]) -> bool:
    """Return True if a file's parent directory (given as path parts relative
    to the scan root) puts it out of scope."""
    if not rel_parts:
        return False
    if any(part in _EXCLUDED_DIR_NAMES for part in rel_parts):
        return True
    if rel_parts[:2] == (".claude", "worktrees"):
        return True
    return False


def _tracked_files(root: Path) -> list[Path]:
    """Every path in *root*'s git index — committed files plus staged ones.

    Deliberately NOT `--others`: untracked runtime state is what this sweep
    must not report (see the module docstring). A failure to run git is
    raised rather than swallowed — silently falling back to a working-tree
    walk would quietly restore the behaviour this replaced, and a guard that
    degrades to "scan nothing" on error is worse than one that stops.

    An EMPTY answer is raised on for the same reason, and it is the sharper
    case: a non-zero exit is loud, but `git ls-files` reporting nothing at
    all exits 0, so without this check the sweep scans zero files and passes
    — green while guarding nothing. What produces an empty answer, measured
    rather than assumed: a checkout with nothing staged (a fresh `git init`),
    or `GIT_DIR` aimed at an *empty* repository. Not reachable in this repo
    today (no active git hooks, `core.hooksPath` unset), but silently
    scanning nothing is the one failure mode a guard must never have, and no
    legitimate checkout of this repo has an empty index.

    Note the narrowness. `GIT_DIR` pointed at a *non-empty* repository does
    NOT produce an empty answer and is not caught here — see the residual
    described on the raise below.
    """
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed in {root} (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()} — this sweep "
            "reads the git index and cannot run outside a git checkout"
        )
    tracked = [
        root / rel
        for rel in proc.stdout.decode("utf-8", errors="replace").split("\0")
        if rel
    ]
    # Residual, deliberately not closed here: if git answers from a
    # DIFFERENT non-empty repository (GIT_DIR aimed elsewhere), the answer is
    # not empty, so this check never fires. The returned paths are that
    # repo's, resolved under `root`, where they do not exist — `_scan_file`
    # takes the OSError branch and returns [] for each, and the sweep goes
    # green having scanned nothing real. Measured against a victim repo with
    # a staged plant: no GIT_DIR reports the plant, GIT_DIR at another
    # non-empty repo reports []. Closing that needs a wrong-repo identity
    # check, which is new mechanism and is tracked separately rather than
    # bolted on here.
    if not tracked:
        raise RuntimeError(
            f"git ls-files reported an empty index for {root} — refusing to "
            "report zero hits from zero files scanned. A checkout with "
            "nothing staged produces this; either way the sweep guarded "
            "nothing and must not pass."
        )
    return tracked


def _iter_scan_files(root: Path):
    for path in _tracked_files(root):
        if path.suffix not in _SCAN_EXTENSIONS:
            continue
        if _is_excluded_dir(path.relative_to(root).parts[:-1]):
            continue
        yield path


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(line_no, matched_text), ...] for every planted-id hit in *path*.

    Never raises: an unreadable or binary-ish file is treated as no hits
    rather than aborting the whole sweep over one bad file.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in CANON.finditer(line):
            hits.append((lineno, m.group(0)))
    return hits


def scan_tree(root: Path) -> list[tuple[str, int, str]]:
    """Read *root*'s git index, apply the extension filter and directory
    exclusions, and return every (relative_path, line_no, matched_text) hit
    found in the files that survive both."""
    all_hits: list[tuple[str, int, str]] = []
    for path in _iter_scan_files(root):
        for lineno, matched in _scan_file(path):
            all_hits.append((str(path.relative_to(root)), lineno, matched))
    return all_hits


def test_no_planted_canonical_ids_in_tracked_files():
    """The acceptance criterion (D#1807 #3-4): the sweep reports zero hits.
    "These files were edited" is not the criterion — the sweep reporting
    zero is.

    Renamed from ...._in_working_tree when the sweep moved to the git index;
    the old name would now describe scope the test does not have."""
    hits = scan_tree(_REPO_ROOT)
    assert hits == [], (
        "planted canonical-shaped hook_event_id found in a tracked file — "
        "any agent transcript that reads these files adopts the id as its "
        "own, contaminating agent_run telemetry:\n"
        + "\n".join(f"  {f}:{ln}: {m}" for f, ln, m in hits)
    )


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


def test_guard_fails_when_a_planted_id_is_present(tmp_path):
    """Negative control (D#1807 criterion 5): the guard must actually guard.

    A sweep that always passes — because the pattern is wrong, the walk
    never descends into real files, or the extension filter is too narrow —
    would be indistinguishable from a working one until the 83rd contaminated
    file lands. Plant a canonical-shaped id in a scratch file inside the
    sweep's own scope and confirm scan_tree finds it and names the exact
    file and line. tmp_path is a fresh directory pytest tears down after the
    test, so nothing needs manual cleanup.
    """
    _init_repo(tmp_path)
    _write_tracked(tmp_path, "scratch.py", f"# example: {TAG}executor-1807-1785301265\n")

    hits = scan_tree(tmp_path)

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
    _SCAN_EXTENSIONS leaves that one green."""
    _init_repo(tmp_path)
    _write_tracked(
        tmp_path,
        "fixtures/transcripts/some_run.jsonl",
        '{"type": "user", "message": {"content": '
        f'"{TAG}executor-1807-1785301265"}}}}\n',
    )

    assert scan_tree(tmp_path) == [
        (
            "fixtures/transcripts/some_run.jsonl",
            1,
            f"{TAG}executor-1807-1785301265",
        )
    ]


def test_guard_ignores_excluded_directories(tmp_path):
    """archive/, .claude/worktrees/, node_modules/, and loop-bootstrap/ are
    excluded by design (D#1807 criterion 7) — a planted id living only inside
    one of those must not fail the sweep.

    Every path here is deliberately git-added, so the exclusion list is what
    keeps them out, not the index. That is the stronger assertion: it still
    holds if someone vendors a dependency or commits an archive fixture.

    The nested entries are insurance, not a reproduction of anything in this
    repo: measured against the index, every tracked file the exclusion list
    prunes sits at depth 0, and no tracked path has an excluded component
    below that. The only node_modules/ on this checkout is dashboard/
    node_modules/, and it is untracked. These cases are pinned so the
    any-depth behaviour cannot regress unnoticed if that ever changes.
    `.git/` is not listed because git cannot track anything inside it — it is
    out of scope structurally."""
    _init_repo(tmp_path)
    for rel in (
        "archive/some-old-thing-2026-01-01/notes.py",
        ".claude/worktrees/some-agent/scratch.py",
        "node_modules/some-pkg/index.js",
        "dashboard/node_modules/some-pkg/index.js",
        "tui/node_modules/some-pkg/nested/deep/index.js",
        "backend/archive/old-fixtures-2026-01-01/run.jsonl",
        "loop-bootstrap/seed.py",
    ):
        _write_tracked(tmp_path, rel, f"{TAG}executor-1-1785301265\n")

    assert scan_tree(tmp_path) == []


def test_untracked_runtime_state_is_not_reported(tmp_path):
    """The reason this sweep reads the index instead of walking the tree.

    `.autonomous-team/hook-events/blocks-<date>.jsonl` is the sandbox hook's
    block log. It records agent prompts verbatim, so it records spawn tags
    verbatim; it is untracked, is NOT gitignored, rotates daily, and
    regenerates — no source edit can clear a hit on it. A working-tree walk
    reported it and stayed red forever on any operator checkout, which with
    CI disabled (D#1937) is the only environment this test runs in.

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

    hits = scan_tree(tmp_path)

    assert [f for f, _, _ in hits] == [
        "backend/tests/fixtures/transcripts/committed.jsonl"
    ], f"expected only the tracked plant, got: {hits}"


def test_empty_index_raises_instead_of_passing_vacuously(tmp_path):
    """A guard that scans zero files and reports zero hits is not a guard.

    `git ls-files` returning nothing exits 0, so the non-zero-exit check in
    _tracked_files does not catch it, and the sweep reports zero hits from
    zero files scanned.

    Reproduced before the check existed by running scan_tree against a
    freshly `git init`-ed repository with nothing staged: it returned [] and
    passed, guarding nothing. That is the condition asserted here, and it is
    reached without touching the environment.
    """
    _init_repo(tmp_path)

    try:
        scan_tree(tmp_path)
    except RuntimeError as exc:
        assert "empty index" in str(exc)
    else:
        raise AssertionError(
            "scan_tree returned instead of raising on an empty index — the "
            "sweep would pass while scanning zero files"
        )
