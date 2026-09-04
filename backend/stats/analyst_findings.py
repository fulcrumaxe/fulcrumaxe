"""backend/stats/analyst_findings.py — Load latest run-analyst findings from disk.

Reads the most-recent run-report JSON from .autonomous-team/run-reports/,
returns findings grouped by severity with evidence refs (file/PR/discussion).

Report shape (written by backend/run_analyst.py::write_report):
    {
      "report_at": "<ISO>",
      "window": {"since": "<ISO>", "until": "<ISO>"},
      "runs_analyzed": <int>,
      "findings": [
        {
          "category": str,
          "severity": "high" | "medium" | "low",
          "title": str,
          "evidence": [str, ...],
          "suggested_discussion_title": str,
          "suggested_tag": str
        },
        ...
      ]
    }

Read-only: never writes files, never modifies state.

The report directory is resolved via the REPO_ROOT constant in run_analyst.py
(which is always the repo root, not the state dir). We mirror that same path
resolution here via the actual repo root, NOT state_paths, because run-reports
live in .autonomous-team/ inside the repo tree.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Mirror run_analyst.py: RUN_REPORTS_DIR = REPO_ROOT / ".autonomous-team" / "run-reports"
# We derive REPO_ROOT as the parent of the backend package.
_BACKEND_DIR = Path(__file__).parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
RUN_REPORTS_DIR: Path = _REPO_ROOT / ".autonomous-team" / "run-reports"

SEVERITY_ORDER = ("high", "medium", "low")


def _latest_report(reports_dir: Path) -> dict[str, Any] | None:
    """Return the parsed JSON of the most recent report file, or None."""
    if not reports_dir.is_dir():
        return None

    json_files = sorted(reports_dir.glob("*.json"), reverse=True)
    if not json_files:
        return None

    latest = json_files[0]
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("analyst_findings: could not read %s: %s", latest, exc)
        return None


def load(reports_dir: Path | None = None) -> dict[str, Any]:
    """Load latest run-analyst findings grouped by severity.

    Parameters
    ----------
    reports_dir:
        Override the default report directory (used in tests).

    Returns
    -------
    dict with keys:
        "report_at": ISO-8601 string of when the report was generated, or None.
        "window": dict with "since"/"until" ISO strings, or None.
        "runs_analyzed": int.
        "by_severity": dict mapping "high"/"medium"/"low" to a list of finding dicts.
        "total": int — total number of findings.
        "generated_at": ISO-8601 string of when this response was built.
        "error": str or None.
    """
    generated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    dir_path = reports_dir if reports_dir is not None else RUN_REPORTS_DIR

    empty: dict[str, Any] = {
        "report_at": None,
        "window": None,
        "runs_analyzed": 0,
        "by_severity": {s: [] for s in SEVERITY_ORDER},
        "total": 0,
        "generated_at": generated_at,
        "error": None,
    }

    report = _latest_report(dir_path)
    if report is None:
        return empty

    findings: list[dict[str, Any]] = report.get("findings") or []

    by_severity: dict[str, list[dict[str, Any]]] = {s: [] for s in SEVERITY_ORDER}
    for finding in findings:
        sev = finding.get("severity", "low")
        if sev not in by_severity:
            sev = "low"
        by_severity[sev].append({
            "category": finding.get("category", ""),
            "severity": sev,
            "title": finding.get("title", ""),
            "evidence": finding.get("evidence") or [],
            "suggested_discussion_title": finding.get("suggested_discussion_title", ""),
            "suggested_tag": finding.get("suggested_tag", ""),
        })

    return {
        "report_at": report.get("report_at"),
        "window": report.get("window"),
        "runs_analyzed": report.get("runs_analyzed", 0),
        "by_severity": by_severity,
        "total": len(findings),
        "generated_at": generated_at,
        "error": None,
    }
