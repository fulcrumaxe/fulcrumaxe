"""EB-10d: the engine merge paths that used to merge without asking the cloud (loop-phased-step5.sh and
team-lead-iteration.sh) and the importer's creation of Discussions that are already SPEC_READY, against the
strict fake decide socket (testsupport/fake_cloud_decide.py).

Every case runs in a throwaway copy of the engine tree with its own state dir, so nothing touches a real
checkout or a real state dir. The gh binary is a stub that records `pr merge` calls."""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("AUTONOMOUS_TEAM_STATE_DIR", tempfile.mkdtemp(prefix="fxmp-state-"))
from testsupport.fake_cloud_decide import FakeDecide  # noqa: E402

SHA = "abcdef0123456789abcdef0123456789abcdef01"
# mode -> (gate exit code, merge happens)
MATRIX = {"act": (0, True), "announce": (0, True), "act_approved": (0, True),
          "ask": (3, False), "ask_declined": (3, False),
          "halted": (4, False), "halted_act": (4, False),
          "http_500": (5, False), "http_403": (5, False), "not_json": (5, False),
          "extra_key": (5, False), "truncated": (5, False), "close_early": (5, False)}

GH_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_CALL_LOG"
case "$1 $2" in
  "pr view")
    case "$*" in
      *headRefOid*) echo "$STUB_HEAD_SHA" ;;
      *labels*) echo '["code-review-passed"]' ;;
      *) echo "" ;;
    esac ;;
esac
exit 0
"""


@pytest.fixture(scope="module")
def engine_copy(tmp_path_factory):
    dst = tmp_path_factory.mktemp("engine") / "tree"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".pytest_cache"))
    return str(dst)


class World:
    def __init__(self, tmp_path, engine_copy):
        self.root = engine_copy
        self.tmp = tmp_path
        self.state = str(tmp_path / "state")
        os.makedirs(self.state, mode=0o700)
        self.run_dir = tempfile.mkdtemp(prefix="fxm")
        os.chmod(self.run_dir, 0o700)
        self.sock = os.path.join(self.run_dir, "fx-runner", "engine-bridge.sock")
        self.calls = str(tmp_path / "calls.log")
        self.bin = str(tmp_path / "bin")
        os.makedirs(self.bin)
        with open(self.bin + "/gh", "w") as f:
            f.write(GH_STUB)
        os.chmod(self.bin + "/gh", 0o755)
        self.tlog = str(tmp_path / "teamlog.txt")
        with open(self.root + "/scripts/rotate-team-log.sh", "w") as f:
            f.write('#!/usr/bin/env bash\nprintf "%%s\\n" "$2" >> "%s"\n' % self.tlog)
        self.server = None
        self.config(True)

    def config(self, operate):
        os.makedirs(self.root + "/.autonomous-team", exist_ok=True)
        cfg = {"repo": "o/r"} if operate == "absent" else {"repo": "o/r", "cloud_bridge": {"operate": operate, "repo_gh_id": 4242, "socket_path": self.sock}}
        with open(self.root + "/.autonomous-team/config.json", "w") as f:
            json.dump(cfg, f)

    def serve(self, mode):
        self.server = FakeDecide(self.sock, mode)
        return self.server

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "FX_ENGINE_COMMAND_ID", "REPO_ROOT")}
        e.update(AUTONOMOUS_TEAM_STATE_DIR=self.state, PATH=self.bin + ":" + e["PATH"], STUB_CALL_LOG=self.calls,
                 STUB_HEAD_SHA=SHA, AUTONOMOUS_TEAM_REPO="o/r")
        e.update(extra)
        return e

    def merged(self):
        return os.path.exists(self.calls) and "pr merge" in open(self.calls).read()

    def lanes(self):
        path = self.state + "/engine-events.jsonl"
        if not os.path.exists(path):
            return []
        return [json.loads(x)["state"] for x in open(path).read().splitlines() if json.loads(x)["kind"] == "merge_lane"]

    def close(self):
        if self.server:
            self.server.close()


@pytest.fixture
def world(tmp_path, engine_copy):
    w = World(tmp_path, engine_copy)
    yield w
    w.close()


# ---------------------------------------------------------------------------------------------------------
# The shared helper and team-lead-iteration.sh's merge step
# ---------------------------------------------------------------------------------------------------------

def _drive_gate(w):
    cmd = ('source scripts/lib/cloud-merge-gate.sh; cloud_merge_gate 77 %s 842; echo "rc=$?"' % SHA)
    return subprocess.run(["bash", "-c", cmd], cwd=w.root, env=w.env(), capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("mode", sorted(MATRIX))
def test_shared_helper_matrix(world, mode):
    code, _merge = MATRIX[mode]
    world.serve(mode)
    r = _drive_gate(world)
    assert "rc=%d" % code in r.stdout, r.stdout + r.stderr
    assert ("refused" in world.lanes()) == (code != 0)
    assert world.server.violations == []
    body = json.loads(world.server.requests[0]["body"])
    assert body["decision_type"] == "engine_merge" and body["request_key"] == "merge:77:" + SHA


def test_shared_helper_operate_off_or_absent_never_asks(world):
    for op in (False, "absent"):
        world.config(op)
        r = _drive_gate(world)
        assert "rc=0" in r.stdout and world.lanes() == []
    assert not os.path.exists(world.sock)


def test_shared_helper_hook_missing_with_operate_on_refuses(world):
    os.remove(world.root + "/scripts/hooks/pre-merge.d/cloud-decide.sh")
    try:
        r = _drive_gate(world)
        assert "rc=5" in r.stdout and world.lanes() == ["refused"]
    finally:
        shutil.copy(os.path.join(ROOT, "scripts/hooks/pre-merge.d/cloud-decide.sh"),
                    world.root + "/scripts/hooks/pre-merge.d/cloud-decide.sh")


def _drive_team_lead(w):
    """Step 6 of team-lead-iteration.sh, cut out of the script and run with one PR needing a merge."""
    src = open(w.root + "/scripts/team-lead-iteration.sh").read()
    a = src.index("else\n  for pr_entry in")
    b = src.index("# ─────", a)
    body = src[a:b]
    prelude = ('set -uo pipefail\nSCRIPT_DIR="%s/scripts"\nREPO_ROOT="%s"\nCODE_REPO="o/r"\nDRY_RUN=false\n'
               'log() { echo "LOG: $*"; }\ndry() { if [ "$DRY_RUN" = "true" ]; then echo "[DRY-RUN] $*"; else eval "$*"; fi; }\n'
               'NEEDS_MERGE=("77:Some PR")\n' % (w.root, w.root))
    script = prelude + "if true; then\n" + body.replace("else\n  for", "  for", 1)
    # post-merge-hook is stubbed: only the merge decision is under test
    script = script.replace('bash "$SCRIPT_DIR/post-merge-hook.sh"', "true")
    return script


def _run_team_lead(w, dry=False):
    script = _drive_team_lead(w)
    if dry:
        script = script.replace("DRY_RUN=false", "DRY_RUN=true")
    return subprocess.run(["bash", "-c", script], cwd=w.root, env=w.env(), capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("mode", sorted(MATRIX))
def test_team_lead_iteration_matrix(world, mode):
    code, merge = MATRIX[mode]
    world.serve(mode)
    r = _run_team_lead(world)
    assert world.merged() == merge, r.stdout + r.stderr
    if merge:
        assert "--match-head-commit " + SHA in open(world.calls).read()
        assert world.lanes() == []
    else:
        assert "cloud decide refused the merge (exit %d" % code in r.stdout
        assert world.lanes() == ["refused"]


def test_team_lead_iteration_operate_off_is_todays_command(world):
    for op in (False, "absent"):
        world.config(op)
        if os.path.exists(world.calls):
            os.remove(world.calls)
        r = _run_team_lead(world)
        calls = open(world.calls).read()
        assert "pr merge 77 --squash --delete-branch --repo o/r" in calls and "match-head" not in calls, r.stdout
        assert "headRefOid" not in calls
    assert not os.path.exists(world.sock)


def test_team_lead_iteration_dry_run_asks_nothing(world):
    world.serve("ask")
    r = _run_team_lead(world, dry=True)
    assert "[DRY-RUN] gh pr merge 77" in r.stdout and not world.merged()
    assert world.lanes() == []


def test_team_lead_iteration_key_carries_the_head_sha(world):
    world.serve("act")
    _run_team_lead(world)
    assert world.merged()
    body = json.loads(world.server.requests[0]["body"])
    assert body["decision_type"] == "engine_merge" and body["request_key"] == "merge:77:" + SHA
    assert body["subject"]["pr"] == 77 and body["subject"]["head_sha"] == SHA
    assert world.server.violations == []


# ---------------------------------------------------------------------------------------------------------
# loop-phased-step5.sh, driven end to end in the throwaway copy
# ---------------------------------------------------------------------------------------------------------

def _step5(w, gate_sha=SHA, disc=30001, pr=30000):
    bb = Path(w.state) / "blackboard" / "pr_state"
    bb.mkdir(parents=True, exist_ok=True)
    (bb / ("%d.json" % pr)).write_text(json.dumps({"value": {
        "pr": pr, "discussion": disc, "phase": "merging", "spawned_phases": [], "completed_phases": [],
        "needs_security_review": False, "fix_cycle_count": 0, "respawn_count": 0, "last_envelope": {},
        "blocked_reason": None, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"},
        "version": 1, "updated_at": "2026-01-01T00:00:00+00:00", "updated_by": "test"}))
    snap = w.tmp / "snap.json"
    snap.write_text(json.dumps({"discussions": [{"number": disc, "title": "t", "body": "<!-- STATUS:SPEC_READY --> x"}],
                                "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}))
    cfg = w.tmp / "cp.json"
    cfg.write_text(json.dumps({"gates": {"auto_merge": True, "security_review": False, "budget_check": False,
                                         "phased_orchestration": True, "phased_code_review": True},
                               "policies": {}, "settings": {}, "audit_log": []}))
    extra = {"AF_CONTROL_PLANE_CONFIG": str(cfg), "SNAPSHOT_PATH": str(snap), "STEP5_TEST_MODE": "1",
             "SPAWN_AGENT": "echo", "GH_MERGE": "echo", "HOOKS_DISABLED": "1", "DASHBOARD_TOUCHED": "no",
             "SECURITY_TRIGGER_RESULT": "no", "DISCUSSING_MOCK": "[]",
             "HAS_LABEL_%d_code_review_passed" % pr: "yes", "HEAD_SHA_%d" % pr: SHA}
    if gate_sha:
        extra["CI_PASSED_SHA"] = gate_sha
    return subprocess.run(["bash", w.root + "/scripts/loop-phased-step5.sh"], cwd=w.root, env=w.env(**extra),
                          capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("mode", sorted(MATRIX))
def test_step5_matrix(world, mode):
    code, merge = MATRIX[mode]
    world.serve(mode)
    r = _step5(world)
    out = r.stdout + r.stderr
    assert ("GH_MERGE_ARGS" in out) == merge, out
    assert world.server.violations == []
    assert json.loads(world.server.requests[0]["body"])["request_key"] == "merge:30000:" + SHA
    if merge:
        assert "--match-head-commit " + SHA in out and "merged successfully" in out
        assert world.lanes() == []
    else:
        assert "cloud decide refused the merge (exit %d" % code in out
        assert "merged successfully" not in out and "merge failed" not in out
        assert world.lanes() == ["refused"]
        pr_state = json.load(open(world.state + "/blackboard/pr_state/30000.json"))
        assert pr_state["value"]["phase"] == "merging"


def test_step5_operate_off_or_absent_is_unchanged(world):
    for op in (False, "absent"):
        world.config(op)
        r = _step5(world, gate_sha=None)
        out = r.stdout + r.stderr
        assert "GH_MERGE_ARGS" in out and "merging unpinned" in out and "--match-head-commit" not in out
    assert not os.path.exists(world.sock)
    assert world.lanes() == []


def test_step5_without_a_gated_sha_decides_on_the_live_head_and_pins_it(world):
    world.serve("act")
    r = _step5(world, gate_sha=None)
    out = r.stdout + r.stderr
    assert "--match-head-commit " + SHA in out and "merged successfully" in out, out
    assert json.loads(world.server.requests[0]["body"])["request_key"] == "merge:30000:" + SHA


# ---------------------------------------------------------------------------------------------------------
# import-epic-tasks.py: creating a Discussion that is already SPEC_READY
# ---------------------------------------------------------------------------------------------------------

def _load_importer():
    spec = importlib.util.spec_from_file_location("import_epic_tasks_eb10d", os.path.join(ROOT, "scripts", "import-epic-tasks.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeClient:
    def __init__(self):
        self.created = []

    def create_discussion(self, title, body):
        self.created.append((title, body))
        return 901, "node"


def _item(w, name="T1"):
    epic_dir = w.tmp / "epics" / "epic-1"
    epic_dir.mkdir(parents=True, exist_ok=True)
    path = epic_dir / (name + ".md")
    path.write_text("body")
    fm = {"schema_version": 1, "epic": 1, "task": name, "title": "Do it", "type": "feature"}
    return {"fm": fm, "path": path, "file_body": "text", "epic_dir": epic_dir, "stem": name}


@pytest.mark.parametrize("mode", sorted(MATRIX))
def test_importer_v1_creation_matrix(world, mode):
    code, create = MATRIX[mode]
    world.serve(mode)
    mod = _load_importer()
    client = FakeClient()
    number = mod._create_v1(_item(world), client, False, "2026-11-16T00:00:00Z", [], world.root)
    assert (number == 901) == create and bool(client.created) == create
    assert world.server.violations == []
    for r in world.server.requests:
        sent = json.loads(r["body"])
        assert sent["decision_type"] == "engine_spec_ready" and sent["request_key"].startswith("spec:import-1-T1:")
        assert "discussion" not in sent["subject"] and sent["subject"]["tag"] == "feature"


def test_importer_spec_key_ignores_since_stamp_but_tracks_body(world):
    world.serve(sorted(MATRIX)[0])
    mod = _load_importer()

    def key(now_iso, text="text"):
        world.server.requests.clear()
        item = _item(world)
        item["file_body"] = text
        mod._create_v1(item, FakeClient(), False, now_iso, [], world.root)
        return json.loads(world.server.requests[-1]["body"])["request_key"]

    a = key("2026-11-16T00:00:00Z")
    assert a == key("2026-11-17T09:30:00Z")
    assert a != key("2026-11-16T00:00:00Z", text="changed body")


@pytest.mark.parametrize("mode", sorted(MATRIX))
def test_importer_create_or_drift_matrix(world, mode):
    code, create = MATRIX[mode]
    world.serve(mode)
    mod = _load_importer()
    client = FakeClient()
    created_map = {}
    rc = mod._create_or_drift(_item(world, "T2"), client, created_map, False, "2026-11-16T00:00:00Z", [], world.root)
    assert bool(client.created) == create
    assert rc == (0 if code in (0, 3) else 1)


def test_importer_operate_off_creates_without_asking_and_dry_run_asks_nothing(world):
    mod = _load_importer()
    for op in (False, "absent"):
        world.config(op)
        client = FakeClient()
        assert mod._create_v1(_item(world), client, False, "2026-11-16T00:00:00Z", [], world.root) == 901
    world.config(True)
    world.serve("ask")
    client = FakeClient()
    assert mod._create_v1(_item(world), client, True, "2026-11-16T00:00:00Z", [], world.root) is None
    assert client.created == []


def test_importer_v0_discussing_body_is_not_a_spec_flip(world):
    world.serve("ask")
    mod = _load_importer()
    item = _item(world, "T3")
    item["fm"].pop("schema_version")
    client = FakeClient()
    rc = mod._create_or_drift(item, client, {}, False, "2026-11-16T00:00:00Z", None, world.root)
    assert rc == 0 and len(client.created) == 1
