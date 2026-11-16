"""cloud_ceiling.py: the dial ceiling a cloud-operated engine respects (EB-10c).

The runner writes <state dir>/cloud-dial-ceiling.json from the cloud's dials reply. With
cloud_bridge.operate true, dial_registry.check() lets no dial class below take effect above its ceiling.
The ceiling only ever lowers a level, never raises it. A file that is missing, expired, not ours, open to
others, a link, unparsable, of another repo or of the wrong shape gives ceiling 1 for all three classes.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
FILE = "cloud-dial-ceiling.json"
CLASSES = ("agent.spawn", "merge.standard", "merge.fast-path")
MAX_BYTES = 16 * 1024
MAX_LIFE_S = 15 * 60
SKEW_S = 300


def _cd():
    lib = os.path.join(REPO_ROOT, "scripts", "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import cloud_decide  # noqa: PLC0415
    return cloud_decide


def _ts(value):
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo is not None else None


def _read_ours(path):
    """The parsed JSON of a regular file we own that no one else can read or write; None otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > MAX_BYTES:
            return None
        return json.loads(os.read(fd, MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def operate_on(repo_root=None):
    """cloud_bridge.operate; a config that cannot be read counts as on (fail closed)."""
    cd = _cd()
    try:
        return cd.operate_enabled(cd.load_bridge(repo_root or REPO_ROOT))
    except cd.Refused:
        return True


def load(repo_root=None, now=None):
    """{class: ceiling} from a valid file for this repo, or None."""
    cd = _cd()
    root = repo_root or REPO_ROOT
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    try:
        bridge = cd.load_bridge(root)
    except cd.Refused:
        return None
    doc = _read_ours(os.path.join(cd.state_dir(), FILE))
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    gh_id = doc.get("repo_gh_id")
    if isinstance(gh_id, bool) or not isinstance(gh_id, int) or gh_id != bridge.get("repo_gh_id") or isinstance(bridge.get("repo_gh_id"), bool):
        return None
    fetched, expires = _ts(doc.get("fetched_at")), _ts(doc.get("expires_at"))
    if fetched is None or expires is None or expires <= now or fetched > now + SKEW_S or not 0 < expires - fetched <= MAX_LIFE_S:
        return None
    ceilings = doc.get("ceilings")
    if not isinstance(ceilings, dict) or any(isinstance(ceilings.get(c), bool) or not isinstance(ceilings.get(c), int) or not 1 <= ceilings[c] <= 5 for c in CLASSES):
        return None
    if doc.get("halted") is not False:
        return {c: 1 for c in CLASSES}  # halted, or a flag we cannot read
    return {c: ceilings[c] for c in CLASSES}


def ceiling_for(class_name, repo_root=None, now=None):
    """The ceiling for a class, or None when none applies (operate off, or a class the cloud does not limit)."""
    if class_name not in CLASSES or not operate_on(repo_root):
        return None
    ceilings = load(repo_root, now)
    return 1 if ceilings is None else ceilings[class_name]
