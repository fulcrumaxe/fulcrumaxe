"""A fake of the runner's local decide socket that enforces the contract the real one does.

It speaks HTTP/1.1 over a Unix socket and refuses (HTTP 400, recorded in `violations`) any request that the
real route would refuse: wrong method or path, a body that is not exactly the EngineDecideRequest shape,
a missing Content-Length, a caller whose uid is not the server's (SO_PEERCRED). Replies are chosen by `mode`.

What it cannot fake: signing and forwarding to the cloud (the runner does that), and the cloud's own
disposition logic. Those belong to the cloud rows' tests.

CLI: fake_cloud_decide.py --sock PATH --mode MODE [--log FILE]   (runs until killed; one JSON line per request)
"""
import argparse
import json
import os
import re
import socket
import struct
import sys
import threading
import time

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
KEY_RE = re.compile(r"[A-Za-z0-9:_-]{1,128}")
TYPES = {"engine_spawn", "engine_merge", "engine_spec_ready"}
SUBJECT_KEYS = {"discussion", "pr", "role", "head_sha", "tag"}


def reply_body(disposition, receipt=UUID_A, ask=None, answered=None, halted=False):
    return {"disposition": disposition, "receipt_id": receipt, "ask_id": ask, "answered": answered, "halted": halted}


# mode -> (status, content_type, body bytes or callable(request) -> bytes, extra)
def _json(o):
    return json.dumps(o).encode()


MODES = {
    "act": (200, "application/json", _json(reply_body("act"))),
    "announce": (200, "application/json", _json(reply_body("announce"))),
    "ask": (200, "application/json", _json(reply_body("ask", receipt=UUID_A, ask=UUID_B))),
    "ask_declined": (200, "application/json", _json(reply_body("ask", ask=UUID_B, answered="decline"))),
    "act_approved": (200, "application/json", _json(reply_body("act", ask=UUID_B, answered="approve"))),
    "halted": (200, "application/json", _json(reply_body("ask", receipt=None, halted=True))),
    "halted_act": (200, "application/json", _json(reply_body("act", halted=True))),
    "http_500": (500, "application/json", _json({"error": "internal"})),
    "http_503": (503, "application/json", _json({"error": "unavailable"})),
    "http_403": (403, "application/json", _json({"error": "operate_off"})),
    "http_409": (409, "application/json", _json({"error": "engine_halted"})),
    "http_302": (302, "application/json", b""),
    "not_json": (200, "application/json", b"<html>nope</html>"),
    "wrong_content_type": (200, "text/html", _json(reply_body("act"))),
    "empty_body": (200, "application/json", b""),
    "array_body": (200, "application/json", b"[]"),
    "extra_key": (200, "application/json", _json({**reply_body("act"), "extra": 1})),
    "missing_halted": (200, "application/json", _json({k: v for k, v in reply_body("act").items() if k != "halted"})),
    "bad_disposition": (200, "application/json", _json(reply_body("deny"))),
    "bad_receipt": (200, "application/json", _json(reply_body("act", receipt="not-a-uuid"))),
    "bad_answered": (200, "application/json", _json(reply_body("ask", ask=UUID_B, answered="defer"))),
    "act_declined": (200, "application/json", _json(reply_body("act", answered="decline"))),
    "halted_string": (200, "application/json", _json({**reply_body("act"), "halted": "false"})),
    "huge": (200, "application/json", b"{" + b" " * (200 * 1024) + b"}"),
}
RAW_MODES = ("garbage", "truncated", "hang", "close_early", "chunked_act", "no_length_act")


def validate_request(method, path, headers, raw):
    """Return a violation string, or None when the request is what the real route accepts."""
    if method != "POST" or path != "/api/runner/engine/decide":
        return "wrong method or path"
    if not headers.get("content-type", "").startswith("application/json"):
        return "content-type"
    if not re.fullmatch(r"[0-9]{1,6}", headers.get("content-length", "")) or int(headers["content-length"]) != len(raw):
        return "content-length"
    try:
        b = json.loads(raw)
    except ValueError:
        return "body not json"
    if not isinstance(b, dict) or not {"repo_gh_id", "decision_type", "request_key", "subject"} <= set(b) \
            or set(b) - {"repo_gh_id", "decision_type", "request_key", "subject", "command_id"}:
        return "body keys"
    if not (isinstance(b["repo_gh_id"], int) and not isinstance(b["repo_gh_id"], bool) and b["repo_gh_id"] >= 1):
        return "repo_gh_id"
    if b["decision_type"] not in TYPES or not KEY_RE.fullmatch(b["request_key"]):
        return "decision_type or request_key"
    s = b["subject"]
    if not isinstance(s, dict) or set(s) - SUBJECT_KEYS:
        return "subject keys"
    for k in ("discussion", "pr"):
        if k in s and not (isinstance(s[k], int) and not isinstance(s[k], bool) and s[k] >= 1):
            return "subject " + k
    if "head_sha" in s and not re.fullmatch(r"[0-9a-f]{40}", str(s["head_sha"])):
        return "subject head_sha"
    return None


class FakeDecide:
    def __init__(self, sock_path, mode="act", log=None):
        self.path, self.mode, self.log = sock_path, mode, log
        self.requests, self.violations = [], []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(sock_path), 0o700)
        self._srv.bind(sock_path)
        os.chmod(sock_path, 0o600)
        self._srv.listen(8)
        self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass

    def _loop(self):
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(5)
            _p, uid, _g = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(4096)
                if not d:
                    return
                buf += d
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _v = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            n = int(headers.get("content-length", "0") or 0)
            while len(rest) < n:
                d = c.recv(4096)
                if not d:
                    break
                rest += d
            v = None if uid == os.getuid() else "peer uid"
            v = v or validate_request(method, path, headers, rest[:n])
            self.requests.append({"violation": v, "body": rest[:n].decode("utf-8", "replace")})
            if self.log:
                with open(self.log, "a") as lf:
                    lf.write(json.dumps(self.requests[-1]) + "\n")
            if v:
                self.violations.append(v)
                self._send(c, 400, "application/json", _json({"error": "invalid_message"}))
                return
            if self.mode in RAW_MODES:
                self._raw(c)
            else:
                status, ctype, body = MODES[self.mode]
                self._send(c, status, ctype, body)
        except (OSError, ValueError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    @staticmethod
    def _send(c, status, ctype, body):
        c.sendall(("HTTP/1.1 %d X\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                   % (status, ctype, len(body))).encode() + body)

    def _raw(self, c):
        m = self.mode
        if m == "garbage":
            c.sendall(b"\x00\x01 not http at all\r\n\r\n")
        elif m == "truncated":
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n{\"disp")
        elif m == "hang":
            time.sleep(30)
        elif m == "close_early":
            return
        elif m == "chunked_act":
            body = _json(reply_body("act"))
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                      + ("%x\r\n" % len(body)).encode() + body + b"\r\n0\r\n\r\n")
        elif m == "no_length_act":
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n" + _json(reply_body("act")))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", required=True)
    ap.add_argument("--mode", required=True)
    ap.add_argument("--log")
    a = ap.parse_args(argv)
    f = FakeDecide(a.sock, a.mode, a.log)
    print("ready", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        f.close()


if __name__ == "__main__":
    main(sys.argv[1:])
