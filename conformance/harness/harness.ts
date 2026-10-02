/* eslint-disable @typescript-eslint/no-explicit-any --
 * Response bodies default to `any` so tests can assert on them field by
 * field; the schemas they are checked against live in the tests. */
import { randomUUID } from "node:crypto";
import postgres from "postgres";
import { tokenFor } from "./clerk.js";
import { integrationDatabaseUrl } from "./database-url.js";
import { recordHit } from "./route-ledger.js";
import { integrationBaseUrl } from "./target.js";

/**
 * Copied from deejaytools-api's src/test/integration/harness.ts and adapted
 * for this repo (deejaytools-api ADR-009, Testing point 3):
 *
 * - HTTP only. There is no in-process app here, so INTEGRATION_BASE_URL is
 *   required, and direct database writes use a plain Postgres client
 *   instead of the Node app's drizzle schema.
 * - Reset spares the identity seed tables and the migration runner's
 *   schema_migrations (ADR-009 harness change 2).
 * - `actor({ admin: true })` also grants deejaytools-admin to the actor's
 *   principal when the identity tables exist (ADR-009 harness change 3).
 */

const target = integrationBaseUrl();
if (!target) {
  throw new Error("INTEGRATION_BASE_URL is required: this suite drives a separately started API over HTTP.");
}
export const baseUrl: string = target;

/** Direct access to the database the target uses. */
export const db = postgres(integrationDatabaseUrl(), { max: 2, onnotice: () => {} });

/** Rows migrations seed and the target relies on: never truncated. */
const SPARED_TABLES = new Set([
  "schema_migrations",
  "identity_issuers",
  "identity_roles",
  "identity_role_scopes",
]);

/** Empty every table but the seeds. Runs before each test. The target's own
 * response cache (3–5 s TTLs) carries over between tests; see CONFORMANCE.md
 * in deejaytools-api. */
export async function resetDatabase(): Promise<void> {
  const rows = await db<{ tablename: string }[]>`
    SELECT tablename FROM pg_tables WHERE schemaname = 'public'`;
  const tables = rows.filter((r) => !SPARED_TABLES.has(r.tablename)).map((r) => `"${r.tablename}"`);
  if (tables.length) {
    await db.unsafe(`TRUNCATE ${tables.join(", ")} RESTART IDENTITY CASCADE`);
  }
}

type Json = Record<string, unknown> | unknown[];

export interface ApiResponse<T = any> {
  status: number;
  body: T;
}

let requestCounter = 0;

/** Call the target over HTTP. Each call gets its own client address so the
 * suite never trips the per-IP rate limit (the target must take the client
 * address from X-Forwarded-For). */
export async function request<T = any>(
  method: string,
  path: string,
  opts: { token?: string | null; body?: Json; form?: FormData; headers?: Record<string, string> } = {}
): Promise<ApiResponse<T>> {
  requestCounter += 1;
  const headers: Record<string, string> = {
    ...opts.headers,
    "x-forwarded-for": `10.${(requestCounter >> 16) & 255}.${(requestCounter >> 8) & 255}.${requestCounter & 255}`,
  };
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  recordHit(method, path);
  // A multipart form sets its own Content-Type, boundary included.
  const init: RequestInit = {
    method,
    headers,
    body: opts.form ?? (opts.body === undefined ? undefined : JSON.stringify(opts.body)),
  };
  const res = await fetch(`${baseUrl}${path}`, init);
  const text = await res.text();
  let body: T;
  try {
    body = (text ? JSON.parse(text) : null) as T;
  } catch {
    throw new Error(`${method} ${path} → ${res.status} with a non-JSON body: ${text.slice(0, 200)}`);
  }
  return { status: res.status, body };
}

/**
 * One scheduler pass (session statuses, queue auto-fill, Drive jobs), through
 * GET /internal/tick. The target runs no background scheduler during tests,
 * so this is the only thing that advances the queue. Sends
 * INTEGRATION_TICK_SECRET as x-tick-secret when set.
 */
export async function tick(): Promise<void> {
  const secret = process.env.INTEGRATION_TICK_SECRET;
  const res = await request("GET", "/internal/tick", {
    headers: secret === undefined ? undefined : { "x-tick-secret": secret },
  });
  if (res.status !== 200) {
    throw new Error(`GET /internal/tick failed: ${res.status} ${JSON.stringify(res.body)}`);
  }
}

/** A signed-in user: synced through the real /auth/sync, optionally made admin. */
export interface Actor {
  id: string;
  email: string;
  token: string;
  get<T = any>(path: string): Promise<ApiResponse<T>>;
  post<T = any>(path: string, body?: Json): Promise<ApiResponse<T>>;
  patch<T = any>(path: string, body?: Json): Promise<ApiResponse<T>>;
  del<T = any>(path: string): Promise<ApiResponse<T>>;
}

export async function actor(
  name: string,
  opts: { admin?: boolean; sync?: boolean } = {}
): Promise<Actor> {
  const id = `user_${name}_${randomUUID().slice(0, 8)}`;
  const email = `${name}.${randomUUID().slice(0, 8)}@example.test`;
  const token = await tokenFor(id);
  const a: Actor = {
    id,
    email,
    token,
    get: (p) => request("GET", p, { token }),
    post: (p, b) => request("POST", p, { token, body: b ?? {} }),
    patch: (p, b) => request("PATCH", p, { token, body: b ?? {} }),
    del: (p) => request("DELETE", p, { token }),
  };
  if (opts.sync !== false) {
    const res = await a.post("/v1/auth/sync", { email, firstName: name, lastName: "Tester" });
    if (res.status !== 200) throw new Error(`sync for ${name} failed: ${res.status} ${JSON.stringify(res.body)}`);
  }
  if (opts.admin) {
    // Admins are promoted in the database, as in every environment: the
    // users.role mirror deejaytools-api reads, and the grant the identity
    // store decides from.
    await db`UPDATE users SET role = 'admin' WHERE id = ${id}`;
    const [{ store }] = await db<{ store: string | null }[]>`
      SELECT to_regclass('public.identity_principal_roles')::text AS store`;
    if (store) {
      await db`
        INSERT INTO identity_principal_roles (principal_id, role_name, granted_by)
        SELECT p.id, 'deejaytools-admin', 'integration_harness'
        FROM identity_principals p WHERE p.subject = ${id}
        ON CONFLICT DO NOTHING`;
    }
  }
  return a;
}

/**
 * Songs normally arrive through the Drive upload flow, which is out of scope
 * here; insert one directly, attached to a partner.
 */
export async function seedSong(userId: string, partnerId: string, division = "Classic"): Promise<string> {
  const id = `song_${randomUUID().slice(0, 8)}`;
  const now = Date.now();
  await db`
    INSERT INTO songs (id, user_id, partner_id, division, display_name, created_at, updated_at)
    VALUES (${id}, ${userId}, ${partnerId}, ${division}, 'Integration Song', ${now}, ${now})`;
  return id;
}

/** The timezone test events are created in (the API's default). */
export const TEST_TIMEZONE = "America/Chicago";

/**
 * Dates for a test event: yesterday through tomorrow in the event's own
 * timezone. The API checks session times against the event's dates in that
 * timezone, so dates built in UTC break every evening in Chicago, when UTC
 * has already reached tomorrow; and a session near now can cross midnight
 * either way.
 */
export function eventDates(): { start_date: string; end_date: string; timezone: string } {
  const day = (offset: number) =>
    new Intl.DateTimeFormat("en-CA", {
      timeZone: TEST_TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(Date.now() + offset * 86_400_000));
  return { start_date: day(-1), end_date: day(1), timezone: TEST_TIMEZONE };
}
