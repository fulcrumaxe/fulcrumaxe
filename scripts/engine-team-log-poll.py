#!/usr/bin/env python3
"""engine-team-log-poll.py: turn new team-log comments into closed-shape engine events.

Reads new comments on the open team-log issue through `gh` (the local token) and
emits team_log_line, plus ask_raised for a needs-boss line. The comment text is
classified locally and dropped: only enums, ids and numbers reach an event. The
cursor lives in the state directory, so a second run emits nothing new. Open asks
are re-read each run; one whose comment was deleted or no longer says needs-boss
is cleared as withdrawn.

Test seams: ENGINE_TEAM_LOG_REPO and ENGINE_TEAM_LOG_ISSUE skip the repo and issue lookup.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import engine_emit  # noqa: E402

CURSOR = "engine-team-log-cursor.json"
FIRST_RUN_MAX = 100
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LINE = re.compile(r"^\s*\[\d{1,2}:\d{2}\]\s+([A-Za-z][A-Za-z-]*?)(?:-\d+)?\s*:")
ROLES = {r for r in json.load(open(engine_emit.SCHEMA_PATH))["$defs"]["role"]["enum"] if r != "other"}
CATEGORIES = [("needs_boss", r"needs-boss"), ("blocked", r"\bblock"), ("merged", r"\bmerged\b"),
              ("spawning", r"\bspawn"), ("heartbeat", r"heartbeat"), ("activated", r"\bactivated\b"),
              ("starting", r"\bstart(?:ed|ing)\b|\bimplementing\b")]
ASK_KINDS = [("credential", r"credential|token|auth|secret"), ("budget", r"budget|usage|\bcap\b"),
             ("merge_blocked", r"merge"), ("review_blocked", r"review"),
             ("owner_decision", r"decision|owner|approve|approval")]


def gh(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("gh failed")
    return r.stdout


def classify(body):
    m = LINE.match(body)
    who = m.group(1).lower() if m else ""
    actor = who if who in ROLES else ("team-lead" if who in ("team-lead", "main") else "other")
    low = body.lower()
    cat = next((c for c, p in CATEGORIES if re.search(p, low)), "other")
    d = re.search(r"(?:\bD#|\bDiscussion #)(\d+)", body)
    p = re.search(r"\bPR ?#(\d+)", body)
    return actor, cat, int(d.group(1)) if d else None, int(p.group(1)) if p else None


def ask_kind(body):
    low = body.lower()
    return next((k for k, p in ASK_KINDS if re.search(p, low)), "other")


def h16(node_id):
    return hashlib.sha256(node_id.encode()).hexdigest()[:16]


def read_cursor(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            c = json.load(f)
        if isinstance(c.get("last_id"), int) and isinstance(c.get("open_asks"), dict):
            return c
    except (OSError, ValueError):
        pass
    return None


def write_cursor(path, c):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".cursor-")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f)
    os.replace(tmp, path)


def locate():
    repo = os.environ.get("ENGINE_TEAM_LOG_REPO")
    if not repo:
        repo = subprocess.run(["bash", "-c", 'source "$1/scripts/lib/repo-resolve.sh" && _resolve_repo', "_", ROOT],
                              capture_output=True, text=True).stdout.strip()
    issue = os.environ.get("ENGINE_TEAM_LOG_ISSUE")
    if not issue and repo:
        issue = gh(["issue", "list", "--repo", repo, "--label", "team-log", "--state", "open", "--limit", "50",
                    "--json", "number", "--jq", "sort_by(.number) | reverse | .[0].number"]).strip()
    if not repo or not issue.isdigit():
        raise RuntimeError("no team-log issue")
    return repo, int(issue)


def check_open_asks(repo, c, sdir):
    """Clear asks whose comment is gone. Returns True when anything was cleared."""
    cleared = False
    for ask_id, cid in list(c["open_asks"].items()):
        try:
            body = json.loads(gh(["api", "repos/%s/issues/comments/%d" % (repo, cid)])).get("body", "")
            gone = "needs-boss" not in body
        except RuntimeError:
            gone = True
        if gone:
            engine_emit.emit("ask_cleared", {"ask_id": ask_id, "how": "withdrawn"}, sdir)
            del c["open_asks"][ask_id]
            cleared = True
    return cleared


def main():
    sdir = engine_emit.state_dir()
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    cpath = os.path.join(sdir, CURSOR)
    repo, issue = locate()
    c = read_cursor(cpath)
    first = c is None or c.get("issue") != issue
    if first:
        c = {"issue": issue, "last_id": 0, "open_asks": {}}
    comments = json.loads(gh(["api", "--paginate", "--slurp", "repos/%s/issues/%d/comments?per_page=100" % (repo, issue)]))
    flat = sorted((x for page in comments for x in page), key=lambda x: x["id"])
    new = [x for x in flat if x["id"] > c["last_id"]]
    if first:
        new = new[-FIRST_RUN_MAX:]
    for x in new:
        body = x.get("body") or ""
        actor, cat, disc, pr = classify(body)
        lid = h16(x["node_id"])
        engine_emit.emit("team_log_line", {"line_id": lid, "actor_role": actor, "category": cat,
                                           "discussion": disc, "pr": pr}, sdir)
        if "needs-boss" in body:
            engine_emit.emit("ask_raised", {"ask_id": lid, "ask_kind": ask_kind(body), "source": "team_log",
                                            "discussion": disc, "pr": pr}, sdir)
            c["open_asks"][lid] = x["id"]
        c["last_id"] = x["id"]
        write_cursor(cpath, c)
    # The cursor changes only when the source did: its mtime is the team_log age on the heartbeat.
    if check_open_asks(repo, c, sdir) or first:
        write_cursor(cpath, c)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (engine_emit.EmitError, RuntimeError, OSError, ValueError) as e:
        print("engine-team-log-poll: %s" % e.__class__.__name__, file=sys.stderr)
        sys.exit(1)
