import { afterAll, beforeEach, inject } from "vitest";
import { integrationDatabaseUrl } from "./database-url.js";

// Guarded first: never open a connection to a database the suite is not
// allowed to truncate.
integrationDatabaseUrl();

const { db, resetDatabase } = await import("./harness.js");
const { useLedgerDir } = await import("./route-ledger.js");
useLedgerDir(inject("routeLedgerDir"));

beforeEach(async () => {
  await resetDatabase();
});

afterAll(async () => {
  await db.end();
});
