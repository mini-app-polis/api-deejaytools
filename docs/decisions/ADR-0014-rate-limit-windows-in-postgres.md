# ADR-0014: Rate-limit windows are kept in Postgres, shared by every worker

**Date:** 2026-11-15
**Status:** Accepted
**Repo:** api-deejaytools

---

## Context

`RateLimitMiddleware` kept its fixed windows in each process's memory. The
Dockerfile runs uvicorn with `WEB_CONCURRENCY=4`, and connections are spread
across the workers, so each limit was up to four times what it said: on
2026-11-15 one client sent nine `POST /v1/feedback` requests in 17 seconds
against a limit of five a minute, and all nine reached the handler. A second
replica would multiply it again.

The limits have to hold for the whole service: per user for signed-in
callers and per address for anonymous ones (ADR-0013), across every worker
and replica.

## Decision

The windows are kept in Postgres, in `rate_limit_windows` (migration 013),
through `PostgresWindows` in `src/api_deejaytools/middleware.py`:

- **One statement per count.** An upsert on the caller's key starts a new
  window when the old one has ended and otherwise adds one, returning the
  window. Every request in a window is counted, refused ones too, so a
  request is allowed when the returned count is at or under the limit. A
  fixed window refuses the same requests either way.
- **Wall-clock time.** `now` is epoch seconds (`time.time`), the same in
  every process, where the in-memory windows used `time.monotonic`.
- **An unlogged table.** A window lasts a minute; losing them in a crash
  only resets the limits. Expired windows are deleted at most once a window
  per process.
- **Fail open.** If the database cannot be reached the request is let
  through and a warning is logged. A limit is not worth an outage, and the
  request would most likely fail on the database anyway.

`MemoryWindows`, the old behaviour, stays as the default for a middleware
built without a store: one process's view, for unit tests.

## Alternatives considered

- **One worker.** Exact, but gives up the concurrency the event capacity
  target (ADR-0013) relies on.
- **Dividing each limit by the worker count.** Cheap, but wrong whenever
  traffic is uneven across workers, and wrong again with a second replica.
- **A file or shared memory in the container.** Shared by the workers but
  not by replicas.
- **Redis.** The usual tool, but a new service to run and pay for, for one
  counter.

## Consequences

- A counted request costs one database round trip. Anonymous reads of the
  public cached routes are not counted (ADR-0013), so at an event most
  traffic never reaches the table; signed-in requests and writes do. This
  is checked by the event load test (`load/event.k6.js` in deejaytools-com)
  like any other change to the request path.
- The limits are now the service's, not a worker's: the numbers in
  `docs/API.md` and `docs/OPERATIONS.md` mean what they say.
- Many requests under one key update the same row. The only key shared by
  many people is an anonymous venue address on a non-public route, which is
  rare at an event.
