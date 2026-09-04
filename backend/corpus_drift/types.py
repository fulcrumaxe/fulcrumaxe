"""backend/corpus_drift/types.py — shared data types for corpus drift claims.

Each claim evaluator returns a ClaimResult.  The report renderer in report.py
consumes only these types — no claim-specific logic leaks into the renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ClaimResult:
    """Result from evaluating one claim against a sample of runs.

    Fields
    ------
    claim_id:
        Dot-separated identifier, e.g. "code-reviewer.pytest_invoked".
    role_scope:
        Role(s) this claim applies to, e.g. "code-reviewer" or "global".
    sample_size:
        Number of runs (transcripts / PR bodies) examined.
    score:
        Float in [0.0, 1.0] for fraction-based claims; raw integer count for
        count-based claims (e.g. global.archive_protocol_honored).
    score_type:
        "fraction" or "count".
    status:
        One of "healthy" | "watch" | "drift" | "n/a".
        "n/a" is used when sample_size < MIN_SAMPLE (default 5).
    evidence:
        One-line pointer to evidence — last run id where claim failed, or
        "all passing" / "no data", etc.
    notes:
        Optional supplementary notes (e.g. why n/a).
    """

    claim_id: str
    role_scope: str
    sample_size: int
    score: float | int
    score_type: str  # "fraction" | "count"
    status: str  # "healthy" | "watch" | "drift" | "n/a"
    evidence: str
    notes: str = ""

    # Thresholds applied when score_type == "fraction"
    HEALTHY_THRESHOLD: float = field(default=0.75, repr=False, compare=False)
    WATCH_THRESHOLD: float = field(default=0.50, repr=False, compare=False)
    MIN_SAMPLE: int = field(default=5, repr=False, compare=False)

    def score_display(self) -> str:
        """Human-readable score string."""
        if self.score_type == "fraction":
            return f"{self.score * 100:.0f}%"
        return str(int(self.score))

    @staticmethod
    def classify_fraction(score: float, sample_size: int) -> str:
        """Return status label for a fraction-based claim."""
        if sample_size < 5:
            return "n/a"
        if score >= 0.75:
            return "healthy"
        if score >= 0.50:
            return "watch"
        return "drift"

    @staticmethod
    def classify_count(count: int, sample_size: int) -> str:
        """Return status label for a count-based claim (pass = 0)."""
        if sample_size < 5:
            return "n/a"
        if count == 0:
            return "healthy"
        return "drift"
