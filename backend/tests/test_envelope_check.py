"""
Tests for backend/envelope_check.py — the D#1791 PR 2 envelope fabrication
detector.

Covers the Spec's acceptance criteria 8-17:
  8-10  check_impossible_sources tri-state behaviour (impossible / unknown /
        legitimate), exercised through both the direct function and the CLI.
  11-12 resolve_claimed_artifact against the LIVE GitHub API — the fabricated
        comment id from the original 2026-07-29 incident (still unresolvable
        on the real D#1790 today) and a real, resolvable comment id.
  13    Fail-closed on an unreachable API: "unverified", never a silent pass,
        and it records.
  14    Every non-null finding writes one audit.jsonl row + one team-log
        line, exercised end-to-end through the real hook script.
  15    The finding is independent of role.
  16    The hook script exits 0 and never touches agent_run.verdict.
  17    No network call when no artifact is claimed.

Plus:
  - Two non-synthetic replay tests against the real captured fabrication
    transcripts from the second and third 2026-09-12 instances (see the
    module docstring below for why those transcripts are read from local
    state rather than committed as fixtures).
  - Credential scoping and redirect refusal: the Authorization header must
    never reach an overridden api_base, and a real local HTTP server
    proves a 302 is refused rather than followed (verified against this
    interpreter's actual behaviour, not against urllib's documentation).
  - The hook only reads a claimed artifact from the PARSED envelope, never
    from surrounding prose — a permalink merely mentioned in text must not
    trigger a network call, an audit row, or a team-log line.

The three tests against the live GitHub API (11, 12, and the CLI variant of
11) skip loudly, with a stated reason, when this environment cannot read
autonomous-agent-7/fulcrumaxe — never silently, since a silent skip here
would itself be the vacuous-gate pattern this whole detector exists to
catch.

All tests that touch the state dir set AUTONOMOUS_TEAM_STATE_DIR to a
pytest tmp_path first (CLAUDE.md's "export it, always" rule) — none of them
touch ~/.autonomous-forever-state/. Every test that could otherwise post a
real comment to the team log stubs scripts/rotate-team-log.sh via
ENVELOPE_CHECK_TEAM_LOG_SCRIPT; none of them post to the real GitHub Issue.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

import backend.envelope_check as envelope_check  # noqa: E402

# A real, live Discussion-comment permalink to check resolve_claimed_artifact
# against, plus one comment id under it that is known NOT to exist. This is
# this OPERATOR's own private companion repo, not a value every checkout of
# this public repo shares — so it is env-overridable rather than a bare
# literal, the same way pr-link-policy.sh resolves its own owner from
# $GITHUB_REPOSITORY rather than hardcoding one. Defaults describe this
# operator's current state (its own Discussion, closed, still carrying the
# comment id an earlier fabrication invented and never posted) and are built
# from parts rather than one static URL literal.
_LIVE_CHECK_OWNER = os.environ.get("ENVELOPE_CHECK_LIVE_TEST_OWNER", "autonomous-agent-7")
_LIVE_CHECK_REPO = os.environ.get("ENVELOPE_CHECK_LIVE_TEST_REPO", "fulcrumaxe")
_LIVE_CHECK_DISCUSSION = os.environ.get("ENVELOPE_CHECK_LIVE_TEST_DISCUSSION", "1790")
_LIVE_CHECK_ABSENT_COMMENT_ID = os.environ.get("ENVELOPE_CHECK_LIVE_TEST_ABSENT_COMMENT_ID", "11645892")
_LIVE_CHECK_PRESENT_COMMENT_ID = os.environ.get("ENVELOPE_CHECK_LIVE_TEST_PRESENT_COMMENT_ID", "17822079")


def _live_artifact_url(comment_id: str) -> str:
    return (
        f"https://github.com/{_LIVE_CHECK_OWNER}/{_LIVE_CHECK_REPO}"
        f"/discussions/{_LIVE_CHECK_DISCUSSION}#discussioncomment-{comment_id}"
    )


_D1790_URL_UNRESOLVED = _live_artifact_url(_LIVE_CHECK_ABSENT_COMMENT_ID)
_D1790_URL_RESOLVED = _live_artifact_url(_LIVE_CHECK_PRESENT_COMMENT_ID)

# ---------------------------------------------------------------------------
# Real captured fabrication transcripts (criteria 4/18-20 replacement).
#
# The Discussion's original acceptance criterion 4 wanted a replay against
# the actual 2026-07-29 captured envelope; the Spec had to renegotiate that
# to a reconstructed fixture because that original no longer exists on this
# host. Two NEW real fabrications happened later the same night the Spec was
# frozen, and both transcripts were preserved:
#
#   ~/.autonomous-forever-state/fabrication-evidence/
#     D1791-instance-2026-09-12-researcher-2565.jsonl
#     D1791-instance-2026-09-12-researcher-2565-second.jsonl
#
# This test reads them from there rather than committing copies into
# backend/tests/fixtures/. That is a deliberate deviation from the Spec's
# Implementation Notes (which named a fixture path) and from the executor
# brief's literal "use these": both transcripts embed this Discussion's own
# incident narrative verbatim in their first user turn (the researcher's
# task brief quoted D#1791's body at length) — publishing that into
# backend/tests/fixtures/ would put private Discussion-plane prose into the
# public code-plane repo this PR ships to, which is exactly what this
# project's own repo-scope rule ("never write private text outward") exists
# to prevent. Reading the real, unmodified transcript from local state at
# test time is a genuine, non-synthetic replay — it calls the real
# count_tool_uses() (PR 1, unmodified) against the real captured file and
# feeds the real result into the real evaluate() — without ever copying
# that file's prose into a public commit.
#
# The files are host-local, sensitive, and not guaranteed to exist on every
# checkout (a fresh clone, CI, another operator's host) — both tests below
# skip, rather than fail, when the file is absent.
#
# Deliberately NOT read via AUTONOMOUS_TEAM_STATE_DIR: CLAUDE.md requires
# every pytest invocation to redirect that variable to a scratch dir (the
# Real-world verification section's own prescribed command does exactly
# that), and this evidence is a fixed, externally-populated artifact, not
# per-run test state — following AUTONOMOUS_TEAM_STATE_DIR here would make
# the replay permanently unable to find it under the very invocation the
# Spec names. A dedicated variable instead, so this is still overridable
# (this operator's home directory is not every operator's) without
# colliding with the state-dir convention it deliberately does not follow.
_FABRICATION_DIR = Path(
    os.environ.get("ENVELOPE_CHECK_FABRICATION_EVIDENCE_DIR")
    or (Path.home() / ".autonomous-forever-state" / "fabrication-evidence")
)
_FABRICATION_FILES = [
    "D1791-instance-2026-09-12-researcher-2565.jsonl",
    "D1791-instance-2026-09-12-researcher-2565-second.jsonl",
]

_ENVELOPE_RE = re.compile(
    r"<!--\s*AGENT_OUTPUT\s*-->\s*```json\s*(.*?)\s*```\s*<!--\s*/AGENT_OUTPUT\s*-->",
    re.DOTALL,
)


def _load_subagent_payload():
    """Load the real, PR-1-merged scripts/lib/subagent_payload.py by path,
    so the replay test exercises the actual shipped count_tool_uses() rather
    than a re-implementation."""
    path = REPO_ROOT / "scripts" / "lib" / "subagent_payload.py"
    spec = importlib.util.spec_from_file_location("subagent_payload_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _extract_last_sources_count(transcript_path: str) -> int:
    """Test-only helper: walk a transcript-shaped JSONL file, find the LAST
    assistant message's AGENT_OUTPUT envelope, and return len(sources).
    Returns 0 on any read/parse failure — this is not a detector signal in
    its own right (envelope_check.py never needs this from a live run in
    this PR's scope; see the PR body), just a way to feed a real captured
    envelope's real sources count into evaluate() for the replay test."""
    last_count = 0
    try:
        with open(transcript_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                role = msg.get("role", "") if isinstance(msg, dict) else ""
                if role != "assistant":
                    continue
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    texts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                    ]
                    text = texts[-1] if texts else ""
                m = _ENVELOPE_RE.search(text)
                if not m:
                    continue
                try:
                    env = json.loads(m.group(1).strip())
                except Exception:
                    continue
                sources = env.get("sources") if isinstance(env, dict) else None
                if isinstance(sources, list):
                    last_count = len(sources)
    except (OSError, IOError):
        return 0
    return last_count


def _write_stub_team_log(tmp_path: Path, marker_name: str = "stub-team-log-calls.log") -> tuple[Path, Path]:
    """Write a stub rotate-team-log.sh that records its args instead of
    posting to the real GitHub Issue. Returns (stub_path, marker_path)."""
    marker = tmp_path / marker_name
    stub = tmp_path / "stub-rotate-team-log.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{marker}"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub, marker


def _probe_live_discussion_plane_access() -> tuple[bool, str]:
    """A minimal, independent reachability probe — deliberately not routed
    through resolve_claimed_artifact, so a bug in the function under test
    can never disguise itself as an access problem. Returns (ok, reason)."""
    try:
        data = envelope_check._graphql_call(  # noqa: SLF001
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){id}}",
            {"owner": _LIVE_CHECK_OWNER, "name": _LIVE_CHECK_REPO},
            envelope_check.DEFAULT_API_BASE,
            5.0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"
    repo = (data.get("data") or {}).get("repository") if isinstance(data, dict) else None
    if not repo:
        return False, f"unexpected response: {str(data)[:300]!r}"
    return True, ""


def _skip_unless_live_discussion_plane_reachable() -> None:
    """Loud, reasoned skip — never a silent one. A contributor without read
    access to autonomous-agent-7/fulcrumaxe (this operator's private
    companion repo) will hit this on every one of the three live-API tests;
    that is an environment/credential limitation, not a code failure, but a
    SILENT skip here would itself be exactly the vacuous-gate pattern D#1791
    is about — a check that looks like it ran and didn't. So this always
    explains, by name, why it isn't running."""
    ok, reason = _probe_live_discussion_plane_access()
    if not ok:
        pytest.skip(
            "skipping live-API check: cannot read "
            f"{_LIVE_CHECK_OWNER}/{_LIVE_CHECK_REPO} from this environment "
            f"({reason}). This requires a GitHub credential with read access "
            "to that repo; override ENVELOPE_CHECK_LIVE_TEST_OWNER / "
            "_REPO / _DISCUSSION / _ABSENT_COMMENT_ID / _PRESENT_COMMENT_ID "
            "to point this test at an equivalent artifact on a repo you can "
            "read, or run it on a host with access. Loud on purpose: a "
            "silent skip here is the failure mode this whole Discussion is "
            "about."
        )


def _run_cli(args: list[str], env: dict | None = None) -> dict:
    """Invoke the real CLI (`python3 -m backend.envelope_check ...`) as a
    subprocess and parse its one line of JSON stdout."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "backend.envelope_check", *args],
        cwd=str(REPO_ROOT),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"CLI exited {result.returncode}: stderr={result.stderr!r}"
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Criteria 8-10: check_impossible_sources tri-state
# ---------------------------------------------------------------------------


def test_impossible_sources_without_tool_calls_direct():
    """Criterion 8, direct function call."""
    result = envelope_check.check_impossible_sources(0, 5)
    assert result["finding"] == "impossible_sources_without_tool_calls"


def test_impossible_sources_without_tool_calls_cli():
    """Criterion 8, exact CLI command from the Spec's verification section."""
    out = _run_cli(["--tool-uses", "0", "--sources-count", "5", "--json"])
    assert out["finding"] == "impossible_sources_without_tool_calls"


def test_tool_uses_unknown_is_not_zero_direct():
    """Criterion 9, direct function call. Unknown must never read as a
    finding — that would slander every run that closes without the hook
    firing (~11.5% of closed runs, per the Spec's own measurement)."""
    result = envelope_check.check_impossible_sources(None, 5)
    assert result["finding"] is None
    assert result["reason"] == "tool_uses_unknown"


def test_tool_uses_unknown_is_not_zero_cli():
    """Criterion 9, exact CLI command from the Spec's verification section."""
    out = _run_cli(["--tool-uses", "", "--sources-count", "5", "--json"])
    assert out["finding"] is None
    assert out["reason"] == "tool_uses_unknown"
    assert "impossible_sources_without_tool_calls" not in json.dumps(out)


def test_legitimate_run_no_finding_direct():
    """Criterion 10, direct function call."""
    result = envelope_check.check_impossible_sources(15, 5)
    assert result["finding"] is None


def test_legitimate_run_no_finding_cli():
    """Criterion 10, exact CLI command."""
    out = _run_cli(["--tool-uses", "15", "--sources-count", "5", "--json"])
    assert out["finding"] is None


def test_empty_sources_with_zero_tool_calls_is_not_a_finding():
    """The correct refusal path (verdict: skip, sources: []) must never
    itself look like a finding — only a NON-empty sources array with zero
    tool calls is structurally impossible."""
    result = envelope_check.check_impossible_sources(0, 0)
    assert result["finding"] is None


# ---------------------------------------------------------------------------
# Criteria 11-12: claimed-artifact resolution against the LIVE API
# ---------------------------------------------------------------------------


def test_claimed_artifact_unresolved_against_real_api():
    """Criterion 11 — not a synthetic fixture: this is the actual comment id
    the original 2026-07-29 fabrication claimed, checked against the real,
    closed D#1790 today."""
    _skip_unless_live_discussion_plane_reachable()
    result = envelope_check.resolve_claimed_artifact(_D1790_URL_UNRESOLVED)
    assert result["finding"] == "claimed_artifact_unresolved"


def test_claimed_artifact_resolved_against_real_api():
    """Criterion 12 — a real, currently-live comment id resolves cleanly."""
    _skip_unless_live_discussion_plane_reachable()
    result = envelope_check.resolve_claimed_artifact(_D1790_URL_RESOLVED)
    assert result["finding"] is None


def test_claimed_artifact_cli_matches_criterion_11():
    _skip_unless_live_discussion_plane_reachable()
    out = _run_cli(["--claimed-artifact", _D1790_URL_UNRESOLVED, "--json"])
    assert out["finding"] == "claimed_artifact_unresolved"


# ---------------------------------------------------------------------------
# Criterion 13: fail-closed on an unreachable API, and it records
# ---------------------------------------------------------------------------


def test_claimed_artifact_unreachable_is_unverified(monkeypatch):
    """192.0.2.1 is TEST-NET-1 (RFC 5737) — guaranteed unroutable, so this
    never depends on a flaky real timeout."""
    monkeypatch.setenv("ENVELOPE_CHECK_GITHUB_API_BASE", "http://192.0.2.1")
    monkeypatch.setenv("ENVELOPE_CHECK_HTTP_TIMEOUT", "2")
    result = envelope_check.resolve_claimed_artifact(_D1790_URL_UNRESOLVED)
    # Must never fall through to a clean pass.
    assert result["finding"] == "unverified"


def test_unverified_finding_writes_audit_row_and_team_log_line(tmp_path, monkeypatch):
    """Criterion 13's "and writes a team-log line", plus the audit half of
    criterion 14 for the unverified case specifically."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ENVELOPE_CHECK_GITHUB_API_BASE", "http://192.0.2.1")
    monkeypatch.setenv("ENVELOPE_CHECK_HTTP_TIMEOUT", "2")
    stub, marker = _write_stub_team_log(tmp_path)
    monkeypatch.setenv("ENVELOPE_CHECK_TEAM_LOG_SCRIPT", str(stub))

    result = envelope_check.evaluate(None, 0, claimed_artifact=_D1790_URL_UNRESOLVED)
    assert result["finding"] == "unverified"

    envelope_check.record_finding(result, role="researcher", discussion="1791")

    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in audit_lines if line.strip()]
    assert any(r["kind"] == "envelope_fabrication_finding" and r["finding"] == "unverified" for r in rows)
    assert marker.exists(), "team-log stub was never invoked"


# ---------------------------------------------------------------------------
# Criterion 15: bound to envelope shape, not to a role
# ---------------------------------------------------------------------------


def test_finding_independent_of_role():
    """A code-reviewer envelope with tool_uses=0 and a non-empty sources
    array produces the identical finding a researcher envelope would —
    check_impossible_sources never takes a role parameter at all."""
    for role in ("researcher", "code-reviewer", "security-reviewer"):
        out = _run_cli(["--tool-uses", "0", "--sources-count", "3", "--json", "--role", role])
        assert out["finding"] == "impossible_sources_without_tool_calls"


# ---------------------------------------------------------------------------
# Criterion 17: no network call when no artifact is claimed
# ---------------------------------------------------------------------------


def test_no_network_call_when_no_artifact_claimed(monkeypatch):
    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("_graphql_call must not be reached when no artifact is claimed")

    monkeypatch.setattr(envelope_check, "_graphql_call", _must_not_be_called)
    result = envelope_check.evaluate(0, 5, claimed_artifact=None)
    assert result["finding"] == "impossible_sources_without_tool_calls"


# ---------------------------------------------------------------------------
# Security: the Authorization header is scoped to the real default API base,
# and a redirect is refused rather than followed (fix for the finding that
# a live GitHub token would otherwise ride an env-controlled host's 3xx off
# to wherever it pointed).
# ---------------------------------------------------------------------------


def test_auth_header_omitted_for_overridden_api_base(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "should-never-be-sent")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"data": null}'

    def _fake_open(req, timeout=None):  # noqa: ARG001
        captured["req"] = req
        return _FakeResp()

    monkeypatch.setattr(envelope_check._NO_REDIRECT_OPENER, "open", _fake_open)  # noqa: SLF001
    envelope_check._graphql_call("query{}", {}, "https://not-the-real-api.invalid", 2)  # noqa: SLF001
    assert captured["req"].get_header("Authorization") is None


def test_auth_header_present_for_real_default_api_base(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "a-fake-token-value")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"data": null}'

    def _fake_open(req, timeout=None):  # noqa: ARG001
        captured["req"] = req
        return _FakeResp()

    monkeypatch.setattr(envelope_check._NO_REDIRECT_OPENER, "open", _fake_open)  # noqa: SLF001
    envelope_check._graphql_call("query{}", {}, envelope_check.DEFAULT_API_BASE, 2)  # noqa: SLF001
    assert captured["req"].get_header("Authorization") == "Bearer a-fake-token-value"


def test_redirect_is_refused_not_followed():
    """Verified against this host's real interpreter, not against docs:
    a real local HTTP server responds with a 302, and the no-redirect
    opener must raise rather than follow the Location header."""
    import http.server
    import threading

    import urllib.error
    import urllib.request as _ur

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.1/graphql")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = _ur.Request(f"http://127.0.0.1:{port}/graphql", data=b"{}", method="POST")
        raised = False
        try:
            envelope_check._NO_REDIRECT_OPENER.open(req, timeout=5)  # noqa: SLF001
        except urllib.error.HTTPError as exc:
            raised = True
            assert exc.code == 302
        assert raised, "the no-redirect opener followed a 302 instead of refusing it"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_resolve_claimed_artifact_does_not_follow_redirect_end_to_end():
    """Same real-server method, through the actual public function: point
    ENVELOPE_CHECK_GITHUB_API_BASE (via the api_base param) at a server that
    redirects, and confirm resolve_claimed_artifact reports unverified
    rather than silently resolving whatever the redirect target says — and
    that the redirect target is never actually hit."""
    import http.server
    import threading

    hits = {"count": 0}

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits["count"] += 1
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.1/graphql")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = envelope_check.resolve_claimed_artifact(
            _D1790_URL_UNRESOLVED, api_base=f"http://127.0.0.1:{port}", timeout=5
        )
        assert result["finding"] == "unverified"
        assert hits["count"] == 1, "expected exactly one request to the redirecting server, no follow-through"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The hook only reads a claimed artifact from the PARSED envelope, never
# from surrounding prose (fix for: a URL merely mentioned in text used to
# trigger a network call, an audit row, and a team-log line).
# ---------------------------------------------------------------------------


def test_extract_functions_only_see_the_parsed_dict():
    env = {"agent": "researcher", "sources": [1, 2, 3]}
    assert envelope_check.extract_sources_count(env) == 3
    assert envelope_check.extract_claimed_artifact(env) is None

    env_with_artifact = {"agent": "researcher", "posted_url": _D1790_URL_UNRESOLVED}
    assert envelope_check.extract_claimed_artifact(env_with_artifact) == _D1790_URL_UNRESOLVED


def test_extract_claimed_artifact_finds_it_nested():
    """False-negative direction: extract_claimed_artifact scans the whole
    serialized envelope, so a permalink buried in a nested field — inside
    issues[], inside a sources[] entry's own url, or several levels down —
    must still be found. The tightened regex (fix for the false-positive
    over-broad-prose-scan direction) must not have also narrowed this."""
    top_level = {"agent": "researcher", "artifact_url": _D1790_URL_UNRESOLVED}
    assert envelope_check.extract_claimed_artifact(top_level) == _D1790_URL_UNRESOLVED

    nested_in_issues = {
        "agent": "code-reviewer",
        "issues": [
            {"file": "foo.py", "note": "see prior discussion"},
            {"file": "bar.py", "note": f"already raised at {_D1790_URL_UNRESOLVED}"},
        ],
    }
    assert envelope_check.extract_claimed_artifact(nested_in_issues) == _D1790_URL_UNRESOLVED

    nested_in_sources = {
        "agent": "researcher",
        "sources": [
            {"url": "https://example.invalid/doc", "claim": "unrelated"},
            {"url": _D1790_URL_UNRESOLVED, "claim": "posted this"},
        ],
    }
    assert envelope_check.extract_claimed_artifact(nested_in_sources) == _D1790_URL_UNRESOLVED


def test_hook_ignores_permalink_mentioned_only_in_prose(tmp_path):
    """A permalink appearing in the surrounding prose, outside the parsed
    AGENT_OUTPUT JSON block, must never be treated as a claimed artifact —
    checked against all three sinks, including the network one. A test that
    only checks the audit-row and team-log sinks would still pass if a
    prose-only mention triggered a network call that happened to resolve
    cleanly; this counts requests against a real local server instead of
    trusting that the other two sinks being empty implies no call was made."""
    import http.server
    import threading

    hits = {"count": 0}

    class _CountingHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits["count"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data": null}')

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _CountingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    state_dir = tmp_path / "state"
    stub, marker = _write_stub_team_log(tmp_path)

    envelope_text = (
        f"I noticed {_D1790_URL_UNRESOLVED} in passing while researching.\n\n"
        "<!-- AGENT_OUTPUT -->\n```json\n"
        + json.dumps({"agent": "researcher", "verdict": "pass", "sources": []})
        + "\n```\n<!-- /AGENT_OUTPUT -->\n"
    )

    hook_script = REPO_ROOT / "scripts" / "hooks" / "post-agent.d" / "envelope-check.sh"
    env = dict(os.environ)
    env.update(
        {
            "TOOL_USES": "0",
            "CONTENT": envelope_text,
            "ROLE": "researcher",
            "DISCUSSION": "1791",
            "PR": "",
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            "ENVELOPE_CHECK_TEAM_LOG_SCRIPT": str(stub),
            "ENVELOPE_CHECK_GITHUB_API_BASE": f"http://127.0.0.1:{port}",
        }
    )
    try:
        result = subprocess.run(["bash", str(hook_script)], env=env, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    assert hits["count"] == 0, "a prose-only mention must never reach the network, even if it would have resolved cleanly"
    assert not marker.exists(), "a prose-only mention must never trigger a team-log write"
    assert not (state_dir / "audit.jsonl").exists(), "a prose-only mention must never trigger an audit row"


# ---------------------------------------------------------------------------
# Criteria 14 & 16: the real hook script — records, exits 0, and never
# touches agent_run.verdict
# ---------------------------------------------------------------------------


def test_hook_script_records_finding_and_exits_zero(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    stub, marker = _write_stub_team_log(tmp_path)

    envelope_text = (
        "Findings below.\n\n"
        "<!-- AGENT_OUTPUT -->\n"
        "```json\n"
        + json.dumps(
            {
                "agent": "researcher",
                "verdict": "pass",
                "sources": [{"url": f"https://example.invalid/{i}"} for i in range(5)],
            }
        )
        + "\n```\n"
        "<!-- /AGENT_OUTPUT -->\n"
    )

    hook_script = REPO_ROOT / "scripts" / "hooks" / "post-agent.d" / "envelope-check.sh"
    env = dict(os.environ)
    env.update(
        {
            "TOOL_USES": "0",
            "CONTENT": envelope_text,
            "ROLE": "researcher",
            "DISCUSSION": "1791",
            "PR": "",
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            "ENVELOPE_CHECK_TEAM_LOG_SCRIPT": str(stub),
        }
    )

    result = subprocess.run(
        ["bash", str(hook_script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    audit_lines = (state_dir / "audit.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in audit_lines if line.strip()]
    assert any(
        r["kind"] == "envelope_fabrication_finding"
        and r["finding"] == "impossible_sources_without_tool_calls"
        for r in rows
    )
    assert marker.exists(), "team-log stub was never invoked"


def test_hook_script_never_touches_agent_run_verdict(tmp_path, monkeypatch):
    """Criterion 16: this is a detector, not a gate. Seed a real agent_run
    row via the real agent_run_tracker, run the hook against a finding-
    producing envelope, and confirm the row's verdict is byte-identical
    before and after — the hook and the module it calls contain no
    agent_run-mutating code path at all."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

    sys.path.insert(0, str(REPO_ROOT))
    from backend import agent_run_tracker  # noqa: PLC0415

    agent_id = "researcher-1791-envelopecheck-test"
    agent_run_tracker.start_run(agent_id=agent_id, role="researcher", discussion=1791)
    agent_run_tracker.complete_run(agent_id=agent_id, verdict="pass", tool_uses=0)

    before = agent_run_tracker.population()

    stub, _marker = _write_stub_team_log(tmp_path)
    hook_script = REPO_ROOT / "scripts" / "hooks" / "post-agent.d" / "envelope-check.sh"
    env = dict(os.environ)
    env.update(
        {
            "TOOL_USES": "0",
            "CONTENT": (
                "<!-- AGENT_OUTPUT -->\n```json\n"
                + json.dumps({"agent": "researcher", "sources": [{"url": "https://example.invalid"}]})
                + "\n```\n<!-- /AGENT_OUTPUT -->\n"
            ),
            "ROLE": "researcher",
            "DISCUSSION": "1791",
            "PR": "",
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            "ENVELOPE_CHECK_TEAM_LOG_SCRIPT": str(stub),
        }
    )
    result = subprocess.run(["bash", str(hook_script)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0

    after = agent_run_tracker.population()
    assert before == after, "agent_run population changed — the detector must never mutate agent_run"


# ---------------------------------------------------------------------------
# D#1791 PR 3: the default SubagentStop path now threads precomputed
# SOURCES_COUNT / CLAIMED_ARTIFACT ambient vars (subagent_payload.py's own
# extract_sources_count / extract_claimed_artifact, run on the parsed
# envelope) instead of leaving this hook to re-derive them from CONTENT,
# which the default path never populated. These pin that the hook prefers
# the precomputed signals when present, and that the CONTENT-parsing
# fallback PR 2 shipped is unchanged for a caller that doesn't set them.
# ---------------------------------------------------------------------------


def test_hook_prefers_precomputed_sources_count_over_content(tmp_path):
    """SOURCES_COUNT set in the environment (the default path once PR 3 is
    wired in) must be trusted directly rather than re-derived from CONTENT
    — set CONTENT to something that would resolve to a DIFFERENT count if
    re-parsed, and confirm the finding matches SOURCES_COUNT instead."""
    state_dir = tmp_path / "state"
    stub, marker = _write_stub_team_log(tmp_path)

    hook_script = REPO_ROOT / "scripts" / "hooks" / "post-agent.d" / "envelope-check.sh"
    env = dict(os.environ)
    env.update(
        {
            "TOOL_USES": "0",
            "SOURCES_COUNT": "5",
            "CLAIMED_ARTIFACT": "",
            # CONTENT carries a DIFFERENT (empty) sources array — if this
            # were re-parsed instead of trusting SOURCES_COUNT, no finding
            # would be recorded.
            "CONTENT": (
                "<!-- AGENT_OUTPUT -->\n```json\n"
                + json.dumps({"agent": "researcher", "verdict": "pass", "sources": []})
                + "\n```\n<!-- /AGENT_OUTPUT -->\n"
            ),
            "ROLE": "researcher",
            "DISCUSSION": "1791",
            "PR": "",
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            "ENVELOPE_CHECK_TEAM_LOG_SCRIPT": str(stub),
        }
    )
    result = subprocess.run(["bash", str(hook_script)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    audit_lines = (state_dir / "audit.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in audit_lines if line.strip()]
    assert any(
        r["kind"] == "envelope_fabrication_finding" and r["finding"] == "impossible_sources_without_tool_calls"
        for r in rows
    ), "SOURCES_COUNT=5 (ambient) must produce a finding even though CONTENT alone would not"
    assert marker.exists()


def test_hook_falls_back_to_content_when_no_precomputed_signals(tmp_path):
    """When SOURCES_COUNT / CLAIMED_ARTIFACT are absent from the
    environment entirely (not merely empty), the hook must fall back to
    parsing CONTENT exactly as PR 2 shipped — this is what keeps every
    existing PR 2 test (which never sets these two vars) passing unchanged."""
    state_dir = tmp_path / "state"
    stub, marker = _write_stub_team_log(tmp_path)

    hook_script = REPO_ROOT / "scripts" / "hooks" / "post-agent.d" / "envelope-check.sh"
    env = dict(os.environ)
    env.pop("SOURCES_COUNT", None)
    env.pop("CLAIMED_ARTIFACT", None)
    env.update(
        {
            "TOOL_USES": "0",
            "CONTENT": (
                "<!-- AGENT_OUTPUT -->\n```json\n"
                + json.dumps({"agent": "researcher", "verdict": "pass", "sources": [{"url": "https://x.invalid"}]})
                + "\n```\n<!-- /AGENT_OUTPUT -->\n"
            ),
            "ROLE": "researcher",
            "DISCUSSION": "1791",
            "PR": "",
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            "ENVELOPE_CHECK_TEAM_LOG_SCRIPT": str(stub),
        }
    )
    result = subprocess.run(["bash", str(hook_script)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    audit_lines = (state_dir / "audit.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in audit_lines if line.strip()]
    assert any(
        r["kind"] == "envelope_fabrication_finding" and r["finding"] == "impossible_sources_without_tool_calls"
        for r in rows
    ), "with no SOURCES_COUNT set at all, the hook must still derive the finding from CONTENT"
    assert marker.exists()


def test_hook_precomputed_claimed_artifact_is_used_over_content(monkeypatch, tmp_path):
    """CLAIMED_ARTIFACT set in the environment must reach
    resolve_claimed_artifact even when CONTENT carries no artifact at
    all — proving the artifact-check half of the precomputed-signals path,
    not just the sources-count half. Uses ENVELOPE_CHECK_GITHUB_API_BASE
    pointed at an unroutable host (the same seam
    test_claimed_artifact_unreachable_is_unverified uses) so this never
    depends on live network access; the finding must be "unverified", not
    silence, which is only possible if resolve_claimed_artifact actually
    ran."""
    state_dir = tmp_path / "state"
    stub, marker = _write_stub_team_log(tmp_path)

    hook_script = REPO_ROOT / "scripts" / "hooks" / "post-agent.d" / "envelope-check.sh"
    env = dict(os.environ)
    env.update(
        {
            "TOOL_USES": "5",
            "SOURCES_COUNT": "0",
            "CLAIMED_ARTIFACT": _D1790_URL_UNRESOLVED,
            "CONTENT": (
                "<!-- AGENT_OUTPUT -->\n```json\n"
                + json.dumps({"agent": "researcher", "verdict": "pass", "sources": []})
                + "\n```\n<!-- /AGENT_OUTPUT -->\n"
            ),
            "ROLE": "researcher",
            "DISCUSSION": "1791",
            "PR": "",
            "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            "ENVELOPE_CHECK_TEAM_LOG_SCRIPT": str(stub),
            "ENVELOPE_CHECK_GITHUB_API_BASE": "http://192.0.2.1",
            "ENVELOPE_CHECK_HTTP_TIMEOUT": "2",
        }
    )
    result = subprocess.run(["bash", str(hook_script)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    audit_lines = (state_dir / "audit.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in audit_lines if line.strip()]
    assert any(
        r["kind"] == "envelope_fabrication_finding" and r["finding"] == "unverified" for r in rows
    ), "CLAIMED_ARTIFACT (ambient) must reach resolve_claimed_artifact, not be ignored in favor of CONTENT"
    assert marker.exists()


# ---------------------------------------------------------------------------
# Non-synthetic replay: the real 2026-09-12 captured fabrications
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", _FABRICATION_FILES)
def test_replay_real_captured_fabrication(filename):
    path = _FABRICATION_DIR / filename
    if not path.is_file():
        pytest.skip(
            f"real captured fabrication evidence not present on this host: {path} "
            "(host-local, sensitive state — not committed to the repo; see the "
            "module docstring above)"
        )

    subagent_payload = _load_subagent_payload()
    tool_uses = subagent_payload.count_tool_uses(str(path))
    assert tool_uses == 0, "both 2026-09-12 instances made zero real tool calls"

    sources_count = _extract_last_sources_count(str(path))
    assert sources_count > 0, "both instances fabricated a non-empty sources array"

    result = envelope_check.evaluate(tool_uses, sources_count)
    assert result["finding"] == "impossible_sources_without_tool_calls"
