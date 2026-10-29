# Troubleshooting

Symptom-first guide to this service's problems: deploys, configuration, the database, Drive, email, the scheduler and CORS. Headings match **exact error text or observable behaviour**. Each entry: **Cause** → **Fix**. Problems a person meets in the web app (uploads, sign-in, sync) are in [deejaytools-com's troubleshooting guide](https://github.com/mini-app-polis/deejaytools-com/blob/main/docs/TROUBLESHOOTING.md).

This service's configuration lives in its own Doppler project (`api-deejaytools`: `dev` locally, `prd` synced to Railway), under the names in [`.env.example`](../.env.example), each a reference to the value in `mini-app-polis-ecosystem`. A variable the Railway service reads must also be listed in [`.railway/railway.ts`](../.railway/README.md), or the next apply removes it.

For procedures (retrying failed song builds, Discord notifications), see [`OPERATIONS.md`](OPERATIONS.md).

---

## Deploy fails at the migration step: `Neither DEEJAYTOOLS_DATABASE_URL_MIGRATIONS nor DEEJAYTOOLS_DATABASE_URL is set.`

**Symptom:** The API deploy never becomes healthy; the Railway deploy log ends with this line from `scripts/apply_migrations.py`.

**Cause:** The container's start command runs `apply_migrations.py`, then `backfill_principals.py`, then uvicorn, chained with `&&`. With no database URL the migration runner exits before the app starts. (The app itself would refuse to start too: `DEEJAYTOOLS_DATABASE_URL` is a required setting.)

**Fix:**

1. Check that `DEEJAYTOOLS_DATABASE_URL` is set on the Railway service (it references Railway's Postgres service; it is not in Doppler).
2. Redeploy.
3. Locally, export it in your shell before `doppler run` (api-deejaytools README, "Running locally").

`DEEJAYTOOLS_DATABASE_URL_MIGRATIONS` is optional. When it is unset the runner logs a warning that migrations run as the runtime role (OPS-002) and carries on with `DEEJAYTOOLS_DATABASE_URL`.

---

## Every authenticated call returns 401 `Authentication required`

**Symptom:** Signed-in users get **401** `{ "error": { "code": "UNAUTHORIZED", "message": "Authentication required" } }` on every authenticated endpoint. API logs show:

```
auth_failed: DEEJAYTOOLS_CLERK_ISSUER / DEEJAYTOOLS_CLERK_JWKS_URL unset
```

**Cause:** The API trusts exactly one Clerk issuer, configured by `DEEJAYTOOLS_CLERK_ISSUER` and `DEEJAYTOOLS_CLERK_JWKS_URL`. With either unset, every bearer token is refused with a 401 (not a 500).

Other `auth_failed` lines name the reason: `auth_failed: missing_token` (no `Authorization: Bearer …` header) or `auth_failed: <verifier error>` (bad or expired token, wrong issuer, JWKS unreachable). Read the verifier error when a rotated key or an unreachable JWKS looks like a run of bad tokens.

**Fix:**

1. Set both in Doppler for the environment, e.g. `DEEJAYTOOLS_CLERK_ISSUER=https://clerk.deejaytools.com` and `DEEJAYTOOLS_CLERK_JWKS_URL=https://clerk.deejaytools.com/.well-known/jwks.json`.
2. Confirm the web app's `DEEJAYTOOLS_CLERK_PUBLISHABLE_KEY` belongs to the same Clerk instance; tokens from another instance carry a different issuer and are refused.
3. Redeploy the API and retry.

---

## Postgres unreachable / API 500s on every database route

**Symptoms (typical):**

- API answers **500** `INTERNAL` on routes that touch the database, while `/health` still answers **200** `{"status":"ok"}`.
- API log: `unhandled <ExceptionType> on <METHOD> <path>`; the same faults appear as `fault · 500` in the Discord `errors` channel and in Sentry.

**Cause:** Wrong `DEEJAYTOOLS_DATABASE_URL`, database down, or network trouble. `/health` is liveness only: it never queries the database, so it does not turn 503 when Postgres is down.

**Fix:**

1. Verify `DEEJAYTOOLS_DATABASE_URL` (host, port, user, password, database name). Plain `postgresql://` or `postgres://` URLs are fine; the app switches them to asyncpg.
2. Confirm the Railway Postgres service is healthy.
3. Read the exception type in the log line or its Sentry event (query parameters are deliberately left out of both).

There are no pool-tuning environment variables; the engine uses SQLAlchemy's defaults with `pool_pre_ping`.

---

## Missing table / relation errors (`relation "…" does not exist`)

**Symptom:** API 500s; Sentry shows a Postgres error mentioning a missing relation (e.g. `sessions`, `users`, `songs`, `song_uploads`).

**Cause:** Migrations were not applied to this database. Common after pointing `DEEJAYTOOLS_DATABASE_URL` at a fresh DB or restoring an old snapshot.

**Fix:**

1. **Production:** every deploy runs `scripts/apply_migrations.py` before the app starts. If you restored the DB by hand, redeploy. A failing migration stops the start command, so the new deployment never passes its `/health` healthcheck; fix the migration and redeploy.
2. **Local**, from the api-deejaytools checkout: `doppler run -- uv run python scripts/apply_migrations.py`, then `doppler run -- uv run python scripts/backfill_principals.py`.
3. An existing database adopted for the first time needs the one-off bootstrap (`BOOTSTRAP_MIGRATIONS` / `BOOTSTRAP_EXCLUDE`) in the api-deejaytools README, "Database and migrations".

---

## `address already in use` on port 3001

**Symptom:** The API fails to start locally; uvicorn cannot bind to port **3001**.

**Cause:** Another process (often a previous uvicorn run of api-deejaytools) is bound to `3001`.

**Fix:**

1. Find the process: `lsof -i :3001` (macOS/Linux).
2. Stop it: `kill <PID>` or stop the other terminal session.
3. Or run uvicorn with a different `--port` and point `DEEJAYTOOLS_API_DEV` (the Vite dev-server proxy target) at it.

Production on Railway uses the injected `PORT`, so this error is almost always local dev.

---

## Uploaded songs never get a Drive file (`drive_work_skipped`)

**Symptom:** Uploads complete in the browser and the song appears, but it never gets a Drive file, and Drive jobs (renames, event copies, trash) stop moving. API log, once per process:

```
drive_work_skipped: Google Drive is not configured; song builds and Drive jobs stay queued
```

**Cause:** `GOOGLE_CREDENTIALS_JSON` or `GOOGLE_DRIVE_PARENT_FOLDER_ID` is missing or empty, or `GOOGLE_CREDENTIALS_JSON` is not a key file with both `client_email` and `private_key`. The scheduler then skips song builds and Drive jobs rather than failing them, so nothing burns its retries. A Drive call made anyway fails with `Google Drive environment variables are not configured`.

**Fix:**

1. Set both in Doppler (see api-deejaytools' `.env.example`). `GOOGLE_CREDENTIALS_JSON` is the fleet's one service-account key file, the same value the cogs read.
2. Redeploy. Queued builds and jobs run on the next scheduler passes; nothing needs re-uploading.

---

## Google service-account key file

**Symptom:** Drive auth fails at runtime with PEM / key-parsing errors; song builds fail (`song_background_upload_failed song=<id>: …`).

**Cause:** `GOOGLE_CREDENTIALS_JSON` holds something other than the JSON key file Google issued: a bare PEM, an edited file, or a key that was since deleted in Google Cloud.

**Fix:**

1. Paste the whole downloaded key file. Either form of the private key's line breaks works: `\n` escapes as Google writes them, or real newlines as Doppler may store them.
2. Redeploy. Builds kept for retry pick up again on their own; any that gave up can be requeued (api-deejaytools OPERATIONS.md, "Retrying failed song builds").

---

## Drive folder not shared with service account

**Symptom:** Song builds fail with a Google API **403** / **404** (e.g. "File not found", "Insufficient permissions") when creating subfolders or uploading under `GOOGLE_DRIVE_PARENT_FOLDER_ID`.

**Cause:** The parent folder exists in your personal Drive but the **service account email** was never granted access. The API uses the `drive.file` scope.

**Fix:**

1. In Google Drive, share `GOOGLE_DRIVE_PARENT_FOLDER_ID` with the service account (the key file's `client_email`) as **Editor** (or Content manager on shared drives).
2. Re-upload songs that were removed (below); requeue builds that gave up (api-deejaytools OPERATIONS.md).

---

## Song record disappears shortly after a "successful" upload

**Symptom:** Upload UI completes; the song briefly appears, then is gone.

**Cause:** The upload is accepted and its bytes are staged in `song_uploads`; the Drive build runs afterwards on the scheduler. When a build fails and **nothing references the song yet** (no event submission, no check-in), the song is deleted and the uploader has to upload again. Log: `song_background_upload_failed song=<id>: …`; Discord `errors`: `song build <id> · failed, song removed`; a Sentry event tagged `subsystem=song_builds`.

A song that **is** referenced cannot be deleted. Its build is retried with backoff (`song_cleanup_delete_failed song=<id>: build will retry`, then `song_build_retrying song=<id> attempts=<n>`) and gives up after 10 attempts (`song_build_exhausted`; Discord `song build <id> · gave up after 10 attempts`).

**Fix:** Read the exception on `song_background_upload_failed` or its Sentry event (Drive credentials, folder sharing, tagging, DB). Fix the root cause, re-upload removed songs, and requeue exhausted builds with the SQL in [`OPERATIONS.md`](OPERATIONS.md).

---

## Feedback form succeeds but no email arrives

**Symptom:** User sees success on `/feedback`; API returns **201**; nothing in inbox.

**Cause:** No Brevo key, or no feedback address. The API reads `BREVO_API_KEY`, the fleet's one Brevo key, and the sender and recipient from `FEEDBACK_FROM_EMAIL` and `FEEDBACK_TO_EMAIL`. Without the key it logs:

```
feedback BREVO_API_KEY not set; skipping transactional email
```

and without either address:

```
feedback FEEDBACK_FROM_EMAIL or FEEDBACK_TO_EMAIL not set; skipping transactional email
```

and still returns success, by design. The Discord `activity` post for that feedback says the message was not kept; it is the only sign the feedback arrived. (Production does not start without both addresses.)

**Fix:**

1. Set whichever is missing in the service's Doppler config: `BREVO_API_KEY` (Brevo → SMTP & API → API Keys), `FEEDBACK_FROM_EMAIL`, `FEEDBACK_TO_EMAIL`.
2. Redeploy.
3. Submit test feedback.

---

## `Failed to send email. Please try again.`

**Symptom:** Feedback form error; API **502**, code `EMAIL_FAILED`.

**Cause:** A Brevo key is set but Brevo rejected the send. API log: `feedback brevo_error status=<status> body=<body>`; Discord `errors` gets a `fault · 502`.

**Fix:** Verify the Brevo API key, sender domain verification, and recipient limits in the Brevo dashboard.

---

## Sessions not advancing status / active queue not filling

**Symptoms:**

- Session stays `scheduled` past check-in open time in the DB (the UI may still **look** correct; see below).
- Active queue empty at floor-trial start; manager "Active" panel never fills.
- No `session_status_updated` or `auto_fill_completed` in logs.

**Cause:** The in-process scheduler is not running, or its pass is failing.

**Fix:**

1. After a deploy, look for **`scheduler_started interval_ms=<n>`**. If you see **`scheduler_disabled disable_scheduler=… env=…`** instead, the scheduler is off:
   - **`DISABLE_SCHEDULER`**: only the literal string **`"1"`** disables it. `"true"`, `"yes"`, `"0"` etc. leave it **on**.
   - **`ENVIRONMENT=test`** also turns it off.
2. **`TICK_INTERVAL_MS`**: default **30000** (30 s). A pass also runs once at startup.
3. A healthy scheduler logs **`tick_completed sessions_checked=<n> sessions_updated=<n>`** and **`auto_fill_completed …`** on every pass. Look for **`tick_failed`** (session work), **`auto_fill_failed session_id=<id>`** (one session), **`song_builds_tick_failed`** and **`drive_jobs_tick_failed`**. A failing step is also posted to Discord `errors` once per run of failures (`scheduler · sessions`, `scheduler · song builds`, `scheduler · drive jobs`).
4. Manual pass: `GET /internal/tick` with header `x-tick-secret: <TICK_SECRET>`; it answers `{ "data": { "ticked": true }, … }` once the pass has finished.

**Reassurance:** Session **display** status on reads is **derived from the wall clock** in API responses (`derive_status` in api-deejaytools `routers/sessions.py`), so the Floor Trials UI can show "check-in open" even when the DB `status` column lags. What actually stops is **DB status transitions** and **automatic active-queue fill**, plus song builds and Drive jobs, which run on the same pass. Check-ins and manual manager actions still work.

---

## CORS error in browser (`Access-Control-Allow-Origin`)

**Symptom:** Browser console blocks a fetch to the API; preflight or response missing CORS headers.

**Cause:** The frontend origin is not in the API's `DEEJAYTOOLS_CORS_ORIGINS`. Unset, it allows only `http://localhost:5173`. The legacy unprefixed `CORS_ORIGINS` is **not** read.

**Fix:**

1. Set `DEEJAYTOOLS_CORS_ORIGINS` in Doppler to a comma-separated list including every frontend origin (production domain, `www`, Cloudflare preview URLs if needed).
2. Redeploy the API.
3. Ensure `DEEJAYTOOLS_API` (or `DEEJAYTOOLS_API_DEV` on previews) points at the same API host the browser calls.

---

## `GET /internal/tick` returns 403 `Admin access required`

**Symptom:** HTTP **403** `FORBIDDEN` with message `Admin access required` when calling `/internal/tick`.

**Cause:** The `x-tick-secret` header is missing or does not match `TICK_SECRET`, **or `TICK_SECRET` is unset**: the route fails closed and refuses every call when no secret is configured. An empty `TICK_SECRET` counts as a set secret, so leave it unset rather than blank.

**Fix:** Set `TICK_SECRET` to a long random string in Doppler and send `x-tick-secret: <exact value>` from whatever calls the route. The in-process scheduler does not use the route; it is an operator override.

---

## Useful log events

The API logs plain-text lines through the shared `mini_app_polis.logger`: an event name, then `key=value` details or `: <error>`. Grep the Railway log stream for these:

| Event | Meaning |
|-------|---------|
| `api-deejaytools starting (env=…, sentry=on\|off)` | Process start; confirms the environment and whether Sentry is configured. |
| `scheduler_started interval_ms=<n>` | In-process scheduler running. **Should appear once per process start** if enabled. |
| `scheduler_disabled disable_scheduler=… env=…` | Scheduler off (`DISABLE_SCHEDULER=1` or `ENVIRONMENT=test`). |
| `scheduler_stopped` | Scheduler stopped at shutdown. |
| `api-deejaytools shutting down` | Graceful shutdown. |
| `tick_completed` | Status pass finished; `sessions_checked`, `sessions_updated`. Confirms the loop is alive even when nothing changed. |
| `tick_failed` | Session status / auto-fill step threw; the pass goes on to Drive work. |
| `session_status_updated` | DB session row moved (e.g. `scheduled` → `checkin_open`); `session_id`, `status`. |
| `auto_fill_completed` | Queue auto-fill sweep finished; `sessions_checked`, `entries_promoted`. |
| `auto_fill_failed` | Auto-fill failed for one session; `session_id`. |
| `drive_work_skipped` | Drive not configured; builds and Drive jobs wait. Logged once per process. |
| `song_builds_tick_failed` / `drive_jobs_tick_failed` | The song-build or Drive-job step of a pass threw. |
| `auth_failed` | `missing_token`, unset Clerk issuer/JWKS, or the verifier's error. |
| `auth_sync_email_conflict` | `/v1/auth/sync` refused: email belongs to another account. |
| `unhandled <Type> on <METHOD> <path>` | Uncaught exception in a route; answered 500, reported to Sentry and Discord. |
| `handler failed after its deadline` | A handler that already got a 503 later raised. |
| `song_background_upload_failed` | A song's Drive build failed; the song is removed or retried (see above). |
| `song_build_retrying` / `song_build_exhausted` | Build kept for retry / gave up after 10 attempts. |
| `song_cleanup_delete_failed` | Song could not be removed after a failed build (usually because it is referenced); the build will retry. |
| `song_drive_share_failed` | Could not share the Drive file with the uploader/partner emails. |
| `song_drive_share_partial_failure` | Some share emails failed; see `failed=`. |
| `chunk_readdir_failed` | Could not list the temp chunk directory during finalize. |
| `feedback brevo_error` | Brevo refused a feedback email. |
| `discord notifications off: no webhook for channel=…` | A Discord channel has no webhook configured (logged once per channel). |

### Route-level events (grep when a specific action breaks)

| Event | Route / area |
|-------|----------------|
| `admin_event_song_submission_list_failed` | Admin event submissions list |
| `admin_checkin_inject_failed` | Admin test check-in inject |
| `admin_self_demote_blocked` | Admin tried to change their own role |
| `checkin_create_failed` | Check-in POST |
| `checkin_self_withdraw_failed` | Self withdraw from queue |
| `checkin_song_not_submitted` | Check-in rejected: song not submitted to event |
| `event_entity_list_failed` | Event entities list |
| `event_song_submission_list_failed` | List own submissions |
| `event_song_submission_create_failed` | Submit song to event |
| `event_song_submission_delete_failed` | Delete submission |
| `managed_partnership_create_failed` | Create managed partnership |
| `managed_partnership_update_failed` | Update managed partnership |
| `managed_partnership_delete_failed` | Delete managed partnership |
| `queue_promote_failed` | Manager/admin queue promote |
| `queue_promote_blocked` | Promote rejected (business rule) |
| `queue_complete_failed` | Mark queue entry complete |
| `queue_incomplete_failed` | Mark entry incomplete |
| `queue_move_down_failed` | Move down in queue |
| `queue_withdraw_failed` | Withdraw from queue |
| `session_divisions_put_failed` | Admin session divisions update |
| `song_delete_failed` | Song delete |
| `team_create_failed` | Team create |
| `drive_jobs_list_failed` / `drive_jobs_summary_failed` | Admin Drive jobs views |
| `drive_rename_backfill_failed` | Admin rename backfill |

Faults (5xx, unhandled exceptions, background failures) also go to Sentry, through the Python SDK with the `SENTRY_DSN_APIS` DSN (the shared `apis` project; events tagged `service=api-deejaytools`), and to the Discord `errors` channel. The browser app reports to its own Sentry project via `SENTRY_DSN_DEEJAYTOOLS` (see [`DEPLOYMENT.md`](https://github.com/mini-app-polis/deejaytools-com/blob/main/docs/DEPLOYMENT.md)).

---

## Related docs

- [README](../README.md): endpoints and their scopes, running locally, migrations, deployment
- [`OPERATIONS.md`](OPERATIONS.md): retrying failed song builds, Discord notifications
- [deejaytools-com `docs/TROUBLESHOOTING.md`](https://github.com/mini-app-polis/deejaytools-com/blob/main/docs/TROUBLESHOOTING.md): problems seen in the web app
