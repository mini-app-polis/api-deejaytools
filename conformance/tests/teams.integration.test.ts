import { describe, expect, it } from "vitest";
import { actor } from "../harness/harness.js";

describe("teams (integration)", () => {
  it("a duplicate team name is a 409 conflict, not a server error", async () => {
    const alice = await actor("alice");
    expect((await alice.post("/v1/teams", { identifier: "Swing Kids" })).status).toBe(201);

    const dup = await alice.post("/v1/teams", { identifier: "Swing Kids" });
    expect(dup.status).toBe(409);
    expect(dup.body.error.code).toBe("conflict");

    const other = await alice.post("/v1/teams", { identifier: "Other" });
    const rename = await alice.patch(`/v1/teams/${other.body.data.id}`, { identifier: "Swing Kids" });
    expect(rename.status).toBe(409);
  });

  it("different users may use the same team name", async () => {
    const alice = await actor("alice");
    const bob = await actor("bob");
    expect((await alice.post("/v1/teams", { identifier: "Swing Kids" })).status).toBe(201);
    expect((await bob.post("/v1/teams", { identifier: "Swing Kids" })).status).toBe(201);
  });
});
