"""tests/test_review_findings.py — unit tests for backend/stats/review_findings.py (D#2426).

D#2426 found that the ratio it was originally filed to compute cannot be
computed: `agent_run` has no `issues`/`severity` column, and non-blocking
findings are not required to be emitted at all (code-reviewer.md:257 gates
`issues` on verdict == needs-fix AND the item being blocking). So the module
under test reports the coverage gap honestly instead of a fabricated ratio.

Covers, one class per acceptance item in the Discussion:
  - AC1 TestIssuesColumnMutation  — absent vs. present-with-two-findings must
    render differently; absent must never report a finding count.
  - AC2 TestEmptyCorpus           — an empty PR-comment corpus says
    "no data"/"coverage 0", never "0 non-blocking findings".
  - AC3 TestLinkageConfidence     — a finding with no downstream reference is
    labelled "unlinked"; the forbidden words never appear anywhere, in the
    source or in any rendered report.
  - AC4 TestVerdictCoverage       — per-role terminal/total counting is
    correct on synthetic data. Reproducing the live-store numbers
    (code-reviewer 112/1119; security-reviewer 26/484) against the real
    production stats.duckdb is a manual verification step reported in the
    PR body — this suite runs under a scratch AUTONOMOUS_TEAM_STATE_DIR
    (AC7) and therefore has no access to that file, by design.
  - AC6 TestScopeAndHostOnEveryCount — no bare integer anywhere in rendered
    output; every numeric line carries scope and host.
  - AC7 TestReadOnly              — the scratch state dir this suite runs
    under is provably untouched; read_only=True is asserted at the SOURCE
    level (this module never opens its own connection — see the module
    docstring's "Read-only discipline" section — so there is no fixture
    connection here to assert against; the assertion is over
    backend/stats_connection.py, the connection factory every real caller
    of this module is expected to use).

HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
No network calls in this file — the PR-comment corpus is exercised entirely
against in-memory fixture data, never a live `gh` call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

duckdb = pytest.importorskip("duckdb")

from backend.stats.review_findings import (  # noqa: E402
    NON_BLOCKING_SEVERITIES,
    TERMINAL_VERDICTS,
    agent_run_columns,
    build_report,
    classify_linkage,
    corpus_report,
    extract_findings,
    is_code_reviewer_comment,
    issues_field_report,
    render_report,
    verdict_coverage,
)

_MODULE_SOURCE = (
    Path(__file__).parent.parent / "backend" / "stats" / "review_findings.py"
).read_text(encoding="utf-8")

_FORBIDDEN_WORDS = ("not acted on", "ignored", "unactioned")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_conn_without_issues() -> "duckdb.DuckDBPyConnection":
    """agent_run exactly as backend/agent_run_tracker.py::_ensure_schema
    creates it today — no issues column, no severity column."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE agent_run (
            agent_id     VARCHAR PRIMARY KEY,
            role         VARCHAR NOT NULL,
            discussion   INTEGER,
            pr           INTEGER,
            start_ts     TIMESTAMP,
            end_ts       TIMESTAMP,
            duration_s   DOUBLE,
            verdict      VARCHAR,
            model        VARCHAR
        )
        """
    )
    rows = [
        ("a1", "code-reviewer", "pass"),
        ("a2", "code-reviewer", "needs-fix"),
        ("a3", "code-reviewer", None),
        ("a4", "code-reviewer", "reconciled-stale"),
        ("a5", "security-reviewer", "pass"),
    ]
    for agent_id, role, verdict in rows:
        conn.execute(
            "INSERT INTO agent_run (agent_id, role, start_ts, verdict) VALUES (?, ?, now(), ?)",
            [agent_id, role, verdict],
        )
    return conn


def _make_conn_with_issues() -> "duckdb.DuckDBPyConnection":
    """Same shape, plus an issues column populated on two rows — each row
    carrying one suggestion-severity finding, so the module must report a
    total of 2 non-blocking findings across the two rows."""
    conn = _make_conn_without_issues()
    conn.execute("ALTER TABLE agent_run ADD COLUMN issues VARCHAR")
    conn.execute(
        "UPDATE agent_run SET issues = ? WHERE agent_id = 'a1'",
        [json.dumps([{"severity": "suggestion", "message": "consider renaming X"}])],
    )
    conn.execute(
        "UPDATE agent_run SET issues = ? WHERE agent_id = 'a2'",
        [json.dumps([{"severity": "suggestion", "message": "missing docstring on Y"}])],
    )
    # A blocking error on the same row set — must NOT be counted as
    # non-blocking, proving the module filters on severity rather than just
    # counting array length.
    conn.execute(
        "UPDATE agent_run SET issues = ? WHERE agent_id = 'a2'",
        [json.dumps([
            {"severity": "suggestion", "message": "missing docstring on Y"},
            {"severity": "error", "message": "blocking: null deref on line 40"},
        ])],
    )
    return conn


# ---------------------------------------------------------------------------
# AC1 — absent vs. present-with-two-findings must render differently
# ---------------------------------------------------------------------------

class TestIssuesColumnMutation:
    def test_agent_run_columns_matches_the_live_schema(self):
        """The fixture without issues mirrors backend/agent_run_tracker.py's
        real _ensure_schema column list (minus the columns this module
        doesn't read) — confirm 'issues' really is absent, via the same
        information_schema query the module uses in production."""
        conn = _make_conn_without_issues()
        try:
            cols = agent_run_columns(conn)
        finally:
            conn.close()
        assert "issues" not in cols
        assert {"agent_id", "role", "verdict"} <= cols

    def test_column_absent_reports_no_count(self):
        conn = _make_conn_without_issues()
        try:
            report = issues_field_report(conn)
            assert report["issues_column_present"] is False
            assert report["non_blocking_count"] is None  # absent, not 0

            full = build_report(conn, scope="test:agent_run", host="test-host")
            text = render_report(full)
        finally:
            conn.close()

        assert "ABSENT" in text
        assert "no finding count can be reported" in text
        # Coverage must still be reported — this is the "reports coverage"
        # half of the acceptance item.
        assert "code-reviewer:" in text
        assert "security-reviewer:" in text
        # The exact phrase used for a real count must never appear here.
        assert "non-blocking findings recorded" not in text

    def test_column_present_with_two_suggestions_reports_two(self):
        conn = _make_conn_with_issues()
        try:
            report = issues_field_report(conn)
            assert report["issues_column_present"] is True
            assert report["non_blocking_count"] == 2  # the error must not count

            full = build_report(conn, scope="test:agent_run", host="test-host")
            text = render_report(full)
        finally:
            conn.close()

        assert "PRESENT" in text
        assert "2 non-blocking findings recorded" in text

    def test_both_states_render_different_text(self):
        """The whole point of AC1: a reader that emits the same text in both
        states fails. Assert the two renderings actually differ."""
        conn_absent = _make_conn_without_issues()
        conn_present = _make_conn_with_issues()
        try:
            text_absent = render_report(
                build_report(conn_absent, scope="s", host="h")
            )
            text_present = render_report(
                build_report(conn_present, scope="s", host="h")
            )
        finally:
            conn_absent.close()
            conn_present.close()
        assert text_absent != text_present


# ---------------------------------------------------------------------------
# AC2 — empty PR-comment corpus
# ---------------------------------------------------------------------------

class TestEmptyCorpus:
    def test_empty_list_is_no_data_not_zero_findings(self):
        report = corpus_report([])
        assert report["status"] == "no_data"
        assert report["comments_read"] == 0
        assert report["findings"] == []

        conn = _make_conn_without_issues()
        try:
            full = build_report(conn, scope="s", host="h", pr_comments=[])
            text = render_report(full)
        finally:
            conn.close()

        assert "no data" in text
        assert "coverage 0" in text
        assert "0 non-blocking findings" not in text

    def test_none_is_unmeasured_and_distinguishable_from_empty(self):
        """Never-fetched (None) must not read the same as fetched-and-empty
        ([]) — the same absent-vs-zero property AC1 tests for agent_run."""
        unmeasured = corpus_report(None)
        empty = corpus_report([])
        assert unmeasured["status"] != empty["status"]
        assert unmeasured["comments_read"] is None
        assert empty["comments_read"] == 0

        conn = _make_conn_without_issues()
        try:
            text_unmeasured = render_report(
                build_report(conn, scope="s", host="h", pr_comments=None)
            )
            text_empty = render_report(
                build_report(conn, scope="s", host="h", pr_comments=[])
            )
        finally:
            conn.close()
        assert text_unmeasured != text_empty
        assert "not fetched this run" in text_unmeasured
        assert "no data available" in text_empty


# ---------------------------------------------------------------------------
# AC3 — linkage confidence + forbidden vocabulary
# ---------------------------------------------------------------------------

class TestLinkageConfidence:
    def test_finding_with_no_downstream_ref_is_unlinked(self):
        finding = {"text": "shadowed redefinition in test file", "source_url": "https://example/1"}
        assert classify_linkage(finding) == "unlinked"

    def test_finding_with_downstream_ref_is_linked(self):
        finding = {
            "text": "clamp headcount input",
            "source_url": "https://example/2",
            "downstream_ref": "abc123f",
        }
        assert classify_linkage(finding) == "linked"

    def test_corpus_report_labels_unlinked_finding_in_rendered_text(self):
        comments = [
            {
                "author": "autonomous-agent-7",
                "url": "https://example/pr/1#comment-1",
                "body": "Code review issues:\n\n- mode 100644 rather than 100755 on the new test file\n",
            }
        ]
        report = corpus_report(comments)
        assert report["status"] == "measured"
        assert len(report["findings"]) == 1
        assert report["findings"][0]["linkage"] == "unlinked"

        conn = _make_conn_without_issues()
        try:
            text = render_report(
                build_report(conn, scope="s", host="h", pr_comments=comments)
            )
        finally:
            conn.close()
        assert "[unlinked]" in text
        assert "100644 rather than 100755" in text

    @pytest.mark.parametrize("word", _FORBIDDEN_WORDS)
    def test_forbidden_word_absent_from_source(self, word):
        assert word.lower() not in _MODULE_SOURCE.lower()

    @pytest.mark.parametrize("word", _FORBIDDEN_WORDS)
    def test_forbidden_word_absent_from_every_rendered_report_in_this_suite(self, word):
        """Grep every report this test file is capable of rendering — the
        absent case, the present case, the empty/unmeasured corpus, and the
        unlinked-finding case — for the words this Discussion bans."""
        conn = _make_conn_with_issues()
        try:
            comments = [
                {
                    "author": "autonomous-agent-7",
                    "url": "https://example/pr/1#comment-1",
                    "body": "Code review issues:\n\n- an unlinked finding with no downstream ref\n",
                }
            ]
            texts = [
                render_report(build_report(conn, scope="s", host="h")),
                render_report(build_report(conn, scope="s", host="h", pr_comments=[])),
                render_report(build_report(conn, scope="s", host="h", pr_comments=None)),
                render_report(build_report(conn, scope="s", host="h", pr_comments=comments)),
            ]
        finally:
            conn.close()
        for text in texts:
            assert word.lower() not in text.lower()


# ---------------------------------------------------------------------------
# AC4 — verdict coverage is reported, not hidden
# ---------------------------------------------------------------------------

class TestVerdictCoverage:
    def test_terminal_over_total_per_role(self):
        conn = _make_conn_without_issues()
        try:
            cr = verdict_coverage(conn, "code-reviewer")
            sr = verdict_coverage(conn, "security-reviewer")
        finally:
            conn.close()
        # From the fixture: code-reviewer has 4 rows (pass, needs-fix, None,
        # reconciled-stale) -> 2 terminal / 4 total.
        assert cr == {"role": "code-reviewer", "terminal": 2, "total": 4}
        # security-reviewer has 1 row (pass) -> 1 terminal / 1 total.
        assert sr == {"role": "security-reviewer", "terminal": 1, "total": 1}

    def test_unknown_role_is_zero_over_zero_not_an_error(self):
        conn = _make_conn_without_issues()
        try:
            cov = verdict_coverage(conn, "run-analyst")
        finally:
            conn.close()
        assert cov == {"role": "run-analyst", "terminal": 0, "total": 0}

    def test_live_store_reproduction_is_a_manual_step(self):
        """AC4 asks the report to reproduce (or explain a drift from) the
        live numbers measured in the Discussion body: code-reviewer
        112/1119, security-reviewer 26/484. This suite runs under a scratch
        AUTONOMOUS_TEAM_STATE_DIR (AC7) and therefore has no access to the
        real production stats.duckdb — reproducing those exact numbers is
        done as a manual verification step against the live file and
        reported in the PR body, not asserted here. This test exists only
        to document that decision at the point a reader would look for it.
        """
        assert TERMINAL_VERDICTS == ("pass", "needs-fix")


# ---------------------------------------------------------------------------
# AC6 — scope and host on every count
# ---------------------------------------------------------------------------

class TestScopeAndHostOnEveryCount:
    def test_report_dict_carries_scope_and_host(self):
        conn = _make_conn_with_issues()
        try:
            report = build_report(conn, scope="agent_run@test.duckdb", host="test-host-01")
        finally:
            conn.close()
        assert report["scope"] == "agent_run@test.duckdb"
        assert report["host"] == "test-host-01"

    def test_every_numeric_line_carries_scope_and_host(self):
        conn = _make_conn_with_issues()
        try:
            comments = [
                {
                    "author": "autonomous-agent-7",
                    "url": "https://example/pr/1#comment-1",
                    "body": "Code review issues:\n\n- mode 100644 rather than 100755\n",
                }
            ]
            text = render_report(
                build_report(conn, scope="agent_run@test.duckdb", host="test-host-01", pr_comments=comments)
            )
        finally:
            conn.close()

        for line in text.splitlines():
            if any(ch.isdigit() for ch in line):
                assert "scope=" in line, f"bare integer with no scope: {line!r}"
                assert "host=" in line, f"bare integer with no host: {line!r}"


# ---------------------------------------------------------------------------
# AC7 — read-only, proven
# ---------------------------------------------------------------------------

def _tree_snapshot(path: Path) -> list:
    entries = []
    for root, dirs, files in os.walk(path):
        for name in sorted(dirs) + sorted(files):
            p = Path(root) / name
            entries.append((str(p.relative_to(path)), p.stat().st_mtime))
    return sorted(entries)


class TestReadOnly:
    def test_state_dir_untouched_by_a_full_report_build(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

        before_dir_mtime = state_dir.stat().st_mtime
        before_tree = _tree_snapshot(state_dir)

        conn = _make_conn_with_issues()
        try:
            comments = [
                {
                    "author": "autonomous-agent-7",
                    "url": "https://example/pr/1",
                    "body": "Code review issues:\n\n- a finding\n",
                }
            ]
            render_report(
                build_report(conn, scope="s", host="h", pr_comments=comments)
            )
        finally:
            conn.close()

        after_dir_mtime = state_dir.stat().st_mtime
        after_tree = _tree_snapshot(state_dir)

        assert before_dir_mtime == after_dir_mtime
        assert before_tree == after_tree
        assert after_tree == []  # nothing was ever created under it

    def test_read_only_true_is_documented_at_the_source_level(self):
        """review_findings.py never opens its own DuckDB connection — see
        its module docstring's "Read-only discipline" section — so there is
        no connection object in THIS module to assert read_only=True
        against. The assertion below is a SOURCE assertion (not a live
        fixture assertion) over the two places a real caller is documented
        to get a connection from:
          1. backend/stats_connection.py::get_read_connection(), the
             project's shared per-call read-only connection factory.
          2. This module's own docstring, which tells callers to use it.
        """
        connection_factory_source = (
            Path(__file__).parent.parent / "backend" / "stats_connection.py"
        ).read_text(encoding="utf-8")
        assert "read_only=True" in connection_factory_source
        assert "get_read_connection" in _MODULE_SOURCE
        assert "read_only=True" in _MODULE_SOURCE  # documented in the module docstring


# ---------------------------------------------------------------------------
# Supporting unit coverage: comment parsing, is_code_reviewer_comment
# ---------------------------------------------------------------------------

class TestCommentParsing:
    def test_recognizes_both_code_reviewer_markers(self):
        assert is_code_reviewer_comment("Code review issues:\n\n- x")
        assert is_code_reviewer_comment("Code review passed.\n\nOne minor non-blocking note: ...")
        assert not is_code_reviewer_comment("LGTM, nice work!")
        assert not is_code_reviewer_comment("")

    def test_extract_findings_skips_non_code_reviewer_comments(self):
        comments = [
            {"author": "someone", "url": "u1", "body": "not a review comment at all"},
            {
                "author": "autonomous-agent-7",
                "url": "u2",
                "body": "Code review issues:\n\n- first finding\n- second finding\n",
            },
        ]
        findings = extract_findings(comments)
        assert [f["text"] for f in findings] == ["first finding", "second finding"]
        assert all(f["source_url"] == "u2" for f in findings)

    def test_non_blocking_severities_vocabulary_matches_code_reviewer_md(self):
        # error | warning | suggestion is code-reviewer.md's own vocabulary;
        # only the non-blocking two belong in NON_BLOCKING_SEVERITIES.
        assert set(NON_BLOCKING_SEVERITIES) == {"suggestion", "warning"}
        assert "error" not in NON_BLOCKING_SEVERITIES
