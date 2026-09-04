// Stub vitest config — enables coverage thresholds when vitest is adopted.
// Tests don't exist yet; this file configures the expected thresholds so they're
// enforced automatically once tests are written.
//
// To activate: npm install -D vitest @vitest/coverage-v8, then run: npx vitest run

import type { UserConfig } from "vitest/config";

const config: UserConfig = {
  test: {
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      thresholds: {
        lines: 80,
        branches: 80,
        functions: 80,
        statements: 80,
      },
      exclude: [
        "dist/**",
        "**/*.d.ts",
        "vitest.config.ts",
      ],
    },
  },
};

export default config;
