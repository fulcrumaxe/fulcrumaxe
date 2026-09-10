/**
 * discussionsQuery.ts — open-Discussions queue-count query.
 *
 * Extracted out of index.tsx (D#2380) so the query composition can be
 * exercised directly in a test: index.tsx calls `render()` at module load,
 * which a test importing it would trigger too. This module has no top-level
 * side effects, so tests can import it and mock `child_process.exec` to
 * assert on the composed command without touching index.tsx at all.
 * index.tsx imports and calls readQueueCountsAsync() exactly as before.
 */
import { exec } from 'child_process';
import { promisify } from 'util';
import { resolveRepo, repoName, repoOwner } from './config/repo.js';

const execAsync = promisify(exec);

/**
 * Compose the `gh api graphql` command for the open-Discussions queue-count
 * query, targeting *repo* in both the `--repo` flag and the `owner:`/`name:`
 * pair inside the query string itself.
 */
export function buildDiscussionsQueryCommand(repo: string): string {
  const owner = repoOwner(repo);
  const name = repoName(repo);
  return `gh api graphql --repo ${repo} -f query='query { repository(owner:"${owner}", name:"${name}") { discussions(first:50, states:[OPEN]) { nodes { body } } } }'`;
}

export async function readQueueCountsAsync(): Promise<{ active: number; ready: number } | null> {
  try {
    const { stdout } = await execAsync(buildDiscussionsQueryCommand(resolveRepo()), { timeout: 15000 });
    const result = JSON.parse(stdout) as {
      data?: { repository?: { discussions?: { nodes?: Array<{ body: string }> } } };
    };
    const nodes = result.data?.repository?.discussions?.nodes ?? [];
    let active = 0;
    let ready = 0;
    for (const node of nodes) {
      if (/STATUS:SPEC_READY/.test(node.body)) {
        ready++;
      } else if (/STATUS:(DISCUSSING|IMPLEMENTING|REVIEWING)/.test(node.body)) {
        active++;
      }
    }
    return { active, ready };
  } catch {
    return null;
  }
}
