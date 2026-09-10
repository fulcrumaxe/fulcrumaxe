/**
 * Anti-drift test for the deliberate second copy of the precedence chain in
 * tui/src/config/repo.ts (see that file's docstring). Imports both resolvers
 * directly — no build-time link between the two packages is required, only
 * a test-time one — and asserts they agree on the shared part of the chain
 * (config.json "repo" field, GH_REPO, _REPO, and the DEFAULT_REPO constant).
 * The origin-remote step is tui-only (D#2380) and is not compared here.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_REPO as TUI_DEFAULT_REPO, resolveRepo as tuiResolveRepo } from '../../src/config/repo.js';
import {
  DEFAULT_REPO as TS_BACKEND_DEFAULT_REPO,
  resolveRepo as tsBackendResolveRepo,
} from '../../../ts-backend/src/config/repo.ts';

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
