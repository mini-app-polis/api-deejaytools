import { describe, expect, it } from "vitest";
import { actor, eventDates, seedSong, tick, type Actor } from "../harness/harness.js";

const DIVISION = "Classic";

/** An admin-created event with one session whose floor trial is under way.
 * Active slots start at zero so nothing leaves the waiting queue until a
 * test opens one. */
async function openSession(admin: Actor) {
  const event = await admin.post("/v1/events", { name: "Integration Open", ...eventDates() });
  expect(event.status).toBe(201);
  const now = Date.now();
  const session = await admin.post("/v1/sessions", {
    event_id: event.body.data.id,
    name: "Friday floor trial",
    checkin_opens_at: now - 3_600_000,
    floor_trial_starts_at: now - 60_000,
    floor_trial_ends_at: now + 7_200_000,
    active_priority_max: 0,
    active_non_priority_max: 0,
    divisions: [{ division_name: DIVISION, is_priority: false }],
  });
  expect(session.status).toBe(201);
  return { eventId: event.body.data.id as string, sessionId: session.body.data.id as string };
}

/** A dancer with a partner, the pair, and a song attached to that partner
 * and submitted to the event — check-in requires the submission. */
async function dancer(name: string, eventId: string) {
  const user = await actor(name);
  const partner = await user.post("/v1/partners", {
    first_name: `${name}'s`,
    last_name: "Partner",
    partner_role: "follower",
  });
  const pair = await user.post("/v1/pairs/find-or-create", { partner_id: partner.body.data.id });
  const songId = await seedSong(user.id, partner.body.data.id, DIVISION);
  const submitted = await user.post("/v1/event-song-submissions", { event_id: eventId, song_id: songId });
  expect(submitted.status).toBe(201);
  return { user, pairId: pair.body.data.id as string, songId };
}

function checkIn(d: Awaited<ReturnType<typeof dancer>>, sessionId: string) {
  return d.user.post("/v1/checkins", {
    sessionId,
    divisionName: DIVISION,
    entityPairId: d.pairId,
    songId: d.songId,
  });
}

describe("floor trial (integration)", () => {
  it("check-in → waiting → active on the scheduler's tick → completed run", async () => {
    const admin = await actor("admin", { admin: true });
    const { eventId, sessionId } = await openSession(admin);
    const alice = await dancer("alice", eventId);
    const bob = await dancer("bob", eventId);

    expect((await checkIn(alice, sessionId)).status).toBe(201);
    expect((await checkIn(bob, sessionId)).status).toBe(201);

    const waiting = await admin.get(`/v1/queue/${sessionId}/waiting`);
    expect(waiting.status).toBe(200);
    expect(waiting.body.data.map((e: { entityPairId: string }) => e.entityPairId)).toEqual([
      alice.pairId,
      bob.pairId,
    ]);

    // No slots open: a tick moves nobody.
    await tick();
    expect((await admin.get(`/v1/queue/${sessionId}/active`)).body.data).toEqual([]);

    // One slot opens: the first to check in goes on.
    expect(
      (await admin.patch(`/v1/sessions/${sessionId}`, { active_priority_max: 1, active_non_priority_max: 1 })).status
    ).toBe(200);
    await tick();
    const active = await admin.get(`/v1/queue/${sessionId}/active`);
    expect(active.body.data).toHaveLength(1);
    expect(active.body.data[0].entityPairId).toBe(alice.pairId);

    const done = await admin.post("/v1/queue/complete", { queueEntryId: active.body.data[0].queueEntryId });
    expect(done.status).toBe(200);

    const runs = await admin.get(`/v1/runs?session_id=${encodeURIComponent(sessionId)}`);
    expect(runs.status).toBe(200);
    expect(runs.body.data).toHaveLength(1);

    // Completing frees the slot; the next tick brings on the next dancer.
    await tick();
    const next = await admin.get(`/v1/queue/${sessionId}/active`);
    expect(next.body.data.map((e: { entityPairId: string }) => e.entityPairId)).toEqual([bob.pairId]);
  });

  it("the same pair cannot hold two live entries in a session", async () => {
    const admin = await actor("admin", { admin: true });
    const { eventId, sessionId } = await openSession(admin);
    const alice = await dancer("alice", eventId);

    expect((await checkIn(alice, sessionId)).status).toBe(201);
    const dup = await checkIn(alice, sessionId);
    expect(dup.status).toBeGreaterThanOrEqual(400);
    expect(dup.status).toBeLessThan(500);
    expect((await admin.get(`/v1/queue/${sessionId}/waiting`)).body.data).toHaveLength(1);
  });

  it("a dancer cannot check in with someone else's song", async () => {
    const admin = await actor("admin", { admin: true });
    const { eventId, sessionId } = await openSession(admin);
    const alice = await dancer("alice", eventId);
    const bob = await dancer("bob", eventId);

    const res = await bob.user.post("/v1/checkins", {
      sessionId,
      divisionName: DIVISION,
      entityPairId: bob.pairId,
      songId: alice.songId,
    });
    expect(res.status).toBe(404);
  });

  it("submitting the same song to an event twice is a 409 conflict", async () => {
    const admin = await actor("admin", { admin: true });
    const { eventId } = await openSession(admin);
    const alice = await dancer("alice", eventId);
    const again = await alice.user.post("/v1/event-song-submissions", { event_id: eventId, song_id: alice.songId });
    expect(again.status).toBe(409);
    expect(again.body.error.code).toBe("conflict");
  });

  it("deleting an event removes its sessions, check-ins and queue entries", async () => {
    const admin = await actor("admin", { admin: true });
    const { eventId, sessionId } = await openSession(admin);
    const alice = await dancer("alice", eventId);
    expect((await checkIn(alice, sessionId)).status).toBe(201);

    const del = await admin.del(`/v1/events/${eventId}`);
    expect(del.status).toBe(200);
    expect((await admin.get(`/v1/sessions/${sessionId}`)).status).toBe(404);
    expect((await alice.user.get("/v1/checkins/mine")).body.data).toEqual([]);
  });
});
