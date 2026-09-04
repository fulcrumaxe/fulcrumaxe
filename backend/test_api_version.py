"""Unit tests for backend.api_version.

Run with:
    python -m pytest backend/test_api_version.py
"""

from __future__ import annotations

import pytest

from backend.api_version import (
    CURRENT_VERSION,
    VersionInfo,
    check_version,
    parse_version,
    unversioned_info,
)


# ---------------------------------------------------------------------------
# parse_version
# ---------------------------------------------------------------------------


def test_parse_version_with_v1_prefix() -> None:
    version, path = parse_version("/v1/health")
    assert version == 1
    assert path == "/health"


def test_parse_version_with_no_prefix() -> None:
    version, path = parse_version("/health")
    assert version == CURRENT_VERSION
    assert path == "/health"


def test_parse_version_with_v2_prefix() -> None:
    version, path = parse_version("/v2/agents")
    assert version == 2
    assert path == "/agents"


def test_parse_version_nested_path() -> None:
    version, path = parse_version("/v1/agents/executor")
    assert version == 1
    assert path == "/agents/executor"


def test_parse_version_unversioned_nested() -> None:
    version, path = parse_version("/registry/stats")
    assert version == CURRENT_VERSION
    assert path == "/registry/stats"


# ---------------------------------------------------------------------------
# check_version
# ---------------------------------------------------------------------------


def test_check_version_v1_supported() -> None:
    info = check_version(1)
    assert isinstance(info, VersionInfo)
    assert info.version == 1
    assert info.deprecated is False
    assert info.sunset_date is None


def test_check_version_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unsupported API version: 99"):
        check_version(99)


def test_check_version_zero_raises() -> None:
    with pytest.raises(ValueError, match="unsupported API version: 0"):
        check_version(0)


# ---------------------------------------------------------------------------
# unversioned_info (deprecated access)
# ---------------------------------------------------------------------------


def test_unversioned_info_deprecated_flag() -> None:
    info = unversioned_info()
    assert info.deprecated is True


def test_unversioned_info_has_sunset_date() -> None:
    info = unversioned_info()
    assert info.sunset_date is not None
    # Sunset should be roughly 6 months in the future (at least 100 days).
    from datetime import date
    sunset = date.fromisoformat(info.sunset_date)
    assert (sunset - date.today()).days >= 100


def test_unversioned_info_version_is_current() -> None:
    info = unversioned_info()
    assert info.version == CURRENT_VERSION
