"""Envelope-aware assert helpers for router tests.

The LegacyEnvelopeMiddleware (backend/middleware/legacy_envelope.py) injects
``{"_api_version": N}`` into every dict JSON response and rewrites 4xx bodies
that carry ``detail`` (but no ``error``) into ``{"error": <detail>, ...}``.

These helpers centralise envelope-awareness so future middleware changes touch
one place rather than every test.
"""

from __future__ import annotations

from backend.api_version import CURRENT_VERSION

API_VERSION: int = CURRENT_VERSION


def assert_body_eq(resp, expected: dict) -> None:
    """Assert that *resp* JSON body equals *expected* plus the envelope key.

    Equivalent to::

        assert resp.json() == {"_api_version": API_VERSION, **expected}

    A missing or extra key in *expected* will still fail — this is a full
    dict equality check, not a subset check.
    """
    assert resp.json() == {"_api_version": API_VERSION, **expected}


def envelope_error(resp) -> object:
    """Return the ``error`` value from a rewritten 4xx body.

    The middleware's Rule 6 renames ``detail`` → ``error`` on 4xx responses
    that have a ``detail`` key but no ``error`` key.  Tests that previously
    read ``resp.json()["detail"]`` should switch to ``envelope_error(resp)``.
    """
    return resp.json()["error"]
