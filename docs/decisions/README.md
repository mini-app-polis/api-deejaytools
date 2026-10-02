# Decisions

The decisions that define this service were made in deejaytools-api, the
service it replaces, and live there: ADR-005 (wire contract baseline),
ADR-006 (the replacement), ADR-007 (authorization), ADR-008 (migrations)
and ADR-009 (wire contract and testing). They are not copied here, so there
is one place to change them.

The records here are decisions this repository made on its own, where those
ADRs leave room. Smaller choices of the same kind are listed in
[../DESIGN.md](../DESIGN.md).

| ADR | Decision |
|-----|----------|
| [ADR-0001](ADR-0001-durable-song-builds.md) | Song builds are durable, staged in `song_uploads` |
