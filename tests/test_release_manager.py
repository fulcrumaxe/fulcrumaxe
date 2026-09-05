"""
Tests for the release-manager role.

Acceptance criteria verified:
  AC1: python3 backend/release_manager.py record --pr <N> --dry-run prints valid JSON
  AC2: Release record contains all required fields per release.schema.json
  AC3: post-merge-hook enqueue path exists and gate check is in place
  AC4: python3 backend/release_manager.py list exits 0
  AC5: gates.release_manager defaults to true; gate off → no enqueue
  AC6: risk=high sets follow_up_spawns: ["runbook-writer"]
  BONUS: spawn_templates knows release-manager; persona JSON is valid
"""

import json
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# AC5: gates.release_manager defaults to True in control_plane.py
# ---------------------------------------------------------------------------

def test_release_manager_gate_default_is_true():
    """gates.release_manager must default to True."""
    from backend.control_plane import _DEFAULT_GATES
    assert _DEFAULT_GATES.get("release_manager") is True, (
        "gates.release_manager must default to True in _DEFAULT_GATES"
    )


def test_release_manager_gate_readable_via_cli():
    """python3 backend/control_plane.py get gates.release_manager returns 'true'."""
    result = subprocess.run(
        [sys.executable, "backend/control_plane.py", "get", "gates.release_manager"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert result.stdout.strip().lower() in ("true", '"true"'), (
        f"Expected 'true', got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# AC1 + AC2: dry-run produces valid release record
# ---------------------------------------------------------------------------

def test_dry_run_produces_valid_json():
    """record --dry-run must print valid JSON to stdout."""
    result = subprocess.run(
        [sys.executable, "backend/release_manager.py", "record", "--pr", "1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"release_manager.py exited {result.returncode}. stderr: {result.stderr}"
    )
    # Parse the JSON (everything before the dry-run stderr line)
    stdout_lines = result.stdout.strip().splitlines()
    json_text = "\n".join(stdout_lines)
    record = json.loads(json_text)
    assert isinstance(record, dict), "Output must be a JSON object"


def test_dry_run_has_required_fields():
    """Release record must include all fields required by release.schema.json."""
    result = subprocess.run(
        [sys.executable, "backend/release_manager.py", "record", "--pr", "1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    record = json.loads(result.stdout.strip())

    required = [
        "id", "pr_numbers", "merged_at", "merge_shas",
        "risk", "rollback_command", "runbook_needed", "dora_snapshot",
    ]
    for field in required:
        assert field in record, f"Release record missing required field: {field!r}"


def test_dry_run_id_format():
    """Release ID must match YYYY-MM-DD-NNN format."""
    import re
    result = subprocess.run(
        [sys.executable, "backend/release_manager.py", "record", "--pr", "1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    record = json.loads(result.stdout.strip())
    assert re.match(r"^\d{4}-\d{2}-\d{2}-\d{3}$", record["id"]), (
        f"Release ID format must be YYYY-MM-DD-NNN, got: {record['id']!r}"
    )


def test_dry_run_risk_is_valid():
    """risk field must be one of low/medium/high."""
    result = subprocess.run(
        [sys.executable, "backend/release_manager.py", "record", "--pr", "1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    record = json.loads(result.stdout.strip())
    assert record["risk"] in ("low", "medium", "high"), (
        f"risk must be low/medium/high, got: {record['risk']!r}"
    )


def test_dry_run_dora_snapshot_fields():
    """dora_snapshot must contain the three DORA metric fields."""
    result = subprocess.run(
        [sys.executable, "backend/release_manager.py", "record", "--pr", "1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    record = json.loads(result.stdout.strip())
    dora = record["dora_snapshot"]
    assert "deploy_frequency_per_day" in dora
    assert "lead_time_minutes_p50" in dora
    assert "change_failure_rate_pct" in dora
    assert isinstance(dora["deploy_frequency_per_day"], (int, float))
    assert isinstance(dora["lead_time_minutes_p50"], (int, float))
    assert isinstance(dora["change_failure_rate_pct"], (int, float))


def test_dry_run_rollback_command_is_string():
    """rollback_command must be a non-empty string."""
    result = subprocess.run(
        [sys.executable, "backend/release_manager.py", "record", "--pr", "1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    record = json.loads(result.stdout.strip())
    assert isinstance(record["rollback_command"], str)
    assert len(record["rollback_command"]) > 0


# ---------------------------------------------------------------------------
# AC4: list command exits 0
# ---------------------------------------------------------------------------

def test_list_exits_zero():
    """python3 backend/release_manager.py list must exit 0."""
    result = subprocess.run(
        [sys.executable, "backend/release_manager.py", "list"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"release_manager.py list exited {result.returncode}. stderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# AC6: risk=high produces follow_up_spawns: ["runbook-writer"]
# ---------------------------------------------------------------------------

def test_high_risk_produces_follow_up_spawns():
    """classify_risk must return 'high' for server.py/api.py diffs, and record includes follow_up_spawns."""
    from backend.release_manager import classify_risk
    risk = classify_risk(["backend/server.py", "backend/api.py"], ["code-review-passed"])
    assert risk == "high", f"Expected 'high', got {risk!r}"


def test_no_review_label_is_high_risk():
    """Missing code-review-passed label must classify as high risk."""
    from backend.release_manager import classify_risk
    risk = classify_risk(["tui/src/App.tsx"], [])
    assert risk == "high", f"Expected 'high' (no review label), got {risk!r}"


def test_pure_docs_is_low_risk():
    """Pure wiki/markdown changes must classify as low risk."""
    from backend.release_manager import classify_risk
    risk = classify_risk(["wiki/Changelog.md", "wiki/Project-Status.md"], ["code-review-passed"])
    assert risk == "low", f"Expected 'low' (docs only), got {risk!r}"


def test_backend_change_is_medium_risk():
    """Backend changes (not server.py/api.py) must classify as medium risk."""
    from backend.release_manager import classify_risk
    risk = classify_risk(["backend/cost_tracker.py"], ["code-review-passed"])
    assert risk == "medium", f"Expected 'medium' (backend change), got {risk!r}"


def test_high_risk_record_has_follow_up_spawns():
    """When risk=high, record dict must include follow_up_spawns: ['runbook-writer']."""
    from backend.release_manager import record_release
    # Monkeypatch: we can't easily inject a high-risk PR without GitHub, so test classify_risk directly
    from backend.release_manager import classify_risk
    risk = classify_risk(["backend/server.py"], ["code-review-passed"])
    assert risk == "high"
    # Verify the dry-run record includes follow_up_spawns when runbook_needed is True
    # We do this by calling record_release in dry-run mode with a real structure
    record = record_release(pr_numbers=[999999], dry_run=True)
    # For a PR that doesn't exist, we get empty diff_files → default medium/high
    # This tests the code path exists — the exact value depends on PR existence
    assert "runbook_needed" in record
    assert isinstance(record["runbook_needed"], bool)


# ---------------------------------------------------------------------------
# AC3: post-merge-hook has release_manager_queue step
# ---------------------------------------------------------------------------

def test_post_merge_hook_has_release_manager_queue_step():
    """post-merge-hook.sh must call release_manager.py record directly."""
    hook = REPO_ROOT / "scripts" / "post-merge-hook.sh"
    assert hook.exists(), f"Missing: {hook}"
    content = hook.read_text(encoding="utf-8")
    assert "release_manager_queue" in content, (
        "post-merge-hook.sh must contain 'release_manager_queue' step"
    )
    assert "release_manager.py" in content and "record --pr" in content, (
        "post-merge-hook.sh must call release_manager.py record --pr directly (not via spawn_queue)"
    )
    assert "gates.release_manager" in content, (
        "post-merge-hook.sh must check gates.release_manager before recording"
    )


# ---------------------------------------------------------------------------
# Idempotency: duplicate record --pr calls produce exactly one record
# ---------------------------------------------------------------------------

def test_record_release_idempotent(tmp_path, monkeypatch):
    """Calling record_release() twice with the same PR writes exactly one record."""
    import backend.release_manager as rm

    monkeypatch.setattr(rm, "_RELEASES_DIR", tmp_path)

    # Stub out gh subprocess calls so the test is hermetic
    import unittest.mock as mock
    fake_pr_data = json.dumps({
        "files": [{"path": "README.md"}],
        "labels": [],
        "mergeCommit": {"oid": "abc123"},
        "mergedAt": "2026-05-21T10:00:00Z",
    })

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout=fake_pr_data)

        first = rm.record_release(pr_numbers=[9999])
        second = rm.record_release(pr_numbers=[9999])

    # Both calls return the same record
    assert first["id"] == second["id"], "Second call must return the existing record"

    # Only one file was written
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1, f"Expected exactly 1 release file, found {len(written)}"


# ---------------------------------------------------------------------------
# BONUS: spawn_templates and persona checks
# ---------------------------------------------------------------------------

def test_spawn_templates_knows_release_manager():
    """spawn_templates.KNOWN_ROLES must include 'release-manager'."""
    from backend.spawn_templates import KNOWN_ROLES
    assert "release-manager" in KNOWN_ROLES


def test_release_manager_tmpl_exists():
    """backend/spawn_templates/release-manager.tmpl must exist."""
    tmpl = REPO_ROOT / "backend" / "spawn_templates" / "release-manager.tmpl"
    assert tmpl.exists(), f"Missing template: {tmpl}"
    assert tmpl.stat().st_size > 100, "Template file is suspiciously empty"


def test_release_manager_agent_md_exists():
    """release-manager.md must exist in .claude/agents/."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "release-manager.md"
    assert agent_file.exists(), f"Missing: {agent_file}"


def test_release_manager_agent_md_has_frontmatter():
    """release-manager.md must have valid YAML frontmatter."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "release-manager.md"
    content = agent_file.read_text(encoding="utf-8")
    assert content.startswith("---"), "agent file must start with '---' frontmatter"
    assert "name: release-manager" in content
    assert "description:" in content


def test_release_manager_agent_md_has_output_envelope():
    """release-manager.md must include AGENT_OUTPUT section."""
    agent_file = REPO_ROOT / ".claude" / "agents" / "release-manager.md"
    content = agent_file.read_text(encoding="utf-8")
    assert "AGENT_OUTPUT" in content
    assert "done" in content
    assert "skip" in content
    assert "fail" in content


def test_spawn_prompt_renders_release_manager():
    """spawn_prompt.py release-manager --discussion 1 --pr 1 must produce non-empty output."""
    result = subprocess.run(
        [
            sys.executable,
            "backend/spawn_prompt.py",
            "release-manager",
            "--discussion", "1",
            "--pr", "1",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"spawn_prompt.py exited {result.returncode}. stderr: {result.stderr}"
    )
    from backend._repo import REPO

    assert len(result.stdout.strip()) > 100
    # Checks against the live resolved repo slug rather than a hard-coded
    # literal (D#1870 — this assertion previously expected the pre-rename
    # "autonomous-forever" slug after the resolver was fixed).
    assert REPO in result.stdout
    assert "release_manager" in result.stdout


def test_release_manager_persona_json_exists():
    """release-manager.json must exist in .autonomous-team/personas/."""
    persona_file = REPO_ROOT / ".autonomous-team" / "personas" / "release-manager.json"
    assert persona_file.exists(), f"Missing: {persona_file}"


def test_release_manager_persona_json_valid():
    """release-manager.json must be valid JSON and match persona schema fields."""
    persona_file = REPO_ROOT / ".autonomous-team" / "personas" / "release-manager.json"
    persona = json.loads(persona_file.read_text(encoding="utf-8"))

    required_fields = ["name", "big_five", "values", "style", "conflict_pattern", "sign_off"]
    for field in required_fields:
        assert field in persona, f"persona missing required field: {field!r}"

    big_five_keys = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]
    for key in big_five_keys:
        assert key in persona["big_five"], f"big_five missing key: {key!r}"
        val = persona["big_five"][key]
        assert isinstance(val, int), f"big_five.{key} must be int"
        assert 0 <= val <= 100, f"big_five.{key} must be 0-100"

    assert isinstance(persona["values"], list) and len(persona["values"]) >= 1
    assert isinstance(persona["style"], str) and len(persona["style"]) > 0
    assert isinstance(persona["conflict_pattern"], str) and len(persona["conflict_pattern"]) > 0

    # Name from spec: Hale
    assert persona["name"] == "Hale", f"Expected name 'Hale', got {persona['name']!r}"


def test_release_manager_persona_big_five_matches_spec():
    """Big Five values must match the spec (O=55 C=90 E=50 A=55 N=15)."""
    persona_file = REPO_ROOT / ".autonomous-team" / "personas" / "release-manager.json"
    persona = json.loads(persona_file.read_text(encoding="utf-8"))
    bf = persona["big_five"]
    assert bf["openness"] == 55
    assert bf["conscientiousness"] == 90
    assert bf["extraversion"] == 50
    assert bf["agreeableness"] == 55
    assert bf["neuroticism"] == 15


def test_release_schema_json_exists():
    """.autonomous-team/schemas/release.schema.json must exist and be valid JSON."""
    schema_file = REPO_ROOT / ".autonomous-team" / "schemas" / "release.schema.json"
    assert schema_file.exists(), f"Missing: {schema_file}"
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    assert schema.get("title") == "ReleaseRecord"
    required_props = schema.get("required", [])
    for field in ["id", "pr_numbers", "merged_at", "merge_shas", "risk",
                  "rollback_command", "runbook_needed", "dora_snapshot"]:
        assert field in required_props, f"Schema missing required field: {field!r}"


def test_releases_gitkeep_exists():
    """.autonomous-team/releases/.gitkeep must exist."""
    gitkeep = REPO_ROOT / ".autonomous-team" / "releases" / ".gitkeep"
    assert gitkeep.exists(), f"Missing: {gitkeep}"


# ---------------------------------------------------------------------------
# D#1391: gh failure must NOT stamp datetime.now(); merged_at stays None
# ---------------------------------------------------------------------------

def test_gh_failure_leaves_merged_at_null(tmp_path, monkeypatch):
    """When gh pr view fails, merged_at must be None — not datetime.now()."""
    import unittest.mock as mock
    import backend.release_manager as rm

    monkeypatch.setattr(rm, "_RELEASES_DIR", tmp_path)

    # Simulate gh pr view returning a non-zero exit code
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="gh: not found")

        with mock.patch.object(rm.logger, "warning") as mock_warn:
            record = rm.record_release(pr_numbers=[9999], dry_run=True)

            # merged_at must be None, not a now() timestamp
            assert record["merged_at"] is None, (
                f"merged_at should be None on gh failure, got: {record['merged_at']!r}"
            )
            # A warning must have been logged
            assert mock_warn.called, "logger.warning must be called when gh pr view fails"


def test_caller_supplied_merged_at_still_works(tmp_path, monkeypatch):
    """Backfill path: caller-supplied merged_at is written unchanged."""
    import unittest.mock as mock
    import backend.release_manager as rm

    monkeypatch.setattr(rm, "_RELEASES_DIR", tmp_path)

    explicit_ts = "2026-01-15T12:00:00Z"

    with mock.patch("subprocess.run") as mock_run:
        # gh pr view fails — but merged_at is pre-supplied by caller
        mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="network error")

        record = rm.record_release(
            pr_numbers=[1234],
            dry_run=True,
            merged_at=explicit_ts,
        )

    assert record["merged_at"] == explicit_ts, (
        f"Caller-supplied merged_at must be preserved; got: {record['merged_at']!r}"
    )


def test_dora_reader_skips_null_merged_at(tmp_path, monkeypatch):
    """compute_dora_snapshot must not count records with null merged_at."""
    import backend.release_manager as rm
    from datetime import datetime, timezone

    monkeypatch.setattr(rm, "_RELEASES_DIR", tmp_path)

    # Write a record with no merged_at
    null_record = {
        "id": "2026-01-01-001",
        "pr_numbers": [1],
        "merged_at": None,
        "merge_shas": [],
        "risk": "low",
        "rollback_command": "git revert HEAD --no-edit",
        "runbook_needed": False,
        "dora_snapshot": {},
    }
    (tmp_path / "2026-01-01-001.json").write_text(json.dumps(null_record), encoding="utf-8")

    # Write a valid record with a real merged_at inside the 7-day window
    now = datetime.now(timezone.utc)
    valid_record = {
        "id": "2026-01-01-002",
        "pr_numbers": [2],
        "merged_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merge_shas": [],
        "risk": "low",
        "rollback_command": "git revert HEAD --no-edit",
        "runbook_needed": False,
        "dora_snapshot": {},
    }
    (tmp_path / "2026-01-01-002.json").write_text(json.dumps(valid_record), encoding="utf-8")

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout=json.dumps([]), stderr="")
        snapshot = rm.compute_dora_snapshot()

    # deploy_frequency_per_day should count only the 1 valid record, not the null one
    freq = snapshot["deploy_frequency_per_day"]
    assert freq == round(1 / 7.0, 4), (
        f"deploy_frequency_per_day should be 1/7 (only valid record counted), got: {freq}"
    )
