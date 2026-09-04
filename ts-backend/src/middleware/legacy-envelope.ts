/**
 * Legacy-envelope response middleware — TypeScript port of
 * backend/middleware/legacy_envelope.py LegacyEnvelopeMiddleware.
 *
 * Reconciles the TS backend's response shape with what the dashboard
 * expects from the legacy Python server:
 *   - Injects _api_version into every dict (object) JSON body
 *   - 4xx responses with "detail" are rewritten to {"error": <detail>, ...}
 *   - Array JSON bodies pass through unchanged (Rule 3)
 *   - /rpc path passes through (Rule 4)
 *   - SSE / streaming content-types pass through (Rule 1)
 *   - 500 emits generic {"error": "internal error", "_api_version": 1}
 *
 * Rules mirror Python LegacyEnvelopeMiddleware._rewrite_body() exactly.
 *
 * CURRENT_VERSION = 1 (mirrors backend/api_version.py CURRENT_VERSION)
 */

import type { Context, MiddlewareHandler, Next } from "hono";

const CURRENT_VERSION = 1;

// Content-type prefixes that must never be buffered (streaming protocols).
const STREAMING_PREFIXES = ["text/event-stream", "multipart/"];

function isStreamingContentType(ct: string): boolean {
  const ctLower = ct.toLowerCase();
  return STREAMING_PREFIXES.some((p) => ctLower.startsWith(p));
}

function genericFiveHundred(): Response {
  const body = JSON.stringify({
    error: "internal error",
    _api_version: CURRENT_VERSION,
  });
  return new Response(body, {
    status: 500,
    headers: {
      "content-type": "application/json",
      "content-length": String(new TextEncoder().encode(body).length),
    },
  });
}

function rewriteBody(
  statusCode: number,
  data: Record<string, unknown>
): Record<string, unknown> {
  // Rule 4: JSON-RPC bodies exempt.
  if ("jsonrpc" in data) return data;

  // Rule 5: 500 only (safety net; normally handled by genericFiveHundred).
  if (statusCode === 500) {
    return { error: "internal error", _api_version: CURRENT_VERSION };
  }

  // Rule 6: 4xx with "detail" and no "error".
  if (statusCode >= 400 && "detail" in data && !("error" in data)) {
    const detail = data["detail"];
    const rest = { ...data };
    delete rest["detail"];
    if ("_api_version" in rest) {
      const v = rest["_api_version"];
      delete rest["_api_version"];
      return { _api_version: v, error: detail, ...rest };
    }
    return { _api_version: CURRENT_VERSION, error: detail, ...rest };
  }

  // Rule 7: Inject _api_version at front if absent.
  if (!("_api_version" in data)) {
    return { _api_version: CURRENT_VERSION, ...data };
  }
  return data;
}

/**
 * Hono middleware that replicates Python LegacyEnvelopeMiddleware.
 *
 * Must be registered LAST in the middleware chain so it wraps all responses.
 * In Hono, middleware registered with app.use("*", ...) runs in registration
 * order — register this after auth and rate-limit (but it's on the response
 * path, so "last" means it processes responses from the outermost wrapping).
 *
 * We implement it as a "wrap" middleware using await next(); response = ctx.res.
 */
export const legacyEnvelopeMiddleware: MiddlewareHandler = async (
  c: Context,
  next: Next
): Promise<void | Response> => {
  // Run downstream handlers first
  try {
    await next();
  } catch {
    // Unhandled exception from downstream — return generic 5xx (CWE-209)
    c.res = genericFiveHundred();
    return;
  }

  const res = c.res;
  if (!res) return;

  const ct = res.headers.get("content-type") ?? "";

  // Rule 1: Streaming content-type — pass through.
  if (isStreamingContentType(ct)) return;

  // Rule 2: Non-JSON content-type — pass through.
  if (!ct.startsWith("application/json")) return;

  // Rule 2: 204 — pass through.
  if (res.status === 204) return;

  // Buffer the body.
  let bodyText: string;
  try {
    bodyText = await res.text();
  } catch {
    return; // Can't read body — pass through
  }

  if (!bodyText) return; // Empty body — pass through

  // Rule 5: 500 — generic body (CWE-209)
  if (res.status === 500) {
    c.res = genericFiveHundred();
    return;
  }

  // Parse JSON
  let data: unknown;
  try {
    data = JSON.parse(bodyText);
  } catch {
    // Not valid JSON — pass through unchanged
    c.res = new Response(bodyText, {
      status: res.status,
      headers: res.headers,
    });
    return;
  }

  // Rule 3: Non-dict JSON (e.g. array) — pass through unchanged.
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    c.res = new Response(bodyText, {
      status: res.status,
      headers: res.headers,
    });
    return;
  }

  // Rule 4: /rpc path — pass through.
  const path = new URL(c.req.url).pathname;
  if (path === "/rpc" || path.startsWith("/rpc/")) {
    c.res = new Response(bodyText, {
      status: res.status,
      headers: res.headers,
    });
    return;
  }

  // Apply envelope rewrites.
  const rewritten = rewriteBody(
    res.status,
    data as Record<string, unknown>
  );

  const newBody = JSON.stringify(rewritten);
  const encoder = new TextEncoder();
  const newBodyBytes = encoder.encode(newBody);

  // Rebuild headers — update content-length, preserve all others.
  const newHeaders = new Headers(res.headers);
  newHeaders.set("content-length", String(newBodyBytes.length));
  newHeaders.set("content-type", "application/json");

  c.res = new Response(newBody, {
    status: res.status,
    headers: newHeaders,
  });
};
