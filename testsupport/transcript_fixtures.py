"""Render checkout-path placeholders in transcript fixtures at load time.

Recorded-transcript fixtures under backend/tests/fixtures/transcripts/ describe
an agent working inside a checkout, so some of their paths have to name one.
They used to name a specific person's machine, which made every one of them a
machine-specific assertion wearing a fixture's clothes: correct on the laptop
that recorded it, arbitrary everywhere else, and silently so — the classifiers
under test key on path *shape*, so a stale root still produced a green run.

The tracked fixture now carries placeholder tokens. This module substitutes
them for the values of the machine actually running the tests, writes the
result to a scratch copy, and hands back that path. The fixture stays portable;
the thing the classifier sees is real.

    {{REPO_ROOT}}      the main checkout, from backend.repo_root.main_repo_root
    {{HOME}}           the running user's home directory
    {{PROJECT_SLUG}}   Claude Code's transcript-directory name for the main
                       checkout: its path with every '/' turned into '-'

The scratch copy keeps the source file's *basename*, which is load-bearing and
easy to lose: run_analyst derives an agent id from the transcript filename via
transcript_reader.agent_id_from_path, so renaming the copy would change the id
the assertions are written against.

Why substitute rather than let the fixture hold today's path: a golden file
pinned to one machine's path is not a golden file, and re-pinning it to a new
machine only moves the expiry date. See D#1997 acceptance item 15 — this module
is the "placeholder token rendered at load time" half of it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from backend.repo_root import main_repo_root

#: Rendered copies live here for the life of the process. Held in a module
#: global rather than cleaned up per call because the returned paths stay in use
#: for the duration of a test, and pytest reports against them on failure.
_SCRATCH: tempfile.TemporaryDirectory | None = None


def _scratch_dir() -> Path:
    global _SCRATCH
    if _SCRATCH is None:
        _SCRATCH = tempfile.TemporaryDirectory(prefix="transcript-fixtures-")
    return Path(_SCRATCH.name)


def substitutions() -> dict[str, str]:
    """The placeholder table for this machine.

    Recomputed per call rather than cached at import: a test that repoints the
    resolver should see the new answer, and this is not on a hot path.
    """
    root = str(main_repo_root())
    return {
        "{{REPO_ROOT}}": root,
        "{{HOME}}": str(Path.home()),
        "{{PROJECT_SLUG}}": root.replace("/", "-"),
    }


def render_text(text: str) -> str:
    """Substitute every placeholder in *text*."""
    for token, value in substitutions().items():
        text = text.replace(token, value)
    return text


def render_fixture(src: Path) -> Path:
    """Return a path to a copy of *src* with placeholders substituted.

    A fixture carrying no placeholders is returned unchanged, so callers can
    route every fixture through here without caring which ones are templated.
    """
    src = Path(src)
    text = src.read_text(encoding="utf-8")
    rendered = render_text(text)
    if rendered == text:
        return src

    # Same basename, fresh directory per source path, so two fixtures with the
    # same name under different parent directories cannot collide.
    slot = _scratch_dir() / str(abs(hash(str(src))))
    slot.mkdir(parents=True, exist_ok=True)
    dest = slot / src.name
    dest.write_text(rendered, encoding="utf-8")
    return dest


__all__ = ["render_fixture", "render_text", "substitutions"]
