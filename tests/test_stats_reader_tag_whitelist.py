"""tests/test_stats_reader_tag_whitelist.py — verify tag key whitelist in stats_reader.

The f-string at stats_reader.py interpolates the tag key directly into SQL.
These tests confirm that invalid keys are rejected before interpolation happens.
"""
from __future__ import annotations

import argparse
import os

import pytest


def _make_args(tag: str) -> argparse.Namespace:
    """Build a minimal Namespace that mimics the CLI args object."""
    ns = argparse.Namespace()
    ns.tag = tag
    ns.since = None
    ns.metric = "some_metric"
    return ns


def _seed_db(db_path: str) -> None:
    """Create a minimal metric_event table in a temp DuckDB file."""
    import duckdb
    con = duckdb.connect(db_path)
    con.execute(
        "CREATE TABLE metric_event "
        "(ts TIMESTAMP, metric VARCHAR, value DOUBLE, tags JSON)"
    )
    con.close()


class TestTagKeyWhitelist:
    """Tag key must match [a-zA-Z0-9_]+; anything else must raise ValueError."""

    def test_invalid_key_raises_value_error(self, tmp_path, monkeypatch):
        """A key containing SQL-special chars must raise ValueError before the query runs."""
        import backend.stats_reader as stats_reader

        db_path = str(tmp_path / "test.duckdb")
        monkeypatch.setenv("STATS_DB_PATH", db_path)
        _seed_db(db_path)

        ns = _make_args(tag="bad'key=value")

        with pytest.raises(ValueError, match="Invalid tag key"):
            stats_reader.cmd_distribution(ns)

    def test_valid_key_does_not_raise_whitelist_error(self, tmp_path, monkeypatch):
        """A clean alphanumeric key must not be rejected by the whitelist."""
        import backend.stats_reader as stats_reader

        db_path = str(tmp_path / "test.duckdb")
        monkeypatch.setenv("STATS_DB_PATH", db_path)
        _seed_db(db_path)

        ns = _make_args(tag="safe_key=some_value")

        try:
            stats_reader.cmd_distribution(ns)
        except ValueError as exc:
            assert "Invalid tag key" not in str(exc), (
                f"Whitelist incorrectly rejected a safe key: {exc}"
            )
        except SystemExit:
            # SystemExit from empty result set or schema mismatch is fine —
            # what matters is no ValueError for whitelist rejection
            pass
        except Exception:
            # Other DB-level exceptions are acceptable for this test
            pass
