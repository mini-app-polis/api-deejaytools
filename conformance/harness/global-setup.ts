import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { drizzle } from "drizzle-orm/postgres-js";
import { migrate } from "drizzle-orm/postgres-js/migrator";
import postgres from "postgres";
import type { TestProject } from "vitest/node";
import { startClerk, stopClerk } from "./clerk.js";
import { integrationDatabaseUrl } from "./database-url.js";
import { ledgerProblems, useLedgerDir } from "./route-ledger.js";
import { integrationBaseUrl, jwksPort } from "./target.js";

declare module "vitest" {
  export interface ProvidedContext {
    routeLedgerDir: string;
    clerkJwksUrl: string;
  }
}

/**
 * Builds the schema the way production has it: from an empty database, by
 * applying every migration in ./drizzle (deejaytools-api's, frozen), then
 * the target's post-baseline migrations from INTEGRATION_EXTRA_MIGRATIONS
 * (ADR-009 harness change 1), so the identity tables and their seed rows
 * exist. That also makes every conformance run a check that the target's
 * baseline and drizzle agree (ADR-008 point 7).
 *
 * Starts the stand-in Clerk key server for the whole run (clerk.ts), and
 * checks that the target answers before any test runs.
 *
 * Once every test has run, checks the route ledger (route-ledger.ts).
 */
export default async function setup(project: TestProject): Promise<() => Promise<void>> {
  const dir = mkdtempSync(join(tmpdir(), "route-ledger-"));
  useLedgerDir(dir);
  project.provide("routeLedgerDir", dir);

  const client = postgres(integrationDatabaseUrl(), { max: 1, onnotice: () => {} });
  try {
    await client.unsafe("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public; DROP SCHEMA IF EXISTS drizzle CASCADE;");
    await migrate(drizzle(client), { migrationsFolder: "./drizzle" });
    await applyExtraMigrations(client);
  } finally {
    await client.end();
  }

  const jwksUrl = await startClerk(jwksPort());
  project.provide("clerkJwksUrl", jwksUrl);

  const target = integrationBaseUrl();
  if (!target) {
    await stopClerk();
    throw new Error("INTEGRATION_BASE_URL is required: this suite drives a separately started API over HTTP.");
  }
  await checkTarget(target, jwksUrl);

  return async () => {
    await stopClerk();
    const problems = ledgerProblems();
    rmSync(dir, { recursive: true, force: true });
    if (problems.length) {
      throw new Error(`Route ledger:\n  ${problems.join("\n  ")}`);
    }
  };
}

/**
 * The target's migrations that come after its baseline, in numeric order,
 * each in its own transaction. The baseline (001) is what ./drizzle just
 * built, so it is skipped, as production's bootstrap skips it.
 */
async function applyExtraMigrations(client: postgres.Sql): Promise<void> {
  const raw = process.env.INTEGRATION_EXTRA_MIGRATIONS?.trim();
  if (!raw) return;
  const dir = resolve(raw);
  const files = readdirSync(dir)
    .filter((f) => /^\d{3,}_.+\.sql$/.test(f) && !f.startsWith("001_"))
    .sort((a, b) => parseInt(a, 10) - parseInt(b, 10));
  for (const file of files) {
    const sql = readFileSync(join(dir, file), "utf8");
    await client.begin((tx) => tx.unsafe(sql));
  }
  console.info(`[integration] applied ${files.length} extra migration(s) from ${dir}: ${files.join(", ")}`);
}

/** Fail fast, with the setup it needs, when the target API is not there. */
async function checkTarget(target: string, jwksUrl: string): Promise<void> {
  let status: number | string;
  try {
    status = (await fetch(`${target}/health`, { signal: AbortSignal.timeout(5_000) })).status;
  } catch (err) {
    status = err instanceof Error ? err.message : String(err);
  }
  if (status !== 200) {
    await stopClerk();
    throw new Error(
      `INTEGRATION_BASE_URL=${target}: GET /health did not return 200 (${status}). ` +
        "Start the API first, pointed at the same _test database, with " +
        `DEEJAYTOOLS_CLERK_JWKS_URL=${jwksUrl}, the test issuer and no background scheduler. ` +
        "See conformance/README.md."
    );
  }
  console.info(`[integration] driving ${target} over HTTP; JWKS at ${jwksUrl}`);
}
