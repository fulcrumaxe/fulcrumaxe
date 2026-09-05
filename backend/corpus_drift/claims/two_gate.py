"""executor.two_gate_evidence — fraction of executor PR bodies containing Gate 1 + Gate 2.

Fetches recent PRs from GitHub and checks whether both "Gate 1" and "Gate 2"
appear in the PR body.  Scoped to PRs produced by executor runs in the window
(matched via agent_runs.pr field, with fallback to recent PR list).

Enforcement boundary
--------------------
Two-gate verification was only required starting from PR #1142 (D#1147).
PRs merged before that boundary pre-date the rule, so including them would
inflate the failure count with false negatives.  The sample is therefore
windowed to PRs with number >= ENFORCEMENT_PR.

If fewer than 3 PRs exist at or above the boundary, the claim returns
status "n/a" to avoid drawing conclusions from an empty or near-empty sample.
"""

from __future__ import annotations

import logging
import subprocess
import json
from pathlib import Path
from typing import Any

# PR bodies live with the code, so this reads the CODE plane. CODE_REPO
# falls back to REPO, whose resolver raises rather than returning "", so
# it can never be empty here.
from backend._repo import CODE_REPO as _CODE_REPO
from backend.corpus_drift.types import ClaimResult

logger = logging.getLogger(__name__)

CLAIM_ID = "executor.two_gate_evidence"
ROLE_SCOPE = "executor"

# Two-gate verification was mandated starting with this PR.
# PRs below this number pre-date the rule and are excluded from the sample.
ENFORCEMENT_PR: int = 1142


def _fetch_pr_bodies(pr_numbers: list[int], limit: int) -> list[tuple[int, str]]:
    """Return [(pr_number, body), ...] for the given PR numbers (or recent PRs when empty).

    Uses gh CLI to fetch PR bodies.  Returns [] on any failure.
    """
    results: list[tuple[int, str]] = []

    if pr_numbers:
        # Fetch specific PRs
        for pr_num in pr_numbers[:limit]:
            try:
                out = subprocess.run(
                    [
                        "gh", "pr", "view", str(pr_num),
                        "--repo", _CODE_REPO,
                        "--json", "number,body",
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if out.returncode == 0:
                    data = json.loads(out.stdout)
                    results.append((data.get("number", pr_num), data.get("body", "") or ""))
            except Exception as exc:  # noqa: BLE001
                logger.debug("two_gate: failed to fetch PR #%s: %s", pr_num, exc)
        return results

    # Fall back: fetch recent merged PRs
    try:
        out = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", _CODE_REPO,
                "--state", "merged",
                "--limit", str(limit),
                "--json", "number,body",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0:
            prs = json.loads(out.stdout)
            for pr in prs:
                results.append((pr.get("number", 0), pr.get("body", "") or ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("two_gate: failed to fetch recent PRs: %s", exc)

    return results


def _has_two_gates(body: str) -> bool:
    """Return True when body contains both 'Gate 1' and 'Gate 2'."""
    return "Gate 1" in body and "Gate 2" in body


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    **_kwargs: Any,
) -> ClaimResult:
    """Evaluate what fraction of executor PR bodies contain Gate 1 + Gate 2 markers.

    Only PRs with number >= ENFORCEMENT_PR are included in the sample.
    PRs below that boundary pre-date the two-gate requirement (D#1147) and
    would generate false negatives if included.

    Returns status "n/a" when the post-enforcement sample has fewer than 3 PRs.

    Parameters
    ----------
    runs:
        Agent run rows for role="executor" in the window.
    transcripts_dir:
        Unused.
    window_days:
        Audit window in days.
    sample_cap:
        Maximum PRs to examine.
    """
    # Collect PR numbers from executor runs, filtered to enforcement boundary
    all_pr_numbers: list[int] = [
        int(r["pr"]) for r in runs
        if r.get("pr") and str(r["pr"]).isdigit()
    ]
    pr_numbers: list[int] = [n for n in all_pr_numbers if n >= ENFORCEMENT_PR]

    bodies = _fetch_pr_bodies(pr_numbers, sample_cap)

    # When using fallback (no specific PR numbers), filter fetched results too
    if not pr_numbers:
        bodies = [(num, body) for num, body in bodies if num >= ENFORCEMENT_PR]

    sample_size = len(bodies)

    if sample_size < 3:
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=sample_size,
            score=0.0,
            score_type="fraction",
            status="n/a",
            evidence=(
                f"fewer than 3 PRs at or above enforcement boundary #{ENFORCEMENT_PR}"
                if sample_size > 0
                else f"no executor PRs >= #{ENFORCEMENT_PR} found in window"
            ),
        )

    passing = 0
    last_fail_pr: int = 0
    for pr_num, body in bodies:
        if _has_two_gates(body):
            passing += 1
        else:
            last_fail_pr = pr_num

    score = passing / sample_size
    status = ClaimResult.classify_fraction(score, sample_size)

    if last_fail_pr:
        evidence = f"{passing}/{sample_size} PRs have both Gate markers; last missing: PR #{last_fail_pr}"
    else:
        evidence = f"all {sample_size} PRs contain Gate 1 and Gate 2 markers"

    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=sample_size,
        score=score,
        score_type="fraction",
        status=status,
        evidence=evidence,
    )
