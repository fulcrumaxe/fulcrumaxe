/**
 * spawn/discussion-cache.ts — SQLite cache for GitHub Discussion reads.
 *
 * Faithful 1:1 port of backend/discussion_cache.py (401 LOC).
 *
 * Mirrors:
 *   get_body(number) → getBody(number): string
 *   get(number)      → getDiscussion(number): DiscussionRecord | {}
 *   list_open()      → listOpen(): DiscussionRecord[]
 *   invalidate(number) → invalidate(number): void
 *   get_stats()      → getStats(): CacheStats
 *
 * Cache lives at $AUTONOMOUS_TEAM_STATE_DIR/discussion_cache.db (never inside the repo).
 * TTL: 300 seconds. On GraphQL failure, stale data is returned with a stderr warning
 * rather than throwing — callers see a non-empty result and can continue.
 *
 * CLI (mirroring Python exactly):
 *   bun run src/spawn/discussion-cache.ts get-body <N>
 *   bun run src/spawn/discussion-cache.ts get <N>
 *   bun run src/spawn/discussion-cache.ts list-open
 *   bun run src/spawn/discussion-cache.ts invalidate <N>
 *   bun run src/spawn/discussion-cache.ts stats
 */

import { Database } from "bun:sqlite";
import { mkdirSync, existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Paths — mirrors backend/state_paths.py resolution order
// ---------------------------------------------------------------------------

function stateDir(): string {
  return sharedStateDir();
}

export function dbPath(): string {
  return (
    process.env["DISCUSSION_CACHE_DB_PATH"] ??
    join(stateDir(), "discussion_cache.db")
  );
}

/** Mirrors ensure_state_dir() — idempotent. */
function ensureStateDir(): void {
  const dir = stateDir();
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }
  const blackboard = join(dir, "blackboard");
  if (!existsSync(blackboard)) {
    mkdirSync(blackboard, { recursive: true });
  }
}

// ---------------------------------------------------------------------------
// Repo resolution — mirrors backend/_repo.py resolution order
// ---------------------------------------------------------------------------

function loadRepo(): { owner: string; name: string } {
  const envRepo = process.env["AUTONOMOUS_TEAM_REPO"];
  if (envRepo) {
    const parts = envRepo.split("/", 2);
    return { owner: parts[0] ?? "", name: parts[1] ?? "" };
  }

  // State-dir project.json
  const stateProjectJson = join(stateDir(), "project.json");
  if (existsSync(stateProjectJson)) {
    try {
      const data = JSON.parse(readFileSync(stateProjectJson, "utf-8")) as Record<string, unknown>;
      const repo = data["repo"] as string | undefined;
      if (repo) {
        const parts = repo.split("/", 2);
        return { owner: parts[0] ?? "", name: parts[1] ?? "" };
      }
    } catch { /* ignore */ }
  }

  // Repo-root .autonomous-team/project.json (backwards compat)
  // This file lives at ts-backend/src/spawn/discussion-cache.ts
  // Repo root = 4 levels up
  const repoRoot =
    process.env["AF_REPO_ROOT"] ??
    join(new URL(import.meta.url).pathname, "..", "..", "..", "..");
  const repoProjectJson = join(repoRoot, ".autonomous-team", "project.json");
  if (existsSync(repoProjectJson)) {
    try {
      const data = JSON.parse(readFileSync(repoProjectJson, "utf-8")) as Record<string, unknown>;
      const repo = data["repo"] as string | undefined;
      if (repo) {
        const parts = repo.split("/", 2);
        return { owner: parts[0] ?? "", name: parts[1] ?? "" };
      }
    } catch { /* ignore */ }
  }

  return { owner: "fulcrumaxe", name: "fulcrumaxe" };
}

const _REPO = loadRepo();
const _REPO_OWNER = _REPO.owner;
const _REPO_NAME = _REPO.name;

// ---------------------------------------------------------------------------
// TTL
// ---------------------------------------------------------------------------

const _TTL_SECONDS = 300;

// ---------------------------------------------------------------------------
// Schema DDL — mirrors Python _DDL and _COUNTER_DDL exactly
// ---------------------------------------------------------------------------

const _DDL = `
CREATE TABLE IF NOT EXISTS discussion_cache (
    number     INTEGER PRIMARY KEY,
    body       TEXT    NOT NULL DEFAULT '',
    title      TEXT    NOT NULL DEFAULT '',
    labels     TEXT    NOT NULL DEFAULT '[]',
    updated_at TEXT    NOT NULL DEFAULT '',
    cached_at  TEXT    NOT NULL DEFAULT ''
);
`;

const _COUNTER_DDL = `
CREATE TABLE IF NOT EXISTS discussion_cache_stats (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
`;

// ---------------------------------------------------------------------------
// DB connection factory — mirrors _conn()
// ---------------------------------------------------------------------------

export function openDb(): Database {
  ensureStateDir();
  const db = new Database(dbPath(), { create: true });
  db.run(_DDL);
  db.run("PRAGMA journal_mode=WAL");
  return db;
}

// ---------------------------------------------------------------------------
// Stats counters — mirrors _inc() + get_stats()
// ---------------------------------------------------------------------------

function inc(key: string): void {
  try {
    const db = openDb();
    db.run(_COUNTER_DDL);
    db.run(
      "INSERT INTO discussion_cache_stats(key, value) VALUES(?, 1) " +
        "ON CONFLICT(key) DO UPDATE SET value = value + 1",
      [key]
    );
    db.close();
  } catch {
    /* best-effort — never raises, mirrors Python pass */
  }
}

export interface CacheStats {
  hits: number;
  misses: number;
  total: number;
  hit_ratio: number;
}

export function getStats(): CacheStats {
  try {
    const db = openDb();
    db.run(_COUNTER_DDL);
    const rows = db
      .query<{ key: string; value: number }, []>(
        "SELECT key, value FROM discussion_cache_stats"
      )
      .all();
    db.close();
    const counts: Record<string, number> = {};
    for (const r of rows) {
      counts[r.key] = r.value;
    }
    const hits = counts["hit"] ?? 0;
    const misses = counts["miss"] ?? 0;
    const total = hits + misses;
    return {
      hits,
      misses,
      total,
      hit_ratio: total > 0 ? Math.round((hits / total) * 10000) / 10000 : 0.0,
    };
  } catch {
    return { hits: 0, misses: 0, total: 0, hit_ratio: 0.0 };
  }
}

// ---------------------------------------------------------------------------
// Time helpers — mirrors _now_iso() and _is_fresh()
// ---------------------------------------------------------------------------

export function nowIso(): string {
  // Mirrors Python: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
  // No milliseconds — match Python format exactly
  const d = new Date();
  const pad = (n: number): string => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`
  );
}

export function isFresh(cachedAt: string): boolean {
  if (!cachedAt) return false;
  try {
    const t = new Date(cachedAt).getTime();
    if (isNaN(t)) return false;
    return (Date.now() - t) / 1000 < _TTL_SECONDS;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// GraphQL helpers — mirrors _gh_graphql, _fetch_one, _fetch_all_open
// ---------------------------------------------------------------------------

function ghGraphql(query: string): Record<string, unknown> | null {
  try {
    // Use execFileSync (no shell) to mirror Python's subprocess.run(["gh","api","graphql","-f",f"query={query}"], ...)
    // This prevents shell metacharacter injection via query string or repo owner/name.
    const stdout = execFileSync("gh", ["api", "graphql", "-f", `query=${query}`], {
      timeout: 30_000,
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
    });
    return JSON.parse(stdout) as Record<string, unknown>;
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    process.stderr.write(`discussion_cache: gh graphql error: ${msg}\n`);
    return null;
  }
}

export interface DiscussionRecord {
  number: number;
  title: string;
  body: string;
  labels: string[];
  updated_at: string;
  cached_at?: string;
}

export function fetchOne(number: number): DiscussionRecord | null {
  const query =
    `query { repository(owner:"${_REPO_OWNER}", name:"${_REPO_NAME}") {` +
    ` discussion(number: ${number}) { title body updatedAt labels(first:10) { nodes { name } } } } }`;
  const data = ghGraphql(query);
  if (!data) return null;
  try {
    type GqlResp = {
      data: {
        repository: {
          discussion: Record<string, unknown> | null;
        };
      };
    };
    const disc = (data as GqlResp).data.repository.discussion;
    if (disc === null || disc === undefined) return null;
    const labelsObj = disc["labels"] as
      | { nodes: { name: string }[] }
      | undefined;
    const labelsNodes = labelsObj?.nodes ?? [];
    const labels = labelsNodes.map((n) => n.name);
    return {
      number,
      title: (disc["title"] as string | undefined) ?? "",
      body: (disc["body"] as string | undefined) ?? "",
      labels,
      updated_at: (disc["updatedAt"] as string | undefined) ?? "",
    };
  } catch {
    return null;
  }
}

export function fetchAllOpen(): DiscussionRecord[] | null {
  const query =
    `query { repository(owner:"${_REPO_OWNER}", name:"${_REPO_NAME}") {` +
    " discussions(first:100, states:[OPEN]) { nodes { number title body updatedAt" +
    " labels(first:10) { nodes { name } } } } } }";
  const data = ghGraphql(query);
  if (!data) return null;
  try {
    type GqlResp = {
      data: {
        repository: {
          discussions: { nodes: Array<Record<string, unknown>> };
        };
      };
    };
    const nodes = (data as GqlResp).data.repository.discussions.nodes;
    return nodes.map((disc) => {
      const labelsObj = disc["labels"] as
        | { nodes: { name: string }[] }
        | undefined;
      const labelsNodes = labelsObj?.nodes ?? [];
      const labels = labelsNodes.map((n) => n.name);
      return {
        number: disc["number"] as number,
        title: (disc["title"] as string | undefined) ?? "",
        body: (disc["body"] as string | undefined) ?? "",
        labels,
        updated_at: (disc["updatedAt"] as string | undefined) ?? "",
      };
    });
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Cache row helpers — mirrors _cache_row and _read_row
// ---------------------------------------------------------------------------

interface CacheRow {
  number: number;
  body: string;
  title: string;
  labels: string;
  updated_at: string;
  cached_at: string;
}

export function cacheRow(db: Database, record: DiscussionRecord): void {
  const labelsJson = JSON.stringify(record.labels ?? []);
  db.run(
    "INSERT INTO discussion_cache(number, body, title, labels, updated_at, cached_at) " +
      "VALUES(?,?,?,?,?,?) " +
      "ON CONFLICT(number) DO UPDATE SET " +
      "  body=excluded.body, title=excluded.title, labels=excluded.labels, " +
      "  updated_at=excluded.updated_at, cached_at=excluded.cached_at",
    [
      record.number,
      record.body ?? "",
      record.title ?? "",
      labelsJson,
      record.updated_at ?? "",
      nowIso(),
    ]
  );
}

export function readRow(db: Database, number: number): CacheRow | null {
  const row = db
    .query<CacheRow, [number]>(
      "SELECT * FROM discussion_cache WHERE number = ?"
    )
    .get(number);
  return row ?? null;
}

// ---------------------------------------------------------------------------
// Public API — mirrors Python exactly
// ---------------------------------------------------------------------------

/**
 * Return the discussion body, using cache if TTL fresh.
 *
 * On GraphQL failure: returns stale cached value (if any) with a stderr warning.
 * Returns "" when nothing is available.
 * Mirrors: get_body(number) → str
 */
export function getBody(number: number): string {
  const db = openDb();
  const row = readRow(db, number);
  if (row && isFresh(row.cached_at)) {
    db.close();
    inc("hit");
    return row.body;
  }
  db.close();

  // Cache miss or stale — fetch
  inc("miss");
  const record = fetchOne(number);

  if (record === null) {
    // Graceful degradation: return stale value if available
    if (row) {
      process.stderr.write(
        `[discussion_cache] WARNING: GraphQL failed, returning stale body for #${number}\n`
      );
      return row.body;
    }
    return "";
  }

  const db2 = openDb();
  cacheRow(db2, record);
  db2.close();

  return record.body;
}

/**
 * Return full cached record. Fetches if stale/missing.
 * Mirrors: get(number) → dict
 */
export function getDiscussion(
  number: number
): DiscussionRecord | Record<string, never> {
  const db = openDb();
  const row = readRow(db, number);
  if (row && isFresh(row.cached_at)) {
    db.close();
    inc("hit");
    return {
      number,
      title: row.title,
      body: row.body,
      labels: JSON.parse(row.labels || "[]") as string[],
      updated_at: row.updated_at,
      cached_at: row.cached_at,
    };
  }
  db.close();

  inc("miss");
  const record = fetchOne(number);

  if (record === null) {
    if (row) {
      process.stderr.write(
        `[discussion_cache] WARNING: GraphQL failed, returning stale record for #${number}\n`
      );
      return {
        number,
        title: row.title,
        body: row.body,
        labels: JSON.parse(row.labels || "[]") as string[],
        updated_at: row.updated_at,
        cached_at: row.cached_at,
      };
    }
    return {};
  }

  const db2 = openDb();
  cacheRow(db2, record);
  db2.close();

  return { ...record, cached_at: nowIso() };
}

/**
 * Fetch ALL open discussions in one GraphQL call, update cache, return list.
 * Mirrors: list_open() → list[dict]
 */
export function listOpen(): DiscussionRecord[] {
  const records = fetchAllOpen();

  if (records === null) {
    // Fallback: return whatever we have cached
    process.stderr.write(
      "[discussion_cache] WARNING: GraphQL failed in list_open, returning stale cache\n"
    );
    const db = openDb();
    const rows = db
      .query<CacheRow, []>("SELECT * FROM discussion_cache")
      .all();
    db.close();
    return rows.map((r) => ({
      number: r.number,
      title: r.title,
      body: r.body,
      labels: JSON.parse(r.labels || "[]") as string[],
      updated_at: r.updated_at,
      cached_at: r.cached_at,
    }));
  }

  const db = openDb();
  for (const record of records) {
    cacheRow(db, record);
  }
  db.close();

  return records.map((r) => ({ ...r, cached_at: nowIso() }));
}

/**
 * Clear cache entry so next read forces a fresh GraphQL fetch.
 * Mirrors: invalidate(number) → None
 */
export function invalidate(number: number): void {
  const db = openDb();
  db.run("UPDATE discussion_cache SET cached_at = '' WHERE number = ?", [
    number,
  ]);
  db.close();
}

// ---------------------------------------------------------------------------
// CLI entrypoint — mirrors Python __main__ block exactly
// ---------------------------------------------------------------------------

function usage(): void {
  process.stderr.write(
    "Usage:\n" +
      "  bun run src/spawn/discussion-cache.ts get-body <N>\n" +
      "  bun run src/spawn/discussion-cache.ts get <N>\n" +
      "  bun run src/spawn/discussion-cache.ts list-open\n" +
      "  bun run src/spawn/discussion-cache.ts invalidate <N>\n" +
      "  bun run src/spawn/discussion-cache.ts stats\n"
  );
}

if (import.meta.main) {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    usage();
    process.exit(1);
  }

  const cmd = args[0];

  if (cmd === "get-body") {
    if (args.length < 2) {
      process.stderr.write("get-body requires a discussion number\n");
      process.exit(1);
    }
    const body = getBody(parseInt(args[1]!, 10));
    process.stdout.write(body);
    // exit 1 if nothing returned so callers can detect missing — mirrors Python exactly
    process.exit(body ? 0 : 1);
  } else if (cmd === "get") {
    if (args.length < 2) {
      process.stderr.write("get requires a discussion number\n");
      process.exit(1);
    }
    const record = getDiscussion(parseInt(args[1]!, 10));
    console.log(JSON.stringify(record, null, 2));
  } else if (cmd === "list-open") {
    const records = listOpen();
    console.log(JSON.stringify(records, null, 2));
  } else if (cmd === "invalidate") {
    if (args.length < 2) {
      process.stderr.write("invalidate requires a discussion number\n");
      process.exit(1);
    }
    invalidate(parseInt(args[1]!, 10));
    console.log(`invalidated #${args[1]}`);
  } else if (cmd === "stats") {
    console.log(JSON.stringify(getStats(), null, 2));
  } else {
    process.stderr.write(`Unknown command: ${cmd}\n`);
    usage();
    process.exit(1);
  }
}
