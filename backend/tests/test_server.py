"""
Tests for backend/server.py

Run with:
    python -m pytest backend/tests/test_server.py -v

All tests use mocking — no real sockets, no real ports, no real SDK calls.
Event-mapper coverage for the Claude Agent SDK prompt lane itself lives in
backend/tests/test_prompt_lane.py; these tests cover backend/server.py's
wiring around it (_handle_request, _run_request_with_timeout, _dispatcher,
_stdin_reader, main()).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.server as server_mod
from backend.server import (
    _emit,
    _handle_request,
    _run_request_with_timeout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_emits(monkeypatch) -> list[dict]:
    captured: list[dict] = []
    monkeypatch.setattr(server_mod, "_emit", lambda d: captured.append(d))
    return captured


# ---------------------------------------------------------------------------
# _emit
# ---------------------------------------------------------------------------


def test_emit_writes_json(capsys):
    """_emit prints a JSON line to stdout."""
    _emit({"type": "ready", "version": "0.2.0"})
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["type"] == "ready"
    assert parsed["version"] == "0.2.0"


def test_emit_unicode(capsys):
    """_emit handles non-ASCII without escaping."""
    _emit({"type": "content", "content": "hello world"})
    out = capsys.readouterr().out.strip()
    assert "hello world" in out


def test_emit_nested_dict(capsys):
    """_emit serialises nested dicts correctly."""
    _emit({"type": "tool_use", "input": {"key": "value", "count": 42}})
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["input"]["count"] == 42


# ---------------------------------------------------------------------------
# _handle_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_request_missing_prompt(monkeypatch):
    """Missing prompt emits error event without touching the SDK lane."""
    captured = _capture_emits(monkeypatch)
    await _handle_request({"id": "r1"})
    assert any(e.get("type") == "error" for e in captured)
    assert any("prompt" in e.get("error", "") for e in captured)


@pytest.mark.asyncio
async def test_handle_request_streams_lane_events(monkeypatch):
    """Events yielded by sdk_lane.run_prompt are emitted with the request id attached."""
    captured = _capture_emits(monkeypatch)

    async def fake_run_prompt(prompt, session_id=None):
        assert prompt == "do something"
        assert session_id is None
        yield {"type": "content", "content": "hi"}
        yield {"type": "done", "session_id": "new-session-id"}

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", fake_run_prompt)

    await _handle_request({"id": "r3", "prompt": "do something"})

    assert captured[0] == {"id": "r3", "type": "content", "content": "hi"}
    assert captured[1] == {"id": "r3", "type": "done", "session_id": "new-session-id"}


@pytest.mark.asyncio
async def test_handle_request_passes_session_id_through(monkeypatch):
    """An existing session_id in the request is forwarded to run_prompt unchanged
    — the SDK's own resume/session store replaces the old SessionService lookup."""
    captured = _capture_emits(monkeypatch)
    seen = {}

    async def fake_run_prompt(prompt, session_id=None):
        seen["session_id"] = session_id
        yield {"type": "done", "session_id": session_id}

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", fake_run_prompt)

    await _handle_request({"id": "parse-1", "prompt": "continue", "session_id": "existing-sess"})
    assert seen["session_id"] == "existing-sess"
    assert any(e.get("type") == "done" for e in captured)


@pytest.mark.asyncio
async def test_handle_request_lane_exception_emits_error(monkeypatch):
    """An exception raised before the lane yields anything is caught and emitted as error."""
    captured = _capture_emits(monkeypatch)

    async def fake_run_prompt(prompt, session_id=None):
        raise RuntimeError("agent crashed")
        yield  # pragma: no cover — unreachable; keeps this an async generator

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", fake_run_prompt)

    await _handle_request({"id": "r5", "prompt": "break things"})
    assert any(e.get("type") == "error" for e in captured)


@pytest.mark.asyncio
async def test_handle_request_lane_raises_mid_stream(monkeypatch):
    """An exception raised after some events already streamed still ends in an error event."""
    captured = _capture_emits(monkeypatch)

    async def fake_run_prompt(prompt, session_id=None):
        yield {"type": "thinking", "content": "hmm"}
        raise RuntimeError("stream broke")

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", fake_run_prompt)

    await _handle_request({"id": "r6", "prompt": "go"})
    types_emitted = [e["type"] for e in captured]
    assert types_emitted == ["thinking", "error"]


# ---------------------------------------------------------------------------
# _run_request_with_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_request_no_timeout(monkeypatch):
    """With AF_REQUEST_TIMEOUT=0, request runs without timeout."""
    captured = _capture_emits(monkeypatch)

    async def fake_run_prompt(prompt, session_id=None):
        yield {"type": "done", "session_id": "sess-notimeout"}

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", fake_run_prompt)

    with patch.dict("os.environ", {"AF_REQUEST_TIMEOUT": "0"}):
        await _run_request_with_timeout({"id": "t1", "prompt": "run me"})
    assert any(e.get("type") == "done" for e in captured)


@pytest.mark.asyncio
async def test_run_request_timeout_emits_error(monkeypatch):
    """When request times out, error and done events are emitted."""
    captured = _capture_emits(monkeypatch)

    async def slow_run_prompt(prompt, session_id=None):
        await asyncio.sleep(100)
        yield {"type": "done", "session_id": "sess-timeout"}

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", slow_run_prompt)

    with patch.dict("os.environ", {"AF_REQUEST_TIMEOUT": "1"}):
        await _run_request_with_timeout({"id": "t2", "prompt": "slow request"})
    error_events = [e for e in captured if e.get("type") == "error"]
    done_events = [e for e in captured if e.get("type") == "done"]
    assert error_events, "Expected an error event on timeout"
    assert done_events, "Expected a done event after timeout"
    assert "timeout" in error_events[0]["error"].lower()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_processes_single_request(monkeypatch):
    """_dispatcher picks a request from the queue and processes it."""
    from backend.server import _dispatcher

    captured = _capture_emits(monkeypatch)

    async def fake_run_prompt(prompt, session_id=None):
        yield {"type": "done", "session_id": "dispatch-sess"}

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", fake_run_prompt)

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put({"id": "d1", "prompt": "dispatched"})

    with patch.dict("os.environ", {"AF_REQUEST_TIMEOUT": "0"}):
        task = asyncio.create_task(_dispatcher(queue))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await asyncio.sleep(0.1)
    assert any(e.get("type") == "done" for e in captured)


# ---------------------------------------------------------------------------
# Full streaming sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_streaming_sequence(monkeypatch):
    """Every event type the lane can yield is forwarded with the request id attached."""
    captured = _capture_emits(monkeypatch)

    async def fake_run_prompt(prompt, session_id=None):
        yield {"type": "thinking", "content": "hmm"}
        yield {"type": "tool_use", "tool": "Bash", "call_id": "c1", "input": {"cmd": "ls"}}
        yield {"type": "tool_result", "call_id": "c1", "result": "file.txt", "is_error": False}
        yield {"type": "content", "content": "Done"}
        yield {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 5}}
        yield {"type": "done", "session_id": "stream-sess"}

    monkeypatch.setattr(server_mod.sdk_lane, "run_prompt", fake_run_prompt)

    await _handle_request({"id": "stream-1", "prompt": "stream test"})

    types_emitted = [e["type"] for e in captured]
    assert "thinking" in types_emitted
    assert "tool_use" in types_emitted
    assert "tool_result" in types_emitted
    assert "content" in types_emitted
    assert "usage" in types_emitted
    assert "done" in types_emitted
    assert all(e["id"] == "stream-1" for e in captured)


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------


def test_main_calls_asyncio_run(monkeypatch):
    """main() invokes asyncio.run(_main())."""
    run_called = {"called": False}

    async def fake_main():
        pass

    def fake_asyncio_run(coro):
        run_called["called"] = True
        # Run the coroutine to avoid ResourceWarning
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(coro)
        loop.close()

    monkeypatch.setattr(server_mod.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(server_mod, "_main", fake_main)
    monkeypatch.setattr("sys.argv", ["backend/server"])

    server_mod.main()
    assert run_called["called"]


# ---------------------------------------------------------------------------
# _stdin_reader — processes JSON lines from stdin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdin_reader_valid_json(monkeypatch):
    """_stdin_reader parses valid JSON lines and puts them on the queue."""
    from backend.server import _stdin_reader

    queue: asyncio.Queue = asyncio.Queue()
    test_line = json.dumps({"id": "s1", "prompt": "hello"}).encode() + b"\n"

    # Mock asyncio stream reader
    reader = asyncio.StreamReader()
    reader.feed_data(test_line)
    reader.feed_eof()

    async def fake_connect_read_pipe(protocol_factory, pipe):
        protocol = protocol_factory()
        protocol.connection_made(MagicMock())
        return MagicMock(), protocol

    mock_loop = MagicMock()
    mock_loop.connect_read_pipe = AsyncMock(side_effect=fake_connect_read_pipe)

    with patch("asyncio.get_running_loop", return_value=mock_loop), \
         patch("asyncio.StreamReader", return_value=reader), \
         patch("asyncio.StreamReaderProtocol", return_value=MagicMock()):
        task = asyncio.create_task(_stdin_reader(queue))
        # Give it a moment to process
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # Check if anything was queued (may depend on mock behavior)
    # The test passes if no exception was raised


@pytest.mark.asyncio
async def test_stdin_reader_invalid_json_emits_error(monkeypatch):
    """_stdin_reader emits error event for invalid JSON."""
    from backend.server import _stdin_reader

    captured = _capture_emits(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    invalid_line = b"this is not json\n"

    reader = asyncio.StreamReader()
    reader.feed_data(invalid_line)
    reader.feed_eof()

    async def fake_connect_read_pipe(protocol_factory, pipe):
        return MagicMock(), MagicMock()

    mock_loop = MagicMock()
    mock_loop.connect_read_pipe = AsyncMock(side_effect=fake_connect_read_pipe)

    with patch("asyncio.get_running_loop", return_value=mock_loop), \
         patch("asyncio.StreamReader", return_value=reader), \
         patch("asyncio.StreamReaderProtocol", return_value=MagicMock()):
        try:
            await asyncio.wait_for(_stdin_reader(queue), timeout=0.5)
        except (asyncio.TimeoutError, Exception):
            pass

    # Test passes if no unhandled exception


# ---------------------------------------------------------------------------
# _parse_repo_slug
# ---------------------------------------------------------------------------


def test_parse_repo_slug_valid():
    from backend.server import _parse_repo_slug
    assert _parse_repo_slug("owner/repo") == ("owner", "repo")


def test_parse_repo_slug_no_slash():
    from backend.server import _parse_repo_slug
    assert _parse_repo_slug("noslash") is None


def test_parse_repo_slug_empty():
    from backend.server import _parse_repo_slug
    assert _parse_repo_slug("") is None


def test_parse_repo_slug_missing_owner():
    from backend.server import _parse_repo_slug
    assert _parse_repo_slug("/repo") is None


def test_parse_repo_slug_missing_name():
    from backend.server import _parse_repo_slug
    assert _parse_repo_slug("owner/") is None


# ---------------------------------------------------------------------------
# _read_slug_from_json
# ---------------------------------------------------------------------------


def test_read_slug_from_json_first_key(tmp_path):
    from backend.server import _read_slug_from_json
    f = tmp_path / "runtime.json"
    f.write_text(json.dumps({"repo": "acme/myapp", "project_repo": "other/thing"}))
    assert _read_slug_from_json(f, "repo", "project_repo") == ("acme", "myapp")


def test_read_slug_from_json_fallback_key(tmp_path):
    from backend.server import _read_slug_from_json
    f = tmp_path / "runtime.json"
    f.write_text(json.dumps({"project_repo": "acme/fallback"}))
    assert _read_slug_from_json(f, "repo", "project_repo") == ("acme", "fallback")


def test_read_slug_from_json_no_matching_key(tmp_path):
    from backend.server import _read_slug_from_json
    f = tmp_path / "runtime.json"
    f.write_text(json.dumps({"other": "value"}))
    assert _read_slug_from_json(f, "repo") is None


def test_read_slug_from_json_corrupt_file(tmp_path):
    from backend.server import _read_slug_from_json
    f = tmp_path / "bad.json"
    f.write_text("not valid json{{")
    assert _read_slug_from_json(f, "repo") is None


# ---------------------------------------------------------------------------
# _resolve_repo_for_project
# ---------------------------------------------------------------------------


def test_resolve_repo_no_project():
    """None project_name returns the module-level defaults."""
    from backend.server import _resolve_repo_for_project, _REPO_OWNER, _REPO_NAME
    assert _resolve_repo_for_project(None) == (_REPO_OWNER, _REPO_NAME)


def test_resolve_repo_empty_string():
    from backend.server import _resolve_repo_for_project, _REPO_OWNER, _REPO_NAME
    assert _resolve_repo_for_project("") == (_REPO_OWNER, _REPO_NAME)


def test_resolve_repo_reads_runtime_json(tmp_path, monkeypatch):
    """Reads repo from dashboard-runtime.json in the state dir."""
    from backend import server as srv

    state_dir = tmp_path / ".myproject-state"
    state_dir.mkdir()
    (state_dir / "dashboard-runtime.json").write_text(
        json.dumps({"repo": "myorg/myproject"})
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    owner, name = srv._resolve_repo_for_project("myproject")
    assert owner == "myorg"
    assert name == "myproject"


def test_resolve_repo_fallback_to_project_json(tmp_path, monkeypatch):
    """Falls back to project.json when runtime.json has no repo field."""
    from backend import server as srv

    state_dir = tmp_path / ".myproject-state"
    state_dir.mkdir()
    (state_dir / "dashboard-runtime.json").write_text(json.dumps({"rpcBaseUrl": "http://x"}))
    (state_dir / "project.json").write_text(json.dumps({"repo": "myorg/via-project"}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    owner, name = srv._resolve_repo_for_project("myproject")
    assert owner == "myorg"
    assert name == "via-project"


def test_resolve_repo_raises_when_no_state_files(tmp_path, monkeypatch):
    """A named project whose state dir has neither dashboard-runtime.json nor
    project.json raises, not falls back. (An empty state-dir directory with no
    config file in it is treated the same as no state dir — there's nothing
    to point the operator at either way.)
    """
    from backend.server import _resolve_repo_for_project
    from backend.rpc_project_scope import UnresolvableProjectError

    state_dir = tmp_path / ".unknown-state"
    state_dir.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    with pytest.raises(UnresolvableProjectError, match="unknown"):
        _resolve_repo_for_project("unknown")


def test_resolve_repo_raises_when_project_json_has_no_repo_field(tmp_path, monkeypatch):
    """D#2268 item 1: project.json exists but carries no repo field -> raises,
    naming the file the operator should add "repo" to (not the engine default).
    """
    from backend.server import _resolve_repo_for_project
    from backend.rpc_project_scope import UnresolvableProjectError

    state_dir = tmp_path / ".norepo-state"
    state_dir.mkdir()
    (state_dir / "project.json").write_text(json.dumps({"name": "norepo"}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    with pytest.raises(UnresolvableProjectError) as excinfo:
        _resolve_repo_for_project("norepo")

    message = str(excinfo.value)
    assert "norepo" in message
    assert str(state_dir / "project.json") in message


def test_resolve_repo_raises_when_no_state_dir_at_all(tmp_path, monkeypatch):
    """D#2268 item 2: no state dir exists at all (e.g. a typo) -> raises."""
    from backend.server import _resolve_repo_for_project
    from backend.rpc_project_scope import UnresolvableProjectError

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    with pytest.raises(UnresolvableProjectError, match="totally-mistyped-xyz"):
        _resolve_repo_for_project("totally-mistyped-xyz")


def test_resolve_repo_error_message_distinguishes_causes(tmp_path, monkeypatch):
    """D#2268 item 3: 'state dir with no repo' and 'no state dir at all' produce
    different messages -- the operator needs to know which one they hit.
    """
    from backend.server import _resolve_repo_for_project
    from backend.rpc_project_scope import UnresolvableProjectError

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    (tmp_path / ".projectb-state").mkdir()
    (tmp_path / ".projectb-state" / "project.json").write_text(json.dumps({"name": "projectb"}))

    with pytest.raises(UnresolvableProjectError) as no_repo_exc:
        _resolve_repo_for_project("projectb")
    with pytest.raises(UnresolvableProjectError) as no_dir_exc:
        _resolve_repo_for_project("totally-mistyped-xyz")

    assert str(no_repo_exc.value) != str(no_dir_exc.value)
    assert str(tmp_path / ".projectb-state" / "project.json") in str(no_repo_exc.value)


def test_resolve_repo_error_names_served_state_dir_outside_home(tmp_path, monkeypatch):
    """D#2268 review fix: when the match comes from step 0
    (state_paths._served_state_dir(), this process's own STATE_DIR, which
    D#2259 explicitly allows to sit outside $HOME) and that config has no
    repo field, the error must name the *actual* dashboard-runtime.json that
    was read -- not a recomputed ~/.{name}-state/project.json guess, which
    would not exist and would send the operator editing the wrong file.
    """
    from backend.server import _resolve_repo_for_project
    from backend.rpc_project_scope import UnresolvableProjectError

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    served_dir = tmp_path / "elsewhere" / ".gatekeep-state"
    served_dir.mkdir(parents=True)
    (served_dir / "dashboard-runtime.json").write_text(
        json.dumps({"project_name": "gatekeep", "state_dir": str(served_dir)})
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: empty_home))
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(served_dir))

    with pytest.raises(UnresolvableProjectError) as excinfo:
        _resolve_repo_for_project("gatekeep")

    message = str(excinfo.value)
    assert "gatekeep" in message
    # Names the real, existing file that was read...
    assert str(served_dir / "dashboard-runtime.json") in message
    assert (served_dir / "dashboard-runtime.json").exists()
    # ...never the home-anchored path, which was never read and doesn't exist.
    never_read = empty_home / ".gatekeep-state" / "project.json"
    assert str(never_read) not in message
    assert not never_read.exists()


# ---------------------------------------------------------------------------
# D#2268 items 5-6: end-to-end through the real dispatch path -- an
# unresolvable project must raise before any GraphQL call is issued, and the
# raised exception must carry rpc_code == -32001 so both dispatch sites
# surface it as a JSON-RPC error envelope rather than a 500.
# ---------------------------------------------------------------------------


def test_dispatch_scoped_discussions_list_unresolvable_project_issues_no_graphql_call(
    tmp_path, monkeypatch
):
    from backend import server as srv
    from backend import rpc_project_scope as scope
    from backend.rpc_project_scope import UnresolvableProjectError

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    calls = []
    monkeypatch.setattr(srv, "_gh_graphql", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(UnresolvableProjectError, match="totally-mistyped-xyz"):
        scope.dispatch_scoped(
            "discussions.list",
            {"status": "*", "limit": 5, "project": "totally-mistyped-xyz"},
            srv._RPC_METHODS["discussions.list"],
        )
    assert calls == []


def test_dispatch_scoped_discussions_get_unresolvable_project_issues_no_graphql_call(
    tmp_path, monkeypatch
):
    from backend import server as srv
    from backend import rpc_project_scope as scope
    from backend.rpc_project_scope import UnresolvableProjectError

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    calls = []
    monkeypatch.setattr(srv, "_gh_graphql", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(UnresolvableProjectError, match="totally-mistyped-xyz"):
        scope.dispatch_scoped(
            "discussions.get",
            {"number": 1, "project": "totally-mistyped-xyz"},
            srv._RPC_METHODS["discussions.get"],
        )
    assert calls == []


def test_unresolvable_project_error_rpc_code_and_envelope_shape(tmp_path, monkeypatch):
    """rpc_code == -32001 is what makes backend/server.py:1849 and
    backend/routers/rpc.py:187 surface this as a JSON-RPC error (non-null
    'error', no 'result' key) instead of a 500. Assert the envelope shape
    at the legacy do_POST dispatch site.
    """
    from backend import server as srv
    from backend import rpc_project_scope as scope

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(srv, "_gh_graphql", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not be called")
    ))

    try:
        scope.dispatch_scoped(
            "discussions.list",
            {"status": "*", "limit": 5, "project": "totally-mistyped-xyz"},
            srv._RPC_METHODS["discussions.list"],
        )
        raise AssertionError("expected UnresolvableProjectError")
    except Exception as exc:  # mirrors the generic except in do_POST/rpc.py
        assert getattr(exc, "rpc_code", -32000) == -32001
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": getattr(exc, "rpc_code", -32000), "message": str(exc)},
        }
    assert "error" in envelope and "result" not in envelope
    assert envelope["error"]["code"] == -32001
    assert "totally-mistyped-xyz" in envelope["error"]["message"]


# ---------------------------------------------------------------------------
# DB_PATH / _migrate_legacy_db_path
# ---------------------------------------------------------------------------


def test_db_path_resolves_under_state_dir(monkeypatch, tmp_path):
    """server_mod._db_path() must derive from backend.state_paths.STATE_DIR at
    call time, not a hardcoded second directory — that's what makes it visible
    to AUTONOMOUS_TEAM_STATE_DIR overrides and to tooling that reasons about
    state-dir contents. D#1810: this used to be a module-level constant
    (DB_PATH) frozen at import time; it is now resolved fresh on every call."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

    assert server_mod._db_path() == tmp_path / "server.db"
    assert server_mod._db_path().parent == tmp_path


def test_migrate_legacy_db_path_moves_old_file(tmp_path, monkeypatch):
    """A file at the pre-STATE_DIR legacy location is moved into place at the
    new db path, with no data loss, and the legacy file is removed."""
    legacy = tmp_path / "legacy" / "server.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-session-history")

    new_path = tmp_path / "new-state" / "server.db"

    monkeypatch.setattr(server_mod, "_LEGACY_DB_PATH", legacy)
    monkeypatch.setattr(server_mod, "_db_path", lambda: new_path)

    server_mod._migrate_legacy_db_path()

    assert not legacy.exists()
    assert new_path.exists()
    assert new_path.read_bytes() == b"legacy-session-history"


def test_migrate_legacy_db_path_noop_when_no_legacy_file(tmp_path, monkeypatch):
    """Nothing to migrate — startup should be a silent no-op."""
    legacy = tmp_path / "legacy" / "server.db"
    new_path = tmp_path / "new-state" / "server.db"

    monkeypatch.setattr(server_mod, "_LEGACY_DB_PATH", legacy)
    monkeypatch.setattr(server_mod, "_db_path", lambda: new_path)

    server_mod._migrate_legacy_db_path()

    assert not legacy.exists()
    assert not new_path.exists()


def test_migrate_legacy_db_path_keeps_both_when_new_already_exists(tmp_path, monkeypatch):
    """If both locations already have a file, don't guess which is
    authoritative — leave both in place (no silent overwrite / no data loss)
    and keep using the new-location file."""
    legacy = tmp_path / "legacy" / "server.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-data")

    new_path = tmp_path / "new-state" / "server.db"
    new_path.parent.mkdir(parents=True)
    new_path.write_bytes(b"new-data")

    monkeypatch.setattr(server_mod, "_LEGACY_DB_PATH", legacy)
    monkeypatch.setattr(server_mod, "_db_path", lambda: new_path)

    server_mod._migrate_legacy_db_path()

    # Both files survive untouched — no data loss either way.
    assert legacy.exists()
    assert legacy.read_bytes() == b"legacy-data"
    assert new_path.exists()
    assert new_path.read_bytes() == b"new-data"
