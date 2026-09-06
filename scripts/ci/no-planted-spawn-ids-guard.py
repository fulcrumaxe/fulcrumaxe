#!/usr/bin/env python3
"""no-planted-spawn-ids-guard.py — a canonical-shaped spawn id must not appear
contiguously in tracked repo source (D#1807, D#1957, D#1959, D#1960).

Why this exists
---------------
scripts/spawn-agent.sh appends a "hook_event_id=<role>-<disc-or-nod>-<unix_ts>"
line to every spawn prompt so scripts/subagent-stop-hook.sh (via
scripts/lib/transcript_event_id.py) can recover which agent_run row a transcript
belongs to. If a repo source file ALSO carries that tag prefix immediately
followed by a canonical-shaped id — as a doc-comment example, a test fixture, or
a ``.find()`` argument — then any agent whose transcript includes that file's
contents (a code review, a "read this file" tool_result, a docstring quoted back
in a reply) adopts the planted id as if it were its own genuine spawn tag.
``complete_run()`` upserts on agent_id, so the planted id lands in ``agent_run``
as a real-looking row against the wrong Discussion with a fabricated timestamp.

Before PR #1802 the extractor accepted the FIRST match after the tag regardless
of shape, so a bare mention in backticks yielded a visibly-wrong id (a lone
backtick). #1802 added shape validation, which is correct and necessary — but it
also means a planted CANONICAL-shaped id is now indistinguishable from a genuine
one. #1802 replaced visible garbage with well-formed garbage; this sweep is what
removes the plant, and keeps it removed.

Which surfaces are guarded, and which deliberately are not (D#1957)
-------------------------------------------------------------------
Three Discussions described the same gap from three angles: this guard covers
*tracked repo source*, while the property actually worth protecting is *anything
an agent reads*. Three surfaces, three different answers, recorded here because
this is the file the next person opens before filing a fourth Discussion.

  loop-bootstrap/ (D#1960) — GUARDED.
      36 tracked files, 30 of them selected by the extension filter below
      (measured on this repo's main at d274c8b7). It was originally excluded as a
      partly-derived working-tree directory; that justification died when the
      sweep moved from a tree walk to ``git ls-files``, since the directory is
      ordinary index content. It also has the widest blast radius in the tree: it
      is the seed payload copied into every newly provisioned repo, so a plant
      there propagates outward rather than staying here. It is therefore no
      longer in EXCLUDED_DIR_NAMES.

  the sandbox hook's block log (D#1959) — MITIGATED AT THE WRITER, not here.
      .autonomous-team/hook-events/blocks-<date>.jsonl records agent prompts
      verbatim, and therefore records spawn tags verbatim. It is untracked, is
      not gitignored, rotates daily, and regenerates — no source edit can clear a
      hit on it, and this sweep reads the index precisely so it does not report
      it. The fix belongs where the log is written: hooks/spawn_tag_redaction.py
      scrubs the tag before hooks/sandbox.py serialises a telemetry line. Note
      that gitignoring the directory would have made this guard quiet while
      leaving every agent that reads the log just as contaminated; that is
      symptom suppression, and D#1959 ruled it out explicitly.

  PR bodies and Discussion bodies (D#1957) — ACCEPTED, on purpose.
      Agents read these constantly (``gh pr view``, ``gh api graphql``, briefs
      that quote them), and contiguous ids are as extractable there as in a .py
      file. Three reasons not to guard them, the third load-bearing:
        1. The blast radius is bounded mis-attribution — a wrong end_ts or token
           count landing on an EXISTING agent_run row — not row fabrication
           (D#1953). Every id in a PR body today got there by an agent honestly
           pasting real output, not by an attack.
        2. A write-path scrubber would edit agent-authored text, and CLAUDE.md
           scores over-blocking as the worse failure. A check that fires on a
           legitimate paste of real log output is a nuisance guard.
        3. The durable fix is consumer hardening: if
           scripts/lib/transcript_event_id.py cross-checked an extracted id
           against known spawn records before attributing it, no surface would
           need guarding. That is D#1784 Phase 3's work. Duplicating it here
           would produce two half-implementations of one cross-check.
      So: no write-path scrubber is added, deliberately. If you came here to add
      one, the open thread you want is D#1784 Phase 3 in
      scripts/lib/transcript_event_id.py — not a fourth surface guard.

Where this runs
---------------
scripts/ci/, so scripts/ci/run-guards.sh discovers it by directory listing and
the `backend (import-smoke)` job runs it. It lived at
tests/test_no_planted_spawn_ids.py until D#1957, where it gated nothing: CI runs
no pytest (D#2443), so it fired only when a human or agent remembered to run it.
That file remains, reduced to negative-control unit tests that import their
constants from this module rather than restating them.

Do NOT add a `run:` step for this file to a workflow. The runner discovers
scripts/ci/* by listing, and scripts/ci/guard-registry-check.py fails by name on
a file that is both discovered and separately referenced.

Scope: the index, not the working tree
--------------------------------------
The sweep reads ``git ls-files``. An earlier revision walked the tree, arguing a
plant contaminates any agent that reads it the moment it is on disk, tracked or
not. That is still true; it was answering a different question than this guard
asks.

What flipped it: widening to ``.jsonl`` immediately matched the hook block log
described above — untracked, unclearable by any source edit. A tree walk was
therefore green on a fresh clone and permanently red on an operator checkout, and
a check that is always red where it runs is a false positive crowding out real
findings. ``git ls-files`` resolves that structurally rather than by listing more
paths to skip: runtime state is untracked, so it is out of scope by construction,
and the result no longer depends on which machine runs it. The index (not HEAD)
is the boundary, so a plant is caught as soon as it is staged — the moment it
becomes source. If the hook log is ever committed it becomes tracked and this
guard fires on it, correctly.

This guard intentionally does NOT import scripts/lib/transcript_event_id.py's
regex (see Implementation Notes on D#1807): its job is to be trivially readable
and hard to accidentally disable, not perfectly in sync with the extractor. The
two patterns can drift; that is an accepted cost, not an oversight.

Exit 0: no planted ids. Exit 1: at least one, each named file:line.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

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
SCAN_EXTENSIONS = frozenset(
    {
        ".py", ".sh", ".md", ".ts", ".tsx", ".js", ".json", ".jsonl", ".yml",
        ".yaml", ".tmpl", ".txt",
    }
)

# Every name in this set is insurance. None of them prunes a single tracked
# file on this repo's main at d274c8b7: archive/, node_modules/ and .git/ each
# hold zero tracked files there, at any depth. They are listed anyway, for
# reasons that outlast today's count and are worth stating rather than
# re-deriving:
#
#   archive/      content there is frozen by the Archive Protocol (CLAUDE.md)
#                 and must never be edited to appease a sweep — that defeats
#                 the point of an archive. Files land there over time, so this
#                 entry is the one most likely to start mattering.
#   node_modules/ in case a dependency is ever vendored into the index.
#   .git/         git cannot track anything inside it; out of scope
#                 structurally, and listed only so the set reads completely.
#
# Deliberately no cross-repo figure here. An earlier revision of this comment
# carried a tracked-file count measured on a different tree, which read as a
# claim about this one; every number above names the ref it was measured on for
# that reason.
#
# loop-bootstrap/ used to be here and was removed by D#1960 — see the decision
# table in the module docstring. Do not put it back. If the sweep reports a hit
# under loop-bootstrap/, that hit is either a real plant or a legitimate example
# that gets an allowlist line of its own with a reason; re-excluding the
# directory wholesale is the outcome D#1960 exists to prevent.
#
# These names are matched at ANY depth, not just at the top level. On this
# repo's main at d274c8b7 the sweep reads 1863 tracked files and the extension
# filter above selects 1827 of them; the count of tracked paths carrying an
# excluded component at any depth is zero, so the any-depth match prunes
# nothing a first-component check would miss today. It is kept because it is
# correct and costs nothing.
EXCLUDED_DIR_NAMES = frozenset({"archive", "node_modules", ".git"})


def _is_excluded_dir(rel_parts: tuple[str, ...]) -> bool:
    """Return True if a file's parent directory (given as path parts relative
    to the scan root) puts it out of scope."""
    if not rel_parts:
        return False
    if any(part in EXCLUDED_DIR_NAMES for part in rel_parts):
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


def iter_scan_files(root: Path):
    for path in _tracked_files(root):
        if path.suffix not in SCAN_EXTENSIONS:
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
    for path in iter_scan_files(root):
        for lineno, matched in _scan_file(path):
            all_hits.append((str(path.relative_to(root)), lineno, matched))
    return all_hits


def main() -> int:
    try:
        hits = scan_tree(REPO_ROOT)
    except RuntimeError as exc:
        print(f"no-planted-spawn-ids: FAIL — {exc}", file=sys.stderr)
        return 1
    if hits:
        print(
            "no-planted-spawn-ids: FAIL — planted canonical-shaped spawn id in a "
            "tracked file. Any agent transcript that reads these files adopts the "
            "id as its own, contaminating agent_run telemetry. Break the adjacency "
            "between the tag and the id (a space or angle brackets is enough); do "
            "not delete the surrounding evidence.",
            file=sys.stderr,
        )
        for rel, lineno, matched in hits:
            print(f"  {rel}:{lineno}: {matched}", file=sys.stderr)
        return 1
    print("no-planted-spawn-ids: OK — no planted canonical spawn ids in tracked source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
