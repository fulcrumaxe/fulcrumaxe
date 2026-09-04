/**
 * Tests for POST /graphql — home-grown GraphQL endpoint parity (D#1437 P6c).
 * Run: bun test tests/graphql.test.ts --timeout 30000
 *
 * Coverage: parser, executor, auth 401/403, missing query → 400,
 * resolver unit tests (spawnQueue/control/agents/audit/kpi/health quirks),
 * introspection (__schema/__type), Python quirk parity (null fields).
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { graphqlHandler } from "../src/routes/graphql.js";

// ---------------------------------------------------------------------------
// App factory
// ---------------------------------------------------------------------------

function makeApp(authKey?: string): Hono {
  if (authKey !== undefined) {
    process.env.AF_API_AUTH_KEY = authKey;
  } else {
    delete process.env.AF_API_AUTH_KEY;
  }
  const app = new Hono();
  app.use("*", defaultDenyMiddleware);
  app.post("/graphql", graphqlHandler);
  return app;
}

async function gql(
  app: Hono,
  query: string,
  authKey?: string,
): Promise<{ status: number; body: unknown }> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authKey !== undefined) {
    headers["Authorization"] = `Bearer ${authKey}`;
  }
  const resp = await app.request("/graphql", {
    method: "POST",
    headers,
    body: JSON.stringify({ query }),
  });
  const body = await resp.json();
  return { status: resp.status, body };
}

// ---------------------------------------------------------------------------
// §1 — HTTP-level: body parsing + missing query
// ---------------------------------------------------------------------------

describe("POST /graphql — body parsing + missing query", () => {
  let app: Hono;

  beforeEach(() => {
    app = makeApp(); // no auth
  });

  afterEach(() => {
    delete process.env.AF_API_AUTH_KEY;
  });

  it("returns 400 with correct detail when query is missing", async () => {
    const resp = await app.request("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    expect(resp.status).toBe(400);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["detail"]).toBe("'query' is required");
  });

  it("returns 400 when query is empty string", async () => {
    const resp = await app.request("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: "" }),
    });
    expect(resp.status).toBe(400);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["detail"]).toBe("'query' is required");
  });

  it("malformed JSON body treated as no body → 400", async () => {
    // Python: bare except on json() → body stays {} → query missing → 400
    const resp = await app.request("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "not json",
    });
    expect(resp.status).toBe(400);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["detail"]).toBe("'query' is required");
  });

  it("Python quirk: unknown chars in query are silently skipped by tokenizer", async () => {
    // Python tokenizer uses finditer — non-matching chars are skipped.
    // { !! } → tokens: LBRACE, RBRACE → empty selection set → {data:{}}
    const { status, body } = await gql(app, "{ !! }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["errors"]).toBeUndefined();
    expect(b["data"]).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// §2 — Auth parity: 401 / 403 / 200
// ---------------------------------------------------------------------------

describe("POST /graphql — auth parity", () => {
  afterEach(() => {
    delete process.env.AF_API_AUTH_KEY;
  });

  it("returns 401 when auth is required and no token is sent", async () => {
    const app = makeApp("secret-key");
    const resp = await app.request("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: "{ spawnQueue { pending_count } }" }),
    });
    expect(resp.status).toBe(401);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["detail"]).toBe("unauthorized");
  });

  it("returns 403 when auth is required and wrong token is sent", async () => {
    const app = makeApp("secret-key");
    const resp = await app.request("/graphql", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer wrong-key",
      },
      body: JSON.stringify({ query: "{ spawnQueue { pending_count } }" }),
    });
    expect(resp.status).toBe(403);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["detail"]).toBe("forbidden");
  });

  it("returns 200 when correct token is sent", async () => {
    const app = makeApp("correct-key");
    const resp = await app.request("/graphql", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer correct-key",
      },
      body: JSON.stringify({ query: "{ spawnQueue { pending_count } }" }),
    });
    expect(resp.status).toBe(200);
  });

  it("returns 200 when auth disabled", async () => {
    const app = makeApp();
    expect((await gql(app, "{ spawnQueue { pending_count } }")).status).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// §3 — GraphQL parser parity
// ---------------------------------------------------------------------------

describe("POST /graphql — parser parity", () => {
  let app: Hono;

  beforeEach(() => {
    app = makeApp();
  });

  afterEach(() => {
    delete process.env.AF_API_AUTH_KEY;
  });

  it("handles simple field selection", async () => {
    const { status, body } = await gql(app, "{ spawnQueue { pending_count active_count } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["data"]).toBeDefined();
    const data = b["data"] as Record<string, unknown>;
    expect(data["spawnQueue"]).toBeDefined();
    const sq = data["spawnQueue"] as Record<string, unknown>;
    expect(typeof sq["pending_count"]).toBe("number");
    expect(typeof sq["active_count"]).toBe("number");
  });

  it("handles 'query' and 'query OperationName' prefixes", async () => {
    for (const q of ["query { spawnQueue { pending_count } }", "query MyQuery { spawnQueue { pending_count } }"]) {
      const { status, body } = await gql(app, q);
      expect(status).toBe(200);
      expect((body as Record<string, unknown>)["data"]).toBeDefined();
    }
  });

  it("handles alias syntax", async () => {
    const { status, body } = await gql(app, "{ q: spawnQueue { cnt: pending_count } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    // alias 'q' used instead of 'spawnQueue'
    expect(data["q"]).toBeDefined();
    const q = data["q"] as Record<string, unknown>;
    // alias 'cnt' used instead of 'pending_count'
    expect(q["cnt"]).toBeDefined();
    expect(typeof q["cnt"]).toBe("number");
  });

  it("handles arguments", async () => {
    // audit supports arguments: source, action, actor, since, limit
    const { status, body } = await gql(app, '{ audit(limit:1) { timestamp source } }');
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["data"]).toBeDefined();
    const data = b["data"] as Record<string, unknown>;
    expect(Array.isArray(data["audit"])).toBe(true);
  });

  it("returns error for unknown top-level field", async () => {
    const { status, body } = await gql(app, "{ nonExistentField { foo } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    // data.nonExistentField = null, errors contains message
    expect(b["data"]).toBeDefined();
    const data = b["data"] as Record<string, unknown>;
    expect(data["nonExistentField"]).toBeNull();
    expect(Array.isArray(b["errors"])).toBe(true);
    const errors = b["errors"] as Array<{ message: string }>;
    expect(errors[0]["message"]).toContain("Unknown field 'nonExistentField'");
  });

  it("returns error for unknown sub-field", async () => {
    const { status, body } = await gql(app, "{ spawnQueue { nonExistentSub } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    // data returned with null + errors
    expect(b["data"]).toBeDefined();
    expect(Array.isArray(b["errors"])).toBe(true);
    const errors = b["errors"] as Array<{ message: string }>;
    expect(errors[0]["message"]).toContain("nonExistentSub");
  });

  it("returns no errors on valid query with no unknown fields", async () => {
    const { status, body } = await gql(app, "{ spawnQueue { pending_count active_count utilization_pct } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["errors"]).toBeUndefined();
    expect(b["data"]).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// §4 — Resolver unit tests: control gates (uses config.json in temp dir)
// ---------------------------------------------------------------------------

describe("POST /graphql — control resolver", () => {
  let app: Hono;
  let tmpDir: string;
  let savedTeamDir: string | undefined;

  beforeEach(() => {
    savedTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
    tmpDir = join(tmpdir(), `gql-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_DIR = tmpDir;
    app = makeApp();
  });

  afterEach(() => {
    if (savedTeamDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_DIR = savedTeamDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_DIR;
    }
    delete process.env.AF_API_AUTH_KEY;
    try {
      rmSync(tmpDir, { recursive: true, force: true });
    } catch { /* ignore */ }
  });

  it("returns gates from config.json with default values when config is missing", async () => {
    // No config.json in tmpDir → uses defaults
    const { status, body } = await gql(app, "{ control { gates { key value } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["data"]).toBeDefined();
    expect(b["errors"]).toBeUndefined();
    const data = b["data"] as Record<string, unknown>;
    const control = data["control"] as Record<string, unknown>;
    const gates = control["gates"] as Array<{ key: string; value: string }>;
    expect(Array.isArray(gates)).toBe(true);
    expect(gates.length).toBeGreaterThan(0);
    // default: auto_merge = true → value = "True" (Python str(True) = "True")
    const autoMerge = gates.find((g) => g["key"] === "auto_merge");
    expect(autoMerge).toBeDefined();
    expect(autoMerge!["value"]).toBe("True");
  });

  it("reads gates from config.json overriding defaults", async () => {
    writeFileSync(
      join(tmpDir, "config.json"),
      JSON.stringify({ gates: { auto_merge: false, custom_gate: true } }),
      "utf-8",
    );
    const { status, body } = await gql(app, "{ control { gates { key value } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const control = data["control"] as Record<string, unknown>;
    const gates = control["gates"] as Array<{ key: string; value: string }>;
    const autoMerge = gates.find((g) => g["key"] === "auto_merge");
    expect(autoMerge!["value"]).toBe("False"); // overridden to false, Python str(False) = "False"
    const custom = gates.find((g) => g["key"] === "custom_gate");
    expect(custom).toBeDefined();
    expect(custom!["value"]).toBe("True");
  });

  it("preserves string gates as-is (self_observe_enforcement)", async () => {
    writeFileSync(
      join(tmpDir, "config.json"),
      JSON.stringify({ gates: { self_observe_enforcement: "enforced" } }),
      "utf-8",
    );
    const { status, body } = await gql(app, "{ control { gates { key value } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const control = data["control"] as Record<string, unknown>;
    const gates = control["gates"] as Array<{ key: string; value: string }>;
    const soe = gates.find((g) => g["key"] === "self_observe_enforcement");
    expect(soe!["value"]).toBe("enforced");
  });
});

// ---------------------------------------------------------------------------
// §5 — Resolver unit tests: spawnQueue (uses spawn-queue.json in temp dir)
// ---------------------------------------------------------------------------

describe("POST /graphql — spawnQueue resolver", () => {
  let app: Hono;
  let tmpDir: string;
  let savedTeamDir: string | undefined;

  beforeEach(() => {
    savedTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
    tmpDir = join(tmpdir(), `gql-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_DIR = tmpDir;
    app = makeApp();
  });

  afterEach(() => {
    if (savedTeamDir !== undefined) {
      process.env.AUTONOMOUS_TEAM_DIR = savedTeamDir;
    } else {
      delete process.env.AUTONOMOUS_TEAM_DIR;
    }
    delete process.env.AF_API_AUTH_KEY;
    try {
      rmSync(tmpDir, { recursive: true, force: true });
    } catch { /* ignore */ }
  });

  it("returns zeros when spawn-queue.json is missing", async () => {
    const { status, body } = await gql(app, "{ spawnQueue { pending_count active_count utilization_pct } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["errors"]).toBeUndefined();
    const data = b["data"] as Record<string, unknown>;
    const sq = data["spawnQueue"] as Record<string, unknown>;
    expect(sq["pending_count"]).toBe(0);
    expect(sq["active_count"]).toBe(0);
    expect(sq["utilization_pct"]).toBe(0);
  });

  it("returns correct counts from spawn-queue.json", async () => {
    writeFileSync(
      join(tmpDir, "spawn-queue.json"),
      JSON.stringify({
        pending: [{ id: "a" }, { id: "b" }, { id: "c" }],
        active: [{ id: "x" }, { id: "y" }],
      }),
      "utf-8",
    );
    const { status, body } = await gql(app, "{ spawnQueue { pending_count active_count utilization_pct } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const sq = data["spawnQueue"] as Record<string, unknown>;
    expect(sq["pending_count"]).toBe(3);
    expect(sq["active_count"]).toBe(2);
    // 2/6 total limit = 33%
    expect(sq["utilization_pct"]).toBe(33);
  });
});

// ---------------------------------------------------------------------------
// §6 — Introspection: __schema and __type
// ---------------------------------------------------------------------------

describe("POST /graphql — introspection", () => {
  let app: Hono;

  beforeEach(() => {
    app = makeApp();
  });

  afterEach(() => {
    delete process.env.AF_API_AUTH_KEY;
  });

  it("__schema.types returns type list with names", async () => {
    const { status, body } = await gql(app, "{ __schema { types { name } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["errors"]).toBeUndefined();
    const data = b["data"] as Record<string, unknown>;
    const schema = data["__schema"] as Record<string, unknown>;
    const types = schema["types"] as Array<{ name: string }>;
    expect(Array.isArray(types)).toBe(true);
    const names = types.map((t) => t["name"]);
    expect(names).toContain("Query");
    expect(names).toContain("HealthStatus");
    expect(names).toContain("SpawnQueue");
  });

  it("__schema.types.fields returns field names", async () => {
    const { status, body } = await gql(app, "{ __schema { types { name fields { name } } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const schema = data["__schema"] as Record<string, unknown>;
    const types = schema["types"] as Array<{ name: string; fields: Array<{ name: string }> }>;
    const queryType = types.find((t) => t["name"] === "Query");
    expect(queryType).toBeDefined();
    const fieldNames = queryType!["fields"].map((f) => f["name"]);
    expect(fieldNames).toContain("health");
    expect(fieldNames).toContain("spawnQueue");
    expect(fieldNames).toContain("control");
  });

  it("__type(name:Query) returns Query type fields", async () => {
    const { status, body } = await gql(app, '{ __type(name:"Query") { name fields { name } } }');
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["errors"]).toBeUndefined();
    const data = b["data"] as Record<string, unknown>;
    const type = data["__type"] as Record<string, unknown>;
    expect(type["name"]).toBe("Query");
    const fields = type["fields"] as Array<{ name: string }>;
    const names = fields.map((f) => f["name"]);
    expect(names).toContain("health");
    expect(names).toContain("audit");
    expect(names).toContain("plugins");
  });

  it("__type returns null for unknown type", async () => {
    const { status, body } = await gql(app, '{ __type(name:"NonExistentType") { name } }');
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    expect(data["__type"]).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// §7 — Python-quirk parity assertions
// ---------------------------------------------------------------------------

describe("POST /graphql — Python quirk parity", () => {
  let app: Hono;
  let tmpDir: string;
  let savedTeamDir: string | undefined;
  let savedStateDbPath: string | undefined;

  beforeEach(() => {
    savedTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
    savedStateDbPath = process.env.STATE_DB_PATH;
    tmpDir = join(tmpdir(), `gql-quirk-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_DIR = tmpDir;
    process.env.STATE_DB_PATH = join(tmpDir, "state.db");
    app = makeApp();
  });

  afterEach(() => {
    if (savedTeamDir !== undefined) process.env.AUTONOMOUS_TEAM_DIR = savedTeamDir;
    else delete process.env.AUTONOMOUS_TEAM_DIR;
    if (savedStateDbPath !== undefined) process.env.STATE_DB_PATH = savedStateDbPath;
    else delete process.env.STATE_DB_PATH;
    delete process.env.AF_API_AUTH_KEY;
    try { rmSync(tmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("budget.used is null (Python key mismatch quirk)", async () => {
    const { status, body } = await gql(app, "{ budget { ceiling used remaining model utilization_pct } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const budget = data["budget"] as Record<string, unknown>;
    // QUIRK: Python s.get("used") → key doesn't exist in get_status() → null
    expect(budget["used"]).toBeNull();
    // QUIRK: Python s.get("model") → not in get_status() → null
    expect(budget["model"]).toBeNull();
    // QUIRK: Python s.get("pct_used") → not in get_status() → null
    expect(budget["utilization_pct"]).toBeNull();
  });

  it("cost.total_usd is null (Python key mismatch quirk)", async () => {
    const { status, body } = await gql(app, "{ cost { total_usd } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const cost = data["cost"] as Record<string, unknown>;
    // QUIRK: Python s.get("total_usd") → key is "total_cost_usd" → null
    expect(cost["total_usd"]).toBeNull();
  });

  it("registry.stats.open is null (Python key mismatch quirk)", async () => {
    writeFileSync(join(tmpDir, "registry.json"), JSON.stringify({ discussions: [] }), "utf-8");
    const { status, body } = await gql(app, "{ registry { stats { total open closed velocity_7d } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const registry = data["registry"] as Record<string, unknown>;
    const stats = registry["stats"] as Record<string, unknown>;
    // total: DiscussionRegistry.stats() does have "total" but Python resolver
    // calls stats.get("total") after computing stats which doesn't match our
    // total computation. Actually Python DOES return total from stats() correctly.
    // But open/closed/velocity_7d are wrong keys → null
    expect(stats["open"]).toBeNull();
    expect(stats["closed"]).toBeNull();
    expect(stats["velocity_7d"]).toBeNull();
  });

  it("kpi.cycle_time.p95_hours is null (Python key mismatch quirk)", async () => {
    writeFileSync(join(tmpDir, "registry.json"), JSON.stringify({ discussions: [] }), "utf-8");
    const { status, body } = await gql(app, "{ kpi { velocity { prs_7d prs_30d } cycle_time { median_hours p95_hours } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const kpi = data["kpi"] as Record<string, unknown>;
    const cycletime = kpi["cycle_time"] as Record<string, unknown>;
    // QUIRK: Python cyc.get("p95_hours") → key doesn't exist in compute_pr_cycle_time → null
    expect(cycletime["p95_hours"]).toBeNull();
  });

  it("health.loop.age is null (Python key mismatch quirk)", async () => {
    const { status, body } = await gql(app, "{ health { ok loop { age threshold healthy } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const health = data["health"] as Record<string, unknown>;
    const loop = health["loop"] as Record<string, unknown>;
    // QUIRK: Python resolver accesses loop.get("age_seconds") but check_loop_health
    // returns "age_minutes" → age is ALWAYS null in Python
    expect(loop["age"]).toBeNull();
    // QUIRK: Python resolver accesses loop.get("threshold_seconds") but check_loop_health
    // returns "threshold_minutes" → threshold is ALWAYS null in Python
    expect(loop["threshold"]).toBeNull();
    // healthy IS a real key in check_loop_health() → should be a boolean
    expect(typeof loop["healthy"]).toBe("boolean");
  });
});

// ---------------------------------------------------------------------------
// §8 — Response shape: data + errors together (partial response)
// ---------------------------------------------------------------------------

describe("POST /graphql — partial response (data + errors)", () => {
  let app: Hono;

  beforeEach(() => {
    app = makeApp();
  });

  afterEach(() => {
    delete process.env.AF_API_AUTH_KEY;
  });

  it("returns both data and errors when one field is valid and one is unknown", async () => {
    const { status, body } = await gql(app, "{ spawnQueue { pending_count } unknownField { x } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    // data has both: spawnQueue is resolved, unknownField is null
    expect(b["data"]).toBeDefined();
    const data = b["data"] as Record<string, unknown>;
    expect(data["spawnQueue"]).toBeDefined();
    expect(data["unknownField"]).toBeNull();
    // errors contains the unknown field error
    expect(Array.isArray(b["errors"])).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// §9 — agents resolver (reads .autonomous-team/agents/)
// ---------------------------------------------------------------------------

describe("POST /graphql — agents resolver", () => {
  let app: Hono;
  let tmpDir: string;
  let savedTeamDir: string | undefined;

  beforeEach(() => {
    savedTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
    tmpDir = join(tmpdir(), `gql-agents-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(join(tmpDir, "agents"), { recursive: true });
    process.env.AUTONOMOUS_TEAM_DIR = tmpDir;
    app = makeApp();
  });

  afterEach(() => {
    if (savedTeamDir !== undefined) process.env.AUTONOMOUS_TEAM_DIR = savedTeamDir;
    else delete process.env.AUTONOMOUS_TEAM_DIR;
    delete process.env.AF_API_AUTH_KEY;
    try { rmSync(tmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("returns empty agents list when agents dir is empty", async () => {
    const { status, body } = await gql(app, "{ agents { agents { role description status } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const agentList = data["agents"] as Record<string, unknown>;
    expect(Array.isArray(agentList["agents"])).toBe(true);
    expect((agentList["agents"] as unknown[]).length).toBe(0);
  });

  it("returns agent data from JSON files", async () => {
    writeFileSync(
      join(tmpDir, "agents", "executor.json"),
      JSON.stringify({
        role: "executor",
        description: "Implements code",
        status: "active",
        tools: ["Bash", "Edit"],
        review_pipeline: "code+security",
      }),
      "utf-8",
    );
    const { status, body } = await gql(app, "{ agents { agents { role description status tools review_pipeline } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const agentList = data["agents"] as Record<string, unknown>;
    const agents = agentList["agents"] as Array<Record<string, unknown>>;
    expect(agents.length).toBe(1);
    expect(agents[0]["role"]).toBe("executor");
    expect(agents[0]["description"]).toBe("Implements code");
    expect(agents[0]["status"]).toBe("active");
    expect(Array.isArray(agents[0]["tools"])).toBe(true);
  });

  it("falls back to role=name status=unknown on parse error", async () => {
    writeFileSync(join(tmpDir, "agents", "broken.json"), "not valid json", "utf-8");
    const { status, body } = await gql(app, "{ agents { agents { role status } } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const agentList = data["agents"] as Record<string, unknown>;
    const agents = agentList["agents"] as Array<Record<string, unknown>>;
    expect(agents.length).toBe(1);
    expect(agents[0]["role"]).toBe("broken");
    expect(agents[0]["status"]).toBe("unknown");
  });
});

// ---------------------------------------------------------------------------
// §10 — audit resolver (reads audit.jsonl in temp dir)
// ---------------------------------------------------------------------------

describe("POST /graphql — audit resolver", () => {
  let app: Hono;
  let tmpDir: string;
  let savedTeamDir: string | undefined;

  beforeEach(() => {
    savedTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
    tmpDir = join(tmpdir(), `gql-audit-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_DIR = tmpDir;
    app = makeApp();
  });

  afterEach(() => {
    if (savedTeamDir !== undefined) process.env.AUTONOMOUS_TEAM_DIR = savedTeamDir;
    else delete process.env.AUTONOMOUS_TEAM_DIR;
    delete process.env.AF_API_AUTH_KEY;
    try { rmSync(tmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("returns empty array when audit.jsonl is missing", async () => {
    const { status, body } = await gql(app, "{ audit { timestamp source action actor details } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    expect(Array.isArray(data["audit"])).toBe(true);
    expect((data["audit"] as unknown[]).length).toBe(0);
  });

  it("returns audit entries from audit.jsonl", async () => {
    const entry1 = JSON.stringify({ ts: "2026-01-01T00:00:00Z", source: "test", action: "write", actor: "tester", details: "d1" });
    const entry2 = JSON.stringify({ ts: "2026-01-01T01:00:00Z", source: "api", action: "read", actor: "system", details: "d2" });
    writeFileSync(join(tmpDir, "audit.jsonl"), `${entry1}\n${entry2}\n`, "utf-8");

    const { status, body } = await gql(app, "{ audit { timestamp source action actor details } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const audit = data["audit"] as Array<Record<string, unknown>>;
    expect(audit.length).toBe(2);
    // timestamp maps to "ts" key in the JSONL entries
    expect(audit[0]["source"]).toBe("test");
    expect(audit[0]["action"]).toBe("write");
  });

  it("filters audit entries by source argument", async () => {
    const e1 = JSON.stringify({ ts: "2026-01-01T00:00:00Z", source: "api", action: "a", actor: "x", details: "" });
    const e2 = JSON.stringify({ ts: "2026-01-01T01:00:00Z", source: "blackboard", action: "b", actor: "y", details: "" });
    writeFileSync(join(tmpDir, "audit.jsonl"), `${e1}\n${e2}\n`, "utf-8");

    const { status, body } = await gql(app, '{ audit(source:"api") { source } }');
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const audit = data["audit"] as Array<Record<string, unknown>>;
    expect(audit.length).toBe(1);
    expect(audit[0]["source"]).toBe("api");
  });

  it("limits audit entries by limit argument", async () => {
    const lines = Array.from({ length: 10 }, (_, i) =>
      JSON.stringify({ ts: `2026-01-0${i+1}T00:00:00Z`, source: "s", action: "a", actor: "u", details: "" })
    ).join("\n");
    writeFileSync(join(tmpDir, "audit.jsonl"), lines + "\n", "utf-8");

    const { status, body } = await gql(app, "{ audit(limit:3) { source } }");
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const data = b["data"] as Record<string, unknown>;
    const audit = data["audit"] as unknown[];
    expect(audit.length).toBe(3);
  });
});
