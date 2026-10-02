import { defineConfig } from "vitest/config";

/**
 * The conformance suite: deejaytools-api's integration tests, driving this
 * service over HTTP against a real Postgres, with Clerk-style tokens from a
 * local key server. Files run one at a time because they share the database.
 */
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.integration.test.ts"],
    globalSetup: ["harness/global-setup.ts"],
    setupFiles: ["harness/setup.ts"],
    fileParallelism: false,
    testTimeout: 30_000,
    hookTimeout: 60_000,
  },
});
