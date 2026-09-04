import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    // E2E specs require a running browser + server — exclude from unit test run
    exclude: ['e2e/**', '**/node_modules/**'],
    // Memory guardrails (D#1897, following up on 2026-05-12's single-fork pin):
    //
    // 1. Multiple agents may run vitest concurrently from their worktrees.
    //    The default threads-pool can spawn CPU-count workers per run, and
    //    6 parallel runs will OOM-kill the host (observed 2026-05-12: crashed
    //    Chrome + terminals). `pool: 'forks'` avoids that regardless of fork
    //    count — forks never spawn CPU-count workers the way threads do.
    //
    // 2. Running the full ~460-test suite inside a single fork (singleFork:
    //    true, the original pin) hits Node's default ~2GB old-space ceiling
    //    on its own, independent of host RAM — jsdom environments for each
    //    test file accumulate without being reclaimed across ~40 files in one
    //    process, and V8's mark-compact eventually gives up (verified: fatal
    //    OOM around file 13/41 with `singleFork: true`). Splitting the suite
    //    across a small, bounded number of forks resets that per-process
    //    accumulation periodically, which is why `maxForks: 2` completes the
    //    full suite cleanly where `singleFork: true` cannot.
    //
    // (Separately, D#1897 found and fixed the actual cause of the 379/463
    // correctness failures that this OOM guard had nothing to do with: a test
    // in client.test.ts stubbed `globalThis.window` and never restored it,
    // corrupting every test file that ran afterward in the same process.)
    pool: 'forks',
    poolOptions: {
      forks: {
        singleFork: false,
        minForks: 1,
        maxForks: 2,
      },
    },
    maxConcurrency: 1,
  },
})
