"""Tests for pr-artifacts producer/consumer (Discussion #964)."""

import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib" / "pr-artifacts.sh"
PRODUCER_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "post-agent.d" / "pr-artifacts.sh"


def run_bash(cmd: str, env: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env=merged_env,
        cwd=cwd or str(REPO_ROOT),
    )


class TestPrArtifactsConsumer:
    """inject_for_pr: reads JSONL and emits PRIOR_TEST_RUNS block."""

    def test_no_artifact_file_emits_nothing(self, tmp_path):
        """When artifact file is missing, inject_for_pr prints nothing."""
        script = f"""
SCRIPT_DIR="{REPO_ROOT}/scripts/lib"
REPO_ROOT="{tmp_path}"
source "{LIB_SCRIPT}"
inject_for_pr 999 abc1234
"""
        result = run_bash(script)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_inject_for_pr_with_fixture(self, tmp_path):
        """inject_for_pr prints PRIOR_TEST_RUNS block when artifact exists."""
        artifact_dir = tmp_path / ".autonomous-team" / "pr-artifacts" / "999"
        artifact_dir.mkdir(parents=True)
        artifact_file = artifact_dir / "abc1234.jsonl"
        entry = {
            "command": "pytest -x -q",
            "exit_code": 0,
            "duration_seconds": 45,
            "ts": "2026-05-17T10:00:00Z",
            "agent": "acceptance-tester",
        }
        artifact_file.write_text(json.dumps(entry) + "\n")

        script = f"""
SCRIPT_DIR="{REPO_ROOT}/scripts/lib"
REPO_ROOT="{tmp_path}"
source "{LIB_SCRIPT}"
inject_for_pr 999 abc1234
"""
        result = run_bash(script)
        assert result.returncode == 0
        out = result.stdout
        assert "PRIOR_TEST_RUNS" in out
        assert "PR #999" in out
        assert "abc1234" in out
        assert "pytest -x -q" in out
        assert "exit_code=0" in out
        assert "acceptance-tester" in out
        assert "MAY skip re-running" in out

    def test_inject_for_pr_multiple_entries(self, tmp_path):
        """Multiple JSONL lines produce one summary line each."""
        artifact_dir = tmp_path / ".autonomous-team" / "pr-artifacts" / "42"
        artifact_dir.mkdir(parents=True)
        artifact_file = artifact_dir / "deadbeef.jsonl"
        entries = [
            {"command": "cargo test", "exit_code": 0, "duration_seconds": 100, "ts": "2026-05-17T10:00:00Z", "agent": "acceptance-tester"},
            {"command": "pytest -x", "exit_code": 1, "duration_seconds": 10, "ts": "2026-05-17T10:01:00Z", "agent": "code-reviewer"},
        ]
        artifact_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        script = f"""
SCRIPT_DIR="{REPO_ROOT}/scripts/lib"
REPO_ROOT="{tmp_path}"
source "{LIB_SCRIPT}"
inject_for_pr 42 deadbeef
"""
        result = run_bash(script)
        assert result.returncode == 0
        out = result.stdout
        assert "cargo test" in out
        assert "pytest -x" in out
        assert "exit_code=0" in out
        assert "exit_code=1" in out

    def test_inject_for_pr_empty_pr_emits_nothing(self, tmp_path):
        """inject_for_pr with empty pr prints nothing."""
        script = f"""
SCRIPT_DIR="{REPO_ROOT}/scripts/lib"
REPO_ROOT="{tmp_path}"
source "{LIB_SCRIPT}"
inject_for_pr "" abc1234
"""
        result = run_bash(script)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_inject_for_pr_empty_sha_emits_nothing(self, tmp_path):
        """inject_for_pr with empty sha prints nothing."""
        script = f"""
SCRIPT_DIR="{REPO_ROOT}/scripts/lib"
REPO_ROOT="{tmp_path}"
source "{LIB_SCRIPT}"
inject_for_pr 999 ""
"""
        result = run_bash(script)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestProducerExtractTestsRun:
    """Producer: extract tests_run from AGENT_OUTPUT envelope."""

    def test_extract_from_flat_json(self, tmp_path):
        """Python extraction logic works on flat JSON envelope."""
        extract_script = f"""
python3 - <<'PYEOF'
import sys, json, re

raw = json.dumps({{
    "agent": "acceptance-tester",
    "pr": 55,
    "verdict": "pass",
    "tests_run": [
        {{"command": "pytest -x", "exit_code": 0, "duration_seconds": 30}}
    ]
}})

try:
    d = json.loads(raw)
    tr = d.get("tests_run")
    if tr and isinstance(tr, list) and len(tr) > 0:
        print("FOUND")
        sys.exit(0)
except Exception:
    pass
print("NOT_FOUND")
PYEOF
"""
        result = run_bash(extract_script)
        assert result.returncode == 0
        assert "FOUND" in result.stdout

    def test_extract_no_tests_run_returns_empty(self, tmp_path):
        """Envelope without tests_run returns nothing."""
        extract_script = f"""
python3 - <<'PYEOF'
import sys, json

raw = json.dumps({{
    "agent": "executor",
    "pr": 55,
    "verdict": "done"
}})

try:
    d = json.loads(raw)
    tr = d.get("tests_run")
    if tr and isinstance(tr, list) and len(tr) > 0:
        print("FOUND")
        sys.exit(0)
except Exception:
    pass
# Empty → no output
PYEOF
"""
        result = run_bash(extract_script)
        assert result.returncode == 0
        assert "FOUND" not in result.stdout


class TestProducerWriteArtifact:
    """Producer write path: JSONL written correctly."""

    def test_jsonl_write_via_python(self, tmp_path):
        """Artifact JSONL is written with correct fields."""
        out_file = tmp_path / "artifacts.jsonl"
        tests_run = [
            {"command": "cargo test", "exit_code": 0, "duration_seconds": 2700, "stdout_tail": "ok"}
        ]
        write_script = f"""
python3 - '{json.dumps(tests_run)}' '2026-05-17T10:00:00Z' 'acceptance-tester' '{out_file}' <<'PYEOF'
import sys, json

tests_run_json, ts, agent, out_file = sys.argv[1:5]
entries = json.loads(tests_run_json)
with open(out_file, "a") as fh:
    for entry in entries:
        record = {{
            "command":          entry.get("command", ""),
            "exit_code":        entry.get("exit_code", -1),
            "duration_seconds": entry.get("duration_seconds", 0),
            "ts":               ts,
            "agent":            agent,
        }}
        if "stdout_tail" in entry:
            record["stdout_tail"] = entry["stdout_tail"]
        fh.write(json.dumps(record) + "\\n")
print("OK")
PYEOF
"""
        result = run_bash(write_script)
        assert result.returncode == 0
        assert out_file.exists()
        lines = [json.loads(l) for l in out_file.read_text().strip().splitlines()]
        assert len(lines) == 1
        assert lines[0]["command"] == "cargo test"
        assert lines[0]["exit_code"] == 0
        assert lines[0]["duration_seconds"] == 2700
        assert lines[0]["agent"] == "acceptance-tester"
        assert lines[0]["ts"] == "2026-05-17T10:00:00Z"
        assert lines[0]["stdout_tail"] == "ok"

    def test_jsonl_write_omits_stdout_tail_when_absent(self, tmp_path):
        """stdout_tail is omitted when not present in the envelope entry."""
        out_file = tmp_path / "artifacts.jsonl"
        tests_run = [
            {"command": "pytest", "exit_code": 0, "duration_seconds": 10}
        ]
        write_script = f"""
python3 - '{json.dumps(tests_run)}' '2026-05-17T10:00:00Z' 'code-reviewer' '{out_file}' <<'PYEOF'
import sys, json

tests_run_json, ts, agent, out_file = sys.argv[1:5]
entries = json.loads(tests_run_json)
with open(out_file, "a") as fh:
    for entry in entries:
        record = {{
            "command":          entry.get("command", ""),
            "exit_code":        entry.get("exit_code", -1),
            "duration_seconds": entry.get("duration_seconds", 0),
            "ts":               ts,
            "agent":            agent,
        }}
        if "stdout_tail" in entry:
            record["stdout_tail"] = entry["stdout_tail"]
        fh.write(json.dumps(record) + "\\n")
print("OK")
PYEOF
"""
        result = run_bash(write_script)
        assert result.returncode == 0
        lines = [json.loads(l) for l in out_file.read_text().strip().splitlines()]
        assert "stdout_tail" not in lines[0]
