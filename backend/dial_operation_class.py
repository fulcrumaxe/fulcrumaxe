"""
dial_operation_class.py — derive the dial operation class a spawn should be
checked against, from what the spawn will actually touch rather than from
the role alone (D#1805).

Background: backend.dial_registry._ROLE_TO_DIAL_CLASS maps every one of the
20 roles to "agent.spawn" (level 4 / ceiling 5), so a role-only derivation
always passes — including for an executor about to rewrite hooks/. That made
the "sandbox.modify" dial (level 1 / ceiling 1, hardcoded absolute) entirely
unreachable in practice: nothing ever classified a spawn into it unless the
caller opted in with --operation-class, and opting in was the only path
that could get denied.

This module adds one narrow rule ahead of the role fallback: a touchpoint
under hooks/ classifies the spawn as sandbox.modify. Everything else falls
through to the existing role mapping, unchanged. Widening the rule to other
directories is a follow-up, not part of this fix — a first version that
denies too much gets the whole gate switched off (CLAUDE.md: over-blocking
is worse than under-blocking).

Pure function, no I/O, no side effects — easy to unit test and easy for a
security reviewer to read in one pass.
"""

from __future__ import annotations

# Deliberately narrow: only the directory the "sandbox.modify" dial is named
# after. See module docstring — widening this is a separate change.
_SANDBOX_TOUCHPOINT_PREFIXES: tuple[str, ...] = ("hooks/",)


def _normalize_touchpoints(touchpoints: str | list[str] | None) -> list[str]:
    """Accept a comma-separated string (the --touchpoints convention used by
    scripts/spawn-agent.sh's file-scope claim gate) or a list of paths.
    Returns a list of stripped, non-empty path strings.
    """
    if not touchpoints:
        return []
    if isinstance(touchpoints, str):
        raw = touchpoints.split(",")
    else:
        raw = list(touchpoints)
    return [p.strip() for p in raw if p and p.strip()]


def derive_class(role: str, touchpoints: str | list[str] | None = None) -> str:
    """Return the dial class this spawn should be checked against.

    Precedence:
      1. Any touchpoint under hooks/ -> "sandbox.modify"
      2. Otherwise, today's role -> class mapping
         (backend.dial_registry._ROLE_TO_DIAL_CLASS), defaulting to
         "agent.spawn" for unmapped/unknown roles.

    This function never consults dial state (level/ceiling) — it only
    decides which class applies. backend.dial_registry.check() decides
    allow/deny for that class.
    """
    for path in _normalize_touchpoints(touchpoints):
        if any(path.startswith(prefix) for prefix in _SANDBOX_TOUCHPOINT_PREFIXES):
            return "sandbox.modify"

    from backend.dial_registry import _ROLE_TO_DIAL_CLASS  # noqa: PLC0415

    return _ROLE_TO_DIAL_CLASS.get(role, "agent.spawn")


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="dial_operation_class",
        description="Derive the dial operation class for a role + touchpoints.",
    )
    p.add_argument("--role", required=True)
    p.add_argument(
        "--touchpoints",
        default="",
        help="Comma-separated file paths this spawn will touch.",
    )
    args = p.parse_args(argv)
    print(derive_class(args.role, args.touchpoints))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
