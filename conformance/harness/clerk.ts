import { createHash, type webcrypto } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * A stand-in for Clerk's side of authentication: an RSA key pair, a JWKS
 * endpoint serving its public half, and a signer for session tokens. The API
 * verifies these exactly as it verifies Clerk's — same fetch of the JWKS,
 * same RS256 check, same issuer and expiry rules — so auth is exercised, not
 * mocked.
 *
 * The key server runs once per suite, in the global setup, so its URL is known
 * before any test file loads and can be handed to a separately started API
 * (see harness.ts, INTEGRATION_BASE_URL). The key pair is kept in the OS temp
 * directory and reused across runs, with a kid derived from the public key: a
 * long-running API that cached the JWKS on an earlier run keeps verifying
 * tokens. Delete the file to rotate; the new kid makes a caching verifier
 * fetch again.
 */
export const TEST_ISSUER = "https://clerk.integration.test";

const KEY_FILE = join(tmpdir(), "deejaytools-integration-clerk-key.json");

interface StoredKey {
  kid: string;
  privateJwk: webcrypto.JsonWebKey;
  publicJwk: webcrypto.JsonWebKey;
}

let server: Server | undefined;
let signingKey: Promise<{ kid: string; key: CryptoKey }> | undefined;

function b64url(data: Uint8Array | string): string {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  return Buffer.from(bytes).toString("base64url");
}

/** The suite's key pair: read from the temp file, or generated and saved there. */
async function loadOrCreateKey(): Promise<StoredKey> {
  if (existsSync(KEY_FILE)) {
    return JSON.parse(readFileSync(KEY_FILE, "utf8")) as StoredKey;
  }
  const pair = (await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"]
  )) as { privateKey: CryptoKey; publicKey: CryptoKey };
  const publicJwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  const stored: StoredKey = {
    kid: `integration-${createHash("sha256").update(publicJwk.n ?? "").digest("hex").slice(0, 16)}`,
    privateJwk: await crypto.subtle.exportKey("jwk", pair.privateKey),
    publicJwk,
  };
  writeFileSync(KEY_FILE, JSON.stringify(stored), { mode: 0o600 });
  return stored;
}

/**
 * Start the key server and return its JWKS URL. Called from the global setup.
 * Port 0 picks a free port; a fixed port is for an API started separately,
 * whose JWKS URL has to be configured before the suite runs.
 */
export async function startClerk(port = 0): Promise<string> {
  const { kid, publicJwk } = await loadOrCreateKey();
  const body = JSON.stringify({ keys: [{ ...publicJwk, kid, alg: "RS256", use: "sig" }] });

  const srv = createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(body);
  });
  await new Promise<void>((resolve, reject) => {
    srv.once("error", reject);
    srv.listen(port, "127.0.0.1", resolve);
  });
  server = srv;
  const { port: bound } = srv.address() as AddressInfo;
  return `http://127.0.0.1:${bound}/.well-known/jwks.json`;
}

export async function stopClerk(): Promise<void> {
  const srv = server;
  server = undefined;
  if (srv) await new Promise<void>((resolve) => srv.close(() => resolve()));
}

/** The private key, imported once per process (test files run in workers). */
function key(): Promise<{ kid: string; key: CryptoKey }> {
  signingKey ??= loadOrCreateKey().then(async (stored) => ({
    kid: stored.kid,
    key: await crypto.subtle.importKey(
      "jwk",
      stored.privateJwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["sign"]
    ),
  }));
  return signingKey;
}

/** A session token for `sub`, signed like Clerk's. */
export async function tokenFor(
  sub: string,
  overrides: { iss?: string; expiresInSec?: number } = {}
): Promise<string> {
  const { kid, key: privateKey } = await key();
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: "RS256", kid, typ: "JWT" }));
  const payload = b64url(
    JSON.stringify({
      sub,
      iss: overrides.iss ?? TEST_ISSUER,
      iat: now,
      nbf: now - 5,
      exp: now + (overrides.expiresInSec ?? 300),
    })
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(`${header}.${payload}`))
  );
  return `${header}.${payload}.${b64url(signature)}`;
}
