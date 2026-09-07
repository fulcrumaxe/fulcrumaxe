"""Tests for backend/flaky_sentinel.py."""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Each test gets its own flaky-history.jsonl in a temp dir."""
    hist = tmp_path / "flaky-history.jsonl"
    monkeypatch.setenv("FLAKY_HISTORY_PATH", str(hist))
    # Re-import to pick up env var via _history_path()
    import importlib
    import backend.flaky_sentinel as mod
    importlib.reload(mod)
    yield hist
    # env var cleanup handled by monkeypatch fixture


# ---------------------------------------------------------------------------
# Import under test (after env var is set by fixture)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fs(isolated_history):
    import backend.flaky_sentinel as mod
    return mod


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


def test_record_creates_file(fs, isolated_history):
    fs.record("suite-a", 0)
    assert isolated_history.exists()


def test_record_returns_dict_with_fields(fs):
    row = fs.record("suite-a", 1)
    assert row["test_id"] == "suite-a"
    assert row["exit_code"] == 1
    assert row["passed"] is False
    assert "ts" in row


def test_record_pass_sets_passed_true(fs):
    row = fs.record("suite-a", 0)
    assert row["passed"] is True


def test_record_appends_multiple(fs, isolated_history):
    fs.record("suite-a", 0)
    fs.record("suite-a", 1)
    fs.record("suite-a", 0)
    lines = [l for l in isolated_history.read_text().splitlines() if l.strip()]
    assert len(lines) == 3


def test_record_accepts_custom_ts(fs):
    t = 1_700_000_000.0
    row = fs.record("suite-a", 0, ts=t)
    assert row["ts"] == t


# ---------------------------------------------------------------------------
# flake_score()
# ---------------------------------------------------------------------------


def test_flake_score_always_pass_is_zero(fs):
    for _ in range(5):
        fs.record("suite-pass", 0)
    assert fs.flake_score("suite-pass") == 0.0


def test_flake_score_always_fail_is_zero(fs):
    for _ in range(5):
        fs.record("suite-fail", 1)
    assert fs.flake_score("suite-fail") == 0.0


def test_flake_score_fail_then_pass_is_positive(fs):
    fs.record("suite-flaky", 1)   # fail
    fs.record("suite-flaky", 0)   # pass  → 1 transition
    score = fs.flake_score("suite-flaky")
    assert score > 0.0


def test_flake_score_single_run_is_zero(fs):
    fs.record("suite-a", 0)
    assert fs.flake_score("suite-a") == 0.0


def test_flake_score_empty_history_is_zero(fs):
    assert fs.flake_score("nonexistent") == 0.0


def test_flake_score_multiple_transitions(fs):
    # fail pass fail pass → 2 transitions out of 3 pairs = 0.666...
    for ec in [1, 0, 1, 0]:
        fs.record("suite-multi", ec)
    score = fs.flake_score("suite-multi")
    assert abs(score - 2 / 3) < 1e-9


def test_flake_score_window_bounded(fs):
    # Insert 25 always-pass runs, then 1 fail followed by 1 pass.
    # The window (20) should still pick up the transition at the end.
    for _ in range(25):
        fs.record("suite-w", 0)
    fs.record("suite-w", 1)
    fs.record("suite-w", 0)
    score = fs.flake_score("suite-w")
    assert score > 0.0


# ---------------------------------------------------------------------------
# is_quarantined()
# ---------------------------------------------------------------------------


def test_is_quarantined_unknown_test_false(fs):
    assert fs.is_quarantined("unknown-suite") is False


def test_is_quarantined_true_for_real_flake(fs):
    # fail, pass, fail, pass — 4 runs, 2 transitions -> score 2/3 >= 0.3
    for ec in [1, 0, 1, 0]:
        fs.record("ctl/flaky", ec)
    assert fs.is_quarantined("ctl/flaky") is True


def test_is_quarantined_false_for_steady_suite(fs):
    for _ in range(6):
        fs.record("ctl/steady", 0)
    assert fs.is_quarantined("ctl/steady") is False
    assert fs.flake_score("ctl/steady") == 0.0


def test_is_quarantined_false_below_min_runs(fs):
    # A single fail->pass reads as flake_score 1.0 but only 2 runs — noise,
    # not signal, and must not quarantine.
    fs.record("suite-short", 1)
    fs.record("suite-short", 0)
    assert fs.flake_score("suite-short") > 0.0
    assert fs.is_quarantined("suite-short") is False


# ---------------------------------------------------------------------------
# list_tests()
# ---------------------------------------------------------------------------


def test_list_tests_empty(fs):
    assert fs.list_tests() == []


def test_list_tests_returns_one_row_per_test(fs):
    fs.record("a", 0)
    fs.record("b", 1)
    fs.record("a", 1)
    rows = fs.list_tests()
    test_ids = {r["test_id"] for r in rows}
    assert test_ids == {"a", "b"}


def test_list_tests_row_has_required_fields(fs):
    fs.record("a", 0)
    row = fs.list_tests()[0]
    for field in ("test_id", "runs", "flake_score", "quarantined", "last_exit_code", "last_ts"):
        assert field in row, f"missing field: {field}"


def test_list_tests_run_count(fs):
    for _ in range(3):
        fs.record("a", 0)
    rows = {r["test_id"]: r for r in fs.list_tests()}
    assert rows["a"]["runs"] == 3


# ---------------------------------------------------------------------------
# _canonical_id()
# ---------------------------------------------------------------------------


def test_canonical_id_strips_reporting_flags(fs):
    a = fs._canonical_id("python3 -m pytest tests/ backend/tests/ -x -q")
    b = fs._canonical_id("python3 -m pytest tests/ backend/tests/ -q")
    assert a == b


def test_canonical_id_preserves_target_paths(fs):
    a = fs._canonical_id("python3 -m pytest tests/ -q")
    b = fs._canonical_id("python3 -m pytest tests/ backend/tests/ -q")
    assert a != b


def test_canonical_id_collapses_whitespace(fs):
    a = fs._canonical_id("python3 -m pytest  tests/ -x -q")
    b = fs._canonical_id("python3 -m pytest tests/ -q")
    assert a == b


def test_canonical_id_leaves_plain_bash_ids_alone(fs):
    raw = "bash tests/test_foo.sh"
    assert fs._canonical_id(raw) == raw


# ---------------------------------------------------------------------------
# list_tests() / flake_score() — canonicalized grouping
# ---------------------------------------------------------------------------


def test_list_tests_groups_flag_spelling_variants(fs):
    for _ in range(2):
        fs.record("python3 -m pytest tests/ backend/tests/ -x -q", 0)
    for _ in range(2):
        fs.record("python3 -m pytest tests/ backend/tests/ -q", 0)
    rows = fs.list_tests()
    matching = [r for r in rows if "backend/tests/" in r["test_id"]]
    assert len(matching) == 1
    assert matching[0]["runs"] == 4


def test_flake_score_sees_across_flag_variants(fs):
    fs.record("python3 -m pytest tests/ -x -q", 1)   # fail
    fs.record("python3 -m pytest tests/ -q", 0)       # pass, different spelling
    score = fs.flake_score("python3 -m pytest tests/ -q")
    assert score > 0.0


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


def test_report_states_rows_and_ids_examined(fs):
    fs.record("a", 0)
    fs.record("b", 1)
    data = fs.report()
    assert data["rows_examined"] == 2
    assert data["ids_examined"] == 2


def test_report_flaky_list_positive_and_negative_control(fs):
    for ec in [1, 0, 1, 0]:
        fs.record("ctl/flaky", ec)
    for _ in range(6):
        fs.record("ctl/steady", 0)
    data = fs.report()
    flaky_ids = {t["test_id"] for t in data["flaky"]}
    assert "ctl/flaky" in flaky_ids
    assert "ctl/steady" not in flaky_ids


def test_report_ids_examined_less_than_raw_when_flags_collapse(fs):
    fs.record("python3 -m pytest tests/ backend/tests/ -x -q", 0)
    fs.record("python3 -m pytest tests/ backend/tests/ -q", 0)
    fs.record("python3 -m pytest tests/ -q", 0)
    raw_distinct = 3
    data = fs.report()
    assert data["ids_examined"] < raw_distinct


# ---------------------------------------------------------------------------
# CLI — all subcommands use named flags
# ---------------------------------------------------------------------------


def test_cli_record(fs):
    rc = fs._cli(["record", "--test-id", "my-suite", "--exit-code", "0"])
    assert rc == 0


def test_cli_flake_score(fs, capsys):
    fs.record("my-suite", 1)
    fs.record("my-suite", 0)
    fs._cli(["flake-score", "--test-id", "my-suite"])
    out = capsys.readouterr().out.strip()
    score = float(out)
    assert score > 0.0


def test_cli_list_json(fs, capsys):
    fs.record("a", 0)
    fs._cli(["list", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed[0]["test_id"] == "a"


def test_cli_is_quarantined_json(fs, capsys):
    fs.record("s", 1)
    fs.record("s", 0)
    fs._cli(["is-quarantined", "--test-id", "s", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["test_id"] == "s"
    assert parsed["flake_score"] > 0.0


def test_cli_list_text(fs, capsys):
    fs.record("a", 0)
    fs._cli(["list"])
    out = capsys.readouterr().out
    assert "a" in out


def test_cli_is_quarantined_text(fs, capsys):
    fs.record("suite-x", 0)
    fs._cli(["is-quarantined", "--test-id", "suite-x"])
    out = capsys.readouterr().out
    assert "suite-x" in out
    assert "flake_score" in out


def test_cli_report_json(fs, capsys):
    fs.record("a", 0)
    fs.record("a", 1)
    fs._cli(["report", "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["rows_examined"] == 2
    assert parsed["ids_examined"] == 1
    assert "flaky" in parsed and "tests" in parsed


def test_cli_advise_stderr_only_when_flaky(fs, capsys):
    for ec in [1, 0, 1, 0]:
        fs.record("ctl/flaky", ec)
    fs._cli(["advise", "--test-id", "ctl/flaky"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "flaky" in captured.err
    assert "ctl/flaky" in captured.err


def test_cli_advise_silent_when_not_flaky(fs, capsys):
    fs.record("ctl/steady", 0)
    fs._cli(["advise", "--test-id", "ctl/steady"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
