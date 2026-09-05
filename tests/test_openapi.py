"""
Tests for OpenAPI spec generation and /openapi.json + /docs endpoints.

Covers:
  1. generate_spec() produces a structurally valid OpenAPI 3.0.1 document
  2. All ROUTES entries appear as paths in the spec
  3. Every path item has at least one response schema
  4. POST endpoints include a requestBody in the spec
  5. Auth-protected endpoints have security: [{bearerAuth: []}]
  6. GET /openapi.json returns HTTP 200 with valid JSON
  7. GET /docs returns HTTP 200 with HTML containing Swagger UI
  8. --no-docs disables both /openapi.json and /docs (404)
  9. bearerAuth security scheme is present in components
 10. Spec has required top-level fields (openapi, info, paths, components)
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.api_routes import ROUTES, VERSIONED_ROUTES  # noqa: E402
from backend.openapi import generate_spec, get_docs_html  # noqa: E402


# ---------------------------------------------------------------------------
# Unit tests — generate_spec()
# ---------------------------------------------------------------------------


def test_spec_top_level_fields():
    """Spec must have all required OpenAPI top-level fields."""
    spec = generate_spec()
    assert spec["openapi"] == "3.0.1"
    assert "info" in spec
    assert "title" in spec["info"]
    assert "version" in spec["info"]
    assert "paths" in spec
    assert "components" in spec


def test_spec_bearer_auth_scheme():
    """components/securitySchemes must define bearerAuth."""
    spec = generate_spec()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert "bearerAuth" in schemes
    assert schemes["bearerAuth"]["type"] == "http"
    assert schemes["bearerAuth"]["scheme"] == "bearer"


def test_spec_all_routes_present():
    """Every route in VERSIONED_ROUTES must appear as a path in the spec."""
    spec = generate_spec()
    paths = spec["paths"]
    for route in VERSIONED_ROUTES:
        openapi_path = re.sub(r"<([^>]+)>", r"{\1}", route["path"])
        assert openapi_path in paths, f"Missing path: {openapi_path}"


def test_spec_endpoint_count():
    """Spec path count must match unique paths in VERSIONED_ROUTES."""
    spec = generate_spec()
    unique_paths = {re.sub(r"<([^>]+)>", r"{\1}", r["path"]) for r in VERSIONED_ROUTES}
    assert len(spec["paths"]) == len(unique_paths)


def test_spec_versioned_paths_present():
    """The spec must include /v1/ prefixed paths for all non-infrastructure routes."""
    spec = generate_spec()
    paths = spec["paths"]
    v1_paths = [p for p in paths if p.startswith("/v1/")]
    assert len(v1_paths) > 0, "No /v1/ paths found in spec"
    # Unversioned paths should be marked deprecated
    for path, path_item in paths.items():
        if path.startswith("/v1/") or path in ("/openapi.json", "/docs", "/dashboard"):
            continue
        for method_op in path_item.values():
            assert method_op.get("deprecated") is True, (
                f"Unversioned path {path} should be marked deprecated"
            )


def test_spec_every_path_has_response_schema():
    """Every operation must have at least one response defined."""
    spec = generate_spec()
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            responses = operation.get("responses", {})
            assert responses, f"No responses for {method.upper()} {path}"


def test_spec_post_endpoints_have_request_body():
    """POST routes with request_body in ROUTES must have requestBody in the spec."""
    spec = generate_spec()
    for route in ROUTES:
        if route["method"] == "POST" and route.get("request_body"):
            openapi_path = re.sub(r"<([^>]+)>", r"{\1}", route["path"])
            operation = spec["paths"][openapi_path]["post"]
            assert "requestBody" in operation, (
                f"Missing requestBody for POST {openapi_path}"
            )


def test_spec_auth_endpoints_have_security():
    """Auth-required routes must have security: [{bearerAuth: []}] in the spec."""
    spec = generate_spec()
    for route in ROUTES:
        if route.get("auth"):
            openapi_path = re.sub(r"<([^>]+)>", r"{\1}", route["path"])
            method = route["method"].lower()
            operation = spec["paths"][openapi_path][method]
            security = operation.get("security", [])
            assert {"bearerAuth": []} in security, (
                f"{method.upper()} {openapi_path} is auth=True but has no bearerAuth security"
            )


def test_spec_non_auth_endpoints_no_security():
    """/health and /openapi.json are auth=False — they must NOT carry bearerAuth."""
    spec = generate_spec()
    exempt_paths = {"/health", "/openapi.json", "/docs", "/metrics"}
    for path, path_item in spec["paths"].items():
        if path in exempt_paths:
            for method, operation in path_item.items():
                security = operation.get("security", [])
                assert {"bearerAuth": []} not in security, (
                    f"{method.upper()} {path} should not require auth but does"
                )


def test_docs_html_contains_swagger_ui():
    """get_docs_html() must return HTML that references Swagger UI."""
    html = get_docs_html()
    assert "swagger-ui" in html.lower()
    assert "/openapi.json" in html
    assert "SwaggerUIBundle" in html


# ---------------------------------------------------------------------------
# Integration tests — live server
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def _get(base_url: str, path: str) -> tuple[int, bytes]:
    url = base_url + path
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _start_server(port: int, extra_args: list[str] | None = None) -> subprocess.Popen:
    cmd = [sys.executable, "backend/api.py", "--port", str(port), "--no-enable-sse"]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.Popen(
        cmd,
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_ready(base_url: str, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    proc.kill()
    proc.wait()
    return False


@pytest.fixture(scope="module")
def api_server_with_docs():
    port = _free_port()
    proc = _start_server(port)
    base_url = f"http://127.0.0.1:{port}"
    if not _wait_ready(base_url, proc):
        pytest.fail("API server (with docs) did not become ready")
    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture(scope="module")
def api_server_no_docs():
    port = _free_port()
    proc = _start_server(port, ["--no-docs"])
    base_url = f"http://127.0.0.1:{port}"
    if not _wait_ready(base_url, proc):
        pytest.fail("API server (no-docs) did not become ready")
    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_openapi_json_endpoint(api_server_with_docs):
    """GET /openapi.json returns 200 with a valid OpenAPI spec."""
    status, body = _get(api_server_with_docs, "/openapi.json")
    assert status == 200
    spec = json.loads(body)
    assert spec["openapi"] == "3.0.1"
    assert "paths" in spec
    assert len(spec["paths"]) > 0


def test_docs_endpoint_returns_html(api_server_with_docs):
    """GET /docs returns 200 with HTML containing Swagger UI."""
    status, body = _get(api_server_with_docs, "/docs")
    assert status == 200
    html = body.decode("utf-8", errors="replace")
    assert "swagger" in html.lower()
    assert "/openapi.json" in html


def test_no_docs_flag_disables_openapi_json(api_server_no_docs):
    """--no-docs makes GET /openapi.json return 404."""
    status, _body = _get(api_server_no_docs, "/openapi.json")
    assert status == 404


def test_no_docs_flag_disables_docs(api_server_no_docs):
    """--no-docs makes GET /docs return 404."""
    status, _body = _get(api_server_no_docs, "/docs")
    assert status == 404
