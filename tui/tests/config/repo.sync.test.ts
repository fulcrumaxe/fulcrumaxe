/**
 * Anti-drift test for the deliberate second copy of the precedence chain in
 * tui/src/config/repo.ts (see that file's docstring). Imports both resolvers
 * directly — no build-time link between the two packages is required, only
 * a test-time one — and asserts they agree on the shared part of the chain
 * (config.json "repo" field, GH_REPO, _REPO, and the DEFAULT_REPO constant).
 * The origin-remote step is tui-only (D#2380) and is not compared here.
 *
 * The "agree on the shared precedence steps" block below only ever exercised
 * non-empty inputs, so it could not catch a divergence in *emptiness*
 * handling — exactly the shape of bug D#2520 found: ts-backend's resolveRepo()
 * returned "" for a defined-but-empty GH_REPO while tui's already treated it
 * as absent. The "agree on emptiness handling" block closes that gap.
 *
 * The "bash and Python also treat whitespace-only as absent" block (D#2536)
 * extends the same property past the two TypeScript copies. Neither bash's
 * scripts/lib/repo-resolve.sh nor Python's backend/_repo.py can be imported
 * into a vitest test directly, so they're driven the same way
 * ts-backend/tests/spawn/*.parity.test.ts already drives cross-language
 * parity elsewhere in this repo: spawn the real interpreter against a copy
 * of the real source, isolated in a scratch tree (each resolver derives its
 * own repo root from its own file location, not from an env var — the
 * scratch copy is what makes that overridable per test). Neither shares
 * config.json's "repo" field with bash/TypeScript (Python reads
 * project.json instead, and with a different precedence order — see
 * backend/_repo_planes.py's module docstring), but AUTONOMOUS_TEAM_REPO is
 * one literal env var both bash's _resolve_repo() and Python's _load_repo()
 * read at their own top precedence step, which is what pins the two
 * together here.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { execFileSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_REPO as TUI_DEFAULT_REPO, resolveRepo as tuiResolveRepo } from '../../src/config/repo.js';
import {
  DEFAULT_REPO as TS_BACKEND_DEFAULT_REPO,
  resolveRepo as tsBackendResolveRepo,
} from '../../../ts-backend/src/config/repo.ts';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

const ENV_KEYS = ['AF_REPO_ROOT', 'GH_REPO', '_REPO'] as const;
let savedEnv: Record<string, string | undefined>;
let scratchDirs: string[];

beforeEach(() => {
  savedEnv = {};
  for (const key of ENV_KEYS) savedEnv[key] = process.env[key];
  for (const key of ENV_KEYS) delete process.env[key];
  scratchDirs = [];
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  for (const dir of scratchDirs) rmSync(dir, { recursive: true, force: true });
});

function makeRoot(): string {
  const dir = mkdtempSync(join(tmpdir(), 'repo-sync-test-'));
  scratchDirs.push(dir);
  return dir;
}

it('DEFAULT_REPO is identical in both copies', () => {
  expect(TUI_DEFAULT_REPO).toBe(TS_BACKEND_DEFAULT_REPO);
});

describe('the two resolvers agree on the shared precedence steps', () => {
  it('config.json "repo" field', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: 'acme/widgets' }));
    process.env['AF_REPO_ROOT'] = root;
    expect(tuiResolveRepo()).toBe(tsBackendResolveRepo());
    expect(tuiResolveRepo()).toBe('acme/widgets');
  });

  it('GH_REPO env var', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = 'ghrepo/target';
    expect(tuiResolveRepo()).toBe(tsBackendResolveRepo());
  });

  it('_REPO env var', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['_REPO'] = 'underscore/target';
    expect(tuiResolveRepo()).toBe(tsBackendResolveRepo());
  });
});

describe('the two resolvers agree on emptiness handling (D#2520)', () => {
  it('GH_REPO="" is treated as absent by both, not as a literal empty repo', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = '';
    process.env['_REPO'] = 'underscore/target';
    expect(tuiResolveRepo()).toBe(tsBackendResolveRepo());
    expect(tuiResolveRepo()).toBe('underscore/target');
  });

  it('a whitespace-only GH_REPO is treated as absent by both', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = '   ';
    process.env['_REPO'] = 'underscore/target';
    expect(tuiResolveRepo()).toBe(tsBackendResolveRepo());
    expect(tuiResolveRepo()).toBe('underscore/target');
  });

  it('an empty config.json "repo" field is treated as absent by both', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: '' }));
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = 'ghrepo/target';
    expect(tuiResolveRepo()).toBe(tsBackendResolveRepo());
    expect(tuiResolveRepo()).toBe('ghrepo/target');
  });

  it('a whitespace-only config.json "repo" field is treated as absent by both', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: '   ' }));
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = 'ghrepo/target';
    expect(tuiResolveRepo()).toBe(tsBackendResolveRepo());
    expect(tuiResolveRepo()).toBe('ghrepo/target');
  });

  it('the all-empty terminal case resolves to DEFAULT_REPO on both sides, never ""', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = '';
    process.env['_REPO'] = '';
    const tuiRepo = tuiResolveRepo();
    const tsBackendRepo = tsBackendResolveRepo();
    expect(tuiRepo).not.toBe('');
    expect(tsBackendRepo).not.toBe('');
    expect(tuiRepo).toBe(tsBackendRepo);
  });
});

// --- bash and Python also treat whitespace-only as absent (D#2536) ---------

function makeBashRoot(): string {
  const dir = makeRoot();
  mkdirSync(join(dir, 'scripts', 'lib'), { recursive: true });
  copyFileSync(
    join(REPO_ROOT, 'scripts', 'lib', 'repo-resolve.sh'),
    join(dir, 'scripts', 'lib', 'repo-resolve.sh'),
  );
  return dir;
}

/** Source repo-resolve.sh in *root* and call `_resolve_repo`. */
function bashResolveRepo(root: string, env: Record<string, string>): { out: string; status: number } {
  try {
    const out = execFileSync('bash', ['-c', 'source scripts/lib/repo-resolve.sh && _resolve_repo'], {
      cwd: root,
      env: { ...process.env, ...env },
      encoding: 'utf-8',
    });
    return { out: out.trim(), status: 0 };
  } catch (e: unknown) {
    const err = e as { stdout?: string; status?: number };
    return { out: (err.stdout ?? '').trim(), status: typeof err.status === 'number' ? err.status : 1 };
  }
}

function makePythonRoot(): string {
  const dir = makeRoot();
  const backendDir = join(dir, 'backend');
  mkdirSync(backendDir, { recursive: true });
  writeFileSync(join(backendDir, '__init__.py'), '');
  for (const f of ['_repo.py', '_repo_planes.py', '_repo_remote.py']) {
    copyFileSync(join(REPO_ROOT, 'backend', f), join(backendDir, f));
  }
  return dir;
}

/**
 * Import backend._repo in *root* and print REPO. A fresh, private
 * AUTONOMOUS_TEAM_STATE_DIR per call keeps this hermetic — otherwise a
 * default state dir with its own project.json would leak into the result.
 */
function pythonResolveRepo(root: string, env: Record<string, string>): { out: string; status: number } {
  const stateDir = mkdtempSync(join(tmpdir(), 'repo-sync-test-py-state-'));
  scratchDirs.push(stateDir);
  try {
    const out = execFileSync(
      'python3',
      ['-c', "import sys; sys.path.insert(0, '.'); import backend._repo as m; print(m.REPO)"],
      {
        cwd: root,
        env: { ...process.env, ...env, AUTONOMOUS_TEAM_STATE_DIR: stateDir },
        encoding: 'utf-8',
      },
    );
    return { out: out.trim(), status: 0 };
  } catch (e: unknown) {
    const err = e as { status?: number };
    return { out: '', status: typeof err.status === 'number' ? err.status : 1 };
  }
}

describe('bash and Python also treat whitespace-only as absent (D#2536)', () => {
  it('bash: a whitespace-only AUTONOMOUS_TEAM_REPO fails loudly (no config.json present)', () => {
    const root = makeBashRoot();
    const { out, status } = bashResolveRepo(root, { AUTONOMOUS_TEAM_REPO: '   ' });
    expect(status).not.toBe(0);
    expect(out).toBe('');
  });

  it('bash: a whitespace-only AUTONOMOUS_TEAM_REPO falls through to a well-formed config.json "repo"', () => {
    const root = makeBashRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: 'acme/widgets' }));
    const { out, status } = bashResolveRepo(root, { AUTONOMOUS_TEAM_REPO: '   ' });
    expect(status).toBe(0);
    expect(out).toBe('acme/widgets');
  });

  it('python: a whitespace-only AUTONOMOUS_TEAM_REPO fails loudly (no project.json anywhere)', () => {
    const root = makePythonRoot();
    const { status } = pythonResolveRepo(root, { AUTONOMOUS_TEAM_REPO: '   ' });
    expect(status).not.toBe(0);
  });

  it('bash and Python agree: a whitespace-only AUTONOMOUS_TEAM_REPO is never returned as the resolved slug', () => {
    const bashRoot = makeBashRoot();
    const pyRoot = makePythonRoot();
    const bashResult = bashResolveRepo(bashRoot, { AUTONOMOUS_TEAM_REPO: '   ' });
    const pyResult = pythonResolveRepo(pyRoot, { AUTONOMOUS_TEAM_REPO: '   ' });
    // Neither implementation may echo the literal whitespace back as if it
    // were a resolved slug — the pre-fix bug in both languages.
    expect(bashResult.out).not.toBe('   ');
    expect(pyResult.out).not.toBe('   ');
  });

  it('bash and Python agree: a well-formed AUTONOMOUS_TEAM_REPO wins for both', () => {
    const bashRoot = makeBashRoot();
    const pyRoot = makePythonRoot();
    const bashResult = bashResolveRepo(bashRoot, { AUTONOMOUS_TEAM_REPO: 'acme/widgets' });
    const pyResult = pythonResolveRepo(pyRoot, { AUTONOMOUS_TEAM_REPO: 'acme/widgets' });
    expect(bashResult.status).toBe(0);
    expect(pyResult.status).toBe(0);
    expect(bashResult.out).toBe('acme/widgets');
    expect(pyResult.out).toBe('acme/widgets');
  });
});
