"""project-manager.three_section_spec_used — % of post-#1132 Discussions with all 3 headers.

For each Discussion filed after the enforcement boundary, checks whether the
body contains all three required section headers:
  ## Intent
  ## Spec (Acceptance)
  ## Implementation Notes

Score = fraction of post-enforcement Discussions with all three headers present.
Healthy when ≥75% of Discussions in the window are fully structured.

All Discussion statuses (SPEC_READY, DONE, DISCUSSING, etc.) are included —
the three-section template applies at filing time, regardless of current status.
Filtering only on SPEC_READY would miss Discussions that have already moved to
DONE, which are the majority of post-enforcement filings.

Uses backend.discussion_status.missing_sections() for the header check.
Fetches Discussion bodies via GitHub CLI.

Enforcement boundary
--------------------
The three-section spec template was mandated starting with PR #1132 (D#1126,
merged 2026-05-19T16:03:36Z).  Discussions filed before that timestamp
pre-date the rule; including them would inflate the failure count with false
negatives.  The sample is therefore windowed to Discussions whose ``created_at``
is strictly after ENFORCEMENT_MERGED_AT.

If fewer than 3 Discussions exist in the post-enforcement window, the claim
returns status "n/a" to avoid drawing conclusions from an empty or near-empty
sample.

Scope: project-manager.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from backend._repo import REPO as _REPO
from backend._repo import REPO_OWNER as _REPO_OWNER, REPO_NAME as _REPO_NAME
from backend.corpus_drift.types import ClaimResult
from backend.discussion_status import missing_sections

logger = logging.getLogger(__name__)

CLAIM_ID = "project-manager.three_section_spec_used"
ROLE_SCOPE = "project-manager"

# Three-section spec template was mandated starting with this PR.
# Discussions filed before ENFORCEMENT_MERGED_AT pre-date the rule and are
# excluded from the sample.
ENFORCEMENT_PR: int = 1132
ENFORCEMENT_MERGED_AT: str = "2026-05-19T16:03:36Z"
_PAGE_SIZE = 50  # Discussions fetched per GraphQL page


def _enforcement_cutoff_ts() -> float:
    """Return ENFORCEMENT_MERGED_AT as a UTC POSIX timestamp."""
    return datetime.fromisoformat(
        ENFORCEMENT_MERGED_AT.replace("Z", "+00:00")
    ).timestamp()


def _fetch_post_enforcement_discussions(window_days: int, max_discussions: int) -> list[dict]:
    """Fetch all Discussions filed within window_days, regardless of status.

    The three-section template check applies to any Discussion filed after the
    enforcement boundary — DONE, SPEC_READY, DISCUSSING alike.  Filtering on
    SPEC_READY alone misses the majority of post-enforcement filings that have
    already been implemented.

    Returns a list of dicts with 'number', 'body', and 'created_at' keys.
    Falls back to empty list on any error.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - window_days * 86400
    results: list[dict] = []

    # Paginate through discussions newest-first
    cursor: str | None = None

    for _page in range(10):  # hard cap: 10 pages × 50 = 500 discussions
        after = f', after: "{cursor}"' if cursor else ""
        query = f"""
        query {{
          repository(owner:"{_REPO_OWNER}", name:"{_REPO_NAME}") {{
            discussions(first: {_PAGE_SIZE}{after}, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{
                number
                body
                createdAt
              }}
            }}
          }}
        }}
        """
        try:
            out = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode != 0:
                logger.debug("three_section_spec_used: graphql error: %s", out.stderr[:200])
                break
            data = json.loads(out.stdout)
            page = (
                data.get("data", {})
                .get("repository", {})
                .get("discussions", {})
            )
            nodes = page.get("nodes", [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("three_section_spec_used: fetch failed: %s", exc)
            break

        for node in nodes:
            body = node.get("body") or ""
            created_at = node.get("createdAt", "")

            # Stop paginating if we've gone past the window
            if created_at:
                try:
                    ts = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    ).timestamp()
                    if ts < cutoff:
                        return results
                except ValueError:
                    pass

            results.append({
                "number": node.get("number"),
                "body": body,
                "created_at": created_at,
            })

            if len(results) >= max_discussions:
                return results

        page_info = page.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return results


def evaluate(
    runs: list[dict[str, Any]],
    transcripts_dir: Path | None,
    window_days: int,
    sample_cap: int = 100,
    discussions: list[dict] | None = None,
    **_kwargs: Any,
) -> ClaimResult:
    """Evaluate fraction of post-#1132 Discussions with all 3 required section headers.

    All Discussions (any status) filed after ENFORCEMENT_MERGED_AT (PR #1132, the
    three-section template merge) are included.  Filtering only on SPEC_READY would
    exclude the majority of post-enforcement Discussions that have already moved to
    DONE.  Discussions predating the boundary were filed before the rule existed and
    are excluded to avoid inflating the failure count.

    Returns status "n/a" when the post-enforcement sample has fewer than 3 Discussions.

    Parameters
    ----------
    runs:
        Agent run rows for role="project-manager" — unused, Discussion list comes
        from GitHub API directly.
    transcripts_dir:
        Unused.
    window_days:
        Audit window in days.
    sample_cap:
        Max Discussions to examine.
    discussions:
        Override for testing — inject a list of {'number': N, 'body': str,
        'created_at': str} dicts instead of fetching from GitHub.
    """
    if discussions is None:
        discussions = _fetch_post_enforcement_discussions(window_days, sample_cap)

    # Window to post-enforcement Discussions only.
    enforcement_ts = _enforcement_cutoff_ts()
    post_enforcement: list[dict] = []
    for disc in discussions:
        created_at = disc.get("created_at", "")
        if created_at:
            try:
                ts = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                ).timestamp()
                if ts <= enforcement_ts:
                    continue  # pre-dates the three-section template rule
            except ValueError:
                pass  # unparseable timestamp → include conservatively
        post_enforcement.append(disc)

    sample_size = len(post_enforcement)

    if sample_size < 3:
        return ClaimResult(
            claim_id=CLAIM_ID,
            role_scope=ROLE_SCOPE,
            sample_size=sample_size,
            score=0.0,
            score_type="fraction",
            status="n/a",
            evidence=(
                f"fewer than 3 Discussions filed after enforcement boundary "
                f"(PR #{ENFORCEMENT_PR})"
                if sample_size > 0
                else f"no Discussions filed after PR #{ENFORCEMENT_PR} found in {window_days}d window"
            ),
        )

    discussions = post_enforcement

    passing = 0
    last_missing_disc: int | None = None

    for disc in discussions:
        body = disc.get("body") or ""
        missing = missing_sections(body)
        if not missing:
            passing += 1
        else:
            last_missing_disc = disc.get("number")
            logger.debug(
                "three_section_spec_used: D#%s missing: %s",
                disc.get("number"), missing,
            )

    score = passing / sample_size
    status = ClaimResult.classify_fraction(score, sample_size)

    if last_missing_disc:
        evidence = (
            f"{passing}/{sample_size} post-#1132 Discussions have all 3 sections; "
            f"last missing: D#{last_missing_disc}"
        )
    else:
        evidence = f"all {sample_size} post-#1132 Discussions contain all 3 section headers"

    return ClaimResult(
        claim_id=CLAIM_ID,
        role_scope=ROLE_SCOPE,
        sample_size=sample_size,
        score=score,
        score_type="fraction",
        status=status,
        evidence=evidence,
    )
