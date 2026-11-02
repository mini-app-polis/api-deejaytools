# Operations

Procedures for running this service.

## Retrying failed song builds

A build is retried with backoff and marked `failed` after 10 attempts,
reported to Sentry as `song_build_exhausted`, with the last error in
`song_uploads.last_error`. Once the cause is fixed (Drive credentials, a
quota), put the rows back in the queue; the next tick picks them up:

```sql
-- One song
UPDATE song_uploads
SET status = 'pending', attempts = 0, next_attempt_at = 0,
    claim_id = NULL, last_error = NULL
WHERE song_id = '<song id>' AND status = 'failed';

-- Every failed build
UPDATE song_uploads
SET status = 'pending', attempts = 0, next_attempt_at = 0,
    claim_id = NULL, last_error = NULL
WHERE status = 'failed';
```

A retried build reuses a file it already uploaded (it finds it by its
`deejaytools_song_id` appProperty), so a retry never uploads twice.

## Allowed origins

Allowed origins come from `DEEJAYTOOLS_CORS_ORIGINS` (comma-separated) only;
the unprefixed `CORS_ORIGINS` is not read. Without `DEEJAYTOOLS_CORS_ORIGINS`
the service allows only `http://localhost:5173`, so the web app's requests
fail in the browser.

## Workers and database connections

The container runs `WEB_CONCURRENCY` uvicorn workers, 4 by default: one per
CPU of the 4 Railway allows (`.railway/railway.ts`). Set `WEB_CONCURRENCY` on
the service to change it. Each worker is its own process with its own
database engine, response cache and rate-limit windows, and its own
scheduler (safe to run more than once).

Each engine holds at most `pool_size` 8 + `max_overflow` 4 = 12
connections (`src/api_deejaytools/database.py`), and each worker also holds
one connection of its own for the cache bus (below), so 4 workers use at
most 52. Railway Postgres allows 100 connections by default, which leaves
about 50 for the crons, migrations (one connection at deploy) and operator
sessions. Keep workers x 13 at or below 60 against a 100-connection limit:
if you raise `WEB_CONCURRENCY`, lower the pool in `database.py` to match, and
if the database's limit changes, recompute the same sum.

## Response cache across workers

Each worker caches the hot public reads (`src/api_deejaytools/cache.py`:
queue reads 3 s, session reads 5 s) as the JSON bytes it sends, so a hit is
sent without being serialised again. Concurrent misses for one key run one
database read between them; one that fails is not kept, and one that a
write invalidates while it reads answers the requests already waiting but
is not stored. A write drops the affected keys in its
own worker and sends a Postgres `NOTIFY` on the `deejaytools_cache` channel;
every other worker `LISTEN`s on its cache-bus connection
(`src/api_deejaytools/cache_bus.py`, `application_name`
`api-deejaytools-cache-bus` in `pg_stat_activity`) and drops the same keys,
within milliseconds.

If a worker's cache-bus connection drops, it logs `cache_bus: connection
lost`, serves its cache on the TTL alone, and reconnects with backoff; on
reconnecting it drops its whole cache, since it may have missed
notifications. Admins and event staff read `GET /v1/sessions` fresh either
way, so a session they create or delete shows at once.

## Shared caching at Cloudflare

The public reads that are the same for every caller send `Cache-Control:
public, max-age=0, s-maxage=<TTL>, stale-while-revalidate=<TTL>`, with the
server cache's TTL, to an anonymous request, and `private, no-store` to a
request that carries `Authorization` (docs/API.md, "Shared caching").
Cloudflare bypasses its cache for a request with `Authorization`, so admins
and event staff still read the session list fresh.

The headers say what may be held; whether Cloudflare holds it is the zone's
configuration. Cloudflare does not cache JSON by default: a Cache Rule in
the infra repo has to make these paths eligible, respecting the origin's
`Cache-Control`. Its cache key has to include the request's `Origin`: the
API answers CORS per origin (`Access-Control-Allow-Origin` and `Vary:
Origin`), and Cloudflare does not vary on `Origin` by itself, so one
origin's cached answer would otherwise reach another, or a request without
`Origin` would cache an answer the browser refuses. A write does not purge
Cloudflare: a cached read can lag a write by `s-maxage` plus
`stale-while-revalidate`.

## Rate limit

The capacity target this has to fit, a whole event behind one venue
address, is in [ADR-0013](decisions/ADR-0013-event-capacity-target.md).

`RateLimitMiddleware` in `src/api_deejaytools/middleware.py` allows
`RATE_LIMIT` requests a minute on `/v1/*` per caller, and a lower limit of
its own on the paths in `PATH_RATE_LIMITS` (`POST /v1/feedback`), counted
per caller as well. Each worker keeps its own windows. The key for a
request is:

1. **A signed-in request** (an `Authorization: Bearer` token that verifies
   against the configured Clerk issuer): the verified user, issuer and
   subject. The token is verified in the middleware, and the route's auth
   reuses that outcome rather than verifying again
   (`auth.verified_credential`), so it costs one verification per request
   as before. A token that does not verify, forged, expired or from another
   issuer, counts as anonymous: a made-up subject never gets a window of
   its own.
2. **An anonymous `GET` of a public cached read** (`PUBLIC_READS`: the
   division list, the session list and one session, a queue's active and
   waiting lists): not counted. A whole event polls these from one address;
   the response cache keeps them off the database, not a limit. A signed-in
   caller's reads of them count against the user.
3. **Any other anonymous request**: the client address (below).

`/health`, `/version` and `/internal/tick` are outside `/v1/` and never
limited.

## Client address

An anonymous request's rate limit keys on the client's address, and
`POST /v1/feedback` is public, so the key must be one the client cannot
choose. Both deployed environments take the same path:
`api.deejaytools.com` and `api-dev.deejaytools.com` are proxied by
Cloudflare (`proxied = true` on their DNS records in the infra repo's
`cloudflare_dns.tf`), which forwards to Railway's edge, which forwards to
the app. Railway's edge is also reachable directly, without Cloudflare.

`client_address()` in `src/api_deejaytools/middleware.py` reads it this way:

1. The peer is the **last** `X-Forwarded-For` hop: Railway's edge appends
   the address that connected to it. Earlier hops are whatever the client
   sent, and are never used.
2. If the peer is a Cloudflare edge (in `CLOUDFLARE_NETWORKS`, Cloudflare's
   published ranges), the key is `cf-connecting-ip`, which Cloudflare sets
   itself.
3. Otherwise the key is the peer. A request sent straight to Railway with
   its own `cf-connecting-ip` is keyed on its real address.
4. Without `X-Forwarded-For` (a local run with no proxy in front), every
   request shares the key `unknown`.

Requests a Cloudflare Worker sends to the origin come from a Cloudflare
edge with a `cf-connecting-ip` Cloudflare sets for Workers, so they share
one window. If Cloudflare adds ranges, update `CLOUDFLARE_NETWORKS`:
until then, clients reaching an edge in a new range share that edge's
window rather than getting one each. If the API ever stops being proxied
by Cloudflare, nothing needs changing: no peer is a Cloudflare edge, and
the key is the address Railway saw.

## Capacity

The target is ADR-0013's: an event of 3,000 dancers on their own phones
behind one venue address. deejaytools-com's `load/event.k6.js` (its
`load/README.md` says how to run it) checks it against the development API
from one address.

### Scenario

- **Steady:** 3,000 phones on one session's page, a third signed in,
  arriving over 3 minutes. Each loads the page once, then polls the
  session and its active and waiting queues every 60 s, anonymously, from a
  random start; a quarter of them regain focus each minute and refetch.
- **Reload burst:** 1,000 page loads in 10 s. A page load is `/version`,
  the session and its two queues, anonymous; signed in, also `auth/me` and
  `checkins/mine` with the token. That is 4 to 6 requests, fewer than the
  ADR's estimate of about 10.
- **Check-in rush:** 300 check-ins through `POST /v1/checkins` in the first
  minute the session's check-in window is open, each with the check-in
  dialog's reads before it and the page's refetches after it.

Pass: p95 under 500 ms for reads and under 2 s for check-ins, no 5xx, and
no 429 or Cloudflare block that a real event would also get. The test has
only three users, so per-user refusals caused by sharing them are counted
apart.

### Sizing, dev and prod

| | dev | prod |
| --- | --- | --- |
| API service | 1 Railway replica, 4 vCPU / 4 GB limit | 1 Railway replica, 4 vCPU / 4 GB limit |
| uvicorn workers | `WEB_CONCURRENCY`, default 4 | same |
| Database connections | `pool_size` 8 + `max_overflow` 4 per worker, plus 1 cache-bus connection each (at most 52) | same |
| Response cache | queues 3 s, sessions 5 s, shared across workers | same |
| Cloudflare | edge cache on the anonymous public reads; a per-IP ceiling on everything else (infra `cloudflare_rules.tf`) | same rules |

### Results

Run 2026-11-02 against dev at api-deejaytools 4e4abfa, deejaytools-com
717d392, from one address on a home connection.

| Measure | Result | Pass? |
| --- | --- | --- |
| Requests | 118,242 in 15.6 min; the API saw about 14,900 | |
| Edge cache (session, active, waiting) | 102,500 HIT, 1,356 EXPIRED, 18 MISS: about 99% answered at the edge | |
| Steady reads p95 | 153 ms | yes |
| Rush reads p95 | 542 ms | no, see below |
| Reload reads p95 | 10.2 s | no, see below |
| Check-in p95 | 1.07 s (221 placed) | yes |
| 5xx | 0 | yes |
| Cloudflare blocks | 0 | yes |
| API 429 per address | 0 | yes |
| API 429 per user (shared test users) | 219, all in the rush | not counted |
| Network errors | 2,809 connection resets, late in the run | no, see below |
| API p95 per minute (Railway) | at most 353 ms | |
| API CPU / memory peak (Railway) | 0.83 of 4 vCPU / 0.7 of 4 GB | |

What the failures were:

- **Reload burst and connection resets: the client, not the API.** While
  the reload reads took seconds, the API answered every minute at a p95 of
  353 ms or less. The time went to the test machine opening about a
  thousand new TLS connections in 10 s (k6 measured up to 10 s waiting for
  a connection and up to 8.7 s in TLS handshakes), and the resets came from
  the same machine and network. At a venue each phone opens its own
  connections, so this measures the test's one machine; the venue's Wi-Fi
  and router are the limit there, and nothing here can raise it.
- **Rush reads, 42 ms over: the test's data.** The check-in dialog reads
  the caller's songs, pairs and event submissions. Every check-in in the
  rush is the admin test user's, so those lists hold all 300 entries; a
  dancer's hold a few.
- **Check-ins placed: the test's users.** 79 of the 300 were refused by the
  per-user limit, because one test user sent all of them. A real rush is
  hundreds of users sending one each.

The API had headroom to spare: under a fifth of its CPU at peak. Run the test
again after a change to the session page's requests, the caches or the
limits.

## Discord notifications

The service posts to the fleet's shared Discord channels through
common-python-utils' `mini_app_polis.activity` and `mini_app_polis.discord`
(`src/api_deejaytools/services/notifications.py`). Every message's footer
says `api-deejaytools` and the environment. Only production posts the
change feed and faults; development and local log them instead.
"Song added" and "feedback" still post from development, with the title
starting `[DEVELOPMENT]` (or `[LOCAL]`) — `[DEVELOPMENT] song added` — so
they are not read as production's. Messages name people only where that
is the point (the dancers in "song added"); the caller of a request is
named by Clerk user id, never by email.

### Configuration

The webhook variables, `NOTIFY_DATA_CHANGES` and `DISCORD_NOTIFY_EVENTS`
are listed in [`.env.example`](../.env.example).

`DISCORD_NOTIFY_EVENTS` decides which messages each environment posts. It
maps an environment name (`ENVIRONMENT`) to the events that environment
sends, as JSON in a variable, in the same shape and with the same defaults
as api-kaianolevine-com's:

| Event | Message |
|---|---|
| `data_changed` | the summary of what a request committed, to `activity` |
| `fault` | every fault, a request's or background work's, to `errors` |

An event left out of an environment's list is not sent, and an environment
with no entry sends every event. The defaults (in `config.py`) send both in
production and only `fault` in development, where the writes are
contract-suite runs and test cleanup — but neither event posts outside
production whatever the list says, so development's entry has no effect. `NOTIFY_DATA_CHANGES` still applies:
a data change is posted only when both allow it. "Song added" and
"feedback" are not in the list and post as before. Don't silence a channel
by unsetting its webhook instead: it would fall back to
`DISCORD_WEBHOOK_URL`, which other channels share.

A channel without its own variable uses `DISCORD_WEBHOOK_URL`. With neither
set, that channel is off: nothing is sent, nothing fails, and the log says
`discord notifications off: no webhook for channel=…` once per channel. A
webhook pasted with GitHub's `/github` suffix works too. Discord's rate
limits are waited out: posts to a webhook go one at a time, paced by the
limit Discord reports, and a refused post is sent again when the limit
lifts. A post that would wait more than 10 s — the tail of a burst, or
anything sent during a minute-long Cloudflare block — is dropped and
logged, and the first drop of an episode is reported to Sentry.

### What posts where

**`activity`**

- **Song added**, one per song, when its Drive build succeeds (the commit
  that records the file), never for a build that fails or rolls back:

  > **song added**
  > Jane Doe added 'Ballad' (Classic, with John Smith)
  > [Drive file](https://drive.google.com/file/d/…/view)

  Uploaded for someone else ("Upload For"):
  `Org Anizer uploaded 'Ballad' for Jane Doe (Classic)`. A managed
  partnership reads `(ProAm, managed partnership Lee Lead & Fay Follow)`, a
  team upload `(Team, team Jt Swing)`, an "other" upload `(…, as Formation)`.
  Names are `First Last`, else the display name, else the user id; the
  routine is the routine name, else the song's display name (the filename).
- **Feedback**, one per piece of site feedback accepted (`POST /v1/feedback`):
  its type and subject, whether it had a screenshot, and whether it was
  emailed. Never the message, the sender's name or their email — those go
  by email only. Without a Brevo key or the feedback addresses the post
  says the message was not kept: it is the only sign the feedback arrived. A Brevo refusal is a
  `fault · 502` in `errors` instead, and no feedback post.

  > **feedback · bug**
  > Queue froze
  > emailed to the maintainer

- **Data changed**, one per request that committed anything worth saying:
  the tables it changed with `+` created, `~` updated, `-` deleted rows and
  `*` statements (row count unknown), and in the footer the method, path and
  caller's name: their first and last name from `users`, or their Clerk user
  id (`users.id`) when they have no name or the name cannot be read. Never
  their email or display name, which provisioning sets to the email: the
  channel is shared.

  > **data changed**
  > `teams` +1
  > POST /v1/teams · Jane Doe · api-deejaytools · production

**`errors`**

- **Every 5xx and unhandled exception** a request ends with (`fault · 500`,
  `fault · 503` for a deadline), naming the method and path, the caller,
  and the cause: the exception's type and Sentry event id
  (`RuntimeError · sentry 3f2a…`), an `ApiError` code
  (`ApiError CHUNK_ERROR`), or `deadline exceeded after 30000ms`. Never the
  exception's text, which for a database error is the statement. The web
  app still gets the same 500 envelope. No 4xx is posted: the only caller
  is the web app, and a person meeting a guard is not a fault.
- **Background faults** (`fault · background`):
  - `song build <song id> · failed, song removed`: a build of a song
    nothing referenced failed, so the song was deleted; its owner sees it
    vanish and must upload again.
  - `song build <song id> · gave up after 10 attempts`: a submitted or
    checked-in song's build is exhausted (see "Retrying failed song
    builds"). Its earlier failed attempts are not posted.
  - `drive job <job id> (<kind>) · gave up after 10 attempts`: a Drive job
    is `failed` for good (retry it with
    `POST /v1/admin/drive-jobs/{id}/retry`). Retries are not posted.
  - `scheduler · sessions`, `scheduler · song builds`,
    `scheduler · drive jobs`, `scheduler · event folder sweep`,
    `drive jobs · claim failed`: a scheduler step
    failed. Posted once per run of failures: the next is posted only after
    the step has succeeded again.

  Each names the exception's type and its Sentry event id (every one of
  these is reported to Sentry, with a `subsystem` tag); nothing is
  reported to Sentry twice for Discord's sake.

### What is left out, and why

| Left out | Why |
|----------|-----|
| `identity_audit_events` (suppressed table) | One row per authorization decision on every scoped request, the polled GETs included. It would turn the feed into the access log. |
| `song_uploads` (suppressed table) | Staged upload bytes, written with the song and deleted when its build finishes. The song is announced on its own. |
| `drive_jobs` (suppressed table, except under `/v1/admin/drive-jobs`) | Queue rows enqueued as a side effect of a submission or a delete, already reported under their own table, and churned by the scheduler. An admin's retry or rename backfill is reported. |
| Changes made by `POST /v1/auth/sync` | Every sign-in updates the users row and re-ensures the principal. Faults are still posted. |
| Changes made by `POST /v1/songs/upload/chunk` | The song (and a team or "other" upload's placeholder partner) is announced by its build instead, and only if the build succeeds. Faults are still posted. |
| Changes made by `GET /internal/tick` | The scheduler's own work, which the background loop does every 30 s unreported. Faults are posted by the scheduler. |
| Changes made under `/v1/checkins` and `/v1/queue` | The live floor: every check-in and withdrawal, and every manager queue action (promote, complete, incomplete, move down, withdraw). Dozens a minute at an event, each routine; they would bury everything else in the shared channel. Session status changes are still reported. Faults are still posted. |
| Changes made by `POST /v1/admin/checkins` and `DELETE /v1/admin/checkins/test` | The admin's synthetic test check-ins (a stub leader, partner, pair and check-in each) and their removal: test data, which would read in the shared feed as dancers arriving. Faults are still posted. |
| `/health`, `/version` | Polled by monitors; never posted, faults included. |

The legacy `POST /v1/songs` (a song record with no file and no build) gets
no "song added": it appears in the change feed as an ordinary change. The
web app's e2e suite uploads real songs to dev. This repo's conformance
suite runs against a local target with no Drive and uploads none; dev posts
faults only, so those songs are not announced.

### What it cannot see

- Writes made with `session.execute(text(...))` or outside an ORM session.
  The request routes write through the ORM, so they are all seen; the raw
  SQL in `services/drive_jobs/processor.py` runs in the background, which the change
  feed does not cover anyway.
- Anything background work changes (scheduler, song builds, Drive jobs):
  there is no request to tally it. What matters there is announced (song
  added) or reported (faults).
- Writes a request makes after its deadline answered `503`: the handler
  runs on, but the feed has already been posted.
- An exception after a response has started streaming (Sentry still has
  it), and a 5xx answered by the server before the app (a crash).
- Anything when Discord is down, and anything a rate limit holds for
  more than 10 s: delivery is fire-and-forget, logged and reported to
  Sentry when it fails. A short rate limit is waited out, not lost. The channel is not a record; the database and Sentry are.
