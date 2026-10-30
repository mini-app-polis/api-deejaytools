# ADR-0004: Clerk verification: session JWTs only, no M2M

Date: 2026-04-24

## Status

Accepted. Revised Sep 2026: machine callers are **out of scope**, not
deferred. The API serves the deejaytools.com web app only and there are no
plans for machines to call it. ecosystem-standards split the receiver side
of CD-019 into CD-029 (verifying Clerk sessions) and CD-030 (verifying
named machine keys); this API is held to CD-029 and records CD-030 as an
**exemption** in `evaluator.yaml`. The JWT-only implementation is
unchanged. Should a machine caller ever be planned, the exemption goes and
the work described in the note below applies.

> **Superseded in part (Sep 2026).** This ADR was written against
> ecosystem-standards CD-012, which required the Clerk M2M opaque-token
> path. CD-012 was retired and replaced by CD-019: machine callers now
> hold one named API key each, verified locally by constant-time
> comparison against configuration, with no Clerk involvement and no
> remote verification call. See ecosystem-standards ADR-0008.
>
> What this means here: the machine half is now CD-030, exempted in
> `evaluator.yaml` since the Sep 2026 revision above, and the
> implementation shape described in **Trigger to revisit** and
> **Consequences** below is out of date. There is no BAPI call to add
> and no `CLERK_SECRET_KEY` to introduce. The work is a machine-key
> verifier alongside the JWT one, routed structurally by dot count.

## Context

Written in deejaytools-api, the Node service this one replaced (archived as
legacy-graveyard/deejaytools-api); in this record "this service", "this
API" and "this repository" mean deejaytools-api.

ecosystem-standards CD-012 requires API services to verify both:
1. Clerk session JWTs (RS256, verified locally against JWKS) — for
   human callers.
2. Clerk M2M opaque tokens (verified remotely via Clerk's BAPI
   `m2m_tokens/verify` endpoint) — for cog/service callers.

The deejaytools-com ecosystem has no machine callers today:
- The API serves the React app at deejaytools.com (browser-only).
- The Python cogs (deejay-cog, evaluator-cog, watcher-cog,
  notes-ingest-cog) push to api-kaianolevine-com, not to this API.
- There is no scheduled job, third-party integration, or sibling
  TypeScript service that calls this API server-to-server.

Adding M2M verification code that nothing exercises would be future
drift surface — code that won't be tested in production traffic and
may rot before its first real caller.

## Decision

Implement only the JWT path. `src/middleware/auth.ts` calls
`verifyClerkToken` from `common-typescript-utils` against the JWKS
document at `CLERK_JWKS_URL`. CD-012 is recorded as a deferral in
`evaluator.yaml`, not an exemption — the rule still applies,
the gap is real, and the trigger to remediate is well-defined.

## Trigger to revisit

None of these is planned. Add a machine path only if one becomes true:
- A Python cog needs to write to deejaytools-api.
- A TypeScript service in the ecosystem needs to make M2M calls and
  `common-typescript-utils` ships an M2M helper.
- Any non-browser caller (scheduled job, third-party webhook, etc.)
  is introduced.

## Consequences

**Easier:**
- One verification path to maintain and test.
- No `CLERK_SECRET_KEY` required on the API service.

**Harder:**
- When an M2M caller appears, this is the first thing that has to
  change. The deferral entry plus this ADR document the implementation
  shape: token format discrimination by dot count (JWTs have two,
  opaque tokens have none); add the BAPI verify call; extract `sub`
  from both paths uniformly.

## References

- ecosystem-standards CD-019 (Bearer credential contract) — replaced CD-012;
  CD-029 / CD-030 (its receiver side, split Sep 2026)
- ecosystem-standards ADR-0008 (named machine keys as the machine identity)
- evaluator.yaml — CD-030 exemption
- src/middleware/auth.ts — JWT-only implementation
