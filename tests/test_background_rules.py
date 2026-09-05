"""tests/test_background_rules.py

Unit tests for hooks/background_rules.py (D#2070).

classify_background is a pure function: no subprocess, no file I/O, no env
reads. These tests exercise it directly rather than through the hook, so
they stay fast unit tests.

Run with:
    python3 -m pytest tests/test_background_rules.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.background_rules import classify_background


def test_run_in_background_true_is_denied():
    decision = classify_background({"command": "x", "run_in_background": True})
    assert decision.allow is False


def test_run_in_background_false_is_allowed():
    decision = classify_background({"command": "x", "run_in_background": False})
    assert decision.allow is True


def test_missing_key_is_allowed():
    decision = classify_background({})
    assert decision.allow is True


def test_command_only_no_background_key_is_allowed():
    decision = classify_background({"command": "x"})
    assert decision.allow is True


def test_reason_is_nonempty_and_names_timeout():
    decision = classify_background({"run_in_background": True})
    assert decision.reason != ""
    assert "timeout" in decision.reason


def test_allowed_decision_has_empty_reason():
    decision = classify_background({"run_in_background": False})
    assert decision.reason == ""


# ---------------------------------------------------------------------------
# D#2248 — shell-level backgrounding inside the command string. Same trap as
# run_in_background:true (nothing re-invokes the sub-agent), different
# spelling. Measured from a worktree cwd: all four denied here previously
# ALLOWed.
# ---------------------------------------------------------------------------

def test_trailing_ampersand_is_denied():
    decision = classify_background({"command": "pytest tests/ &"})
    assert decision.allow is False


def test_nohup_redirected_with_trailing_ampersand_is_denied():
    decision = classify_background(
        {"command": "nohup pytest tests/ > out.log 2>&1 &"}
    )
    assert decision.allow is False


def test_setsid_is_denied():
    decision = classify_background({"command": "setsid bash run.sh &"})
    assert decision.allow is False


def test_trailing_ampersand_disown_is_denied():
    decision = classify_background(
        {"command": "pytest tests/ > out.log 2>&1 & disown"}
    )
    assert decision.allow is False


# ---------------------------------------------------------------------------
# Negative corpus (Spec criterion 2) — none of these are shell backgrounding
# and all must stay allowed. The dominant risk here is over-blocking, not
# under-blocking (CLAUDE.md hooks/ scoring rule).
# ---------------------------------------------------------------------------

def test_double_ampersand_chain_is_allowed():
    decision = classify_background({"command": "make lint && make test"})
    assert decision.allow is True


def test_foreground_redirect_with_stderr_merge_is_allowed():
    decision = classify_background({"command": "pytest tests/ > out.log 2>&1"})
    assert decision.allow is True


def test_quoted_url_ampersand_in_double_quotes_is_allowed():
    decision = classify_background(
        {"command": 'gh api "repos/o/r/issues?a=1&b=2"'}
    )
    assert decision.allow is True


def test_quoted_ampersand_in_grep_pattern_is_allowed():
    decision = classify_background(
        {"command": 'grep "foo & bar" file.txt'}
    )
    assert decision.allow is True


def test_stdout_stderr_redirect_operator_is_allowed():
    decision = classify_background({"command": "echo hi &> out.log"})
    assert decision.allow is True
