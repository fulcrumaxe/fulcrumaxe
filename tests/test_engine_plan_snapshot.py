"""engine-plan-snapshot.py: PLAN items, roadmap rows, chunking and the closed shapes."""
import importlib.util
import json
import os
import re
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SNAP = os.path.join(ROOT, "scripts", "engine-plan-snapshot.py")

PLAN = """# PLAN 2026-11-16

1. **D#37 WS-F9b**: onboarding step.
2. **D#2 H14c-3-3b**: continuation, see also D#7.
- [x] D#101 RM-1 merged yesterday
- [ ] D#37 again
"""


def roadmap(n_extra=0):
    ts = {
        "D#102": {"status": "done", "planned_prs": 1},
        "D#2:H14c-3-3b": {"status": "not_started", "planned_prs": 3},
        "D#2:H08-followup": {"status": "split", "planned_prs": 0},
        "D#483:product-role-cards": {"status": "deferred", "planned_prs": 1},
        "D#9:BAD": {"status": "mystery", "planned_prs": 1},          # status outside the set
        "D#9:BAD2": {"status": "done", "planned_prs": "many"},       # not a count
        "D#9:has space": {"status": "done", "planned_prs": 1},       # key form
        "not-a-d-number": {"status": "done", "planned_prs": 1},
    }
    for i in range(n_extra):
        ts["D#%d:ROW%d" % (1000 + i, i)] = {"status": "not_started", "planned_prs": 1}
    return {"version": 5, "task_status": ts}


def setup(tmp_path, n_extra=0):
    root = tmp_path / "root"
    (root / ".autonomous-team").mkdir(parents=True)
    (root / ".autonomous-team" / "PLAN-2026-11-16.md").write_text(PLAN)
    (root / ".autonomous-team" / "roadmap.json").write_text(json.dumps(roadmap(n_extra)))
    return root, tmp_path / "state"


def run(root, state, *args):
    env = dict(os.environ, AUTONOMOUS_TEAM_STATE_DIR=str(state))
    return subprocess.run(["python3", SNAP, "--root", str(root), "--plan-date", "2026-11-16", *args], env=env,
                          capture_output=True, text=True)


def events(state):
    return [json.loads(x) for x in open(state / "engine-events.jsonl").read().splitlines()]


def test_one_snapshot_with_every_plan_discussion_and_the_fitting_roadmap_rows(tmp_path):
    root, state = setup(tmp_path)
    r = run(root, state)
    assert r.returncode == 0, r.stderr
    (ev,) = events(state)
    assert ev["kind"] == "plan_snapshot" and (ev["chunk_index"], ev["chunk_count"]) == (0, 1)
    assert re.fullmatch(r"[0-9a-f]{16}", ev["snapshot_id"]) and ev["plan_date"] == "2026-11-16"
    assert [(i["discussion"], i["ordinal"], i["state"]) for i in ev["items"]] == [
        (37, 0, "planned"), (2, 1, "planned"), (7, 2, "planned"), (101, 3, "merged")]
    rows = {(x["discussion"], x["row_key"]): (x["status"], x["planned_prs"]) for x in ev["roadmap"]}
    assert rows == {(102, ""): ("done", 1), (2, "H14c-3-3b"): ("not_started", 3),
                    (2, "H08-followup"): ("split", 0), (483, "product-role-cards"): ("deferred", 1)}
    assert "4 roadmap row(s) left out" in r.stderr  # counted, never named
    assert "mystery" not in r.stderr and "BAD" not in r.stderr


def test_a_large_roadmap_is_chunked_under_the_row_and_size_caps(tmp_path):
    root, state = setup(tmp_path, n_extra=1540)
    assert run(root, state).returncode == 0
    ev = events(state)
    assert len(ev) >= 4
    assert len({e["snapshot_id"] for e in ev}) == 1
    assert [e["chunk_index"] for e in ev] == list(range(len(ev)))
    assert {e["chunk_count"] for e in ev} == {len(ev)}
    for e in ev:
        assert len(e["items"]) + len(e["roadmap"]) <= 500
        assert len(json.dumps(e)) < 64 * 1024
    assert sum(len(e["roadmap"]) for e in ev) == 1544
    assert sum(len(e["items"]) for e in ev) == 4


def test_if_changed_skips_an_unchanged_plan_and_resends_after_a_change(tmp_path):
    root, state = setup(tmp_path)
    run(root, state, "--if-changed")
    run(root, state, "--if-changed")
    assert len(events(state)) == 1
    plan = root / ".autonomous-team" / "PLAN-2026-11-16.md"
    plan.write_text(PLAN + "- [ ] D#55 new\n")
    os.utime(plan, (os.path.getmtime(plan) + 5, os.path.getmtime(plan) + 5))
    run(root, state, "--if-changed")
    assert len(events(state)) == 2


def test_nothing_to_snapshot_is_not_an_error(tmp_path):
    root = tmp_path / "empty"
    (root / ".autonomous-team").mkdir(parents=True)
    r = run(root, tmp_path / "state")
    assert r.returncode == 0 and not os.path.exists(tmp_path / "state" / "engine-events.jsonl")


def test_chunks_keep_the_items_list_under_the_protocol_cap():
    snap = importlib.util.spec_from_file_location("engine_plan_snapshot", SNAP)
    mod = importlib.util.module_from_spec(snap)
    snap.loader.exec_module(mod)
    items = [{"discussion": n, "ordinal": n, "state": "planned"} for n in range(1, 701)]
    out = mod.chunks("2026-11-16", items, [])
    assert len(out) >= 3 and all(len(c["items"]) <= 300 for c in out)
    assert sum(len(c["items"]) for c in out) == 700
