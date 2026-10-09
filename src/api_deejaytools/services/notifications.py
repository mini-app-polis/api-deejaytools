"""Discord notifications: the change feed, faults, and "song added".

The machinery is ``mini_app_polis.activity`` and ``mini_app_polis.discord``,
shared with the fleet's other APIs: SQLAlchemy listeners that tally what a
request committed, a middleware that posts one change summary per request
to the shared ``activity`` channel and every 5xx or unhandled exception to
``errors``, announcements that go out only when their transaction commits,
and background fault reports. Their module docstrings say what is and is not
seen. Importing this module imports ``activity``, which is what registers
the listeners.

What stays here is this service's policy:

- **Where messages come from.** Every message's footer says
  ``api-deejaytools``, because the channels are shared with the rest of the
  fleet. The actor is ``request.state.caller``, which ``auth.require_scope``
  stamps with the caller's Clerk user id (the principal's subject) — never
  their email, which does not belong in a shared channel; no extra query.
- **Titles say the environment** (``label=True`` on every producer here):
  outside production they read ``[DEVELOPMENT] fault · 500``, so a
  development fault in the shared ``errors`` channel is not mistaken for
  production's.
- **What is not news** (``SUPPRESSED_TABLES``, ``EXCLUDED_PATHS``,
  ``CHANGES_NOT_REPORTED``), each with its reason. ``NOTIFY_DATA_CHANGES``
  turns the change feed off altogether without a deploy, as in
  api-kaianolevine-com; faults and "song added" are unaffected.
- **No machine callers.** The only caller is the web app, so a 4xx is a
  person meeting a guard and is never reported (``is_machine`` is None).
- **Webhooks resolve from ``Settings``**, which also reads a ``.env``
  file, rather than straight from the environment. With none set,
  notifications are off: the first message dropped on each channel is
  logged, the rest silently, and nothing is posted.
- **Song added** (``announce_song_added``): one message per song, posted
  when its Drive build commits, never for a build that fails or rolls back.
- **Background faults** (``report_fault``, ``report_fault_once``): work with
  no request — the scheduler, song builds, Drive jobs — reports only what
  is final or what stops a whole step, never each retry.

Nothing here raises into a request or a job: a dropped notification is not
a failed request.
"""

from __future__ import annotations

import re
from typing import Any

from mini_app_polis import activity, discord
from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix

from ..config import Settings, get_settings

logger = get_logger()

SERVICE = "api-deejaytools"

#: Tables whose writes are bookkeeping rather than news. A request that
#: changed only these posts nothing; one that changed others lists only
#: the others.
SUPPRESSED_TABLES = frozenset(
    {
        # One row per authorization decision, allow and deny alike, on every
        # scoped request — the polled GETs included (the audit sink commits
        # it itself). Leaving it in would make the feed a copy of the access
        # log, and every real change would carry "+1" of it.
        "identity_audit_events",
        # The staged bytes of an upload, written with the song and deleted
        # when its build finishes. The song is what is news, and it is
        # announced on its own when its build succeeds.
        "song_uploads",
        # The Drive job queue: rows enqueued as a side effect of a
        # submission or a delete (already reported under their own table).
        # Failures that are final go to errors. An operator's own changes to
        # the queue are still reported (OPERATOR_QUEUE_PATHS).
        "drive_jobs",
    }
)

#: Paths outside the feed entirely, faults included. Liveness and version
#: are polled by uptime monitors and the deploy smoke test, write nothing,
#: and their failures are the monitors' job.
EXCLUDED_PATHS = ("/health", "/version")

#: Paths whose faults are reported but whose changes are not.
CHANGES_NOT_REPORTED = frozenset(
    {
        # Every sign-in upserts the users row (email, updated_at) and
        # re-ensures the principal and its dancer role: a change on every
        # login, not news.
        "/v1/auth/sync",
        # The final chunk creates the song (and, for a team or "other"
        # upload, its placeholder partner), but a song is only added once
        # its Drive build succeeds: a failed build deletes it again. The
        # build announces it then (announce_song_added), naming the
        # placeholder; a tally here would announce songs that never came
        # to exist. Earlier chunks write nothing but the audit row.
        "/v1/songs/upload/chunk",
        # The operator's scheduler pass: session status advances, queue
        # fills and Drive jobs, which the background loop runs every 30 s
        # without a tally. The same work is no more news when an operator
        # triggers it. Its failures are reported by the scheduler.
        "/internal/tick",
        # Synthetic check-ins for exercising the floor: a stub leader, its
        # partner, pair and check-in on every injection, and their removal.
        # Test data an admin made on purpose, reported in the shared feed
        # as if dancers had arrived. The admin who made it knows.
        "/v1/admin/checkins",
        "/v1/admin/checkins/test",
    }
)

#: Paths where a ``drive_jobs`` write is the point of the request — an
#: admin retrying an exhausted job or queueing renames — so the table is
#: reported there.
OPERATOR_QUEUE_PATHS = ("/v1/admin/drive-jobs",)

#: Discord embed colour for an announcement: the change feed's blue.
_ANNOUNCE_COLOR = 0x58A6FF

#: Channels already logged as having no webhook, so "off" is said once.
_unconfigured_logged: set[str] = set()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def webhook_source(settings: Settings) -> dict[str, str | None]:
    """The webhook variables as ``Settings`` resolved them (``.env`` included)."""
    names = (discord.FALLBACK_ENV, *discord.CHANNEL_ENV.values())
    return {name: getattr(settings, name, None) for name in names}


async def send(channel: str, payload: dict[str, Any], context: str) -> bool:
    """Post a built message to ``channel``'s webhook; never raises.

    The library's ``Sender``. With no webhook for the channel (its own
    variable and ``DISCORD_WEBHOOK_URL`` both unset), notifications are off:
    that is logged once per channel and the message is dropped.
    """
    url = discord.webhook_url(channel, source=webhook_source(get_settings()))
    if url is None:
        if channel not in _unconfigured_logged:
            _unconfigured_logged.add(channel)
            logger.warning(
                with_log_prefix(
                    LOG_WARNING,
                    f"discord notifications off: no webhook for channel={channel} "
                    "(DISCORD_WEBHOOK_URL unset)",
                )
            )
        return False
    return await discord.post_webhook(
        url, json=payload, channel=channel, context=context
    )


# ---------------------------------------------------------------------------
# Requests: change feed and faults
# ---------------------------------------------------------------------------


def _config(
    *,
    report_changes: bool = True,
    suppressed_tables: frozenset[str] = SUPPRESSED_TABLES,
) -> activity.ActivityConfig:
    settings = get_settings()
    return activity.ActivityConfig(
        service=SERVICE,
        environment=settings.ENVIRONMENT,
        suppressed_tables=suppressed_tables,
        excluded_paths=EXCLUDED_PATHS,
        report_changes=report_changes and settings.NOTIFY_DATA_CHANGES,
        send=send,
        label=True,
    )


_feed = activity.activity_middleware(lambda: _config())
_faults_only = activity.activity_middleware(lambda: _config(report_changes=False))
_operator_queue = activity.activity_middleware(
    lambda: _config(suppressed_tables=SUPPRESSED_TABLES - {"drive_jobs"})
)


async def activity_middleware(request: Any, call_next: Any) -> Any:
    """Report what the request committed, and any fault, on the way out.

    Registered outside the error middleware and the request limits, so it
    sees the status that went on the wire (a deadline's 503 included) and
    the fault detail ``errors.UnhandledErrorMiddleware`` records.
    """
    path = request.url.path
    if path in CHANGES_NOT_REPORTED:
        return await _faults_only(request, call_next)
    if activity.is_excluded(path, OPERATOR_QUEUE_PATHS):
        return await _operator_queue(request, call_next)
    return await _feed(request, call_next)


# ---------------------------------------------------------------------------
# Background faults
# ---------------------------------------------------------------------------


def report_fault(
    where: str, exc: BaseException, *, event_id: str | None = None
) -> None:
    """Post a fault from work with no request, without waiting on Discord.

    The message carries ``where``, the exception's type and the Sentry id —
    never its text. Pass ``event_id`` when the caller already reported the
    exception to Sentry; nothing is reported to Sentry here, so a fault the
    caller did not report names its type only and the log line has the rest.
    """
    activity.dispatch(
        activity.report_fault(
            where,
            exc,
            event_id=event_id,
            service=SERVICE,
            environment=get_settings().ENVIRONMENT,
            capture=None,
            send=send,
            label=True,
        )
    )


#: Fault keys currently failing, for ``report_fault_once``.
_failing: set[str] = set()


def report_fault_once(
    key: str, where: str, exc: BaseException, *, event_id: str | None = None
) -> None:
    """``report_fault`` for a step that repeats: once per run of failures.

    A scheduler step fails on every tick while its cause lasts (every 30 s),
    so only the first failure is posted; ``clear_fault`` after a success
    re-arms it.
    """
    if key in _failing:
        return
    _failing.add(key)
    report_fault(where, exc, event_id=event_id)


def clear_fault(key: str) -> None:
    """The step behind ``key`` succeeded: its next failure is news again."""
    _failing.discard(key)


def reset() -> None:
    """Forget failing steps and logged channels. For tests."""
    _failing.clear()
    _unconfigured_logged.clear()


# ---------------------------------------------------------------------------
# Song added
# ---------------------------------------------------------------------------

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]])")


def _md(value: str) -> str:
    """Escape Discord markdown in text people typed (names, routine names)."""
    return _MARKDOWN.sub(r"\\\1", value)


def person_name(first: str | None, last: str | None, *fallbacks: str | None) -> str:
    """``First Last``, else the first non-blank fallback, else ``someone``."""
    name = " ".join(p.strip() for p in (first, last) if p and p.strip())
    if name:
        return name
    for fallback in fallbacks:
        if fallback and fallback.strip():
            return fallback.strip()
    return "someone"


def drive_file_url(file_id: str) -> str:
    """The song file's Drive page."""
    return f"https://drive.google.com/file/d/{file_id}/view"


def song_added_text(
    *,
    owner: str,
    uploader: str | None,
    routine: str | None,
    division: str | None,
    partner: str | None = None,
    partner_kind: str | None = None,
    managed: str | None = None,
) -> str:
    """One sentence: who added what, in which division, with whom.

    ``owner`` is the dancer the song belongs to. ``uploader`` is set when
    someone else uploaded it for them ("Upload For"): then
    ``Uploader uploaded 'Routine' for Owner (…)``; otherwise
    ``Owner added 'Routine' (…)``. ``partner_kind`` is the partners row's
    kind: ``team`` and ``other`` are portal entries (a team name, a
    free-text entity), not a person dancing with the owner.
    """
    what = f"'{_md(routine)}'" if routine else "a song"
    if uploader:
        sentence = f"{_md(uploader)} uploaded {what} for {_md(owner)}"
    else:
        sentence = f"{_md(owner)} added {what}"
    details: list[str] = []
    if division:
        details.append(_md(division))
    if managed:
        details.append(f"managed partnership {_md(managed)}")
    elif partner and partner_kind == "team":
        details.append(f"team {_md(partner)}")
    elif partner and partner_kind and partner_kind != "partner":
        details.append(f"as {_md(partner)}")
    elif partner:
        details.append(f"with {_md(partner)}")
    if details:
        sentence += f" ({', '.join(details)})"
    return sentence


def announce_song_added(
    session: Any, text: str, *, drive_file_id: str | None = None
) -> None:
    """Post "song added" to activity when ``session``'s transaction commits.

    An embed rather than plain content: mentions inside an embed never
    ping, so an ``@everyone`` typed into a routine name stays text. Outside
    production the title carries the environment (``[DEVELOPMENT]``).
    """
    description = text
    if drive_file_id:
        description += f"\n[Drive file]({drive_file_url(drive_file_id)})"
    activity.announce_on_commit(
        session,
        discord.CHANNEL_ACTIVITY,
        embeds=[
            {
                "title": "song added",
                "color": _ANNOUNCE_COLOR,
                "description": description,
                "footer": {"text": f"{SERVICE} · {get_settings().ENVIRONMENT}"},
            }
        ],
        context="songs/added",
        send=send,
    )
