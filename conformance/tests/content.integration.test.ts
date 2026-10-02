import { describe, expect, it } from "vitest";
import { actor, eventDates } from "../harness/harness.js";

describe("a dancer's content (integration)", () => {
  it("songs: create, list, read, edit and delete, visible only to their owner", async () => {
    const alice = await actor("alice");
    const bob = await actor("bob");

    const created = await alice.post("/v1/songs", { display_name: "Blue Monday", division: "Classic" });
    expect(created.status).toBe(201);
    const id = created.body.data.id as string;

    expect((await alice.get("/v1/songs")).body.data.map((s: { id: string }) => s.id)).toEqual([id]);
    expect((await bob.get("/v1/songs")).body.data).toEqual([]);
    expect((await bob.get(`/v1/songs/${id}`)).status).toBe(404);

    const edited = await alice.patch(`/v1/songs/${id}`, { display_name: "Blue Tuesday" });
    expect(edited.status).toBe(200);
    expect((await alice.get(`/v1/songs/${id}`)).body.data.display_name).toBe("Blue Tuesday");

    expect((await bob.del(`/v1/songs/${id}`)).status).toBe(404);
    expect((await alice.del(`/v1/songs/${id}`)).status).toBe(204);
    expect((await alice.get("/v1/songs")).body.data).toEqual([]);
  });

  it("event submissions: listed per event and withdrawn by their owner", async () => {
    const admin = await actor("admin", { admin: true });
    const event = await admin.post("/v1/events", { name: "Submissions", ...eventDates() });
    const eventId = event.body.data.id as string;
    const alice = await actor("alice");
    const song = await alice.post("/v1/songs", { display_name: "Tune", division: "Classic" });
    const submitted = await alice.post("/v1/event-song-submissions", { event_id: eventId, song_id: song.body.data.id });
    expect(submitted.status).toBe(201);
    const submissionId = submitted.body.data.id as string;

    const list = await alice.get(`/v1/event-song-submissions?event_id=${eventId}`);
    expect(list.body.data.map((s: { id: string }) => s.id)).toEqual([submissionId]);

    const bob = await actor("bob");
    expect((await bob.del(`/v1/event-song-submissions/${submissionId}`)).status).toBe(404);
    expect((await alice.del(`/v1/event-song-submissions/${submissionId}`)).status).toBe(204);
    expect((await alice.get(`/v1/event-song-submissions?event_id=${eventId}`)).body.data).toEqual([]);
  });

  it("managed partnerships: create, list, edit and delete", async () => {
    const alice = await actor("alice");
    const body = {
      leader_first_name: "Lee",
      leader_last_name: "Leader",
      follower_first_name: "Fay",
      follower_last_name: "Follower",
    };
    const created = await alice.post("/v1/managed-partnerships", body);
    expect(created.status).toBe(201);
    const id = created.body.data.id as string;

    const edited = await alice.patch(`/v1/managed-partnerships/${id}`, { ...body, follower_last_name: "Follows" });
    expect(edited.status).toBe(200);
    const list = await alice.get("/v1/managed-partnerships");
    expect(list.body.data).toHaveLength(1);
    expect(list.body.data[0].follower_last_name).toBe("Follows");

    const bob = await actor("bob");
    expect((await bob.del(`/v1/managed-partnerships/${id}`)).status).toBe(404);
    expect((await alice.del(`/v1/managed-partnerships/${id}`)).status).toBe(204);
    expect((await alice.get("/v1/managed-partnerships")).body.data).toEqual([]);
  });

  it("teams: listed and deleted by their owner", async () => {
    const alice = await actor("alice");
    const team = await alice.post("/v1/teams", { identifier: "Swing Kids" });
    expect((await alice.get("/v1/teams")).body.data.map((t: { id: string }) => t.id)).toEqual([team.body.data.id]);
    const bob = await actor("bob");
    expect((await bob.del(`/v1/teams/${team.body.data.id}`)).status).toBe(404);
    expect((await alice.del(`/v1/teams/${team.body.data.id}`)).status).toBe(204);
    expect((await alice.get("/v1/teams")).body.data).toEqual([]);
  });

  it("a partner's leading pairs and associations", async () => {
    const alice = await actor("alice");
    const partner = await alice.post("/v1/partners", { first_name: "Pat", last_name: "P", partner_role: "follower" });
    const pair = await alice.post("/v1/pairs/find-or-create", { partner_id: partner.body.data.id });

    const leading = await alice.get("/v1/partners/leading-pairs");
    expect(leading.body.data.map((p: { id: string }) => p.id)).toContain(pair.body.data.id);
    const assoc = await alice.get(`/v1/partners/${partner.body.data.id}/associations`);
    expect(assoc.status).toBe(200);

    const bob = await actor("bob");
    expect((await bob.get(`/v1/partners/${partner.body.data.id}/associations`)).status).toBe(404);
  });
});
