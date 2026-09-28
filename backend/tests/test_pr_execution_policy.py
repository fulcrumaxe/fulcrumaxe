"""Tests for backend/pr_execution_policy.py (D#2644).

Every resolve() test injects a fake subprocess runner and never shells out to
a real `gh` login or the real pr_intake_gate.py / external_intake_gate.py
scripts on disk — only their documented exit-code contract is exercised.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from backend import pr_execution_policy as policy


def _stub_run(pr_exit=None, pr_raise=None, disc_exit=None, disc_raise=None):
    """Build a fake subprocess.run() that dispatches on which CLI script the
    command names, so a single stub can stand in for both intake gates.
    """

    def _run(cmd, **kwargs):
        script = cmd[1]
        if "pr_intake_gate.py" in script:
            if pr_raise is not None:
                raise pr_raise
            return SimpleNamespace(returncode=pr_exit, stdout="", stderr="")
        if "external_intake_gate.py" in script:
            if disc_raise is not None:
                raise disc_raise
            return SimpleNamespace(returncode=disc_exit, stdout="", stderr="")
        raise AssertionError(f"unexpected command in resolve(): {cmd}")

    return _run


# ---------------------------------------------------------------------------
# resolve() — the Spec's table, one row per test
# ---------------------------------------------------------------------------


def test_both_checks_confirmed_internal_is_host():
    mode, reason = policy.resolve(1, "o/r", 5, run=_stub_run(pr_exit=1, disc_exit=1))
    assert mode == policy.HOST
    assert reason


def test_internal_author_no_discussion_is_host():
    mode, reason = policy.resolve(1, "o/r", None, run=_stub_run(pr_exit=1))
    assert mode == policy.HOST
    assert reason


def test_out_of_trust_author_is_static_only():
    mode, reason = policy.resolve(1, "o/r", 5, run=_stub_run(pr_exit=0, disc_exit=1))
    assert mode == policy.STATIC_ONLY
    assert "pr_intake_gate" in reason


def test_external_discussion_label_is_static_only():
    mode, reason = policy.resolve(1, "o/r", 5, run=_stub_run(pr_exit=1, disc_exit=0))
    assert mode == policy.STATIC_ONLY
    assert "external_intake_gate" in reason


def test_pr_check_unreadable_is_static_only():
    mode, reason = policy.resolve(1, "o/r", 5, run=_stub_run(pr_exit=3, disc_exit=1))
    assert mode == policy.STATIC_ONLY
    assert "pr_intake_gate" in reason


def test_discussion_check_unreadable_is_static_only():
    mode, reason = policy.resolve(1, "o/r", 5, run=_stub_run(pr_exit=1, disc_exit=3))
    assert mode == policy.STATIC_ONLY
    assert "external_intake_gate" in reason


def test_discussion_check_drifted_is_static_only():
    mode, reason = policy.resolve(1, "o/r", 5, run=_stub_run(pr_exit=1, disc_exit=4))
    assert mode == policy.STATIC_ONLY
    assert "external_intake_gate" in reason


def test_pr_check_timeout_is_static_only():
    mode, reason = policy.resolve(
        1,
        "o/r",
        5,
        run=_stub_run(pr_raise=subprocess.TimeoutExpired(cmd="pr_intake_gate.py", timeout=60)),
    )
    assert mode == policy.STATIC_ONLY
    assert reason


def test_pr_check_missing_script_is_static_only():
    mode, reason = policy.resolve(
        1,
        "o/r",
        5,
        run=_stub_run(pr_raise=FileNotFoundError("no such file or directory")),
    )
    assert mode == policy.STATIC_ONLY
    assert reason


# ---------------------------------------------------------------------------
# normalize_mode() / host_execution_line() — fail-closed by construction
# ---------------------------------------------------------------------------


def test_normalize_mode_host_passes_through():
    assert policy.normalize_mode("host") == policy.HOST


def test_normalize_mode_anything_else_is_static_only():
    for bad in ("", "Host", "HOST", "static-only", "contained", "true", "1"):
        assert policy.normalize_mode(bad) == policy.STATIC_ONLY


def test_host_execution_line_renders_normalized_mode():
    assert policy.host_execution_line("host") == "HOST_EXECUTION: host"
    assert policy.host_execution_line("") == "HOST_EXECUTION: static-only"
    assert policy.host_execution_line("nonsense") == "HOST_EXECUTION: static-only"


# ---------------------------------------------------------------------------
# apply_host_execution() — the template post-processing helper
# ---------------------------------------------------------------------------


def _sample_body() -> str:
    return (
        "HOST_EXECUTION: __PR_HOST_EXECUTION_MODE__\n"
        "\n"
        "before\n"
        f"{policy.HOST_EXEC_BEGIN}\n"
        "run the real test suite here\n"
        f"{policy.HOST_EXEC_END}\n"
        "after\n"
    )


def test_apply_host_execution_host_mode_keeps_wrapped_content():
    out = policy.apply_host_execution(_sample_body(), "host", pr=42, pr_repo="o/r")
    assert "HOST_EXECUTION: host" in out
    assert "run the real test suite here" in out
    assert policy.HOST_EXEC_BEGIN in out
    assert policy.HOST_EXEC_END in out


def test_apply_host_execution_static_only_replaces_wrapped_content():
    out = policy.apply_host_execution(_sample_body(), "static-only", pr=42, pr_repo="o/r")
    assert "HOST_EXECUTION: static-only" in out
    assert "run the real test suite here" not in out
    assert "gh pr checks 42 --repo o/r" in out


def test_apply_host_execution_fail_closed_on_empty_mode():
    out = policy.apply_host_execution(_sample_body(), "", pr=42, pr_repo="o/r")
    assert "HOST_EXECUTION: static-only" in out
    assert "run the real test suite here" not in out


def test_apply_host_execution_no_markers_is_unchanged_besides_the_line():
    body = "HOST_EXECUTION: __PR_HOST_EXECUTION_MODE__\nno markers here at all\n"
    out = policy.apply_host_execution(body, "host", pr=1, pr_repo="o/r")
    assert out == "HOST_EXECUTION: host\nno markers here at all\n"
