import { describe, expect, it } from "vitest";
import { tokenFor } from "../harness/clerk.js";
import { actor, request } from "../harness/harness.js";

describe("auth (integration)", () => {
  it("sync creates the user and /me returns it", async () => {
    const alice = await actor("alice");
    const me = await alice.get("/v1/auth/me");
    expect(me.status).toBe(200);
    expect(me.body.data).toMatchObject({ id: alice.id, email: alice.email, role: "user" });
  });

  it("re-sync updates the email but keeps the names the user manages", async () => {
    const alice = await actor("alice");
    const renamed = await alice.patch("/v1/auth/me", { firstName: "Alicia", lastName: "Keys" });
    expect(renamed.status).toBe(200);

    const resync = await alice.post("/v1/auth/sync", {
      email: "alice.new@example.test",
      firstName: "Clerk",
      lastName: "Name",
    });
    expect(resync.status).toBe(200);
    expect(resync.body.data).toMatchObject({
      email: "alice.new@example.test",
      first_name: "Alicia",
      last_name: "Keys",
    });
  });

  it("a verified user who has not synced is told to sync", async () => {
    const token = await tokenFor("user_never_synced");
    const res = await request("GET", "/v1/auth/me", { token });
    expect(res.status).toBe(401);
    expect(res.body.error.code).toBe("USER_NOT_SYNCED");
  });

  it.each([
    ["no token", async () => null],
    ["a token from another issuer", async () => tokenFor("user_x", { iss: "https://evil.example" })],
    ["an expired token", async () => tokenFor("user_x", { expiresInSec: -60 })],
    ["a malformed token", async () => "not.a.jwt"],
  ])("rejects %s with the error envelope", async (_label, make) => {
    const res = await request("GET", "/v1/auth/me", { token: await make() });
    expect(res.status).toBe(401);
    expect(res.body.error).toMatchObject({ code: expect.any(String), message: expect.any(String) });
  });

  it("admin endpoints follow the role stored in the database", async () => {
    const user = await actor("user");
    const admin = await actor("admin", { admin: true });
    expect((await user.get("/v1/admin/users")).status).toBe(403);
    const res = await admin.get("/v1/admin/users");
    expect(res.status).toBe(200);
    expect(res.body.data.map((u: { id: string }) => u.id)).toEqual(
      expect.arrayContaining([user.id, admin.id])
    );
  });

  // Rows stay tied to Clerk ids: a new Clerk id whose email another row
  // already holds (a re-created Clerk account) is refused with a reason,
  // not handed the other row's data and not failed as a 500.
  it("refuses a new Clerk id whose email another user already has", async () => {
    const first = await actor("first");
    const second = await actor("second", { sync: false });
    const res = await second.post("/v1/auth/sync", { email: first.email });
    expect(res.status).toBe(409);
    expect(res.body.error.code).toBe("EMAIL_BELONGS_TO_ANOTHER_ACCOUNT");

    // Nothing was created for the new id, and the original is untouched.
    expect((await second.get("/v1/auth/me")).body.error.code).toBe("USER_NOT_SYNCED");
    expect((await first.get("/v1/auth/me")).body.data.email).toBe(first.email);
  });

  it("refuses an email change to an address another user already has", async () => {
    const alice = await actor("alice");
    const bob = await actor("bob");
    const res = await bob.post("/v1/auth/sync", { email: alice.email });
    expect(res.status).toBe(409);
    expect(res.body.error.code).toBe("EMAIL_BELONGS_TO_ANOTHER_ACCOUNT");
    expect((await bob.get("/v1/auth/me")).body.data.email).toBe(bob.email);
  });
});
