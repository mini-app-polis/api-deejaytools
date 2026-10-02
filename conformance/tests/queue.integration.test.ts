import { describe, expect, it } from "vitest";
import { actor, eventDates, seedSong, tick } from "../harness/harness.js";

const DIVISION = "Classic";

/** An in-progress session with no active slots, and three injected pairs
 * waiting in order: A, B, C. */
async function queueOfThree() {
  const admin = await actor("admin", { admin: true });
  const event = await admin.post("/v1/events", { name: "Queue Test", ...eventDates() });
  const now = Date.now();
  const session = await admin.post("/v1/sessions", {
    event_id: event.body.data.id,
    name: "Queue",
    checkin_opens_at: now - 3_600_000,
    floor_trial_starts_at: now - 60_000,
    floor_trial_ends_at: now + 7_200_000,
    active_priority_max: 0,
    active_non_priority_max: 0,
    divisions: [{ division_name: DIVISION, is_priority: false }],
  });
  const sessionId = session.body.data.id as string;
  for (const name of ["A", "B", "C"]) {
    const res = await admin.post("/v1/admin/checkins", {
      sessionId,
      divisionName: DIVISION,
      leaderFirstName: "Lead",
      leaderLastName: name,
      followerFirstName: "Follow",
      followerLastName: name,
    });
    expect(res.status).toBe(201);
  }
  const waiting = async () =>
    ((await admin.get(`/v1/queue/${sessionId}/waiting`)).body.data as { queueEntryId: string; entityLabel: string }[]);
  const active = async () =>
    ((await admin.get(`/v1/queue/${sessionId}/active`)).body.data as { queueEntryId: string; entityLabel: string }[]);
  return { admin, eventId: event.body.data.id as string, sessionId, waiting, active };
}

const leaders = (rows: { entityLabel: string }[]) => rows.map((r) => r.entityLabel.match(/Lead (\w)/)?.[1]);

describe("queue management (integration)", () => {
  it("test check-ins are listed by session and cleared together", async () => {
    const { admin, sessionId, waiting } = await queueOfThree();
    const list = await admin.get("/v1/admin/checkins/test");
    expect(list.body.data.filter((t: { session_id: string }) => t.session_id === sessionId)).toHaveLength(3);

    const cleared = await admin.del("/v1/admin/checkins/test");
    expect(cleared.status).toBe(200);
    expect(cleared.body.data.deleted).toBe(3);
    expect(await waiting()).toEqual([]);
  });

  it("the waiting queue splits into standard and priority views", async () => {
    const { admin, sessionId } = await queueOfThree();
    expect((await admin.get(`/v1/queue/${sessionId}/non-priority`)).body.data).toHaveLength(3);
    expect((await admin.get(`/v1/queue/${sessionId}/priority`)).body.data).toEqual([]);
  });

  it("move-down swaps an entry with the one below it", async () => {
    const { admin, waiting } = await queueOfThree();
    const [a] = await waiting();
    expect((await admin.post("/v1/queue/move-down", { queueEntryId: a.queueEntryId })).status).toBe(200);
    expect(leaders(await waiting())).toEqual(["B", "A", "C"]);
  });

  it("withdraw removes an entry and closes the gap", async () => {
    const { admin, waiting } = await queueOfThree();
    const [, b] = await waiting();
    expect((await admin.post("/v1/queue/withdraw", { queueEntryId: b.queueEntryId })).status).toBe(200);
    expect(leaders(await waiting())).toEqual(["A", "C"]);
  });

  it("promote puts an entry on the floor ahead of its turn, within the session's slots", async () => {
    const { admin, sessionId, waiting, active } = await queueOfThree();
    const [, , c] = await waiting();
    // No slots open: a live session refuses to exceed its caps.
    expect((await admin.post("/v1/queue/promote", { queueEntryId: c.queueEntryId })).status).toBe(400);

    // The standard cap may not exceed the priority cap, so open both.
    expect(
      (await admin.patch(`/v1/sessions/${sessionId}`, { active_priority_max: 1, active_non_priority_max: 1 })).status
    ).toBe(200);
    expect((await admin.post("/v1/queue/promote", { queueEntryId: c.queueEntryId })).status).toBe(200);
    expect(leaders(await active())).toEqual(["C"]);
    expect(leaders(await waiting())).toEqual(["A", "B"]);
  });

  it("an incomplete run sends the couple to the back of the floor, without recording a run", async () => {
    const { admin, sessionId, active } = await queueOfThree();
    expect(
      (await admin.patch(`/v1/sessions/${sessionId}`, { active_priority_max: 2, active_non_priority_max: 2 })).status
    ).toBe(200);
    await tick();
    expect(leaders(await active())).toEqual(["A", "B"]);

    const [a] = await active();
    const res = await admin.post("/v1/queue/incomplete", { queueEntryId: a.queueEntryId, reason: "music stopped" });
    expect(res.status).toBe(200);
    expect(leaders(await active())).toEqual(["B", "A"]);
    expect((await admin.get(`/v1/runs?session_id=${sessionId}`)).body.data).toEqual([]);
  });

  it("queue actions are admin-only", async () => {
    const { waiting } = await queueOfThree();
    const user = await actor("user");
    const [a] = await waiting();
    expect((await user.post("/v1/queue/promote", { queueEntryId: a.queueEntryId })).status).toBe(403);
    expect((await user.get("/v1/admin/checkins/test")).status).toBe(403);
  });

  it("a dancer withdraws their own check-in, and cannot withdraw someone else's", async () => {
    const admin = await actor("admin", { admin: true });
    const event = await admin.post("/v1/events", { name: "Own", ...eventDates() });
    const now = Date.now();
    const session = await admin.post("/v1/sessions", {
      event_id: event.body.data.id,
      name: "Own",
      checkin_opens_at: now - 3_600_000,
      floor_trial_starts_at: now - 60_000,
      floor_trial_ends_at: now + 7_200_000,
      active_priority_max: 0,
      active_non_priority_max: 0,
      divisions: [{ division_name: DIVISION, is_priority: false }],
    });
    const alice = await actor("alice");
    const partner = await alice.post("/v1/partners", { first_name: "P", last_name: "Q", partner_role: "follower" });
    const pair = await alice.post("/v1/pairs/find-or-create", { partner_id: partner.body.data.id });
    const songId = await seedSong(alice.id, partner.body.data.id, DIVISION);
    await alice.post("/v1/event-song-submissions", { event_id: event.body.data.id, song_id: songId });
    const checkin = await alice.post("/v1/checkins", {
      sessionId: session.body.data.id,
      divisionName: DIVISION,
      entityPairId: pair.body.data.id,
      songId,
    });
    expect(checkin.status).toBe(201);
    const checkinId = checkin.body.data.id as string;

    const mine = await alice.get("/v1/checkins/mine");
    expect(mine.body.data.map((c: { id: string }) => c.id)).toContain(checkinId);

    const bob = await actor("bob");
    expect((await bob.del(`/v1/checkins/${checkinId}`)).status).toBe(403);
    expect((await alice.del(`/v1/checkins/${checkinId}`)).status).toBe(200);
    await tick();
    expect((await admin.get(`/v1/queue/${session.body.data.id}/waiting`)).body.data).toEqual([]);
  });
});
