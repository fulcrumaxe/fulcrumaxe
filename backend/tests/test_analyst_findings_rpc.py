"""
tests/test_analyst_findings_rpc.py — unit tests for analyst_findings stats module + RPC.

Tests:
  - load() returns clean empty state when reports dir is missing
  - load() returns clean empty state when reports dir exists but is empty
  - load() parses a fixture report and groups by severity
  - load() picks the LATEST report when multiple files exist
  - load() gracefully handles a corrupt JSON file
  - RPC handle() delegates to load() and returns valid structure
  - RPC handle() returns empty structure when no reports

HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
No network calls, no subprocess calls beyond what is tested.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.stats.analyst_findings import load, SEVERITY_ORDER


# ── Fixtures ────────────────────────────────────────────────────────────────

def _make_report(
    findings: list[dict],
    report_at: str = "2026-05-20T14:44:44Z",
    runs_analyzed: int = 5,
) -> dict:
    return {
        "report_at": report_at,
        "window": {
            "since": "2026-05-20T02:44:44Z",
            "until": "2026-05-20T14:44:44Z",
        },
        "runs_analyzed": runs_analyzed,
        "findings": findings,
    }


FIXTURE_FINDINGS = [
    {
        "category": "failure_cluster",
        "severity": "high",
        "title": "Pattern 'OOM' hit 6 times in recent runs",
        "evidence": ["executor/run-1", "executor/run-2"],
        "suggested_discussion_title": "[Bug] Recurring OOM errors",
        "suggested_tag": "[Bug]",
    },
    {
        "category": "cost_outlier",
        "severity": "medium",
        "title": "Role 'code-reviewer' uses 120,000 tokens/pass",
        "evidence": ["code-reviewer"],
        "suggested_discussion_title": "[Small] Reduce token usage for code-reviewer",
        "suggested_tag": "[Small]",
    },
    {
        "category": "stale_snapshot_consumption",
        "severity": "medium",
        "title": "Current loop-snapshot is 143532s old",
        "evidence": ["age=143532s", "generated_at=2026-05-18T22:52:32Z"],
        "suggested_discussion_title": "[Small] Snapshot refresh not triggering",
        "suggested_tag": "[Small]",
    },
]


# ── Tests: load() ──────────────────────────────────────────────────────────

class TestAnalystFindingsLoad:
    def test_missing_dir_returns_empty(self, tmp_path):
        """Missing reports dir should return graceful empty state."""
        no_dir = tmp_path / "nonexistent"
        result = load(reports_dir=no_dir)

        assert result["total"] == 0
        assert result["report_at"] is None
        assert result["window"] is None
        assert result["runs_analyzed"] == 0
        assert result["error"] is None
        for sev in SEVERITY_ORDER:
            assert result["by_severity"][sev] == []

    def test_empty_dir_returns_empty(self, tmp_path):
        """Empty reports dir (no JSON files) should return graceful empty state."""
        result = load(reports_dir=tmp_path)
        assert result["total"] == 0
        assert result["report_at"] is None

    def test_parses_fixture_report(self, tmp_path):
        """Fixture report should parse and group findings by severity."""
        report = _make_report(FIXTURE_FINDINGS)
        (tmp_path / "2026-05-20T14-44-44Z.json").write_text(json.dumps(report))

        result = load(reports_dir=tmp_path)

        assert result["total"] == 3
        assert result["runs_analyzed"] == 5
        assert result["report_at"] == "2026-05-20T14:44:44Z"
        assert len(result["by_severity"]["high"]) == 1
        assert len(result["by_severity"]["medium"]) == 2
        assert len(result["by_severity"]["low"]) == 0

        high = result["by_severity"]["high"][0]
        assert high["category"] == "failure_cluster"
        assert high["severity"] == "high"
        assert "OOM" in high["title"]
        assert "executor/run-1" in high["evidence"]

    def test_picks_latest_report(self, tmp_path):
        """When multiple report files exist, the lexicographically latest is used."""
        old_report = _make_report([FIXTURE_FINDINGS[0]], report_at="2026-05-19T10-00-00Z", runs_analyzed=2)
        new_report = _make_report(FIXTURE_FINDINGS, report_at="2026-05-20T14:44:44Z", runs_analyzed=5)

        (tmp_path / "2026-05-19T10-00-00Z.json").write_text(json.dumps(old_report))
        (tmp_path / "2026-05-20T14-44-44Z.json").write_text(json.dumps(new_report))

        result = load(reports_dir=tmp_path)
        # Latest report has 3 findings, old has 1
        assert result["total"] == 3
        assert result["runs_analyzed"] == 5

    def test_graceful_on_corrupt_json(self, tmp_path):
        """A corrupt JSON file should not crash load(); returns empty state."""
        (tmp_path / "2026-05-20T14-44-44Z.json").write_text("{ not valid json !!!")

        result = load(reports_dir=tmp_path)
        assert result["total"] == 0
        assert result["error"] is None  # error is logged, not surfaced as an exception

    def test_unknown_severity_goes_to_low(self, tmp_path):
        """Findings with unrecognized severity should be bucketed as low."""
        finding = {
            "category": "weird_category",
            "severity": "critical",  # not a known level
            "title": "Something strange",
            "evidence": [],
        }
        report = _make_report([finding])
        (tmp_path / "2026-05-20T14-44-44Z.json").write_text(json.dumps(report))

        result = load(reports_dir=tmp_path)
        assert result["total"] == 1
        assert len(result["by_severity"]["low"]) == 1

    def test_empty_findings_list(self, tmp_path):
        """A report with zero findings should return total=0 but still parse report metadata."""
        report = _make_report([], runs_analyzed=0)
        (tmp_path / "2026-05-20T14-44-44Z.json").write_text(json.dumps(report))

        result = load(reports_dir=tmp_path)
        assert result["total"] == 0
        assert result["report_at"] == "2026-05-20T14:44:44Z"
        assert result["runs_analyzed"] == 0

    def test_generated_at_is_present(self, tmp_path):
        """generated_at field should always be set to an ISO string."""
        result = load(reports_dir=tmp_path)
        assert "generated_at" in result
        assert result["generated_at"].endswith("Z")


# ── Tests: RPC handle() ─────────────────────────────────────────────────────

class TestAnalystFindingsRpc:
    def test_handle_returns_dict(self):
        """handle() should return a dict without crashing (may have empty findings)."""
        from backend.rpc.stats_analyst_findings import handle

        result = handle({})
        assert isinstance(result, dict)
        assert "total" in result
        assert "by_severity" in result
        assert "generated_at" in result

    def test_handle_by_severity_has_all_keys(self):
        """by_severity always has high/medium/low keys."""
        from backend.rpc.stats_analyst_findings import handle

        result = handle({})
        by_sev = result["by_severity"]
        assert "high" in by_sev
        assert "medium" in by_sev
        assert "low" in by_sev
        for sev in ("high", "medium", "low"):
            assert isinstance(by_sev[sev], list)
