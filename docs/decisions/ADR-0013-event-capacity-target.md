# ADR-0013: The event capacity target is thousands of dancers behind one IP

**Date:** 2026-11-02
**Status:** Accepted
**Repo:** api-deejaytools

---

Recorded from the 2026-11-01 capacity review (api-deejaytools 22b6a34,
deejaytools-com b4c6d93, infra dev).

## Context

The per-IP limits in the API (300 requests a minute on `/v1/*`) and at
Cloudflare suit a public API whose callers are spread across addresses.
They contradict how the service is used at an event: every dancer is on
their own phone, and all of them are on the venue's one Wi-Fi, so the
whole event reaches the API from a single address.

## Decision

The service is sized for an event of 3,000 dancers on their own phones
behind one venue IP:

- **Steady state:** 3,000 phones x 3 requests a minute, about 150 requests
  a second from one address.
- **Reload burst:** 1,000 phones reloading the session page within about
  10 s at about 10 requests each, about 1,000 requests a second from one
  address.

What the target requires:

- **No per-IP limits on public reads.** An address is not a person at an
  event. This holds in the API and at Cloudflare.
- **Shared caching of public reads.** A read that is the same for every
  caller is cached once and served to all of them, at Cloudflare or the
  API, so the burst does not become 1,000 requests a second at the
  database.
- **Per-user limits for signed-in callers.** A limit on a personalised
  read or a write keys on the signed-in user, never on the address.

It is checked by the load test, which drives the steady and burst rates
above from one source address and must pass. A change that adds a limit or
a personalised read is checked against this target before it ships.

## Consequences

- The per-IP rate limit and its client-address keying (docs/OPERATIONS.md,
  "Client address") are not right for public reads and are to be removed or
  replaced for them. Abuse of a public read is handled by caching, not by
  limiting an address.
- A new read is either public and cacheable, or personalised and limited
  per user. A personalised read on the session page multiplies by every
  phone, so it needs that thought before it is added.
- Public reads cannot carry per-caller content; anything per caller moves
  to a signed-in route.
