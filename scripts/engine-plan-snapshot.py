#!/usr/bin/env python3
"""engine-plan-snapshot.py: emit the day's plan and the roadmap as plan_snapshot events.

  engine-plan-snapshot.py [--root DIR] [--plan-date YYYY-MM-DD] [--if-changed]

Items come from .autonomous-team/PLAN-<date>.md (else the newest PLAN-*.md): every
distinct D#N, in order of first appearance, "merged" when a line naming it carries a
checked box and "planned" otherwise. Roadmap rows come from the task_status table in
.autonomous-team/roadmap.json. A row that does not fit the closed shapes (key form,
status, planned_prs) is left out and counted on stderr; the snapshot is still sent.

A snapshot is sent in chunks of at most 500 rows (and well under 64 KiB each) that share
a snapshot_id, so the reader applies it only once every chunk has arrived. --if-changed
does nothing unless the plan or roadmap file changed since the last snapshot.
Exit: 0 sent or nothing to do, 2 refused, 1 could not write.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import engine_emit  # noqa: E402

MAX_ROWS, MAX_BYTES, MAX_CHUNKS = 500, 60000, 100
CURSOR = "engine-plan-cursor.json"
ALL = engine_emit.load_schema()["$defs"]
DEFS = ALL["plan_snapshot"]["properties"]
STATUSES = set(DEFS["roadmap"]["items"]["properties"]["status"]["enum"])
KEY_RE = re.compile(r"D#(\d+)(?::(.*))?")


def plan_items(path):
    first, merged = {}, set()
    for line in open(path, encoding="utf-8", errors="replace"):
        checked = re.match(r"\s*(?:[-*]|\d+\.)\s*\[[xX]\]", line) is not None
        for m in re.finditer(r"D#(\d+)", line):
            n = int(m.group(1))
            first.setdefault(n, len(first))
            if checked:
                merged.add(n)
    return [{"discussion": n, "ordinal": o, "state": "merged" if n in merged else "planned"}
            for n, o in sorted(first.items(), key=lambda kv: kv[1])]


def roadmap_rows(path):
    rows, skipped = [], 0
    with open(path) as f:
        table = json.load(f).get("task_status", {})
    for key, v in table.items():
        m = KEY_RE.fullmatch(key) if isinstance(v, dict) else None
        row = {"row_key": (m.group(2) or "") if m else "", "discussion": int(m.group(1)) if m else 0,
               "status": v.get("status") if isinstance(v, dict) else None,
               "planned_prs": v.get("planned_prs") if isinstance(v, dict) else None}
        ok = m and row["status"] in STATUSES and engine_emit._check(
            row, DEFS["roadmap"]["items"], ALL) is None
        if ok:
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


def chunks(plan_date, items, rows):
    sid = uuid.uuid4().hex[:16]
    pending = [("items", x) for x in items] + [("roadmap", x) for x in rows]
    out, cur = [], {"items": [], "roadmap": []}
    for part, x in pending:
        cur[part].append(x)
        size = len(json.dumps(cur, separators=(",", ":")))
        if len(cur["items"]) + len(cur["roadmap"]) > MAX_ROWS or size > MAX_BYTES:
            cur[part].pop()
            out.append(cur)
            cur = {"items": [], "roadmap": []}
            cur[part].append(x)
    out.append(cur)
    if len(out) > MAX_CHUNKS:
        raise engine_emit.EmitError("too many chunks")
    return [{"plan_date": plan_date, "snapshot_id": sid, "chunk_index": i, "chunk_count": len(out), **c}
            for i, c in enumerate(out)]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("ENGINE_EMIT_REPO_ROOT") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--plan-date", default=datetime.date.today().isoformat())
    ap.add_argument("--if-changed", action="store_true")
    a = ap.parse_args(argv)
    at = os.path.join(a.root, ".autonomous-team")
    plan = os.path.join(at, "PLAN-%s.md" % a.plan_date)
    if not os.path.isfile(plan):
        found = sorted(glob.glob(os.path.join(at, "PLAN-*.md")), key=os.path.getmtime)
        plan = found[-1] if found else None
    road = os.path.join(at, "roadmap.json")
    road = road if os.path.isfile(road) else None
    if not plan and not road:
        return 0
    mt = max(os.stat(p).st_mtime for p in (plan, road) if p)
    sdir = engine_emit.state_dir()
    cpath = os.path.join(sdir, CURSOR)
    try:
        if a.if_changed and json.load(open(cpath)).get("mtime") == mt:
            return 0
    except (OSError, ValueError):
        pass
    try:
        date = re.search(r"PLAN-(\d{4}-\d{2}-\d{2})\.md$", plan or "")
        items = plan_items(plan) if plan else []
        rows, skipped = roadmap_rows(road) if road else ([], 0)
        if skipped:
            print("engine-plan-snapshot: %d roadmap row(s) left out (did not fit)" % skipped, file=sys.stderr)
        events = chunks(date.group(1) if date else a.plan_date, items, rows)
        for e in events:
            engine_emit.validate({"v": 1, "seq": 0, "ts": "2026-01-01T00:00:00Z", "kind": "plan_snapshot",
                                  "engine_instance_id": str(uuid.uuid4()), **e})
        for e in events:
            engine_emit.emit("plan_snapshot", e, sdir)
        fd = os.open(cpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"mtime": mt}, f)
    except engine_emit.EmitError as e:
        print("engine-plan-snapshot: refused: %s" % e, file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print("engine-plan-snapshot: failed (%s)" % e.__class__.__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
