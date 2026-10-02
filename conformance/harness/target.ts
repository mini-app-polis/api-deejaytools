/**
 * Which API the integration suite drives.
 *
 * Unset (the default, and what CI does): the app is imported and called
 * in-process. Set INTEGRATION_BASE_URL to an origin, e.g.
 * `http://localhost:8000`, and every request goes over real HTTP to an API
 * started separately — this one, or a reimplementation of it. The suite then
 * acts as a black-box conformance check of that server against this API's
 * route table. What the target must be configured with is in
 * docs/CONFORMANCE.md.
 */
export function integrationBaseUrl(): string | undefined {
  const raw = process.env.INTEGRATION_BASE_URL?.trim();
  if (!raw) return undefined;
  const url = new URL(raw);
  if (url.pathname !== "/" || url.search || url.hash) {
    throw new Error(`INTEGRATION_BASE_URL must be an origin with no path, e.g. http://localhost:8000 (got ${raw})`);
  }
  return url.origin;
}

/** Port for the stand-in Clerk JWKS server. An HTTP target needs a fixed one
 * to be configured with; in-process runs take any free port. */
export function jwksPort(): number {
  const raw = process.env.INTEGRATION_JWKS_PORT?.trim();
  if (raw) {
    const port = Number(raw);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(`INTEGRATION_JWKS_PORT must be a port number (got ${raw})`);
    }
    return port;
  }
  return integrationBaseUrl() ? DEFAULT_HTTP_JWKS_PORT : 0;
}

export const DEFAULT_HTTP_JWKS_PORT = 4455;
