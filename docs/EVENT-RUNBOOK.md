# Event-day runbook

What to do before, during and after an event that runs its floor trials on
deejaytools, and how staff keep the floor running if the app goes down.
The platform's measured capacity is in [OPERATIONS.md](OPERATIONS.md#capacity);
the queue's rules are in
[ADR-0006](decisions/ADR-0006-floor-trial-queue-model-as-built.md).

## The week before

1. **Sessions are right.** Each floor-trial session has its divisions, the
   priority ones ticked, sensible caps, and a check-in window that opens
   before the trial (the manager guide's "Before the event" covers each
   setting). A session's status can be set from Admin → Sessions.
2. **Staff have their roles.** Event managers are granted from Admin →
   Events → Managers; coordinators and DJs from the event's manage portal.
   A DJ or event manager is shared the event's Drive music folder when they
   get the role.
3. **Music is on the DJ's machine.** In Google Drive, the event's folder
   (`<season>/Events/<event name>/`, shared with each DJ) holds a copy of
   every song entered for the event. The DJ makes it available offline, or
   downloads it, the day before, so music doesn't depend on the venue's
   Wi-Fi.
4. **Alerts reach someone.** The `mini-app-polis-api-alerts` SNS
   subscription is confirmed (an unconfirmed one delivers nothing), and
   whoever is on call for the event watches the errors Discord channel and
   Sentry (`SENTRY_DSN_APIS`).
5. **Print the [fallback](#if-the-app-goes-down) page** and give it to the
   event managers and the desk.

## The day before

1. **Edge caching is hitting.** From any machine:

   ```bash
   B=https://api.deejaytools.com
   SID=$(curl -s "$B/v1/sessions" | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else '')")
   for i in 1 2; do curl -s -o /dev/null -D - "$B/v1/queue/$SID/active" | tr -d '\r' | grep -i '^cf-cache-status'; done
   ```

   The second request says `HIT`. `DYNAMIC` means the cache rule in infra's
   `cloudflare_rules.tf` isn't matching. A first `MISS`, or a second one on
   a quiet edge, is normal.
2. **Production is healthy.** On the CloudWatch dashboard
   `mini-app-polis-services`, the api-deejaytools row shows its usual
   latency and no 5xx; `mini-app-polis-data` shows the deejaytools database
   reporting. Railway shows api-deejaytools and Postgres-deejaytools
   running.
3. **No scale-up is needed.** The load test (OPERATIONS.md, Capacity) put
   3,000 phones on one replica at under a fifth of its 4 vCPU. If an event
   is much larger than that, raise the service's CPU limit in Railway, or
   its replica count (the response cache and its cross-worker invalidation
   work across replicas; the rate limit is per worker, so more workers
   loosen it slightly).
4. **Don't deploy during the event.** Merge nothing to `main` from the day
   before until the event is over. A deploy restarts the API mid-trial.

## During the event

Watch the `mini-app-polis-services` dashboard (api-deejaytools row), the
errors Discord channel and Sentry.

| You see | It means | Do |
| --- | --- | --- |
| p95 latency rising past a second, or the p95-latency alarm | The API or database is struggling | Check Railway's CPU and memory for api-deejaytools and the database row on `mini-app-polis-data`; raise the CPU limit or add a replica in Railway if CPU is at its limit. Search the Deploy Logs for "slow request" to see which routes |
| 5xx on the dashboard, the 5xx-rate alarm, errors in Discord or Sentry | Requests are failing | Sentry has the stack. One failing action can wait; widespread failures are the fallback below |
| Dancers or staff told to slow down, or a Cloudflare "1015" page | A rate limit refused them | The API's per-user limit is far above what a person sends. A whole venue blocked at once is Cloudflare's per-IP ceiling (`cloudflare_rules.tf`): raise `requests_per_period` there, or disable that rule in the dashboard for the day and put it back after |
| The queue page not changing for a minute | Normal: it polls once a minute, from a random start | Staff can press Refresh on Active Sessions |

## If the app goes down

*Print this page.*

The floor keeps running on paper. Nothing is lost: what's already in the
app stays there.

1. **Stop using the app for the queue** once it fails for more than a
   couple of minutes. Use the last queue you saw (a screenshot of Active
   Sessions helps).
2. **Write the queue on paper**, in three lists like the app's cards:
   Active (on deck now), Priority waiting, Standard waiting. New arrivals
   check in at the desk: write their names, division and song, and put them
   at the bottom of Priority if their division is a priority one and they
   haven't used its priority runs (the session's run limit, usually one),
   otherwise Standard.
3. **Run the floor as the app would.** Active holds as many entries as the
   session's caps. When a couple finishes, mark their run on the paper
   list, then move the next one up: Priority first, Standard only when
   Priority is empty. A false start goes to the bottom of Active.
4. **The DJ plays from the local copy** of the event's Drive folder (step 3
   of the week before), found by the song's name.
5. **When the app is back**, an event manager:
   - withdraws anyone in the app's queues who has left;
   - checks in, with Check In For, anyone who arrived during the outage and
     is still waiting, in the paper order;
   - keeps the paper list of runs danced during the outage. The app can't
     record a run after the fact, so those runs don't count toward priority
     in the app: staff apply the paper list by hand when someone asks for a
     priority slot in that division later in the event.

## After the event

1. Merging to `main` is fine again.
2. Note anything that went wrong, and any time the fallback was used, so
   the next event's runbook can be corrected.
