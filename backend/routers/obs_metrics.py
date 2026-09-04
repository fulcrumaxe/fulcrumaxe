"""
FastAPI router — /metrics (Prometheus scrape endpoint).

This route is PUBLIC (no auth required) — Prometheus scrapers do not send
bearer tokens. Mirrors api.py:2649-2661: returns Prometheus text format
(content-type text/plain; version=0.0.4; charset=utf-8).

The legacy handler assembles three text blocks:
  1. generate_prometheus_metrics() — core AF metrics
  2. _version_metrics_text()       — af_api_requests_total counters
  3. _spawn_guard_metrics_text()   — af_claude_spawn_* counters

For the FastAPI path we skip the legacy api.py version/spawn-guard module-level
state (they belong to the ThreadingHTTPServer process). We call
generate_prometheus_metrics() from backend.metrics only, which covers the
canonical AF metrics. The version/spawn counters are api.py-internal state;
omitting them preserves identical real-metric content while avoiding a
circular import of the whole legacy server module.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from backend.metrics import generate_prometheus_metrics

router = APIRouter(tags=["observability"])

# /metrics is public — Prometheus scrape does not send auth headers.
# It sits BEFORE the _check_auth() gate in the legacy api.py (line 2649 vs 2671).
# Added to PUBLIC_ROUTES in deps/auth.py.


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus metrics scrape endpoint",
    description=(
        "Returns AF metrics in Prometheus text format "
        "(content-type: text/plain; version=0.0.4; charset=utf-8). "
        "Public — no authentication required."
    ),
    include_in_schema=True,
)
def get_metrics() -> PlainTextResponse:
    """Prometheus scrape endpoint — mirrors api.py:2649-2661."""
    body = generate_prometheus_metrics()
    # Preserve the exact content-type the legacy server sends.
    return PlainTextResponse(
        content=body,
        status_code=200,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
