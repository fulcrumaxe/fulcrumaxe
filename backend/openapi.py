"""
OpenAPI 3.0.1 spec generator for the fulcrumaxe REST API.

Reads the ROUTES registry from api_routes.py and produces a valid OpenAPI 3.0.1
JSON document. No new dependencies — the spec is just a Python dict
serialized to JSON.

Usage:
    from backend.openapi import generate_spec
    spec = generate_spec()

    # Or run standalone to print the spec:
    python backend/openapi.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.api_routes import VERSIONED_ROUTES as ROUTES  # noqa: E402
from backend.api_examples import EXAMPLES  # noqa: E402


# ---------------------------------------------------------------------------
# Tag metadata — descriptions shown in Swagger UI tag sections
# ---------------------------------------------------------------------------

_TAG_DESCRIPTIONS: dict[str, str] = {
    "health": "Server liveness and loop heartbeat checks. No auth required.",
    "budget": "Token budget management — view usage and set ceilings.",
    "cost": "Cost tracking in USD — per-agent, per-discussion, per-model.",
    "registry": "GitHub Discussion registry — the team's tracked backlog.",
    "control": "Feature gates and per-role policy settings.",
    "agents": "Agent card registry — registered roles and their status.",
    "plugins": "Loaded plugin metadata and hooks.",
    "kpi": "Key performance indicators — velocity, cycle time, estimation accuracy.",
    "deps": "Static module dependency graph and impact analysis.",
    "stream": "Server-Sent Events (SSE) and WebSocket agent output feeds.",
    "spawn-queue": "Agent spawn queue — pending and active spawn requests.",
    "notifications": "Notification delivery and testing.",
    "audit": "Audit trail of all team actions.",
    "backup": "State backup management.",
    "validate": "Payload schema validation.",
    "replays": "Recorded session replays.",
    "benchmarks": "Performance benchmarks collected during loop runs.",
    "traces": "Distributed traces for agent runs.",
    "metrics": "Prometheus-format metrics endpoint.",
}


# ---------------------------------------------------------------------------
# OpenAPI spec builder
# ---------------------------------------------------------------------------

_OPENAPI_VERSION = "3.0.1"
_API_TITLE = "fulcrumaxe API"
_API_VERSION = "1.0.0"
_API_DESCRIPTION = (
    "REST API gateway for the fulcrumaxe backend — budget, registry, "
    "control plane, agent cards, KPI, SSE streams, and interactive docs."
)


def _to_openapi_path(path: str) -> str:
    """Convert Flask-style /agents/<role> to OpenAPI /agents/{role}."""
    return re.sub(r"<([^>]+)>", r"{\1}", path)


def _extract_path_params(path: str) -> list[str]:
    """Return a list of path parameter names from a path like /agents/<role>."""
    return re.findall(r"<([^>]+)>", path)


def _default_description(status_code: int) -> str:
    descriptions = {
        200: "Success",
        201: "Created",
        400: "Bad request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not found",
        429: "Rate limit exceeded",
        500: "Internal server error",
    }
    return descriptions.get(status_code, str(status_code))


def _build_responses(responses: dict, example_response: object = None) -> dict:
    """Convert the route registry responses dict to OpenAPI responses object.

    If example_response is provided, inject it as an example into the 200
    response content block.
    """
    result = {}
    for status_code, info in responses.items():
        entry: dict = {
            "description": info.get("description", _default_description(int(status_code)))
        }
        schema = info.get("schema")
        if schema:
            content_block: dict = {"schema": schema}
            if example_response is not None and str(status_code) == "200":
                content_block["example"] = example_response
            entry["content"] = {"application/json": content_block}
        result[str(status_code)] = entry

    # Always add a generic error response if not already present.
    if "default" not in result and "500" not in result:
        result["default"] = {
            "description": "Unexpected error",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "error": {"type": "string"},
                        },
                        "required": ["error"],
                    }
                }
            },
        }
    return result


def _build_path_item(route: dict) -> tuple[str, str, dict]:
    """Return (openapi_path, method, operation_dict) for one route entry."""
    method = route["method"].lower()

    # Look up examples for this route using "METHOD /path" key.
    example_key = f"{route['method'].upper()} {route['path']}"
    example_data = EXAMPLES.get(example_key, {})

    operation: dict = {
        "summary": route["summary"],
        "tags": route.get("tags", []),
        "responses": _build_responses(
            route.get("responses", {}),
            example_response=example_data.get("response_body"),
        ),
    }

    if route.get("deprecated"):
        operation["deprecated"] = True

    description = route.get("description")
    if description:
        operation["description"] = description

    # Attach x-getting-started extension when flagged in examples.
    if example_data.get("getting_started"):
        operation["x-getting-started"] = True

    parameters = list(route.get("parameters", []))

    # Auto-add path parameters not already listed.
    path_params = _extract_path_params(route["path"])
    for pname in path_params:
        if not any(p.get("name") == pname and p.get("in") == "path" for p in parameters):
            parameters.append(
                {
                    "name": pname,
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            )

    if parameters:
        operation["parameters"] = parameters

    request_body = route.get("request_body")
    if request_body:
        content_block: dict = {"schema": request_body}
        # Inject example request body when available.
        if example_data.get("request_body") is not None:
            content_block["example"] = example_data["request_body"]
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": content_block},
        }

    if route.get("auth", False):
        operation["security"] = [{"bearerAuth": []}]

    openapi_path = _to_openapi_path(route["path"])
    return openapi_path, method, operation


def generate_spec() -> dict:
    """Build and return the full OpenAPI 3.0.1 spec as a Python dict."""
    paths: dict = {}

    for route in ROUTES:
        openapi_path, method, operation = _build_path_item(route)
        if openapi_path not in paths:
            paths[openapi_path] = {}
        paths[openapi_path][method] = operation

    # Collect unique tags in declaration order.
    all_tags: list[str] = []
    seen: set[str] = set()
    for route in ROUTES:
        for tag in route.get("tags", []):
            if tag not in seen:
                all_tags.append(tag)
                seen.add(tag)

    spec = {
        "openapi": _OPENAPI_VERSION,
        "info": {
            "title": _API_TITLE,
            "version": _API_VERSION,
            "description": _API_DESCRIPTION,
        },
        "servers": [{"url": "/", "description": "This server"}],
        "tags": [
            {"name": t, "description": _TAG_DESCRIPTIONS.get(t, "")}
            for t in all_tags
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "API key passed as a Bearer token. "
                        "Set AF_API_AUTH_KEY on the server to enable auth."
                    ),
                }
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {"type": "string"},
                    },
                    "required": ["error"],
                }
            },
        },
    }
    return spec


# ---------------------------------------------------------------------------
# Swagger UI HTML — enhanced with Getting Started panel, curl buttons,
# response previews, pre-filled examples, and dark/light theme toggle.
# All customisation is vanilla JS/CSS — no build step, no npm.
# ---------------------------------------------------------------------------

_SWAGGER_UI_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
_HLJS_CDN = "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build"


def _getting_started_cards_json() -> str:
    """Return a JSON array of Getting Started card data for injection into the page."""
    from backend.api_examples import EXAMPLES, GETTING_STARTED_ORDER  # local import avoids cycle

    cards = []
    for key in GETTING_STARTED_ORDER:
        ex = EXAMPLES.get(key, {})
        method, path = key.split(" ", 1)
        cards.append(
            {
                "method": method,
                "path": path,
                "description": ex.get("description", ""),
            }
        )
    return json.dumps(cards)


def get_docs_html() -> str:
    """Return a self-contained HTML page that renders the OpenAPI spec via Swagger UI."""
    gs_cards = _getting_started_cards_json()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>fulcrumaxe API — Docs</title>
  <link rel="stylesheet" href="{_SWAGGER_UI_CDN}/swagger-ui.css">
  <link rel="stylesheet" href="{_HLJS_CDN}/styles/github-dark.min.css" id="hljs-theme">
  <style>
    /* ── base ─────────────────────────────────────────────── */
    :root {{
      --bg: #fff;
      --fg: #1a1a1a;
      --card-bg: #f8f9fa;
      --card-border: #e0e0e0;
      --accent: #4a90e2;
      --tag: #6c757d;
      --method-get: #0d904f;
      --method-post: #bf5900;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #1a1a1a;
        --fg: #e8e8e8;
        --card-bg: #2a2a2a;
        --card-border: #444;
        --accent: #6cb3f5;
      }}
    }}
    body.dark {{
      --bg: #1a1a1a;
      --fg: #e8e8e8;
      --card-bg: #2a2a2a;
      --card-border: #444;
      --accent: #6cb3f5;
    }}
    body.light {{
      --bg: #fff;
      --fg: #1a1a1a;
      --card-bg: #f8f9fa;
      --card-border: #e0e0e0;
      --accent: #4a90e2;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      transition: background 0.2s, color 0.2s;
    }}
    #page-wrap {{ max-width: 1280px; margin: 0 auto; padding: 0 16px 40px; }}

    /* ── top bar ──────────────────────────────────────────── */
    #top-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 0;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 20px;
    }}
    #top-bar h1 {{ margin: 0; font-size: 1.1rem; }}
    #theme-toggle {{
      cursor: pointer;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 5px 12px;
      font-size: 0.85rem;
      color: var(--fg);
    }}
    #theme-toggle:hover {{ opacity: 0.8; }}

    /* ── Getting Started panel ───────────────────────────── */
    #getting-started {{
      margin-bottom: 28px;
    }}
    #getting-started h2 {{
      font-size: 1rem;
      font-weight: 600;
      margin: 0 0 12px;
      color: var(--fg);
    }}
    #gs-cards {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
    }}
    .gs-card {{
      flex: 1 1 200px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 14px 16px;
      cursor: pointer;
      transition: border-color 0.15s;
    }}
    .gs-card:hover {{ border-color: var(--accent); }}
    .gs-card .gs-method {{
      font-size: 0.7rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 4px;
    }}
    .gs-card .gs-method.get {{ color: var(--method-get); }}
    .gs-card .gs-method.post {{ color: var(--method-post); }}
    .gs-card .gs-path {{
      font-size: 0.9rem;
      font-weight: 600;
      margin-bottom: 6px;
      color: var(--fg);
    }}
    .gs-card .gs-desc {{
      font-size: 0.8rem;
      color: var(--tag);
      line-height: 1.4;
      margin-bottom: 10px;
    }}
    .gs-try-btn {{
      display: inline-block;
      background: var(--accent);
      color: #fff;
      border: none;
      border-radius: 4px;
      padding: 4px 10px;
      font-size: 0.78rem;
      cursor: pointer;
    }}
    .gs-try-btn:hover {{ opacity: 0.85; }}

    /* ── swagger-ui wrapper ───────────────────────────────── */
    #swagger-ui {{ margin-top: 0; }}

    /* ── curl copy button injected per-operation ─────────── */
    .af-curl-btn {{
      background: transparent;
      border: 1px solid var(--card-border, #ccc);
      border-radius: 4px;
      color: var(--tag, #666);
      cursor: pointer;
      font-size: 0.72rem;
      margin-left: 8px;
      padding: 2px 8px;
      vertical-align: middle;
    }}
    .af-curl-btn:hover {{ border-color: var(--accent, #4a90e2); color: var(--accent, #4a90e2); }}

    /* ── response preview blocks ─────────────────────────── */
    .af-preview {{
      background: #1e2329;
      border-radius: 6px;
      margin: 8px 0 0;
      overflow: hidden;
    }}
    .af-preview-label {{
      background: #2c3240;
      color: #8b9dc3;
      font-size: 0.72rem;
      padding: 4px 10px;
    }}
    .af-preview pre {{
      margin: 0;
      padding: 10px 12px;
      overflow-x: auto;
      font-size: 0.8rem;
    }}
  </style>
</head>
<body>
  <div id="page-wrap">
    <!-- Top bar -->
    <div id="top-bar">
      <h1>fulcrumaxe API</h1>
      <button id="theme-toggle" onclick="toggleTheme()">Toggle dark/light</button>
    </div>

    <!-- Getting Started panel -->
    <div id="getting-started">
      <h2>Getting Started</h2>
      <div id="gs-cards"></div>
    </div>

    <!-- Swagger UI mount point -->
    <div id="swagger-ui"></div>
  </div>

  <script src="{_SWAGGER_UI_CDN}/swagger-ui-bundle.js"></script>
  <script src="{_HLJS_CDN}/highlight.min.js"></script>
  <script>
  // ── theme ────────────────────────────────────────────────────────────────
  (function () {{
    var stored = localStorage.getItem('af-theme');
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var theme = stored || (prefersDark ? 'dark' : 'light');
    document.body.classList.add(theme);
    var hljsLink = document.getElementById('hljs-theme');
    if (theme === 'light') {{
      hljsLink.href = '{_HLJS_CDN}/styles/github.min.css';
    }}
  }})();

  function toggleTheme() {{
    var isDark = document.body.classList.contains('dark');
    document.body.classList.toggle('dark', !isDark);
    document.body.classList.toggle('light', isDark);
    localStorage.setItem('af-theme', isDark ? 'light' : 'dark');
    var hljsLink = document.getElementById('hljs-theme');
    hljsLink.href = isDark
      ? '{_HLJS_CDN}/styles/github.min.css'
      : '{_HLJS_CDN}/styles/github-dark.min.css';
  }}

  // ── Getting Started cards ───────────────────────────────────────────────
  var GS_CARDS = {gs_cards};

  function buildGsCards() {{
    var wrap = document.getElementById('gs-cards');
    GS_CARDS.forEach(function (card) {{
      var div = document.createElement('div');
      div.className = 'gs-card';
      div.innerHTML =
        '<div class="gs-method ' + card.method.toLowerCase() + '">' + card.method + '</div>' +
        '<div class="gs-path">' + card.path + '</div>' +
        '<div class="gs-desc">' + card.desc_escaped(card.description) + '</div>' +
        '<button class="gs-try-btn">Try it</button>';
      div.querySelector('.gs-try-btn').addEventListener('click', function () {{
        scrollAndOpenOperation(card.method, card.path);
      }});
      wrap.appendChild(div);
    }});
  }}

  // Minimal HTML escaping for card descriptions
  GS_CARDS.forEach(function(c) {{
    c.desc_escaped = function(s) {{
      return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }};
  }});

  // ── Swagger UI ──────────────────────────────────────────────────────────
  var ui;
  function initSwagger() {{
    ui = SwaggerUIBundle({{
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
      layout: "BaseLayout",
      deepLinking: true,
      tryItOutEnabled: false,
      requestSnippetsEnabled: false,
      onComplete: function() {{
        setTimeout(postRenderEnhancements, 400);
      }}
    }});
  }}

  // ── post-render enhancements ────────────────────────────────────────────
  var _specCache = null;

  function fetchSpec(cb) {{
    if (_specCache) {{ cb(_specCache); return; }}
    fetch('/openapi.json').then(function(r) {{ return r.json(); }}).then(function(s) {{
      _specCache = s;
      cb(s);
    }});
  }}

  function postRenderEnhancements() {{
    fetchSpec(function(spec) {{
      injectResponsePreviews(spec);
      injectCurlButtons(spec);
    }});
  }}

  // Build a curl command for an operation
  function buildCurl(method, path, exampleBody) {{
    var base = window.location.origin;
    var url = base + path;
    var cmd = 'curl -s -X ' + method + ' "' + url + '"';
    cmd += ' \\\\ -H "Content-Type: application/json"';
    if (exampleBody) {{
      cmd += ' \\\\ -d \\'' + JSON.stringify(exampleBody) + '\\'';
    }}
    return cmd;
  }}

  function getExampleBody(spec, method, path) {{
    try {{
      return spec.paths[path][method.toLowerCase()].requestBody.content['application/json'].example;
    }} catch(e) {{ return null; }}
  }}

  function getExampleResponse(spec, method, path) {{
    try {{
      return spec.paths[path][method.toLowerCase()].responses['200'].content['application/json'].example;
    }} catch(e) {{ return null; }}
  }}

  // Inject response preview blocks into each operation section
  function injectResponsePreviews(spec) {{
    var opblocks = document.querySelectorAll('.opblock');
    opblocks.forEach(function(block) {{
      var methodEl = block.querySelector('.opblock-summary-method');
      var pathEl = block.querySelector('.opblock-summary-path');
      if (!methodEl || !pathEl) return;
      var method = (methodEl.textContent || '').trim().toLowerCase();
      var path = (pathEl.getAttribute('data-path') || pathEl.textContent || '').trim();
      var example = getExampleResponse(spec, method, path);
      if (!example) return;
      var descEl = block.querySelector('.opblock-summary-description') ||
                   block.querySelector('.opblock-description-wrapper');
      if (!descEl) return;
      if (block.querySelector('.af-preview')) return; // already injected
      var pre = document.createElement('div');
      pre.className = 'af-preview';
      pre.innerHTML =
        '<div class="af-preview-label">Example response</div>' +
        '<pre><code class="language-json">' +
        escapeHtml(JSON.stringify(example, null, 2)) +
        '</code></pre>';
      descEl.after(pre);
      pre.querySelectorAll('code').forEach(function(el) {{ hljs.highlightElement(el); }});
    }});
  }}

  // Inject Copy-as-curl buttons next to each operation's method badge
  function injectCurlButtons(spec) {{
    var opblocks = document.querySelectorAll('.opblock');
    opblocks.forEach(function(block) {{
      var methodEl = block.querySelector('.opblock-summary-method');
      var pathEl = block.querySelector('.opblock-summary-path');
      if (!methodEl || !pathEl) return;
      if (block.querySelector('.af-curl-btn')) return; // already injected
      var method = (methodEl.textContent || '').trim().toUpperCase();
      var path = (pathEl.getAttribute('data-path') || pathEl.textContent || '').trim();
      var exampleBody = getExampleBody(spec, method, path);
      var curl = buildCurl(method, path, exampleBody);
      var btn = document.createElement('button');
      btn.className = 'af-curl-btn';
      btn.title = 'Copy as curl';
      btn.textContent = 'curl';
      btn.addEventListener('click', function(e) {{
        e.stopPropagation();
        navigator.clipboard.writeText(curl).then(function() {{
          btn.textContent = 'copied!';
          setTimeout(function() {{ btn.textContent = 'curl'; }}, 1500);
        }}).catch(function() {{
          // Fallback for insecure contexts
          var ta = document.createElement('textarea');
          ta.value = curl;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
          btn.textContent = 'copied!';
          setTimeout(function() {{ btn.textContent = 'curl'; }}, 1500);
        }});
      }});
      methodEl.parentNode.insertBefore(btn, methodEl.nextSibling);
    }});
  }}

  // ── scroll + open a specific operation ─────────────────────────────────
  function scrollAndOpenOperation(method, path) {{
    // Swagger UI renders operations with an id like "operations-<tag>-<method><path_slug>"
    // The most reliable approach is to match by visible text in the summary.
    var opblocks = document.querySelectorAll('.opblock');
    for (var i = 0; i < opblocks.length; i++) {{
      var block = opblocks[i];
      var methodEl = block.querySelector('.opblock-summary-method');
      var pathEl = block.querySelector('.opblock-summary-path');
      if (!methodEl || !pathEl) continue;
      var bMethod = (methodEl.textContent || '').trim().toUpperCase();
      var bPath = (pathEl.getAttribute('data-path') || pathEl.textContent || '').trim();
      if (bMethod === method.toUpperCase() && bPath === path) {{
        block.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
        // Open the block if collapsed
        var summary = block.querySelector('.opblock-summary');
        if (summary && block.classList.contains('is-open') === false) {{
          summary.click();
        }}
        // Click "Try it out" button
        setTimeout(function(b) {{
          return function() {{
            var tryBtn = b.querySelector('.try-out__btn');
            if (tryBtn && !b.classList.contains('try-out')) tryBtn.click();
          }};
        }}(block), 300);
        return;
      }}
    }}
  }}

  function escapeHtml(s) {{
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  // ── boot ─────────────────────────────────────────────────────────────────
  buildGsCards();
  initSwagger();

  // Re-run enhancements when Swagger UI re-renders (e.g. tag expand/collapse)
  var _enhancementTimer = null;
  var _observer = new MutationObserver(function() {{
    clearTimeout(_enhancementTimer);
    _enhancementTimer = setTimeout(function() {{
      fetchSpec(function(spec) {{
        injectResponsePreviews(spec);
        injectCurlButtons(spec);
      }});
    }}, 250);
  }});
  _observer.observe(document.getElementById('swagger-ui'), {{childList: true, subtree: true}});
  </script>
</body>
</html>"""


if __name__ == "__main__":
    print(json.dumps(generate_spec(), indent=2))
