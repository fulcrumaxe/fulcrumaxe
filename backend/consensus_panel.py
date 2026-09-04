"""
Consensus panel config and synthesis schema for the PM consensus protocol.

Panel compositions, token-budget guardrails, and specialist output schema.

Usage:
    python3 backend/consensus_panel.py get-panel --title "[Critical] my title"
    python3 backend/consensus_panel.py check-budget --panel "[Feature] my title"
    python3 backend/consensus_panel.py list-roles
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Token caps
# ---------------------------------------------------------------------------

SPECIALIST_TOKEN_CAP = 100_000   # per specialist per round
PANEL_TOKEN_CAP = 200_000        # total for all specialists + all rounds

# ---------------------------------------------------------------------------
# Panel configs: tag → specialist roles
# ---------------------------------------------------------------------------

PANEL_BY_TAG: dict[str, list[str]] = {
    "critical": ["technical-architect", "security-expert", "cost-analyst"],
    "feature":  ["technical-architect", "product-owner",   "performance-expert"],
    "process":  ["technical-architect", "product-owner"],
    "small": [], "bug": [], "doc": [],
}

SPECIALIST_ROLES = frozenset({
    "technical-architect", "product-owner", "cost-analyst",
    "performance-expert",  "security-expert",
})

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PanelConfig:
    tag: str
    specialists: list[str]
    mandatory: bool
    max_rounds: int = 2
    per_specialist_token_cap: int = SPECIALIST_TOKEN_CAP
    total_token_cap: int = PANEL_TOKEN_CAP


@dataclass
class SpecialistOutput:
    """Parsed output from a single specialist in a panel round."""
    role: str
    discussion: int
    panel_round: int
    perspective: str = ""
    concerns: str = ""
    questions: str = ""
    raw_response: str = ""
    verdict: str = "pass"


@dataclass
class ConsensusResult:
    """Aggregated result from one or more panel rounds."""
    discussion: int
    tag: str
    panel: list[str]
    rounds_run: int
    outputs: list[SpecialistOutput] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    summary: str = ""

    def to_summary_block(self) -> str:
        """Render the ### Consensus Summary block for the Discussion body."""
        panel_str = ", ".join(self.panel) if self.panel else "(none)"
        round2 = "Yes" if self.rounds_run >= 2 else "No"
        lines = [
            "### Consensus Summary", "",
            f"Panel: {panel_str}",
            f"Round 2 run: {round2}", "",
        ]
        for out in self.outputs:
            short = (out.perspective or "").split("\n")[0][:120]
            lines.append(f"**{out.role}**: {short}")
        disagreements = self.disagreements or ["None"]
        lines += ["", f"**Resolved disagreements**: {'; '.join(disagreements)}"]
        if self.summary:
            lines += ["", f"**Spec decisions informed by panel**: {self.summary}"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r'\[(\w+)\]', re.IGNORECASE)
_SEC_RE = re.compile(r'###\s+{}\s*\n(.*?)(?=###|\Z)', re.IGNORECASE | re.DOTALL)


def extract_tag(title: str) -> str:
    m = _TAG_RE.search(title)
    return m.group(1).lower() if m else "small"


def get_panel_for_title(title: str) -> PanelConfig:
    tag = extract_tag(title)
    specialists = PANEL_BY_TAG.get(tag, [])
    return PanelConfig(tag=tag, specialists=specialists, mandatory=tag in ("critical", "feature"))


def panel_requires_consensus(title: str) -> bool:
    cfg = get_panel_for_title(title)
    return cfg.mandatory and len(cfg.specialists) > 0


def check_budget(title: str, spent_tokens: int = 0) -> dict:
    cfg = get_panel_for_title(title)
    return {
        "allowed": spent_tokens < PANEL_TOKEN_CAP,
        "remaining": PANEL_TOKEN_CAP - spent_tokens,
        "cap": PANEL_TOKEN_CAP,
        "specialists": cfg.specialists,
        "per_specialist_cap": SPECIALIST_TOKEN_CAP,
    }


def parse_specialist_output(
    role: str, discussion: int, panel_round: int, raw: str,
) -> SpecialistOutput:
    def _extract(section: str) -> str:
        m = re.compile(
            rf'###\s+{section}\s*\n(.*?)(?=###|\Z)', re.IGNORECASE | re.DOTALL
        ).search(raw)
        return m.group(1).strip() if m else ""

    return SpecialistOutput(
        role=role, discussion=discussion, panel_round=panel_round,
        perspective=_extract("perspective"),
        concerns=_extract("concerns"),
        questions=_extract("questions"),
        raw_response=raw,
    )


def round2_needed(outputs: list[SpecialistOutput]) -> bool:
    """Return True if any specialist has non-empty questions (signals disagreement)."""
    return any(out.questions.strip() for out in outputs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Consensus panel configuration CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get-panel")
    p.add_argument("--title", required=True)

    p = sub.add_parser("check-budget")
    p.add_argument("--panel", required=True)
    p.add_argument("--spent", type=int, default=0)

    sub.add_parser("list-roles")

    args = parser.parse_args(argv)

    if args.cmd == "get-panel":
        cfg = get_panel_for_title(args.title)
        print(json.dumps({
            "tag": cfg.tag, "specialists": cfg.specialists, "mandatory": cfg.mandatory,
            "per_specialist_token_cap": cfg.per_specialist_token_cap,
            "total_token_cap": cfg.total_token_cap,
        }, indent=2))
    elif args.cmd == "check-budget":
        status = check_budget(args.panel, args.spent)
        print(json.dumps(status, indent=2))
        if not status["allowed"]:
            sys.exit(1)
    elif args.cmd == "list-roles":
        for role in sorted(SPECIALIST_ROLES):
            print(role)


if __name__ == "__main__":
    main()
