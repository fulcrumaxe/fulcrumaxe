/**
 * openapi.test.ts — validate the OpenAPI spec generator output.
 *
 * Run: bun test tests/openapi.test.ts --timeout 15000
 *
 * Covers:
 *   1. Well-formed OpenAPI document — required top-level fields present
 *   2. Every path has at least one HTTP method
 *   3. Every operation has at least one response
 *   4. The committed openapi.json matches generator output (drift guard)
 *   5. GET /openapi.json HTTP route returns the spec
 *   6. /openapi.json is reachable without auth (public route)
 */

import { describe, it, expect } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Hono } from "hono";
import { buildOpenApiDocument, type OpenApiDocument } from "../src/openapi.js";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { legacyEnvelopeMiddleware } from "../src/middleware/legacy-envelope.js";

// ---------------------------------------------------------------------------
// Helper — build a minimal test app that mirrors the index.ts structure
// ---------------------------------------------------------------------------

function buildTestApp(): Hono {
  const app = new Hono();
  app.use("*", legacyEnvelopeMiddleware);
  app.use("*", defaultDenyMiddleware);
  app.get("/openapi.json", (c) => c.json(buildOpenApiDocument()));
  return app;
}

// ---------------------------------------------------------------------------
// 1. Well-formed OpenAPI document
// ---------------------------------------------------------------------------

describe("buildOpenApiDocument — top-level structure", () => {
  it("returns an object with required OpenAPI 3.x fields", () => {
    const doc = buildOpenApiDocument();
    expect(typeof doc).toBe("object");
    expect(doc).not.toBeNull();
  });

  it("has openapi field starting with '3.'", () => {
    const doc = buildOpenApiDocument();
    expect(typeof doc.openapi).toBe("string");
    expect(doc.openapi.startsWith("3.")).toBe(true);
  });

  it("has info with title and version", () => {
    const doc = buildOpenApiDocument();
    expect(typeof doc.info).toBe("object");
    expect(typeof doc.info.title).toBe("string");
    expect((doc.info.title as string).length).toBeGreaterThan(0);
    expect(typeof doc.info.version).toBe("string");
    expect((doc.info.version as string).length).toBeGreaterThan(0);
  });

  it("has a paths object", () => {
    const doc = buildOpenApiDocument();
    expect(typeof doc.paths).toBe("object");
    expect(doc.paths).not.toBeNull();
  });

  it("has a components object with securitySchemes and schemas", () => {
    const doc = buildOpenApiDocument();
    expect(typeof doc.components).toBe("object");
    expect(typeof doc.components.securitySchemes).toBe("object");
    expect(typeof doc.components.schemas).toBe("object");
  });

  it("has at least one server entry", () => {
    const doc = buildOpenApiDocument();
    expect(Array.isArray(doc.servers)).toBe(true);
    expect(doc.servers.length).toBeGreaterThan(0);
    const server = doc.servers[0] as Record<string, unknown>;
    expect(typeof server.url).toBe("string");
  });

  it("has a tags array", () => {
    const doc = buildOpenApiDocument();
    expect(Array.isArray(doc.tags)).toBe(true);
    expect(doc.tags.length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// 2. Every path has at least one HTTP method
// ---------------------------------------------------------------------------

describe("buildOpenApiDocument — paths coverage", () => {
  it("every path has at least one HTTP method defined", () => {
    const doc = buildOpenApiDocument();
    const HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options"];
    for (const pathItem of Object.values(doc.paths)) {
      const methods = Object.keys(pathItem as Record<string, unknown>).filter((k) =>
        HTTP_METHODS.includes(k.toLowerCase())
      );
      expect(methods.length).toBeGreaterThan(0);
    }
  });

  it("covers the expected set of routes", () => {
    const doc = buildOpenApiDocument();
    const paths = Object.keys(doc.paths);
    const expected = [
      "/health",
      "/sessions",
      "/sessions/current",
      "/sessions/compare",
      "/sessions/{session_id}",
      "/spawn-queue",
      "/spawn-queue/pending",
      "/spawn-queue/active",
      "/spawn-blocks",
      "/stats/metrics/summary",
      "/stats/metrics/series/{name}",
      "/feed",
      "/events",
      "/budget/init",
      "/rpc",
      "/graphql",
      "/openapi.json",
    ];
    for (const expectedPath of expected) {
      expect(paths).toContain(expectedPath);
    }
  });

  it("GET /health is marked as a public route (security: [])", () => {
    const doc = buildOpenApiDocument();
    const healthGet = (doc.paths["/health"] as Record<string, unknown>)["get"] as Record<string, unknown>;
    expect(Array.isArray(healthGet.security)).toBe(true);
    expect((healthGet.security as unknown[]).length).toBe(0);
  });

  it("GET /openapi.json is marked public (security: [])", () => {
    const doc = buildOpenApiDocument();
    const openapiGet = (doc.paths["/openapi.json"] as Record<string, unknown>)["get"] as Record<string, unknown>;
    expect(Array.isArray(openapiGet.security)).toBe(true);
    expect((openapiGet.security as unknown[]).length).toBe(0);
  });

  it("POST /rpc has no top-level security (uses its own RPC token auth)", () => {
    const doc = buildOpenApiDocument();
    const rpcPost = (doc.paths["/rpc"] as Record<string, unknown>)["post"] as Record<string, unknown>;
    expect(Array.isArray(rpcPost.security)).toBe(true);
    expect((rpcPost.security as unknown[]).length).toBe(0);
  });

  it("auth-gated routes (GET /sessions) require BearerAuth", () => {
    const doc = buildOpenApiDocument();
    const sessionsGet = (doc.paths["/sessions"] as Record<string, unknown>)["get"] as Record<string, unknown>;
    const security = sessionsGet.security as Array<Record<string, unknown>>;
    expect(Array.isArray(security)).toBe(true);
    expect(security.length).toBeGreaterThan(0);
    const hasBearerAuth = security.some((s) => "BearerAuth" in s);
    expect(hasBearerAuth).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 3. Every operation has at least one response
// ---------------------------------------------------------------------------

describe("buildOpenApiDocument — operation completeness", () => {
  it("every operation has at least one response code defined", () => {
    const doc = buildOpenApiDocument();
    const HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options"];
    for (const pathItem of Object.values(doc.paths)) {
      for (const [method, op] of Object.entries(pathItem as Record<string, unknown>)) {
        if (!HTTP_METHODS.includes(method.toLowerCase())) continue;
        const operation = op as Record<string, unknown>;
        expect(typeof operation.responses).toBe("object");
        expect(operation.responses).not.toBeNull();
        const responseCodes = Object.keys(operation.responses as Record<string, unknown>);
        expect(responseCodes.length).toBeGreaterThan(0);
      }
    }
  });

  it("every operation has an operationId", () => {
    const doc = buildOpenApiDocument();
    const HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options"];
    for (const pathItem of Object.values(doc.paths)) {
      for (const [method, op] of Object.entries(pathItem as Record<string, unknown>)) {
        if (!HTTP_METHODS.includes(method.toLowerCase())) continue;
        const operation = op as Record<string, unknown>;
        expect(typeof operation.operationId).toBe("string");
        expect((operation.operationId as string).length).toBeGreaterThan(0);
      }
    }
  });

  it("every operation has at least one tag", () => {
    const doc = buildOpenApiDocument();
    const HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options"];
    for (const pathItem of Object.values(doc.paths)) {
      for (const [method, op] of Object.entries(pathItem as Record<string, unknown>)) {
        if (!HTTP_METHODS.includes(method.toLowerCase())) continue;
        const operation = op as Record<string, unknown>;
        expect(Array.isArray(operation.tags)).toBe(true);
        expect((operation.tags as unknown[]).length).toBeGreaterThan(0);
      }
    }
  });

  it("error responses (4xx) use DetailError or JsonRpcResponse schema refs", () => {
    const doc = buildOpenApiDocument();
    const HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options"];
    const VALID_ERROR_REFS = [
      "#/components/schemas/DetailError",
      "#/components/schemas/JsonRpcResponse",
    ];
    for (const pathItem of Object.values(doc.paths)) {
      for (const [method, op] of Object.entries(pathItem as Record<string, unknown>)) {
        if (!HTTP_METHODS.includes(method.toLowerCase())) continue;
        const operation = op as Record<string, unknown>;
        const responses = operation.responses as Record<string, unknown>;
        for (const [code, resp] of Object.entries(responses)) {
          const statusCode = parseInt(code, 10);
          if (statusCode < 400 || statusCode > 499) continue;
          const response = resp as Record<string, unknown>;
          if (!response.content) continue; // some 4xx may not have content
          const contentTypes = response.content as Record<string, unknown>;
          for (const mediaType of Object.values(contentTypes)) {
            const mt = mediaType as Record<string, unknown>;
            if (!mt.schema) continue;
            const schema = mt.schema as Record<string, unknown>;
            if (schema.$ref) {
              expect(VALID_ERROR_REFS).toContain(schema.$ref as string);
            }
          }
        }
      }
    }
  });
});

// ---------------------------------------------------------------------------
// 4. Drift guard — committed openapi.json matches generator output
// ---------------------------------------------------------------------------

describe("openapi.json snapshot drift guard", () => {
  it("committed openapi.json matches buildOpenApiDocument() output", () => {
    const snapshotPath = join(import.meta.dir, "..", "openapi.json");
    const snapshotRaw = readFileSync(snapshotPath, "utf-8");
    const snapshot = JSON.parse(snapshotRaw) as unknown;
    const generated = buildOpenApiDocument();
    // Deep-equal comparison — if generator changes, regenerate with `bun run openapi:gen`
    expect(JSON.stringify(generated, null, 2)).toBe(JSON.stringify(snapshot, null, 2));
  });
});

// ---------------------------------------------------------------------------
// 5 & 6. HTTP route — GET /openapi.json returns the spec, no auth needed
// ---------------------------------------------------------------------------

describe("GET /openapi.json HTTP route", () => {
  it("returns 200 with the spec document when AF_API_AUTH_KEY is set", async () => {
    const savedKey = process.env.AF_API_AUTH_KEY;
    process.env.AF_API_AUTH_KEY = "test-key-for-openapi";
    try {
      const app = buildTestApp();
      const res = await app.request("/openapi.json");
      expect(res.status).toBe(200);
      const body = await res.json() as OpenApiDocument;
      expect(typeof body.openapi).toBe("string");
      expect(body.openapi.startsWith("3.")).toBe(true);
      expect(typeof body.paths).toBe("object");
    } finally {
      if (savedKey === undefined) {
        delete process.env.AF_API_AUTH_KEY;
      } else {
        process.env.AF_API_AUTH_KEY = savedKey;
      }
    }
  });

  it("returns 200 with no auth header (public route)", async () => {
    const savedKey = process.env.AF_API_AUTH_KEY;
    process.env.AF_API_AUTH_KEY = "test-key-for-openapi";
    try {
      const app = buildTestApp();
      // No Authorization header — should still pass because /openapi.json is public
      const res = await app.request("/openapi.json", {
        headers: {},
      });
      expect(res.status).toBe(200);
    } finally {
      if (savedKey === undefined) {
        delete process.env.AF_API_AUTH_KEY;
      } else {
        process.env.AF_API_AUTH_KEY = savedKey;
      }
    }
  });

  it("response content-type is application/json", async () => {
    const app = buildTestApp();
    const res = await app.request("/openapi.json");
    const ct = res.headers.get("content-type") ?? "";
    expect(ct).toContain("application/json");
  });

  it("response body contains all expected top-level keys", async () => {
    const app = buildTestApp();
    const res = await app.request("/openapi.json");
    const body = await res.json() as Record<string, unknown>;
    for (const key of ["openapi", "info", "paths", "components", "servers", "tags"]) {
      expect(body).toHaveProperty(key);
    }
  });
});
