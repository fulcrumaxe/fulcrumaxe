import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_REPO, repoName, repoOwner, resolveRepo } from '../../src/config/repo.js';

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
  const dir = mkdtempSync(join(tmpdir(), 'tui-repo-test-'));
  scratchDirs.push(dir);
  return dir;
}

describe('resolveRepo precedence', () => {
  it('resolves from .autonomous-team/config.json "repo" field first', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: 'acme/widgets' }));
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = 'should-not-win/anything';
    expect(resolveRepo()).toBe('acme/widgets');
  });

  it('falls back to GH_REPO when there is no config.json', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = 'ghrepo/target';
    expect(resolveRepo()).toBe('ghrepo/target');
  });

  it('falls back to _REPO when GH_REPO is unset', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['_REPO'] = 'underscore/target';
    expect(resolveRepo()).toBe('underscore/target');
  });

  it('falls back to the origin remote in .git/config when nothing else is set — the adopter-clone case (AC 2)', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.git'), { recursive: true });
    writeFileSync(
      join(root, '.git', 'config'),
      '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = https://github.com/forker/theirfork.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    );
    process.env['AF_REPO_ROOT'] = root;
    expect(resolveRepo()).toBe('forker/theirfork');
  });

  it('parses an scp-like origin URL (git@host:owner/name.git)', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.git'), { recursive: true });
    writeFileSync(join(root, '.git', 'config'), '[remote "origin"]\n\turl = git@github.com:forker/theirfork.git\n');
    process.env['AF_REPO_ROOT'] = root;
    expect(resolveRepo()).toBe('forker/theirfork');
  });

  it('falls back to DEFAULT_REPO when config, env, and origin are all absent', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    expect(resolveRepo()).toBe(DEFAULT_REPO);
  });
});

describe('resolveRepo treats empty-but-defined values as absent (D#2380 code review)', () => {
  it('GH_REPO="" falls through to _REPO, not an empty string', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = '';
    process.env['_REPO'] = 'underscore/target';
    expect(resolveRepo()).toBe('underscore/target');
  });

  it('GH_REPO="" and _REPO="" both fall through to the origin remote', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.git'), { recursive: true });
    writeFileSync(join(root, '.git', 'config'), '[remote "origin"]\n\turl = https://github.com/forker/theirfork.git\n');
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = '';
    process.env['_REPO'] = '';
    expect(resolveRepo()).toBe('forker/theirfork');
  });

  it('a whitespace-only GH_REPO is treated the same as an empty one', () => {
    const root = makeRoot();
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = '   ';
    process.env['_REPO'] = 'underscore/target';
    expect(resolveRepo()).toBe('underscore/target');
  });

  it('a whitespace-only config.json "repo" field falls through to GH_REPO', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: '   ' }));
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = 'ghrepo/target';
    expect(resolveRepo()).toBe('ghrepo/target');
  });

  it('an origin remote that parses to an empty owner or name does not count as resolved', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.git'), { recursive: true });
    // "https://github.com//repo.git" parses to owner="" name="repo" via slugFromUrl —
    // must not be treated as a resolved slug.
    writeFileSync(join(root, '.git', 'config'), '[remote "origin"]\n\turl = https://github.com//repo.git\n');
    process.env['AF_REPO_ROOT'] = root;
    expect(resolveRepo()).toBe(DEFAULT_REPO);
  });

  it('the all-sources-empty terminal case: config, GH_REPO, _REPO all empty strings and no origin remote resolves to DEFAULT_REPO, never ""', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: '' }));
    process.env['AF_REPO_ROOT'] = root;
    process.env['GH_REPO'] = '';
    process.env['_REPO'] = '';
    const repo = resolveRepo();
    expect(repo).not.toBe('');
    expect(repo).toBe(DEFAULT_REPO);
  });
});

describe('repoOwner / repoName', () => {
  it('splits a slug into owner and name', () => {
    expect(repoOwner('acme/widgets')).toBe('acme');
    expect(repoName('acme/widgets')).toBe('widgets');
  });
});
