import { randomUUID } from "node:crypto";
import { describe, expect, it } from "vitest";
import { actor, request, type Actor } from "../harness/harness.js";

/**
 * The chunked song upload through the API: multipart parsing, chunks written
 * to disk and reassembled, format checks, the song row, and cleanup when the
 * background Drive upload fails.
 *
 * Copied from deejaytools-api and adapted for HTTP only: the two tests that
 * swap the Drive calls for in-process stand-ins (the success path, and the
 * event copy queued after an in-flight upload lands) cannot reach another
 * process and are not here. The service's own pytest suite covers that path
 * against a fake Drive. The rest hold because the target runs without Drive
 * credentials, so every Drive call fails for real.
 */

/** A small file that passes the MP3 check: an empty ID3v2.4 header, then a
 * recognisable payload standing in for the audio frames. */
const AUDIO_PAYLOAD = Buffer.from(`audio-frames-${"x".repeat(3000)}-end`);
const MP3 = Buffer.concat([Buffer.from([0x49, 0x44, 0x33, 0x04, 0, 0, 0, 0, 0, 0]), AUDIO_PAYLOAD]);

function sendChunk(
  user: Actor,
  opts: { uploadId: string; index: number; total: number; bytes: Buffer; extra?: Record<string, string> }
) {
  const form = new FormData();
  form.set("upload_id", opts.uploadId);
  form.set("chunk_index", String(opts.index));
  form.set("total_chunks", String(opts.total));
  form.set("original_filename", "my routine.mp3");
  form.set("mime_type", "audio/mpeg");
  form.set("division", "Classic");
  for (const [k, v] of Object.entries(opts.extra ?? {})) form.set(k, v);
  form.set("chunk", new File([new Uint8Array(opts.bytes)], "chunk"));
  return request("POST", "/v1/songs/upload/chunk", { token: user.token, form });
}

/** The background upload runs after the response; wait for its outcome. */
async function eventually<T>(read: () => Promise<T>, done: (v: T) => boolean): Promise<T> {
  const deadline = Date.now() + 10_000;
  for (;;) {
    const v = await read();
    if (done(v) || Date.now() > deadline) return v;
    await new Promise((r) => setTimeout(r, 100));
  }
}

describe("song upload (integration)", () => {
  it("removes the song again when the Drive upload fails, so no broken entry is left behind", async () => {
    const alice = await actor("alice");
    const uploadId = randomUUID();
    const last = await sendChunk(alice, { uploadId, index: 0, total: 1, bytes: MP3 });
    expect(last.status).toBe(200);
    const songId = last.body.data.song.id as string;

    const gone = await eventually(
      () => alice.get(`/v1/songs/${songId}`),
      (r) => r.status === 404
    );
    expect(gone.status).toBe(404);
  });

  it("refuses an upload with a missing chunk, a non-audio file, or a malformed upload id", async () => {
    const alice = await actor("alice");

    const gappy = randomUUID();
    await sendChunk(alice, { uploadId: gappy, index: 0, total: 3, bytes: MP3.subarray(0, 100) });
    const missing = await sendChunk(alice, { uploadId: gappy, index: 2, total: 3, bytes: MP3.subarray(200) });
    expect(missing.status).toBe(409);
    expect(missing.body.error.code).toBe("CHUNK_MISSING");

    const notAudio = await sendChunk(alice, {
      uploadId: randomUUID(),
      index: 0,
      total: 1,
      bytes: Buffer.from("this is a text file, not a song"),
    });
    expect(notAudio.status).toBe(400);
    expect(notAudio.body.error.code).toBe("UNSUPPORTED_FORMAT");

    const bad = await sendChunk(alice, { uploadId: "not-a-uuid", index: 0, total: 1, bytes: MP3 });
    expect(bad.status).toBe(400);
  });

  it("a partner that isn't yours is refused", async () => {
    const alice = await actor("alice");
    const bob = await actor("bob");
    const bobsPartner = await bob.post("/v1/partners", { first_name: "B", last_name: "P", partner_role: "follower" });
    const res = await sendChunk(alice, {
      uploadId: randomUUID(),
      index: 0,
      total: 1,
      bytes: MP3,
      extra: { partner_id: bobsPartner.body.data.id },
    });
    expect(res.status).toBe(400);
  });
});
