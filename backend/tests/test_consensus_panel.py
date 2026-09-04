"""Tests for backend/consensus_panel.py — panel config, synthesis, and CLI."""

from __future__ import annotations
import json
import sys
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.consensus_panel import (
    PANEL_TOKEN_CAP, SPECIALIST_TOKEN_CAP, ConsensusResult, SpecialistOutput,
    check_budget, extract_tag, get_panel_for_title, main,
    panel_requires_consensus, parse_specialist_output, round2_needed,
)


class TestExtractTag:
    def test_critical(self): assert extract_tag("[Critical] PM") == "critical"
    def test_feature(self): assert extract_tag("[Feature] Add KPI") == "feature"
    def test_small(self): assert extract_tag("[Small] typo") == "small"
    def test_bug(self): assert extract_tag("[Bug] crash") == "bug"
    def test_doc(self): assert extract_tag("[Doc] update") == "doc"
    def test_process(self): assert extract_tag("[Process] refine") == "process"
    def test_no_tag(self): assert extract_tag("Untitled") == "small"
    def test_case_insensitive(self): assert extract_tag("[CRITICAL] urgent") == "critical"


class TestGetPanelForTitle:
    def test_critical_panel(self):
        cfg = get_panel_for_title("[Critical] some issue")
        assert set(cfg.specialists) == {"technical-architect", "security-expert", "cost-analyst"}
        assert cfg.mandatory is True

    def test_feature_panel(self):
        cfg = get_panel_for_title("[Feature] new feature")
        assert set(cfg.specialists) == {"technical-architect", "product-owner", "performance-expert"}
        assert cfg.mandatory is True

    def test_small_no_panel(self):
        cfg = get_panel_for_title("[Small] tiny")
        assert cfg.specialists == [] and cfg.mandatory is False

    def test_bug_no_panel(self):
        cfg = get_panel_for_title("[Bug] crash")
        assert cfg.specialists == [] and cfg.mandatory is False

    def test_process_panel(self):
        cfg = get_panel_for_title("[Process] refine spawn policy")
        assert "technical-architect" in cfg.specialists and "product-owner" in cfg.specialists

    def test_token_caps(self):
        cfg = get_panel_for_title("[Critical] anything")
        assert cfg.per_specialist_token_cap == SPECIALIST_TOKEN_CAP
        assert cfg.total_token_cap == PANEL_TOKEN_CAP


class TestPanelRequiresConsensus:
    def test_critical_requires(self): assert panel_requires_consensus("[Critical]") is True
    def test_feature_requires(self): assert panel_requires_consensus("[Feature]") is True
    def test_small_does_not(self): assert panel_requires_consensus("[Small]") is False
    def test_bug_does_not(self): assert panel_requires_consensus("[Bug]") is False


class TestCheckBudget:
    def test_allows_under_cap(self):
        s = check_budget("[Critical] test", 0)
        assert s["allowed"] is True and s["remaining"] == PANEL_TOKEN_CAP

    def test_blocks_over_cap(self):
        assert check_budget("[Critical] test", PANEL_TOKEN_CAP + 1)["allowed"] is False

    def test_includes_specialists(self):
        assert "technical-architect" in check_budget("[Critical] test")["specialists"]


class TestParseSpecialistOutput:
    _RAW = (
        "### perspective\nModular design recommended.\n\n"
        "### concerns\nMissing error handling.\n\n"
        "### questions\nShould X be async or sync?\n"
    )

    def test_parses_all_fields(self):
        out = parse_specialist_output("technical-architect", 42, 1, self._RAW)
        assert "Modular" in out.perspective
        assert "error handling" in out.concerns
        assert "async" in out.questions
        assert out.role == "technical-architect"
        assert out.discussion == 42 and out.panel_round == 1

    def test_empty_sections_ok(self):
        out = parse_specialist_output("cost-analyst", 42, 1, "no sections here")
        assert out.perspective == "" and out.concerns == "" and out.questions == ""


class TestRound2Needed:
    def test_true_when_questions(self):
        outs = [SpecialistOutput("technical-architect", 1, 1, questions="Should X be Y?")]
        assert round2_needed(outs) is True

    def test_false_when_no_questions(self):
        outs = [SpecialistOutput("technical-architect", 1, 1, questions="")]
        assert round2_needed(outs) is False


class TestConsensusResult:
    def test_summary_block(self):
        result = ConsensusResult(
            discussion=42, tag="critical",
            panel=["technical-architect", "security-expert"],
            rounds_run=1,
            outputs=[
                SpecialistOutput("technical-architect", 42, 1, perspective="Modular design."),
                SpecialistOutput("security-expert", 42, 1, perspective="Input validation needed."),
            ],
            disagreements=["tech-arch prefers sync; security prefers async"],
            summary="Chose async per security-expert.",
        )
        block = result.to_summary_block()
        assert "### Consensus Summary" in block
        assert "technical-architect" in block and "security-expert" in block
        assert "Round 2 run: No" in block

    def test_empty_panel_produces_valid_block(self):
        assert "### Consensus Summary" in ConsensusResult(7, "small", [], 0).to_summary_block()


class TestCLI:
    def test_get_panel_critical(self, capsys):
        main(["get-panel", "--title", "[Critical] urgent"])
        data = json.loads(capsys.readouterr().out)
        assert data["tag"] == "critical" and data["mandatory"] is True
        assert "technical-architect" in data["specialists"]

    def test_get_panel_small(self, capsys):
        main(["get-panel", "--title", "[Small] tiny"])
        assert json.loads(capsys.readouterr().out)["specialists"] == []

    def test_check_budget_allowed(self, capsys):
        main(["check-budget", "--panel", "[Feature] test"])
        assert json.loads(capsys.readouterr().out)["allowed"] is True

    def test_check_budget_exceeded(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["check-budget", "--panel", "[Feature] test", "--spent", str(PANEL_TOKEN_CAP + 1)])
        assert exc_info.value.code == 1

    def test_list_roles(self, capsys):
        main(["list-roles"])
        out = capsys.readouterr().out
        for role in ["technical-architect", "security-expert", "cost-analyst",
                     "product-owner", "performance-expert"]:
            assert role in out
