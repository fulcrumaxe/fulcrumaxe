#!/usr/bin/env python3
"""cloud_decide.py: ask the cloud whether the engine may do something, over the runner's local socket.

The request is HTTP/1.1 over the runner's Unix socket: POST /api/runner/engine/decide with a JSON body
(repo_gh_id, decision_type, request_key, subject, command_id?). The runner signs and forwards it, so no
credential travels on this hop: the socket's owner and the peer uid are the authentication.
Every failure to get a clean, well-formed 2xx answer is a refusal (fail closed).

CLI: cloud_decide.py decide --type engine_merge --key K [--pr N] [--discussion N] [--head-sha S]
                            [--role R] [--tag T] [--command-id U] [--repo-root DIR]
stdout: one line "<outcome> receipt=<uuid|-> ask=<uuid|->"
exit:   0 act/announce   3 ask (a person must answer)   4 halted   5 cloud unreachable/unusable
        2 usage error (never reached by a merge; the hook maps anything unexpected to 5)
Stderr carries fixed reason codes only, never text received from the peer.
"""
import argparse
import json
import os
import re
import socket
import stat
import struct
import sys
import time

DECIDE_PATH = "/api/runner/engine/decide"
TIMEOUT_S = 10.0
MAX_REPLY = 64 * 1024
MAX_CONFIG = 1024 * 1024
DECISION_TYPES = ("engine_spawn", "engine_merge", "engine_spec_ready")
TAGS = ("feature", "critical", "small", "bug", "doc", "process", "other")
ROLES = ("executor", "code-reviewer", "security-reviewer", "acceptance-tester", "browser-tester",
         "project-manager", "technical-architect", "product-owner", "cost-analyst",
         "performance-expert", "security-expert", "other")
KEY_RE = re.compile(r"[A-Za-z0-9:_-]{1,128}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EXIT_ACT, EXIT_ASK, EXIT_HALTED, EXIT_UNREACHABLE = 0, 3, 4, 5


class Refused(Exception):
    """The decide cannot be trusted. The message is a fixed reason code."""


def state_dir():
    return os.environ.get("AUTONOMOUS_TEAM_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".autonomous-forever-state")


def _read_json_nofollow(path, cap, what):
    """None when the file does not exist; Refused when it is a symlink, not a file, too big or not JSON."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused(what + "_unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            raise Refused(what + "_unreadable")
        data = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refused(what + "_unparsable")


def load_bridge(repo_root):
    """The cloud_bridge block of .autonomous-team/config.json, or {} when there is none."""
    cfg = _read_json_nofollow(os.path.join(repo_root, ".autonomous-team", "config.json"), MAX_CONFIG, "config")
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        raise Refused("config_unparsable")
    bridge = cfg.get("cloud_bridge")
    if bridge is None:
        return {}
    if not isinstance(bridge, dict):
        raise Refused("config_bad_bridge")
    return bridge


def operate_enabled(bridge):
    """False only when operate is absent or exactly false; any other non-true value is a misconfiguration."""
    op = bridge.get("operate", False)
    if op is True:
        return True
    if op is False:
        return False
    raise Refused("config_bad_operate")


def halted_locally():
    halt = _read_json_nofollow(os.path.join(state_dir(), "engine-halt.json"), MAX_CONFIG, "halt_file")
    if halt is None:
        return False
    if not isinstance(halt, dict) or not isinstance(halt.get("halted"), bool):
        raise Refused("halt_file_unparsable")
    return halt["halted"]


def socket_path(bridge):
    p = bridge.get("socket_path")
    if p is None:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if not xdg:
            raise Refused("socket_missing")
        p = os.path.join(xdg, "fx-runner", "engine-bridge.sock")
    if not isinstance(p, str) or not os.path.isabs(p) or len(p.encode()) > 100 or "\0" in p:
        raise Refused("socket_path_bad")
    return p


def check_socket_file(path):
    """The socket and its directory must be ours, private, and not symlinks."""
    me = os.getuid()
    try:
        st = os.lstat(path)
        dst = os.lstat(os.path.dirname(path))
    except OSError:
        raise Refused("socket_missing")
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != me or st.st_mode & 0o077:
        raise Refused("socket_untrusted")
    if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != me or dst.st_mode & 0o077:
        raise Refused("socket_dir_untrusted")


def build_request(repo_gh_id, decision_type, key, subject, command_id=None):
    if decision_type not in DECISION_TYPES or not KEY_RE.fullmatch(key or ""):
        raise Refused("request_invalid")
    if not (isinstance(repo_gh_id, int) and not isinstance(repo_gh_id, bool) and repo_gh_id >= 1):
        raise Refused("request_invalid")
    subj = {}
    for k in ("discussion", "pr"):
        v = subject.get(k)
        if isinstance(v, str) and re.fullmatch(r"[1-9][0-9]{0,15}", v):
            v = int(v)
        if v is not None:
            if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 2**53 - 1):
                raise Refused("request_invalid")
            subj[k] = v
    if subject.get("role") is not None:
        if subject["role"] not in ROLES:
            raise Refused("request_invalid")
        subj["role"] = subject["role"]
    if subject.get("head_sha") is not None:
        if not SHA_RE.fullmatch(subject["head_sha"]):
            raise Refused("request_invalid")
        subj["head_sha"] = subject["head_sha"]
    if subject.get("tag") is not None:
        if subject["tag"] not in TAGS:
            raise Refused("request_invalid")
        subj["tag"] = subject["tag"]
    body = {"repo_gh_id": repo_gh_id, "decision_type": decision_type, "request_key": key, "subject": subj}
    if command_id is not None:
        if not UUID_RE.fullmatch(command_id):
            raise Refused("request_invalid")
        body["command_id"] = command_id
    return body


def _read_until(sock, deadline, buf, stop):
    """Read into buf until stop(buf) is true or EOF; bounded in size and time."""
    while not stop(buf):
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused("timeout")
        sock.settimeout(left)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            raise Refused("timeout")
        except OSError:
            raise Refused("io_error")
        if not chunk:
            return False
        buf += chunk
        if len(buf) > MAX_REPLY + 8192:
            raise Refused("reply_too_big")
    return True


def _decode_chunked(raw):
    out, i = b"", 0
    while True:
        j = raw.find(b"\r\n", i)
        if j < 0:
            raise Refused("reply_malformed")
        try:
            n = int(raw[i:j].split(b";")[0], 16)
        except ValueError:
            raise Refused("reply_malformed")
        i = j + 2
        if n == 0:
            return out
        if i + n + 2 > len(raw) or raw[i + n:i + n + 2] != b"\r\n":
            raise Refused("reply_malformed")
        out += raw[i:i + n]
        i += n + 2


def exchange(path, body, timeout=TIMEOUT_S):
    """Send the request, return (status, body bytes). Raises Refused on every failure."""
    deadline = time.monotonic() + timeout
    payload = json.dumps(body, separators=(",", ":")).encode()
    head = ("POST %s HTTP/1.1\r\nHost: engine-bridge\r\nContent-Type: application/json\r\n"
            "Accept: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
            % (DECIDE_PATH, len(payload))).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        try:
            s.connect(path)
        except socket.timeout:
            raise Refused("timeout")
        except OSError:
            raise Refused("connect_failed")
        _pid, uid, _gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != os.getuid():
            raise Refused("peer_uid_mismatch")
        s.settimeout(max(deadline - time.monotonic(), 0.001))
        try:
            s.sendall(head + payload)
        except socket.timeout:
            raise Refused("timeout")
        except OSError:
            raise Refused("io_error")
        buf = bytearray()
        if not _read_until(s, deadline, buf, lambda b: b"\r\n\r\n" in b):
            raise Refused("reply_malformed")
        raw_head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        lines = raw_head.decode("latin-1").split("\r\n")
        m = re.fullmatch(r"HTTP/1\.[01] ([1-5][0-9]{2})(?: .*)?", lines[0])
        if not m:
            raise Refused("reply_malformed")
        status = int(m.group(1))
        headers = {}
        for ln in lines[1:]:
            k, sep, v = ln.partition(":")
            if not sep or k.strip().lower() in headers:
                raise Refused("reply_malformed")
            headers[k.strip().lower()] = v.strip()
        if not 200 <= status < 300:
            raise Refused("http_%dxx" % (status // 100))
        if not headers.get("content-type", "").lower().startswith("application/json"):
            raise Refused("reply_malformed")
        rest = bytearray(rest)
        if headers.get("transfer-encoding", "").lower() == "chunked":
            _read_until(s, deadline, rest, lambda b: b.endswith(b"0\r\n\r\n"))
            data = _decode_chunked(bytes(rest))
        elif "content-length" in headers:
            if not re.fullmatch(r"[0-9]{1,6}", headers["content-length"]):
                raise Refused("reply_malformed")
            n = int(headers["content-length"])
            if n > MAX_REPLY:
                raise Refused("reply_too_big")
            _read_until(s, deadline, rest, lambda b: len(b) >= n)
            if len(rest) < n:
                raise Refused("reply_malformed")
            data = bytes(rest[:n])
        else:
            _read_until(s, deadline, rest, lambda b: False)
            data = bytes(rest)
        if len(data) > MAX_REPLY:
            raise Refused("reply_too_big")
        return status, data
    finally:
        s.close()


def parse_reply(data):
    """The EngineDecideReply shape, exactly: no missing keys, no extra keys, no wrong types."""
    try:
        r = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refused("reply_malformed")
    keys = {"disposition", "receipt_id", "ask_id", "answered", "halted"}
    if not isinstance(r, dict) or set(r) != keys:
        raise Refused("reply_malformed")
    if r["disposition"] not in ("act", "announce", "ask") or not isinstance(r["halted"], bool):
        raise Refused("reply_malformed")
    for k in ("receipt_id", "ask_id"):
        if r[k] is not None and not (isinstance(r[k], str) and UUID_RE.fullmatch(r[k])):
            raise Refused("reply_malformed")
    if r["answered"] not in (None, "approve", "decline"):
        raise Refused("reply_malformed")
    if r["disposition"] != "ask" and r["answered"] == "decline":
        raise Refused("reply_malformed")
    return r


def decide(repo_root, decision_type, key, subject, command_id=None, timeout=TIMEOUT_S):
    """Returns (exit_code, outcome, reply|None). Never raises: any Refused becomes exit 5."""
    try:
        bridge = load_bridge(repo_root)
        if not operate_enabled(bridge):
            return EXIT_ACT, "off", None
        if halted_locally():
            return EXIT_HALTED, "halted", None
        repo_gh_id = bridge.get("repo_gh_id")
        path = socket_path(bridge)
        body = build_request(repo_gh_id, decision_type, key, subject, command_id)
        check_socket_file(path)
        _status, data = exchange(path, body, timeout)
        reply = parse_reply(data)
    except Refused as e:
        return EXIT_UNREACHABLE, "unreachable:%s" % e, None
    if reply["halted"]:
        return EXIT_HALTED, "halted", reply
    if reply["disposition"] == "ask":
        return EXIT_ASK, "ask", reply
    return EXIT_ACT, reply["disposition"], reply


def main(argv):
    ap = argparse.ArgumentParser(prog="cloud_decide.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--type", required=True, dest="dtype")
    d.add_argument("--key", required=True)
    d.add_argument("--pr")
    d.add_argument("--discussion")
    d.add_argument("--head-sha")
    d.add_argument("--role")
    d.add_argument("--tag")
    d.add_argument("--command-id")
    d.add_argument("--repo-root", default=os.getcwd())
    a = ap.parse_args(argv)
    subject = {"pr": a.pr, "discussion": a.discussion, "head_sha": a.head_sha, "role": a.role, "tag": a.tag}
    code, outcome, reply = decide(a.repo_root, a.dtype, a.key, subject, a.command_id)
    r = reply or {}
    reason = outcome.split(":", 1)[1] if outcome.startswith("unreachable:") else ""
    if reason:
        print("cloud_decide: refused (%s)" % reason, file=sys.stderr)
        outcome = "unreachable"
    answered = r.get("answered") or "-"
    print("%s receipt=%s ask=%s answered=%s" % (outcome, r.get("receipt_id") or "-", r.get("ask_id") or "-", answered))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        print("cloud_decide: refused (internal_error)", file=sys.stderr)
        print("unreachable receipt=- ask=- answered=-")
        sys.exit(EXIT_UNREACHABLE)
