/**
 * The integration suite truncates every table before each test, so it must
 * never be pointed at a database anyone cares about. It refuses to run unless
 * the database URL names a database whose name ends in `_test`, on a local
 * host (the CI service container, or a developer's own machine).
 *
 * Resolved in the same order as src/db/index.ts, so the URL checked here is
 * the URL the app connects to.
 */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "postgres"]);

export function integrationDatabaseUrl(): string {
  const raw = process.env.DEEJAYTOOLS_DATABASE_URL || process.env.DATABASE_URL;
  if (!raw) {
    throw new Error(
      "DEEJAYTOOLS_DATABASE_URL (or DATABASE_URL) is required for the integration suite, e.g. postgres://postgres:postgres@localhost:5432/deejaytools_test"
    );
  }
  const url = new URL(raw);
  const name = url.pathname.replace(/^\//, "");
  if (!LOCAL_HOSTS.has(url.hostname) || !name.endsWith("_test")) {
    throw new Error(
      `Refusing to run the integration suite against ${url.hostname}/${name}: it truncates every table. ` +
        "Use a local database whose name ends in _test."
    );
  }
  return raw;
}
