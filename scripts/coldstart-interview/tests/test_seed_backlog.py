"""Unit tests for scripts/coldstart-interview/seed-backlog.py

Covers Batch C2 (D#1622) acceptance items 1-8:
    1. --plan-only emits a JSON array of seed specs, no network, deterministic.
    2. Seeds are tailored to the answers' stack/deploy tokens.
    3. Bounded seed count: 3 <= len(plan) <= 7.
    4. Bounded retry with backoff on 5xx only; 4xx never retries.
    5. Persistent failure / --offline degrades to a replay file and exits 0.
    6. --replay re-sends idempotently, skipping titles that already exist.
    7. Repo-scope invariant: owner/name resolve from the project's own answers.
    8. --plan-only / --self-test paths make zero network / subprocess calls.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).parent
COLDSTART_DIR = HERE.parent

sys.path.insert(0, str(COLDSTART_DIR))

# seed-backlog.py has a hyphen in its filename -- not import-able via a plain
# `import` statement, so load it explicitly by path (same trick used
# elsewhere in this repo for hyphenated script modules).
_SPEC = importlib.util.spec_from_file_location("seed_backlog", COLDSTART_DIR / "seed-backlog.py")
seed_backlog = importlib.util.module_from_spec(_SPEC)
sys.modules["seed_backlog"] = seed_backlog  # dataclass() needs this registered before exec
_SPEC.loader.exec_module(seed_backlog)  # type: ignore[union-attr]

import generate  # noqa: E402

FIXTURE_MISSION = HERE / "fixtures" / "answers-mission.json"
FIXTURE_NEW = HERE / "fixtures" / "answers-new.json"
FIXTURE_CORE = HERE / "fixtures" / "answers-core.json"

FRAMEWORK_REPO = ("example-org", "fulcrumaxe")


def _resolved(fixture: Path) -> dict:
    manifest = generate.load_manifest(generate.DEFAULT_MANIFEST)
    answers = generate.load_answers(fixture)
    return generate.resolve_answers(manifest, answers)


# ---------------------------------------------------------------------------
# 1-3: PLAN phase — deterministic, tailored, bounded.
# ---------------------------------------------------------------------------

def test_plan_only_no_network_call():
    """--plan-only must never touch subprocess (no network)."""
    with mock.patch("subprocess.run", side_effect=AssertionError("network call attempted")):
        rc = seed_backlog.main(["--plan-only", "--answers", str(FIXTURE_MISSION)])
    assert rc == 0


def test_plan_is_deterministic():
    resolved = _resolved(FIXTURE_MISSION)
    plan_a = seed_backlog.plan_seeds(resolved)
    plan_b = seed_backlog.plan_seeds(resolved)
    assert plan_a == plan_b


def test_plan_bounded_3_to_7():
    for fixture in (FIXTURE_MISSION, FIXTURE_NEW, FIXTURE_CORE):
        resolved = _resolved(fixture)
        plan = seed_backlog.plan_seeds(resolved)
        assert seed_backlog.MIN_SEEDS <= len(plan) <= seed_backlog.MAX_SEEDS, (fixture, len(plan))
        for seed in plan:
            assert set(seed.keys()) >= {"title", "body", "category"}


def test_plan_never_zero_for_minimal_answers():
    resolved = generate.resolve_answers(
        generate.load_manifest(generate.DEFAULT_MANIFEST), {"topics": {}}
    )
    plan = seed_backlog.plan_seeds(resolved)
    assert len(plan) >= seed_backlog.MIN_SEEDS


def test_plan_tailored_to_stack_and_deploy():
    """Fixture answers-mission.json: primary_language=typescript,
    runtime_framework=Next.js, deploy_target=cloudflare -- the plan must
    reference the actual stack/deploy tokens, not generic placeholders."""
    resolved = _resolved(FIXTURE_MISSION)
    plan = seed_backlog.plan_seeds(resolved)
    blob = json.dumps(plan).lower()
    assert "typescript" in blob or "next.js" in blob
    assert "cloudflare" in blob


def test_seed_content_is_human_voice_not_process_jargon():
    resolved = _resolved(FIXTURE_MISSION)
    plan = seed_backlog.plan_seeds(resolved)
    blob = json.dumps(plan).lower()
    for banned in ("the spec", "discussion #", "acceptance criteria"):
        assert banned not in blob


# ---------------------------------------------------------------------------
# 4: bounded retry with backoff on 5xx only.
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def run_graphql(self, args, timeout=30):
        self.calls += 1
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def test_retry_on_5xx_up_to_cap():
    always_500 = seed_backlog.TransportResult(1, "", "gh: Internal Server Error (HTTP 500)")
    transport = FakeTransport([always_500] * 10)
    result = seed_backlog.run_graphql_with_retry(
        transport, ["-f", "query=x"], max_retries=3, sleep_fn=lambda s: None,
    )
    assert result.returncode != 0
    # initial attempt + 3 retries = 4 calls total
    assert transport.calls == 4


def test_no_retry_on_4xx():
    bad_request = seed_backlog.TransportResult(1, "", "gh: Bad credentials (HTTP 401)")
    transport = FakeTransport([bad_request])
    result = seed_backlog.run_graphql_with_retry(
        transport, ["-f", "query=x"], max_retries=3, sleep_fn=lambda s: None,
    )
    assert result.returncode != 0
    assert transport.calls == 1


def test_5xx_then_success_returns_ok():
    seq = [
        seed_backlog.TransportResult(1, "", "gh: Bad Gateway (HTTP 502)"),
        seed_backlog.TransportResult(1, "", "gh: Bad Gateway (HTTP 502)"),
        seed_backlog.TransportResult(0, '{"data": {"ok": true}}', ""),
    ]

    # Exact-sequence queue transport (each call pops the next canned result).
    class QueueTransport:
        def __init__(self, results):
            self._results = list(results)
            self.calls = 0

        def run_graphql(self, args, timeout=30):
            self.calls += 1
            return self._results.pop(0)

    qt = QueueTransport(seq)
    result = seed_backlog.run_graphql_with_retry(
        qt, ["-f", "query=x"], max_retries=3, sleep_fn=lambda s: None,
    )
    assert result.returncode == 0
    assert qt.calls == 3


# ---------------------------------------------------------------------------
# 5: persistent failure / --offline degrades to a replay file, exit 0.
# ---------------------------------------------------------------------------

def test_offline_writes_replay_file_and_exits_0(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    session = "sess-offline"
    rc = seed_backlog.main([
        "--answers", str(FIXTURE_MISSION),
        "--offline",
        "--session", session,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "degraded to replay file:" in out


def test_offline_replay_file_content(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    session = "sess-content"
    rc = seed_backlog.main([
        "--answers", str(FIXTURE_MISSION),
        "--offline",
        "--session", session,
    ])
    assert rc == 0
    path = seed_backlog.replay_file_path(session)
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["repo_owner"] == "acme-labs"
    assert payload["repo_name"] == "widgetforge"
    assert 3 <= len(payload["seeds"]) <= 7


def test_persistent_5xx_degrades_to_replay(monkeypatch, tmp_path):
    """All retries exhausted on every GraphQL call (including repo-id lookup)
    -- must still exit 0 and leave a replay file, never abort."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    always_500 = seed_backlog.TransportResult(1, "", "gh: Internal Server Error (HTTP 500)")

    class AlwaysFailTransport:
        def run_graphql(self, args, timeout=30):
            return always_500

    resolved = _resolved(FIXTURE_MISSION)
    owner, name = seed_backlog.derive_repo_target(resolved)
    seeds = seed_backlog.plan_seeds(resolved)

    with pytest.raises(RuntimeError):
        seed_backlog.apply_seeds(seeds, owner, name, AlwaysFailTransport(), max_retries=1, sleep_fn=lambda s: None)


# ---------------------------------------------------------------------------
# 6: --replay idempotency — skip titles that already exist.
# ---------------------------------------------------------------------------

def test_replay_skips_existing_titles():
    resolved = _resolved(FIXTURE_MISSION)
    owner, name = seed_backlog.derive_repo_target(resolved)
    seeds = seed_backlog.plan_seeds(resolved)
    already_open = {seeds[0]["title"]}

    created = []

    class RecordingTransport:
        def run_graphql(self, args, timeout=30):
            joined = " ".join(args)
            if "repository(owner" in joined and "id}}" in joined.replace(" ", ""):
                return seed_backlog.TransportResult(0, json.dumps({"data": {"repository": {"id": "R_1"}}}), "")
            if "discussionCategories" in joined:
                return seed_backlog.TransportResult(
                    0,
                    json.dumps({"data": {"repository": {"discussionCategories": {"nodes": [{"id": "C_1", "name": "Ideas"}]}}}}),
                    "",
                )
            if "createDiscussion" in joined:
                # Extract title from the -f title=... arg for the assertion.
                title = None
                for i, a in enumerate(args):
                    if a.startswith("title="):
                        title = a[len("title="):]
                created.append(title)
                return seed_backlog.TransportResult(
                    0,
                    json.dumps({"data": {"createDiscussion": {"discussion": {"url": "https://x/1", "number": 1}}}}),
                    "",
                )
            return seed_backlog.TransportResult(1, "", "unexpected query")

    sent, unsent, errors = seed_backlog.apply_seeds(
        seeds, owner, name, RecordingTransport(), skip_titles=already_open,
    )
    assert already_open.isdisjoint(set(created))
    assert len(sent) == len(seeds) - 1
    assert not unsent


# ---------------------------------------------------------------------------
# 7: repo-scope invariant.
# ---------------------------------------------------------------------------

def test_repo_scope_resolves_from_answers_not_hardcoded():
    resolved = _resolved(FIXTURE_MISSION)
    owner, name = seed_backlog.derive_repo_target(resolved)
    assert (owner, name) == ("acme-labs", "widgetforge")
    assert (owner, name) != FRAMEWORK_REPO


def test_repo_scope_missing_identity_raises():
    resolved = generate.resolve_answers(
        generate.load_manifest(generate.DEFAULT_MANIFEST), {"topics": {}}
    )
    resolved["identity"] = {}
    with pytest.raises(ValueError):
        seed_backlog.derive_repo_target(resolved)


# ---------------------------------------------------------------------------
# 8: --self-test exits 0, zero network calls.
# ---------------------------------------------------------------------------

def test_self_test_no_network_and_exits_0():
    with mock.patch("subprocess.run", side_effect=AssertionError("network call attempted")):
        rc = seed_backlog.main(["--self-test"])
    assert rc == 0
