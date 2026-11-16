"""Runs tests/test_engine_emit_callers.sh so the shell checks are collected with the rest."""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def test_instrumented_callers_under_errexit():
    r = subprocess.run(["bash", os.path.join(HERE, "test_engine_emit_callers.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
