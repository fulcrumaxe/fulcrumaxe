/**
 * Exercises the real query path (D#2380 Spec item 3): readQueueCountsAsync
 * is the exact function index.tsx calls, unmodified by this test — only
 * child_process.exec underneath it is mocked, so this asserts on the
 * composed command index.tsx would actually hand to `gh`, not on a helper
 * the production code doesn't call.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

type ExecCallback = (error: Error | null, result: { stdout: string; stderr: string }) => void;

const execMock = vi.fn((_command: string, optionsOrCallback: unknown, maybeCallback?: unknown) => {
  const callback = (typeof optionsOrCallback === 'function' ? optionsOrCallback : maybeCallback) as ExecCallback;
  callback(null, {
    stdout: JSON.stringify({ data: { repository: { discussions: { nodes: [] } } } }),
    stderr: '',
  });
});

vi.mock('child_process', () => ({ exec: execMock }));

const { buildDiscussionsQueryCommand, readQueueCountsAsync } = await import('../src/discussionsQuery.js');

const ENV_KEYS = ['AF_REPO_ROOT', 'GH_REPO', '_REPO'] as const;
let savedEnv: Record<string, string | undefined>;
let scratchDirs: string[];

beforeEach(() => {
  savedEnv = {};
  for (const key of ENV_KEYS) savedEnv[key] = process.env[key];
  for (const key of ENV_KEYS) delete process.env[key];
  scratchDirs = [];
  execMock.mockClear();
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  for (const dir of scratchDirs) rmSync(dir, { recursive: true, force: true });
});

function makeRoot(): string {
  const dir = mkdtempSync(join(tmpdir(), 'discussions-query-test-'));
  scratchDirs.push(dir);
  return dir;
}

describe('buildDiscussionsQueryCommand', () => {
  it('targets the given repo in both the --repo flag and the owner:/name: pair', () => {
    const cmd = buildDiscussionsQueryCommand('acme/widgets');
    expect(cmd).toContain('--repo acme/widgets');
    expect(cmd).toContain('owner:"acme"');
    expect(cmd).toContain('name:"widgets"');
  });
});

describe('readQueueCountsAsync — the real query path (BINDING, Spec item 1 and 3)', () => {
  it('composes the exec command from .autonomous-team/config.json, not a hardcoded slug', async () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: 'configured/target' }));
    process.env['AF_REPO_ROOT'] = root;

    await readQueueCountsAsync();

    expect(execMock).toHaveBeenCalledTimes(1);
    const command = execMock.mock.calls[0]?.[0] as string;
    expect(command).toContain('--repo configured/target');
    expect(command).toContain('owner:"configured"');
    expect(command).toContain('name:"target"');
    expect(command).not.toContain('autonomous-forever');
  });
});
