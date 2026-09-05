"""API versioning utilities for autonomous-forever REST gateway.

Provides version extraction from URL paths, Accept-Version header support,
and deprecation metadata for unversioned access.

Usage:
    from backend.api_version import parse_version, check_version, CURRENT_VERSION

    version, canonical_path = parse_version("/v1/health")
    info = check_version(version)
    if info.deprecated:
        # send Deprecation headers
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# The current API version served by this gateway.
CURRENT_VERSION: int = 1

# Versions we still support (older versions may be added here when they exist).
_SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})

# Sunset date for unversioned (deprecated) access — 6 months from module load.
_SUNSET_DATE: str = (date.today() + timedelta(days=182)).isoformat()

_VERSION_RE = re.compile(r"^/v(\d+)(/.*)$")


def parse_version(path: str) -> tuple[int, str]:
    """Extract API version and canonical path from a URL path string.

    Returns a (version, canonical_path) tuple.

    Examples:
        parse_version("/v1/health")  -> (1, "/health")
        parse_version("/health")     -> (CURRENT_VERSION, "/health")
        parse_version("/v2/agents")  -> (2, "/agents")
    """
    m = _VERSION_RE.match(path)
    if m:
        version = int(m.group(1))
        canonical = m.group(2)
        return version, canonical
    # No version prefix — treat as current version, mark as unversioned.
    return CURRENT_VERSION, path


@dataclass
class VersionInfo:
    """Metadata about a requested API version."""

    version: int
    deprecated: bool
    sunset_date: str | None


def check_version(requested: int) -> VersionInfo:
    """Validate *requested* version and return its VersionInfo.

    Raises ValueError for unsupported versions (caller should return HTTP 400).
    """
    if requested not in _SUPPORTED_VERSIONS:
        raise ValueError(f"unsupported API version: {requested}")
    return VersionInfo(
        version=requested,
        deprecated=False,
        sunset_date=None,
    )


def unversioned_info() -> VersionInfo:
    """Return VersionInfo for unversioned (deprecated) access."""
    return VersionInfo(
        version=CURRENT_VERSION,
        deprecated=True,
        sunset_date=_SUNSET_DATE,
    )
