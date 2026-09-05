"""
Shared pytest fixtures for the backend coordination module test suite.

All fixtures use tmp_path for filesystem isolation — no real .autonomous-team/
data is ever touched.
"""

import json
import os
from pathlib import Path

import pytest

# Allow imports from repo root
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testsupport.fixture_paths import FIXTURE_MAIN_REPO

# hooks/sandbox_rules.py derives MAIN_REPO_ROOT from the filesystem
# (hooks/repo_root.py) rather than a hardcoded literal — that was the
# D#1890 PR 1 fix round: the literal it used to carry didn't exist on this
# machine and left the sandbox not actually sandboxing.
# tests/test_classify_cwd.py, tests/test_sandbox_rules.py and
# tests/test_sandbox_agent_block.py pin a SYNTHETIC fixture root
# (`_MAIN_REPO`) — they were never asserting anything about this machine's
# real path, just checking the tiering logic against made-up paths. That root
# now comes from testsupport/fixture_paths.py so there is one copy of it
# rather than one per test file (D#1997). SANDBOX_MAIN_REPO_ROOT (test-only
# override, see hooks/repo_root.py's module docstring for why it is safe to
# expose) pins the fixture root back to what those tests expect, both for
# in-process imports of hooks.sandbox_rules and for the subprocess-invoked
# hooks/sandbox.py calls in
# tests/test_sandbox_agent_block.py — subprocess.run inherits this process's
# environment by default, so setting it once here, before any test module
# imports hooks.sandbox_rules, covers both.
os.environ.setdefault("SANDBOX_MAIN_REPO_ROOT", FIXTURE_MAIN_REPO)

from backend.blackboard import Blackboard
from backend.budget import BudgetTracker
from backend.context_manager import ProjectContext
from backend.workflow_runner import WorkflowRunner
from backend.agent_cards import AgentCards


@pytest.fixture
def bb(tmp_path):
    """Isolated Blackboard instance backed by a temp directory."""
    return Blackboard(root=tmp_path / "blackboard")


@pytest.fixture
def ctx(tmp_path):
    """Isolated ProjectContext instance backed by a temp directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    return ProjectContext(state_dir=state_dir)


@pytest.fixture
def workflows_dir(tmp_path):
    """Temp directory pre-populated with a minimal valid workflow YAML."""
    d = tmp_path / "workflows"
    d.mkdir()
    sample = d / "sample-workflow.yaml"
    sample.write_text(
        "name: sample-workflow\n"
        "description: A sample workflow for testing\n"
        "pattern: sequence\n"
        "inputs:\n"
        "  discussion_number:\n"
        "    required: true\n"
        "    description: Discussion number\n"
        "steps:\n"
        "  - id: implement\n"
        "    agent: executor\n"
        "    prompt_template: Implement discussion {{discussion_number}}\n"
        "    expects:\n"
        "      - pr_number\n"
        "    timeout_minutes: 30\n"
        "outputs:\n"
        "  pr_number: '{{steps.implement.pr_number}}'\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def agents_dir(tmp_path):
    """Temp directory pre-populated with a sample agent card JSON."""
    d = tmp_path / "agents"
    d.mkdir()
    card = d / "executor.json"
    card.write_text(
        json.dumps(
            {
                "role": "executor",
                "description": "Implements code from a spec",
                "capabilities": ["git", "bash", "file_write"],
                "authorized_tools": ["Bash", "Write", "Edit", "Read"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return d
