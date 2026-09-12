/**
 * config/project-repo-slug.ts — resolve a *named project's* GitHub repo
 * slug and checkout root, mirroring backend/project_repo_slug.py's
 * resolve_project_repo_slug() (D#2540, porting D#2518/D#2327's contract).
 *
 * Why this exists
 * ----------------
 * backend/rpc/stats_dora.py (and, before it, stats_weekly_velocity.py /
 * stats_cost_per_outcome.py) each need the same answer: given a project
 * *name*, what GitHub repo does it talk about, and where does its own
 * .autonomous-team/ live? Two copies of that decision is exactly the shape
 * D#2327 existed to stop on the Python side; this file is the TS side's
 * single copy so ts-backend/src/rpc/stats-dora.ts does not grow its own.
 *
 * Scope, precisely — what this mirrors and what it does not
 * -----------------------------------------------------------
 * Python's resolver is backend/state_paths.py's for_project(), which has
 * FOUR resolution steps for state_dir: (0) this *server process's own*
 * STATE_DIR when its dashboard-runtime.json declares the requested
 * project, (1) ~/.<name>-state/dashboard-runtime.json, (2)
 * ~/.<name>-state/project.json, (3) the conventional ~/.<name>-state/
 * itself. Step 0 exists to find an adopter whose state dir sits outside
 * $HOME without a marker file planted there — it depends on Python's own
 * server process ever having been configured to serve that project
 * (backend/state_paths.py's AUTONOMOUS_TEAM_STATE_DIR machinery), which
 * ts-backend has no runtime equivalent of (config/state-paths.ts resolves
 * only *this* checkout's own state dir, never a per-project one). This
 * file ports steps 1-3 — the home-anchored convention every project
 * resolves through in the common case, and the one the parity fixture
 * below (and backend/tests/test_rpc_project_scope.py's
 * `_write_project_layout()`) actually exercises. A project whose state dir
 * was relocated AND is only discoverable via step 0 is out of scope here;
 * that is a real, narrower gap than the one this Discussion closes, not a
 * silent one — recorded in ts-backend/PARITY-CAVEATS.md.
 *
 * Same charset GitHub itself allows in an owner or repo name — matches
 * backend/project_repo_slug.py's _SLUG_RE exactly (CWE-88 guard: both
 * callers hand this value straight to a `gh --repo` argv, so an
 * unvalidated slug must never reach it).
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const SLUG_RE = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;

// Same charset backend/state_paths.py's _PROJECT_NAME_RE validates a
// project name against before building any path from it (CWE-22 guard).
const PROJECT_NAME_RE = /^[A-Za-z0-9._-]+$/;

/**
 * carries UnresolvableProjectError's rpc_code — Python's own class (in
 * backend/rpc_project_scope.py) carries `rpc_code = -32001` so both JSON-RPC
 * dispatch sites surface it as a non-null error rather than a 500 or a
 * silent empty body; ts-backend/src/routes/rpc.ts reads the same
 * `rpc_code` property off a thrown Error (see rpc/loop.ts's
 * `_rpc_invalid_params` for the established convention).
 */
export class UnresolvableProjectError extends Error {
  rpc_code = -32001;

  constructor(message: string) {
    super(message);
    this.name = "UnresolvableProjectError";
  }
}

interface ResolvedProjectPaths {
  stateDir: string;
  repo: string | null;
}

function readJsonRecord(path: string): Record<string, unknown> | null {
  if (!existsSync(path)) return null;
  try {
    const data = JSON.parse(readFileSync(path, "utf-8"));
    return typeof data === "object" && data !== null && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/**
 * Home directory to resolve `~/.<project>-state/` against.
 *
 * `AF_PROJECT_HOME_OVERRIDE` is a test-only escape hatch, matching this
 * codebase's convention for path resolvers (RPC_TOKEN_DIR_OVERRIDE in
 * routes/rpc.ts, CODE_PLANE_REMOTE_OVERRIDE in repo-resolve.sh): Bun's
 * `os.homedir()` resolves once from the OS (getpwuid), not from
 * `process.env.HOME` — mutating `process.env.HOME` at test time (the
 * mechanism Python's test suite uses via `monkeypatch.setattr(Path,
 * "home", ...)`) has no effect on Bun's `homedir()`, so a test-only env
 * var is the only way to point this resolver at a fixture directory
 * without touching the real $HOME.
 */
function projectsHome(): string {
  return process.env["AF_PROJECT_HOME_OVERRIDE"] ?? homedir();
}

/**
 * Resolve steps 1-3 of backend/state_paths.py's for_project() for *name*.
 * Never throws — any I/O or parse failure is treated the same as "not
 * found" (Python's for_project() only guards the two JSON reads with
 * `except (OSError, ValueError): pass`; this mirrors that fail-soft shape).
 */
function resolveProjectPaths(name: string): ResolvedProjectPaths | null {
  if (!PROJECT_NAME_RE.test(name)) return null;

  const conventionalStateDir = join(projectsHome(), `.${name}-state`);
  let stateDir = conventionalStateDir;
  let repo: string | null = null;

  const runtimeData = readJsonRecord(join(conventionalStateDir, "dashboard-runtime.json"));
  if (runtimeData) {
    const sd = nonEmptyString(runtimeData["state_dir"]);
    if (sd) stateDir = sd;
    repo = nonEmptyString(runtimeData["repo"]) ?? nonEmptyString(runtimeData["project_repo"]);
  }

  if (repo === null) {
    const projectData = readJsonRecord(join(conventionalStateDir, "project.json"));
    if (projectData) {
      repo = nonEmptyString(projectData["repo"]);
    }
  }

  return { stateDir, repo };
}

/**
 * Return the GitHub `owner/name` slug for *project*, or `null`.
 *
 * `null` means either "no project was named" (the caller's own default
 * applies) or "this project declares no repo" — which also covers a `repo`
 * field that fails the slug charset check above; a malformed value is
 * treated exactly like a missing one, never returned as-is. Callers must
 * tell those two `null` cases apart themselves by checking `project` first
 * — a *named* project that resolves to nothing must be declined
 * (UnresolvableProjectError), never served the serving checkout's own repo.
 *
 * Mirrors backend/project_repo_slug.py:38's resolve_project_repo_slug().
 * Never raises.
 */
export function resolveProjectRepoSlug(project: string | null | undefined): string | null {
  if (!project) return null;
  try {
    const paths = resolveProjectPaths(project);
    if (paths?.repo && SLUG_RE.test(paths.repo)) {
      return paths.repo;
    }
  } catch {
    /* never throw — same fail-soft contract as the Python resolver */
  }
  return null;
}

/**
 * The named project's own checkout root — mirrors
 * `state_dir.parent / project` in backend/rpc/stats_dora.py's handle(),
 * the same convention Python's kpi.history/kpi.cycle_time handlers already
 * use for per-project scoping. Used to scope local file reads (releases
 * dir, registry.json) to *this* project's own `.autonomous-team/` instead
 * of the serving checkout's.
 *
 * Returns `null` under the same conditions resolveProjectRepoSlug() does —
 * no project named, or an invalid project name. Note this does NOT require
 * a resolved repo: an unresolvable repo is a decline made by the caller
 * (stats-dora.ts), not by this accessor.
 */
export function resolveProjectRoot(project: string | null | undefined): string | null {
  if (!project) return null;
  try {
    const paths = resolveProjectPaths(project);
    if (!paths) return null;
    return join(dirname(paths.stateDir), project);
  } catch {
    return null;
  }
}
