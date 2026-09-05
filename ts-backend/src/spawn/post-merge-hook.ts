/**
 * spawn/post-merge-hook.ts — Mirrors scripts/post-merge-hook.sh logic.
 *
 * Runs after EVERY PR merge to enforce coordination discipline:
 * records agent-feed event, syncs wiki, closes linked Discussions,
 * posts cost comments, writes completion blocks, updates worktree
 * registry, scores quality, records lessons, logs to team-log, auto-pulls
 * main, queues browser tours, emits 8 stats metrics, runs release-manager
 * record, fires interactive-metrics-tick, hourly-stats-refresh, reaps
 * zombie chromes, drains pending PRs, runs post-merge.d/ drop-ins, and
 * sweeps stale loop-run logs.
 *
 * # Steps (in order, mirrors bash step sequence exactly)
 *   agent_feed → wiki_sync → discussion_close → cost_comment
 *   → completion_block → worktree_merge_registry → quality_score
 *   → lessons_record → team_log → tmux_reload_flag → auto_pull
 *   → browser_tour_queue → stats_metrics → release_manager_queue
 *   → interactive_metrics_tick → hourly_stats_refresh → reap_chromes
 *   → drain_pending_prs → [post-merge.d/ hooks] → sweep_loop_runs
 *
 * # CLI usage (matches bash CLI exactly)
 *   bun run src/spawn/post-merge-hook.ts \
 *     --pr <N> \
 *     [--discussion <N>] \
 *     [--event-id <id>] \
 *     [--resume]
 *
 * # Side effects NOT covered by parity tests (require external systems):
 *   - bash scripts/agent-feed-append.sh ...            (JSONL disk append)
 *   - bash scripts/post-merge-wiki.sh                  (git push to gh-pages wiki)
 *   - gh api graphql closeDiscussion / updateDiscussion (GitHub GraphQL mutations)
 *   - gh api graphql addDiscussionComment              (GitHub GraphQL mutations)
 *   - python3 backend/cost_tracker.py by-discussion    (discussion spend lookup)
 *   - python3 backend/cost_formatter.py                (spend markdown formatter)
 *   - python3 backend/quality_scorer.py score          (PR quality scoring)
 *   - python3 backend/lessons.py / LessonsStore        (lessons DB write)
 *   - bash scripts/rotate-team-log.sh comment "..."    (GitHub issue comment)
 *   - gh pr view <N> --json files                      (CLAUDE.md change detection)
 *   - git -C <repoRoot> checkout main / pull           (auto-pull main)
 *   - gh pr view <N> --json files (for browser-tour)   (dashboard file detection)
 *   - python3 backend/budget.py status                 (budget spend lookup)
 *   - gh pr view / gh pr list / gh api repos/.../timeline (stats data gathering)
 *   - python3 backend/release_manager.py record        (release artifact)
 *   - bash scripts/interactive-metrics-tick.sh         (loop-metrics row)
 *   - bash scripts/spawn-hourly-stats.sh               (hourly stats)
 *   - bash scripts/reap-zombie-chromes.sh              (chrome reaper)
 *   - bash scripts/drain-pending-prs.sh                (pending-prs drain)
 *   - bash scripts/hooks/post-merge.d/*.sh             (feature drop-ins)
 *   - bash scripts/sweep-loop-runs.sh                  (30-day loop-run retention)
 *   - python3 backend/control_plane.py get ...         (gate reads)
 *   - python3 / worktree-registry.sh mark-status       (worktree status)
 *
 * These are reproduced faithfully in the implementation (correct ARGV arrays,
 * no shell-string interpolation) but are mocked/skipped in the parity test
 * which focuses on the deterministic store-mutating step: stats_metrics
 * (record_many → stats.duckdb) and exit behaviour.
 */

import { spawnSync } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { DuckDBInstance } from "@duckdb/node-api";
import { resolveCodeRepo, resolveRepo } from "../config/repo.js";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Path resolution (mirrors bash REPO_ROOT / SCRIPT_DIR logic)
// ---------------------------------------------------------------------------

function repoRoot(): string {
  if (process.env["AF_REPO_ROOT"]) return process.env["AF_REPO_ROOT"]!;
  // This file: ts-backend/src/spawn/post-merge-hook.ts
  // → ts-backend/src/spawn/ → ts-backend/src/ → ts-backend/ → repo root
  const thisFile = new URL(import.meta.url).pathname;
  return join(thisFile, "..", "..", "..", "..");
}

function scriptsDir(): string {
  return join(repoRoot(), "scripts");
}

function stateDir(): string {
  return sharedStateDir();
}

function autonomousTeamDir(): string {
  return process.env["AUTONOMOUS_TEAM_DIR"] ?? join(repoRoot(), ".autonomous-team");
}

// ---------------------------------------------------------------------------
// CLI args parser (mirrors bash getopts-style while loop)
// ---------------------------------------------------------------------------

export interface PostMergeHookArgs {
  pr: string;
  discussion?: string | null;
  eventId?: string | null;
  resume: boolean;
}

export function parseArgs(argv: string[]): PostMergeHookArgs {
  let pr = "";
  let discussion: string | null = null;
  let eventId: string | null = null;
  let resume = false;

  let i = 0;
  while (i < argv.length) {
    const arg = argv[i]!;
    switch (arg) {
      case "--pr":         pr = argv[++i]!; break;
      case "--discussion": discussion = argv[++i] ?? null; break;
      case "--event-id":   eventId = argv[++i] ?? null; break;
      case "--resume":     resume = true; break;
      default:
        process.stderr.write(`Unknown argument: ${arg}\n`);
        process.stderr.write("Usage: post-merge-hook.ts --pr <N> [--discussion <N>]\n");
        process.exit(1);
    }
    i++;
  }

  if (!pr) {
    process.stderr.write("Error: --pr is required\n");
    process.exit(1);
  }

  return { pr, discussion, eventId, resume };
}

// ---------------------------------------------------------------------------
// Idempotency step tracker — mirrors hook_event_{has,mark}_step in bash
// ---------------------------------------------------------------------------

export class StepTracker {
  private done = new Set<string>();

  has(step: string): boolean {
    return this.done.has(step);
  }

  mark(step: string): void {
    this.done.add(step);
  }

  completed(): string[] {
    return Array.from(this.done);
  }
}

// ---------------------------------------------------------------------------
// Shell command runner — non-fatal, argv arrays only (no shell-string eval)
// ---------------------------------------------------------------------------

function runShellNonFatal(
  cmd: string[],
  label: string,
  opts?: { env?: Record<string, string>; timeoutMs?: number }
): { exitCode: number; stdout: string; stderr: string } {
  try {
    const result = spawnSync(cmd[0]!, cmd.slice(1), {
      env: { ...process.env, ...(opts?.env ?? {}) },
      cwd: repoRoot(),
      timeout: opts?.timeoutMs ?? 30_000,
      encoding: "utf8",
    });
    return {
      exitCode: result.status ?? 0,
      stdout: (result.stdout as string) ?? "",
      stderr: (result.stderr as string) ?? "",
    };
  } catch (e) {
    process.stderr.write(`[post-merge-hook] ${label} failed: ${String(e)}\n`);
    return { exitCode: 1, stdout: "", stderr: String(e) };
  }
}

function runPythonNonFatal(
  args: string[],
  label: string,
  opts?: { env?: Record<string, string> }
): { exitCode: number; stdout: string; stderr: string } {
  return runShellNonFatal(["python3", ...args], label, opts);
}

// ---------------------------------------------------------------------------
// Stats DB path — mirrors Python stats_writer._db_path() priority order
// ---------------------------------------------------------------------------

function statsDbPath(overrideStatsDb?: string): string {
  if (overrideStatsDb) return overrideStatsDb;
  const envPath = process.env["STATS_DB_PATH"];
  if (envPath) return envPath;
  const sd = stateDir();
  return join(sd, "stats.duckdb");
}

// ---------------------------------------------------------------------------
// Stats writer — mirrors Python stats_writer.record_many()
// ---------------------------------------------------------------------------

export interface MetricRow {
  metric: string;
  value: number;
  unit: string;
  tags?: Record<string, string>;
  source?: string;
}

export async function recordMetrics(
  rows: MetricRow[],
  overrideStatsDb?: string
): Promise<void> {
  const dbPath = statsDbPath(overrideStatsDb);
  mkdirSync(dirname(dbPath), { recursive: true });

  let instance: InstanceType<typeof DuckDBInstance> | null = null;
  let conn: Awaited<ReturnType<InstanceType<typeof DuckDBInstance>["connect"]>> | null = null;

  try {
    instance = await DuckDBInstance.create(dbPath, { access_mode: "READ_WRITE" });
    conn = await instance.connect();

    // Ensure schema (mirrors Python _ensure_schema)
    await conn.run(`
      CREATE TABLE IF NOT EXISTS metric_event (
        ts      TIMESTAMP NOT NULL,
        metric  TEXT      NOT NULL,
        tags    JSON,
        value   DOUBLE    NOT NULL,
        unit    TEXT      NOT NULL,
        source  TEXT,
        PRIMARY KEY (ts, metric, tags)
      )
    `);
    await conn.run(
      "CREATE INDEX IF NOT EXISTS idx_metric_time ON metric_event(metric, ts)"
    );

    const nowTs = new Date()
      .toISOString()
      .replace("T", " ")
      .replace("Z", "")
      .slice(0, 23); // "YYYY-MM-DD HH:MM:SS.mmm"

    for (const row of rows) {
      const tagsJson = JSON.stringify(row.tags ?? {});
      try {
        await conn.run(
          `INSERT OR IGNORE INTO metric_event (ts, metric, tags, value, unit, source)
           VALUES (CAST(? AS TIMESTAMP), ?, CAST(? AS JSON), ?, ?, ?)`,
          [nowTs, row.metric, tagsJson, row.value, row.unit, row.source ?? null]
        );
      } catch {
        // Primary key conflict is expected (idempotent); other errors logged
        process.stderr.write(
          `[post-merge-hook] Warning: metric insert failed for ${row.metric} (non-fatal)\n`
        );
      }
    }
  } catch (e) {
    process.stderr.write(
      `[post-merge-hook] Warning: stats DB open failed: ${String(e)} (non-fatal)\n`
    );
  } finally {
    try { conn?.closeSync(); } catch { /* ignore */ }
    try { instance?.closeSync(); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// ISO date parser (mirrors Python _parse_iso)
// ---------------------------------------------------------------------------

export function parseIso(s: string): Date | null {
  if (!s.trim()) return null;
  try {
    const d = new Date(s.replace("Z", "+00:00"));
    return isNaN(d.getTime()) ? null : d;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Step 0: agent_feed — JSONL merge event (mirrors bash step agent_feed)
// ---------------------------------------------------------------------------

function stepAgentFeed(args: PostMergeHookArgs, discussion: string | null): void {
  let feedMsg = `merged PR #${args.pr}`;
  if (discussion) feedMsg += ` (closes D#${discussion})`;
  feedMsg = feedMsg.slice(0, 280);

  const feedArgs = [
    "bash", join(scriptsDir(), "agent-feed-append.sh"),
    "--role", "merge",
    "--event-type", "merge",
    "--message", feedMsg,
    "--verdict", "done",
    "--pr", args.pr,
  ];
  if (discussion) feedArgs.push("--discussion", discussion);

  runShellNonFatal(feedArgs, "agent-feed-append");
}

// ---------------------------------------------------------------------------
// Step 1: wiki_sync — run post-merge-wiki.sh
// ---------------------------------------------------------------------------

function stepWikiSync(pr: string): void {
  process.stdout.write(`[post-merge-hook] Running wiki sync for PR #${pr}\n`);
  runShellNonFatal(
    ["bash", join(scriptsDir(), "post-merge-wiki.sh")],
    "wiki_sync",
    { timeoutMs: 60_000 }
  );
}

// ---------------------------------------------------------------------------
// Step 2: discussion_close — close linked discussions (via gh graphql)
// Note: complex umbrella logic fully ported but relies on external GitHub API.
// Parity test does not exercise this step.
// ---------------------------------------------------------------------------

function stepDiscussionClose(discussions: string[], pr: string): void {
  // Two planes: stepDiscussionClose — PR list; discussion id and body.
  //
  // The code-plane value is `codeRepo`, and the name is
  // load-bearing: `repo` still means the Discussion plane
  // elsewhere in this file, and the embedded Python below
  // receives its slug by argv position, so the two have to
  // be distinguishable by more than context.
  const codeRepo = resolveCodeRepo();
  const repo = resolveRepo();
  const repoOwner = repo.split("/")[0]!;
  const repoName = repo.split("/")[1]!;

  for (const disc of discussions) {
    // Fetch Discussion data
    const dataResult = runShellNonFatal(
      [
        "gh", "api", "graphql",
        "-f",
        `query=query { repository(owner:"${repoOwner}", name:"${repoName}") { discussion(number:${disc}) { id body } } }`,
        "--jq", ".data.repository.discussion",
      ],
      `discussion_close fetch #${disc}`
    );

    let discId = "";
    let currentBody = "";
    if (dataResult.exitCode === 0 && dataResult.stdout.trim()) {
      try {
        const parsed = JSON.parse(dataResult.stdout.trim()) as { id?: string; body?: string };
        discId = parsed?.id ?? "";
        currentBody = parsed?.body ?? "";
      } catch { /* non-fatal */ }
    }

    // Umbrella detection
    const umbrellaMarker = currentBody.match(/UMBRELLA:\d+-PR/)?.[0] ?? "";
    const prSections = (currentBody.match(/^### PR-[a-z]:/gm) ?? []).length;
    let isUmbrella = !!(umbrellaMarker || prSections > 1);

    if (isUmbrella) {
      process.stdout.write(
        `[post-merge-hook] Discussion #${disc} is an umbrella — checking progress\n`
      );

      // Planned PRs
      const plannedLabels = (currentBody.match(/^### PR-[a-z]:/gm) ?? [])
        .map((s) => s.replace(/^### /, "").replace(/:$/, ""));
      let plannedCount = plannedLabels.length;
      if (plannedCount === 0 && umbrellaMarker) {
        const numMatch = umbrellaMarker.match(/(\d+)/);
        plannedCount = numMatch ? parseInt(numMatch[1]!, 10) : 0;
      }

      // Merged count
      const mergedResult = runShellNonFatal(
        [
          "gh", "pr", "list", "--repo", codeRepo,
          "--state", "merged", "--json", "number,body",
          "--jq", `[.[] | select(.body | test("#${disc}([^0-9]|$)"))] | length`,
        ],
        `umbrella merged count #${disc}`
      );
      const mergedCount = parseInt(mergedResult.stdout.trim() || "0", 10) || 0;

      const remainingCount = Math.max(0, plannedCount - mergedCount);

      if (remainingCount <= 0) {
        // Fall through to close
        isUmbrella = false;
      } else {
        // Post progress comment
        let progressMsg: string;
        if (plannedLabels.length > 0) {
          // Determine remaining labels
          const mergedBodiesResult = runShellNonFatal(
            [
              "gh", "pr", "list", "--repo", codeRepo,
              "--state", "merged", "--json", "number,body,title",
              "--jq",
              `[.[] | select(.body | test("#${disc}([^0-9]|$)"))] | map(.body + " " + .title) | join(" ")`,
            ],
            `umbrella merged bodies #${disc}`
          );
          const mergedBodies = mergedBodiesResult.stdout.toLowerCase();
          const remaining = plannedLabels.filter(
            (lbl) => !new RegExp(`\\b${lbl}\\b`, "i").test(mergedBodies)
          );
          progressMsg = remaining.length > 0
            ? `PR #${pr} merged. ${remainingCount} of ${plannedCount} PRs remaining: ${remaining.join(" ")}.`
            : `PR #${pr} merged. Approximately ${remainingCount} of ${plannedCount} PRs remaining.`;
        } else {
          progressMsg = `PR #${pr} merged. Approximately ${remainingCount} of ${plannedCount} PRs remaining.`;
        }

        process.stdout.write(
          `[post-merge-hook] Posting umbrella progress comment on Discussion #${disc}\n`
        );
        if (discId) {
          runShellNonFatal(
            [
              "gh", "api", "graphql",
              "-f",
              `query=mutation { addDiscussionComment(input:{discussionId:"${discId}", body:${JSON.stringify(progressMsg)}}) { comment { id } } }`,
            ],
            `umbrella progress comment #${disc}`
          );
          process.stdout.write(
            `[post-merge-hook] Progress comment posted: ${progressMsg}\n`
          );
        }
      }
    }

    // Single-PR or last-umbrella-PR: close the Discussion
    if (!isUmbrella) {
      process.stdout.write(`[post-merge-hook] Closing Discussion #${disc}\n`);
      if (!discId) {
        process.stderr.write(
          `[post-merge-hook] Warning: could not resolve Discussion #${disc} id — skipping close\n`
        );
        continue;
      }

      runShellNonFatal(
        [
          "gh", "api", "graphql",
          "-f",
          `query=mutation { closeDiscussion(input:{discussionId:"${discId}", reason:RESOLVED}) { discussion { id closed } } }`,
        ],
        `closeDiscussion #${disc}`
      );
      process.stdout.write(`[post-merge-hook] Discussion #${disc} closed (id=${discId})\n`);

      // Update STATUS to DONE
      process.stdout.write(
        `[post-merge-hook] Updating Discussion #${disc} STATUS to DONE\n`
      );
      if (currentBody) {
        const updatedBody = currentBody.replace(/STATUS:[A-Z_]*/g, "STATUS:DONE");
        if (updatedBody !== currentBody) {
          runShellNonFatal(
            [
              "gh", "api", "graphql",
              "-f",
              `query=mutation { updateDiscussion(input:{discussionId:"${discId}", body:${JSON.stringify(updatedBody)}}) { discussion { id } } }`,
            ],
            `updateDiscussion STATUS #${disc}`
          );
          process.stdout.write(
            `[post-merge-hook] Discussion #${disc} STATUS updated to DONE\n`
          );
        } else {
          process.stdout.write(
            `[post-merge-hook] Discussion #${disc} body has no STATUS line to update\n`
          );
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Step 2a: cost_comment — post per-Discussion spend
// ---------------------------------------------------------------------------

function stepCostComment(discussion: string, discId: string): void {
  const root = repoRoot();
  const costResult = runPythonNonFatal(
    [join(root, "backend", "cost_tracker.py"), "by-discussion", "--discussion", discussion, "--json"],
    "cost_tracker"
  );

  if (!costResult.stdout.trim() || costResult.stdout.trim() === "null") {
    process.stdout.write(
      `[post-merge-hook] cost_comment: no spend record for Discussion #${discussion} — skipping\n`
    );
    return;
  }

  let costTotal = 0;
  try {
    const d = JSON.parse(costResult.stdout.trim()) as { total_cost_usd?: number };
    costTotal = d?.total_cost_usd ?? 0;
  } catch { /* non-fatal */ }

  if (costTotal <= 0) {
    process.stdout.write(
      `[post-merge-hook] cost_comment: total_cost=0 for Discussion #${discussion} — skipping comment\n`
    );
    return;
  }

  // cost_formatter reads from stdin in bash; pass via env to avoid stdin pipe
  const formatterResult = runPythonNonFatal(
    ["-c",
     `import sys, json\ndata = json.loads(sys.argv[1])\nprint("Cost summary: total $" + str(data.get("total_cost_usd", 0)))\n`,
     costResult.stdout.trim(),
    ],
    "cost_formatter_inline"
  );

  // Try the real cost_formatter.py if available
  const fmtResult = runShellNonFatal(
    ["python3", join(root, "backend", "cost_formatter.py")],
    "cost_formatter",
    { env: { COST_JSON: costResult.stdout.trim() } }
  );
  const costMd = (fmtResult.exitCode === 0 ? fmtResult.stdout : formatterResult.stdout).trim();

  if (!costMd) {
    process.stderr.write(
      `[post-merge-hook] Warning: cost_formatter returned empty output for Discussion #${discussion} (non-fatal)\n`
    );
    return;
  }

  runShellNonFatal(
    [
      "gh", "api", "graphql",
      "-f",
      `query=mutation { addDiscussionComment(input:{discussionId:"${discId}", body:${JSON.stringify(costMd)}}) { comment { id } } }`,
    ],
    `cost_comment #${discussion}`
  );
  process.stdout.write(
    `[post-merge-hook] Cost comment posted to Discussion #${discussion} (total: $${costTotal})\n`
  );
}

// ---------------------------------------------------------------------------
// Step 2b: completion_block — write actual_hours to Discussion body
// ---------------------------------------------------------------------------

function stepCompletionBlock(
  discussion: string,
  discId: string,
  currentBody: string,
  pr: string
): void {
  // Two planes: stepCompletionBlock — PR list and view; discussion createdAt.
  //
  // The code-plane value is `codeRepo`, and the name is
  // load-bearing: `repo` still means the Discussion plane
  // elsewhere in this file, and the embedded Python below
  // receives its slug by argv position, so the two have to
  // be distinguishable by more than context.
  const codeRepo = resolveCodeRepo();
  const repo = resolveRepo();
  const repoOwner = repo.split("/")[0]!;
  const repoName = repo.split("/")[1]!;

  // Umbrella check — skip if not all planned PRs merged yet
  const umbrellaMarker = currentBody.match(/UMBRELLA:\d+-PR/)?.[0] ?? "";
  const prSections = (currentBody.match(/^### PR-[a-z]:/gm) ?? []).length;
  const isUmbrella = !!(umbrellaMarker || prSections > 1);

  if (isUmbrella) {
    let plannedCount = prSections > 1 ? prSections : 0;
    if (plannedCount === 0 && umbrellaMarker) {
      const numMatch = umbrellaMarker.match(/(\d+)/);
      plannedCount = numMatch ? parseInt(numMatch[1]!, 10) : 0;
    }
    const mergedResult = runShellNonFatal(
      [
        "gh", "pr", "list", "--repo", codeRepo,
        "--state", "merged", "--json", "number,body",
        "--jq", `[.[] | select(.body | test("#${discussion}([^0-9]|$)"))] | length`,
      ],
      `completion_block umbrella merged count #${discussion}`
    );
    const mergedCount = parseInt(mergedResult.stdout.trim() || "0", 10) || 0;
    if (mergedCount < plannedCount) {
      process.stdout.write(
        `[post-merge-hook] Umbrella Discussion #${discussion}: ${mergedCount}/${plannedCount} PRs merged — skipping completion_block until last PR\n`
      );
      return;
    }
  }

  const discCreatedResult = runShellNonFatal(
    [
      "gh", "api", "graphql",
      "-f",
      `query=query { repository(owner:"${repoOwner}", name:"${repoName}") { discussion(number:${discussion}) { createdAt } } }`,
      "--jq", ".data.repository.discussion.createdAt",
    ],
    `completion_block disc created_at #${discussion}`
  );
  const discCreatedAt = discCreatedResult.stdout.trim();

  const prMergedResult = runShellNonFatal(
    ["gh", "pr", "view", pr, "--repo", codeRepo, "--json", "mergedAt", "--jq", ".mergedAt"],
    `completion_block pr merged_at #${pr}`
  );
  const prMergedAt = prMergedResult.stdout.trim();

  if (!discCreatedAt || !prMergedAt) {
    process.stderr.write(
      `[post-merge-hook] Warning: missing timestamps for COMPLETION block on Discussion #${discussion} (non-fatal)\n`
    );
    return;
  }

  const createdDt = parseIso(discCreatedAt);
  const mergedDt = parseIso(prMergedAt);
  if (!createdDt || !mergedDt) {
    process.stderr.write(
      `[post-merge-hook] Warning: could not compute actual_hours for Discussion #${discussion} (non-fatal)\n`
    );
    return;
  }

  const actualHours = Math.round(((mergedDt.getTime() - createdDt.getTime()) / 3600000) * 100) / 100;
  const completionBlock =
    `\n<!-- COMPLETION -->\nactual_hours: ${actualHours}\nmerged_at: ${prMergedAt}\nmerged_pr: ${pr}\n<!-- /COMPLETION -->`;

  // Remove existing COMPLETION block, append fresh one (mirrors Python re.sub)
  const cleaned = currentBody.replace(/\n?<!-- COMPLETION -->[\s\S]*?<!-- \/COMPLETION -->/g, "");
  const updatedBody = cleaned.trimEnd() + completionBlock;

  runShellNonFatal(
    [
      "gh", "api", "graphql",
      "-f",
      `query=mutation { updateDiscussion(input:{discussionId:"${discId}", body:${JSON.stringify(updatedBody)}}) { discussion { id } } }`,
    ],
    `completion_block updateDiscussion #${discussion}`
  );
  process.stdout.write(
    `[post-merge-hook] Discussion #${discussion} COMPLETION block written (actual_hours=${actualHours})\n`
  );
}

// ---------------------------------------------------------------------------
// Step 2b: worktree_merge_registry — mark worktree as merged
// ---------------------------------------------------------------------------

function stepWorktreeMergeRegistry(pr: string): void {
  const registryFile = join(autonomousTeamDir(), "worktrees.json");
  if (!existsSync(registryFile)) {
    process.stdout.write(
      "[post-merge-hook] worktree_merge_registry: no worktrees.json — skipping\n"
    );
    return;
  }

  let worktreeId = "";
  try {
    const data = JSON.parse(readFileSync(registryFile, "utf8")) as Array<{
      pr?: number; status?: string; worktree_id?: string;
    }>;
    const prNum = parseInt(pr, 10);
    const entry = data.find(
      (e) => e.pr === prNum && ["active", "committed", "pushed"].includes(e.status ?? "")
    );
    worktreeId = entry?.worktree_id ?? "";
  } catch { /* non-fatal */ }

  if (worktreeId) {
    runShellNonFatal(
      [
        "bash", join(scriptsDir(), "lib", "worktree-registry.sh"),
        "mark-status", worktreeId, "merged",
      ],
      `worktree_merge_registry mark ${worktreeId}`
    );
    process.stdout.write(
      `[post-merge-hook] Marked worktree ${worktreeId} as merged (PR #${pr})\n`
    );
  }
}

// ---------------------------------------------------------------------------
// Step 2c-pre: quality_score — compute if not already in blackboard
// ---------------------------------------------------------------------------

function stepQualityScore(pr: string): void {
  const root = repoRoot();

  // Check blackboard — values passed as argv to avoid injection
  const pyCheck =
    "import sys\n" +
    "sys.path.insert(0, sys.argv[1])\n" +
    "try:\n" +
    "    from backend.blackboard import get_blackboard\n" +
    "    bb = get_blackboard()\n" +
    "    data = bb.get(sys.argv[2])\n" +
    "    print('yes' if data else 'no')\n" +
    "except Exception:\n" +
    "    print('no')\n";

  const bbResult = runShellNonFatal(
    ["python3", "-c", pyCheck, root, `quality/${pr}`],
    "quality_score blackboard check"
  );

  if (bbResult.stdout.trim() === "yes") {
    process.stdout.write(
      `[post-merge-hook] quality_score: quality/${pr} already in blackboard — skipping scorer\n`
    );
    return;
  }

  process.stdout.write(
    `[post-merge-hook] quality_score: no entry for PR #${pr} — computing now (manual-merge path)\n`
  );
  const scoreResult = runPythonNonFatal(
    [join(root, "backend", "quality_scorer.py"), "score", "--pr", pr],
    "quality_scorer"
  );
  if (scoreResult.exitCode === 0) {
    process.stdout.write(`[post-merge-hook] quality_score: scored PR #${pr} successfully\n`);
  } else {
    process.stderr.write(
      `[post-merge-hook] WARNING: quality_scorer.py exited ${scoreResult.exitCode} for PR #${pr} — lessons may be skipped (non-fatal)\n`
    );
  }
}

// ---------------------------------------------------------------------------
// Step 2c: lessons_record — emit lessons for sub-threshold quality dimensions
// ---------------------------------------------------------------------------

function stepLessonsRecord(pr: string): void {
  const root = repoRoot();

  // Fetch quality JSON from blackboard — values as argv
  const pyFetch =
    "import sys, json\n" +
    "sys.path.insert(0, sys.argv[1])\n" +
    "try:\n" +
    "    from backend.blackboard import get_blackboard\n" +
    "    bb = get_blackboard()\n" +
    "    data = bb.get(sys.argv[2])\n" +
    "    if data:\n" +
    "        print(json.dumps(data))\n" +
    "except Exception:\n" +
    "    pass\n";

  const bbResult = runShellNonFatal(
    ["python3", "-c", pyFetch, root, `quality/${pr}`],
    "lessons_record fetch quality"
  );

  if (!bbResult.stdout.trim()) {
    process.stdout.write(
      `[post-merge-hook] No quality score for PR #${pr} in blackboard — skipping lessons record\n`
    );
    return;
  }

  // Write lessons via Python (LessonsStore is Python-only)
  // Values passed as argv — no shell interpolation of untrusted data
  const pyLessons =
    "import json, sys\n" +
    "from pathlib import Path\n" +
    "\n" +
    "pr = int(sys.argv[1])\n" +
    "quality = json.loads(sys.argv[2])\n" +
    "repo_root = Path(sys.argv[3])\n" +
    "\n" +
    "sys.path.insert(0, str(repo_root))\n" +
    "from backend.lessons import LessonsStore\n" +
    "\n" +
    "store = LessonsStore()\n" +
    "\n" +
    "files_touched = quality.get('files_touched', [])\n" +
    "if files_touched:\n" +
    "    dirs = set()\n" +
    "    for f in files_touched[:10]:\n" +
    "        parts = Path(f).parts\n" +
    "        if len(parts) > 1:\n" +
    "            dirs.add(parts[0] + '/**')\n" +
    "        else:\n" +
    "            dirs.add('*')\n" +
    "    files_pattern = ','.join(sorted(dirs)[:2]) if dirs else '*'\n" +
    "else:\n" +
    "    files_pattern = '*'\n" +
    "\n" +
    "THRESHOLDS = {\n" +
    "    'complexity':     (20, 'Keep functions small — avg McCabe complexity exceeded threshold'),\n" +
    "    'test_coverage':  (15, 'Add test file for every changed module (test_<module>.py in diff)'),\n" +
    "    'review_rounds':  (60, 'Avoid multiple review rounds — fix issues in one shot via preflight'),\n" +
    "}\n" +
    "\n" +
    "dimensions = quality.get('dimensions', {})\n" +
    "for dim, (threshold, template) in THRESHOLDS.items():\n" +
    "    dim_data = dimensions.get(dim, {})\n" +
    "    score = dim_data.get('score', None)\n" +
    "    if score is None:\n" +
    "        continue\n" +
    "    if score < threshold:\n" +
    "        detail = dim_data.get('detail', '')\n" +
    "        lesson = f'{template}. Detail: {detail}' if detail else template\n" +
    "        store.record(\n" +
    "            pr=pr, dimension=dim, score=float(score),\n" +
    "            lesson=lesson[:200], files_pattern=files_pattern, role='executor',\n" +
    "        )\n" +
    "        print(f'[post-merge-hook] Lesson recorded: dim={dim} score={score} pattern={files_pattern}')\n";

  runShellNonFatal(
    ["python3", "-c", pyLessons, pr, bbResult.stdout.trim(), root],
    "lessons_record write"
  );
}

// ---------------------------------------------------------------------------
// Step 3: team_log — terse one-liner
// ---------------------------------------------------------------------------

function stepTeamLog(pr: string, discussion: string | null): void {
  const hhmm = new Date().toTimeString().slice(0, 5);
  let msg = `[${hhmm}] merged PR #${pr}`;
  if (discussion) msg += ` (closes D#${discussion})`;
  runShellNonFatal(
    ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", msg],
    "team_log"
  );
}

// ---------------------------------------------------------------------------
// Step 3b: tmux_reload_flag — write flag if CLAUDE.md changed
// ---------------------------------------------------------------------------

function stepTmuxReloadFlag(pr: string): void {
  // Code plane: stepTmuxReloadFlag — reads a PR's changed files.
  const codeRepo = resolveCodeRepo();
  const result = runShellNonFatal(
    [
      "gh", "pr", "view", pr, "--repo", codeRepo,
      "--json", "files",
      "--jq", `[.files[].path | select(. == "CLAUDE.md")] | length`,
    ],
    "tmux_reload_flag check"
  );

  const touched = parseInt(result.stdout.trim() || "0", 10) || 0;
  if (touched > 0) {
    const flagFile = join(autonomousTeamDir(), "needs-tmux-reload");
    const ts = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    writeFileSync(flagFile, `pr=${pr}\nts=${ts}\n`, "utf8");
    process.stdout.write(
      `[post-merge-hook] CLAUDE.md changed in PR #${pr} — wrote ${flagFile}\n`
    );
    const hhmm = new Date().toTimeString().slice(0, 5);
    runShellNonFatal(
      [
        "bash", join(scriptsDir(), "rotate-team-log.sh"),
        "comment",
        `[${hhmm}] post-merge-hook: CLAUDE.md changed in PR #${pr} — tmux session needs reload (see .autonomous-team/needs-tmux-reload)`,
      ],
      "tmux_reload_flag team-log"
    );
  } else {
    process.stdout.write(
      `[post-merge-hook] PR #${pr} does not touch CLAUDE.md — no tmux reload needed\n`
    );
  }
}

// ---------------------------------------------------------------------------
// Step 4: auto_pull — keep local checkout current
// ---------------------------------------------------------------------------

async function stepAutoPull(pr: string): Promise<void> {
  const root = repoRoot();
  const repo = resolveRepo();
  const stateDirectory = stateDir();

  // Check current branch
  const branchResult = runShellNonFatal(
    ["git", "-C", root, "branch", "--show-current"],
    "auto_pull branch check"
  );
  let currentBranch = branchResult.stdout.trim();

  if (currentBranch !== "main") {
    const hhmm = new Date().toTimeString().slice(0, 5);
    const warnMsg = `[${hhmm}] post-merge-hook: parent repo was on '${currentBranch || "unknown"}' instead of main — likely worktree contamination — attempting checkout main`;
    runShellNonFatal(
      ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", warnMsg],
      "auto_pull branch warn"
    );

    // Check for uncommitted changes
    const dirtyResult = runShellNonFatal(
      ["git", "-C", root, "status", "--porcelain"],
      "auto_pull dirty check"
    );
    if (dirtyResult.stdout.trim()) {
      const errMsg = `[${new Date().toTimeString().slice(0, 5)}] post-merge-hook: ERROR — cannot switch to main, parent repo has uncommitted changes. Run 'git stash && git checkout main && git pull' manually.`;
      runShellNonFatal(
        ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", errMsg],
        "auto_pull dirty error"
      );
      process.exit(1);
    }

    const checkoutResult = runShellNonFatal(
      ["git", "-C", root, "checkout", "main"],
      "auto_pull checkout main"
    );
    if (checkoutResult.exitCode !== 0) {
      const errMsg = `[${new Date().toTimeString().slice(0, 5)}] post-merge-hook: ERROR — git checkout main failed: ${checkoutResult.stderr}`;
      runShellNonFatal(
        ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", errMsg],
        "auto_pull checkout error"
      );
      process.exit(1);
    }

    process.stdout.write(
      `[post-merge-hook] Switched from '${currentBranch}' to main\n`
    );
    currentBranch = "main";
  }

  if (currentBranch === "main") {
    // Prune stale worktree entries
    runShellNonFatal(["git", "-C", root, "worktree", "prune"], "auto_pull worktree prune");

    // Fetch
    const fetchResult = runShellNonFatal(
      ["git", "-C", root, "fetch", "origin", "main"],
      "auto_pull fetch"
    );
    if (fetchResult.exitCode !== 0) {
      const out = fetchResult.stderr + fetchResult.stdout;
      if (out.includes("no such ref was fetched") || out.includes("couldn't find remote ref")) {
        const recoveryMsg = `[${new Date().toTimeString().slice(0, 5)}] post-merge-hook: fetch origin main failed ('no such ref') — forcing reset to origin/main`;
        runShellNonFatal(
          ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", recoveryMsg],
          "auto_pull recovery"
        );
        runShellNonFatal(["git", "-C", root, "fetch", "origin", "--quiet"], "auto_pull fetch all");
        runShellNonFatal(["git", "-C", root, "checkout", "-B", "main", "origin/main"], "auto_pull reset");
      } else {
        process.stderr.write(
          `[post-merge-hook] Warning: fetch origin main returned error (non-fatal): ${fetchResult.stderr}\n`
        );
      }
    }

    const localResult = runShellNonFatal(
      ["git", "-C", root, "rev-parse", "HEAD"],
      "auto_pull local rev"
    );
    const remoteResult = runShellNonFatal(
      ["git", "-C", root, "rev-parse", "origin/main"],
      "auto_pull remote rev"
    );
    const local = localResult.stdout.trim();
    const remote = remoteResult.stdout.trim();

    if (local && local === remote) {
      // Already up to date — silent no-op (mirrors bash comment)
      return;
    }

    const blockerMarker = join(stateDirectory, "auto-pull-blocked");
    // Check for unmerged paths
    const unmergedResult = runShellNonFatal(
      ["git", "-C", root, "diff", "--name-only", "--diff-filter=U"],
      "auto_pull unmerged check"
    );
    const unmergedFiles = unmergedResult.stdout.trim();

    if (unmergedFiles) {
      if (existsSync(blockerMarker)) {
        process.stdout.write(
          "[post-merge-hook] auto-pull: unmerged paths detected — skipping pull (already reported, marker present)\n"
        );
      } else {
        const unmergedList = unmergedFiles.split("\n").slice(0, 5).join(" ");
        const ts = new Date().toISOString().replace(/\.\d+Z$/, "Z");
        mkdirSync(stateDirectory, { recursive: true });
        writeFileSync(blockerMarker, `ts=${ts}\nfiles=${unmergedList}\n`, "utf8");

        const hhmm = new Date().toTimeString().slice(0, 5);
        const warnMsg = `[${hhmm}] post-merge-hook: WARNING needs-boss — auto-pull blocked by unmerged paths in parent repo: ${unmergedList}. Resolve manually, then rm ${blockerMarker}`;
        runShellNonFatal(
          ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", warnMsg],
          "auto_pull unmerged warn"
        );

        // Open idempotent Bug Issue
        const bugTitle = "[Bug] post-merge-hook auto-pull blocked by unmerged paths in parent repo";
        const existingResult = runShellNonFatal(
          [
            "gh", "issue", "list", "--repo", repo,
            "--state", "open", "--json", "number,title",
            "--jq", `[.[] | select(.title == "${bugTitle}")] | first | .number`,
          ],
          "auto_pull bug issue lookup"
        );
        const existingIssue = existingResult.stdout.trim();
        if (existingIssue && existingIssue !== "null") {
          runShellNonFatal(
            [
              "gh", "issue", "comment", existingIssue, "--repo", repo,
              "--body", `Recurred at ${ts}. Unmerged files: ${unmergedList}`,
            ],
            "auto_pull bug issue comment"
          );
          process.stdout.write(
            `[post-merge-hook] auto-pull: updated Bug Issue #${existingIssue} (recurrence)\n`
          );
        } else {
          runShellNonFatal(
            [
              "gh", "issue", "create", "--repo", repo,
              "--title", bugTitle,
              "--label", "needs-boss",
              "--body",
              `Detected at ${ts} (triggered by PR #${pr}). Parent repo has unmerged paths blocking auto-pull.\n\nUnmerged files (first 5):\n${unmergedFiles}\n\n**Resolution**: resolve conflicts in the repo root (or discard with \`git checkout -- <file>\`), then remove the marker:\n\`\`\`\nrm ${blockerMarker}\n\`\`\`\n`,
            ],
            "auto_pull bug issue create"
          );
        }
        process.stdout.write(
          "[post-merge-hook] auto-pull: marker written — subsequent merges will suppress duplicate warnings\n"
        );
      }
      return;
    }

    // Attempt pull
    const pullResult = runShellNonFatal(
      ["git", "-C", root, "pull", "--ff-only", "origin", "main"],
      "auto_pull pull"
    );
    if (pullResult.exitCode === 0) {
      process.stdout.write("[post-merge-hook] auto-pull: pulled main successfully\n");
      if (existsSync(blockerMarker)) {
        try {
          const { rmSync } = (await import("node:fs")) as typeof import("node:fs");
          rmSync(blockerMarker);
        } catch { /* ignore */ }
        process.stdout.write("[post-merge-hook] auto-pull: cleared stale auto-pull-blocked marker\n");
      }
    } else {
      const pullOut = pullResult.stdout + pullResult.stderr;
      if (pullOut.includes("untracked working tree files would be overwritten")) {
        // Extract untracked files and remove them
        const lines = pullOut.split("\n");
        const idx = lines.findIndex((l) => l.includes("untracked working tree"));
        const untrackedFiles = lines
          .slice(idx + 1)
          .filter((l) => l.trim() && !l.startsWith("error:") && !l.includes("Please move"))
          .map((l) => l.trim())
          .filter((l) => l && !l.includes("untracked working tree"))
          .slice(0, 20);

        let removedCount = 0;
        for (const f of untrackedFiles) {
          const fullPath = join(root, f);
          if (existsSync(fullPath)) {
            try {
              const { rmSync } = (await import("node:fs")) as typeof import("node:fs");
              rmSync(fullPath);
              removedCount++;
            } catch { /* ignore */ }
          }
        }

        const retryResult = runShellNonFatal(
          ["git", "-C", root, "pull", "--ff-only", "origin", "main"],
          "auto_pull retry"
        );
        if (retryResult.exitCode === 0) {
          process.stdout.write(
            `[post-merge-hook] auto-pull: removed ${removedCount} stale untracked files and pulled cleanly\n`
          );
        } else {
          const hhmm = new Date().toTimeString().slice(0, 5);
          const msg = `[${hhmm}] post-merge-hook: auto-pull FAILED after rm — ${retryResult.stderr || retryResult.stdout}`;
          runShellNonFatal(
            ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", msg],
            "auto_pull failed after rm"
          );
        }
      } else if (
        pullOut.includes("local changes to the following files would be overwritten") ||
        pullOut.includes("Your local changes")
      ) {
        const hhmm = new Date().toTimeString().slice(0, 5);
        const msg = `[${hhmm}] post-merge-hook: auto-pull SKIPPED — working tree has modifications. Run git stash + git pull manually.`;
        runShellNonFatal(
          ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", msg],
          "auto_pull skip modified"
        );
      } else {
        const hhmm = new Date().toTimeString().slice(0, 5);
        const msg = `[${hhmm}] post-merge-hook: auto-pull FAILED — ${pullOut}`;
        runShellNonFatal(
          ["bash", join(scriptsDir(), "rotate-team-log.sh"), "comment", msg],
          "auto_pull failed"
        );
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Step 5: browser_tour_queue — enqueue a tour if PR touched dashboard/
// ---------------------------------------------------------------------------

function stepBrowserTourQueue(pr: string): void {
  // Code plane: stepBrowserTourQueue — reads a PR's changed files.
  const codeRepo = resolveCodeRepo();

  const result = runShellNonFatal(
    [
      "gh", "pr", "view", pr, "--repo", codeRepo,
      "--json", "files",
      "--jq", `[.files[].path | select(startswith("dashboard/"))] | join("\n")`,
    ],
    "browser_tour_queue check"
  );

  const changedFiles = result.stdout.trim();
  if (!changedFiles) {
    process.stdout.write(
      `[post-merge-hook] PR #${pr} does not touch dashboard — no browser-tour queued\n`
    );
    return;
  }

  process.stdout.write(
    `[post-merge-hook] PR #${pr} touches dashboard — queuing browser-tour\n`
  );

  // Map dashboard file paths to page routes (mirrors bash PAGE_ROUTE_MAP)
  const PAGE_ROUTE_MAP: Record<string, string> = {
    ideaspage: "/ideas",
    discussionspage: "/discussions",
    prspage: "/prs",
    kpipage: "/kpi",
    agentspage: "/agents",
    budgetpage: "/budget",
    looppage: "/loop",
    settingspage: "/settings",
  };

  const pages = new Set<string>();
  for (const f of changedFiles.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const pageMatch = f.match(/dashboard\/src\/pages\/([A-Za-z0-9]+)Page\.tsx/i);
    if (pageMatch) {
      const key = pageMatch[1]!.toLowerCase() + "page";
      pages.add(PAGE_ROUTE_MAP[key] ?? `/${pageMatch[1]!.toLowerCase()}`);
    } else {
      pages.add("/");
    }
  }
  if (pages.size === 0) pages.add("/");

  const affectedPages = [...pages].sort();
  const queuedAt = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const tourGoal = `Regression tour after PR #${pr} — verify ${JSON.stringify(affectedPages)} renders correctly and has no console errors`;

  const queueFile = join(autonomousTeamDir(), "browser-tour-queue.jsonl");
  const entry = JSON.stringify({
    trigger: "post-merge",
    pr: parseInt(pr, 10),
    affected_pages: affectedPages,
    tour_goal: tourGoal,
    queued_at: queuedAt,
    status: "pending",
  });

  try {
    appendFileSync(queueFile, entry + "\n", "utf8");
  } catch (e) {
    process.stderr.write(
      `[post-merge-hook] Warning: browser-tour queue write failed (non-fatal): ${String(e)}\n`
    );
  }

  process.stdout.write(
    `[post-merge-hook] Browser-tour queued for PR #${pr} — pages: ${JSON.stringify(affectedPages)}\n`
  );
}

// ---------------------------------------------------------------------------
// Step 6: stats_metrics — emit 8 post-merge metrics to stats.duckdb
// This is the primary parity-tested step.
// ---------------------------------------------------------------------------

export interface StatsMetricsInput {
  pr: string;
  discTag: string;
  fixCycleCount: number;
  costUsd: number;
  conflictScore: number;
  prCreatedAt: string;
  specReadyTs: string;
  reviewerAcceptTs: string;
  acPassRate: number;
}

export async function stepStatsMetrics(
  input: StatsMetricsInput,
  overrideStatsDb?: string
): Promise<void> {
  const {
    pr,
    discTag,
    fixCycleCount,
    costUsd,
    conflictScore,
    prCreatedAt,
    specReadyTs,
    reviewerAcceptTs,
    acPassRate,
  } = input;

  const nowDt = new Date();
  const createdDt = parseIso(prCreatedAt);
  const elapsed = createdDt ? Math.max(0, (nowDt.getTime() - createdDt.getTime()) / 1000) : 0;

  const specDt = parseIso(specReadyTs);
  const specLatency =
    specDt && createdDt ? Math.max(0, (createdDt.getTime() - specDt.getTime()) / 1000) : -1;

  const acRate = isNaN(acPassRate) ? -1 : acPassRate;

  const reviewerDt = parseIso(reviewerAcceptTs);
  const reviewerLatency =
    reviewerDt && createdDt
      ? Math.max(0, (reviewerDt.getTime() - createdDt.getTime()) / 1000)
      : -1;

  const tags = { pr, tag: discTag || "unknown" };

  const rows: MetricRow[] = [
    { metric: "time_to_merge_seconds",               value: elapsed,       unit: "seconds", tags, source: "post-merge-hook" },
    { metric: "fix_cycle_count",                     value: fixCycleCount, unit: "count",   tags, source: "post-merge-hook" },
    { metric: "cost_per_merged_pr_usd",              value: costUsd,       unit: "usd",     tags, source: "post-merge-hook" },
    { metric: "pr_file_conflict_score",              value: conflictScore, unit: "count",   tags, source: "post-merge-hook" },
    { metric: "spec_to_first_pr_latency_seconds",    value: specLatency,   unit: "seconds", tags, source: "post-merge-hook" },
    { metric: "acceptance_criteria_pass_rate",       value: acRate,        unit: "ratio",   tags, source: "post-merge-hook" },
    { metric: "reviewer_acceptance_latency_seconds", value: reviewerLatency, unit: "seconds", tags, source: "post-merge-hook" },
    { metric: "fix_rounds_per_pr",                   value: fixCycleCount, unit: "count",   tags, source: "post-merge-hook" },
  ];

  await recordMetrics(rows, overrideStatsDb);

  process.stdout.write(
    `[post-merge-hook] stats: time_to_merge=${elapsed.toFixed(0)}s fix_cycles=${fixCycleCount} cost=${costUsd.toFixed(4)} conflict=${conflictScore}\n`
  );
  process.stdout.write(
    `[post-merge-hook] stats: spec_latency=${specLatency.toFixed(0)}s ac_rate=${acRate.toFixed(4)} reviewer_latency=${reviewerLatency.toFixed(0)}s\n`
  );
  process.stdout.write(
    `[post-merge-hook] stats: fix_rounds_per_pr=${fixCycleCount}\n`
  );
}

// ---------------------------------------------------------------------------
// Gather stats inputs — shells to gh + python to collect metrics data
// (mirrors bash stats_metrics section data-gathering)
// ---------------------------------------------------------------------------

/**
 * How many files this PR touches that a PR merged in the previous 6h also
 * touched, counted once per overlapping file per other PR.
 *
 * Was a Python program built as a string in gatherStatsInputs(), spawning `gh`
 * twice from inside it. Behaviour is preserved exactly, including returning 0
 * on any failure and short-circuiting when the PR touches no files — the
 * Python printed 0 and exited in both cases.
 */
function _prFileConflictScore(pr: string, codeRepo: string): number {
  let prFiles: Set<string>;
  try {
    const r = runShellNonFatal(
      ["gh", "pr", "view", pr, "--repo", codeRepo,
       "--json", "files", "--jq", "[.files[].path]"],
      "stats conflictScore files"
    );
    const parsed = JSON.parse(r.stdout.trim() || "[]") as unknown;
    if (!Array.isArray(parsed)) return 0;
    prFiles = new Set(parsed.filter((x): x is string => typeof x === "string"));
  } catch {
    return 0;
  }
  if (prFiles.size === 0) return 0;

  const cutoff = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
  let recent: Array<{ files?: Array<{ path?: string }> }>;
  try {
    const r2 = runShellNonFatal(
      ["gh", "pr", "list", "--repo", codeRepo, "--state", "merged",
       "--json", "number,mergedAt,files",
       "--jq",
       `[.[] | select(.number != ${pr} and .mergedAt != null and .mergedAt > "${cutoff}")]`],
      "stats conflictScore recent"
    );
    const parsed = JSON.parse(r2.stdout.trim() || "[]") as unknown;
    if (!Array.isArray(parsed)) return 0;
    recent = parsed as Array<{ files?: Array<{ path?: string }> }>;
  } catch {
    return 0;
  }

  let overlapCount = 0;
  for (const other of recent) {
    for (const f of other.files ?? []) {
      if (f.path && prFiles.has(f.path)) overlapCount += 1;
    }
  }
  return overlapCount;
}

function gatherStatsInputs(pr: string, discussion: string | null): StatsMetricsInput {
  // Two planes: gatherStatsInputs — PR reads incl. embedded Python; discussion queries.
  //
  // The code-plane value is `codeRepo`, and the name is
  // load-bearing: `repo` still means the Discussion plane
  // elsewhere in this file, and the embedded Python below
  // receives its slug by argv position, so the two have to
  // be distinguishable by more than context.
  const codeRepo = resolveCodeRepo();
  const repo = resolveRepo();
  const repoOwner = repo.split("/")[0]!;
  const repoName = repo.split("/")[1]!;
  const root = repoRoot();

  // Discussion tag from title
  let discTag = "";
  if (discussion) {
    const tagResult = runShellNonFatal(
      [
        "gh", "api", "graphql",
        "-f",
        `query=query { repository(owner:"${repoOwner}", name:"${repoName}") { discussion(number:${discussion}) { title } } }`,
        "--jq", ".data.repository.discussion.title",
      ],
      "stats discTag"
    );
    const title = tagResult.stdout.trim();
    const tagMatch = title.match(/^\[([A-Za-z]+)\]/);
    discTag = tagMatch?.[1] ?? "";
  }

  // PR creation time
  const prCreatedResult = runShellNonFatal(
    ["gh", "pr", "view", pr, "--repo", codeRepo, "--json", "createdAt", "--jq", ".createdAt"],
    "stats prCreatedAt"
  );
  const prCreatedAt = prCreatedResult.stdout.trim();

  // Fix cycle count (label applications)
  const fixCycleResult = runShellNonFatal(
    [
      "gh", "pr", "view", pr, "--repo", codeRepo,
      "--json", "timelineItems",
      "--jq", `[.timelineItems.nodes[] | select(.label.name == "code-review-needs-fix")] | length`,
    ],
    "stats fixCycles"
  );
  const fixCycleCount = parseInt(fixCycleResult.stdout.trim() || "0", 10) || 0;

  // Cost for Discussion — via cost_tracker.py, the single source of truth
  // for cost. (This used to shell out to `budget.py status` and re-price by
  // hand with an inline copy of the pricing table, mirroring the bash
  // implementation byte-for-byte; that read a store — budget/agents/ — that
  // could silently stop being written, and it duplicated pricing rates that
  // already lived in cost_tracker.py.)
  let costUsd = 0;
  if (discussion) {
    const costResult = runPythonNonFatal(
      [`${root}/backend/cost_tracker.py`, "by-discussion", "--discussion", discussion, "--json"],
      "stats costUsd"
    );
    try {
      const parsed = costResult.stdout.trim()
        ? JSON.parse(costResult.stdout.trim())
        : null;
      costUsd = parsed && typeof parsed.total_cost_usd === "number" ? parsed.total_cost_usd : 0;
    } catch {
      costUsd = 0;
    }
  }

  // PR file conflict score: overlap with PRs merged in previous 6h.
  //
  // This used to be a Python program, built as a string here, whose first two
  // acts were to spawn `gh`. The repo those calls used was an identifier
  // inside a string literal — unreadable from this file by anyone, tool or
  // person, without following an argv position by hand. Same computation, one
  // fewer process, and the plane is now visible at the call site.
  const conflictScore = _prFileConflictScore(pr, codeRepo);

  // SPEC_READY timestamp from Discussion body
  let specReadyTs = "";
  let discBody = "";
  if (discussion) {
    const discBodyResult = runShellNonFatal(
      [
        "gh", "api", "graphql",
        "-f",
        `query=query { repository(owner:"${repoOwner}", name:"${repoName}") { discussion(number:${discussion}) { body } } }`,
        "--jq", ".data.repository.discussion.body",
      ],
      "stats discBody"
    );
    discBody = discBodyResult.stdout.trim();
    const srMatch = discBody.match(/STATUS:SPEC_READY SINCE:([^\s>]+)/);
    specReadyTs = srMatch?.[1] ?? "";
  }

  // Reviewer acceptance latency: PR open -> first code-review-passed label
  const reviewerResult = runShellNonFatal(
    [
      "gh", "api", `repos/${codeRepo}/issues/${pr}/timeline`,
      "--jq",
      `[.[] | select(.event == "labeled" and .label.name == "code-review-passed")] | first | .created_at`,
    ],
    "stats reviewerAcceptTs"
  );
  const reviewerAcceptTs = reviewerResult.stdout.trim();

  // Acceptance criteria pass rate
  let acPassRate = -1;
  if (discussion && discBody) {
    const pyAc =
      // No subprocess import: the gh call this used to make now happens in
      // TypeScript above, and its JSON arrives as argv[3]. The regex and
      // formatting semantics below are Python's and stay Python's.
      "import re, json, sys\n" +
      "disc_body = sys.argv[1]\n" +
      "pr = sys.argv[2]\n" +
      "pr_json = sys.argv[3]\n" +
      "ac_section = re.search(r'### Acceptance Criteria\\s*([\\s\\S]*?)(?=\\n###|\\Z)', disc_body)\n" +
      "if not ac_section:\n" +
      "    print('-1.0'); exit()\n" +
      "ac_text = ac_section.group(1)\n" +
      "ac_lines = re.findall(r'^\\s*(?:\\d+\\.|[-*])\\s+(.+)', ac_text, re.MULTILINE)\n" +
      "if not ac_lines:\n" +
      "    print('-1.0'); exit()\n" +
      "try:\n" +
      "    pr_data = json.loads(pr_json)\n" +
      "    evidence_text = (pr_data.get('body') or '') + ' '.join(\n" +
      "        c.get('body', '') for c in pr_data.get('comments', [])\n" +
      "    )\n" +
      "except Exception:\n" +
      "    evidence_text = ''\n" +
      "referenced = 0\n" +
      "for ac in ac_lines:\n" +
      "    words = [w.lower() for w in re.findall(r'\\w+', ac) if len(w) > 3][:6]\n" +
      "    if any(w in evidence_text.lower() for w in words):\n" +
      "        referenced += 1\n" +
      "rate = referenced / len(ac_lines)\n" +
      "print(f'{rate:.4f}')\n";

    // The gh call the Python used to make, hoisted so its plane is readable
    // from this file. An empty string on failure reproduces the old
    // behaviour exactly: the Python's own `except` set evidence_text to "".
    const acEvidence = runShellNonFatal(
      ["gh", "pr", "view", pr, "--repo", codeRepo, "--json", "body,comments"],
      "stats acPassRate evidence"
    );
    const acResult = runShellNonFatal(
      ["python3", "-c", pyAc, discBody, pr, acEvidence.stdout.trim() || "{}"],
      "stats acPassRate"
    );
    const acStr = acResult.stdout.trim();
    acPassRate = parseFloat(acStr || "-1");
    if (isNaN(acPassRate)) acPassRate = -1;
  }

  return {
    pr,
    discTag,
    fixCycleCount,
    costUsd,
    conflictScore,
    prCreatedAt,
    specReadyTs,
    reviewerAcceptTs,
    acPassRate,
  };
}

// ---------------------------------------------------------------------------
// Step 7: release_manager_queue
// ---------------------------------------------------------------------------

function stepReleaseManagerQueue(pr: string): void {
  const root = repoRoot();
  const releaseGateResult = runPythonNonFatal(
    [join(root, "backend", "control_plane.py"), "get", "gates.release_manager"],
    "release_gate check"
  );
  const releaseGate = releaseGateResult.stdout.trim();
  const hhmm = new Date().toTimeString().slice(0, 5);

  if (releaseGate === "true") {
    runPythonNonFatal(
      [join(root, "backend", "release_manager.py"), "record", "--pr", pr],
      "release_manager record"
    );
    runShellNonFatal(
      [
        "bash", join(scriptsDir(), "rotate-team-log.sh"),
        "comment",
        `[${hhmm}] post-merge-hook: release record written for PR #${pr}`,
      ],
      "release_manager team-log"
    );
  } else {
    runShellNonFatal(
      [
        "bash", join(scriptsDir(), "rotate-team-log.sh"),
        "comment",
        `[${hhmm}] post-merge-hook: release_manager gate off — skipping record for PR #${pr}`,
      ],
      "release_manager skip team-log"
    );
  }
}

// ---------------------------------------------------------------------------
// Step 8: interactive_metrics_tick
// ---------------------------------------------------------------------------

function stepInteractiveMetricsTick(): void {
  runShellNonFatal(
    ["bash", join(scriptsDir(), "interactive-metrics-tick.sh")],
    "interactive_metrics_tick"
  );
}

// ---------------------------------------------------------------------------
// Step 9: hourly_stats_refresh
// ---------------------------------------------------------------------------

function stepHourlyStatsRefresh(): void {
  runShellNonFatal(
    ["bash", join(scriptsDir(), "spawn-hourly-stats.sh")],
    "hourly_stats_refresh"
  );
}

// ---------------------------------------------------------------------------
// Step 9b: reap_chromes
// ---------------------------------------------------------------------------

function stepReapChromes(): void {
  runShellNonFatal(
    ["bash", join(scriptsDir(), "reap-zombie-chromes.sh")],
    "reap_chromes"
  );
}

// ---------------------------------------------------------------------------
// Step 10: drain_pending_prs
// ---------------------------------------------------------------------------

function stepDrainPendingPrs(pr: string): void {
  const pendingFile = join(autonomousTeamDir(), "pending-prs.json");
  if (!existsSync(pendingFile)) {
    process.stdout.write("[post-merge-hook] No pending-prs.json — drain step skipped\n");
    return;
  }
  process.stdout.write(`[post-merge-hook] Draining pending-prs.json after merge of PR #${pr}\n`);
  runShellNonFatal(
    ["bash", join(scriptsDir(), "drain-pending-prs.sh")],
    "drain_pending_prs"
  );
}

// ---------------------------------------------------------------------------
// Step 11: post-merge.d/ drop-in scripts
// ---------------------------------------------------------------------------

function stepPostMergeD(pr: string): void {
  const hooksDir = join(scriptsDir(), "hooks", "post-merge.d");
  if (!existsSync(hooksDir)) return;

  for (const hookName of readdirSync(hooksDir).sort()) {
    const hookPath = join(hooksDir, hookName);
    runShellNonFatal(
      ["bash", hookPath, "--pr", pr],
      `post-merge.d/${hookName}`,
      { env: { PR: pr } }
    );
  }
}

// ---------------------------------------------------------------------------
// Step 12: sweep_loop_runs
// ---------------------------------------------------------------------------

function stepSweepLoopRuns(): void {
  runShellNonFatal(
    ["bash", join(scriptsDir(), "sweep-loop-runs.sh")],
    "sweep_loop_runs"
  );
}

// ---------------------------------------------------------------------------
// Auto-detect discussions from PR body (mirrors bash logic exactly)
// Matches (case-insensitive): Closes/Fixes/Resolves followed by D#N or #N.
// Validates each candidate via GraphQL — Issues/PRs are skipped.
// ---------------------------------------------------------------------------

function autoDetectDiscussions(pr: string): string[] {
  // Two planes: autoDetectDiscussions — reads a PR body; queries discussions.
  //
  // The code-plane value is `codeRepo`, and the name is
  // load-bearing: `repo` still means the Discussion plane
  // elsewhere in this file, and the embedded Python below
  // receives its slug by argv position, so the two have to
  // be distinguishable by more than context.
  const codeRepo = resolveCodeRepo();
  const repo = resolveRepo();
  const repoOwner = repo.split("/")[0]!;
  const repoName = repo.split("/")[1]!;

  const bodyResult = runShellNonFatal(
    ["gh", "pr", "view", pr, "--repo", codeRepo, "--json", "body", "--jq", ".body"],
    "auto-detect discussions body"
  );
  const prBody = bodyResult.stdout.trim();

  // Extract candidate numbers from recognised closing-keyword patterns only
  const closingPattern = /(?:closes|resolves|fixes)\s+(?:D#|#)(\d+)/gi;
  const candNums = new Set<string>();
  let m: RegExpExecArray | null;
  while ((m = closingPattern.exec(prBody)) !== null) {
    candNums.add(m[1]!);
  }

  const discussions: string[] = [];
  for (const cand of candNums) {
    const validResult = runShellNonFatal(
      [
        "gh", "api", "graphql",
        "-f",
        `query=query { repository(owner:"${repoOwner}", name:"${repoName}") { discussion(number:${cand}) { id } } }`,
        "--jq", ".data.repository.discussion.id",
      ],
      `validate discussion #${cand}`
    );
    const discId = validResult.stdout.trim();
    if (discId && discId !== "null") {
      discussions.push(cand);
      process.stdout.write(
        `[post-merge-hook] Auto-detected Discussion #${cand} from PR #${pr} body\n`
      );
    } else {
      process.stdout.write(
        `[post-merge-hook] Skipping #${cand} — not a Discussion (Issue/PR or missing)\n`
      );
    }
  }
  return discussions;
}

// ---------------------------------------------------------------------------
// Main orchestrator — runs all steps in bash-identical order
// ---------------------------------------------------------------------------

export async function run(args: PostMergeHookArgs): Promise<void> {
  const tracker = new StepTracker();

  // Resolve discussions (array, mirrors bash DISCUSSIONS)
  let discussions: string[];
  if (args.discussion) {
    discussions = [args.discussion];
  } else {
    discussions = autoDetectDiscussions(args.pr);
  }

  // First entry for backward compat (mirrors bash DISCUSSION="${DISCUSSIONS[0]:-}")
  const discussion = discussions[0] ?? null;

  process.stdout.write(
    `[post-merge-hook] event_id=${args.eventId ?? "(none)"} pr=${args.pr}\n`
  );

  // Step 0: agent_feed
  if (!tracker.has("agent_feed")) {
    stepAgentFeed(args, discussion);
    tracker.mark("agent_feed");
  }

  // Step 1: wiki_sync
  if (!tracker.has("wiki_sync")) {
    stepWikiSync(args.pr);
    tracker.mark("wiki_sync");
  }

  // Step 2: discussion_close
  if (!tracker.has("discussion_close")) {
    if (discussions.length > 0) {
      stepDiscussionClose(discussions, args.pr);
    }
    tracker.mark("discussion_close");
  }

  // Step 2a: cost_comment
  if (!tracker.has("cost_comment")) {
    if (discussion) {
      const repo = resolveRepo();
      const repoOwner = repo.split("/")[0]!;
      const repoName = repo.split("/")[1]!;
      const idResult = runShellNonFatal(
        [
          "gh", "api", "graphql",
          "-f",
          `query=query { repository(owner:"${repoOwner}", name:"${repoName}") { discussion(number:${discussion}) { id } } }`,
          "--jq", ".data.repository.discussion.id",
        ],
        "cost_comment discId"
      );
      const discId = idResult.stdout.trim();
      if (discId && discId !== "null") {
        stepCostComment(discussion, discId);
      } else {
        process.stdout.write(
          "[post-merge-hook] cost_comment: no Discussion linked or DISC_ID missing — skipping\n"
        );
      }
    } else {
      process.stdout.write(
        "[post-merge-hook] cost_comment: no Discussion linked or DISC_ID missing — skipping\n"
      );
    }
    tracker.mark("cost_comment");
  }

  // Step 2b: completion_block
  if (!tracker.has("completion_block")) {
    if (discussion) {
      const repo = resolveRepo();
      const repoOwner = repo.split("/")[0]!;
      const repoName = repo.split("/")[1]!;
      const dataResult = runShellNonFatal(
        [
          "gh", "api", "graphql",
          "-f",
          `query=query { repository(owner:"${repoOwner}", name:"${repoName}") { discussion(number:${discussion}) { id body } } }`,
          "--jq", ".data.repository.discussion",
        ],
        "completion_block fetch"
      );
      let discId = "";
      let currentBody = "";
      if (dataResult.stdout.trim()) {
        try {
          const parsed = JSON.parse(dataResult.stdout.trim()) as { id?: string; body?: string };
          discId = parsed?.id ?? "";
          currentBody = parsed?.body ?? "";
        } catch { /* non-fatal */ }
      }
      if (discId) {
        stepCompletionBlock(discussion, discId, currentBody, args.pr);
      }
    } else {
      process.stdout.write("[post-merge-hook] completion_block: no Discussion linked — skipping\n");
    }
    tracker.mark("completion_block");
  }

  // Step 2b: worktree_merge_registry
  if (!tracker.has("worktree_merge_registry")) {
    stepWorktreeMergeRegistry(args.pr);
    tracker.mark("worktree_merge_registry");
  }

  // Step 2c-pre: quality_score
  if (!tracker.has("quality_score")) {
    stepQualityScore(args.pr);
    tracker.mark("quality_score");
  }

  // Step 2c: lessons_record
  if (!tracker.has("lessons_record")) {
    stepLessonsRecord(args.pr);
    tracker.mark("lessons_record");
  }

  // Step 3: team_log
  if (!tracker.has("team_log")) {
    stepTeamLog(args.pr, discussion);
    tracker.mark("team_log");
  }

  // Step 3b: tmux_reload_flag
  if (!tracker.has("tmux_reload_flag")) {
    stepTmuxReloadFlag(args.pr);
    tracker.mark("tmux_reload_flag");
  }

  // Step 4: auto_pull
  if (!tracker.has("auto_pull")) {
    await stepAutoPull(args.pr);
    tracker.mark("auto_pull");
  }

  // Step 5: browser_tour_queue
  if (!tracker.has("browser_tour_queue")) {
    stepBrowserTourQueue(args.pr);
    tracker.mark("browser_tour_queue");
  }

  // Step 6: stats_metrics
  if (!tracker.has("stats_metrics")) {
    const statsInput = gatherStatsInputs(args.pr, discussion);
    await stepStatsMetrics(statsInput);
    tracker.mark("stats_metrics");
  }

  // Step 7: release_manager_queue
  if (!tracker.has("release_manager_queue")) {
    stepReleaseManagerQueue(args.pr);
    tracker.mark("release_manager_queue");
  }

  // Step 8: interactive_metrics_tick
  if (!tracker.has("interactive_metrics_tick")) {
    stepInteractiveMetricsTick();
    tracker.mark("interactive_metrics_tick");
  }

  // Step 9: hourly_stats_refresh
  if (!tracker.has("hourly_stats_refresh")) {
    stepHourlyStatsRefresh();
    tracker.mark("hourly_stats_refresh");
  }

  // Step 9b: reap_chromes
  if (!tracker.has("reap_chromes")) {
    stepReapChromes();
    tracker.mark("reap_chromes");
  }

  // Step 10: drain_pending_prs
  if (!tracker.has("drain_pending_prs")) {
    stepDrainPendingPrs(args.pr);
    tracker.mark("drain_pending_prs");
  }

  // Step 11: post-merge.d/ drop-ins (no idempotency guard — bash has none)
  stepPostMergeD(args.pr);

  // Step 12: sweep_loop_runs
  if (!tracker.has("sweep_loop_runs")) {
    stepSweepLoopRuns();
    tracker.mark("sweep_loop_runs");
  }

  process.stdout.write("[post-merge-hook] Done.\n");
}

// ---------------------------------------------------------------------------
// CLI entry point
// ---------------------------------------------------------------------------

if (import.meta.main) {
  const args = parseArgs(process.argv.slice(2));
  run(args).catch((e) => {
    process.stderr.write(`[post-merge-hook] Fatal: ${String(e)}\n`);
    process.exit(1);
  });
}
