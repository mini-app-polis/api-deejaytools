import { describe, expect, it } from "vitest";
import { actor } from "../harness/harness.js";

describe("partners and pairs (integration)", () => {
  it("a user's partners are invisible and untouchable to other users", async () => {
    const alice = await actor("alice");
    const bob = await actor("bob");

    const created = await alice.post("/v1/partners", {
      first_name: "Pat",
      last_name: "Partner",
      partner_role: "follower",
    });
    expect(created.status).toBe(201);
    const id = created.body.data.id as string;

    expect((await alice.get(`/v1/partners/${id}`)).status).toBe(200);
    expect((await bob.get("/v1/partners")).body.data).toEqual([]);
    expect((await bob.get(`/v1/partners/${id}`)).status).toBe(404);
    expect((await bob.patch(`/v1/partners/${id}`, { first_name: "Mallory" })).status).toBe(404);
    expect((await bob.del(`/v1/partners/${id}`)).status).toBe(404);

    const after = await alice.get(`/v1/partners/${id}`);
    expect(after.body.data.first_name).toBe("Pat");
  });

  it("find-or-create returns the same pair every time", async () => {
    const alice = await actor("alice");
    const partner = await alice.post("/v1/partners", {
      first_name: "Pat",
      last_name: "Partner",
      partner_role: "follower",
    });
    const first = await alice.post("/v1/pairs/find-or-create", { partner_id: partner.body.data.id });
    const again = await alice.post("/v1/pairs/find-or-create", { partner_id: partner.body.data.id });
    expect(first.status).toBe(201);
    expect(again.status).toBe(200);
    expect(again.body.data.id).toBe(first.body.data.id);
  });

  it("a user cannot pair with someone else's partner", async () => {
    const alice = await actor("alice");
    const bob = await actor("bob");
    const partner = await alice.post("/v1/partners", {
      first_name: "Pat",
      last_name: "Partner",
      partner_role: "follower",
    });
    const res = await bob.post("/v1/pairs/find-or-create", { partner_id: partner.body.data.id });
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(res.status).toBeLessThan(500);
  });
});
