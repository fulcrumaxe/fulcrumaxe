/**
 * config/repo.ts — repo slug resolver for tui/.
 *
 * Replaces two pre-rename hardcoded literals (`tui/src/backend.ts` GH_REPO,
 * `tui/src/index.tsx`'s embedded `gh api graphql --repo` command) that were
 * allowlisted in scripts/ci/repo-target-gate.sh pending "a focused TUI-config
 * follow-up" (D#2380). Both allowlist entries are removed alongside this file.
 *
 * Mirrors ts-backend/src/config/repo.ts's precedence chain:
 *   1. .autonomous-team/config.json "repo" field
 *   2. GH_REPO environment variable
 *   3. _REPO environment variable
 *   4. DEFAULT_REPO constant (hardcoded fallback)
 *
 * with one addition: an origin-remote step (mirroring
 * backend/_repo_remote.py, added to the Python resolver by #2341) inserted
 * just before the hardcoded fallback. That step is what makes a forked
 * adopter's checkout — no config.json, no env vars — resolve to their own
 * repo instead of this project's, and D#2380's Spec calls it out by name as
 * belonging in whichever implementation wins here.
 *
 * Why a second copy instead of importing ts-backend/src/config/repo.ts: the
 * two packages have no workspace link — there is no root package.json on
 * this tree at all (open-source export, D#2348), so no npm workspaces, no
 * shared node_modules, nothing to `npm link`. They also build with
 * different, incompatible toolchains: ts-backend targets bun
 * ("moduleResolution": "bundler", `types: ["bun-types"]`) while tui compiles
 * with tsc under "moduleResolution": "NodeNext" and `rootDir: "src"`, which
 * would reject an import reaching outside tui/src at typecheck time. Wiring
 * either side into the other is not the one-line patch this Discussion is
 * scoped to avoid.
 *
 * Anti-drift: tui/tests/config/repo.sync.test.ts imports this file AND
 * ts-backend/src/config/repo.ts directly (vitest resolves both — no build
 * link required, only a test-time one) and asserts DEFAULT_REPO and
 * resolveRepo() agree across a matrix of config.json / env-var scenarios. A
 * change to either file's precedence or default that the other doesn't
 * follow fails that test, not just this comment.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const DEFAULT_REPO = 'autonomous-agent-7/fulcrumaxe';

function repoRoot(): string {
  return (
    process.env['AF_REPO_ROOT'] ??
    resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
  );
}

/**
 * Treat unset, empty, and whitespace-only values as absent — the same
 * "no answer" outcome as `null`/`undefined`. A precedence step's `??` only
 * short-circuits on nullish values, so a defined-but-empty string (e.g.
 * `GH_REPO=""`) would otherwise win the step and compose into an empty
 * `--repo` / `owner:` / `name:` downstream (D#2380 code review).
 */
function nonEmpty(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  return value.trim() ? value : null;
}

function configJsonField(key: string): string | null {
  const configPath = join(repoRoot(), '.autonomous-team', 'config.json');
  if (!existsSync(configPath)) return null;
  try {
    const data = JSON.parse(readFileSync(configPath, 'utf8')) as Record<string, unknown>;
    const value = data[key];
    return typeof value === 'string' ? nonEmpty(value) : null;
  } catch {
    return null;
  }
}

function configJsonRepo(): string | null {
  return configJsonField('repo');
}

// OWNER and NAME as GitHub actually allows them: no "/" (that's the
// separator), no whitespace, no quoting or fragment punctuation. Mirrors
// backend/_repo_remote.py's _VALID_SLUG_PART.
const VALID_SLUG_PART = /^[A-Za-z0-9._-]+$/;

/**
 * Return OWNER/NAME from a git remote URL, or null if it isn't one. Handles
 * the two forms git writes for GitHub remotes — `https://github.com/O/N.git`
 * and `git@github.com:O/N.git` (plus the `ssh://` spelling of the latter).
 * Anything else returns null: a wrong slug is worse than no slug. Ported
 * from backend/_repo_remote.py's _slug_from_url.
 */
function slugFromUrl(rawUrl: string): string | null {
  const url = rawUrl.trim();
  if (!url) return null;

  let path: string;
  if (url.includes('://')) {
    const rest = url.slice(url.indexOf('://') + 3);
    const slashIdx = rest.indexOf('/');
    if (slashIdx === -1) return null;
    path = rest.slice(slashIdx + 1);
  } else if (url.includes('@') && url.includes(':')) {
    path = url.slice(url.indexOf(':') + 1);
  } else {
    return null;
  }

  if ((path.match(/\//g) ?? []).length !== 1 || /\s/.test(path)) return null;
  const [owner, nameRaw] = path.split('/') as [string, string];
  if (!owner || !nameRaw) return null;
  const name = nameRaw.endsWith('.git') ? nameRaw.slice(0, -'.git'.length) : nameRaw;
  if (!VALID_SLUG_PART.test(owner) || !VALID_SLUG_PART.test(name)) return null;
  return `${owner}/${name}`;
}

/**
 * Return the OWNER/NAME slug of the `origin` remote under *root*, or null.
 * Never throws: a missing .git/config, an unreadable file, a checkout with
 * no origin, or a malformed section all fall through to null so the caller
 * moves on to DEFAULT_REPO. Deliberately narrow — reads only
 * `[remote "origin"]`'s `url` key, not a general INI parser.
 */
function repoSlugFromGitConfig(root: string): string | null {
  const configPath = join(root, '.git', 'config');
  let raw: string;
  try {
    raw = readFileSync(configPath, 'utf8');
  } catch {
    return null;
  }

  let inOrigin = false;
  for (const line of raw.split('\n')) {
    const trimmed = line.trim();
    if (trimmed.startsWith('[')) {
      inOrigin = /^\[remote\s+"origin"\]$/.test(trimmed);
      continue;
    }
    if (inOrigin) {
      const match = /^url\s*=\s*(.+)$/.exec(trimmed);
      if (match?.[1]) {
        const slug = slugFromUrl(match[1]);
        if (slug) return slug;
      }
    }
  }
  return null;
}

/**
 * Resolve the repo slug ("owner/name") using the precedence order documented
 * above. Safe to call repeatedly — re-reads env/config/git each call so
 * tests can override state between cases.
 *
 * Every step is normalized through `nonEmpty()` so a defined-but-empty or
 * whitespace-only value (e.g. `GH_REPO=""`) falls through to the next
 * source instead of winning the step — see `nonEmpty()`'s docstring.
 *
 * DEFAULT_REPO is a non-empty hardcoded literal, so in practice this chain
 * always resolves. The explicit throw below is a guard on that invariant,
 * not reachable code today: it exists so a future edit that weakens
 * DEFAULT_REPO (or the chain) fails loudly at the resolution site instead
 * of silently handing an empty slug to a `gh --repo` call, which exits 0
 * and quietly falls back to the git remote — exactly the failure mode this
 * resolver exists to prevent (D#2380).
 */
export function resolveRepo(): string {
  const repo =
    configJsonRepo() ??
    nonEmpty(process.env['GH_REPO']) ??
    nonEmpty(process.env['_REPO']) ??
    repoSlugFromGitConfig(repoRoot()) ??
    nonEmpty(DEFAULT_REPO);
  if (!repo) {
    throw new Error(
      'resolveRepo(): no non-empty repo slug from .autonomous-team/config.json, GH_REPO, _REPO, ' +
        'the .git/config origin remote, or DEFAULT_REPO — refusing to return an empty slug'
    );
  }
  return repo;
}

/** Split helper: the "owner" half of a resolved (or supplied) repo slug. */
export function repoOwner(repo: string = resolveRepo()): string {
  return repo.split('/')[0] ?? '';
}

/** Split helper: the "name" half of a resolved (or supplied) repo slug. */
export function repoName(repo: string = resolveRepo()): string {
  return repo.split('/')[1] ?? '';
}
