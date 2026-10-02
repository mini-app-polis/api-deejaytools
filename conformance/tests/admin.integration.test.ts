import { describe, expect, it } from "vitest";
import { actor, eventDates, request } from "../harness/harness.js";

describe("admin tools (integration)", () => {
  it("an admin changes a user's role, and the new role takes effect", async () => {
    const admin = await actor("admin", { admin: true });
    const user = await actor("user");
    expect((await user.get("/v1/admin/users")).status).toBe(403);

    const res = await admin.patch(`/v1/admin/users/${user.id}/role`, { role: "admin" });
    expect(res.status).toBe(200);
    expect((await user.get("/v1/admin/users")).status).toBe(200);
  });

  it("an admin sees a user's partners and their submissions to an event", async () => {
    const admin = await actor("admin", { admin: true });
    const event = await admin.post("/v1/events", { name: "Admin View", ...eventDates() });
    const alice = await actor("alice");
    await alice.post("/v1/partners", { first_name: "Pat", last_name: "P", partner_role: "follower" });
    const song = await alice.post("/v1/songs", { display_name: "Tune", division: "Classic" });
    await alice.post("/v1/event-song-submissions", { event_id: event.body.data.id, song_id: song.body.data.id });

    expect((await admin.get(`/v1/admin/users/${alice.id}/partners`)).body.data).toHaveLength(1);
    const subs = await admin.get(`/v1/admin/users/${alice.id}/event-song-submissions?event_id=${event.body.data.id}`);
    expect(subs.body.data).toHaveLength(1);
    const all = await admin.get(`/v1/admin/event-song-submissions?event_id=${event.body.data.id}`);
    expect(all.body.data).toHaveLength(1);
    const songs = await admin.get("/v1/admin/songs");
    expect(songs.body.data.map((s: { id: string }) => s.id)).toContain(song.body.data.id);
  });

  it("drive jobs: summary, list, backfill and retry", async () => {
    const admin = await actor("admin", { admin: true });
    expect((await admin.get("/v1/admin/drive-jobs/summary")).status).toBe(200);
    expect((await admin.get("/v1/admin/drive-jobs?status=failed")).body.data).toEqual([]);
    expect((await admin.post("/v1/admin/drive-jobs/backfill-renames")).status).toBe(200);
    expect((await admin.post("/v1/admin/drive-jobs/no-such-job/retry")).status).toBe(404);
  });

  it("anyone can send feedback; a bad screenshot is rejected", async () => {
    const ok = await request("POST", "/v1/feedback", {
      body: { type: "bug", subject: "Queue froze", message: "It stopped updating." },
    });
    expect(ok.status).toBeLessThan(300);
    const bad = await request("POST", "/v1/feedback", {
      body: { type: "bug", subject: "x", message: "y", screenshot: "not-a-data-url" },
    });
    expect(bad.status).toBe(400);
  });
});
