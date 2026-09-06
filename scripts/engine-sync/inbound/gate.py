#!/usr/bin/env python3
"""scripts/engine-sync/inbound/gate.py -- the read-only classify report's
gating half.

Four gates, applied in this order to every path the changeset touches:

  1. provenance   -- is the touching commit's PR author GitHub-authenticated
                      and in the trust set?
  2. path safety  -- does the path resolve inside the repo, with no
                      traversal, no symlink escape, no non-canonical form?
                      (reusing pull.canonicalize_relpath/validate_path)
  3. surface      -- is the path (after reverse-mapping the generated
                      mirrors) actually inside the export surface at all?
                      (also via pull.validate_path's include-pattern check)
  4. sensitivity  -- even though it IS inside the export surface, is it one
                      of the prefixes a human must approve regardless of
                      hash state?

Only a path that clears all four ever reaches hash-based classification
(pull.classify_against_baseline). Nothing here writes to the working tree;
nothing here calls `git diff <a> <b>` between branch tips.
"""
from __future__ import annotations

import sys
from pathlib import Path

_INBOUND_DIR = Path(__file__).resolve().parent
_ENGINE_SYNC_DIR = _INBOUND_DIR.parent
REPO_ROOT = _ENGINE_SYNC_DIR.parent.parent

for _p in (str(_ENGINE_SYNC_DIR), str(REPO_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pull  # noqa: E402  (canonicalize_relpath, validate_path -- reused, not reimplemented)

SENSITIVE_PATH = _INBOUND_DIR / "sensitive.txt"
MANIFEST_MD_PATH = REPO_ROOT / "open-source" / "MANIFEST.md"

# Categories a path can land in before it ever reaches hash classification.
CAT_GENERATED = "generated"
CAT_QUARANTINED = "quarantined:untrusted-provenance"
CAT_PATH_UNSAFE = "rejected:path-unsafe"
CAT_OUT_OF_SURFACE = "rejected:out-of-surface"
CAT_NEEDS_APPROVAL = "needs-human-approval"
CAT_COLLISION = "rejected:reverse-map-collision"

#: The two generated mirrors export.sh produces at the export root, plus the
#: one fully-synthetic file it bakes with no engine-side source at all.
#: Derived as a function of MANIFEST.md's own markers, not a guess: its
#: GENERATED_PATHS_START block (`agents/`, `commands/`) inverted against
#: its own PATHS_START entries (`.claude/agents/*.md`, `.claude/commands/*.md`),
#: plus the one export.sh-baked file named in BOOTSTRAP_PATHS_START's sibling
#: block. Hard-coded here rather than re-derived at import time because
#: there are exactly three shapes and MANIFEST.md's markers already assert
#: their own presence (see load_export_surface_patterns).
_GENERATED_ONLY_FILES = frozenset({"loop-bootstrap/bootstrap-paths.generated"})
_MIRROR_PREFIXES = (("agents/", ".claude/agents/"), ("commands/", ".claude/commands/"))


def reverse_map_path(remote_path: str) -> tuple[str | None, str]:
    """(engine_path, category). engine_path is None when the remote path is
    purely export-generated and has no engine-side source at all -- category
    is CAT_GENERATED in that case, and the caller must report it rather than
    drop it silently: a commit that touches only the generated mirror is a
    real, reportable event even though there is nowhere on the engine side
    for it to land.

    For everything else category is "" (not yet classified) and engine_path
    is the path this repo would recognize -- identical to remote_path unless
    it is one of the two generated mirrors, in which case it is rewritten
    to its `.claude/` source location before any other gate runs."""
    if remote_path in _GENERATED_ONLY_FILES:
        return None, CAT_GENERATED
    for remote_prefix, engine_prefix in _MIRROR_PREFIXES:
        if remote_path.startswith(remote_prefix):
            return engine_prefix + remote_path[len(remote_prefix):], ""
    return remote_path, ""


def find_reverse_map_collisions(remote_paths: list[str]) -> dict[str, list[str]]:
    """{engine_path: [remote_paths]} for every engine_path that more than
    one distinct remote_path reverse-maps to -- e.g. `agents/executor.md`
    and `.claude/agents/executor.md` both land on engine
    `.claude/agents/executor.md`. If those two carry different content,
    classifying them independently means the LAST one processed silently
    wins whatever a caller ends up writing, and neither classification's
    hash comparison has any way to know the other exists. This makes the
    collision itself detectable so a caller can refuse or flag it, rather
    than depending on the sensitive-prefix list to happen to cover every
    colliding pair -- `sensitive.txt` answers a different question and is
    not a substitute for this check.

    Only genuinely reverse-mapped paths participate: a pure-generated path
    (CAT_GENERATED, no engine_path at all) can never collide with anything
    and is excluded."""
    by_engine_path: dict[str, list[str]] = {}
    for remote_path in remote_paths:
        engine_path, category = reverse_map_path(remote_path)
        if category == CAT_GENERATED:
            continue
        by_engine_path.setdefault(engine_path, []).append(remote_path)
    return {engine_path: sorted(remotes) for engine_path, remotes in by_engine_path.items() if len(remotes) > 1}


def _parse_marker_block(text: str, marker: str) -> list[str]:
    """Pure-Python reimplementation of open-source/lib/manifest_paths.sh's
    marker-block parser: exact-line-equality on the trimmed line, so
    "PATHS_START" never fires on "BOOTSTRAP_PATHS_START" as a substring
    match would. Not shelling out to the bash version so this module (and
    its tests) never need bash + a subprocess round trip for a 15-line
    parse; the marker semantics are copied intentionally, not
    reinterpreted."""
    start = f"<!-- {marker}_START -->"
    end = f"<!-- {marker}_END -->"
    out: list[str] = []
    in_block = False
    for raw_line in text.splitlines():
        trimmed = raw_line.strip()
        if trimmed == start:
            in_block = True
            continue
        if trimmed == end:
            in_block = False
            continue
        if in_block and trimmed:
            out.append(trimmed)
    return out


def load_export_surface_patterns(manifest_md_path: Path = MANIFEST_MD_PATH) -> list[str]:
    """Every pattern that is legitimately part of the public export: the
    real PATHS_START entries plus the GENERATED_PATHS_START entries (the
    mirrors themselves are legitimately public -- they just aren't the
    canonical source; reverse_map_path is what redirects them to their
    source before this list is consulted, and they end up covered here
    twice, once directly and once via their .claude/ counterpart, which
    is harmless since is_in_export_surface is an OR across patterns).

    A directory-style entry ("scripts/", "backend/") is converted to a
    glob covering its whole subtree ("scripts/*") before matching --
    fnmatch has no special-cased path separator, so "*" already spans
    "/", but the bare trailing-slash form on its own would only match the
    literal string "scripts/" and nothing under it."""
    text = manifest_md_path.read_text()
    patterns = _parse_marker_block(text, "PATHS") + _parse_marker_block(text, "GENERATED_PATHS")
    if not patterns:
        raise RuntimeError(f"no PATHS_START/GENERATED_PATHS_START entries found in {manifest_md_path}")
    out = []
    for pat in patterns:
        out.append(pat + "*" if pat.endswith("/") else pat)
    return out


# Re-exported, not reimplemented: pull.validate_path (the function
# path_gate below actually calls in production) answers "is this path
# covered by the allowlist" through pull.manifest_mod.is_included. A
# second, hand-rolled fnmatch loop here would answer the same question with
# a second implementation that could silently drift from the one
# production uses -- so this name is an alias, not a parallel matcher.
is_in_export_surface = pull.manifest_mod.is_included


def read_sensitive_prefixes(path: Path = SENSITIVE_PATH) -> list[str]:
    out = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def is_sensitive(relpath: str, prefixes: list[str]) -> bool:
    for prefix in prefixes:
        if prefix.endswith("/"):
            if relpath == prefix.rstrip("/") or relpath.startswith(prefix):
                return True
        elif relpath == prefix:
            return True
    return False


def check_provenance(author: str | None, allowlist: set[str], *, is_trusted_author) -> tuple[bool, str]:
    """True only when *author* -- the GitHub-authenticated PR author, never
    a commit trailer -- is in *allowlist*. `is_trusted_author` is injected
    (pr_comment_trust.is_trusted_author in production) so this function
    itself never imports a live trust resolver and stays a pure decision
    given its inputs: the same commit, re-run with a forged Co-Authored-By
    or committer naming a trusted login, must still quarantine, because
    nothing here ever looks at commit metadata at all."""
    if is_trusted_author(author, allowlist):
        return True, ""
    return False, f"PR author {author!r} not in trust set"


def path_gate(
    remote_path: str,
    engine_path: str,
    surface_patterns: list[str],
    sensitive_prefixes: list[str],
    *,
    target_root: Path = REPO_ROOT,
) -> tuple[str, str]:
    """(category, reason). category == "" means the path cleared every
    gate here and is ready for hash classification.

    Path safety and export-surface membership are checked against
    *engine_path* -- that is the real filesystem location this repo would
    write to, so that is the one traversal/allowlist checks have to agree
    with. Sensitivity is checked against BOTH *engine_path* and
    *remote_path* -- belt-and-suspenders, so a bug in the reverse-map (or a
    future generated mirror this module doesn't know about yet) cannot
    silently drop the sensitivity check just because the mapped path
    happened not to look sensitive.

    With today's mirror map this second arm is not reachable in production:
    both `agents/` and `commands/` reverse-map under `.claude/`, which is
    itself a sensitive prefix, so the mapped-path check above always fires
    first for the only two mirrors that exist. It only starts doing real
    work the day a new generated mirror is added whose reverse-mapped
    destination is not already sensitive -- which is exactly the case a
    single-argument version of this check would silently mishandle. Keep
    this in mind reading the test that exercises it: that test proves the
    logic is correct by constructing exactly that not-yet-real case, not
    that today's inputs exercise it."""
    valid, reason = pull.validate_path(engine_path, target_root, surface_patterns, excludes=[])
    if not valid:
        # pull.validate_path's own reasons distinguish "not covered by any
        # allowlist include pattern" (out-of-surface) from every other
        # rejection (traversal / absolute / symlink / non-canonical form /
        # case-variant). Preserve that distinction in the category.
        if reason == "not covered by any allowlist include pattern":
            return CAT_OUT_OF_SURFACE, reason
        return CAT_PATH_UNSAFE, reason

    if is_sensitive(engine_path, sensitive_prefixes):
        return CAT_NEEDS_APPROVAL, "matches a sensitive prefix -- human approval required regardless of hash state"
    if is_sensitive(remote_path, sensitive_prefixes):
        return (
            CAT_NEEDS_APPROVAL,
            "raw remote path matches a sensitive prefix even though the reverse-mapped path did not -- "
            "human approval required regardless of hash state",
        )

    return "", ""
