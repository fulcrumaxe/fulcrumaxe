"""
Shared global SSE/WS stream limiter singleton.

This module holds the ONE authoritative GlobalStreamLimiter instance that ALL
SSE routes (``/stream/*``, ``/feed``, ``/events``) share. Both ``asgi_app.py``
and any router that creates SSE responses import from here so they all count
against the same cap.

``AF_GLOBAL_STREAM_CAP`` controls the cap; default is 40.

Usage::

    from backend.deps.shared_limiter import get_shared_limiter

    limiter = get_shared_limiter()
    if not limiter.acquire():
        return Response(status_code=503)
    try:
        yield  # stream data
    finally:
        limiter.release()
"""

from __future__ import annotations

import os

from backend.deps.stream_limiter import GlobalStreamLimiter

#: Default cap when AF_GLOBAL_STREAM_CAP is not set.
DEFAULT_GLOBAL_STREAM_CAP: int = 40

# ---------------------------------------------------------------------------
# Singleton — created once at import time using the env var.
# ---------------------------------------------------------------------------

def _make_limiter() -> GlobalStreamLimiter:
    raw = os.environ.get("AF_GLOBAL_STREAM_CAP", "")
    try:
        cap = max(1, int(raw))
    except (ValueError, TypeError):
        cap = DEFAULT_GLOBAL_STREAM_CAP
    return GlobalStreamLimiter(max_global=cap)


# Module-level singleton — replace in tests via monkeypatch.
_limiter: GlobalStreamLimiter = _make_limiter()


def get_shared_limiter() -> GlobalStreamLimiter:
    """Return the module-level shared stream limiter."""
    return _limiter


def _reset_limiter() -> None:
    """Re-read AF_GLOBAL_STREAM_CAP and create a fresh limiter.

    Called from tests that need to adjust the cap via monkeypatch.
    """
    global _limiter
    _limiter = _make_limiter()
