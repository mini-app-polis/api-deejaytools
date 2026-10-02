import { describe, expect, it } from "vitest";
import { actor, eventDates, request } from "../harness/harness.js";


async function eventWithSession() {
  const admin = await actor("admin", { admin: true });
  const event = await admin.post("/v1/events", { name: "Spring Open", ...eventDates() });
  const now = Date.now();
  const session = await admin.post("/v1/sessions", {
    event_id: event.body.data.id,
    name: "Friday",
    checkin_opens_at: now + 3_600_000,
    floor_trial_starts_at: now + 7_200_000,
    floor_trial_ends_at: now + 10_800_000,
    divisions: [{ division_name: "Classic", is_priority: false }],
  });
  return { admin, eventId: event.body.data.id as string, sessionId: session.body.data.id as string };
}

describe("events and sessions (integration)", () => {
  it("the health check and the operator's tick answer", async () => {
    expect((await request("GET", "/health")).status).toBe(200);
    // Adapted: this service's tick fails closed without TICK_SECRET
    // (deejaytools-api ADR-007), so the operator sends the secret, as the
    // harness's tick() does. Without it, the route refuses.
    const secret = process.env.INTEGRATION_TICK_SECRET;
    const refused = await request("GET", "/internal/tick");
    expect(refused.status).toBe(403);
    const tick = await request("GET", "/internal/tick", {
      headers: secret === undefined ? undefined : { "x-tick-secret": secret },
    });
    expect(tick.status).toBe(200);
    expect(tick.body.data).toEqual({ ticked: true });
  });

  it("events are public to read and admin-only to change", async () => {
    const { admin, eventId } = await eventWithSession();
    const user = await actor("user");

    expect((await request("GET", "/v1/events")).body.data.map((e: { id: string }) => e.id)).toContain(eventId);
    expect((await request("GET", `/v1/events/${eventId}`)).body.data.name).toBe("Spring Open");
    expect((await user.get(`/v1/events/${eventId}/entities`)).status).toBe(200);

    expect((await user.patch(`/v1/events/${eventId}`, { name: "Hijacked" })).status).toBe(403);
    const renamed = await admin.patch(`/v1/events/${eventId}`, { name: "Spring Open 2026" });
    expect(renamed.status).toBe(200);
    expect((await request("GET", `/v1/events/${eventId}`)).body.data.name).toBe("Spring Open 2026");
  });

  it("an admin manages a session's divisions, status and lifetime", async () => {
    const { admin, eventId, sessionId } = await eventWithSession();

    const list = await request("GET", `/v1/sessions?event_id=${eventId}`);
    expect(list.body.data.map((s: { id: string }) => s.id)).toEqual([sessionId]);

    const put = await request("PUT", `/v1/sessions/${sessionId}/divisions`, {
      token: admin.token,
      body: {
        divisions: [
          { division_name: "Classic", is_priority: true, sort_order: 0 },
          { division_name: "Showcase", is_priority: false, sort_order: 1 },
        ],
      },
    });
    expect(put.status).toBe(200);

    // Status is computed from the clock on every read; check-in opens in an
    // hour, so it is "scheduled" — whatever was stored.
    const got = await request("GET", `/v1/sessions/${sessionId}`);
    expect(got.body.data.status).toBe("scheduled");
    expect(got.body.data.divisions.map((d: { division_name: string }) => d.division_name)).toEqual([
      "Classic",
      "Showcase",
    ]);

    // "cancelled" is the one status an admin sets that the clock does not override.
    expect((await admin.patch(`/v1/sessions/${sessionId}/status`, { status: "cancelled" })).status).toBe(200);
    expect((await request("GET", `/v1/sessions/${sessionId}`)).body.data.status).toBe("cancelled");

    expect((await admin.del(`/v1/sessions/${sessionId}`)).status).toBe(200);
    expect((await request("GET", `/v1/sessions/${sessionId}`)).status).toBe(404);
  });
});
