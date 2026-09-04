"""hooks/repo_root.py

Resolve the *main* repository root for the sandbox hooks.

Why this module exists
----------------------
The sandbox hooks used to hardcode the main repo root as a literal path. When
that literal does not exist on the machine the hooks are running on, every
worktree of *this* repo matches the "some other self-governed team" predicate in
``sandbox_rules.is_foreign_self_governed`` and the PreToolUse hook defers to it —
allowing every Bash call, every ``Agent()`` spawn and every merge before any tier
logic runs. The repo then looks protected and is not.

Deriving the root from ``__file__`` alone is not enough either. Sub-agents run
from a git worktree that contains a full copy of ``hooks/``, so
``Path(__file__).resolve().parent.parent`` yields the *worktree* path when the
hook is invoked from a worktree copy. If that became the main root, the
worktree's own CWD would classify as ``team_lead`` and the Team Lead exemption
would allow everything — a worse bypass than the one this module fixes.

So: derive the candidate from ``__file__``, then correct for the worktree case by
reading the ``.git`` entry. In a linked git worktree ``.git`` is a *file* holding
``gitdir: /path/to/main/.git/worktrees/<id>``; the text before ``/.git/worktrees/``
is the real main root. In the main checkout ``.git`` is a directory and the
candidate is already correct.

That read can fail — ``.git`` missing, empty, garbage, unreadable, marker-less,
or a worktree marker pointing at a path that isn't actually a repo root.
``derive_repo_root_from`` reports a ``confident`` flag alongside the root for
exactly this reason: when the evidence is degraded by accident — an
interrupted ``git worktree add``, a moved checkout, a stale or truncated
``.git`` file — the safe answer is "I don't know", never a best-effort guess.
Silently falling back to ``candidate`` in that case would recreate the
worktree-promoted-to-``team_lead`` bypass described above through a different
door — an accidentally corrupted or absent ``.git`` entry instead of a
hardcoded literal or the env override. Callers that decide tier
(``classify_cwd``) must refuse the permissive outcome when ``confident`` is
False and tier down instead; they must never hard-fail the hook over it,
since that would block every agent on a transient filesystem hiccup.

This is a guardrail against *accidental* misclassification, not a defense
against an agent that deliberately fabricates git evidence — see "Residual"
below for exactly what is and is not covered.

Pure filesystem. No subprocess — ``hooks/sandbox_rules.py`` is subprocess-free by
design and imports this module.

Environment override — TEST ONLY, and deliberately not load-bearing
-------------------------------------------------------------------
``SANDBOX_MAIN_REPO_ROOT`` overrides the derived root. It exists so that the
shell tests, which invoke the hook as a subprocess and cannot monkeypatch a
Python attribute, can pin a synthetic root and keep their fixture paths
meaningful.

This variable is settable by the very agent being sandboxed, so it must never be
the thing that decides whether that agent is contained. In ``hooks/sandbox_rules.py``
it is not: both containment checks also test against :func:`derive_main_repo_root` —
the filesystem-derived root, which ignores the environment entirely — as a floor the
override cannot lift. ``is_foreign_self_governed`` returns ``False`` early for any
path under the derived root, before it ever looks at ``MAIN_REPO_ROOT``. ``classify_cwd``
matches ``"worktree"`` against worktree prefixes built from the derived root (as well
as from ``MAIN_REPO_ROOT``), and that check runs before the ``"team_lead"`` check, so
setting the override to a bogus path cannot, by itself, promote a real worktree to
``team_lead`` or make this repo look like a foreign team — the derived root is what
those two checks actually key off, and the override never touches it. That is a
narrower guarantee than "this repo's tiering cannot be bypassed": see "Residual" below
for the case where the *derived* root itself is wrong because the agent forged the
``.git`` evidence it is derived from.

Residual — accepted out of scope
---------------------------------
The confidence check defends against *accidental* degraded ``.git`` states —
missing, empty, garbage, unreadable, marker-less, or a worktree marker
pointing nowhere real. It does NOT defend against an agent that deliberately
constructs convincing evidence inside a tree it already owns:

* Replacing a worktree's ``.git`` *file* with a *directory* containing a
  hand-written ``HEAD`` file satisfies :func:`_is_real_git_dir`, which yields
  ``confident=True`` with the worktree's own path as the reported root — the
  worktree-promoted-to-``team_lead`` shape this module exists to prevent,
  reconstructed on purpose instead of by accident.
* Planting a ``gitdir:`` marker whose line contains ``/.git/worktrees/``,
  where the text before that marker names a second, self-constructed
  directory carrying a forged ``HEAD`` at ``<that directory>/.git``, satisfies
  the same check — the resolver splits the line at the marker and checks
  ``<main_root>/.git``, it never stats the marker's literal target
  (``<main_root>/.git/worktrees/<id>``), which need not exist at all. The
  resolver has no way to distinguish a forged main root from the real one.

Both require deliberate, multi-step construction inside a tree the agent
already has write access to; neither happens by filesystem accident. The
Team Lead's ruling (D#1890, PR #1894 round 5) is that ``hooks/`` is a
guardrail against accidental writes, not a boundary against an agent
deliberately probing for a bypass, and that no observed agent behaviour has
warranted the stronger model — so both are accepted as out of scope rather
than fixed here. ``tests/test_hooks_repo_root.py`` records both as strict
``xfail`` so this stays an explicit, tracked decision rather than a silent
gap.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_OVERRIDE = "SANDBOX_MAIN_REPO_ROOT"

# Marker embedded in the ``gitdir:`` line of a linked worktree's ``.git`` file.
_WORKTREE_GITDIR_MARKER = "/.git/worktrees/"


def _is_real_git_dir(git_entry: Path) -> bool:
    """True if *git_entry* is a directory containing a ``HEAD`` file.

    A bare ``.is_dir()`` is not enough — an empty directory dropped in place of
    a worktree's ``.git`` *file* (or a bogus target named by a corrupted
    ``gitdir:`` line) also passes that check, and costs the caller nothing to
    plant. Requiring a ``HEAD`` file rejects that zero-effort decoy for free.

    It does NOT reject a decoy that goes one step further and writes a
    plausible ``HEAD`` file into the directory it creates — that satisfies
    this check too. See the module docstring's "Residual" section: this is a
    guardrail against accidental degraded ``.git`` states, not a check that
    survives deliberate forgery.
    """
    try:
        return git_entry.is_dir() and (git_entry / "HEAD").is_file()
    except OSError:
        return False


def derive_repo_root_from(module_file: str | Path) -> tuple[Path, bool]:
    """Return ``(main repo root, confident)`` implied by *module_file*, ignoring
    the environment.

    *module_file* is any file inside ``<root>/hooks/``. Handles both the main
    checkout (``.git`` is a directory) and a linked worktree copy (``.git`` is a
    file pointing back at the main checkout).

    ``confident`` is True when ``git_entry`` (or, for a worktree marker, the
    ``<main_root>/.git`` obtained by splitting the marker line at
    ``/.git/worktrees/`` — never the marker's literal target) passes
    :func:`_is_real_git_dir` — a real git *directory* containing a ``HEAD``
    file. It is False for every accidentally degraded ``.git`` this module
    was built to catch: missing, empty, garbage, unreadable, marker-less, or
    a worktree marker whose derived main root isn't actually a repo root.
    ``candidate`` (the naive, module-location guess) is still
    returned in the unconfident case so callers have something anchored to
    where this code is actually running, never an arbitrary path read out of a
    tampered ``.git`` file — but callers must not treat it as authoritative
    for granting an elevated tier (see ``classify_cwd``).

    ``confident`` does NOT mean the evidence is genuine — it means it passed
    the ``HEAD``-file check. A directory with a hand-written ``HEAD`` file
    also satisfies :func:`_is_real_git_dir` and reports ``confident=True``.
    That gap is accepted out of scope; see the module docstring's "Residual"
    section.
    """
    candidate = Path(module_file).resolve().parent.parent

    git_entry = candidate / ".git"
    try:
        if _is_real_git_dir(git_entry):
            return candidate, True
        if git_entry.is_file():
            for line in git_entry.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("gitdir:"):
                    continue
                gitdir = line[len("gitdir:") :].strip()
                if _WORKTREE_GITDIR_MARKER in gitdir:
                    main_root = Path(gitdir.split(_WORKTREE_GITDIR_MARKER, 1)[0])
                    if _is_real_git_dir(main_root / ".git"):
                        return main_root, True
                # Marker-less (e.g. a submodule pointer), or a worktree marker
                # pointing somewhere that isn't actually a repo root — not confident.
                break
    except OSError:
        pass

    return candidate, False


# Derived once at import. This is the security-critical value: it never reads the
# environment, so it cannot be steered by a sandboxed agent.
_DERIVED_ROOT, _DERIVED_ROOT_CONFIDENT = derive_repo_root_from(__file__)


def derive_main_repo_root() -> Path:
    """Main repo root derived purely from the filesystem. Never honours the env override."""
    return _DERIVED_ROOT


def is_main_repo_root_confident() -> bool:
    """True if the ``HEAD``-file check behind ``derive_main_repo_root()`` passed.

    This does NOT mean the evidence is genuine — confident means the
    ``HEAD``-file check passed, not that the ``.git`` state wasn't
    deliberately forged (see the module docstring's "Residual" section).
    False means the ``.git`` read that produced it was missing, unparseable,
    or otherwise ambiguous. Callers must not use the derived root to grant an
    elevated tier when this is False — tier down instead (see
    ``sandbox_rules.classify_cwd``).
    """
    return _DERIVED_ROOT_CONFIDENT


def resolve_main_repo_root() -> Path:
    """Main repo root, honouring the test-only ``SANDBOX_MAIN_REPO_ROOT`` override.

    See the module docstring for why the override is safe to expose: the
    containment predicate that decides whether a path belongs to a foreign team
    uses :func:`derive_main_repo_root`, not this function.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override and os.path.isabs(override):
        return Path(override)
    return _DERIVED_ROOT
