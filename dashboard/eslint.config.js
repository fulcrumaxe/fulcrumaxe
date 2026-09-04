import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true, checkJS: false },
      ],
      // Prevent raw fetch() calls in dashboard/src outside the HTTP client modules.
      // All network calls must go through apiClient (src/api/client.ts) or
      // jsonRpcClient (src/lib/jsonrpcClient.ts) so they benefit from the 401-retry
      // and auth logic added in PR #1064. To make a new API call, add a method to
      // the appropriate api object in src/api/client.ts — not a bare fetch().
      //
      // Override path: if you genuinely need raw fetch (e.g. a new transport wrapper),
      // add an eslint-disable-next-line comment with a brief justification, OR add
      // the file to the allowlist config block below.
      'no-restricted-syntax': [
        'error',
        {
          selector: "CallExpression[callee.name='fetch']",
          message:
            "Use apiClient or jsonRpc() instead of raw fetch() — see src/api/client.ts. " +
            "Raw fetch bypasses 401-retry and auth logic (PR #1064). " +
            "To override, add eslint-disable-next-line with justification.",
        },
        {
          // D#1602 -> D#1785 -> D#1896: three recurrences of the same bug.
          // A data-testid on an unconditionally-rendered <section> resolves
          // `await findByTestId(...)` before the tile's data has landed, so
          // the synchronous query that follows races the fetch. Put the
          // testid on the element that only renders once data has arrived
          // instead (see CostSpikesTile.tsx or LoopIdleRatioTile.tsx for the
          // pattern) — that turns the existing `await findByTestId` into a
          // genuine data-wait instead of removing it.
          selector: "JSXOpeningElement[name.name='section'] > JSXAttribute[name.name='data-testid']",
          message:
            "Don't put data-testid on a <section> — if the section renders unconditionally, " +
            "the testid resolves before data loads and the next findByTestId/getBy* races " +
            "(D#1896). Move it onto the data-gated element instead. " +
            "To override, add eslint-disable-next-line with justification.",
        },
      ],
    },
  },
  // Allowlist: files that ARE the HTTP client layer and legitimately use fetch().
  // Do not add application components here — fix them to use apiClient instead.
  {
    files: [
      'src/api/client.ts',
      'src/lib/jsonrpcClient.ts',
      'src/sw.ts',
      'src/**/__tests__/**',
    ],
    rules: {
      'no-restricted-syntax': 'off',
    },
  },
  // Known debt (D#1896): these files still put data-testid on an unconditional
  // <section>, same bug shape as the 8 pages/stats tiles that were fixed in the
  // same PR that added this rule. Listed explicitly so the debt is visible
  // rather than silently exempt — adding a NEW file here requires a deliberate
  // edit, it isn't inherited by pattern. Fix a listed file the same way the
  // pages/stats tiles were fixed (move data-testid onto the data-gated
  // element), then remove its line here.
  {
    files: [
      'src/pages/runs/ActiveAgentsTile.tsx',
      'src/pages/runs/StuckRunsTile.tsx',
      'src/pages/runs/SdkVsCcTile.tsx',
      'src/pages/runs/RecentRunsFeedTile.tsx',
      'src/pages/runs/AnalystFindingsTile.tsx',
      'src/pages/runs/DurationPercentilesTile.tsx',
      'src/pages/fleet/FleetConcurrencyTile.tsx',
      'src/pages/fleet/FleetCostTile.tsx',
      'src/pages/fleet/ProjectListTile.tsx',
      'src/pages/kpi/CostChart.tsx',
      'src/pages/kpi/CycleTimeChart.tsx',
      'src/pages/kpi/VelocityChart.tsx',
    ],
    rules: {
      'no-restricted-syntax': ['error', {
        selector: "CallExpression[callee.name='fetch']",
        message:
          "Use apiClient or jsonRpc() instead of raw fetch() — see src/api/client.ts. " +
          "Raw fetch bypasses 401-retry and auth logic (PR #1064). " +
          "To override, add eslint-disable-next-line with justification.",
      }],
    },
  },
)
