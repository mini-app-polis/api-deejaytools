# Decisions

ADR-0002 to ADR-0010 were written in deejaytools-api, the service this one
replaced, and carry one line saying so in their Context. The ones that
define this service are ADR-0006 (floor-trial queue model, as built),
ADR-0007 (the service), ADR-0008 (authorization), ADR-0009 (migrations),
ADR-0010 (wire contract and testing) and ADR-0012 (per-event staff roles).
Smaller choices of the same kind are listed in [../DESIGN.md](../DESIGN.md).

Files are named `ADR-NNNN-short-slug.md` (ecosystem-standards DOC-005). An
ADR in another repository is cited with that repository's name first, as in
`ecosystem-standards ADR-0012`.

| ADR | Decision |
|-----|----------|
| [ADR-0001](ADR-0001-durable-song-builds.md) | Song builds are durable, staged in `song_uploads` |
| [ADR-0002](ADR-0002-drizzle-migrations-at-deploy.md) | Run Drizzle migrations at Railway deploy time — superseded by ADR-0009 |
| [ADR-0003](ADR-0003-validation-envelope-shape.md) | Validation 400 responses use the canonical error envelope |
| [ADR-0004](ADR-0004-jwt-only-clerk-verification.md) | Clerk verification: session JWTs only, no M2M |
| [ADR-0005](ADR-0005-floor-trial-queue-model.md) | Floor-trial queue model — superseded by ADR-0006 |
| [ADR-0006](ADR-0006-floor-trial-queue-model-as-built.md) | Floor-trial queue model, as built |
| [ADR-0007](ADR-0007-replacement-api-fastapi-on-existing-database.md) | Replacement API: a FastAPI service on the existing database |
| [ADR-0008](ADR-0008-replacement-api-identity-authorization.md) | Replacement API: authorization through the identity library |
| [ADR-0009](ADR-0009-replacement-api-raw-sql-migrations.md) | Replacement API: numbered raw-SQL migrations from a baseline |
| [ADR-0010](ADR-0010-replacement-api-contract-and-testing.md) | Replacement API: wire contract, reuse, and how it is tested |
| [ADR-0011](ADR-0011-season-year-rolls-over-october-1.md) | The season year rolls over on October 1 |
| [ADR-0012](ADR-0012-per-event-staff-roles.md) | Per-event staff roles: coordinator, DJ and event manager |
