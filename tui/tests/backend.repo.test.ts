/**
 * BINDING mutation check (D#2380 Spec item 1): asserts on the composed
 * subprocess environment BackendClient.start() hands to spawn(), not on
 * source text. See the PR description for the red/green transcript produced
 * by temporarily reintroducing the old hardcoded GH_REPO literal against
 * this same test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

class FakeChildProcess extends EventEmitter {
  stdout = new PassThrough();
  stderr = new PassThrough();
  stdin = new PassThrough();
  kill(): void {
    // no-op — nothing was really spawned.
  }
}

const spawnMock = vi.fn(() => new FakeChildProcess());

vi.mock('child_process', () => ({ spawn: spawnMock }));

const { BackendClient } = await import('../src/backend.js');

const ENV_KEYS = ['AF_REPO_ROOT', 'GH_REPO', '_REPO'] as const;
let savedEnv: Record<string, string | undefined>;
let scratchDirs: string[];

beforeEach(() => {
  savedEnv = {};
  for (const key of ENV_KEYS) savedEnv[key] = process.env[key];
  for (const key of ENV_KEYS) delete process.env[key];
  scratchDirs = [];
  spawnMock.mockClear();
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  for (const dir of scratchDirs) rmSync(dir, { recursive: true, force: true });
});

function makeRoot(): string {
  const dir = mkdtempSync(join(tmpdir(), 'backend-repo-test-'));
  scratchDirs.push(dir);
  return dir;
}

describe('BackendClient.start — subprocess environment', () => {
  it('passes the .autonomous-team/config.json-resolved repo slug as GH_REPO, not a hardcoded literal', () => {
    const root = makeRoot();
    mkdirSync(join(root, '.autonomous-team'), { recursive: true });
    writeFileSync(join(root, '.autonomous-team', 'config.json'), JSON.stringify({ repo: 'configured/target' }));
    process.env['AF_REPO_ROOT'] = root;

    const client = new BackendClient();
    client.start();

    expect(spawnMock).toHaveBeenCalledTimes(1);
    const call = spawnMock.mock.calls[0] as unknown as [string, string[], { env?: Record<string, string> }];
    const options = call[2];
    expect(options.env?.['GH_REPO']).toBe('configured/target');
    expect(options.env?.['GH_REPO']).not.toBe('autonomous-agent-7/autonomous-forever');
  });
});
