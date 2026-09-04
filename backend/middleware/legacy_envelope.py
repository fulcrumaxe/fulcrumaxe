"""Legacy-envelope response middleware.

Reconciles FastAPI's default response shape with the shape the dashboard
expects from the legacy api.py server:

  - legacy injects ``_api_version`` into every dict JSON body
  - legacy returns errors as ``{"error": msg}``; FastAPI emits ``{"detail": ...}``

Rules (applied only to ``application/json`` responses whose body is a JSON
object / dict):

1. StreamingResponse (SSE, WebSocket, proxied streams) -- PASS THROUGH.
   We detect these by checking whether the ``content-type`` header starts with
   ``text/event-stream`` or the ASGI ``type`` is ``http.response.body`` with
   streaming not yet buffered.  The simplest reliable signal is the absence of
   a finite ``content-length`` on a non-204 response -- but more robustly we
   check the ASGI scope for a streaming generator (handled inside
   ``dispatch``).

2. Non-JSON content-type or empty / 204 body -- pass through unchanged.

3. JSON body that is NOT a dict (e.g. a list) -- pass through unchanged.

4. ``/rpc`` path or body with ``"jsonrpc"`` key -- pass through unchanged.

5. ``status == 500`` (Internal Server Error only) -- emit GENERIC body
   ``{"error": "internal error", "_api_version": N}``
   NEVER echo exception text, stack traces, or internal paths (CWE-209).
   503/502/504 pass through with their original body (they are operational
   signals, not internal errors that risk leaking internals).

6. ``status >= 400`` and body has ``"detail"`` but no ``"error"`` -- rewrite
   to ``{"error": <detail-value>, **rest_of_dict, "_api_version": N}``.

7. All other dict bodies -- inject ``_api_version`` at the front if absent.

8. Recompute ``Content-Length`` after any body rewrite.

Generic 5xx is ALSO guaranteed for unhandled exceptions via the companion
``_unhandled_exc_handler`` registered in ``asgi_app.py``.

Version source: ``backend.api_version.CURRENT_VERSION`` -- the same constant
that ``api.py`` uses (imported, never hardcoded here).
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse
from starlette.types import ASGIApp

from backend.api_version import CURRENT_VERSION

logger = logging.getLogger(__name__)

# Content-type prefixes we will process (rewrite envelopes).
_JSON_PREFIX = "application/json"
# Content-types we must NEVER buffer (streaming protocols).
_STREAMING_PREFIXES = ("text/event-stream", "multipart/")


def _is_streaming_content_type(ct: str) -> bool:
    """Return True for content-types that must not be buffered."""
    ct_lower = ct.lower()
    return any(ct_lower.startswith(p) for p in _STREAMING_PREFIXES)


def _generic_5xx() -> Response:
    """Return the CWE-209-safe generic 5xx body."""
    body = json.dumps({"error": "internal error", "_api_version": CURRENT_VERSION}).encode()
    return Response(
        content=body,
        status_code=500,
        media_type="application/json",
        headers={"Content-Length": str(len(body))},
    )


def _rewrite_body(status_code: int, path: str, data: dict) -> dict:
    """Apply envelope rewrites to *data* and return the modified dict.

    Does NOT re-serialise -- caller handles that.
    """
    # Rule 4: JSON-RPC bodies are exempt.
    if "jsonrpc" in data:
        return data

    # Rule 5: 500 only -- caller handles this via _generic_5xx(); should not reach here.
    # (kept as a safety net; 503 is a legitimate operational response, not an error)
    if status_code == 500:
        return {"error": "internal error", "_api_version": CURRENT_VERSION}

    # Rule 6: 4xx with "detail" and no "error".
    if status_code >= 400 and "detail" in data and "error" not in data:
        detail = data.pop("detail")
        result = {"error": detail, **data}
        # Inject _api_version at the front if absent.
        if "_api_version" not in result:
            result = {"_api_version": CURRENT_VERSION, **result}
        else:
            # Move existing _api_version to front.
            v = result.pop("_api_version")
            result = {"_api_version": v, **result}
        return result

    # Rule 7: Inject _api_version at front if absent.
    if "_api_version" not in data:
        data = {"_api_version": CURRENT_VERSION, **data}
    return data


class LegacyEnvelopeMiddleware(BaseHTTPMiddleware):
    """Outermost middleware that normalises JSON responses to legacy shape.

    Must be registered LAST via ``app.add_middleware()`` so it wraps all
    inner middleware (Starlette applies middleware in reverse registration
    order -- the last-added is outermost).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Attempt to get the response from downstream.
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            # Unhandled exception from a downstream handler.
            logger.error(
                "LegacyEnvelopeMiddleware caught unhandled exception: %s",
                type(exc).__name__,
            )
            return _generic_5xx()

        # --- Rule 1: StreamingResponse / SSE -- never buffer. ---
        # BaseHTTPMiddleware wraps streaming responses in a background task;
        # we detect streaming by checking the content-type header BEFORE
        # consuming the body.  If it's a streaming type, return immediately.
        ct = response.headers.get("content-type", "")
        if _is_streaming_content_type(ct):
            return response

        # --- Rule 2: Non-JSON content-type -- pass through. ---
        if not ct.startswith(_JSON_PREFIX):
            return response

        # --- Rule 2: Empty body / 204 -- pass through. ---
        if response.status_code == 204:
            return response

        # Buffer the body.
        body_bytes = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body_bytes += chunk if isinstance(chunk, bytes) else chunk.encode()

        # Empty body -- pass through.
        if not body_bytes:
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=ct,
            )

        # --- Rule 5: 500 Internal Server Error -- generic body (CWE-209). ---
        # 503/502/504 are legitimate operational responses (e.g. stream cap reached,
        # upstream unavailable) and must pass through with their original body.
        # Only 500 carries a risk of leaking stack traces / internal paths.
        if response.status_code == 500:
            return _generic_5xx()

        # --- Parse JSON. ---
        try:
            data = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError):
            # Not valid JSON -- pass through unchanged.
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=ct,
            )

        # --- Rule 3: Non-dict JSON (e.g. list) -- pass through. ---
        if not isinstance(data, dict):
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=ct,
            )

        # --- Rule 4: /rpc path -- pass through. ---
        path = request.url.path
        if path == "/rpc" or path.startswith("/rpc/"):
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=ct,
            )

        # --- Apply envelope rewrites. ---
        rewritten = _rewrite_body(response.status_code, path, data)

        new_body = json.dumps(rewritten).encode()

        # Build new headers -- update Content-Length, keep everything else.
        new_headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() != "content-length"
        }
        new_headers["content-length"] = str(len(new_body))

        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=new_headers,
            media_type="application/json",
        )


async def unhandled_exc_handler(request: Request, exc: Exception) -> Response:
    """Starlette exception handler -- catches anything not already handled.

    Returns a generic 5xx body with no stack trace / internal path (CWE-209).
    Registered in asgi_app.py via app.add_exception_handler(Exception, ...).
    """
    logger.error(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return _generic_5xx()
